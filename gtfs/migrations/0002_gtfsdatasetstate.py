from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('gtfs', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='GtfsDatasetState',
            fields=[
                (
                    'singleton_id',
                    models.PositiveSmallIntegerField(
                        default=1,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ('revision', models.PositiveBigIntegerField(default=0)),
            ],
            options={
                'verbose_name': 'GTFS dataset state',
                'verbose_name_plural': 'GTFS dataset state',
            },
        ),
    ]
