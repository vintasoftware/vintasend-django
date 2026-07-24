from django.contrib.auth import get_user_model
from django.core.serializers.json import DjangoJSONEncoder
from django.db import models
from django.utils.translation import gettext_lazy as _

from model_utils.fields import AutoCreatedField, AutoLastModifiedField

from vintasend_django.constants import NotificationStatusChoices, NotificationTypesChoices


User = get_user_model()

class Notification(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, null=True, blank=True)
    email_or_phone = models.CharField(max_length=255, blank=True)
    first_name = models.CharField(max_length=255, blank=True)
    last_name = models.CharField(max_length=255, blank=True)
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
    context_kwargs = models.JSONField(default=dict, encoder=DjangoJSONEncoder)

    send_after = models.DateTimeField(null=True)

    # Set by mark_pending_as_sent / mark_sent_as_read at delivery and read time. Kept
    # nullable so a pending or never-read notification carries no timestamp, which the
    # filter vocabulary's ``sent_at_range`` / ``read_at_range`` rely on for their NULL
    # semantics (a NULL never matches a positive range filter).
    sent_at = models.DateTimeField(_("sent at"), null=True, blank=True)
    read_at = models.DateTimeField(_("read at"), null=True, blank=True)

    # Optional multi-tenant partition key. ``null=True`` (not blank "") so a tenant-less
    # row is distinguishable from one whose tenant is the empty string -- the filter
    # vocabulary's NULL semantics require a real NULL here, not "". noqa DJ001 for that reason.
    tenant = models.CharField(_("tenant"), max_length=255, null=True, blank=True, db_index=True)  # noqa: DJ001

    # System-managed: written only by NotificationService (through the backend's
    # ``store_git_commit_sha``) at send time, and always already normalized to 40
    # lowercase hex characters. Never set on creation or through update_notification.
    # ``null=True`` so an unresolved SHA is None, matching the dataclass field, not "".
    git_commit_sha = models.CharField(_("git commit sha"), max_length=40, null=True, blank=True)  # noqa: DJ001

    created = AutoCreatedField(_("created"), db_index=True)
    modified = AutoLastModifiedField(_("modified"), db_index=True)

    adapter_extra_parameters = models.JSONField(_("extra parameters for the notification adapter"), null=True, encoder=DjangoJSONEncoder)

    context_used = models.JSONField(_("context used when notification was sent"), null=True, encoder=DjangoJSONEncoder)
    adapter_used = models.CharField(_("adapter used to send the notification"), max_length=255, blank=True)

    objects: models.Manager["Notification"]

    class Meta:
        ordering = ("-created",)

    def __str__(self):
        return f"{self.user} - {self.notification_type} - {self.title} - {self.status}{f' (scheduled to {self.send_after})' if self.send_after else ''}"


class AttachmentFileRecord(models.Model):
    """A checksum-indexed, stored blob. One record can back many notifications.

    The bytes themselves live wherever the injected attachment manager put them; this
    row only describes the blob and carries the manager's opaque ``storage_identifiers``
    back to it for reconstruction and deletion. The backend never opens the file.
    """

    filename = models.CharField(max_length=255)
    content_type = models.CharField(max_length=255, blank=True)
    size = models.PositiveBigIntegerField(default=0)
    # sha256 hex digest, indexed for the (checksum, size) dedup lookup.
    checksum = models.CharField(max_length=64, blank=True, db_index=True)
    # Opaque, manager-defined identifiers. Must carry a non-empty ``id``; every other key
    # belongs to whichever manager wrote the bytes.
    storage_identifiers = models.JSONField(default=dict, encoder=DjangoJSONEncoder)

    created = AutoCreatedField(_("created"), db_index=True)
    modified = AutoLastModifiedField(_("modified"), db_index=True)

    objects: models.Manager["AttachmentFileRecord"]

    class Meta:
        ordering = ("-created",)
        indexes = [  # noqa: RUF012 - Django Meta options are not ClassVar-annotated
            models.Index(fields=["checksum", "size"]),
        ]

    def __str__(self):
        return f"{self.filename} ({self.checksum[:12]})"


class NotificationAttachment(models.Model):
    """Join row linking a notification to a stored ``AttachmentFileRecord``.

    ``is_inline`` / ``description`` live here rather than on the file record because they
    describe how *this* notification uses the file, not the file itself. Deleting this row
    drops one reference; the file record survives until nothing references it.
    """

    notification = models.ForeignKey(
        Notification, on_delete=models.CASCADE, related_name="attachments"
    )
    file = models.ForeignKey(
        AttachmentFileRecord, on_delete=models.PROTECT, related_name="notification_attachments"
    )
    # ``null=True`` mirrors StoredAttachment.description (``str | None``); an absent
    # description is None, not "". noqa DJ001 for that reason.
    description = models.CharField(max_length=255, null=True, blank=True)  # noqa: DJ001
    is_inline = models.BooleanField(default=False)

    created = AutoCreatedField(_("created"), db_index=True)
    modified = AutoLastModifiedField(_("modified"), db_index=True)

    objects: models.Manager["NotificationAttachment"]

    class Meta:
        ordering = ("-created",)

    def __str__(self):
        return f"{self.file.filename} ({self.notification})"
