# VintaSend Django

Django implementations for VintaSend.

`DjangoDbNotificationBackend` implements the full notification backend, including the in-app
listing API: `filter_in_app_unread_notifications` / `filter_all_in_app_unread_notifications`
(unread), `filter_in_app_notifications` / `filter_all_in_app_notifications` (read + unread),
the `count_in_app_notifications` / `count_in_app_unread_notifications` counters, and the
idempotent, optionally user-scoped `mark_sent_as_read_bulk`. See the `vintasend` README for the
service-level in-app API (pagination, counts and bulk mark-as-read).

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
reports all four in `get_action_capabilities()` (`repin.expectedVersion`,
`requeue.expectedVersion`, `requeue.templateVersion`, `versions.filter`). It also evaluates the
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
