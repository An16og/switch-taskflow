"""Unit tests for Thread Fidelity and Reliable Agent Message Filtering.

Verifies:
1. Source thread fidelity: keep all replies, handoffs, deliverables, and task receipts
   in the source thread (inspecting thread_id, thread_ts, parent_id, root_id).
2. Reliable self/agent message filtering: inspect IDs (not just names), bot_id, subtype,
   sender_kind, and channel_type to prevent self-echo and message loops.
3. Native RoomTask thread_id persistence across restarts.
"""

import json
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from models import (
    extract_thread_id,
    should_skip_agent_message,
    ConfidenceLevel,
    TaskTriageResult,
    ExecutionType,
)
from room_tasks import RoomTask, RoomTaskStore, TaskStatus
from approvals import PendingApprovalStore, ApprovalStatus
from interactions import InteractionDispatcher


class TestExtractThreadId(unittest.TestCase):
    """Verifies thread identifier resolution across platforms and schemas."""

    def test_prefers_thread_id_over_message_id(self):
        payload = {
            "thread_id": "thread_parent_001",
            "id": "msg_child_002",
            "message_id": "msg_child_002",
        }
        self.assertEqual(extract_thread_id(payload), "thread_parent_001")

    def test_prefers_thread_ts_over_ts(self):
        payload = {
            "thread_ts": "1712345678.000100",
            "ts": "1712345690.000200",
        }
        self.assertEqual(extract_thread_id(payload), "1712345678.000100")

    def test_prefers_parent_id_over_id(self):
        payload = {
            "parent_id": "parent_conv_999",
            "id": "reply_msg_111",
        }
        self.assertEqual(extract_thread_id(payload), "parent_conv_999")

    def test_prefers_root_id_over_message_id(self):
        payload = {
            "root_id": "root_topic_555",
            "message_id": "leaf_msg_777",
        }
        self.assertEqual(extract_thread_id(payload), "root_topic_555")

    def test_falls_back_to_message_id_when_no_thread(self):
        payload = {
            "id": "msg_standalone_888",
        }
        self.assertEqual(extract_thread_id(payload), "msg_standalone_888")

    def test_falls_back_to_ts_when_no_thread(self):
        payload = {
            "ts": "1719999999.000300",
        }
        self.assertEqual(extract_thread_id(payload), "1719999999.000300")

    def test_handles_empty_or_whitespace_values(self):
        payload = {
            "thread_id": "   ",
            "thread_ts": "",
            "id": "msg_valid_444",
        }
        self.assertEqual(extract_thread_id(payload), "msg_valid_444")

    def test_handles_non_dict_payload(self):
        self.assertIsNone(extract_thread_id(None))
        self.assertIsNone(extract_thread_id([]))
        self.assertIsNone(extract_thread_id("invalid"))


class TestShouldSkipAgentMessageTriage(unittest.TestCase):
    """Verifies that Triage reliably skips agent/bot messages using IDs, channel_type, etc."""

    def setUp(self):
        self.mock_client = MagicMock()
        self.mock_client.agent_id = "agent_triage_registered_id"
        self.mock_client.agent_name = "taskflow-triage"

    def test_skips_by_registered_agent_id_even_with_human_name(self):
        # Adversarial or misconfigured frame using a human name with bot's registered ID
        payload = {
            "sender": "agent_triage_registered_id",
            "sender_name": "Bob",
            "body": "@taskflow please write a python script",
        }
        self.assertTrue(should_skip_agent_message(payload, client=self.mock_client, role="triage"))

    def test_skips_by_configured_worker_id(self):
        with patch("models.SWITCH_WORKER_AGENT_ID", "sw_worker_fixed_id"):
            payload = {
                "sender_id": "sw_worker_fixed_id",
                "sender_name": "ArbitraryName",
                "body": "Here is the deliverable",
            }
            self.assertTrue(should_skip_agent_message(payload, client=self.mock_client, role="triage"))

    def test_skips_by_bot_id(self):
        payload = {
            "sender": "U_INTEGRATION",
            "sender_name": "ExternalBot",
            "bot_id": "B0123456789",
            "body": "Build completed successfully",
        }
        self.assertTrue(should_skip_agent_message(payload, client=self.mock_client, role="triage"))

    def test_skips_by_subtype_bot_message(self):
        payload = {
            "sender": "U_SYSTEM",
            "subtype": "bot_message",
            "body": "Notification from CI",
        }
        self.assertTrue(should_skip_agent_message(payload, client=self.mock_client, role="triage"))

    def test_skips_by_channel_type_control_or_system(self):
        for ctype in ("control", "system", "bot", "heartbeat", "notification", "internal"):
            payload = {
                "sender": "U_USER1",
                "sender_name": "Alice",
                "channel_type": ctype,
                "body": "Please triage this task",
            }
            self.assertTrue(
                should_skip_agent_message(payload, client=self.mock_client, role="triage"),
                f"Failed to skip message with channel_type='{ctype}'"
            )

    def test_skips_by_switch_agent_prefix(self):
        payload = {
            "sender": "@switch-agent-autogen-1234",
            "sender_name": "Worker-Node",
            "body": "Task completed",
        }
        self.assertTrue(should_skip_agent_message(payload, client=self.mock_client, role="triage"))

    def test_allows_genuine_human_user_message(self):
        payload = {
            "sender": "U_HUMAN_ALICE",
            "sender_name": "Alice",
            "channel_type": "channel_public",
            "body": "@taskflow please write a benchmark script for redis",
        }
        self.assertFalse(should_skip_agent_message(payload, client=self.mock_client, role="triage"))


class TestShouldSkipAgentMessageWorker(unittest.TestCase):
    """Verifies that Worker skips its own messages but reliably accepts triage handoffs."""

    def setUp(self):
        self.mock_worker = MagicMock()
        self.mock_worker.agent_id = "agent_worker_registered_id"
        self.mock_worker.agent_name = "taskflow-worker"

    def test_worker_skips_own_messages_to_prevent_loops(self):
        payload = {
            "sender": "agent_worker_registered_id",
            "sender_name": "taskflow-worker",
            "body": "🎯 Task Tracked in Switch Room",
        }
        self.assertTrue(should_skip_agent_message(payload, client=self.mock_worker, role="worker"))

    def test_worker_skips_own_messages_even_with_handoff_string(self):
        # Echo of handoff sent by worker itself must still be skipped
        payload = {
            "sender": "agent_worker_registered_id",
            "body": "@taskflow-worker <!-- TASKFLOW_HANDOFF: {} -->",
        }
        self.assertTrue(should_skip_agent_message(payload, client=self.mock_worker, role="worker"))

    def test_worker_permits_triage_handoff(self):
        with patch("models.SWITCH_TRIAGE_AGENT_ID", "sw_triage_id"):
            payload = {
                "sender": "sw_triage_id",
                "sender_name": "taskflow-triage",
                "body": '@taskflow-worker <!-- TASKFLOW_HANDOFF: {"task_title": "Release Notes"} -->',
            }
            self.assertFalse(should_skip_agent_message(payload, client=self.mock_worker, role="worker"))

    def test_worker_skips_triage_chatter_when_not_handoff(self):
        with patch("models.SWITCH_TRIAGE_AGENT_ID", "sw_triage_id"):
            payload = {
                "sender": "sw_triage_id",
                "sender_name": "taskflow-triage",
                "body": "Channel backlog triage is up to date.",
            }
            self.assertTrue(should_skip_agent_message(payload, client=self.mock_worker, role="worker"))

    def test_worker_allows_human_interactive_command(self):
        payload = {
            "sender": "U_LEAD_PRIYA",
            "sender_name": "Priya",
            "channel_type": "channel_public",
            "body": "@taskflow-worker approve appr_998877",
        }
        self.assertFalse(should_skip_agent_message(payload, client=self.mock_worker, role="worker"))


class TestRoomTaskThreadFidelity(unittest.TestCase):
    """Verifies RoomTask stores and restores thread_id across persistence cycles."""

    def setUp(self):
        self.temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".json")
        self.temp_file.write(b'{"tasks": []}')
        self.temp_file.close()
        self.store = RoomTaskStore(storage_path=self.temp_file.name)

    def tearDown(self):
        if os.path.exists(self.temp_file.name):
            os.unlink(self.temp_file.name)

    def test_room_task_persists_and_restores_thread_id(self):
        task = self.store.create_task(
            title="Benchmark database latency",
            room_id="room_perf",
            assignee_name="Anuj",
            thread_id="thread_src_12345",
        )
        self.assertEqual(task.thread_id, "thread_src_12345")

        # Reload store from disk
        reloaded_store = RoomTaskStore(storage_path=self.temp_file.name)
        reloaded_task = reloaded_store.get_task(task.task_id)
        self.assertIsNotNone(reloaded_task)
        self.assertEqual(reloaded_task.thread_id, "thread_src_12345")


class TestInteractionsThreadFidelity(unittest.TestCase):
    """Verifies that interactive confirm/approve dispatches maintain source thread fidelity."""

    def setUp(self):
        self.temp_appr = tempfile.NamedTemporaryFile(delete=False, suffix=".json")
        self.temp_appr.write(b'{"approvals": []}')
        self.temp_appr.close()
        self.approval_store = PendingApprovalStore(storage_path=self.temp_appr.name)

    def tearDown(self):
        if os.path.exists(self.temp_appr.name):
            os.unlink(self.temp_appr.name)

    def test_assignee_forwarding_keeps_source_thread_id(self):
        appr = self.approval_store.create_approval(
            action_summary="Audit Redis latency",
            artifact_content="* [ ] Check slowlog",
            action_type="HUMAN_TODO",
            assignee_name="Anuj",
            assignee_id="U0CFRP39TRS",
            assigner_name="Priya",
            assigner_id="U_PRIYA",
            room_id="room_ops_01",
            message_ts="source_thread_root_777",
        )

        mock_switch = MagicMock()
        mock_switch.send_targeted_message.return_value = {"success": True}

        dispatcher = InteractionDispatcher(
            approval_store=self.approval_store,
            switch_client=mock_switch,
        )

        payload = {
            "user": {"id": "U_PRIYA", "name": "priya"},
            "actions": [{"action_id": "btn_confirm_task", "value": json.dumps({"approval_id": appr.approval_id, "action": "confirm"})}],
            "room_id": "room_ops_01",
            "thread_id": "cmd_thread_999",
        }

        result = dispatcher.handle_interaction(payload)
        self.assertTrue(result["success"])

        # Verify assignee targeted message was sent to the source thread (message_ts), NOT dropped
        mock_switch.send_targeted_message.assert_called_once()
        call_kwargs = mock_switch.send_targeted_message.call_args[1]
        self.assertEqual(call_kwargs["thread_id"], "source_thread_root_777")
        self.assertEqual(call_kwargs["room_id"], "room_ops_01")
        self.assertIn("Anuj", call_kwargs["target_names"])


if __name__ == "__main__":
    unittest.main()
