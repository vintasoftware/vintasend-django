import hashlib
import os
import random
import tempfile
from datetime import timedelta
from unittest.mock import Mock

from django.utils import timezone

import pytest
from vintasend.constants import NotificationStatus, NotificationTypes
from vintasend.exceptions import (
    NotificationCancelError,
    NotificationNotFoundError,
    NotificationUpdateError,
)
from vintasend.services.dataclasses import Notification, NotificationAttachment, OneOffNotification

from vintasend_django.models import Attachment as AttachmentModel
from vintasend_django.models import Notification as NotificationModel
from vintasend_django.services.notification_backends.django_db_notification_backend import (
    DjangoDbNotificationBackend,
)
from vintasend_django.test_helpers import VintaSendDjangoTestCase


class DjangoDBNotificationBackendTestCase(VintaSendDjangoTestCase):
    def setUp(self):
        super().setUp()
        self.temp_files = []  # Track temporary files created during tests

    def tearDown(self):
        super().tearDown()
        # Clean up temporary files
        for file_path in self.temp_files:
            try:
                if os.path.exists(file_path):
                    os.unlink(file_path)
            except OSError:
                pass  # File might already be deleted

        # Clean up attachment files from Django's file storage
        # This will clean up files created by attachment tests
        for attachment in AttachmentModel.objects.all():
            try:
                if attachment.file:
                    attachment.file.delete(save=False)
            except OSError:
                pass  # File might already be deleted

        # Additional cleanup: manually remove any remaining files in the attachments directory
        import glob
        attachment_dir = "notifications/attachments"
        if os.path.exists(attachment_dir):
            # Remove all files in the attachments directory
            for file_path in glob.glob(os.path.join(attachment_dir, "*")):
                try:
                    if os.path.isfile(file_path):
                        os.unlink(file_path)
                except OSError:
                    pass  # File might already be deleted

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

    def test_persist_notification_with_file_path_attachment(self):
        """Test persisting regular notification with file path attachment"""
        backend = DjangoDbNotificationBackend()

        # Create a temporary file for testing
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as tmp_file:
            tmp_file.write("Test attachment content")
            tmp_file_path = tmp_file.name

        # Track temp file for cleanup
        self.temp_files.append(tmp_file_path)

        # Create mock attachment
        mock_attachment = Mock(spec=NotificationAttachment)
        mock_attachment.file_path = tmp_file_path

        notification = backend.persist_notification(
            user_id=self.user.pk,
            notification_type=NotificationTypes.EMAIL.value,
            title="Test with attachment",
            body_template="test",
            context_name="test",
            context_kwargs={},
            send_after=None,
            attachments=[mock_attachment],
        )

        assert isinstance(notification, Notification)
        assert notification.user_id == str(self.user.pk)

        # Verify attachment was stored
        notification_db_record = NotificationModel.objects.get(id=notification.id)
        attachments = notification_db_record.attachments.all()
        assert len(attachments) == 1

        attachment = attachments[0]
        assert attachment.name == os.path.basename(tmp_file_path)
        assert attachment.mime_type == 'application/octet-stream'
        assert attachment.size > 0
        assert attachment.file  # File should be saved

    def test_persist_one_off_notification_with_file_path_attachment(self):
        """Test persisting one-off notification with file path attachment"""
        backend = DjangoDbNotificationBackend()

        # Create a temporary file for testing
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as tmp_file:
            tmp_file.write("Test one-off attachment content")
            tmp_file_path = tmp_file.name

        # Track temp file for cleanup
        self.temp_files.append(tmp_file_path)

        # Create mock attachment
        mock_attachment = Mock(spec=NotificationAttachment)
        mock_attachment.file_path = tmp_file_path

        one_off_notification = backend.persist_one_off_notification(
            email_or_phone="test@example.com",
            first_name="John",
            last_name="Doe",
            notification_type=NotificationTypes.EMAIL.value,
            title="One-off with attachment",
            body_template="test",
            context_name="test",
            context_kwargs={},
            attachments=[mock_attachment],
        )

        assert isinstance(one_off_notification, OneOffNotification)
        assert one_off_notification.email_or_phone == "test@example.com"

        # Verify attachment was stored
        notification_db_record = NotificationModel.objects.get(id=one_off_notification.id)
        attachments = notification_db_record.attachments.all()
        assert len(attachments) == 1

        attachment = attachments[0]
        assert attachment.name == os.path.basename(tmp_file_path)
        assert attachment.mime_type == 'application/octet-stream'
        assert attachment.size > 0
        assert attachment.file  # File should be saved

    def test_store_attachments_with_file_path(self):
        """Test _store_attachments method with file path attachment"""
        backend = DjangoDbNotificationBackend()

        # Create a temporary file for testing
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as tmp_file:
            tmp_file.write("Test content for attachment")
            tmp_file_path = tmp_file.name

        # Track the temp file for cleanup
        self.temp_files.append(tmp_file_path)

        # Create mock attachment with file_path
        mock_attachment = Mock(spec=NotificationAttachment)
        mock_attachment.file_path = tmp_file_path

        # Test the _store_attachments method directly
        stored_attachments = backend._store_attachments([mock_attachment])

        assert len(stored_attachments) == 1
        attachment = stored_attachments[0]

        # Verify attachment properties
        assert isinstance(attachment, AttachmentModel)
        assert attachment.name == os.path.basename(tmp_file_path)
        assert attachment.mime_type == 'application/octet-stream'
        assert attachment.size > 0
        assert attachment.file  # File should be saved but not yet linked to notification

        # Note: attachment.notification should be None since it's not yet linked
        assert attachment.notification_id is None

    def test_store_attachments_without_file_path(self):
        """Test _store_attachments method with attachment that doesn't have file_path"""
        backend = DjangoDbNotificationBackend()

        # Create mock attachment without file_path
        mock_attachment = Mock(spec=NotificationAttachment)
        # Deliberately not setting file_path attribute

        # Test the _store_attachments method directly
        with pytest.raises(ValueError):
            backend._store_attachments([mock_attachment])

    def test_store_attachments_multiple_files(self):
        """Test _store_attachments method with multiple file path attachments"""
        backend = DjangoDbNotificationBackend()

        # Create multiple temporary files for testing
        tmp_files = []
        for i in range(3):
            tmp_file = tempfile.NamedTemporaryFile(mode='w', suffix=f'_{i}.txt', delete=False)
            tmp_file.write(f"Test content for attachment {i}")
            tmp_file.close()
            tmp_files.append(tmp_file.name)

        # Track temp files for cleanup
        self.temp_files.extend(tmp_files)

        # Create mock attachments
        mock_attachments = []
        for tmp_file_path in tmp_files:
            mock_attachment = Mock(spec=NotificationAttachment)
            mock_attachment.file_path = tmp_file_path
            mock_attachments.append(mock_attachment)

        # Test the _store_attachments method with multiple attachments
        stored_attachments = backend._store_attachments(mock_attachments)

        assert len(stored_attachments) == 3

        # Sort both lists by file name for consistent comparison
        stored_attachments_sorted = sorted(stored_attachments, key=lambda x: x.name)
        tmp_files_sorted = sorted(tmp_files, key=lambda x: os.path.basename(x))

        for i, attachment in enumerate(stored_attachments_sorted):
            assert isinstance(attachment, AttachmentModel)
            assert attachment.name == os.path.basename(tmp_files_sorted[i])
            assert attachment.mime_type == 'application/octet-stream'
            assert attachment.size > 0
            assert attachment.file

    def test_persist_notification_with_multiple_attachments(self):
        """Test persisting regular notification with multiple attachments"""
        backend = DjangoDbNotificationBackend()

        # Create multiple temporary files for testing
        tmp_files = []
        for i in range(2):
            tmp_file = tempfile.NamedTemporaryFile(mode='w', suffix=f'_multi_{i}.txt', delete=False)
            tmp_file.write(f"Multi attachment content {i}")
            tmp_file.close()
            tmp_files.append(tmp_file.name)

        # Track temp files for cleanup
        self.temp_files.extend(tmp_files)

        # Create mock attachments
        mock_attachments = []
        for tmp_file_path in tmp_files:
            mock_attachment = Mock(spec=NotificationAttachment)
            mock_attachment.file_path = tmp_file_path
            mock_attachments.append(mock_attachment)

        notification = backend.persist_notification(
            user_id=self.user.pk,
            notification_type=NotificationTypes.EMAIL.value,
            title="Test with multiple attachments",
            body_template="test",
            context_name="test",
            context_kwargs={},
            send_after=None,
            attachments=mock_attachments,
        )

        assert isinstance(notification, Notification)

        # Verify both attachments were stored
        notification_db_record = NotificationModel.objects.get(id=notification.id)
        attachments = notification_db_record.attachments.all()
        assert len(attachments) == 2

        # Get attachment names and verify all expected files are there
        attachment_names = [attachment.name for attachment in attachments]
        expected_names = [os.path.basename(tmp_file) for tmp_file in tmp_files]
        assert sorted(attachment_names) == sorted(expected_names)

    def test_persist_one_off_notification_with_multiple_attachments(self):
        """Test persisting one-off notification with multiple attachments"""
        backend = DjangoDbNotificationBackend()

        # Create multiple temporary files for testing
        tmp_files = []
        for i in range(2):
            tmp_file = tempfile.NamedTemporaryFile(mode='w', suffix=f'_oneoff_multi_{i}.txt', delete=False)
            tmp_file.write(f"One-off multi attachment content {i}")
            tmp_file.close()
            tmp_files.append(tmp_file.name)

        # Track temp files for cleanup
        self.temp_files.extend(tmp_files)

        # Create mock attachments
        mock_attachments = []
        for tmp_file_path in tmp_files:
            mock_attachment = Mock(spec=NotificationAttachment)
            mock_attachment.file_path = tmp_file_path
            mock_attachments.append(mock_attachment)

        one_off_notification = backend.persist_one_off_notification(
            email_or_phone="multi@example.com",
            first_name="Jane",
            last_name="Smith",
            notification_type=NotificationTypes.EMAIL.value,
            title="One-off with multiple attachments",
            body_template="test",
            context_name="test",
            context_kwargs={},
            attachments=mock_attachments,
        )

        assert isinstance(one_off_notification, OneOffNotification)

        # Verify both attachments were stored
        notification_db_record = NotificationModel.objects.get(id=one_off_notification.id)
        attachments = notification_db_record.attachments.all()
        assert len(attachments) == 2

        # Get attachment names and verify all expected files are there
        attachment_names = [attachment.name for attachment in attachments]
        expected_names = [os.path.basename(tmp_file) for tmp_file in tmp_files]
        assert sorted(attachment_names) == sorted(expected_names)

    def test_persist_notification_with_empty_attachments_list(self):
        """Test persisting notification with empty attachments list"""
        backend = DjangoDbNotificationBackend()

        notification = backend.persist_notification(
            user_id=self.user.pk,
            notification_type=NotificationTypes.EMAIL.value,
            title="Test without attachments",
            body_template="test",
            context_name="test",
            context_kwargs={},
            send_after=None,
            attachments=[],  # Empty list
        )

        assert isinstance(notification, Notification)

        # Verify no attachments were stored
        notification_db_record = NotificationModel.objects.get(id=notification.id)
        attachments = notification_db_record.attachments.all()
        assert len(attachments) == 0

    def test_persist_notification_with_none_attachments(self):
        """Test persisting notification with None attachments"""
        backend = DjangoDbNotificationBackend()

        notification = backend.persist_notification(
            user_id=self.user.pk,
            notification_type=NotificationTypes.EMAIL.value,
            title="Test with None attachments",
            body_template="test",
            context_name="test",
            context_kwargs={},
            send_after=None,
            attachments=None,  # None
        )

        assert isinstance(notification, Notification)

        # Verify no attachments were stored
        notification_db_record = NotificationModel.objects.get(id=notification.id)
        attachments = notification_db_record.attachments.all()
        assert len(attachments) == 0

    def test_store_attachments_with_file_read_error(self):
        """Test _store_attachments method with file that cannot be read"""
        backend = DjangoDbNotificationBackend()

        # Create mock attachment with non-existent file path
        mock_attachment = Mock(spec=NotificationAttachment)
        mock_attachment.file_path = "/non/existent/file/path.txt"

        # Test should raise FileNotFoundError
        with pytest.raises(FileNotFoundError):
            backend._store_attachments([mock_attachment])

    def test_serialize_one_off_notification_with_attachments(self):
        """Test serialization of one-off notification includes attachments"""
        backend = DjangoDbNotificationBackend()

        # Create a temporary file for testing
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as tmp_file:
            tmp_file.write("Serialization test content")
            tmp_file_path = tmp_file.name

        # Track temp file for cleanup
        self.temp_files.append(tmp_file_path)

        # Create mock attachment
        mock_attachment = Mock(spec=NotificationAttachment)
        mock_attachment.file_path = tmp_file_path

        one_off_notification = backend.persist_one_off_notification(
            email_or_phone="serialize@example.com",
            first_name="Test",
            last_name="User",
            notification_type=NotificationTypes.EMAIL.value,
            title="Serialization test",
            body_template="test",
            context_name="test",
            context_kwargs={},
            attachments=[mock_attachment],
        )

        # Verify serialization includes attachments
        assert isinstance(one_off_notification, OneOffNotification)
        assert hasattr(one_off_notification, 'attachments')
        assert len(one_off_notification.attachments) == 1

        attachment = one_off_notification.attachments[0]
        assert attachment.filename == os.path.basename(tmp_file_path)
        assert attachment.content_type == 'application/octet-stream'
        assert attachment.size > 0

    def test_serialize_attachment_checksum_calculation(self):
        """Test that _serialize_attachment correctly calculates SHA-256 checksum"""
        backend = DjangoDbNotificationBackend()

        # Create a temporary file with known content
        test_content = b"Test content for checksum calculation"
        expected_checksum = hashlib.sha256(test_content).hexdigest()

        with tempfile.NamedTemporaryFile(delete=False) as tmp_file:
            tmp_file.write(test_content)
            tmp_file_path = tmp_file.name

        self.temp_files.append(tmp_file_path)

        try:
            # Create mock attachment and store it
            mock_attachment = Mock(spec=NotificationAttachment)
            mock_attachment.file_path = tmp_file_path

            stored_attachments = backend._store_attachments([mock_attachment])
            attachment_instance = stored_attachments[0]

            # Set notification (required for save)
            notification_instance = NotificationModel.objects.create(
                user_id=str(self.user.pk),
                notification_type=NotificationTypes.EMAIL.value,
                title="Checksum test",
                body_template="test",
                context_name="test",
                context_kwargs={},
            )
            attachment_instance.notification = notification_instance
            attachment_instance.save()

            # Test the _serialize_attachment method
            stored_attachment = backend._serialize_attachment(attachment_instance)

            # Verify checksum is calculated correctly
            assert stored_attachment.checksum == expected_checksum
            assert len(stored_attachment.checksum) == 64  # SHA-256 hex length
            assert stored_attachment.filename == os.path.basename(tmp_file_path)
            assert stored_attachment.size == len(test_content)

        finally:
            # Cleanup is handled by tearDown method
            pass

    def test_serialize_attachment_checksum_with_empty_file(self):
        """Test checksum calculation with an empty file"""
        backend = DjangoDbNotificationBackend()

        # Empty content should have specific SHA-256 hash
        test_content = b""
        expected_checksum = hashlib.sha256(test_content).hexdigest()

        with tempfile.NamedTemporaryFile(delete=False) as tmp_file:
            # Don't write anything - file is empty
            tmp_file_path = tmp_file.name

        self.temp_files.append(tmp_file_path)

        try:
            # Create mock attachment and store it
            mock_attachment = Mock(spec=NotificationAttachment)
            mock_attachment.file_path = tmp_file_path

            stored_attachments = backend._store_attachments([mock_attachment])
            attachment_instance = stored_attachments[0]

            # Set notification (required for save)
            notification_instance = NotificationModel.objects.create(
                user_id=str(self.user.pk),
                notification_type=NotificationTypes.EMAIL.value,
                title="Empty checksum test",
                body_template="test",
                context_name="test",
                context_kwargs={},
            )
            attachment_instance.notification = notification_instance
            attachment_instance.save()

            # Test the _serialize_attachment method
            stored_attachment = backend._serialize_attachment(attachment_instance)

            # Verify checksum for empty file
            assert stored_attachment.checksum == expected_checksum
            assert stored_attachment.checksum == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"  # SHA-256 of empty string

        finally:
            # Cleanup is handled by tearDown method
            pass

    def test_serialize_attachment_checksum_with_binary_content(self):
        """Test checksum calculation with binary content"""
        backend = DjangoDbNotificationBackend()

        # Create binary content
        test_content = bytes(range(256))  # All possible byte values
        expected_checksum = hashlib.sha256(test_content).hexdigest()

        with tempfile.NamedTemporaryFile(delete=False) as tmp_file:
            tmp_file.write(test_content)
            tmp_file_path = tmp_file.name

        self.temp_files.append(tmp_file_path)

        try:
            # Create mock attachment and store it
            mock_attachment = Mock(spec=NotificationAttachment)
            mock_attachment.file_path = tmp_file_path

            stored_attachments = backend._store_attachments([mock_attachment])
            attachment_instance = stored_attachments[0]

            # Set notification (required for save)
            notification_instance = NotificationModel.objects.create(
                user_id=str(self.user.pk),
                notification_type=NotificationTypes.EMAIL.value,
                title="Binary checksum test",
                body_template="test",
                context_name="test",
                context_kwargs={},
            )
            attachment_instance.notification = notification_instance
            attachment_instance.save()

            # Test the _serialize_attachment method
            stored_attachment = backend._serialize_attachment(attachment_instance)

            # Verify checksum for binary content
            assert stored_attachment.checksum == expected_checksum
            assert len(stored_attachment.checksum) == 64  # SHA-256 hex length
            assert stored_attachment.size == len(test_content)

        finally:
            # Cleanup is handled by tearDown method
            pass

    def test_serialize_attachment_checksum_with_no_file(self):
        """Test _serialize_attachment when attachment has no file"""
        backend = DjangoDbNotificationBackend()

        # Create attachment without file
        attachment_instance = AttachmentModel(
            name="no_file_attachment",
            mime_type="text/plain",
            size=0,
        )

        # Don't set the file field

        # Test the _serialize_attachment method
        stored_attachment = backend._serialize_attachment(attachment_instance)

        # Should have empty checksum when no file
        assert stored_attachment.checksum == ""
        assert stored_attachment.filename == "no_file_attachment"
        assert stored_attachment.size == 0

    def test_serialize_attachment_checksum_with_file_read_error(self):
        """Test _serialize_attachment handles file read errors gracefully"""
        backend = DjangoDbNotificationBackend()

        # Create attachment with mock file that raises OSError
        attachment_instance = AttachmentModel(
            name="error_file_attachment",
            mime_type="text/plain",
            size=100,
        )

        # Mock the file to raise an OSError when read
        mock_file = Mock()
        mock_file.seek.side_effect = OSError("File read error")
        attachment_instance.file = mock_file

        # Test the _serialize_attachment method
        stored_attachment = backend._serialize_attachment(attachment_instance)

        # Should have empty checksum when file read fails
        assert stored_attachment.checksum == ""
        assert stored_attachment.filename == "error_file_attachment"
        assert stored_attachment.size == 100
