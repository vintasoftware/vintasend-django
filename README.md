# VintaSend Django

Django implementations for VintaSend.

`DjangoDbNotificationBackend` implements the full notification backend, including the in-app
listing API: `filter_in_app_unread_notifications` / `filter_all_in_app_unread_notifications`
(unread), `filter_in_app_notifications` / `filter_all_in_app_notifications` (read + unread),
the `count_in_app_notifications` / `count_in_app_unread_notifications` counters, and the
idempotent, optionally user-scoped `mark_sent_as_read_bulk`. See the `vintasend` README for the
service-level in-app API (pagination, counts and bulk mark-as-read).