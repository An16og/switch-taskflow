"""Unit tests for ContextResolver, Switch room reference attachments, and Worker real context grounding."""

import unittest
from unittest.mock import MagicMock, patch
from context_resolver import ContextResolver, RealTaskContext, default_context_resolver
from switch_client import SwitchAgentClient
from models import TaskTriageResult, ConfidenceLevel, ExecutionType, SlackMessage, WorkerExecutionResult
from agents.worker_agent import WorkerAgent


class TestContextResolver(unittest.TestCase):
    def setUp(self):
        self.resolver = ContextResolver()

    def test_git_commit_log_extraction(self):
        """Verify real git commits are extracted from repository."""
        commits = self.resolver.get_git_commit_log(limit=5)
        self.assertTrue(len(commits) > 0)
        self.assertIn("*", commits)

    def test_git_branch_and_remote(self):
        """Verify git branch and remote repository resolution."""
        info = self.resolver.get_git_branch_and_remote()
        self.assertIn("branch", info)
        self.assertIn("remote", info)
        self.assertTrue(len(info["branch"]) > 0)

    def test_changelog_extraction(self):
        """Verify CHANGELOG.md content is retrieved and contains genuine versions."""
        changelog = self.resolver.get_changelog()
        self.assertTrue(len(changelog) > 0)
        self.assertIn("## [1.2.", changelog)

    def test_ticket_reference_extraction(self):
        """Verify PR numbers (#89) and Jira keys (PROD-102) are parsed cleanly."""
        text = "Please review PR #89 and benchmark against PROD-102 before tomorrow (also see #42)."
        tickets = self.resolver.extract_ticket_references(text)
        self.assertIn("#89", tickets)
        self.assertIn("#42", tickets)
        self.assertIn("PROD-102", tickets)

    def test_real_task_context_formatting(self):
        """Verify RealTaskContext properly frames verified context blocks."""
        ctx = RealTaskContext(
            repo_commits="* cf814c7 - fix(triage): preserve worker checklist\n* 93cc834 - fix(dm): open DMs",
            git_branch="main",
            repo_url="https://github.com/An16og/switch-taskflow",
            changelog_snippet="## [1.2.3] - 2026-11-07\n- Medium confidence fix",
            ticket_references=["#89", "PROD-102"],
            attached_references=[{"name": "taskflow-repo", "type": "github", "description": "Core repo", "value": {"urls": ["https://github.com/An16og/switch-taskflow"]}}],
            room_backlog="• [Alex]: Need release notes"
        )
        formatted = ctx.format_prompt_context()
        self.assertIn("<verified_repository_context>", formatted)
        self.assertIn("cf814c7", formatted)
        self.assertIn("<verified_changelog_context>", formatted)
        self.assertIn("<referenced_tickets_and_prs>", formatted)
        self.assertIn("#89", formatted)
        self.assertIn("<switch_room_references>", formatted)
        self.assertIn("<room_backlog_timeline>", formatted)

    def test_switch_client_reference_attachment(self):
        """Verify SwitchAgentClient creates and attaches room references."""
        client = SwitchAgentClient(
            server_url="http://mock-switch:52422",
            agent_id="test_worker",
            api_key="test_key",
            room_id="room_123"
        )

        mock_create = MagicMock()
        mock_create.status_code = 200
        mock_create.json.return_value = {"result": {"id": "ref_gh_001", "name": "taskflow-repo"}}

        mock_attach = MagicMock()
        mock_attach.status_code = 200
        mock_attach.json.return_value = {"result": {"ok": True}}

        mock_list = MagicMock()
        mock_list.status_code = 200
        mock_list.json.return_value = {"result": []}

        with patch("requests.post") as mock_post:
            # Configure sequence of post calls
            mock_post.side_effect = [mock_list, mock_create, mock_attach, mock_create, mock_attach]
            attached = client.attach_default_room_references("room_123")
            self.assertGreaterEqual(len(attached), 1)

    def test_worker_agent_uses_real_context_in_prompt(self):
        """Verify WorkerAgent passes verified context blocks into gateway generation."""
        mock_gateway = MagicMock()
        mock_gateway.generate_structured.return_value = WorkerExecutionResult(
            artifact_title="Release Notes v1.2",
            artifact_content="# Release Notes v1.2\n- Grounded in real commits",
            suggested_action="Publish to channel"
        )
        worker = WorkerAgent(gateway=mock_gateway)

        triage = TaskTriageResult(
            is_task=True,
            confidence=ConfidenceLevel.HIGH,
            confidence_reason="Explicit ask",
            action_summary="Draft v1.2 release notes",
            execution_type=ExecutionType.AUTO_EXECUTE
        )
        msgs = [SlackMessage(user_id="U1", user_name="Alex", text="Please draft v1.2 release notes", ts="123")]

        real_ctx = RealTaskContext(
            repo_commits="* cf814c7 - fix(triage): preserve worker checklist",
            git_branch="main",
            repo_url="https://github.com/An16og/switch-taskflow",
            changelog_snippet="## [1.2.3] - 2026-11-07",
            ticket_references=["#89"]
        )

        worker.execute_task(triage, msgs, real_context=real_ctx)

        mock_gateway.generate_structured.assert_called_once()
        called_prompt = mock_gateway.generate_structured.call_args[1]["prompt"]
        self.assertIn("<verified_repository_context>", called_prompt)
        self.assertIn("cf814c7", called_prompt)
        self.assertIn("<verified_changelog_context>", called_prompt)
        self.assertIn("<referenced_tickets_and_prs>", called_prompt)
        self.assertIn("#89", called_prompt)


if __name__ == "__main__":
    unittest.main()
