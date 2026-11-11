"""The ``notification_version`` backfill shared by migrations 0009 and 0010.

Not a migration itself: the loader skips modules whose name starts with ``_``.

Every row whose token is NULL gets its own UUIDv7. ``AddField`` / ``AlterField`` with the
callable default cannot do this: Django evaluates the default once, so every row would share it.

The backfill reads primary keys only, never any other column, and only rows still NULL, so
running it again after an interruption picks up where it stopped. Each batch is committed on its
own (the calling migrations are ``atomic = False``), so no lock is held across the whole table.
"""

from django.db import models, transaction

from vintasend_django.models import new_notification_version


BATCH_SIZE = 2000


def backfill_notification_versions(apps, schema_editor):
    notification_model = apps.get_model("vintasend_django", "Notification")
    alias = schema_editor.connection.alias
    missing = (
        notification_model.objects.using(alias)
        .filter(notification_version__isnull=True)
        .order_by("pk")
    )
    while True:
        with transaction.atomic(using=alias):
            pks = list(missing.values_list("pk", flat=True)[:BATCH_SIZE])
            if not pks:
                return
            # One UPDATE per batch, each row its own token. It re-checks NULL, so a row a
            # concurrent writer gave a token in the meantime keeps that one.
            notification_model.objects.using(alias).filter(
                pk__in=pks, notification_version__isnull=True
            ).update(
                notification_version=models.Case(
                    *(
                        models.When(pk=pk, then=models.Value(new_notification_version()))
                        for pk in pks
                    ),
                    output_field=models.UUIDField(),
                )
            )
