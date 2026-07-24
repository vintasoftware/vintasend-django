"""Backfill sha256 checksums for attachment file records that lack one.

The ``0005_migrate_attachments_to_file_records`` data migration deliberately does not read files
(so it runs the same on local disk or a remote backend like S3, and never downloads a byte), which
leaves every migrated ``AttachmentFileRecord`` with an empty ``checksum``. Those records therefore
do not participate in the 2.0 (checksum, size) attachment dedup.

This command is the opt-in, operator-controlled second half: it fetches each file one by one
through the configured attachment manager, computes its sha256 and real size, and stores them. Run
it whenever suits the deployment -- off-peak, in a worker, over several sessions -- rather than
paying the file-read cost during a deploy migration. It is safe to re-run and safe to interrupt:
each record is updated on its own, and only records still missing a checksum are touched unless
``--all`` is given.
"""

import hashlib

from django.core.management.base import BaseCommand
from django.db.models import Q

from vintasend.services.helpers import get_attachment_manager

from vintasend_django.models import AttachmentFileRecord
from vintasend_django.services.attachment_managers.django_storage import DjangoAttachmentManager


class Command(BaseCommand):
    help = (
        "Compute and store sha256 checksums for AttachmentFileRecord rows that lack one "
        "(e.g. rows created by the 2.0 attachment migration), so they join the attachment dedup."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--batch-size",
            type=int,
            default=500,
            help="How many records to stream from the database at a time (default: 500).",
        )
        parser.add_argument(
            "--all",
            action="store_true",
            help="Recompute every record's checksum, not only rows whose checksum is empty.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would change without writing anything.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help="Process at most this many records this run (for chunking a large backfill).",
        )

    def _resolve_manager(self):
        # Read files through the manager the app actually configured (it wrote the
        # storage_identifiers), falling back to the backend's default Django-storage manager.
        manager = get_attachment_manager(None)
        if manager is None:
            manager = DjangoAttachmentManager()
        return manager

    def handle(self, *args, **options):
        batch_size = options["batch_size"]
        recompute_all = options["all"]
        dry_run = options["dry_run"]
        limit = options["limit"]

        manager = self._resolve_manager()

        queryset = AttachmentFileRecord.objects.all().order_by("pk")
        if not recompute_all:
            queryset = queryset.filter(Q(checksum="") | Q(checksum__isnull=True))

        total = queryset.count()
        if limit is not None:
            total = min(total, limit)
        self.stdout.write(f"Backfilling checksums for {total} attachment file record(s).")

        updated = 0
        skipped = 0
        processed = 0
        for record in queryset.iterator(chunk_size=batch_size):
            if limit is not None and processed >= limit:
                break
            processed += 1

            identifiers = record.storage_identifiers or {}
            if not (identifiers.get("name") or identifiers.get("id")):
                skipped += 1
                self.stderr.write(
                    f"  record {record.pk} ({record.filename}): no storage identifiers, skipped"
                )
                continue

            try:
                data = manager.reconstruct_attachment_file(identifiers).read()
            except Exception as error:  # noqa: BLE001 - one bad file must not abort the backfill
                skipped += 1
                self.stderr.write(
                    f"  record {record.pk} ({record.filename}): could not read file ({error}), skipped"
                )
                continue

            checksum = hashlib.sha256(data).hexdigest()
            size = len(data)
            if record.checksum == checksum and record.size == size:
                continue

            if dry_run:
                updated += 1
                continue

            # Bypass the model's save() so AutoLastModifiedField does not churn ``modified`` on a
            # pure metadata backfill; only checksum and size change.
            AttachmentFileRecord.objects.filter(pk=record.pk).update(
                checksum=checksum, size=size
            )
            updated += 1

        verb = "Would update" if dry_run else "Updated"
        self.stdout.write(
            self.style.SUCCESS(
                f"{verb} {updated} record(s); skipped {skipped}; processed {processed}."
            )
        )
