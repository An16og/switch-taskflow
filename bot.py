import sys
import os
import re
import json
import time
import uuid
import signal
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
    AMBIENT_EXTRA_KEYWORDS,
    TRIAGE_STATIC_WINDOW,
    TRIAGE_EXPANDED_WINDOW,
    HANDOFF_TTL_SECONDS,
    check_env
)
from models import (
    SlackMessage,
    ConfidenceLevel,
    ExecutionType,
    TaskTriageResult,
    TaskHandoffMessage,
    WorkerExecutionResult,
    format_deadline_with_timezone,
    extract_thread_id,
    should_skip_agent_message,
    build_todoist_payload,
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
from rate_limiter import RoomRateLimiter, default_room_rate_limiter
from security import validate_auto_post_safety, detect_prompt_injection
from user_resolver import default_user_resolver
from interactions import InteractionDispatcher, handle_confirm_assignment

__all__ = [
    "handle_triage_message",
    "handle_worker_message",
    "handle_confirm_assignment",
    "parse_approval_command",
    "parse_assign_command",
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

# Per-room rate limiter: guards against LLM cost DoS and runaway API expenditure
room_rate_limiter = default_room_rate_limiter

# Serializes compact-handoff consumption per room so two worker handlers
# cannot pick up the same queued handoff document.
room_handoff_locks: Dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

UNASSIGNED_LABELS = {"unassigned", "team", "none", "n/a", "anyone", "someone"}


def is_unassigned_assignee(name: Optional[str]) -> bool:
    """True when a task intentionally belongs to the room rather than a person."""
    return not name or name.strip().lstrip("@").lower() in UNASSIGNED_LABELS


def assignee_display(name: Optional[str]) -> str:
    """Format an assignee for room cards without pinging a placeholder name."""
    return "**Unassigned**" if is_unassigned_assignee(name) else f"@{name}"

# Action keywords for ambient triage policy. These are only a cheap first-pass
# gate: the LLM makes the final task/no-task decision. Keep them broad enough
# that an unassigned announcement ("someone should...", "we need...") is not
# silently ignored just because it has no @mention.
ACTION_KEYWORDS = [
    "todo", "task", "fix", "deploy", "release", "ship", "bug", "action item",
    "please create", "needs to be done", "investigate", "write doc", "document",
    "audit", "update", "checklist", "add", "create", "build", "implement",
    "improve", "refactor", "migrate", "test", "review", "draft", "prepare",
    "set up", "setup", "configure", "optimize", "clean up", "remove", "rename",
    "move", "design", "plan", "research", "summarize", "analyze", "check",
    "verify", "validate", "ensure", "handle", "follow up", "take care",
    "look into", "needs", "need to", "should", "must", "deadline", "due",
    "assign", "owner", "reminder", "asap", "eod", "backlog", "ticket", "issue",
    "pr ", "pull request", "readme", "docs", "documentation", "changelog",
    "announcement", "postmortem", "incident", "onboarding", "spec",
]

# Word-boundary matchers keep short keywords from firing inside unrelated
# words (e.g. "test" inside "latest").
ACTION_KEYWORD_PATTERNS = [
    re.compile(r"\b" + re.escape(kw.strip()) + r"\b", re.IGNORECASE)
    for kw in ACTION_KEYWORDS
    if kw.strip()
]
AMBIENT_EXTRA_KEYWORD_PATTERNS = [
    re.compile(r"\b" + re.escape(kw.strip()) + r"\b", re.IGNORECASE)
    for kw in AMBIENT_EXTRA_KEYWORDS
    if kw.strip()
]

# Phrases that indicate a task announcement without a specific assignee.
TASK_INTENT_PATTERNS = [
    r"\bwe\s+(need|should|must|have to|ought to)\b",
    r"\b(someone|somebody|anyone|who)\s+(should|needs?\s+to|can|could|will|has\s+to)\b",
    r"\b(can|could|would)\s+(someone|somebody|anyone|you)\b",
    r"\b(let'?s|lets)\b",
    r"\b(need|needs|needed)\s+(to\s+be\s+)?(done|updated|fixed|created|added|reviewed|tested|deployed|written|drafted|checked)\b",
    r"\bby\s+(eod|end\s+of\s+day|tomorrow|today|monday|tuesday|wednesday|thursday|friday|next\s+week)\b",
    r"\b(action\s+item|follow[-\s]?up|take\s+care\s+of|look\s+into)\b",
]


def should_ambient_triage(body: str) -> bool:
    """Evaluates whether an unaddressed room message should reach the LLM.

    This is deliberately high-recall. The LLM is responsible for rejecting
    banter; this gate only prevents the bot from spending a token on every
    single message in a busy room.
    """
    if AMBIENT_TRIAGE_POLICY == "all":
        return True
    if AMBIENT_TRIAGE_POLICY == "addressed_only":
        return False
    body_lower = (body or "").lower()
    if any(pattern.search(body_lower) for pattern in ACTION_KEYWORD_PATTERNS):
        return True
    if any(pattern.search(body_lower) for pattern in AMBIENT_EXTRA_KEYWORD_PATTERNS):
        return True
    return any(re.search(pattern, body_lower) for pattern in TASK_INTENT_PATTERNS)


def get_static_context(room_id: str) -> List[SlackMessage]:
    """The small, high-signal window used for the first triage pass."""
    return room_buffers.get_buffer(room_id, limit=TRIAGE_STATIC_WINDOW)


def get_expanded_context(room_id: str) -> List[SlackMessage]:
    """The larger window used when the latest message is a fragment."""
    return room_buffers.get_buffer(room_id, limit=TRIAGE_EXPANDED_WINDOW)


def should_expand_context(
    triage_result: TaskTriageResult,
    latest_text: str,
    static_count: int,
    total_count: int,
) -> bool:
    """Whether to retry triage with the expanded window.

    The static window is cheap and usually enough. We expand when the model
    explicitly asks for more context, or when a short fragment / task-like
    announcement was not detected in the static window but older messages are
    available.
    """
    if triage_result.error:
        return False
    if triage_result.needs_more_context:
        return True
    if total_count <= static_count or triage_result.is_task:
        return False

    text = (latest_text or "").strip().lower()
    fragment_patterns = (
        r"^(and|also|plus|but|then|so)\b",
        r"^(by|before|for|with|because|after)\b",
        r"\b(it|this|that|they|them|those|these)\b",
    )
    looks_like_fragment = len(text.split()) <= 10 or any(
        re.search(pattern, text) for pattern in fragment_patterns
    )
    looks_like_task_announcement = any(
        re.search(pattern, text) for pattern in TASK_INTENT_PATTERNS
    )
    return looks_like_fragment or looks_like_task_announcement


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
    thread_id = extract_thread_id(payload)

    # Never process messages originating from TaskFlow agents themselves, bots, or internal channel types
    if should_skip_agent_message(payload, client=client, role="triage"):
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
            thread_id=thread_id,
            room_id=room_id
        )
        from jobs.triage_channel import ChannelTriageJob
        job = ChannelTriageJob(triage_client=client, room_id=room_id)
        await asyncio.to_thread(job.run, limit=50, post_digest=True, thread_id=thread_id)
        return

    # Guardrail: Per-Room Rate Limit check (LLM Cost DoS Prevention)
    allowed, retry_after, limit_reason = room_rate_limiter.check_and_acquire(room_id, cost=1)
    if not allowed:
        logger.warning(
            "🛑 [Triage] Room [%s] exceeded LLM rate limit (Cost DoS Guardrail, retry_after=%ds): %s",
            room_id[:8] if room_id else "", retry_after, limit_reason
        )
        if is_addressed_to_us:
            throttle_reply = (
                f"🛡️ @{sender_name} **[Rate Limit: LLM Cost DoS Protection]**\n"
                f"This room has reached its LLM processing quota ({room_rate_limiter.max_requests} requests per {int(room_rate_limiter.window_seconds)}s).\n"
                f"Please retry in **{retry_after} seconds**.\n"
                f"*(Per-room rate limiting prevents runaway API costs and service starvation)*"
            )
            await asyncio.to_thread(
                client.send_targeted_message,
                body=throttle_reply,
                target_names=[sender_name],
                thread_id=thread_id,
                room_id=room_id
            )
        return

    # Agent 1: Classify a small static window first. If the latest message is
    # a fragment or the model needs more context, retry with the expanded
    # window (default 5 -> 15). The window actually used is forwarded to the
    # worker so a task split across several messages keeps its full context.
    static_context = get_static_context(room_id)
    if not static_context:
        static_context = [msg_obj]
    active_tasks = task_deduplicator.get_active_task_summaries(room_id)
    triage_result: TaskTriageResult = await asyncio.to_thread(
        triage_classifier.triage_conversation,
        static_context,
        active_tasks=active_tasks,
    )
    context_used = static_context

    expanded_context = get_expanded_context(room_id)
    if should_expand_context(
        triage_result,
        latest_text=body,
        static_count=len(static_context),
        total_count=len(expanded_context),
    ):
        expanded_allowed, _, expanded_limit_reason = room_rate_limiter.check_and_acquire(
            room_id, cost=1
        )
        if expanded_allowed:
            logger.info(
                "🧠 [Triage] Expanding context window from %d to %d messages (%s)",
                len(static_context),
                len(expanded_context),
                "needs_more_context" if triage_result.needs_more_context else "fragment/task-like message",
            )
            expanded_result: TaskTriageResult = await asyncio.to_thread(
                triage_classifier.triage_conversation,
                expanded_context,
                active_tasks=active_tasks,
            )
            if not expanded_result.error:
                triage_result = expanded_result
                context_used = expanded_context
            else:
                logger.warning(
                    "⚠️ [Triage] Expanded-window classification failed; keeping static result: %s",
                    expanded_result.error,
                )
        else:
            logger.info(
                "🧠 [Triage] Context expansion skipped by rate limiter: %s",
                expanded_limit_reason,
            )

    logger.info(
        "[Triage] Decision: is_task=%s, type=%s, conf=%s, window=%d/%d (%s)",
        triage_result.is_task,
        triage_result.execution_type,
        triage_result.confidence,
        len(context_used),
        len(expanded_context),
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
    # The LLM call may have taken a few seconds; if split-task detail arrived
    # while it was thinking, give the worker the freshest expanded window.
    fresh_context = get_expanded_context(room_id)
    if len(fresh_context) > len(context_used):
        logger.info(
            "🔄 [Triage] Refreshing handoff context with %d new message(s) that arrived during classification.",
            len(fresh_context) - len(context_used),
        )
        context_used = fresh_context

    # Instead of an in-process Python call, hand off via a Switch targeted room message
    participants = await asyncio.to_thread(client.list_participants, room_id)
    assignee_name, assignee_id = default_user_resolver.resolve_assignee(
        triage_assignee_name=triage_result.assignee_name,
        triage_assignee_id=triage_result.assignee_id,
        message_text=body,
        sender_id=sender,
        sender_name=sender_name,
        participants=participants,
        # A human task with nobody named belongs to the room, not to whoever
        # happened to announce it. People can claim/assign it later.
        prefer_unassigned=(triage_result.execution_type == ExecutionType.HUMAN_TODO),
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
        recent_context=[m.model_dump() for m in context_used],
    )

    # Keep the chat clean: store the structured handoff in the local TaskFlow
    # document store and send only a short status line plus a compact reference.
    # The old full envelope remains as a fallback if the store is unavailable.
    handoff_ref = None
    try:
        handoff_doc = client.document_store.create_document(
            title=f"TaskFlow Handoff: {handoff.task_title}",
            content=handoff.model_dump_json(),
            name=f"taskflow-handoff-{uuid.uuid4().hex[:10]}",
            description="Internal Triage -> Worker handoff payload. Safe to delete.",
            room_id=room_id,
            doc_id=f"doc_{uuid.uuid4().hex[:12]}",
            status="DRAFT",
            created_by=client.agent_name,
            metadata={
                "type": "taskflow_handoff",
                "status": "PENDING",
                "task_title": handoff.task_title,
                "execution_type": handoff.execution_type.value,
            },
        )
        handoff_ref = getattr(handoff_doc, "doc_id", None)
        if not isinstance(handoff_ref, str):
            handoff_ref = None
    except Exception as e:
        logger.warning("Could not persist compact handoff payload; using inline fallback: %s", e)

    if handoff_ref:
        # Deliberately short: the structured payload is in the handoff
        # document, so the room sees a normal status line instead of a log.
        handoff_body = "@taskflow-worker On it. [handoff]"
    else:
        handoff_body = (
            f"@taskflow-worker [TASK_HANDOFF]\n"
            f"• **Task:** {handoff.task_title}\n"
            f"• **Autonomy Mode:** `{handoff.execution_type.value}`\n"
            f"• **Assignee:** {assignee_display(handoff.assignee_name)}\n"
            f"• **Requested by:** @{handoff.assigner_name}\n"
            f"• **Deadline:** {format_deadline_with_timezone(handoff.deadline) or 'None'}\n"
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
        r"@?taskflow-worker\b.*?\b(approve|dismiss|reject|confirm|accept|save|save_to_todoist)\s+(appr_[a-f0-9]+)",
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
        elif action == "save_to_todoist":
            action = "save"
        return action, appr_id

    # Pattern 2: Directly addressed to worker or command syntax starting with command
    if addressed or clean_body.lower().startswith(("approve appr_", "dismiss appr_", "reject appr_", "confirm appr_", "accept appr_", "save appr_", "save_to_todoist appr_")):
        m2 = re.search(
            r"\b(approve|dismiss|reject|confirm|accept|save|save_to_todoist)\s+(appr_[a-f0-9]+)",
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
            elif action == "save_to_todoist":
                action = "save"
            return action, appr_id

    return None


def parse_assign_command(body: str) -> Optional[Tuple[str, str]]:
    """Parses `assign <task_id> @person` for an existing room task.

    Example: `@taskflow-worker assign sw_task_ab12cd34 @Rohit`
    """
    match = re.search(
        r"\bassign\s+(sw_task_[A-Za-z0-9_]+)\s+(?:to\s+)?@?([A-Za-z0-9_.\-]+)",
        body or "",
        re.IGNORECASE,
    )
    if match:
        return match.group(1), match.group(2).lstrip("@")
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
    thread_id = extract_thread_id(payload)

    # Filter out worker's own messages and non-handoff agent chatter
    if should_skip_agent_message(payload, client=client, role="worker"):
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
            dismiss_target_thread = (res["approval"].message_ts if res.get("approval") else None) or thread_id
            await asyncio.to_thread(
                client.post_message,
                body=dismiss_msg,
                thread_id=dismiss_target_thread,
                room_id=room_id
            )
            logger.info("❌ [Worker] Dismissed approval %s by @%s", approval_id, sender_name)
            return

        # If success and action was approve, confirm, or save
        appr_obj = res.get("approval")
        is_save = action in ("save", "save_to_todoist")
        is_assignment = getattr(appr_obj, "action_type", "") in ("ASSIGNMENT", "HUMAN_TODO") or action in ("confirm", "accept")

        if (is_assignment or is_save) and appr_obj:
            task_id_line = f"• **Task ID:** `{res.get('task_id')}`\n" if res.get('task_id') else ""
            todoist_url = res.get('todoist_url')
            todoist_error = res.get('todoist_error')
            if todoist_url:
                sync_line = f"• **External Sync:** [View in Todoist]({todoist_url})\n"
            elif todoist_error:
                sync_line = f"• ⚠️ **External Sync Notice:** {todoist_error}\n"
            else:
                sync_line = "• **External Sync:** Native room only\n"

            if is_save:
                header = "💾 **Deliverable Saved to Workroom & Todoist**"
                footer = f"*(Saved by @{sender_name} to Todoist)*"
                role_line = f"• **Author / Owner:** @{appr_obj.assignee_name or sender_name}\n• **Saved by:** @{sender_name}\n"
            else:
                header = "🎯 **Task Confirmed & Assigned**"
                footer = f"*(Confirmed by @{sender_name}; action checklist forwarded to @{appr_obj.assignee_name})*"
                role_line = f"• **Assignee:** @{appr_obj.assignee_name}\n• **Assigned by:** @{sender_name}\n"

            confirm_receipt = (
                f"{header}\n\n"
                f"• **Task:** {appr_obj.action_summary}\n"
                f"{role_line}"
                f"• **Due Date:** {format_deadline_with_timezone(appr_obj.deadline) or 'Not specified'}\n"
                f"{task_id_line}"
                f"{sync_line}\n"
                f"{footer}"
            )
            confirm_target_thread = appr_obj.message_ts or thread_id
            await asyncio.to_thread(
                client.post_message,
                body=confirm_receipt,
                thread_id=confirm_target_thread,
                room_id=room_id
            )

        task_deduplicator.mark_completed(
            room_id=room_id or client.room_id or "",
            action_summary=res["approval"].action_summary if res.get("approval") else approval_id
        )
        logger.info("✅ [Worker] Successfully %sd and resolved '%s' in room [%s] by @%s", action, approval_id, room_id, sender_name)
        return

    # 2. Assign an existing (typically Unassigned) room task to a person.
    assign_cmd = parse_assign_command(body)
    if assign_cmd:
        task_id, target = assign_cmd
        task = default_room_task_store.get_task(task_id)
        if not task or task.room_id != room_id:
            await asyncio.to_thread(
                client.post_message,
                body=f"⚠️ **Task Not Found**\nNo task `{task_id}` exists in this room.",
                thread_id=thread_id,
                room_id=room_id,
            )
            return

        lookup = default_user_resolver.lookup_user(target)
        if lookup:
            new_assignee_id, new_assignee_name = lookup
        else:
            new_assignee_id, new_assignee_name = None, target

        task.assignee_name = new_assignee_name
        task.assignee_id = new_assignee_id
        task.updated_at = time.time()
        default_room_task_store._save()

        assign_msg = (
            f"✅ **Task Assigned**\n\n"
            f"• **Task ID:** `{task.task_id}`\n"
            f"• **Task:** {task.title}\n"
            f"• **Assignee:** {assignee_display(new_assignee_name)}\n"
            f"• **Assigned by:** @{sender_name}\n"
            f"• **Due Date:** {format_deadline_with_timezone(task.deadline) or 'Not specified'}"
        )
        await asyncio.to_thread(
            client.post_message,
            body=assign_msg,
            thread_id=thread_id,
            room_id=room_id,
        )
        logger.info("✅ [Worker] Assigned task %s to @%s in room [%s].", task_id, new_assignee_name, room_id)
        return

    # Check if this message is intended for Worker
    has_handoff_block = "<!-- TASKFLOW_HANDOFF:" in body
    handoff_ref_match = re.search(r"\[ref:(doc_[a-zA-Z0-9_]+)\]", body)
    has_compact_handoff = "[handoff]" in body.lower() or handoff_ref_match is not None
    is_worker_target = (
        has_handoff_block or
        has_compact_handoff or
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

    # Compact handoff: the structured payload lives in the TaskFlow document
    # store, so the room only sees a short "On it. [ref:...]" line.
    if handoff is None and handoff_ref_match:
        ref_id = handoff_ref_match.group(1)
        try:
            doc = client.document_store.get_document(ref_id, room_id=room_id)
            if doc is None and hasattr(client.document_store, "reload"):
                client.document_store.reload()
                doc = client.document_store.get_document(ref_id, room_id=room_id)
            if doc is not None and getattr(doc, "content", None):
                handoff = TaskHandoffMessage.model_validate_json(doc.content)
                client.document_store.delete_document(ref_id)
                logger.info("📥 [Worker] Loaded compact handoff payload %s.", ref_id)
            else:
                logger.warning("[Worker] Compact handoff reference %s was not found in the local store.", ref_id)
        except Exception as e:
            logger.warning("[Worker] Failed loading compact handoff %s: %s", ref_id, e)

    # Compact handoff without a visible ref: the triage agent queued a
    # taskflow_handoff document and sent only "On it.". Claim the oldest
    # pending handoff in this room under a per-room lock.
    if handoff is None and has_compact_handoff:
        lock = room_handoff_locks[room_id or "default_room"]
        async with lock:
            try:
                def pending_handoffs():
                    found = []
                    for d in client.document_store.list_documents(room_id=room_id, status="DRAFT"):
                        meta = getattr(d, "metadata", None)
                        if not isinstance(meta, dict):
                            continue
                        if meta.get("type") != "taskflow_handoff" or meta.get("status") != "PENDING":
                            continue
                        if time.time() - d.created_at > HANDOFF_TTL_SECONDS:
                            client.document_store.delete_document(d.doc_id)
                            continue
                        found.append(d)
                    found.sort(key=lambda d: d.created_at)
                    return found

                # Always re-read the backing file: triage and worker may be
                # separate processes, and a handoff written after this worker
                # started must still be visible.
                if hasattr(client.document_store, "reload"):
                    client.document_store.reload()
                pending = pending_handoffs()
                if pending:
                    claimed = pending[0]
                    handoff = TaskHandoffMessage.model_validate_json(claimed.content)
                    client.document_store.delete_document(claimed.doc_id)
                    logger.info("📥 [Worker] Claimed queued compact handoff %s.", claimed.doc_id)
            except Exception as e:
                logger.warning("[Worker] Failed claiming queued compact handoff: %s", e)

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

    # Guardrail: Per-Room Rate Limit check for worker execution (LLM Cost DoS Prevention)
    active_room = room_id or handoff.room_id or client.room_id or "default_room"
    allowed, retry_after, limit_reason = room_rate_limiter.check_and_acquire(active_room, cost=1)
    if not allowed:
        logger.warning(
            "🛑 [Worker] Deliverable execution throttled for room [%s] (Cost DoS Guardrail): %s",
            active_room[:8], limit_reason
        )
        throttle_msg = (
            f"⏳ @{handoff.requester_name or sender_name} **[TaskFlow Rate Limit: LLM Cost DoS Protection]**\n\n"
            f"Worker execution for **'{handoff.task_title}'** was throttled because this room has reached its LLM quota.\n"
            f"Please wait **{retry_after} seconds** before triggering additional worker executions."
        )
        target_users = [handoff.requester_name or sender_name]
        await asyncio.to_thread(
            client.send_targeted_message,
            body=throttle_msg,
            target_names=target_users,
            thread_id=thread_id,
            room_id=active_room
        )
        return

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
        proposal_lock_targets = f"@{handoff.assigner_name}"
        if not is_unassigned_assignee(handoff.assignee_name):
            proposal_lock_targets += f", @{handoff.assignee_name}"
        short_proposal_card = (
            f"⚡ **TaskFlow Deliverable Drafted (Pending Approval)**\n\n"
            f"• **Deliverable:** {worker_result.artifact_title or handoff.task_title}\n"
            f"• **Task:** {handoff.task_title}\n"
            f"• **Assignee:** {assignee_display(handoff.assignee_name)}\n"
            f"• **Assigned by:** @{handoff.assigner_name}\n"
            f"• **Approval ID:** `{approval.approval_id}`\n"
            f"{doc_info}"
            f"• **Status:** `PENDING_APPROVAL`\n\n"
            f"📝 **Preview:**\n> {preview_text}\n\n"
            f"👉 **To approve & publish to room:**\n`@taskflow-worker approve {approval.approval_id}`\n\n"
            f"👉 **To save to Todoist:**\n`@taskflow-worker save {approval.approval_id}`\n\n"
            f"👉 **To dismiss:**\n`@taskflow-worker dismiss {approval.approval_id}`\n\n"
            f"*(Approval locked to {proposal_lock_targets}, or room role holders)*"
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
        is_unassigned = is_unassigned_assignee(handoff.assignee_name)

        if is_medium and not is_unassigned:
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
                f"• **Due Date:** {format_deadline_with_timezone(handoff.deadline) or 'Not specified'}\n"
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

        if is_unassigned:
            logger.info(
                "📥 [Worker] Recording unassigned task '%s' directly; no confirmation gate and no person paged.",
                handoff.task_title,
            )

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
            thread_id=handoff.thread_id,
        )

        todo_payload = build_todoist_payload(
            title=handoff.task_title,
            assignee_name=handoff.assignee_name,
            assigner_name=handoff.assigner_name,
            context_notes=handoff.context_notes,
            content_body=worker_result.artifact_content,
            deadline=handoff.deadline,
            external_id=room_task.task_id,
            execution_type=handoff.execution_type.value if hasattr(handoff.execution_type, "value") else str(handoff.execution_type),
        )

        # 2. Optional Todoist Sync with 4xx diagnostics and idempotency
        sync_res = await asyncio.to_thread(
            todoist.sync_task,
            **todo_payload,
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

        unassigned_line = (
            f"• **Next step:** This task has no owner yet; the team can assign one later.\n"
            if is_unassigned else ""
        )
        receipt_text = (
            f"🎯 **Task Tracked in Switch Room**\n\n"
            f"• **Task ID:** `{room_task.task_id}`\n"
            f"• **Task:** {handoff.task_title}\n"
            f"• **Assignee:** {assignee_display(handoff.assignee_name)}\n"
            f"• **Assigned by:** @{handoff.assigner_name}\n"
            f"• **Due Date:** {format_deadline_with_timezone(handoff.deadline) or 'Not specified'}\n"
            f"• **Status:** `{room_task.status.value}` (Native Switch Workroom)\n"
            f"{unassigned_line}"
            f"{tracker_line}"
            f"📋 **Action Checklist:**\n{worker_result.artifact_content}\n\n"
            f"*(Scaffolded by @taskflow-worker via Switch targeted room message)*"
        )

        if is_unassigned:
            # Broadcast so the whole room sees the unclaimed task and can pick it up.
            target_users = []
            await asyncio.to_thread(
                client.post_message,
                body=receipt_text,
                thread_id=handoff.thread_id,
                room_id=handoff.room_id
            )
        else:
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
        logger.info("✅ [Worker] Recorded Switch room task %s & delivered receipt to %s.",
                    room_task.task_id, target_users or ["room"])


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

    # Register signal handlers for graceful WebSocket close and task cleanup
    shutdown_event = asyncio.Event()

    def _trigger_shutdown():
        logger.info("🛑 [Signal] Termination requested; initiating graceful multi-agent shutdown...")
        shutdown_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _trigger_shutdown)
        except (NotImplementedError, AttributeError):
            try:
                signal.signal(sig, lambda s, f: _trigger_shutdown())
            except Exception:
                pass

    if args.agent == "triage":
        logger.info("Starting TaskFlow Triage Agent...")
        triage = await run_triage_agent()
        active_room = SWITCH_ROOM_ID or triage.room_id
        try:
            triage.apply_room_briefing(room_id=active_room)
            logger.info("📋 [Switch Room] Applied ROOM_BRIEFING.md on connect via update_room_instructions")
        except Exception as e:
            logger.debug("Could not apply ROOM_BRIEFING.md on startup: %s", e)
        triage_task = asyncio.create_task(triage.connect_and_listen())
        shutdown_waiter = asyncio.create_task(shutdown_event.wait())
        done, pending = await asyncio.wait(
            [triage_task, shutdown_waiter],
            return_when=asyncio.FIRST_COMPLETED,
        )
        logger.info("🛑 [Graceful Shutdown] Closing triage WebSocket connection...")
        await triage.close(code=1000, reason="Process shutdown")
        for task in (triage_task, shutdown_waiter):
            if not task.done():
                task.cancel()
        logger.info("TaskFlow triage agent shut down gracefully.")
    elif args.agent == "worker":
        logger.info("Starting TaskFlow Worker Agent...")
        worker = await run_worker_agent()
        active_room = SWITCH_ROOM_ID or worker.room_id
        try:
            worker.apply_room_briefing(room_id=active_room)
            logger.info("📋 [Switch Room] Applied ROOM_BRIEFING.md on connect via update_room_instructions")
        except Exception as e:
            logger.debug("Could not apply ROOM_BRIEFING.md on startup: %s", e)
        worker_task = asyncio.create_task(worker.connect_and_listen())
        shutdown_waiter = asyncio.create_task(shutdown_event.wait())
        done, pending = await asyncio.wait(
            [worker_task, shutdown_waiter],
            return_when=asyncio.FIRST_COMPLETED,
        )
        logger.info("🛑 [Graceful Shutdown] Closing worker WebSocket connection...")
        await worker.close(code=1000, reason="Process shutdown")
        for task in (worker_task, shutdown_waiter):
            if not task.done():
                task.cancel()
        logger.info("TaskFlow worker agent shut down gracefully.")
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

        triage_task = asyncio.create_task(triage.connect_and_listen())
        worker_task = asyncio.create_task(worker.connect_and_listen())
        shutdown_waiter = asyncio.create_task(shutdown_event.wait())

        done, pending = await asyncio.wait(
            [triage_task, worker_task, shutdown_waiter],
            return_when=asyncio.FIRST_COMPLETED,
        )

        logger.info("🛑 [Graceful Shutdown] Closing multi-agent WebSocket connections...")
        await asyncio.gather(
            triage.close(code=1000, reason="Process shutdown"),
            worker.close(code=1000, reason="Process shutdown"),
            return_exceptions=True,
        )
        for task in (triage_task, worker_task, shutdown_waiter):
            if not task.done():
                task.cancel()
        logger.info("TaskFlow multi-agent daemon shut down gracefully.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("TaskFlow shut down gracefully.")
