import random
import pytest
from datetime import timedelta

from django.utils import timezone

from vintasend_django.test_helpers import VintaSendDjangoTestCase
from vintasend.constants import NotificationStatus, NotificationTypes
from vintasend.exceptions import (
    NotificationCancelError,
    NotificationNotFoundError,
    NotificationUpdateError,
)
from vintasend.services.dataclasses import Notification
from vintasend_django.models import Notification as NotificationModel
from vintasend_django.services.notification_backends.django_db_notification_backend import (
    DjangoDbNotificationBackend,
)


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
