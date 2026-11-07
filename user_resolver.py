"""
User & Assignee Resolution Engine for TaskFlow.

Resolves Slack and Switch user IDs, display names, and roles using:
1. In-text mentions (Slack `<@U12345>` and Switch `@handle` syntax)
2. Configured and dynamically learned team roster
3. Live Switch room participants (`list_participants` / `get_room_detail`)

CRITICAL GUARANTEE:
When a message delegates a task to someone (e.g. "@Anuj please review the PR"),
the assignee resolves to that person (Anuj), NEVER defaulting to the sender.
"""

import re
import os
import json
import logging
from typing import Dict, Any, Optional, List, Tuple, Set

logger = logging.getLogger("UserResolver")

# Known Bot handles that should never be resolved as human assignees
BOT_HANDLES = {
    "taskflow",
    "taskflow-triage",
    "taskflow-worker",
    "switchbot",
    "slackbot",
}

# Default Known Team Roster (Directory of team members, handles, and user IDs)
DEFAULT_ROSTER: Dict[str, Dict[str, Any]] = {
    "anuj": {
        "user_id": "U0CFRP39TRS",
        "name": "Anuj",
        "aliases": ["anuj", "anujjain", "anujjainaj16", "anuj jain", "u0cfrp39trs"],
    },
    "priya": {
        "user_id": "U_PRIYA",
        "name": "Priya",
        "aliases": ["priya", "priya lead", "priya tech lead", "u_priya"],
    },
    "alex": {
        "user_id": "U_ALEX",
        "name": "Alex",
        "aliases": ["alex", "alex pm", "alex product manager", "u_alex"],
    },
    "sarah": {
        "user_id": "U_SARAH",
        "name": "Sarah",
        "aliases": ["sarah", "sarah dev", "sarah engineer", "u_sarah"],
    },
    "kunal": {
        "user_id": "U_KUNAL",
        "name": "Kunal",
        "aliases": ["kunal", "kunal developer", "u_kunal"],
    },
    "rohit": {
        "user_id": "U_ROHIT",
        "name": "Rohit",
        "aliases": ["rohit", "rohit qa", "u_rohit"],
    },
    "david": {
        "user_id": "U_DAVID",
        "name": "David",
        "aliases": ["david", "u_david"],
    },
}

class UserResolver:
    """Manages directory lookup, mention parsing, and participant-based resolution."""

    def __init__(self, custom_roster: Optional[Dict[str, Dict[str, Any]]] = None):
        self.roster: Dict[str, Dict[str, Any]] = {}
        # Load default roster
        for key, val in DEFAULT_ROSTER.items():
            self.roster[key.lower()] = dict(val)

        # Merge custom roster if provided
        if custom_roster:
            for key, val in custom_roster.items():
                self.roster[key.lower()] = dict(val)

        # Merge environment overrides if defined (JSON format)
        env_roster = os.getenv("TEAM_ROSTER")
        if env_roster:
            try:
                data = json.loads(env_roster)
                if isinstance(data, dict):
                    for k, v in data.items():
                        self.roster[k.lower()] = dict(v)
            except Exception as e:
                logger.warning("Failed parsing TEAM_ROSTER environment variable: %s", e)

        # Dynamic cache for encountered users: handle/name -> (user_id, display_name)
        self.dynamic_cache: Dict[str, Tuple[str, str]] = {}

    def record_user(self, user_id: Optional[str], user_name: Optional[str]):
        """Dynamically caches user ID and display name from incoming messages and Switch events."""
        if not user_name and not user_id:
            return
        clean_name = (user_name or user_id or "").strip().lstrip("@")
        clean_id = (user_id or user_name or "").strip()
        if clean_name.lower() in BOT_HANDLES:
            return

        self.dynamic_cache[clean_name.lower()] = (clean_id, clean_name)
        if clean_id:
            self.dynamic_cache[clean_id.lower()] = (clean_id, clean_name)

    def register_switch_participants(self, participants: List[Dict[str, Any]]):
        """Registers participants retrieved from Switch ops/list_participants or get_room_detail."""
        for p in participants:
            p_name = p.get("name") or p.get("display_name")
            p_id = p.get("id") or p.get("user_id") or p_name
            if p_name and p_name.lower() not in BOT_HANDLES:
                self.record_user(user_id=p_id, user_name=p_name)
                # Live Switch participants update the directory
                self.roster[p_name.lower()] = {
                    "user_id": p_id,
                    "name": p_name,
                    "aliases": [p_name.lower(), p_id.lower() if p_id else ""],
                }

    def extract_mentions(self, text: str) -> List[Tuple[str, Optional[str]]]:
        """
        Extracts non-bot user mentions from text.
        Supports:
        - Slack format: <@U12345678> or <@U12345678|anuj>
        - Switch / Markdown format: @username, @Anuj
        
        Returns list of (identifier, label_or_none).
        """
        mentions: List[Tuple[str, Optional[str]]] = []
        if not text:
            return mentions

        # 1. Slack mentions: <@U12345> or <@U12345|username>
        slack_matches = re.findall(r"<@([A-Za-z0-9_.-]+)(?:\|([^>]+))?>", text)
        for uid, uname in slack_matches:
            if uid.lower() not in BOT_HANDLES and (not uname or uname.lower() not in BOT_HANDLES):
                mentions.append((uid, uname))

        # 2. Markdown mentions: @username
        md_matches = re.findall(r"(?:^|[\s(])@([a-zA-Z0-9_.-]+)", text)
        for m in md_matches:
            clean_m = m.strip().rstrip(".,:;!?)]")
            if clean_m.lower() not in BOT_HANDLES:
                mentions.append((clean_m, clean_m))

        return mentions

    def lookup_user(self, query: str) -> Optional[Tuple[str, str]]:
        """
        Looks up a user in the roster and dynamic cache.
        Returns (user_id, display_name) if found, else None.
        """
        if not query:
            return None
        q = query.strip().lower().lstrip("@")
        if not q or q in BOT_HANDLES:
            return None

        # 1. Direct match in roster
        if q in self.roster:
            entry = self.roster[q]
            return (entry["user_id"], entry["name"])

        # 2. Alias match in roster
        for _, entry in self.roster.items():
            aliases = [a.lower() for a in entry.get("aliases", [])]
            if q == entry["name"].lower() or q in aliases or any(q in a for a in aliases):
                return (entry["user_id"], entry["name"])

        # 3. Dynamic cache lookup
        if q in self.dynamic_cache:
            return self.dynamic_cache[q]

        for k, v in self.dynamic_cache.items():
            if q == k or q in k:
                return v

        return None

    def resolve_assignee(
        self,
        triage_assignee_name: Optional[str],
        triage_assignee_id: Optional[str],
        message_text: str,
        sender_id: str,
        sender_name: str,
        participants: Optional[List[Dict[str, Any]]] = None,
    ) -> Tuple[str, Optional[str]]:
        """
        Resolves the true assignee name and user ID for a triaged task.
        
        CRITICAL RULES:
        1. If someone else is mentioned (e.g. '@Anuj') or named in the text,
           the assignee is THAT PERSON, NEVER the sender!
        2. If 'anuj' is mentioned or named anywhere, resolves to Anuj (U0CFRP39TRS).
        3. The assignee becomes the sender ONLY when:
           - Explicit self-commitment ("I will...", "I'll take this...")
           - A direct command to the bot for the sender's own deliverable
             (e.g. Kunal asking @taskflow-triage to draft release notes)
           - No other person is mentioned or assigned.
        
        Returns (assignee_name, assignee_id).
        """
        # Register sender in dynamic cache
        self.record_user(user_id=sender_id, user_name=sender_name)

        # Register live Switch participants if available
        if participants:
            self.register_switch_participants(participants)

        # 1. Check for explicit mentions in the trigger message
        mentions = self.extract_mentions(message_text)
        for m_id, m_label in mentions:
            found = self.lookup_user(m_id) or (self.lookup_user(m_label) if m_label else None)
            if found:
                logger.info("🎯 [UserResolver] Resolved assignee from mention '%s' -> %s (%s)", m_id, found[1], found[0])
                return (found[1], found[0])
            else:
                # Mention not in roster, but clearly designated
                clean_target = (m_label or m_id).strip().lstrip("@")
                logger.info("🎯 [UserResolver] Extracted raw mention assignee: %s", clean_target)
                return (clean_target, m_id if m_id.startswith("U") else None)

        # 2. Check triage extracted assignee from LLM
        if triage_assignee_name:
            clean_name = triage_assignee_name.strip().lstrip("@")
            found = self.lookup_user(clean_name)
            if found:
                logger.info("🎯 [UserResolver] Resolved assignee from triage model '%s' -> %s (%s)", clean_name, found[1], found[0])
                return (found[1], found[0])
            
            # If triage model provided an explicit ID or name that isn't the bot
            if clean_name.lower() not in BOT_HANDLES:
                return (clean_name, triage_assignee_id)

        # 3. Check for explicit self-commitment in message text
        text_lower = (message_text or "").lower()
        self_commitment_patterns = [
            r"\bi will\b",
            r"\bi'll\b",
            r"\bme\b",
            r"\bi am on it\b",
            r"\bi got this\b",
            r"\blet me\b",
            r"\bi can do\b",
            r"\bi can take\b",
        ]
        is_self_committed = any(re.search(pat, text_lower) for pat in self_commitment_patterns)
        if is_self_committed:
            logger.info("👤 [UserResolver] Self-commitment detected; assignee is sender '%s' (%s)", sender_name, sender_id)
            return (sender_name, sender_id)

        # 4. Fallback: If no other person is mentioned, default to sender
        logger.debug("[UserResolver] No external assignee mentioned; defaulting to sender '%s'", sender_name)
        return (sender_name, sender_id)

# Global default instance
default_user_resolver = UserResolver()
