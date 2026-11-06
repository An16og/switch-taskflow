import json
from typing import Optional

def build_auto_execute_card(
    action_summary: str,
    artifact_title: str,
    artifact_content: str,
    assigner_id: str,
    category: str = "Automated Work",
    permalink: Optional[str] = None,
    channel_id: Optional[str] = None,
    message_ts: Optional[str] = None
) -> list:
    """
    Card sent when Worker Agent has AUTO-EXECUTED a routine task (docs, release notes, summaries).
    """
    # Slack text block length safety (truncate if massive)
    display_content = artifact_content
    if len(display_content) > 2500:
        display_content = display_content[:2500] + "\n\n*(Preview truncated for Slack display)*"

    payload_state = json.dumps({
        "action_summary": action_summary,
        "artifact_title": artifact_title,
        "artifact_content": artifact_content,
        "assigner_id": assigner_id,
        "category": category,
        "permalink": permalink,
        "channel_id": channel_id,
        "message_ts": message_ts
    })

    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": "⚡ TaskFlow Worker: Task Auto-Executed!",
                "emoji": True
            }
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"I noticed a routine knowledge task in the room: *\"{action_summary}\"*\n*Category:* `{category}`. I have generated the completed draft below:"
            }
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*📄 {artifact_title}*\n```\n{display_content}\n```"
            }
        }
    ]

    if permalink:
        blocks.append({
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"🔗 <{permalink}|View original request in Slack>"
                }
            ]
        })

    blocks.append({
        "type": "actions",
        "elements": [
            {
                "type": "button",
                "text": {
                    "type": "plain_text",
                    "text": "🚀 Approve & Post to Channel",
                    "emoji": True
                },
                "style": "primary",
                "action_id": "btn_approve_auto_execute",
                "value": payload_state
            },
            {
                "type": "button",
                "text": {
                    "type": "plain_text",
                    "text": "📋 Save to Todoist",
                    "emoji": True
                },
                "action_id": "btn_accept_task",
                "value": payload_state
            },
            {
                "type": "button",
                "text": {
                    "type": "plain_text",
                    "text": "❌ Dismiss",
                    "emoji": True
                },
                "style": "danger",
                "action_id": "btn_dismiss_task",
                "value": payload_state
            }
        ]
    })

    return blocks


def build_human_todo_card(
    action_summary: str,
    assigner_id: str,
    action_plan: str,
    deadline: Optional[str] = None,
    context_notes: Optional[str] = None,
    permalink: Optional[str] = None,
    channel_id: Optional[str] = None,
    message_ts: Optional[str] = None
) -> list:
    """
    Card sent to Assignee for HUMAN_TODO tasks, enriched with an AI Action Checklist.
    """
    payload_state = json.dumps({
        "action_summary": action_summary,
        "assigner_id": assigner_id,
        "action_plan": action_plan,
        "deadline": deadline,
        "context_notes": context_notes,
        "permalink": permalink,
        "channel_id": channel_id,
        "message_ts": message_ts
    })

    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": "📌 TaskFlow: Task Assigned to You",
                "emoji": True
            }
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"Hey! <@{assigner_id}> mentioned a task for you in the room:"
            }
        },
        {
            "type": "section",
            "fields": [
                {
                    "type": "mrkdwn",
                    "text": f"*📌 Task:*\n{action_summary}"
                },
                {
                    "type": "mrkdwn",
                    "text": f"*⏰ Due Date:*\n{deadline or 'No deadline specified'}"
                }
            ]
        }
    ]

    if action_plan:
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*🤖 AI Action Breakdown:*\n{action_plan}"
            }
        })

    if permalink:
        blocks.append({
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"🔗 <{permalink}|View conversation in Slack>"
                }
            ]
        })

    blocks.append({
        "type": "actions",
        "elements": [
            {
                "type": "button",
                "text": {
                    "type": "plain_text",
                    "text": "✅ Accept & Add to Todoist",
                    "emoji": True
                },
                "style": "primary",
                "action_id": "btn_accept_task",
                "value": payload_state
            },
            {
                "type": "button",
                "text": {
                    "type": "plain_text",
                    "text": "❌ Dismiss",
                    "emoji": True
                },
                "style": "danger",
                "action_id": "btn_dismiss_task",
                "value": payload_state
            }
        ]
    })

    return blocks


def build_assigner_card(
    action_summary: str,
    target_assignee: str,
    target_assignee_id: Optional[str] = None,
    deadline: Optional[str] = None,
    context_notes: Optional[str] = None,
    permalink: Optional[str] = None,
    channel_id: Optional[str] = None,
    message_ts: Optional[str] = None
) -> list:
    """
    Card sent to Assigner for MEDIUM CONFIDENCE tasks.
    """
    payload_state = json.dumps({
        "action_summary": action_summary,
        "target_assignee_id": target_assignee_id,
        "target_assignee": target_assignee,
        "deadline": deadline,
        "context_notes": context_notes,
        "permalink": permalink,
        "channel_id": channel_id,
        "message_ts": message_ts
    })

    assignee_display = f"<@{target_assignee_id}>" if target_assignee_id else target_assignee

    return [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": "🤔 TaskFlow: Did you mean to assign this?",
                "emoji": True
            }
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"You mentioned an action item in the room. Should I track this for {assignee_display}?"
            }
        },
        {
            "type": "section",
            "fields": [
                {
                    "type": "mrkdwn",
                    "text": f"*📌 Action:*\n{action_summary}"
                },
                {
                    "type": "mrkdwn",
                    "text": f"*⏰ Due:*\n{deadline or 'None'}"
                }
            ]
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": "✅ Yes, Assign Task",
                        "emoji": True
                    },
                    "style": "primary",
                    "action_id": "btn_confirm_assignment",
                    "value": payload_state
                },
                {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": "❌ No, Just Chat",
                        "emoji": True
                    },
                    "action_id": "btn_dismiss_task",
                    "value": payload_state
                }
            ]
        }
    ]


def build_receipt_blocks(
    action_summary: str,
    deadline: Optional[str],
    permalink: Optional[str],
    todoist_url: Optional[str] = None
) -> list:
    text_lines = [
        f"✅ *Task added to Todoist!*",
        f"📌 *Task:* {action_summary}",
        f"⏰ *Due:* {deadline or 'None'}"
    ]
    if permalink:
        text_lines.append(f"🔗 <{permalink}|Slack Thread>")
    if todoist_url:
        text_lines.append(f"📋 <{todoist_url}|Open in Todoist>")

    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "\n".join(text_lines)
            }
        }
    ]
