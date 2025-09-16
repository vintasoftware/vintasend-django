import random
from datetime import timedelta

from django.utils import timezone

import pytest
from vintasend.constants import NotificationStatus, NotificationTypes
from vintasend.exceptions import (
    NotificationCancelError,
    NotificationNotFoundError,
    NotificationUpdateError,
)
from vintasend.services.dataclasses import Notification, OneOffNotification

from vintasend_django.models import Notification as NotificationModel
from vintasend_django.services.notification_backends.django_db_notification_backend import (
    DjangoDbNotificationBackend,
)
from vintasend_django.test_helpers import VintaSendDjangoTestCase


class DjangoDBNotificationBackendTestCase(VintaSendDjangoTestCase):
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
        assert notification.user_id == self.user.pk
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
        assert notification_db_record.user_id == self.user.pk
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
            if isinstance(notification, Notification) and notification.id == regular_notification.id:
                regular_found = True
            elif isinstance(notification, OneOffNotification) and notification.id == one_off_notification.id:
                one_off_found = True
        
        assert regular_found, "Regular notification not found in pending notifications"
        assert one_off_found, "One-off notification not found in pending notifications"
