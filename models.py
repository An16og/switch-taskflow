from enum import Enum
from typing import Optional, List
from pydantic import BaseModel, Field

class ConfidenceLevel(str, Enum):
    HIGH = "HIGH"       # Direct, clear task assignment to a specific person
    MEDIUM = "MEDIUM"   # Ambiguous, implicit request, or informal check
    LOW = "LOW"         # General chatter, status check, rant, or non-task

class ExecutionType(str, Enum):
    AUTO_EXECUTE = "AUTO_EXECUTE"  # Knowledge work AI can do (docs, summaries, release notes, issue templates)
    HUMAN_TODO = "HUMAN_TODO"      # Complex dev, code reviews, client calls, architecture design

class TaskTriageResult(BaseModel):
    is_task: bool = Field(
        description="True if the message thread contains an actionable task or delegation."
    )
    confidence: ConfidenceLevel = Field(
        description="Confidence level of detection: HIGH, MEDIUM, or LOW."
    )
    confidence_reason: str = Field(
        description="One brief sentence explaining why this was or wasn't classified as a task."
    )
    action_summary: str = Field(
        default="",
        description="Crisp, actionable imperative task title (e.g., 'Update payment webhook documentation')."
    )
    assignee_id: Optional[str] = Field(
        default=None,
        description="Slack User ID (e.g. 'U12345678') if tagged or known."
    )
    assignee_name: Optional[str] = Field(
        default=None,
        description="Name or handle of the assignee if mentioned in text."
    )
    deadline: Optional[str] = Field(
        default=None,
        description="Natural language deadline (e.g. 'Friday 5:00 PM', 'Tomorrow EOD') or null."
    )
    context_notes: Optional[str] = Field(
        default=None,
        description="Brief 1-sentence background summary from conversation history."
    )
    execution_type: ExecutionType = Field(
        default=ExecutionType.HUMAN_TODO,
        description="AUTO_EXECUTE if routine knowledge work (docs/summaries/templates), HUMAN_TODO if requires human."
    )
    execution_category: str = Field(
        default="General Task",
        description="Category: 'Documentation', 'Release Notes', 'Summary', 'Code Review', 'Meeting', etc."
    )
    needs_more_context: bool = Field(
        default=False,
        description="True if critical context is missing and the sliding window should expand."
    )

class WorkerExecutionResult(BaseModel):
    artifact_title: str = Field(
        description="Title of the completed artifact (e.g. 'Release Notes Draft v1.2' or 'Implementation Checklist')."
    )
    artifact_content: str = Field(
        description="The actual generated artifact content in clean Markdown format."
    )
    suggested_action: str = Field(
        default="Review and approve",
        description="Suggested next step for the user."
    )

class SlackMessage(BaseModel):
    user_id: str
    user_name: str
    text: str
    ts: str
    permalink: Optional[str] = None
