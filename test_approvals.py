"""Unit tests for PendingApprovalStore and lightweight Slack button payloads."""

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
    parse_button_payload
)


class TestPendingApprovalStore(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.store_file = Path(self.test_dir) / "test_approvals.json"
        self.store = PendingApprovalStore(storage_path=str(self.store_file), ttl_seconds=3600.0)

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
        )
        self.assertEqual(appr.status, ApprovalStatus.PENDING)

        resolved = self.store.resolve_approval(
            approval_id=appr.approval_id,
            status=ApprovalStatus.APPROVED,
            resolved_by="U_MANAGER"
        )
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
        """Verify human_todo and assigner cards generate lightweight button payloads."""
        human_blocks = build_human_todo_card(
            action_summary="Review PR #42",
            assigner_id="U100",
            action_plan="1. Check migrations\n2. Run load tests",
            store=self.store
        )
        action_block = next(b for b in human_blocks if b.get("type") == "actions")
        for btn in action_block["elements"]:
            self.assertLess(len(btn["value"]), 100)
            parsed = parse_button_payload(btn["value"])
            self.assertTrue(parsed["approval_id"].startswith("appr_"))

        assigner_blocks = build_assigner_card(
            action_summary="Prepare demo deck",
            target_assignee="Sarah",
            target_assignee_id="U200",
            store=self.store
        )
        action_block_2 = next(b for b in assigner_blocks if b.get("type") == "actions")
        for btn in action_block_2["elements"]:
            self.assertLess(len(btn["value"]), 100)

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


if __name__ == "__main__":
    unittest.main()
