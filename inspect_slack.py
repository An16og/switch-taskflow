from slack_sdk import WebClient
from config import SLACK_BOT_TOKEN

client = WebClient(token=SLACK_BOT_TOKEN)

print("--- Slack Workspace Inspection ---")
users = client.users_list()["members"]
print("Active Users:")
for u in users:
    if not u.get("is_bot") and not u.get("deleted"):
        print(f"  - User ID: {u['id']}, Handle: @{u['name']}, Real Name: {u.get('real_name')}")

channels = client.conversations_list(types="public_channel,private_channel")["channels"]
print("\nChannels:")
for c in channels:
    print(f"  - Channel ID: {c['id']}, Name: #{c['name']}, Bot is member: {c.get('is_member')}")
