# Generated by Django 4.2.22 on 2025-06-26 09:20

from django.db import migrations, models
from django.core.validators import MinValueValidator
from datetime import timedelta


class Migration(migrations.Migration):
    dependencies = [
        ("posthog", "0784_fix_null_event_triggers"),
    ]

    operations = [
        migrations.AddField(
            model_name="team",
            name="drop_events_older_than",
            field=models.DurationField(
                null=True,
                blank=True,
                validators=[MinValueValidator(timedelta(hours=1))],  # For safety minimum 1h
                help_text="Events older than this threshold will be dropped in ingestion. Empty means no timestamp restrictions.",
            ),
        ),
    ]
