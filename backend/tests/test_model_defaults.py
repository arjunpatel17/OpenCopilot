"""Default-model and CLI-effort regressions without launching an LLM request."""

import os
from pathlib import Path
import unittest
from unittest.mock import patch

from pydantic import ValidationError

from app.config import Settings
from app.services import copilot


ROOT = Path(__file__).resolve().parents[2]


class DefaultSettingsTests(unittest.TestCase):
    def test_default_is_opus_55_with_1m_and_high_reasoning(self):
        with patch.dict(os.environ, {}, clear=True):
            settings = Settings(_env_file=None)
        self.assertEqual(settings.copilot_model, "claude-opus-5.5-1m")
        self.assertEqual(settings.copilot_reasoning_effort, "high")
        self.assertEqual(
            copilot._resolve_model_args(settings.copilot_model),
            ["--model", "claude-opus-5.5", "--context", "long_context"],
        )

    def test_environment_can_override_model_and_effort(self):
        with patch.dict(os.environ, {
            "COPILOT_MODEL": "claude-sonnet-5.5",
            "COPILOT_REASONING_EFFORT": "medium",
        }, clear=True):
            settings = Settings(_env_file=None)
        self.assertEqual(settings.copilot_model, "claude-sonnet-5.5")
        self.assertEqual(settings.copilot_reasoning_effort, "medium")

    def test_empty_effort_leaves_the_cli_setting_untouched(self):
        with patch.dict(os.environ, {"COPILOT_REASONING_EFFORT": ""}, clear=True):
            settings = Settings(_env_file=None)
        self.assertEqual(settings.copilot_reasoning_effort, "")

    def test_invalid_effort_is_rejected_before_launch(self):
        with patch.dict(os.environ, {"COPILOT_REASONING_EFFORT": "typo"}, clear=True):
            with self.assertRaises(ValidationError):
                Settings(_env_file=None)

    def test_deployment_scripts_use_the_same_defaults(self):
        for filename in ("deploy.sh", "update.sh"):
            source = (ROOT / filename).read_text(encoding="utf-8")
            with self.subTest(script=filename):
                self.assertIn('COPILOT_MODEL="claude-opus-5.5-1m"', source)
                self.assertIn('COPILOT_REASONING_EFFORT="high"', source)
                self.assertIn('"COPILOT_MODEL=$COPILOT_MODEL"', source)
                self.assertIn('"COPILOT_REASONING_EFFORT=$COPILOT_REASONING_EFFORT"', source)


class CliDefaultArgumentsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(patch.object(copilot.settings, "copilot_model", "claude-opus-5.5-1m"))
        self.enterContext(patch.object(copilot.settings, "copilot_reasoning_effort", "high"))
        self.enterContext(patch.object(copilot, "_find_cli", return_value="/mock/copilot"))
        self.enterContext(patch("app.services.blob_storage.restore_data_from_storage", return_value=0))
        self.calls = []

        async def fake_stream(args):
            self.calls.append(args)
            yield "test response"

        self.enterContext(patch.object(copilot, "_run_jsonl_stream", fake_stream))

    async def collect(self, runner, **kwargs):
        chunks = [chunk async for chunk in runner("test prompt", **kwargs)]
        self.assertEqual(chunks, ["test response"])
        return self.calls[-1]

    def assert_default_arguments(self, args):
        self.assertEqual(args[args.index("--model") + 1], "claude-opus-5.5")
        self.assertEqual(args[args.index("--context") + 1], "long_context")
        self.assertEqual(args[args.index("--reasoning-effort") + 1], "high")
        self.assertNotIn("claude-opus-5.5-1m", args)

    async def test_code_chat_uses_new_default_and_high_effort(self):
        args = await self.collect(copilot.run_code_chat)
        self.assert_default_arguments(args)

    async def test_plan_mode_uses_new_default_and_high_effort(self):
        args = await self.collect(copilot.run_plan_mode, agent_name="test-agent")
        self.assert_default_arguments(args)
        self.assertEqual(args[args.index("--agent") + 1], "test-agent")

    async def test_explicit_model_override_still_wins(self):
        args = await self.collect(copilot.run_code_chat, model_name="gpt-6.1-sol")
        self.assertEqual(args[args.index("--model") + 1], "gpt-6.1-sol")
        self.assertNotIn("--context", args)
        self.assertEqual(args[args.index("--reasoning-effort") + 1], "high")

    async def test_effort_override_applies_in_both_modes(self):
        with patch.object(copilot.settings, "copilot_reasoning_effort", "medium"):
            for runner in (copilot.run_code_chat, copilot.run_plan_mode):
                args = await self.collect(runner)
                self.assertEqual(args[args.index("--reasoning-effort") + 1], "medium")

    async def test_empty_effort_omits_the_flag_in_both_modes(self):
        with patch.object(copilot.settings, "copilot_reasoning_effort", ""):
            for runner in (copilot.run_code_chat, copilot.run_plan_mode):
                args = await self.collect(runner)
                self.assertNotIn("--reasoning-effort", args)


if __name__ == "__main__":
    unittest.main()