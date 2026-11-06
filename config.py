import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env from current directory
env_path = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=env_path)

# Slack
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN", "").strip()
SLACK_APP_TOKEN = os.getenv("SLACK_APP_TOKEN", "").strip()

# Primary LLM: Google Gemini
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()

# Fallback LLMs (Optional, enabled automatically if present)
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()

# Task Tracker
TODOIST_API_TOKEN = os.getenv("TODOIST_API_TOKEN", "").strip()

# Switch Trust (Optional)
SWITCH_TRUST_API_KEY = os.getenv("SWITCH_TRUST_API_KEY", "").strip()

def check_env():
    missing = []
    if not SLACK_BOT_TOKEN:
        missing.append("SLACK_BOT_TOKEN (starts with xoxb-)")
    if not SLACK_APP_TOKEN:
        missing.append("SLACK_APP_TOKEN (starts with xapp-)")
    if not GEMINI_API_KEY and not OPENAI_API_KEY:
        missing.append("At least one LLM Key: GEMINI_API_KEY or OPENAI_API_KEY")
    if not TODOIST_API_TOKEN:
        missing.append("TODOIST_API_TOKEN")
    return missing
