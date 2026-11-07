"""Task Deduplication & Room Buffer Persistence Engine for TaskFlow.

Prevents repeated task executions caused by sliding conversation windows,
WebSocket reconnections, and daemon restarts. Backed by SQLite persistence.
Combines:
1. Message trigger ID & timestamp tracking.
2. Canonical token normalization & hash fingerprinting.
3. Jaccard token-similarity semantic duplicate detection.
4. Per-room TTL/cooldown state tracking with SQLite backing.
5. Persistent sliding-window conversation buffer per room.
"""

import hashlib
import json
import logging
import os
import re
import sqlite3
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Deque, Dict, Iterable, List, Optional, Set, Tuple

from models import SlackMessage

try:
    from config import TASKFLOW_STATE_DB
except ImportError:
    TASKFLOW_STATE_DB = os.getenv("TASKFLOW_STATE_DB", "taskflow_state.db")

logger = logging.getLogger("TaskDeduplicator")

# Common English and chat noise stop words that do not change task semantics
STOP_WORDS: Set[str] = {
    "a", "an", "the", "and", "or", "but", "if", "then", "else", "when",
    "at", "by", "for", "with", "about", "against", "between", "into", "through",
    "during", "before", "after", "above", "below", "to", "from", "up", "down",
    "in", "out", "on", "off", "over", "under", "again", "further", "then", "once",
    "here", "there", "all", "any", "both", "each", "few", "more", "most", "other",
    "some", "such", "no", "nor", "not", "only", "own", "same", "so", "than", "too",
    "very", "can", "will", "just", "should", "now", "please", "could", "would",
    "need", "someone", "team", "hey", "hi", "hello", "thanks", "thank", "you",
    "we", "our", "us", "i", "me", "my", "it", "its", "this", "that", "these", "those",
}

# Regex to strip non-alphanumeric characters (keep version numbers like v1.2, PR numbers like #89)
TOKEN_CLEAN_REGEX = re.compile(r"[^\w\.\-\#]+")


def normalize_tokens(text: str) -> List[str]:
    """Extracts sorted, normalized, lowercased content tokens from task text."""
    if not text:
        return []
    cleaned = TOKEN_CLEAN_REGEX.sub(" ", text.lower())
    tokens = [
        t for t in cleaned.split()
        if len(t) > 1 and t not in STOP_WORDS
    ]
    return sorted(tokens)


def compute_task_fingerprint(action_summary: str) -> str:
    """Computes a deterministic hash fingerprint for an action summary."""
    tokens = normalize_tokens(action_summary)
    token_str = " ".join(tokens)
    return hashlib.sha256(token_str.encode("utf-8")).hexdigest()[:16]


def token_jaccard_similarity(tokens_a: List[str], tokens_b: List[str]) -> float:
    """Computes Jaccard similarity between two token lists."""
    set_a = set(tokens_a)
    set_b = set(tokens_b)
    if not set_a or not set_b:
        return 0.0
    intersection = len(set_a.intersection(set_b))
    union = len(set_a.union(set_b))
    return float(intersection) / float(union) if union > 0 else 0.0


@dataclass
class TaskRecord:
    task_id: str
    room_id: str
    action_summary: str
    normalized_tokens: List[str]
    fingerprint: str
    trigger_ts: Optional[str]
    execution_type: str
    status: str = "HANDED_OFF"  # HANDED_OFF, COMPLETED
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


class TaskDeduplicator:
    """Thread-safe, TTL-based Task Deduplication engine backed by SQLite.
    
    Survives bot restarts and WebSocket reconnections without re-firing tasks.
    """

    def __init__(
        self,
        ttl_seconds: float = 1800.0,
        similarity_threshold: float = 0.60,
        storage_path: Optional[str] = None,
    ):
        """
        Args:
            ttl_seconds: Time-to-live for task deduplication cache (default 30 mins / 1800s).
            similarity_threshold: Jaccard token similarity above which tasks are duplicates.
            storage_path: Path to SQLite DB file (defaults to TASKFLOW_STATE_DB or 'taskflow_state.db').
        """
        self.ttl_seconds = ttl_seconds
        self.similarity_threshold = similarity_threshold
        self.storage_path = TASKFLOW_STATE_DB if storage_path is None else storage_path
        self._lock = threading.Lock()
        # room_id -> list of TaskRecord
        self._records: Dict[str, List[TaskRecord]] = {}
        # room_id -> set of processed trigger message timestamps
        self._seen_trigger_ts: Dict[str, Set[str]] = {}
        self._conn: Optional[sqlite3.Connection] = None

        if self.storage_path:
            dir_name = os.path.dirname(os.path.abspath(self.storage_path))
            if dir_name:
                os.makedirs(dir_name, exist_ok=True)
            self._conn = sqlite3.connect(self.storage_path, check_same_thread=False)
            self._init_db()
            self._load_from_db()

    def _init_db(self) -> None:
        if not self._conn:
            return
        with self._lock:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS dedup_tasks (
                    task_id TEXT PRIMARY KEY,
                    room_id TEXT NOT NULL,
                    action_summary TEXT NOT NULL,
                    normalized_tokens TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    trigger_ts TEXT,
                    execution_type TEXT,
                    status TEXT NOT NULL DEFAULT 'HANDED_OFF',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_dedup_room_created ON dedup_tasks (room_id, created_at);"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_dedup_room_fp ON dedup_tasks (room_id, fingerprint);"
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS dedup_seen_triggers (
                    room_id TEXT NOT NULL,
                    trigger_ts TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (room_id, trigger_ts)
                );
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_seen_triggers_room ON dedup_seen_triggers (room_id);"
            )
            self._conn.commit()

    def _load_from_db(self) -> None:
        """Loads non-expired records from SQLite into memory on startup."""
        if not self._conn:
            return
        with self._lock:
            now = time.time()
            cutoff = now - self.ttl_seconds
            try:
                # Clean up expired records from DB on startup
                self._conn.execute("DELETE FROM dedup_tasks WHERE created_at < ?", (cutoff,))
                self._conn.execute("DELETE FROM dedup_seen_triggers WHERE created_at < ?", (cutoff,))
                self._conn.commit()

                # Load active task records
                cur = self._conn.cursor()
                cur.execute(
                    """
                    SELECT task_id, room_id, action_summary, normalized_tokens,
                           fingerprint, trigger_ts, execution_type, status,
                           created_at, updated_at
                    FROM dedup_tasks
                    WHERE created_at >= ?
                    ORDER BY created_at ASC
                    """,
                    (cutoff,)
                )
                for row in cur.fetchall():
                    try:
                        tokens = json.loads(row[3]) if row[3] else []
                    except Exception:
                        tokens = []
                    rec = TaskRecord(
                        task_id=row[0],
                        room_id=row[1],
                        action_summary=row[2],
                        normalized_tokens=tokens,
                        fingerprint=row[4],
                        trigger_ts=row[5],
                        execution_type=row[6] or "AUTO_EXECUTE",
                        status=row[7] or "HANDED_OFF",
                        created_at=float(row[8]),
                        updated_at=float(row[9]),
                    )
                    self._records.setdefault(rec.room_id, []).append(rec)

                # Load seen trigger timestamps
                cur.execute(
                    "SELECT room_id, trigger_ts FROM dedup_seen_triggers WHERE created_at >= ?",
                    (cutoff,)
                )
                for room_id, trigger_ts in cur.fetchall():
                    self._seen_trigger_ts.setdefault(room_id, set()).add(trigger_ts)

                logger.info(
                    "🛡️ [Deduplicator] Loaded state from SQLite (%d rooms, %d active tasks)",
                    len(self._records),
                    sum(len(v) for v in self._records.values()),
                )
            except Exception as e:
                logger.error("Failed to load dedup records from SQLite: %s", e)

    def _cleanup_expired(self, room_id: str) -> None:
        """Removes records older than ttl_seconds for the given room."""
        now = time.time()
        cutoff = now - self.ttl_seconds
        if room_id in self._records:
            self._records[room_id] = [
                r for r in self._records[room_id]
                if (now - r.created_at) < self.ttl_seconds
            ]
        if self._conn:
            try:
                self._conn.execute(
                    "DELETE FROM dedup_tasks WHERE room_id = ? AND created_at < ?",
                    (room_id, cutoff)
                )
                self._conn.execute(
                    "DELETE FROM dedup_seen_triggers WHERE room_id = ? AND created_at < ?",
                    (room_id, cutoff)
                )
                self._conn.commit()
            except Exception as e:
                logger.debug("Failed to clean up expired dedup records in SQLite: %s", e)

    def is_duplicate(
        self,
        room_id: str,
        action_summary: str,
        trigger_ts: Optional[str] = None,
        force: bool = False,
    ) -> Tuple[bool, Optional[str]]:
        """Checks whether a proposed task is a duplicate of a recent task in this room.
        
        Args:
            room_id: Switch room ID.
            action_summary: Crisp action title generated by TriageAgent.
            trigger_ts: Optional timestamp or message ID that initiated this triage.
            force: If True, bypasses deduplication (e.g. explicitly commanded rerun).
            
        Returns:
            Tuple of (is_duplicate: bool, reason: Optional[str]).
        """
        if force:
            return False, None

        if not action_summary or not action_summary.strip():
            return True, "Empty task summary"

        with self._lock:
            self._cleanup_expired(room_id)

            # 1. Exact trigger message timestamp / ID deduplication
            if trigger_ts and room_id in self._seen_trigger_ts:
                if trigger_ts in self._seen_trigger_ts[room_id]:
                    return True, f"Trigger message ts '{trigger_ts}' was already processed into a task"

            candidate_tokens = normalize_tokens(action_summary)
            candidate_fp = compute_task_fingerprint(action_summary)
            now = time.time()

            records = self._records.get(room_id, [])

            # 2. Exact fingerprint check
            for rec in reversed(records):
                if rec.fingerprint == candidate_fp:
                    elapsed = int(now - rec.created_at)
                    return True, (
                        f"Identical task '{rec.action_summary}' was already triaged {elapsed}s ago "
                        f"(status: {rec.status})"
                    )

            # 3. Semantic token-similarity check (catches minor phrasing variations)
            for rec in reversed(records):
                sim = token_jaccard_similarity(candidate_tokens, rec.normalized_tokens)
                if sim >= self.similarity_threshold:
                    elapsed = int(now - rec.created_at)
                    return True, (
                        f"Semantic duplicate of '{rec.action_summary}' ({int(sim * 100)}% match, "
                        f"triaged {elapsed}s ago, status: {rec.status})"
                    )

            return False, None

    def record_task(
        self,
        room_id: str,
        action_summary: str,
        trigger_ts: Optional[str] = None,
        execution_type: str = "AUTO_EXECUTE",
        task_id: Optional[str] = None,
    ) -> str:
        """Records a new active task in the deduplication store and SQLite database.
        
        Returns:
            Canonical fingerprint hash string.
        """
        with self._lock:
            self._cleanup_expired(room_id)
            if room_id not in self._records:
                self._records[room_id] = []
            if room_id not in self._seen_trigger_ts:
                self._seen_trigger_ts[room_id] = set()

            tokens = normalize_tokens(action_summary)
            fp = compute_task_fingerprint(action_summary)
            tid = task_id or fp
            now = time.time()

            record = TaskRecord(
                task_id=tid,
                room_id=room_id,
                action_summary=action_summary,
                normalized_tokens=tokens,
                fingerprint=fp,
                trigger_ts=trigger_ts,
                execution_type=execution_type,
                status="HANDED_OFF",
                created_at=now,
                updated_at=now,
            )
            self._records[room_id].append(record)

            if trigger_ts:
                self._seen_trigger_ts[room_id].add(trigger_ts)

            if self._conn:
                try:
                    self._conn.execute(
                        """
                        INSERT OR REPLACE INTO dedup_tasks
                        (task_id, room_id, action_summary, normalized_tokens, fingerprint,
                         trigger_ts, execution_type, status, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            record.task_id,
                            record.room_id,
                            record.action_summary,
                            json.dumps(record.normalized_tokens),
                            record.fingerprint,
                            record.trigger_ts,
                            record.execution_type,
                            record.status,
                            record.created_at,
                            record.updated_at,
                        ),
                    )
                    if trigger_ts:
                        self._conn.execute(
                            """
                            INSERT OR REPLACE INTO dedup_seen_triggers
                            (room_id, trigger_ts, created_at)
                            VALUES (?, ?, ?)
                            """,
                            (room_id, trigger_ts, now),
                        )
                    self._conn.commit()
                except Exception as e:
                    logger.error("Failed to persist task record to SQLite: %s", e)

            logger.info(
                "🛡️ [Deduplicator] Recorded task: '%s' (fp=%s, room=%s)",
                action_summary, fp, room_id[:8]
            )
            return fp

    def record_seen_trigger(self, room_id: str, trigger_ts: str) -> None:
        """Records a trigger timestamp or message ID as processed."""
        if not trigger_ts:
            return
        with self._lock:
            if room_id not in self._seen_trigger_ts:
                self._seen_trigger_ts[room_id] = set()
            self._seen_trigger_ts[room_id].add(trigger_ts)
            if self._conn:
                try:
                    self._conn.execute(
                        """
                        INSERT OR REPLACE INTO dedup_seen_triggers
                        (room_id, trigger_ts, created_at)
                        VALUES (?, ?, ?)
                        """,
                        (room_id, trigger_ts, time.time()),
                    )
                    self._conn.commit()
                except Exception as e:
                    logger.error("Failed to persist seen trigger to SQLite: %s", e)

    def has_seen_trigger(self, room_id: str, trigger_ts: str) -> bool:
        """Checks if a trigger timestamp has already been processed."""
        if not trigger_ts:
            return False
        with self._lock:
            return trigger_ts in self._seen_trigger_ts.get(room_id, set())

    def mark_completed(
        self,
        room_id: str,
        action_summary: Optional[str] = None,
        fingerprint: Optional[str] = None,
    ) -> bool:
        """Marks a task as completed once worker finishes artifact drafting or Todoist sync."""
        with self._lock:
            if room_id not in self._records:
                return False

            target_fp = fingerprint or (compute_task_fingerprint(action_summary) if action_summary else None)
            if not target_fp:
                return False

            found = False
            now = time.time()
            for rec in self._records[room_id]:
                if rec.fingerprint == target_fp:
                    rec.status = "COMPLETED"
                    rec.updated_at = now
                    found = True
                    logger.info("🛡️ [Deduplicator] Marked task as COMPLETED: '%s'", rec.action_summary)

            if found and self._conn:
                try:
                    self._conn.execute(
                        """
                        UPDATE dedup_tasks
                        SET status = 'COMPLETED', updated_at = ?
                        WHERE room_id = ? AND fingerprint = ?
                        """,
                        (now, room_id, target_fp),
                    )
                    self._conn.commit()
                except Exception as e:
                    logger.error("Failed to update task completion in SQLite: %s", e)

            return found

    def get_active_task_summaries(self, room_id: str) -> List[str]:
        """Returns the list of active task action summaries in this room for prompt context injection."""
        with self._lock:
            self._cleanup_expired(room_id)
            records = self._records.get(room_id, [])
            return [r.action_summary for r in records[-10:]]

    def clear(self, room_id: Optional[str] = None) -> None:
        """Clears records from memory and SQLite (useful for testing)."""
        with self._lock:
            if room_id:
                self._records.pop(room_id, None)
                self._seen_trigger_ts.pop(room_id, None)
                if self._conn:
                    try:
                        self._conn.execute("DELETE FROM dedup_tasks WHERE room_id = ?", (room_id,))
                        self._conn.execute("DELETE FROM dedup_seen_triggers WHERE room_id = ?", (room_id,))
                        self._conn.commit()
                    except Exception as e:
                        logger.error("Failed to clear room from SQLite: %s", e)
            else:
                self._records.clear()
                self._seen_trigger_ts.clear()
                if self._conn:
                    try:
                        self._conn.execute("DELETE FROM dedup_tasks")
                        self._conn.execute("DELETE FROM dedup_seen_triggers")
                        self._conn.commit()
                    except Exception as e:
                        logger.error("Failed to clear dedup tables in SQLite: %s", e)

    def close(self) -> None:
        """Closes the underlying SQLite connection if open."""
        with self._lock:
            if self._conn:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None


class _RoomBufferView:
    """Deque-compatible proxy view for a specific room's message buffer."""

    def __init__(self, store: "RoomBufferStore", room_id: str):
        self._store = store
        self._room_id = room_id

    def append(self, message: SlackMessage) -> None:
        self._store.append_message(self._room_id, message)

    def extend(self, messages: Iterable[SlackMessage]) -> None:
        for msg in messages:
            self._store.append_message(self._room_id, msg)

    def __iter__(self):
        return iter(self._store.get_buffer(self._room_id))

    def __len__(self) -> int:
        return len(self._store.get_buffer(self._room_id))

    def __getitem__(self, index):
        return self._store.get_buffer(self._room_id)[index]

    def clear(self) -> None:
        self._store.clear(self._room_id)


class RoomBufferStore:
    """Persistent, thread-safe sliding message buffer per room backed by SQLite.
    
    Provides rolling conversation context for triage and worker reasoning.
    Preserves recent context across daemon restarts and WebSocket reconnects.
    Supports dictionary/deque access: room_buffers[room_id].append(msg_obj)
    """

    def __init__(
        self,
        storage_path: Optional[str] = None,
        max_len: int = 15,
    ):
        self.storage_path = TASKFLOW_STATE_DB if storage_path is None else storage_path
        self.max_len = max_len
        self._lock = threading.Lock()
        self._buffers: Dict[str, Deque[SlackMessage]] = defaultdict(lambda: deque(maxlen=self.max_len))
        self._conn: Optional[sqlite3.Connection] = None

        if self.storage_path:
            dir_name = os.path.dirname(os.path.abspath(self.storage_path))
            if dir_name:
                os.makedirs(dir_name, exist_ok=True)
            self._conn = sqlite3.connect(self.storage_path, check_same_thread=False)
            self._init_db()

    def _init_db(self) -> None:
        if not self._conn:
            return
        with self._lock:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS room_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    room_id TEXT NOT NULL,
                    user_id TEXT,
                    user_name TEXT,
                    text TEXT NOT NULL,
                    ts TEXT NOT NULL,
                    permalink TEXT,
                    created_at REAL NOT NULL
                );
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_room_messages_room_id ON room_messages (room_id, id DESC);"
            )
            self._conn.commit()

    def _ensure_loaded(self, room_id: str) -> None:
        """Loads recent messages from SQLite into the in-memory deque if not yet loaded."""
        if room_id in self._buffers:
            return

        dq: Deque[SlackMessage] = deque(maxlen=self.max_len)
        if self._conn:
            try:
                cur = self._conn.cursor()
                cur.execute(
                    """
                    SELECT user_id, user_name, text, ts, permalink
                    FROM (
                        SELECT id, user_id, user_name, text, ts, permalink
                        FROM room_messages
                        WHERE room_id = ?
                        ORDER BY id DESC
                        LIMIT ?
                    )
                    ORDER BY id ASC
                    """,
                    (room_id, self.max_len)
                )
                rows = cur.fetchall()
                for row in rows:
                    dq.append(
                        SlackMessage(
                            user_id=row[0] or "",
                            user_name=row[1] or "",
                            text=row[2] or "",
                            ts=str(row[3] or ""),
                            permalink=row[4],
                        )
                    )
            except Exception as e:
                logger.error("Failed to load room messages from SQLite: %s", e)

        self._buffers[room_id] = dq

    def append_message(self, room_id: str, message: SlackMessage) -> None:
        """Appends a message to the room's rolling window and persists it to SQLite."""
        room_key = room_id or "default_room"
        with self._lock:
            self._ensure_loaded(room_key)
            self._buffers[room_key].append(message)

            if self._conn:
                try:
                    self._conn.execute(
                        """
                        INSERT INTO room_messages (room_id, user_id, user_name, text, ts, permalink, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            room_key,
                            message.user_id,
                            message.user_name,
                            message.text,
                            str(message.ts),
                            message.permalink,
                            time.time(),
                        ),
                    )
                    self._conn.commit()
                except Exception as e:
                    logger.error("Failed to persist message to SQLite: %s", e)

    def get_buffer(self, room_id: str, limit: Optional[int] = None) -> List[SlackMessage]:
        """Returns recent messages in the room in chronological order."""
        room_key = room_id or "default_room"
        with self._lock:
            self._ensure_loaded(room_key)
            messages = list(self._buffers[room_key])
            if limit is not None:
                return messages[-limit:]
            return messages

    def clear(self, room_id: Optional[str] = None) -> None:
        """Clears messages from buffer and SQLite."""
        with self._lock:
            if room_id:
                self._buffers.pop(room_id, None)
                if self._conn:
                    try:
                        self._conn.execute("DELETE FROM room_messages WHERE room_id = ?", (room_id,))
                        self._conn.commit()
                    except Exception as e:
                        logger.error("Failed to clear room messages in SQLite: %s", e)
            else:
                self._buffers.clear()
                if self._conn:
                    try:
                        self._conn.execute("DELETE FROM room_messages")
                        self._conn.commit()
                    except Exception as e:
                        logger.error("Failed to clear all room messages in SQLite: %s", e)

    def close(self) -> None:
        """Closes the underlying SQLite connection if open."""
        with self._lock:
            if self._conn:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None

    def __getitem__(self, room_id: str) -> _RoomBufferView:
        """Provides backward-compatible deque-like access: room_buffers[room_id].append(msg)."""
        return _RoomBufferView(self, room_id or "default_room")

    def __contains__(self, room_id: str) -> bool:
        room_key = room_id or "default_room"
        with self._lock:
            self._ensure_loaded(room_key)
            return len(self._buffers.get(room_key, [])) > 0


# Default shared singletons for daemon runtime
default_task_deduplicator = TaskDeduplicator()
default_room_buffer_store = RoomBufferStore()

__all__ = [
    "TaskRecord",
    "TaskDeduplicator",
    "RoomBufferStore",
    "default_task_deduplicator",
    "default_room_buffer_store",
    "normalize_tokens",
    "compute_task_fingerprint",
    "token_jaccard_similarity",
]
