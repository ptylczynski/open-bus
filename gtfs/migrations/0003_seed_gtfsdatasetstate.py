from typing import Any

from django.db import migrations


def seed_dataset_revision(apps: Any, schema_editor: Any) -> None:
    trip_model = apps.get_model('gtfs', 'Trip')
    if not trip_model.objects.exists():
        return

    state_model = apps.get_model('gtfs', 'GtfsDatasetState')
    state_model.objects.update_or_create(
        singleton_id=1,
        defaults={'revision': 1},
    )


class Migration(migrations.Migration):
    dependencies = [
        ('gtfs', '0002_gtfsdatasetstate'),
    ]

    operations = [
        migrations.RunPython(
            seed_dataset_revision,
            reverse_code=migrations.RunPython.noop,
        ),
    ]
