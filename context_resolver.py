"""Real Context Resolver for TaskFlow Worker Agent.

Resolves ground-truth context to prevent LLM hallucinations:
1. Git Commit History & Branch Metadata (from actual git repository).
2. Changelog & Project Manifests (from CHANGELOG.md and pyproject.toml).
3. Switch Room References & Backlog (from Switch list_all_references & read_context).
4. Ticket and PR References (parsed from context and matched to git commits).
"""

import os
import re
import subprocess
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Dict, Any, Optional

logger = logging.getLogger("ContextResolver")


@dataclass
class RealTaskContext:
    """Encapsulates verified ground-truth context for worker execution."""
    repo_commits: str = ""
    git_branch: str = "main"
    repo_url: str = "https://github.com/An16og/switch-taskflow"
    changelog_snippet: str = ""
    project_version: str = "1.2.3"
    room_backlog: str = ""
    ticket_references: List[str] = field(default_factory=list)
    attached_references: List[Dict[str, Any]] = field(default_factory=list)

    def format_prompt_context(self) -> str:
        """Formats the verified context blocks for injection into the worker prompt."""
        sections = []

        if self.repo_commits:
            sections.append(
                f"<verified_repository_context>\n"
                f"Repository: {self.repo_url} (branch: {self.git_branch})\n"
                f"Recent Verified Commits:\n{self.repo_commits}\n"
                f"</verified_repository_context>"
            )

        if self.changelog_snippet:
            sections.append(
                f"<verified_changelog_context>\n"
                f"{self.changelog_snippet}\n"
                f"</verified_changelog_context>"
            )

        if self.ticket_references:
            sections.append(
                f"<referenced_tickets_and_prs>\n"
                f"Referenced Items: {', '.join(self.ticket_references)}\n"
                f"</referenced_tickets_and_prs>"
            )

        if self.attached_references:
            ref_lines = [
                f"- {r.get('name')} ({r.get('type')}): {r.get('description')} [URLs: {r.get('value', {}).get('urls', [])}]"
                for r in self.attached_references
            ]
            sections.append(
                f"<switch_room_references>\n"
                + "\n".join(ref_lines) + "\n"
                f"</switch_room_references>"
            )

        if self.room_backlog:
            sections.append(
                f"<room_backlog_timeline>\n"
                f"{self.room_backlog}\n"
                f"</room_backlog_timeline>"
            )

        return "\n\n".join(sections)


class ContextResolver:
    """Resolves ground-truth context from git, repository files, and Switch room ops."""

    def __init__(self, repo_dir: Optional[str] = None):
        if repo_dir:
            self.repo_dir = repo_dir
        else:
            cwd = os.getcwd()
            file_dir = str(Path(__file__).resolve().parent)
            if (Path(cwd) / ".git").exists() or (Path(cwd) / "CHANGELOG.md").exists():
                self.repo_dir = cwd
            else:
                self.repo_dir = file_dir

    def get_git_commit_log(self, limit: int = 15) -> str:
        """Fetches the latest real git commit messages and hashes."""
        try:
            cmd = ["git", "log", f"-n{limit}", "--pretty=format:* %h - %s (%an, %ar)"]
            res = subprocess.run(
                cmd,
                cwd=self.repo_dir,
                capture_output=True,
                text=True,
                timeout=5,
                encoding="utf-8"
            )
            if res.returncode == 0 and res.stdout.strip():
                return res.stdout.strip()
        except Exception as e:
            logger.debug("Failed reading git commit log: %s", e)
        return ""

    def get_git_branch_and_remote(self) -> Dict[str, str]:
        """Resolves active git branch and origin remote URL."""
        info = {
            "branch": "main",
            "remote": "https://github.com/An16og/switch-taskflow"
        }
        try:
            branch_res = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=self.repo_dir,
                capture_output=True,
                text=True,
                timeout=3,
                encoding="utf-8"
            )
            if branch_res.returncode == 0 and branch_res.stdout.strip():
                info["branch"] = branch_res.stdout.strip()

            remote_res = subprocess.run(
                ["git", "remote", "get-url", "origin"],
                cwd=self.repo_dir,
                capture_output=True,
                text=True,
                timeout=3,
                encoding="utf-8"
            )
            if remote_res.returncode == 0 and remote_res.stdout.strip():
                clean_url = remote_res.stdout.strip().replace(".git", "")
                info["remote"] = clean_url
        except Exception as e:
            logger.debug("Failed reading git remote info: %s", e)
        return info

    def get_changelog(self, max_chars: int = 3000) -> str:
        """Reads CHANGELOG.md from repository root."""
        changelog_path = Path(self.repo_dir) / "CHANGELOG.md"
        if changelog_path.exists():
            try:
                content = changelog_path.read_text(encoding="utf-8", errors="ignore").strip()
                if len(content) > max_chars:
                    return content[:max_chars] + "\n...(truncated for context length)"
                return content
            except Exception as e:
                logger.debug("Failed reading CHANGELOG.md: %s", e)
        return ""

    def get_project_version(self) -> str:
        """Extracts current version from pyproject.toml."""
        pyproject_path = Path(self.repo_dir) / "pyproject.toml"
        if pyproject_path.exists():
            try:
                content = pyproject_path.read_text(encoding="utf-8", errors="ignore")
                match = re.search(r'version\s*=\s*["\']([^"\']+)["\']', content)
                if match:
                    return match.group(1)
            except Exception as e:
                logger.debug("Failed reading pyproject.toml: %s", e)
        return "1.2.3"

    def extract_ticket_references(self, text: str) -> List[str]:
        """Extracts PR numbers (#123) and issue keys (PROD-456) from text."""
        tickets = []
        # Match PR or Issue numbers like #89, #42
        for match in re.finditer(r"(?:^|[\s(,])#(\d+)\b", text):
            tickets.append(f"#{match.group(1)}")
        # Match Jira/Ticket keys like PROD-102
        for match in re.finditer(r"\b([A-Z]{2,10}-\d+)\b", text):
            tickets.append(match.group(1))
        return list(dict.fromkeys(tickets))

    def resolve_room_backlog(
        self,
        switch_client: Optional[Any] = None,
        room_id: Optional[str] = None,
        limit: int = 25
    ) -> str:
        """Reads recent messages from Switch room context via read_context."""
        if not switch_client or not hasattr(switch_client, "read_context"):
            return ""

        try:
            messages = switch_client.read_context(limit=limit, room_id=room_id)
            if not messages:
                return ""
            lines = []
            for m in messages:
                sender = m.get("sender_name") or m.get("sender") or "User"
                body = m.get("body", "").strip()
                # Exclude handoff envelopes or TaskFlow internal messages
                if "<!-- TASKFLOW_HANDOFF:" in body or "[TASK_HANDOFF]" in body:
                    continue
                if sender.lower() in ("taskflow-triage", "taskflow-worker", "taskflow"):
                    continue
                if body:
                    lines.append(f"• [{sender}]: {body}")
            return "\n".join(lines[-limit:])
        except Exception as e:
            logger.debug("Failed resolving Switch room backlog: %s", e)
            return ""

    def resolve_context(
        self,
        task_summary: str,
        context_notes: Optional[str] = None,
        switch_client: Optional[Any] = None,
        room_id: Optional[str] = None,
        limit_backlog: int = 25
    ) -> RealTaskContext:
        """Resolves full ground-truth context bundle for worker execution."""
        git_info = self.get_git_branch_and_remote()
        commits = self.get_git_commit_log(limit=15)
        changelog = self.get_changelog()
        version = self.get_project_version()

        # Combine text to extract tickets
        combined_text = f"{task_summary} {context_notes or ''}"
        tickets = self.extract_ticket_references(combined_text)

        # Room backlog & attached references
        room_backlog = self.resolve_room_backlog(
            switch_client=switch_client,
            room_id=room_id,
            limit=limit_backlog
        )
        attached_refs = []
        if switch_client and hasattr(switch_client, "list_all_references"):
            try:
                attached_refs = switch_client.list_all_references()
            except Exception as e:
                logger.debug("Could not query Switch references: %s", e)

        return RealTaskContext(
            repo_commits=commits,
            git_branch=git_info["branch"],
            repo_url=git_info["remote"],
            changelog_snippet=changelog,
            project_version=version,
            room_backlog=room_backlog,
            ticket_references=tickets,
            attached_references=attached_refs
        )


# Global default resolver
default_context_resolver = ContextResolver()
