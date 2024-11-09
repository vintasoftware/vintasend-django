import uuid
import pytest

from django.core import mail

from vintasend.constants import NotificationStatus, NotificationTypes
from vintasend.exceptions import (
    NotificationTemplateRenderingError,
)
from vintasend.services.dataclasses import Notification
from vintasend.services.notification_backends.stubs.fake_backend import FakeFileBackend
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
        assert email.to == ["testemail@example.com"]  # This is the email that the FakeFileBackend returns

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
