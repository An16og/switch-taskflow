"""Tests for ambient task detection and the 5 -> 15 context expansion."""

import sys
import unittest
from unittest.mock import MagicMock

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

from models import ConfidenceLevel, ExecutionType, SlackMessage, TaskTriageResult
from agents.worker_agent import WorkerAgent
from llm_gateway import LLMGateway
from bot import should_ambient_triage, should_expand_context


class TestAmbientAndContext(unittest.TestCase):
    def test_unassigned_task_announcements_are_ambient_tasks(self):
        """An announcement with no @mention must still reach the triage LLM."""
        samples = [
            "We need to update the README before the launch",
            "Someone should look into the staging errors",
            "The docs need a new API section",
            "Can someone add a checklist for onboarding?",
            "Let's prepare the release announcement for Friday",
        ]
        for text in samples:
            with self.subTest(text=text):
                self.assertTrue(should_ambient_triage(text))

    def test_plain_banter_is_not_ambient_triage(self):
        for text in ["thanks!", "good morning team", "haha nice", "ok sounds good"]:
            with self.subTest(text=text):
                self.assertFalse(should_ambient_triage(text))

    def test_expand_when_model_asks_for_more_context(self):
        result = TaskTriageResult(
            is_task=False,
            confidence=ConfidenceLevel.LOW,
            confidence_reason="Fragment needs earlier context",
            needs_more_context=True,
        )
        self.assertTrue(should_expand_context(result, "by Friday please", 5, 15))

    def test_expand_short_fragment_when_older_messages_exist(self):
        result = TaskTriageResult(
            is_task=False,
            confidence=ConfidenceLevel.LOW,
            confidence_reason="not enough context",
        )
        self.assertTrue(should_expand_context(result, "and also add the auth section", 5, 15))

    def test_no_expand_when_static_window_already_found_task(self):
        result = TaskTriageResult(
            is_task=True,
            confidence=ConfidenceLevel.HIGH,
            confidence_reason="Clear task",
            action_summary="Update README",
            execution_type=ExecutionType.AUTO_EXECUTE,
        )
        self.assertFalse(should_expand_context(result, "update the README", 5, 15))

    def test_worker_receives_the_full_expanded_window(self):
        """The worker must not silently truncate the handoff window to 5."""
        mock_gateway = MagicMock(spec=LLMGateway)
        worker = WorkerAgent(gateway=mock_gateway)
        messages = [
            SlackMessage(user_id="U1", user_name="Alice", text=f"context message {i}", ts=str(i))
            for i in range(1, 11)
        ]
        triage = TaskTriageResult(
            is_task=True,
            confidence=ConfidenceLevel.HIGH,
            confidence_reason="Split task",
            action_summary="Update README with new auth section",
            execution_type=ExecutionType.AUTO_EXECUTE,
        )
        mock_gateway.generate_structured.return_value = MagicMock(
            artifact_title="README update",
            artifact_content="# README\n\nAuth section added.",
            suggested_action="Review",
        )

        worker.execute_task(triage, messages)

        prompt = mock_gateway.generate_structured.call_args.kwargs["prompt"]
        self.assertIn("context message 1", prompt)
        self.assertIn("context message 10", prompt)


if __name__ == "__main__":
    unittest.main()
