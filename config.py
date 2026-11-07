import json
import os
import sys
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv

env_path = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=env_path)

# ==============================================================================
# Switch Server & Collaboration Bridge Configuration
# ==============================================================================
# CRITICAL ARCHITECTURAL GUARANTEE:
# Chat platforms (Slack, Teams, Discord, Mattermost) are connected directly through
# Switch Console / Switch Core under "Messaging Apps" / Bridges.
# Switch is the authoritative owner of the Slack connection.
#
# TaskFlow NEVER opens a parallel Slack Bolt app or standalone Socket Mode listener.
# Running a parallel Bolt app on the same Slack workspace will collide and fight
# for events. Slack events are enabled ONLY if Slack is the Switch-connected app,
# routed cleanly over Switch's native WebSocket event stream.
# (Configuring standalone Event Subscriptions like 'message.channels' is the wrong
# fix when Switch owns the Slack connection).
# ==============================================================================
SWITCH_SERVER_URL = os.getenv("SWITCH_SERVER_URL", "http://127.0.0.1:52422").strip().rstrip("/")
SWITCH_WS_URL = os.getenv("SWITCH_WS_URL", SWITCH_SERVER_URL.replace("http://", "ws://").replace("https://", "wss://")).strip().rstrip("/")
AGENT_REGISTRATION_TOKEN = os.getenv("AGENT_REGISTRATION_TOKEN", "").strip()

# Optional: Specific Switch room ID to bind to (leave empty to discover/claim)
SWITCH_ROOM_ID = os.getenv("SWITCH_ROOM_ID", "").strip()

# ==============================================================================
# Agent 1: TaskFlow Triage (The Ear - Ambient / High-Recall Classifier)
# ==============================================================================
SWITCH_TRIAGE_AGENT_NAME = os.getenv("SWITCH_TRIAGE_AGENT_NAME", "taskflow-triage").strip()
SWITCH_TRIAGE_AGENT_ID = os.getenv("SWITCH_TRIAGE_AGENT_ID", "").strip()
SWITCH_TRIAGE_AGENT_API_KEY = os.getenv("SWITCH_TRIAGE_AGENT_API_KEY", "").strip()

# ==============================================================================
# Agent 2: TaskFlow Worker (The Hands - Autonomous Drafts & Todoist Scaffolder)
# ==============================================================================
SWITCH_WORKER_AGENT_NAME = os.getenv("SWITCH_WORKER_AGENT_NAME", "taskflow-worker").strip()
SWITCH_WORKER_AGENT_ID = os.getenv("SWITCH_WORKER_AGENT_ID", "").strip()
SWITCH_WORKER_AGENT_API_KEY = os.getenv("SWITCH_WORKER_AGENT_API_KEY", "").strip()

# Legacy single-agent fallback
SWITCH_AGENT_ID = os.getenv("SWITCH_AGENT_ID", SWITCH_TRIAGE_AGENT_ID).strip()
SWITCH_AGENT_API_KEY = os.getenv("SWITCH_AGENT_API_KEY", SWITCH_TRIAGE_AGENT_API_KEY).strip()

# Switch Trust Guardrails Configuration
SWITCH_TRUST_API_KEY = os.getenv("SWITCH_TRUST_API_KEY", "").strip()
SWITCH_TRUST_GATEWAY_URL = os.getenv("SWITCH_TRUST_GATEWAY_URL", "https://api.switchagents.ai").strip()
SWITCH_TRUST_POLICY_ID = os.getenv("SWITCH_TRUST_POLICY_ID", "default").strip()
SWITCH_TRUST_REQUIRE_GUARDRAILS = os.getenv("SWITCH_TRUST_REQUIRE_GUARDRAILS", "false").strip().lower() in ("true", "1", "yes")
SWITCH_TRUST_ALLOW_INSECURE_GATEWAY = os.getenv("SWITCH_TRUST_ALLOW_INSECURE_GATEWAY", "false").strip().lower() in ("true", "1", "yes")

# Ambient Triage Policy ("addressed_only", "action_keywords", "all")
AMBIENT_TRIAGE_POLICY = os.getenv("AMBIENT_TRIAGE_POLICY", "action_keywords").strip().lower()

# Extra comma-separated action keywords for this team's vocabulary
AMBIENT_EXTRA_KEYWORDS = [
    kw.strip().lower()
    for kw in os.getenv("AMBIENT_EXTRA_KEYWORDS", "").split(",")
    if kw.strip()
]

# ==============================================================================
# Context Window & Repository Grounding
# ==============================================================================
# Triage starts with a small high-signal window and expands when the latest
# message is a fragment / needs earlier context. The expanded window is also
# passed to the worker, so a task split across several messages keeps its
# original announcement and follow-up detail.
TRIAGE_STATIC_WINDOW = int(os.getenv("TRIAGE_STATIC_WINDOW", "5").strip() or 5)
TRIAGE_EXPANDED_WINDOW = int(os.getenv("TRIAGE_EXPANDED_WINDOW", "15").strip() or 15)
# Pending compact handoffs older than this are ignored, so a restart does not
# execute a stale task from a previous session.
HANDOFF_TTL_SECONDS = int(os.getenv("HANDOFF_TTL_SECONDS", "600").strip() or 600)

# Local checkout the worker reads git history / changelog / version from.
# Defaults to the current working directory when it is a git repository, then
# to the TaskFlow project directory.
TASKFLOW_REPO_DIR = os.getenv("TASKFLOW_REPO_DIR", "").strip()
# The repository URL shown to the model and attached as a Switch reference.
TASKFLOW_REPO_URL = os.getenv("TASKFLOW_REPO_URL", "").strip()
# Optional per-room override, e.g.:
# TASKFLOW_ROOM_REPOS={"8033d9bb-...": {"dir": "/repos/app", "url": "https://github.com/acme/app"}}
try:
    _room_repo_raw = json.loads(os.getenv("TASKFLOW_ROOM_REPOS", "{}") or "{}")
    TASKFLOW_ROOM_REPOS = _room_repo_raw if isinstance(_room_repo_raw, dict) else {}
except Exception:
    TASKFLOW_ROOM_REPOS = {}

# LLM Providers & Configured Models
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite").strip()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-3-5-sonnet-20241022").strip()

# LLM Retry & Backoff Configuration
LLM_MAX_RETRIES = int(os.getenv("LLM_MAX_RETRIES", "3").strip() or 3)
LLM_INITIAL_BACKOFF = float(os.getenv("LLM_INITIAL_BACKOFF", "0.5").strip() or 0.5)

# Room Rate Limiting (LLM Cost DoS Prevention)
ROOM_RATE_LIMIT_ENABLED = os.getenv("ROOM_RATE_LIMIT_ENABLED", "true").strip().lower() in ("true", "1", "yes")
ROOM_RATE_LIMIT_REQUESTS = int(os.getenv("ROOM_RATE_LIMIT_REQUESTS", "15").strip() or 15)
ROOM_RATE_LIMIT_WINDOW_SECONDS = float(os.getenv("ROOM_RATE_LIMIT_WINDOW_SECONDS", "60.0").strip() or 60.0)

# TaskFlow Persistent State Storage (SQLite)
TASKFLOW_STATE_DB = os.getenv("TASKFLOW_STATE_DB", "taskflow_state.db").strip()

# Timezone Configuration (for deadlines like 'Friday 5 PM' and Todoist sync)
SYSTEM_TIMEZONE = datetime.now().astimezone().tzname() or "UTC"
TIMEZONE = os.getenv("TIMEZONE", SYSTEM_TIMEZONE).strip()

# Task Tracker (Optional - Switch room tasks & documents are primary)
TODOIST_API_TOKEN = os.getenv("TODOIST_API_TOKEN", "").strip()

# Optional External Chat Token (for simulate_team.py or external bridges)
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN", "").strip()

def check_env():
    """Validates essential environment keys required for TaskFlow to operate."""
    missing = []
    if not GEMINI_API_KEY and not OPENAI_API_KEY and not ANTHROPIC_API_KEY:
        missing.append("At least one LLM key (GEMINI_API_KEY, OPENAI_API_KEY, ANTHROPIC_API_KEY)")
    if not SWITCH_SERVER_URL:
        missing.append("SWITCH_SERVER_URL")

    if missing:
        print("[WARNING] Missing recommended environment variables:", file=sys.stderr)
        for m in missing:
            print(f"  - {m}", file=sys.stderr)
    return len(missing) == 0
