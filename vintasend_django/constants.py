from vintasend.constants import NotificationStatus, NotificationTypes
from django.utils.translation import gettext_lazy as _
from django.db.models import TextChoices


class NotificationStatusChoices(TextChoices):
    PENDING_SEND = NotificationStatus.PENDING_SEND.value, _("Pending Send")
    SENT = NotificationStatus.SENT.value, _("Sent")
    CANCELLED = NotificationStatus.CANCELLED.value, _("Cancelled")
    FAILED = NotificationStatus.FAILED.value, _("Failed")
    READ = NotificationStatus.READ.value, _("Read")


class NotificationTypesChoices(TextChoices):
    EMAIL = NotificationTypes.EMAIL.value, _("Email")
    IN_APP = NotificationTypes.IN_APP.value, _("In App")
    SMS = NotificationTypes.SMS.value, _("SMS")
    PUSH = NotificationTypes.PUSH.value, _("Push")
