# Generated by Django 4.2.22 on 2025-06-12 09:13

from django.db import migrations

PARENT_PREFIXES = [
    "template-webhook",
    "template-slack",
    "template-microsoft-teams",
    "template-discord",
]

SUBTEMPLATES = [
    # Webhook
    "template-webhook-survey-response",
    "template-webhook-error-tracking-issue-reopened",
    "template-webhook-error-tracking-issue-created",
    # Slack
    "template-slack-survey-response",
    "template-slack-error-tracking-issue-reopened",
    "template-slack-error-tracking-issue-created",
    "template-slack-early-access-feature-enrollment",
    "template-slack-activity-log",
    # Microsoft Teams
    "template-microsoft-teams-survey-response",
    "template-microsoft-teams-error-tracking-issue-created",
    "template-microsoft-teams-error-tracking-issue-reopened",
    # Discord
    "template-discord-survey-response",
    "template-discord-error-tracking-issue-reopened",
    "template-discord-error-tracking-issue-created",
    "template-discord-early-access-feature-enrollment",
]

TEMPLATE_MAPPING = {sub: next(prefix for prefix in PARENT_PREFIXES if sub.startswith(prefix)) for sub in SUBTEMPLATES}


def forwards(apps, schema_editor):
    HogFunction = apps.get_model("posthog", "HogFunction")
    for old_id, new_id in TEMPLATE_MAPPING.items():
        HogFunction.objects.filter(template_id=old_id).update(template_id=new_id)


def backwards(apps, schema_editor):
    # This migration cannot be reversed as multiple sub-templates map to the same parent template,
    # and there's no way to determine which specific sub-template a function originally used.
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("posthog", "0765_hogflows"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
