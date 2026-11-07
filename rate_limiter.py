"""Per-Room Rate Limiting & LLM Cost DoS Guardrail for TaskFlow.

Protects against excessive LLM API expenditures, runaway bot loops,
and malicious/adversarial room flooding attacks by enforcing sliding-window
request quotas per Switch room.
"""

import logging
import math
import os
import threading
import time
from collections import defaultdict, deque
from typing import Any, Deque, Dict, Optional, Tuple

try:
    from config import (
        ROOM_RATE_LIMIT_ENABLED,
        ROOM_RATE_LIMIT_REQUESTS,
        ROOM_RATE_LIMIT_WINDOW_SECONDS,
    )
except ImportError:
    ROOM_RATE_LIMIT_ENABLED = os.getenv("ROOM_RATE_LIMIT_ENABLED", "true").strip().lower() in ("true", "1", "yes")
    ROOM_RATE_LIMIT_REQUESTS = int(os.getenv("ROOM_RATE_LIMIT_REQUESTS", "15").strip() or 15)
    ROOM_RATE_LIMIT_WINDOW_SECONDS = float(os.getenv("ROOM_RATE_LIMIT_WINDOW_SECONDS", "60.0").strip() or 60.0)

logger = logging.getLogger("RoomRateLimiter")


class RoomRateLimiter:
    """Thread-safe, sliding-window rate limiter per Switch room.
    
    Prevents LLM cost DoS by constraining the number of LLM operations permitted
    within a rolling time window per room.
    """

    def __init__(
        self,
        max_requests: int = ROOM_RATE_LIMIT_REQUESTS,
        window_seconds: float = ROOM_RATE_LIMIT_WINDOW_SECONDS,
        enabled: bool = ROOM_RATE_LIMIT_ENABLED,
    ):
        """
        Args:
            max_requests: Maximum allowed LLM requests per room within window_seconds.
            window_seconds: Duration of the rolling window in seconds (default 60s).
            enabled: Master toggle to enable/disable rate limiting.
        """
        self.max_requests = max(1, max_requests)
        self.window_seconds = max(0.05, float(window_seconds))
        self.enabled = enabled
        self._lock = threading.Lock()
        # room_id -> deque of request timestamps (oldest first)
        self._room_timestamps: Dict[str, Deque[float]] = defaultdict(deque)

    def _prune_expired(self, room_id: str, now: float) -> None:
        """Removes timestamps outside the current sliding window for the room."""
        cutoff = now - self.window_seconds
        dq = self._room_timestamps.get(room_id)
        if dq:
            while dq and dq[0] <= cutoff:
                dq.popleft()
            if not dq:
                self._room_timestamps.pop(room_id, None)

    def check(self, room_id: str) -> Tuple[bool, int, Optional[str]]:
        """Inspects whether a request would be permitted without consuming quota.
        
        Returns:
            Tuple of (allowed: bool, retry_after_seconds: int, reason: Optional[str]).
        """
        if not self.enabled:
            return True, 0, None

        room_key = room_id or "default_room"
        now = time.time()

        with self._lock:
            self._prune_expired(room_key, now)
            dq = self._room_timestamps.get(room_key)
            current_count = len(dq) if dq else 0

            if current_count >= self.max_requests:
                oldest_ts = dq[0]
                retry_after = max(1, math.ceil(oldest_ts + self.window_seconds - now))
                reason = (
                    f"Room rate limit exceeded: {current_count}/{self.max_requests} requests "
                    f"used in rolling {int(self.window_seconds)}s window. Retry in {retry_after}s."
                )
                return False, retry_after, reason

            return True, 0, None

    def acquire(self, room_id: str, cost: int = 1) -> Tuple[bool, int, Optional[str]]:
        """Attempts to consume quota for an LLM request in the specified room.
        
        Args:
            room_id: Switch room identifier.
            cost: Number of request units to consume (default 1).
            
        Returns:
            Tuple of (allowed: bool, retry_after_seconds: int, reason: Optional[str]).
        """
        if not self.enabled:
            return True, 0, None

        room_key = room_id or "default_room"
        now = time.time()

        with self._lock:
            self._prune_expired(room_key, now)
            dq = self._room_timestamps[room_key]
            current_count = len(dq)

            if current_count + cost > self.max_requests:
                oldest_ts = dq[0] if dq else now
                retry_after = max(1, math.ceil(oldest_ts + self.window_seconds - now))
                reason = (
                    f"Room rate limit exceeded (LLM Cost DoS protection): "
                    f"{current_count}/{self.max_requests} requests used in {int(self.window_seconds)}s window. "
                    f"Retry in {retry_after}s."
                )
                logger.warning(
                    "🛑 [RateLimiter] Throttled room [%s]: %d/%d requests used. Retry after %ds.",
                    room_key[:8], current_count, self.max_requests, retry_after
                )
                return False, retry_after, reason

            for _ in range(cost):
                dq.append(now)

            logger.debug(
                "🛡️ [RateLimiter] Acquired quota for room [%s]: %d/%d used.",
                room_key[:8], len(dq), self.max_requests
            )
            return True, 0, None

    def check_and_acquire(self, room_id: str, cost: int = 1) -> Tuple[bool, int, Optional[str]]:
        """Convenience alias for acquire()."""
        return self.acquire(room_id, cost=cost)

    def get_usage(self, room_id: str) -> Dict[str, Any]:
        """Returns diagnostic usage statistics for the given room."""
        room_key = room_id or "default_room"
        now = time.time()
        with self._lock:
            self._prune_expired(room_key, now)
            dq = self._room_timestamps.get(room_key)
            count = len(dq) if dq else 0
            reset_in = max(0, math.ceil(dq[0] + self.window_seconds - now)) if (dq and count >= self.max_requests) else 0

            return {
                "room_id": room_key,
                "enabled": self.enabled,
                "current_requests": count,
                "max_requests": self.max_requests,
                "window_seconds": self.window_seconds,
                "remaining_requests": max(0, self.max_requests - count),
                "retry_after_seconds": reset_in,
            }

    def reset(self, room_id: Optional[str] = None) -> None:
        """Resets rate limiting state (useful for unit tests or manual overrides)."""
        with self._lock:
            if room_id:
                self._room_timestamps.pop(room_id or "default_room", None)
            else:
                self._room_timestamps.clear()


# Default shared singleton instance
default_room_rate_limiter = RoomRateLimiter()

__all__ = [
    "RoomRateLimiter",
    "default_room_rate_limiter",
]
