import shutil
import tempfile

from django.core.files.storage import default_storage
from django.test import override_settings

import pytest

from vintasend_django.services.attachment_managers.django_storage import DjangoAttachmentManager
from vintasend_django.test_helpers import VintaSendDjangoTestCase


class DjangoAttachmentManagerTestCase(VintaSendDjangoTestCase):
    def setUp(self):
        super().setUp()
        self._media_root = tempfile.mkdtemp()
        self._media_override = override_settings(MEDIA_ROOT=self._media_root)
        self._media_override.enable()

    def tearDown(self):
        self._media_override.disable()
        shutil.rmtree(self._media_root, ignore_errors=True)
        super().tearDown()

    def test_upload_reconstruct_and_read(self):
        manager = DjangoAttachmentManager()
        record = manager.upload_file(b"manager bytes", "m.txt", "text/plain")
        assert record.checksum
        assert record.size == len(b"manager bytes")
        handle = manager.reconstruct_attachment_file(record.storage_identifiers)
        assert handle.read() == b"manager bytes"

    def test_reconstruct_without_identifier_raises(self):
        manager = DjangoAttachmentManager()
        with pytest.raises(ValueError):
            manager.reconstruct_attachment_file({})

    def test_delete_file_by_identifiers_removes_bytes(self):
        manager = DjangoAttachmentManager()
        record = manager.upload_file(b"to delete", "d.txt", "text/plain")
        name = record.storage_identifiers["name"]
        assert default_storage.exists(name)
        manager.delete_file_by_identifiers(record.storage_identifiers)
        assert not default_storage.exists(name)
        # Deleting an unknown identifier is a no-op, not an error.
        manager.delete_file_by_identifiers({"id": "nope", "name": "nope"})
