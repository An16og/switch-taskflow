"""
Unit tests for UserResolver and Assignee Resolution.

Validates that:
1. In-text mentions (@Anuj, <@U0CFRP39TRS>) resolve to that user, NEVER defaulting to the sender.
2. Team roster aliases ('anuj', 'anuj jain', 'anujjainaj16') correctly map to user IDs and names.
3. Switch participants retrieved from list_participants are dynamically registered and resolved.
4. Self-commitments ('I will...', 'I'll...') resolve to the sender.
5. Bot handles (@taskflow, @taskflow-triage) are excluded from being assigned.
"""

import unittest
from user_resolver import UserResolver, default_user_resolver


class TestUserResolver(unittest.TestCase):
    def setUp(self):
        self.resolver = UserResolver()

    def test_anuj_mention_resolves_to_anuj_not_sender(self):
        """CRITICAL: If Priya delegates a task to @Anuj, assignee is Anuj, NOT Priya (the sender)."""
        message = "@Anuj please review and benchmark the database migration PR #89 before Friday 5 PM."
        assignee_name, assignee_id = self.resolver.resolve_assignee(
            triage_assignee_name="Anuj",
            triage_assignee_id=None,
            message_text=message,
            sender_id="U_PRIYA_LEAD",
            sender_name="Priya (Tech Lead)",
        )
        self.assertEqual(assignee_name, "Anuj")
        self.assertEqual(assignee_id, "U0CFRP39TRS")
        self.assertNotEqual(assignee_name, "Priya (Tech Lead)")
        self.assertNotEqual(assignee_id, "U_PRIYA_LEAD")

    def test_slack_syntax_mention_resolution(self):
        """Verify Slack mention <@U0CFRP39TRS|anujjain> resolves to Anuj."""
        message = "Can <@U0CFRP39TRS|anujjain> please inspect the connection pool?"
        assignee_name, assignee_id = self.resolver.resolve_assignee(
            triage_assignee_name=None,
            triage_assignee_id=None,
            message_text=message,
            sender_id="U_ALEX_PM",
            sender_name="Alex",
        )
        self.assertEqual(assignee_name, "Anuj")
        self.assertEqual(assignee_id, "U0CFRP39TRS")

    def test_triage_model_assignee_name_resolves_via_roster(self):
        """Even without @ prefix, if LLM extracts 'anuj', it maps to Anuj from roster."""
        message = "Someone urgently needs to check the cache, Anuj can you handle it?"
        assignee_name, assignee_id = self.resolver.resolve_assignee(
            triage_assignee_name="Anuj",
            triage_assignee_id=None,
            message_text=message,
            sender_id="U_SARAH",
            sender_name="Sarah",
        )
        self.assertEqual(assignee_name, "Anuj")
        self.assertEqual(assignee_id, "U0CFRP39TRS")
        self.assertNotEqual(assignee_name, "Sarah")

    def test_switch_participants_dynamic_registration(self):
        """Verify participants from Switch list_participants are registered and resolvable."""
        mock_participants = [
            {"id": "U_KUNAL_DEV", "name": "Kunal", "kind": "user", "roles": ["developer"]},
            {"id": "U_ROHIT_QA", "name": "Rohit", "kind": "user", "roles": ["lead"]},
        ]
        message = "@Rohit please verify the staging deployment."
        assignee_name, assignee_id = self.resolver.resolve_assignee(
            triage_assignee_name=None,
            triage_assignee_id=None,
            message_text=message,
            sender_id="U_ALEX",
            sender_name="Alex",
            participants=mock_participants,
        )
        self.assertEqual(assignee_name, "Rohit")
        self.assertEqual(assignee_id, "U_ROHIT_QA")

    def test_self_commitment_resolves_to_sender(self):
        """Verify self-commitments like 'I will...' resolve to the sender."""
        message = "I will take care of the database connection timeouts before tomorrow morning."
        assignee_name, assignee_id = self.resolver.resolve_assignee(
            triage_assignee_name=None,
            triage_assignee_id=None,
            message_text=message,
            sender_id="U_SARAH_DEV",
            sender_name="Sarah",
        )
        self.assertEqual(assignee_name, "Sarah")
        self.assertEqual(assignee_id, "U_SARAH_DEV")

    def test_bot_handles_are_never_assigned(self):
        """Verify bot mentions like @taskflow or @taskflow-triage are never treated as human assignees."""
        message = "@taskflow-triage please draft release notes for v1.3 with dark mode."
        assignee_name, assignee_id = self.resolver.resolve_assignee(
            triage_assignee_name=None,
            triage_assignee_id=None,
            message_text=message,
            sender_id="U_KUNAL",
            sender_name="Kunal",
        )
        # Should default to the requesting sender (Kunal), NOT @taskflow-triage
        self.assertEqual(assignee_name, "Kunal")
        self.assertEqual(assignee_id, "U_KUNAL")


if __name__ == "__main__":
    unittest.main()
