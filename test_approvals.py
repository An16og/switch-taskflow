"""Unit tests for PendingApprovalStore, authorization bindings, and lightweight Slack button payloads."""

import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from approvals import PendingApprovalStore, ApprovalStatus
from slack_cards import (
    build_auto_execute_card,
    build_human_todo_card,
    build_assigner_card,
    build_approval_resolution_card,
    build_unauthorized_action_card,
    parse_button_payload
)
from interactions import InteractionDispatcher, handle_confirm_assignment


class TestPendingApprovalStore(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.store_file = Path(self.test_dir) / "test_approvals.json"
        self.store = PendingApprovalStore(storage_path=str(self.store_file), ttl_seconds=3600.0)
        self.dispatcher = InteractionDispatcher(approval_store=self.store)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_store_and_retrieve_massive_artifact(self):
        """Verify 10,000+ character artifact is preserved intact without truncation."""
        massive_release_notes = "# Release Notes v2.0.0\n\n" + ("- Feature update item with detailed logs.\n" * 400)
        self.assertGreater(len(massive_release_notes), 10000)

        appr = self.store.create_approval(
            action_summary="Draft v2.0.0 Release Notes",
            artifact_content=massive_release_notes,
            artifact_title="Release Notes v2.0.0",
            action_type="AUTO_EXECUTE",
            assigner_id="U12345",
            room_id="room-99"
        )

        self.assertTrue(appr.approval_id.startswith("appr_"))
        self.assertEqual(appr.status, ApprovalStatus.PENDING)

        # Retrieve
        fetched = self.store.get_approval(appr.approval_id)
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched.artifact_content, massive_release_notes)
        self.assertEqual(len(fetched.artifact_content), len(massive_release_notes))

    def test_button_payload_size_restriction(self):
        """Verify button value payload is strictly < 100 characters even with huge artifacts."""
        huge_content = "X" * 15000  # 15 KB content (would crash Slack if in value)
        blocks = build_auto_execute_card(
            action_summary="Generate Architecture Spec",
            artifact_title="Architecture Spec v1",
            artifact_content=huge_content,
            assigner_id="UADMIN",
            store=self.store
        )

        # Locate actions block
        action_block = next(b for b in blocks if b.get("type") == "actions")
        for btn in action_block["elements"]:
            btn_value = btn["value"]
            # Strict guarantee: payload must be well below Slack's 2,000 char limit
            self.assertLess(len(btn_value), 100, f"Button payload exceeds 100 chars: {len(btn_value)}")

            # Verify it parses properly and links back to the stored artifact
            parsed = parse_button_payload(btn_value)
            self.assertIn("approval_id", parsed)
            self.assertIn("action", parsed)
            self.assertTrue(parsed["approval_id"].startswith("appr_"))

            stored = self.store.get_approval(parsed["approval_id"])
            self.assertIsNotNone(stored)
            self.assertEqual(stored.artifact_content, huge_content)

    def test_status_transitions(self):
        """Test approval transitions from PENDING -> APPROVED / REJECTED."""
        appr = self.store.create_approval(
            action_summary="Deploy staging",
            artifact_content="git checkout main && docker compose up",
            assigner_id="U_MANAGER"
        )
        self.assertEqual(appr.status, ApprovalStatus.PENDING)

        resolved, is_auth, _ = self.store.resolve_approval(
            approval_id=appr.approval_id,
            status=ApprovalStatus.APPROVED,
            user_id="U_MANAGER"
        )
        self.assertTrue(is_auth)
        self.assertEqual(resolved.status, ApprovalStatus.APPROVED)
        self.assertEqual(resolved.resolved_by, "U_MANAGER")
        self.assertIsNotNone(resolved.resolved_at)

        # Confirm persisted on reload
        reloaded_store = PendingApprovalStore(storage_path=str(self.store_file))
        persisted = reloaded_store.get_approval(appr.approval_id)
        self.assertEqual(persisted.status, ApprovalStatus.APPROVED)

    def test_ttl_expiration(self):
        """Verify approvals expire after TTL."""
        short_store = PendingApprovalStore(storage_path=str(self.store_file), ttl_seconds=0.1)
        appr = short_store.create_approval(
            action_summary="Quick task",
            artifact_content="content"
        )
        time.sleep(0.15)
        # Check cleanup
        expired_count = short_store.cleanup_expired()
        self.assertEqual(expired_count, 1)

        fetched = short_store.get_approval(appr.approval_id)
        self.assertEqual(fetched.status, ApprovalStatus.EXPIRED)

    def test_human_todo_and_assigner_cards(self):
        """Verify human_todo and assigner cards generate lightweight button payloads and auth badges."""
        human_blocks = build_human_todo_card(
            action_summary="Review PR #42",
            assigner_id="U100",
            assignee_id="U200",
            action_plan="1. Check migrations\n2. Run load tests",
            store=self.store
        )
        # Verify context badge displays authorized users
        context_block = next(b for b in human_blocks if b.get("type") == "context")
        auth_elem = next(e for e in context_block["elements"] if "Authorized" in e["text"])
        self.assertIn("<@U200>", auth_elem["text"])

        action_block = next(b for b in human_blocks if b.get("type") == "actions")
        for btn in action_block["elements"]:
            self.assertLess(len(btn["value"]), 100)
            parsed = parse_button_payload(btn["value"])
            self.assertTrue(parsed["approval_id"].startswith("appr_"))

        assigner_blocks = build_assigner_card(
            action_summary="Prepare demo deck",
            target_assignee="Sarah",
            target_assignee_id="U200",
            assigner_id="U100",
            store=self.store
        )
        action_block_2 = next(b for b in assigner_blocks if b.get("type") == "actions")
        for btn in action_block_2["elements"]:
            self.assertLess(len(btn["value"]), 100)

    def test_unauthorized_button_click_blocked(self):
        """Verify unauthorized users cannot approve tasks and receive refusal card."""
        # Task assigned to Alice by Priya
        appr = self.store.create_approval(
            action_summary="Draft v1.3 Release Notes",
            artifact_content="# Release Notes v1.3...",
            assigner_id="U_PRIYA",
            assigner_name="Priya",
            assignee_id="U_ALICE",
            assignee_name="Alice",
            allowed_roles=["owner", "admin"]
        )

        # Unauthorized user Bob clicks "Approve & Post to Channel"
        bob_payload = {
            "user": {"id": "U_BOB", "username": "bob"},
            "actions": [
                {
                    "action_id": "btn_approve_auto_execute",
                    "value": json.dumps({"approval_id": appr.approval_id, "action": "approve"})
                }
            ]
        }

        result = self.dispatcher.handle_interaction(bob_payload, user_roles=[])
        self.assertFalse(result["authorized"])
        self.assertFalse(result["success"])
        self.assertEqual(result["status"], "UNAUTHORIZED")
        self.assertIn("bob is not permitted", result["reason"])

        # Check refusal card format
        blocks = result["blocks"]
        self.assertIn("⚠️ Action Refused: Unauthorized", blocks[0]["text"]["text"])
        self.assertIn("<@U_BOB>", blocks[1]["text"]["text"])

        # Confirm approval remains in PENDING state in storage
        unaltered_appr = self.store.get_approval(appr.approval_id)
        self.assertEqual(unaltered_appr.status, ApprovalStatus.PENDING)

    def test_authorized_assignee_click_succeeds(self):
        """Verify designated assignee can approve and execute task."""
        appr = self.store.create_approval(
            action_summary="Review database migrations",
            artifact_content="1. Check indexes\n2. Test rollback",
            assignee_id="U_ALICE",
            assignee_name="Alice",
            assigner_id="U_PRIYA",
            assigner_name="Priya"
        )

        alice_payload = {
            "user": {"id": "U_ALICE", "username": "alice"},
            "actions": [
                {
                    "action_id": "btn_accept_task",
                    "value": json.dumps({"approval_id": appr.approval_id, "action": "accept"})
                }
            ]
        }

        result = self.dispatcher.handle_interaction(alice_payload)
        self.assertTrue(result["authorized"])
        self.assertTrue(result["success"])
        self.assertEqual(result["status"], "APPROVED")

        # Confirm approval is APPROVED by Alice
        updated = self.store.get_approval(appr.approval_id)
        self.assertEqual(updated.status, ApprovalStatus.APPROVED)
        self.assertEqual(updated.resolved_by, "alice")

    def test_authorized_room_role_click_succeeds(self):
        """Verify user with room role (e.g. admin or lead) can approve on behalf of team."""
        appr = self.store.create_approval(
            action_summary="Urgent hotfix approval",
            artifact_content="Deploy auth patch",
            assignee_id="U_ALICE",
            allowed_roles=["admin", "lead", "owner"]
        )

        # Charlie is not Alice, but holds the "admin" role in the room
        charlie_payload = {
            "user": {"id": "U_CHARLIE", "username": "charlie"},
            "actions": [
                {
                    "action_id": "btn_approve_auto_execute",
                    "value": json.dumps({"approval_id": appr.approval_id, "action": "approve"})
                }
            ]
        }

        result = self.dispatcher.handle_interaction(charlie_payload, user_roles=["admin"])
        self.assertTrue(result["authorized"])
        self.assertTrue(result["success"])
        self.assertEqual(result["status"], "APPROVED")

        # Confirm resolved
        updated = self.store.get_approval(appr.approval_id)
        self.assertEqual(updated.status, ApprovalStatus.APPROVED)
        self.assertEqual(updated.resolved_by, "charlie")

    def test_approval_resolution_card(self):
        """Verify approval resolution replacement card structure."""
        blocks = build_approval_resolution_card(
            action_summary="Draft v1.3 Notes",
            status="APPROVED",
            resolved_by="U999",
            artifact_title="Release Notes v1.3",
            todoist_url="https://todoist.com/app/task/123"
        )
        self.assertEqual(len(blocks), 1)
        text = blocks[0]["text"]["text"]
        self.assertIn("Task Approved & Executed", text)
        self.assertIn("<@U999>", text)
        self.assertIn("Release Notes v1.3", text)
        self.assertIn("https://todoist.com/app/task/123", text)


    def test_build_assigner_card_preserves_action_plan_on_medium_confidence_path(self):
        """Verify build_assigner_card preserves worker checklist and embeds preview in card."""
        worker_plan = "* [ ] Analyze DB migration PR\n* [ ] Run benchmark suite\n* [ ] Sign off before Friday"
        blocks = build_assigner_card(
            action_summary="Review and benchmark database migration",
            target_assignee="Anuj",
            target_assignee_id="U0CFRP39TRS",
            assigner_id="U_PRIYA",
            assigner_name="Priya",
            deadline="Friday 5 PM",
            action_plan=worker_plan,
            store=self.store
        )
        # Verify action plan preview is displayed in blocks
        plan_block = next((b for b in blocks if "Proposed AI Action Plan" in b.get("text", {}).get("text", "")), None)
        self.assertIsNotNone(plan_block)
        self.assertIn("Analyze DB migration PR", plan_block["text"]["text"])

        # Extract approval_id and verify stored artifact_content contains the full worker checklist
        action_elem = next(b for b in blocks if b.get("type") == "actions")["elements"][0]
        btn_payload = parse_button_payload(action_elem["value"])
        appr = self.store.get_approval(btn_payload["approval_id"])
        self.assertIsNotNone(appr)
        self.assertEqual(appr.artifact_content, worker_plan)

    def test_handle_confirm_assignment_preserves_worker_plan_never_stubs(self):
        """Verify handle_confirm_assignment forwards full worker checklist, NEVER a stub."""
        from unittest.mock import MagicMock
        worker_plan = "* [ ] Step 1: Benchmark DB\n* [ ] Step 2: Test rollback\n* [ ] Step 3: Approve PR"
        appr = self.store.create_approval(
            action_type="ASSIGNMENT",
            action_summary="Review PR #89",
            artifact_content=worker_plan,
            assignee_id="U0CFRP39TRS",
            assignee_name="Anuj",
            assigner_id="U_PRIYA",
            assigner_name="Priya",
            deadline="Friday 5 PM"
        )

        mock_ack = MagicMock()
        mock_respond = MagicMock()
        mock_client = MagicMock()
        mock_client.conversations_open.return_value = {"ok": True, "channel": {"id": "D_ANUJ"}}
        mock_client.chat_postMessage.return_value = {"ok": True, "ts": "123456"}

        body = {
            "user": {"id": "U_PRIYA", "username": "priya"},
            "actions": [
                {
                    "action_id": "btn_confirm_assignment",
                    "value": json.dumps({"approval_id": appr.approval_id, "action": "confirm"})
                }
            ]
        }

        res = handle_confirm_assignment(
            ack=mock_ack,
            body=body,
            client=mock_client,
            respond=mock_respond,
            store=self.store
        )

        self.assertTrue(res["success"])
        self.assertEqual(res["action_plan"], worker_plan)
        self.assertNotEqual(res["action_plan"], "* [ ] Review requirements and complete task")

        # Verify chat_postMessage was invoked with the full worker checklist
        mock_client.chat_postMessage.assert_called_once()
        called_kwargs = mock_client.chat_postMessage.call_args[1]
        blocks = called_kwargs.get("blocks", [])
        # Check action plan block in the forwarded human todo card
        plan_block = next((b for b in blocks if "Step 1: Benchmark DB" in b.get("text", {}).get("text", "")), None)
        self.assertIsNotNone(plan_block)
        self.assertNotIn("* [ ] Review requirements and complete task", str(blocks))

    def test_interaction_dispatcher_confirm_forwards_plan_and_notifies(self):
        """Verify InteractionDispatcher handles 'confirm' by resolving approval and notifying Switch."""
        from unittest.mock import MagicMock
        worker_plan = "* [ ] Step 1: Reproduce issue\n* [ ] Step 2: Deploy fix"
        appr = self.store.create_approval(
            action_type="ASSIGNMENT",
            action_summary="Fix staging timeout",
            artifact_content=worker_plan,
            assignee_id="U0CFRP39TRS",
            assignee_name="Anuj",
            assigner_id="U_PRIYA",
            assigner_name="Priya",
            room_id="room_99"
        )

        mock_switch = MagicMock()
        mock_todoist = MagicMock()
        mock_todoist.create_task.return_value = {"id": "task_123", "url": "https://todoist.com/app/task/123"}
        dispatcher = InteractionDispatcher(
            approval_store=self.store,
            switch_client=mock_switch,
            todoist_client=mock_todoist
        )

        confirm_payload = {
            "user": {"id": "U_PRIYA", "username": "priya"},
            "actions": [
                {
                    "action_id": "btn_confirm_assignment",
                    "value": json.dumps({"approval_id": appr.approval_id, "action": "confirm"})
                }
            ]
        }

        result = dispatcher.handle_interaction(confirm_payload)
        self.assertTrue(result["success"])
        self.assertTrue(result["authorized"])

        # Verify Todoist was called with full worker plan
        mock_todoist.create_task.assert_called_once()
        todoist_args = mock_todoist.create_task.call_args[1]
        self.assertIn("Step 1: Reproduce issue", todoist_args["description"])

        # Verify Switch targeted message was dispatched to Anuj with worker plan
        mock_switch.send_targeted_message.assert_called_once()
        switch_args = mock_switch.send_targeted_message.call_args[1]
        self.assertEqual(switch_args["target_names"], ["Anuj"])
        self.assertIn("Step 1: Reproduce issue", switch_args["body"])


if __name__ == "__main__":
    unittest.main()
