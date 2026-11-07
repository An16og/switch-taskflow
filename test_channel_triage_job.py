"""Test suite for TaskFlow Channel Triage Job.

Verifies:
1. Channel context retrieval & message filtering.
2. Backlog task identification across conversation windows.
3. AUTO_EXECUTE knowledge deliverable generation.
4. HUMAN_TODO action checklist scaffolding & Todoist tracking.
5. Consolidated Channel Triage Digest Markdown compilation.
"""

import sys
import unittest
from unittest.mock import MagicMock, patch

if hasattr(sys.stdout, 'reconfigure') and sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')

from models import SlackMessage, TaskTriageResult, ExecutionType, ConfidenceLevel, WorkerExecutionResult
from jobs.triage_channel import ChannelTriageJob


class TestChannelTriageJob(unittest.TestCase):

    def setUp(self):
        self.mock_triage_client = MagicMock()
        self.mock_worker_client = MagicMock()
        self.mock_todoist = MagicMock()

        self.job = ChannelTriageJob(
            triage_client=self.mock_triage_client,
            worker_client=self.mock_worker_client,
            todoist_client=self.mock_todoist,
            room_id="test-room-123",
        )

    def test_empty_channel_backlog(self):
        """Verifies graceful handling of an empty channel history."""
        self.mock_triage_client.read_context.return_value = []
        report = self.job.run(limit=10, dry_run=True, post_digest=False)

        self.assertEqual(report["status"], "empty")
        self.assertEqual(report["tasks_count"], 0)
        self.assertIn("No messages found in room history", report["digest_markdown"])

    def test_channel_with_only_agent_chatter(self):
        """Verifies agent handoffs and bot messages are filtered out."""
        self.mock_triage_client.read_context.return_value = [
            {"sender": "taskflow-triage", "sender_name": "taskflow-triage", "body": "<!-- TASKFLOW_HANDOFF: {} -->"},
            {"sender": "@switch-agent-1", "sender_name": "taskflow-worker", "body": "Here are the release notes..."},
        ]
        report = self.job.run(limit=10, dry_run=True, post_digest=False)

        self.assertEqual(report["status"], "up_to_date")
        self.assertEqual(report["tasks_count"], 0)
        self.assertIn("Channel is fully up to date", report["digest_markdown"])

    def test_full_backlog_triage_with_auto_and_human_tasks(self):
        """Verifies full backlog triage identifying AUTO_EXECUTE and HUMAN_TODO items."""
        simulated_messages = [
            {
                "sender": "user_alex",
                "sender_name": "Alex",
                "body": "Hey team, standup at 10 AM today.",
                "timestamp": 1700000000000,
            },
            {
                "sender": "user_kunal",
                "sender_name": "Kunal",
                "body": "@taskflow-triage please draft release notes for v1.3 with dark mode and security patches.",
                "timestamp": 1700000010000,
            },
            {
                "sender": "user_sarah",
                "sender_name": "Sarah",
                "body": "Todo: We need someone to audit the database connection pool settings and fix connection timeouts before Friday.",
                "timestamp": 1700000020000,
            },
        ]
        self.mock_triage_client.read_context.return_value = simulated_messages

        # Mock TriageAgent decisions for predictable testing
        def mock_triage_fn(messages, active_tasks=None, **kwargs):
            last_text = messages[-1].text if messages else ""
            if "release notes" in last_text.lower():
                return TaskTriageResult(
                    is_task=True,
                    confidence=ConfidenceLevel.HIGH,
                    confidence_reason="Direct release notes request",
                    action_summary="Draft release notes for v1.3 with dark mode and security patches",
                    assignee_name="Kunal",
                    execution_type=ExecutionType.AUTO_EXECUTE,
                    execution_category="Release Notes",
                )
            elif "database connection pool" in last_text.lower():
                return TaskTriageResult(
                    is_task=True,
                    confidence=ConfidenceLevel.HIGH,
                    confidence_reason="Database maintenance todo",
                    action_summary="Audit database connection pool settings and fix connection timeouts",
                    assignee_name="Sarah",
                    deadline="before Friday",
                    execution_type=ExecutionType.HUMAN_TODO,
                    execution_category="Database Operations",
                )
            return TaskTriageResult(
                is_task=False,
                confidence=ConfidenceLevel.LOW,
                confidence_reason="Chatter"
            )

        self.job.triage_agent.triage_conversation = MagicMock(side_effect=mock_triage_fn)

        # Mock WorkerAgent execution
        def mock_worker_fn(triage_res, context):
            if triage_res.execution_type == ExecutionType.AUTO_EXECUTE:
                return WorkerExecutionResult(
                    artifact_title="Release Notes v1.3",
                    artifact_content="# Release Notes v1.3\n- Dark mode\n- Security patches",
                    suggested_action="Review and publish"
                )
            return WorkerExecutionResult(
                artifact_title="Database Audit Checklist",
                artifact_content="* [ ] Step 1: Check metrics\n* [ ] Step 2: Tune pool\n* [ ] Step 3: Verify",
                suggested_action="Assign to engineer"
            )

        self.job.worker_agent.execute_task = MagicMock(side_effect=mock_worker_fn)

        # Mock Todoist task creation
        mock_task = MagicMock()
        mock_task.id = "todo_123"
        mock_task.url = "https://todoist.com/app/task/todo_123"
        self.mock_todoist.create_task.return_value = mock_task

        # Run triage job
        report = self.job.run(limit=10, dry_run=False, post_digest=True)

        self.assertEqual(report["status"], "success")
        self.assertGreaterEqual(report["tasks_count"], 2)

        # Check AUTO_EXECUTE deliverable
        self.assertGreaterEqual(len(report["auto_executed"]), 1)
        auto_task = report["auto_executed"][0]
        self.assertIn("release notes", auto_task["title"].lower())
        self.mock_worker_client.post_message.assert_called()

        # Check HUMAN_TODO item
        self.assertGreaterEqual(len(report["human_todos"]), 1)
        human_task = report["human_todos"][0]
        self.assertIn("connection pool", human_task["title"].lower())
        self.mock_todoist.create_task.assert_called()
        self.mock_worker_client.send_targeted_message.assert_called()

        # Check digest content
        digest = report["digest_markdown"]
        self.assertIn("📋 **TaskFlow Channel Triage Digest**", digest)
        self.assertIn("Auto-Executed Deliverables", digest)
        self.assertIn("Human To-Dos Tracked", digest)
        self.assertIn("https://todoist.com/app/task/todo_123", digest)

        # Check that digest was posted
        self.mock_triage_client.post_message.assert_called_with(
            body=digest,
            room_id="test-room-123"
        )
        print("✅ Full channel backlog triage test passed successfully!")


if __name__ == "__main__":
    unittest.main()
