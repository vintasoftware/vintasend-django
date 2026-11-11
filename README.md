# VintaSend Django

Django implementations for VintaSend.

`DjangoDbNotificationBackend` implements the full notification backend, including the in-app
listing API: `filter_in_app_unread_notifications` / `filter_all_in_app_unread_notifications`
(unread), `filter_in_app_notifications` / `filter_all_in_app_notifications` (read + unread),
the `count_in_app_notifications` / `count_in_app_unread_notifications` counters, and the
idempotent, optionally user-scoped `mark_sent_as_read_bulk`. See the `vintasend` README for the
service-level in-app API (pagination, counts and bulk mark-as-read).

## Backend options

`DjangoDbNotificationBackend` takes two keyword-only options:

* `database` (default `"default"`): the Django database alias the backend runs on. Every query,
  transaction and save it makes uses that alias, so two backends on two aliases never touch each
  other's tables. `DjangoAttachmentManager` does not touch the database at all.
* `backend_identifier` (default `None`): what `get_backend_identifier()` returns, the stable
  name a multi-backend `NotificationService` addresses the backend by (`backend_identifier=` on
  its methods, the replication queue). Leave it `None` and the service names it `backend-{n}` by
  position, which shifts if backends are reordered.

```python
DjangoDbNotificationBackend(database="replica", backend_identifier="replica")

# Or from an import string, as settings do:
NotificationService(
    notification_backend="vintasend_django.services.notification_backends."
    "django_db_notification_backend.DjangoDbNotificationBackend",
    notification_backend_kwargs={"database": "replica", "backend_identifier": "replica"},
)
```

Both are kept in `backend_kwargs` when they are not the defaults, so a backend rebuilt from them,
such as the one a `vintasend-celery` worker builds, runs on the same alias. The alias must be
migrated like any other: `python manage.py migrate --database=replica`.

## Requeueing and repinning

`DjangoDbNotificationBackend` implements `requeue_notification` and `repin_notification` as single
conditional updates, so two callers racing cannot both succeed:

* `requeue_notification` moves a `FAILED` or `CANCELLED` notification back to `PENDING_SEND` with
  one `filter(id=..., status__in=[FAILED, CANCELLED]).update(...)`. It clears `sent_at`, applies
  `send_after` only when it is passed (`None` included), pins `template_version` in the same
  update when one is passed, and returns the notification with its attachments.
* `repin_notification` changes `requested_template_version` only, with one update whose `WHERE`
  carries the statuses a pin may still change in (`PENDING_SEND`, `FAILED`, `CANCELLED`) and
  "not already on this version" (an unpinned row included). A notification a worker sends in
  between is never repinned, and one already on the version is returned as it is, with nothing
  written.

When an update matches nothing, the row is read once to say why, and nothing is written: an
unknown id raises `NotificationNotFoundError`, a notification written since `expected_version`
raises `NotificationVersionChangedError` (see below), and the wrong status raises
`NotificationRequeueError` or `NotificationRepinError`. The service maps these to
`NotificationActionError` reasons `NOT_FOUND`, `CHANGED` and `INVALID_STATUS`.

A cancelled notification keeps its row with status `CANCELLED`, and `get_notification` returns
it, so it can be requeued. `persist_notification_update` accepts a `FAILED` or `CANCELLED`
notification as well as a pending one; a `SENT` or `READ` one still raises
`NotificationUpdateError`. See the `vintasend` README for `requeue_notification` and
`repin_notification` on the service.

## Moving notifications safely

The backend implements every optional action vintasend offers for moving notifications, and
reports all five in `get_action_capabilities()` (`repin.expectedVersion`,
`requeue.expectedVersion`, `requeue.templateVersion`, `versions.filter`,
`writes.notificationVersion`; see [Replicas](#replicas) for the last). It also evaluates the
presence filters `has_send_after` and `has_requested_template_version` (`IS NOT NULL` / `IS NULL`,
negation included) and reports them in `get_filter_capabilities()`.

```python
failed = {"and": [{"body_template": "welcome"}, {"status": NotificationStatus.FAILED}]}

# Selects the id and notification_version columns only: no recipient, context or content.
for listed in service.filter_notification_versions(failed, page=1, page_size=100):
    # One UPDATE: pin, requeue, and only if the row is unchanged since it was listed.
    service.requeue_notification(
        listed.id, template_version=4, expected_version=listed.version, send=False
    )
```

Moving notifications changes which ones match, so list again from page 1 rather than moving on to
page 2.

### The version token

The token is a `notification_version` UUID column on `Notification`. It is a new UUIDv7 on
**every** write to the row, and `filter_notification_versions` hands it out as `str(uuid)`.
`expected_version` joins the update's `WHERE`, so a notification written since it was listed is
refused with `NotificationVersionChangedError` (reason `CHANGED`) and nothing is written. A token
that is not a UUID in that form is refused the same way, before any query.

Every write the backend makes sets the token, and so does `Notification.save()`, the Django admin
and `save(update_fields=...)` included. Two kinds of write do not, and must set it themselves, or
`expected_version` cannot see them:

* `QuerySet.update()` in your own code, because Django does not run `save()` or field defaults
  for it:

  ```python
  from vintasend_django.models import Notification, new_notification_version

  Notification.objects.filter(id=notification_id).update(
      title="New title", notification_version=new_notification_version()
  )
  ```

* writes outside the Django ORM, such as raw SQL or another service writing to the same table:
  set `notification_version` to a fresh UUID in the same statement.

`bulk_update()` skips `save()` too, so include `notification_version` in its fields with a new
value per object.

### Replicas

With a second `DjangoDbNotificationBackend` as an additional backend, on its own database, the
replica holds the **primary's** tokens, so versions listed from it are ones the primary accepts:

```python
# settings.DATABASES has "default" and "replica", both migrated.
service = NotificationService(
    notification_backend=DjangoDbNotificationBackend(backend_identifier="primary"),
    additional_backends=[
        DjangoDbNotificationBackend(database="replica", backend_identifier="replica")
    ],
    replication_mode="inline",
)

for listed in service.filter_notification_versions(failed, 1, 100, backend_identifier="replica"):
    service.requeue_notification(listed.id, expected_version=listed.version, send=False)
```

`get_backend_supported_action_capabilities("replica")` reports `versions.filter` for it.

* **Snapshot apply carries the token.** After every replicated write, requeue and repin
  included, the service hands the replica the primary's post-write snapshot.
  `apply_replication_snapshot_if_newer` upserts it by id in one transaction: it inserts the row
  with the primary's id, `created`, `modified` and token, or overwrites an existing row with one
  conditional `UPDATE` whose `WHERE` carries `modified <= snapshot.modified`, so a row newer than
  the snapshot is left alone and the snapshot is declined as `superseded`, so the service does
  not replay the older write over it. Every other write the backend makes sets `modified` to
  now, so of two snapshots applied out of order the later one wins. A snapshot with no token, or
  one that is not a UUID, gets a fresh one. It is also declined, with nothing written, when the
  id does not fit this database's primary key (its range read from this database) or the
  notification's user does not exist in this database.
* **Replayed rows are stamped.** When the replica declines a snapshot for any other reason, the
  service replays the write with the replica's own methods, with no token, so each replayed
  write mints the replica's own. It then re-reads the row and, only if it matches the primary's
  snapshot, writes the primary's token with
  `persist_notification_update(id, {}, notification_version=<token>)`. The backend accepts that
  token-only write in any status, `SENT` and `READ` included, and changes nothing else, not even
  `modified`; a content update of a delivered notification is still refused. If any step fails
  the replica keeps a token of its own, which the primary refuses.
* **Promoting a replica.** Rows the replica receives keep the primary's ids, and an explicit-id
  insert does not advance the id sequence on PostgreSQL or Oracle. The snapshot apply resets the
  sequence after each insert, but a sequence reset is not transactional, so two concurrent
  inserts can leave it behind the highest id. Before making a replica the primary (after
  `migrate_to_backend`, say), with replication stopped, run
  `reset_notification_id_sequence("<alias>")` from
  `vintasend_django.services.notification_backends.django_db_notification_backend`, or
  `python manage.py sqlsequencereset vintasend_django --database=<alias>` and run its output
  with `python manage.py dbshell --database=<alias>`. SQLite and MySQL need neither.
* **Lag means `CHANGED`.** With queued replication, or a replica that missed a write, the replica
  can list a token the primary has moved past. The primary refuses it with reason `CHANGED` and
  nothing is written on either side: list again and retry.
* **Attachments share storage.** A snapshot's attachments are linked on the replica by matching
  its file records on `(checksum, size)`, or by creating a record from the snapshot's file
  metadata and `storage_identifiers`; join rows are then added and removed to match. No byte is
  read or copied, so this assumes both backends' attachment managers read the **same** storage
  (the same Django storage, bucket or `MEDIA_ROOT`). A replica whose manager reads other storage
  holds attachments it cannot open.
* **Users must exist on both.** The notification's user is a foreign key, so the replica's user
  table must hold the same users, with the same primary keys.

### Migration

Three migrations add the column:

- `0008` adds `notification_version` as a nullable column, with no backfill.
- `0009` gives every row whose token is NULL its own UUIDv7. It reads primary keys only, in
  batches of 2,000, one `UPDATE` per batch, and commits each batch on its own (the migration is
  not atomic), so it never holds locks across the whole table. If it is interrupted, run it
  again: it only touches the rows still NULL.
- `0010` runs the same backfill again, for rows inserted with no token after `0009`, then makes
  the column non-nullable.

All three reverse: `0010` makes the column nullable again, the backfill's reverse is a no-op, and
rolling back past `0008` drops the column.

A non-rolling deploy just runs `python manage.py migrate`.

For a rolling deploy, where instances running the previous release keep writing while the new one
rolls out:

1. Before deploying the new code, run `python manage.py migrate vintasend_django 0009`. The
   previous release does not know the column and inserts rows with a NULL token, which the
   nullable column accepts.
2. Deploy the new code.
3. Once no instance of the previous release is running, run `python manage.py migrate`. `0010`
   gives the rows inserted in the meantime their tokens, then sets NOT NULL.

Between steps 2 and 3, a row with no token yet is still listed by `filter_notification_versions`,
with the empty string as its version, and a conditional `requeue_notification` or
`repin_notification` against it is refused with `NotificationVersionChangedError` whatever
`expected_version` is passed. Moves without `expected_version` work as usual and give it a token.
