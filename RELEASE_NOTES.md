# Release Notes


## Version 2.0.0 (2026-07-23)

Compatibility release for VintaSend core 2.0.0. Implements the new backend abstract methods
(the filtering / ordering API, the attachment manager seam, git-commit-SHA storage) and adds a
Django-storage-backed attachment manager. Requires `vintasend>=2.0.0`.

### Features

* **Filtering / ordering** — `filter_notifications(filter, page, page_size, order_by=None)`,
  `count_notifications(filter)` and `get_filter_capabilities()` translate the composable filter
  vocabulary from `vintasend.services.notification_backends.filters` into Django `Q` objects.
  Translation runs in negation-normal form so NULL semantics match the reference in-memory
  evaluator exactly: a positive filter never matches a NULL value, and NULL rows ARE included
  under `not`. Ordering always appends an `id` tiebreaker in the primary direction for stable
  offset pagination. The backend supports the full vocabulary, so `get_filter_capabilities()`
  returns `{}`.
* **Attachment manager seam** — the backend now persists checksum-indexed file records and
  notification/file join rows and delegates every byte to an injected `BaseAttachmentManager`.
  New `DjangoAttachmentManager`
  (`vintasend_django.services.attachment_managers.django_storage`) stores the bytes in a Django
  storage backend (`default_storage` by default; pass `storage` / `upload_to` to target another).
  Uploads are deduplicated on (checksum, size); attach-by-reference
  (`NotificationAttachmentReference`) links an already-stored file by id. The backend defaults to
  a `DjangoAttachmentManager` when the service injects none, so it works standalone.
* **Git commit SHA** — `store_git_commit_sha` persists the send-time revision;
  `Notification.git_commit_sha` is serialized back onto the dataclass.
* `persist_notification` / `persist_one_off_notification` accept the new optional `tenant`
  keyword; `mark_pending_as_sent` sets `sent_at` and `mark_sent_as_read` /
  `mark_sent_as_read_bulk` set `read_at`. `serialize_*` carry `sent_at`, `read_at`, `tenant`,
  `git_commit_sha`, and the notification's attachments.
* `DjangoTemplatedEmailRenderer.render_from_template_content(notification, template_content,
  context)` renders an email from supplied `EmailTemplateContent` for previews / audits without
  consulting the notification's stored templates; `render` now also populates
  `TemplatedEmail.preheader`.

### Migrations

The old `Attachment` model is replaced by `AttachmentFileRecord` (a checksum-indexed stored-blob
row) + `NotificationAttachment` (the notification/file join row). The upgrade is split across three
migrations so **existing attachments are preserved** — nothing is dropped until the data has been
copied:

* `0004_notification_git_commit_sha_notification_read_at_and_more` — additive only: adds `sent_at`,
  `read_at`, `tenant`, `git_commit_sha` to `Notification` and creates the two new attachment
  tables.
* `0005_migrate_attachments_to_file_records` — data migration that copies every legacy `Attachment`
  row into `AttachmentFileRecord` + `NotificationAttachment`, in bulk (`bulk_create`). The
  underlying file is **left exactly where it is in storage** — the new record's
  `storage_identifiers` point at the same path, so no bytes are moved or copied. The migration
  **never reads a file**, so it runs the same on local disk or a remote backend like S3 and cannot
  fail because an object is momentarily unreachable. The trade-off: migrated records carry an empty
  `checksum` and therefore do not participate in the 2.0 (checksum, size) dedup until the same bytes
  are uploaded fresh — dedup is an optimization, never a correctness requirement. Reversible:
  reversing it rebuilds the legacy rows.
* `0006_delete_attachment` — drops the now-empty legacy `Attachment` table.

Run `migrate` after upgrading. Take a database backup first, as with any schema migration.

**Optional checksum backfill.** Because `0005` does not read files, migrated records start with an
empty `checksum` and do not participate in attachment dedup. A management command backfills them on
your schedule, decoupled from the deploy:

```bash
python manage.py backfill_attachment_checksums          # only rows missing a checksum
python manage.py backfill_attachment_checksums --dry-run --limit 1000
python manage.py backfill_attachment_checksums --all    # recompute every record
```

It fetches each file one by one through the configured attachment manager (falling back to
`DjangoAttachmentManager`), computes the sha256 and real size, and updates the record. It is safe to
re-run, safe to interrupt, and skips files it cannot read (reporting them) rather than failing the
whole run. Run it off-peak or in a worker — this is the step that actually touches remote storage
(e.g. S3), which is exactly why it is kept out of the migration.

### Backwards compatibility

* **Backend/attachment authors:** the `Attachment` model is gone; use `AttachmentFileRecord` +
  `NotificationAttachment`. Any existing attachment rows (created via the admin, direct ORM use, or
  the pre-2.0 duck-typed `file_path` / `file_bytes` / `file_obj` write path) are migrated
  automatically by `0005`; their files are not touched.
* Application code that only consumes the notification services needs no change. See the core
  `MIGRATION_TO_2.0.0.md` for the service-level changes (notably `raise_on_failed_send` now
  defaults to `False`).


## Version 1.2.1 (2026-06-16)

### Bugfix

* Fixed `TypeError: Object of type datetime is not JSON serializable` when storing render
  context: `context_used`, `context_kwargs`, and `adapter_extra_parameters` now use
  `DjangoJSONEncoder`, which serializes `datetime`/`date`/`Decimal`/`UUID` to JSON. Note that
  these values round-trip back as ISO strings, not native objects.
* `DjangoTemplatedEmailRenderer` no longer raises `NotificationPreheaderTemplateRenderingError`
  when `preheader_template` is empty — the preheader render is now skipped entirely for
  notifications without a preheader template.

### Migrations

* `0003_alter_notification_adapter_extra_parameters_and_more` — alters the `encoder` on the three
  `JSONField`s. No-op at the database level; run `migrate` to keep Django's migration state in sync.

### Backwards compatibility

* No method signature or semantic change. Existing rows are unaffected.


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
