"""Slack Block Kit card builders for TaskFlow.

Resolves Slack Block Kit 2,000-character payload limits:
Instead of serializing multi-kilobyte Markdown artifacts into interactive button
'value' attributes, artifacts are persisted in the PendingApprovalStore and
buttons only carry lightweight IDs (e.g., {"approval_id": "appr_...", "action": "approve"}).
"""

import json
from typing import Optional, Dict, Any
from approvals import PendingApprovalStore, default_approval_store, ApprovalStatus


def parse_button_payload(value_str: str) -> Dict[str, Any]:
    """Safely extracts approval_id and action from button payload."""
    try:
        data = json.loads(value_str)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {"approval_id": value_str, "action": "unknown"}


def build_auto_execute_card(
    action_summary: str,
    artifact_title: str,
    artifact_content: str,
    assigner_id: str,
    category: str = "Automated Work",
    permalink: Optional[str] = None,
    channel_id: Optional[str] = None,
    message_ts: Optional[str] = None,
    approval_id: Optional[str] = None,
    room_id: Optional[str] = None,
    switch_doc_id: Optional[str] = None,
    store: Optional[PendingApprovalStore] = None
) -> list:
    """Card sent when Worker Agent has AUTO-EXECUTED a routine task.
    
    Persists artifact in PendingApprovalStore and embeds lightweight ID on buttons.
    """
    active_store = store or default_approval_store

    if not approval_id:
        appr = active_store.create_approval(
            action_type="AUTO_EXECUTE",
            action_summary=action_summary,
            artifact_title=artifact_title,
            artifact_content=artifact_content,
            assigner_id=assigner_id,
            category=category,
            permalink=permalink,
            channel_id=channel_id,
            message_ts=message_ts,
            room_id=room_id,
            switch_doc_id=switch_doc_id
        )
        approval_id = appr.approval_id

    # Slack section text length safety (truncate display preview to 2500 chars)
    display_content = artifact_content
    if len(display_content) > 2500:
        display_content = display_content[:2500] + "\n\n*(Preview truncated for Slack display - full content saved in store)*"

    btn_approve_val = json.dumps({"approval_id": approval_id, "action": "approve"})
    btn_todoist_val = json.dumps({"approval_id": approval_id, "action": "accept"})
    btn_dismiss_val = json.dumps({"approval_id": approval_id, "action": "dismiss"})

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

    context_elements = []
    if permalink:
        context_elements.append({
            "type": "mrkdwn",
            "text": f"🔗 <{permalink}|View original request in Slack>"
        })
    if switch_doc_id:
        context_elements.append({
            "type": "mrkdwn",
            "text": f"📑 Switch Document ID: `{switch_doc_id}`"
        })
    if approval_id:
        context_elements.append({
            "type": "mrkdwn",
            "text": f"🆔 Approval: `{approval_id}`"
        })

    if context_elements:
        blocks.append({
            "type": "context",
            "elements": context_elements
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
                "value": btn_approve_val
            },
            {
                "type": "button",
                "text": {
                    "type": "plain_text",
                    "text": "📋 Save to Todoist",
                    "emoji": True
                },
                "action_id": "btn_accept_task",
                "value": btn_todoist_val
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
                "value": btn_dismiss_val
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
    message_ts: Optional[str] = None,
    approval_id: Optional[str] = None,
    room_id: Optional[str] = None,
    switch_doc_id: Optional[str] = None,
    store: Optional[PendingApprovalStore] = None
) -> list:
    """Card sent to Assignee for HUMAN_TODO tasks with AI Action Checklist.
    
    Persists checklist and stores lightweight ID on interactive buttons.
    """
    active_store = store or default_approval_store

    if not approval_id:
        appr = active_store.create_approval(
            action_type="HUMAN_TODO",
            action_summary=action_summary,
            artifact_title=f"Action Plan: {action_summary}",
            artifact_content=action_plan,
            assigner_id=assigner_id,
            deadline=deadline,
            context_notes=context_notes,
            permalink=permalink,
            channel_id=channel_id,
            message_ts=message_ts,
            room_id=room_id,
            switch_doc_id=switch_doc_id
        )
        approval_id = appr.approval_id

    btn_accept_val = json.dumps({"approval_id": approval_id, "action": "accept"})
    btn_dismiss_val = json.dumps({"approval_id": approval_id, "action": "dismiss"})

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

    context_elements = []
    if permalink:
        context_elements.append({
            "type": "mrkdwn",
            "text": f"🔗 <{permalink}|View conversation in Slack>"
        })
    if approval_id:
        context_elements.append({
            "type": "mrkdwn",
            "text": f"🆔 Approval: `{approval_id}`"
        })

    if context_elements:
        blocks.append({
            "type": "context",
            "elements": context_elements
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
                "value": btn_accept_val
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
                "value": btn_dismiss_val
            }
        ]
    })

    return blocks


def build_assignee_card(
    action_summary: str,
    assigner_id: str,
    action_plan: str = "",
    deadline: Optional[str] = None,
    context_notes: Optional[str] = None,
    permalink: Optional[str] = None,
    channel_id: Optional[str] = None,
    message_ts: Optional[str] = None,
    approval_id: Optional[str] = None,
    room_id: Optional[str] = None,
    switch_doc_id: Optional[str] = None,
    store: Optional[PendingApprovalStore] = None
) -> list:
    """Backward compatibility alias for build_human_todo_card."""
    return build_human_todo_card(
        action_summary=action_summary,
        assigner_id=assigner_id,
        action_plan=action_plan,
        deadline=deadline,
        context_notes=context_notes,
        permalink=permalink,
        channel_id=channel_id,
        message_ts=message_ts,
        approval_id=approval_id,
        room_id=room_id,
        switch_doc_id=switch_doc_id,
        store=store
    )


def build_assigner_card(
    action_summary: str,
    target_assignee: str,
    target_assignee_id: Optional[str] = None,
    deadline: Optional[str] = None,
    context_notes: Optional[str] = None,
    permalink: Optional[str] = None,
    channel_id: Optional[str] = None,
    message_ts: Optional[str] = None,
    approval_id: Optional[str] = None,
    room_id: Optional[str] = None,
    store: Optional[PendingApprovalStore] = None
) -> list:
    """Card sent to Assigner for MEDIUM CONFIDENCE tasks.
    
    Persists proposal in PendingApprovalStore and embeds lightweight ID on buttons.
    """
    active_store = store or default_approval_store

    if not approval_id:
        appr = active_store.create_approval(
            action_type="ASSIGNMENT",
            action_summary=action_summary,
            artifact_title=f"Task Assignment: {action_summary}",
            artifact_content=context_notes or "",
            assignee_id=target_assignee_id,
            assignee_name=target_assignee,
            deadline=deadline,
            context_notes=context_notes,
            permalink=permalink,
            channel_id=channel_id,
            message_ts=message_ts,
            room_id=room_id
        )
        approval_id = appr.approval_id

    btn_confirm_val = json.dumps({"approval_id": approval_id, "action": "confirm"})
    btn_dismiss_val = json.dumps({"approval_id": approval_id, "action": "dismiss"})

    assignee_display = f"<@{target_assignee_id}>" if target_assignee_id else target_assignee

    blocks = [
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
        }
    ]

    context_elements = []
    if permalink:
        context_elements.append({
            "type": "mrkdwn",
            "text": f"🔗 <{permalink}|View conversation in Slack>"
        })
    if approval_id:
        context_elements.append({
            "type": "mrkdwn",
            "text": f"🆔 Approval: `{approval_id}`"
        })

    if context_elements:
        blocks.append({
            "type": "context",
            "elements": context_elements
        })

    blocks.append({
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
                "value": btn_confirm_val
            },
            {
                "type": "button",
                "text": {
                    "type": "plain_text",
                    "text": "❌ No, Just Chat",
                    "emoji": True
                },
                "action_id": "btn_dismiss_task",
                "value": btn_dismiss_val
            }
        ]
    })

    return blocks


def build_approval_resolution_card(
    action_summary: str,
    status: str,
    resolved_by: str,
    artifact_title: Optional[str] = None,
    todoist_url: Optional[str] = None,
    permalink: Optional[str] = None,
) -> list:
    """Generates confirmation blocks to replace interactive buttons after user clicks an action."""
    status_upper = status.upper()
    if status_upper in ("APPROVED", "CONFIRM", "ACCEPT"):
        status_text = f"✅ *Task Approved & Executed by <@{resolved_by}>*"
    elif status_upper in ("REJECTED", "DISMISSED", "DISMISS"):
        status_text = f"❌ *Dismissed by <@{resolved_by}>*"
    else:
        status_text = f"ℹ️ *Status: {status} (by <@{resolved_by}>)*"

    lines = [
        status_text,
        f"📌 *Task:* {action_summary}",
    ]
    if artifact_title:
        lines.append(f"📄 *Artifact:* {artifact_title}")
    if todoist_url:
        lines.append(f"📋 <{todoist_url}|View in Todoist>")
    if permalink:
        lines.append(f"🔗 <{permalink}|Original Slack Message>")

    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "\n".join(lines)
            }
        }
    ]


def build_receipt_blocks(
    action_summary: str,
    deadline: Optional[str],
    permalink: Optional[str],
    todoist_url: Optional[str] = None
) -> list:
    """Card sent after task creation/sync to confirm state."""
    text_lines = [
        "✅ *Task added to Todoist!*",
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
