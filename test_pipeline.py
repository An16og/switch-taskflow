"""Unit and integration tests for TaskFlow pipeline models and Slack cards."""

import json
import sys
import unittest

# Ensure UTF-8 output on Windows console
if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')

from models import TaskTriageResult, ConfidenceLevel, ExecutionType, SlackMessage
from slack_cards import (
    build_human_todo_card,
    build_auto_execute_card,
    build_assigner_card,
    build_receipt_blocks,
    parse_button_payload
)
from approvals import default_approval_store


class TestTaskPipeline(unittest.TestCase):
    def test_task_triage_result_validation(self):
        """Verify TaskTriageResult Pydantic model validation."""
        task = TaskTriageResult(
            is_task=True,
            confidence=ConfidenceLevel.HIGH,
            confidence_reason="Explicit request with deadline",
            action_summary="Fix staging auth token bug",
            assignee_id="U123456",
            assignee_name="Rohit",
            deadline="Tomorrow 5 PM",
            context_notes="Demo is on Friday",
            execution_type=ExecutionType.HUMAN_TODO,
            needs_more_context=False
        )
        self.assertTrue(task.is_task)
        self.assertEqual(task.confidence, ConfidenceLevel.HIGH)
        self.assertEqual(task.action_summary, "Fix staging auth token bug")
        self.assertEqual(task.execution_type, ExecutionType.HUMAN_TODO)

    def test_human_todo_card_and_approval_payload(self):
        """Verify build_human_todo_card produces valid blocks with lightweight approval payload."""
        blocks = build_human_todo_card(
            action_summary="Fix staging auth token bug",
            assigner_id="U999999",
            assignee_id="U123456",
            assignee_name="Rohit",
            action_plan="1. Reproduce auth token expiry\n2. Patch refresh logic",
            deadline="Tomorrow 5 PM",
            context_notes="Demo is on Friday",
            permalink="https://slack.com/archives/C123/p123456789",
            channel_id="C123",
            message_ts="123456789.00"
        )
        self.assertGreaterEqual(len(blocks), 4)

        # Check action elements
        action_block = next(b for b in blocks if b.get("type") == "actions")
        action_elem = action_block["elements"][0]
        btn_val = action_elem["value"]

        # Payload is lightweight JSON under 100 characters
        self.assertLess(len(btn_val), 100)
        payload = parse_button_payload(btn_val)
        self.assertIn("approval_id", payload)
        self.assertEqual(payload["action"], "accept")

        # Approval store contains full action details
        appr = default_approval_store.get_approval(payload["approval_id"])
        self.assertIsNotNone(appr)
        self.assertEqual(appr.action_summary, "Fix staging auth token bug")
        self.assertIn("Patch refresh logic", appr.artifact_content)

    def test_auto_execute_card_generation(self):
        """Verify build_auto_execute_card produces valid blocks."""
        blocks = build_auto_execute_card(
            action_summary="Draft v1.3 release notes",
            artifact_title="Release Notes v1.3",
            artifact_content="# Release Notes v1.3\n- Feature updates",
            assigner_id="U999999",
            category="Release Notes"
        )
        self.assertGreaterEqual(len(blocks), 4)
        action_block = next(b for b in blocks if b.get("type") == "actions")
        self.assertLess(len(action_block["elements"][0]["value"]), 100)

    def test_assigner_card_generation(self):
        """Verify build_assigner_card generation."""
        assigner_blocks = build_assigner_card(
            action_summary="Update documentation",
            target_assignee="Alex",
            target_assignee_id="U555555",
            assigner_id="U999999",
            deadline="EOD",
            context_notes="Client onboarding",
            permalink="https://slack.com/archives/C123/p123"
        )
        self.assertGreaterEqual(len(assigner_blocks), 3)
        action_block = next(b for b in assigner_blocks if b.get("type") == "actions")
        self.assertLess(len(action_block["elements"][0]["value"]), 100)

    def test_receipt_blocks_generation(self):
        """Verify build_receipt_blocks generation."""
        receipt = build_receipt_blocks(
            action_summary="Fix staging auth token bug",
            deadline="Tomorrow 5 PM",
            permalink="https://slack.com/archives/C123/p123",
            todoist_url="https://todoist.com/app/task/456"
        )
        self.assertEqual(len(receipt), 1)
        text = receipt[0]["text"]["text"]
        self.assertIn("Task added to Todoist!", text)
        self.assertIn("Fix staging auth token bug", text)


if __name__ == "__main__":
    unittest.main()
