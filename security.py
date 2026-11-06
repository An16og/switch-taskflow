import re
import logging
from typing import Tuple, List

logger = logging.getLogger("Security")

# Invisible and bi-directional control characters frequently used in prompt injection cloaking
INVISIBLE_CHARS_REGEX = re.compile(
    r"[\u200B-\u200D\uFEFF\u202A-\u202E\u2060-\u206F\u00A0]"
)

# Common prompt injection and jailbreak signatures
INJECTION_PATTERNS = [
    re.compile(r"ignore\s+(all\s+)?(previous|above|prior)\s+(instructions|directives|prompts)", re.IGNORECASE),
    re.compile(r"disregard\s+(all\s+)?(previous|above|prior)\s+(instructions|directives|prompts)", re.IGNORECASE),
    re.compile(r"system\s+(override|prompt|directive):", re.IGNORECASE),
    re.compile(r"you\s+are\s+now\s+(in\s+)?(developer\s+mode|dan|jailbroken)", re.IGNORECASE),
    re.compile(r"(print|reveal|output|display)\s+(your\s+)?(system\s+prompt|initial\s+instructions)", re.IGNORECASE),
    re.compile(r"</?(system|instructions?|prompt|context|assistant|human)>", re.IGNORECASE),
    re.compile(r"bypass\s+(all\s+)?(safety|security|guardrails)", re.IGNORECASE),
]

def sanitize_channel_text(text: str, max_length: int = 2000) -> str:
    """Sanitizes untrusted text originating from chat channels.
    
    1. Removes invisible/zero-width and bidirectional unicode control characters.
    2. Defangs dangerous XML/system tags.
    3. Defangs markdown delimiter breakout sequences.
    4. Enforces a maximum length to prevent context-stuffing denial of service.
    """
    if not text:
        return ""

    # 1. Strip invisible / zero-width characters
    sanitized = INVISIBLE_CHARS_REGEX.sub(" ", text)

    # 2. Defang XML-like prompt boundaries (<system>, </instructions>, etc.)
    sanitized = re.sub(r"<\s*(/?\s*(system|instructions?|prompt|context|assistant|human|tool))\s*>", r"[\1]", sanitized, flags=re.IGNORECASE)

    # 3. Defang markdown fence breakout attempts (replace 4+ backticks with safe quotes)
    sanitized = re.sub(r"`{3,}", "```", sanitized)

    # 4. Truncate excessive input
    if len(sanitized) > max_length:
        sanitized = sanitized[:max_length] + " ... [TRUNCATED FOR SECURITY]"

    return sanitized.strip()

def detect_prompt_injection(text: str) -> Tuple[bool, str]:
    """Detects overt prompt injection or jailbreak attempts in channel text."""
    if not text:
        return False, ""

    for pattern in INJECTION_PATTERNS:
        match = pattern.search(text)
        if match:
            reason = f"Matched injection pattern: '{match.group(0)}'"
            logger.warning("🚨 [Prompt Injection Detected]: %s", reason)
            return True, reason

    return False, ""

def frame_untrusted_message(user_name: str, user_id: str, ts: str, raw_text: str) -> str:
    """Encapsulates channel message inside secure untrusted boundaries."""
    sanitized = sanitize_channel_text(raw_text)
    is_injection, reason = detect_prompt_injection(raw_text)
    
    flag = f' flag="SUSPECTED_INJECTION: {reason}"' if is_injection else ''
    return f'<untrusted_message sender="{user_name}" user_id="{user_id}" ts="{ts}"{flag}>\n{sanitized}\n</untrusted_message>'
