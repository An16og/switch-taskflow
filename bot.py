import sys
import json
import logging
from collections import defaultdict, deque
from typing import Dict, Deque

# Ensure UTF-8 output on Windows console
if sys.stdout.encoding != 'utf-8':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from config import SLACK_BOT_TOKEN, SLACK_APP_TOKEN, check_env
from models import SlackMessage, ConfidenceLevel, ExecutionType
from llm_gateway import LLMGateway
from agents.triage_agent import TriageAgent
from agents.worker_agent import WorkerAgent
from todoist_client import TodoistClient
from slack_cards import (
    build_auto_execute_card,
    build_human_todo_card,
    build_assigner_card,
    build_receipt_blocks
)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("TaskFlowBot")

# Validate environment variables before initializing Slack App
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

# Initialize Slack App
app = App(token=SLACK_BOT_TOKEN)

# Initialize Gateway & Two-Agent System
gateway = LLMGateway()
triage_agent = TriageAgent(gateway=gateway)
worker_agent = WorkerAgent(gateway=gateway)
todoist = TodoistClient()

# Sliding window buffer per channel: keeps the last 15 messages
channel_buffers: Dict[str, Deque[SlackMessage]] = defaultdict(lambda: deque(maxlen=15))

# Cache for Slack user info (ID -> display_name)
user_cache: Dict[str, str] = {}


def get_username(client, user_id: str) -> str:
    """Fetches user's display or real name with local in-memory caching."""
    if not user_id:
        return "Unknown"
    if user_id in user_cache:
        return user_cache[user_id]
    try:
        res = client.users_info(user=user_id)
        if res.get("ok"):
            profile = res["user"].get("profile", {})
            name = profile.get("display_name") or profile.get("real_name") or res["user"].get("name", user_id)
            user_cache[user_id] = name
            return name
    except Exception as e:
        logger.warning(f"Could not fetch user name for {user_id}: {e}")
    return user_id


@app.event("message")
def handle_channel_message(event, client, logger):
    """
    Ambient listener in Switch Room / Slack:
    1. Agent 1 (Triage) extracts task and determines AUTO_EXECUTE vs HUMAN_TODO.
    2. Agent 2 (Worker) autonomously executes routine tasks or generates action plan.
    """
    if event.get("bot_id") or event.get("subtype"):
        return

    channel_id = event.get("channel")
    user_id = event.get("user")
    text = event.get("text", "").strip()
    ts = event.get("ts")

    if not text or not user_id:
        return

    user_name = get_username(client, user_id)

    # Persona Simulation Support: Allow testing fake teammates via "[Name]: <message>"
    import re
    persona_match = re.match(r"^\[(.*?)\]:\s*(.*)", text)
    if persona_match:
        user_name = persona_match.group(1).strip()
        text = persona_match.group(2).strip()
        logger.info(f"🎭 Simulated Teammate Persona: '{user_name}'")

    # 1. Add message to channel's sliding window
    msg_obj = SlackMessage(
        user_id=user_id,
        user_name=user_name,
        text=text,
        ts=ts
    )
    buffer = channel_buffers[channel_id]
    buffer.append(msg_obj)

    logger.info(f"[{channel_id}] {user_name}: {text}")

    # 2. AGENT 1: Triage recent messages
    recent_msgs = list(buffer)[-5:]
    triage = triage_agent.triage_conversation(recent_msgs)

    # Dynamic window expansion
    if triage.needs_more_context and len(buffer) > 5:
        logger.info(f"Expanding sliding window to {len(buffer)} messages.")
        triage = triage_agent.triage_conversation(list(buffer))

    logger.info(
        f"Triage -> is_task: {triage.is_task}, "
        f"confidence: {triage.confidence.value}, "
        f"type: {triage.execution_type.value}, "
        f"action: '{triage.action_summary}'"
    )

    if not triage.is_task or triage.confidence == ConfidenceLevel.LOW:
        return

    # Permalink
    permalink = None
    try:
        link_res = client.chat_getPermalink(channel=channel_id, message_ts=ts)
        if link_res.get("ok"):
            permalink = link_res.get("permalink")
    except Exception as e:
        logger.warning(f"Failed to fetch permalink: {e}")

    # Resolve target assignee ID
    target_id = triage.assignee_id
    if not target_id and triage.assignee_name:
        if "anuj" in triage.assignee_name.lower():
            target_id = user_id
    if not target_id:
        target_id = user_id

    # 3. ROUTE BY EXECUTION TYPE
    if triage.execution_type == ExecutionType.AUTO_EXECUTE:
        # PATH A: WORKER AGENT AUTO-EXECUTES ROUTINE KNOWLEDGE TASK
        logger.info(f"⚡ Launching Worker Agent for AUTO_EXECUTE: {triage.action_summary}")
        worker_res = worker_agent.execute_task(triage, list(buffer))

        blocks = build_auto_execute_card(
            action_summary=triage.action_summary,
            artifact_title=worker_res.artifact_title,
            artifact_content=worker_res.artifact_content,
            assigner_id=user_id,
            category=triage.execution_category,
            permalink=permalink,
            channel_id=channel_id,
            message_ts=ts
        )

        # Send card to the requester / assignee
        try:
            client.chat_postMessage(
                channel=target_id,
                text=f"⚡ TaskFlow Worker auto-executed: {triage.action_summary}",
                blocks=blocks
            )
        except Exception as e:
            logger.error(f"Failed to dispatch auto-execute card: {e}")

    else:
        # PATH B: HUMAN_TODO -> WORKER GENERATES ACTION CHECKLIST & SENDS TO TODOIST FLOW
        logger.info(f"📌 Processing HUMAN_TODO: {triage.action_summary}")
        worker_res = worker_agent.execute_task(triage, list(buffer))

        if triage.confidence == ConfidenceLevel.HIGH:
            blocks = build_human_todo_card(
                action_summary=triage.action_summary,
                assigner_id=user_id,
                action_plan=worker_res.artifact_content,
                deadline=triage.deadline,
                context_notes=triage.context_notes,
                permalink=permalink,
                channel_id=channel_id,
                message_ts=ts
            )
            try:
                client.chat_postMessage(
                    channel=target_id,
                    text=f"📌 TaskFlow detected a new task: {triage.action_summary}",
                    blocks=blocks
                )
            except Exception as e:
                logger.error(f"Failed to dispatch human task card: {e}")

        elif triage.confidence == ConfidenceLevel.MEDIUM:
            target_name = triage.assignee_name or f"<@{target_id}>"
            blocks = build_assigner_card(
                action_summary=triage.action_summary,
                target_assignee=target_name,
                target_assignee_id=target_id,
                deadline=triage.deadline,
                context_notes=triage.context_notes,
                permalink=permalink,
                channel_id=channel_id,
                message_ts=ts
            )
            try:
                client.chat_postMessage(
                    channel=user_id,
                    text=f"🤔 TaskFlow: Did you mean to assign this task?",
                    blocks=blocks
                )
            except Exception as e:
                logger.error(f"Failed to dispatch medium confidence prompt: {e}")


# =========================================================================
# INTERACTIVE BUTTON HANDLERS
# =========================================================================

@app.action("btn_approve_auto_execute")
def handle_approve_auto_execute(ack, body, client, respond, logger):
    """
    User clicked 'Approve & Post to Channel'.
    Worker Agent posts the generated artifact directly into the channel thread.
    """
    ack()
    actions = body.get("actions", [])
    if not actions:
        return

    payload = json.loads(actions[0].get("value", "{}"))
    action_summary = payload.get("action_summary")
    artifact_title = payload.get("artifact_title")
    artifact_content = payload.get("artifact_content")
    channel_id = payload.get("channel_id")
    message_ts = payload.get("message_ts")

    logger.info(f"User approved auto-executed artifact: {artifact_title}")

    # Post completed work directly to the original Slack channel / thread
    if channel_id:
        try:
            client.chat_postMessage(
                channel=channel_id,
                thread_ts=message_ts,
                text=f"⚡ *TaskFlow Worker Completed:* *{artifact_title}*\n\n{artifact_content}"
            )
        except Exception as e:
            logger.warning(f"Could not post to channel: {e}")

    respond(
        replace_original=True,
        text=f"🚀 *Approved & Published!* The generated *{artifact_title}* has been posted to the channel."
    )


@app.action("btn_accept_task")
def handle_accept_task(ack, body, client, respond, logger):
    """
    User clicked 'Accept & Add to Todoist'.
    Syncs task + AI action plan to Todoist and notifies assigner.
    """
    ack()
    user_id = body["user"]["id"]
    actions = body.get("actions", [])
    if not actions:
        return

    payload = json.loads(actions[0].get("value", "{}"))
    action_summary = payload.get("action_summary", "Untitled Task")
    assigner_id = payload.get("assigner_id")
    deadline = payload.get("deadline")
    permalink = payload.get("permalink")
    action_plan = payload.get("action_plan") or payload.get("artifact_content")
    channel_id = payload.get("channel_id")
    message_ts = payload.get("message_ts")

    logger.info(f"User <@{user_id}> accepted task '{action_summary}'")

    # Format Todoist task description with AI Action Plan
    description_lines = []
    if assigner_id:
        assigner_name = get_username(client, assigner_id)
        description_lines.append(f"Assigned by: {assigner_name}")
    if permalink:
        description_lines.append(f"Slack Context: {permalink}")
    if action_plan:
        description_lines.append(f"\n--- AI Action Plan ---\n{action_plan}")

    todoist_task = todoist.create_task(
        content=action_summary,
        description="\n".join(description_lines),
        due_string=deadline
    )

    todoist_url = todoist_task.get("url") if todoist_task else None

    # Replace original card with receipt
    receipt_blocks = build_receipt_blocks(
        action_summary=action_summary,
        deadline=deadline,
        permalink=permalink,
        todoist_url=todoist_url
    )
    respond(replace_original=True, blocks=receipt_blocks, text=f"✅ Task added to Todoist: {action_summary}")

    # Mutual notification
    if assigner_id and assigner_id != user_id:
        try:
            client.chat_postMessage(
                channel=assigner_id,
                text=f"🎉 *Task Confirmed:* <@{user_id}> accepted *\"{action_summary}\"*, now synced to Todoist with an AI Action Plan!"
            )
        except Exception as e:
            logger.warning(f"Could not send confirmation to assigner: {e}")

    # Reaction
    if channel_id and message_ts:
        try:
            client.reactions_add(channel=channel_id, timestamp=message_ts, name="white_check_mark")
        except Exception:
            pass


@app.action("btn_confirm_assignment")
def handle_confirm_assignment(ack, body, client, respond, logger):
    ack()
    assigner_id = body["user"]["id"]
    actions = body.get("actions", [])
    if not actions:
        return

    payload = json.loads(actions[0].get("value", "{}"))
    action_summary = payload.get("action_summary")
    target_id = payload.get("target_assignee_id")
    deadline = payload.get("deadline")
    permalink = payload.get("permalink")
    channel_id = payload.get("channel_id")
    message_ts = payload.get("message_ts")

    respond(replace_original=True, text=f"👍 Dispatched confirmation request for *\"{action_summary}\"*.")

    if target_id:
        blocks = build_human_todo_card(
            action_summary=action_summary,
            assigner_id=assigner_id,
            action_plan="* [ ] Review requirements and complete task",
            deadline=deadline,
            permalink=permalink,
            channel_id=channel_id,
            message_ts=message_ts
        )
        try:
            client.chat_postMessage(
                channel=target_id,
                text=f"📌 TaskFlow detected a new task: {action_summary}",
                blocks=blocks
            )
        except Exception as e:
            logger.error(f"Failed to forward task to assignee {target_id}: {e}")


@app.action("btn_dismiss_task")
def handle_dismiss_task(ack, body, respond):
    ack()
    respond(replace_original=True, text="❌ Task dismissed.")


def main():
    print("\n" + "=" * 60)
    print("🚀 STARTING TASKFLOW MULTI-AGENT WORKROOM (Socket Mode)...")
    print("   • Agent 1: Triage & Intent Classifier (Ambient Listener)")
    print("   • Agent 2: Autonomous Worker (Auto-Executes & Plans)")
    print("   • Multi-Model Gateway: Gemini -> OpenAI -> Claude")
    print("=" * 60 + "\n")

    handler = SocketModeHandler(app, SLACK_APP_TOKEN)
    handler.start()


if __name__ == "__main__":
    main()
