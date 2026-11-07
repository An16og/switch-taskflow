"""Unit and integration tests for TaskFlow pipeline models and Slack cards."""

import json
import sys
import unittest
from pathlib import Path

# Ensure UTF-8 output on Windows console
if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')

from models import TaskTriageResult, ConfidenceLevel, ExecutionType, SlackMessage
from slack_cards import (
    build_human_todo_card,
    build_auto_execute_card,
    build_assigner_card,
    build_receipt_blocks,
    parse_button_payload,
    open_slack_dm,
    post_slack_dm
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

    def test_switch_client_slack_bridge_detection(self):
        """Verify SwitchAgentClient correctly detects whether Slack is the Switch-connected app."""
        from unittest.mock import patch, MagicMock
        from switch_client import SwitchAgentClient

        client = SwitchAgentClient(
            server_url="http://mock-switch:52422",
            agent_id="test_agent",
            api_key="test_key",
            room_id="room_123"
        )

        # Case 1: Room has an active Slack bridge managed by Switch
        mock_slack_resp = MagicMock()
        mock_slack_resp.status_code = 200
        mock_slack_resp.json.return_value = {
            "result": {
                "bridge_id": "br_slack_001",
                "bridge_display_name": "Slack Workspace",
                "external_channel_id": "C012345678"
            }
        }
        with patch("requests.post", return_value=mock_slack_resp):
            info = client.get_room_bridge_info("room_123")
            self.assertTrue(info["connected"])
            self.assertTrue(info["is_slack"])
            self.assertEqual(info["bridge_name"], "Slack Workspace")
            self.assertEqual(info["external_channel_id"], "C012345678")
            self.assertTrue(client.is_slack_connected("room_123"))

        # Case 2: Room is in pure Switch workroom mode (no external bridge)
        mock_native_resp = MagicMock()
        mock_native_resp.status_code = 200
        mock_native_resp.json.return_value = {
            "result": {
                "bridge_id": None,
                "bridge_display_name": None,
                "external_channel_id": None
            }
        }
        with patch("requests.post", return_value=mock_native_resp):
            info = client.get_room_bridge_info("room_123")
            self.assertFalse(info["connected"])
            self.assertFalse(info["is_slack"])
            self.assertFalse(client.is_slack_connected("room_123"))

    def test_switch_owns_slack_connection_no_parallel_bolt(self):
        """Ensure TaskFlow config and bot do not mandate or initialize a parallel Slack Bolt app."""
        import config
        from unittest.mock import patch
        # Verify that SLACK_BOT_TOKEN is not a required env variable in check_env()
        with patch.object(config, "GEMINI_API_KEY", "mock-gemini-key"):
            self.assertTrue(config.check_env())
        # Verify that config does not mandate a parallel Bolt socket mode
        self.assertFalse(hasattr(config, "SLACK_APP_TOKEN"))

        # Verify that requirements dropped unused slack-bolt while retaining slack-sdk for cards
        req_path = Path(__file__).resolve().parent / "requirements.txt"
        with open(req_path, "r", encoding="utf-8") as f:
            reqs = f.read()
            self.assertNotIn("slack-bolt", reqs)
            self.assertIn("slack-sdk", reqs)


    def test_open_slack_dm_resolves_dm_channel(self):
        """Verify open_slack_dm calls conversations.open and retrieves the DM channel ID."""
        from unittest.mock import MagicMock
        mock_client = MagicMock()
        mock_client.conversations_open.return_value = {
            "ok": True,
            "channel": {"id": "D0123456789"}
        }

        channel = open_slack_dm(mock_client, "U0CFRP39TRS")
        mock_client.conversations_open.assert_called_once_with(users=["U0CFRP39TRS"])
        self.assertEqual(channel, "D0123456789")

    def test_open_slack_dm_bypasses_for_existing_channel(self):
        """Verify open_slack_dm skips conversations.open if channel is already a channel ID."""
        from unittest.mock import MagicMock
        mock_client = MagicMock()

        # D... is already a direct message channel
        dm_chan = open_slack_dm(mock_client, "D0CFSUM8SKB")
        self.assertEqual(dm_chan, "D0CFSUM8SKB")
        mock_client.conversations_open.assert_not_called()

        # C... is already a public channel
        pub_chan = open_slack_dm(mock_client, "C012345678")
        self.assertEqual(pub_chan, "C012345678")
        mock_client.conversations_open.assert_not_called()

    def test_open_slack_dm_fallback_on_api_error(self):
        """Verify open_slack_dm gracefully falls back to user_id if conversations.open fails."""
        from unittest.mock import MagicMock
        mock_client = MagicMock()
        mock_client.conversations_open.side_effect = Exception("missing_scope: im:write")

        fallback_chan = open_slack_dm(mock_client, "U0CFRP39TRS")
        self.assertEqual(fallback_chan, "U0CFRP39TRS")

    def test_post_slack_dm_opens_conversation_first(self):
        """Verify post_slack_dm opens DM channel first before invoking chat_postMessage."""
        from unittest.mock import MagicMock
        mock_client = MagicMock()
        mock_client.conversations_open.return_value = {
            "ok": True,
            "channel": {"id": "D099887766"}
        }
        mock_client.chat_postMessage.return_value = {
            "ok": True,
            "channel": "D099887766",
            "ts": "1234567890.123456"
        }

        resp = post_slack_dm(
            client=mock_client,
            user_id="U0CFRP39TRS",
            text="TaskFlow Update",
            blocks=[{"type": "section"}]
        )

        mock_client.conversations_open.assert_called_once_with(users=["U0CFRP39TRS"])
        mock_client.chat_postMessage.assert_called_once_with(
            channel="D099887766",
            text="TaskFlow Update",
            blocks=[{"type": "section"}]
        )
        self.assertEqual(resp["channel"], "D099887766")

    def test_on_switch_use_send_targeted_message_instead_of_slack_dms(self):
        """Verify that on Switch, agents use send_targeted_message natively to target users."""
        from unittest.mock import patch, MagicMock
        from switch_client import SwitchAgentClient

        client = SwitchAgentClient(
            server_url="http://mock-switch:52422",
            agent_id="test_worker_agent",
            api_key="test_worker_key",
            room_id="room_switch_001"
        )

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"result": {"id": "msg_targeted_123", "body": "Targeted task"}}

        with patch("requests.post", return_value=mock_resp) as mock_post:
            res = client.send_targeted_message(
                body="Task delivery receipt for @Anuj",
                target_names=["Anuj"],
                room_id="room_switch_001"
            )
            self.assertEqual(res.get("id"), "msg_targeted_123")
            mock_post.assert_called_once()
            called_args, called_kwargs = mock_post.call_args
            self.assertIn("/agents/test_worker_agent/ops/send_targeted_message", called_args[0])
            self.assertEqual(called_kwargs["json"]["target_names"], ["Anuj"])
            self.assertEqual(called_kwargs["json"]["body"], "Task delivery receipt for @Anuj")


if __name__ == "__main__":
    unittest.main()
