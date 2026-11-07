"""Unit tests for TaskFlow Multi-Agent Pipeline (TriageAgent + WorkerAgent).

Deterministic, mocked tests for autonomous routine task execution (AUTO_EXECUTE)
and complex human workflow scaffolding (HUMAN_TODO) without requiring live
Gemini / Anthropic / OpenAI API credentials or network connectivity.
"""

import sys
import unittest
from unittest.mock import MagicMock

# Ensure UTF-8 output on console
if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')

from models import (
    ConfidenceLevel,
    ExecutionType,
    SlackMessage,
    TaskTriageResult,
    WorkerExecutionResult,
)
from llm_gateway import LLMGateway
from agents.triage_agent import TriageAgent
from agents.worker_agent import WorkerAgent


class TestMultiAgentPipeline(unittest.TestCase):
    """Test suite for the multi-agent triage and worker pipeline with deterministic mocks."""

    def setUp(self):
        """Set up mock LLMGateway and agent instances before each test."""
        self.mock_gateway = MagicMock(spec=LLMGateway)
        self.triage_agent = TriageAgent(gateway=self.mock_gateway)
        self.worker_agent = WorkerAgent(gateway=self.mock_gateway)

    def test_auto_execute_pipeline(self):
        """TEST 1: AUTO_EXECUTE routine documentation task (e.g. Release Notes)."""
        auto_msg = [
            SlackMessage(
                user_id="U1",
                user_name="Alex (PM)",
                text="Can someone draft the v1.2 release notes highlighting our new Stripe webhook fix and dark mode?",
                ts="100.0",
            )
        ]

        expected_triage = TaskTriageResult(
            is_task=True,
            confidence=ConfidenceLevel.HIGH,
            confidence_reason="Routine documentation draft request suitable for auto-execution",
            action_summary="Draft v1.2 release notes highlighting our new Stripe webhook fix and dark mode",
            execution_type=ExecutionType.AUTO_EXECUTE,
            execution_category="DOCUMENTATION",
        )

        expected_worker = WorkerExecutionResult(
            artifact_title="Release Notes - Version 1.2.3 & Recent Enhancements",
            artifact_content=(
                "# Release Notes: TaskFlow on Switch v1.2.x\n\n"
                "We are excited to announce the latest updates and hardening improvements for **TaskFlow on Switch**.\n\n"
                "### Highlights\n"
                "- Stripe webhook fix for idempotent processing\n"
                "- Dark mode visual theme enhancements\n"
            ),
            suggested_action="Review and publish release notes",
        )

        def mock_generate(prompt, system_prompt, response_schema, **kwargs):
            if response_schema == TaskTriageResult:
                return expected_triage
            elif response_schema == WorkerExecutionResult:
                return expected_worker
            raise ValueError(f"Unexpected response schema: {response_schema}")

        self.mock_gateway.generate_structured.side_effect = mock_generate

        # Step 1: Triage
        triage_res = self.triage_agent.triage_conversation(auto_msg)
        self.assertTrue(triage_res.is_task)
        self.assertEqual(triage_res.execution_type, ExecutionType.AUTO_EXECUTE)
        self.assertIn("Draft v1.2 release notes", triage_res.action_summary)

        # Step 2: Worker Execution
        worker_res = self.worker_agent.execute_task(triage_res, auto_msg)
        self.assertEqual(worker_res.artifact_title, "Release Notes - Version 1.2.3 & Recent Enhancements")
        self.assertIn("Stripe webhook fix", worker_res.artifact_content)
        self.assertIn("Dark mode", worker_res.artifact_content)
        self.assertEqual(worker_res.suggested_action, "Review and publish release notes")

    def test_human_todo_pipeline(self):
        """TEST 2: HUMAN_TODO complex engineering task (e.g. PR Review / Migration)."""
        human_msg = [
            SlackMessage(
                user_id="U2",
                user_name="Priya (Lead)",
                text="@Anuj please review and benchmark the database migration PR #89 before Friday 5 PM.",
                ts="200.0",
            )
        ]

        expected_triage = TaskTriageResult(
            is_task=True,
            confidence=ConfidenceLevel.HIGH,
            confidence_reason="Engineering code review task requiring human judgment",
            action_summary="Review and benchmark database migration PR #89 before Friday 5 PM",
            execution_type=ExecutionType.HUMAN_TODO,
            execution_category="ENGINEERING",
            assignee_name="Anuj",
            deadline="Friday 5 PM",
        )

        expected_worker = WorkerExecutionResult(
            artifact_title="PR #89 Code Review & Benchmark Action Checklist",
            artifact_content=(
                "# PR #89 Code Review & Benchmark Action Checklist\n\n"
                "Assigned to: Anuj\n"
                "Deadline: Friday 5 PM\n\n"
                "## Action Checklist\n\n"
                "* [ ] **Step 1: Code Review & Schema Inspection**\n"
                "  - Review SQLite migration schema changes in PR #89.\n\n"
                "* [ ] **Step 2: Staging Benchmark Execution**\n"
                "  - Run load tests against the per-room sliding-window rate limiter.\n\n"
                "* [ ] **Step 3: Security & DLP Verification**\n"
                "  - Confirm no credentials exposed in logs.\n\n"
                "* [ ] **Step 4: Approval & Production Coordination**\n"
                "  - Post review findings on PR #89.\n"
            ),
            suggested_action="Track in Switch Room / Todoist",
        )

        def mock_generate(prompt, system_prompt, response_schema, **kwargs):
            if response_schema == TaskTriageResult:
                return expected_triage
            elif response_schema == WorkerExecutionResult:
                return expected_worker
            raise ValueError(f"Unexpected response schema: {response_schema}")

        self.mock_gateway.generate_structured.side_effect = mock_generate

        # Step 1: Triage
        triage_res = self.triage_agent.triage_conversation(human_msg)
        self.assertTrue(triage_res.is_task)
        self.assertEqual(triage_res.execution_type, ExecutionType.HUMAN_TODO)
        self.assertEqual(triage_res.assignee_name, "Anuj")
        self.assertEqual(triage_res.deadline, "Friday 5 PM")

        # Step 2: Worker Execution Scaffolding
        worker_res = self.worker_agent.execute_task(triage_res, human_msg)
        self.assertEqual(worker_res.artifact_title, "PR #89 Code Review & Benchmark Action Checklist")
        self.assertIn("* [ ] **Step 1:", worker_res.artifact_content)
        self.assertIn("* [ ] **Step 2:", worker_res.artifact_content)
        self.assertIn("PR #89", worker_res.artifact_content)

    def test_multi_agent_pipeline_end_to_end(self):
        """End-to-end sequential verification of both task paths without live credentials."""
        self.test_auto_execute_pipeline()
        self.test_human_todo_pipeline()


def test_multi_agent():
    """Standalone runner maintaining CLI backwards compatibility."""
    print("--- Testing TaskFlow Multi-Agent Pipeline ---")
    suite = unittest.TestLoader().loadTestsFromTestCase(TestMultiAgentPipeline)
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    if not result.wasSuccessful():
        sys.exit(1)
    print("\n🎉 ALL MULTI-AGENT TESTS PASSED SUCCESSFULLY!")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--legacy":
        test_multi_agent()
    else:
        unittest.main()
