"""Unit tests for Switch Room Approval Commands & AUTO_EXECUTE Wiring.

Verifies:
1. AUTO_EXECUTE is wired to the approval store on the Switch path.
2. Worker posts a short proposal card + approval_id, NOT the final broadcast.
3. Commands like '@taskflow-worker approve appr_...' and '@taskflow-worker dismiss appr_...'
   execute with strict assignee/role authorization checks from interactions.py.
4. Unauthorized users are blocked with an informative refusal message.
"""

import asyncio
import unittest
from unittest.mock import MagicMock, patch, AsyncMock

from approvals import PendingApprovalStore, ApprovalStatus, default_approval_store
from models import (
    ConfidenceLevel,
    ExecutionType,
    TaskHandoffMessage,
    WorkerExecutionResult,
)
from bot import handle_worker_message, parse_approval_command


class TestSwitchApprovalCommands(unittest.TestCase):

    def test_parse_approval_command(self):
        # Explicit mentions
        self.assertEqual(
            parse_approval_command("@taskflow-worker approve appr_12345678"),
            ("approve", "appr_12345678")
        )
        self.assertEqual(
            parse_approval_command("taskflow-worker dismiss appr_abcdef12"),
            ("dismiss", "appr_abcdef12")
        )
        self.assertEqual(
            parse_approval_command("@taskflow-worker reject appr_fedcba98"),
            ("dismiss", "appr_fedcba98")
        )
        self.assertEqual(
            parse_approval_command("@taskflow-worker confirm appr_11223344"),
            ("approve", "appr_11223344")
        )

        # Directly addressed without @-prefix
        self.assertEqual(
            parse_approval_command("approve appr_99887766", addressed=True),
            ("approve", "appr_99887766")
        )
        self.assertEqual(
            parse_approval_command("dismiss appr_aabbccdd", addressed=True),
            ("dismiss", "appr_aabbccdd")
        )

        # Non-commands
        self.assertIsNone(parse_approval_command("I will approve the PR tomorrow"))
        self.assertIsNone(parse_approval_command("@taskflow-worker please review the code"))

    def test_auto_execute_creates_pending_approval_and_posts_short_card(self):
        """Verifies that AUTO_EXECUTE posts a short card + approval_id, not the final broadcast."""
        mock_client = MagicMock()
        mock_client.agent_id = "agent_worker_1"
        mock_client.room_id = "room_alpha"
        mock_client.post_message = MagicMock()

        handoff = TaskHandoffMessage(
            task_title="Draft v1.5 Release Notes",
            execution_type=ExecutionType.AUTO_EXECUTE,
            confidence=ConfidenceLevel.HIGH,
            assignee_name="Alice",
            assignee_id="U_ALICE",
            assigner_name="Priya",
            assigner_id="U_PRIYA",
            room_id="room_alpha",
            thread_id="msg_100",
            requester_name="Priya",
        )

        handoff_envelope = (
            f"@taskflow-worker <!-- TASKFLOW_HANDOFF: {handoff.model_dump_json()} -->"
        )
        payload = {
            "body": handoff_envelope,
            "sender": "agent_triage_1",
            "sender_name": "taskflow-triage",
            "room_id": "room_alpha",
            "thread_id": "msg_100",
        }

        # Mock worker execution
        with patch("bot.worker_executor.execute_task") as mock_exec:
            mock_exec.return_value = WorkerExecutionResult(
                artifact_title="Release Notes v1.5",
                artifact_content="# Release Notes v1.5\n- Added dark mode\n- Fixed memory leak",
                execution_notes="Generated release notes"
            )

            asyncio.run(handle_worker_message(mock_client, payload))

            # Verify post_message was called with the short proposal card (NOT the final full broadcast)
            mock_client.post_message.assert_called_once()
            call_kwargs = mock_client.post_message.call_args[1]
            body_posted = call_kwargs["body"]

            # Must contain proposal elements:
            self.assertIn("TaskFlow Deliverable Drafted (Pending Approval)", body_posted)
            self.assertIn("Approval ID:", body_posted)
            self.assertIn("@taskflow-worker approve", body_posted)
            self.assertIn("@taskflow-worker dismiss", body_posted)
            self.assertIn("Release Notes v1.5", body_posted)
            # Must NOT be the raw final deliverable
            self.assertNotIn("*(Generated autonomously by @taskflow-worker on Switch Core)*", body_posted)

            # Extract approval_id from body
            import re
            m = re.search(r"`(appr_[a-f0-9]+)`", body_posted)
            self.assertIsNotNone(m)
            appr_id = m.group(1)

            # Verify approval exists in store in PENDING state
            stored = default_approval_store.get_approval(appr_id)
            self.assertIsNotNone(stored)
            self.assertEqual(stored.status, ApprovalStatus.PENDING)
            self.assertEqual(stored.assignee_name, "Alice")
            self.assertEqual(stored.assigner_name, "Priya")
            self.assertIn("Added dark mode", stored.artifact_content)

    def test_authorized_assignee_can_approve_via_command(self):
        """Verifies that @taskflow-worker approve appr_... broadcasts the deliverable if authorized."""
        # 1. Create a pending approval
        appr = default_approval_store.create_approval(
            action_summary="Draft Incident Postmortem",
            artifact_content="# Incident Postmortem: Outage on Nov 7\nRoot cause was Redis OOM.",
            artifact_title="Incident Postmortem",
            action_type="AUTO_EXECUTE",
            assignee_name="Alice",
            assignee_id="U_ALICE",
            assigner_name="Priya",
            assigner_id="U_PRIYA",
            room_id="room_alpha",
        )

        mock_client = MagicMock()
        mock_client.agent_id = "agent_worker_1"
        mock_client.room_id = "room_alpha"
        mock_client.post_message = MagicMock()

        # Alice (Assignee) runs the command
        cmd_payload = {
            "body": f"@taskflow-worker approve {appr.approval_id}",
            "sender": "U_ALICE",
            "sender_name": "Alice",
            "room_id": "room_alpha",
            "thread_id": "msg_101",
        }

        asyncio.run(handle_worker_message(mock_client, cmd_payload))

        # Check post_message was called to publish the deliverable
        mock_client.post_message.assert_called()
        calls = [c[1]["body"] for c in mock_client.post_message.call_args_list]
        published_text = " ".join(calls)

        self.assertIn("Incident Postmortem", published_text)
        self.assertIn("Root cause was Redis OOM", published_text)
        self.assertIn("Approved & posted by @Alice", published_text)

        # Check status is APPROVED in store
        resolved = default_approval_store.get_approval(appr.approval_id)
        self.assertEqual(resolved.status, ApprovalStatus.APPROVED)

    def test_unauthorized_user_blocked_from_approving(self):
        """Verifies that an unauthorized room member cannot approve or broadcast someone else's deliverable."""
        # Create approval locked to Alice & Priya
        appr = default_approval_store.create_approval(
            action_summary="Security Audit Report",
            artifact_content="# Security Audit\nSensitive findings.",
            artifact_title="Security Audit Report",
            action_type="AUTO_EXECUTE",
            assignee_name="Alice",
            assignee_id="U_ALICE",
            assigner_name="Priya",
            assigner_id="U_PRIYA",
            room_id="room_alpha",
        )

        mock_client = MagicMock()
        mock_client.agent_id = "agent_worker_1"
        mock_client.room_id = "room_alpha"
        mock_client.post_message = MagicMock()
        mock_client.get_user_roles.return_value = []  # No admin roles

        # Mallor (unauthorized) tries to approve
        cmd_payload = {
            "body": f"@taskflow-worker approve {appr.approval_id}",
            "sender": "U_MALLORY",
            "sender_name": "Mallory",
            "room_id": "room_alpha",
            "thread_id": "msg_102",
        }

        asyncio.run(handle_worker_message(mock_client, cmd_payload))

        # Check refusal message was posted
        mock_client.post_message.assert_called_once()
        refusal_msg = mock_client.post_message.call_args[1]["body"]
        self.assertIn("Action Refused: Unauthorized", refusal_msg)
        self.assertIn("Mallory", refusal_msg)
        self.assertNotIn("Sensitive findings", refusal_msg)

        # Verify status is STILL PENDING
        still_pending = default_approval_store.get_approval(appr.approval_id)
        self.assertEqual(still_pending.status, ApprovalStatus.PENDING)

    def test_authorized_user_can_dismiss_via_command(self):
        """Verifies that an authorized user can dismiss the deliverable."""
        appr = default_approval_store.create_approval(
            action_summary="Draft Architecture RFC",
            artifact_content="# RFC: Event Bus",
            artifact_title="Architecture RFC",
            action_type="AUTO_EXECUTE",
            assignee_name="Alice",
            assignee_id="U_ALICE",
            assigner_name="Priya",
            assigner_id="U_PRIYA",
            room_id="room_alpha",
        )

        mock_client = MagicMock()
        mock_client.agent_id = "agent_worker_1"
        mock_client.room_id = "room_alpha"
        mock_client.post_message = MagicMock()

        # Priya (Assigner) dismisses it
        cmd_payload = {
            "body": f"@taskflow-worker dismiss {appr.approval_id}",
            "sender": "U_PRIYA",
            "sender_name": "Priya",
            "room_id": "room_alpha",
            "thread_id": "msg_103",
        }

        asyncio.run(handle_worker_message(mock_client, cmd_payload))

        # Check dismissal message
        mock_client.post_message.assert_called_once()
        msg = mock_client.post_message.call_args[1]["body"]
        self.assertIn("TaskFlow Deliverable Dismissed", msg)
        self.assertIn(appr.approval_id, msg)

        # Check status is DISMISSED in store
        dismissed = default_approval_store.get_approval(appr.approval_id)
        self.assertEqual(dismissed.status, ApprovalStatus.DISMISSED)


if __name__ == "__main__":
    unittest.main()
