"""Unit tests for Switch Room Task Store & Todoist Optional Sync.

Verifies:
1. Native Switch Room Tasks (work stays natively in the room).
2. Todoist as an optional sync (never mandatory).
3. Clear 4xx diagnostics on 401 Unauthorized, 400 Bad Request, 429 Rate Limit.
4. Receipt formatting and state persistence across restarts.
"""

import os
import tempfile
import unittest
from unittest.mock import patch, MagicMock

from room_tasks import RoomTask, RoomTaskStore, TaskStatus
from todoist_client import TodoistClient, TodoistSyncResult
from interactions import InteractionDispatcher, handle_confirm_assignment
from approvals import PendingApprovalStore, ApprovalStatus


class TestRoomTasks(unittest.TestCase):

    def setUp(self):
        self.temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".json")
        self.temp_file.write(b'{"tasks": []}')
        self.temp_file.close()
        self.store = RoomTaskStore(storage_path=self.temp_file.name)

    def tearDown(self):
        if os.path.exists(self.temp_file.name):
            os.unlink(self.temp_file.name)

    def test_create_and_retrieve_native_task(self):
        task = self.store.create_task(
            title="Benchmark database migration",
            room_id="room_alpha",
            assignee_name="Anuj",
            assignee_id="U0CFRP39TRS",
            assigner_name="Priya",
            assigner_id="U_PRIYA",
            deadline="Friday 5 PM",
            action_plan="* [ ] Run migration on staging\n* [ ] Measure query latency",
            context_notes="PR #89",
        )
        self.assertIsNotNone(task.task_id)
        self.assertTrue(task.task_id.startswith("sw_task_"))
        self.assertEqual(task.status, TaskStatus.PENDING)
        self.assertEqual(task.room_id, "room_alpha")
        self.assertEqual(task.assignee_name, "Anuj")

        # Retrieve
        fetched = self.store.get_task(task.task_id)
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched.title, "Benchmark database migration")

    def test_persistence_across_store_reloads(self):
        task = self.store.create_task(
            title="Implement OAuth2 provider",
            room_id="room_beta",
            assignee_name="Sarah",
            deadline="Tomorrow",
        )
        task_id = task.task_id

        # Reload new store instance from same file
        reloaded_store = RoomTaskStore(storage_path=self.temp_file.name)
        reloaded_task = reloaded_store.get_task(task_id)
        self.assertIsNotNone(reloaded_task)
        self.assertEqual(reloaded_task.title, "Implement OAuth2 provider")
        self.assertEqual(reloaded_task.assignee_name, "Sarah")

    def test_update_task_status(self):
        task = self.store.create_task(
            title="Update API docs",
            room_id="room_gamma",
            assignee_name="Alex",
        )
        self.assertEqual(task.status, TaskStatus.PENDING)

        updated = self.store.update_task_status(task.task_id, TaskStatus.COMPLETED)
        self.assertIsNotNone(updated)
        self.assertEqual(updated.status, TaskStatus.COMPLETED)

        # Check reloaded
        reloaded = self.store.get_task(task.task_id)
        self.assertEqual(reloaded.status, TaskStatus.COMPLETED)

    def test_list_tasks_filtering(self):
        self.store.create_task(title="Task 1", room_id="room_1", assignee_name="Anuj")
        self.store.create_task(title="Task 2", room_id="room_1", assignee_name="Priya")
        self.store.create_task(title="Task 3", room_id="room_2", assignee_name="Anuj")

        r1_tasks = self.store.list_tasks(room_id="room_1")
        self.assertEqual(len(r1_tasks), 2)

        anuj_tasks = self.store.list_tasks(assignee="Anuj")
        self.assertEqual(len(anuj_tasks), 2)

    def test_format_receipt_with_sync_and_fallback(self):
        # 1. Native only (Todoist unconfigured)
        task1 = self.store.create_task(
            title="Fix telemetry pipeline",
            room_id="room_1",
            assignee_name="Anuj",
            assigner_name="Priya",
        )
        receipt1 = task1.format_receipt()
        self.assertIn("Task Tracked in Switch Room", receipt1)
        self.assertIn(task1.task_id, receipt1)
        self.assertIn("Native room only (Todoist unconfigured)", receipt1)

        # 2. External Sync Success
        task2 = self.store.create_task(
            title="Review PR",
            room_id="room_1",
            assignee_name="Anuj",
            todoist_url="https://todoist.com/app/task/999",
        )
        receipt2 = task2.format_receipt()
        self.assertIn("[View in Todoist](https://todoist.com/app/task/999)", receipt2)

        # 3. External Sync 4xx Notice
        task3 = self.store.create_task(
            title="Deploy release",
            room_id="room_1",
            assignee_name="Anuj",
            todoist_error="HTTP 401 Unauthorized: Invalid or expired TODOIST_API_TOKEN.",
        )
        receipt3 = task3.format_receipt()
        self.assertIn("External Sync Notice:", receipt3)
        self.assertIn("HTTP 401 Unauthorized", receipt3)


class TestTodoistDiagnostics(unittest.TestCase):

    def test_unconfigured_token_returns_clean_result(self):
        client = TodoistClient(api_token="")
        res = client.sync_task(content="Test task")
        self.assertFalse(res.success)
        self.assertIn("token is not configured", res.error_message)
        self.assertIn("remains tracked natively in Switch Workroom", res.error_message)

    @patch("requests.post")
    def test_http_401_unauthorized_clear_diagnostic(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_resp.text = "Unauthorized"
        mock_post.return_value = mock_resp

        client = TodoistClient(api_token="bad_token_123")
        res = client.sync_task(content="Urgent hotfix")

        self.assertFalse(res.success)
        self.assertTrue(res.is_client_error)
        self.assertEqual(res.status_code, 401)
        self.assertIn("HTTP 401 Unauthorized", res.error_message)
        self.assertIn("Invalid or expired TODOIST_API_TOKEN", res.error_message)
        self.assertIn("Task remains tracked natively in Switch Workroom", res.error_message)

    @patch("requests.post")
    def test_http_400_bad_request_clear_diagnostic(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 400
        mock_resp.text = '{"error": "Project not found"}'
        mock_post.return_value = mock_resp

        client = TodoistClient(api_token="valid_token")
        res = client.sync_task(content="Test 400")

        self.assertFalse(res.success)
        self.assertTrue(res.is_client_error)
        self.assertEqual(res.status_code, 400)
        self.assertIn("HTTP 400 Bad Request", res.error_message)
        self.assertIn("Project not found", res.error_message)
        self.assertIn("Task remains tracked natively in Switch Workroom", res.error_message)

    @patch("requests.post")
    def test_http_429_rate_limit_clear_diagnostic(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 429
        mock_resp.text = "Too many requests"
        mock_post.return_value = mock_resp

        client = TodoistClient(api_token="valid_token", base_backoff=0.001)
        res = client.sync_task(content="Test 429")

        self.assertFalse(res.success)
        self.assertTrue(res.is_client_error)
        self.assertEqual(res.status_code, 429)
        self.assertIn("HTTP 429 Too Many Requests", res.error_message)
        self.assertIn("rate limit exceeded", res.error_message)
        self.assertIn("Task remains tracked natively in Switch Workroom", res.error_message)

    @patch("requests.post")
    def test_http_200_success(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "id": "123456",
            "url": "https://todoist.com/app/task/123456"
        }
        mock_post.return_value = mock_resp

        client = TodoistClient(api_token="valid_token")
        res = client.sync_task(content="Review PR #42")

        self.assertTrue(res.success)
        self.assertEqual(res.task_id, "123456")
        self.assertEqual(res.task_url, "https://todoist.com/app/task/123456")

    @patch("requests.post")
    def test_create_task_backwards_compatible_wrapper(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "id": "7890",
            "url": "https://todoist.com/app/task/7890"
        }
        mock_post.return_value = mock_resp

        client = TodoistClient(api_token="valid_token")
        dict_res = client.create_task(content="Legacy call")

        self.assertIsNotNone(dict_res)
        self.assertEqual(dict_res["id"], "7890")
        self.assertEqual(dict_res["url"], "https://todoist.com/app/task/7890")

    @patch("requests.post")
    def test_idempotent_sync_with_external_id_and_header(self, mock_post):
        """Verifies deterministic X-Request-Id header is sent and cached to prevent duplicates."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "id": "td_task_555",
            "url": "https://todoist.com/app/task/td_task_555"
        }
        mock_post.return_value = mock_resp

        client = TodoistClient(api_token="valid_token", base_backoff=0.001)
        res1 = client.sync_task(
            content="Deploy staging v2",
            description="Run ansible playbook",
            due_string="tomorrow",
            external_id="sw_task_abc123"
        )
        self.assertTrue(res1.success)
        self.assertEqual(res1.task_id, "td_task_555")
        self.assertIsNotNone(res1.idempotency_key)

        # Verify X-Request-Id header was sent on the POST call
        call_headers = mock_post.call_args[1]["headers"]
        self.assertIn("X-Request-Id", call_headers)
        self.assertEqual(call_headers["X-Request-Id"], res1.idempotency_key)

        # Repeated call with same external_id must return cached result without hitting network again
        res2 = client.sync_task(
            content="Deploy staging v2",
            description="Run ansible playbook",
            due_string="tomorrow",
            external_id="sw_task_abc123"
        )
        self.assertTrue(res2.success)
        self.assertEqual(res2.task_id, "td_task_555")
        self.assertEqual(mock_post.call_count, 1, "Must not make duplicate HTTP request for same external_id")

    @patch("requests.post")
    def test_idempotency_content_hash_when_external_id_omitted(self, mock_post):
        """Verifies deterministic content hash produces identical X-Request-Id and caches."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "id": "td_task_888",
            "url": "https://todoist.com/app/task/td_task_888"
        }
        mock_post.return_value = mock_resp

        client = TodoistClient(api_token="valid_token", base_backoff=0.001)
        res1 = client.sync_task(content="Audit SSL certs", description="Check expiry on *.api.com")
        self.assertTrue(res1.success)
        self.assertEqual(res1.task_id, "td_task_888")

        # Second call with identical content (no external_id) should hit idempotency cache
        res2 = client.sync_task(content="Audit SSL certs", description="Check expiry on *.api.com")
        self.assertTrue(res2.success)
        self.assertEqual(res2.task_id, "td_task_888")
        self.assertEqual(res1.idempotency_key, res2.idempotency_key)
        self.assertEqual(mock_post.call_count, 1)

    @patch("requests.post")
    def test_http_429_retry_and_eventual_success(self, mock_post):
        """Verifies that HTTP 429 triggers exponential backoff and succeeds on subsequent attempt."""
        resp_429 = MagicMock()
        resp_429.status_code = 429
        resp_429.headers = {"Retry-After": "0.01"}
        resp_429.text = "Rate limit exceeded"

        resp_200 = MagicMock()
        resp_200.status_code = 200
        resp_200.json.return_value = {
            "id": "td_task_retry_ok",
            "url": "https://todoist.com/app/task/td_task_retry_ok"
        }

        # 429 on first 2 calls, 200 on 3rd call
        mock_post.side_effect = [resp_429, resp_429, resp_200]

        slept_times = []
        client = TodoistClient(
            api_token="valid_token",
            max_retries=3,
            base_backoff=0.001,
            sleep_fn=lambda s: slept_times.append(s)
        )

        res = client.sync_task(content="Retried task", external_id="retry_test_1")
        self.assertTrue(res.success)
        self.assertEqual(res.task_id, "td_task_retry_ok")
        self.assertEqual(res.retries_attempted, 2)
        self.assertEqual(len(slept_times), 2)
        self.assertEqual(mock_post.call_count, 3)

        # Verify X-Request-Id was identical across all retry attempts
        headers_first = mock_post.call_args_list[0][1]["headers"]["X-Request-Id"]
        headers_second = mock_post.call_args_list[1][1]["headers"]["X-Request-Id"]
        headers_third = mock_post.call_args_list[2][1]["headers"]["X-Request-Id"]
        self.assertEqual(headers_first, headers_second)
        self.assertEqual(headers_second, headers_third)


class TestInteractionWithRoomTasks(unittest.TestCase):

    def setUp(self):
        self.temp_approval_file = tempfile.NamedTemporaryFile(delete=False, suffix=".json")
        self.temp_approval_file.write(b'{}')
        self.temp_approval_file.close()
        self.approval_store = PendingApprovalStore(storage_path=self.temp_approval_file.name)

    def tearDown(self):
        if os.path.exists(self.temp_approval_file.name):
            os.unlink(self.temp_approval_file.name)

    def test_handle_interaction_records_room_task_and_handles_401(self):
        # Create approval
        appr = self.approval_store.create_approval(
            action_summary="Audit Redis latency",
            artifact_content="* [ ] Run redis-cli --latency\n* [ ] Check slowlog",
            action_type="HUMAN_TODO",
            assignee_name="Anuj",
            assignee_id="U0CFRP39TRS",
            assigner_name="Priya",
            assigner_id="U_PRIYA",
            deadline="Today 6 PM",
            room_id="room_test_100",
        )

        mock_todoist = MagicMock(spec=TodoistClient)
        mock_todoist.create_task.return_value = None
        mock_todoist.last_error = "HTTP 401 Unauthorized: Invalid or expired TODOIST_API_TOKEN."

        dispatcher = InteractionDispatcher(
            approval_store=self.approval_store,
            todoist_client=mock_todoist
        )

        payload = {
            "user": {"id": "U0CFRP39TRS", "name": "anuj"},
            "actions": [{"action_id": "btn_accept_task", "value": f'{{"approval_id": "{appr.approval_id}", "action": "accept"}}'}]
        }

        result = dispatcher.handle_interaction(payload)
        self.assertTrue(result["success"])
        self.assertIsNotNone(result.get("task_id"))
        self.assertIn("HTTP 401 Unauthorized", result.get("todoist_error"))

        # Verify confirmation block contains the 401 sync notice
        blocks = result["blocks"]
        block_text = blocks[0]["text"]["text"]
        self.assertIn("*Sync Notice:* HTTP 401 Unauthorized", block_text)
        self.assertIn(result["task_id"], block_text)


if __name__ == "__main__":
    unittest.main()
