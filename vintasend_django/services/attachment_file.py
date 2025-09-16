from typing import BinaryIO

from vintasend.services.dataclasses import AttachmentFile

from vintasend_django.models import Attachment


class DjangoAttachmentFile(AttachmentFile):
    """Django-specific implementation for file access"""

    def __init__(self, attachment: Attachment):
        self.attachment = attachment

    @property
    def name(self) -> str:
        """Get the attachment name"""
        return self.attachment.name

    @property
    def mime_type(self) -> str:
        """Get the attachment MIME type"""
        return self.attachment.mime_type

    def read(self) -> bytes:
        """Read the entire file content"""
        self.attachment.file.seek(0)
        return self.attachment.file.read()

    def stream(self) -> BinaryIO:
        """
        Return a new file stream for large files.

        Each call to this method opens a new file handle. 
        The caller is responsible for closing the returned stream to prevent resource leaks.
        """
        return self.attachment.file.open('rb')

    def url(self, expires_in: int = 3600) -> str:
        """Generate temporary URL if supported"""
        return self.attachment.file.url

    def delete(self) -> None:
        """Delete from storage"""
        if self.attachment.file:
            self.attachment.file.delete(save=False)
        self.attachment.delete()
