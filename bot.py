import sys
import os
import re
import json
import time
import asyncio
import logging
from collections import defaultdict, deque
from typing import Dict, Deque, Any, Optional, List, Tuple

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
from room_tasks import default_room_task_store
from approvals import default_approval_store, ApprovalStatus
from switch_client import SwitchAgentClient
from deduplication import (
    TaskDeduplicator,
    RoomBufferStore,
    default_task_deduplicator,
    default_room_buffer_store,
)
from security import validate_auto_post_safety, detect_prompt_injection
from user_resolver import default_user_resolver
from interactions import InteractionDispatcher, handle_confirm_assignment

__all__ = [
    "handle_triage_message",
    "handle_worker_message",
    "handle_confirm_assignment",
    "parse_approval_command",
    "run_triage_agent",
    "run_worker_agent",
]

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
# Backed by SQLite persistence (taskflow_state.db) across restarts
task_deduplicator = default_task_deduplicator

# Sliding window buffer per room: keeps the last 15 messages for context
# Backed by SQLite persistence (taskflow_state.db) across restarts
room_buffers = default_room_buffer_store

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

    # Determine triage activation
    is_addressed_to_us = addressed or any(tag in body.lower() for tag in ("@taskflow", "@taskflow-triage"))
    is_ambient_hit = should_ambient_triage(body)

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

    # Check if triage encountered an LLM provider or model error (DO NOT silently classify as 'not a task')
    if triage_result.error:
        logger.error(
            "❌ [Triage] LLM classification failure on message from @%s: %s",
            sender_name,
            triage_result.error
        )
        if is_addressed_to_us:
            err_notice = (
                f"⚠️ @{sender_name} **[Triage Agent Error]**\n"
                f"Could not classify your request due to an LLM provider error:\n"
                f"```\n{triage_result.error}\n```\n"
                f"*(Please check model ID and API key configuration in `.env`)*"
            )
            await asyncio.to_thread(
                client.send_targeted_message,
                body=err_notice,
                target_names=[sender_name],
                thread_id=thread_id,
                room_id=room_id
            )
        return

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
    trigger_ref = str(message_id) if message_id else msg_obj.ts
    is_dup, dup_reason = task_deduplicator.is_duplicate(
        room_id=room_id,
        action_summary=triage_result.action_summary,
        trigger_ts=trigger_ref,
    )
    if not is_dup and task_deduplicator.has_seen_trigger(room_id, msg_obj.ts):
        is_dup = True
        dup_reason = f"Message timestamp '{msg_obj.ts}' was already processed into a task"

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
        trigger_ts=trigger_ref,
        execution_type=triage_result.execution_type.value,
    )
    if message_id and msg_obj.ts != trigger_ref:
        task_deduplicator.record_seen_trigger(room_id, msg_obj.ts)

    # TASK DETECTED!
    # Instead of an in-process Python call, hand off via a Switch targeted room message
    participants = await asyncio.to_thread(client.list_participants, room_id)
    assignee_name, assignee_id = default_user_resolver.resolve_assignee(
        triage_assignee_name=triage_result.assignee_name,
        triage_assignee_id=triage_result.assignee_id,
        message_text=body,
        sender_id=sender,
        sender_name=sender_name,
        participants=participants,
    )

    handoff = TaskHandoffMessage(
        task_title=triage_result.action_summary,
        execution_type=triage_result.execution_type,
        confidence=triage_result.confidence,
        assignee_name=assignee_name,
        assignee_id=assignee_id,
        assigner_name=sender_name,
        assigner_id=sender,
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


def parse_approval_command(body: str, addressed: bool = False) -> Optional[Tuple[str, str]]:
    """Extracts (action, approval_id) if message contains an approval command for taskflow-worker.
    
    Supports commands like:
    - @taskflow-worker approve appr_...
    - @taskflow-worker dismiss appr_...
    - @taskflow-worker reject appr_...
    - approve appr_... / dismiss appr_... (when addressed or directed to worker)
    """
    clean_body = body.strip()

    # Pattern 1: Explicit mention of @taskflow-worker with command
    m = re.search(
        r"@?taskflow-worker\b.*?\b(approve|dismiss|reject|confirm|accept)\s+(appr_[a-f0-9]+)",
        clean_body,
        re.IGNORECASE
    )
    if m:
        action = m.group(1).lower()
        appr_id = m.group(2)
        if action == "reject":
            action = "dismiss"
        elif action == "accept":
            action = "confirm"
        return action, appr_id

    # Pattern 2: Directly addressed to worker or command syntax starting with command
    if addressed or clean_body.lower().startswith(("approve appr_", "dismiss appr_", "reject appr_", "confirm appr_", "accept appr_")):
        m2 = re.search(
            r"\b(approve|dismiss|reject|confirm|accept)\s+(appr_[a-f0-9]+)",
            clean_body,
            re.IGNORECASE
        )
        if m2:
            action = m2.group(1).lower()
            appr_id = m2.group(2)
            if action == "reject":
                action = "dismiss"
            elif action == "accept":
                action = "confirm"
            return action, appr_id

    return None


# ==============================================================================
# AGENT 2: TaskFlow Worker (The Hands - Autonomous Drafts & Todoist Scaffolder)
# ==============================================================================
async def handle_worker_message(client: SwitchAgentClient, payload: Dict[str, Any]):
    """Processes room messages for TaskFlow Worker: receives handoff, handles approval commands, and executes tasks."""
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

    # 1. Check for interactive approval commands in the room (@taskflow-worker approve/dismiss appr_...)
    cmd = parse_approval_command(body, addressed)
    if cmd:
        action, approval_id = cmd
        logger.info("🎯 [Worker] Received approval command '%s' for '%s' from @%s (%s)", action, approval_id, sender_name, sender)

        dispatcher = InteractionDispatcher(
            approval_store=default_approval_store,
            switch_client=client,
            todoist_client=todoist
        )
        interaction_payload = {
            "user": {
                "id": sender,
                "name": sender_name,
            },
            "actions": [{
                "action_id": f"cmd_{action}_{approval_id}",
                "value": json.dumps({"approval_id": approval_id, "action": action}),
            }],
            "room_id": room_id,
            "thread_id": thread_id,
        }
        res = await asyncio.to_thread(dispatcher.handle_interaction, interaction_payload)

        if not res.get("success"):
            status = res.get("status", "")
            if status == "NOT_FOUND":
                msg = f"⚠️ **Approval Not Found or Expired**\nApproval ID `{approval_id}` does not exist in the active store."
            elif status.startswith("ALREADY_"):
                msg = f"ℹ️ **Approval Already Resolved**\nApproval ID `{approval_id}` was already {status.replace('ALREADY_', '').lower()}."
            elif status == "UNAUTHORIZED" or not res.get("authorized"):
                msg = (
                    f"⚠️ **Action Refused: Unauthorized**\n\n"
                    f"Sorry @{sender_name}! You do not have permission to {action} `{approval_id}`.\n"
                    f"• {res.get('reason', 'Unauthorized')}\n\n"
                    f"*(Approval locked to designated assignee, assigner, or room role holders)*"
                )
            elif status == "BLOCKED_SECURITY_POLICY":
                msg = f"🛡️ **[Security Policy] Action Blocked**\n{res.get('reason', 'Security violation: deliverable failed safety verification.')}"
            else:
                msg = f"⚠️ Could not complete {action} on `{approval_id}`: {res.get('reason', 'Unknown error')}"

            await asyncio.to_thread(
                client.post_message,
                body=msg,
                thread_id=thread_id,
                room_id=room_id
            )
            return

        # If success and action was dismiss
        if action == "dismiss":
            doc_id = res['approval'].switch_doc_id if res.get('approval') else None
            doc_line = f"• **Room Document:** `{doc_id}` [Dismissed]\n" if doc_id else ""
            dismiss_msg = (
                f"❌ **TaskFlow Deliverable Dismissed**\n\n"
                f"• **Approval ID:** `{approval_id}`\n"
                f"• **Deliverable:** {res['approval'].artifact_title or res['approval'].action_summary if res.get('approval') else 'Task'}\n"
                f"{doc_line}"
                f"*(Dismissed by @{sender_name})*"
            )
            await asyncio.to_thread(
                client.post_message,
                body=dismiss_msg,
                thread_id=thread_id,
                room_id=room_id
            )
            logger.info("❌ [Worker] Dismissed approval %s by @%s", approval_id, sender_name)
            return

        # If success and action was approve or confirm
        appr_obj = res.get("approval")
        is_assignment = getattr(appr_obj, "action_type", "") in ("ASSIGNMENT", "HUMAN_TODO") or action in ("confirm", "accept")

        if is_assignment and appr_obj:
            task_id_line = f"• **Task ID:** `{res.get('task_id')}`\n" if res.get('task_id') else ""
            todoist_url = res.get('todoist_url')
            todoist_error = res.get('todoist_error')
            if todoist_url:
                sync_line = f"• **External Sync:** [View in Todoist]({todoist_url})\n"
            elif todoist_error:
                sync_line = f"• ⚠️ **External Sync Notice:** {todoist_error}\n"
            else:
                sync_line = "• **External Sync:** Native room only\n"

            confirm_receipt = (
                f"🎯 **Task Confirmed & Assigned**\n\n"
                f"• **Task:** {appr_obj.action_summary}\n"
                f"• **Assignee:** @{appr_obj.assignee_name}\n"
                f"• **Assigned by:** @{sender_name}\n"
                f"• **Due Date:** {appr_obj.deadline or 'Not specified'}\n"
                f"{task_id_line}"
                f"{sync_line}\n"
                f"*(Confirmed by @{sender_name}; action checklist forwarded to @{appr_obj.assignee_name})*"
            )
            await asyncio.to_thread(
                client.post_message,
                body=confirm_receipt,
                thread_id=thread_id,
                room_id=room_id
            )

        task_deduplicator.mark_completed(
            room_id=room_id or client.room_id or "",
            action_summary=res["approval"].action_summary if res.get("approval") else approval_id
        )
        logger.info("✅ [Worker] Successfully %sd and resolved '%s' in room [%s] by @%s", action, approval_id, room_id, sender_name)
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
        assignee_id=handoff.assignee_id,
        deadline=handoff.deadline,
    )
    context_msgs = [SlackMessage(**m) for m in (handoff.recent_context or [])]

    # Execute task according to autonomy mode
    if handoff.execution_type == ExecutionType.AUTO_EXECUTE:
        logger.info("✍️ [Worker] Autonomously drafting deliverable for: '%s'...", handoff.task_title)
        worker_result: WorkerExecutionResult = await asyncio.to_thread(
            worker_executor.execute_task,
            triage_result,
            context_msgs,
            None,
            client,
            room_id
        )

        # Mandatory Pre-Publication Safety Policy Gate (DLP / Anti-Exfil / Anti-Jailbreak)
        is_safe, violations, _ = validate_auto_post_safety(
            text=worker_result.artifact_content,
            artifact_title=worker_result.artifact_title,
            requester=handoff.requester_name
        )
        if not is_safe:
            logger.critical("🚨 [Security Policy] BLOCKED deliverable '%s': %s", handoff.task_title, violations)
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

        # Persist draft deliverable natively as a Switch Room Document
        room_doc = await asyncio.to_thread(
            client.create_room_document,
            title=worker_result.artifact_title or handoff.task_title,
            content=worker_result.artifact_content,
            room_id=handoff.room_id or client.room_id,
            metadata={
                "status": "DRAFT",
                "task_title": handoff.task_title,
                "assignee": handoff.assignee_name,
                "assigner": handoff.assigner_name,
                "category": handoff.execution_category,
            }
        )
        switch_doc_id = None
        if isinstance(room_doc, dict):
            raw_id = room_doc.get("document_id") or room_doc.get("doc_id")
            if raw_id and isinstance(raw_id, str):
                switch_doc_id = raw_id

        # Wire AUTO_EXECUTE to approval store: Worker posts a short card + approval_id, not the final broadcast!
        approval = default_approval_store.create_approval(
            action_summary=handoff.task_title,
            artifact_content=worker_result.artifact_content,
            artifact_title=worker_result.artifact_title or handoff.task_title,
            action_type="AUTO_EXECUTE",
            assignee_name=handoff.assignee_name,
            assignee_id=handoff.assignee_id,
            assigner_name=handoff.assigner_name,
            assigner_id=handoff.assigner_id,
            category=handoff.execution_category or "Knowledge Deliverable",
            deadline=handoff.deadline,
            context_notes=handoff.context_notes,
            room_id=handoff.room_id or client.room_id or "",
            message_ts=handoff.thread_id,
            switch_doc_id=switch_doc_id,
        )

        # Extract concise preview snippet (skip title headers)
        preview_lines = [l.strip() for l in worker_result.artifact_content.splitlines() if l.strip() and not l.strip().startswith("#")]
        preview_text = preview_lines[0] if preview_lines else "Deliverable drafted and awaiting review."
        if len(preview_text) > 160:
            preview_text = preview_text[:157] + "..."

        doc_info = f"• **Room Document:** `{switch_doc_id}` (Draft saved to Switch Workroom)\n" if switch_doc_id else ""
        short_proposal_card = (
            f"⚡ **TaskFlow Deliverable Drafted (Pending Approval)**\n\n"
            f"• **Deliverable:** {worker_result.artifact_title or handoff.task_title}\n"
            f"• **Task:** {handoff.task_title}\n"
            f"• **Assignee:** @{handoff.assignee_name}\n"
            f"• **Assigned by:** @{handoff.assigner_name}\n"
            f"• **Approval ID:** `{approval.approval_id}`\n"
            f"{doc_info}"
            f"• **Status:** `PENDING_APPROVAL`\n\n"
            f"📝 **Preview:**\n> {preview_text}\n\n"
            f"👉 **To approve & publish to room:**\n`@taskflow-worker approve {approval.approval_id}`\n\n"
            f"👉 **To dismiss:**\n`@taskflow-worker dismiss {approval.approval_id}`\n\n"
            f"*(Approval locked to @{handoff.assignee_name}, @{handoff.assigner_name}, or room role holders)*"
        )

        await asyncio.to_thread(
            client.post_message,
            body=short_proposal_card,
            thread_id=handoff.thread_id,
            room_id=handoff.room_id
        )
        logger.info("📋 [Worker] Created pending approval %s and posted proposal card to room [%s].", approval.approval_id, handoff.room_id)

    elif handoff.execution_type == ExecutionType.HUMAN_TODO:
        logger.info("📋 [Worker] Scaffolding action plan for: '%s'...", handoff.task_title)
        worker_result: WorkerExecutionResult = await asyncio.to_thread(
            worker_executor.execute_task,
            triage_result,
            context_msgs,
            None,
            client,
            room_id
        )

        is_medium = (
            handoff.confidence == ConfidenceLevel.MEDIUM
            or str(getattr(handoff.confidence, "value", handoff.confidence)).upper() == "MEDIUM"
        )

        if is_medium:
            # Medium-confidence path: Hold task for assigner confirmation before paging assignee!
            # Keep the full AI worker-generated action checklist in the approval artifact and preview.
            approval = default_approval_store.create_approval(
                action_type="ASSIGNMENT",
                action_summary=handoff.task_title,
                artifact_title=f"Task Assignment: {handoff.task_title}",
                artifact_content=worker_result.artifact_content,
                assignee_name=handoff.assignee_name,
                assignee_id=handoff.assignee_id,
                assigner_name=handoff.assigner_name,
                assigner_id=handoff.assigner_id,
                category=handoff.execution_category or "Human Task",
                deadline=handoff.deadline,
                context_notes=handoff.context_notes,
                room_id=handoff.room_id or client.room_id or "",
                message_ts=handoff.thread_id,
                allowed_user_names=[handoff.assigner_name] if handoff.assigner_name else [],
                allowed_user_ids=[handoff.assigner_id] if handoff.assigner_id else [],
                allowed_roles=["owner", "admin", "lead", "operator"],
            )

            confirm_card = (
                f"🤔 **TaskFlow: Did you mean to assign this? (Confirmation Needed)**\n\n"
                f"• **Task:** {handoff.task_title}\n"
                f"• **Suggested Assignee:** @{handoff.assignee_name}\n"
                f"• **Assigned by:** @{handoff.assigner_name}\n"
                f"• **Due Date:** {handoff.deadline or 'Not specified'}\n"
                f"• **Confidence:** `MEDIUM` (Implicit or ambiguous request)\n"
                f"• **Approval ID:** `{approval.approval_id}`\n\n"
                f"📋 **Proposed Action Plan Checklist:**\n"
                f"{worker_result.artifact_content}\n\n"
                f"👉 **To confirm assignment & notify @{handoff.assignee_name}:**\n"
                f"`@taskflow-worker confirm {approval.approval_id}` (or `@taskflow-worker approve {approval.approval_id}`)\n\n"
                f"👉 **To dismiss:**\n"
                f"`@taskflow-worker dismiss {approval.approval_id}`\n\n"
                f"*(Confirmation locked to assigner @{handoff.assigner_name} or room roles)*"
            )

            target_users = [handoff.assigner_name] if handoff.assigner_name else []
            await asyncio.to_thread(
                client.send_targeted_message,
                body=confirm_card,
                target_names=target_users,
                thread_id=handoff.thread_id,
                room_id=handoff.room_id
            )
            logger.info("🤔 [Worker] Medium-confidence task '%s' held for assigner confirmation (%s). Assignee @%s not paged.",
                        handoff.task_title, approval.approval_id, handoff.assignee_name)
            return

        # 1. Register task natively in Switch Room Task Store (High Confidence path)
        room_task = default_room_task_store.create_task(
            title=handoff.task_title,
            room_id=handoff.room_id or client.room_id or "",
            assignee_name=handoff.assignee_name,
            assignee_id=handoff.assignee_id,
            assigner_name=handoff.assigner_name,
            assigner_id=handoff.assigner_id,
            deadline=handoff.deadline,
            execution_type=handoff.execution_type.value if hasattr(handoff.execution_type, "value") else str(handoff.execution_type),
            action_plan=worker_result.artifact_content,
            context_notes=handoff.context_notes,
        )

        task_description = (
            f"Assigned by: @{handoff.assigner_name}\n"
            f"Context: {handoff.context_notes or 'Captured via TaskFlow on Switch'}\n\n"
            f"Action Plan Checklist:\n{worker_result.artifact_content}"
        )

        # 2. Optional Todoist Sync with 4xx diagnostics and idempotency
        sync_res = await asyncio.to_thread(
            todoist.sync_task,
            content=f"[{handoff.assignee_name}] {handoff.task_title}",
            description=task_description,
            due_string=handoff.deadline,
            external_id=room_task.task_id,
        )

        task_url = sync_res.task_url if sync_res.success else None
        if task_url:
            room_task.todoist_url = task_url
            default_room_task_store._save()
            tracker_line = f"• **External Sync:** [View in Todoist]({task_url})\n\n"
        elif sync_res.error_message:
            room_task.todoist_error = sync_res.error_message
            default_room_task_store._save()
            if sync_res.is_client_error:
                tracker_line = f"• ⚠️ **External Sync Notice:** {sync_res.error_message}\n\n"
            else:
                tracker_line = f"• **External Sync:** Native room only ({sync_res.error_message})\n\n"
        else:
            tracker_line = "• **External Sync:** Native room only (Todoist unconfigured)\n\n"

        receipt_text = (
            f"🎯 **Task Tracked in Switch Room**\n\n"
            f"• **Task ID:** `{room_task.task_id}`\n"
            f"• **Task:** {handoff.task_title}\n"
            f"• **Assignee:** @{handoff.assignee_name}\n"
            f"• **Assigned by:** @{handoff.assigner_name}\n"
            f"• **Due Date:** {handoff.deadline or 'Not specified'}\n"
            f"• **Status:** `{room_task.status.value}` (Native Switch Workroom)\n"
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
        logger.info("✅ [Worker] Recorded Switch room task %s & sent targeted receipt to %s.", room_task.task_id, target_users)


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

    # Startup probe of LLM providers & models (verifies model IDs & credentials)
    logger.info("🔍 Probing configured LLM model IDs at startup...")
    healthy, probe_msg = gateway.probe_active_provider()
    if healthy:
        logger.info("🤖 [Startup Probe] Active LLM: %s", probe_msg)
    else:
        logger.warning("⚠️ [Startup Probe] LLM Probe Warning: %s", probe_msg)

    if args.agent == "triage":
        logger.info("Starting TaskFlow Triage Agent...")
        triage = await run_triage_agent()
        active_room = SWITCH_ROOM_ID or triage.room_id
        try:
            triage.apply_room_briefing(room_id=active_room)
            logger.info("📋 [Switch Room] Applied ROOM_BRIEFING.md on connect via update_room_instructions")
        except Exception as e:
            logger.debug("Could not apply ROOM_BRIEFING.md on startup: %s", e)
        await triage.connect_and_listen()
    elif args.agent == "worker":
        logger.info("Starting TaskFlow Worker Agent...")
        worker = await run_worker_agent()
        active_room = SWITCH_ROOM_ID or worker.room_id
        try:
            worker.apply_room_briefing(room_id=active_room)
            logger.info("📋 [Switch Room] Applied ROOM_BRIEFING.md on connect via update_room_instructions")
        except Exception as e:
            logger.debug("Could not apply ROOM_BRIEFING.md on startup: %s", e)
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

        # Apply ROOM_BRIEFING.md on connect via update_room_instructions
        active_room = SWITCH_ROOM_ID or triage.room_id
        try:
            triage.apply_room_briefing(room_id=active_room)
            logger.info("📋 [Switch Room] Applied ROOM_BRIEFING.md on connect via update_room_instructions")
        except Exception as e:
            logger.debug("Could not apply ROOM_BRIEFING.md on startup: %s", e)

        # Attach default room references (GitHub repo & ticket tracker) on Switch
        try:
            worker.attach_default_room_references(active_room)
        except Exception as e:
            logger.debug("Could not attach default room references: %s", e)

        # Inspect if Switch has an active Slack collaboration bridge connected
        try:
            bridge_info = triage.get_room_bridge_info(active_room)
            if bridge_info.get("is_slack"):
                logger.info(
                    "🔗 [Switch Bridge] Slack collaboration bridge active for room '%s' (bridge: %s, external channel: %s). Slack events enabled via Switch.",
                    active_room,
                    bridge_info.get("bridge_name"),
                    bridge_info.get("external_channel_id"),
                )
            elif bridge_info.get("connected"):
                logger.info(
                    "🔗 [Switch Bridge] External bridge '%s' active for room '%s'.",
                    bridge_info.get("bridge_name"),
                    active_room,
                )
            else:
                logger.info(
                    "ℹ️ [Switch Native] Pure Switch Workroom mode active (Slack bridge not attached to room '%s'). Zero Slack tokens required; TaskFlow runs natively without a parallel Bolt app.",
                    active_room,
                )
        except Exception as e:
            logger.debug("Could not resolve bridge info on startup: %s", e)

        await asyncio.gather(
            triage.connect_and_listen(),
            worker.connect_and_listen()
        )

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("TaskFlow shut down gracefully.")
