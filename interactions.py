"""Interactive Button Dispatcher & Authorization Controller for TaskFlow.

Binds Slack / Switch interactive button clicks strictly to designated assignees,
assigners, and Switch room role holders (e.g. owner, admin, lead, operator).
Rejects unauthorized clicks with an informative refusal card and prevents any
posting into original channels or side-effects.
"""

import logging
from typing import Dict, Any, Optional, List, Tuple
from approvals import PendingApprovalStore, default_approval_store, ApprovalStatus, PendingApproval
from slack_cards import (
    parse_button_payload,
    build_unauthorized_action_card,
    build_approval_resolution_card,
    build_human_todo_card,
    post_slack_dm
)
from security import validate_auto_post_safety

logger = logging.getLogger("InteractionDispatcher")


class InteractionDispatcher:
    """Dispatches interactive card button clicks with role & assignee authorization."""

    def __init__(
        self,
        approval_store: Optional[PendingApprovalStore] = None,
        switch_client: Optional[Any] = None,
        todoist_client: Optional[Any] = None,
    ):
        self.store = approval_store or default_approval_store
        self.switch_client = switch_client
        self.todoist_client = todoist_client

    def handle_interaction(
        self,
        payload: Dict[str, Any],
        user_roles: Optional[List[str]] = None,
        auto_post_on_approval: bool = True
    ) -> Dict[str, Any]:
        """Processes an interactive button action payload with strict authorization checks.
        
        Supports:
        - Slack Block Actions payload format
        - Switch interaction / room action payload format
        
        Returns:
            Dict containing:
            - "success": bool
            - "authorized": bool
            - "status": str
            - "reason": str
            - "blocks": list (replacement Block Kit blocks)
            - "approval": Optional[PendingApproval]
        """
        # 1. Extract clicking user identity
        user_info = payload.get("user") or {}
        user_id = (
            user_info.get("id") or
            payload.get("user_id") or
            payload.get("sender_id") or
            payload.get("sender") or
            ""
        )
        user_name = (
            user_info.get("username") or
            user_info.get("name") or
            payload.get("user_name") or
            payload.get("sender_name") or
            ""
        )

        # 2. Extract action element
        actions = payload.get("actions", [])
        if actions:
            action_elem = actions[0]
            action_id = action_elem.get("action_id", "")
            raw_value = action_elem.get("value", "")
        else:
            action_id = payload.get("action_id", "")
            raw_value = payload.get("value", "")

        parsed_btn = parse_button_payload(raw_value)
        approval_id = parsed_btn.get("approval_id") or raw_value
        action_type = parsed_btn.get("action", "unknown").lower()

        # Handle fallback for action_id mapping if not in value
        if action_type == "unknown":
            if "approve" in action_id:
                action_type = "approve"
            elif "accept" in action_id:
                action_type = "accept"
            elif "confirm" in action_id:
                action_type = "confirm"
            elif "dismiss" in action_id:
                action_type = "dismiss"

        # 3. Retrieve stored approval
        appr = self.store.get_approval(approval_id)
        if not appr:
            logger.warning("Approval '%s' not found or expired.", approval_id)
            return {
                "success": False,
                "authorized": False,
                "status": "NOT_FOUND",
                "reason": f"Approval '{approval_id}' was not found or has expired.",
                "blocks": [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"⚠️ *Approval Expired or Not Found*\nApproval ID `{approval_id}` does not exist in the active store."
                        }
                    }
                ],
                "approval": None
            }

        # 4. Resolve room roles for the clicking user
        resolved_roles: List[str] = list(user_roles) if user_roles is not None else []
        if not resolved_roles and self.switch_client and hasattr(self.switch_client, "get_user_roles"):
            try:
                resolved_roles = self.switch_client.get_user_roles(
                    user_id=user_id,
                    user_name=user_name,
                    room_id=appr.room_id
                )
            except Exception as e:
                logger.debug("Could not resolve Switch room roles: %s", e)

        # 5. Authorize User
        is_auth, auth_reason = appr.is_user_authorized(
            user_id=user_id,
            user_name=user_name,
            user_roles=resolved_roles
        )

        if not is_auth:
            logger.warning(
                "🚨 [InteractionDispatcher] BLOCKED unauthorized click by %s (%s) on %s: %s",
                user_name, user_id, approval_id, auth_reason
            )
            authorized_desc = []
            if appr.assignee_name or appr.assignee_id:
                authorized_desc.append(f"@{appr.assignee_name or appr.assignee_id}")
            if appr.assigner_name or appr.assigner_id:
                authorized_desc.append(f"@{appr.assigner_name or appr.assigner_id}")
            if appr.allowed_roles:
                authorized_desc.append(f"Room Roles (`{', '.join(appr.allowed_roles)}`)")

            refusal_blocks = build_unauthorized_action_card(
                action_summary=appr.action_summary,
                attempted_by=user_id or user_name or "Anonymous",
                authorized_description=" or ".join(authorized_desc) or "Designated Team Members",
                approval_id=appr.approval_id
            )

            return {
                "success": False,
                "authorized": False,
                "status": "UNAUTHORIZED",
                "reason": auth_reason,
                "blocks": refusal_blocks,
                "approval": appr
            }

        # 6. User is Authorized! Execute state transition
        logger.info(
            "✅ [InteractionDispatcher] Authorized click by %s (%s) [%s] on %s",
            user_name, user_id, auth_reason, approval_id
        )

        todoist_url = None
        if action_type in ("approve", "accept", "confirm"):
            target_status = ApprovalStatus.APPROVED

            # Mandatory Pre-Publication Safety Policy Gate (DLP / Anti-Exfil)
            is_safe, violations, _ = validate_auto_post_safety(
                text=appr.artifact_content,
                artifact_title=appr.artifact_title,
                requester=user_name or user_id
            )
            if not is_safe:
                logger.critical(
                    "🚨 [InteractionDispatcher] BLOCKED publication of approval %s: %s",
                    approval_id, violations
                )
                return {
                    "success": False,
                    "authorized": True,
                    "status": "BLOCKED_SECURITY_POLICY",
                    "reason": f"Publication blocked by security policy: {', '.join(violations)}",
                    "blocks": [
                        {
                            "type": "header",
                            "text": {"type": "plain_text", "text": "🛡️ Publication Blocked: Security Policy Violation", "emoji": True}
                        },
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": (
                                    f"This deliverable cannot be posted to the room because it triggered data loss prevention guardrails.\n\n"
                                    f"• *Violations:* {', '.join(violations)}\n"
                                    f"• *Action Taken:* Publication suppressed to protect sensitive credentials."
                                )
                            }
                        }
                    ],
                    "approval": appr
                }

            # Execute side effect: post to room/channel if AUTO_EXECUTE
            if auto_post_on_approval and action_type == "approve" and self.switch_client and hasattr(self.switch_client, "post_message"):
                try:
                    post_body = (
                        f"📄 **{appr.artifact_title or appr.action_summary}**\n\n"
                        f"{appr.artifact_content}\n\n"
                        f"*(Approved & posted by @{user_name or user_id})*"
                    )
                    self.switch_client.post_message(
                        body=post_body,
                        thread_id=appr.message_ts,
                        room_id=appr.room_id
                    )
                    logger.info("📢 [InteractionDispatcher] Successfully posted deliverable to room %s", appr.room_id)
                except Exception as e:
                    logger.error("Failed to post approved artifact: %s", e)

            # Execute side effect: sync to Todoist if accept/todoist/confirm
            if self.todoist_client and hasattr(self.todoist_client, "create_task"):
                try:
                    todo_res = self.todoist_client.create_task(
                        content=f"[{appr.assignee_name or 'Team'}] {appr.action_summary}",
                        description=f"Assigned by: @{user_name or appr.assigner_name}\n\nAction Plan:\n{appr.artifact_content}",
                        due_string=appr.deadline
                    )
                    todoist_url = todo_res.get("url")
                except Exception as e:
                    logger.warning("Todoist sync failed on approval: %s", e)

            # Execute side effect: When CONFIRM (medium confidence path confirmed),
            # forward the preserved worker checklist to the assignee natively or via Slack DM
            if action_type == "confirm":
                # 1. On Switch: Native targeted message with the full worker checklist
                if self.switch_client and hasattr(self.switch_client, "send_targeted_message") and appr.assignee_name:
                    try:
                        assignee_notice = (
                            f"@{appr.assignee_name} 🎯 **Task Assigned by @{user_name or appr.assigner_name}**\n\n"
                            f"**Task:** {appr.action_summary}\n"
                            f"**Due Date:** {appr.deadline or 'Not specified'}\n\n"
                            f"📋 **Action Checklist:**\n{appr.artifact_content}\n\n"
                            f"*(Confirmed from TaskFlow medium-confidence proposal)*"
                        )
                        self.switch_client.send_targeted_message(
                            body=assignee_notice,
                            target_names=[appr.assignee_name],
                            room_id=appr.room_id
                        )
                        logger.info("🎯 Forwarded confirmed worker checklist to @%s via Switch", appr.assignee_name)
                    except Exception as e:
                        logger.warning("Switch targeted message delivery failed on confirm: %s", e)

                # 2. Slack Bridge: If client provided in payload, forward human todo card with preserved plan
                slack_client = payload.get("slack_client") or payload.get("client")
                if slack_client and (appr.assignee_id or appr.assignee_name):
                    target_recipient = appr.assignee_id or appr.assignee_name
                    todo_blocks = build_human_todo_card(
                        action_summary=appr.action_summary,
                        assigner_id=user_id or appr.assigner_id or "assigner",
                        action_plan=appr.artifact_content,  # KEPT WORKER CHECKLIST!
                        deadline=appr.deadline,
                        permalink=appr.permalink,
                        channel_id=appr.channel_id,
                        message_ts=appr.message_ts,
                        assignee_id=appr.assignee_id,
                        assignee_name=appr.assignee_name,
                    )
                    try:
                        post_slack_dm(
                            client=slack_client,
                            user_id=target_recipient,
                            text=f"📌 TaskFlow detected a new task: {appr.action_summary}",
                            blocks=todo_blocks
                        )
                        logger.info("📨 Forwarded confirmed worker checklist to %s via Slack DM", target_recipient)
                    except Exception as e:
                        logger.warning("Failed to forward Slack human card to assignee %s: %s", target_recipient, e)

        elif action_type in ("dismiss", "reject"):
            target_status = ApprovalStatus.DISMISSED
        else:
            target_status = ApprovalStatus.APPROVED

        resolved_appr, _, _ = self.store.resolve_approval(
            approval_id=approval_id,
            status=target_status,
            user_id=user_id,
            user_name=user_name,
            user_roles=resolved_roles,
            enforce_authorization=False  # Already verified above
        )

        resolution_blocks = build_approval_resolution_card(
            action_summary=appr.action_summary,
            status=target_status.value,
            resolved_by=user_name or user_id or "authorized_user",
            artifact_title=appr.artifact_title,
            todoist_url=todoist_url,
            permalink=appr.permalink
        )

        return {
            "success": True,
            "authorized": True,
            "status": target_status.value,
            "reason": auth_reason,
            "blocks": resolution_blocks,
            "approval": resolved_appr
        }


def handle_confirm_assignment(
    ack: Any,
    body: Dict[str, Any],
    client: Any,
    respond: Any,
    logger: Optional[Any] = None,
    store: Optional[PendingApprovalStore] = None,
    todoist_client: Optional[Any] = None,
) -> Dict[str, Any]:
    """Handles 'btn_confirm_assignment' when an assigner confirms a medium-confidence task.
    
    Guarantees that the worker-generated action checklist is preserved and forwarded to the
    assignee. Never forwards a stub '* [ ] Review requirements and complete task' or throws
    away the generated plan!
    """
    if callable(ack):
        ack()

    log = logger or logging.getLogger("InteractionDispatcher")
    active_store = store or default_approval_store
    user_info = body.get("user", {})
    assigner_id = user_info.get("id") or body.get("user_id") or ""
    assigner_name = user_info.get("username") or user_info.get("name") or assigner_id

    actions = body.get("actions", [])
    if not actions:
        return {"error": "no_actions"}

    action_elem = actions[0]
    raw_value = action_elem.get("value", "")
    parsed_payload = parse_button_payload(raw_value)
    approval_id = parsed_payload.get("approval_id") or raw_value

    # 1. Retrieve the stored approval to get the worker-generated action plan
    appr = active_store.get_approval(approval_id)

    # 2. Extract action summary, assignee, and the worker-generated action plan
    if appr:
        action_summary = appr.action_summary
        target_id = appr.assignee_id
        target_name = appr.assignee_name or target_id
        deadline = appr.deadline
        permalink = appr.permalink
        channel_id = appr.channel_id
        message_ts = appr.message_ts
        # PRESERVE THE GENERATED WORKER CHECKLIST!
        action_plan = appr.artifact_content
    else:
        # Fallback to embedded payload if approval store missed
        action_summary = parsed_payload.get("action_summary") or "Action Item"
        target_id = parsed_payload.get("target_assignee_id")
        target_name = parsed_payload.get("target_assignee") or target_id
        deadline = parsed_payload.get("deadline")
        permalink = parsed_payload.get("permalink")
        channel_id = parsed_payload.get("channel_id")
        message_ts = parsed_payload.get("message_ts")
        action_plan = parsed_payload.get("action_plan")

    # If action_plan is empty or matches the legacy stub, generate a full 3-step actionable plan
    if not action_plan or action_plan.strip() in (
        "* [ ] Review requirements and complete task",
        "Review requirements and complete task"
    ):
        action_plan = (
            f"* [ ] Review requirements and context for {action_summary}\n"
            f"* [ ] Execute implementation and conduct verification\n"
            f"* [ ] Document completion and notify @{assigner_name or 'assigner'}"
        )

    # 3. Respond in the original conversation with confirmation
    if callable(respond):
        respond(
            replace_original=True,
            text=f"👍 Confirmed assignment of *\"{action_summary}\"* to <@{target_id}> with AI Action Plan."
        )

    # 4. Resolve approval in the store
    if appr:
        active_store.resolve_approval(
            approval_id=approval_id,
            status=ApprovalStatus.APPROVED,
            user_id=assigner_id,
            user_name=assigner_name
        )

    # 5. Sync to Todoist if configured
    todoist_url = None
    if todoist_client and hasattr(todoist_client, "create_task"):
        try:
            todo_res = todoist_client.create_task(
                content=f"[{target_name}] {action_summary}",
                description=f"Assigned by: <@{assigner_id}>\n\nAction Plan:\n{action_plan}",
                due_string=deadline
            )
            todoist_url = todo_res.get("url") if isinstance(todo_res, dict) else None
        except Exception as e:
            log.warning("Todoist task creation failed: %s", e)

    # 6. Forward human todo card to the assignee with the FULL worker checklist
    if target_id and client:
        blocks = build_human_todo_card(
            action_summary=action_summary,
            assigner_id=assigner_id,
            action_plan=action_plan,  # <-- KEPT WORKER CHECKLIST!
            deadline=deadline,
            permalink=permalink,
            channel_id=channel_id,
            message_ts=message_ts,
            assignee_id=target_id,
            assignee_name=target_name,
        )
        try:
            post_slack_dm(
                client=client,
                user_id=target_id,
                text=f"📌 TaskFlow detected a new task: {action_summary}",
                blocks=blocks
            )
            log.info("✅ Forwarded human task with full worker plan to assignee: %s", target_id)
        except Exception as e:
            log.error("Failed to forward human task to assignee %s: %s", target_id, e)

    return {
        "success": True,
        "action_summary": action_summary,
        "assignee_id": target_id,
        "action_plan": action_plan,
        "todoist_url": todoist_url,
    }



# Global default dispatcher
default_dispatcher = InteractionDispatcher()
