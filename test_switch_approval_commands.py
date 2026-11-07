"""Unit tests for Switch Room Approval Commands & AUTO_EXECUTE Wiring.

Verifies:
1. AUTO_EXECUTE is wired to the approval store on the Switch path.
2. Worker posts a short proposal card + approval_id, NOT the final broadcast.
3. Commands like '@taskflow-worker approve appr_...' and '@taskflow-worker dismiss appr_...'
   execute with strict assignee/role authorization checks from interactions.py.
4. Unauthorized users are blocked with an informative refusal message.
"""

import asyncio
import json
import unittest
from unittest.mock import MagicMock, patch, AsyncMock

from approvals import PendingApprovalStore, ApprovalStatus, default_approval_store
from models import (
    ConfidenceLevel,
    ExecutionType,
    TaskHandoffMessage,
    WorkerExecutionResult,
    build_todoist_payload,
)
from bot import handle_worker_message, parse_approval_command
from interactions import InteractionDispatcher
from room_tasks import default_room_task_store


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
            ("confirm", "appr_11223344")
        )
        self.assertEqual(
            parse_approval_command("@taskflow-worker save appr_55667788"),
            ("save", "appr_55667788")
        )
        self.assertEqual(
            parse_approval_command("@taskflow-worker save_to_todoist appr_55667788"),
            ("save", "appr_55667788")
        )

        # Directly addressed without @-prefix
        self.assertEqual(
            parse_approval_command("approve appr_99887766", addressed=True),
            ("approve", "appr_99887766")
        )
        self.assertEqual(
            parse_approval_command("save appr_99887766", addressed=True),
            ("save", "appr_99887766")
        )
        self.assertEqual(
            parse_approval_command("save_to_todoist appr_aabbccdd", addressed=True),
            ("save", "appr_aabbccdd")
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
            self.assertIn("@taskflow-worker save", body_posted)
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

    def test_medium_confidence_human_todo_asks_assigner_and_keeps_checklist(self):
        """Verifies that medium-confidence tasks hold for assigner confirmation and preserve worker checklist."""
        mock_client = MagicMock()
        mock_client.agent_id = "agent_worker_1"
        mock_client.room_id = "room_beta"
        mock_client.send_targeted_message = MagicMock()

        handoff = TaskHandoffMessage(
            task_title="Investigate high latency in cache cluster",
            execution_type=ExecutionType.HUMAN_TODO,
            confidence=ConfidenceLevel.MEDIUM,
            assignee_name="Alice",
            assignee_id="U_ALICE",
            assigner_name="Priya",
            assigner_id="U_PRIYA",
            room_id="room_beta",
            thread_id="msg_200",
            requester_name="Priya",
        )

        handoff_envelope = (
            f"@taskflow-worker <!-- TASKFLOW_HANDOFF: {handoff.model_dump_json()} -->"
        )
        payload = {
            "body": handoff_envelope,
            "sender": "agent_triage_1",
            "sender_name": "taskflow-triage",
            "room_id": "room_beta",
            "thread_id": "msg_200",
        }

        worker_plan = "* [ ] Step 1: Check Redis hit ratios\n* [ ] Step 2: Validate TCP keepalive"
        with patch("bot.worker_executor.execute_task") as mock_exec:
            mock_exec.return_value = WorkerExecutionResult(
                artifact_title="Action Plan",
                artifact_content=worker_plan,
                execution_notes="Generated action plan"
            )

            asyncio.run(handle_worker_message(mock_client, payload))

        # Assigner Priya must be targeted; Assignee Alice must NOT be paged yet
        mock_client.send_targeted_message.assert_called_once()
        call_args = mock_client.send_targeted_message.call_args[1]
        self.assertEqual(call_args["target_names"], ["Priya"])
        self.assertNotIn("Alice", call_args["target_names"])

        # Message must contain the preserved worker checklist
        body = call_args["body"]
        self.assertIn("Did you mean to assign this?", body)
        self.assertIn("Step 1: Check Redis hit ratios", body)
        self.assertIn("Step 2: Validate TCP keepalive", body)
        self.assertIn("@taskflow-worker confirm", body)

        # Pending approval must be stored with action_type="ASSIGNMENT" and worker checklist preserved
        import re
        m = re.search(r"`(appr_[a-f0-9]+)`", body)
        self.assertTrue(m, "Card must include approval_id")
        appr_id = m.group(1)

        appr = default_approval_store.get_approval(appr_id)
        self.assertIsNotNone(appr)
        self.assertEqual(appr.action_type, "ASSIGNMENT")
        self.assertEqual(appr.artifact_content, worker_plan)
        self.assertEqual(appr.status, ApprovalStatus.PENDING)

    def test_assigner_can_confirm_medium_confidence_assignment_and_page_assignee(self):
        """Verifies that assigner running confirm executes side effects and pages assignee with worker checklist."""
        worker_plan = "* [ ] Step 1: Inspect database locks\n* [ ] Step 2: Kill runaway query"
        appr = default_approval_store.create_approval(
            action_summary="Fix database query bottleneck",
            artifact_content=worker_plan,
            artifact_title="Task Assignment: Fix database query bottleneck",
            action_type="ASSIGNMENT",
            assignee_name="Alice",
            assignee_id="U_ALICE",
            assigner_name="Priya",
            assigner_id="U_PRIYA",
            room_id="room_gamma",
            allowed_user_names=["Priya"],
            allowed_user_ids=["U_PRIYA"],
            allowed_roles=["owner", "admin", "lead", "operator"],
        )

        mock_client = MagicMock()
        mock_client.agent_id = "agent_worker_1"
        mock_client.room_id = "room_gamma"
        mock_client.post_message = MagicMock()
        mock_client.send_targeted_message = MagicMock()

        cmd_payload = {
            "body": f"@taskflow-worker confirm {appr.approval_id}",
            "sender": "U_PRIYA",
            "sender_name": "Priya",
            "room_id": "room_gamma",
            "thread_id": "msg_201",
        }

        asyncio.run(handle_worker_message(mock_client, cmd_payload))

        # Assignee Alice must be paged via targeted message with worker checklist
        mock_client.send_targeted_message.assert_called_once()
        target_call = mock_client.send_targeted_message.call_args[1]
        self.assertEqual(target_call["target_names"], ["Alice"])
        self.assertIn("Step 1: Inspect database locks", target_call["body"])
        self.assertIn("Step 2: Kill runaway query", target_call["body"])

        # Room receipt must be posted
        mock_client.post_message.assert_called_once()
        post_call = mock_client.post_message.call_args[1]["body"]
        self.assertIn("Task Confirmed & Assigned", post_call)
        self.assertIn("@Alice", post_call)

        # Status must be resolved to APPROVED
        resolved = default_approval_store.get_approval(appr.approval_id)
        self.assertEqual(resolved.status, ApprovalStatus.APPROVED)
        self.assertEqual(resolved.resolved_by, "Priya")

    def test_build_todoist_payload_schema_normalization(self):
        """Verifies AUTO_EXECUTE and HUMAN_TODO generate identical normalized Todoist payload schemas."""
        # 1. AUTO_EXECUTE schema test
        auto_payload = build_todoist_payload(
            title="Release Notes v1.5",
            assignee_name="@Alice",
            assigner_name="@Priya",
            context_notes="Discussed in sprint retro",
            content_body="# Features\n- Dark mode support\n- Faster cold boot",
            deadline="Friday 5 PM",
            external_id="sw_task_auto_101",
            execution_type="AUTO_EXECUTE",
        )

        self.assertEqual(auto_payload["content"], "[Alice] Release Notes v1.5")
        self.assertIn("Assigned by: @Priya", auto_payload["description"])
        self.assertIn("Context: Discussed in sprint retro", auto_payload["description"])
        self.assertIn("Draft Deliverable:\n# Features\n- Dark mode support\n- Faster cold boot", auto_payload["description"])
        self.assertEqual(auto_payload["due_string"], "Friday 5 PM")
        self.assertEqual(auto_payload["external_id"], "sw_task_auto_101")

        # 2. HUMAN_TODO schema test
        human_payload = build_todoist_payload(
            title="Refactor auth service tokens",
            assignee_name="Bob",
            assigner_name="Priya",
            context_notes="Security audit recommendation",
            content_body="* [ ] Rotate signing key\n* [ ] Update token TTL",
            deadline="tomorrow",
            external_id="sw_task_human_202",
            execution_type="HUMAN_TODO",
        )

        self.assertEqual(human_payload["content"], "[Bob] Refactor auth service tokens")
        self.assertIn("Assigned by: @Priya", human_payload["description"])
        self.assertIn("Context: Security audit recommendation", human_payload["description"])
        self.assertIn("Action Plan Checklist:\n* [ ] Rotate signing key\n* [ ] Update token TTL", human_payload["description"])
        self.assertEqual(human_payload["due_string"], "tomorrow")
        self.assertEqual(human_payload["external_id"], "sw_task_human_202")

        # 3. Fallbacks and stripping edge cases
        fallback_payload = build_todoist_payload(
            title="",
            assignee_name="@@",
            assigner_name=None,
            context_notes="",
            content_body=None,
        )
        self.assertEqual(fallback_payload["content"], "[Team] Untitled Task")
        self.assertIn("Assigned by: @Team Member", fallback_payload["description"])
        self.assertIn("Context: Captured via TaskFlow on Switch", fallback_payload["description"])

    def test_auto_execute_save_command_records_room_task_and_syncs_todoist(self):
        """Verifies @taskflow-worker save records native room task with AUTO_EXECUTE and syncs Todoist with normalized schema."""
        appr = default_approval_store.create_approval(
            action_summary="Draft v1.5 Release Notes",
            artifact_content="# Release Notes v1.5\n- Added dark mode\n- Fixed memory leak",
            artifact_title="Release Notes v1.5",
            action_type="AUTO_EXECUTE",
            assignee_name="Alice",
            assignee_id="U_ALICE",
            assigner_name="Priya",
            assigner_id="U_PRIYA",
            room_id="room_save_test",
            deadline="Friday 5 PM",
        )

        mock_client = MagicMock()
        mock_client.agent_id = "agent_worker_1"
        mock_client.room_id = "room_save_test"
        mock_client.post_message = MagicMock()
        mock_client.send_targeted_message = MagicMock()

        mock_todoist = MagicMock()
        mock_todoist.create_task.return_value = {
            "id": "todo_12345",
            "url": "https://todoist.com/app/task/todo_12345"
        }

        cmd_payload = {
            "body": f"@taskflow-worker save {appr.approval_id}",
            "sender": "U_ALICE",
            "sender_name": "Alice",
            "room_id": "room_save_test",
            "thread_id": "msg_save_1",
        }

        with patch("bot.todoist", mock_todoist):
            asyncio.run(handle_worker_message(mock_client, cmd_payload))

        # Check Todoist create_task was called with normalized schema
        mock_todoist.create_task.assert_called_once()
        todo_kwargs = mock_todoist.create_task.call_args[1]
        self.assertEqual(todo_kwargs["content"], "[Alice] Draft v1.5 Release Notes")
        self.assertIn("Assigned by: @Alice", todo_kwargs["description"])
        self.assertIn("Draft Deliverable:\n# Release Notes v1.5", todo_kwargs["description"])
        self.assertEqual(todo_kwargs["due_string"], "Friday 5 PM")
        self.assertTrue(todo_kwargs["external_id"].startswith("sw_task_"))

        # Check native Switch room task was recorded with execution_type="AUTO_EXECUTE"
        room_task = default_room_task_store.get_task(todo_kwargs["external_id"])
        self.assertIsNotNone(room_task)
        self.assertEqual(room_task.execution_type, "AUTO_EXECUTE")
        self.assertEqual(room_task.title, "Draft v1.5 Release Notes")
        self.assertEqual(room_task.todoist_url, "https://todoist.com/app/task/todo_12345")

        # Check delivery receipt posted to room
        mock_client.post_message.assert_called()
        calls = [c[1]["body"] for c in mock_client.post_message.call_args_list]
        receipt_text = " ".join(calls)
        self.assertIn("Deliverable Saved to Workroom & Todoist", receipt_text)
        self.assertIn("View in Todoist", receipt_text)

        # Assignee targeted notification should NOT fire for AUTO_EXECUTE saves
        mock_client.send_targeted_message.assert_not_called()

        # Approval status should be resolved to APPROVED
        resolved = default_approval_store.get_approval(appr.approval_id)
        self.assertEqual(resolved.status, ApprovalStatus.APPROVED)

    def test_interaction_dispatcher_save_button_action(self):
        """Verifies InteractionDispatcher handles 'save' action from interactive button."""
        appr = default_approval_store.create_approval(
            action_summary="Security Checklist v2",
            artifact_content="1. Enforce 2FA\n2. Rotate root keys",
            artifact_title="Security Checklist",
            action_type="AUTO_EXECUTE",
            assignee_name="Alice",
            assignee_id="U_ALICE",
            assigner_name="Priya",
            assigner_id="U_PRIYA",
            room_id="room_btn_save",
        )

        mock_todoist = MagicMock()
        mock_todoist.create_task.return_value = {
            "id": "todo_btn_999",
            "url": "https://todoist.com/app/task/todo_btn_999"
        }

        dispatcher = InteractionDispatcher(
            approval_store=default_approval_store,
            todoist_client=mock_todoist,
        )

        res = dispatcher.handle_interaction(
            payload={
                "user": {"id": "U_ALICE", "username": "Alice"},
                "actions": [
                    {
                        "action_id": "btn_accept_task",
                        "value": json.dumps({"approval_id": appr.approval_id, "action": "save"}),
                    }
                ],
            }
        )

        self.assertTrue(res["success"])
        self.assertEqual(res["display_status"], "SAVED")
        self.assertEqual(res["status"], "APPROVED")
        self.assertIsNotNone(res["room_task"])
        self.assertEqual(res["room_task"].execution_type, "AUTO_EXECUTE")
        self.assertEqual(res["todoist_url"], "https://todoist.com/app/task/todo_btn_999")

        # Card block should contain deliverable saved header
        block_text = str(res["blocks"])
        self.assertIn("Deliverable Saved to Todoist & Workroom by @Alice", block_text)


if __name__ == "__main__":
    unittest.main()
