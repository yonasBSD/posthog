# Generated by Django 4.2.18 on 2025-05-23 12:59

from django.conf import settings
from django.db import migrations, models
import django.utils.timezone
import posthog.models.utils


class Migration(migrations.Migration):
    dependencies = [
        ("posthog", "0737_experiment_deleted"),
    ]

    operations = [
        migrations.CreateModel(
            name="PersistedFolder",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=posthog.models.utils.uuid7, editable=False, primary_key=True, serialize=False
                    ),
                ),
                ("type", models.CharField(choices=[("home", "Home"), ("pinned", "Pinned")], max_length=32)),
                ("protocol", models.CharField(default="products://", max_length=64)),
                ("path", models.TextField(blank=True, default="")),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now, editable=False)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("team", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to="posthog.team")),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "verbose_name": "Persisted Folder",
                "verbose_name_plural": "Persisted Folders",
                "indexes": [
                    models.Index(models.F("team_id"), models.F("user_id"), name="posthog_pf_team_user"),
                    models.Index(
                        models.F("team_id"), models.F("user_id"), models.F("type"), name="posthog_pf_team_user_type"
                    ),
                ],
                "unique_together": {("team", "user", "type")},
            },
        ),
    ]
