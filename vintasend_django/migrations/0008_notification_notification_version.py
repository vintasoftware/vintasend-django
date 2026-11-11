"""Add ``Notification.notification_version``, the version token, as a nullable column.

No backfill here: 0009 gives every existing row its own token in committed batches, and 0010
makes the column non-nullable. Until 0010 runs, code that predates the column can keep inserting
rows (with a NULL token), which is what lets a rolling deploy stop at 0009.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("vintasend_django", "0007_notification_requested_template_version_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="notification",
            name="notification_version",
            field=models.UUIDField(
                editable=False, null=True, verbose_name="notification version"
            ),
        ),
    ]
