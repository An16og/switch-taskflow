import asyncio
import json
import logging
import uuid
from typing import Callable, Awaitable, Optional, Dict, Any, List

import requests
import websockets

logger = logging.getLogger("SwitchClient")


class SwitchAgentClient:
    """Native Switch Agent Client.
    
    Handles:
    1. Agent Registration & API Key resolution (POST /agents).
    2. Room Discovery & Claiming (POST /agents/{id}/ops/connect_to_room).
    3. Non-blocking WebSocket connection & heartbeat engine (/agents/{id}/connection/ws).
    4. Room Ops: post_message, send_targeted_message, read_context.
    """

    def __init__(
        self,
        server_url: str = "http://127.0.0.1:52422",
        registration_token: Optional[str] = None,
        agent_id: Optional[str] = None,
        api_key: Optional[str] = None,
        room_id: Optional[str] = None,
        agent_name: str = "taskflow",
        display_name: str = "TaskFlow",
        description: str = "Autonomous Task Triage & Workroom Worker",
    ):
        self.server_url = server_url.rstrip("/")
        self.ws_base_url = self.server_url.replace("http://", "ws://").replace("https://", "wss://")
        self.registration_token = registration_token
        self.agent_id = agent_id
        self.api_key = api_key
        self.room_id = room_id
        self.agent_name = agent_name
        self.display_name = display_name
        self.description = description

        self.connection_id = str(uuid.uuid4())
        self._ws = None
        self._running = False
        self._message_handlers: List[Callable[[Dict[str, Any]], Awaitable[None]]] = []

    @property
    def auth_headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    @property
    def op_headers(self) -> Dict[str, str]:
        headers = self.auth_headers.copy()
        headers["X-Switch-Connection-Id"] = self.connection_id
        return headers

    def register_or_validate(self) -> bool:
        """Ensures the agent is registered with the Switch Server."""
        if self.agent_id and self.api_key:
            try:
                resp = requests.get(
                    f"{self.server_url}/agents/{self.agent_id}/ops",
                    headers=self.auth_headers,
                    timeout=5,
                )
                if resp.status_code == 200:
                    logger.info("Validated existing Switch Agent ID: %s", self.agent_id)
                    return True
                logger.warning("Existing agent credentials invalid (%s). Re-registering...", resp.status_code)
            except Exception as e:
                logger.warning("Failed to validate existing agent: %s", e)

        if not self.registration_token:
            raise ValueError("Cannot register agent without AGENT_REGISTRATION_TOKEN")

        payload = {
            "name": self.agent_name,
            "description": self.description,
            "display_name": self.display_name,
            "connector_type": "custom",
            "integration_profile": {
                "connection_model": "always_on",
                "message_exchange": True,
                "pre_invocation_mediation": [],
                "post_invocation_mediation": [],
                "event_reporting": [],
            },
            "overwrite": True,
        }

        logger.info("Registering '%s' on Switch Server %s...", self.agent_name, self.server_url)
        resp = requests.post(
            f"{self.server_url}/agents",
            headers={
                "Authorization": f"Bearer {self.registration_token}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=10,
        )

        if resp.status_code not in (200, 201):
            raise RuntimeError(f"Switch agent registration failed [{resp.status_code}]: {resp.text}")

        data = resp.json()
        self.agent_id = data.get("id")
        self.api_key = data.get("api_key")
        logger.info("Registered successfully! Agent ID: %s", self.agent_id)
        return True

    def ensure_room(self) -> str:
        """Finds an existing room or creates an operations room for the agent."""
        if not self.agent_id or not self.api_key:
            raise RuntimeError("Agent must be registered before ensuring room membership.")

        # Check existing assigned rooms
        try:
            resp = requests.post(
                f"{self.server_url}/agents/{self.agent_id}/ops/list_rooms",
                headers=self.auth_headers,
                json={},
                timeout=5,
            )
            if resp.status_code == 200:
                rooms = resp.json().get("result", [])
                if self.room_id:
                    for r in rooms:
                        if r.get("room_id") == self.room_id:
                            logger.info("Found assigned room: %s (%s)", r.get("name"), self.room_id)
                            return self.room_id
                if rooms:
                    self.room_id = rooms[0].get("room_id")
                    logger.info("Connected to room: %s (%s)", rooms[0].get("name"), self.room_id)
                    return self.room_id
        except Exception as e:
            logger.warning("Failed listing rooms: %s", e)

        # Create new room if none found
        create_payload = {
            "name": "taskflow-workroom",
            "description": "Autonomous Task Triage & Workroom Worker",
            "agent_names": [self.agent_name],
            "internal_only": True,
        }

        logger.info("Creating default room for '%s'...", self.agent_name)
        resp = requests.post(
            f"{self.server_url}/agents/{self.agent_id}/ops/create_room",
            headers=self.auth_headers,
            json=create_payload,
            timeout=10,
        )
        if resp.status_code == 200:
            result = resp.json().get("result", {})
            self.room_id = result.get("id")
            logger.info("Created new room: %s (id: %s)", result.get("name"), self.room_id)
            return self.room_id

        raise RuntimeError(f"Could not find or create room: [{resp.status_code}] {resp.text}")

    def on_message(self, handler: Callable[[Dict[str, Any]], Awaitable[None]]):
        """Registers an asynchronous callback for incoming room messages."""
        self._message_handlers.append(handler)

    async def connect_and_listen(self):
        """Connects over WebSocket with a dedicated heartbeat pump."""
        self.register_or_validate()
        self.ensure_room()

        ws_url = (
            f"{self.ws_base_url}/agents/{self.agent_id}/connection/ws"
            f"?connection_id={self.connection_id}&rooms={self.room_id}"
        )
        logger.info("Opening Switch WebSocket connection: %s", ws_url)
        headers = {"Authorization": f"Bearer {self.api_key}"}

        self._running = True
        try:
            try:
                ws_ctx = websockets.connect(ws_url, additional_headers=headers)
            except TypeError:
                ws_ctx = websockets.connect(ws_url, extra_headers=headers)

            async with ws_ctx as ws:
                self._ws = ws
                logger.info("Connected to Switch Server WebSocket! Connection ID: %s", self.connection_id)

                while self._running:
                    try:
                        raw_msg = await ws.recv()
                    except websockets.exceptions.ConnectionClosed as e:
                        logger.warning("Switch WebSocket closed: %s", e)
                        break

                    try:
                        frame = json.loads(raw_msg)
                    except Exception:
                        continue

                    event = frame.get("event") or frame.get("type")
                    data = frame.get("data") or frame.get("payload") or {}

                    # Real-time Heartbeat ping -> pong (must never stall)
                    if event == "ping":
                        pong_frame = {"type": "pong"}
                        await ws.send(json.dumps(pong_frame))
                        logger.debug("Received ping, sent pong.")
                        continue

                    if event == "connection_state":
                        logger.info("Connection confirmed by Switch Server: rooms=%s", frame.get("rooms") or data.get("rooms"))
                        continue

                    if event == "message":
                        # Switch message frames may nest payload under frame['data']['payload'] or frame['payload']
                        raw_data = frame.get("data") if isinstance(frame.get("data"), dict) else frame
                        if isinstance(raw_data.get("payload"), dict):
                            inner_payload = raw_data["payload"]
                        elif isinstance(frame.get("payload"), dict):
                            inner_payload = frame["payload"]
                        else:
                            inner_payload = raw_data

                        event_data = {
                            "room_id": frame.get("room_id") or raw_data.get("room_id") or self.room_id,
                            "channel_type": frame.get("channel_type") or raw_data.get("channel_type"),
                            **inner_payload,
                        }
                        sender = event_data.get("sender_name") or event_data.get("sender")
                        logger.info("Received room message: sender=%s, addressed=%s, body=%s", sender, event_data.get("addressed"), str(event_data.get("body", ""))[:40])
                        for handler in self._message_handlers:
                            asyncio.create_task(self._safe_invoke_handler(handler, event_data))

        except Exception as e:
            logger.error("WebSocket connection error: %s", e)
            raise

    async def _safe_invoke_handler(self, handler: Callable[[Dict[str, Any]], Any], data: Dict[str, Any]):
        try:
            res = handler(data)
            if asyncio.iscoroutine(res):
                await res
        except Exception as ex:
            logger.error("Error in message handler: %s", ex, exc_info=True)

    def stop(self):
        self._running = False
        if self._ws:
            asyncio.create_task(self._ws.close())

    def invite_agent_to_room(self, agent_name: str, room_id: Optional[str] = None) -> bool:
        """Invites another Switch agent into the room by name."""
        target_room = room_id or self.room_id
        if not target_room:
            return False
        payload = {"room_id": target_room, "agent_name": agent_name}
        try:
            resp = requests.post(
                f"{self.server_url}/agents/{self.agent_id}/ops/invite_agent_to_room",
                headers=self.op_headers,
                json=payload,
                timeout=10,
            )
            return resp.status_code == 200
        except Exception as e:
            logger.warning("Failed inviting agent %s: %s", agent_name, e)
            return False

    def set_agent_addressing(self, target_agent_id: str, addressing: str = "anyone") -> bool:
        """Sets an agent's addressing policy ('anyone', 'owner_and_owner_agents', or 'owner_only')."""
        payload = {
            "agent_id": target_agent_id,
            "addressing": addressing
        }
        try:
            resp = requests.post(
                f"{self.server_url}/agents/{self.agent_id}/ops/update_agent_detail",
                headers=self.op_headers,
                json=payload,
                timeout=10,
            )
            if resp.status_code == 200:
                logger.info("Updated addressing for agent %s to '%s'", target_agent_id, addressing)
                return True
            logger.warning("Failed updating addressing for agent %s [%s]: %s", target_agent_id, resp.status_code, resp.text)
            return False
        except Exception as e:
            logger.warning("Error updating addressing for agent %s: %s", target_agent_id, e)
            return False

    def post_message(self, body: str, thread_id: Optional[str] = None, room_id: Optional[str] = None) -> Dict[str, Any]:
        """Broadcasts a message to the connected room (optionally threaded)."""
        payload: Dict[str, Any] = {"body": body}
        if thread_id:
            payload["thread_id"] = thread_id

        headers = self.op_headers.copy()
        if room_id:
            headers["X-Switch-Room-Id"] = room_id

        resp = requests.post(
            f"{self.server_url}/agents/{self.agent_id}/ops/post_message",
            headers=headers,
            json=payload,
            timeout=10,
        )
        if resp.status_code == 200:
            return resp.json().get("result", {})

        # If thread_id is not found in room, fall back to top-level broadcast
        if thread_id and resp.status_code == 400 and "thread_id not found" in resp.text.lower():
            logger.warning("thread_id '%s' not found in room, falling back to top-level message", thread_id)
            payload.pop("thread_id", None)
            retry_resp = requests.post(
                f"{self.server_url}/agents/{self.agent_id}/ops/post_message",
                headers=headers,
                json=payload,
                timeout=10,
            )
            if retry_resp.status_code == 200:
                return retry_resp.json().get("result", {})

        logger.error("post_message failed [%s]: %s", resp.status_code, resp.text)
        return {"error": resp.text}

    def send_targeted_message(
        self,
        body: str,
        target_names: Optional[List[str]] = None,
        target_roles: Optional[List[str]] = None,
        thread_id: Optional[str] = None,
        room_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Sends an addressed message to specific participants."""
        payload: Dict[str, Any] = {"body": body}
        if target_names:
            payload["target_names"] = target_names
        if target_roles:
            payload["target_roles"] = target_roles
        if thread_id:
            payload["thread_id"] = thread_id

        # At least one target is required by Switch
        if not target_names and not target_roles:
            return self.post_message(body, thread_id, room_id=room_id)

        headers = self.op_headers.copy()
        if room_id:
            headers["X-Switch-Room-Id"] = room_id

        resp = requests.post(
            f"{self.server_url}/agents/{self.agent_id}/ops/send_targeted_message",
            headers=headers,
            json=payload,
            timeout=10,
        )
        if resp.status_code == 200:
            return resp.json().get("result", {})
        
        # Fall back to post_message if targeting fails
        logger.warning("send_targeted_message failed [%s]: %s, falling back to post_message", resp.status_code, resp.text)
        target_prefix = " ".join([f"@{name}" for name in (target_names or [])])
        fallback_body = f"{target_prefix} {body}" if target_prefix else body
        return self.post_message(fallback_body, thread_id, room_id=room_id)

    def read_context(self, limit: int = 20) -> List[Dict[str, Any]]:
        """Reads recent messages and context from the room."""
        resp = requests.post(
            f"{self.server_url}/agents/{self.agent_id}/ops/read_context",
            headers=self.op_headers,
            json={"limit": limit},
            timeout=10,
        )
        if resp.status_code == 200:
            return resp.json().get("result", {}).get("messages", [])
        return []
