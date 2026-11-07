"""TaskFlow Channel Triage Job.

Scans the conversation history of a Switch room / connected Slack channel,
detects unhandled action items and knowledge work, triages them into
AUTO_EXECUTE vs HUMAN_TODO, executes drafts or syncs to Todoist, and
posts a comprehensive Channel Triage Digest.
"""

import argparse
import logging
import os
import sys
import time
from typing import Dict, Any, List, Optional

if hasattr(sys.stdout, 'reconfigure') and sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')

# Ensure switch-taskflow root is on sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import (
    SWITCH_SERVER_URL,
    SWITCH_ROOM_ID,
    SWITCH_TRIAGE_AGENT_ID,
    SWITCH_TRIAGE_AGENT_API_KEY,
    SWITCH_WORKER_AGENT_ID,
    SWITCH_WORKER_AGENT_API_KEY,
    TODOIST_API_TOKEN,
)
from models import SlackMessage, TaskTriageResult, ExecutionType, WorkerExecutionResult
from switch_client import SwitchAgentClient
from agents.triage_agent import TriageAgent
from agents.worker_agent import WorkerAgent
from todoist_client import TodoistClient
from security import sanitize_channel_text, detect_prompt_injection, validate_auto_post_safety
from deduplication import TaskDeduplicator

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("ChannelTriageJob")


class ChannelTriageJob:
    """Orchestrates an on-demand or scheduled channel triage job across Switch Core."""

    def __init__(
        self,
        triage_client: Optional[SwitchAgentClient] = None,
        worker_client: Optional[SwitchAgentClient] = None,
        todoist_client: Optional[TodoistClient] = None,
        room_id: Optional[str] = None,
    ):
        self.room_id = room_id or SWITCH_ROOM_ID
        self.triage_client = triage_client or SwitchAgentClient(
            server_url=SWITCH_SERVER_URL,
            agent_id=SWITCH_TRIAGE_AGENT_ID,
            api_key=SWITCH_TRIAGE_AGENT_API_KEY,
            room_id=self.room_id,
            agent_name="taskflow-triage",
        )
        self.worker_client = worker_client or SwitchAgentClient(
            server_url=SWITCH_SERVER_URL,
            agent_id=SWITCH_WORKER_AGENT_ID,
            api_key=SWITCH_WORKER_AGENT_API_KEY,
            room_id=self.room_id,
            agent_name="taskflow-worker",
        )
        self.triage_agent = TriageAgent()
        self.worker_agent = WorkerAgent()
        self.todoist_client = todoist_client or TodoistClient(api_token=TODOIST_API_TOKEN)

    def run(
        self,
        limit: int = 50,
        dry_run: bool = False,
        post_digest: bool = True,
    ) -> Dict[str, Any]:
        """Runs the channel triage job.
        
        Args:
            limit: Number of recent messages to inspect.
            dry_run: If True, triages and logs without posting to Switch or creating Todoist tasks.
            post_digest: If True, broadcasts the final summary card to the room.
            
        Returns:
            Dictionary containing scan statistics, auto-executed deliverables, and human to-dos.
        """
        logger.info("🚀 Starting Channel Triage Job for room: %s (limit=%d, dry_run=%s)", self.room_id, limit, dry_run)

        # 1. Fetch recent room context from Switch
        raw_messages = self.triage_client.read_context(limit=limit, room_id=self.room_id)
        logger.info("Fetched %d raw messages from room context.", len(raw_messages))

        if not raw_messages:
            logger.warning("No messages retrieved from room context.")
            empty_digest = (
                "📋 **TaskFlow Channel Triage Digest**\n\n"
                "### 📊 Scan Statistics:\n"
                "• 💬 Messages Analyzed: **0**\n"
                "• 🔍 Actionable Tasks Detected: **0**\n\n"
                "✅ **No messages found in room history.**\n\n"
                "*(Automated triage job executed on Switch Core)*"
            )
            return {
                "status": "empty",
                "total_raw_scanned": 0,
                "total_cleaned_scanned": 0,
                "tasks_count": 0,
                "auto_executed": [],
                "human_todos": [],
                "digest_markdown": empty_digest,
            }

        # 2. Filter out bot messages, handoff envelopes, and system notices
        cleaned_messages: List[SlackMessage] = []
        for raw in raw_messages:
            sender = raw.get("sender") or raw.get("sender_name") or "unknown"
            sender_name = raw.get("sender_name") or sender
            body = raw.get("body") or ""
            ts = str(raw.get("timestamp") or time.time())

            # Skip TaskFlow agents or Switch server notices
            if sender_name.lower() in ("taskflow-triage", "taskflow-worker", "taskflow", "switch-server"):
                continue
            if "@switch-agent-" in sender.lower():
                continue
            if "<!-- TASKFLOW_HANDOFF:" in body or "[TASK_HANDOFF]" in body or "[TEST_HANDOFF]" in body:
                continue
            if "📋 **taskflow channel triage digest**" in body.lower():
                continue

            cleaned_messages.append(
                SlackMessage(
                    user_id=sender,
                    user_name=sender_name,
                    text=body,
                    ts=ts,
                )
            )

        logger.info("Filtered to %d relevant human/team messages for triage.", len(cleaned_messages))
        if not cleaned_messages:
            logger.info("No non-agent team messages found to triage.")
            clean_digest = (
                "📋 **TaskFlow Channel Triage Digest**\n"
                f"*Scanned {len(raw_messages)} messages from room backlog*\n\n"
                "### 📊 Scan Statistics:\n"
                f"• 💬 Messages Analyzed: **{len(raw_messages)}**\n"
                "• 🔍 Actionable Tasks Detected: **0**\n"
                "• ⚡ Knowledge Deliverables Auto-Drafted: **0**\n"
                "• 📌 Human Action Plans Synced to Todoist: **0**\n\n"
                "✅ **Channel is fully up to date!** No pending human tasks or unaddressed action items detected in the scanned backlog.\n\n"
                "*(Automated triage job executed on Switch Core)*"
            )
            if post_digest and not dry_run:
                self.triage_client.post_message(body=clean_digest, room_id=self.room_id)
            return {
                "status": "up_to_date",
                "total_raw_scanned": len(raw_messages),
                "total_cleaned_scanned": 0,
                "tasks_count": 0,
                "auto_executed": [],
                "human_todos": [],
                "digest_markdown": clean_digest,
            }

        # 3. Analyze messages to detect tasks with deduplication
        # Walk sliding windows across cleaned messages
        triaged_tasks: List[Dict[str, Any]] = []
        deduplicator = TaskDeduplicator(ttl_seconds=86400.0, similarity_threshold=0.60)

        window_size = 5
        for i in range(len(cleaned_messages)):
            window = cleaned_messages[max(0, i - window_size + 1) : i + 1]
            target_msg = cleaned_messages[i]

            # Fast skip if message contains prompt injection
            is_injection, _ = detect_prompt_injection(target_msg.text)
            if is_injection:
                logger.warning("Skipping message with detected prompt injection from %s", target_msg.user_name)
                continue

            # Check if this message warrants triage
            body_lower = target_msg.text.lower()
            action_triggers = ("todo", "action", "please", "can you", "need to", "draft", "fix", "update", "audit")
            is_candidate = any(t in body_lower for t in action_triggers) or "@taskflow" in body_lower

            if not is_candidate:
                continue

            # Run through triage agent with active tasks context
            active_tasks = deduplicator.get_active_task_summaries(self.room_id)
            result: TaskTriageResult = self.triage_agent.triage_conversation(window, active_tasks=active_tasks)
            if not result.is_task:
                continue

            # Check for exact or semantic duplicates in this channel scan
            is_dup, reason = deduplicator.is_duplicate(
                room_id=self.room_id,
                action_summary=result.action_summary,
                trigger_ts=target_msg.ts,
            )
            if is_dup:
                logger.info("🛑 Deduplicating task during channel scan: '%s' (%s)", result.action_summary, reason)
                continue

            deduplicator.record_task(
                room_id=self.room_id,
                action_summary=result.action_summary,
                trigger_ts=target_msg.ts,
                execution_type=result.execution_type.value,
            )

            triaged_tasks.append({
                "result": result,
                "context_window": window,
                "requester": target_msg.user_name,
                "requester_id": target_msg.user_id,
                "ts": target_msg.ts,
            })

        logger.info("Identified %d actionable tasks in channel history.", len(triaged_tasks))

        # 4. Execute Tasks
        auto_executed: List[Dict[str, Any]] = []
        human_todos: List[Dict[str, Any]] = []

        for item in triaged_tasks:
            res: TaskTriageResult = item["result"]
            window: List[SlackMessage] = item["context_window"]
            requester: str = item["requester"]

            if res.execution_type == ExecutionType.AUTO_EXECUTE:
                logger.info("⚡ [AUTO_EXECUTE] Drafting deliverable: '%s'", res.action_summary)
                exec_result: WorkerExecutionResult = self.worker_agent.execute_task(res, window)

                # Mandatory Pre-Publication Safety Policy Gate
                is_safe, violations, _ = validate_auto_post_safety(
                    text=exec_result.artifact_content,
                    artifact_title=res.action_summary,
                    requester=requester
                )
                if not is_safe:
                    logger.critical("🚨 [Security Policy] BLOCKED deliverable in Channel Triage Job: %s", violations)
                    auto_executed.append({
                        "title": res.action_summary,
                        "category": res.execution_category,
                        "requester": requester,
                        "artifact_title": "Blocked by Security Policy",
                        "content_preview": f"Suppressed due to security policy violations: {', '.join(violations)}",
                        "status": "BLOCKED_SECURITY_POLICY"
                    })
                    continue

                if not dry_run:
                    # Broadcast completed knowledge deliverable to room
                    self.worker_client.post_message(
                        body=exec_result.artifact_content,
                        room_id=self.room_id
                    )

                auto_executed.append({
                    "title": res.action_summary,
                    "category": res.execution_category,
                    "requester": requester,
                    "artifact_title": exec_result.artifact_title,
                    "content_preview": exec_result.artifact_content[:200] + "...",
                })

            else:  # HUMAN_TODO
                logger.info("📌 [HUMAN_TODO] Scaffolding action checklist & Todoist sync: '%s'", res.action_summary)
                exec_result: WorkerExecutionResult = self.worker_agent.execute_task(res, window)
                assignee = res.assignee_name or requester

                todoist_task = None
                task_url = "https://todoist.com/app"
                if not dry_run and self.todoist_client.api_token:
                    todoist_task = self.todoist_client.create_task(
                        content=res.action_summary,
                        description=exec_result.artifact_content,
                        due_string=res.deadline
                    )
                    if todoist_task and getattr(todoist_task, "url", None):
                        task_url = todoist_task.url

                receipt = (
                    f"@{assignee} 🎯 **Task Tracked from Channel Triage**\n\n"
                    f"**Task:** {res.action_summary}\n"
                    f"**Assignee:** @{assignee}\n"
                    f"**Due Date:** {res.deadline or 'Not specified'}\n"
                    f"**Todoist Task:** [Open in Todoist]({task_url})\n\n"
                    f"📋 **Action Checklist:**\n{exec_result.artifact_content}\n\n"
                    f"*(Triaged during Channel Backlog Scan)*"
                )

                if not dry_run:
                    self.worker_client.send_targeted_message(
                        body=receipt,
                        target_names=[assignee, requester],
                        room_id=self.room_id
                    )

                human_todos.append({
                    "title": res.action_summary,
                    "assignee": assignee,
                    "deadline": res.deadline or "None",
                    "task_url": task_url,
                    "checklist": exec_result.artifact_content,
                })

        # 5. Build Channel Triage Digest Markdown
        digest_lines = [
            "📋 **TaskFlow Channel Triage Digest**",
            f"*Scanned {len(raw_messages)} messages from room backlog*",
            "",
            "### 📊 Scan Statistics:",
            f"• 💬 Messages Analyzed: **{len(cleaned_messages)}**",
            f"• 🔍 Actionable Tasks Detected: **{len(triaged_tasks)}**",
            f"• ⚡ Knowledge Deliverables Auto-Drafted: **{len(auto_executed)}**",
            f"• 📌 Human Action Plans Synced to Todoist: **{len(human_todos)}**",
            "",
            "---",
        ]

        if auto_executed:
            digest_lines.append("\n### ⚡ Auto-Executed Deliverables:")
            for item in auto_executed:
                digest_lines.append(f"- **{item['title']}** (`{item['category']}`)")
                digest_lines.append(f"  *Drafted and broadcast to room by @taskflow-worker*")

        if human_todos:
            digest_lines.append("\n### 📌 Human To-Dos Tracked:")
            for item in human_todos:
                digest_lines.append(f"- **{item['title']}**")
                digest_lines.append(f"  • Assignee: @{item['assignee']} | Deadline: `{item['deadline']}`")
                digest_lines.append(f"  • Todoist: [View Task in Todoist]({item['task_url']})")

        if not auto_executed and not human_todos:
            digest_lines.append("\n✅ **Channel is fully up to date!** No outstanding action items detected.")

        digest_lines.append("\n*(Automated triage job executed on Switch Core)*")
        digest_markdown = "\n".join(digest_lines)

        # 6. Post Digest Card to Room
        if post_digest and not dry_run:
            self.triage_client.post_message(body=digest_markdown, room_id=self.room_id)
            logger.info("Published Channel Triage Digest to room.")

        report = {
            "status": "success",
            "total_raw_scanned": len(raw_messages),
            "total_cleaned_scanned": len(cleaned_messages),
            "tasks_count": len(triaged_tasks),
            "auto_executed": auto_executed,
            "human_todos": human_todos,
            "digest_markdown": digest_markdown,
        }
        return report


def main():
    parser = argparse.ArgumentParser(description="TaskFlow Channel Triage Job")
    parser.add_argument("--room-id", type=str, default=None, help="Target Switch room ID")
    parser.add_argument("--limit", type=int, default=50, help="Number of messages to scan")
    parser.add_argument("--dry-run", action="store_true", help="Simulate without posting deliverables or Todoist tasks")
    parser.add_argument("--no-digest", action="store_true", help="Suppress posting the final summary digest")
    args = parser.parse_args()

    job = ChannelTriageJob(room_id=args.room_id)
    report = job.run(limit=args.limit, dry_run=args.dry_run, post_digest=not args.no_digest)
    print("\n" + "=" * 60)
    print(report["digest_markdown"])
    print("=" * 60)


if __name__ == "__main__":
    main()
