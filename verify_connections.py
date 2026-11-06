import sys
if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')

from config import SLACK_BOT_TOKEN, GEMINI_API_KEY, TODOIST_API_TOKEN
from slack_sdk import WebClient
import requests
from google import genai

print("--- Testing API Connections ---")

# 1. Slack Auth Test
print("1. Testing Slack Bot Token...")
try:
    slack_client = WebClient(token=SLACK_BOT_TOKEN)
    auth = slack_client.auth_test()
    print(f"   [OK] Connected as Slack bot: @{auth['user']} (Team: {auth['team']})")
except Exception as e:
    print(f"   [FAIL] Slack Bot Token error: {e}")

# 2. Gemini API Test
print("2. Testing Gemini API Key...")
try:
    gemini_client = genai.Client(api_key=GEMINI_API_KEY)
    response = gemini_client.models.generate_content(
        model="gemini-3.5-flash",
        contents="Say 'Gemini OK' in 2 words."
    )
    print(f"   [OK] Gemini Response: {response.text.strip()}")
except Exception as e:
    print(f"   [FAIL] Gemini API error: {e}")

# 3. Todoist API Test
print("3. Testing Todoist API Token...")
try:
    headers = {"Authorization": f"Bearer {TODOIST_API_TOKEN}"}
    r = requests.get("https://api.todoist.com/api/v1/projects", headers=headers, timeout=10)
    if r.status_code == 200:
        projects = r.json()
        print(f"   [OK] Todoist Connected! Found {len(projects)} projects.")
    else:
        print(f"   [FAIL] Todoist error {r.status_code}: {r.text}")
except Exception as e:
    print(f"   [FAIL] Todoist error: {e}")

print("\n--- All tests completed ---")
