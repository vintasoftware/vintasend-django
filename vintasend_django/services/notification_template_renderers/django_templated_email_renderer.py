from typing import TYPE_CHECKING

from django.template.loader import render_to_string

from vintasend.exceptions import (
    NotificationBodyTemplateRenderingError,
    NotificationPreheaderTemplateRenderingError,
    NotificationSubjectTemplateRenderingError,
)
from vintasend.services.dataclasses import Notification, OneOffNotification
from vintasend.services.notification_template_renderers.base_templated_email_renderer import (
    BaseTemplatedEmailRenderer,
    TemplatedEmail,
)


if TYPE_CHECKING:
    from vintasend.services.notification_service import NotificationContextDict


class DjangoTemplatedEmailRenderer(BaseTemplatedEmailRenderer):
    def render(
        self,
        notification: "Notification | OneOffNotification",
        context: "NotificationContextDict",
        **kwargs
    ) -> TemplatedEmail:
        subject_template = notification.subject_template
        body_template = notification.body_template
        preheader_template = notification.preheader_template

        # Add recipient information to context for one-off notifications
        enhanced_context = context.copy()
        if isinstance(notification, OneOffNotification):
            enhanced_context.update({
                'recipient_email': notification.email_or_phone,
                'recipient_first_name': notification.first_name,
                'recipient_last_name': notification.last_name,
                'recipient_full_name': f"{notification.first_name} {notification.last_name}".strip(),
            })

        try:
            enhanced_context["private_preheader"] = render_to_string(
                preheader_template,
                enhanced_context,
            )
        except Exception as e:  # noqa: BLE001
            raise NotificationPreheaderTemplateRenderingError(
                "Failed to render preheader template"
            ) from e

        try:
            subject = render_to_string(subject_template, enhanced_context)
        except Exception as e:  # noqa: BLE001
            raise NotificationSubjectTemplateRenderingError(
                "Failed to render subject template"
            ) from e

        try:
            body = render_to_string(body_template, enhanced_context)
        except Exception as e:  # noqa: BLE001
            raise NotificationBodyTemplateRenderingError("Failed to render body template") from e

        return TemplatedEmail(subject=subject, body=body)
