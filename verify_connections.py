import sys
if sys.stdout.encoding != 'utf-8':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

import requests
from config import (
    SWITCH_SERVER_URL,
    SWITCH_ROOM_ID,
    SWITCH_TRIAGE_AGENT_ID,
    SWITCH_TRIAGE_AGENT_API_KEY,
    SWITCH_WORKER_AGENT_ID,
    SWITCH_WORKER_AGENT_API_KEY,
    GEMINI_API_KEY,
    OPENAI_API_KEY,
    ANTHROPIC_API_KEY,
    TODOIST_API_TOKEN,
)
from switch_client import SwitchAgentClient

print("=" * 65)
print("🔍 TaskFlow on Switch: Connection & Environment Verifier")
print("=" * 65)

# 1. Switch Server Health Test
print("\n1. Testing Switch Server Connection...")
try:
    health_resp = requests.get(f"{SWITCH_SERVER_URL}/health", timeout=5)
    if health_resp.status_code == 200:
        print(f"   [OK] Switch Server is healthy at {SWITCH_SERVER_URL} (status: {health_resp.json().get('status', 'ok')})")
    else:
        print(f"   [FAIL] Switch Server returned status {health_resp.status_code}: {health_resp.text}")
except Exception as e:
    print(f"   [FAIL] Could not connect to Switch Server at {SWITCH_SERVER_URL}: {e}")

# 2. Switch Agent & Room Bridge Test (Switch-connected Slack, NOT parallel Bolt)
print("\n2. Testing Switch Multi-Agent Credentials & Collaboration Bridge...")
room_id = SWITCH_ROOM_ID or "a327cb7f-9cd0-45f8-8872-9a96022f8522"
try:
    client = SwitchAgentClient(
        server_url=SWITCH_SERVER_URL,
        agent_id=SWITCH_TRIAGE_AGENT_ID,
        api_key=SWITCH_TRIAGE_AGENT_API_KEY,
        room_id=room_id,
    )
    bridge_info = client.get_room_bridge_info(room_id)
    print(f"   [OK] TaskFlow Triage Agent verified (ID: {SWITCH_TRIAGE_AGENT_ID[:12]}...)")
    
    if bridge_info.get("is_slack"):
        print(f"   [OK] Slack Bridge Active: Switch is the connected Slack app for room '{room_id}'")
        print(f"        Bridge Name: {bridge_info.get('bridge_name')}, Channel ID: {bridge_info.get('external_channel_id')}")
        print("        (Events are routed natively through Switch WebSocket; no parallel Bolt app running).")
    elif bridge_info.get("connected"):
        print(f"   [OK] External Bridge Active: {bridge_info.get('bridge_name')} connected to room '{room_id}'")
    else:
        print(f"   [INFO] Pure Switch Workroom Mode: No external Slack bridge attached to room '{room_id}'.")
        print("          Operating natively inside Switch Core workrooms (Zero Slack tokens required).")
except Exception as e:
    print(f"   [FAIL] Switch Agent / Bridge verification error: {e}")

# 3. LLM Gateway Provider Test
print("\n3. Testing LLM Gateway Provider...")
llm_tested = False
if GEMINI_API_KEY:
    try:
        from google import genai
        gemini_client = genai.Client(api_key=GEMINI_API_KEY)
        response = gemini_client.models.generate_content(
            model="gemini-3.5-flash-lite",
            contents="Respond with 'Gemini OK' in two words."
        )
        print(f"   [OK] Gemini API Connected! Response: {response.text.strip()}")
        llm_tested = True
    except Exception as e:
        print(f"   [FAIL] Gemini API error: {e}")

if not llm_tested and OPENAI_API_KEY:
    try:
        from openai import OpenAI
        openai_client = OpenAI(api_key=OPENAI_API_KEY)
        response = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": "Respond with 'OpenAI OK' in two words."}],
            max_tokens=10
        )
        print(f"   [OK] OpenAI API Connected! Response: {response.choices[0].message.content.strip()}")
        llm_tested = True
    except Exception as e:
        print(f"   [FAIL] OpenAI API error: {e}")

if not llm_tested and ANTHROPIC_API_KEY:
    try:
        from anthropic import Anthropic
        anthropic_client = Anthropic(api_key=ANTHROPIC_API_KEY)
        response = anthropic_client.messages.create(
            model="claude-3-5-haiku-latest",
            messages=[{"role": "user", "content": "Respond with 'Anthropic OK' in two words."}],
            max_tokens=10
        )
        print(f"   [OK] Anthropic API Connected! Response: {response.content[0].text.strip()}")
        llm_tested = True
    except Exception as e:
        print(f"   [FAIL] Anthropic API error: {e}")

if not llm_tested:
    print("   [FAIL] No active LLM API key configured! Please set GEMINI_API_KEY, OPENAI_API_KEY, or ANTHROPIC_API_KEY in .env.")

# 4. Todoist Task Tracker Test (Optional)
print("\n4. Testing Optional External Task Sync (Todoist)...")
if TODOIST_API_TOKEN:
    try:
        headers = {"Authorization": f"Bearer {TODOIST_API_TOKEN}"}
        r = requests.get("https://api.todoist.com/api/v1/projects", headers=headers, timeout=8)
        if r.status_code == 200:
            projects = r.json()
            print(f"   [OK] Todoist Connected! Found {len(projects)} active projects.")
        else:
            print(f"   [FAIL] Todoist returned status {r.status_code}: {r.text}")
    except Exception as e:
        print(f"   [FAIL] Todoist error: {e}")
else:
    print("   [INFO] Todoist is not configured. Tasks are tracked natively in Switch Workroom documents.")

print("\n" + "=" * 65)
print("✅ Verification Check Complete!")
print("=" * 65)
