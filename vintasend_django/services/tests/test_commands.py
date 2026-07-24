import hashlib
import shutil
import tempfile

from django.core.management import call_command
from django.test import override_settings

from vintasend_django.models import AttachmentFileRecord
from vintasend_django.services.attachment_managers.django_storage import DjangoAttachmentManager
from vintasend_django.test_helpers import VintaSendDjangoTestCase


class BackfillAttachmentChecksumsCommandTest(VintaSendDjangoTestCase):
    def setUp(self):
        super().setUp()
        self._media_root = tempfile.mkdtemp()
        self._media_override = override_settings(MEDIA_ROOT=self._media_root)
        self._media_override.enable()
        self.manager = DjangoAttachmentManager()

    def tearDown(self):
        self._media_override.disable()
        shutil.rmtree(self._media_root, ignore_errors=True)
        super().tearDown()

    def _record_without_checksum(self, data: bytes, filename: str = "f.txt") -> AttachmentFileRecord:
        # Store real bytes through the manager, then persist a record with an empty checksum,
        # mimicking what the 0005 data migration leaves behind.
        file_record = self.manager.upload_file(data, filename, "text/plain")
        return AttachmentFileRecord.objects.create(
            filename=filename,
            content_type="text/plain",
            size=file_record.size,
            checksum="",
            storage_identifiers=file_record.storage_identifiers,
        )

    def test_backfills_empty_checksums(self):
        data = b"backfill me"
        record = self._record_without_checksum(data)

        call_command("backfill_attachment_checksums")

        record.refresh_from_db()
        assert record.checksum == hashlib.sha256(data).hexdigest()
        assert record.size == len(data)

    def test_dry_run_does_not_write(self):
        record = self._record_without_checksum(b"unchanged")

        call_command("backfill_attachment_checksums", "--dry-run")

        record.refresh_from_db()
        assert record.checksum == ""

    def test_already_checksummed_records_are_left_alone(self):
        # A fully populated record is not in the default (empty-checksum) work set.
        record = self._record_without_checksum(b"content")
        AttachmentFileRecord.objects.filter(pk=record.pk).update(checksum="preexisting")

        call_command("backfill_attachment_checksums")

        record.refresh_from_db()
        assert record.checksum == "preexisting"

    def test_all_flag_recomputes_existing(self):
        data = b"recompute"
        record = self._record_without_checksum(data)
        AttachmentFileRecord.objects.filter(pk=record.pk).update(checksum="stale")

        call_command("backfill_attachment_checksums", "--all")

        record.refresh_from_db()
        assert record.checksum == hashlib.sha256(data).hexdigest()

    def test_unreadable_file_is_skipped(self):
        # storage_identifiers point at a path that was never written, so the read fails.
        record = AttachmentFileRecord.objects.create(
            filename="missing.txt",
            content_type="text/plain",
            size=10,
            checksum="",
            storage_identifiers={
                "id": "notifications/attachments/missing.txt",
                "name": "notifications/attachments/missing.txt",
            },
        )

        call_command("backfill_attachment_checksums")

        record.refresh_from_db()
        assert record.checksum == ""

    def test_limit_processes_at_most_n_records(self):
        first = self._record_without_checksum(b"one", "one.txt")
        second = self._record_without_checksum(b"two", "two.txt")

        call_command("backfill_attachment_checksums", "--limit", "1")

        first.refresh_from_db()
        second.refresh_from_db()
        # Records are processed by ascending pk; only the first is reached under --limit 1.
        checksummed = [r for r in (first, second) if r.checksum]
        assert len(checksummed) == 1

    def test_record_without_storage_identifiers_is_skipped(self):
        record = AttachmentFileRecord.objects.create(
            filename="orphan.txt",
            content_type="text/plain",
            size=0,
            checksum="",
            storage_identifiers={},
        )

        call_command("backfill_attachment_checksums")

        record.refresh_from_db()
        assert record.checksum == ""
