import asyncio
import json
import logging
import os
import re
import time
import uuid
from pathlib import Path
from typing import Callable, Awaitable, Optional, Dict, Any, List

import requests
import websockets
from room_documents import default_room_document_store, RoomDocumentStore, DocumentStatus

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
        overwrite: bool = False,
        connection_id: Optional[str] = None,
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
        self.overwrite = overwrite

        # Stable connection_id, generation, and cursor for Switch WebSocket liveness & reconnection
        self.connection_id = connection_id or str(uuid.uuid4())
        self.generation: Optional[int] = None
        self.cursor: int = 0
        self._ws = None
        self._running = False
        self._message_handlers: List[Callable[[Dict[str, Any]], Awaitable[None]]] = []
        self.document_store: RoomDocumentStore = default_room_document_store

        # Process health & heartbeat metrics
        self._connected_at: Optional[float] = None
        self._pings_received: int = 0
        self._pongs_sent: int = 0
        self._messages_processed: int = 0
        self._health_task: Optional[asyncio.Task] = None
        self._health_interval: float = 30.0

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

    def register_or_validate(self, overwrite: Optional[bool] = None) -> bool:
        """Ensures the agent is registered with the Switch Server without overwriting existing credentials.
        
        Protocol rule: overwrite=True issues a new API key and invalidates the previous one,
        killing existing agent sessions. By default, overwrite=False is strictly enforced.
        """
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
                logger.warning("Existing agent credentials invalid (%s). Attempting safe registration...", resp.status_code)
            except Exception as e:
                logger.warning("Failed to validate existing agent: %s", e)

        if not self.registration_token:
            raise ValueError("Cannot register agent without AGENT_REGISTRATION_TOKEN")

        do_overwrite = self.overwrite if overwrite is None else overwrite

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
            "overwrite": do_overwrite,
        }

        logger.info("Registering '%s' on Switch Server %s (overwrite=%s)...", self.agent_name, self.server_url, do_overwrite)
        resp = requests.post(
            f"{self.server_url}/agents",
            headers={
                "Authorization": f"Bearer {self.registration_token}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=10,
        )

        if resp.status_code in (200, 201):
            data = resp.json()
            self.agent_id = data.get("id")
            self.api_key = data.get("api_key")
            logger.info("Registered successfully! Agent ID: %s", self.agent_id)
            return True

        if resp.status_code == 409:
            err_msg = (
                f"Agent '{self.agent_name}' already exists on Switch Server. "
                "Registration stopped with overwrite=False to protect active credentials and connections "
                "(per Switch protocol: overwrite=True rotates the API key and invalidates the old one). "
                "Please configure SWITCH_*_AGENT_ID and SWITCH_*_AGENT_API_KEY in your environment, "
                "or explicitly pass overwrite=True only if you intend to rotate credentials."
            )
            logger.error(err_msg)
            raise RuntimeError(err_msg)

        raise RuntimeError(f"Switch agent registration failed [{resp.status_code}]: {resp.text}")

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

        # Create new room if none found (bridged by default so chat channel is enabled in Console)
        create_payload = {
            "name": "taskflow-workroom",
            "description": "Autonomous Task Triage & Workroom Worker",
            "agent_names": [self.agent_name],
            "channel_type": "channel_public",
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

    async def connect_and_listen(
        self,
        apply_briefing: bool = True,
        auto_reconnect: bool = True,
        reconnect_delay: float = 1.0,
        max_reconnect_delay: float = 30.0,
    ):
        """Connects over WebSocket with a dedicated heartbeat pump and resilient reconnection.
        
        Maintains stable connection_id, tracks stream generation, advances cursor,
        and returns pong frames containing cursor required by Switch liveness protocol.
        """
        self.register_or_validate()
        self.ensure_room()

        # Apply ROOM_BRIEFING.md on connect via update_room_instructions
        if apply_briefing:
            try:
                self.apply_room_briefing(room_id=self.room_id)
            except Exception as e:
                logger.warning("Could not apply ROOM_BRIEFING.md on connect: %s", e)

        self._running = True
        current_delay = reconnect_delay

        try:
            while self._running:
                # Reconnect with stable connection_id, generation, and cursor
                query_params = [
                    f"connection_id={self.connection_id}",
                    f"rooms={self.room_id}",
                ]
                if self.generation is not None:
                    query_params.append(f"expected_generation={self.generation}")
                if self.cursor > 0:
                    query_params.append(f"start_at={self.cursor}")

                ws_url = f"{self.ws_base_url}/agents/{self.agent_id}/connection/ws?{'&'.join(query_params)}"
                logger.info("Opening Switch WebSocket connection: %s", ws_url)
                headers = {"Authorization": f"Bearer {self.api_key}"}

                try:
                    try:
                        ws_ctx = websockets.connect(ws_url, additional_headers=headers)
                    except TypeError:
                        ws_ctx = websockets.connect(ws_url, extra_headers=headers)

                    async with ws_ctx as ws:
                        self._ws = ws
                        self._connected_at = time.time()
                        current_delay = reconnect_delay
                        logger.info("Connected to Switch Server WebSocket! Connection ID: %s", self.connection_id)
                        if not self._health_task or self._health_task.done():
                            self._health_task = asyncio.create_task(self._run_health_logger())

                        try:
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

                                # Advance and track cursor from frame or data
                                frame_cursor = frame.get("cursor") or frame.get("id")
                                if not isinstance(frame_cursor, int) and isinstance(data, dict):
                                    frame_cursor = data.get("cursor")
                                if isinstance(frame_cursor, int) and frame_cursor > self.cursor:
                                    self.cursor = frame_cursor

                                # Switch liveness protocol: server sends ping, client MUST answer pong with cursor
                                if event == "ping":
                                    self._pings_received += 1
                                    pong_frame = {
                                        "type": "pong",
                                        "cursor": self.cursor,
                                    }
                                    if self.generation is not None:
                                        pong_frame["generation"] = self.generation
                                    await ws.send(json.dumps(pong_frame))
                                    self._pongs_sent += 1
                                    logger.info(
                                        "💓 [Heartbeat] Agent '%s' answered ping -> pong (cursor=%s, generation=%s, conn_id=%s)",
                                        self.agent_name,
                                        self.cursor,
                                        self.generation,
                                        self.connection_id,
                                    )
                                    continue

                                if event == "connection_state":
                                    # Server informs client of confirmed generation, cursor, and connection_id
                                    server_conn_id = data.get("connection_id") if isinstance(data, dict) else None
                                    if server_conn_id:
                                        self.connection_id = server_conn_id
                                    if isinstance(data, dict):
                                        if "generation" in data:
                                            self.generation = data["generation"]
                                        if "cursor" in data and isinstance(data["cursor"], int):
                                            self.cursor = max(self.cursor, data["cursor"])

                                    logger.info(
                                        "Connection confirmed by Switch Server: rooms=%s, generation=%s, cursor=%s",
                                        frame.get("rooms") or (data.get("rooms") if isinstance(data, dict) else None),
                                        self.generation,
                                        self.cursor,
                                    )
                                    if apply_briefing:
                                        try:
                                            self.apply_room_briefing(room_id=self.room_id)
                                        except Exception as e:
                                            logger.debug("Could not re-apply room briefing on connection_state: %s", e)
                                    continue

                                if event == "refused":
                                    logger.warning("Connection refused by Switch Server: %s", data)
                                    # Reset generation if stale incarnation was rejected
                                    self.generation = None
                                    break

                                if event == "message":
                                    self._messages_processed += 1
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
                        finally:
                            if self._health_task and not self._health_task.done():
                                self._health_task.cancel()
                                self._health_task = None

                except asyncio.CancelledError:
                    logger.info("Switch connection cancelled; stopping listener.")
                    break
                except Exception as e:
                    if not self._running or not auto_reconnect:
                        logger.warning("Switch WebSocket error (no reconnect): %s", e)
                        break
                    logger.warning(
                        "Switch WebSocket disconnected (%s). Reconnecting with stable connection_id=%s, generation=%s, cursor=%s in %.1fs...",
                        e,
                        self.connection_id,
                        self.generation,
                        self.cursor,
                        current_delay,
                    )
                    await asyncio.sleep(current_delay)
                    current_delay = min(current_delay * 1.5, max_reconnect_delay)

                if not auto_reconnect or not self._running:
                    break
        finally:
            if self._health_task and not self._health_task.done():
                self._health_task.cancel()
                self._health_task = None
            if self._ws and not getattr(self._ws, "closed", False):
                try:
                    await self.close(code=1000, reason="Listener stopped")
                    logger.info("🔌 [Graceful Close] WebSocket connection closed cleanly on listener exit.")
                except Exception as e:
                    logger.debug("Error closing WS on exit: %s", e)

    async def _safe_invoke_handler(self, handler: Callable[[Dict[str, Any]], Any], data: Dict[str, Any]):
        try:
            res = handler(data)
            if asyncio.iscoroutine(res):
                await res
        except Exception as ex:
            logger.error("Error in message handler: %s", ex, exc_info=True)

    def log_process_health(self) -> Dict[str, Any]:
        """Emits a structured process health and heartbeat log line and returns health metrics."""
        uptime = time.time() - self._connected_at if self._connected_at else 0.0
        ws_active = bool(self._ws and not getattr(self._ws, "closed", False))
        state = {
            "agent_name": self.agent_name,
            "agent_id": self.agent_id,
            "pid": os.getpid(),
            "status": "CONNECTED" if ws_active else ("RECONNECTING" if self._running else "STOPPED"),
            "room_id": self.room_id,
            "cursor": self.cursor,
            "generation": self.generation,
            "connection_id": self.connection_id,
            "uptime_seconds": round(uptime, 1),
            "pings_received": self._pings_received,
            "pongs_sent": self._pongs_sent,
            "messages_processed": self._messages_processed,
        }
        logger.info(
            "💚 [Process Health] Agent '%s' (PID %d) status=%s | room=%s | cursor=%s | generation=%s | uptime=%.0fs | pongs=%d | msgs=%d",
            state["agent_name"],
            state["pid"],
            state["status"],
            state["room_id"],
            state["cursor"],
            state["generation"],
            state["uptime_seconds"],
            state["pongs_sent"],
            state["messages_processed"],
        )
        return state

    async def _run_health_logger(self):
        """Periodically emits process health and heartbeat log lines for observability."""
        while self._running:
            try:
                await asyncio.sleep(self._health_interval)
                if self._running and self._ws and not getattr(self._ws, "closed", False):
                    self.log_process_health()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug("Error in health logger: %s", e)

    async def close(self, code: int = 1000, reason: str = "Graceful shutdown"):
        """Gracefully closes the WebSocket connection with status code and reason."""
        self._running = False
        if self._health_task and not self._health_task.done():
            self._health_task.cancel()
            try:
                await self._health_task
            except asyncio.CancelledError:
                pass
            self._health_task = None

        if self._ws is not None:
            try:
                if not getattr(self._ws, "closed", False):
                    logger.info(
                        "🔌 [Graceful Close] Closing WebSocket for agent '%s' (code=%d, reason='%s')...",
                        self.agent_name,
                        code,
                        reason,
                    )
                    try:
                        await self._ws.close(code=code, reason=reason)
                    except TypeError:
                        await self._ws.close()
                    logger.info("✅ [Graceful Close] WebSocket closed cleanly for agent '%s'.", self.agent_name)
            except Exception as e:
                logger.debug("Error during graceful WebSocket close for '%s': %s", self.agent_name, e)
            finally:
                self._ws = None

    def stop(self):
        """Signals the agent to stop listening and initiates graceful close."""
        self._running = False
        if self._health_task and not self._health_task.done():
            self._health_task.cancel()
        if self._ws and not getattr(self._ws, "closed", False):
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self.close())
            except RuntimeError:
                try:
                    asyncio.run(self.close())
                except Exception as e:
                    logger.debug("Error in stop() fallback close: %s", e)

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

    def update_room_instructions(self, instructions: str, room_id: Optional[str] = None) -> bool:
        """Updates the room instructions / operational briefing on Switch."""
        target_room = room_id or self.room_id
        if not target_room:
            return False
        payload = {
            "room_id": target_room,
            "instructions": instructions
        }
        try:
            resp = requests.post(
                f"{self.server_url}/agents/{self.agent_id}/ops/update_room",
                headers=self.auth_headers,
                json=payload,
                timeout=10,
            )
            if resp.status_code == 200:
                logger.info("Successfully updated room %s instructions (%d chars)", target_room, len(instructions))
                return True
            logger.warning("Failed updating room instructions [%s]: %s", resp.status_code, resp.text)
            return False
        except Exception as e:
            logger.warning("Error updating room instructions: %s", e)
            return False

    def apply_room_briefing(
        self,
        briefing_path: Optional[str] = None,
        room_id: Optional[str] = None,
    ) -> bool:
        """Applies ROOM_BRIEFING.md on connect via update_room_instructions.
        
        Reads the room operational briefing markdown file and updates room
        instructions on Switch Core so all room participants and agents share
        the operational context.
        """
        target_room = room_id or self.room_id
        if not target_room:
            logger.warning("apply_room_briefing called without target room_id")
            return False

        # Resolve candidate locations for ROOM_BRIEFING.md
        candidates = []
        if briefing_path:
            candidates.append(Path(briefing_path))
        candidates.extend([
            Path(__file__).parent / "ROOM_BRIEFING.md",
            Path.cwd() / "ROOM_BRIEFING.md",
            Path(__file__).parent.parent / "ROOM_BRIEFING.md",
        ])

        briefing_content = None
        for candidate in candidates:
            if candidate.exists() and candidate.is_file():
                try:
                    briefing_content = candidate.read_text(encoding="utf-8")
                    logger.info("Loaded room briefing from %s (%d chars)", candidate.name, len(briefing_content))
                    break
                except Exception as e:
                    logger.debug("Failed reading briefing candidate %s: %s", candidate, e)

        if not briefing_content:
            logger.warning("ROOM_BRIEFING.md not found in candidate paths; skipped applying room briefing.")
            return False

        return self.update_room_instructions(briefing_content, room_id=target_room)

    def read_context(self, limit: int = 50, room_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Reads recent messages and context from the room, unpacked into a chronological timeline."""
        target_room = room_id or self.room_id
        payload = {"limit": limit}
        if target_room:
            payload["room_id"] = target_room

        # Prefer auth_headers to avoid 409 if connection_id is not actively connected to WS
        headers = self.auth_headers.copy()
        if target_room:
            headers["X-Switch-Room-Id"] = target_room

        try:
            resp = requests.post(
                f"{self.server_url}/agents/{self.agent_id}/ops/read_context",
                headers=headers,
                json=payload,
                timeout=10,
            )
            if resp.status_code != 200 and self.op_headers:
                # Retry with op_headers if server requires caller connection
                resp = requests.post(
                    f"{self.server_url}/agents/{self.agent_id}/ops/read_context",
                    headers=self.op_headers,
                    json=payload,
                    timeout=10,
                )

            if resp.status_code == 200:
                result = resp.json().get("result", {})
                if "threads" in result:
                    messages = []
                    for th in result["threads"]:
                        if "root" in th and th["root"]:
                            messages.append(th["root"])
                        for rep in th.get("replies", []):
                            if rep:
                                messages.append(rep)
                    messages.sort(key=lambda m: m.get("timestamp", 0))
                    return messages
                return result.get("messages", [])
            logger.warning("read_context failed [%s]: %s", resp.status_code, resp.text)
            return []
        except Exception as e:
            logger.error("Failed to read context: %s", e)
            return []

    def create_reference(
        self,
        ref_type: str,
        name: str,
        description: str,
        instructions: str,
        urls: List[str],
    ) -> Dict[str, Any]:
        """Creates an external reference (e.g. GitHub repo, Jira board, Confluence page) on Switch."""
        if not self.agent_id:
            return {}
        payload = {
            "type": ref_type,
            "name": name,
            "description": description,
            "instructions": instructions,
            "value": {"urls": urls},
        }
        try:
            resp = requests.post(
                f"{self.server_url}/agents/{self.agent_id}/ops/create_reference",
                headers=self.auth_headers,
                json=payload,
                timeout=10,
            )
            if resp.status_code == 200:
                logger.info("Created Switch reference '%s' (%s)", name, ref_type)
                return resp.json().get("result", {})
            logger.warning("create_reference failed [%s]: %s", resp.status_code, resp.text)
        except Exception as e:
            logger.warning("Error creating Switch reference: %s", e)
        return {}

    def attach_reference_to_room(
        self,
        reference_id: str,
        room_id: Optional[str] = None,
    ) -> bool:
        """Attaches a created reference to the specified Switch room."""
        target_room = room_id or self.room_id
        if not self.agent_id or not target_room or not reference_id:
            return False
        payload = {
            "room_id": target_room,
            "reference_id": reference_id,
        }
        try:
            resp = requests.post(
                f"{self.server_url}/agents/{self.agent_id}/ops/attach_reference_to_room",
                headers=self.auth_headers,
                json=payload,
                timeout=10,
            )
            if resp.status_code == 200:
                logger.info("Attached reference '%s' to room '%s'", reference_id, target_room)
                return True
            logger.warning("attach_reference_to_room failed [%s]: %s", resp.status_code, resp.text)
        except Exception as e:
            logger.warning("Error attaching reference to room: %s", e)
        return False

    def list_all_references(self) -> List[Dict[str, Any]]:
        """Lists all references registered by or visible to the agent on Switch."""
        if not self.agent_id:
            return []
        try:
            resp = requests.post(
                f"{self.server_url}/agents/{self.agent_id}/ops/list_all_references",
                headers=self.auth_headers,
                json={},
                timeout=5,
            )
            if resp.status_code == 200:
                return resp.json().get("result", [])
        except Exception as e:
            logger.warning("Error querying list_all_references: %s", e)
        return []

    def attach_default_room_references(
        self,
        room_id: Optional[str] = None,
        github_url: str = "https://github.com/An16og/switch-taskflow",
    ) -> List[Dict[str, Any]]:
        """Ensures repository, changelog, and ticket tracker references are attached to the room."""
        target_room = room_id or self.room_id
        if not target_room:
            return []

        existing = self.list_all_references()
        existing_names = {ref.get("name"): ref for ref in existing}
        attached = []

        # 1. GitHub Repository Reference
        gh_ref = existing_names.get("taskflow-repo")
        if not gh_ref:
            gh_res = self.create_reference(
                ref_type="github",
                name="taskflow-repo",
                description="TaskFlow core git repository and commit history",
                instructions="Consult for commit logs, pull requests, and code changes",
                urls=[github_url],
            )
            if gh_res and "id" in gh_res:
                gh_ref = gh_res

        if gh_ref and "id" in gh_ref:
            self.attach_reference_to_room(gh_ref["id"], target_room)
            attached.append(gh_ref)

        # 2. Issue / Ticket Tracker Reference
        jira_ref = existing_names.get("taskflow-tickets")
        if not jira_ref:
            jira_res = self.create_reference(
                ref_type="jira",
                name="taskflow-tickets",
                description="TaskFlow backlog, PRs, and issue tracking board",
                instructions="Consult for issue specifications, requirements, and ticket details",
                urls=[f"{github_url}/issues"],
            )
            if jira_res and "id" in jira_res:
                jira_ref = jira_res

        if jira_ref and "id" in jira_ref:
            self.attach_reference_to_room(jira_ref["id"], target_room)
            attached.append(jira_ref)

        return attached

    def get_user_roles(
        self,
        user_id: Optional[str] = None,
        user_name: Optional[str] = None,
        room_id: Optional[str] = None,
    ) -> List[str]:
        """Resolves active room roles held by a user in the Switch room.
        
        Checks:
        1. Role definitions and live leases in the room (`held_by`).
        2. Room creator / owner status via `get_room_detail`.
        """
        target_room = room_id or self.room_id
        roles: List[str] = []
        clean_user = (user_name or "").strip().lower().lstrip("@")
        clean_id = (user_id or "").strip().lower()

        if not target_room or not self.agent_id:
            return roles

        try:
            resp = requests.post(
                f"{self.server_url}/agents/{self.agent_id}/ops/get_room_detail",
                headers=self.auth_headers,
                json={"room_id": target_room},
                timeout=5,
            )
            if resp.status_code == 200:
                result = resp.json().get("result", {})
                room_roles = result.get("roles", [])
                for r in room_roles:
                    r_name = r.get("name", "").lower()
                    for holder in r.get("held_by", []):
                        holder_name = (holder.get("name") or "").strip().lower().lstrip("@")
                        if holder_name and holder_name in (clean_user, clean_id):
                            if r_name not in roles:
                                roles.append(r_name)

                creator = (result.get("creator_name") or result.get("owner_name") or "").strip().lower().lstrip("@")
                if creator and creator in (clean_user, clean_id):
                    for admin_role in ("owner", "admin", "operator"):
                        if admin_role not in roles:
                            roles.append(admin_role)
        except Exception as e:
            logger.debug("Failed querying room roles for %s: %s", user_name or user_id, e)

        return roles

    def list_participants(self, room_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Lists participants (connected users, role holders, and agents) in the Switch room.
        Attempts ops/list_participants, falling back to get_room_detail participant extraction.
        """
        target_room = room_id or self.room_id
        if not target_room or not self.agent_id:
            return []

        participants: List[Dict[str, Any]] = []
        seen_names = set()

        # 1. Try Switch ops/list_participants
        try:
            resp = requests.post(
                f"{self.server_url}/agents/{self.agent_id}/ops/list_participants",
                headers=self.auth_headers,
                json={},
                timeout=3,
            )
            if resp.status_code == 200:
                result = resp.json().get("result", [])
                if isinstance(result, list):
                    for p in result:
                        p_name = p.get("name") or p.get("username") or p.get("display_name")
                        if p_name and p_name.lower() not in seen_names:
                            seen_names.add(p_name.lower())
                            participants.append({
                                "id": p.get("id") or p.get("user_id"),
                                "name": p_name,
                                "kind": p.get("kind", "user"),
                                "roles": p.get("roles", []),
                            })
        except Exception as e:
            logger.debug("ops/list_participants call skipped/failed: %s", e)

        # 2. Extract participants from get_room_detail
        try:
            resp = requests.post(
                f"{self.server_url}/agents/{self.agent_id}/ops/get_room_detail",
                headers=self.auth_headers,
                json={"room_id": target_room},
                timeout=5,
            )
            if resp.status_code == 200:
                res = resp.json().get("result", {})
                for u_name in res.get("connected_user_names", []):
                    if u_name and u_name.lower() not in seen_names:
                        seen_names.add(u_name.lower())
                        participants.append({
                            "id": None,
                            "name": u_name,
                            "kind": "user",
                            "roles": [],
                        })
                for r in res.get("roles", []):
                    r_name = r.get("name", "")
                    for holder in r.get("held_by", []):
                        h_name = holder.get("name")
                        h_id = holder.get("id")
                        if h_name and h_name.lower() not in seen_names:
                            seen_names.add(h_name.lower())
                            participants.append({
                                "id": h_id,
                                "name": h_name,
                                "kind": "user",
                                "roles": [r_name],
                            })
                creator = res.get("creator_name") or res.get("owner_name")
                if creator and creator.lower() not in seen_names:
                    seen_names.add(creator.lower())
                    participants.append({
                        "id": None,
                        "name": creator,
                        "kind": "user",
                        "roles": ["owner"],
                    })
        except Exception as e:
            logger.debug("Failed extracting participants from room detail: %s", e)

        return participants

    def get_room_bridge_info(self, room_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Inspects Switch room metadata to determine if an external collaboration
        bridge (such as Slack or Mattermost) is connected to this room.
        
        Switch owns the Slack connection; TaskFlow never runs a parallel Bolt app.
        """
        target_room = room_id or self.room_id
        if not target_room or not self.agent_id:
            return {
                "connected": False,
                "is_slack": False,
                "bridge_id": None,
                "bridge_name": None,
                "external_channel_id": None,
            }

        try:
            resp = requests.post(
                f"{self.server_url}/agents/{self.agent_id}/ops/get_room_detail",
                headers=self.auth_headers,
                json={"room_id": target_room},
                timeout=5,
            )
            if resp.status_code == 200:
                result = resp.json().get("result", {})
                bridge_id = result.get("bridge_id")
                bridge_name = result.get("bridge_display_name")
                channel_id = result.get("external_channel_id")
                is_slack = bool(bridge_name and "slack" in bridge_name.lower())
                return {
                    "connected": bool(bridge_id or bridge_name),
                    "is_slack": is_slack,
                    "bridge_id": bridge_id,
                    "bridge_name": bridge_name,
                    "external_channel_id": channel_id,
                }
        except Exception as e:
            logger.debug("Failed querying room bridge info for %s: %s", target_room, e)

        return {
            "connected": False,
            "is_slack": False,
            "bridge_id": None,
            "bridge_name": None,
            "external_channel_id": None,
        }

    def is_slack_connected(self, room_id: Optional[str] = None) -> bool:
        """Returns True if Switch has an active Slack collaboration bridge connected to this room."""
        return self.get_room_bridge_info(room_id).get("is_slack", False)

    def create_room_document(
        self,
        title: Optional[str] = None,
        content: str = "",
        name: Optional[str] = None,
        description: Optional[str] = None,
        instructions: Optional[str] = None,
        room_id: Optional[str] = None,
        doc_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Creates an internal document scoped to the connected room on Switch.
        
        Attempts live Switch Core endpoint (POST /agents/{agent_id}/ops/create_room_document),
        with transparent local fallback to RoomDocumentStore so drafts are never lost.
        """
        target_room = room_id or self.room_id or "default_room"
        doc_title = title or name or "Draft Deliverable"
        slug = re.sub(r'[^a-zA-Z0-9_\-]', '-', doc_title.lower()).strip('-')[:35]
        doc_name = name or (f"{slug}-{uuid.uuid4().hex[:6]}" if slug else f"doc-{uuid.uuid4().hex[:8]}")
        doc_desc = description or f"Draft deliverable: {doc_title}"
        doc_inst = instructions or "TaskFlow autonomous deliverable draft awaiting room review."

        server_doc_id = None
        if self.agent_id:
            payload = {
                "name": doc_name,
                "description": doc_desc,
                "instructions": doc_inst,
                "content": content,
            }
            headers = self.op_headers.copy()
            if target_room:
                headers["X-Switch-Room-Id"] = target_room
            try:
                resp = requests.post(
                    f"{self.server_url}/agents/{self.agent_id}/ops/create_room_document",
                    headers=headers,
                    json=payload,
                    timeout=10,
                )
                if resp.status_code == 200:
                    res = resp.json().get("result", {})
                    server_doc_id = res.get("document_id") or resp.json().get("document_id")
                    logger.info("Created Switch room document on server: %s", server_doc_id)
                else:
                    logger.debug("Server create_room_document returned %s: %s", resp.status_code, resp.text)
            except Exception as e:
                logger.debug("Server create_room_document call failed/skipped: %s", e)

        assigned_id = server_doc_id or doc_id or f"doc_{uuid.uuid4().hex[:12]}"
        meta = dict(metadata or {})
        if server_doc_id:
            meta["server_synced"] = True

        status_str = meta.get("status", "DRAFT")
        doc = self.document_store.create_document(
            title=doc_title,
            content=content,
            name=doc_name,
            description=doc_desc,
            instructions=doc_inst,
            room_id=target_room,
            doc_id=assigned_id,
            status=status_str,
            created_by=self.agent_name,
            metadata=meta,
        )
        return doc.to_dict()

    def update_room_document(
        self,
        doc_id: Optional[str] = None,
        document_id: Optional[str] = None,
        content: Optional[str] = None,
        title: Optional[str] = None,
        name: Optional[str] = None,
        description: Optional[str] = None,
        instructions: Optional[str] = None,
        room_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Updates a room-scoped document on Switch Core and local persistent store."""
        target_id = document_id or doc_id
        if not target_id:
            logger.warning("update_room_document called without doc_id or document_id")
            return {}

        target_room = room_id or self.room_id

        # Attempt server update if agent registered
        if self.agent_id:
            payload: Dict[str, Any] = {"document_id": target_id}
            if name is not None:
                payload["name"] = name
            if description is not None:
                payload["description"] = description
            if instructions is not None:
                payload["instructions"] = instructions
            if content is not None:
                payload["content"] = content

            headers = self.op_headers.copy()
            if target_room:
                headers["X-Switch-Room-Id"] = target_room
            try:
                resp = requests.post(
                    f"{self.server_url}/agents/{self.agent_id}/ops/update_room_document",
                    headers=headers,
                    json=payload,
                    timeout=10,
                )
                if resp.status_code == 200:
                    logger.info("Updated Switch room document on server: %s", target_id)
            except Exception as e:
                logger.debug("Server update_room_document call failed/skipped: %s", e)

        status_val = (metadata or {}).get("status")
        updated_by = (
            (metadata or {}).get("approved_by")
            or (metadata or {}).get("saved_by")
            or (metadata or {}).get("dismissed_by")
            or (metadata or {}).get("updated_by")
            or self.agent_name
        )
        doc = self.document_store.update_document(
            doc_id=target_id,
            content=content,
            title=title,
            name=name,
            description=description,
            instructions=instructions,
            status=status_val,
            updated_by=updated_by,
            metadata=metadata,
        )
        if doc:
            return doc.to_dict()

        return {"document_id": target_id, "doc_id": target_id, "status": status_val or "UPDATED"}

    def get_room_document(
        self,
        doc_id: Optional[str] = None,
        document_id: Optional[str] = None,
        room_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Retrieves a room document by its ID from local store or Switch Core load_internal_documents."""
        target_id = document_id or doc_id
        if not target_id:
            return None

        # Check local store first
        local_doc = self.document_store.get_document(target_id, room_id=room_id)
        if local_doc:
            return local_doc.to_dict()

        # Try server load_internal_documents
        if self.agent_id:
            headers = self.op_headers.copy()
            if room_id or self.room_id:
                headers["X-Switch-Room-Id"] = room_id or self.room_id
            try:
                resp = requests.post(
                    f"{self.server_url}/agents/{self.agent_id}/ops/load_internal_documents",
                    headers=headers,
                    json={"ids": [target_id]},
                    timeout=5,
                )
                if resp.status_code == 200:
                    docs = resp.json().get("result", [])
                    if docs and isinstance(docs, list):
                        d = docs[0]
                        return {
                            "document_id": d.get("id") or target_id,
                            "doc_id": d.get("id") or target_id,
                            "name": d.get("name", ""),
                            "title": d.get("name", ""),
                            "description": d.get("description", ""),
                            "content": d.get("content", ""),
                            "status": "PUBLISHED",
                        }
            except Exception as e:
                logger.debug("Server load_internal_documents failed: %s", e)

        return None

    def list_room_documents(
        self,
        room_id: Optional[str] = None,
        status: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Lists room documents matching the specified room and/or status."""
        target_room = room_id or self.room_id
        docs = self.document_store.list_documents(room_id=target_room, status=status)
        return [d.to_dict() for d in docs]

    def delete_room_document(
        self,
        doc_id: Optional[str] = None,
        document_id: Optional[str] = None,
        room_id: Optional[str] = None,
    ) -> bool:
        """Deletes a room document on Switch Core and local persistent store."""
        target_id = document_id or doc_id
        if not target_id:
            return False

        if self.agent_id:
            headers = self.op_headers.copy()
            if room_id or self.room_id:
                headers["X-Switch-Room-Id"] = room_id or self.room_id
            try:
                resp = requests.post(
                    f"{self.server_url}/agents/{self.agent_id}/ops/delete_room_document",
                    headers=headers,
                    json={"document_id": target_id},
                    timeout=5,
                )
                if resp.status_code == 200:
                    logger.info("Deleted room document on server: %s", target_id)
            except Exception as e:
                logger.debug("Server delete_room_document failed: %s", e)

        return self.document_store.delete_document(target_id)
