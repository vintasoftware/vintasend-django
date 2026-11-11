"""The version token, conditional requeue and repin, version listing and the presence filters.

Every refusal is checked to write nothing: the row is read back and compared with what it was
before the call, ``notification_version`` included.
"""

import re
import shutil
import tempfile
import uuid
from datetime import timedelta
from unittest import mock

from django.db import connection
from django.db.models import QuerySet
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

import pytest
from vintasend.constants import NotificationStatus, NotificationTypes
from vintasend.exceptions import (
    AttachmentFileNotFoundError,
    NotificationActionError,
    NotificationNotFoundError,
    NotificationRepinError,
    NotificationRequeueError,
    NotificationUpdateError,
    NotificationVersionChangedError,
)
from vintasend.services.dataclasses import (
    Notification,
    NotificationAttachment,
    NotificationAttachmentReference,
    NotificationVersion,
)
from vintasend.services.notification_backends.filters import matches_filter
from vintasend.services.notification_service import NotificationService, register_context

from vintasend_django.models import Notification as NotificationModel
from vintasend_django.models import NotificationAttachment as NotificationAttachmentModel
from vintasend_django.services.notification_backends import django_db_notification_backend
from vintasend_django.services.notification_backends.django_db_notification_backend import (
    DjangoDbNotificationBackend,
)
from vintasend_django.test_helpers import VintaSendDjangoTestCase


PENDING = NotificationStatus.PENDING_SEND
SENT = NotificationStatus.SENT
READ = NotificationStatus.READ
FAILED = NotificationStatus.FAILED
CANCELLED = NotificationStatus.CANCELLED

FAKE_EMAIL_ADAPTER = "vintasend.services.notification_adapters.stubs.fake_adapter.FakeEmailAdapter"
FAKE_RENDERER = (
    "vintasend.services.notification_template_renderers.stubs."
    "fake_templated_email_renderer.FakeTemplateRenderer"
)
CONTEXT_NAME = "django_conditional_moves_test_context"
register_context(CONTEXT_NAME)(lambda: {"greeting": "hi"})

# Every column a refused move could touch, plus the token itself.
SNAPSHOT_FIELDS = (
    "status",
    "requested_template_version",
    "send_after",
    "sent_at",
    "modified",
    "notification_version",
)


def stranded_filter(body_template: str, version: int) -> dict:
    """Pending and scheduled but pinned to another version, or failed: what a fix should move."""
    return {
        "and": [
            {"body_template": body_template},
            {
                "or": [
                    {
                        "and": [
                            {"status": PENDING},
                            {"has_send_after": True},
                            {"has_requested_template_version": True},
                            {"not": {"requested_template_version": version}},
                        ]
                    },
                    {"status": FAILED},
                ]
            },
        ]
    }


class ConditionalMovesTestCase(VintaSendDjangoTestCase):
    def setUp(self):
        super().setUp()
        self._media_root = tempfile.mkdtemp()
        self._media_override = override_settings(MEDIA_ROOT=self._media_root)
        self._media_override.enable()
        self.backend = DjangoDbNotificationBackend()

    def tearDown(self):
        self._media_override.disable()
        shutil.rmtree(self._media_root, ignore_errors=True)
        super().tearDown()

    def _make(self, status: NotificationStatus = PENDING, **kwargs) -> Notification:
        kwargs.setdefault("send_after", timezone.now() + timedelta(days=1))
        notification = self.backend.persist_notification(
            user_id=self.user.pk,
            notification_type=NotificationTypes.EMAIL.value,
            title="Move",
            body_template=kwargs.pop("body_template", "welcome"),
            context_name=kwargs.pop("context_name", CONTEXT_NAME),
            context_kwargs={},
            **kwargs,
        )
        if status != PENDING:
            # A save, so the status change moves the token like any other write.
            record = NotificationModel.objects.get(id=notification.id)
            record.status = status.value
            record.save(update_fields=["status"])
        return notification

    @staticmethod
    def _snapshot(notification_id) -> dict:
        return NotificationModel.objects.values(*SNAPSHOT_FIELDS).get(id=notification_id)

    @staticmethod
    def _token(notification_id) -> str:
        return str(NotificationModel.objects.get(id=notification_id).notification_version)


class VersionTokenTestCase(ConditionalMovesTestCase):
    def test_every_row_gets_its_own_uuid7(self):
        first = self._make()
        second = self._make()
        one_off = self.backend.persist_one_off_notification(
            email_or_phone="someone@example.com",
            first_name="Some",
            last_name="One",
            notification_type=NotificationTypes.EMAIL.value,
            title="One-off",
            body_template="welcome",
            context_name=CONTEXT_NAME,
            context_kwargs={},
        )

        tokens = {self._token(n.id) for n in (first, second, one_off)}
        assert len(tokens) == 3
        assert all(uuid.UUID(token).version == 7 for token in tokens)

    def test_every_backend_write_moves_the_token(self):
        source = self.backend.persist_notification(
            user_id=self.user.pk,
            notification_type=NotificationTypes.EMAIL.value,
            title="Source",
            body_template="welcome",
            context_name=CONTEXT_NAME,
            context_kwargs={},
            send_after=None,
            attachments=[NotificationAttachment(file=b"report", filename="report.txt")],
        )
        backend = self.backend

        def sent_one():
            notification = self._make()
            backend.mark_pending_as_sent(notification.id)
            return notification

        writes = {
            "persist_notification_update": (
                self._make,
                lambda n: backend.persist_notification_update(n.id, {"title": "new"}),
            ),
            "persist_notification_update linking attachments": (
                self._make,
                lambda n: backend.persist_notification_update(
                    n.id, {"attachments": list(source.attachments)}
                ),
            ),
            "mark_pending_as_sent": (self._make, lambda n: backend.mark_pending_as_sent(n.id)),
            "mark_pending_as_failed": (self._make, lambda n: backend.mark_pending_as_failed(n.id)),
            "mark_sent_as_read": (sent_one, lambda n: backend.mark_sent_as_read(n.id)),
            "mark_sent_as_read_bulk": (
                sent_one,
                lambda n: list(backend.mark_sent_as_read_bulk([n.id], user_id=self.user.pk)),
            ),
            "cancel_notification": (self._make, lambda n: backend.cancel_notification(n.id)),
            "requeue_notification": (
                lambda: self._make(FAILED),
                lambda n: backend.requeue_notification(n.id),
            ),
            "repin_notification": (self._make, lambda n: backend.repin_notification(n.id, 7)),
            "store_context_used": (
                self._make,
                lambda n: backend.store_context_used(n.id, {"k": "v"}, "adapter.path"),
            ),
            "store_git_commit_sha": (
                self._make,
                lambda n: backend.store_git_commit_sha(n.id, "a" * 40),
            ),
            "store_template_version": (
                self._make,
                lambda n: backend.store_template_version(n.id, 3),
            ),
            "delete_notification_attachment": (
                lambda: source,
                lambda n: backend.delete_notification_attachment(n.attachments[0].id),
            ),
            "Model.save": (
                self._make,
                lambda n: NotificationModel.objects.get(id=n.id).save(),
            ),
            "Model.save with update_fields": (
                self._make,
                lambda n: NotificationModel.objects.get(id=n.id).save(update_fields=["title"]),
            ),
        }
        for name, (make, write) in writes.items():
            with self.subTest(write=name):
                notification = make()
                before = self._token(notification.id)

                write(notification)

                assert self._token(notification.id) != before

    def test_save_with_empty_update_fields_writes_nothing(self):
        notification = self._make()
        before = self._token(notification.id)

        NotificationModel.objects.get(id=notification.id).save(update_fields=[])

        assert self._token(notification.id) == before

    def test_a_refused_write_leaves_the_token(self):
        notification = self._make(SENT)
        before = self._snapshot(notification.id)

        with pytest.raises(NotificationUpdateError):
            self.backend.mark_pending_as_failed(notification.id)
        with pytest.raises(NotificationUpdateError):
            self.backend.persist_notification_update(notification.id, {"title": "new"})

        assert self._snapshot(notification.id) == before

    def test_a_failed_attachment_link_rolls_back_the_update(self):
        notification = self._make()
        before = self._snapshot(notification.id)
        title_before = NotificationModel.objects.get(id=notification.id).title
        missing_file = NotificationAttachmentReference(file_id=str(uuid.uuid4()))

        with pytest.raises(AttachmentFileNotFoundError):
            self.backend.persist_notification_update(
                notification.id,
                {"title": "x", "attachments": [missing_file]},
            )

        assert self._snapshot(notification.id) == before
        assert NotificationModel.objects.get(id=notification.id).title == title_before
        assert not NotificationAttachmentModel.objects.filter(
            notification_id=notification.id
        ).exists()

    def test_creating_with_attachments_moves_the_token_once_after_linking(self):
        """The token the row is created with is replaced once the attachments are linked."""
        moved_to = uuid.uuid4()
        attachment = NotificationAttachment(file=b"report", filename="report.txt")
        creators = {
            "persist_notification": lambda attachments: self.backend.persist_notification(
                user_id=self.user.pk,
                notification_type=NotificationTypes.EMAIL.value,
                title="Created",
                body_template="welcome",
                context_name=CONTEXT_NAME,
                context_kwargs={},
                send_after=None,
                attachments=attachments,
            ),
            "persist_one_off_notification": lambda attachments: (
                self.backend.persist_one_off_notification(
                    email_or_phone="someone@example.com",
                    first_name="Some",
                    last_name="One",
                    notification_type=NotificationTypes.EMAIL.value,
                    title="Created",
                    body_template="welcome",
                    context_name=CONTEXT_NAME,
                    context_kwargs={},
                    attachments=attachments,
                )
            ),
        }
        for name, create in creators.items():
            with self.subTest(create=name):
                # Patched in the backend only: the row's own default still runs at INSERT.
                with (
                    mock.patch.object(
                        django_db_notification_backend,
                        "new_notification_version",
                        return_value=moved_to,
                    ) as new_version,
                    CaptureQueriesContext(connection) as queries,
                ):
                    with_attachment = create([attachment])
                    without_attachment = create(None)

                assert new_version.call_count == 1
                assert self._token(with_attachment.id) == str(moved_to)
                assert self._token(without_attachment.id) != str(moved_to)
                statements = [q["sql"].split(" ", 1)[0] for q in queries.captured_queries]
                assert statements.count("UPDATE") == 1
                NotificationModel.objects.filter(
                    id__in=[with_attachment.id, without_attachment.id]
                ).delete()


class RequeueTestCase(ConditionalMovesTestCase):
    def test_requeues_pins_and_schedules_when_the_version_matches(self):
        notification = self._make(FAILED, requested_template_version=1)
        token = self._token(notification.id)
        send_after = timezone.now() + timedelta(hours=3)

        requeued = self.backend.requeue_notification(
            notification.id, send_after, template_version=4, expected_version=token
        )

        assert requeued.status == PENDING.value
        assert requeued.requested_template_version == 4
        assert requeued.send_after == send_after
        assert requeued.sent_at is None
        assert self._token(notification.id) != token

    def test_pins_and_requeues_in_one_update(self):
        notification = self._make(CANCELLED)

        with CaptureQueriesContext(connection) as queries:
            self.backend.requeue_notification(
                notification.id,
                template_version=2,
                expected_version=self._token(notification.id),
            )

        updates = [q["sql"] for q in queries.captured_queries if q["sql"].startswith("UPDATE")]
        assert len(updates) == 1

    def test_a_changed_notification_is_refused_and_nothing_is_written(self):
        notification = self._make(FAILED)
        stale = self._token(notification.id)
        self.backend.store_template_version(notification.id, 9)  # someone else writes
        before = self._snapshot(notification.id)

        with pytest.raises(NotificationVersionChangedError) as raised:
            self.backend.requeue_notification(
                notification.id, None, template_version=4, expected_version=stale
            )

        assert str(raised.value) == (
            f"Cannot requeue notification {notification.id}: it changed since that version was read"
        )
        assert self._snapshot(notification.id) == before

    def test_a_malformed_token_matches_nothing_and_is_refused_as_changed(self):
        notification = self._make(FAILED)
        token = self._token(notification.id)
        before = self._snapshot(notification.id)

        for malformed in (
            "not-a-uuid",
            "",
            token.upper(),
            "{" + token + "}",
            token.replace("-", ""),
            123,
        ):
            with self.subTest(token=malformed), pytest.raises(NotificationVersionChangedError):
                self.backend.requeue_notification(
                    notification.id,
                    expected_version=malformed,
                )

        assert self._snapshot(notification.id) == before

    def test_an_unknown_id_with_a_malformed_token_is_not_found(self):
        missing = NotificationModel.objects.order_by("-id").values_list("id", flat=True).first()
        missing = (missing or 0) + 1000

        for malformed in ("not-a-uuid", "", 123):
            with self.subTest(token=malformed), pytest.raises(NotificationNotFoundError):
                self.backend.requeue_notification(
                    missing,
                    expected_version=malformed,
                )

    def test_a_change_between_the_update_and_the_read_is_refused(self):
        """The UPDATE matched nothing, yet the row is requeueable (and on the expected version)
        when read: it changed in between, so this is CHANGED, not a status answer."""
        notification = self._make(FAILED)
        before = self._snapshot(notification.id)

        for expected_version in (None, str(before["notification_version"])):
            with (
                self.subTest(expected_version=expected_version),
                mock.patch.object(QuerySet, "update", return_value=0),
                pytest.raises(NotificationVersionChangedError),
            ):
                self.backend.requeue_notification(
                    notification.id, expected_version=expected_version
                )

        assert self._snapshot(notification.id) == before

    def test_the_wrong_status_is_refused_when_the_version_matches(self):
        for status in (PENDING, SENT, READ):
            with self.subTest(status=status):
                notification = self._make(status)
                before = self._snapshot(notification.id)

                with pytest.raises(NotificationRequeueError):
                    self.backend.requeue_notification(
                        notification.id,
                        None,
                        template_version=4,
                        expected_version=str(before["notification_version"]),
                    )

                assert self._snapshot(notification.id) == before

    def test_a_changed_version_is_reported_before_the_wrong_status(self):
        notification = self._make(SENT)
        before = self._snapshot(notification.id)

        with pytest.raises(NotificationVersionChangedError):
            self.backend.requeue_notification(notification.id, expected_version=str(uuid.uuid4()))

        assert self._snapshot(notification.id) == before

    def test_an_unknown_id_is_not_found(self):
        missing = NotificationModel.objects.order_by("-id").values_list("id", flat=True).first()
        missing = (missing or 0) + 1000

        with pytest.raises(NotificationNotFoundError):
            self.backend.requeue_notification(missing, expected_version=str(uuid.uuid4()))

    def test_a_refusal_reads_only_the_status_and_the_version(self):
        notification = self._make(SENT)

        with CaptureQueriesContext(connection) as queries, pytest.raises(NotificationRequeueError):
            self.backend.requeue_notification(notification.id)

        selects = [q["sql"] for q in queries.captured_queries if q["sql"].startswith("SELECT")]
        assert len(selects) == 1
        assert _selected_columns(selects[0]) == ["status", "notification_version"]


class RepinTestCase(ConditionalMovesTestCase):
    def test_pins_an_unpinned_notification_and_changes_nothing_else(self):
        # The WHERE must name NULL explicitly: ``requested_template_version <> 3`` is not true
        # for a NULL.
        notification = self._make(PENDING)
        before = self._snapshot(notification.id)
        assert before["requested_template_version"] is None

        repinned = self.backend.repin_notification(notification.id, 3)

        after = self._snapshot(notification.id)
        assert repinned.requested_template_version == 3
        assert after["requested_template_version"] == 3
        assert after["notification_version"] != before["notification_version"]
        assert after["modified"] >= before["modified"]
        for field in ("status", "send_after", "sent_at"):
            assert after[field] == before[field]

    def test_moves_a_pin_on_every_repinnable_status(self):
        for status in (PENDING, FAILED, CANCELLED):
            with self.subTest(status=status):
                notification = self._make(status, requested_template_version=1)

                repinned = self.backend.repin_notification(
                    notification.id, 2, expected_version=self._token(notification.id)
                )

                assert repinned.requested_template_version == 2
                assert repinned.status == status.value

    def test_a_changed_notification_is_refused_and_nothing_is_written(self):
        notification = self._make(PENDING, requested_template_version=1)
        stale = self._token(notification.id)
        self.backend.store_context_used(notification.id, {}, "adapter.path")
        before = self._snapshot(notification.id)

        with pytest.raises(NotificationVersionChangedError):
            self.backend.repin_notification(notification.id, 2, expected_version=stale)

        assert self._snapshot(notification.id) == before

    def test_a_malformed_token_matches_nothing_and_is_refused_as_changed(self):
        notification = self._make(PENDING)
        before = self._snapshot(notification.id)

        for malformed in ("v1", "", 123):
            with self.subTest(token=malformed), pytest.raises(NotificationVersionChangedError):
                self.backend.repin_notification(
                    notification.id,
                    2,
                    expected_version=malformed,
                )

        assert self._snapshot(notification.id) == before

    def test_an_unknown_id_with_a_malformed_token_is_not_found(self):
        missing = (
            NotificationModel.objects.order_by("-id").values_list("id", flat=True).first() or 0
        ) + 1000

        for malformed in ("v1", "", 123):
            with self.subTest(token=malformed), pytest.raises(NotificationNotFoundError):
                self.backend.repin_notification(
                    missing,
                    2,
                    expected_version=malformed,
                )

    def test_a_delivered_notification_is_refused_and_nothing_is_written(self):
        for status in (SENT, READ):
            with self.subTest(status=status):
                notification = self._make(status, requested_template_version=1)
                before = self._snapshot(notification.id)

                with pytest.raises(NotificationRepinError):
                    self.backend.repin_notification(notification.id, 2)
                with pytest.raises(NotificationRepinError):
                    self.backend.repin_notification(
                        notification.id,
                        2,
                        expected_version=str(before["notification_version"]),
                    )
                # A version that does not match is reported first.
                with pytest.raises(NotificationVersionChangedError):
                    self.backend.repin_notification(
                        notification.id, 2, expected_version=str(uuid.uuid4())
                    )

                assert self._snapshot(notification.id) == before

    def test_an_unknown_id_is_not_found(self):
        missing = (
            NotificationModel.objects.order_by("-id").values_list("id", flat=True).first() or 0
        ) + 1000

        with pytest.raises(NotificationNotFoundError):
            self.backend.repin_notification(missing, 2)

    def test_already_on_the_version_returns_it_and_writes_nothing(self):
        notification = self._make(FAILED, requested_template_version=5)
        before = self._snapshot(notification.id)

        for expected_version in (None, str(before["notification_version"])):
            with self.subTest(expected_version=expected_version):
                repinned = self.backend.repin_notification(
                    notification.id, 5, expected_version=expected_version
                )

                assert repinned.requested_template_version == 5
                assert self._snapshot(notification.id) == before

    def test_already_on_the_version_with_a_stale_token_is_changed(self):
        notification = self._make(FAILED, requested_template_version=5)
        before = self._snapshot(notification.id)

        with pytest.raises(NotificationVersionChangedError):
            self.backend.repin_notification(notification.id, 5, expected_version=str(uuid.uuid4()))

        assert self._snapshot(notification.id) == before

    def test_a_change_between_the_update_and_the_read_is_refused(self):
        """The UPDATE matched nothing, yet the row matches every condition when read: it changed
        in between, so this is CHANGED, not a status or a pin answer."""
        notification = self._make(PENDING, requested_template_version=1)
        before = self._snapshot(notification.id)

        with mock.patch.object(QuerySet, "update", return_value=0):
            with pytest.raises(NotificationVersionChangedError) as raised:
                self.backend.repin_notification(
                    notification.id, 2, expected_version=str(before["notification_version"])
                )

        assert str(raised.value) == (
            f"Cannot repin notification {notification.id}: it changed while it was being written"
        )
        assert self._snapshot(notification.id) == before

    def test_one_update_and_no_read_when_it_matches(self):
        notification = self._make(PENDING)

        with CaptureQueriesContext(connection) as queries:
            self.backend.repin_notification(notification.id, 2)

        statements = [q["sql"].split(" ", 1)[0] for q in queries.captured_queries]
        # The UPDATE, then the read of the result and its attachments.
        assert statements[0] == "UPDATE"
        assert statements.count("UPDATE") == 1


class FilterNotificationVersionsTestCase(ConditionalMovesTestCase):
    def test_lists_ids_and_tokens_ordered_by_id(self):
        created = [self._make() for _ in range(3)]

        versions = self.backend.filter_notification_versions({}, page=1, page_size=10)

        assert versions == [
            NotificationVersion(id=n.id, version=self._token(n.id))
            for n in sorted(created, key=lambda n: n.id)
        ]
        assert all(isinstance(v.id, int) for v in versions)

    def test_pages_are_one_indexed(self):
        ids = sorted(self._make().id for _ in range(5))

        pages = [
            [v.id for v in self.backend.filter_notification_versions({}, page, 2)]
            for page in (1, 2, 3, 4)
        ]

        assert pages == [ids[0:2], ids[2:4], ids[4:5], []]

    def test_applies_the_filter(self):
        failed = self._make(FAILED)
        self._make(PENDING)

        versions = self.backend.filter_notification_versions({"status": FAILED}, 1, 10)

        assert [v.id for v in versions] == [failed.id]

    def test_selects_the_id_and_the_token_only(self):
        self._make(FAILED, attachments=[NotificationAttachment(file=b"x", filename="x.txt")])

        with CaptureQueriesContext(connection) as queries:
            self.backend.filter_notification_versions(
                {"and": [{"status": FAILED}, {"body_template": "welcome"}]}, 1, 10
            )

        assert len(queries.captured_queries) == 1
        sql = queries.captured_queries[0]["sql"]
        assert _selected_columns(sql) == ["id", "notification_version"]
        assert "JOIN" not in sql

    def test_a_listed_token_moves_the_notification(self):
        notification = self._make(FAILED)
        [listed] = self.backend.filter_notification_versions({"status": FAILED}, 1, 10)

        self.backend.requeue_notification(listed.id, expected_version=listed.version)

        assert self._snapshot(notification.id)["status"] == PENDING.value
        with pytest.raises(NotificationVersionChangedError):
            self.backend.repin_notification(listed.id, 2, expected_version=listed.version)


class PresenceFiltersTestCase(ConditionalMovesTestCase):
    def _ids(self, filter_) -> set:
        """The rows the SQL translation matches, checked against vintasend's reference evaluator
        over the same notifications, so SQL NULL handling cannot drift from the documented one."""
        matched = {n.id for n in self.backend.filter_notifications(filter_, page=1, page_size=100)}
        everything = self.backend.filter_notifications({}, page=1, page_size=100)
        assert matched == {n.id for n in everything if matches_filter(n, filter_)}
        return matched

    def test_has_send_after(self):
        scheduled = self._make()
        unscheduled = self._make(send_after=None)

        assert self._ids({"has_send_after": True}) == {scheduled.id}
        assert self._ids({"has_send_after": False}) == {unscheduled.id}
        assert self._ids({"not": {"has_send_after": True}}) == {unscheduled.id}
        assert self._ids({"not": {"has_send_after": False}}) == {scheduled.id}

    def test_has_requested_template_version(self):
        pinned = self._make(requested_template_version=0)
        unpinned = self._make()

        assert self._ids({"has_requested_template_version": True}) == {pinned.id}
        assert self._ids({"has_requested_template_version": False}) == {unpinned.id}
        assert self._ids({"not": {"has_requested_template_version": True}}) == {unpinned.id}
        assert self._ids({"not": {"has_requested_template_version": False}}) == {pinned.id}

    def test_a_non_bool_matches_nothing_and_its_negation_everything(self):
        both = {self._make().id, self._make(send_after=None).id}

        for value in (1, "true", None):
            with self.subTest(value=value):
                assert self._ids({"has_send_after": value}) == set()
                assert self._ids({"not": {"has_send_after": value}}) == both

    def test_a_negated_version_includes_unpinned_rows(self):
        on_v = self._make(requested_template_version=3)
        other = self._make(requested_template_version=2)
        unpinned = self._make()

        assert self._ids({"not": {"requested_template_version": 3}}) == {other.id, unpinned.id}
        pinned_elsewhere = {
            "and": [
                {"has_requested_template_version": True},
                {"not": {"requested_template_version": 3}},
            ]
        }
        assert self._ids(pinned_elsewhere) == {other.id}
        assert self._ids({"not": pinned_elsewhere}) == {on_v.id, unpinned.id}

    def test_the_stranded_filter_runs(self):
        stranded = {
            self._make(PENDING, requested_template_version=2).id,
            self._make(FAILED, requested_template_version=3).id,
            self._make(FAILED).id,
            self._make(FAILED, send_after=None).id,
        }
        self._make(PENDING, requested_template_version=3)  # already on the fix
        self._make(PENDING)  # unpinned: renders the current version anyway
        self._make(PENDING, send_after=None, requested_template_version=2)  # due, not stranded
        self._make(SENT, requested_template_version=2)
        self._make(CANCELLED, requested_template_version=2)
        self._make(FAILED, body_template="other")
        filter_ = stranded_filter("welcome", 3)

        assert self._ids(filter_) == stranded
        assert self.backend.count_notifications(filter_) == len(stranded)
        listed = self.backend.filter_notification_versions(filter_, 1, 100)
        assert [v.id for v in listed] == sorted(stranded)

    def test_capabilities(self):
        assert self.backend.get_filter_capabilities() == {
            "fields.hasSendAfter": True,
            "negation.hasSendAfter": True,
            "fields.hasRequestedTemplateVersion": True,
            "negation.hasRequestedTemplateVersion": True,
        }
        assert self.backend.get_action_capabilities() == {
            "repin.expectedVersion": True,
            "requeue.expectedVersion": True,
            "requeue.templateVersion": True,
            "versions.filter": True,
        }


class ServiceTestCase(ConditionalMovesTestCase):
    def setUp(self):
        super().setUp()
        self.service = NotificationService(
            notification_adapters=[(FAKE_EMAIL_ADAPTER, FAKE_RENDERER)],
            notification_backend=self.backend,
        )

    def test_reports_every_action_and_the_presence_filters(self):
        assert self.service.get_backend_supported_action_capabilities() == {
            "repin.expectedVersion": True,
            "requeue.expectedVersion": True,
            "requeue.templateVersion": True,
            "versions.filter": True,
        }
        filter_capabilities = self.service.get_backend_supported_filter_capabilities()
        assert filter_capabilities["fields.hasSendAfter"] is True
        assert filter_capabilities["negation.hasRequestedTemplateVersion"] is True

    def test_requeue_pins_with_a_version_and_leaves_it_for_the_pending_sender(self):
        notification = self._make(FAILED, requested_template_version=1)
        token = self._token(notification.id)

        requeued = self.service.requeue_notification(
            notification.id, None, template_version=4, expected_version=token, send=False
        )

        assert requeued.status == PENDING.value
        assert requeued.requested_template_version == 4
        after = self._snapshot(notification.id)
        assert after["status"] == PENDING.value
        assert after["send_after"] is None
        assert str(after["notification_version"]) != token

    def test_requeue_with_a_stale_version_is_changed_and_writes_nothing(self):
        notification = self._make(FAILED)
        stale = self._token(notification.id)
        self.backend.store_git_commit_sha(notification.id, "b" * 40)
        before = self._snapshot(notification.id)

        for token in (stale, "not-a-token"):
            with self.subTest(token=token), pytest.raises(NotificationActionError) as raised:
                self.service.requeue_notification(
                    notification.id, template_version=4, expected_version=token, send=False
                )
            assert raised.value.reason == "CHANGED"
            assert raised.value.action == "requeue"

        assert self._snapshot(notification.id) == before

    def test_requeue_of_a_sent_notification_is_invalid_status(self):
        notification = self._make(SENT)
        before = self._snapshot(notification.id)

        with pytest.raises(NotificationActionError) as raised:
            self.service.requeue_notification(
                notification.id, expected_version=str(before["notification_version"])
            )

        assert raised.value.reason == "INVALID_STATUS"
        assert self._snapshot(notification.id) == before

    def test_repin_with_a_matching_version(self):
        notification = self._make(PENDING, requested_template_version=1)

        repinned = self.service.repin_notification(
            notification.id, 2, expected_version=self._token(notification.id)
        )

        assert repinned.requested_template_version == 2
        assert self._snapshot(notification.id)["requested_template_version"] == 2

    def test_repin_with_a_stale_version_is_changed_and_writes_nothing(self):
        notification = self._make(PENDING, requested_template_version=1)
        stale = self._token(notification.id)
        self.backend.persist_notification_update(notification.id, {"title": "edited"})
        before = self._snapshot(notification.id)

        with pytest.raises(NotificationActionError) as raised:
            self.service.repin_notification(notification.id, 2, expected_version=stale)

        assert raised.value.reason == "CHANGED"
        assert self._snapshot(notification.id) == before

    def test_repin_never_overwrites_a_notification_a_worker_sent_in_between(self):
        notification = self._make(PENDING, requested_template_version=1)
        read = self.backend.get_notification

        def read_then_worker_sends(notification_id, *args, **kwargs):
            # The service reads it while pending; a worker sends it before the backend writes.
            result = read(notification_id, *args, **kwargs)
            self.backend.mark_pending_as_sent(notification_id)
            return result

        with (
            mock.patch.object(
                self.backend, "get_notification", side_effect=read_then_worker_sends
            ) as patched,
            pytest.raises(NotificationActionError) as raised,
        ):
            self.service.repin_notification(notification.id, 2)

        assert patched.call_count == 1
        assert raised.value.reason == "INVALID_STATUS"
        after = self._snapshot(notification.id)
        assert after["status"] == SENT.value
        assert after["requested_template_version"] == 1

    def test_filter_notification_versions_pages_through_the_service(self):
        ids = sorted(self._make(FAILED).id for _ in range(3))

        first = self.service.filter_notification_versions({"status": FAILED}, 1, 2)
        second = self.service.filter_notification_versions({"status": FAILED}, 2, 2)

        assert [v.id for v in first + second] == ids
        assert all(v.version == self._token(v.id) for v in first + second)

    def test_moving_the_stranded_notifications(self):
        pending = self._make(PENDING, requested_template_version=2)
        failed = self._make(FAILED, requested_template_version=2)
        on_fix = self._make(PENDING, requested_template_version=3)
        filter_ = stranded_filter("welcome", 3)

        for listed in self.service.filter_notification_versions(filter_, 1, 100):
            if self._snapshot(listed.id)["status"] == FAILED.value:
                self.service.requeue_notification(
                    listed.id, template_version=3, expected_version=listed.version, send=False
                )
            else:
                self.service.repin_notification(listed.id, 3, expected_version=listed.version)

        assert self.service.filter_notification_versions(filter_, 1, 100) == []
        for notification in (pending, failed, on_fix):
            after = self._snapshot(notification.id)
            assert after["requested_template_version"] == 3
            assert after["status"] == PENDING.value


def _selected_columns(sql: str) -> list[str]:
    """The ``vintasend_django_notification`` columns a ``SELECT`` statement's select list names."""
    select_list = sql[len("SELECT ") : sql.index(" FROM ")]
    return re.findall(r'"vintasend_django_notification"\."(\w+)"', select_list)
