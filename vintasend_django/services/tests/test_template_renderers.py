import uuid
from typing import TYPE_CHECKING 

from vintasend.constants import NotificationStatus, NotificationTypes
from vintasend.services.dataclasses import Notification
from vintasend_django.services.notification_template_renderers.django_templated_email_renderer import (
    DjangoTemplatedEmailRenderer,
)
from vintasend_django.test_helpers import VintaSendDjangoTestCase
from django.contrib.auth import get_user_model


if TYPE_CHECKING:
    from django.contrib.auth.models import AbstractBaseUser as DjangoUser

User = get_user_model()


class DjangoTemplatedEmailRendererTestCase(VintaSendDjangoTestCase):
    def create_notification(self, user: "DjangoUser") -> Notification:
        return Notification(
            id=uuid.uuid4(),
            user_id=user.pk,
            notification_type=NotificationTypes.EMAIL.value,
            title="Test Notification",
            body_template="vintasend_django/emails/test/test_templated_email_body.html",
            context_name="test_context",
            context_kwargs={},
            send_after=None,
            subject_template="vintasend_django/emails/test/test_templated_email_subject.txt",
            preheader_template="vintasend_django/emails/test/test_templated_email_preheader.html",
            status=NotificationStatus.PENDING_SEND.value,
        )

    def create_notification_context(self, notification: Notification):
        return {
            "test_subject": "this_is_my_test_subject_string",
            "test_preheader": "this_is_my_test_preheader_string",
            "test_body": "this_is_my_test_body_string",
        }

    def test_render(self):
        renderer = DjangoTemplatedEmailRenderer()
        user = self.create_user()
        notification = self.create_notification(user)
        context = self.create_notification_context(notification)
        email = renderer.render(notification, context)
        assert "this_is_my_test_subject_string" in email.subject
        assert "this_is_my_test_preheader_string" in email.body
        assert "this_is_my_test_body_string" in email.body
