"""Drop the legacy ``Attachment`` table.

Safe to run only because migration 0005 has already copied every ``Attachment`` row into the new
``AttachmentFileRecord`` / ``NotificationAttachment`` tables (leaving the underlying files in
place). Reversing this migration recreates the empty table; reversing 0005 then repopulates it.
"""

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("vintasend_django", "0005_migrate_attachments_to_file_records"),
    ]

    operations = [
        migrations.DeleteModel(
            name="Attachment",
        ),
    ]
