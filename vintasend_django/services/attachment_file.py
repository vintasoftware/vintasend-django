from typing import TYPE_CHECKING, BinaryIO, cast

from vintasend.services.dataclasses import AttachmentFile


if TYPE_CHECKING:
    from django.core.files.storage import Storage


class DjangoStorageAttachmentFile(AttachmentFile):
    """Read-back handle for a file the ``DjangoAttachmentManager`` stored in a Django storage.

    Built lazily from a storage backend plus the stored file's name -- no I/O happens until
    a method is called -- so ``reconstruct_attachment_file`` can stay synchronous. The name
    is whatever ``Storage.save`` returned when the bytes were uploaded, carried in an
    ``AttachmentFileRecord``'s ``storage_identifiers``.
    """

    def __init__(self, storage: "Storage", name: str):
        self.storage = storage
        self.name = name

    def read(self) -> bytes:
        with self.storage.open(self.name, "rb") as file:
            return file.read()

    def stream(self) -> BinaryIO:
        """Open a fresh read stream for the file.

        Each call opens a new handle; the caller is responsible for closing the returned
        stream to avoid leaking file descriptors.
        """
        return cast(BinaryIO, self.storage.open(self.name, "rb"))

    def url(self, expires_in: int = 3600) -> str:
        """Return the storage URL for the file.

        ``expires_in`` is accepted to satisfy the ``AttachmentFile`` interface; whether it is
        honored depends on the storage backend (e.g. a signed-URL S3 backend uses it, the
        local filesystem backend ignores it).
        """
        return self.storage.url(self.name)

    def delete(self) -> None:
        if self.name and self.storage.exists(self.name):
            self.storage.delete(self.name)
