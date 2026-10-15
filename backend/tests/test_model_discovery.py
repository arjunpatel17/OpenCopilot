"""Regression tests for account-aware Copilot model discovery (no network)."""

import asyncio
import os
import time
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from app.services import copilot


CATALOG_URL = "https://api.githubcopilot.com/models"


def catalog_model(model_id, name=None, **overrides):
    """Minimal model metadata matching the authenticated Copilot catalog."""
    model = {
        "id": model_id,
        "name": name or model_id,
        "model_picker_enabled": True,
        "policy": {"state": "enabled"},
        "capabilities": {"type": "chat", "supports": {"tool_calls": True}},
    }
    model.update(overrides)
    return model


def model_ids(groups):
    return [model["id"] for group in groups for model in group["models"]]


class FallbackModelsTests(unittest.TestCase):
    def test_current_models_are_included(self):
        current = {
            "claude-opus-5.5", "claude-sonnet-5.5", "claude-haiku-5.5",
            "claude-opus-5", "claude-sonnet-5", "gpt-6.1-sol",
            "gpt-6-astra", "gpt-6-luna", "gpt-6-sol", "gpt-5.6-sol",
            "gpt-5.6-luna", "gpt-5.6-terra", "gemini-3.7-flash",
            "gemini-3.8-flash", "mai-code-1.1-flash", "grok-4.7",
        }
        self.assertTrue(current.issubset(model_ids(copilot._DEFAULT_MODELS)))

    def test_retired_models_are_not_advertised(self):
        retired = {
            "claude-opus-4.7", "claude-opus-4.7-1m", "claude-opus-4.6",
            "claude-opus-4.5", "claude-sonnet-4.5", "claude-sonnet-4",
            "gpt-5.2", "gpt-5.2-codex", "gpt-5.1", "gpt-5.1-codex",
            "gpt-5.1-codex-max", "gpt-5.1-codex-mini", "gpt-4.1",
            "gemini-3-pro-preview", "mai-code-1-flash",
        }
        self.assertFalse(retired.intersection(model_ids(copilot._DEFAULT_MODELS)))

    def test_current_default_is_preserved(self):
        self.assertIn("claude-opus-4.8-1m", model_ids(copilot._DEFAULT_MODELS))

    def test_long_context_aliases_use_real_cli_model_ids(self):
        for model_id in model_ids(copilot._DEFAULT_MODELS):
            if model_id.endswith("-1m"):
                with self.subTest(model=model_id):
                    self.assertEqual(
                        copilot._resolve_model_args(model_id),
                        ["--model", model_id[:-3], "--context", "long_context"],
                    )

    def test_empty_cached_catalog_is_authoritative(self):
        with patch.object(copilot, "_cached_models", []):
            self.assertEqual(copilot.get_models(), [])


class CatalogGroupingTests(unittest.TestCase):
    def test_groups_current_and_future_providers(self):
        models = [
            catalog_model("claude-opus-5.5", "Claude Opus 5.5"),
            catalog_model("gpt-6.1-sol", "GPT-6.1 Sol"),
            catalog_model("gemini-3.8-flash", "Gemini 3.8 Flash"),
            catalog_model("mai-code-1.1-flash", "MAI-Code-1.1-Flash"),
            catalog_model("grok-4.7", "Grok 4.7"),
            catalog_model("kimi-k3", "Kimi K3"),
            catalog_model("new-vendor-model", "New Vendor Model"),
        ]
        groups = copilot._group_catalog_models(models)
        self.assertEqual(
            [group["group"] for group in groups],
            ["Claude", "GPT", "Gemini", "Microsoft", "Grok", "Kimi", "Other"],
        )
        self.assertEqual(model_ids(groups), [model["id"] for model in models])

    def test_hidden_disabled_and_non_agent_models_are_filtered(self):
        models = [
            catalog_model("gpt-6.1-sol", "GPT-6.1 Sol"),
            catalog_model("hidden-model", model_picker_enabled=False),
            catalog_model("disabled-model", policy={"state": "disabled"}),
            catalog_model("unconfigured-model", policy={"state": "unconfigured"}),
            catalog_model("embedding-model", capabilities={"type": "embeddings"}),
            catalog_model("no-tools", capabilities={"type": "chat", "supports": {"tool_calls": False}}),
            catalog_model("missing-capabilities", capabilities=None),
        ]
        self.assertEqual(model_ids(copilot._group_catalog_models(models)), ["gpt-6.1-sol"])

    def test_internal_models_are_filtered_even_when_picker_enabled(self):
        models = [
            catalog_model("gpt-5.6-sol-fast", "GPT-5.6 Sol Fast (Internal only)"),
            catalog_model("claude-opus-internal", "Claude Opus"),
            catalog_model("internal-upper", "INTERNAL ONLY"),
            catalog_model("gpt-5.6-sol", "GPT-5.6 Sol"),
        ]
        self.assertEqual(model_ids(copilot._group_catalog_models(models)), ["gpt-5.6-sol"])

    def test_models_without_optional_policy_are_allowed(self):
        model = catalog_model("gpt-5.3-codex", "GPT-5.3-Codex")
        del model["policy"]
        self.assertEqual(model_ids(copilot._group_catalog_models([model])), ["gpt-5.3-codex"])

    def test_invalid_and_duplicate_entries_do_not_hide_valid_models(self):
        model = catalog_model("gpt-6.1-sol", "GPT-6.1 Sol")
        models = [None, "bad entry", {}, catalog_model(42), model, model.copy()]
        self.assertEqual(model_ids(copilot._group_catalog_models(models)), ["gpt-6.1-sol"])

    def test_long_context_alias_is_preserved_for_supported_base(self):
        model = catalog_model("claude-opus-4.8", "Claude Opus 4.8")
        model["capabilities"]["limits"] = {"max_context_window_tokens": 1000000}
        self.assertEqual(
            model_ids(copilot._group_catalog_models([model])),
            ["claude-opus-4.8", "claude-opus-4.8-1m"],
        )

    def test_long_context_alias_requires_confirmed_context_capacity(self):
        model = catalog_model("claude-opus-4.8", "Claude Opus 4.8")
        model["capabilities"]["limits"] = {"max_context_window_tokens": 200000}
        self.assertEqual(model_ids(copilot._group_catalog_models([model])), ["claude-opus-4.8"])

    def test_native_large_context_does_not_invent_a_pricing_tier(self):
        model = catalog_model("gpt-6.1-sol", "GPT-6.1 Sol")
        model["capabilities"]["limits"] = {"max_context_window_tokens": 1050000}
        self.assertEqual(model_ids(copilot._group_catalog_models([model])), ["gpt-6.1-sol"])


class ModelDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {"GH_TOKEN": "test-catalog-token"}, clear=True))
        self.enterContext(patch.object(copilot, "_cached_models", None))
        self.enterContext(patch.object(copilot, "_model_cache_expires_at", 0.0))
        self.enterContext(patch.object(copilot, "_model_discovery_lock", asyncio.Lock()))
        self.client = AsyncMock()
        self.client_factory = self.enterContext(patch("app.services.copilot.httpx.AsyncClient"))
        self.client_factory.return_value.__aenter__.return_value = self.client
        self.client_factory.return_value.__aexit__.return_value = False
        self.client.get.return_value = self.response({"data": [catalog_model("gpt-6.1-sol", "GPT-6.1 Sol")]})

    @staticmethod
    def response(payload, status_code=200):
        return httpx.Response(status_code, json=payload, request=httpx.Request("GET", CATALOG_URL))

    async def test_discovery_uses_authenticated_catalog_not_an_llm_prompt(self):
        with patch.object(copilot, "run_code_chat") as chat, patch.object(copilot, "_find_cli") as find_cli:
            groups = await copilot.discover_models()
        self.assertEqual(model_ids(groups), ["gpt-6.1-sol"])
        chat.assert_not_called()
        find_cli.assert_not_called()
        args, kwargs = self.client.get.call_args
        self.assertEqual(args, (CATALOG_URL,))
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer test-catalog-token")
        self.assertEqual(kwargs["headers"]["Copilot-Integration-Id"], "copilot-cli")

    async def test_cli_token_environment_precedence_is_respected(self):
        os.environ.update({"COPILOT_GITHUB_TOKEN": "preferred-token", "GITHUB_TOKEN": "other-token"})
        await copilot.discover_models()
        self.assertEqual(self.client.get.call_args.kwargs["headers"]["Authorization"], "Bearer preferred-token")

    async def test_github_token_is_used_when_other_tokens_are_absent(self):
        os.environ.pop("GH_TOKEN")
        os.environ["GITHUB_TOKEN"] = "github-token"
        await copilot.discover_models()
        self.assertEqual(self.client.get.call_args.kwargs["headers"]["Authorization"], "Bearer github-token")

    async def test_missing_credentials_uses_fallback_without_network(self):
        os.environ.clear()
        self.assertEqual(await copilot.discover_models(), copilot._DEFAULT_MODELS)
        self.client_factory.assert_not_called()

    async def test_fresh_cache_avoids_another_request(self):
        first = await copilot.discover_models()
        self.assertEqual(await copilot.discover_models(), first)
        self.client.get.assert_awaited_once()
        self.assertGreater(copilot._model_cache_expires_at, time.monotonic())

    async def test_expired_cache_is_refreshed(self):
        await copilot.discover_models()
        copilot._model_cache_expires_at = 0.0
        self.client.get.return_value = self.response({"data": [catalog_model("claude-sonnet-5.5")]})
        self.assertEqual(model_ids(await copilot.discover_models()), ["claude-sonnet-5.5"])
        self.assertEqual(self.client.get.await_count, 2)

    async def test_request_failure_preserves_last_successful_catalog(self):
        previous = await copilot.discover_models()
        copilot._model_cache_expires_at = 0.0
        self.client.get.return_value = self.response({"error": "unavailable"}, status_code=503)
        self.assertEqual(await copilot.discover_models(), previous)
        self.assertEqual(copilot.get_models(), previous)

    async def test_failed_requests_are_throttled_without_logging_secrets(self):
        self.client.get.return_value = self.response({"error": "private-response-body"}, status_code=401)
        with self.assertLogs(copilot.logger, level="WARNING") as logs:
            self.assertEqual(await copilot.discover_models(), copilot._DEFAULT_MODELS)
            await copilot.discover_models()
        self.client.get.assert_awaited_once()
        self.assertNotIn("test-catalog-token", " ".join(logs.output))
        self.assertNotIn("private-response-body", " ".join(logs.output))

    async def test_network_timeout_uses_fallback(self):
        self.client.get.side_effect = httpx.ReadTimeout("timed out")
        self.assertEqual(await copilot.discover_models(), copilot._DEFAULT_MODELS)

    async def test_malformed_catalog_uses_fallback(self):
        self.client.get.return_value = self.response({"data": None})
        self.assertEqual(await copilot.discover_models(), copilot._DEFAULT_MODELS)
        self.assertIsNone(copilot._cached_models)

    async def test_empty_successful_catalog_does_not_reintroduce_unavailable_models(self):
        self.client.get.return_value = self.response({"data": []})
        self.assertEqual(await copilot.discover_models(), [])
        self.assertEqual(copilot.get_models(), [])
        self.assertEqual(await copilot.discover_models(), [])
        self.client.get.assert_awaited_once()

    async def test_concurrent_requests_share_one_discovery(self):
        started = asyncio.Event()
        release = asyncio.Event()

        async def fetch(*args, **kwargs):
            started.set()
            await release.wait()
            return self.response({"data": [catalog_model("gpt-6.1-sol")]})

        self.client.get.side_effect = fetch
        tasks = [asyncio.create_task(copilot.discover_models()) for _ in range(4)]
        await started.wait()
        release.set()
        results = await asyncio.gather(*tasks)
        self.assertTrue(all(model_ids(groups) == ["gpt-6.1-sol"] for groups in results))
        self.client.get.assert_awaited_once()

    async def test_cancellation_is_not_swallowed(self):
        self.client.get.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await copilot.discover_models()
        self.assertIsNone(copilot._cached_models)


class ModelConsumerTests(unittest.IsolatedAsyncioTestCase):
    async def test_models_api_refreshes_the_catalog(self):
        from app.main import list_models

        groups = [{"group": "GPT", "models": [{"id": "gpt-6.1-sol", "name": "GPT-6.1 Sol"}]}]
        with patch.object(copilot, "discover_models", AsyncMock(return_value=groups)) as discover:
            self.assertEqual(await list_models(), groups)
        discover.assert_awaited_once()

    async def test_telegram_models_uses_the_same_refreshed_catalog(self):
        from app.services.telegram_bot import _handle_cmd_models

        groups = [{"group": "GPT", "models": [{"id": "gpt-6.1-sol", "name": "GPT-6.1 Sol"}]}]
        bot = AsyncMock()
        with patch.object(copilot, "discover_models", AsyncMock(return_value=groups)) as discover:
            await _handle_cmd_models(bot, 123)
        discover.assert_awaited_once()
        self.assertIn("gpt-6.1-sol", bot.send_message.call_args.kwargs["text"])

    async def test_telegram_model_names_are_markdown_escaped(self):
        from app.services.telegram_bot import _handle_cmd_models

        groups = [{"group": "Other", "models": [{"id": "future-model", "name": "Future_Model *Preview* [test]"}]}]
        bot = AsyncMock()
        with patch.object(copilot, "discover_models", AsyncMock(return_value=groups)):
            await _handle_cmd_models(bot, 123)
        self.assertIn(r"Future\_Model \*Preview\* \[test]", bot.send_message.call_args.kwargs["text"])

    async def test_telegram_large_catalog_is_split_into_safe_messages(self):
        from app.services.telegram_bot import _handle_cmd_models

        models = [{"id": f"future-model-{index}", "name": f"Future Model {index} Preview"} for index in range(150)]
        groups = [{"group": "Other", "models": models}]
        bot = AsyncMock()
        with patch.object(copilot, "discover_models", AsyncMock(return_value=groups)):
            await _handle_cmd_models(bot, 123)
        messages = [call.kwargs["text"] for call in bot.send_message.call_args_list]
        self.assertGreater(len(messages), 1)
        self.assertTrue(all(len(message) <= 4096 for message in messages))
        self.assertTrue(all(f"`{model['id']}`" in "\n".join(messages) for model in models))


if __name__ == "__main__":
    unittest.main()