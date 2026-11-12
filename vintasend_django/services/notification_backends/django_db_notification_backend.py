import datetime
import uuid
from collections.abc import Iterable, Mapping
from typing import cast

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management.color import no_style
from django.db import connections, models, transaction
from django.db.models import Case, F, Q, QuerySet, Value, When
from django.db.models.fields.json import KeyTransform
from django.utils import timezone

from vintasend.constants import (
    REPINNABLE_NOTIFICATION_STATUSES,
    REQUEUEABLE_NOTIFICATION_STATUSES,
    UNSET,
    NotificationStatus,
    NotificationTypes,
    UnsetType,
)
from vintasend.exceptions import (
    AttachmentFileNotFoundError,
    NotificationCancelError,
    NotificationNotFoundError,
    NotificationRepinError,
    NotificationRequeueError,
    NotificationUpdateError,
    NotificationUserNotFoundError,
    NotificationVersionChangedError,
)
from vintasend.services.attachment_managers.base import BaseAttachmentManager
from vintasend.services.dataclasses import (
    AnyNotificationAttachment,
    ApplyResult,
    AttachmentFileRecord,
    Notification,
    NotificationAttachment,
    NotificationVersion,
    OneOffNotification,
    StoredAttachment,
    UpdateNotificationKwargs,
    is_attachment_reference,
)
from vintasend.services.notification_backends.base import BaseNotificationBackend
from vintasend.services.notification_backends.filters import (
    NotificationFilter,
    NotificationOrderBy,
    is_field_filter,
)

from vintasend_django.models import AttachmentFileRecord as AttachmentFileRecordModel
from vintasend_django.models import Notification as NotificationModel
from vintasend_django.models import NotificationAttachment as NotificationAttachmentModel
from vintasend_django.models import new_notification_version
from vintasend_django.services.attachment_managers.django_storage import DjangoAttachmentManager
from vintasend_django.services.notification_backends.filters import (
    MATCH_NOTHING,
    ORDER_FIELD_TO_ATTR,
    and_all,
    field_leaf,
    or_all,
)


# The presence filters default to unsupported in vintasend; this backend evaluates them.
PRESENCE_FILTER_CAPABILITIES: dict[str, bool] = {
    "fields.hasSendAfter": True,
    "negation.hasSendAfter": True,
    "fields.hasRequestedTemplateVersion": True,
    "negation.hasRequestedTemplateVersion": True,
}
ACTION_CAPABILITIES: dict[str, bool] = {
    "repin.expectedVersion": True,
    "requeue.expectedVersion": True,
    "requeue.templateVersion": True,
    "versions.filter": True,
    "writes.notificationVersion": True,
}


def _changed_error(
    action: str, notification_id: int | str | uuid.UUID
) -> NotificationVersionChangedError:
    return NotificationVersionChangedError(
        f"Cannot {action} notification {notification_id}: it changed since that version was read"
    )


def _parse_expected_version(expected_version: object) -> uuid.UUID | None:
    """The ``notification_version`` an ``expected_version`` names, or ``None`` when none was given.

    Tokens are handed out as ``str(uuid)``. Anything else (a malformed string, a non-string, or
    the empty string listed for a row with no token yet) is a token this backend could not have
    handed out. It becomes a fresh token no row holds rather than an error here, so the
    conditional UPDATE matches nothing and the refusal read reports not-found before changed,
    the same order as a well-formed token that no longer matches.
    """
    if expected_version is None:
        return None
    if not isinstance(expected_version, str):
        return new_notification_version()
    try:
        parsed = uuid.UUID(expected_version)
    except ValueError:
        return new_notification_version()
    if str(parsed) != expected_version:
        return new_notification_version()
    return parsed


def _token_for_write(notification_version: object) -> uuid.UUID:
    """The ``notification_version`` a write stores.

    The primary's token, passed to a replica's write (``writes.notificationVersion``) or carried
    by a replication snapshot, parsed as a UUID. With none, or one that is not a UUID, a fresh
    UUIDv7, the same as any write that is not handed a token.
    """
    if not isinstance(notification_version, str):
        return new_notification_version()
    try:
        return uuid.UUID(notification_version)
    except ValueError:
        return new_notification_version()


def reset_notification_id_sequence(database: str = "default") -> None:
    """Move the notification id sequence on ``database`` past the highest id stored there.

    A row inserted with an explicit id, as ``apply_replication_snapshot_if_newer`` inserts the
    primary's, does not advance the id sequence on PostgreSQL or Oracle, so a replica later
    promoted to primary would fail its first insert on an id it already holds. Runs Django's
    ``sequence_reset_sql`` for the notification table on that alias, which is nothing on
    databases whose ids follow the highest one stored (SQLite, MySQL).

    The snapshot apply runs it after each insert. A sequence reset is not transactional, though:
    two applies inserting concurrently can leave it at the lower of their ids. Run this once
    more, with no replication running, before promoting a replica (or pipe
    ``manage.py sqlsequencereset vintasend_django --database=<alias>`` into ``dbshell``).
    """
    statements = connections[database].ops.sequence_reset_sql(no_style(), [NotificationModel])
    if not statements:
        return
    with connections[database].cursor() as cursor:
        for statement in statements:
            cursor.execute(statement)


def _serialize_version(notification_version: uuid.UUID | None) -> str | None:
    """The row's token as the dataclasses carry it; ``None`` on a row inserted by code predating
    the column, between migrations 0009 and 0010."""
    return None if notification_version is None else str(notification_version)


class DjangoDbNotificationBackend(BaseNotificationBackend):
    """Notifications stored in the ``vintasend_django`` models through the Django ORM.

    Every write to a notification row sets a fresh ``notification_version`` (a UUIDv7), the
    token ``filter_notification_versions`` hands out and ``requeue_notification`` /
    ``repin_notification`` check as ``expected_version``. ``QuerySet.update()`` does not run
    ``save()`` or field defaults, so every ``update()`` here sets it explicitly, and sets
    ``modified`` to now with it (``update()`` skips the auto-modified field too). Two exceptions:
    ``apply_replication_snapshot_if_newer``, which stores the snapshot's ``modified``, since
    newer-wins replication compares it; and the token-only stamp
    (``persist_notification_update(id, {}, notification_version=T)``), which writes the given
    token and nothing else, in any status.

    Options, keyword-only:

    * ``database``: the Django database alias every query, transaction and save of this backend
      runs on (``"default"`` unless given). Two instances on two aliases are two backends, e.g.
      a primary and a replica in a multi-backend ``NotificationService``.
    * ``backend_identifier``: the stable identifier ``get_backend_identifier()`` returns, which a
      multi-backend ``NotificationService`` addresses this backend by. ``None`` lets the service
      fall back to its positional ``backend-{n}``.

    Both are kept in ``backend_kwargs`` when they are not the defaults, so a backend rebuilt
    from them (a task queue worker, for instance) runs on the same alias.

    **Replication.** As a replica it applies the primary's snapshots
    (``apply_replication_snapshot_if_newer``), and takes the token-only stamp the service writes
    once a replayed row matches the primary's (``writes.notificationVersion``), so it lists the
    primary's tokens. Rows it inserts keep the primary's ids, so it moves the id sequence past
    them (see ``reset_notification_id_sequence``) and can be promoted to primary.
    Attachment bytes are never copied: a replica links the file records the snapshot names,
    which assumes both backends' attachment managers read the same storage.
    """

    def __init__(
        self,
        *args,
        database: str = "default",
        backend_identifier: str | None = None,
        **kwargs,
    ):
        if database != "default":
            kwargs["database"] = database
        if backend_identifier is not None:
            kwargs["backend_identifier"] = backend_identifier
        super().__init__(*args, **kwargs)
        self.database = database
        self.backend_identifier = backend_identifier
        # Default to a Django-storage-backed manager so the backend is usable standalone; the
        # service replaces this through inject_attachment_manager when one is configured.
        # It never touches the database: rows are this backend's, bytes are the manager's.
        self._attachment_manager: BaseAttachmentManager = DjangoAttachmentManager()

    def get_backend_identifier(self) -> str | None:
        return self.backend_identifier

    # ------------------------------------------------------------------ database routing

    def _notifications(self) -> QuerySet["NotificationModel"]:
        return NotificationModel.objects.using(self.database)

    def _file_records(self) -> QuerySet["AttachmentFileRecordModel"]:
        return AttachmentFileRecordModel.objects.using(self.database)

    def _join_rows(self) -> QuerySet["NotificationAttachmentModel"]:
        return NotificationAttachmentModel.objects.using(self.database)

    def _atomic(self) -> transaction.Atomic:
        return transaction.atomic(using=self.database)

    def _insert_notification(
        self, notification_version: str | None, **columns: object
    ) -> NotificationModel:
        """INSERT one notification row on this backend's database, holding the given token.

        Saves through ``models.Model.save`` rather than ``NotificationModel.save``: the override
        replaces the token on every save, and this INSERT must keep the one it was given. With
        no token given the column's default mints one, as it does for any other INSERT.
        """
        if notification_version is not None:
            columns["notification_version"] = _token_for_write(notification_version)
        instance = NotificationModel(**columns)
        models.Model.save(instance, force_insert=True, using=self.database)
        return instance

    def _get_all_future_notifications_queryset(self) -> QuerySet["NotificationModel"]:
        return (
            self._notifications()
            .filter(
                Q(send_after__gte=timezone.now()) | Q(send_after__isnull=False),
                status=NotificationStatus.PENDING_SEND.value,
            )
            .order_by("created")
        )

    def _get_all_in_app_unread_notifications_queryset(
        self, user_id: int | str | uuid.UUID
    ) -> QuerySet["NotificationModel"]:
        return (
            self._notifications()
            .filter(
                user_id=str(user_id),
                status=NotificationStatus.SENT.value,
                notification_type=NotificationTypes.IN_APP.value,
            )
            .order_by("-created", "-id")
        )

    def _get_all_in_app_notifications_queryset(
        self, user_id: int | str | uuid.UUID
    ) -> QuerySet["NotificationModel"]:
        """Read + unread in-app notifications (SENT or READ), newest-first.

        Excludes internal pipeline states (PENDING_SEND, FAILED, CANCELLED) so
        they are never exposed to end users.
        """
        return (
            self._notifications()
            .filter(
                user_id=str(user_id),
                notification_type=NotificationTypes.IN_APP.value,
                status__in=[NotificationStatus.SENT.value, NotificationStatus.READ.value],
            )
            .order_by("-created", "-id")
        )

    def _get_all_pending_notifications_queryset(self) -> QuerySet["NotificationModel"]:
        return (
            self._notifications()
            .filter(
                Q(send_after__lte=timezone.now()) | Q(send_after__isnull=True),
                status=NotificationStatus.PENDING_SEND.value,
            )
            .order_by("created")
        )

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

    def serialize_notification(
        self, notification: NotificationModel
    ) -> Notification | OneOffNotification:
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
            context_used=notification.context_used,
            adapter_used=notification.adapter_used or None,
            adapter_extra_parameters=notification.adapter_extra_parameters,
            created=notification.created,
            modified=notification.modified,
            sent_at=notification.sent_at,
            read_at=notification.read_at,
            tenant=notification.tenant,
            git_commit_sha=notification.git_commit_sha,
            requested_template_version=notification.requested_template_version,
            used_template_version=notification.used_template_version,
            attachments=list(self.get_attachments(notification.pk)),
            notification_version=_serialize_version(notification.notification_version),
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
            context_used=notification.context_used,
            adapter_used=notification.adapter_used or None,
            adapter_extra_parameters=notification.adapter_extra_parameters,
            created=notification.created,
            modified=notification.modified,
            sent_at=notification.sent_at,
            read_at=notification.read_at,
            tenant=notification.tenant,
            git_commit_sha=notification.git_commit_sha,
            requested_template_version=notification.requested_template_version,
            used_template_version=notification.used_template_version,
            attachments=list(self.get_attachments(notification.pk)),
            notification_version=_serialize_version(notification.notification_version),
        )

    # ------------------------------------------------------------------ attachments

    def _serialize_file_record(self, record: AttachmentFileRecordModel) -> AttachmentFileRecord:
        """Convert an ``AttachmentFileRecord`` model row to its dataclass.

        ``id`` is the model's own pk (as a string), which is what every ``file_id`` the
        backend hands out or accepts refers to. The ``storage_identifiers`` are opaque and
        only ever handed back to the injected attachment manager.
        """
        return AttachmentFileRecord(
            id=str(record.pk),
            filename=record.filename,
            content_type=record.content_type,
            size=record.size,
            checksum=record.checksum,
            created_at=record.created,
            updated_at=record.modified,
            storage_identifiers=record.storage_identifiers or {},
        )

    def _stored_attachment_from_join_row(
        self, join_row: NotificationAttachmentModel, record: AttachmentFileRecordModel
    ) -> StoredAttachment:
        manager = self._attachment_manager
        attachment_file = manager.reconstruct_attachment_file(record.storage_identifiers or {})
        return StoredAttachment(
            id=str(join_row.pk),
            filename=record.filename,
            content_type=record.content_type,
            size=record.size,
            checksum=record.checksum,
            created_at=record.created,
            file=attachment_file,
            description=join_row.description,
            is_inline=join_row.is_inline,
            file_id=str(record.pk),
            storage_identifiers=record.storage_identifiers or {},
        )

    def _store_attachments(
        self,
        attachments: list[AnyNotificationAttachment],
        notification_id: int | str | uuid.UUID,
    ) -> list[StoredAttachment]:
        """Persist attachments by delegating every byte operation to the injected manager.

        Uploads are deduplicated on (checksum, size): a matching existing file record is
        reused and no upload happens; otherwise the manager stores the bytes and the new
        record is persisted. A reference attaches an already-stored file by id, raising
        ``AttachmentFileNotFoundError`` if that id is unknown. Either path writes one join
        row; the returned handle is always rebuilt through the manager.
        """
        manager = self._attachment_manager
        stored_attachments: list[StoredAttachment] = []

        for attachment in attachments:
            if is_attachment_reference(attachment):
                try:
                    record = self._file_records().get(pk=attachment.file_id)
                except (AttachmentFileRecordModel.DoesNotExist, ValueError, TypeError) as e:
                    raise AttachmentFileNotFoundError(
                        f"No attachment file record found for file_id={attachment.file_id!r}"
                    ) from e
                join_row = self._join_rows().create(
                    notification_id=str(notification_id),
                    file=record,
                    description=attachment.description,
                    is_inline=attachment.is_inline,
                )
                stored_attachments.append(self._stored_attachment_from_join_row(join_row, record))
                continue

            # TypeGuard narrows only the reference branch, so restate the upload type.
            assert isinstance(attachment, NotificationAttachment)  # noqa: S101

            # Read the bytes once, up front, so the checksum lookup and (on a miss) the
            # upload never re-read the same path/URL/stream twice.
            data = manager.file_to_bytes(attachment.file)
            checksum = manager.calculate_checksum(data)
            existing = self._file_records().filter(checksum=checksum, size=len(data)).first()
            if existing is not None:
                record = existing
            else:
                file_record = manager.upload_file(
                    data, attachment.filename, attachment.content_type
                )
                record = self._file_records().create(
                    filename=file_record.filename,
                    content_type=file_record.content_type or "",
                    size=file_record.size,
                    checksum=file_record.checksum,
                    storage_identifiers=file_record.storage_identifiers,
                )

            join_row = self._join_rows().create(
                notification_id=str(notification_id),
                file=record,
                description=attachment.description,
                is_inline=attachment.is_inline,
            )
            stored_attachments.append(self._stored_attachment_from_join_row(join_row, record))

        return stored_attachments

    def _store_attachments_and_move_version(
        self,
        attachments: list[AnyNotificationAttachment],
        notification_id: int | str | uuid.UUID,
        notification_version: str | None = None,
    ) -> list[StoredAttachment]:
        """``_store_attachments`` for a just-created row, then move its version token once.

        Linking attachments changes the notification, so, as everywhere else, it moves the token:
        the one the row was created with may already have been listed. One transaction, so a link
        that fails leaves neither join rows nor a moved token behind. A given
        ``notification_version`` (the primary's) is the token it moves to.
        """
        with self._atomic():
            stored = self._store_attachments(attachments, notification_id)
            if stored:
                self._notifications().filter(id=str(notification_id)).update(
                    notification_version=_token_for_write(notification_version),
                    modified=timezone.now(),
                )
        return stored

    def _attach_stored_attachments(
        self,
        notification_id: int | str | uuid.UUID,
        attachments: list[StoredAttachment],
    ) -> None:
        """Link already-stored files to a notification by writing join rows only.

        Used by ``persist_notification_update`` (the resend path): each ``StoredAttachment``
        already points at a persisted ``AttachmentFileRecord`` via ``file_id``, so there is
        no upload -- only a new join row per attachment.
        """
        for attachment in attachments:
            file_id = str(attachment.file_id or attachment.id)
            try:
                record = self._file_records().get(pk=file_id)
            except (AttachmentFileRecordModel.DoesNotExist, ValueError, TypeError) as e:
                raise AttachmentFileNotFoundError(
                    f"No attachment file record found for file_id={file_id!r}"
                ) from e
            self._join_rows().create(
                notification_id=str(notification_id),
                file=record,
                description=attachment.description,
                is_inline=attachment.is_inline,
            )

    def store_attachment_file_record(self, record: AttachmentFileRecord) -> AttachmentFileRecord:
        instance = self._file_records().create(
            filename=record.filename,
            content_type=record.content_type or "",
            size=record.size,
            checksum=record.checksum,
            storage_identifiers=record.storage_identifiers,
        )
        return self._serialize_file_record(instance)

    def get_attachment_file_record(self, file_id: str) -> AttachmentFileRecord | None:
        try:
            instance = self._file_records().get(pk=file_id)
        except (AttachmentFileRecordModel.DoesNotExist, ValueError, TypeError):
            return None
        return self._serialize_file_record(instance)

    def find_attachment_file_by_checksum(
        self, checksum: str, size: int
    ) -> AttachmentFileRecord | None:
        instance = self._file_records().filter(checksum=checksum, size=size).first()
        if instance is None:
            return None
        return self._serialize_file_record(instance)

    def delete_attachment_file(self, file_id: str) -> None:
        self._file_records().filter(pk=file_id).delete()

    def get_orphaned_attachment_files(self) -> Iterable[AttachmentFileRecord]:
        """Return file records no longer referenced by any notification join row.

        Reclaiming one is a caller-driven, two-step operation this only surfaces candidates
        for: ``manager.delete_file_by_identifiers(record.storage_identifiers)`` to remove the
        bytes, then ``backend.delete_attachment_file(record.id)`` to drop the row. Nothing
        here deletes anything automatically.
        """
        orphaned = self._file_records().filter(notification_attachments__isnull=True)
        return [self._serialize_file_record(record) for record in orphaned.iterator()]

    def get_attachments(self, notification_id: int | str | uuid.UUID) -> Iterable[StoredAttachment]:
        join_rows = (
            self._join_rows().filter(notification_id=str(notification_id)).select_related("file")
        )
        return [
            self._stored_attachment_from_join_row(join_row, join_row.file)
            for join_row in join_rows.iterator()
        ]

    def delete_notification_attachment(
        self, attachment_id: int | str | uuid.UUID, *, notification_version: str | None = None
    ) -> None:
        """Delete a single notification attachment join row by its own id.

        Drops only the join row, never the ``AttachmentFileRecord`` or its bytes -- a file
        may still back other notifications. Reclaiming an orphaned file is a separate,
        caller-driven step via ``get_orphaned_attachment_files``.

        The notification it was attached to changes with it, so its version token moves, to
        ``notification_version`` when given.
        """
        join_rows = self._join_rows().filter(pk=str(attachment_id))
        with self._atomic():
            notification_ids = list(join_rows.values_list("notification_id", flat=True))
            join_rows.delete()
            if notification_ids:
                self._notifications().filter(id__in=notification_ids).update(
                    notification_version=_token_for_write(notification_version),
                    modified=timezone.now(),
                )

    # ------------------------------------------------------------------ persistence

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
        attachments: list[AnyNotificationAttachment] | None = None,
        tenant: str | None = None,
        requested_template_version: int | None = None,
        *,
        notification_version: str | None = None,
    ) -> Notification:
        notification_instance = self._insert_notification(
            notification_version,
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
            tenant=tenant,
            requested_template_version=requested_template_version,
        )

        if attachments:
            self._store_attachments_and_move_version(
                attachments, notification_instance.pk, notification_version
            )
            notification_instance.refresh_from_db(fields=["notification_version", "modified"])

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
        attachments: list[AnyNotificationAttachment] | None = None,
        tenant: str | None = None,
        requested_template_version: int | None = None,
        *,
        notification_version: str | None = None,
    ) -> OneOffNotification:
        """Create and store a one-off notification"""

        notification_instance = self._insert_notification(
            notification_version,
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
            tenant=tenant,
            requested_template_version=requested_template_version,
        )

        if attachments:
            self._store_attachments_and_move_version(
                attachments, notification_instance.pk, notification_version
            )
            notification_instance.refresh_from_db(fields=["notification_version", "modified"])

        return self.serialize_one_off_notification(notification_instance)

    def persist_notification_update(
        self,
        notification_id: int | str | uuid.UUID,
        update_data: UpdateNotificationKwargs,
        *,
        notification_version: str | None = None,
    ) -> Notification | OneOffNotification:
        # ``attachments`` is not a scalar column; it is a set of already-stored files to link
        # via join rows (the resend path), so pull it out before the row ``update``.
        if not update_data and notification_version is not None:
            return self._stamp_notification_version(notification_id, notification_version)

        values = dict(update_data)
        attachments = cast("list[StoredAttachment] | None", values.pop("attachments", None))

        # A notification that did not go out can be updated: repin_notification moves the pin
        # on FAILED and CANCELLED notifications too. A delivered one cannot.
        pending = self._notifications().filter(
            id=str(notification_id), status__in=REPINNABLE_NOTIFICATION_STATUSES
        )
        # A given token is written even with nothing else to change: the row must hold it after.
        if values or attachments or notification_version is not None:
            # Linking attachments changes the notification too, so it moves the version token
            # as well. One transaction, so a link that fails leaves the token where it was.
            with self._atomic():
                records_updated = pending.update(
                    **values,
                    modified=timezone.now(),
                    notification_version=_token_for_write(notification_version),
                )
                if records_updated == 0:
                    raise NotificationUpdateError(
                        "Failed to update notification, it may have already been sent"
                    )
                if attachments:
                    self._attach_stored_attachments(notification_id, attachments)
        elif not pending.exists():
            raise NotificationUpdateError(
                "Failed to update notification, it may have already been sent"
            )

        return self.serialize_notification(self._notifications().get(id=str(notification_id)))

    def _stamp_notification_version(
        self, notification_id: int | str | uuid.UUID, notification_version: str
    ) -> Notification | OneOffNotification:
        """The token-only write: ``persist_notification_update(id, {}, notification_version=T)``.

        How the service stamps a converged replica row with the primary's token. Accepted in any
        status, SENT and READ included, and it writes the token and nothing else, ``modified``
        included: the row's content did not change, and newer-wins replication compares
        ``modified``.
        """
        records_updated = (
            self._notifications()
            .filter(id=str(notification_id))
            .update(notification_version=_token_for_write(notification_version))
        )
        if records_updated == 0:
            raise NotificationNotFoundError(f"Notification {notification_id} not found")
        return self.serialize_notification(self._notifications().get(id=str(notification_id)))

    def mark_pending_as_sent(
        self, notification_id: int | str | uuid.UUID, *, notification_version: str | None = None
    ) -> Notification | OneOffNotification:
        records_updated = (
            self._notifications()
            .filter(id=str(notification_id), status=NotificationStatus.PENDING_SEND.value)
            .update(
                status=NotificationStatus.SENT.value,
                sent_at=timezone.now(),
                notification_version=_token_for_write(notification_version),
                modified=timezone.now(),
            )
        )
        if records_updated == 0:
            raise NotificationUpdateError("Failed to update notification status")
        return self.serialize_notification(self._notifications().get(id=str(notification_id)))

    def mark_pending_as_failed(
        self, notification_id: int | str | uuid.UUID, *, notification_version: str | None = None
    ) -> Notification | OneOffNotification:
        records_updated = (
            self._notifications()
            .filter(id=str(notification_id), status=NotificationStatus.PENDING_SEND.value)
            .update(
                status=NotificationStatus.FAILED.value,
                notification_version=_token_for_write(notification_version),
                modified=timezone.now(),
            )
        )
        if records_updated == 0:
            raise NotificationUpdateError("Failed to update notification status")
        return self.serialize_notification(self._notifications().get(id=str(notification_id)))

    def mark_sent_as_read(
        self, notification_id: int | str | uuid.UUID, *, notification_version: str | None = None
    ) -> Notification | OneOffNotification:
        records_updated = (
            self._notifications()
            .filter(id=str(notification_id), status=NotificationStatus.SENT.value)
            .update(
                status=NotificationStatus.READ.value,
                read_at=timezone.now(),
                notification_version=_token_for_write(notification_version),
                modified=timezone.now(),
            )
        )
        if records_updated == 0:
            raise NotificationUpdateError("Failed to update notification status")
        return self.serialize_notification(self._notifications().get(id=str(notification_id)))

    def cancel_notification(
        self, notification_id: int | str | uuid.UUID, *, notification_version: str | None = None
    ) -> None:
        records_updated = (
            self._notifications()
            .filter(id=str(notification_id), status=NotificationStatus.PENDING_SEND.value)
            .update(
                status=NotificationStatus.CANCELLED.value,
                notification_version=_token_for_write(notification_version),
                modified=timezone.now(),
            )
        )

        if records_updated == 0:
            raise NotificationCancelError("Failed to delete notification")

    def requeue_notification(
        self,
        notification_id: int | str | uuid.UUID,
        send_after: datetime.datetime | UnsetType | None = UNSET,
        *,
        template_version: int | None = None,
        expected_version: str | None = None,
        notification_version: str | None = None,
    ) -> Notification | OneOffNotification:
        """Put a FAILED or CANCELLED notification back into PENDING_SEND, in one conditional UPDATE.

        The status check is part of the UPDATE's WHERE clause, so a notification another caller
        requeued first, or a worker sent in the meantime, is refused rather than overwritten.
        ``template_version`` is pinned in the same UPDATE. ``expected_version`` (a token from
        ``filter_notification_versions``) joins the WHERE clause too, so a notification written
        since it was listed is refused with ``NotificationVersionChangedError``.

        When the UPDATE matches nothing, the row's status and version are read once to say why:
        ``NotificationNotFoundError``, ``NotificationVersionChangedError`` or
        ``NotificationRequeueError``. Nothing is written in any of those cases.

        ``notification_version``, when given, is the token the row holds after the requeue, in
        the same UPDATE; ``expected_version`` is still checked against the current one.
        """
        expected = _parse_expected_version(expected_version)
        values: dict[str, object] = {
            "status": NotificationStatus.PENDING_SEND.value,
            "sent_at": None,
            "modified": timezone.now(),
            "notification_version": _token_for_write(notification_version),
        }
        if send_after is not UNSET:
            values["send_after"] = send_after
        if template_version is not None:
            values["requested_template_version"] = template_version

        conditions = Q(id=str(notification_id), status__in=REQUEUEABLE_NOTIFICATION_STATUSES)
        if expected is not None:
            conditions &= Q(notification_version=expected)
        records_updated = self._notifications().filter(conditions).update(**values)
        if records_updated == 0:
            current = self._get_status_and_version(notification_id)
            if current is None:
                raise NotificationNotFoundError(f"Notification {notification_id} not found")
            status, version = current
            if expected is not None and version != expected:
                raise _changed_error("requeue", notification_id)
            if status in REQUEUEABLE_NOTIFICATION_STATUSES:
                # Every condition of the UPDATE holds on this second look, so the row was
                # written between the two statements (e.g. requeued and failed again): changed.
                raise _changed_error("requeue", notification_id)
            raise NotificationRequeueError(
                f"Cannot requeue notification {notification_id}: it is not FAILED or CANCELLED"
            )
        return self.serialize_notification(self._notifications().get(id=str(notification_id)))

    def repin_notification(
        self,
        notification_id: int | str | uuid.UUID,
        template_version: int,
        *,
        expected_version: str | None = None,
        notification_version: str | None = None,
    ) -> Notification | OneOffNotification:
        """Pin a notification to a template version, in one conditional UPDATE.

        The WHERE clause carries the statuses a pin may still change in (PENDING_SEND, FAILED,
        CANCELLED), ``expected_version`` when given, and "not already pinned to this version",
        so a notification a worker sends in the meantime is never repinned. Only
        ``requested_template_version`` changes, with ``modified`` and the version token.

        When the UPDATE matches nothing, the row is read once to say why, and nothing is
        written: ``NotificationNotFoundError``, ``NotificationVersionChangedError`` (written
        since ``expected_version``), ``NotificationRepinError`` (already delivered), or the
        notification as it is when it is already on ``template_version``. A row that matches
        every condition on that second look changed between the two statements, which is
        ``NotificationVersionChangedError`` too.

        ``notification_version``, when given, is the token the row holds afterwards, in the same
        UPDATE. On a notification already on ``template_version`` it is written on its own, so
        the row still ends on it.
        """
        expected = _parse_expected_version(expected_version)
        # ``requested_template_version != V`` is not true for a NULL, so an unpinned row is
        # named on its own.
        conditions = Q(id=str(notification_id), status__in=REPINNABLE_NOTIFICATION_STATUSES) & (
            Q(requested_template_version__isnull=True)
            | ~Q(requested_template_version=template_version)
        )
        if expected is not None:
            conditions &= Q(notification_version=expected)
        records_updated = (
            self._notifications()
            .filter(conditions)
            .update(
                requested_template_version=template_version,
                modified=timezone.now(),
                notification_version=_token_for_write(notification_version),
            )
        )
        if records_updated == 0:
            try:
                current = self._notifications().get(id=str(notification_id))
            except NotificationModel.DoesNotExist as e:
                raise NotificationNotFoundError(f"Notification {notification_id} not found") from e
            if expected is not None and current.notification_version != expected:
                raise _changed_error("repin", notification_id)
            if current.status not in REPINNABLE_NOTIFICATION_STATUSES:
                raise NotificationRepinError(
                    f"Cannot repin notification {notification_id}: it has already been delivered"
                )
            if current.requested_template_version == template_version:
                if notification_version is not None:
                    # Conditional on the token just read, so a write since is not overwritten.
                    self._notifications().filter(
                        id=current.pk, notification_version=current.notification_version
                    ).update(
                        modified=timezone.now(),
                        notification_version=_token_for_write(notification_version),
                    )
                    current.refresh_from_db()
                return self.serialize_notification(current)
            raise NotificationVersionChangedError(
                f"Cannot repin notification {notification_id}: it changed while it was being "
                "written"
            )
        return self.serialize_notification(self._notifications().get(id=str(notification_id)))

    def _get_status_and_version(
        self, notification_id: int | str | uuid.UUID
    ) -> tuple[str, uuid.UUID | None] | None:
        """The row's status and version token, reading no other column; ``None`` if missing.

        The token is ``None`` on a row inserted by code predating the column, between
        migrations 0009 and 0010.
        """
        return (
            self._notifications()
            .filter(id=str(notification_id))
            .values_list("status", "notification_version")
            .first()
        )

    def get_notification(
        self, notification_id: int | str | uuid.UUID, for_update=False
    ) -> Notification | OneOffNotification:
        """Get notification by ID, supporting both regular and one-off notifications.

        A cancelled notification is returned too, with status CANCELLED, so it can be read and
        requeued. ``filter_notifications`` lists cancelled notifications the same way.
        """
        queryset = self._notifications().all()

        if for_update:
            queryset = queryset.select_for_update()
        try:
            notification_instance = queryset.get(id=str(notification_id))
        except NotificationModel.DoesNotExist as e:
            raise NotificationNotFoundError("Notification not found") from e

        # Check if it's a one-off notification (no user) or regular notification
        return self.serialize_notification(notification_instance)

    def _get_one_off_notification(
        self, notification_id: int | str | uuid.UUID
    ) -> OneOffNotification:
        """Retrieve one-off notification from storage"""
        try:
            notification_instance = self._notifications().get(
                id=str(notification_id), user__isnull=True
            )
        except NotificationModel.DoesNotExist as e:
            raise NotificationNotFoundError(
                f"One-off notification {notification_id} not found"
            ) from e

        return self.serialize_one_off_notification(notification_instance)

    def get_all_pending_notifications(self) -> Iterable[Notification | OneOffNotification]:
        """Return both regular notifications and one-off notifications that are pending"""
        queryset = self._get_all_pending_notifications_queryset()

        # Separate regular notifications (with user) from one-off notifications (without user)
        all_notifications: list[Notification | OneOffNotification] = []

        for notification in queryset:
            if notification.user_id:
                all_notifications.append(self.serialize_notification(notification))
            else:
                all_notifications.append(self.serialize_one_off_notification(notification))

        return all_notifications

    def get_pending_notifications(
        self, page: int, page_size: int
    ) -> Iterable[Notification | OneOffNotification]:
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

    def filter_all_in_app_notifications(
        self,
        user_id: int | str | uuid.UUID,
    ) -> Iterable[Notification]:
        """Unpaginated read + unread in-app notifications.

        Prefer :meth:`filter_in_app_notifications` (paginated) plus
        :meth:`count_in_app_notifications` for end-user listings.
        """
        return self._serialize_user_notification_queryset(
            self._get_all_in_app_notifications_queryset(user_id),
        )

    def filter_in_app_notifications(
        self,
        user_id: int | str | uuid.UUID,
        page: int = 1,
        page_size: int = 10,
    ) -> Iterable[Notification]:
        return self._serialize_user_notification_queryset(
            self._paginate_queryset(
                self._get_all_in_app_notifications_queryset(user_id),
                page,
                page_size,
            )
        )

    def count_in_app_notifications(self, user_id: int | str | uuid.UUID) -> int:
        return self._get_all_in_app_notifications_queryset(user_id).count()

    def count_in_app_unread_notifications(self, user_id: int | str | uuid.UUID) -> int:
        return self._get_all_in_app_unread_notifications_queryset(user_id).count()

    def mark_sent_as_read_bulk(
        self,
        notification_ids: Iterable[int | str | uuid.UUID],
        user_id: int | str | uuid.UUID | None = None,
        *,
        notification_versions: Mapping[int | str | uuid.UUID, str] | None = None,
    ) -> Iterable[Notification]:
        """Mark every SENT notification in ``notification_ids`` as READ.

        Idempotent: ids that are already READ, missing, not owned (when
        ``user_id`` is given), or in a non-SENT state are simply skipped and
        never raise. When ``user_id`` is provided the update is scoped to that
        user so rows owned by others are never touched (recommended for
        endpoints). ``read_at`` is set on every row moved to READ. Returns the
        serialized notifications for the requested ids that are READ after the
        operation (newly-marked + already-read).

        ``notification_versions`` maps ids (compared as strings) to the token each returned row
        holds afterwards: the primary's, on a replica (``writes.notificationVersion``). A row
        already READ that is in the mapping takes its token too; every other row this marks
        gets a new one. All in the same UPDATE.
        """
        ids = [str(i) for i in notification_ids]
        base = self._notifications().filter(id__in=ids)
        if user_id is not None:
            base = base.filter(user_id=str(user_id))
        requested = set(ids)
        held = {
            str(key): _token_for_write(token)
            for key, token in (notification_versions or {}).items()
            if str(key) in requested
        }
        sent = Q(status=NotificationStatus.SENT.value)

        with self._atomic():
            if not held:
                # Every row this marks gets the same new token. Tokens are compared per row, with
                # the id in the same WHERE clause, so sharing a value across rows is harmless.
                base.filter(sent).update(
                    status=NotificationStatus.READ.value,
                    read_at=timezone.now(),
                    notification_version=new_notification_version(),
                    modified=timezone.now(),
                )
            else:
                base.filter(
                    sent | Q(status=NotificationStatus.READ.value, id__in=list(held))
                ).update(
                    status=NotificationStatus.READ.value,
                    read_at=Case(
                        When(sent, then=Value(timezone.now())),
                        default=F("read_at"),
                        output_field=models.DateTimeField(),
                    ),
                    modified=timezone.now(),
                    notification_version=Case(
                        *(When(id=key, then=Value(token)) for key, token in held.items()),
                        default=Value(new_notification_version()),
                        output_field=models.UUIDField(),
                    ),
                )

        read_qs = base.filter(status=NotificationStatus.READ.value).order_by("-created", "-id")
        return self._serialize_user_notification_queryset(read_qs)

    def get_all_future_notifications(self) -> Iterable["Notification | OneOffNotification"]:
        return self._serialize_notification_queryset(self._get_all_future_notifications_queryset())

    def get_future_notifications(
        self, page: int, page_size: int
    ) -> Iterable["Notification | OneOffNotification"]:
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
            self._notifications().select_related("user").get(id=str(notification_id)).user
        )
        if not notification_user or not notification_user.is_active:
            raise NotificationUserNotFoundError("User not found")
        return notification_user.email

    def store_context_used(
        self,
        notification_id: int | str | uuid.UUID,
        context: dict,
        adapter_import_str: str,
        *,
        notification_version: str | None = None,
    ) -> None:
        self._notifications().filter(id=str(notification_id)).update(
            context_used=context,
            adapter_used=adapter_import_str,
            notification_version=_token_for_write(notification_version),
            modified=timezone.now(),
        )

    def store_git_commit_sha(
        self,
        notification_id: int | str | uuid.UUID,
        git_commit_sha: str,
        *,
        notification_version: str | None = None,
    ) -> None:
        self._notifications().filter(id=str(notification_id)).update(
            git_commit_sha=git_commit_sha,
            notification_version=_token_for_write(notification_version),
            modified=timezone.now(),
        )

    def store_template_version(
        self,
        notification_id: int | str | uuid.UUID,
        template_version: int,
        *,
        notification_version: str | None = None,
    ) -> None:
        # Overridden rather than inherited: the seam's default is a no-op so a backend with
        # nowhere to put this keeps working, and there is a column for it here.
        self._notifications().filter(id=str(notification_id)).update(
            used_template_version=template_version,
            notification_version=_token_for_write(notification_version),
            modified=timezone.now(),
        )

    # ------------------------------------------------------------------ replication

    def apply_replication_snapshot_if_newer(
        self, snapshot: Notification | OneOffNotification
    ) -> ApplyResult:
        """Id-keyed, newer-wins upsert of the primary's snapshot, in one transaction.

        * The row keeps the snapshot's id, ``created`` and ``modified`` (not this database's
          clock: the next newer-wins comparison reads them) and every other persisted field.
        * An existing row is overwritten by one conditional UPDATE whose WHERE clause carries
          ``modified <= snapshot.modified`` (a NULL on either side applies), so a newer row
          written concurrently is never overwritten; when it matches nothing the row is newer
          and the snapshot is declined.
        * The row's token becomes ``snapshot.notification_version``, so the versions this
          backend lists are ones the primary accepts. A fresh one is minted only when the
          snapshot has none, or one that is not a UUID.
        * The join rows are made to match ``snapshot.attachments``. Each file is matched to
          this database's file record by ``(checksum, size)``, or a record is created from the
          snapshot's metadata and ``storage_identifiers``. No byte is read or copied: this
          assumes this backend's attachment manager reads the same storage as the primary's.
          Linking here writes join rows only, so it does not move the token written above.

        * An inserted row keeps the primary's id, which does not advance an id sequence (on
          PostgreSQL and Oracle), so the sequence is reset past it in the same transaction:
          ``reset_notification_id_sequence``.

        Declined, with nothing written: an id this database's primary key cannot hold (e.g. a
        UUID into an integer key, or an integer outside this database's range), a user that does
        not exist in this database, an attachment with no record here and no
        ``storage_identifiers`` to create one from, and a row newer than the snapshot. The last
        one is ``superseded``: the row already reflects a later write, so the service does not
        replay the older snapshot over it. Reasons name ids only, never content or recipients.
        """
        pk_field = NotificationModel._meta.pk
        assert pk_field is not None  # noqa: S101
        pk: int | str | None
        try:
            pk = pk_field.to_python(snapshot.id)
        except ValidationError:
            pk = None
        # The column's range too, on this backend's database (the field's own validators read
        # the default connection's): an integer the database cannot store is not an id here.
        if isinstance(pk, int) and isinstance(pk_field, models.IntegerField):
            low, high = connections[self.database].ops.integer_field_range(
                pk_field.get_internal_type()
            )
            if (low is not None and pk < low) or (high is not None and pk > high):
                pk = None
        # The id must survive the round trip: "0012", True or a UUID's integer value would land
        # on a row the primary does not know by that id.
        if pk is None or isinstance(snapshot.id, bool) or str(pk) != str(snapshot.id):
            return ApplyResult(
                applied=False,
                reason=f"notification id {snapshot.id} does not fit this backend's id column",
            )

        columns = self._snapshot_columns(snapshot)
        if isinstance(snapshot, Notification) and not self._user_exists(snapshot.user_id):
            return ApplyResult(
                applied=False,
                reason=f"the user of notification {pk} does not exist in this database",
            )

        with self._atomic():
            file_records = self._snapshot_file_records(snapshot.attachments)
            if file_records is None:
                return ApplyResult(
                    applied=False,
                    reason=(
                        f"an attachment of notification {pk} has no file record here and no "
                        "storage identifiers"
                    ),
                )
            token = _token_for_write(snapshot.notification_version)
            conditions = Q(id=pk)
            if snapshot.modified is not None:
                conditions &= Q(modified__isnull=True) | Q(modified__lte=snapshot.modified)
            updated = (
                self._notifications()
                .filter(conditions)
                .update(**columns, notification_version=token)
            )
            if updated == 0:
                if self._notifications().filter(id=pk).exists():
                    return ApplyResult(
                        applied=False,
                        superseded=True,
                        reason=f"notification {pk} on this backend is newer than the snapshot",
                    )
                self._insert_notification(str(token), id=pk, **columns)
                # The row keeps the primary's id, which does not advance an id sequence.
                reset_notification_id_sequence(self.database)
            self._reconcile_snapshot_attachments(pk, snapshot.attachments, file_records)
        return ApplyResult(applied=True)

    @staticmethod
    def _snapshot_columns(snapshot: Notification | OneOffNotification) -> dict[str, object]:
        """Every persisted column of a snapshot but the id and the token."""
        columns: dict[str, object] = {
            "notification_type": snapshot.notification_type,
            "title": snapshot.title,
            "body_template": snapshot.body_template,
            "context_name": snapshot.context_name,
            "context_kwargs": snapshot.context_kwargs,
            "send_after": snapshot.send_after,
            "subject_template": snapshot.subject_template or "",
            "preheader_template": snapshot.preheader_template or "",
            "status": snapshot.status,
            "context_used": snapshot.context_used,
            "adapter_used": snapshot.adapter_used or "",
            "adapter_extra_parameters": snapshot.adapter_extra_parameters,
            "sent_at": snapshot.sent_at,
            "read_at": snapshot.read_at,
            "tenant": snapshot.tenant,
            "git_commit_sha": snapshot.git_commit_sha,
            "requested_template_version": snapshot.requested_template_version,
            "used_template_version": snapshot.used_template_version,
        }
        if isinstance(snapshot, OneOffNotification):
            columns.update(
                user_id=None,
                email_or_phone=snapshot.email_or_phone,
                first_name=snapshot.first_name,
                last_name=snapshot.last_name,
            )
        else:
            columns.update(user_id=snapshot.user_id, email_or_phone="", first_name="", last_name="")
        # Left to the field defaults only when the snapshot has none (both columns are NOT NULL).
        if snapshot.created is not None:
            columns["created"] = snapshot.created
        if snapshot.modified is not None:
            columns["modified"] = snapshot.modified
        return columns

    def _user_exists(self, user_id: int | str | uuid.UUID) -> bool:
        user_model = get_user_model()
        # The base manager: a custom default manager may hide users (inactive ones, say) the
        # foreign key still accepts.
        try:
            return user_model._base_manager.using(self.database).filter(pk=str(user_id)).exists()
        except (ValidationError, ValueError, TypeError):
            return False

    def _snapshot_file_records(
        self, attachments: list[StoredAttachment]
    ) -> list[AttachmentFileRecordModel | StoredAttachment] | None:
        """Per snapshot attachment, this database's file record for it, or the attachment itself
        when a record has to be created from it. ``None`` when one can be neither found nor
        created. Reads only."""
        resolved: list[AttachmentFileRecordModel | StoredAttachment] = []
        for attachment in attachments:
            identifiers = dict(attachment.storage_identifiers or {})
            if attachment.checksum:
                record = (
                    self._file_records()
                    .filter(checksum=attachment.checksum, size=attachment.size)
                    .first()
                )
            else:
                # No checksum to match on (a record predating checksums): only the very same
                # stored file is the same file. Matched key by key in SQL, which ignores key
                # order on every database, then exactly here, which rules out extra keys.
                candidates = self._file_records().filter(checksum="", size=attachment.size)
                if identifiers:
                    for index, (key, value) in enumerate(identifiers.items()):
                        alias = f"_storage_identifier_{index}"
                        candidates = candidates.alias(
                            **{alias: KeyTransform(key, "storage_identifiers")}
                        ).filter(**{alias: value})
                else:
                    candidates = candidates.filter(storage_identifiers={})
                record = next(
                    (
                        candidate
                        for candidate in candidates
                        if (candidate.storage_identifiers or {}) == identifiers
                    ),
                    None,
                )
            if record is None and not identifiers:
                return None
            resolved.append(record if record is not None else attachment)
        return resolved

    def _reconcile_snapshot_attachments(
        self,
        notification_pk: int | str,
        attachments: list[StoredAttachment],
        file_records: list[AttachmentFileRecordModel | StoredAttachment],
    ) -> None:
        """Make the notification's join rows match ``attachments``, keeping the ones that do."""
        wanted: list[tuple[int, bool, str | None]] = []
        for attachment, resolved in zip(attachments, file_records, strict=True):
            if isinstance(resolved, AttachmentFileRecordModel):
                record = resolved
            else:
                record = self._file_records().create(
                    filename=resolved.filename,
                    content_type=resolved.content_type or "",
                    size=resolved.size,
                    checksum=resolved.checksum,
                    storage_identifiers=dict(resolved.storage_identifiers),
                    created=resolved.created_at,
                )
            wanted.append((record.pk, attachment.is_inline, attachment.description))

        stale: list[int] = []
        for row in self._join_rows().filter(notification_id=notification_pk).order_by("id"):
            key = (row.file_id, row.is_inline, row.description)
            if key in wanted:
                wanted.remove(key)
            else:
                stale.append(row.pk)
        if stale:
            self._join_rows().filter(pk__in=stale).delete()
        for file_pk, is_inline, description in wanted:
            self._join_rows().create(
                notification_id=notification_pk,
                file_id=file_pk,
                is_inline=is_inline,
                description=description,
            )

    # ------------------------------------------------------------------ filtering

    def _field_leaf(self, field: str, value: object) -> tuple[Q, str | None]:
        """Positive Q for one field filter, plus the model field to OR ``__isnull`` on when
        this leaf is negated.

        Delegates to :mod:`vintasend_django.services.notification_backends.filters`; a subclass
        that adds filter fields overrides this and falls back to ``super()`` for the rest.
        """
        return field_leaf(field, value)

    def _translate_filter(self, filter: NotificationFilter, negated: bool = False) -> Q:  # noqa: A002
        """Translate a composable filter to a Django ``Q``, pushing negation to the leaves.

        Working in negation-normal form keeps NULL semantics correct: a positive leaf excludes
        NULL rows (a positive filter on a NULL value never matches), while a negated leaf ORs
        in ``field__isnull=True`` so NULL rows ARE included under ``not`` -- exactly what the
        reference in-memory evaluator does.
        """
        if "and" in filter:
            subs = [self._translate_filter(sub, negated) for sub in filter["and"]]  # type: ignore[typeddict-item]
            combiner = or_all if negated else and_all  # De Morgan under negation
            return combiner(subs)
        if "or" in filter:
            subs = [self._translate_filter(sub, negated) for sub in filter["or"]]  # type: ignore[typeddict-item]
            combiner = and_all if negated else or_all
            return combiner(subs)
        if "not" in filter:
            return self._translate_filter(filter["not"], not negated)  # type: ignore[typeddict-item]

        # Field filter. Empty ``{}`` matches everything (or nothing when negated). Multiple keys
        # are an implicit AND (OR under negation, by De Morgan).
        if not is_field_filter(filter):
            return MATCH_NOTHING if not negated else Q()
        items = list(filter.items())
        if not items:
            return MATCH_NOTHING if negated else Q()
        leaf_qs: list[Q] = []
        for key, value in items:
            positive_q, null_field = self._field_leaf(key, value)
            if not negated:
                leaf_qs.append(positive_q)
            else:
                negated_q = ~positive_q
                if null_field is not None:
                    negated_q |= Q(**{f"{null_field}__isnull": True})
                leaf_qs.append(negated_q)
        return or_all(leaf_qs) if negated else and_all(leaf_qs)

    def _filtered_queryset(
        self,
        filter: NotificationFilter,  # noqa: A002
        order_by: NotificationOrderBy | None = None,
    ) -> QuerySet["NotificationModel"]:
        queryset = self._notifications().filter(self._translate_filter(filter))
        if order_by is None:
            order_fields = ["-created", "-id"]
        else:
            attr = ORDER_FIELD_TO_ATTR[order_by["field"]]
            prefix = "-" if order_by["direction"] == "desc" else ""
            # id tiebreaker in the SAME direction, so offset pagination over a non-unique key
            # does not drop or duplicate rows across pages.
            order_fields = [f"{prefix}{attr}", f"{prefix}id"]
        return queryset.order_by(*order_fields)

    def filter_notifications(
        self,
        filter: NotificationFilter,  # noqa: A002
        page: int,
        page_size: int,
        order_by: NotificationOrderBy | None = None,
    ) -> Iterable[Notification | OneOffNotification]:
        return self._serialize_notification_queryset(
            self._paginate_queryset(self._filtered_queryset(filter, order_by), page, page_size)
        )

    def count_notifications(self, filter: NotificationFilter) -> int:  # noqa: A002
        return self._notifications().filter(self._translate_filter(filter)).count()

    def filter_notification_versions(
        self,
        filter: NotificationFilter,  # noqa: A002
        page: int,
        page_size: int,
    ) -> list[NotificationVersion]:
        """The ID and version token of every notification matching ``filter``, ordered by ID.

        Selects the ``id`` and ``notification_version`` columns and nothing else, with no
        ``select_related`` or ``prefetch_related``, so no recipient, context, context parameters,
        rendered content or attachment is read. Pages are 1-indexed, like the rest of this
        backend. ``version`` is the token as a string; pass it as ``expected_version`` to
        ``requeue_notification`` or ``repin_notification`` to act on the notification only
        while it is unchanged.
        """
        offset = (page - 1) * page_size
        rows = (
            self._notifications()
            .filter(self._translate_filter(filter))
            .order_by("id")
            .values_list("id", "notification_version")[offset : offset + page_size]
        )
        # A row inserted by code predating the column, between migrations 0009 and 0010, has no
        # token yet. It is still listed (hiding it would hide a notification), with the empty
        # string as its version rather than ``str(None)``. That is not a token this backend
        # hands out, and a NULL never equals any token, so a conditional requeue or repin against
        # such a row, with any ``expected_version``, is refused as changed. Listing does not
        # write a token: a read that writes would move the version it is reporting.
        return [
            NotificationVersion(id=pk, version="" if token is None else str(token))
            for pk, token in rows
        ]

    def get_filter_capabilities(self) -> dict[str, bool]:
        # This backend translates the full vocabulary into SQL. Every key but the presence
        # filters is supported by default, so those are the only ones it has to report.
        return dict(PRESENCE_FILTER_CAPABILITIES)

    def get_action_capabilities(self) -> dict[str, bool]:
        # Requeue and repin are single conditional UPDATEs that check expected_version against
        # notification_version, requeue pins in the same UPDATE, and filter_notification_versions
        # selects the id and the token only.
        return dict(ACTION_CAPABILITIES)
