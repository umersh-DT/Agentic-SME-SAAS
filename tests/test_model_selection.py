import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient
import litellm

from src.core.agent_loop import LiteLLMAgent, missing_model_api_key, resolve_model_name
from src.core.storage_models import TenantDatabaseManager, load_default_settings
from src.gateway.main import app
from src.skills.whatsapp_reply import WhatsAppReplySkill


class TestModelSelection(unittest.IsolatedAsyncioTestCase):
    def test_llm_model_env_overrides_config_default(self):
        settings = load_default_settings()
        with patch.dict(os.environ, {"LLM_MODEL": ""}):
            self.assertEqual(resolve_model_name(settings), "openai/gpt-4o-mini")
        with patch.dict(os.environ, {"LLM_MODEL": "gemini/gemini-2.5-flash"}):
            self.assertEqual(resolve_model_name(settings), "gemini/gemini-2.5-flash")

    def test_gemini_model_is_routed_to_gemini_provider(self):
        _, provider, _, _ = litellm.get_llm_provider("gemini/gemini-2.5-flash")
        self.assertEqual(provider, "gemini")

    def test_missing_key_is_detected_per_provider(self):
        with patch.dict(os.environ, {"GEMINI_API_KEY": "", "OPENAI_API_KEY": "sk-test"}):
            self.assertEqual(missing_model_api_key("gemini/gemini-2.5-flash"), "GEMINI_API_KEY")
            self.assertIsNone(missing_model_api_key("openai/gpt-4o-mini"))
        with patch.dict(os.environ, {"GEMINI_API_KEY": "AIza-test", "OPENAI_API_KEY": ""}):
            self.assertIsNone(missing_model_api_key("gemini/gemini-2.5-flash"))
            self.assertEqual(missing_model_api_key("openai/gpt-4o-mini"), "OPENAI_API_KEY")

    def test_app_refuses_to_start_without_key_for_chosen_model(self):
        with patch.dict(os.environ, {"LLM_MODEL": "gemini/gemini-2.5-flash", "GEMINI_API_KEY": ""}):
            with self.assertRaisesRegex(RuntimeError, "needs GEMINI_API_KEY"):
                with TestClient(app):
                    pass

    async def test_gemini_turn_uses_gemini_model_and_records_gemini_cost(self):
        with tempfile.TemporaryDirectory() as data_dir:
            agent = LiteLLMAgent(tenant_id="tenant_gemini_01", base_data_dir=data_dir)
            reply_skill = MagicMock(spec=WhatsAppReplySkill)
            reply_skill.send_reply = AsyncMock(return_value={"success": True, "message_sid": "SM_ok"})

            message = SimpleNamespace(
                content="Hello!", tool_calls=None, to_dict=lambda: {"role": "assistant", "content": "Hello!"}
            )
            response = SimpleNamespace(
                choices=[SimpleNamespace(message=message)],
                usage=SimpleNamespace(prompt_tokens=1_000_000, completion_tokens=1_000_000),
            )
            with patch.dict(os.environ, {"LLM_MODEL": "gemini/gemini-2.5-flash"}), \
                 patch("litellm.acompletion", AsyncMock(return_value=response)) as mock_llm:
                result = await agent.process_user_turn(
                    message_sid="SM_gemini_1", from_number="+971501234567",
                    user_message="Hi", reply_skill=reply_skill,
                )

            self.assertEqual(result["status"], "completed")
            self.assertEqual(mock_llm.call_args.kwargs["model"], "gemini/gemini-2.5-flash")
            db = TenantDatabaseManager("tenant_gemini_01", base_dir=data_dir)
            with db._get_connection() as conn:
                row = conn.execute("SELECT model, cost_usd FROM token_usage;").fetchone()
            # 1M prompt + 1M completion tokens at the configured gemini-2.5-flash rates (0.30 + 2.50)
            self.assertEqual(row["model"], "gemini/gemini-2.5-flash")
            self.assertAlmostEqual(row["cost_usd"], 2.80, places=6)


class TestRateLimits(unittest.IsolatedAsyncioTestCase):
    def _rate_limit(self):
        return litellm.RateLimitError(
            message='geminiException - {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", '
                    '"details": [{"retryDelay": "12s"}]}}',
            llm_provider="gemini", model="gemini-2.5-flash",
        )

    async def _turn(self, side_effect):
        with tempfile.TemporaryDirectory() as data_dir:
            agent = LiteLLMAgent(tenant_id="tenant_rl_01", base_data_dir=data_dir)
            reply_skill = MagicMock(spec=WhatsAppReplySkill)
            reply_skill.send_reply = AsyncMock(return_value={"success": True, "message_sid": "SM_ok"})
            sleep = AsyncMock()
            with patch("litellm.acompletion", AsyncMock(side_effect=side_effect)) as mock_llm, \
                 patch("src.core.agent_loop.asyncio.sleep", sleep):
                result = await agent.process_user_turn(
                    message_sid="SM_rl", from_number="+971501234567", user_message="Hi", reply_skill=reply_skill)
            spend = await TenantDatabaseManager("tenant_rl_01", base_dir=data_dir).get_current_month_cost()
        return result, mock_llm, sleep, reply_skill, spend

    async def test_one_rate_limit_is_retried_after_suggested_wait(self):
        message = SimpleNamespace(content="Hello!", tool_calls=None, to_dict=lambda: {"role": "assistant", "content": "Hello!"})
        ok = SimpleNamespace(choices=[SimpleNamespace(message=message)],
                             usage=SimpleNamespace(prompt_tokens=10, completion_tokens=2))
        result, mock_llm, sleep, reply_skill, _ = await self._turn([self._rate_limit(), ok])
        self.assertEqual(result["reply"], "Hello!")
        self.assertEqual(mock_llm.call_count, 2)
        sleep.assert_awaited_once_with(12.0)

    async def test_repeated_rate_limit_gives_busy_reply(self):
        result, mock_llm, _, reply_skill, spend = await self._turn([self._rate_limit(), self._rate_limit()])
        self.assertEqual(result["reply"], "The AI service is busy right now. Please try again in a minute.")
        self.assertEqual(reply_skill.send_reply.call_args.kwargs["message"],
                         "The AI service is busy right now. Please try again in a minute.")
        self.assertEqual(spend, 0.0)


class TestStartupModelCheck(unittest.IsolatedAsyncioTestCase):
    async def _check(self, side_effect):
        from src.gateway.main import check_ai_model
        with patch("litellm.acompletion", AsyncMock(side_effect=side_effect)) as mock_llm, \
             patch("src.gateway.main.send_platform_alert", AsyncMock(return_value={"email": True})) as alert:
            problem = await check_ai_model("gemini/gemini-2.5-flash-lite")
        return problem, mock_llm, alert

    async def test_retired_model_is_reported_and_emailed(self):
        not_found = litellm.NotFoundError(message="404 models/gemini-2.5-flash-lite is not found",
                                          llm_provider="gemini", model="gemini-2.5-flash-lite")
        problem, mock_llm, alert = await self._check(not_found)
        self.assertEqual(problem, "Google/OpenAI says the AI model 'gemini/gemini-2.5-flash-lite' does not exist or "
                                  "is retired. Set LLM_MODEL in .env to a current model (e.g. "
                                  "gemini/gemini-flash-lite-latest) and run up -d.")
        self.assertEqual(mock_llm.call_args.kwargs["max_tokens"], 1)
        alert.assert_awaited_once_with("[Assistant] AI model not working", problem)

    async def test_working_or_rate_limited_model_is_fine(self):
        ok = SimpleNamespace(choices=[], usage=None)
        rate_limited = litellm.RateLimitError(message="429", llm_provider="gemini", model="x")
        for outcome in (ok, rate_limited):
            problem, _, alert = await self._check([outcome] if outcome is ok else rate_limited)
            self.assertIsNone(problem)
            alert.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
