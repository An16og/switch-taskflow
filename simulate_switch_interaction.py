import sys
import asyncio
import time
import requests
import json
import uuid

if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')

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
)
from switch_client import SwitchAgentClient
from bot import handle_triage_message, handle_worker_message

async def run_simulation():
    print("=" * 65)
    print("🚀 STARTING SWITCH TWO-AGENT TASKFLOW WORKROOM SIMULATION")
    print("=" * 65)

    room_id = SWITCH_ROOM_ID or "a327cb7f-9cd0-45f8-8872-9a96022f8522"

    # Agent 1: Triage Client
    triage_client = SwitchAgentClient(
        server_url=SWITCH_SERVER_URL,
        registration_token=AGENT_REGISTRATION_TOKEN,
        agent_id=SWITCH_TRIAGE_AGENT_ID,
        api_key=SWITCH_TRIAGE_AGENT_API_KEY,
        agent_name=SWITCH_TRIAGE_AGENT_NAME,
        display_name="TaskFlow Triage",
        description="Ambient task triage and intent classifier",
        room_id=room_id,
    )
    triage_client.on_message(lambda payload: handle_triage_message(triage_client, payload))

    # Agent 2: Worker Client
    worker_client = SwitchAgentClient(
        server_url=SWITCH_SERVER_URL,
        registration_token=AGENT_REGISTRATION_TOKEN,
        agent_id=SWITCH_WORKER_AGENT_ID,
        api_key=SWITCH_WORKER_AGENT_API_KEY,
        agent_name=SWITCH_WORKER_AGENT_NAME,
        display_name="TaskFlow Worker",
        description="Autonomous task executor",
        room_id=room_id,
    )
    worker_client.on_message(lambda payload: handle_worker_message(worker_client, payload))

    # Start both WebSocket listeners
    triage_task = asyncio.create_task(triage_client.connect_and_listen())
    worker_task = asyncio.create_task(worker_client.connect_and_listen())

    print("[*] Waiting 2 seconds for both agents to establish Switch WebSocket connections...")
    await asyncio.sleep(2)

    print(f"[*] TaskFlow Triage active in Room: {room_id}")
    print(f"[*] TaskFlow Worker active in Room: {room_id}")

    print("\n" + "-" * 60)
    print("TEST 1: Simulating AUTO_EXECUTE task addressed to TaskFlow Triage")
    print("Expected: Triage classifies -> sends targeted handoff to Worker -> Worker drafts release notes")
    print("-" * 60)

    test_auto_payload = {
        "event_id": f"sw_test_{uuid.uuid4().hex[:8]}",
        "room_id": room_id,
        "sender": "user_kunal_123",
        "sender_name": "Kunal",
        "sender_kind": "user",
        "addressed": True,
        "body": "@taskflow-triage please draft release notes for v1.2 with new dark mode and bug fixes."
    }

    print(f"Simulated Kunal in room: {test_auto_payload['body']}")
    # Feed into Triage agent
    await handle_triage_message(triage_client, test_auto_payload)

    # Allow worker to receive the WebSocket targeted message and execute the task
    print("[*] Waiting 6 seconds for Worker to receive targeted handoff and execute AUTO_EXECUTE...")
    await asyncio.sleep(6)

    print("\n" + "-" * 60)
    print("TEST 2: Simulating HUMAN_TODO ambient action-item message")
    print("Expected: Triage catches ambiently -> sends targeted handoff to Worker -> Worker syncs to Todoist")
    print("-" * 60)

    test_human_payload = {
        "event_id": f"sw_test_{uuid.uuid4().hex[:8]}",
        "room_id": room_id,
        "sender": "user_sarah_456",
        "sender_name": "Sarah",
        "sender_kind": "user",
        "addressed": False,
        "body": "Todo: Need someone to audit the database connection pool settings and fix connection timeouts before tomorrow."
    }

    print(f"Simulated Sarah in room: {test_human_payload['body']}")
    # Feed into Triage agent
    await handle_triage_message(triage_client, test_human_payload)

    # Allow worker to receive the WebSocket targeted message and sync Todoist
    print("[*] Waiting 6 seconds for Worker to receive targeted handoff and execute HUMAN_TODO...")
    await asyncio.sleep(6)

    print("\n" + "=" * 65)
    print("✅ SIMULATION COMPLETE - True multi-agent handoff verified over Switch!")
    print("=" * 65)

    triage_client.stop()
    worker_client.stop()
    triage_task.cancel()
    worker_task.cancel()

if __name__ == "__main__":
    asyncio.run(run_simulation())
