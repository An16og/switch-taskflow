import sys
import os
import re
import json
import time
import asyncio
import logging
from collections import defaultdict, deque
from typing import Dict, Deque, Any, Optional, List

# Ensure UTF-8 output on Windows console
if sys.stdout.encoding != 'utf-8':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

from config import (
    SWITCH_SERVER_URL,
    AGENT_REGISTRATION_TOKEN,
    SWITCH_ROOM_ID,
    SWITCH_TRIAGE_AGENT_NAME,
    SWITCH_TRIAGE_AGENT_ID,
    SWITCH_TRIAGE_AGENT_API_KEY,
    SWITCH_WORKER_AGENT_NAME,
    SWITCH_WORKER_AGENT_ID,
    SWITCH_WORKER_AGENT_API_KEY,
    AMBIENT_TRIAGE_POLICY,
    check_env
)
from models import (
    SlackMessage,
    ConfidenceLevel,
    ExecutionType,
    TaskTriageResult,
    TaskHandoffMessage,
    WorkerExecutionResult
)
from llm_gateway import LLMGateway
from agents.triage_agent import TriageAgent
from agents.worker_agent import WorkerAgent
from todoist_client import TodoistClient
from switch_client import SwitchAgentClient
from deduplication import TaskDeduplicator
from security import validate_auto_post_safety, detect_prompt_injection

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("TaskFlow")

# Validate environment variables before running
missing = check_env()
if not missing:
    logger.warning("Some environment variables are missing; check .env if errors occur.")

# Initialize LLM Gateway & Worker Components
gateway = LLMGateway()
triage_classifier = TriageAgent(gateway=gateway)
worker_executor = WorkerAgent(gateway=gateway)
todoist = TodoistClient()

# Task Deduplication engine per room (30-minute cooldown, 60% semantic similarity threshold)
task_deduplicator = TaskDeduplicator(ttl_seconds=1800.0, similarity_threshold=0.60)

# Sliding window buffer per room: keeps the last 15 messages for context
room_buffers: Dict[str, Deque[SlackMessage]] = defaultdict(lambda: deque(maxlen=15))

# Action keywords for ambient triage policy
ACTION_KEYWORDS = [
    "todo", "task", "fix", "deploy", "release", "ship", "bug", "action item",
    "please create", "needs to be done", "investigate", "write doc", "document",
    "audit", "update", "checklist"
]

def should_ambient_triage(body: str) -> bool:
    """Evaluates whether an unaddressed room message meets the ambient triage policy."""
    if AMBIENT_TRIAGE_POLICY == "all":
        return True
    if AMBIENT_TRIAGE_POLICY == "addressed_only":
        return False
    body_lower = body.lower()
    return any(kw in body_lower for kw in ACTION_KEYWORDS)


# ==============================================================================
# AGENT 1: TaskFlow Triage (The Ear - Ambient / High-Recall Classifier)
# ==============================================================================
async def handle_triage_message(client: SwitchAgentClient, payload: Dict[str, Any]):
    """Processes room messages for TaskFlow Triage: detects tasks and hands off to Worker."""
    body = payload.get("body", "").strip()
    if not body:
        return

    sender = payload.get("sender", "")
    sender_name = payload.get("sender_name", "") or sender or "Team Member"
    addressed = payload.get("addressed", False)
    room_id = payload.get("room_id", client.room_id)
    message_id = payload.get("id") or payload.get("message_id")
    thread_id = payload.get("thread_id") or message_id

    # Never process messages originating from TaskFlow agents themselves
    if sender_name.lower() in ("taskflow-triage", "taskflow-worker", "taskflow") or sender in (client.agent_id, SWITCH_WORKER_AGENT_ID):
        return

    logger.info("👂 [Triage] Room message from %s (addressed=%s): %s", sender_name, addressed, body[:60])

    # Security Guardrail: Check for prompt injection or rule override attempts
    is_inj, inj_reason = detect_prompt_injection(body)
    if is_inj:
        logger.warning("🚨 [Triage] BLOCKED prompt injection from %s: %s", sender_name, inj_reason)
        if is_addressed_to_us:
            sec_reply = (
                f"🛡️ @{sender_name} **[Security Policy Block]**\n"
                f"Your request was flagged by the Switch Trust Guardrails policy for containing an adversarial instruction override or prompt injection signature: `{inj_reason}`."
            )
            await asyncio.to_thread(
                client.send_targeted_message,
                body=sec_reply,
                target_names=[sender_name],
                thread_id=thread_id,
                room_id=room_id
            )
        return

    # Record message in the sliding context buffer
    msg_obj = SlackMessage(
        user_id=sender,
        user_name=sender_name,
        text=body,
        ts=str(payload.get("timestamp") or time.time()),
    )
    room_buffers[room_id].append(msg_obj)

    # Determine triage activation
    is_addressed_to_us = addressed or any(tag in body.lower() for tag in ("@taskflow", "@taskflow-triage"))
    is_ambient_hit = should_ambient_triage(body)

    if not is_addressed_to_us and not is_ambient_hit:
        logger.debug("[Triage] Ignored message (ambient triage policy not met).")
        return

    trigger_type = "DIRECTLY ADDRESSED" if is_addressed_to_us else "AMBIENT TRIAGE"
    logger.info("⚡ [Triage] Triaging message via [%s]...", trigger_type)

    # Check for On-Demand Channel Triage Job trigger
    body_lower = body.lower()
    if is_addressed_to_us and any(cmd in body_lower for cmd in ("triage this channel", "triage channel", "/triage-channel", "run channel triage")):
        logger.info("📋 [Triage] Channel Triage Job triggered by %s for room %s", sender_name, room_id)
        await asyncio.to_thread(
            client.post_message,
            body=f"🔍 @{sender_name} Starting full channel backlog triage scan (analyzing recent conversation history)...",
            room_id=room_id
        )
        from jobs.triage_channel import ChannelTriageJob
        job = ChannelTriageJob(triage_client=client, room_id=room_id)
        await asyncio.to_thread(job.run, limit=50, post_digest=True)
        return

    # Agent 1: Classify conversation buffer in worker thread
    recent_context = list(room_buffers[room_id])
    active_tasks = task_deduplicator.get_active_task_summaries(room_id)
    triage_result: TaskTriageResult = await asyncio.to_thread(
        triage_classifier.triage_conversation,
        recent_context,
        active_tasks=active_tasks,
    )

    logger.info(
        "[Triage] Decision: is_task=%s, type=%s, conf=%s (%s)",
        triage_result.is_task,
        triage_result.execution_type,
        triage_result.confidence,
        triage_result.confidence_reason
    )

    # If addressed directly but no actionable task was detected
    if not triage_result.is_task:
        if is_addressed_to_us:
            reply = (
                f"Hello @{sender_name}! I heard your request, but did not detect an actionable task or document generation request.\n"
                f"I can automatically coordinate **Release Notes**, **Docs**, **Bug Analyses**, or scaffold **Todoist Tasks**."
            )
            await asyncio.to_thread(
                client.send_targeted_message,
                body=reply,
                target_names=[sender_name],
                thread_id=thread_id,
                room_id=room_id
            )
        return

    # Task Deduplication Guardrail: Check if this task or trigger has already been handled
    is_dup, dup_reason = task_deduplicator.is_duplicate(
        room_id=room_id,
        action_summary=triage_result.action_summary,
        trigger_ts=msg_obj.ts,
    )
    if is_dup:
        logger.info(
            "🛑 [Triage] Suppressing duplicate task: '%s' (%s)",
            triage_result.action_summary,
            dup_reason
        )
        if is_addressed_to_us:
            dup_notice = (
                f"@{sender_name} ℹ️ Task **'{triage_result.action_summary}'** was already triaged recently.\n"
                f"*(Deduplication active: {dup_reason})*"
            )
            await asyncio.to_thread(
                client.send_targeted_message,
                body=dup_notice,
                target_names=[sender_name],
                thread_id=thread_id,
                room_id=room_id
            )
        return

    # Record the newly detected task in deduplication registry
    task_deduplicator.record_task(
        room_id=room_id,
        action_summary=triage_result.action_summary,
        trigger_ts=msg_obj.ts,
        execution_type=triage_result.execution_type.value,
    )

    # TASK DETECTED!
    # Instead of an in-process Python call, hand off via a Switch targeted room message
    assignee = triage_result.assignee_name or sender_name
    handoff = TaskHandoffMessage(
        task_title=triage_result.action_summary,
        execution_type=triage_result.execution_type,
        confidence=triage_result.confidence,
        assignee_name=assignee,
        assigner_name=sender_name,
        deadline=triage_result.deadline,
        context_notes=triage_result.context_notes,
        execution_category=triage_result.execution_category,
        thread_id=thread_id,
        room_id=room_id,
        requester_name=sender_name,
        recent_context=[m.model_dump() for m in recent_context[-5:]],
    )

    handoff_body = (
        f"@taskflow-worker [TASK_HANDOFF]\n"
        f"• **Task:** {handoff.task_title}\n"
        f"• **Autonomy Mode:** `{handoff.execution_type.value}`\n"
        f"• **Assignee:** @{handoff.assignee_name}\n"
        f"• **Requested by:** @{handoff.assigner_name}\n"
        f"• **Deadline:** {handoff.deadline or 'None'}\n"
        f"• **Category:** {handoff.execution_category}\n\n"
        f"<!-- TASKFLOW_HANDOFF: {handoff.model_dump_json()} -->"
    )

    logger.info("🚀 [Triage] Handing off task to @taskflow-worker via Switch targeted message...")
    await asyncio.to_thread(
        client.send_targeted_message,
        body=handoff_body,
        target_names=["taskflow-worker"],
        thread_id=thread_id,
        room_id=room_id,
    )
    logger.info("✅ [Triage] Handoff message dispatched.")


# ==============================================================================
# AGENT 2: TaskFlow Worker (The Hands - Autonomous Drafts & Todoist Scaffolder)
# ==============================================================================
async def handle_worker_message(client: SwitchAgentClient, payload: Dict[str, Any]):
    """Processes room messages for TaskFlow Worker: receives handoff and executes tasks."""
    body = payload.get("body", "").strip()
    if not body:
        return

    sender = payload.get("sender", "")
    sender_name = payload.get("sender_name", "") or sender or "Team Member"
    addressed = payload.get("addressed", False)
    room_id = payload.get("room_id", client.room_id)
    message_id = payload.get("id") or payload.get("message_id")
    thread_id = payload.get("thread_id") or message_id

    # Filter out worker's own messages
    if sender_name.lower() in ("taskflow-worker",) or sender == client.agent_id:
        return

    # Check if this message is intended for Worker
    has_handoff_block = "<!-- TASKFLOW_HANDOFF:" in body
    is_worker_target = (
        has_handoff_block or
        "[TASK_HANDOFF]" in body or
        (addressed and any(kw in body.lower() for kw in ("@taskflow-worker", "worker")))
    )

    if not is_worker_target:
        return

    logger.info("🛠️ [Worker] Received targeted work item from %s in room [%s]!", sender_name, room_id)

    # Parse handoff payload
    handoff: Optional[TaskHandoffMessage] = None
    if has_handoff_block:
        try:
            match = re.search(r"<!-- TASKFLOW_HANDOFF:\s*({.*?})\s*-->", body, re.DOTALL)
            if match:
                handoff = TaskHandoffMessage.model_validate_json(match.group(1))
        except Exception as e:
            logger.warning("[Worker] Failed parsing JSON handoff block: %s", e)

    # Fallback: synthesize handoff if addressed directly without handoff block
    if not handoff:
        handoff = TaskHandoffMessage(
            task_title=body.replace("@taskflow-worker", "").strip(),
            execution_type=ExecutionType.AUTO_EXECUTE if any(kw in body.lower() for kw in ("release notes", "doc", "draft")) else ExecutionType.HUMAN_TODO,
            confidence=ConfidenceLevel.HIGH,
            assignee_name=sender_name,
            assigner_name=sender_name,
            thread_id=thread_id,
            room_id=room_id,
            requester_name=sender_name,
            recent_context=[],
        )

    # Reconstruct TaskTriageResult for WorkerAgent
    triage_result = TaskTriageResult(
        is_task=True,
        confidence=handoff.confidence,
        confidence_reason=handoff.context_notes or "Handed off via Switch room message",
        action_summary=handoff.task_title,
        execution_type=handoff.execution_type,
        execution_category=handoff.execution_category or "General Task",
        assignee_name=handoff.assignee_name,
        deadline=handoff.deadline,
    )
    context_msgs = [SlackMessage(**m) for m in (handoff.recent_context or [])]

    # Execute task according to autonomy mode
    if handoff.execution_type == ExecutionType.AUTO_EXECUTE:
        logger.info("✍️ [Worker] Autonomously generating deliverable for: '%s'...", handoff.task_title)
        worker_result: WorkerExecutionResult = await asyncio.to_thread(
            worker_executor.execute_task,
            triage_result,
            context_msgs
        )

        # Mandatory Pre-Publication Safety Policy Gate (DLP / Anti-Exfil / Anti-Jailbreak)
        is_safe, violations, _ = validate_auto_post_safety(
            text=worker_result.artifact_content,
            artifact_title=worker_result.artifact_title,
            requester=handoff.requester_name
        )
        if not is_safe:
            logger.critical("🚨 [Security Policy] BLOCKED AUTO-POST of deliverable '%s': %s", handoff.task_title, violations)
            quarantine_notice = (
                f"🛡️ **[TaskFlow Security Guardrail] Auto-Post Blocked**\n\n"
                f"The autonomous worker drafted a deliverable for **'{handoff.task_title}'**, but it was **BLOCKED** by the Switch Trust Exfiltration & Safety Policy.\n"
                f"• **Reason:** {', '.join(violations)}\n"
                f"• **Action Taken:** Delivery suppressed to protect against prompt injection and secret exfiltration."
            )
            await asyncio.to_thread(
                client.post_message,
                body=quarantine_notice,
                thread_id=handoff.thread_id,
                room_id=handoff.room_id
            )
            return

        response_text = (
            f"{worker_result.artifact_content}\n\n"
            f"*(Generated autonomously by @taskflow-worker on Switch Core)*"
        )
        await asyncio.to_thread(
            client.post_message,
            body=response_text,
            thread_id=handoff.thread_id,
            room_id=handoff.room_id
        )
        task_deduplicator.mark_completed(
            room_id=handoff.room_id or client.room_id or "",
            action_summary=handoff.task_title
        )
        logger.info("✅ [Worker] Posted autonomous deliverable to room [%s].", handoff.room_id)

    elif handoff.execution_type == ExecutionType.HUMAN_TODO:
        logger.info("📋 [Worker] Scaffolding action plan & creating Todoist task for: '%s'...", handoff.task_title)
        worker_result: WorkerExecutionResult = await asyncio.to_thread(
            worker_executor.execute_task,
            triage_result,
            context_msgs
        )

        task_description = (
            f"Assigned by: @{handoff.assigner_name}\n"
            f"Context: {handoff.context_notes or 'Captured via TaskFlow on Switch'}\n\n"
            f"Action Plan Checklist:\n{worker_result.artifact_content}"
        )

        todoist_res = await asyncio.to_thread(
            todoist.create_task,
            content=f"[{handoff.assignee_name}] {handoff.task_title}",
            description=task_description,
            due_string=handoff.deadline,
        )

        task_url = (todoist_res or {}).get("url") if isinstance(todoist_res, dict) else None
        if task_url:
            tracker_line = f"**Todoist Task:** [Open in Todoist]({task_url})\n\n"
        else:
            tracker_line = f"**Status:** Tracked natively in Switch Workroom\n\n"

        receipt_text = (
            f"🎯 **Task Handed Off & Tracked**\n\n"
            f"**Task:** {handoff.task_title}\n"
            f"**Assignee:** @{handoff.assignee_name}\n"
            f"**Due Date:** {handoff.deadline or 'Not specified'}\n"
            f"{tracker_line}"
            f"📋 **Action Checklist:**\n{worker_result.artifact_content}\n\n"
            f"*(Scaffolded by @taskflow-worker via Switch targeted room message)*"
        )

        target_users = list(set(filter(None, [handoff.assigner_name, handoff.assignee_name])))
        await asyncio.to_thread(
            client.send_targeted_message,
            body=receipt_text,
            target_names=target_users,
            thread_id=handoff.thread_id,
            room_id=handoff.room_id
        )
        task_deduplicator.mark_completed(
            room_id=handoff.room_id or client.room_id or "",
            action_summary=handoff.task_title
        )
        logger.info("✅ [Worker] Created Todoist task & sent targeted receipt to %s.", target_users)


# ==============================================================================
# Switch Runners
# ==============================================================================
async def run_triage_agent() -> SwitchAgentClient:
    """Connects and runs the TaskFlow Triage Agent on Switch."""
    client = SwitchAgentClient(
        server_url=SWITCH_SERVER_URL,
        registration_token=AGENT_REGISTRATION_TOKEN,
        agent_id=SWITCH_TRIAGE_AGENT_ID,
        api_key=SWITCH_TRIAGE_AGENT_API_KEY,
        agent_name=SWITCH_TRIAGE_AGENT_NAME,
        display_name="TaskFlow Triage",
        description="Ambient high-recall task triage and classifier agent",
        room_id=SWITCH_ROOM_ID,
    )
    client.on_message(lambda payload: handle_triage_message(client, payload))
    return client

async def run_worker_agent() -> SwitchAgentClient:
    """Connects and runs the TaskFlow Worker Agent on Switch."""
    client = SwitchAgentClient(
        server_url=SWITCH_SERVER_URL,
        registration_token=AGENT_REGISTRATION_TOKEN,
        agent_id=SWITCH_WORKER_AGENT_ID,
        api_key=SWITCH_WORKER_AGENT_API_KEY,
        agent_name=SWITCH_WORKER_AGENT_NAME,
        display_name="TaskFlow Worker",
        description="Autonomous task executor and action scaffolder",
        room_id=SWITCH_ROOM_ID,
    )
    client.on_message(lambda payload: handle_worker_message(client, payload))
    return client

async def main():
    import argparse
    parser = argparse.ArgumentParser(description="TaskFlow on Switch Multi-Agent Daemon")
    parser.add_argument("--agent", choices=["all", "triage", "worker"], default="all", help="Which Switch agent to run")
    args = parser.parse_args()

    print("=" * 65)
    print("⚡ TASKFLOW ON SWITCH - MULTI-AGENT WORKROOM DAEMON")
    print(f"Switch Server: {SWITCH_SERVER_URL}")
    print(f"Ambient Triage Policy: {AMBIENT_TRIAGE_POLICY}")
    print("=" * 65)

    if args.agent == "triage":
        logger.info("Starting TaskFlow Triage Agent...")
        triage = await run_triage_agent()
        await triage.connect_and_listen()
    elif args.agent == "worker":
        logger.info("Starting TaskFlow Worker Agent...")
        worker = await run_worker_agent()
        await worker.connect_and_listen()
    else:
        logger.info("Starting BOTH TaskFlow Triage and Worker Agents on Switch...")
        triage = await run_triage_agent()
        worker = await run_worker_agent()
        # Set agent addressing to 'anyone' so teammates and the sibling agent can talk to them
        try:
            triage.set_agent_addressing(SWITCH_WORKER_AGENT_ID, "anyone")
            worker.set_agent_addressing(SWITCH_TRIAGE_AGENT_ID, "anyone")
        except Exception as e:
            logger.warning("Could not set addressing on startup: %s", e)

        await asyncio.gather(
            triage.connect_and_listen(),
            worker.connect_and_listen()
        )

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("TaskFlow shut down gracefully.")
