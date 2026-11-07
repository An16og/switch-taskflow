"""Persistent Pending Approval Store for TaskFlow.

Resolves Slack Block Kit 2,000-character payload limits:
Instead of encoding full artifacts (release notes, postmortems, checklists)
directly into interactive button 'value' fields, artifacts are persisted
server-side and referenced by a lightweight approval ID (~17 chars).
"""

import json
import logging
import os
import time
import uuid
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Any
from pydantic import BaseModel, Field

logger = logging.getLogger("PendingApprovalStore")


class ApprovalStatus(str, Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    DISMISSED = "DISMISSED"
    EXPIRED = "EXPIRED"


class PendingApproval(BaseModel):
    """Full representation of a pending task or artifact approval."""
    approval_id: str = Field(description="Short unique ID placed in button payload, e.g. 'appr_8f3a1b2c4d5e'")
    action_type: str = Field(default="AUTO_EXECUTE", description="AUTO_EXECUTE, HUMAN_TODO, or ASSIGNMENT")
    action_summary: str = Field(description="Action title or task summary")
    artifact_title: str = Field(default="", description="Artifact title, e.g. 'Release Notes v1.3'")
    artifact_content: str = Field(default="", description="Full unabridged Markdown artifact (no 2000-char limit)")
    assignee_id: Optional[str] = None
    assignee_name: Optional[str] = None
    assigner_id: Optional[str] = None
    assigner_name: Optional[str] = None
    category: str = "General Task"
    deadline: Optional[str] = None
    context_notes: Optional[str] = None
    permalink: Optional[str] = None
    channel_id: Optional[str] = None
    message_ts: Optional[str] = None
    room_id: Optional[str] = None
    switch_doc_id: Optional[str] = None
    status: ApprovalStatus = ApprovalStatus.PENDING
    created_at: float = Field(default_factory=time.time)
    resolved_at: Optional[float] = None
    resolved_by: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class PendingApprovalStore:
    """Thread-safe persistent storage for pending task approvals and artifacts."""

    def __init__(self, storage_path: Optional[str] = None, ttl_seconds: float = 604800.0):
        """
        Args:
            storage_path: Path to JSON persistence file.
            ttl_seconds: Approval expiration time (default 7 days / 604,800s).
        """
        if storage_path is None:
            base_dir = Path(__file__).parent / "data"
            base_dir.mkdir(parents=True, exist_ok=True)
            storage_path = str(base_dir / "pending_approvals.json")

        self.storage_path = Path(storage_path)
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        self.ttl_seconds = ttl_seconds
        self._approvals: Dict[str, PendingApproval] = {}
        self._load()

    def _load(self) -> None:
        """Loads approvals from disk if available."""
        if not self.storage_path.exists():
            return
        try:
            with open(self.storage_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                for item in data.values():
                    appr = PendingApproval(**item)
                    self._approvals[appr.approval_id] = appr
            logger.info("Loaded %d pending approvals from %s", len(self._approvals), self.storage_path.name)
        except Exception as e:
            logger.warning("Failed loading approval store from %s: %s", self.storage_path, e)

    def _save(self) -> None:
        """Persists approvals atomically to disk."""
        try:
            data = {k: v.model_dump() for k, v in self._approvals.items()}
            temp_path = self.storage_path.with_suffix(".tmp")
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            temp_path.replace(self.storage_path)
        except Exception as e:
            logger.error("Failed saving approval store to %s: %s", self.storage_path, e)

    def cleanup_expired(self) -> int:
        """Marks approvals older than ttl_seconds as EXPIRED."""
        now = time.time()
        expired_count = 0
        for appr in self._approvals.values():
            if appr.status == ApprovalStatus.PENDING and (now - appr.created_at) > self.ttl_seconds:
                appr.status = ApprovalStatus.EXPIRED
                appr.resolved_at = now
                expired_count += 1
        if expired_count > 0:
            self._save()
        return expired_count

    def create_approval(
        self,
        action_summary: str,
        artifact_content: str,
        artifact_title: str = "",
        action_type: str = "AUTO_EXECUTE",
        assignee_id: Optional[str] = None,
        assignee_name: Optional[str] = None,
        assigner_id: Optional[str] = None,
        assigner_name: Optional[str] = None,
        category: str = "General Task",
        deadline: Optional[str] = None,
        context_notes: Optional[str] = None,
        permalink: Optional[str] = None,
        channel_id: Optional[str] = None,
        message_ts: Optional[str] = None,
        room_id: Optional[str] = None,
        switch_doc_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> PendingApproval:
        """Creates and persists a pending approval record.
        
        Returns:
            PendingApproval instance containing the short approval_id.
        """
        self.cleanup_expired()
        approval_id = f"appr_{uuid.uuid4().hex[:12]}"

        appr = PendingApproval(
            approval_id=approval_id,
            action_type=action_type,
            action_summary=action_summary,
            artifact_title=artifact_title or action_summary,
            artifact_content=artifact_content,
            assignee_id=assignee_id,
            assignee_name=assignee_name,
            assigner_id=assigner_id,
            assigner_name=assigner_name,
            category=category,
            deadline=deadline,
            context_notes=context_notes,
            permalink=permalink,
            channel_id=channel_id,
            message_ts=message_ts,
            room_id=room_id,
            switch_doc_id=switch_doc_id,
            metadata=metadata or {},
        )
        self._approvals[approval_id] = appr
        self._save()

        logger.info(
            "💾 [ApprovalStore] Persisted approval %s ('%s', %d chars content)",
            approval_id, action_summary, len(artifact_content)
        )
        return appr

    def get_approval(self, approval_id: str) -> Optional[PendingApproval]:
        """Retrieves a pending approval by ID."""
        self.cleanup_expired()
        return self._approvals.get(approval_id)

    def resolve_approval(
        self,
        approval_id: str,
        status: ApprovalStatus,
        resolved_by: Optional[str] = None,
    ) -> Optional[PendingApproval]:
        """Marks an approval as APPROVED, REJECTED, or DISMISSED."""
        appr = self.get_approval(approval_id)
        if not appr:
            logger.warning("Approval %s not found in store", approval_id)
            return None

        appr.status = status
        appr.resolved_at = time.time()
        appr.resolved_by = resolved_by
        self._save()

        logger.info("✅ [ApprovalStore] Resolved approval %s -> %s by %s", approval_id, status.value, resolved_by)
        return appr

    def list_pending(self, room_id: Optional[str] = None) -> List[PendingApproval]:
        """Lists all pending approvals, optionally filtered by room."""
        self.cleanup_expired()
        pending = [
            a for a in self._approvals.values()
            if a.status == ApprovalStatus.PENDING
        ]
        if room_id:
            pending = [a for a in pending if a.room_id == room_id]
        return pending

    def clear(self) -> None:
        """Clears all records (useful for testing)."""
        self._approvals.clear()
        if self.storage_path.exists():
            try:
                self.storage_path.unlink()
            except Exception:
                pass


# Global default approval store
default_approval_store = PendingApprovalStore()
