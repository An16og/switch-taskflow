"""Native Switch Room Task & Job Registry for TaskFlow.

Keeps all tasks, action checklists, and assignments natively tracked inside the
Switch workroom, ensuring work stays in the room regardless of external sync services.
"""

import os
import json
import time
import uuid
import logging
from enum import Enum
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import List, Dict, Any, Optional

from models import format_deadline_with_timezone

logger = logging.getLogger("RoomTaskStore")


class TaskStatus(str, Enum):
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


@dataclass
class RoomTask:
    """A native task tracked inside a Switch room."""
    task_id: str
    room_id: str
    title: str
    assignee_name: str
    assignee_id: Optional[str] = None
    assigner_name: str = "Team Member"
    assigner_id: Optional[str] = None
    deadline: Optional[str] = None
    status: TaskStatus = TaskStatus.PENDING
    execution_type: str = "HUMAN_TODO"
    action_plan: str = ""
    context_notes: Optional[str] = None
    thread_id: Optional[str] = None
    todoist_url: Optional[str] = None
    todoist_error: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value if isinstance(self.status, TaskStatus) else str(self.status)
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RoomTask":
        clean_data = dict(data)
        if "status" in clean_data and isinstance(clean_data["status"], str):
            try:
                clean_data["status"] = TaskStatus(clean_data["status"])
            except ValueError:
                clean_data["status"] = TaskStatus.PENDING
        return cls(**clean_data)

    def format_receipt(self) -> str:
        """Formats a human-readable room delivery receipt with native ID and sync status."""
        lines = [
            f"🎯 **Task Tracked in Switch Room**",
            f"• **Task ID:** `{self.task_id}`",
            f"• **Task:** {self.title}",
            f"• **Assignee:** @{self.assignee_name}",
            f"• **Assigned by:** @{self.assigner_name}",
            f"• **Due Date:** {format_deadline_with_timezone(self.deadline) or 'Not specified'}",
            f"• **Status:** `{self.status.value}` (Native Switch Workroom)",
        ]

        if self.todoist_url:
            lines.append(f"• **External Sync:** [View in Todoist]({self.todoist_url})")
        elif self.todoist_error:
            lines.append(f"• ⚠️ **External Sync Notice:** {self.todoist_error}")
        else:
            lines.append("• **External Sync:** Native room only (Todoist unconfigured)")

        if self.action_plan:
            lines.append(f"\n📋 **Action Checklist:**\n{self.action_plan}")

        return "\n".join(lines)


class RoomTaskStore:
    """Persistent storage for native Switch room tasks."""

    def __init__(self, storage_path: str = "room_tasks.json"):
        self.storage_path = Path(storage_path)
        self._tasks: Dict[str, RoomTask] = {}
        self._load()

    def _load(self):
        if self.storage_path.exists():
            try:
                data = json.loads(self.storage_path.read_text(encoding="utf-8"))
                for item in data.get("tasks", []):
                    task = RoomTask.from_dict(item)
                    self._tasks[task.task_id] = task
                logger.info("Loaded %d native room tasks from %s", len(self._tasks), self.storage_path.name)
            except Exception as e:
                logger.warning("Failed loading room tasks from %s: %s", self.storage_path, e)

    def _save(self):
        try:
            payload = {
                "tasks": [t.to_dict() for t in self._tasks.values()],
                "updated_at": time.time(),
            }
            self.storage_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except Exception as e:
            logger.warning("Failed saving room tasks to %s: %s", self.storage_path, e)

    def create_task(
        self,
        title: str,
        room_id: str,
        assignee_name: str,
        assignee_id: Optional[str] = None,
        assigner_name: str = "Team Member",
        assigner_id: Optional[str] = None,
        deadline: Optional[str] = None,
        execution_type: str = "HUMAN_TODO",
        action_plan: str = "",
        context_notes: Optional[str] = None,
        thread_id: Optional[str] = None,
        todoist_url: Optional[str] = None,
        todoist_error: Optional[str] = None,
    ) -> RoomTask:
        """Creates and records a native task in the room task store."""
        task_id = f"sw_task_{uuid.uuid4().hex[:8]}"
        task = RoomTask(
            task_id=task_id,
            room_id=str(room_id) if room_id is not None else "default_room",
            title=str(title) if title is not None else "Untitled Task",
            assignee_name=str(assignee_name) if assignee_name is not None else "Team",
            assignee_id=str(assignee_id) if assignee_id is not None else None,
            assigner_name=str(assigner_name) if assigner_name is not None else "Team Member",
            assigner_id=str(assigner_id) if assigner_id is not None else None,
            deadline=str(deadline) if deadline is not None else None,
            status=TaskStatus.PENDING,
            execution_type=str(execution_type) if execution_type is not None else "HUMAN_TODO",
            action_plan=str(action_plan) if action_plan is not None else "",
            context_notes=str(context_notes) if context_notes is not None else None,
            thread_id=str(thread_id) if thread_id is not None else None,
            todoist_url=str(todoist_url) if todoist_url is not None else None,
            todoist_error=str(todoist_error) if todoist_error is not None else None,
        )
        self._tasks[task_id] = task
        self._save()
        logger.info("Recorded native Switch room task '%s' (%s) in room %s", title, task_id, room_id)
        return task

    def get_task(self, task_id: str) -> Optional[RoomTask]:
        return self._tasks.get(task_id)

    def list_tasks(
        self,
        room_id: Optional[str] = None,
        status: Optional[TaskStatus] = None,
        assignee: Optional[str] = None,
    ) -> List[RoomTask]:
        """Lists tasks filtered by room, status, or assignee."""
        tasks = list(self._tasks.values())
        if room_id:
            tasks = [t for t in tasks if t.room_id == room_id]
        if status:
            tasks = [t for t in tasks if t.status == status]
        if assignee:
            clean_assignee = assignee.lower().lstrip("@")
            tasks = [t for t in tasks if clean_assignee in (t.assignee_name.lower(), (t.assignee_id or "").lower())]
        tasks.sort(key=lambda t: t.created_at, reverse=True)
        return tasks

    def update_task_status(self, task_id: str, status: TaskStatus) -> Optional[RoomTask]:
        task = self.get_task(task_id)
        if task:
            task.status = status
            task.updated_at = time.time()
            self._save()
            logger.info("Updated native task '%s' status -> %s", task_id, status.value)
            return task
        return None


# Default global store
default_room_task_store = RoomTaskStore()
