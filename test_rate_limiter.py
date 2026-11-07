"""Unit tests for RoomRateLimiter and LLM Cost DoS guardrail."""

import asyncio
import sys
import time
import unittest
from unittest.mock import MagicMock, patch

if hasattr(sys.stdout, 'reconfigure') and sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')

from models import TaskTriageResult, ConfidenceLevel, ExecutionType
from rate_limiter import RoomRateLimiter


class TestRoomRateLimiter(unittest.TestCase):

    def setUp(self):
        # 3 requests per 2.0 seconds window for fast, reliable unit testing
        self.limiter = RoomRateLimiter(max_requests=3, window_seconds=2.0, enabled=True)
        self.room_a = "room-alpha"
        self.room_b = "room-beta"

    def tearDown(self):
        self.limiter.reset()

    def test_sliding_window_permits_up_to_max(self):
        """Verifies requests up to max_requests are allowed."""
        # 1st request
        ok1, retry1, _ = self.limiter.acquire(self.room_a)
        self.assertTrue(ok1)
        self.assertEqual(retry1, 0)

        # 2nd request
        ok2, retry2, _ = self.limiter.acquire(self.room_a)
        self.assertTrue(ok2)
        self.assertEqual(retry2, 0)

        # 3rd request (hits capacity)
        ok3, retry3, _ = self.limiter.acquire(self.room_a)
        self.assertTrue(ok3)
        self.assertEqual(retry3, 0)

        # 4th request (exceeds capacity)
        ok4, retry4, reason4 = self.limiter.acquire(self.room_a)
        self.assertFalse(ok4)
        self.assertGreaterEqual(retry4, 1)
        self.assertIn("Room rate limit exceeded", reason4)
        self.assertIn("LLM Cost DoS protection", reason4)

    def test_room_isolation(self):
        """Verifies that room A exhausting its quota does NOT impact room B."""
        # Exhaust room A
        for _ in range(3):
            ok, _, _ = self.limiter.acquire(self.room_a)
            self.assertTrue(ok)

        # Room A must now be blocked
        ok_a, _, _ = self.limiter.acquire(self.room_a)
        self.assertFalse(ok_a)

        # Room B must be completely unaffected and allowed
        ok_b, retry_b, _ = self.limiter.acquire(self.room_b)
        self.assertTrue(ok_b)
        self.assertEqual(retry_b, 0)

    def test_window_expiry_restores_quota(self):
        """Verifies quota replenishes as timestamps slide past window_seconds."""
        # Use very short window for fast sleep test
        fast_limiter = RoomRateLimiter(max_requests=2, window_seconds=0.15)
        room = "fast-room"

        ok1, _, _ = fast_limiter.acquire(room)
        ok2, _, _ = fast_limiter.acquire(room)
        self.assertTrue(ok1)
        self.assertTrue(ok2)

        # Blocked immediately
        blocked, retry, _ = fast_limiter.acquire(room)
        self.assertFalse(blocked)

        # Wait for window to slide past
        time.sleep(0.20)

        # Restored!
        restored, _, _ = fast_limiter.acquire(room)
        self.assertTrue(restored)

    def test_disabled_limiter_permits_all(self):
        """Verifies that when rate limiting is disabled, all requests succeed."""
        disabled_limiter = RoomRateLimiter(max_requests=1, window_seconds=60.0, enabled=False)
        for _ in range(10):
            ok, retry, reason = disabled_limiter.acquire("any-room")
            self.assertTrue(ok)
            self.assertEqual(retry, 0)
            self.assertIsNone(reason)

    def test_check_does_not_consume_quota(self):
        """Verifies check() inspects capacity without consuming tokens."""
        ok_check, retry, _ = self.limiter.check(self.room_a)
        self.assertTrue(ok_check)
        self.assertEqual(retry, 0)

        usage = self.limiter.get_usage(self.room_a)
        self.assertEqual(usage["current_requests"], 0)

    def test_custom_cost_acquisition(self):
        """Verifies cost parameter consumes multiple units."""
        # Consuming 3 units at once hits capacity
        ok, _, _ = self.limiter.acquire(self.room_a, cost=3)
        self.assertTrue(ok)

        # Next request must be blocked
        blocked, _, _ = self.limiter.acquire(self.room_a, cost=1)
        self.assertFalse(blocked)


class TestBotRateLimitIntegration(unittest.TestCase):

    def setUp(self):
        from bot import room_rate_limiter
        room_rate_limiter.reset()

    def tearDown(self):
        from bot import room_rate_limiter
        room_rate_limiter.reset()

    def test_triage_addressed_message_throttled_with_notice(self):
        """Verifies that when an addressed message hits the room rate limit,
        the bot dispatches an LLM Cost DoS notice and does not execute triage LLM.
        """
        import bot
        from bot import handle_triage_message, room_rate_limiter

        # Configure small capacity on bot's limiter for testing
        test_limiter = RoomRateLimiter(max_requests=1, window_seconds=60.0)
        bot.room_rate_limiter = test_limiter

        mock_client = MagicMock()
        mock_client.room_id = "test-dos-room"
        mock_client.agent_id = "agent-triage"

        payload = {
            "body": "@taskflow draft release notes for v1.2",
            "sender": "U_USER1",
            "sender_name": "Bob",
            "addressed": True,
            "room_id": "test-dos-room",
            "id": "msg_001",
        }

        # 1st request consumes the single token
        with patch.object(bot.triage_classifier, "triage_conversation") as mock_triage:
            mock_triage.return_value = TaskTriageResult(
                is_task=True,
                confidence=ConfidenceLevel.HIGH,
                confidence_reason="Valid task",
                action_summary="Draft release notes",
                execution_type=ExecutionType.AUTO_EXECUTE
            )
            asyncio.run(handle_triage_message(mock_client, payload))
            self.assertEqual(mock_triage.call_count, 1)

        # 2nd request in the same room must be throttled before calling LLM
        payload2 = {
            "body": "@taskflow draft another document immediately",
            "sender": "U_USER1",
            "sender_name": "Bob",
            "addressed": True,
            "room_id": "test-dos-room",
            "id": "msg_002",
        }

        with patch.object(bot.triage_classifier, "triage_conversation") as mock_triage2:
            asyncio.run(handle_triage_message(mock_client, payload2))
            # LLM was NEVER called on throttled message!
            mock_triage2.assert_not_called()

            # Verify rate limit notice was sent to sender
            mock_client.send_targeted_message.assert_called()
            last_call = mock_client.send_targeted_message.call_args.kwargs
            self.assertIn("[Rate Limit: LLM Cost DoS Protection]", last_call["body"])
            self.assertIn("quota", last_call["body"])

    def test_ambient_message_throttled_silently_without_spamming_room(self):
        """Verifies that ambient chatter hitting the rate limit is dropped with a log warning,
        WITHOUT spamming the room with rate-limit cards.
        """
        import bot
        from bot import handle_triage_message

        test_limiter = RoomRateLimiter(max_requests=1, window_seconds=60.0)
        bot.room_rate_limiter = test_limiter

        # Exhaust quota
        test_limiter.acquire("test-ambient-room", cost=1)

        mock_client = MagicMock()
        mock_client.room_id = "test-ambient-room"
        mock_client.agent_id = "agent-triage"

        ambient_payload = {
            "body": "todo: check staging server logs please",  # Ambient keyword match
            "sender": "U_USER2",
            "sender_name": "Alice",
            "addressed": False,
            "room_id": "test-ambient-room",
            "id": "msg_ambient_003",
        }

        with patch.object(bot.triage_classifier, "triage_conversation") as mock_triage:
            asyncio.run(handle_triage_message(mock_client, ambient_payload))
            # LLM was NOT called
            mock_triage.assert_not_called()
            # Bot did NOT post annoying cards to the room on ambient throttle
            mock_client.send_targeted_message.assert_not_called()
            mock_client.post_message.assert_not_called()

    def test_worker_execution_throttled_when_room_quota_exhausted(self):
        """Verifies that worker deliverable drafting is throttled when room quota is exhausted."""
        import bot
        from bot import handle_worker_message

        test_limiter = RoomRateLimiter(max_requests=1, window_seconds=60.0)
        bot.room_rate_limiter = test_limiter

        # Exhaust room quota
        test_limiter.acquire("test-worker-room", cost=1)

        mock_client = MagicMock()
        mock_client.room_id = "test-worker-room"
        mock_client.agent_id = "agent-worker"

        worker_payload = {
            "body": '@taskflow-worker <!-- TASKFLOW_HANDOFF: {"task_title": "Draft release notes", "execution_type": "AUTO_EXECUTE", "confidence": "HIGH", "room_id": "test-worker-room", "requester_name": "Bob"} -->',
            "sender": "U_USER1",
            "sender_name": "Bob",
            "addressed": True,
            "room_id": "test-worker-room",
            "id": "msg_work_001",
        }

        with patch.object(bot.worker_executor, "execute_task") as mock_worker:
            asyncio.run(handle_worker_message(mock_client, worker_payload))
            # Worker LLM execution was prevented!
            mock_worker.assert_not_called()
            # Targeted throttle notice sent to requester
            mock_client.send_targeted_message.assert_called_once()
            call_body = mock_client.send_targeted_message.call_args.kwargs["body"]
            self.assertIn("[TaskFlow Rate Limit: LLM Cost DoS Protection]", call_body)


if __name__ == "__main__":
    unittest.main()
