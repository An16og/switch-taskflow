# Changelog

All notable changes to **TaskFlow on Switch** are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.2.3] - 2026-11-07

### Added
- **Medium-Confidence Plan Preservation:** `build_assigner_card` and `handle_confirm_assignment` preserve the full AI worker-generated action checklist instead of discarding it.
- **Stub Elimination:** Removed legacy `* [ ] Review requirements and complete task` placeholder; assigners and assignees see complete, actionable step-by-step scaffolds.

## [1.2.2] - 2026-11-07

### Added
- **Safe Slack DM Opening:** Implemented `open_slack_dm` via `conversations.open` prior to `chat_postMessage` to prevent `channel_not_found` errors on uninitialized DM channels.
- **Native Switch Targeted Messaging:** Enforced Switch `send_targeted_message` as the primary private delivery channel on Switch Core, eliminating extraneous external Slack DM calls.

## [1.2.1] - 2026-11-07

### Added
- **Dynamic User Resolution Engine (`user_resolver.py`):** Resolves Slack `<@U...>` mentions, Switch `@handle` mentions, team directory aliases, and dynamic Switch room participants (`list_participants`).
- **Eliminated Hardcoded IDs:** Removed static `MY_USER_ID = "U0CFRP39TRS"` from `simulate_team.py`; resolved dynamically via `auth_test()`, environment overrides, or team directory.

## [1.2.0] - 2026-11-07

### Added
- **Native Switch Two-Agent Architecture:** Migrated from single-process Slack Bolt to native Switch Agent Protocol over persistent WebSocket connections (`taskflow-triage` and `taskflow-worker`).
- **Switch Trust SDK Guardrails:** Integrated `switch-trust-sdk-py` pre-invocation and post-invocation safety gates, including prompt injection sanitization, anti-jailbreak screening, and Data Loss Prevention (DLP) secret masking.
- **Role-Based Interactive Approvals:** Persisted deliverables in `PendingApprovalStore` with lightweight button payloads (< 100 bytes) and strict authorization checks bound to room roles (`owner`, `admin`, `lead`, `operator`).
- **Channel Backlog Triage Job:** On-demand full channel scanning with semantic deduplication (60% similarity threshold) and Todoist task scaffolding.
