"""Todoist REST API v1 Client with idempotency, auto-retry on 429, and clear 4xx diagnostics.

Provides optional external sync for tasks.
TaskFlow's primary task tracker remains native inside the Switch Workroom;
Todoist acts purely as an optional sync target.
"""

import hashlib
import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Optional, Dict, Any, Callable

import requests
from config import TODOIST_API_TOKEN

logger = logging.getLogger("TodoistClient")

TODOIST_API_URL = "https://api.todoist.com/api/v1/tasks"


@dataclass
class TodoistSyncResult:
    """Detailed result metadata for Todoist task synchronization."""
    success: bool
    task_id: Optional[str] = None
    task_url: Optional[str] = None
    status_code: Optional[int] = None
    error_message: Optional[str] = None
    is_client_error: bool = False
    idempotency_key: Optional[str] = None
    retries_attempted: int = 0


class TodoistClient:
    """Client for Todoist API v1 supporting optional sync, idempotency, 429 retries, and 4xx diagnostics."""

    def __init__(
        self,
        api_token: Optional[str] = None,
        max_retries: int = 3,
        base_backoff: float = 0.05,
        sleep_fn: Optional[Callable[[float], None]] = None,
    ):
        if api_token is not None:
            self.api_token = api_token
        else:
            self.api_token = TODOIST_API_TOKEN
        self.headers = {
            "Authorization": f"Bearer {self.api_token}",
            "Content-Type": "application/json"
        }
        self.max_retries = max_retries
        self.base_backoff = base_backoff
        self._sleep_fn = sleep_fn or time.sleep
        self.last_error: Optional[str] = None
        self.last_status_code: Optional[int] = None
        # In-memory idempotency cache mapping idempotency_key/external_id -> TodoistSyncResult
        self._idempotency_cache: Dict[str, TodoistSyncResult] = {}

    def compute_idempotency_key(
        self,
        content: str,
        description: str = "",
        due_string: Optional[str] = None,
        external_id: Optional[str] = None,
    ) -> str:
        """Computes a deterministic UUID v5 idempotency key from external_id or normalized task content."""
        if external_id and str(external_id).strip():
            seed = f"ext:{str(external_id).strip()}"
        else:
            norm_content = content.strip()
            norm_desc = description.strip()
            norm_due = str(due_string or "").strip().lower()
            seed = f"content:{norm_content}|{norm_desc}|{norm_due}"
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"https://api.todoist.com/tasks/{seed}"))

    def sync_task(
        self,
        content: str,
        description: str = "",
        due_string: Optional[str] = None,
        external_id: Optional[str] = None,
    ) -> TodoistSyncResult:
        """Synchronizes a task with Todoist API v1 with idempotency (X-Request-Id / external_id hash)

        and automatic exponential backoff retry on HTTP 429 (Too Many Requests).
        Returns a TodoistSyncResult with detailed success/failure metadata.
        """
        self.last_error = None
        self.last_status_code = None

        if not self.api_token:
            msg = "Todoist API token is not configured (optional external sync disabled). Task remains tracked natively in Switch Workroom."
            logger.debug(msg)
            return TodoistSyncResult(
                success=False,
                error_message=msg,
                is_client_error=False
            )

        idempotency_key = self.compute_idempotency_key(
            content=content,
            description=description,
            due_string=due_string,
            external_id=external_id,
        )

        # 1. Check in-memory idempotency cache to prevent duplicate network calls
        if idempotency_key in self._idempotency_cache:
            cached = self._idempotency_cache[idempotency_key]
            logger.info("ℹ️ [Todoist Idempotency] Task already synced for key %s -> task_id=%s", idempotency_key, cached.task_id)
            return cached
        if external_id and str(external_id).strip() in self._idempotency_cache:
            cached = self._idempotency_cache[str(external_id).strip()]
            logger.info("ℹ️ [Todoist Idempotency] Task already synced for external_id %s -> task_id=%s", external_id, cached.task_id)
            return cached

        # 2. Build headers with official Todoist X-Request-Id idempotency header
        request_headers = dict(self.headers)
        request_headers["X-Request-Id"] = idempotency_key

        payload = {
            "content": content,
            "description": description
        }

        # Filter out invalid or vague due_strings
        if due_string and str(due_string).strip().lower() not in ("none", "null", "asap", "n/a"):
            payload["due_string"] = str(due_string).strip()

        retries_attempted = 0
        response = None

        # 3. Execution loop with 429 retry and 400 invalid-date fallback
        for attempt in range(self.max_retries + 1):
            try:
                response = requests.post(
                    TODOIST_API_URL,
                    headers=request_headers,
                    json=payload,
                    timeout=10
                )
            except requests.exceptions.RequestException as e:
                # Retry on network error if attempts remain
                if attempt < self.max_retries:
                    retries_attempted += 1
                    delay = self.base_backoff * (2 ** attempt)
                    logger.warning("⚠️ Network error syncing to Todoist (%s). Retrying in %.2fs (attempt %d/%d)...", e, delay, attempt + 1, self.max_retries)
                    self._sleep_fn(delay)
                    continue
                err_msg = f"Network exception communicating with Todoist API: {e}. Task remains tracked natively in Switch Workroom."
                self.last_error = err_msg
                logger.warning("⚠️ [Todoist Sync Exception] %s", err_msg)
                return TodoistSyncResult(
                    success=False,
                    error_message=err_msg,
                    is_client_error=False,
                    idempotency_key=idempotency_key,
                    retries_attempted=retries_attempted,
                )

            # If 400 due to invalid date format, retry without due_string
            if response.status_code == 400 and "date format" in response.text.lower() and "due_string" in payload:
                logger.warning("Todoist rejected due_string '%s'. Retrying without due_string...", due_string)
                payload.pop("due_string", None)
                continue

            # If 429 Too Many Requests, retry with Retry-After or exponential backoff
            if response.status_code == 429:
                if attempt < self.max_retries:
                    retries_attempted += 1
                    delay = self.base_backoff * (2 ** attempt)
                    if hasattr(response, "headers") and hasattr(response.headers, "get"):
                        hdr_val = response.headers.get("Retry-After")
                        if isinstance(hdr_val, (int, float, str)):
                            try:
                                delay = max(0.001, float(hdr_val))
                            except (ValueError, TypeError):
                                pass

                    logger.warning(
                        "⚠️ [Todoist Rate Limit] HTTP 429 received. Retrying in %.3fs (attempt %d/%d)...",
                        delay,
                        attempt + 1,
                        self.max_retries,
                    )
                    self._sleep_fn(delay)
                    continue
                else:
                    # Retries exhausted
                    self.last_status_code = 429
                    err_msg = (
                        f"HTTP 429 Too Many Requests: Todoist rate limit exceeded (retried {self.max_retries} times). "
                        f"Task remains tracked natively in Switch Workroom."
                    )
                    self.last_error = err_msg
                    logger.warning("⚠️ [Todoist Sync Failed] %s", err_msg)
                    return TodoistSyncResult(
                        success=False,
                        status_code=429,
                        error_message=err_msg,
                        is_client_error=True,
                        idempotency_key=idempotency_key,
                        retries_attempted=retries_attempted,
                    )

            # Success or other terminal status: exit retry loop
            break

        if response is None:
            err_msg = "No response received from Todoist API. Task remains tracked natively in Switch Workroom."
            self.last_error = err_msg
            return TodoistSyncResult(success=False, error_message=err_msg, is_client_error=False)

        # Handle 4xx Client Errors
        if 400 <= response.status_code < 500:
            self.last_status_code = response.status_code
            if response.status_code == 401:
                err_msg = (
                    "HTTP 401 Unauthorized: Invalid or expired TODOIST_API_TOKEN. "
                    "Task remains tracked natively in Switch Workroom."
                )
            elif response.status_code == 400:
                err_msg = (
                    f"HTTP 400 Bad Request: Todoist rejected task payload ({response.text.strip()}). "
                    "Task remains tracked natively in Switch Workroom."
                )
            elif response.status_code == 403:
                err_msg = (
                    f"HTTP 403 Forbidden: Account permissions insufficient ({response.text.strip()}). "
                    "Task remains tracked natively in Switch Workroom."
                )
            elif response.status_code == 429:
                err_msg = (
                    "HTTP 429 Too Many Requests: Todoist rate limit exceeded. "
                    "Task remains tracked natively in Switch Workroom."
                )
            else:
                err_msg = (
                    f"HTTP {response.status_code} Client Error: {response.text.strip()}. "
                    "Task remains tracked natively in Switch Workroom."
                )

            self.last_error = err_msg
            logger.warning("⚠️ [Todoist Sync Failed] %s", err_msg)
            return TodoistSyncResult(
                success=False,
                status_code=response.status_code,
                error_message=err_msg,
                is_client_error=True,
                idempotency_key=idempotency_key,
                retries_attempted=retries_attempted,
            )

        # Handle 5xx Server Errors
        if response.status_code >= 500:
            self.last_status_code = response.status_code
            err_msg = f"HTTP {response.status_code} Server Error from Todoist API. Task remains tracked natively in Switch Workroom."
            self.last_error = err_msg
            logger.error("🚨 [Todoist Server Error] %s", err_msg)
            return TodoistSyncResult(
                success=False,
                status_code=response.status_code,
                error_message=err_msg,
                is_client_error=False,
                idempotency_key=idempotency_key,
                retries_attempted=retries_attempted,
            )

        task_data = response.json()
        task_id = task_data.get("id")
        task_url = task_data.get("url") or (f"https://todoist.com/app/task/{task_id}" if task_id else None)
        logger.info("✅ Successfully synced task to Todoist: %s (%s)", task_id, content)

        res = TodoistSyncResult(
            success=True,
            task_id=task_id,
            task_url=task_url,
            status_code=response.status_code,
            idempotency_key=idempotency_key,
            retries_attempted=retries_attempted,
        )
        # Cache for idempotency
        self._idempotency_cache[idempotency_key] = res
        if external_id and str(external_id).strip():
            self._idempotency_cache[str(external_id).strip()] = res
        return res

    def create_task(
        self,
        content: str,
        description: str = "",
        due_string: Optional[str] = None,
        external_id: Optional[str] = None,
    ) -> Optional[dict]:
        """Backwards-compatible wrapper around sync_task with idempotency and 429 retry."""
        result = self.sync_task(
            content=content,
            description=description,
            due_string=due_string,
            external_id=external_id,
        )
        if result.success and result.task_id:
            return {
                "id": result.task_id,
                "url": result.task_url or f"https://todoist.com/app/task/{result.task_id}",
                "content": content,
                "description": description,
                "idempotency_key": result.idempotency_key,
            }
        return None
