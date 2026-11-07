"""Native Switch Room Document Store for TaskFlow.

Persists draft deliverables, release notes, postmortems, and specs natively
inside Switch Workroom documents, ensuring artifacts are preserved server-side
and within room memory before and after human approval.
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

logger = logging.getLogger("RoomDocumentStore")


class DocumentStatus(str, Enum):
    DRAFT = "DRAFT"
    PUBLISHED = "PUBLISHED"
    DISMISSED = "DISMISSED"
    ARCHIVED = "ARCHIVED"


@dataclass
class RoomDocument:
    """A native document tracked and stored inside a Switch Workroom."""
    doc_id: str
    room_id: str
    name: str
    title: str
    content: str
    description: str = ""
    instructions: str = ""
    status: DocumentStatus = DocumentStatus.DRAFT
    version: int = 1
    created_by: str = "taskflow-worker"
    updated_by: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def document_id(self) -> str:
        """Alias for doc_id matching Switch Core API naming."""
        return self.doc_id

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["document_id"] = self.doc_id
        d["status"] = self.status.value if isinstance(self.status, DocumentStatus) else str(self.status)
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RoomDocument":
        clean_data = dict(data)
        # Handle doc_id / document_id alias
        if "doc_id" not in clean_data and "document_id" in clean_data:
            clean_data["doc_id"] = clean_data["document_id"]
        clean_data.pop("document_id", None)

        if "status" in clean_data and isinstance(clean_data["status"], str):
            try:
                clean_data["status"] = DocumentStatus(clean_data["status"])
            except ValueError:
                clean_data["status"] = DocumentStatus.DRAFT
        return cls(**clean_data)

    def format_summary(self) -> str:
        """Formats a human-readable summary of the room document."""
        lines = [
            f"📄 **Switch Room Document**",
            f"• **Doc ID:** `{self.doc_id}`",
            f"• **Title:** {self.title}",
            f"• **Name:** `{self.name}`",
            f"• **Status:** `{self.status.value}` (Version {self.version})",
            f"• **Created by:** @{self.created_by}",
        ]
        if self.description:
            lines.append(f"• **Description:** {self.description}")
        return "\n".join(lines)


class RoomDocumentStore:
    """Persistent storage for native Switch room documents."""

    def __init__(self, storage_path: str = "room_documents.json"):
        self.storage_path = Path(storage_path)
        self._documents: Dict[str, RoomDocument] = {}
        self._load()

    def _load(self):
        if self.storage_path.exists():
            try:
                data = json.loads(self.storage_path.read_text(encoding="utf-8"))
                for item in data.get("documents", []):
                    doc = RoomDocument.from_dict(item)
                    self._documents[doc.doc_id] = doc
                logger.info("Loaded %d room documents from %s", len(self._documents), self.storage_path.name)
            except Exception as e:
                logger.warning("Failed loading room documents from %s: %s", self.storage_path, e)

    def _save(self):
        try:
            payload = {
                "documents": [d.to_dict() for d in self._documents.values()]
            }
            temp_path = self.storage_path.with_suffix(".tmp")
            temp_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
            temp_path.replace(self.storage_path)
        except Exception as e:
            logger.error("Failed saving room documents to %s: %s", self.storage_path, e)

    def create_document(
        self,
        title: str,
        content: str,
        name: Optional[str] = None,
        description: Optional[str] = None,
        instructions: Optional[str] = None,
        room_id: Optional[str] = None,
        doc_id: Optional[str] = None,
        status: str = "DRAFT",
        created_by: str = "taskflow-worker",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> RoomDocument:
        """Creates and persists a new room document."""
        assigned_id = doc_id or f"doc_{uuid.uuid4().hex[:12]}"
        doc_name = name or title.lower().replace(" ", "-")[:40] or f"doc-{uuid.uuid4().hex[:8]}"

        try:
            doc_status = DocumentStatus(status)
        except ValueError:
            doc_status = DocumentStatus.DRAFT

        doc = RoomDocument(
            doc_id=assigned_id,
            room_id=room_id or "default_room",
            name=doc_name,
            title=title,
            content=content,
            description=description or f"Room deliverable: {title}",
            instructions=instructions or "TaskFlow autonomous deliverable draft",
            status=doc_status,
            version=1,
            created_by=created_by,
            created_at=time.time(),
            updated_at=time.time(),
            metadata=dict(metadata or {}),
        )
        self._documents[assigned_id] = doc
        self._save()
        logger.info("Recorded native Switch room document '%s' (%s) in room %s", title, assigned_id, room_id)
        return doc

    def update_document(
        self,
        doc_id: str,
        content: Optional[str] = None,
        title: Optional[str] = None,
        name: Optional[str] = None,
        description: Optional[str] = None,
        instructions: Optional[str] = None,
        status: Optional[str] = None,
        updated_by: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[RoomDocument]:
        """Updates an existing room document."""
        doc = self._documents.get(doc_id)
        if not doc:
            return None

        if content is not None:
            doc.content = content
        if title is not None:
            doc.title = title
        if name is not None:
            doc.name = name
        if description is not None:
            doc.description = description
        if instructions is not None:
            doc.instructions = instructions
        if status is not None:
            try:
                doc.status = DocumentStatus(status)
            except ValueError:
                pass
        if updated_by is not None:
            doc.updated_by = updated_by
        if metadata:
            doc.metadata.update(metadata)

        doc.version += 1
        doc.updated_at = time.time()
        self._save()
        logger.info("Updated Switch room document %s (status: %s, version: %d)", doc_id, doc.status.value, doc.version)
        return doc

    def get_document(self, doc_id: str, room_id: Optional[str] = None) -> Optional[RoomDocument]:
        """Retrieves a room document by its ID."""
        doc = self._documents.get(doc_id)
        if doc and room_id and doc.room_id != room_id:
            return None
        return doc

    def list_documents(
        self,
        room_id: Optional[str] = None,
        status: Optional[str] = None,
    ) -> List[RoomDocument]:
        """Lists room documents filtered by room and/or status."""
        results = list(self._documents.values())
        if room_id:
            results = [d for d in results if d.room_id == room_id]
        if status:
            target_status = status.value if isinstance(status, DocumentStatus) else str(status).upper()
            results = [d for d in results if d.status.value == target_status]
        results.sort(key=lambda d: d.created_at, reverse=True)
        return results

    def delete_document(self, doc_id: str) -> bool:
        """Deletes a document from the store."""
        if doc_id in self._documents:
            del self._documents[doc_id]
            self._save()
            return True
        return False


default_room_document_store = RoomDocumentStore()
