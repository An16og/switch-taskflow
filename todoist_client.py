import logging
import requests
from typing import Optional
from config import TODOIST_API_TOKEN

logger = logging.getLogger(__name__)

TODOIST_API_URL = "https://api.todoist.com/api/v1/tasks"

class TodoistClient:
    def __init__(self, api_token: Optional[str] = None):
        self.api_token = api_token or TODOIST_API_TOKEN
        self.headers = {
            "Authorization": f"Bearer {self.api_token}",
            "Content-Type": "application/json"
        }

    def create_task(
        self,
        content: str,
        description: str = "",
        due_string: Optional[str] = None
    ) -> Optional[dict]:
        """
        Creates a task in Todoist.
        - content: Task title (action summary)
        - description: Assigner details + context + action plan
        - due_string: Natural language date or ISO timestamp (e.g. 'Tomorrow 5pm')
        """
        if not self.api_token:
            logger.error("Todoist API token is missing.")
            return None

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
                logger.warning(f"Todoist rejected due_string '{due_string}'. Retrying without due_string...")
                payload.pop("due_string", None)
                response = requests.post(
                    TODOIST_API_URL,
                    headers=self.headers,
                    json=payload,
                    timeout=10
                )

            response.raise_for_status()
            task_data = response.json()
            logger.info(f"Successfully created Todoist task: {task_data.get('id')} - {content}")
            return task_data
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to create Todoist task: {e}")
            if hasattr(e, 'response') and e.response is not None:
                logger.error(f"Response status: {e.response.status_code}, body: {e.response.text}")
            return None
