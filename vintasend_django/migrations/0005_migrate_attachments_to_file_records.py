"""Copy legacy ``Attachment`` rows into the 2.0 attachment tables.

The pre-2.0 backend stored each attachment as a single ``Attachment`` row that owned both the
file (a ``FileField``) and the notification link. 2.0 splits that into a checksum-indexed
``AttachmentFileRecord`` (the stored blob) plus a ``NotificationAttachment`` join row. This data
migration preserves every existing attachment.

Two deliberate properties keep it fast and storage-agnostic:

* **No file is read.** The underlying blob is left exactly where it is; the new record's
  ``storage_identifiers`` point at the same storage path (``FieldFile.name``, a plain string that
  needs no I/O). So the migration never opens, downloads, or copies a byte -- it behaves the same
  on local disk and on a remote backend like S3, and cannot fail because an object is momentarily
  unreachable. The consequence is that migrated records carry an **empty checksum**: they simply do
  not participate in the 2.0 (checksum, size) dedup until a fresh upload of the same bytes creates
  a checksummed record. Dedup is an optimization, never a correctness requirement, so this is safe.
* **Rows are written in bulk.** Records and join rows are inserted with ``bulk_create`` in batches.
  On a backend that cannot return primary keys from a bulk insert (e.g. MySQL) the records are
  saved individually so the join-row foreign key can be set, while the join rows still go in one
  bulk insert per batch.

Migration 0006 drops the now-empty ``Attachment`` table only after this copy has run.
"""

from django.db import migrations


BATCH_SIZE = 2000


def _storage_identifiers(attachment):
    # ``FieldFile.name`` is the stored path -- a string, no I/O. storage_identifiers must carry a
    # non-empty "id"; point both keys at that path so DjangoAttachmentManager can reconstruct and
    # delete the same file later.
    name = attachment.file.name if attachment.file else ""
    return {"id": name, "name": name} if name else {}


def forwards(apps, schema_editor):
    attachment_model = apps.get_model("vintasend_django", "Attachment")
    file_record_model = apps.get_model("vintasend_django", "AttachmentFileRecord")
    join_model = apps.get_model("vintasend_django", "NotificationAttachment")
    can_return_pks = schema_editor.connection.features.can_return_rows_from_bulk_insert

    def flush(attachments):
        records = [
            file_record_model(
                filename=attachment.name,
                content_type=attachment.mime_type,
                size=attachment.size or 0,
                checksum="",  # not computed here -- see the module docstring
                storage_identifiers=_storage_identifiers(attachment),
                # created is preserved (AutoCreatedField honors an explicit value); modified is
                # stamped at migration time by AutoLastModifiedField.
                created=attachment.created,
            )
            for attachment in attachments
        ]
        if can_return_pks:
            file_record_model.objects.bulk_create(records, batch_size=BATCH_SIZE)
        else:
            # This backend does not return PKs from a bulk insert, so persist records one by one
            # to obtain the PKs the join-row FK needs. The join rows still go in one bulk insert.
            for record in records:
                record.save()

        join_rows = [
            join_model(
                notification_id=attachment.notification_id,
                file=record,
                description=None,
                is_inline=False,
                created=attachment.created,
            )
            for attachment, record in zip(attachments, records, strict=True)
        ]
        join_model.objects.bulk_create(join_rows, batch_size=BATCH_SIZE)

    batch = []
    for attachment in attachment_model.objects.all().iterator(chunk_size=BATCH_SIZE):
        batch.append(attachment)
        if len(batch) >= BATCH_SIZE:
            flush(batch)
            batch = []
    if batch:
        flush(batch)


def backwards(apps, schema_editor):
    """Recreate legacy ``Attachment`` rows from the 2.0 tables.

    Best-effort inverse: 2.0-only concepts (a file record shared by several notifications,
    ``description`` / ``is_inline``, attach-by-reference) collapse back into one ``Attachment``
    row per join row, pointing at the same stored file. Like the forward pass it performs no file
    I/O -- it only sets ``FieldFile.name`` to the preserved path. Runs after 0006 has been
    reversed, so the ``Attachment`` table exists again.
    """
    Attachment = apps.get_model("vintasend_django", "Attachment")
    NotificationAttachment = apps.get_model("vintasend_django", "NotificationAttachment")

    restored = []
    for join_row in NotificationAttachment.objects.select_related("file").iterator(
        chunk_size=BATCH_SIZE
    ):
        record = join_row.file
        attachment = Attachment(
            notification_id=join_row.notification_id,
            name=record.filename,
            mime_type=record.content_type,
            size=record.size,
            created=join_row.created,
        )
        identifiers = record.storage_identifiers or {}
        stored_name = identifiers.get("name") or identifiers.get("id")
        if stored_name:
            # Point the FileField at the existing file without re-uploading it.
            attachment.file.name = stored_name
        restored.append(attachment)
        if len(restored) >= BATCH_SIZE:
            Attachment.objects.bulk_create(restored, batch_size=BATCH_SIZE)
            restored = []
    if restored:
        Attachment.objects.bulk_create(restored, batch_size=BATCH_SIZE)


class Migration(migrations.Migration):

    dependencies = [
        ("vintasend_django", "0004_notification_git_commit_sha_notification_read_at_and_more"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
