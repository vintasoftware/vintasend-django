from typing import TYPE_CHECKING

from django.contrib.auth import get_user_model
from django.core.mail import EmailMessage

from vintasend.constants import NotificationTypes
from vintasend.exceptions import (
    NotificationSendError,
    NotificationTemplateRenderingError,
)
from vintasend.services.dataclasses import Notification
from vintasend.services.helpers import get_notification_backend, get_template_renderer
from vintasend.services.notification_adapters.base import BaseNotificationAdapter
from vintasend.app_settings import NotificationSettings


if TYPE_CHECKING:
    from vintasend.services.notification_service import NotificationContextDict


User = get_user_model()


class DjangoEmailNotificationAdapter(BaseNotificationAdapter):
    notification_type = NotificationTypes.EMAIL

    def __init__(
        self, template_renderer: str, backend: str | None, backend_kwargs: dict | None = None
    ) -> None:
        self.backend = get_notification_backend(backend, backend_kwargs)
        self.template_renderer = get_template_renderer(template_renderer)

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

        try:
            template = self.template_renderer.render(notification, context_with_base_url)
        except NotificationTemplateRenderingError as e:
            self.backend.mark_pending_as_failed(notification.id)
            raise NotificationTemplateRenderingError() from e

        email = EmailMessage(
            subject=template.subject.strip(),
            body=template.body,
            from_email=notification_settings.NOTIFICATION_DEFAULT_FROM_EMAIL,
            to=to,
            bcc=bcc,
            headers=headers,
        )
        email.content_subtype = "html"

        try:
            email.send()
        except Exception as e:  # noqa: BLE001
            self.backend.mark_pending_as_failed(notification.id)
            raise NotificationSendError() from e

        self.backend.mark_pending_as_sent(notification.id)
