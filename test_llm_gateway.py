"""Unit tests for LLMGateway retries/backoff, Claude structured JSON, and model-id probe."""

import sys
import unittest
from unittest.mock import MagicMock, patch

if hasattr(sys.stdout, 'reconfigure') and sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')

from models import TaskTriageResult, ConfidenceLevel, ExecutionType, SlackMessage
from llm_gateway import (
    LLMGateway,
    is_transient_error,
    execute_with_retry,
    extract_json_dict,
)
from agents.triage_agent import TriageAgent


class DummyTransientError(Exception):
    def __init__(self, message="Rate limit exceeded", status_code=429):
        super().__init__(message)
        self.status_code = status_code


class DummyNonTransientError(Exception):
    def __init__(self, message="Invalid model ID", status_code=404):
        super().__init__(message)
        self.status_code = status_code


class TestLLMGatewayRetriesAndBackoff(unittest.TestCase):

    def test_transient_error_detection(self):
        """Verifies transient error detection on HTTP status codes and error messages."""
        # Status code transient
        self.assertTrue(is_transient_error(DummyTransientError(status_code=429)))
        self.assertTrue(is_transient_error(DummyTransientError(status_code=500)))
        self.assertTrue(is_transient_error(DummyTransientError(status_code=503)))
        self.assertTrue(is_transient_error(DummyTransientError(status_code=529)))

        # Substring transient
        self.assertTrue(is_transient_error(Exception("429 Too Many Requests")))
        self.assertTrue(is_transient_error(Exception("ResourceExhausted: quota exceeded")))
        self.assertTrue(is_transient_error(Exception("Server overloaded, try again later")))
        self.assertTrue(is_transient_error(Exception("Connection reset by peer")))

        # Non-transient
        self.assertFalse(is_transient_error(DummyNonTransientError(status_code=404)))
        self.assertFalse(is_transient_error(DummyNonTransientError("401 Unauthorized", status_code=401)))
        self.assertFalse(is_transient_error(ValueError("Invalid schema field")))

    def test_retry_with_backoff_recovers_after_transient_failures(self):
        """Verifies execute_with_retry retries transient errors and succeeds when backend recovers."""
        attempts = 0

        def flaky_call():
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise DummyTransientError("Temporary 429 rate limit", status_code=429)
            return "SUCCESS_DATA"

        with patch("time.sleep") as mock_sleep:
            result = execute_with_retry(
                flaky_call,
                provider_name="TestProvider",
                max_retries=3,
                initial_backoff=0.1,
            )
            self.assertEqual(result, "SUCCESS_DATA")
            self.assertEqual(attempts, 3)
            self.assertEqual(mock_sleep.call_count, 2)

    def test_retry_exhaustion_raises_final_error(self):
        """Verifies execute_with_retry raises after exceeding max_retries."""
        attempts = 0

        def always_failing():
            nonlocal attempts
            attempts += 1
            raise DummyTransientError("Continuous 503 outage", status_code=503)

        with patch("time.sleep"):
            with self.assertRaises(DummyTransientError):
                execute_with_retry(
                    always_failing,
                    provider_name="TestProvider",
                    max_retries=2,
                    initial_backoff=0.1,
                )
            self.assertEqual(attempts, 3)  # Initial attempt + 2 retries

    def test_non_transient_error_fails_immediately_without_retry(self):
        """Verifies non-transient errors (e.g. 404 Model Not Found) fail immediately."""
        attempts = 0

        def bad_model_call():
            nonlocal attempts
            attempts += 1
            raise DummyNonTransientError("Model not found", status_code=404)

        with patch("time.sleep") as mock_sleep:
            with self.assertRaises(DummyNonTransientError):
                execute_with_retry(bad_model_call, provider_name="TestProvider", max_retries=3)
            self.assertEqual(attempts, 1)
            mock_sleep.assert_not_called()


class TestClaudeStructuredJSON(unittest.TestCase):

    def test_extract_json_dict_variations(self):
        """Verifies extract_json_dict handles markdown fences, whitespace, and clean JSON."""
        # Clean JSON
        raw = '{"is_task": true, "action_summary": "Draft notes"}'
        self.assertEqual(extract_json_dict(raw), {"is_task": True, "action_summary": "Draft notes"})

        # Markdown fenced
        fenced = "```json\n{\n  \"is_task\": false,\n  \"confidence\": \"LOW\"\n}\n```"
        self.assertEqual(extract_json_dict(fenced), {"is_task": False, "confidence": "LOW"})

        # Conversational text preamble
        preamble = 'Here is the JSON requested:\n{"is_task": true}\nHope this helps!'
        self.assertEqual(extract_json_dict(preamble), {"is_task": True})

    def test_claude_structured_output_via_tool_calling(self):
        """Verifies Claude tool calling generates validated Pydantic model directly."""
        gateway = LLMGateway()
        mock_client = MagicMock()

        # Mock Claude returning tool_use block with structured input
        mock_tool_use = MagicMock()
        mock_tool_use.type = "tool_use"
        mock_tool_use.name = "return_tasktriageresult"
        mock_tool_use.input = {
            "is_task": True,
            "confidence": "HIGH",
            "confidence_reason": "Direct explicit delegation to Priya",
            "action_summary": "Deploy payment service migration",
            "execution_type": "HUMAN_TODO",
            "execution_category": "DevOps",
            "needs_more_context": False,
        }

        mock_msg = MagicMock()
        mock_msg.content = [mock_tool_use]
        mock_client.messages.create.return_value = mock_msg

        res: TaskTriageResult = gateway._generate_claude_structured(
            anthropic_client=mock_client,
            prompt="Deploy payment service",
            system_prompt="Triage agent prompt",
            response_schema=TaskTriageResult,
        )

        self.assertIsInstance(res, TaskTriageResult)
        self.assertTrue(res.is_task)
        self.assertEqual(res.confidence, ConfidenceLevel.HIGH)
        self.assertEqual(res.action_summary, "Deploy payment service migration")
        self.assertEqual(res.execution_type, ExecutionType.HUMAN_TODO)

    def test_claude_structured_output_via_json_prefill_fallback(self):
        """Verifies Claude falls back to raw JSON prefill if tool calling fails."""
        gateway = LLMGateway()
        mock_client = MagicMock()

        # First tool call raises error
        mock_client.messages.create.side_effect = [
            Exception("Tool use not supported on this endpoint"),
            # Second call (prefill) succeeds returning markdown json
            MagicMock(content=[MagicMock(text='```json\n{"is_task": true, "confidence": "MEDIUM", "confidence_reason": "Implicit task", "action_summary": "Update docs", "execution_type": "AUTO_EXECUTE"}\n```')])
        ]

        res: TaskTriageResult = gateway._generate_claude_structured(
            anthropic_client=mock_client,
            prompt="Update docs please",
            system_prompt="System",
            response_schema=TaskTriageResult,
        )

        self.assertTrue(res.is_task)
        self.assertEqual(res.confidence, ConfidenceLevel.MEDIUM)
        self.assertEqual(res.action_summary, "Update docs")
        self.assertEqual(res.execution_type, ExecutionType.AUTO_EXECUTE)


class TestModelIdProbeAndErrorPrevention(unittest.TestCase):

    def test_startup_probe_identifies_healthy_and_failing_providers(self):
        """Verifies probe_models reports model availability and diagnostics without crashing."""
        gateway = LLMGateway(gemini_model="gemini-test-model", openai_model="gpt-4o-test")

        # Mock gemini healthy, openai bad model
        mock_gemini = MagicMock()
        mock_openai = MagicMock()
        mock_openai.chat.completions.create.side_effect = DummyNonTransientError("Model 'gpt-4o-test' not found (404)")

        gateway._gemini_client = mock_gemini
        gateway._openai_client = mock_openai

        with patch("llm_gateway.GEMINI_API_KEY", "dummy_gemini_key"), \
             patch("llm_gateway.OPENAI_API_KEY", "dummy_openai_key"), \
             patch("llm_gateway.ANTHROPIC_API_KEY", ""):

            results = gateway.probe_models()
            self.assertIn("gemini", results)
            self.assertTrue(results["gemini"]["available"])
            self.assertIsNone(results["gemini"]["error"])

            self.assertIn("openai", results)
            self.assertFalse(results["openai"]["available"])
            self.assertIn("Model 'gpt-4o-test' not found", results["openai"]["error"])

            # Verify active provider summary reports Gemini
            healthy, summary = gateway.probe_active_provider()
            self.assertTrue(healthy)
            self.assertIn("Gemini", summary)

    def test_triage_agent_never_silently_classifies_llm_error_as_not_a_task(self):
        """CRITICAL USER REQUIREMENT:
        When the LLM provider fails, TriageAgent must NOT silently classify it as 'not a task'.
        It must populate .error and explain the failure in confidence_reason.
        """
        mock_gateway = MagicMock()
        mock_gateway.generate_structured.side_effect = RuntimeError(
            "All LLM providers failed: Gemini gemini-test: 404 Model Not Found; OpenAI: 401 Unauthorized"
        )

        triage = TriageAgent(gateway=mock_gateway)
        msg = [SlackMessage(user_id="U1", user_name="Alex", text="Draft release notes for v1.2", ts="100.0")]

        result = triage.triage_conversation(msg)

        # Result is not a task, but error is explicitly populated!
        self.assertFalse(result.is_task)
        self.assertIsNotNone(result.error)
        self.assertIn("All LLM providers failed", result.error)
        self.assertIn("LLM triage execution failed", result.confidence_reason)

    def test_bot_alerts_on_triage_llm_error_instead_of_silent_dismissal(self):
        """Verifies bot.py catches triage_result.error and sends an explicit diagnostic message
        rather than saying 'I did not detect an actionable task'.
        """
        import asyncio
        from bot import handle_triage_message

        # Mock client
        mock_client = MagicMock()
        mock_client.room_id = "test-room"
        mock_client.agent_id = "agent-triage-id"

        # Mock triage classifier to return an error result
        err_result = TaskTriageResult(
            is_task=False,
            confidence=ConfidenceLevel.LOW,
            confidence_reason="LLM triage execution failed: ClientError: 404 Model Not Found",
            error="ClientError: 404 Model 'invalid-model' Not Found",
        )

        payload = {
            "body": "@taskflow draft release notes for v2.0",
            "sender": "U_USER1",
            "sender_name": "Alice",
            "addressed": True,
            "room_id": "test-room",
            "id": "msg_123",
        }

        with patch("bot.triage_classifier.triage_conversation", return_value=err_result):
            asyncio.run(handle_triage_message(mock_client, payload))

            # Verify targeted message was dispatched with Triage Agent Error alert
            mock_client.send_targeted_message.assert_called_once()
            call_kwargs = mock_client.send_targeted_message.call_args.kwargs
            self.assertIn("[Triage Agent Error]", call_kwargs["body"])
            self.assertIn("404 Model 'invalid-model' Not Found", call_kwargs["body"])
            self.assertNotIn("I heard your request, but did not detect an actionable task", call_kwargs["body"])


if __name__ == "__main__":
    unittest.main()
