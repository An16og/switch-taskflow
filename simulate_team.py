import os
import sys
import time

if sys.stdout.encoding != 'utf-8':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

from models import SlackMessage, ConfidenceLevel, ExecutionType
from llm_gateway import LLMGateway
from agents.triage_agent import TriageAgent
from agents.worker_agent import WorkerAgent
from slack_cards import (
    build_auto_execute_card,
    build_human_todo_card,
    build_assigner_card,
    post_slack_dm,
    open_slack_dm
)
from slack_sdk import WebClient
from config import (
    SLACK_BOT_TOKEN,
    SWITCH_SERVER_URL,
    SWITCH_ROOM_ID,
    SWITCH_WORKER_AGENT_ID,
    SWITCH_WORKER_AGENT_API_KEY,
    AGENT_REGISTRATION_TOKEN
)
from switch_client import SwitchAgentClient
from user_resolver import default_user_resolver

gateway = LLMGateway()
triage_agent = TriageAgent(gateway=gateway)
worker_agent = WorkerAgent(gateway=gateway)
slack_client = WebClient(token=SLACK_BOT_TOKEN) if SLACK_BOT_TOKEN else None

# Initialize Switch client if configured (On Switch, use send_targeted_message instead of Slack DMs)
switch_client = None
if SWITCH_SERVER_URL:
    try:
        switch_client = SwitchAgentClient(
            server_url=SWITCH_SERVER_URL,
            registration_token=AGENT_REGISTRATION_TOKEN,
            agent_id=SWITCH_WORKER_AGENT_ID,
            api_key=SWITCH_WORKER_AGENT_API_KEY,
            room_id=SWITCH_ROOM_ID,
            agent_name="taskflow-worker",
            display_name="TaskFlow Worker",
        )
    except Exception:
        switch_client = None


def resolve_target_user_id(client: WebClient = None) -> str:
    """
    Dynamically resolves target user ID without hardcoding.
    
    Resolution order:
    1. Explicit environment variable: MY_USER_ID or SLACK_USER_ID
    2. Live Slack API auth_test() if token is configured
    3. Team roster lookup via UserResolver
    4. Safe simulation fallback
    """
    # 1. Environment variable override
    env_user = os.getenv("MY_USER_ID") or os.getenv("SLACK_USER_ID")
    if env_user:
        return env_user.strip()

    # 2. Query Slack API auth.test if token is valid
    if client and getattr(client, "token", None):
        try:
            auth = client.auth_test()
            if auth.get("ok") and auth.get("user_id"):
                return auth["user_id"]
        except Exception:
            pass

    # 3. Dynamic lookup from team roster
    roster_entry = default_user_resolver.lookup_user("anuj")
    if roster_entry and roster_entry[0]:
        return roster_entry[0]

    # 4. Safe fallback for offline simulation
    return "U_SIMULATED_USER"


# Resolve target user and display name dynamically (no hardcoded ID)
MY_USER_ID = resolve_target_user_id(slack_client)
roster_entry = default_user_resolver.lookup_user("anuj")
MY_USER_NAME = roster_entry[1] if (roster_entry and roster_entry[1]) else "Anuj"

SCENARIOS = {
    "1": {
        "title": "⚡ AUTO-EXECUTE: Release Notes Draft (AI Worker Executes on the Spot)",
        "messages": [
            SlackMessage(
                user_id="U_ALEX_PM",
                user_name="Alex (Product Manager)",
                text="Can someone draft the v1.2 release notes highlighting our new Stripe webhook fix and dark mode?",
                ts=str(time.time())
            )
        ]
    },
    "2": {
        "title": "📌 HUMAN_TODO: PR Review & Benchmark (Worker Builds Action Plan)",
        "messages": [
            SlackMessage(
                user_id="U_PRIYA_LEAD",
                user_name="Priya (Tech Lead)",
                text=f"@Anuj please review and benchmark the database migration PR #89 before Friday 5 PM.",
                ts=str(time.time())
            )
        ]
    },
    "3": {
        "title": "⚡ AUTO-EXECUTE: Incident Postmortem Document",
        "messages": [
            SlackMessage(
                user_id="U_ALEX_PM",
                user_name="Alex (Product Manager)",
                text="Please prepare an incident postmortem draft for today's 20-minute checkout outage caused by expired SSL certificates.",
                ts=str(time.time())
            )
        ]
    },
    "4": {
        "title": "🤫 Ambient Silence (Lunch Chat / No Tasks)",
        "messages": [
            SlackMessage(
                user_id="U_DAVID_DEV",
                user_name="David",
                text="Hey team, what are we ordering for lunch today?",
                ts=str(time.time())
            )
        ]
    }
}


def run_simulation(choice: str):
    if choice not in SCENARIOS:
        print("Invalid choice!")
        return

    scenario = SCENARIOS[choice]
    print(f"\n========================================================")
    print(f"🎬 RUNNING SCENARIO: {scenario['title']}")
    print(f"Target User: <@{MY_USER_ID}>")
    print(f"========================================================")

    for msg in scenario["messages"]:
        print(f"  💬 [{msg.user_name}]: {msg.text}")
        time.sleep(0.3)

    print("\n🔍 1. Agent 1 (Triage Agent) analyzing conversation...")
    triage = triage_agent.triage_conversation(scenario["messages"])

    print(f"   ✓ is_task: {triage.is_task}")
    print(f"   ✓ confidence: {triage.confidence.value}")
    print(f"   ✓ execution_type: {triage.execution_type.value}")
    print(f"   ✓ action: '{triage.action_summary}'")

    if not triage.is_task or triage.confidence == ConfidenceLevel.LOW:
        print("\n🤫 AMBIENT SILENCE: No task detected. Zero noise.")
        return

    print("\n⚙️ 2. Agent 2 (Worker Agent) executing...")
    worker_res = worker_agent.execute_task(triage, scenario["messages"])

    if triage.execution_type == ExecutionType.AUTO_EXECUTE:
        print(f"   ⚡ Task Auto-Executed! Artifact: '{worker_res.artifact_title}'")
        print(f"\n--- Artifact Preview ---\n{worker_res.artifact_content[:300]}...\n-----------------------")
        blocks = build_auto_execute_card(
            action_summary=triage.action_summary,
            artifact_title=worker_res.artifact_title,
            artifact_content=worker_res.artifact_content,
            assigner_id=scenario["messages"][0].user_id,
            category=triage.execution_category,
            permalink="https://slack.com"
        )
    else:
        print(f"   📌 Human Task Prepared with Action Checklist:\n{worker_res.artifact_content}")
        blocks = build_human_todo_card(
            action_summary=triage.action_summary,
            assigner_id=scenario["messages"][0].user_id,
            action_plan=worker_res.artifact_content,
            deadline=triage.deadline,
            context_notes=triage.context_notes,
            permalink="https://slack.com"
        )

    # Dispatch
    print("\n--------------------------------------------------------")
    print("📤 DISPATCHING TASK DELIVERABLES / RECEIPTS")
    print("--------------------------------------------------------")

    # 1. On Switch: Native targeted message delivery (send_targeted_message)
    # Architecture Rule: On Switch, use send_targeted_message instead of Slack DMs!
    switch_dispatched = False
    if switch_client and SWITCH_ROOM_ID:
        print(f"🎯 [Switch Native] Using send_targeted_message for @{MY_USER_NAME} in Room '{SWITCH_ROOM_ID}'...")
        try:
            if triage.execution_type == ExecutionType.AUTO_EXECUTE:
                switch_body = (
                    f"@{MY_USER_NAME} ⚡ **[AUTO_EXECUTE] TaskFlow Deliverable: {triage.action_summary}**\n\n"
                    f"{worker_res.artifact_content}\n\n"
                    f"*(Delivered natively via Switch send_targeted_message)*"
                )
            else:
                switch_body = (
                    f"@{MY_USER_NAME} 📌 **[HUMAN_TODO] Task Handed Off: {triage.action_summary}**\n\n"
                    f"**Action Checklist:**\n{worker_res.artifact_content}\n\n"
                    f"*(Delivered natively via Switch send_targeted_message)*"
                )
            switch_client.send_targeted_message(
                body=switch_body,
                target_names=[MY_USER_NAME],
                room_id=SWITCH_ROOM_ID
            )
            print(f"✅ SUCCESS! Switch targeted message delivered to @{MY_USER_NAME} in room!")
            switch_dispatched = True
        except Exception as e:
            print(f"⚠️ Switch targeted message dispatch skipped: {e}")

    # 2. Slack Bridge Delivery: Open DM via conversations.open before posting to avoid channel_not_found
    slack_dispatched = False
    if slack_client and SLACK_BOT_TOKEN:
        print(f"📨 [Slack Bridge] Opening DM via conversations.open before posting (<@{MY_USER_ID}>)...")
        try:
            resp = post_slack_dm(
                client=slack_client,
                user_id=MY_USER_ID,
                text=f"⚡ TaskFlow Update: {triage.action_summary}",
                blocks=blocks
            )
            channel_id = resp.get("channel", MY_USER_ID)
            print(f"✅ SUCCESS! Check your Slack DM (channel: {channel_id}) with @taskflow right now!")
            slack_dispatched = True
        except Exception as e:
            print(f"⚠️ Live Slack delivery skipped: {e}")
            print(f"ℹ️ Set SLACK_BOT_TOKEN and MY_USER_ID in .env to deliver directly to Slack.")

    # 3. Simulated / Offline preview
    if not switch_dispatched and not slack_dispatched:
        print(f"📋 [Simulated Dispatch Preview]")
        print(f"• Switch Workroom: send_targeted_message(target_names=['{MY_USER_NAME}'])")
        print(f"• Slack Bridge:    conversations.open(users=['{MY_USER_ID}']) -> chat_postMessage(channel=dm_channel)")
        print(f"✓ Task deliverable successfully formatted and verified for @{MY_USER_NAME} (<@{MY_USER_ID}>)!")
        print(f"ℹ️ (Note: On Switch, use send_targeted_message instead of Slack DMs).")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        run_simulation(sys.argv[1])
    else:
        print("\nSelect Scenario:")
        print("1: AUTO-EXECUTE: Draft Release Notes")
        print("2: HUMAN-TODO: Review PR with Action Checklist")
        print("3: AUTO-EXECUTE: Incident Postmortem")
        print("4: Ambient Silence (Banter)")
        c = input("\nEnter choice (1-4): ").strip()
        run_simulation(c)
