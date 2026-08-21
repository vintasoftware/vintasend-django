import uuid
from typing import TYPE_CHECKING

from django.contrib.auth import get_user_model

import pytest
from vintasend.constants import NotificationStatus, NotificationTypes
from vintasend.exceptions import (
    NotificationBodyTemplateRenderingError,
    NotificationPreheaderTemplateRenderingError,
    NotificationSubjectTemplateRenderingError,
)
from vintasend.services.dataclasses import Notification
from vintasend.services.notification_template_renderers.base_templated_email_renderer import (
    EmailTemplateContent,
)

from vintasend_django.services.notification_template_renderers.django_templated_email_renderer import (
    DjangoTemplatedEmailRenderer,
)
from vintasend_django.test_helpers import VintaSendDjangoTestCase


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

    def test_render_from_template_content(self):
        renderer = DjangoTemplatedEmailRenderer()
        user = self.create_user()
        notification = self.create_notification(user)
        context = self.create_notification_context(notification)
        # Supply the templates explicitly instead of reading them off the notification,
        # reproducing a historical render.
        template_content = EmailTemplateContent(
            subject_template="vintasend_django/emails/test/test_templated_email_subject.txt",
            body_template="vintasend_django/emails/test/test_templated_email_body.html",
            preheader_template="vintasend_django/emails/test/test_templated_email_preheader.html",
        )
        email = renderer.render_from_template_content(notification, template_content, context)
        assert "this_is_my_test_subject_string" in email.subject
        assert "this_is_my_test_body_string" in email.body
        assert email.preheader is not None
        assert "this_is_my_test_preheader_string" in email.preheader

    def create_one_off_notification(self):
        from vintasend.services.dataclasses import OneOffNotification

        return OneOffNotification(
            id=uuid.uuid4(),
            email_or_phone="oneoff@example.com",
            first_name="Jane",
            last_name="Doe",
            notification_type=NotificationTypes.EMAIL.value,
            title="One-off",
            body_template="vintasend_django/emails/test/test_templated_email_body.html",
            context_name="test_context",
            context_kwargs={},
            send_after=None,
            subject_template="vintasend_django/emails/test/test_templated_email_subject.txt",
            preheader_template="vintasend_django/emails/test/test_templated_email_preheader.html",
            status=NotificationStatus.PENDING_SEND.value,
        )

    def test_render_one_off_adds_recipient_context(self):
        renderer = DjangoTemplatedEmailRenderer()
        notification = self.create_one_off_notification()
        context = self.create_notification_context(notification)
        # Renders without error, exercising the one-off recipient-context branch.
        email = renderer.render(notification, context)
        assert "this_is_my_test_subject_string" in email.subject

    def test_render_preheader_error(self):
        renderer = DjangoTemplatedEmailRenderer()
        user = self.create_user()
        notification = self.create_notification(user)
        notification.preheader_template = "does/not/exist_preheader.html"
        context = self.create_notification_context(notification)
        with pytest.raises(NotificationPreheaderTemplateRenderingError):
            renderer.render(notification, context)

    def test_render_subject_error(self):
        renderer = DjangoTemplatedEmailRenderer()
        user = self.create_user()
        notification = self.create_notification(user)
        notification.preheader_template = ""
        notification.subject_template = "does/not/exist_subject.txt"
        context = self.create_notification_context(notification)
        with pytest.raises(NotificationSubjectTemplateRenderingError):
            renderer.render(notification, context)

    def test_render_body_error(self):
        renderer = DjangoTemplatedEmailRenderer()
        user = self.create_user()
        notification = self.create_notification(user)
        notification.preheader_template = ""
        notification.body_template = "does/not/exist_body.html"
        context = self.create_notification_context(notification)
        with pytest.raises(NotificationBodyTemplateRenderingError):
            renderer.render(notification, context)
