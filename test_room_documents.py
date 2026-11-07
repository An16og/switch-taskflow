"""Unit tests for Switch Room Document Persistence (create_room_document / update_room_document).

Verifies:
1. Native RoomDocument and RoomDocumentStore storage, serialization, versioning, and listing.
2. SwitchAgentClient create_room_document, update_room_document, get_room_document, and list_room_documents.
3. Resilient fallback to RoomDocumentStore when Switch Core endpoint is offline.
4. End-to-end draft lifecycle in bot.py:
   - Worker AUTO_EXECUTE persists deliverable as a Switch room document (DRAFT).
   - Proposal card surfaces Switch Document ID to the room.
   - @taskflow-worker approve updates room document status to PUBLISHED.
   - @taskflow-worker dismiss updates room document status to DISMISSED.
"""

import os
import json
import uuid
import time
import asyncio
import unittest
from unittest.mock import MagicMock, patch
import websockets

from room_documents import (
    RoomDocument,
    RoomDocumentStore,
    DocumentStatus,
    default_room_document_store,
)
from switch_client import SwitchAgentClient
from approvals import PendingApprovalStore, default_approval_store, ApprovalStatus
from interactions import InteractionDispatcher
from models import (
    ConfidenceLevel,
    ExecutionType,
    TaskHandoffMessage,
    WorkerExecutionResult,
)
from bot import handle_worker_message


class TestRoomDocuments(unittest.TestCase):

    def setUp(self):
        self.test_store_path = f"test_room_documents_{uuid.uuid4().hex[:8]}.json"
        self.doc_store = RoomDocumentStore(storage_path=self.test_store_path)

    def tearDown(self):
        if os.path.exists(self.test_store_path):
            os.remove(self.test_store_path)
        tmp = f"{self.test_store_path}.tmp"
        if os.path.exists(tmp):
            os.remove(tmp)

    def test_room_document_model_and_store(self):
        """Tests core RoomDocument lifecycle in RoomDocumentStore."""
        doc = self.doc_store.create_document(
            title="Q3 Security Architecture Spec",
            content="# Q3 Security Spec\n- Zero trust network\n- mTLS",
            name="q3-sec-spec",
            description="High-level security spec for Q3",
            room_id="room_sec_101",
            status="DRAFT",
            created_by="taskflow-worker",
            metadata={"ticket": "SEC-402"},
        )

        self.assertIsNotNone(doc.doc_id)
        self.assertEqual(doc.document_id, doc.doc_id)
        self.assertEqual(doc.title, "Q3 Security Architecture Spec")
        self.assertEqual(doc.status, DocumentStatus.DRAFT)
        self.assertEqual(doc.version, 1)

        # Verify persistence and retrieval
        retrieved = self.doc_store.get_document(doc.doc_id)
        self.assertIsNotNone(retrieved)
        self.assertEqual(retrieved.content, doc.content)
        self.assertEqual(retrieved.metadata.get("ticket"), "SEC-402")

        # Test Update
        updated = self.doc_store.update_document(
            doc_id=doc.doc_id,
            content="# Q3 Security Spec (Approved)\n- Zero trust network\n- mTLS\n- Audit logs",
            status="PUBLISHED",
            updated_by="Alice",
            metadata={"approved_by": "Alice"},
        )
        self.assertIsNotNone(updated)
        self.assertEqual(updated.status, DocumentStatus.PUBLISHED)
        self.assertEqual(updated.version, 2)
        self.assertEqual(updated.updated_by, "Alice")
        self.assertIn("Audit logs", updated.content)

        # Test Listing by room and status
        drafts = self.doc_store.list_documents(room_id="room_sec_101", status="DRAFT")
        self.assertEqual(len(drafts), 0)
        published = self.doc_store.list_documents(room_id="room_sec_101", status="PUBLISHED")
        self.assertEqual(len(published), 1)

        # Test Serialization
        doc_dict = updated.to_dict()
        self.assertEqual(doc_dict["document_id"], doc.doc_id)
        self.assertEqual(doc_dict["status"], "PUBLISHED")

        reconstructed = RoomDocument.from_dict(doc_dict)
        self.assertEqual(reconstructed.doc_id, doc.doc_id)
        self.assertEqual(reconstructed.title, updated.title)

        # Test Delete
        self.assertTrue(self.doc_store.delete_document(doc.doc_id))
        self.assertIsNone(self.doc_store.get_document(doc.doc_id))

    def test_switch_agent_client_document_ops(self):
        """Tests SwitchAgentClient create, update, get, list, and delete room document operations."""
        client = SwitchAgentClient(
            server_url="http://127.0.0.1:52422",
            agent_id="test_agent_1",
            api_key="test_key_1",
            room_id="test_room_docs",
            agent_name="taskflow-worker",
        )
        client.document_store = self.doc_store

        # 1. Create Room Document
        created = client.create_room_document(
            title="Sprint 44 Release Notes",
            content="# Sprint 44 Release Notes\n- Fixed login bug\n- New dashboard",
            room_id="test_room_docs",
            metadata={"status": "DRAFT", "author": "WorkerAgent"}
        )

        doc_id = created.get("doc_id") or created.get("document_id")
        self.assertIsNotNone(doc_id)
        self.assertEqual(created.get("title"), "Sprint 44 Release Notes")
        self.assertEqual(created.get("status"), "DRAFT")

        # 2. Get Room Document
        fetched = client.get_room_document(doc_id=doc_id, room_id="test_room_docs")
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched["doc_id"], doc_id)
        self.assertIn("Fixed login bug", fetched["content"])

        # 3. Update Room Document
        updated = client.update_room_document(
            doc_id=doc_id,
            description="Sprint 44 Release Notes (Published to Workroom)",
            metadata={"status": "PUBLISHED", "approved_by": "LeadEngineer"}
        )
        self.assertEqual(updated.get("status"), "PUBLISHED")
        self.assertEqual(updated.get("description"), "Sprint 44 Release Notes (Published to Workroom)")

        # 4. List Room Documents
        all_docs = client.list_room_documents(room_id="test_room_docs")
        self.assertEqual(len(all_docs), 1)
        self.assertEqual(all_docs[0]["doc_id"], doc_id)

        published_docs = client.list_room_documents(room_id="test_room_docs", status="PUBLISHED")
        self.assertEqual(len(published_docs), 1)

        draft_docs = client.list_room_documents(room_id="test_room_docs", status="DRAFT")
        self.assertEqual(len(draft_docs), 0)

        # 5. Delete Room Document
        deleted = client.delete_room_document(doc_id=doc_id, room_id="test_room_docs")
        self.assertTrue(deleted)
        self.assertIsNone(client.get_room_document(doc_id=doc_id))

    def test_switch_agent_client_server_integration_and_fallback(self):
        """Verifies SwitchAgentClient interacts with server ops and falls back gracefully on error."""
        client = SwitchAgentClient(
            server_url="http://127.0.0.1:52422",
            agent_id="test_agent_server",
            api_key="test_key_server",
            room_id="room_server_test",
        )
        client.document_store = self.doc_store

        # Case 1: Server endpoint returns 200 OK with server-assigned UUID
        server_uuid = str(uuid.uuid4())
        mock_resp_200 = MagicMock()
        mock_resp_200.status_code = 200
        mock_resp_200.json.return_value = {"result": {"document_id": server_uuid}}

        with patch("requests.post", return_value=mock_resp_200):
            created = client.create_room_document(
                title="Server-Synced Architecture Doc",
                content="# Architecture\nServer synced content",
                room_id="room_server_test",
            )
            self.assertEqual(created["doc_id"], server_uuid)
            self.assertEqual(created["document_id"], server_uuid)
            self.assertTrue(created["metadata"].get("server_synced"))

        # Case 2: Server endpoint raises ConnectionError / returns 500
        mock_resp_500 = MagicMock()
        mock_resp_500.status_code = 500
        mock_resp_500.text = "Internal Server Error"

        with patch("requests.post", return_value=mock_resp_500):
            fallback_created = client.create_room_document(
                title="Offline Fallback Document",
                content="# Fallback Content",
                room_id="room_server_test",
            )
            self.assertIsNotNone(fallback_created["doc_id"])
            self.assertFalse(fallback_created["metadata"].get("server_synced", False))
            self.assertEqual(fallback_created["title"], "Offline Fallback Document")

            # Document is still reliably saved in store
            stored_doc = self.doc_store.get_document(fallback_created["doc_id"])
            self.assertIsNotNone(stored_doc)

    def test_auto_execute_persists_draft_and_surfaces_doc_id(self):
        """Verifies that WorkerAgent AUTO_EXECUTE creates a Switch room document draft and includes it in proposal."""
        client = SwitchAgentClient(
            server_url="http://127.0.0.1:52422",
            agent_id="test_worker_agent",
            api_key="test_worker_key",
            room_id="room_docs_pipeline",
            agent_name="taskflow-worker",
        )
        client.document_store = self.doc_store
        client.post_message = MagicMock()

        handoff = TaskHandoffMessage(
            task_title="Draft Release Notes v2.1.0",
            execution_type=ExecutionType.AUTO_EXECUTE,
            confidence=ConfidenceLevel.HIGH,
            assignee_name="Alice",
            assignee_id="U_ALICE",
            assigner_name="Priya",
            assigner_id="U_PRIYA",
            room_id="room_docs_pipeline",
            thread_id="msg_draft_1",
            requester_name="Priya",
        )

        payload = {
            "body": f"@taskflow-worker <!-- TASKFLOW_HANDOFF: {handoff.model_dump_json()} -->",
            "sender": "agent_triage_1",
            "sender_name": "taskflow-triage",
            "room_id": "room_docs_pipeline",
            "thread_id": "msg_draft_1",
        }

        with patch("bot.worker_executor.execute_task") as mock_exec:
            mock_exec.return_value = WorkerExecutionResult(
                artifact_title="Release Notes v2.1.0",
                artifact_content="# Release Notes v2.1.0\n- Feature X\n- Bugfix Y",
                execution_notes="Generated release notes"
            )

            asyncio.run(handle_worker_message(client, payload))

            # 1. Verify a room document was created in the store
            docs = self.doc_store.list_documents(room_id="room_docs_pipeline")
            self.assertEqual(len(docs), 1)
            draft_doc = docs[0]
            self.assertEqual(draft_doc.title, "Release Notes v2.1.0")
            self.assertEqual(draft_doc.status, DocumentStatus.DRAFT)
            self.assertIn("Feature X", draft_doc.content)

            # 2. Verify proposal card posted to room includes the Room Document ID
            client.post_message.assert_called_once()
            call_kwargs = client.post_message.call_args[1]
            proposal_body = call_kwargs["body"]

            self.assertIn("TaskFlow Deliverable Drafted (Pending Approval)", proposal_body)
            self.assertIn("Room Document:", proposal_body)
            self.assertIn(draft_doc.doc_id, proposal_body)
            self.assertIn("Draft saved to Switch Workroom", proposal_body)

    def test_approve_command_updates_room_document_to_published(self):
        """Verifies that @taskflow-worker approve updates the room document to PUBLISHED."""
        client = SwitchAgentClient(
            server_url="http://127.0.0.1:52422",
            agent_id="test_worker_agent",
            api_key="test_worker_key",
            room_id="room_docs_publish",
            agent_name="taskflow-worker",
        )
        client.document_store = self.doc_store
        client.post_message = MagicMock()

        # 1. Create a draft room document
        doc = self.doc_store.create_document(
            title="Q4 Postmortem Report",
            content="# Outage Postmortem\nRedis master failover completed in 42s.",
            room_id="room_docs_publish",
            status="DRAFT",
        )

        # 2. Create pending approval referencing the switch_doc_id
        appr = default_approval_store.create_approval(
            action_summary="Q4 Postmortem Report",
            artifact_content=doc.content,
            artifact_title=doc.title,
            action_type="AUTO_EXECUTE",
            assignee_name="Alice",
            assignee_id="U_ALICE",
            assigner_name="Priya",
            assigner_id="U_PRIYA",
            room_id="room_docs_publish",
            switch_doc_id=doc.doc_id,
        )

        # 3. Simulate Alice running `@taskflow-worker approve <appr_id>`
        cmd_payload = {
            "body": f"@taskflow-worker approve {appr.approval_id}",
            "sender": "U_ALICE",
            "sender_name": "Alice",
            "room_id": "room_docs_publish",
            "thread_id": "msg_cmd_1",
        }

        asyncio.run(handle_worker_message(client, cmd_payload))

        # 4. Verify room document status transitioned to PUBLISHED
        updated_doc = self.doc_store.get_document(doc.doc_id)
        self.assertIsNotNone(updated_doc)
        self.assertEqual(updated_doc.status, DocumentStatus.PUBLISHED)
        self.assertEqual(updated_doc.updated_by, "Alice")

        # 5. Verify room broadcast contains Room Document ID with [Published]
        client.post_message.assert_called()
        broadcast_call = client.post_message.call_args_list[0][1]
        broadcast_body = broadcast_call["body"]
        self.assertIn("Room Document:", broadcast_body)
        self.assertIn(doc.doc_id, broadcast_body)
        self.assertIn("[Published]", broadcast_body)

    def test_dismiss_command_updates_room_document_to_dismissed(self):
        """Verifies that @taskflow-worker dismiss updates the room document to DISMISSED."""
        client = SwitchAgentClient(
            server_url="http://127.0.0.1:52422",
            agent_id="test_worker_agent",
            api_key="test_worker_key",
            room_id="room_docs_dismiss",
            agent_name="taskflow-worker",
        )
        client.document_store = self.doc_store
        client.post_message = MagicMock()

        # 1. Create a draft room document
        doc = self.doc_store.create_document(
            title="Unneeded Internal RFC",
            content="# RFC: Deprecate v1 endpoints",
            room_id="room_docs_dismiss",
            status="DRAFT",
        )

        # 2. Create pending approval referencing the switch_doc_id
        appr = default_approval_store.create_approval(
            action_summary="Unneeded Internal RFC",
            artifact_content=doc.content,
            artifact_title=doc.title,
            action_type="AUTO_EXECUTE",
            assignee_name="Alice",
            assignee_id="U_ALICE",
            assigner_name="Priya",
            assigner_id="U_PRIYA",
            room_id="room_docs_dismiss",
            switch_doc_id=doc.doc_id,
        )

        # 3. Simulate Priya running `@taskflow-worker dismiss <appr_id>`
        cmd_payload = {
            "body": f"@taskflow-worker dismiss {appr.approval_id}",
            "sender": "U_PRIYA",
            "sender_name": "Priya",
            "room_id": "room_docs_dismiss",
            "thread_id": "msg_cmd_2",
        }

        asyncio.run(handle_worker_message(client, cmd_payload))

        # 4. Verify room document status transitioned to DISMISSED
        updated_doc = self.doc_store.get_document(doc.doc_id)
        self.assertIsNotNone(updated_doc)
        self.assertEqual(updated_doc.status, DocumentStatus.DISMISSED)
        self.assertEqual(updated_doc.updated_by, "Priya")

        # 5. Verify dismissal notification surfaces Room Document ID
        client.post_message.assert_called()
        dismiss_call = client.post_message.call_args_list[0][1]
        dismiss_body = dismiss_call["body"]
        self.assertIn("TaskFlow Deliverable Dismissed", dismiss_body)
        self.assertIn("Room Document:", dismiss_body)
        self.assertIn(doc.doc_id, dismiss_body)
        self.assertIn("[Dismissed]", dismiss_body)

    def test_apply_room_briefing_calls_update_room_instructions(self):
        """Verifies apply_room_briefing loads ROOM_BRIEFING.md and calls update_room_instructions."""
        client = SwitchAgentClient(
            server_url="http://127.0.0.1:52422",
            agent_id="test_worker_briefing",
            api_key="test_key",
            room_id="room_briefing_test",
        )

        mock_update = MagicMock(return_value=True)
        client.update_room_instructions = mock_update

        success = client.apply_room_briefing(room_id="room_briefing_test")
        self.assertTrue(success)
        mock_update.assert_called_once()

        call_args = mock_update.call_args
        instructions = call_args[0][0] if call_args[0] else call_args[1].get("instructions")
        target_room = call_args[1].get("room_id") if "room_id" in call_args[1] else (call_args[0][1] if len(call_args[0]) > 1 else None)

        self.assertIn("TaskFlow Room Briefing", instructions)
        self.assertIn("Dual-Agent Workroom Pair", instructions)
        self.assertEqual(target_room, "room_briefing_test")

    def test_connect_and_listen_applies_briefing_on_connect(self):
        """Verifies connect_and_listen invokes apply_room_briefing on connect."""
        client = SwitchAgentClient(
            server_url="http://127.0.0.1:52422",
            agent_id="test_worker_briefing_connect",
            api_key="test_key",
            room_id="room_connect_test",
        )
        client.register_or_validate = MagicMock(return_value=True)
        client.ensure_room = MagicMock(return_value="room_connect_test")
        client.apply_room_briefing = MagicMock(return_value=True)

        class MockWs:
            async def __aenter__(self):
                return self
            async def __aexit__(self, exc_type, exc_val, exc_tb):
                pass
            async def recv(self):
                client.stop()
                return json.dumps({"event": "connection_state", "rooms": ["room_connect_test"]})
            async def send(self, data):
                pass
            async def close(self):
                pass

        def mock_ws_connect(*args, **kwargs):
            return MockWs()

        with patch("websockets.connect", side_effect=mock_ws_connect):
            asyncio.run(client.connect_and_listen(apply_briefing=True))

            client.apply_room_briefing.assert_called()

    def test_register_or_validate_defaults_to_overwrite_false(self):
        """Verifies register_or_validate sends overwrite=False by default to avoid rotating keys."""
        client = SwitchAgentClient(
            server_url="http://127.0.0.1:52422",
            registration_token="test_token_123",
            agent_name="taskflow-test",
        )

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"id": "agent_new_id", "api_key": "api_new_key"}

        with patch("requests.post", return_value=mock_resp) as mock_post:
            res = client.register_or_validate()
            self.assertTrue(res)
            mock_post.assert_called_once()
            payload = mock_post.call_args[1]["json"]
            self.assertFalse(payload["overwrite"])
            self.assertEqual(client.agent_id, "agent_new_id")
            self.assertEqual(client.api_key, "api_new_key")

    def test_register_or_validate_skips_registration_when_valid(self):
        """Verifies register_or_validate validates existing credentials and avoids calling POST /agents."""
        client = SwitchAgentClient(
            server_url="http://127.0.0.1:52422",
            agent_id="existing_agent_id",
            api_key="existing_key",
            registration_token="test_token_123",
        )

        mock_get_resp = MagicMock()
        mock_get_resp.status_code = 200

        with patch("requests.get", return_value=mock_get_resp) as mock_get, \
             patch("requests.post") as mock_post:
            res = client.register_or_validate()
            self.assertTrue(res)
            mock_get.assert_called_once()
            mock_post.assert_not_called()

    def test_register_or_validate_handles_409_conflict_safely(self):
        """Verifies register_or_validate raises RuntimeError on 409 conflict when overwrite=False."""
        client = SwitchAgentClient(
            server_url="http://127.0.0.1:52422",
            registration_token="test_token_123",
            agent_name="taskflow-conflict",
        )

        mock_resp = MagicMock()
        mock_resp.status_code = 409
        mock_resp.text = '{"detail":"Agent already exists"}'

        with patch("requests.post", return_value=mock_resp):
            with self.assertRaises(RuntimeError) as ctx:
                client.register_or_validate(overwrite=False)
            self.assertIn("overwrite=False", str(ctx.exception))
            self.assertIn("protect active credentials", str(ctx.exception))

    def test_pong_includes_cursor_and_generation(self):
        """Verifies that WebSocket ping response returns pong containing cursor and generation."""
        stable_id = str(uuid.uuid4())
        client = SwitchAgentClient(
            server_url="http://127.0.0.1:52422",
            agent_id="test_worker_pong",
            api_key="test_key",
            room_id="room_pong_test",
            connection_id=stable_id,
        )
        client.register_or_validate = MagicMock(return_value=True)
        client.ensure_room = MagicMock(return_value="room_pong_test")
        client.apply_room_briefing = MagicMock(return_value=True)

        sent_messages = []

        class MockWs:
            def __init__(self):
                self.messages = [
                    json.dumps({
                        "event": "connection_state",
                        "data": {
                            "connection_id": stable_id,
                            "generation": 1024,
                            "cursor": 15,
                            "rooms": ["room_pong_test"]
                        }
                    }),
                    json.dumps({"event": "ping", "data": {}}),
                ]
                self.idx = 0

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc_val, exc_tb):
                pass

            async def recv(self):
                if self.idx < len(self.messages):
                    msg = self.messages[self.idx]
                    self.idx += 1
                    return msg
                client.stop()
                raise websockets.exceptions.ConnectionClosed(None, None)

            async def send(self, data):
                sent_messages.append(json.loads(data))

            async def close(self):
                pass

        with patch("websockets.connect", return_value=MockWs()):
            asyncio.run(client.connect_and_listen(apply_briefing=False, auto_reconnect=False))

        # Verify state
        self.assertEqual(client.connection_id, stable_id)
        self.assertEqual(client.generation, 1024)
        self.assertEqual(client.cursor, 15)

        # Verify pong frame
        pongs = [m for m in sent_messages if m.get("type") == "pong"]
        self.assertEqual(len(pongs), 1)
        pong = pongs[0]
        self.assertEqual(pong["cursor"], 15)
        self.assertEqual(pong["generation"], 1024)

    def test_reconnect_preserves_stable_connection_id_and_expected_generation(self):
        """Verifies reconnect preserves connection_id and passes expected_generation and start_from."""
        stable_id = "stable-conn-id-999"
        client = SwitchAgentClient(
            server_url="http://127.0.0.1:52422",
            agent_id="test_worker_reconnect",
            api_key="test_key",
            room_id="room_recon_test",
            connection_id=stable_id,
        )
        client.register_or_validate = MagicMock(return_value=True)
        client.ensure_room = MagicMock(return_value="room_recon_test")
        client.apply_room_briefing = MagicMock(return_value=True)

        connected_urls = []

        class MockWsSession:
            def __init__(self, session_num):
                self.session_num = session_num
                self.sent_state = False

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc_val, exc_tb):
                pass

            async def recv(self):
                if not self.sent_state:
                    self.sent_state = True
                    return json.dumps({
                        "event": "connection_state",
                        "data": {
                            "connection_id": stable_id,
                            "generation": 5000 + self.session_num,
                            "cursor": 20 * self.session_num,
                            "rooms": ["room_recon_test"]
                        }
                    })
                # After sending connection_state, close first socket to force reconnect; stop on second
                if self.session_num == 1:
                    raise websockets.exceptions.ConnectionClosed(None, None)
                else:
                    client.stop()
                    raise websockets.exceptions.ConnectionClosed(None, None)

            async def send(self, data):
                pass

            async def close(self):
                pass

        session_counter = [0]
        def mock_ws_connect(url, *args, **kwargs):
            connected_urls.append(url)
            session_counter[0] += 1
            return MockWsSession(session_counter[0])

        with patch("websockets.connect", side_effect=mock_ws_connect):
            asyncio.run(client.connect_and_listen(apply_briefing=False, auto_reconnect=True, reconnect_delay=0.01))

        self.assertEqual(len(connected_urls), 2)
        # First connect: initial connection_id
        self.assertIn(f"connection_id={stable_id}", connected_urls[0])
        self.assertNotIn("expected_generation=", connected_urls[0])

        # Second connect (reconnect): stable connection_id preserved + expected_generation + start_from
        self.assertIn(f"connection_id={stable_id}", connected_urls[1])
        self.assertIn("expected_generation=5001", connected_urls[1])
        self.assertIn("start_from=20", connected_urls[1])

    def test_template_yaml_validity_and_custom_protocol_agent_binding(self):
        """Verifies templates/triage-pair.template.yaml binds custom protocol agents without spawning Claude/Codex."""
        import yaml
        from pathlib import Path
        
        template_path = Path(__file__).parent / "templates" / "triage-pair.template.yaml"
        self.assertTrue(template_path.exists(), "Template file must exist")
        
        content = template_path.read_text(encoding="utf-8")
        data = yaml.safe_load(content)
        
        self.assertEqual(data.get("version"), 2)
        resources = data.get("resources", {})
        self.assertIn("triage_room", resources)
        
        room = resources["triage_room"]
        self.assertEqual(room.get("type"), "room")
        self.assertIn("taskflow-triage", room.get("agents", []))
        self.assertIn("taskflow-worker", room.get("agents", []))
        
        # Verify it does NOT define managed agent resources that force Claude/Codex CLI providers
        for key, res in resources.items():
            if res.get("type") == "agent":
                provider = res.get("provider", "")
                self.assertNotIn("claude", str(provider).lower())
                self.assertNotIn("codex", str(provider).lower())
        
        # Verify instructions mention running bot.py
        self.assertIn("python bot.py", content)

    def test_process_health_log_metrics(self):
        """Verifies log_process_health returns structured metrics and emits process health log line."""
        client = SwitchAgentClient(
            server_url="http://127.0.0.1:52422",
            agent_id="test_health_agent",
            agent_name="taskflow-worker",
            room_id="room_health_test",
            connection_id="conn-12345",
        )
        client.cursor = 42
        client.generation = 999
        client._connected_at = 1000.0
        client._pings_received = 5
        client._pongs_sent = 5
        client._messages_processed = 12

        with patch("time.time", return_value=1050.0):
            with self.assertLogs("SwitchClient", level="INFO") as log_capture:
                health = client.log_process_health()

        self.assertEqual(health["agent_name"], "taskflow-worker")
        self.assertEqual(health["cursor"], 42)
        self.assertEqual(health["generation"], 999)
        self.assertEqual(health["uptime_seconds"], 50.0)
        self.assertEqual(health["pongs_sent"], 5)
        self.assertEqual(health["messages_processed"], 12)
        self.assertTrue(any("[Process Health]" in record for record in log_capture.output))

    def test_graceful_ws_close_sends_code_and_reason(self):
        """Verifies graceful WS close invokes ws.close with code 1000 and custom reason."""
        client = SwitchAgentClient(
            server_url="http://127.0.0.1:52422",
            agent_id="test_close_agent",
            agent_name="taskflow-triage",
        )
        client._running = True

        mock_ws = MagicMock()
        mock_ws.closed = False
        close_called = {}

        async def mock_close(code=1000, reason=""):
            close_called["code"] = code
            close_called["reason"] = reason

        mock_ws.close = mock_close
        client._ws = mock_ws

        asyncio.run(client.close(code=1000, reason="Daemon shutdown"))

        self.assertFalse(client._running)
        self.assertIsNone(client._ws)
        self.assertEqual(close_called.get("code"), 1000)
        self.assertEqual(close_called.get("reason"), "Daemon shutdown")

    def test_heartbeat_pong_increments_metrics_and_logs(self):
        """Verifies receiving ping answers pong, increments metrics, and logs heartbeat at INFO level."""
        stable_id = str(uuid.uuid4())
        client = SwitchAgentClient(
            server_url="http://127.0.0.1:52422",
            agent_id="test_heartbeat_agent",
            agent_name="taskflow-triage",
            connection_id=stable_id,
        )
        client.register_or_validate = MagicMock(return_value=True)
        client.ensure_room = MagicMock(return_value="room_hb_test")

        sent_messages = []

        class MockPingWs:
            def __init__(self):
                self.messages = [
                    json.dumps({"event": "ping", "data": {}}),
                ]
                self.idx = 0
                self.closed = False

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc_val, exc_tb):
                pass

            async def recv(self):
                if self.idx < len(self.messages):
                    msg = self.messages[self.idx]
                    self.idx += 1
                    return msg
                client.stop()
                raise websockets.exceptions.ConnectionClosed(None, None)

            async def send(self, data):
                sent_messages.append(json.loads(data))

            async def close(self, code=1000, reason=""):
                self.closed = True

        with patch("websockets.connect", return_value=MockPingWs()):
            with self.assertLogs("SwitchClient", level="INFO") as log_capture:
                asyncio.run(client.connect_and_listen(apply_briefing=False, auto_reconnect=False))

        self.assertEqual(client._pings_received, 1)
        self.assertEqual(client._pongs_sent, 1)
        self.assertTrue(any("[Heartbeat]" in msg for msg in log_capture.output))


if __name__ == "__main__":
    unittest.main()
