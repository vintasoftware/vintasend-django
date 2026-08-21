import hashlib
import shutil
import tempfile

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase, override_settings


MIGRATE_FROM = (
    "vintasend_django",
    "0004_notification_git_commit_sha_notification_read_at_and_more",
)
MIGRATE_TO = ("vintasend_django", "0006_delete_attachment")


class AttachmentDataMigrationTest(TransactionTestCase):
    """The 0005 data migration must carry legacy ``Attachment`` rows (and their files) forward.

    Dropping the old table without this would silently destroy every attachment a pre-2.0
    deployment had stored, which is why 0004 defers the delete and 0005 copies the data first.
    """

    def setUp(self):
        self._media_root = tempfile.mkdtemp()
        self._media_override = override_settings(MEDIA_ROOT=self._media_root)
        self._media_override.enable()

    def tearDown(self):
        # Restore the schema to the latest migration so later tests see the 2.0 tables.
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        self._media_override.disable()
        shutil.rmtree(self._media_root, ignore_errors=True)

    def test_legacy_attachment_is_migrated_and_file_preserved(self):
        # Roll the schema back to before the data copy: the old Attachment table exists and the
        # new tables are present but empty.
        executor = MigrationExecutor(connection)
        executor.migrate([MIGRATE_FROM])
        old_apps = executor.loader.project_state([MIGRATE_FROM]).apps

        Notification = old_apps.get_model("vintasend_django", "Notification")
        Attachment = old_apps.get_model("vintasend_django", "Attachment")

        notification = Notification.objects.create(
            notification_type="EMAIL",
            title="Legacy",
            body_template="body",
            context_kwargs={},
        )
        attachment = Attachment(
            notification=notification,
            name="legacy.txt",
            mime_type="text/plain",
        )
        attachment.file.save("legacy.txt", ContentFile(b"legacy bytes"), save=False)
        attachment.size = attachment.file.size
        attachment.save()
        stored_name = attachment.file.name
        assert default_storage.exists(stored_name)

        # Apply the data migration + the delete.
        executor = MigrationExecutor(connection)
        executor.migrate([MIGRATE_TO])
        new_apps = executor.loader.project_state([MIGRATE_TO]).apps

        AttachmentFileRecord = new_apps.get_model("vintasend_django", "AttachmentFileRecord")
        NotificationAttachment = new_apps.get_model("vintasend_django", "NotificationAttachment")

        # One file record + one join row, and the underlying file is untouched.
        assert AttachmentFileRecord.objects.count() == 1
        record = AttachmentFileRecord.objects.get()
        assert record.filename == "legacy.txt"
        assert record.content_type == "text/plain"
        # Size comes from the stored Attachment.size; the migration never reads the file, so the
        # checksum is left empty (migrated files join dedup only once re-uploaded).
        assert record.size == len(b"legacy bytes")
        assert record.checksum == ""
        assert record.storage_identifiers.get("name") == stored_name
        assert default_storage.exists(stored_name)

        join_row = NotificationAttachment.objects.get()
        assert join_row.file_id == record.pk
        assert join_row.notification_id == notification.pk

        # The legacy table is gone only after the copy ran.
        with self.assertRaises(LookupError):
            new_apps.get_model("vintasend_django", "Attachment")

    def test_multiple_legacy_attachments_are_bulk_migrated(self):
        executor = MigrationExecutor(connection)
        executor.migrate([MIGRATE_FROM])
        old_apps = executor.loader.project_state([MIGRATE_FROM]).apps

        Notification = old_apps.get_model("vintasend_django", "Notification")
        Attachment = old_apps.get_model("vintasend_django", "Attachment")

        notification = Notification.objects.create(
            notification_type="EMAIL",
            title="Many",
            body_template="body",
            context_kwargs={},
        )
        expected_names = set()
        for i in range(5):
            attachment = Attachment(
                notification=notification,
                name=f"file_{i}.txt",
                mime_type="text/plain",
            )
            attachment.file.save(f"file_{i}.txt", ContentFile(f"bytes {i}".encode()), save=False)
            attachment.size = attachment.file.size
            attachment.save()
            expected_names.add(attachment.name)

        executor = MigrationExecutor(connection)
        executor.migrate([MIGRATE_TO])
        new_apps = executor.loader.project_state([MIGRATE_TO]).apps
        AttachmentFileRecord = new_apps.get_model("vintasend_django", "AttachmentFileRecord")
        NotificationAttachment = new_apps.get_model("vintasend_django", "NotificationAttachment")

        assert AttachmentFileRecord.objects.count() == 5
        assert NotificationAttachment.objects.count() == 5
        assert {r.filename for r in AttachmentFileRecord.objects.all()} == expected_names
        # Every join row points at a distinct file record on the same notification.
        assert all(
            na.notification_id == notification.pk for na in NotificationAttachment.objects.all()
        )
        assert len({na.file_id for na in NotificationAttachment.objects.all()}) == 5

    def test_migration_reverse_restores_attachment(self):
        # Forward already applied by the test DB setup; roll back to 0004 and confirm the
        # reverse of 0005 rebuilds an Attachment row pointing at the same file.
        executor = MigrationExecutor(connection)
        executor.migrate([MIGRATE_TO])
        to_state = executor.loader.project_state([MIGRATE_TO]).apps

        Notification = to_state.get_model("vintasend_django", "Notification")
        AttachmentFileRecord = to_state.get_model("vintasend_django", "AttachmentFileRecord")
        NotificationAttachment = to_state.get_model("vintasend_django", "NotificationAttachment")

        notification = Notification.objects.create(
            notification_type="EMAIL",
            title="Reverse",
            body_template="body",
            context_kwargs={},
        )
        name = "notifications/attachments/reverse.txt"
        default_storage.save(name, ContentFile(b"reverse bytes"))
        record = AttachmentFileRecord.objects.create(
            filename="reverse.txt",
            content_type="text/plain",
            size=len(b"reverse bytes"),
            checksum=hashlib.sha256(b"reverse bytes").hexdigest(),
            storage_identifiers={"id": name, "name": name},
        )
        NotificationAttachment.objects.create(notification=notification, file=record)

        # Reverse to before the data copy.
        executor = MigrationExecutor(connection)
        executor.migrate([MIGRATE_FROM])
        from_state = executor.loader.project_state([MIGRATE_FROM]).apps
        Attachment = from_state.get_model("vintasend_django", "Attachment")

        restored = Attachment.objects.get()
        assert restored.name == "reverse.txt"
        assert restored.mime_type == "text/plain"
        assert restored.file.name == name
        assert restored.file.read() == b"reverse bytes"
