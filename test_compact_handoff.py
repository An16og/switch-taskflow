"""Verifies the compact document-backed Triage -> Worker handoff."""

import asyncio
import sys
import tempfile
import unittest
import uuid
from unittest.mock import MagicMock, patch

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

from models import ConfidenceLevel, ExecutionType, TaskHandoffMessage, TaskTriageResult, WorkerExecutionResult
from room_documents import RoomDocumentStore
from room_tasks import RoomTaskStore


class FakeSwitchClient:
    def __init__(self, room_id, agent_name, document_store):
        self.room_id = room_id
        self.agent_id = f"agent-{agent_name}"
        self.agent_name = agent_name
        self.document_store = document_store
        self.sent = []
        self.posted = []

    def list_participants(self, room_id=None):
        return []

    def send_targeted_message(self, body, target_names=None, target_roles=None, thread_id=None, room_id=None):
        self.sent.append({
            "body": body,
            "target_names": target_names,
            "thread_id": thread_id,
            "room_id": room_id,
        })
        return {"event_id": "evt"}

    def post_message(self, body, thread_id=None, room_id=None):
        self.posted.append({"body": body, "thread_id": thread_id, "room_id": room_id})
        return {"event_id": "evt"}

    def create_room_document(self, **kwargs):
        return {"document_id": "doc_deliverable"}


class TestCompactHandoff(unittest.TestCase):
    def setUp(self):
        import bot
        self.bot = bot
        bot.room_rate_limiter.reset()
        self.room_id = f"compact-handoff-{uuid.uuid4().hex[:8]}"
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = RoomDocumentStore(storage_path=f"{self.tempdir.name}/room_documents.json")
        self.task_store = RoomTaskStore(storage_path=f"{self.tempdir.name}/room_tasks.json")

    def tearDown(self):
        self.bot.room_buffers.clear(self.room_id)
        self.bot.room_rate_limiter.reset()
        self.tempdir.cleanup()

    def test_triage_stores_payload_and_sends_only_a_short_ref(self):
        triage_client = FakeSwitchClient(self.room_id, "taskflow-triage", self.store)
        task = TaskTriageResult(
            is_task=True,
            confidence=ConfidenceLevel.HIGH,
            confidence_reason="Clear task",
            action_summary=f"Update README {uuid.uuid4().hex[:6]}",
            execution_type=ExecutionType.AUTO_EXECUTE,
            execution_category="Documentation",
        )

        payload = {
            "body": "Please update the README with the new auth section",
            "sender": "U_USER1",
            "sender_name": "Alice",
            "addressed": False,
            "room_id": self.room_id,
            "id": f"msg_{uuid.uuid4().hex[:8]}",
        }

        with patch.object(self.bot.triage_classifier, "triage_conversation", return_value=task):
            asyncio.run(self.bot.handle_triage_message(triage_client, payload))

        self.assertEqual(len(triage_client.sent), 1)
        body = triage_client.sent[0]["body"]
        self.assertEqual(body, "@taskflow-worker On it. [handoff]")
        self.assertNotIn("TASKFLOW_HANDOFF", body)
        self.assertNotIn("Autonomy Mode", body)

        pending = [
            d for d in self.store.list_documents(room_id=self.room_id)
            if d.metadata.get("type") == "taskflow_handoff"
        ]
        self.assertEqual(len(pending), 1)
        parsed = TaskHandoffMessage.model_validate_json(pending[0].content)
        self.assertEqual(parsed.task_title, task.action_summary)

    def test_worker_loads_payload_from_ref_and_executes(self):
        # Seed a handoff document exactly as triage would.
        handoff = TaskHandoffMessage(
            task_title="Update README with auth section",
            execution_type=ExecutionType.AUTO_EXECUTE,
            confidence=ConfidenceLevel.HIGH,
            assignee_name="Alice",
            assigner_name="Bob",
            room_id=self.room_id,
            requester_name="Bob",
            recent_context=[],
        )
        doc = self.store.create_document(
            title="TaskFlow Handoff",
            content=handoff.model_dump_json(),
            room_id=self.room_id,
            doc_id="doc_compact_test",
            created_by="taskflow-triage",
            metadata={"type": "taskflow_handoff", "status": "PENDING"},
        )

        worker_client = FakeSwitchClient(self.room_id, "taskflow-worker", self.store)
        payload = {
            "body": "@taskflow-worker On it. [handoff]",
            "sender": "agent-taskflow-triage",
            "sender_name": "taskflow-triage",
            "addressed": True,
            "room_id": self.room_id,
            "id": "msg_compact_001",
        }
        result = WorkerExecutionResult(
            artifact_title="README Update",
            artifact_content="# README\n\nNew auth section.",
            suggested_action="Approve",
        )
        fake_approval = MagicMock()
        fake_approval.approval_id = "appr_compact"

        with patch.object(self.bot.worker_executor, "execute_task", return_value=result) as mock_execute, \
             patch.object(self.bot.default_approval_store, "create_approval", return_value=fake_approval):
            asyncio.run(self.bot.handle_worker_message(worker_client, payload))

        mock_execute.assert_called_once()
        self.assertIsNone(self.store.get_document("doc_compact_test", room_id=self.room_id))
        self.assertTrue(any("Pending Approval" in p["body"] for p in worker_client.posted))


    def test_unassigned_human_task_is_recorded_without_confirmation(self):
        """An unassigned HUMAN_TODO should be tracked as Unassigned, not held for confirmation."""
        handoff = TaskHandoffMessage(
            task_title="Update the README before launch",
            execution_type=ExecutionType.HUMAN_TODO,
            confidence=ConfidenceLevel.MEDIUM,
            assignee_name="Unassigned",
            assignee_id=None,
            assigner_name="Bob",
            assigner_id="U_BOB",
            room_id=self.room_id,
            requester_name="Bob",
            recent_context=[],
        )
        self.store.create_document(
            title="TaskFlow Handoff",
            content=handoff.model_dump_json(),
            room_id=self.room_id,
            doc_id="doc_unassigned_test",
            created_by="taskflow-triage",
            metadata={"type": "taskflow_handoff", "status": "PENDING"},
        )

        worker_client = FakeSwitchClient(self.room_id, "taskflow-worker", self.store)
        payload = {
            "body": "@taskflow-worker On it. [handoff]",
            "sender": "agent-taskflow-triage",
            "sender_name": "taskflow-triage",
            "addressed": True,
            "room_id": self.room_id,
            "id": "msg_unassigned_001",
        }
        result = WorkerExecutionResult(
            artifact_title="README Update Checklist",
            artifact_content="1. Update the README\n2. Add the new launch section",
            suggested_action="Assign an owner",
        )
        fake_approval = MagicMock()
        sync_result = MagicMock()
        sync_result.success = False
        sync_result.task_url = None
        sync_result.error_message = None
        sync_result.is_client_error = False

        with patch.object(self.bot.worker_executor, "execute_task", return_value=result), \
             patch.object(self.bot.default_approval_store, "create_approval", return_value=fake_approval) as mock_approval, \
             patch.object(self.bot, "default_room_task_store", self.task_store), \
             patch.object(self.bot.todoist, "sync_task", return_value=sync_result):
            asyncio.run(self.bot.handle_worker_message(worker_client, payload))

        # No confirmation card: the task is immediately tracked as Unassigned.
        mock_approval.assert_not_called()
        tasks = self.task_store.list_tasks(room_id=self.room_id)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].assignee_name, "Unassigned")
        self.assertEqual(tasks[0].status.value, "PENDING")

        self.assertEqual(len(worker_client.sent), 0)
        self.assertEqual(len(worker_client.posted), 1)
        receipt = worker_client.posted[0]
        self.assertIn("Unassigned", receipt["body"])
        self.assertIn("This task has no owner yet", receipt["body"])


    def test_assign_command_claims_an_unassigned_task(self):
        """@taskflow-worker assign <task_id> @person updates the room task."""
        task = self.task_store.create_task(
            title="Update the README before launch",
            room_id=self.room_id,
            assignee_name="Unassigned",
            assigner_name="Bob",
        )
        worker_client = FakeSwitchClient(self.room_id, "taskflow-worker", self.store)
        payload = {
            "body": f"@taskflow-worker assign {task.task_id} to @Rohit",
            "sender": "U_ALICE",
            "sender_name": "Alice",
            "addressed": True,
            "room_id": self.room_id,
            "id": "msg_assign_001",
        }

        with patch.object(self.bot, "default_room_task_store", self.task_store):
            asyncio.run(self.bot.handle_worker_message(worker_client, payload))

        updated = self.task_store.get_task(task.task_id)
        self.assertEqual(updated.assignee_name, "Rohit")
        self.assertEqual(updated.assignee_id, "U_ROHIT")
        self.assertEqual(len(worker_client.posted), 1)
        self.assertIn("Task Assigned", worker_client.posted[0]["body"])
        self.assertIn("Rohit", worker_client.posted[0]["body"])


if __name__ == "__main__":
    unittest.main()
