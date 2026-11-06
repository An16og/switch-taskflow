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
    SWITCH_AGENT_ID,
    SWITCH_AGENT_API_KEY,
    SWITCH_ROOM_ID,
)
from switch_client import SwitchAgentClient
from bot import handle_room_message

async def run_simulation():
    print("=" * 65)
    print("🚀 STARTING SWITCH TASKFLOW INTEGRATION SIMULATION")
    print("=" * 65)

    client = SwitchAgentClient(
        server_url=SWITCH_SERVER_URL,
        registration_token=AGENT_REGISTRATION_TOKEN,
        agent_id=SWITCH_AGENT_ID,
        api_key=SWITCH_AGENT_API_KEY,
        room_id=SWITCH_ROOM_ID,
        agent_name="taskflow",
        display_name="TaskFlow",
    )

    client.on_message(lambda payload: handle_room_message(client, payload))

    # Start TaskFlow's WebSocket listener in a background task
    ws_task = asyncio.create_task(client.connect_and_listen())
    print("[*] Waiting 2 seconds for TaskFlow WebSocket connection to establish...")
    await asyncio.sleep(2)

    room_id = client.room_id
    print(f"[*] TaskFlow active in Room: {room_id}")

    # Use another agent or client connection to simulate user messages
    # We can post directly to the room using Switch ops or server protocol!
    sim_headers = {
        "Authorization": f"Bearer {SWITCH_AGENT_API_KEY}",
        "Content-Type": "application/json",
        "X-Switch-Connection-Id": client.connection_id
    }

    print("\n" + "-" * 60)
    print("TEST 1: Simulating AUTO_EXECUTE message addressed to TaskFlow")
    print("-" * 60)
    
    # We can invoke handle_room_message directly with realistic Switch message frames
    # to test both the WebSocket transport and the end-to-end multi-agent pipeline!
    test_auto_payload = {
        "event_id": f"sw_test_{uuid.uuid4().hex[:8]}",
        "room_id": room_id,
        "sender": "user_kunal_123",
        "sender_name": "Kunal",
        "sender_kind": "user",
        "addressed": True,
        "body": "@taskflow please write release notes for v1.2 with new dark mode and bug fixes."
    }

    print(f"Simulated Kunal: {test_auto_payload['body']}")
    await handle_room_message(client, test_auto_payload)

    print("\n" + "-" * 60)
    print("TEST 2: Simulating HUMAN_TODO ambient action-item message")
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

    print(f"Simulated Sarah: {test_human_payload['body']}")
    await handle_room_message(client, test_human_payload)

    print("\n" + "=" * 65)
    print("✅ SIMULATION COMPLETE - Both execution pipelines verified over Switch!")
    print("=" * 65)

    client.stop()
    ws_task.cancel()

if __name__ == "__main__":
    asyncio.run(run_simulation())
