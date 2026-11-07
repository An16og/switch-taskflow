"""Unit tests for TaskDeduplicator."""

import sys
import time
import unittest

if hasattr(sys.stdout, 'reconfigure') and sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')

from deduplication import (
    TaskDeduplicator,
    normalize_tokens,
    compute_task_fingerprint,
    token_jaccard_similarity,
)


class TestTaskDeduplicator(unittest.TestCase):

    def setUp(self):
        self.dedup = TaskDeduplicator(ttl_seconds=300.0, similarity_threshold=0.60)
        self.room_id = "test-room-123"

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
        short_dedup = TaskDeduplicator(ttl_seconds=0.1)
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
        """Simulates the exact user problem:
        A sliding buffer re-evaluates the same 'draft release notes' on subsequent messages.
        Deduplicator must allow the first task, suppress it on subsequent messages,
        and allow a new, distinct task.
        """
        # Message 1: User asks for release notes
        msg1_task = "Draft release notes for v1.2 with new dark mode"
        is_dup1, _ = self.dedup.is_duplicate(self.room_id, msg1_task, trigger_ts="1001.0")
        self.assertFalse(is_dup1, "Initial task must not be treated as a duplicate")
        self.dedup.record_task(self.room_id, msg1_task, trigger_ts="1001.0")

        # Message 2 arrives: "Looks great, thanks team!"
        # Triage re-evaluates sliding window [Msg 1, Msg 2] and attempts to output Msg 1 task again
        is_dup2, reason2 = self.dedup.is_duplicate(self.room_id, msg1_task, trigger_ts="1002.0")
        self.assertTrue(is_dup2, "Must suppress duplicate on message 2")
        self.assertIn("Identical task", reason2)

        # Message 3 arrives: "Can someone check CI?"
        # Triage re-evaluates sliding window [Msg 1, Msg 2, Msg 3] and outputs a slightly rephrased version
        msg3_variant = "Please draft the v1.2 release notes highlighting dark mode"
        is_dup3, reason3 = self.dedup.is_duplicate(self.room_id, msg3_variant, trigger_ts="1003.0")
        self.assertTrue(is_dup3, "Must suppress semantic variant on message 3")
        self.assertIn("Semantic duplicate", reason3)

        # Message 4 arrives: A genuinely new task appears: "Todo: audit database connection pool settings"
        new_task = "Audit database connection pool settings and fix connection timeouts"
        is_dup4, reason4 = self.dedup.is_duplicate(self.room_id, new_task, trigger_ts="1004.0")
        self.assertFalse(is_dup4, "New distinct task must be allowed through")
        self.dedup.record_task(self.room_id, new_task, trigger_ts="1004.0")

        # Message 5 arrives: Chatter following Message 4
        # Triage attempts to re-output database task
        is_dup5, reason5 = self.dedup.is_duplicate(self.room_id, new_task, trigger_ts="1005.0")
        self.assertTrue(is_dup5, "Must suppress re-triage of database task on message 5")
        self.assertIn("Identical task", reason5)


if __name__ == "__main__":
    unittest.main()
