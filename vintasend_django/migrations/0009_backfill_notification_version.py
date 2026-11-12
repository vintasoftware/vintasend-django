"""Give every notification whose ``notification_version`` is NULL its own UUIDv7.

``atomic = False``: each batch of 2,000 rows is committed on its own, so the migration never
holds row locks across the whole table, and running it again after an interruption only touches
the rows still NULL. See ``_notification_version_backfill``.
"""

from django.db import migrations

from vintasend_django.migrations._notification_version_backfill import (
    backfill_notification_versions,
)


class Migration(migrations.Migration):

    atomic = False

    dependencies = [
        ("vintasend_django", "0008_notification_notification_version"),
    ]

    operations = [
        # Reversing it is a no-op: reversing 0008 drops the column.
        migrations.RunPython(backfill_notification_versions, migrations.RunPython.noop),
    ]
