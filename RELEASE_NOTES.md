# Release Notes


## Version 1.2.0 (2026-06-14)

### Bugfix

* Fixed in-app unread queries returning nothing: `_get_all_in_app_unread_notifications_queryset`
  filtered by the raw `NotificationTypes.IN_APP` enum (which stringifies to
  `"NotificationTypes.IN_APP"`) instead of `NotificationTypes.IN_APP.value` (`"IN_APP"`).
  `filter_in_app_unread_notifications` / `filter_all_in_app_unread_notifications` now return rows.

### Features

* List ALL in-app notifications (read + unread): `filter_all_in_app_notifications` (unpaginated),
  `filter_in_app_notifications(page, page_size)` (paginated). "All" = IN_APP and status in
  (SENT, READ); internal pipeline states (PENDING_SEND, FAILED, CANCELLED) are never exposed.
* Count helpers `count_in_app_notifications` and `count_in_app_unread_notifications` (efficient
  `.count()` overrides) so callers can build count / next / previous envelopes for both the
  unread and the all lists. Prefer the paginated `filter_*` + matching `count_*` over the
  unpaginated `filter_all_*` variants.
* Bulk mark-as-read: `mark_sent_as_read_bulk(notification_ids, user_id=None)` — single bulk
  UPDATE in a transaction. Idempotent (already-READ / missing / non-SENT ids are skipped, never
  an error), optionally scoped to `user_id` (rows owned by others are never touched), and returns
  the final READ state for the requested ids.
* In-app list querysets now order newest-first with a PK tiebreaker (`-created`, `-id`) for
  deterministic pagination (unread list changed from ascending `created` to `-created, -id`).
* `serialize_user_notification` / `serialize_one_off_notification` now carry `created`,
  `modified`, and `context_used` (plus `adapter_used`, `adapter_extra_parameters`) into the
  dataclass, so listed notifications keep timestamps and stored render context.

### Backwards compatibility

* Requires `vintasend>=1.2.0`. Custom `BaseNotificationBackend` / `AsyncIOBaseNotificationBackend`
  subclasses must now implement `filter_all_in_app_notifications`, `filter_in_app_notifications`,
  and `mark_sent_as_read_bulk` (newly abstract). `count_in_app_notifications` and
  `count_in_app_unread_notifications` ship as concrete defaults derived from the existing
  iterables, so existing backends keep working without changes — but SHOULD be overridden for
  efficiency (this backend overrides both with `.count()`).
* No existing method signature or semantic changed.

* Bump vintasend to 1.2.0


## Version 1.1.3 (2026-06-12)

* Bump vintasend to 1.1.3

## Version 1.1.2 (2026-06-12)

* Bump vintasend to 1.1.2

## Version 1.1.1 (2026-06-03)

* Bump vintasend to 1.1.1
* Add Python 3.14 support (Django 6.0)


## Version 1.1.0 (2026-06-03)

* Bump vintasend to 1.1.0
* Add Django 6.0 support; drop Python 3.10/3.11 (now requires Python >=3.12)
* Bump dev dependencies (pytest 9, django-stubs 6, coverage 7.14, tox 4.55, others)


## Version 1.0.1 (2025-09-16)

* Bump vintasend version to 1.0.1


## Version 1.0.0 (2025-09-16)

### 🚀 Major Features

* Upgrade vintasend to version 1.0.0
* Support one-off notifications (without user)
* Support attachements

---

## Version 0.1.4 (Initial Release)

Initial version of VintaSend with core notification functionality.
