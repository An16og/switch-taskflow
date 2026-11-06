import sys
import time
if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')

from models import SlackMessage, ConfidenceLevel, ExecutionType
from llm_gateway import LLMGateway
from agents.triage_agent import TriageAgent
from agents.worker_agent import WorkerAgent
from slack_cards import build_auto_execute_card, build_human_todo_card, build_assigner_card
from slack_sdk import WebClient
from config import SLACK_BOT_TOKEN

gateway = LLMGateway()
triage_agent = TriageAgent(gateway=gateway)
worker_agent = WorkerAgent(gateway=gateway)
slack_client = WebClient(token=SLACK_BOT_TOKEN)

MY_USER_ID = "U0CFRP39TRS"

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

    print(f"\n📨 Sending card to your Slack DM (<@{MY_USER_ID}>)...")
    try:
        slack_client.chat_postMessage(
            channel=MY_USER_ID,
            text=f"⚡ TaskFlow Update: {triage.action_summary}",
            blocks=blocks
        )
        print("✅ SUCCESS! Check your Slack DM with @taskflow right now!")
    except Exception as e:
        print(f"❌ Slack API Error: {e}")

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
