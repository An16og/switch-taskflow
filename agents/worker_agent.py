import logging
from typing import List

from models import TaskTriageResult, ExecutionType, WorkerExecutionResult, SlackMessage
from llm_gateway import LLMGateway

logger = logging.getLogger("WorkerAgent")

WORKER_SYSTEM_PROMPT = """You are TaskFlow Worker Agent, an autonomous AI execution worker in a Switch Room.
When a task is triaged, your job is to actually DO the work or prepare the concrete execution scaffold.

1. If execution_type is AUTO_EXECUTE:
   - Generate the complete, high-quality, production-ready artifact (e.g. formatted Release Notes, comprehensive Documentation snippet, Incident Postmortem draft, or Announcement).
   - Write in professional, clean Markdown with headers, bullet points, and code blocks where applicable.
   - Do NOT say "Here is what you can do". Actually WRITE the full draft/content!

2. If execution_type is HUMAN_TODO:
   - Generate a concise 3-4 step Action Checklist that breaks down the task into bite-sized actionable milestones.
   - Example:
     * [ ] Step 1: Check server logs for error trace
     * [ ] Step 2: Reproduce on staging environment
     * [ ] Step 3: Implement fix and run regression test
"""

class WorkerAgent:
    def __init__(self, gateway: LLMGateway = None):
        self.gateway = gateway or LLMGateway()

    def execute_task(self, triage: TaskTriageResult, context_messages: List[SlackMessage]) -> WorkerExecutionResult:
        """
        Executes the task autonomously (for AUTO_EXECUTE) or scaffolds the action plan (for HUMAN_TODO).
        """
        convo_summary = "\n".join([f"{m.user_name}: {m.text}" for m in context_messages[-5:]])

        prompt = f"""Task Title: {triage.action_summary}
Execution Category: {triage.execution_category}
Execution Type: {triage.execution_type.value}
Assigned to: {triage.assignee_name or 'Team'}
Deadline: {triage.deadline or 'ASAP'}
Background Context: {triage.context_notes or 'None'}

Conversation Stream:
---
{convo_summary}
---

Generate the complete WorkerExecutionResult with:
- artifact_title
- artifact_content (Full drafted markdown artifact or step-by-step checklist)
- suggested_action
"""
        try:
            return self.gateway.generate_structured(
                prompt=prompt,
                system_prompt=WORKER_SYSTEM_PROMPT,
                response_schema=WorkerExecutionResult
            )
        except Exception as e:
            logger.error(f"WorkerAgent execution failed: {e}", exc_info=True)
            # Safe fallback
            if triage.execution_type == ExecutionType.AUTO_EXECUTE:
                content = f"### Auto-Generated Draft for: {triage.action_summary}\n\n*Draft generated based on chat context.*"
            else:
                content = f"* [ ] Step 1: Initial review of {triage.action_summary}\n* [ ] Step 2: Implementation\n* [ ] Step 3: Verification"

            return WorkerExecutionResult(
                artifact_title=f"{triage.action_summary}",
                artifact_content=content,
                suggested_action="Review and track"
            )
