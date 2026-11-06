import sys
if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')

from models import SlackMessage, ExecutionType
from llm_gateway import LLMGateway
from agents.triage_agent import TriageAgent
from agents.worker_agent import WorkerAgent

def test_multi_agent():
    print("--- Testing TaskFlow Multi-Agent Pipeline ---")
    gateway = LLMGateway()
    triage_agent = TriageAgent(gateway=gateway)
    worker_agent = WorkerAgent(gateway=gateway)

    # TEST 1: AUTO_EXECUTE Routine Task (Release Notes)
    print("\n1. Testing AUTO_EXECUTE: Routine Documentation Task...")
    auto_msg = [
        SlackMessage(
            user_id="U1",
            user_name="Alex (PM)",
            text="Can someone draft the v1.2 release notes highlighting our new Stripe webhook fix and dark mode?",
            ts="100.0"
        )
    ]
    triage_1 = triage_agent.triage_conversation(auto_msg)
    print(f"   [Triage Result] is_task: {triage_1.is_task}")
    print(f"   [Execution Type]: {triage_1.execution_type.value}")
    print(f"   [Action]: {triage_1.action_summary}")

    assert triage_1.is_task is True
    assert triage_1.execution_type == ExecutionType.AUTO_EXECUTE

    worker_res_1 = worker_agent.execute_task(triage_1, auto_msg)
    print(f"   [Worker Artifact Title]: {worker_res_1.artifact_title}")
    print(f"   [Artifact Content Preview]:\n{worker_res_1.artifact_content[:200]}...")

    # TEST 2: HUMAN_TODO Complex Task (PR Review / Dev)
    print("\n2. Testing HUMAN_TODO: Complex Engineering Task...")
    human_msg = [
        SlackMessage(
            user_id="U2",
            user_name="Priya (Lead)",
            text="@Anuj please review and benchmark the database migration PR #89 before Friday 5 PM.",
            ts="200.0"
        )
    ]
    triage_2 = triage_agent.triage_conversation(human_msg)
    print(f"   [Triage Result] is_task: {triage_2.is_task}")
    print(f"   [Execution Type]: {triage_2.execution_type.value}")
    print(f"   [Action]: {triage_2.action_summary}")

    assert triage_2.is_task is True
    assert triage_2.execution_type == ExecutionType.HUMAN_TODO

    worker_res_2 = worker_agent.execute_task(triage_2, human_msg)
    print(f"   [Worker AI Action Plan Preview]:\n{worker_res_2.artifact_content}")

    print("\n🎉 ALL MULTI-AGENT TESTS PASSED SUCCESSFULLY!")

if __name__ == "__main__":
    test_multi_agent()
