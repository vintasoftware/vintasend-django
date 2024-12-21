from django.contrib.auth import get_user_model
from django.db import models
from django.utils.translation import gettext_lazy as _

from model_utils.fields import AutoCreatedField, AutoLastModifiedField

from vintasend_django.constants import NotificationStatusChoices, NotificationTypesChoices


User = get_user_model()

class Notification(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    notification_type = models.CharField(max_length=50, choices=NotificationTypesChoices)
    title = models.CharField(max_length=255)
    status = models.CharField(
        max_length=50, choices=NotificationStatusChoices, default=NotificationStatusChoices.PENDING_SEND
    )
    body_template = models.CharField(max_length=255)

    # Email specific fields
    subject_template = models.CharField(max_length=255, blank=True)
    preheader_template = models.CharField(max_length=255, blank=True)
    context_name = models.CharField(max_length=255, blank=True)
    context_kwargs = models.JSONField(default=dict)

    send_after = models.DateTimeField(null=True)

    created = AutoCreatedField(_("created"), db_index=True)
    modified = AutoLastModifiedField(_("modified"), db_index=True)

    adapter_extra_parameters = models.JSONField(_("adapter_extra_parameters"), null=True)

    context_used = models.JSONField(_("context used when notification was sent"), null=True)
    adapter_used = models.CharField(_("adapter used to send the notification"), max_length=255, null=True)

    objects: models.Manager["Notification"]

    class Meta:
        ordering = ("-created",)

    def __str__(self):
        return f"{self.user} - {self.notification_type} - {self.title} - {self.status}{f' (scheduled to {self.send_after})' if self.send_after else ''}"
