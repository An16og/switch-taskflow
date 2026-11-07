"""Switch Trust & Prompt Injection Defense for TaskFlow.

Comprehensive Security Architecture:
1. Strips invisible/zero-width and bidirectional unicode control characters.
2. Defangs XML/system tags and markdown fence breakouts.
3. Detects prompt injections, jailbreaks, and rule-override directives.
4. Frames channel context inside strict `<untrusted_message>` XML boundaries.
5. Mandatory Pre-Publication Safety Policy (Data Loss Prevention / Exfil Blocking):
   Screens all deliverables before auto-posting to block secret leaks,
   API keys, bearer tokens, private keys, environment dumps, and beacon exfiltration.
"""

import os
import re
import logging
from typing import Tuple, List, Optional, Set

logger = logging.getLogger("Security")

# Invisible and bi-directional control characters frequently used in prompt injection cloaking
INVISIBLE_CHARS_REGEX = re.compile(
    r"[\u200B-\u200D\uFEFF\u202A-\u202E\u2060-\u206F\u00A0]"
)

# Comprehensive prompt injection, jailbreak, and instruction hijack signatures
INJECTION_PATTERNS = [
    # Directive overrides
    re.compile(r"ignore\s+(all\s+)?(previous|above|prior|triage|safety|system)?\s*(instructions|directives|prompts|rules|policies|filters)", re.IGNORECASE),
    re.compile(r"disregard\s+(all\s+)?(previous|above|prior|triage|safety|system)?\s*(instructions|directives|prompts|rules|policies|filters)", re.IGNORECASE),
    re.compile(r"forget\s+(all\s+)?(previous|above|prior|triage|safety|system)?\s*(instructions|directives|prompts|rules|policies|filters)", re.IGNORECASE),
    re.compile(r"(bypass|override|disable)\s+(all\s+)?(safety|security|guardrails|triage|rules|policies)", re.IGNORECASE),
    
    # System directive spoofing
    re.compile(r"system\s+(override|prompt|directive):", re.IGNORECASE),
    re.compile(r"\[\s*(system|instruction|admin)\s*(override)?\s*\]", re.IGNORECASE),
    re.compile(r"new\s+(system\s+)?(instruction|prompt|directive|role):", re.IGNORECASE),
    
    # Mode switching / Jailbreaks
    re.compile(r"you\s+are\s+now\s+(in\s+)?(developer\s+mode|dan|jailbroken|unrestricted)", re.IGNORECASE),
    re.compile(r"act\s+as\s+(an?\s+)?(unrestricted|jailbroken|god|admin|root|evil)\s+(ai|model|assistant|bot)?", re.IGNORECASE),
    re.compile(r"mode\s*:\s*(unrestricted|jailbreak|developer|god)", re.IGNORECASE),
    
    # Exfiltration and secret harvesting commands
    re.compile(r"(auto_execute|force\s+execute|override\s+triage).*?(secrets|env|tokens|keys|passwords)", re.IGNORECASE),
    re.compile(r"(post|leak|exfiltrate|reveal|dump|print|output|display|show|send|publish)\s+.*?(secrets|keys|passwords|env|tokens|credentials|api_key|token|system\s+prompt)", re.IGNORECASE),
    
    # Boundary manipulation
    re.compile(r"</?(system|instructions?|prompt|context|assistant|human|tool|untrusted_message|untrusted_context)>", re.IGNORECASE),
]

# Sensitive credentials & secret detection regexes (Data Loss Prevention / DLP)
SECRET_PATTERNS = [
    ("OpenAI API Key", re.compile(r"sk-[a-zA-Z0-9_-]{20,}")),
    ("Google AIza API Key", re.compile(r"AIza[0-9A-Za-z-_]{35}")),
    ("Slack Token", re.compile(r"xox[baprs]-[0-9a-zA-Z-]{10,}")),
    ("GitHub Personal Access Token", re.compile(r"gh[pousr]_[0-9a-zA-Z]{36}")),
    ("Anthropic API Key", re.compile(r"sk-ant-[a-zA-Z0-9_-]{20,}")),
    ("AWS Access Key ID", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("AWS Secret Access Key", re.compile(r"(?i)aws_secret_access_key\s*=\s*[a-zA-Z0-9/+=]{40}")),
    ("Private Key", re.compile(r"-----BEGIN\s+([A-Z\s]+)?PRIVATE\s+KEY-----")),
    ("Database Connection String", re.compile(r"(postgres|postgresql|mysql|mongodb|redis|amqp)://[a-zA-Z0-9_]+:[^@\s]{4,}@[a-zA-Z0-9_\-\.]+", re.IGNORECASE)),
    ("Bearer Authorization Token", re.compile(r"Bearer\s+[a-zA-Z0-9_\-\.]{25,}", re.IGNORECASE)),
    ("Exfiltration Beacon (Markdown Image)", re.compile(r"!\[.*?\]\(https?://[^\s\)]+(\?|&)(key|token|secret|data|exfil|leak)=[^\s\)]+\)", re.IGNORECASE)),
    ("Exfiltration Beacon (HTML Image)", re.compile(r"<img\s+[^>]*src=[\"']https?://[^\"']+(\?|&)(key|token|secret|data)=[^\s\"']+[\"']", re.IGNORECASE)),
]


def sanitize_channel_text(text: str, max_length: int = 3000) -> str:
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
    sanitized = re.sub(
        r"<\s*(/?\s*(system|instructions?|prompt|context|assistant|human|tool|untrusted_message|untrusted_context))\s*>",
        r"[\1]",
        sanitized,
        flags=re.IGNORECASE
    )

    # 3. Defang markdown fence breakout attempts (replace 4+ backticks with safe standard fences)
    sanitized = re.sub(r"`{4,}", "```", sanitized)

    # 4. Truncate excessive input
    if len(sanitized) > max_length:
        sanitized = sanitized[:max_length] + " ... [TRUNCATED FOR SECURITY]"

    return sanitized.strip()


def detect_prompt_injection(text: str) -> Tuple[bool, str]:
    """Detects prompt injection, jailbreak, or rule override attempts in channel text."""
    if not text:
        return False, ""

    for pattern in INJECTION_PATTERNS:
        match = pattern.search(text)
        if match:
            reason = f"Matched injection pattern: '{match.group(0)}'"
            logger.warning("🚨 [Prompt Injection Detected]: %s", reason)
            return True, reason

    return False, ""


def get_known_active_secrets() -> Set[str]:
    """Gathers known sensitive tokens currently present in configuration / environment."""
    known: Set[str] = set()
    secret_env_keys = [
        "SWITCH_TRUST_API_KEY",
        "SWITCH_TRIAGE_AGENT_API_KEY",
        "SWITCH_WORKER_AGENT_API_KEY",
        "AGENT_REGISTRATION_TOKEN",
        "GEMINI_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "TODOIST_API_TOKEN",
        "SLACK_BOT_TOKEN",
    ]
    for key in secret_env_keys:
        val = os.getenv(key, "").strip()
        # Only track substantial secrets (>= 8 chars) to avoid false positives on small words
        if len(val) >= 8:
            known.add(val)
    return known


def detect_secrets_and_exfiltration(text: str) -> Tuple[bool, List[str]]:
    """Scans text for sensitive credentials, secrets, or exfiltration indicators (DLP).
    
    Returns:
        (has_violations: bool, violation_reasons: List[str])
    """
    if not text:
        return False, []

    violations: List[str] = []

    # 1. Regex pattern check for credential formats
    for name, pattern in SECRET_PATTERNS:
        match = pattern.search(text)
        if match:
            violation = f"Credential leak detected: {name}"
            logger.critical("🚨 [Security Violation]: %s (matched: '%s')", violation, match.group(0)[:20] + "...")
            violations.append(violation)

    # 2. Check for active environment secrets verbatim
    known_secrets = get_known_active_secrets()
    for secret in known_secrets:
        if secret in text:
            violation = "Critical: Verbatim environment secret detected in content"
            logger.critical("🚨 [Security Violation]: %s", violation)
            if violation not in violations:
                violations.append(violation)

    # 3. Check for .env file dump signatures
    env_dump_match = re.search(r"^[A-Z0-9_]{4,}=(sk-|AIza|ghp_|xox|[a-zA-Z0-9_\-\.]{15,})", text, re.MULTILINE)
    if env_dump_match:
        violation = "Environment variable dump (.env file leakage) detected"
        logger.critical("🚨 [Security Violation]: %s", violation)
        violations.append(violation)

    return len(violations) > 0, violations


def validate_auto_post_safety(
    text: str,
    artifact_title: Optional[str] = None,
    requester: Optional[str] = None,
) -> Tuple[bool, List[str], str]:
    """Mandatory Pre-Publication Security Gate.
    
    MUST be called before any deliverable is auto-posted to a Switch room or Slack channel.
    Blocks exfiltration, secret leakage, and jailbreak payloads from reaching chat.
    
    Returns:
        (is_safe: bool, violations: List[str], policy_decision: str)
    """
    violations: List[str] = []

    # 1. Check for prompt injection / jailbreak payloads inside the output
    is_injection, inj_reason = detect_prompt_injection(text)
    if is_injection:
        violations.append(f"Prompt injection / jailbreak payload: {inj_reason}")

    # 2. Check for sensitive credentials, secrets, and exfiltration beacons
    has_secrets, secret_violations = detect_secrets_and_exfiltration(text)
    if has_secrets:
        violations.extend(secret_violations)

    if violations:
        logger.critical(
            "🚨 [Security Policy Gate] BLOCKED auto-post for '%s' (requester: %s). Violations: %s",
            artifact_title or "Untitled", requester or "Unknown", violations
        )
        return False, violations, "BLOCK_EXFIL_AND_JAILBREAKS"

    return True, [], "PASSED_SECURITY_POLICY"


def frame_untrusted_message(user_name: str, user_id: str, ts: str, raw_text: str) -> str:
    """Encapsulates channel message inside secure untrusted boundaries."""
    sanitized = sanitize_channel_text(raw_text)
    is_injection, reason = detect_prompt_injection(raw_text)
    
    flag = f' flag="SUSPECTED_INJECTION: {reason}"' if is_injection else ''
    return f'<untrusted_message sender="{user_name}" user_id="{user_id}" ts="{ts}"{flag}>\n{sanitized}\n</untrusted_message>'
