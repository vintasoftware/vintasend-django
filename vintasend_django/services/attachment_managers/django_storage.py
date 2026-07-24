import datetime
import posixpath
import uuid

from django.core.files.base import ContentFile
from django.core.files.storage import Storage, default_storage
from django.utils import timezone

from vintasend.services.attachment_managers.base import BaseAttachmentManager
from vintasend.services.dataclasses import (
    AttachmentFile,
    AttachmentFileRecord,
    FileAttachment,
    StorageIdentifiers,
)

from vintasend_django.services.attachment_file import DjangoStorageAttachmentFile


class DjangoAttachmentManager(BaseAttachmentManager):
    """Attachment manager that keeps the bytes in a Django storage backend.

    The notification backend persists rows and hands ``storage_identifiers`` back here; this
    manager owns every byte. By default it writes to ``default_storage`` under
    ``notifications/attachments/`` -- pass a different ``storage`` or ``upload_to`` to target
    another backend (S3, a private bucket, and so on).

    ``storage_identifiers`` carries the saved file's name under both ``"id"`` (the required,
    non-empty key every manager must provide) and ``"name"`` (the key this manager reads back).
    ``Storage.save`` returns a collision-free name, so the two are always equal and unique.
    """

    def __init__(
        self,
        storage: Storage | None = None,
        upload_to: str = "notifications/attachments/",
    ) -> None:
        self.storage = storage or default_storage
        self.upload_to = upload_to

    def upload_file(
        self,
        file: FileAttachment,
        filename: str,
        content_type: str | None = None,
    ) -> AttachmentFileRecord:
        data = self.file_to_bytes(file)
        # Prefix with a uuid so two files that share a filename never collide before the
        # storage backend's own collision handling even runs.
        target = posixpath.join(self.upload_to, f"{uuid.uuid4().hex}_{filename}")
        saved_name = self.storage.save(target, ContentFile(data))
        now: datetime.datetime = timezone.now()
        return AttachmentFileRecord(
            id=str(uuid.uuid4()),
            filename=filename,
            content_type=content_type or self.detect_content_type(filename),
            size=len(data),
            checksum=self.calculate_checksum(data),
            created_at=now,
            updated_at=now,
            storage_identifiers={"id": saved_name, "name": saved_name},
        )

    def reconstruct_attachment_file(
        self, storage_identifiers: StorageIdentifiers
    ) -> AttachmentFile:
        name = storage_identifiers.get("name") or storage_identifiers.get("id")
        if not name:
            raise ValueError("storage_identifiers must carry a non-empty 'name' or 'id'")
        return DjangoStorageAttachmentFile(self.storage, name)

    def delete_file_by_identifiers(self, storage_identifiers: StorageIdentifiers) -> None:
        name = storage_identifiers.get("name") or storage_identifiers.get("id")
        if name and self.storage.exists(name):
            self.storage.delete(name)
