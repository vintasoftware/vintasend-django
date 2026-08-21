import random
import shutil
import tempfile
from datetime import timedelta

from django.test import override_settings
from django.utils import timezone

import pytest
from vintasend.constants import NotificationStatus, NotificationTypes
from vintasend.exceptions import (
    AttachmentFileNotFoundError,
    NotificationCancelError,
    NotificationNotFoundError,
    NotificationUpdateError,
)
from vintasend.services.dataclasses import (
    Notification,
    NotificationAttachment,
    NotificationAttachmentReference,
    OneOffNotification,
)

from vintasend_django.models import AttachmentFileRecord as AttachmentFileRecordModel
from vintasend_django.models import Notification as NotificationModel
from vintasend_django.models import NotificationAttachment as NotificationAttachmentModel
from vintasend_django.services.notification_backends.django_db_notification_backend import (
    DjangoDbNotificationBackend,
)
from vintasend_django.test_helpers import VintaSendDjangoTestCase


class DjangoDBNotificationBackendTestCase(VintaSendDjangoTestCase):
    def setUp(self):
        super().setUp()
        # Point Django's default file storage at a throwaway directory so the
        # DjangoAttachmentManager writes attachment bytes somewhere isolated per test.
        self._media_root = tempfile.mkdtemp()
        self._media_override = override_settings(MEDIA_ROOT=self._media_root)
        self._media_override.enable()

    def tearDown(self):
        self._media_override.disable()
        shutil.rmtree(self._media_root, ignore_errors=True)
        super().tearDown()

    @staticmethod
    def _attachment(
        content: bytes = b"attachment content",
        filename: str = "document.txt",
        content_type: str | None = "text/plain",
        **kwargs,
    ) -> NotificationAttachment:
        return NotificationAttachment(
            file=content, filename=filename, content_type=content_type, **kwargs
        )

    def test_persist_notification(self):
        notification = DjangoDbNotificationBackend().persist_notification(
            user_id=self.user.pk,
            notification_type=NotificationTypes.EMAIL.value,
            title="test",
            body_template="test",
            context_name="test",
            context_kwargs={},
            send_after=None,
            subject_template="test",
            preheader_template="test",
        )

        assert isinstance(notification, Notification)
        assert str(notification.user_id) == str(self.user.pk)
        assert notification.notification_type == NotificationTypes.EMAIL.value
        assert notification.title == "test"
        assert notification.body_template == "test"
        assert notification.context_name == "test"
        assert notification.context_kwargs == {}
        assert notification.send_after is None
        assert notification.subject_template == "test"
        assert notification.preheader_template == "test"
        assert notification.status == NotificationStatus.PENDING_SEND.value
        assert notification.id is not None
        notification_db_record = NotificationModel.objects.get(id=notification.id)
        assert str(notification_db_record.user_id) == str(self.user.pk)
        assert notification_db_record.notification_type == NotificationTypes.EMAIL.value
        assert notification_db_record.title == "test"
        assert notification_db_record.body_template == "test"
        assert notification_db_record.context_name == "test"
        assert notification_db_record.context_kwargs == {}
        assert notification_db_record.send_after is None
        assert notification_db_record.subject_template == "test"
        assert notification_db_record.preheader_template == "test"
        assert notification_db_record.status == NotificationStatus.PENDING_SEND.value

    def test_update_notification(self):
        notification = DjangoDbNotificationBackend().persist_notification(
            user_id=self.user.pk,
            notification_type=NotificationTypes.EMAIL.value,
            title="test",
            body_template="test",
            context_name="test",
            context_kwargs={},
            send_after=None,
            subject_template="test",
            preheader_template="test",
        )

        updated_notification = DjangoDbNotificationBackend().persist_notification_update(
            notification_id=notification.id,
            updated_data={"subject_template": "updated test subject"},
        )

        assert updated_notification.subject_template == "updated test subject"
        notification_db_record = NotificationModel.objects.get(id=notification.id)
        assert notification_db_record.subject_template == "updated test subject"

    def get_all_pending_notifications(self):
        DjangoDbNotificationBackend().persist_notification(
            user_id=self.user.pk,
            notification_type=NotificationTypes.EMAIL.value,
            title="test",
            body_template="test",
            context_name="test",
            context_kwargs={},
            send_after=None,
            subject_template="test",
            preheader_template="test",
        )

        DjangoDbNotificationBackend().persist_notification(
            user_id=self.user.pk,
            notification_type=NotificationTypes.EMAIL.value,
            title="test 2",
            body_template="test 2",
            context_name="test 2",
            context_kwargs={},
            send_after=None,
            subject_template="test 2",
            preheader_template="test 2",
        )

        already_sent = DjangoDbNotificationBackend().persist_notification(
            user_id=self.user.pk,
            notification_type=NotificationTypes.EMAIL.value,
            title="test already sent",
            body_template="test already sent",
            context_name="test already sent",
            context_kwargs={},
            send_after=None,
            subject_template="test already sent",
            preheader_template="test already sent",
        )
        DjangoDbNotificationBackend().mark_pending_as_sent(notification_id=already_sent.id)

        notifications = DjangoDbNotificationBackend().get_all_pending_notifications()
        assert len(notifications) == 2
        notification_1 = notifications[1]
        assert isinstance(notification_1, Notification)
        assert notification_1.user_id == self.user.pk
        assert notification_1.notification_type == NotificationTypes.EMAIL.value
        assert notification_1.title == "test"
        assert notification_1.body_template == "test"
        assert notification_1.context_name == "test"
        assert notification_1.context_kwargs == {}
        assert notification_1.send_after is None
        assert notification_1.subject_template == "test"
        assert notification_1.preheader_template == "test"
        assert notification_1.status == NotificationStatus.PENDING_SEND.value
        notification_2 = notifications[0]
        assert isinstance(notification_2, Notification)
        assert notification_2.user_id == self.user.pk
        assert notification_2.notification_type == NotificationTypes.EMAIL.value
        assert notification_2.title == "test 2"
        assert notification_2.body_template == "test 2"
        assert notification_2.context_name == "test 2"
        assert notification_2.context_kwargs == {}
        assert notification_2.send_after is None
        assert notification_2.subject_template == "test 2"
        assert notification_2.preheader_template == "test 2"
        assert notification_2.status == NotificationStatus.PENDING_SEND.value

    def test_get_pending_notifications(self):
        DjangoDbNotificationBackend().persist_notification(
            user_id=self.user.pk,
            notification_type=NotificationTypes.EMAIL.value,
            title="test",
            body_template="test",
            context_name="test",
            context_kwargs={},
            send_after=None,
            subject_template="test",
            preheader_template="test",
        )

        DjangoDbNotificationBackend().persist_notification(
            user_id=self.user.pk,
            notification_type=NotificationTypes.EMAIL.value,
            title="test 2",
            body_template="test 2",
            context_name="test 2",
            context_kwargs={},
            send_after=None,
            subject_template="test 2",
            preheader_template="test 2",
        )

        already_sent = DjangoDbNotificationBackend().persist_notification(
            user_id=self.user.pk,
            notification_type=NotificationTypes.EMAIL.value,
            title="test already sent",
            body_template="test already sent",
            context_name="test already sent",
            context_kwargs={},
            send_after=None,
            subject_template="test already sent",
            preheader_template="test already sent",
        )
        DjangoDbNotificationBackend().persist_notification_update(
            notification_id=already_sent.id,
            updated_data={"status": NotificationStatus.SENT.value},
        )

        notifications = list(
            DjangoDbNotificationBackend().get_pending_notifications(page=1, page_size=1)
        )
        assert len(notifications) == 1
        notification_1 = notifications[0]
        assert isinstance(notification_1, Notification)
        assert notification_1.user_id == self.user.pk
        assert notification_1.notification_type == NotificationTypes.EMAIL.value
        assert notification_1.title == "test"
        assert notification_1.body_template == "test"
        assert notification_1.context_name == "test"
        assert notification_1.context_kwargs == {}
        assert notification_1.send_after is None
        assert notification_1.subject_template == "test"
        assert notification_1.preheader_template == "test"
        assert notification_1.status == NotificationStatus.PENDING_SEND.value

        notifications = list(
            DjangoDbNotificationBackend().get_pending_notifications(page=2, page_size=1)
        )
        assert len(notifications) == 1
        notification_2 = notifications[0]
        assert isinstance(notification_2, Notification)
        assert notification_2.user_id == self.user.pk
        assert notification_2.notification_type == NotificationTypes.EMAIL.value
        assert notification_2.title == "test 2"
        assert notification_2.body_template == "test 2"
        assert notification_2.context_name == "test 2"
        assert notification_2.context_kwargs == {}
        assert notification_2.send_after is None
        assert notification_2.subject_template == "test 2"
        assert notification_2.preheader_template == "test 2"
        assert notification_2.status == NotificationStatus.PENDING_SEND.value

    def test_mark_pending_as_sent(self):
        notification = DjangoDbNotificationBackend().persist_notification(
            user_id=self.user.pk,
            notification_type=NotificationTypes.EMAIL.value,
            title="test",
            body_template="test",
            context_name="test",
            context_kwargs={},
            send_after=None,
            subject_template="test",
            preheader_template="test",
        )

        notification = DjangoDbNotificationBackend().mark_pending_as_sent(notification.id)
        assert notification.status == NotificationStatus.SENT.value
        notification_db_record = NotificationModel.objects.get(id=notification.id)
        assert notification_db_record.status == NotificationStatus.SENT.value

    def test_mark_pending_as_failed(self):
        notification = DjangoDbNotificationBackend().persist_notification(
            user_id=self.user.pk,
            notification_type=NotificationTypes.EMAIL.value,
            title="test",
            body_template="test",
            context_name="test",
            context_kwargs={},
            send_after=None,
            subject_template="test",
            preheader_template="test",
        )

        notification = DjangoDbNotificationBackend().mark_pending_as_failed(notification.id)
        assert notification.status == NotificationStatus.FAILED.value
        notification_db_record = NotificationModel.objects.get(id=notification.id)
        assert notification_db_record.status == NotificationStatus.FAILED.value

    def test_mark_pending_as_failed_already_sent(self):
        notification = DjangoDbNotificationBackend().persist_notification(
            user_id=self.user.pk,
            notification_type=NotificationTypes.EMAIL.value,
            title="test",
            body_template="test",
            context_name="test",
            context_kwargs={},
            send_after=None,
            subject_template="test",
            preheader_template="test",
        )
        DjangoDbNotificationBackend().mark_pending_as_sent(notification.id)

        with pytest.raises(NotificationUpdateError):
            DjangoDbNotificationBackend().mark_pending_as_failed(notification.id)
        notification_db_record = NotificationModel.objects.get(id=notification.id)
        assert notification_db_record.status == NotificationStatus.SENT.value

    def test_mark_sent_as_read(self):
        notification = DjangoDbNotificationBackend().persist_notification(
            user_id=self.user.pk,
            notification_type=NotificationTypes.EMAIL.value,
            title="test",
            body_template="test",
            context_name="test",
            context_kwargs={},
            send_after=None,
            subject_template="test",
            preheader_template="test",
        )
        DjangoDbNotificationBackend().mark_pending_as_sent(notification.id)

        notification = DjangoDbNotificationBackend().mark_sent_as_read(notification.id)
        assert notification.status == NotificationStatus.READ.value
        notification_db_record = NotificationModel.objects.get(id=notification.id)
        assert notification_db_record.status == NotificationStatus.READ.value

    def _make_notification(self, user, status, notification_type=NotificationTypes.IN_APP):
        return NotificationModel.objects.create(
            user=user,
            notification_type=notification_type.value,
            title="test",
            body_template="test",
            context_name="test",
            context_kwargs={},
            send_after=None,
            status=status.value,
        )

    def test_filter_in_app_unread_notifications_returns_sent_in_app(self):
        # Regression test for the NotificationTypes.IN_APP (missing .value) bug:
        # a SENT IN_APP notification must be returned by the unread filter.
        notification = self._make_notification(self.user, NotificationStatus.SENT)

        results = list(
            DjangoDbNotificationBackend().filter_in_app_unread_notifications(self.user.pk)
        )

        assert [n.id for n in results] == [notification.pk]

    def test_filter_in_app_notifications_returns_sent_and_read_excludes_others(self):
        sent = self._make_notification(self.user, NotificationStatus.SENT)
        read = self._make_notification(self.user, NotificationStatus.READ)
        # Excluded internal pipeline states
        self._make_notification(self.user, NotificationStatus.PENDING_SEND)
        self._make_notification(self.user, NotificationStatus.FAILED)
        self._make_notification(self.user, NotificationStatus.CANCELLED)
        # Excluded: non-IN_APP
        self._make_notification(
            self.user, NotificationStatus.SENT, notification_type=NotificationTypes.EMAIL
        )

        results = list(DjangoDbNotificationBackend().filter_in_app_notifications(self.user.pk))

        # newest-first ("-created", "-id"); read was created after sent
        assert [n.id for n in results] == [read.pk, sent.pk]

    def test_filter_in_app_notifications_scoped_per_user(self):
        mine = self._make_notification(self.user, NotificationStatus.SENT)
        other = self.create_user(email="other@example.com")
        self._make_notification(other, NotificationStatus.SENT)

        results = list(DjangoDbNotificationBackend().filter_in_app_notifications(self.user.pk))

        assert [n.id for n in results] == [mine.pk]

    def test_filter_in_app_notifications_pagination(self):
        created = [self._make_notification(self.user, NotificationStatus.SENT) for _ in range(5)]
        # newest-first
        expected_order = list(reversed(created))

        backend = DjangoDbNotificationBackend()
        page1 = list(backend.filter_in_app_notifications(self.user.pk, page=1, page_size=2))
        page2 = list(backend.filter_in_app_notifications(self.user.pk, page=2, page_size=2))
        page3 = list(backend.filter_in_app_notifications(self.user.pk, page=3, page_size=2))

        assert [n.id for n in page1] == [n.pk for n in expected_order[0:2]]
        assert [n.id for n in page2] == [n.pk for n in expected_order[2:4]]
        assert [n.id for n in page3] == [n.pk for n in expected_order[4:5]]

    def test_count_in_app_notifications(self):
        self._make_notification(self.user, NotificationStatus.SENT)
        self._make_notification(self.user, NotificationStatus.READ)
        self._make_notification(self.user, NotificationStatus.PENDING_SEND)
        self._make_notification(
            self.user, NotificationStatus.SENT, notification_type=NotificationTypes.EMAIL
        )

        assert DjangoDbNotificationBackend().count_in_app_notifications(self.user.pk) == 2

    def test_count_in_app_unread_notifications(self):
        self._make_notification(self.user, NotificationStatus.SENT)
        self._make_notification(self.user, NotificationStatus.SENT)
        self._make_notification(self.user, NotificationStatus.READ)

        assert DjangoDbNotificationBackend().count_in_app_unread_notifications(self.user.pk) == 2

    def test_mark_sent_as_read_bulk_marks_and_returns_final_state(self):
        sent_a = self._make_notification(self.user, NotificationStatus.SENT)
        sent_b = self._make_notification(self.user, NotificationStatus.SENT)
        already_read = self._make_notification(self.user, NotificationStatus.READ)

        results = list(
            DjangoDbNotificationBackend().mark_sent_as_read_bulk(
                [sent_a.pk, sent_b.pk, already_read.pk], user_id=self.user.pk
            )
        )

        assert {n.id for n in results} == {sent_a.pk, sent_b.pk, already_read.pk}
        assert all(n.status == NotificationStatus.READ.value for n in results)
        for n in (sent_a, sent_b, already_read):
            n.refresh_from_db()
            assert n.status == NotificationStatus.READ.value

    def test_mark_sent_as_read_bulk_idempotent_on_already_read(self):
        already_read = self._make_notification(self.user, NotificationStatus.READ)

        # Must not raise (single mark_sent_as_read would).
        results = list(DjangoDbNotificationBackend().mark_sent_as_read_bulk([already_read.pk]))

        assert [n.id for n in results] == [already_read.pk]

    def test_mark_sent_as_read_bulk_skips_missing_and_non_sent(self):
        sent = self._make_notification(self.user, NotificationStatus.SENT)
        pending = self._make_notification(self.user, NotificationStatus.PENDING_SEND)

        results = list(
            DjangoDbNotificationBackend().mark_sent_as_read_bulk(
                [sent.pk, pending.pk, 99999], user_id=self.user.pk
            )
        )

        assert [n.id for n in results] == [sent.pk]
        pending.refresh_from_db()
        assert pending.status == NotificationStatus.PENDING_SEND.value

    def test_mark_sent_as_read_bulk_respects_user_scoping(self):
        other = self.create_user(email="other@example.com")
        mine = self._make_notification(self.user, NotificationStatus.SENT)
        theirs = self._make_notification(other, NotificationStatus.SENT)

        results = list(
            DjangoDbNotificationBackend().mark_sent_as_read_bulk(
                [mine.pk, theirs.pk], user_id=self.user.pk
            )
        )

        assert [n.id for n in results] == [mine.pk]
        theirs.refresh_from_db()
        assert theirs.status == NotificationStatus.SENT.value

    def test_serialize_user_notification_carries_timestamps_and_context(self):
        record = self._make_notification(self.user, NotificationStatus.SENT)
        record.context_used = {"foo": "bar"}
        record.save()

        serialized = DjangoDbNotificationBackend().serialize_user_notification(record)

        assert serialized.context_used == {"foo": "bar"}
        assert serialized.created == record.created
        assert serialized.modified == record.modified

    def test_cancel_notification(self):
        notification = DjangoDbNotificationBackend().persist_notification(
            user_id=self.user.pk,
            notification_type=NotificationTypes.EMAIL.value,
            title="test",
            body_template="test",
            context_name="test",
            context_kwargs={},
            send_after=timezone.now() + timedelta(days=1),
            subject_template="test",
            preheader_template="test",
        )

        DjangoDbNotificationBackend().cancel_notification(notification.id)
        notification_db_record = NotificationModel.objects.get(id=notification.id)
        assert notification_db_record.status == NotificationStatus.CANCELLED.value

    def test_cancel_notification_already_sent(self):
        notification = DjangoDbNotificationBackend().persist_notification(
            user_id=self.user.pk,
            notification_type=NotificationTypes.EMAIL.value,
            title="test",
            body_template="test",
            context_name="test",
            context_kwargs={},
            send_after=None,
            subject_template="test",
            preheader_template="test",
        )
        DjangoDbNotificationBackend().mark_pending_as_sent(notification.id)

        with pytest.raises(NotificationCancelError):
            DjangoDbNotificationBackend().cancel_notification(notification.id)
        notification_db_record = NotificationModel.objects.get(id=notification.id)
        assert notification_db_record.status != NotificationStatus.CANCELLED.value

    def test_get_notification(self):
        notification = DjangoDbNotificationBackend().persist_notification(
            user_id=self.user.pk,
            notification_type=NotificationTypes.EMAIL.value,
            title="test",
            body_template="test",
            context_name="test",
            context_kwargs={},
            send_after=None,
            subject_template="test",
            preheader_template="test",
        )

        notification_retrieved = DjangoDbNotificationBackend().get_notification(notification.id)
        assert notification_retrieved.id == notification.id
        assert notification_retrieved.user_id == self.user.pk
        assert notification_retrieved.notification_type == NotificationTypes.EMAIL.value
        assert notification_retrieved.title == "test"
        assert notification_retrieved.body_template == "test"
        assert notification_retrieved.context_name == "test"
        assert notification_retrieved.context_kwargs == {}
        assert notification_retrieved.send_after is None
        assert notification_retrieved.subject_template == "test"
        assert notification_retrieved.preheader_template == "test"
        assert notification_retrieved.status == NotificationStatus.PENDING_SEND.value

    def test_get_notification_not_found(self):
        with pytest.raises(NotificationNotFoundError):
            DjangoDbNotificationBackend().get_notification(random.randint(1, 100))

    def test_get_notification_cancelled(self):
        notification = DjangoDbNotificationBackend().persist_notification(
            user_id=self.user.pk,
            notification_type=NotificationTypes.EMAIL.value,
            title="test",
            body_template="test",
            context_name="test",
            context_kwargs={},
            send_after=None,
            subject_template="test",
            preheader_template="test",
        )
        DjangoDbNotificationBackend().cancel_notification(notification.id)
        with pytest.raises(NotificationNotFoundError):
            DjangoDbNotificationBackend().get_notification(notification.id)

    def test_persist_one_off_notification(self):
        """Test creating one-off notification"""
        backend = DjangoDbNotificationBackend()
        one_off_notification = backend.persist_one_off_notification(
            email_or_phone="test@example.com",
            first_name="John",
            last_name="Doe",
            notification_type=NotificationTypes.EMAIL.value,
            title="Welcome Email",
            body_template="welcome_email",
            context_name="welcome_context",
            context_kwargs={"user_name": "John"},
            send_after=None,
            subject_template="Welcome to our platform",
            preheader_template="Get started today",
        )

        assert isinstance(one_off_notification, OneOffNotification)
        assert one_off_notification.email_or_phone == "test@example.com"
        assert one_off_notification.first_name == "John"
        assert one_off_notification.last_name == "Doe"
        assert one_off_notification.notification_type == NotificationTypes.EMAIL.value
        assert one_off_notification.title == "Welcome Email"
        assert one_off_notification.status == NotificationStatus.PENDING_SEND.value

        # Verify it's stored in database
        notification_db_record = NotificationModel.objects.get(id=one_off_notification.id)
        assert notification_db_record.email_or_phone == "test@example.com"
        assert notification_db_record.first_name == "John"
        assert notification_db_record.last_name == "Doe"
        assert notification_db_record.user is None  # One-off notifications have no user

    def test_get_one_off_notification(self):
        """Test retrieving one-off notification"""
        backend = DjangoDbNotificationBackend()

        # Create one-off notification
        one_off_notification = backend.persist_one_off_notification(
            email_or_phone="test@example.com",
            first_name="Jane",
            last_name="Smith",
            notification_type=NotificationTypes.EMAIL.value,
            title="Test One-off",
            body_template="test_template",
            context_name="test_context",
            context_kwargs={},
        )

        # Retrieve via _get_one_off_notification method
        retrieved = backend._get_one_off_notification(one_off_notification.id)
        assert isinstance(retrieved, OneOffNotification)
        assert retrieved.id == one_off_notification.id
        assert retrieved.email_or_phone == "test@example.com"
        assert retrieved.first_name == "Jane"
        assert retrieved.last_name == "Smith"

    def test_get_notification_handles_both_types(self):
        """Test that get_notification works for both regular and one-off notifications"""
        backend = DjangoDbNotificationBackend()

        # Create regular notification
        regular_notification = backend.persist_notification(
            user_id=self.user.pk,
            notification_type=NotificationTypes.EMAIL.value,
            title="Regular notification",
            body_template="test",
            context_name="test",
            context_kwargs={},
            send_after=None,
        )

        # Create one-off notification
        one_off_notification = backend.persist_one_off_notification(
            email_or_phone="test@example.com",
            first_name="Test",
            last_name="User",
            notification_type=NotificationTypes.EMAIL.value,
            title="One-off notification",
            body_template="test",
            context_name="test",
            context_kwargs={},
        )

        # Retrieve both via get_notification
        retrieved_regular = backend.get_notification(regular_notification.id)
        retrieved_one_off = backend.get_notification(one_off_notification.id)

        assert isinstance(retrieved_regular, Notification)
        assert isinstance(retrieved_one_off, OneOffNotification)
        assert retrieved_regular.id == regular_notification.id
        assert retrieved_one_off.id == one_off_notification.id

    def test_get_all_pending_notifications_includes_one_off(self):
        """Test that get_all_pending_notifications returns both types"""
        backend = DjangoDbNotificationBackend()

        # Create regular notification
        regular_notification = backend.persist_notification(
            user_id=self.user.pk,
            notification_type=NotificationTypes.EMAIL.value,
            title="Regular notification",
            body_template="test",
            context_name="test",
            context_kwargs={},
            send_after=None,
        )

        # Create one-off notification
        one_off_notification = backend.persist_one_off_notification(
            email_or_phone="test@example.com",
            first_name="Test",
            last_name="User",
            notification_type=NotificationTypes.EMAIL.value,
            title="One-off notification",
            body_template="test",
            context_name="test",
            context_kwargs={},
        )

        # Get all pending notifications
        all_pending = list(backend.get_all_pending_notifications())

        # Should contain both notifications
        assert len(all_pending) >= 2

        # Check that we have both types
        regular_found = False
        one_off_found = False
        for notification in all_pending:
            if (
                isinstance(notification, Notification)
                and notification.id == regular_notification.id
            ):
                regular_found = True
            elif (
                isinstance(notification, OneOffNotification)
                and notification.id == one_off_notification.id
            ):
                one_off_found = True

        assert regular_found, "Regular notification not found in pending notifications"
        assert one_off_found, "One-off notification not found in pending notifications"

    # --------------------------------------------------------------- new 2.0 fields

    def test_mark_pending_as_sent_sets_sent_at(self):
        notification = DjangoDbNotificationBackend().persist_notification(
            user_id=self.user.pk,
            notification_type=NotificationTypes.EMAIL.value,
            title="Title",
            body_template="test",
            context_name="test",
            context_kwargs={},
            send_after=None,
        )
        sent = DjangoDbNotificationBackend().mark_pending_as_sent(notification.id)
        assert sent.status == NotificationStatus.SENT.value
        assert sent.sent_at is not None
        assert NotificationModel.objects.get(id=notification.id).sent_at is not None

    def test_mark_sent_as_read_sets_read_at(self):
        backend = DjangoDbNotificationBackend()
        notification = backend.persist_notification(
            user_id=self.user.pk,
            notification_type=NotificationTypes.IN_APP.value,
            title="Title",
            body_template="test",
            context_name="test",
            context_kwargs={},
            send_after=None,
        )
        backend.mark_pending_as_sent(notification.id)
        read = backend.mark_sent_as_read(notification.id)
        assert read.status == NotificationStatus.READ.value
        assert read.read_at is not None

    def test_mark_sent_as_read_bulk_sets_read_at(self):
        backend = DjangoDbNotificationBackend()
        notifications = []
        for _ in range(2):
            n = backend.persist_notification(
                user_id=self.user.pk,
                notification_type=NotificationTypes.IN_APP.value,
                title="Bulk",
                body_template="test",
                context_name="test",
                context_kwargs={},
                send_after=None,
            )
            backend.mark_pending_as_sent(n.id)
            notifications.append(n)

        result = list(
            backend.mark_sent_as_read_bulk([n.id for n in notifications], user_id=self.user.pk)
        )
        assert len(result) == 2
        assert all(n.read_at is not None for n in result)

    def test_persist_notification_with_tenant(self):
        notification = DjangoDbNotificationBackend().persist_notification(
            user_id=self.user.pk,
            notification_type=NotificationTypes.EMAIL.value,
            title="Tenant title",
            body_template="test",
            context_name="test",
            context_kwargs={},
            send_after=None,
            tenant="acme",
        )
        assert notification.tenant == "acme"
        assert NotificationModel.objects.get(id=notification.id).tenant == "acme"

    def test_store_git_commit_sha(self):
        backend = DjangoDbNotificationBackend()
        notification = backend.persist_notification(
            user_id=self.user.pk,
            notification_type=NotificationTypes.EMAIL.value,
            title="Sha title",
            body_template="test",
            context_name="test",
            context_kwargs={},
            send_after=None,
        )
        sha = "a" * 40
        backend.store_git_commit_sha(notification.id, sha)
        reloaded = backend.get_notification(notification.id)
        assert reloaded.git_commit_sha == sha

    # --------------------------------------------------------------- template versions

    def test_persist_notification_records_the_requested_template_version(self):
        backend = DjangoDbNotificationBackend()

        notification = backend.persist_notification(
            user_id=self.user.pk,
            notification_type=NotificationTypes.EMAIL.value,
            title="Pinned",
            body_template="welcome",
            context_name="test",
            context_kwargs={},
            send_after=None,
            requested_template_version=3,
        )

        assert notification.requested_template_version == 3
        assert backend.get_notification(notification.id).requested_template_version == 3

    def test_a_notification_with_no_pin_stores_null(self):
        backend = DjangoDbNotificationBackend()

        notification = backend.persist_notification(
            user_id=self.user.pk,
            notification_type=NotificationTypes.EMAIL.value,
            title="Unpinned",
            body_template="welcome",
            context_name="test",
            context_kwargs={},
            send_after=None,
        )

        assert notification.requested_template_version is None
        assert NotificationModel.objects.get(id=notification.id).requested_template_version is None

    def test_store_template_version(self):
        backend = DjangoDbNotificationBackend()
        notification = backend.persist_notification(
            user_id=self.user.pk,
            notification_type=NotificationTypes.EMAIL.value,
            title="Version title",
            body_template="welcome",
            context_name="test",
            context_kwargs={},
            send_after=None,
        )

        backend.store_template_version(notification.id, 5)

        assert backend.get_notification(notification.id).used_template_version == 5

    def test_a_one_off_notification_records_the_requested_template_version(self):
        backend = DjangoDbNotificationBackend()

        notification = backend.persist_one_off_notification(
            email_or_phone="someone@example.com",
            first_name="Some",
            last_name="One",
            notification_type=NotificationTypes.EMAIL.value,
            title="Pinned one-off",
            body_template="welcome",
            context_name="test",
            context_kwargs={},
            send_after=None,
            requested_template_version=2,
        )

        assert notification.requested_template_version == 2

    # --------------------------------------------------------------- attachments

    def test_persist_notification_with_attachment(self):
        backend = DjangoDbNotificationBackend()
        notification = backend.persist_notification(
            user_id=self.user.pk,
            notification_type=NotificationTypes.EMAIL.value,
            title="With attachment",
            body_template="test",
            context_name="test",
            context_kwargs={},
            send_after=None,
            attachments=[self._attachment(content=b"hello world", filename="hello.txt")],
        )

        assert len(notification.attachments) == 1
        stored = notification.attachments[0]
        assert stored.filename == "hello.txt"
        assert stored.content_type == "text/plain"
        assert stored.size == len(b"hello world")
        assert stored.file.read() == b"hello world"
        assert AttachmentFileRecordModel.objects.count() == 1
        assert (
            NotificationAttachmentModel.objects.filter(notification_id=notification.id).count() == 1
        )

    def test_persist_one_off_notification_with_attachment(self):
        backend = DjangoDbNotificationBackend()
        notification = backend.persist_one_off_notification(
            email_or_phone="user@example.com",
            first_name="A",
            last_name="B",
            notification_type=NotificationTypes.EMAIL.value,
            title="One-off with attachment",
            body_template="test",
            context_name="test",
            context_kwargs={},
            attachments=[self._attachment(content=b"one off bytes", filename="oo.txt")],
        )
        assert len(notification.attachments) == 1
        assert notification.attachments[0].file.read() == b"one off bytes"

    def test_identical_attachments_are_deduplicated_on_checksum(self):
        backend = DjangoDbNotificationBackend()
        backend.persist_notification(
            user_id=self.user.pk,
            notification_type=NotificationTypes.EMAIL.value,
            title="First",
            body_template="test",
            context_name="test",
            context_kwargs={},
            send_after=None,
            attachments=[self._attachment(content=b"same bytes", filename="a.txt")],
        )
        backend.persist_notification(
            user_id=self.user.pk,
            notification_type=NotificationTypes.EMAIL.value,
            title="Second",
            body_template="test",
            context_name="test",
            context_kwargs={},
            send_after=None,
            attachments=[self._attachment(content=b"same bytes", filename="a.txt")],
        )
        # One shared file record, two join rows.
        assert AttachmentFileRecordModel.objects.count() == 1
        assert NotificationAttachmentModel.objects.count() == 2

    def test_attach_by_reference_reuses_existing_file(self):
        backend = DjangoDbNotificationBackend()
        first = backend.persist_notification(
            user_id=self.user.pk,
            notification_type=NotificationTypes.EMAIL.value,
            title="Owner",
            body_template="test",
            context_name="test",
            context_kwargs={},
            send_after=None,
            attachments=[self._attachment(content=b"referenced", filename="ref.txt")],
        )
        file_id = first.attachments[0].file_id
        assert file_id

        second = backend.persist_notification(
            user_id=self.user.pk,
            notification_type=NotificationTypes.EMAIL.value,
            title="Referencer",
            body_template="test",
            context_name="test",
            context_kwargs={},
            send_after=None,
            attachments=[NotificationAttachmentReference(file_id=file_id)],
        )
        assert second.attachments[0].file_id == file_id
        assert AttachmentFileRecordModel.objects.count() == 1
        assert NotificationAttachmentModel.objects.count() == 2

    def test_attach_by_unknown_reference_raises(self):
        backend = DjangoDbNotificationBackend()
        with pytest.raises(AttachmentFileNotFoundError):
            backend.persist_notification(
                user_id=self.user.pk,
                notification_type=NotificationTypes.EMAIL.value,
                title="Bad ref",
                body_template="test",
                context_name="test",
                context_kwargs={},
                send_after=None,
                attachments=[NotificationAttachmentReference(file_id="999999")],
            )

    def test_get_attachments(self):
        backend = DjangoDbNotificationBackend()
        notification = backend.persist_notification(
            user_id=self.user.pk,
            notification_type=NotificationTypes.EMAIL.value,
            title="Get attachments",
            body_template="test",
            context_name="test",
            context_kwargs={},
            send_after=None,
            attachments=[self._attachment(content=b"payload", filename="p.txt")],
        )
        attachments = list(backend.get_attachments(notification.id))
        assert len(attachments) == 1
        assert attachments[0].file.read() == b"payload"

    def test_store_and_find_attachment_file_record(self):
        backend = DjangoDbNotificationBackend()
        manager = backend._attachment_manager
        record = manager.upload_file(b"blob bytes", "blob.txt", "text/plain")
        stored = backend.store_attachment_file_record(record)
        assert backend.get_attachment_file_record(stored.id) is not None
        found = backend.find_attachment_file_by_checksum(record.checksum, record.size)
        assert found is not None
        assert found.id == stored.id
        # Size guards against a checksum-only collision.
        assert backend.find_attachment_file_by_checksum(record.checksum, record.size + 1) is None

    def test_delete_notification_attachment_keeps_file_record(self):
        backend = DjangoDbNotificationBackend()
        notification = backend.persist_notification(
            user_id=self.user.pk,
            notification_type=NotificationTypes.EMAIL.value,
            title="Delete join",
            body_template="test",
            context_name="test",
            context_kwargs={},
            send_after=None,
            attachments=[self._attachment(content=b"keep me", filename="k.txt")],
        )
        join_id = notification.attachments[0].id
        backend.delete_notification_attachment(join_id)
        assert NotificationAttachmentModel.objects.filter(pk=join_id).count() == 0
        # The file record survives -- deleting one reference never drops the blob.
        assert AttachmentFileRecordModel.objects.count() == 1

    def test_get_orphaned_attachment_files(self):
        backend = DjangoDbNotificationBackend()
        notification = backend.persist_notification(
            user_id=self.user.pk,
            notification_type=NotificationTypes.EMAIL.value,
            title="Orphan",
            body_template="test",
            context_name="test",
            context_kwargs={},
            send_after=None,
            attachments=[self._attachment(content=b"orphan bytes", filename="o.txt")],
        )
        # Referenced: not orphaned yet.
        assert list(backend.get_orphaned_attachment_files()) == []
        backend.delete_notification_attachment(notification.attachments[0].id)
        orphans = list(backend.get_orphaned_attachment_files())
        assert len(orphans) == 1

    def test_delete_attachment_file(self):
        backend = DjangoDbNotificationBackend()
        record = backend.store_attachment_file_record(
            backend._attachment_manager.upload_file(b"x", "x.txt", "text/plain")
        )
        backend.delete_attachment_file(record.id)
        assert backend.get_attachment_file_record(record.id) is None

    # --------------------------------------------------------------- filter_notifications

    def _make(self, backend, **overrides):
        kwargs = dict(
            user_id=self.user.pk,
            notification_type=NotificationTypes.EMAIL.value,
            title="T",
            body_template="body_a",
            context_name="ctx",
            context_kwargs={},
            send_after=None,
        )
        kwargs.update(overrides)
        return backend.persist_notification(**kwargs)

    def test_filter_notifications_empty_filter_matches_all(self):
        backend = DjangoDbNotificationBackend()
        self._make(backend)
        self._make(backend, notification_type=NotificationTypes.IN_APP.value)
        results = list(backend.filter_notifications({}, page=1, page_size=10))
        assert len(results) == 2
        assert backend.count_notifications({}) == 2

    def test_filter_notifications_membership(self):
        backend = DjangoDbNotificationBackend()
        self._make(backend, notification_type=NotificationTypes.EMAIL.value)
        self._make(backend, notification_type=NotificationTypes.IN_APP.value)
        results = list(
            backend.filter_notifications(
                {"notification_type": NotificationTypes.IN_APP.value}, page=1, page_size=10
            )
        )
        assert len(results) == 1
        assert results[0].notification_type == NotificationTypes.IN_APP.value

    def test_filter_notifications_by_template_version(self):
        """Which notifications are pinned to a given version, and which rendered one."""
        backend = DjangoDbNotificationBackend()
        pinned = self._make(backend, requested_template_version=3)
        self._make(backend, requested_template_version=4)
        backend.store_template_version(pinned.id, 3)

        requested = list(
            backend.filter_notifications({"requested_template_version": 3}, page=1, page_size=10)
        )
        assert [n.id for n in requested] == [pinned.id]

        used = list(
            backend.filter_notifications({"used_template_version": 3}, page=1, page_size=10)
        )
        assert [n.id for n in used] == [pinned.id]

    def test_filter_notifications_by_a_list_of_template_versions(self):
        backend = DjangoDbNotificationBackend()
        self._make(backend, requested_template_version=1)
        self._make(backend, requested_template_version=2)
        self._make(backend, requested_template_version=9)

        results = list(
            backend.filter_notifications(
                {"requested_template_version": [1, 2]}, page=1, page_size=10
            )
        )
        assert len(results) == 2
        assert backend.count_notifications({"requested_template_version": [1, 2]}) == 2

    def test_an_unpinned_notification_does_not_match_a_version_filter(self):
        backend = DjangoDbNotificationBackend()
        self._make(backend)

        assert backend.count_notifications({"requested_template_version": 1}) == 0

    def test_negating_a_version_filter_includes_the_unpinned_rows(self):
        """The library's NULL semantics, in SQL: ``NOT IN`` would drop the NULLs."""
        backend = DjangoDbNotificationBackend()
        self._make(backend, requested_template_version=1)
        self._make(backend, requested_template_version=2)
        self._make(backend)  # never pinned

        results = list(
            backend.filter_notifications(
                {"not": {"requested_template_version": 1}}, page=1, page_size=10
            )
        )
        assert len(results) == 2

    def test_a_non_integer_version_candidate_matches_nothing_rather_than_raising(self):
        """A stringified version would raise out of Django's int coercion if forwarded."""
        backend = DjangoDbNotificationBackend()
        self._make(backend, requested_template_version=3)

        assert backend.count_notifications({"requested_template_version": "3"}) == 0
        assert backend.count_notifications({"requested_template_version": [3, "x"]}) == 0

    def test_filter_notifications_string_lookup(self):
        backend = DjangoDbNotificationBackend()
        self._make(backend, body_template="welcome_email")
        self._make(backend, body_template="goodbye_email")
        results = list(
            backend.filter_notifications(
                {"body_template": {"lookup": "starts_with", "value": "welcome"}},
                page=1,
                page_size=10,
            )
        )
        assert len(results) == 1
        assert results[0].body_template == "welcome_email"

    def test_filter_notifications_and_or_not(self):
        backend = DjangoDbNotificationBackend()
        self._make(backend, tenant="acme", body_template="body_a")
        self._make(backend, tenant="acme", body_template="body_b")
        self._make(backend, tenant="other", body_template="body_a")

        and_results = list(
            backend.filter_notifications(
                {"and": [{"tenant": "acme"}, {"body_template": "body_a"}]},
                page=1,
                page_size=10,
            )
        )
        assert len(and_results) == 1

        or_results = list(
            backend.filter_notifications(
                {"or": [{"tenant": "other"}, {"body_template": "body_b"}]},
                page=1,
                page_size=10,
            )
        )
        assert len(or_results) == 2

    def test_filter_notifications_not_includes_null_rows(self):
        backend = DjangoDbNotificationBackend()
        self._make(backend, tenant="acme")
        self._make(backend, tenant=None)  # tenant is NULL
        results = list(
            backend.filter_notifications({"not": {"tenant": "acme"}}, page=1, page_size=10)
        )
        # A positive filter never matches NULL, so NULL rows ARE included under negation.
        assert len(results) == 1
        assert results[0].tenant is None

    def test_filter_notifications_ordering_and_pagination(self):
        backend = DjangoDbNotificationBackend()
        first = self._make(backend, title="first")
        second = self._make(backend, title="second")
        order_by = {"field": "created_at", "direction": "asc"}
        page1 = list(backend.filter_notifications({}, page=1, page_size=1, order_by=order_by))
        page2 = list(backend.filter_notifications({}, page=2, page_size=1, order_by=order_by))
        assert [str(page1[0].id)] == [str(first.id)]
        assert [str(page2[0].id)] == [str(second.id)]

    def test_filter_notifications_date_range(self):
        backend = DjangoDbNotificationBackend()
        past = self._make(backend, send_after=timezone.now() - timedelta(days=2))
        self._make(backend, send_after=timezone.now() + timedelta(days=2))
        results = list(
            backend.filter_notifications(
                {"send_after_range": {"to": timezone.now()}}, page=1, page_size=10
            )
        )
        assert [str(n.id) for n in results] == [str(past.id)]

    def test_get_filter_capabilities_is_empty(self):
        assert DjangoDbNotificationBackend().get_filter_capabilities() == {}

    # --------------------------------------------------------------- resend attachment path

    def test_persist_notification_update_links_stored_attachments(self):
        backend = DjangoDbNotificationBackend()
        source = backend.persist_notification(
            user_id=self.user.pk,
            notification_type=NotificationTypes.EMAIL.value,
            title="Source",
            body_template="test",
            context_name="test",
            context_kwargs={},
            send_after=None,
            attachments=[self._attachment(content=b"resend bytes", filename="r.txt")],
        )
        clone = backend.persist_notification(
            user_id=self.user.pk,
            notification_type=NotificationTypes.EMAIL.value,
            title="Clone",
            body_template="test",
            context_name="test",
            context_kwargs={},
            send_after=None,
        )
        updated = backend.persist_notification_update(
            clone.id, {"attachments": list(source.attachments)}
        )
        assert len(updated.attachments) == 1
        assert updated.attachments[0].file.read() == b"resend bytes"
        # No new blob -- the clone references the source's file record.
        assert AttachmentFileRecordModel.objects.count() == 1

    # --------------------------------------------------------------- coverage: edges

    def test_future_notification_queries(self):
        backend = DjangoDbNotificationBackend()
        future = backend.persist_notification(
            user_id=self.user.pk,
            notification_type=NotificationTypes.EMAIL.value,
            title="Future",
            body_template="test",
            context_name="test",
            context_kwargs={},
            send_after=timezone.now() + timedelta(days=1),
        )
        assert any(str(n.id) == str(future.id) for n in backend.get_all_future_notifications())
        assert any(
            str(n.id) == str(future.id)
            for n in backend.get_future_notifications(page=1, page_size=10)
        )
        assert any(
            str(n.id) == str(future.id)
            for n in backend.get_all_future_notifications_from_user(self.user.pk)
        )
        assert any(
            str(n.id) == str(future.id)
            for n in backend.get_future_notifications_from_user(self.user.pk, page=1, page_size=10)
        )

    def test_get_user_email_from_notification_inactive_user_raises(self):
        from vintasend.exceptions import NotificationUserNotFoundError

        inactive = self.create_user(email="inactive@example.com", is_active=False)
        backend = DjangoDbNotificationBackend()
        notification = backend.persist_notification(
            user_id=inactive.pk,
            notification_type=NotificationTypes.EMAIL.value,
            title="Inactive",
            body_template="test",
            context_name="test",
            context_kwargs={},
            send_after=None,
        )
        with pytest.raises(NotificationUserNotFoundError):
            backend.get_user_email_from_notification(notification.id)

    def test_get_notification_for_update(self):
        backend = DjangoDbNotificationBackend()
        notification = backend.persist_notification(
            user_id=self.user.pk,
            notification_type=NotificationTypes.EMAIL.value,
            title="For update",
            body_template="test",
            context_name="test",
            context_kwargs={},
            send_after=None,
        )
        fetched = backend.get_notification(notification.id, for_update=True)
        assert str(fetched.id) == str(notification.id)

    def test_get_one_off_notification_not_found(self):
        backend = DjangoDbNotificationBackend()
        with pytest.raises(NotificationNotFoundError):
            backend._get_one_off_notification(random.randint(10000, 20000))

    def test_mark_pending_as_failed_already_sent_raises(self):
        backend = DjangoDbNotificationBackend()
        notification = backend.persist_notification(
            user_id=self.user.pk,
            notification_type=NotificationTypes.EMAIL.value,
            title="Failed guard",
            body_template="test",
            context_name="test",
            context_kwargs={},
            send_after=None,
        )
        backend.mark_pending_as_sent(notification.id)
        with pytest.raises(NotificationUpdateError):
            backend.mark_pending_as_failed(notification.id)

    def test_mark_sent_as_read_not_sent_raises(self):
        backend = DjangoDbNotificationBackend()
        notification = backend.persist_notification(
            user_id=self.user.pk,
            notification_type=NotificationTypes.IN_APP.value,
            title="Read guard",
            body_template="test",
            context_name="test",
            context_kwargs={},
            send_after=None,
        )
        # Still pending, never sent -> cannot be marked read.
        with pytest.raises(NotificationUpdateError):
            backend.mark_sent_as_read(notification.id)

    def test_persist_notification_update_already_sent_raises(self):
        backend = DjangoDbNotificationBackend()
        notification = backend.persist_notification(
            user_id=self.user.pk,
            notification_type=NotificationTypes.EMAIL.value,
            title="Update guard",
            body_template="test",
            context_name="test",
            context_kwargs={},
            send_after=None,
        )
        backend.mark_pending_as_sent(notification.id)
        with pytest.raises(NotificationUpdateError):
            backend.persist_notification_update(notification.id, {"title": "new"})

    def test_persist_notification_update_attachments_only_on_sent_raises(self):
        backend = DjangoDbNotificationBackend()
        source = backend.persist_notification(
            user_id=self.user.pk,
            notification_type=NotificationTypes.EMAIL.value,
            title="Attach source",
            body_template="test",
            context_name="test",
            context_kwargs={},
            send_after=None,
            attachments=[self._attachment(content=b"bytes", filename="a.txt")],
        )
        sent = backend.persist_notification(
            user_id=self.user.pk,
            notification_type=NotificationTypes.EMAIL.value,
            title="Already sent",
            body_template="test",
            context_name="test",
            context_kwargs={},
            send_after=None,
        )
        backend.mark_pending_as_sent(sent.id)
        with pytest.raises(NotificationUpdateError):
            backend.persist_notification_update(sent.id, {"attachments": list(source.attachments)})

    def test_persist_notification_update_unknown_stored_attachment_raises(self):
        from vintasend.services.dataclasses import StoredAttachment

        backend = DjangoDbNotificationBackend()
        clone = backend.persist_notification(
            user_id=self.user.pk,
            notification_type=NotificationTypes.EMAIL.value,
            title="Clone",
            body_template="test",
            context_name="test",
            context_kwargs={},
            send_after=None,
        )
        bogus = StoredAttachment(
            id="1",
            filename="x.txt",
            content_type="text/plain",
            size=1,
            checksum="x",
            created_at=timezone.now(),
            file=backend._attachment_manager.reconstruct_attachment_file(
                {"id": "missing", "name": "missing"}
            ),
            file_id="999999",
        )
        with pytest.raises(AttachmentFileNotFoundError):
            backend.persist_notification_update(clone.id, {"attachments": [bogus]})

    def test_store_context_used(self):
        backend = DjangoDbNotificationBackend()
        notification = backend.persist_notification(
            user_id=self.user.pk,
            notification_type=NotificationTypes.EMAIL.value,
            title="Context",
            body_template="test",
            context_name="test",
            context_kwargs={},
            send_after=None,
        )
        backend.store_context_used(notification.id, {"k": "v"}, "adapter.path")
        record = NotificationModel.objects.get(id=notification.id)
        assert record.context_used == {"k": "v"}
        assert record.adapter_used == "adapter.path"

    def test_filter_notifications_date_range_lower_bound(self):
        backend = DjangoDbNotificationBackend()
        recent = self._make(backend, send_after=timezone.now() + timedelta(days=1))
        self._make(backend, send_after=timezone.now() - timedelta(days=5))
        results = list(
            backend.filter_notifications(
                {"send_after_range": {"from": timezone.now()}}, page=1, page_size=10
            )
        )
        assert [str(n.id) for n in results] == [str(recent.id)]

    def test_filter_notifications_unknown_field_matches_nothing(self):
        backend = DjangoDbNotificationBackend()
        self._make(backend)
        assert list(backend.filter_notifications({"nope": "x"}, page=1, page_size=10)) == []
        # Under negation an unknown field matches everything.
        assert (
            len(list(backend.filter_notifications({"not": {"nope": "x"}}, page=1, page_size=10)))
            == 1
        )

    def test_filter_notifications_choice_accepts_member_or_wire_value(self):
        backend = DjangoDbNotificationBackend()
        email = self._make(backend, notification_type=NotificationTypes.EMAIL.value)
        self._make(backend, notification_type=NotificationTypes.IN_APP.value)
        by_member = list(
            backend.filter_notifications(
                {"notification_type": NotificationTypes.EMAIL}, page=1, page_size=10
            )
        )
        by_wire_value = list(
            backend.filter_notifications(
                {"notification_type": [NotificationTypes.EMAIL.value]}, page=1, page_size=10
            )
        )
        assert [str(n.id) for n in by_member] == [str(email.id)]
        assert [str(n.id) for n in by_wire_value] == [str(email.id)]

    def test_filter_notifications_unknown_choice_matches_nothing(self):
        backend = DjangoDbNotificationBackend()
        self._make(backend)
        # A status the enum does not define never reaches SQL.
        assert list(backend.filter_notifications({"status": "BOGUS"}, page=1, page_size=10)) == []
        # One bad candidate rejects the whole leaf, it does not degrade to the good ones.
        mixed = {"status": [NotificationStatus.PENDING_SEND.value, "BOGUS"]}
        assert list(backend.filter_notifications(mixed, page=1, page_size=10)) == []
        # A member of an unrelated enum is not a status either.
        wrong_enum = {"status": NotificationTypes.EMAIL}
        assert list(backend.filter_notifications(wrong_enum, page=1, page_size=10)) == []

    def test_filter_notifications_malformed_string_lookup_matches_nothing(self):
        backend = DjangoDbNotificationBackend()
        self._make(backend, body_template="body_a")
        unknown_lookup = {"body_template": {"lookup": "regex", "value": "body"}}
        assert list(backend.filter_notifications(unknown_lookup, page=1, page_size=10)) == []
        # A lookup with no needle would match every row, so it is rejected instead.
        no_value = {"body_template": {"lookup": "includes"}}
        assert list(backend.filter_notifications(no_value, page=1, page_size=10)) == []

    def test_filter_notifications_case_insensitive_string_lookup(self):
        backend = DjangoDbNotificationBackend()
        match = self._make(backend, body_template="Welcome_Email")
        self._make(backend, body_template="goodbye_email")
        results = list(
            backend.filter_notifications(
                {
                    "body_template": {
                        "lookup": "starts_with",
                        "value": "welcome",
                        "case_sensitive": False,
                    }
                },
                page=1,
                page_size=10,
            )
        )
        assert [str(n.id) for n in results] == [str(match.id)]

    def test_filter_notifications_unbounded_date_range_excludes_null_rows(self):
        backend = DjangoDbNotificationBackend()
        scheduled = self._make(backend, send_after=timezone.now())
        self._make(backend, send_after=None)
        # A range filter never matches a NULL, even with no bounds to compare against.
        results = list(backend.filter_notifications({"send_after_range": {}}, page=1, page_size=10))
        assert [str(n.id) for n in results] == [str(scheduled.id)]

    def test_filter_notifications_malformed_date_range_matches_nothing(self):
        backend = DjangoDbNotificationBackend()
        self._make(backend, send_after=timezone.now())
        # A bound that is not a datetime, and a typo'd bound that would silently widen the
        # range to unbounded, both reject the leaf.
        assert (
            list(
                backend.filter_notifications(
                    {"send_after_range": {"to": "2026-01-01"}}, page=1, page_size=10
                )
            )
            == []
        )
        assert (
            list(
                backend.filter_notifications(
                    {"send_after_range": {"form": timezone.now()}}, page=1, page_size=10
                )
            )
            == []
        )

    def test_filter_notifications_membership_list(self):
        backend = DjangoDbNotificationBackend()
        acme = self._make(backend, tenant="acme")
        globex = self._make(backend, tenant="globex")
        self._make(backend, tenant="initech")
        results = list(
            backend.filter_notifications({"tenant": ["acme", "globex"]}, page=1, page_size=10)
        )
        assert {str(n.id) for n in results} == {str(acme.id), str(globex.id)}

    def test_filter_notifications_empty_logical_groups(self):
        backend = DjangoDbNotificationBackend()
        self._make(backend)
        # Empty AND matches everything; empty OR matches nothing.
        assert len(list(backend.filter_notifications({"and": []}, page=1, page_size=10))) == 1
        assert list(backend.filter_notifications({"or": []}, page=1, page_size=10)) == []

    # --------------------------------------------------------------- attachment file handle

    def test_attachment_file_handle_stream_url_delete(self):
        backend = DjangoDbNotificationBackend()
        notification = backend.persist_notification(
            user_id=self.user.pk,
            notification_type=NotificationTypes.EMAIL.value,
            title="Handle",
            body_template="test",
            context_name="test",
            context_kwargs={},
            send_after=None,
            attachments=[self._attachment(content=b"stream me", filename="s.txt")],
        )
        stored = next(iter(backend.get_attachments(notification.id)))
        with stored.file.stream() as stream:
            assert stream.read() == b"stream me"
        assert stored.file.url()
        stored.file.delete()
