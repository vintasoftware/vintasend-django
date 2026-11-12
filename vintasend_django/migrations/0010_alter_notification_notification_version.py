"""Make ``Notification.notification_version`` non-nullable.

Runs the 0009 backfill again first, for rows that code predating the column inserted (with a
NULL token) after 0009 ran, then sets NOT NULL. Run it once no such code is running any more.

``atomic = False`` like 0009, so the backfill commits per batch and the ``ALTER`` runs after the
rows it depends on were committed (PostgreSQL can refuse to alter a table with pending trigger
events from the same transaction). Reversing it makes the column nullable again; the backfill's
reverse is a no-op.
"""

from django.db import migrations, models

import vintasend_django.models
from vintasend_django.migrations._notification_version_backfill import (
    backfill_notification_versions,
)


class Migration(migrations.Migration):

    atomic = False

    dependencies = [
        ("vintasend_django", "0009_backfill_notification_version"),
    ]

    operations = [
        migrations.RunPython(backfill_notification_versions, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="notification",
            name="notification_version",
            field=models.UUIDField(
                default=vintasend_django.models.new_notification_version,
                editable=False,
                verbose_name="notification version",
            ),
        ),
    ]
