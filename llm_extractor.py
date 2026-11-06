import json
import logging
from datetime import datetime
from typing import List, Optional
from google import genai
from google.genai import types

from config import GEMINI_API_KEY
from models import TaskExtraction, ConfidenceLevel, SlackMessage

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are TaskFlow, an ambient AI teammate monitoring a team's Slack communication.
Your goal is HIGH RECALL task extraction: You must NEVER let a genuine task, commitment, or delegation slip through the cracks. Missing a real task is considered a critical system failure.

Analyze the given conversation window (ordered oldest to newest). The LAST message is the trigger message that just arrived.

### Classification Rules:
1. HIGH CONFIDENCE:
   - Explicit task assigned to a specific person (e.g. "@Rohit please fix the auth bug before the demo", "Priya, can you send the report by EOD?").
   - Clear action required, clear assignee.

2. MEDIUM CONFIDENCE:
   - Ambiguous or implicit task (e.g. "Could someone update the documentation?", "Client needs the slide deck ASAP Rohit", "It would be great if we tested the payment flow").
   - Self-assigned tasks ("I will deploy the staging fix tonight").
   - An ask where the assignee or deadline is not 100% explicit, but an action item is definitely being discussed.

3. LOW CONFIDENCE (NOT A TASK):
   - Casual banter, greetings ("Hey guys", "Good morning").
   - Status inquiries / questions ("Did you finish the build?", "Is the PR merged?").
   - Emotional venting / opinions ("I hate this bug", "This framework is so slow").
   - Pure information sharing ("The server is restarting", "Here is the Figma link").

4. DYNAMIC CONTEXT (needs_more_context):
   - If the trigger message is a fragment (e.g. "by 5 PM please", "and also update the unit tests", "sure, I'll take it") that depends on earlier context not fully captured in the current window, set `needs_more_context: true`.

### Output Guidelines:
- action_summary: Concise, professional imperative title (e.g., 'Update payment webhook documentation').
- assignee_id: Extract the Slack user ID if tagged (format: U12345678). If not tagged with an ID, leave null and set assignee_name.
- deadline: Formatted naturally (e.g. 'Today by 5:00 PM', 'Monday EOD', or '2026-11-09 17:00'). Optional; leave null if unmentioned.
- context_notes: 1 sentence explaining the background why this task is needed.
"""

class LLMExtractor:
    def __init__(self, api_key: Optional[str] = None, model_name: str = "gemini-3.5-flash"):
        self.api_key = api_key or GEMINI_API_KEY
        self.model_name = model_name
        self._client = None

    @property
    def client(self) -> genai.Client:
        if self._client is None:
            if not self.api_key:
                raise ValueError("GEMINI_API_KEY is not configured.")
            self._client = genai.Client(api_key=self.api_key)
        return self._client

    def extract_task(self, messages: List[SlackMessage]) -> TaskExtraction:
        """
        Takes a sliding window of Slack messages and extracts structured task metadata.
        """
        if not messages:
            return TaskExtraction(
                is_task=False,
                confidence=ConfidenceLevel.LOW,
                confidence_reason="No messages provided."
            )

        # Format conversation context
        formatted_convo = []
        for msg in messages:
            formatted_convo.append(
                f"[{msg.ts}] User <@{msg.user_id}> ({msg.user_name}): {msg.text}"
            )
        convo_text = "\n".join(formatted_convo)

        now_str = datetime.now().strftime("%A, %Y-%m-%d %H:%M:%S")

        prompt = f"""Current reference time: {now_str}

Conversation Window (latest message is at the bottom):
---
{convo_text}
---

Analyze whether the latest message (in context with previous messages) constitutes an action item or task.
Return the structured TaskExtraction."""

        try:
            config = types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                response_mime_type="application/json",
                response_schema=TaskExtraction,
                temperature=0.1,  # Low temperature for deterministic classification
            )

            # Try primary model, fallback to gemini-2.0-flash / gemini-1.5-flash if needed
            try:
                response = self.client.models.generate_content(
                    model=self.model_name,
                    contents=prompt,
                    config=config,
                )
            except Exception as model_err:
                logger.warning(f"Failed with {self.model_name}, trying gemini-3.1-flash-lite: {model_err}")
                response = self.client.models.generate_content(
                    model="gemini-3.1-flash-lite",
                    contents=prompt,
                    config=config,
                )

            # Parse result
            if response.parsed:
                return response.parsed
            
            # Fallback text parse
            data = json.loads(response.text)
            return TaskExtraction.model_validate(data)

        except Exception as e:
            logger.error(f"Error in LLM task extraction: {e}", exc_info=True)
            return TaskExtraction(
                is_task=False,
                confidence=ConfidenceLevel.LOW,
                confidence_reason=f"LLM extraction error: {str(e)}"
            )
