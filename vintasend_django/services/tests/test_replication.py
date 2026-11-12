"""A second ``DjangoDbNotificationBackend`` on a second database, as a replica.

The primary's version token travels to the replica, by snapshot apply or, when the replica
declines a snapshot, by the token-only stamp the service writes once the replayed row matches the
primary's, so versions listed from the replica are ones the primary accepts. Every refusal is
checked to write nothing, on either database.
"""

import contextlib
import dataclasses
import os
import shutil
import tempfile
import uuid
from datetime import timedelta
from typing import Any
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management.color import no_style
from django.db import connections
from django.db.models.signals import post_init
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

import pytest
from freezegun import freeze_time
from vintasend.constants import NotificationStatus, NotificationTypes
from vintasend.exceptions import (
    NotificationActionError,
    NotificationNotFoundError,
    NotificationUpdateError,
    NotificationVersionChangedError,
)
from vintasend.services.dataclasses import (
    ApplyResult,
    Notification,
    NotificationAttachment,
    OneOffNotification,
    StoredAttachment,
)
from vintasend.services.git_commit_sha_providers.stubs.fake_git_commit_sha_provider import (
    FakeGitCommitShaProvider,
)
from vintasend.services.notification_service import NotificationService, register_context

from vintasend_django.models import AttachmentFileRecord as AttachmentFileRecordModel
from vintasend_django.models import Notification as NotificationModel
from vintasend_django.models import NotificationAttachment as NotificationAttachmentModel
from vintasend_django.services.attachment_managers.django_storage import DjangoAttachmentManager
from vintasend_django.services.notification_backends.django_db_notification_backend import (
    DjangoDbNotificationBackend,
    reset_notification_id_sequence,
)
from vintasend_django.test_helpers import VintaSendDjangoTestCase


User = get_user_model()

PENDING = NotificationStatus.PENDING_SEND.value
SENT = NotificationStatus.SENT.value
READ = NotificationStatus.READ.value
FAILED = NotificationStatus.FAILED.value
CANCELLED = NotificationStatus.CANCELLED.value

BACKEND = (
    "vintasend_django.services.notification_backends.django_db_notification_backend."
    "DjangoDbNotificationBackend"
)
FAKE_EMAIL_ADAPTER = "vintasend.services.notification_adapters.stubs.fake_adapter.FakeEmailAdapter"
VERSIONED_RENDERER = (
    "vintasend.services.notification_template_renderers.stubs."
    "fake_templated_email_renderer.FakeVersionedTemplateRenderer"
)
CONTEXT_NAME = "django_replication_test_context"
register_context(CONTEXT_NAME)(lambda: {"greeting": "hi"})

# Every column of the row, the id, created, modified and the token included.
ROW_FIELDS = tuple(field.attname for field in NotificationModel._meta.concrete_fields)
# What a replayed write cannot carry over: each database stamps these with its own clock.
CLOCK_FIELDS = frozenset({"created", "modified", "sent_at", "read_at"})


def _row(alias: str, notification_id: Any) -> dict:
    return NotificationModel.objects.using(alias).values(*ROW_FIELDS).get(id=notification_id)


def _attachments(alias: str, notification_id: Any) -> list[tuple]:
    """The notification's join rows, as what they point at rather than their own ids."""
    return sorted(
        (
            row.file.checksum,
            row.file.size,
            row.file.filename,
            tuple(sorted(row.file.storage_identifiers.items())),
            row.is_inline,
            row.description or "",
        )
        for row in NotificationAttachmentModel.objects.using(alias)
        .filter(notification_id=notification_id)
        .select_related("file")
    )


class _ReplicaCase(VintaSendDjangoTestCase):
    databases = {"default", "replica"}  # noqa: RUF012 - Django's own class attribute

    def setUp(self):
        super().setUp()
        self._media_root = tempfile.mkdtemp()
        self._media_override = override_settings(MEDIA_ROOT=self._media_root)
        self._media_override.enable()
        # The replica's notifications point at the same user, so it exists there too.
        replica_user = User(pk=self.user.pk, username=self.user.get_username())
        replica_user.save(using="replica")
        self.primary = DjangoDbNotificationBackend(backend_identifier="primary")
        self.replica = DjangoDbNotificationBackend(database="replica", backend_identifier="replica")
        self.service = NotificationService(
            notification_adapters=[(FAKE_EMAIL_ADAPTER, VERSIONED_RENDERER)],
            notification_backend=self.primary,
            additional_backends=[self.replica],
            replication_mode="inline",
            git_commit_sha_provider=FakeGitCommitShaProvider(),
        )

    def tearDown(self):
        self._media_override.disable()
        shutil.rmtree(self._media_root, ignore_errors=True)
        super().tearDown()

    def _create(self, **kwargs) -> Notification:
        kwargs.setdefault("send_after", timezone.now() + timedelta(days=1))
        return self.service.create_notification(
            user_id=self.user.pk,
            notification_type=NotificationTypes.EMAIL.value,
            title="Replicated",
            body_template="welcome",
            context_name=CONTEXT_NAME,
            context_kwargs={},
            **kwargs,
        )

    def _create_one_off(self) -> OneOffNotification:
        return self.service.create_one_off_notification(
            email_or_phone="someone@example.com",
            first_name="Some",
            last_name="One",
            notification_type=NotificationTypes.EMAIL.value,
            title="Replicated",
            body_template="welcome",
            context_name=CONTEXT_NAME,
            context_kwargs={},
            send_after=timezone.now() + timedelta(days=1),
        )

    def _failed_send(self, notification_id: Any) -> None:
        adapter = next(iter(self.service.notification_adapters))
        with mock.patch.object(adapter, "send", side_effect=RuntimeError("smtp down")):
            self.service.send(self.primary.get_notification(notification_id))

    def _listed(self, backend_identifier: str | None = None) -> dict[str, str]:
        return {
            str(entry.id): entry.version
            for entry in self.service.filter_notification_versions({}, 1, 1000, backend_identifier)
        }

    def assert_on_primary_token(self, notification_id: Any, *, replayed: bool = False) -> str:
        """The replica lists the primary's ids and tokens, and holds the primary's row.

        A replayed write runs on each database's clock, so its timestamps are left out.
        """
        primary_listing = self._listed()
        assert self._listed("replica") == primary_listing
        token = primary_listing[str(notification_id)]
        assert self.replica.get_notification(notification_id).notification_version == token
        primary_row, replica_row = (
            _row("default", notification_id),
            _row("replica", notification_id),
        )
        if replayed:
            for field in CLOCK_FIELDS:
                primary_row.pop(field)
                replica_row.pop(field)
        assert replica_row == primary_row
        assert _attachments("replica", notification_id) == _attachments("default", notification_id)
        return token


class ReplicatedTokenTestCase(_ReplicaCase):
    def test_each_replicated_write_leaves_the_replica_on_the_primary_token(self):
        def cancel_then_requeue(notification_id: Any) -> None:
            self.service.cancel_notification(notification_id)
            self.service.requeue_notification(notification_id, send=False)

        writes = {
            "update": lambda id_: self.service.update_notification(id_, title="Changed"),
            # Marks sent, then stores the context, the git commit SHA and the template version.
            "send": lambda id_: self.service.send(self.primary.get_notification(id_)),
            "failed send": self._failed_send,
            "cancel": self.service.cancel_notification,
            "requeue": cancel_then_requeue,
            "repin": lambda id_: self.service.repin_notification(id_, template_version=4),
        }
        for name, write in writes.items():
            with self.subTest(write=name):
                created = self._create()
                after_create = self.assert_on_primary_token(created.id)

                write(created.id)

                after_write = self.assert_on_primary_token(created.id)
                assert after_write != after_create

        sent = NotificationModel.objects.using("replica").get(status=SENT)
        assert sent.used_template_version == 7
        assert sent.git_commit_sha is not None
        assert sent.context_used is not None

    def test_creates_and_read_marking_leave_the_replica_on_the_primary_token(self):
        one_off = self._create_one_off()
        sent = [self._create(send_after=None) for _ in range(3)]
        for notification in (one_off, *sent):
            self.assert_on_primary_token(notification.id)

        self.service.mark_read(sent[0].id)
        read = self.service.mark_read_bulk([sent[1].id, sent[2].id], user_id=self.user.pk)

        assert {n.id for n in read} == {sent[1].id, sent[2].id}
        for notification in sent:
            self.assert_on_primary_token(notification.id)
            assert self.replica.get_notification(notification.id).status == READ

    def test_attachments_are_linked_on_the_replica_without_copying_bytes(self):
        attachment = NotificationAttachment(
            file=b"report", filename="report.txt", description="The report"
        )
        uploads = []
        original_upload = DjangoAttachmentManager.upload_file

        def counting_upload(manager, *args, **kwargs):
            uploads.append(args[1])
            return original_upload(manager, *args, **kwargs)

        with mock.patch.object(DjangoAttachmentManager, "upload_file", counting_upload):
            created = self._create(attachments=[attachment])

        assert uploads == ["report.txt"]  # once, by the primary
        self.assert_on_primary_token(created.id)
        stored_files = os.listdir(os.path.join(self._media_root, "notifications", "attachments"))
        assert len(stored_files) == 1
        # The replica's handle opens the same stored file.
        (replica_attachment,) = self.replica.get_notification(created.id).attachments
        assert replica_attachment.file.read() == b"report"

    def test_versions_listed_from_the_replica_move_the_primary(self):
        created = self._create()
        self._failed_send(created.id)
        listed = self._listed("replica")[str(created.id)]

        requeued = self.service.requeue_notification(
            created.id, template_version=4, expected_version=listed, send=False
        )

        assert (requeued.status, requeued.requested_template_version) == (PENDING, 4)
        after_requeue = self.assert_on_primary_token(created.id)
        assert after_requeue != listed

        listed = self._listed("replica")[str(created.id)]
        repinned = self.service.repin_notification(
            created.id, template_version=5, expected_version=listed
        )

        assert repinned.requested_template_version == 5
        assert self.assert_on_primary_token(created.id) != after_requeue

    def test_a_replica_that_missed_a_write_gives_a_version_the_primary_refuses(self):
        created = self._create()
        self._failed_send(created.id)
        self.assert_on_primary_token(created.id)

        # The replica declines the snapshot once, and the replayed write fails too: it misses
        # the repin and keeps the previous token. (A declined snapshot alone does not do it: the
        # replayed row is stamped with the primary's token.)
        with (
            mock.patch.object(
                self.replica,
                "apply_replication_snapshot_if_newer",
                return_value=ApplyResult(applied=False, reason="declined for test"),
            ),
            mock.patch.object(
                self.replica,
                "persist_notification_update",
                side_effect=NotificationUpdateError("replica unavailable"),
            ),
        ):
            self.service.repin_notification(created.id, template_version=3)
        stale = self._listed("replica")[str(created.id)]
        assert stale != self._listed()[str(created.id)]
        before = (_row("default", created.id), _row("replica", created.id))

        moves = {
            "requeue": lambda: self.service.requeue_notification(
                created.id, template_version=4, expected_version=stale, send=False
            ),
            "repin": lambda: self.service.repin_notification(
                created.id, template_version=5, expected_version=stale
            ),
        }
        for action, move in moves.items():
            with self.subTest(action=action), pytest.raises(NotificationActionError) as raised:
                move()
            assert raised.value.reason == "CHANGED"
            assert raised.value.action == action

        assert (_row("default", created.id), _row("replica", created.id)) == before

    def test_reports_versions_filter_for_the_replica(self):
        capabilities = self.service.get_backend_supported_action_capabilities("replica")

        assert capabilities["versions.filter"] is True
        assert capabilities["writes.notificationVersion"] is True


class ReplayedTokenTestCase(_ReplicaCase):
    """The replica declines every snapshot, so the service replays each write on it.

    The rows are created while snapshots still apply: no replayed write creates a row with the
    primary's id. No replayed write carries a token; once the row matches the primary's, the
    service stamps it with the primary's token in one token-only write.
    """

    REPLAYED_WRITES = (
        "persist_notification_update",
        "mark_pending_as_sent",
        "mark_pending_as_failed",
        "mark_sent_as_read",
        "mark_sent_as_read_bulk",
        "cancel_notification",
        "requeue_notification",
        "repin_notification",
        "store_context_used",
        "store_git_commit_sha",
        "store_template_version",
    )

    def _declining(self):
        return mock.patch.object(
            self.replica,
            "apply_replication_snapshot_if_newer",
            return_value=ApplyResult(applied=False, reason="declined for test"),
        )

    def _spying(self, stack: contextlib.ExitStack) -> dict[str, mock.MagicMock]:
        return {
            name: stack.enter_context(
                mock.patch.object(self.replica, name, wraps=getattr(self.replica, name))
            )
            for name in self.REPLAYED_WRITES
        }

    @staticmethod
    def assert_only_the_stamp_carries_a_token(spies: dict[str, mock.MagicMock]) -> None:
        stamps = []
        for name, spy in spies.items():
            for call in spy.call_args_list:
                if "notification_version" in call.kwargs or "notification_versions" in call.kwargs:
                    stamps.append((name, call))
        assert stamps, "the replayed row was never stamped"
        for name, call in stamps:
            assert name == "persist_notification_update"
            assert call.args[1] == {}

    def test_each_replayed_write_leaves_the_replica_on_the_primary_token(self):
        def cancel_then_requeue(notification_id: Any) -> None:
            self.service.cancel_notification(notification_id)
            self.service.requeue_notification(notification_id, send=False)

        scheduled = timezone.now() + timedelta(days=1)
        # Each write, and the send_after its notification is created with (None: sent at once).
        writes = {
            "update": (scheduled, lambda id_: self.service.update_notification(id_, title="X")),
            # Marks sent, then stores the context used, the git commit SHA and the template
            # version: the stores land on a row the replica already holds as SENT.
            "send": (scheduled, lambda id_: self.service.send(self.primary.get_notification(id_))),
            "failed send": (scheduled, self._failed_send),
            "mark read": (None, self.service.mark_read),
            "cancel": (scheduled, self.service.cancel_notification),
            "requeue": (scheduled, cancel_then_requeue),
            "repin": (
                scheduled,
                lambda id_: self.service.repin_notification(id_, template_version=4),
            ),
        }
        for name, (send_after, write) in writes.items():
            with self.subTest(write=name):
                created = self._create(send_after=send_after)
                after_create = self.assert_on_primary_token(created.id)

                with contextlib.ExitStack() as stack:
                    stack.enter_context(self._declining())
                    spies = self._spying(stack)
                    write(created.id)

                after_write = self.assert_on_primary_token(created.id, replayed=True)
                assert after_write != after_create
                self.assert_only_the_stamp_carries_a_token(spies)

        sent = NotificationModel.objects.using("replica").get(status=SENT)
        assert sent.context_used is not None
        assert sent.git_commit_sha is not None
        assert sent.used_template_version == 7
        assert NotificationModel.objects.using("replica").filter(status=READ).count() == 1

    def test_replayed_bulk_read_marking_takes_the_primary_tokens(self):
        sent = [self._create(send_after=None) for _ in range(2)]

        with contextlib.ExitStack() as stack:
            stack.enter_context(self._declining())
            spies = self._spying(stack)
            self.service.mark_read_bulk([n.id for n in sent], user_id=self.user.pk)

        # Stamped row by row, once each row is read.
        self.assert_only_the_stamp_carries_a_token(spies)
        stamped = [call.args[0] for call in spies["persist_notification_update"].call_args_list]
        assert sorted(stamped) == sorted(n.id for n in sent)
        for notification in sent:
            self.assert_on_primary_token(notification.id, replayed=True)
            assert self.replica.get_notification(notification.id).status == READ

    def test_a_partial_replay_leaves_the_replica_on_its_own_token(self):
        """The replayed update lands, the stamp does not: the replica lists a token of its own,
        which the primary refuses, rather than the primary's on a row that may differ."""
        created = self._create()
        before = self.assert_on_primary_token(created.id)
        replica_update = self.replica.persist_notification_update

        def failing_stamp(notification_id, update_data, **kwargs):
            if not update_data:
                raise NotificationUpdateError("stamp lost")
            return replica_update(notification_id, update_data, **kwargs)

        with (
            self._declining(),
            mock.patch.object(
                self.replica, "persist_notification_update", side_effect=failing_stamp
            ),
        ):
            self.service.update_notification(created.id, title="Changed")

        assert self.replica.get_notification(created.id).title == "Changed"
        own = self._listed("replica")[str(created.id)]
        assert own not in (before, self._listed()[str(created.id)])
        before_move = (_row("default", created.id), _row("replica", created.id))
        with pytest.raises(NotificationActionError) as raised:
            self.service.repin_notification(created.id, template_version=5, expected_version=own)
        assert raised.value.reason == "CHANGED"
        assert (_row("default", created.id), _row("replica", created.id)) == before_move


class OutOfOrderReplicationTestCase(_ReplicaCase):
    def test_an_older_snapshot_arriving_last_leaves_the_replica_on_the_later_write(self):
        """Two primary writes replicate inline, the earlier one's snapshot reaching the replica
        after the later one's: the replica declines it as superseded, the service replays
        nothing, and the replica keeps the later state and token."""
        start = timezone.now().replace(microsecond=0)
        with freeze_time(start) as frozen:
            created = self._create()
            original_apply = self.replica.apply_replication_snapshot_if_newer
            results: list[ApplyResult] = []
            interleaved = False

            def apply_after_the_second_write(snapshot):
                nonlocal interleaved
                if not interleaved:
                    interleaved = True
                    frozen.tick(timedelta(minutes=1))
                    self.service.update_notification(created.id, title="Second")
                results.append(original_apply(snapshot))
                return results[-1]

            frozen.tick(timedelta(minutes=1))
            with (
                mock.patch.object(
                    self.replica,
                    "apply_replication_snapshot_if_newer",
                    side_effect=apply_after_the_second_write,
                ),
                mock.patch.object(
                    self.replica,
                    "persist_notification_update",
                    wraps=self.replica.persist_notification_update,
                ) as replayed,
            ):
                self.service.update_notification(created.id, title="First")

        second, first = results
        assert (second.applied, second.superseded) == (True, False)
        assert (first.applied, first.superseded) == (False, True)
        replayed.assert_not_called()
        assert self.primary.get_notification(created.id).title == "Second"
        self.assert_on_primary_token(created.id)


class ApplySnapshotTestCase(_ReplicaCase):
    """``apply_replication_snapshot_if_newer`` called directly on the replica."""

    def _primary_notification(self, **kwargs) -> Notification:
        return self.primary.persist_notification(
            user_id=self.user.pk,
            notification_type=NotificationTypes.EMAIL.value,
            title="Snapshot",
            body_template="welcome",
            context_name=CONTEXT_NAME,
            context_kwargs={"order": 1},
            send_after=timezone.now() + timedelta(days=1),
            **kwargs,
        )

    def _replica_count(self) -> tuple[int, int, int]:
        return (
            NotificationModel.objects.using("replica").count(),
            NotificationAttachmentModel.objects.using("replica").count(),
            AttachmentFileRecordModel.objects.using("replica").count(),
        )

    def test_insert_keeps_the_id_created_modified_and_token(self):
        created = self._primary_notification(tenant="acme", requested_template_version=2)
        self.primary.store_context_used(created.id, {"greeting": "hi"}, "some.Adapter")
        self.primary.store_git_commit_sha(created.id, "a" * 40)
        self.primary.mark_pending_as_sent(created.id)
        self.primary.store_template_version(created.id, 2)
        snapshot = self.primary.get_notification(created.id)

        result = self.replica.apply_replication_snapshot_if_newer(snapshot)

        assert result.applied is True
        assert _row("replica", created.id) == _row("default", created.id)
        assert self.replica.get_notification(created.id) == snapshot

    def test_insert_of_a_one_off_notification(self):
        created = self.primary.persist_one_off_notification(
            email_or_phone="someone@example.com",
            first_name="Some",
            last_name="One",
            notification_type=NotificationTypes.EMAIL.value,
            title="Snapshot",
            body_template="welcome",
            context_name=CONTEXT_NAME,
            context_kwargs={},
        )
        snapshot = self.primary.get_notification(created.id)

        assert self.replica.apply_replication_snapshot_if_newer(snapshot).applied is True
        assert _row("replica", created.id) == _row("default", created.id)

    def test_two_primary_writes_applied_out_of_order(self):
        """The later write's snapshot wins; the earlier one, arriving after it, is declined."""
        start = timezone.now().replace(microsecond=0)
        with freeze_time(start):
            created = self._primary_notification()
        with freeze_time(start + timedelta(minutes=1)):
            self.primary.persist_notification_update(created.id, {"title": "First"})
        first = self.primary.get_notification(created.id)
        with freeze_time(start + timedelta(minutes=2)):
            self.primary.persist_notification_update(created.id, {"title": "Second"})
        second = self.primary.get_notification(created.id)
        assert first.modified is not None and second.modified is not None
        assert first.modified < second.modified

        assert self.replica.apply_replication_snapshot_if_newer(second).applied is True
        before = _row("replica", created.id)
        result = self.replica.apply_replication_snapshot_if_newer(first)

        assert (result.applied, result.superseded) == (False, True)
        assert _row("replica", created.id) == before == _row("default", created.id)
        assert self.replica.get_notification(created.id).notification_version == (
            second.notification_version
        )

    def test_an_older_snapshot_is_declined_and_writes_nothing(self):
        snapshot = self.primary.get_notification(self._primary_notification().id)
        self.replica.apply_replication_snapshot_if_newer(snapshot)
        before = _row("replica", snapshot.id)
        assert snapshot.modified is not None
        older = dataclasses.replace(
            snapshot,
            title="Older",
            modified=snapshot.modified - timedelta(seconds=1),
            notification_version=str(uuid.uuid4()),
        )

        result = self.replica.apply_replication_snapshot_if_newer(older)

        assert (result.applied, result.superseded) == (False, True)
        assert str(snapshot.id) in (result.reason or "")
        assert _row("replica", snapshot.id) == before

    def test_an_equally_modified_snapshot_with_a_new_token_applies(self):
        snapshot = self.primary.get_notification(self._primary_notification().id)
        self.replica.apply_replication_snapshot_if_newer(snapshot)
        token = str(uuid.uuid4())

        result = self.replica.apply_replication_snapshot_if_newer(
            dataclasses.replace(snapshot, title="Same time", notification_version=token)
        )

        assert result.applied is True
        row = _row("replica", snapshot.id)
        assert (row["title"], str(row["notification_version"]), row["modified"]) == (
            "Same time",
            token,
            snapshot.modified,
        )

    def test_a_snapshot_without_a_usable_token_gets_a_fresh_one(self):
        snapshot = self.primary.get_notification(self._primary_notification().id)
        self.replica.apply_replication_snapshot_if_newer(snapshot)
        for token in (None, "not-a-token"):
            with self.subTest(token=token):
                before = str(_row("replica", snapshot.id)["notification_version"])

                result = self.replica.apply_replication_snapshot_if_newer(
                    dataclasses.replace(snapshot, notification_version=token)
                )

                assert result.applied is True
                stored = _row("replica", snapshot.id)["notification_version"]
                assert stored.version == 7
                assert str(stored) not in (before, snapshot.notification_version)

    def test_an_id_this_backend_cannot_hold_is_declined_and_writes_nothing(self):
        snapshot = self.primary.get_notification(self._primary_notification().id)
        for bad_id in (str(uuid.uuid4()), uuid.uuid4(), f"00{snapshot.id}", "not-an-id", 2**70):
            with self.subTest(bad_id=bad_id):
                result = self.replica.apply_replication_snapshot_if_newer(
                    dataclasses.replace(snapshot, id=bad_id)
                )

                assert (result.applied, result.superseded) == (False, False)
                assert str(bad_id) in (result.reason or "")
                assert self._replica_count() == (0, 0, 0)

    def test_the_id_range_is_this_databases(self):
        snapshot = self.primary.get_notification(self._primary_notification().id)
        tight = (1, int(snapshot.id) - 1)

        with mock.patch.object(
            connections["default"].ops, "integer_field_range", return_value=tight
        ):
            assert self.replica.apply_replication_snapshot_if_newer(snapshot).applied is True

        NotificationModel.objects.using("replica").all().delete()
        with mock.patch.object(
            connections["replica"].ops, "integer_field_range", return_value=tight
        ):
            result = self.replica.apply_replication_snapshot_if_newer(snapshot)

        assert (result.applied, result.superseded) == (False, False)
        assert self._replica_count() == (0, 0, 0)

    def test_an_insert_resets_the_id_sequence_on_this_database(self):
        snapshot = self.primary.get_notification(self._primary_notification().id)
        # SQLite needs no reset: its ids follow the highest one stored.
        assert connections["replica"].ops.sequence_reset_sql(no_style(), [NotificationModel]) == []

        with (
            CaptureQueriesContext(connections["default"]) as on_default,
            CaptureQueriesContext(connections["replica"]) as on_replica,
            mock.patch.object(
                connections["replica"].ops, "sequence_reset_sql", return_value=["SELECT 4242"]
            ) as reset_sql,
        ):
            assert self.replica.apply_replication_snapshot_if_newer(snapshot).applied is True
            # An update of a row it holds inserts nothing, so it resets nothing.
            assert self.replica.apply_replication_snapshot_if_newer(snapshot).applied is True

        reset_sql.assert_called_once()
        assert reset_sql.call_args.args[1] == [NotificationModel]
        assert [q["sql"] for q in on_replica.captured_queries].count("SELECT 4242") == 1
        assert len(on_default.captured_queries) == 0

    def test_a_promoted_replica_inserts_after_the_ids_it_received(self):
        snapshot = self.primary.get_notification(self._primary_notification().id)
        self.replica.apply_replication_snapshot_if_newer(snapshot)
        reset_notification_id_sequence("replica")

        created = self.replica.persist_notification(
            user_id=self.user.pk,
            notification_type=NotificationTypes.EMAIL.value,
            title="After promotion",
            body_template="welcome",
            context_name=CONTEXT_NAME,
            context_kwargs={},
            send_after=None,
        )

        assert int(created.id) > int(snapshot.id)

    def test_a_user_hidden_by_the_default_manager_still_exists(self):
        snapshot = self.primary.get_notification(self._primary_notification().id)
        default_manager = type(User._default_manager)
        assert type(User._base_manager) is not default_manager

        with mock.patch.object(
            default_manager, "get_queryset", lambda manager: User._base_manager.none()
        ):
            assert self.replica.apply_replication_snapshot_if_newer(snapshot).applied is True

    def test_a_user_missing_from_the_replica_is_declined_and_writes_nothing(self):
        stranger = self.create_user()  # on the primary's database only
        snapshot = self.primary.get_notification(
            self.primary.persist_notification(
                user_id=stranger.pk,
                notification_type=NotificationTypes.EMAIL.value,
                title="Snapshot",
                body_template="welcome",
                context_name=CONTEXT_NAME,
                context_kwargs={},
                send_after=None,
            ).id
        )

        result = self.replica.apply_replication_snapshot_if_newer(snapshot)

        assert (result.applied, result.superseded) == (False, False)
        assert str(snapshot.id) in (result.reason or "")
        assert self._replica_count() == (0, 0, 0)

    def test_attachments_are_reconciled_without_touching_bytes(self):
        first = self._primary_notification(
            attachments=[
                NotificationAttachment(file=b"one", filename="one.txt"),
                NotificationAttachment(file=b"two", filename="two.txt", is_inline=True),
            ]
        )
        other = self._primary_notification(
            attachments=[NotificationAttachment(file=b"three", filename="three.txt")]
        )
        snapshot = self.primary.get_notification(first.id)
        one, two = sorted(snapshot.attachments, key=lambda a: a.filename)
        (three,) = self.primary.get_notification(other.id).attachments
        byte_methods = ("file_to_bytes", "upload_file", "delete_file_by_identifiers")
        patches = [
            mock.patch.object(DjangoAttachmentManager, name, side_effect=AssertionError(name))
            for name in byte_methods
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

        assert self.replica.apply_replication_snapshot_if_newer(snapshot).applied is True
        assert _attachments("replica", first.id) == _attachments("default", first.id)

        token = str(uuid.uuid4())
        changed = dataclasses.replace(
            snapshot,
            attachments=[two, dataclasses.replace(three, description="Added")],
            notification_version=token,
        )
        assert self.replica.apply_replication_snapshot_if_newer(changed).applied is True

        linked = _attachments("replica", first.id)
        assert sorted((f[2], f[4], f[5]) for f in linked) == [
            ("three.txt", False, "Added"),
            ("two.txt", True, ""),
        ]
        # Linking does not move the token: the row holds the snapshot's.
        assert str(_row("replica", first.id)["notification_version"]) == token
        # The file record for "one" stays: only its join row went.
        assert AttachmentFileRecordModel.objects.using("replica").count() == 3
        assert one.storage_identifiers in [
            r.storage_identifiers for r in AttachmentFileRecordModel.objects.using("replica")
        ]

    def test_an_attachment_with_nothing_to_link_is_declined_and_writes_nothing(self):
        snapshot = self.primary.get_notification(self._primary_notification().id)
        orphan = StoredAttachment(
            id="1",
            filename="lost.txt",
            content_type="text/plain",
            size=4,
            checksum="f" * 64,
            created_at=timezone.now(),
            file=mock.Mock(),
        )

        result = self.replica.apply_replication_snapshot_if_newer(
            dataclasses.replace(snapshot, attachments=[orphan])
        )

        assert (result.applied, result.superseded) == (False, False)
        assert self._replica_count() == (0, 0, 0)

    def test_a_checksumless_file_is_matched_by_its_storage_identifiers_in_sql(self):
        snapshot = self.primary.get_notification(self._primary_notification().id)
        identifiers = {"name": "notifications/attachments/a.txt", "id": "a"}
        records = AttachmentFileRecordModel.objects.using("replica")
        same = records.create(
            filename="a.txt",
            size=3,
            checksum="",
            # Another key order: the same identifiers.
            storage_identifiers=dict(reversed(list(identifiers.items()))),
        )
        for other in (
            {"name": "notifications/attachments/b.txt", "id": "b"},
            {**identifiers, "extra": "x"},
        ):
            records.create(filename="a.txt", size=3, checksum="", storage_identifiers=other)
        attachment = StoredAttachment(
            id="1",
            filename="a.txt",
            content_type="text/plain",
            size=3,
            checksum="",
            created_at=timezone.now(),
            file=mock.Mock(),
            storage_identifiers=identifiers,
        )

        loaded: list[object] = []

        def count_loaded(sender, instance, **kwargs):
            loaded.append(instance.pk)

        # Counted through post_init, which every Django fires for a loaded row. Wrapping
        # from_db instead breaks on Django 6.1, which inspects it.
        post_init.connect(count_loaded, sender=AttachmentFileRecordModel)
        try:
            resolved = self.replica._snapshot_file_records([attachment])
        finally:
            post_init.disconnect(count_loaded, sender=AttachmentFileRecordModel)

        assert resolved == [same]
        # Narrowed in SQL: the record with other identifiers is never loaded. The one with an
        # extra key matches every key asked for, and is ruled out on the exact comparison.
        assert len(loaded) == 2

        assert (
            self.replica.apply_replication_snapshot_if_newer(
                dataclasses.replace(snapshot, attachments=[attachment])
            ).applied
            is True
        )
        assert records.count() == 3

    def test_a_failure_while_linking_writes_nothing(self):
        snapshot = self.primary.get_notification(
            self._primary_notification(
                attachments=[NotificationAttachment(file=b"one", filename="one.txt")]
            ).id
        )

        with (
            # The first write after the row's INSERT: the file record the attachment needs.
            mock.patch("django.db.models.query.QuerySet.create", side_effect=RuntimeError("full")),
            pytest.raises(RuntimeError),
        ):
            self.replica.apply_replication_snapshot_if_newer(snapshot)

        assert self._replica_count() == (0, 0, 0)


class DatabaseOptionTestCase(_ReplicaCase):
    def test_backend_kwargs_carry_the_options_only_when_given(self):
        assert DjangoDbNotificationBackend().backend_kwargs == {}
        assert DjangoDbNotificationBackend().get_backend_identifier() is None
        backend = DjangoDbNotificationBackend(database="replica", backend_identifier="eu")
        assert backend.backend_kwargs == {"database": "replica", "backend_identifier": "eu"}
        assert backend.get_backend_identifier() == "eu"

    def test_built_from_an_import_string(self):
        service = NotificationService(
            notification_adapters=[(FAKE_EMAIL_ADAPTER, VERSIONED_RENDERER)],
            notification_backend=BACKEND,
            notification_backend_kwargs={"database": "replica", "backend_identifier": "eu"},
        )

        backend = service.notification_backend
        assert isinstance(backend, DjangoDbNotificationBackend)
        assert (backend.database, backend.get_backend_identifier()) == ("replica", "eu")
        assert service.has_backend("eu")

    def test_nothing_touches_the_default_database(self):
        replica = self.replica
        snapshot = self.primary.get_notification(
            self.primary.persist_notification(
                user_id=self.user.pk,
                notification_type=NotificationTypes.EMAIL.value,
                title="From the primary",
                body_template="welcome",
                context_name=CONTEXT_NAME,
                context_kwargs={},
                send_after=None,
            ).id
        )

        with self.assertNumQueries(0, using="default"):
            replica.apply_replication_snapshot_if_newer(snapshot)
            created = replica.persist_notification(
                user_id=self.user.pk,
                notification_type=NotificationTypes.EMAIL.value,
                title="On the replica",
                body_template="welcome",
                context_name=CONTEXT_NAME,
                context_kwargs={},
                send_after=None,
                attachments=[NotificationAttachment(file=b"one", filename="one.txt")],
            )
            one_off = replica.persist_one_off_notification(
                email_or_phone="someone@example.com",
                first_name="Some",
                last_name="One",
                notification_type=NotificationTypes.EMAIL.value,
                title="On the replica",
                body_template="welcome",
                context_name=CONTEXT_NAME,
                context_kwargs={},
            )
            replica.persist_notification_update(created.id, {"title": "Changed"})
            replica.store_context_used(created.id, {"greeting": "hi"}, "some.Adapter")
            replica.store_git_commit_sha(created.id, "a" * 40)
            replica.store_template_version(created.id, 3)
            replica.mark_pending_as_sent(created.id)
            replica.get_user_email_from_notification(created.id)
            replica.mark_sent_as_read(created.id)
            replica.mark_sent_as_read_bulk([created.id], user_id=self.user.pk)
            replica.mark_pending_as_failed(one_off.id)
            replica.requeue_notification(one_off.id, None)
            replica.repin_notification(one_off.id, 4)
            replica.cancel_notification(one_off.id)
            list(replica.get_all_pending_notifications())
            list(replica.get_pending_notifications(1, 10))
            list(replica.get_all_future_notifications())
            list(replica.filter_in_app_notifications(self.user.pk))
            replica.count_in_app_unread_notifications(self.user.pk)
            list(replica.filter_notifications({}, 1, 10))
            replica.count_notifications({})
            replica.filter_notification_versions({}, 1, 10)
            replica.get_notification(created.id, for_update=True)
            (attachment,) = replica.get_notification(created.id).attachments
            assert replica.find_attachment_file_by_checksum(attachment.checksum, attachment.size)
            replica.get_attachment_file_record(attachment.file_id)
            replica.delete_notification_attachment(attachment.id)
            (orphan,) = replica.get_orphaned_attachment_files()
            replica.delete_attachment_file(orphan.id)

        assert NotificationModel.objects.using("default").count() == 1
        assert NotificationModel.objects.using("replica").count() == 3

    def test_listing_pending_notifications_reads_no_user(self):
        for _ in range(2):
            self.replica.persist_notification(
                user_id=self.user.pk,
                notification_type=NotificationTypes.EMAIL.value,
                title="Pending",
                body_template="welcome",
                context_name=CONTEXT_NAME,
                context_kwargs={},
                send_after=None,
            )

        # The rows, then each row's attachments: no query for its user.
        with self.assertNumQueries(3, using="replica"):
            pending = list(self.replica.get_all_pending_notifications())

        assert [type(n) for n in pending] == [Notification, Notification]


class GivenTokenTestCase(VintaSendDjangoTestCase):
    """``writes.notificationVersion``: every write stores the token it is handed.

    Without one, or with one that is not a UUID, it mints a fresh UUIDv7 as before.
    """

    GIVEN = (None, "0190f9d8-0f3a-7cde-8f00-1234567890ab", "not-a-token")

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

    def _make(self, status: str = PENDING, **kwargs) -> Any:
        notification = self.backend.persist_notification(
            user_id=self.user.pk,
            notification_type=NotificationTypes.EMAIL.value,
            title="Given",
            body_template="welcome",
            context_name=CONTEXT_NAME,
            context_kwargs={},
            send_after=None,
            **kwargs,
        )
        if status != PENDING:
            NotificationModel.objects.filter(id=notification.id).update(status=status)
        return notification.id

    @staticmethod
    def _token(notification_id: Any) -> str:
        return str(NotificationModel.objects.get(id=notification_id).notification_version)

    @staticmethod
    def _kwargs(given: str | None) -> dict[str, str]:
        return {} if given is None else {"notification_version": given}

    def assert_holds(self, given: str | None, before: str | None, after: str) -> None:
        if given is not None and given != "not-a-token":
            assert after == given
        else:
            assert after not in (before, given)
            assert uuid.UUID(after).version == 7

    def test_creates(self):
        creates = {
            "persist_notification": lambda **kw: self.backend.persist_notification(
                user_id=self.user.pk,
                notification_type=NotificationTypes.EMAIL.value,
                title="Given",
                body_template="welcome",
                context_name=CONTEXT_NAME,
                context_kwargs={},
                send_after=None,
                **kw,
            ),
            "persist_notification with attachments": lambda **kw: self.backend.persist_notification(
                user_id=self.user.pk,
                notification_type=NotificationTypes.EMAIL.value,
                title="Given",
                body_template="welcome",
                context_name=CONTEXT_NAME,
                context_kwargs={},
                send_after=None,
                attachments=[NotificationAttachment(file=b"one", filename="one.txt")],
                **kw,
            ),
            "persist_one_off_notification": lambda **kw: self.backend.persist_one_off_notification(
                email_or_phone="someone@example.com",
                first_name="Some",
                last_name="One",
                notification_type=NotificationTypes.EMAIL.value,
                title="Given",
                body_template="welcome",
                context_name=CONTEXT_NAME,
                context_kwargs={},
                **kw,
            ),
        }
        for name, create in creates.items():
            for given in self.GIVEN:
                with self.subTest(create=name, given=given):
                    created = create(**self._kwargs(given))

                    self.assert_holds(given, None, self._token(created.id))
                    assert created.notification_version == self._token(created.id)
                    # Linking attachments moves modified too; the returned row reflects it.
                    assert created.modified == NotificationModel.objects.get(id=created.id).modified

    def _writes(self) -> dict[str, tuple[Any, Any]]:
        """Every single-notification write: how to make its notification, and the write."""

        def with_attachment() -> Any:
            return self._make(attachments=[NotificationAttachment(file=b"a", filename="a.txt")])

        def delete_attachment(notification_id: Any, **kw: str) -> None:
            (attachment,) = self.backend.get_attachments(notification_id)
            self.backend.delete_notification_attachment(attachment.id, **kw)

        return {
            "persist_notification_update": (
                self._make,
                lambda id_, **kw: self.backend.persist_notification_update(
                    id_, {"title": "Changed"}, **kw
                ),
            ),
            "mark_pending_as_sent": (self._make, self.backend.mark_pending_as_sent),
            "mark_pending_as_failed": (self._make, self.backend.mark_pending_as_failed),
            "mark_sent_as_read": (lambda: self._make(SENT), self.backend.mark_sent_as_read),
            "cancel_notification": (self._make, self.backend.cancel_notification),
            "store_context_used": (
                self._make,
                lambda id_, **kw: self.backend.store_context_used(id_, {"a": 1}, "x.Y", **kw),
            ),
            "store_git_commit_sha": (
                self._make,
                lambda id_, **kw: self.backend.store_git_commit_sha(id_, "a" * 40, **kw),
            ),
            "store_template_version": (
                self._make,
                lambda id_, **kw: self.backend.store_template_version(id_, 3, **kw),
            ),
            "requeue_notification": (
                lambda: self._make(FAILED),
                lambda id_, **kw: self.backend.requeue_notification(id_, None, **kw),
            ),
            "requeue_notification with expected_version": (
                lambda: self._make(FAILED),
                lambda id_, **kw: self.backend.requeue_notification(
                    id_, template_version=2, expected_version=self._token(id_), **kw
                ),
            ),
            "repin_notification": (
                self._make,
                lambda id_, **kw: self.backend.repin_notification(id_, 3, **kw),
            ),
            "repin_notification with expected_version": (
                self._make,
                lambda id_, **kw: self.backend.repin_notification(
                    id_, 3, expected_version=self._token(id_), **kw
                ),
            ),
            "delete_notification_attachment": (with_attachment, delete_attachment),
        }

    def test_writes(self):
        for name, (make, write) in self._writes().items():
            for given in self.GIVEN:
                with self.subTest(write=name, given=given):
                    notification_id = make()
                    before = self._token(notification_id)

                    write(notification_id, **self._kwargs(given))

                    self.assert_holds(given, before, self._token(notification_id))

    def test_every_write_moves_modified(self):
        """``QuerySet.update()`` skips the auto-modified field, so each write sets it itself."""
        start = timezone.now().replace(microsecond=0)
        later = start + timedelta(hours=1)
        writes = {
            **self._writes(),
            "persist_notification with attachments": (
                lambda: self._make(),
                lambda id_, **kw: self.backend._store_attachments_and_move_version(
                    [NotificationAttachment(file=b"b", filename="b.txt")], id_, **kw
                ),
            ),
            "repin_notification already on the version, given a token": (
                lambda: self._make(requested_template_version=3),
                lambda id_, **kw: self.backend.repin_notification(
                    id_, 3, notification_version=self.GIVEN[1]
                ),
            ),
            "mark_sent_as_read_bulk": (
                lambda: self._make(SENT),
                lambda id_, **kw: list(self.backend.mark_sent_as_read_bulk([id_])),
            ),
            "mark_sent_as_read_bulk with tokens": (
                lambda: self._make(SENT),
                lambda id_, **kw: list(
                    self.backend.mark_sent_as_read_bulk(
                        [id_], notification_versions={id_: self.GIVEN[1]}
                    )
                ),
            ),
        }
        for name, (make, write) in writes.items():
            for given in (None, self.GIVEN[1]):
                with self.subTest(write=name, given=given):
                    with freeze_time(start):
                        notification_id = make()
                    assert NotificationModel.objects.get(id=notification_id).modified == start

                    with freeze_time(later):
                        write(notification_id, **self._kwargs(given))

                    assert NotificationModel.objects.get(id=notification_id).modified == later

    def test_an_update_with_nothing_else_still_writes_the_given_token(self):
        notification_id = self._make()
        given = self.GIVEN[1]

        self.backend.persist_notification_update(notification_id, {}, notification_version=given)

        assert self._token(notification_id) == given

    def test_the_token_only_stamp_writes_the_token_alone_in_any_status(self):
        """How the service stamps a converged replica row: in any status, delivered included,
        and nothing but the token changes, ``modified`` included."""
        start = timezone.now().replace(microsecond=0)
        for status in (PENDING, SENT, READ, FAILED, CANCELLED):
            with self.subTest(status=status):
                with freeze_time(start):
                    notification_id = self._make(status)
                before = NotificationModel.objects.values().get(id=notification_id)

                with freeze_time(start + timedelta(hours=1)):
                    stamped = self.backend.persist_notification_update(
                        notification_id, {}, notification_version=self.GIVEN[1]
                    )

                after = NotificationModel.objects.values().get(id=notification_id)
                assert str(after.pop("notification_version")) == self.GIVEN[1]
                before.pop("notification_version")
                assert after == before
                assert stamped.notification_version == self.GIVEN[1]
                assert stamped.status == status

    def test_a_delivered_notification_still_refuses_a_content_update(self):
        for status in (SENT, READ):
            for given in (None, self.GIVEN[1]):
                with self.subTest(status=status, given=given):
                    notification_id = self._make(status)
                    before = NotificationModel.objects.values().get(id=notification_id)

                    with pytest.raises(NotificationUpdateError):
                        self.backend.persist_notification_update(
                            notification_id, {"title": "Changed"}, **self._kwargs(given)
                        )

                    assert NotificationModel.objects.values().get(id=notification_id) == before

    def test_the_token_only_stamp_of_a_missing_notification(self):
        with pytest.raises(NotificationNotFoundError):
            self.backend.persist_notification_update(987654, {}, notification_version=self.GIVEN[1])

    def test_a_repin_already_on_the_version_takes_the_given_token(self):
        notification_id = self._make(requested_template_version=3)
        before = self._token(notification_id)

        self.backend.repin_notification(notification_id, 3)
        assert self._token(notification_id) == before  # nothing to write without one

        repinned = self.backend.repin_notification(
            notification_id, 3, notification_version=self.GIVEN[1]
        )
        assert self._token(notification_id) == self.GIVEN[1]
        assert repinned.notification_version == self.GIVEN[1]

    def test_a_stale_expected_version_is_refused_even_with_a_given_token(self):
        notification_id = self._make(FAILED)
        stale = self._token(notification_id)
        self.backend.store_git_commit_sha(notification_id, "b" * 40)
        before = NotificationModel.objects.values().get(id=notification_id)

        for name, move in {
            "requeue": lambda: self.backend.requeue_notification(
                notification_id, expected_version=stale, notification_version=self.GIVEN[1]
            ),
            "repin": lambda: self.backend.repin_notification(
                notification_id, 4, expected_version=stale, notification_version=self.GIVEN[1]
            ),
        }.items():
            with self.subTest(move=name), pytest.raises(NotificationVersionChangedError):
                move()

        assert NotificationModel.objects.values().get(id=notification_id) == before

    def test_bulk_read_marking_takes_a_token_per_row(self):
        sent = [self._make(SENT) for _ in range(3)]
        already_read = self._make(READ)
        unmapped_before = self._token(sent[2])
        given = {
            # Keys are compared as strings, whatever their type.
            sent[0]: "0190f9d8-0f3a-7cde-8f00-000000000001",
            str(sent[1]): "0190f9d8-0f3a-7cde-8f00-000000000002",
            already_read: "0190f9d8-0f3a-7cde-8f00-000000000003",
            # Not requested: ignored.
            self._make(SENT): "0190f9d8-0f3a-7cde-8f00-000000000004",
        }

        read = list(
            self.backend.mark_sent_as_read_bulk(
                [*sent, already_read], user_id=self.user.pk, notification_versions=given
            )
        )

        assert {n.id for n in read} == {*sent, already_read}
        assert self._token(sent[0]) == given[sent[0]]
        assert self._token(sent[1]) == given[str(sent[1])]
        assert self._token(already_read) == given[already_read]
        assert self._token(sent[2]) != unmapped_before
        assert self._token(sent[2]) not in given.values()
        statuses = NotificationModel.objects.filter(id__in=[*sent, already_read])
        assert set(statuses.values_list("status", flat=True)) == {READ}
        # read_at is set on the rows this marked, and left alone on the one already READ.
        assert all(n.read_at is not None for n in statuses if n.id in sent)
        assert statuses.get(id=already_read).read_at is None
        assert {n.id: n.notification_version for n in read} == {
            n.id: str(n.notification_version) for n in statuses
        }

    def test_bulk_read_marking_without_tokens(self):
        sent = [self._make(SENT) for _ in range(2)]
        before = {i: self._token(i) for i in sent}

        self.backend.mark_sent_as_read_bulk(sent)

        for notification_id in sent:
            self.assert_holds(None, before[notification_id], self._token(notification_id))
