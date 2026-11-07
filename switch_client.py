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
