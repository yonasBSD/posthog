from inline_snapshot import snapshot
from posthog.cdp.templates.helpers import BaseHogFunctionTemplateTest
from posthog.cdp.templates.webhook.template_webhook_mappings import template as template_webhook


class TestTemplateWebhook(BaseHogFunctionTemplateTest):
    template = template_webhook

    def test_function_works(self):
        self.run_function(
            inputs={
                "url": "https://posthog.com",
                "method": "GET",
                "headers": {},
                "body": {"hello": "world"},
                "debug": False,
            }
        )

        assert self.get_mock_fetch_calls()[0] == snapshot(
            ("https://posthog.com", {"headers": {}, "body": {"hello": "world"}, "method": "GET"})
        )
        assert self.get_mock_print_calls() == snapshot([])

    def test_function_merges_headers(self):
        self.run_function(
            inputs={
                "url": "https://posthog.com",
                "method": "GET",
                "headers": {"Content-Type": "application/json"},
                "additional_headers": {"X-Custom-Header": "test"},
                "body": {"hello": "world"},
                "debug": False,
            }
        )

        assert self.get_mock_fetch_calls()[0] == snapshot(
            (
                "https://posthog.com",
                {
                    "headers": {"Content-Type": "application/json", "X-Custom-Header": "test"},
                    "body": {"hello": "world"},
                    "method": "GET",
                },
            )
        )

    def test_prints_when_debugging(self):
        self.run_function(
            inputs={
                "url": "https://posthog.com",
                "method": "GET",
                "headers": {},
                "body": {"hello": "world"},
                "debug": True,
            }
        )

        assert self.get_mock_fetch_calls()[0] == snapshot(
            ("https://posthog.com", {"headers": {}, "body": {"hello": "world"}, "method": "GET"})
        )
        assert self.get_mock_print_calls() == snapshot(
            [
                ("Request", "https://posthog.com", {"headers": {}, "body": {"hello": "world"}, "method": "GET"}),
                ("Response", 200, {}),
            ]
        )
