import sys
import os
import json
import time
import asyncio
import logging
from collections import defaultdict, deque
from typing import Dict, Deque, Any, Optional

# Ensure UTF-8 output on Windows console
if sys.stdout.encoding != 'utf-8':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

from config import (
    SWITCH_SERVER_URL,
    AGENT_REGISTRATION_TOKEN,
    SWITCH_AGENT_ID,
    SWITCH_AGENT_API_KEY,
    SWITCH_ROOM_ID,
    AMBIENT_TRIAGE_POLICY,
    check_env
)
from models import SlackMessage, ConfidenceLevel, ExecutionType, TaskTriageResult
from llm_gateway import LLMGateway
from agents.triage_agent import TriageAgent
from agents.worker_agent import WorkerAgent
from todoist_client import TodoistClient
from switch_client import SwitchAgentClient

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("TaskFlowBot")

# Validate environment variables before running
missing = check_env()
if missing:
    print("\n" + "=" * 60)
    print("❌ MISSING ENVIRONMENT VARIABLES IN .env:")
    for m in missing:
        print(f"   - {m}")
    print("=" * 60)
    print("Please edit the .env file in 'switch-taskflow' and paste your keys.")
    print("=" * 60 + "\n")
    sys.exit(1)

# Initialize Gateway & Two-Agent System
gateway = LLMGateway()
triage_agent = TriageAgent(gateway=gateway)
worker_agent = WorkerAgent(gateway=gateway)
todoist = TodoistClient()

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
    # Default: "action_keywords"
    body_lower = body.lower()
    return any(kw in body_lower for kw in ACTION_KEYWORDS)


async def handle_room_message(client: SwitchAgentClient, payload: Dict[str, Any]):
    """Event handler for incoming Switch room message frames."""
    body = payload.get("body", "").strip()
    if not body:
        return

    sender = payload.get("sender", "")
    sender_name = payload.get("sender_name", "") or sender or "Team Member"
    addressed = payload.get("addressed", False)
    room_id = payload.get("room_id", client.room_id)
    message_id = payload.get("id") or payload.get("event_id")
    thread_id = payload.get("thread_id") or message_id

    # Filter out TaskFlow's own posted messages
    if sender_name.lower() in ("taskflow", "taskflow-bot") or sender == client.agent_id:
        logger.debug("Ignoring message from TaskFlow itself.")
        return

    logger.info("📩 Message in room [%s] from %s (addressed=%s): %s", room_id, sender_name, addressed, body[:60])

    # Record message in the sliding context buffer
    msg_obj = SlackMessage(
        user_id=sender or "U_UNKNOWN",
        user_name=sender_name,
        text=body,
        ts=str(time.time()),
        permalink=None
    )
    room_buffers[room_id].append(msg_obj)

    # Determine if we should triage this message
    is_addressed = bool(addressed)
    is_ambient = not is_addressed and should_ambient_triage(body)

    if not is_addressed and not is_ambient:
        logger.debug("Ambient message did not meet triage policy; preserved in buffer.")
        return

    mode = "DIRECT MENTION" if is_addressed else "AMBIENT TRIAGE"
    logger.info("⚡ Triaging message via [%s]...", mode)

    # Step 1: Run Agent 1 (Triage & Autonomy Classifier) in non-blocking thread
    context_msgs = list(room_buffers[room_id])
    try:
        triage_result = await asyncio.to_thread(triage_agent.triage_conversation, context_msgs)
    except Exception as e:
        logger.error("TriageAgent failed: %s", e)
        return

    logger.info("Triage Decision: is_task=%s, type=%s, conf=%s",
                triage_result.is_task, triage_result.execution_type.value, triage_result.confidence.value)

    # If not an actionable task
    if not triage_result.is_task:
        if is_addressed:
            reply = (
                f"Hello @{sender_name}! I heard your request, but did not detect an actionable task or document generation request.\n"
                f"I can automatically draft **Release Notes**, **Docs**, **Bug Analyses**, or scaffold **Todoist Tasks**."
            )
            await asyncio.to_thread(client.send_targeted_message, body=reply, target_names=[sender_name], thread_id=thread_id, room_id=room_id)
        return

    # Step 2: Route according to Autonomous Execution vs Human Todo
    if triage_result.execution_type == ExecutionType.AUTO_EXECUTE:
        # Agent 2: Worker Agent - Auto Execute Deliverable
        logger.info("🤖 Auto-executing deliverable...")
        try:
            worker_res = await asyncio.to_thread(worker_agent.execute_task, triage_result, context_msgs)
        except Exception as e:
            logger.error("WorkerAgent autonomous execution failed: %s", e)
            return

        response_text = (
            f"### ⚡ Autonomous Deliverable Generated\n\n"
            f"**Title**: {worker_res.artifact_title}\n"
            f"**Intent**: {triage_result.action_summary}\n"
            f"**Next Action**: {worker_res.suggested_action}\n\n"
            f"---\n\n"
            f"{worker_res.artifact_content}\n\n"
            f"---\n"
            f"*(Generated autonomously by TaskFlow Worker Agent on Switch Core)*"
        )

        await asyncio.to_thread(client.post_message, body=response_text, thread_id=thread_id, room_id=room_id)
        logger.info("✅ Posted autonomous deliverable to room [%s].", room_id)

    elif triage_result.execution_type == ExecutionType.HUMAN_TODO:
        # Agent 2: Worker Agent - Scaffold Action Checklist
        logger.info("📋 Scaffolding action plan for human task...")
        try:
            worker_res = await asyncio.to_thread(worker_agent.execute_task, triage_result, context_msgs)
        except Exception as e:
            logger.error("WorkerAgent scaffolding failed: %s", e)
            return

        # Prepare description for Todoist
        todo_title = triage_result.action_summary or body[:60]
        todoist_desc = (
            f"Action Plan / Checklist:\n\n"
            f"{worker_res.artifact_content}\n\n"
            f"Triage Context: {triage_result.context_notes or 'Captured from Switch Room'}\n"
            f"Suggested Action: {worker_res.suggested_action}"
        )

        # Sync to Todoist
        todo_task = await asyncio.to_thread(
            todoist.create_task,
            content=f"[TaskFlow] {todo_title}",
            description=todoist_desc,
            due_string=triage_result.deadline
        )

        todo_url = todo_task.get("url", "https://todoist.com/app/today") if todo_task else "https://todoist.com"

        receipt_text = (
            f"### 📋 Task Synced to Todoist\n\n"
            f"**Task**: {todo_title}\n"
            f"**Assignee**: {triage_result.assignee_name or sender_name}\n"
            f"**Due**: {triage_result.deadline or 'Today'}\n\n"
            f"**Action Plan**:\n{worker_res.artifact_content}\n\n"
            f"🔗 **[Open in Todoist]({todo_url})**\n\n"
            f"*(Triaged & synced by TaskFlow on Switch Core)*"
        )

        await asyncio.to_thread(
            client.send_targeted_message,
            body=receipt_text,
            target_names=[sender_name],
            thread_id=thread_id,
            room_id=room_id
        )
        logger.info("✅ Created Todoist task & sent targeted receipt to @%s in room [%s].", sender_name, room_id)


async def main():
    print("""
    ===============================================================
       SWITCH TASKFLOW : Autonomous Workroom Agent (Native WS)    
       - Zero Socket Mode / Pure Native Switch Agent Protocol     
       - WebSocket /agents/{id}/connection/ws & Heartbeat Engine   
       - Triage Agent (AUTO_EXECUTE vs HUMAN_TODO)                
       - Worker Agent (Artifacts + Todoist Sync)                  
    ===============================================================
    """)

    client = SwitchAgentClient(
        server_url=SWITCH_SERVER_URL,
        registration_token=AGENT_REGISTRATION_TOKEN,
        agent_id=SWITCH_AGENT_ID,
        api_key=SWITCH_AGENT_API_KEY,
        room_id=SWITCH_ROOM_ID,
        agent_name="taskflow",
        display_name="TaskFlow",
        description="Autonomous Task Triage & Workroom Worker"
    )

    # Register message handler callback
    client.on_message(lambda payload: handle_room_message(client, payload))

    print(f"[*] Target Switch Server: {SWITCH_SERVER_URL}")
    print(f"[*] Ambient Triage Policy: {AMBIENT_TRIAGE_POLICY.upper()}")

    try:
        await client.connect_and_listen()
    except KeyboardInterrupt:
        print("\n[!] Shutting down TaskFlow cleanly...")
        client.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nShutdown complete.")
