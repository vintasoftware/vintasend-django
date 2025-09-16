from typing import TYPE_CHECKING, Generic, TypeVar

from django.contrib.auth import get_user_model
from django.core.mail import EmailMessage

from vintasend.app_settings import NotificationSettings
from vintasend.constants import NotificationTypes
from vintasend.services.dataclasses import Notification, OneOffNotification
from vintasend.services.notification_adapters.base import BaseNotificationAdapter
from vintasend.services.notification_backends.base import BaseNotificationBackend
from vintasend.services.notification_template_renderers.base_templated_email_renderer import (
    BaseTemplatedEmailRenderer,
)


if TYPE_CHECKING:
    from vintasend.services.notification_service import NotificationContextDict


User = get_user_model()


B = TypeVar("B", bound=BaseNotificationBackend)
T = TypeVar("T", bound=BaseTemplatedEmailRenderer)

class DjangoEmailNotificationAdapter(Generic[B, T], BaseNotificationAdapter[B, T]):
    notification_type = NotificationTypes.EMAIL

    def send(
        self,
        notification: "Notification | OneOffNotification",
        context: "NotificationContextDict",
        headers: dict[str, str] | None = None,
    ) -> None:
        """
        Send the notification to the user through email.

        :param notification: The notification to send (regular or one-off).
        :param context: The context to render the notification templates.
        """
        notification_settings = NotificationSettings()

        # Get recipient information based on notification type
        recipient_info = self._get_recipient_info(notification)

        to = [recipient_info["email"]]
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

        # Attach files if any
        self._attach_files(email, notification)

        email.send()

    def _get_recipient_info(self, notification: "Notification | OneOffNotification") -> dict[str, str]:
        """Extract recipient information from notification"""

        if isinstance(notification, OneOffNotification):
            # One-off notification: use provided email/phone and name
            return {
                "email": notification.email_or_phone,
                "full_name": f"{notification.first_name} {notification.last_name}".strip(),
            }
        else:
            # Regular notification: fetch user information
            user_email = self.backend.get_user_email_from_notification(notification.id)
            return {
                "email": user_email,
                "full_name": "",  # Could be extended to get user's full name
            }

    def _attach_files(self, email_message: EmailMessage, notification: "Notification | OneOffNotification") -> None:
        """Attach files to email message"""
        if not hasattr(notification, 'attachments') or not notification.attachments:
            return

        for attachment in notification.attachments:
            try:
                file_data = attachment.file.read()
                # Access Django attachment model for metadata
                django_file = attachment.file
                if hasattr(django_file, 'attachment'):
                    name = django_file.attachment.name
                    mime_type = django_file.attachment.mime_type
                else:
                    # Fallback for basic attachment info
                    name = str(attachment.id) if hasattr(attachment, 'id') else 'attachment'
                    mime_type = 'application/octet-stream'

                email_message.attach(name, file_data, mime_type)
            except Exception as e:
                # Log error but don't break notification sending
                import logging
                logging.warning("Failed to attach file %s: %s", getattr(attachment, 'id', 'unknown'), e)
                continue
