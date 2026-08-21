import io
import uuid

from django.core import mail
from django.utils import timezone

import pytest
from vintasend.constants import NotificationStatus, NotificationTypes
from vintasend.exceptions import (
    NotificationTemplateRenderingError,
)
from vintasend.services.dataclasses import (
    AttachmentFile,
    Notification,
    OneOffNotification,
    StoredAttachment,
)
from vintasend.services.notification_backends.stubs.fake_backend import FakeFileBackend


class _InMemoryAttachmentFile(AttachmentFile):
    """Minimal AttachmentFile handle for exercising the adapter's attach path."""

    def __init__(self, data: bytes, fail: bool = False):
        self._data = data
        self._fail = fail

    def read(self) -> bytes:
        if self._fail:
            raise OSError("cannot read attachment")
        return self._data

    def stream(self):
        return io.BytesIO(self._data)

    def url(self, expires_in: int = 3600) -> str:
        return "mem://attachment"

    def delete(self) -> None:
        pass


def _stored_attachment(data: bytes = b"payload", fail: bool = False) -> StoredAttachment:
    return StoredAttachment(
        id="1",
        filename="doc.txt",
        content_type="text/plain",
        size=len(data),
        checksum="deadbeef",
        created_at=timezone.now(),
        file=_InMemoryAttachmentFile(data, fail=fail),
    )


from vintasend_django.services.notification_adapters.django_email import (
    DjangoEmailNotificationAdapter,
)
from vintasend_django.test_helpers import VintaSendDjangoTestCase


class DjangoEmailNotificationAdapterTestCase(VintaSendDjangoTestCase):
    def tearDown(self) -> None:
        mail.outbox = []
        FakeFileBackend(database_file_name="django-email-adapter-test-notifications.json").clear()
        return super().tearDown()

    def create_notification(self, user):
        return Notification(
            id=uuid.uuid4(),
            user_id=user.id,
            notification_type=NotificationTypes.EMAIL.value,
            title="Test Notification",
            body_template="Test Body",
            context_name="test_context",
            context_kwargs={"test": "test"},
            send_after=None,
            subject_template="Test Subject",
            preheader_template="Test Preheader",
            status=NotificationStatus.PENDING_SEND.value,
        )

    def create_notification_context(self):
        return {"foo": "bar"}

    def test_send_notification(self):
        user = self.create_user(email="testadapter@example.com")
        notification = self.create_notification(user)
        context = self.create_notification_context()

        backend = FakeFileBackend(database_file_name="django-email-adapter-test-notifications.json")
        backend.notifications.append(notification)
        backend._store_notifications()

        adapter = DjangoEmailNotificationAdapter(
            "vintasend.services.notification_template_renderers.stubs.fake_templated_email_renderer.FakeTemplateRenderer",
            "vintasend.services.notification_backends.stubs.fake_backend.FakeFileBackend",
            backend_kwargs={"database_file_name": "django-email-adapter-test-notifications.json"},
        )

        adapter.send(notification, context)

        assert len(mail.outbox) == 1
        email = mail.outbox[0]
        assert email.subject == notification.subject_template
        assert email.body == notification.body_template
        assert email.to == [
            "testemail@example.com"
        ]  # This is the email that the FakeFileBackend returns

    def test_send_notification_with_render_error(self):
        user = self.create_user(email="testadapter@example.com")
        notification = self.create_notification(user)
        context = self.create_notification_context()

        backend = FakeFileBackend(database_file_name="django-email-adapter-test-notifications.json")
        backend.notifications.append(notification)
        backend._store_notifications()

        adapter = DjangoEmailNotificationAdapter(
            "vintasend.services.notification_template_renderers.stubs.fake_templated_email_renderer.FakeTemplateRendererWithException",
            "vintasend.services.notification_backends.stubs.fake_backend.FakeFileBackend",
            backend_kwargs={"database_file_name": "django-email-adapter-test-notifications.json"},
        )
        with pytest.raises(NotificationTemplateRenderingError):
            adapter.send(notification, context)

        assert len(mail.outbox) == 0

    def test_send_one_off_notification(self):
        """Test sending one-off notification"""
        one_off_notification = OneOffNotification(
            id=uuid.uuid4(),
            email_or_phone="oneoff@example.com",
            first_name="John",
            last_name="Doe",
            notification_type=NotificationTypes.EMAIL.value,
            title="One-off Test Notification",
            body_template="Test Body",
            context_name="test_context",
            context_kwargs={"test": "test"},
            send_after=None,
            subject_template="Test Subject",
            preheader_template="Test Preheader",
            status=NotificationStatus.PENDING_SEND.value,
            attachments=[],
        )
        context = self.create_notification_context()

        adapter = DjangoEmailNotificationAdapter(
            "vintasend.services.notification_template_renderers.stubs.fake_templated_email_renderer.FakeTemplateRenderer",
            "vintasend.services.notification_backends.stubs.fake_backend.FakeFileBackend",
            backend_kwargs={"database_file_name": "django-email-adapter-test-one-off.json"},
        )

        adapter.send(one_off_notification, context)

        assert len(mail.outbox) == 1
        email = mail.outbox[0]
        assert email.subject == one_off_notification.subject_template
        assert email.body == one_off_notification.body_template
        assert email.to == ["oneoff@example.com"]

    def create_one_off_notification(self):
        """Helper method to create one-off notification for testing"""
        return OneOffNotification(
            id=uuid.uuid4(),
            email_or_phone="oneoff@example.com",
            first_name="Test",
            last_name="User",
            notification_type=NotificationTypes.EMAIL.value,
            title="Test One-off Notification",
            body_template="Test Body",
            context_name="test_context",
            context_kwargs={"test": "test"},
            send_after=None,
            subject_template="Test Subject",
            preheader_template="Test Preheader",
            status=NotificationStatus.PENDING_SEND.value,
            attachments=[],
        )

    def test_send_one_off_notification_with_attachment(self):
        """The adapter reads each StoredAttachment through its file handle and attaches it."""
        one_off_notification = self.create_one_off_notification()
        one_off_notification.attachments = [_stored_attachment(b"file bytes")]
        context = self.create_notification_context()

        adapter = DjangoEmailNotificationAdapter(
            "vintasend.services.notification_template_renderers.stubs.fake_templated_email_renderer.FakeTemplateRenderer",
            "vintasend.services.notification_backends.stubs.fake_backend.FakeFileBackend",
            backend_kwargs={"database_file_name": "django-email-adapter-test-attachment.json"},
        )

        adapter.send(one_off_notification, context)

        assert len(mail.outbox) == 1
        email = mail.outbox[0]
        assert len(email.attachments) == 1
        name, content, mimetype = email.attachments[0]
        assert name == "doc.txt"
        # Django decodes text/* attachment payloads to str; normalize before comparing.
        assert (content.encode() if isinstance(content, str) else content) == b"file bytes"
        assert mimetype == "text/plain"

    def test_send_one_off_notification_with_unreadable_attachment_still_sends(self):
        """A failing attachment is logged and skipped; the email still goes out."""
        one_off_notification = self.create_one_off_notification()
        one_off_notification.attachments = [_stored_attachment(fail=True)]
        context = self.create_notification_context()

        adapter = DjangoEmailNotificationAdapter(
            "vintasend.services.notification_template_renderers.stubs.fake_templated_email_renderer.FakeTemplateRenderer",
            "vintasend.services.notification_backends.stubs.fake_backend.FakeFileBackend",
            backend_kwargs={"database_file_name": "django-email-adapter-test-attachment-fail.json"},
        )

        adapter.send(one_off_notification, context)

        assert len(mail.outbox) == 1
        assert len(mail.outbox[0].attachments) == 0
