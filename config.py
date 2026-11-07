import os
import sys
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
AGENT_REGISTRATION_TOKEN = os.getenv("AGENT_REGISTRATION_TOKEN", "8EQjhvEdXZst0NB_7HGKjcjJO5itH_sx").strip()

# Optional: Specific Switch room ID to bind to (leave empty to discover/claim)
SWITCH_ROOM_ID = os.getenv("SWITCH_ROOM_ID", "").strip()

# ==============================================================================
# Agent 1: TaskFlow Triage (The Ear - Ambient / High-Recall Classifier)
# ==============================================================================
SWITCH_TRIAGE_AGENT_NAME = os.getenv("SWITCH_TRIAGE_AGENT_NAME", "taskflow-triage").strip()
SWITCH_TRIAGE_AGENT_ID = os.getenv("SWITCH_TRIAGE_AGENT_ID", "8e390fbb-737c-4018-9960-a36bc1457fd6").strip()
SWITCH_TRIAGE_AGENT_API_KEY = os.getenv("SWITCH_TRIAGE_AGENT_API_KEY", "3w37cL1CxA0U9fnvU7SVZOeq96TQ-6GGCaBhwbOmKy0").strip()

# ==============================================================================
# Agent 2: TaskFlow Worker (The Hands - Autonomous Drafts & Todoist Scaffolder)
# ==============================================================================
SWITCH_WORKER_AGENT_NAME = os.getenv("SWITCH_WORKER_AGENT_NAME", "taskflow-worker").strip()
SWITCH_WORKER_AGENT_ID = os.getenv("SWITCH_WORKER_AGENT_ID", "deb3d8b9-c6f3-47b2-be0a-3f272d9ae425").strip()
SWITCH_WORKER_AGENT_API_KEY = os.getenv("SWITCH_WORKER_AGENT_API_KEY", "1uz2H6a5LiO9BfK_gEB8buN1qU-j1CYOww1v48w04-4").strip()

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

# LLM Providers
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()

# Task Tracker (Optional - Switch room tasks & documents are primary)
TODOIST_API_TOKEN = os.getenv("TODOIST_API_TOKEN", "").strip()

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
