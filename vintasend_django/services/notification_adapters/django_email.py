from typing import TYPE_CHECKING, Generic, TypeVar

from django.contrib.auth import get_user_model
from django.core.mail import EmailMessage

from vintasend.constants import NotificationTypes

from vintasend.services.dataclasses import Notification
from vintasend.services.notification_backends.base import BaseNotificationBackend
from vintasend.services.notification_adapters.base import BaseNotificationAdapter
from vintasend.services.notification_template_renderers.base_templated_email_renderer import BaseTemplatedEmailRenderer
from vintasend.app_settings import NotificationSettings


if TYPE_CHECKING:
    from vintasend.services.notification_service import NotificationContextDict


User = get_user_model()


B = TypeVar("B", bound=BaseNotificationBackend)
T = TypeVar("T", bound=BaseTemplatedEmailRenderer)

class DjangoEmailNotificationAdapter(Generic[B, T], BaseNotificationAdapter[B, T]):
    notification_type = NotificationTypes.EMAIL

    def send(
        self,
        notification: Notification,
        context: "NotificationContextDict",
        headers: dict[str, str] | None = None,
    ) -> None:
        """
        Send the notification to the user through email.

        :param notification: The notification to send.
        :param context: The context to render the notification templates.
        """
        notification_settings = NotificationSettings()

        user_email = self.backend.get_user_email_from_notification(notification.id)
        to = [user_email]
        bcc = [email for email in notification_settings.NOTIFICATION_DEFAULT_BCC_EMAILS] or []

        context_with_base_url: "NotificationContextDict" = context.copy()
        context_with_base_url["base_url"] = f"{notification_settings.NOTIFICATION_DEFAULT_BASE_URL_PROTOCOL}://{notification_settings.NOTIFICATION_DEFAULT_BASE_URL_DOMAIN}"

        template = self.template_renderer.render(notification, context_with_base_url)

        email = EmailMessage(
            subject=template.subject.strip(),
            body=template.body,
            from_email=notification_settings.NOTIFICATION_DEFAULT_FROM_EMAIL,
            to=to,
            bcc=bcc,
            headers=headers,
        )
        email.content_subtype = "html"

        email.send()
