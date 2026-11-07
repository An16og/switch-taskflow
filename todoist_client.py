"""Todoist REST API v1 Client with auto-retry and clear 4xx diagnostics.

Provides optional external sync for tasks.
TaskFlow's primary task tracker remains native inside the Switch Workroom;
Todoist acts purely as an optional sync target.
"""

import logging
import requests
from dataclasses import dataclass
from typing import Optional, Dict, Any

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


class TodoistClient:
    """Client for Todoist API v1 supporting optional sync and 4xx diagnostics."""

    def __init__(self, api_token: Optional[str] = None):
        if api_token is not None:
            self.api_token = api_token
        else:
            self.api_token = TODOIST_API_TOKEN
        self.headers = {
            "Authorization": f"Bearer {self.api_token}",
            "Content-Type": "application/json"
        }
        self.last_error: Optional[str] = None
        self.last_status_code: Optional[int] = None

    def sync_task(
        self,
        content: str,
        description: str = "",
        due_string: Optional[str] = None
    ) -> TodoistSyncResult:
        """
        Synchronizes a task with Todoist API v1.
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

        payload = {
            "content": content,
            "description": description
        }

        # Filter out invalid or vague due_strings
        if due_string and str(due_string).strip().lower() not in ("none", "null", "asap", "n/a"):
            payload["due_string"] = str(due_string).strip()

        try:
            response = requests.post(
                TODOIST_API_URL,
                headers=self.headers,
                json=payload,
                timeout=10
            )

            # If 400 due to invalid date format, retry without due_string
            if response.status_code == 400 and "date format" in response.text.lower():
                logger.warning("Todoist rejected due_string '%s'. Retrying without due_string...", due_string)
                payload.pop("due_string", None)
                response = requests.post(
                    TODOIST_API_URL,
                    headers=self.headers,
                    json=payload,
                    timeout=10
                )

            # Handle 4xx Client Errors with clear diagnostic messages
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
                    is_client_error=True
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
                    is_client_error=False
                )

            task_data = response.json()
            task_id = task_data.get("id")
            task_url = task_data.get("url") or (f"https://todoist.com/app/task/{task_id}" if task_id else None)
            logger.info("✅ Successfully synced task to Todoist: %s (%s)", task_id, content)
            return TodoistSyncResult(
                success=True,
                task_id=task_id,
                task_url=task_url,
                status_code=response.status_code
            )

        except requests.exceptions.RequestException as e:
            err_msg = f"Network exception communicating with Todoist API: {e}. Task remains tracked natively in Switch Workroom."
            self.last_error = err_msg
            logger.warning("⚠️ [Todoist Sync Exception] %s", err_msg)
            return TodoistSyncResult(
                success=False,
                error_message=err_msg,
                is_client_error=False
            )

    def create_task(
        self,
        content: str,
        description: str = "",
        due_string: Optional[str] = None
    ) -> Optional[dict]:
        """
        Backwards-compatible wrapper around sync_task.
        Returns dict containing task data on success, or None on failure (storing last_error).
        """
        result = self.sync_task(content=content, description=description, due_string=due_string)
        if result.success and result.task_id:
            return {
                "id": result.task_id,
                "url": result.task_url or f"https://todoist.com/app/task/{result.task_id}",
                "content": content,
                "description": description,
            }
        return None
