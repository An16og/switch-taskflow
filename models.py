import os
import re
from datetime import datetime
from enum import Enum
from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field

from config import (
    SWITCH_TRIAGE_AGENT_NAME,
    SWITCH_TRIAGE_AGENT_ID,
    SWITCH_WORKER_AGENT_NAME,
    SWITCH_WORKER_AGENT_ID,
)

def format_deadline_with_timezone(deadline: Optional[str], tz: Optional[str] = None) -> Optional[str]:
    """Formats natural language deadline with explicit timezone indicator if missing.
    
    Examples:
        'Friday 5 PM' -> 'Friday 5 PM (IST)'
        'Friday 5 PM UTC' -> 'Friday 5 PM UTC'
        'Tomorrow' -> 'Tomorrow' (dates without specific times stay untouched)
    """
    if not deadline:
        return None
    cleaned = str(deadline).strip()
    if not cleaned or cleaned.lower() in ("none", "null", "n/a"):
        return None
    tz_val = tz or os.getenv("TIMEZONE") or datetime.now().astimezone().tzname() or "UTC"
    tz_pattern = re.compile(
        r'\b(UTC|GMT|IST|EST|EDT|CST|CDT|MST|MDT|PST|PDT|BST|CET|CEST|JST|AEST|AEDT)\b|[+-]\d{2}:?\d{2}',
        re.IGNORECASE
    )
    if tz_pattern.search(cleaned):
        return cleaned
    time_pattern = re.compile(r'\b(\d{1,2}(?::\d{2})?\s*(?:am|pm)?|\d{1,2}:\d{2})\b', re.IGNORECASE)
    if time_pattern.search(cleaned) and tz_val:
        return f"{cleaned} ({tz_val})"
    return cleaned

def extract_thread_id(payload: Dict[str, Any]) -> Optional[str]:
    """Resolves the source thread identifier across Switch and Slack schemas.
    
    Preserves source thread fidelity by inspecting thread/parent indicators
    (thread_id, thread_ts, parent_id, root_id) before falling back to the message's
    own identifier (id, message_id, ts) to anchor root conversations into a thread.
    """
    if not isinstance(payload, dict):
        return None
    for key in ("thread_id", "thread_ts", "parent_id", "root_id"):
        val = payload.get(key)
        if val is not None and str(val).strip():
            return str(val).strip()
    for key in ("id", "message_id", "ts"):
        val = payload.get(key)
        if val is not None and str(val).strip():
            return str(val).strip()
    return None

def should_skip_agent_message(
    payload: Dict[str, Any],
    client: Any = None,
    client_agent_id: Optional[str] = None,
    client_agent_name: Optional[str] = None,
    role: str = "triage",
) -> bool:
    """Determines whether an incoming room message should be skipped to prevent self-echo and loopback.
    
    Reliably inspects:
    1. Identifiers (IDs, not just names): Matches sender, sender_id, agent_id, bot_id, user_id,
       or author_id against known agent IDs (client_agent_id, SWITCH_TRIAGE_AGENT_ID, SWITCH_WORKER_AGENT_ID).
    2. Channel Type: Skips messages originating from internal/control/system/bot channel types
       (e.g., 'bot', 'system', 'control', 'heartbeat', 'status', 'internal').
    3. Bot/Agent Flags & Subtypes: Skips messages with subtype in ('bot_message', 'triage_message', etc.),
       is_bot=True, is_agent=True, or sender_kind in ('agent', 'bot', 'system').
    4. Names: Matches sender_name, user_name, author_name against known TaskFlow and sibling agent names.
    
    For worker: Permits task handoffs (containing task handoff envelope), but strictly skips
    its own worker messages.
    """
    if not isinstance(payload, dict):
        return True

    # 1. Channel Type inspection
    channel_type = str(payload.get("channel_type") or "").strip().lower()
    if channel_type in ("bot", "agent", "system", "notification", "control", "heartbeat", "status", "internal"):
        return True

    # 2. Subtype inspection
    subtype = str(payload.get("subtype") or "").strip().lower()
    if subtype in ("bot_message", "triage_message", "channel_join", "channel_leave", "message_deleted", "message_changed"):
        return True

    is_bot = bool(payload.get("is_bot"))
    is_agent = bool(payload.get("is_agent"))
    sender_kind = str(payload.get("sender_kind") or "").strip().lower()
    bot_id = payload.get("bot_id")

    # 3. Collect all incoming IDs across schemas
    incoming_ids = set()
    for key in ("sender", "sender_id", "agent_id", "bot_id", "user_id", "author_id"):
        val = payload.get(key)
        if val is not None and str(val).strip():
            incoming_ids.add(str(val).strip().lower())

    user_field = payload.get("user")
    if isinstance(user_field, dict):
        uid = user_field.get("id")
        if uid is not None and str(uid).strip():
            incoming_ids.add(str(uid).strip().lower())
    elif isinstance(user_field, str) and user_field.strip():
        incoming_ids.add(user_field.strip().lower())

    # 4. Known Agent IDs
    known_triage_ids = set()
    if SWITCH_TRIAGE_AGENT_ID:
        known_triage_ids.add(SWITCH_TRIAGE_AGENT_ID.lower())

    known_worker_ids = set()
    if SWITCH_WORKER_AGENT_ID:
        known_worker_ids.add(SWITCH_WORKER_AGENT_ID.lower())

    cid = getattr(client, "agent_id", None) or client_agent_id
    if cid:
        cid_str = str(cid).strip().lower()
        if role == "triage":
            known_triage_ids.add(cid_str)
        elif role == "worker":
            known_worker_ids.add(cid_str)
        else:
            known_triage_ids.add(cid_str)
            known_worker_ids.add(cid_str)

    all_agent_ids = known_triage_ids | known_worker_ids

    # 5. Collect incoming names
    incoming_names = set()
    for key in ("sender_name", "user_name", "author_name"):
        val = payload.get(key)
        if val is not None and str(val).strip():
            incoming_names.add(str(val).strip().lower())
    if isinstance(user_field, dict):
        uname = user_field.get("name")
        if uname is not None and str(uname).strip():
            incoming_names.add(str(uname).strip().lower())

    # 6. Known Agent Names
    system_agent_names = {"taskflow-triage", "taskflow-worker", "taskflow", "switch-server", "slackbot"}
    known_triage_names = {"taskflow-triage", "taskflow", "triage"}
    if SWITCH_TRIAGE_AGENT_NAME:
        known_triage_names.add(SWITCH_TRIAGE_AGENT_NAME.lower())

    known_worker_names = {"taskflow-worker", "worker"}
    if SWITCH_WORKER_AGENT_NAME:
        known_worker_names.add(SWITCH_WORKER_AGENT_NAME.lower())

    cname = getattr(client, "agent_name", None) or client_agent_name
    if cname:
        cname_str = str(cname).strip().lower()
        if role == "triage":
            known_triage_names.add(cname_str)
        elif role == "worker":
            known_worker_names.add(cname_str)
        else:
            known_triage_names.add(cname_str)
            known_worker_names.add(cname_str)

    all_agent_names = system_agent_names | known_triage_names | known_worker_names

    # Prefix checks for auto-generated agent IDs (e.g. '@switch-agent-...')
    has_switch_agent_id_prefix = any(
        s_id.startswith(("@switch-agent-", "switch-agent-", "sw_agent_")) for s_id in incoming_ids
    )

    body = str(payload.get("body") or "")
    is_handoff = (
        "<!-- TASKFLOW_HANDOFF:" in body
        or "[TASK_HANDOFF]" in body
        or "[TEST_HANDOFF]" in body
    )

    if role == "worker":
        # Worker must NEVER process its own worker messages (loop prevention)
        if incoming_ids & known_worker_ids:
            return True
        if incoming_names & known_worker_names:
            return True
        # If it's a valid handoff from triage, worker permits it
        if is_handoff:
            return False
        # If it's not a handoff, worker skips triage/system/bot messages
        if incoming_ids & known_triage_ids or has_switch_agent_id_prefix:
            return True
        if incoming_names & all_agent_names:
            return True
        if is_bot or is_agent or sender_kind in ("agent", "bot", "system") or bot_id is not None:
            return True
        return False

    # Default: Triage role skips ANY agent or bot message
    if incoming_ids & all_agent_ids or has_switch_agent_id_prefix:
        return True
    if incoming_names & all_agent_names:
        return True
    if is_bot or is_agent or sender_kind in ("agent", "bot", "system") or bot_id is not None:
        return True

    return False

class ConfidenceLevel(str, Enum):
    HIGH = "HIGH"       # Direct, clear task assignment to a specific person
    MEDIUM = "MEDIUM"   # Ambiguous, implicit request, or informal check
    LOW = "LOW"         # General chatter, status check, rant, or non-task

class ExecutionType(str, Enum):
    AUTO_EXECUTE = "AUTO_EXECUTE"  # Knowledge work AI can do (docs, summaries, release notes, issue templates)
    HUMAN_TODO = "HUMAN_TODO"      # Complex dev, code reviews, client calls, architecture design

class TaskTriageResult(BaseModel):
    is_task: bool = Field(
        description="True if the message thread contains an actionable task or delegation."
    )
    confidence: ConfidenceLevel = Field(
        description="Confidence level of detection: HIGH, MEDIUM, or LOW."
    )
    confidence_reason: str = Field(
        description="One brief sentence explaining why this was or wasn't classified as a task."
    )
    action_summary: str = Field(
        default="",
        description="Crisp, actionable imperative task title (e.g., 'Update payment webhook documentation')."
    )
    assignee_id: Optional[str] = Field(
        default=None,
        description="Slack User ID (e.g. 'U12345678') if tagged or known."
    )
    assignee_name: Optional[str] = Field(
        default=None,
        description="Name or handle of the assignee if mentioned in text."
    )
    deadline: Optional[str] = Field(
        default=None,
        description="Natural language deadline (e.g. 'Friday 5:00 PM', 'Tomorrow EOD') or null."
    )
    context_notes: Optional[str] = Field(
        default=None,
        description="Brief 1-sentence background summary from conversation history."
    )
    execution_type: ExecutionType = Field(
        default=ExecutionType.HUMAN_TODO,
        description="AUTO_EXECUTE if routine knowledge work (docs/summaries/templates), HUMAN_TODO if requires human."
    )
    execution_category: str = Field(
        default="General Task",
        description="Category: 'Documentation', 'Release Notes', 'Summary', 'Code Review', 'Meeting', etc."
    )
    needs_more_context: bool = Field(
        default=False,
        description="True if critical context is missing and the sliding window should expand."
    )
    error: Optional[str] = Field(
        default=None,
        description="Captures runtime LLM or provider errors so they are never silently classified as 'not a task'."
    )

class TaskHandoffMessage(BaseModel):
    """Structured handoff payload sent from TaskFlow Triage to TaskFlow Worker via Switch targeted room message."""
    task_title: str
    execution_type: ExecutionType
    confidence: ConfidenceLevel
    assignee_name: Optional[str] = None
    assignee_id: Optional[str] = None
    assigner_name: Optional[str] = None
    assigner_id: Optional[str] = None
    deadline: Optional[str] = None
    context_notes: Optional[str] = None
    execution_category: Optional[str] = "General Task"
    thread_id: Optional[str] = None
    room_id: Optional[str] = None
    requester_name: str
    recent_context: Optional[List[Dict[str, Any]]] = None

class WorkerExecutionResult(BaseModel):
    artifact_title: str = Field(
        description="Title of the completed artifact (e.g. 'Release Notes Draft v1.2' or 'Implementation Checklist')."
    )
    artifact_content: str = Field(
        description="The actual generated artifact content in clean Markdown format."
    )
    suggested_action: str = Field(
        default="Review and approve",
        description="Suggested next step for the user."
    )

class SlackMessage(BaseModel):
    user_id: str
    user_name: str
    text: str
    ts: str
    permalink: Optional[str] = None
