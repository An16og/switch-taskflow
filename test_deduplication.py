"""Unit tests for TaskDeduplicator and RoomBufferStore SQLite persistence."""

import os
import sys
import tempfile
import time
import unittest

if hasattr(sys.stdout, 'reconfigure') and sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')

from models import SlackMessage
from deduplication import (
    TaskDeduplicator,
    RoomBufferStore,
    normalize_tokens,
    compute_task_fingerprint,
    token_jaccard_similarity,
)


class TestTaskDeduplicator(unittest.TestCase):

    def setUp(self):
        self.temp_file = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.temp_file.close()
        self.db_path = self.temp_file.name
        self.dedup = TaskDeduplicator(ttl_seconds=300.0, similarity_threshold=0.60, storage_path=self.db_path)
        self.room_id = "test-room-123"

    def tearDown(self):
        self.dedup.close()
        if os.path.exists(self.db_path):
            try:
                os.remove(self.db_path)
            except Exception:
                pass

    def test_token_normalization(self):
        tokens = normalize_tokens("Draft release notes for v1.2 with new dark mode and bug fixes!")
        expected = ["bug", "dark", "draft", "fixes", "mode", "new", "notes", "release", "v1.2"]
        self.assertEqual(tokens, expected)

    def test_exact_duplicate_detection(self):
        task_title = "Draft release notes for v1.2"
        self.dedup.record_task(self.room_id, task_title, trigger_ts="100.1")

        is_dup, reason = self.dedup.is_duplicate(self.room_id, task_title, trigger_ts="100.2")
        self.assertTrue(is_dup)
        self.assertIn("Identical task", reason)

    def test_semantic_variation_detection(self):
        original = "Draft release notes for v1.2 with new dark mode"
        variation = "Please draft the v1.2 release notes highlighting dark mode"
        
        self.dedup.record_task(self.room_id, original, trigger_ts="200.1")

        is_dup, reason = self.dedup.is_duplicate(self.room_id, variation, trigger_ts="200.2")
        self.assertTrue(is_dup)
        self.assertIn("Semantic duplicate", reason)

    def test_trigger_timestamp_deduplication(self):
        self.dedup.record_task(self.room_id, "Some task", trigger_ts="ts_abc_123")
        is_dup, reason = self.dedup.is_duplicate(self.room_id, "Completely different task", trigger_ts="ts_abc_123")
        self.assertTrue(is_dup)
        self.assertIn("Trigger message ts 'ts_abc_123' was already processed", reason)

    def test_distinct_tasks_allowed(self):
        task1 = "Draft release notes for v1.2 with new dark mode"
        task2 = "Audit database connection pool settings and fix connection timeouts"

        self.dedup.record_task(self.room_id, task1, trigger_ts="300.1")
        is_dup, reason = self.dedup.is_duplicate(self.room_id, task2, trigger_ts="300.2")

        self.assertFalse(is_dup)
        self.assertIsNone(reason)

    def test_force_flag_bypasses_dedup(self):
        task_title = "Draft release notes for v1.2"
        self.dedup.record_task(self.room_id, task_title, trigger_ts="400.1")

        is_dup, reason = self.dedup.is_duplicate(self.room_id, task_title, force=True)
        self.assertFalse(is_dup)
        self.assertIsNone(reason)

    def test_ttl_expiration(self):
        short_dedup = TaskDeduplicator(ttl_seconds=0.1, storage_path=None)
        task_title = "Expiring task"
        short_dedup.record_task(self.room_id, task_title, trigger_ts="500.1")

        time.sleep(0.15)
        is_dup, reason = short_dedup.is_duplicate(self.room_id, task_title, trigger_ts="500.2")
        self.assertFalse(is_dup)

    def test_mark_completed_and_active_summaries(self):
        task_title = "Deploy staging cluster"
        fp = self.dedup.record_task(self.room_id, task_title)
        
        summaries = self.dedup.get_active_task_summaries(self.room_id)
        self.assertIn(task_title, summaries)

        ok = self.dedup.mark_completed(self.room_id, fingerprint=fp)
        self.assertTrue(ok)

    def test_sliding_window_deduplication_scenario(self):
        """Simulates the sliding buffer re-evaluating the same 'draft release notes' on subsequent messages."""
        msg1_task = "Draft release notes for v1.2 with new dark mode"
        is_dup1, _ = self.dedup.is_duplicate(self.room_id, msg1_task, trigger_ts="1001.0")
        self.assertFalse(is_dup1, "Initial task must not be treated as a duplicate")
        self.dedup.record_task(self.room_id, msg1_task, trigger_ts="1001.0")

        msg1_repeat = "Draft release notes for v1.2 with new dark mode"
        is_dup2, reason2 = self.dedup.is_duplicate(self.room_id, msg1_repeat, trigger_ts="1002.0")
        self.assertTrue(is_dup2, "Must suppress duplicate on message 2")
        self.assertIn("Identical task", reason2)

        msg3_variant = "Please draft the v1.2 release notes highlighting dark mode"
        is_dup3, reason3 = self.dedup.is_duplicate(self.room_id, msg3_variant, trigger_ts="1003.0")
        self.assertTrue(is_dup3, "Must suppress semantic variant on message 3")
        self.assertIn("Semantic duplicate", reason3)

        new_task = "Audit database connection pool settings and fix connection timeouts"
        is_dup4, reason4 = self.dedup.is_duplicate(self.room_id, new_task, trigger_ts="1004.0")
        self.assertFalse(is_dup4, "New distinct task must be allowed through")
        self.dedup.record_task(self.room_id, new_task, trigger_ts="1004.0")

        is_dup5, reason5 = self.dedup.is_duplicate(self.room_id, new_task, trigger_ts="1005.0")
        self.assertTrue(is_dup5, "Must suppress re-triage of database task on message 5")
        self.assertIn("Identical task", reason5)

    def test_sqlite_persistence_across_restart_simulation(self):
        """Simulates daemon shutdown and restart: newly created TaskDeduplicator
        connected to the same SQLite database must restore tasks and trigger timestamps.
        """
        room = "persist-room-456"
        task1 = "Draft v2.0 Architecture Whitepaper"
        trigger1 = "msg_arch_001"
        fp1 = self.dedup.record_task(room, task1, trigger_ts=trigger1)

        task2 = "Fix race condition in payment webhook"
        trigger2 = "msg_pay_002"
        fp2 = self.dedup.record_task(room, task2, trigger_ts=trigger2)
        self.dedup.mark_completed(room, fingerprint=fp2)

        # Simulate daemon exit
        self.dedup.close()

        # Simulate fresh daemon restart reading from the same DB file
        fresh_dedup = TaskDeduplicator(ttl_seconds=300.0, similarity_threshold=0.60, storage_path=self.db_path)

        try:
            # 1. Exact task duplicate detected across restart
            is_dup, reason = fresh_dedup.is_duplicate(room, task1, trigger_ts="msg_new_003")
            self.assertTrue(is_dup)
            self.assertIn("Identical task", reason)

            # 2. Semantic variation detected across restart
            variant = "Draft whitepaper for v2.0 architecture design"
            is_dup_var, reason_var = fresh_dedup.is_duplicate(room, variant, trigger_ts="msg_new_004")
            self.assertTrue(is_dup_var)
            self.assertIn("Semantic duplicate", reason_var)

            # 3. Seen trigger timestamp / ID detected across restart
            is_dup_trig, reason_trig = fresh_dedup.is_duplicate(room, "Completely unrelated task", trigger_ts=trigger1)
            self.assertTrue(is_dup_trig)
            self.assertIn("Trigger message ts 'msg_arch_001' was already processed", reason_trig)

            # 4. Completed status restored across restart
            self.assertTrue(fresh_dedup.has_seen_trigger(room, trigger2))
            records = fresh_dedup._records.get(room, [])
            pay_record = next((r for r in records if r.fingerprint == fp2), None)
            self.assertIsNotNone(pay_record)
            self.assertEqual(pay_record.status, "COMPLETED")

            # 5. Active task summaries populated across restart
            summaries = fresh_dedup.get_active_task_summaries(room)
            self.assertIn(task1, summaries)
            self.assertIn(task2, summaries)
        finally:
            fresh_dedup.close()


class TestRoomBufferStore(unittest.TestCase):

    def setUp(self):
        self.temp_file = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.temp_file.close()
        self.db_path = self.temp_file.name
        self.store = RoomBufferStore(storage_path=self.db_path, max_len=5)
        self.room_id = "test-buffer-room"

    def tearDown(self):
        self.store.close()
        if os.path.exists(self.db_path):
            try:
                os.remove(self.db_path)
            except Exception:
                pass

    def test_append_and_deque_compatibility(self):
        """Tests that RoomBufferStore supports both direct and dict/deque-like access."""
        msg1 = SlackMessage(user_id="U1", user_name="Alice", text="Hello team", ts="100.1")
        msg2 = SlackMessage(user_id="U2", user_name="Bob", text="Can we release v1.5?", ts="100.2")

        # Dict/deque style access
        self.store[self.room_id].append(msg1)
        self.store[self.room_id].append(msg2)

        buf = list(self.store[self.room_id])
        self.assertEqual(len(buf), 2)
        self.assertEqual(buf[0].text, "Hello team")
        self.assertEqual(buf[1].text, "Can we release v1.5?")
        self.assertEqual(len(self.store[self.room_id]), 2)
        self.assertEqual(self.store[self.room_id][0].user_name, "Alice")

    def test_room_buffer_persistence_across_restart(self):
        """Simulates daemon shutdown and restart: newly created RoomBufferStore
        connected to the same SQLite database restores recent conversation context.
        """
        messages = [
            SlackMessage(user_id="U1", user_name="Alice", text="Meeting in 5", ts="201.0"),
            SlackMessage(user_id="U2", user_name="Bob", text="Please deploy staging", ts="202.0"),
            SlackMessage(user_id="U3", user_name="Charlie", text="Staging deployed successfully", ts="203.0"),
        ]
        for m in messages:
            self.store[self.room_id].append(m)

        # Simulate daemon exit
        self.store.close()

        # Simulate fresh daemon restart reading from the same DB file
        fresh_store = RoomBufferStore(storage_path=self.db_path, max_len=5)
        try:
            restored = list(fresh_store[self.room_id])
            self.assertEqual(len(restored), 3)
            self.assertEqual([m.text for m in restored], ["Meeting in 5", "Please deploy staging", "Staging deployed successfully"])
            self.assertEqual([m.user_name for m in restored], ["Alice", "Bob", "Charlie"])
            self.assertEqual([m.ts for m in restored], ["201.0", "202.0", "203.0"])

            # Test rolling window continues seamlessly after restart
            fresh_store[self.room_id].append(SlackMessage(user_id="U1", user_name="Alice", text="Running sanity tests", ts="204.0"))
            fresh_store[self.room_id].append(SlackMessage(user_id="U2", user_name="Bob", text="Sanity tests green", ts="205.0"))
            fresh_store[self.room_id].append(SlackMessage(user_id="U3", user_name="Charlie", text="Rolling to prod", ts="206.0"))

            # Max len is 5; the oldest ("Meeting in 5") must have rolled out
            updated = list(fresh_store[self.room_id])
            self.assertEqual(len(updated), 5)
            self.assertEqual(updated[0].text, "Please deploy staging")
            self.assertEqual(updated[-1].text, "Rolling to prod")
        finally:
            fresh_store.close()

    def test_multi_room_isolation_and_clear(self):
        """Tests that buffers for separate rooms remain isolated and clear behaves correctly."""
        room_a = "room_alpha"
        room_b = "room_beta"

        self.store[room_a].append(SlackMessage(user_id="U1", user_name="A1", text="Alpha Msg", ts="301"))
        self.store[room_b].append(SlackMessage(user_id="U2", user_name="B1", text="Beta Msg", ts="302"))

        self.assertEqual(len(self.store[room_a]), 1)
        self.assertEqual(len(self.store[room_b]), 1)

        # Clear room_a only
        self.store.clear(room_a)
        self.assertEqual(len(self.store[room_a]), 0)
        self.assertEqual(len(self.store[room_b]), 1)

        # Restart and ensure room_b retained in SQLite while room_a is empty
        self.store.close()
        fresh_store = RoomBufferStore(storage_path=self.db_path, max_len=5)
        try:
            self.assertEqual(len(fresh_store[room_a]), 0)
            self.assertEqual(len(fresh_store[room_b]), 1)
            self.assertEqual(fresh_store[room_b][0].text, "Beta Msg")
        finally:
            fresh_store.close()


if __name__ == "__main__":
    unittest.main()
