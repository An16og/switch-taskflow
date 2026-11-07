"""Slack utility functions for TaskFlow.

Provides safe DM channel opening via conversations.open before chat_postMessage.
Note: On Switch, native agents use send_targeted_message instead of Slack DMs.
"""

from slack_cards import open_slack_dm, post_slack_dm

__all__ = ["open_slack_dm", "post_slack_dm"]
