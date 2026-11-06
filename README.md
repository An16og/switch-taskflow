# TaskFlow on Switch ⚡
> **Autonomous Multi-Agent Task Triage & Workroom Execution Engine**  
> *Built for the "Flip the Switch" Hackathon (Powered by Switch AI, WeMakeDevs & DevTools Academy)*

[![Switch AI](https://img.shields.io/badge/Orchestrated%20by-Switch%20AI-6366F1)](https://switchagents.ai)
[![Python 3.12](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)](https://python.org)
[![Slack Bolt](https://img.shields.io/badge/Slack-Socket%20Mode-4A154B?logo=slack&logoColor=white)](https://api.slack.com)
[![Gemini](https://img.shields.io/badge/LLM-Gemini%203.5%20Flash-4285F4?logo=google&logoColor=white)](https://aistudio.google.com)
[![Todoist](https://img.shields.io/badge/Task%20Tracker-Todoist%20v1-E44332?logo=todoist&logoColor=white)](https://todoist.com)

---

## 💡 The Problem: The Passive To-Do Trap
In fast-paced engineering and product teams (Slack / Teams), informal task handoffs happen constantly in natural language:
> *"Hey @Anuj, can you draft the release notes for v1.2?"*  
> *"Someone urgently needs to update the API docs before client demo."*

Most bot solutions fall into **The Passive To-Do Trap**: they simply create another item on an already overwhelming to-do list. The human still has to do 100% of the manual labor, and informal asks get lost in chat noise.

## 🚀 The Solution: TaskFlow on Switch
**TaskFlow** turns team chat channels into an **Autonomous Multi-Agent Workroom** powered by **Switch AI**.

Instead of just creating to-do items, TaskFlow introduces a **Two-Agent Collaborative Assembly Line**:
1. **Agent 1: Triage & Intent Classifier (The Ear):** Ambiently listens to the Switch Room, extracts the task metadata (Assignee, Assigner, Deadline, Context), and categorizes tasks by **AI Autonomy**.
2. **Agent 2: Autonomous Worker Agent (The Hands):**
   * **`AUTO_EXECUTE` (Routine Knowledge Work):** If the ask is routine (drafting release notes, writing documentation, generating incident postmortems, synthesizing meeting notes), the Worker Agent **actually does the work on the spot** and presents the completed artifact in Slack for 1-click publishing!
   * **`HUMAN_TODO` (Complex Engineering / Physical):** If the task requires human expertise (code review, database migrations, client calls), the Worker Agent scaffolds a **step-by-step AI action checklist** and dispatches it to **Todoist** with mutual accountability loops.

---

## 🏛️ System Architecture

```
                 [ Slack Channel / Team Chat ]
                               │
                               ▼
                 [ SWITCH CONSOLE / ROOMS ]
                 • Maps channels to persistent Switch Rooms
                 • Governs role permissions & room context
                 • Integrated with Switch Trust (Guardrails)
                               │
                               ▼
               ┌───────────────────────────────┐
               │    TASKFLOW TWO-AGENT ENGINE  │
               └───────────────┬───────────────┘
                               │
                ┌──────────────┴──────────────┐
                ▼                             ▼
   [ AGENT 1: TRIAGE AGENT ]     [ RESILIENT MULTI-MODEL GATEWAY ]
   • Ambient stream listener     • Primary: Gemini 3.5 Flash
   • Intent & Entity resolution  • Fallback 1: OpenAI GPT-4o
   • High-recall task detection  • Fallback 2: Claude 3.5 Sonnet
                │
                ▼
      Is it AI-Actionable?
        ├── YES (AUTO_EXECUTE) ──► [ AGENT 2: WORKER AGENT ]
        │                          • Autonomously drafts the artifact (Docs, Release Notes)
        │                          • Presents in Slack: [🚀 Approve & Post] [📋 Save]
        │
        └── NO (HUMAN_TODO)   ──► [ AGENT 2: WORKER AGENT ]
                                   • Scaffolds step-by-step Action Checklist
                                   • Syncs to Todoist with original Slack permalink
                                   • Notifies both Assigner & Assignee
```

---

## 🛡️ Why Switch AI? (Enterprise Security & Multi-Agent Rooms)

Traditional standalone Slack bots suffer from:
* **Chat Lock-in:** Code is tightly coupled to Slack API; moving to MS Teams or Discord requires a total rewrite.
* **Security & Prompt Injection:** Raw bots blindly execute malicious user prompts.
* **Solitary Silos:** Standalone bots talk over each other without shared context.

**How TaskFlow leverages Switch:**
1. **Switch Rooms:** Switch isolates the team context into designated workrooms where human team members and AI worker agents share persistent context.
2. **Switch Trust Guardrails:** Incorporates `switch-trust-sdk-py` to scan conversational streams for prompt injection and security policies before reaching the agent.
3. **Multi-Agent Coordination:** Switch natively coordinates handoffs between the ambient Triage Agent (Read-only) and the autonomous Worker Agent (Execution/Write).

---

## 📂 Project Structure

```
switch-taskflow/
├── agents/
│   ├── __init__.py
│   ├── triage_agent.py      # Agent 1: Triage, Entity Extraction & Autonomy Classifier
│   └── worker_agent.py      # Agent 2: Autonomous Artifact Generation & Action Scaffolder
├── config.py                # Environment and multi-key validation
├── models.py                # Pydantic schemas (TaskTriageResult, WorkerExecutionResult)
├── llm_gateway.py           # Multi-Provider Resilient LLM Gateway (Gemini -> OpenAI -> Claude)
├── slack_cards.py           # Slack Block Kit UI (Interactive cards, approvals, receipts)
├── todoist_client.py        # Todoist REST API v1 Client
├── bot.py                   # Main Switch Room / Slack Socket Mode runner
├── simulate_team.py         # Automated Multi-Persona Team Simulator CLI
├── test_multi_agent.py      # Pipeline unit test suite
├── requirements.txt         # Frozen dependencies
└── README.md
```

---

## ⚡ Quickstart & Setup

### 1. Prerequisites
* Python 3.12+
* Slack Workspace with Bot Token & Socket Mode App Token
* Google Gemini API Key (or OpenAI / Anthropic keys for fallback)
* Todoist Account & API Token

### 2. Installation
```bash
git clone https://github.com/anujjainaj16/switch-taskflow.git
cd switch-taskflow

python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

pip install -r requirements.txt
```

### 3. Configure Environment Variables
Copy `.env.example` to `.env` and fill in your keys:
```env
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...
GEMINI_API_KEY=AIzaSy...
TODOIST_API_TOKEN=...

# Optional Fallbacks
OPENAI_API_KEY=sk-...
ANTHROPIC_API_KEY=sk-ant-...
```

### 4. Run the Pipeline
```bash
python bot.py
```

---

## 🧪 Testing & Demo Scenarios

You can test all multi-agent scenarios without waiting for team chat using our built-in simulator:

```bash
python simulate_team.py 1  # ⚡ AUTO-EXECUTE: Drafts v1.2 Release Notes on the spot
python simulate_team.py 2  # 📌 HUMAN_TODO: PR Review with AI-generated Action Checklist
python simulate_team.py 3  # ⚡ AUTO-EXECUTE: Drafts Incident Postmortem Document
python simulate_team.py 4  # 🤫 Ambient Silence: Casual chatter (Zero false alarms)
```

---

## 🏆 Hackathon Submission Details
* **Hackathon:** Flip the Switch Hackathon (Nov 2026)
* **Hosts:** WeMakeDevs & DevTools Academy
* **Powered by:** Switch AI (`switchagents.ai` / SandboxAQ)
