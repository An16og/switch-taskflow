import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env from current directory
env_path = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=env_path)

# Switch Server Configuration
SWITCH_SERVER_URL = os.getenv("SWITCH_SERVER_URL", "http://127.0.0.1:52422").strip().rstrip("/")
SWITCH_WS_URL = os.getenv("SWITCH_WS_URL", SWITCH_SERVER_URL.replace("http://", "ws://").replace("https://", "wss://")).strip().rstrip("/")
AGENT_REGISTRATION_TOKEN = os.getenv("AGENT_REGISTRATION_TOKEN", "8EQjhvEdXZst0NB_7HGKjcjJO5itH_sx").strip()
SWITCH_AGENT_ID = os.getenv("SWITCH_AGENT_ID", "b8e848ea-edf6-4f14-8368-412b0745b88e").strip()
SWITCH_AGENT_API_KEY = os.getenv("SWITCH_AGENT_API_KEY", "u5PplbAq_PAnj-qXobDroWakOzCgA7htWAXCTO1-ejs").strip()
SWITCH_ROOM_ID = os.getenv("SWITCH_ROOM_ID", "a327cb7f-9cd0-45f8-8872-9a96022f8522").strip()

# Ambient Triage Policy ("addressed_only", "action_keywords", "all")
AMBIENT_TRIAGE_POLICY = os.getenv("AMBIENT_TRIAGE_POLICY", "action_keywords").strip().lower()

# Primary LLM: Google Gemini
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()

# Fallback LLMs (Optional, enabled automatically if present)
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()

# Task Tracker
TODOIST_API_TOKEN = os.getenv("TODOIST_API_TOKEN", "").strip()

# Switch Trust (Optional)
SWITCH_TRUST_API_KEY = os.getenv("SWITCH_TRUST_API_KEY", "").strip()

# Slack Tokens (Optional, used if direct Slack verification is needed)
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN", "").strip()
SLACK_APP_TOKEN = os.getenv("SLACK_APP_TOKEN", "").strip()

def check_env():
    """Validates essential environment keys required for TaskFlow to operate."""
    missing = []
    if not GEMINI_API_KEY and not OPENAI_API_KEY and not ANTHROPIC_API_KEY:
        missing.append("At least one LLM Key: GEMINI_API_KEY, OPENAI_API_KEY, or ANTHROPIC_API_KEY")
    if not TODOIST_API_TOKEN:
        missing.append("TODOIST_API_TOKEN")
    return missing
