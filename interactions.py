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
    build_approval_resolution_card
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

            # Execute side effect: sync to Todoist if accept/todoist
            if self.todoist_client and hasattr(self.todoist_client, "create_task"):
                try:
                    todo_res = self.todoist_client.create_task(
                        content=appr.action_summary,
                        description=f"{appr.artifact_content}\n\nApproved by @{user_name or user_id}",
                        due_string=appr.deadline
                    )
                    todoist_url = todo_res.get("url")
                except Exception as e:
                    logger.warning("Todoist sync failed on approval: %s", e)

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


# Global default dispatcher
default_dispatcher = InteractionDispatcher()
