import datetime
import uuid
from collections.abc import Iterable

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
    UpdateNotificationKwargs,
)
from vintasend.services.notification_backends.base import BaseNotificationBackend

from vintasend_django.models import Notification as NotificationModel


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

    def _serialize_notification_queryset(
        self, queryset: "QuerySet[NotificationModel]"
    ) -> Iterable[Notification]:
        return (self.serialize_notification(n) for n in queryset.iterator())

    def serialize_notification(self, notification: NotificationModel) -> Notification:
        return Notification(
            id=notification.pk,
            user_id=notification.user.id,
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
        return self.serialize_notification(notification_instance)

    def persist_notification_update(
        self, notification_id: int | str | uuid.UUID, updated_data: UpdateNotificationKwargs
    ) -> Notification:
        records_updated = NotificationModel.objects.filter(
            id=str(notification_id), status=NotificationStatus.PENDING_SEND.value
        ).update(**updated_data)

        if records_updated == 0:
            raise NotificationUpdateError(
                "Failed to update notification, it may have already been sent"
            )
        return self.serialize_notification(NotificationModel.objects.get(id=str(notification_id)))

    def mark_pending_as_sent(self, notification_id: int | str | uuid.UUID) -> Notification:
        records_updated = NotificationModel.objects.filter(
            id=str(notification_id), status=NotificationStatus.PENDING_SEND.value
        ).update(status=NotificationStatus.SENT.value)
        if records_updated == 0:
            raise NotificationUpdateError("Failed to update notification status")
        return self.serialize_notification(NotificationModel.objects.get(id=str(notification_id)))

    def mark_pending_as_failed(self, notification_id: int | str | uuid.UUID) -> Notification:
        records_updated = NotificationModel.objects.filter(
            id=str(notification_id), status=NotificationStatus.PENDING_SEND.value
        ).update(status=NotificationStatus.FAILED.value)
        if records_updated == 0:
            raise NotificationUpdateError("Failed to update notification status")
        return self.serialize_notification(NotificationModel.objects.get(id=str(notification_id)))

    def mark_sent_as_read(self, notification_id: int | str | uuid.UUID) -> Notification:
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
    ) -> Notification:
        queryset = NotificationModel.objects.exclude(status=NotificationStatus.CANCELLED.value)

        if for_update:
            queryset = queryset.select_for_update()
        try:
            notification_instance = queryset.get(id=str(notification_id))
        except NotificationModel.DoesNotExist as e:
            raise NotificationNotFoundError("Notification not found") from e
        return self.serialize_notification(notification_instance)

    def get_all_pending_notifications(self) -> Iterable[Notification]:
        return self._serialize_notification_queryset(self._get_all_pending_notifications_queryset())

    def get_pending_notifications(self, page: int, page_size: int) -> Iterable[Notification]:
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
        return self._serialize_notification_queryset(
            self._get_all_in_app_unread_notifications_queryset(user_id),
        )

    def filter_in_app_unread_notifications(
        self,
        user_id: int | str | uuid.UUID,
        page: int = 1,
        page_size: int = 10,
    ) -> Iterable[Notification]:
        return self._serialize_notification_queryset(
            self._paginate_queryset(
                self._get_all_in_app_unread_notifications_queryset(user_id),
                page,
                page_size,
            )
        )

    def get_all_future_notifications(self) -> Iterable["Notification"]:
        return self._serialize_notification_queryset(self._get_all_future_notifications_queryset())

    def get_future_notifications(self, page: int, page_size: int) -> Iterable["Notification"]:
        return self._serialize_notification_queryset(
            self._paginate_queryset(self._get_all_future_notifications_queryset(), page, page_size)
        )

    def get_all_future_notifications_from_user(
        self, user_id: int | str | uuid.UUID
    ) -> Iterable["Notification"]:
        return self._serialize_notification_queryset(
            self._get_all_future_notifications_queryset().filter(user_id=str(user_id))
        )

    def get_future_notifications_from_user(
        self, user_id: int | str | uuid.UUID, page: int, page_size: int
    ) -> Iterable["Notification"]:
        return self._serialize_notification_queryset(
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
