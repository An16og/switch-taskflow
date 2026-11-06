import sys
import json

# Ensure UTF-8 output on Windows console
if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')
from models import TaskExtraction, ConfidenceLevel, SlackMessage
from slack_cards import build_assignee_card, build_assigner_card, build_receipt_blocks

def test_pipeline():
    print("1. Testing Pydantic TaskExtraction validation...")
    task = TaskExtraction(
        is_task=True,
        confidence=ConfidenceLevel.HIGH,
        confidence_reason="Explicit request with deadline",
        action_summary="Fix staging auth token bug",
        assignee_id="U123456",
        assignee_name="Rohit",
        deadline="Tomorrow 5 PM",
        context_notes="Demo is on Friday",
        needs_more_context=False
    )
    assert task.is_task is True
    assert task.confidence == ConfidenceLevel.HIGH
    print("   ✓ TaskExtraction model valid")

    print("2. Testing Slack Block Kit generation...")
    blocks = build_assignee_card(
        action_summary=task.action_summary,
        assigner_id="U999999",
        deadline=task.deadline,
        context_notes=task.context_notes,
        permalink="https://slack.com/archives/C123/p123456789",
        channel_id="C123",
        message_ts="123456789.00"
    )
    assert len(blocks) >= 4
    # Check that action button contains payload
    action_elem = blocks[-1]["elements"][0]
    payload = json.loads(action_elem["value"])
    assert payload["action_summary"] == "Fix staging auth token bug"
    print("   ✓ Assignee Block Kit card generated with valid JSON payload")

    print("3. Testing Assigner card generation...")
    assigner_blocks = build_assigner_card(
        action_summary="Update documentation",
        target_assignee="Alex",
        target_assignee_id="U555555",
        deadline="EOD",
        context_notes="Client onboarding",
        permalink="https://slack.com/archives/C123/p123"
    )
    assert len(assigner_blocks) >= 3
    print("   ✓ Assigner Block Kit card generated")

    print("4. Testing Receipt card generation...")
    receipt = build_receipt_blocks(
        action_summary="Fix staging auth token bug",
        deadline="Tomorrow 5 PM",
        permalink="https://slack.com/archives/C123/p123",
        todoist_url="https://todoist.com/showTask?id=123"
    )
    assert len(receipt) == 1
    print("   ✓ Receipt card generated")

    print("\n🎉 ALL LOCAL PIPELINE TESTS PASSED!")

if __name__ == "__main__":
    test_pipeline()
