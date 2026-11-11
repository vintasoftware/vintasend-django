# VintaSend Django

Django implementations for VintaSend.

`DjangoDbNotificationBackend` implements the full notification backend, including the in-app
listing API: `filter_in_app_unread_notifications` / `filter_all_in_app_unread_notifications`
(unread), `filter_in_app_notifications` / `filter_all_in_app_notifications` (read + unread),
the `count_in_app_notifications` / `count_in_app_unread_notifications` counters, and the
idempotent, optionally user-scoped `mark_sent_as_read_bulk`. See the `vintasend` README for the
service-level in-app API (pagination, counts and bulk mark-as-read).

## Requeueing and repinning

`DjangoDbNotificationBackend` implements `requeue_notification`, which vintasend 3.7.0 added to
the backend contract. It moves a `FAILED` or `CANCELLED` notification back to `PENDING_SEND` with
one conditional update (`filter(id=..., status__in=[FAILED, CANCELLED]).update(...)`), so two
callers racing cannot both succeed. It clears `sent_at`, applies `send_after` only when it is
passed (`None` included), and returns the notification with its attachments. An unknown id raises
`NotificationNotFoundError`, and any other status raises `NotificationRequeueError`.

A cancelled notification keeps its row with status `CANCELLED`, and `get_notification` returns
it, so it can be requeued. `persist_notification_update` accepts a `FAILED` or `CANCELLED`
notification as well as a pending one, which `repin_notification` relies on; a `SENT` or `READ`
one still raises `NotificationUpdateError`. See the `vintasend` README for
`requeue_notification` and `repin_notification` on the service.