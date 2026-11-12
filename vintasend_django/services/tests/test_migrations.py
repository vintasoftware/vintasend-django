import hashlib
import importlib
import re
import shutil
import tempfile
import uuid
from unittest import mock

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext


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


BEFORE_VERSION = ("vintasend_django", "0007_notification_requested_template_version_and_more")
VERSION_ADDED = ("vintasend_django", "0008_notification_notification_version")
VERSION_BACKFILLED = ("vintasend_django", "0009_backfill_notification_version")
VERSION_REQUIRED = ("vintasend_django", "0010_alter_notification_notification_version")
BACKFILL_MODULE = "vintasend_django.migrations._notification_version_backfill"


class NotificationVersionMigrationTest(TransactionTestCase):
    """0008-0010 add ``notification_version`` and give every existing row its own token.

    ``AddField`` with a callable default would give every existing row the same value, which
    would let ``expected_version`` match a row it was not read from.
    """

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    @staticmethod
    def _migrate(target):
        executor = MigrationExecutor(connection)
        executor.migrate([target])
        return executor.loader.project_state([target]).apps

    @staticmethod
    def _create(apps, count, **kwargs):
        notification_model = apps.get_model("vintasend_django", "Notification")
        for i in range(count):
            notification_model.objects.create(
                notification_type="EMAIL",
                title=f"n{i}",
                body_template="body",
                context_kwargs={},
                **kwargs,
            )
        return notification_model

    @staticmethod
    def _columns():
        with connection.cursor() as cursor:
            return {
                column.name: column
                for column in connection.introspection.get_table_description(
                    cursor, "vintasend_django_notification"
                )
            }

    def test_0008_adds_a_nullable_column_and_writes_no_token(self):
        self._create(self._migrate(BEFORE_VERSION), 3)

        apps = self._migrate(VERSION_ADDED)

        notification_model = apps.get_model("vintasend_django", "Notification")
        assert self._columns()["notification_version"].null_ok
        assert set(notification_model.objects.values_list("notification_version", flat=True)) == {
            None
        }

    def test_existing_rows_each_get_their_own_uuid7(self):
        self._create(self._migrate(BEFORE_VERSION), 5)

        apps = self._migrate(VERSION_BACKFILLED)

        notification_model = apps.get_model("vintasend_django", "Notification")
        tokens = list(notification_model.objects.values_list("notification_version", flat=True))
        assert len(tokens) == 5
        assert len(set(tokens)) == 5
        assert all(token is not None and token.version == 7 for token in tokens)
        # 0009 leaves the column nullable, so code predating it can keep inserting rows.
        assert self._columns()["notification_version"].null_ok

    def test_the_backfill_commits_per_batch_and_reads_primary_keys_only(self):
        backfill = importlib.import_module(BACKFILL_MODULE)
        apps = self._migrate(VERSION_ADDED)
        notification_model = self._create(apps, 5)

        with (
            mock.patch.object(backfill, "BATCH_SIZE", 2),
            mock.patch.object(
                backfill.transaction, "atomic", wraps=backfill.transaction.atomic
            ) as atomic,
            CaptureQueriesContext(connection) as queries,
        ):
            backfill.backfill_notification_versions(apps, connection.schema_editor())

        # 3 batches of at most 2, then an empty read: one transaction each, one bulk UPDATE per
        # non-empty batch.
        assert atomic.call_count == 4
        selects = [q["sql"] for q in queries.captured_queries if q["sql"].startswith("SELECT")]
        updates = [q["sql"] for q in queries.captured_queries if q["sql"].startswith("UPDATE")]
        assert len(selects) == 4
        assert len(updates) == 3
        for sql in selects:
            select_list = sql[len("SELECT ") : sql.index(" FROM ")]
            assert re.findall(r'"vintasend_django_notification"\."(\w+)"', select_list) == ["id"]
        tokens = list(notification_model.objects.values_list("notification_version", flat=True))
        assert None not in tokens
        assert len(set(tokens)) == 5

    def test_rerunning_the_backfill_only_fills_nulls(self):
        backfill = importlib.import_module(BACKFILL_MODULE)
        apps = self._migrate(VERSION_ADDED)
        notification_model = self._create(apps, 3)
        backfill.backfill_notification_versions(apps, connection.schema_editor())
        first = dict(notification_model.objects.values_list("pk", "notification_version"))
        # An interrupted run, or rows inserted since: some rows still have no token.
        self._create(apps, 2)

        backfill.backfill_notification_versions(apps, connection.schema_editor())

        second = dict(notification_model.objects.values_list("pk", "notification_version"))
        assert {pk: second[pk] for pk in first} == first
        assert len(second) == 5
        assert None not in second.values()
        assert len(set(second.values())) == 5

    def test_0010_fills_rows_inserted_after_0009_then_requires_the_token(self):
        apps = self._migrate(VERSION_BACKFILLED)
        # Code predating the column, still running after 0009, inserts rows with no token.
        notification_model = self._create(apps, 3)
        assert notification_model.objects.filter(notification_version__isnull=True).count() == 3

        apps = self._migrate(VERSION_REQUIRED)

        notification_model = apps.get_model("vintasend_django", "Notification")
        tokens = list(notification_model.objects.values_list("notification_version", flat=True))
        assert len(tokens) == 3
        assert None not in tokens
        assert len(set(tokens)) == 3
        assert not self._columns()["notification_version"].null_ok

    def test_the_backend_tolerates_a_row_with_no_token_between_0009_and_0010(self):
        from vintasend.exceptions import NotificationVersionChangedError

        from vintasend_django.services.notification_backends.django_db_notification_backend import (
            DjangoDbNotificationBackend,
        )

        apps = self._migrate(VERSION_BACKFILLED)
        notification_model = self._create(apps, 1, status="FAILED")
        pk = notification_model.objects.get().pk
        backend = DjangoDbNotificationBackend()

        # Listed, with the empty string as its version...
        listed = backend.filter_notification_versions({}, 1, 10)
        assert [(v.id, v.version) for v in listed] == [(pk, "")]
        # ...and no token, that one included, moves it conditionally.
        for token in ("", str(uuid.uuid4())):
            with self.subTest(token=token):
                with self.assertRaises(NotificationVersionChangedError):
                    backend.requeue_notification(pk, expected_version=token)
                with self.assertRaises(NotificationVersionChangedError):
                    backend.repin_notification(pk, 2, expected_version=token)
        row = notification_model.objects.get()
        assert row.status == "FAILED"
        assert row.requested_template_version is None
        assert row.notification_version is None

    def test_reverses_to_0007(self):
        self._create(self._migrate(VERSION_REQUIRED), 2)

        apps = self._migrate(BEFORE_VERSION)

        notification_model = apps.get_model("vintasend_django", "Notification")
        assert notification_model.objects.count() == 2
        assert "notification_version" not in self._columns()

    def test_0010_reverses_to_a_nullable_column(self):
        self._create(self._migrate(VERSION_REQUIRED), 2)

        apps = self._migrate(VERSION_BACKFILLED)

        notification_model = apps.get_model("vintasend_django", "Notification")
        assert self._columns()["notification_version"].null_ok
        assert notification_model.objects.filter(notification_version__isnull=True).count() == 0
