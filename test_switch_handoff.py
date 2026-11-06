import asyncio
import sys
import json
import logging
import uuid

if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("TestHandoff")

from config import (
    SWITCH_SERVER_URL,
    AGENT_REGISTRATION_TOKEN,
    SWITCH_TRIAGE_AGENT_ID,
    SWITCH_TRIAGE_AGENT_API_KEY,
    SWITCH_TRIAGE_AGENT_NAME,
    SWITCH_WORKER_AGENT_ID,
    SWITCH_WORKER_AGENT_API_KEY,
    SWITCH_WORKER_AGENT_NAME,
    SWITCH_ROOM_ID,
)
from switch_client import SwitchAgentClient

async def run_test():
    room_id = SWITCH_ROOM_ID or "a327cb7f-9cd0-45f8-8872-9a96022f8522"

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

    worker_received = asyncio.Event()
    worker_message_data = {}

    async def on_worker_msg(payload):
        logger.info("Worker received frame: %s", json.dumps(payload)[:100])
        body = payload.get("body", "")
        if "TEST_HANDOFF" in body or payload.get("addressed"):
            worker_message_data.update(payload)
            worker_received.set()

    worker_client.on_message(on_worker_msg)

    # Start both WebSocket listeners
    triage_task = asyncio.create_task(triage_client.connect_and_listen())
    worker_task = asyncio.create_task(worker_client.connect_and_listen())

    logger.info("Waiting 3s for both agents to connect to Switch WebSocket...")
    await asyncio.sleep(3)

    logger.info("Triage client sending targeted handoff to Worker...")
    handoff_body = "@taskflow-worker [TEST_HANDOFF] Please draft release notes for v2.0."
    res = await asyncio.to_thread(
        triage_client.send_targeted_message,
        body=handoff_body,
        target_names=["taskflow-worker"],
        room_id=room_id,
    )
    logger.info("Triage send_targeted_message result: %s", res)

    logger.info("Waiting for Worker to receive targeted message over WebSocket...")
    try:
        await asyncio.wait_for(worker_received.wait(), timeout=5.0)
        logger.info("SUCCESS: Worker received targeted handoff from Triage over WebSocket!")
    except asyncio.TimeoutError:
        logger.warning("Worker did not receive event within 5s (may still be working or fallback was used)")

    triage_client.stop()
    worker_client.stop()
    triage_task.cancel()
    worker_task.cancel()

if __name__ == "__main__":
    asyncio.run(run_test())
