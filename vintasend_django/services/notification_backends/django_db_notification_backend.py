import datetime
import hashlib
import os
import uuid
from collections.abc import Iterable
from typing import cast

from django.core.files.base import ContentFile
from django.db.models import Q, QuerySet

from vintasend.constants import NotificationStatus, NotificationTypes
from vintasend.exceptions import (
    NotificationCancelError,
    NotificationNotFoundError,
    NotificationUpdateError,
    NotificationUserNotFoundError,
)
from vintasend.services.dataclasses import (
    Notification,
    NotificationAttachment,
    OneOffNotification,
    StoredAttachment,
    UpdateNotificationKwargs,
)
from vintasend.services.notification_backends.base import BaseNotificationBackend

from vintasend_django.models import Attachment as AttachmentModel
from vintasend_django.models import Notification as NotificationModel
from vintasend_django.services.attachment_file import DjangoAttachmentFile


class DjangoDbNotificationBackend(BaseNotificationBackend):
    def _get_all_future_notifications_queryset(self) -> QuerySet["NotificationModel"]:
        return NotificationModel.objects.filter(
            Q(send_after__gte=datetime.datetime.now()) | Q(send_after__isnull=False),
            status=NotificationStatus.PENDING_SEND.value,
        ).order_by("created")

    def _get_all_in_app_unread_notifications_queryset(
        self, user_id: int | str | uuid.UUID
    ) -> QuerySet["NotificationModel"]:
        return NotificationModel.objects.filter(
            user_id=str(user_id),
            status=NotificationStatus.SENT.value,
            notification_type=NotificationTypes.IN_APP,
        ).order_by("created")

    def _get_all_pending_notifications_queryset(self) -> QuerySet["NotificationModel"]:
        return NotificationModel.objects.filter(
            Q(send_after__lte=datetime.datetime.now()) | Q(send_after__isnull=True),
            status=NotificationStatus.PENDING_SEND.value,
        ).order_by("created")

    def _paginate_queryset(
        self, queryset: "QuerySet[NotificationModel]", page: int, page_size: int
    ) -> QuerySet["NotificationModel"]:
        return queryset[((page - 1) * page_size) : ((page - 1) * page_size) + page_size]

    def _serialize_user_notification_queryset(
        self, queryset: "QuerySet[NotificationModel]"
    ) -> Iterable[Notification]:
        return (self.serialize_user_notification(n) for n in queryset.iterator())

    def _serialize_notification_queryset(
        self, queryset: "QuerySet[NotificationModel]"
    ) -> Iterable[Notification | OneOffNotification]:
        return (self.serialize_notification(n) for n in queryset.iterator())

    def serialize_notification(self, notification: NotificationModel) -> Notification | OneOffNotification:
        if notification.user_id:
            return self.serialize_user_notification(notification)
        return self.serialize_one_off_notification(notification)

    def serialize_user_notification(self, notification: NotificationModel) -> Notification:
        if not notification.user_id:
            raise NotificationUserNotFoundError("User not found")

        return Notification(
            id=notification.pk,
            user_id=cast(int | str | uuid.UUID, notification.user_id),
            notification_type=notification.notification_type,
            title=notification.title,
            body_template=notification.body_template,
            context_name=notification.context_name,
            context_kwargs=notification.context_kwargs,
            send_after=notification.send_after,
            subject_template=notification.subject_template,
            preheader_template=notification.preheader_template,
            status=notification.status,
        )

    def serialize_one_off_notification(self, notification: NotificationModel) -> OneOffNotification:
        """Serialize Django model to OneOffNotification dataclass"""
        return OneOffNotification(
            id=notification.pk,
            email_or_phone=notification.email_or_phone,
            first_name=notification.first_name,
            last_name=notification.last_name,
            notification_type=notification.notification_type,
            title=notification.title,
            body_template=notification.body_template,
            context_name=notification.context_name,
            context_kwargs=notification.context_kwargs,
            send_after=notification.send_after,
            subject_template=notification.subject_template,
            preheader_template=notification.preheader_template,
            status=notification.status,
            attachments=[self._serialize_attachment(att) for att in notification.attachments.all()],
        )

    def _serialize_attachment(self, attachment) -> StoredAttachment:
        """
        Convert Django attachment model to StoredAttachment.

        Calculates SHA-256 checksum of the file content.
        """
        # Calculate checksum by reading the file content
        checksum = ""
        if attachment.file:
            try:
                # Read file content and calculate SHA-256 hash
                attachment.file.seek(0)  # Ensure we're at the beginning
                file_content = attachment.file.read()
                checksum = hashlib.sha256(file_content).hexdigest()
                attachment.file.seek(0)  # Reset file position
            except OSError:
                # If file reading fails, use empty checksum
                checksum = ""

        return StoredAttachment(
            id=str(attachment.pk),
            filename=attachment.name,
            content_type=attachment.mime_type,
            checksum=checksum,
            size=attachment.size or 0,
            created_at=attachment.created,
            file=DjangoAttachmentFile(attachment),
        )

    def _store_attachments(self, attachments: list[NotificationAttachment]) -> list:
        """Store attachments and return stored attachment objects"""
        stored_attachments = []

        for attachment in attachments:


            # Handle different attachment input types (simplified for now)
            file_content = b''
            file_name = 'attachment'
            mime_type = 'application/octet-stream'

            # Handle different attachment types
            if hasattr(attachment, 'file_path'):
                with open(attachment.file_path, 'rb') as f:
                    file_content = f.read()
                file_name = os.path.basename(attachment.file_path)
            elif hasattr(attachment, 'file_bytes'):
                file_content = attachment.file_bytes
                file_name = getattr(attachment, 'file_name', 'attachment')
            elif hasattr(attachment, 'file_obj'):
                file_obj = attachment.file_obj
                file_obj.seek(0)
                file_content = file_obj.read()
                file_name = getattr(attachment, 'file_name', 'attachment')
            else:
                raise ValueError(
                    f"Unsupported attachment type: {type(attachment)}. "
                    "Attachment must have 'file_path', 'file_bytes', or 'file_obj'."
                )

            # Create attachment record in database
            attachment_instance = AttachmentModel(
                name=file_name,
                mime_type=mime_type,
                size=len(file_content),
            )

            # Save file content to storage
            attachment_instance.file.save(
                file_name,
                ContentFile(file_content),
                save=False  # Don't save the model instance yet
            )

            stored_attachments.append(attachment_instance)

        return stored_attachments

    def persist_notification(
        self,
        user_id: int | str | uuid.UUID,
        notification_type: str,
        title: str,
        body_template: str,
        context_name: str,
        context_kwargs: dict[str, uuid.UUID | str | int],
        send_after: datetime.datetime | None,
        subject_template: str | None = None,
        preheader_template: str | None = None,
        adapter_extra_parameters: dict | None = None,
        attachments: list[NotificationAttachment] | None = None,
    ) -> Notification:
        notification_instance = NotificationModel.objects.create(
            user_id=str(user_id),
            notification_type=notification_type,
            title=title,
            body_template=body_template,
            context_name=context_name,
            context_kwargs=context_kwargs,
            send_after=send_after,
            subject_template=subject_template or "",
            preheader_template=preheader_template or "",
            adapter_extra_parameters=adapter_extra_parameters,
        )

        # Store attachments relationship
        if attachments:
            stored_attachments = self._store_attachments(attachments)
            for attachment in stored_attachments:
                attachment.notification = notification_instance
                attachment.save()

        return self.serialize_user_notification(notification_instance)

    def persist_one_off_notification(
        self,
        email_or_phone: str,
        first_name: str,
        last_name: str,
        notification_type: str,
        title: str,
        body_template: str,
        context_name: str,
        context_kwargs: dict[str, uuid.UUID | str | int],
        send_after: datetime.datetime | None = None,
        subject_template: str = "",
        preheader_template: str = "",
        adapter_extra_parameters: dict | None = None,
        attachments: list[NotificationAttachment] | None = None,
    ) -> OneOffNotification:
        """Create and store a one-off notification"""

        notification_instance = NotificationModel.objects.create(
            user=None,  # No user for one-off notifications
            email_or_phone=email_or_phone,
            first_name=first_name,
            last_name=last_name,
            notification_type=notification_type,
            title=title,
            body_template=body_template,
            context_name=context_name,
            context_kwargs=context_kwargs,
            send_after=send_after,
            subject_template=subject_template or "",
            preheader_template=preheader_template or "",
            adapter_extra_parameters=adapter_extra_parameters,
        )

        # Store attachments relationship
        if attachments:
            stored_attachments = self._store_attachments(attachments)
            for attachment in stored_attachments:
                attachment.notification = notification_instance
                attachment.save()

        return self.serialize_one_off_notification(notification_instance)

    def persist_notification_update(
        self, notification_id: int | str | uuid.UUID, updated_data: UpdateNotificationKwargs
    ) -> Notification | OneOffNotification:
        records_updated = NotificationModel.objects.filter(
            id=str(notification_id), status=NotificationStatus.PENDING_SEND.value
        ).update(**updated_data)

        if records_updated == 0:
            raise NotificationUpdateError(
                "Failed to update notification, it may have already been sent"
            )
        return self.serialize_notification(NotificationModel.objects.get(id=str(notification_id)))

    def mark_pending_as_sent(self, notification_id: int | str | uuid.UUID) -> Notification | OneOffNotification:
        records_updated = NotificationModel.objects.filter(
            id=str(notification_id), status=NotificationStatus.PENDING_SEND.value
        ).update(status=NotificationStatus.SENT.value)
        if records_updated == 0:
            raise NotificationUpdateError("Failed to update notification status")
        return self.serialize_notification(NotificationModel.objects.get(id=str(notification_id)))

    def mark_pending_as_failed(self, notification_id: int | str | uuid.UUID) -> Notification | OneOffNotification:
        records_updated = NotificationModel.objects.filter(
            id=str(notification_id), status=NotificationStatus.PENDING_SEND.value
        ).update(status=NotificationStatus.FAILED.value)
        if records_updated == 0:
            raise NotificationUpdateError("Failed to update notification status")
        return self.serialize_notification(NotificationModel.objects.get(id=str(notification_id)))

    def mark_sent_as_read(self, notification_id: int | str | uuid.UUID) -> Notification | OneOffNotification:
        records_updated = NotificationModel.objects.filter(
            id=str(notification_id), status=NotificationStatus.SENT.value
        ).update(status=NotificationStatus.READ.value)
        if records_updated == 0:
            raise NotificationUpdateError("Failed to update notification status")
        return self.serialize_notification(NotificationModel.objects.get(id=str(notification_id)))

    def cancel_notification(self, notification_id: int | str | uuid.UUID) -> None:
        records_updated = NotificationModel.objects.filter(
            id=str(notification_id), status=NotificationStatus.PENDING_SEND.value
        ).update(status=NotificationStatus.CANCELLED.value)

        if records_updated == 0:
            raise NotificationCancelError("Failed to delete notification")

    def get_notification(
        self, notification_id: int | str | uuid.UUID, for_update=False
    ) -> Notification | OneOffNotification:
        """Get notification by ID, supporting both regular and one-off notifications"""
        queryset = NotificationModel.objects.exclude(status=NotificationStatus.CANCELLED.value)

        if for_update:
            queryset = queryset.select_for_update()
        try:
            notification_instance = queryset.get(id=str(notification_id))
        except NotificationModel.DoesNotExist as e:
            raise NotificationNotFoundError("Notification not found") from e

        # Check if it's a one-off notification (no user) or regular notification
        return self.serialize_notification(notification_instance)

    def _get_one_off_notification(self, notification_id: int | str | uuid.UUID) -> OneOffNotification:
        """Retrieve one-off notification from storage"""
        try:
            notification_instance = NotificationModel.objects.exclude(
                status=NotificationStatus.CANCELLED.value
            ).get(id=str(notification_id), user__isnull=True)
        except NotificationModel.DoesNotExist as e:
            raise NotificationNotFoundError(f"One-off notification {notification_id} not found") from e

        return self.serialize_one_off_notification(notification_instance)

    def get_all_pending_notifications(self) -> Iterable[Notification | OneOffNotification]:
        """Return both regular notifications and one-off notifications that are pending"""
        queryset = self._get_all_pending_notifications_queryset()

        # Separate regular notifications (with user) from one-off notifications (without user)
        all_notifications: list[Notification | OneOffNotification] = []

        for notification in queryset:
            if notification.user:
                all_notifications.append(self.serialize_notification(notification))
            else:
                all_notifications.append(self.serialize_one_off_notification(notification))

        return all_notifications

    def get_pending_notifications(self, page: int, page_size: int) -> Iterable[Notification | OneOffNotification]:
        return self._serialize_notification_queryset(
            self._paginate_queryset(
                self._get_all_pending_notifications_queryset(),
                page,
                page_size,
            )
        )

    def filter_all_in_app_unread_notifications(
        self,
        user_id: int | str | uuid.UUID,
    ) -> Iterable[Notification]:
        return self._serialize_user_notification_queryset(
            self._get_all_in_app_unread_notifications_queryset(user_id),
        )

    def filter_in_app_unread_notifications(
        self,
        user_id: int | str | uuid.UUID,
        page: int = 1,
        page_size: int = 10,
    ) -> Iterable[Notification]:
        return self._serialize_user_notification_queryset(
            self._paginate_queryset(
                self._get_all_in_app_unread_notifications_queryset(user_id),
                page,
                page_size,
            )
        )

    def get_all_future_notifications(self) -> Iterable["Notification | OneOffNotification"]:
        return self._serialize_notification_queryset(self._get_all_future_notifications_queryset())

    def get_future_notifications(self, page: int, page_size: int) -> Iterable["Notification | OneOffNotification"]:
        return self._serialize_notification_queryset(
            self._paginate_queryset(self._get_all_future_notifications_queryset(), page, page_size)
        )

    def get_all_future_notifications_from_user(
        self, user_id: int | str | uuid.UUID
    ) -> Iterable["Notification | OneOffNotification"]:
        return self._serialize_user_notification_queryset(
            self._get_all_future_notifications_queryset().filter(user_id=str(user_id))
        )

    def get_future_notifications_from_user(
        self, user_id: int | str | uuid.UUID, page: int, page_size: int
    ) -> Iterable["Notification | OneOffNotification"]:
        return self._serialize_user_notification_queryset(
            self._paginate_queryset(
                self._get_all_future_notifications_queryset().filter(user_id=str(user_id)),
                page,
                page_size,
            )
        )

    def get_user_email_from_notification(self, notification_id: int | str | uuid.UUID) -> str:
        notification_user = (
            NotificationModel.objects.select_related("user").get(id=str(notification_id)).user
        )
        if not notification_user or not notification_user.is_active:
            raise NotificationUserNotFoundError("User not found")
        return notification_user.email

    def store_context_used(
        self,
        notification_id: int | str | uuid.UUID,
        context: dict,
        adapter_import_str: str,
    ) -> None:
        NotificationModel.objects.filter(id=str(notification_id)).update(
            context_used=context, adapter_used=adapter_import_str
        )
