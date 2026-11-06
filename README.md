# TaskFlow on Switch ⚡
> **Autonomous Multi-Agent Task Triage & Workroom Execution Engine**  
> *Built for the "Flip the Switch" Hackathon (Powered by Switch AI, WeMakeDevs & DevTools Academy)*

[![Switch AI](https://img.shields.io/badge/Native%20Protocol-Switch%20AI%20Core-6366F1)](https://switchagents.ai)
[![Python 3.12](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)](https://python.org)
[![WebSocket](https://img.shields.io/badge/Transport-Native%20WebSocket%20%2B%20Ops-00C7B7)](https://switchagents.ai)
[![Gemini](https://img.shields.io/badge/LLM-Gemini%203.5%20Flash-4285F4?logo=google&logoColor=white)](https://aistudio.google.com)
[![Todoist](https://img.shields.io/badge/Task%20Tracker-Todoist%20v1-E44332?logo=todoist&logoColor=white)](https://todoist.com)

---

## 💡 The Problem: The Passive To-Do Trap
In fast-paced engineering and product teams (Slack / Teams), informal task handoffs happen constantly in natural language:
> *"Hey @TaskFlow, can you draft the release notes for v1.2?"*  
> *"Someone urgently needs to audit the database connection pool before tomorrow."*

Most bot solutions fall into **The Passive To-Do Trap**: they simply create another item on an already overwhelming to-do list. The human still has to do 100% of the manual labor, and informal asks get lost in chat noise.

## 🚀 The Solution: TaskFlow on Switch
**TaskFlow** operates as a native multi-agent workroom system deployed directly on **Switch AI Core**. Rather than opening a fragile direct socket to Slack or coupling logic inside an in-process script, TaskFlow registers **two independent Switch Agents**:

1. **`taskflow-triage` (The Ear - Ambient / High-Recall Classifier):**  
   Listens to the Switch room WebSocket (`/agents/{id}/connection/ws`), ambiently analyzes conversation, extracts task metadata (Assignee, Assigner, Deadline, Category), and classifies tasks by **AI Autonomy**.
2. **`taskflow-worker` (The Hands - Deliverable Drafter & Action Scaffolder):**  
   Receives task assignments from `taskflow-triage` **natively via Switch targeted room messages** (`send_targeted_message`), never through in-process function calls!
   * **`AUTO_EXECUTE` (Routine Knowledge Work):** If the ask is routine (drafting release notes, writing documentation, generating incident postmortems), the Worker Agent **autonomously drafts the artifact** and broadcasts it to the room via `post_message`.
   * **`HUMAN_TODO` (Complex Engineering / Action):** If the task requires human expertise, the Worker scaffolds a **concrete 3-step action checklist**, creates the task in **Todoist**, and sends targeted receipts to the team.

3. **Declarative Room Template (`triage-pair.template.yaml`):**  
   Written in the official **Switch Version 2 Template Specification**, allowing the entire two-agent workroom to be provisioned declaratively via Switch Console or `create_room_from_yaml`.

---

## 🏛️ System Architecture

```
                  [ Team Chat: Slack / Teams / Mattermost ]
                                 │
                                 ▼
                      [ SWITCH LOCAL SERVER ]
                      • Core Gateway & REST API (Port 52422)
                      • Collaboration Bridge (Slack Adapter)
                      • Room State & Targeted Message Bus
                                 │
                    WebSocket Stream & Ops API
          (/agents/{id}/connection/ws  +  /ops/send_targeted_message)
                                 │
        ┌────────────────────────┴────────────────────────┐
        ▼                                                 ▼
┌───────────────────────────────┐     ┌───────────────────────────────┐
│ SWITCH AGENT: TASKFLOW-TRIAGE │     │ SWITCH AGENT: TASKFLOW-WORKER │
│ • Persistent WS & Heartbeat   │     │ • Persistent WS & Heartbeat   │
│ • Ambient Stream Listener     │     │ • Listens for Targeted Handoff│
│ • Intent & Autonomy Classifier│     │ • Autonomously Drafts Docs    │
│ • Multi-Provider LLM Gateway  │     │ • Scaffolds Action Checklists │
│ • ID: 8e390fbb-737c-4018-...  │     │ • Syncs Tasks to Todoist REST │
└───────────────┬───────────────┘     └───────────────▲───────────────┘
                │                                     │
                │  Switch Targeted Room Message       │
                │  (send_targeted_message)            │
                └─────────────────────────────────────┘
```

---

## 🛡️ Switch Native Protocol Highlights

* **Two Registered Switch Agents:** Registered via `POST /agents` with independent identities, `always_on` integration profiles, and dedicated API keys.
* **Targeted Room Message Handoffs:** Handoffs between Triage and Worker are decoupled room messages (`send_targeted_message` targeted to `taskflow-worker`), making the agents truly distributed and independently restartable.
* **Declarative Template Provisioning:** Fully validated against Switch Core's `lint_template` validator using `triage-pair.template.yaml` (Switch version 2 specification).
* **Zero-Lapse Heartbeat Engine:** Switch expects heartbeat `ping` frames and requires `pong` beats every 2 seconds. TaskFlow uses an asynchronous non-blocking event pump with `asyncio.to_thread` for LLM tasks so heartbeats are never delayed.
* **Ambient vs. Addressed Triage Policy:**
  - **`addressed` (`@taskflow-triage`):** Always triggers task evaluation.
  - **`ambient` (room chatter):** Analyzes conversational background without interrupting unless explicit action item triggers ("todo", "deploy", "audit", "fix") are detected.

---

## 📂 Project Structure

```
switch-taskflow/
├── templates/
│   └── triage-pair.template.yaml     # Declarative Switch V2 room template (triage + worker + room)
├── agents/
│   ├── __init__.py
│   ├── triage_agent.py               # Triage classifier & entity extraction
│   └── worker_agent.py               # Deliverable generator & 3-step action scaffolder
├── switch_client.py                  # Native Switch Agent Client (WebSocket, Heartbeat, Ops)
├── bot.py                            # Multi-Agent Daemon (Runs taskflow-triage & taskflow-worker)
├── config.py                         # Environment and multi-agent configuration
├── models.py                         # Pydantic schemas (TaskTriageResult, TaskHandoffMessage, etc.)
├── llm_gateway.py                    # Multi-Provider Resilient LLM Gateway (Gemini -> OpenAI -> Claude)
├── todoist_client.py                 # Todoist REST API v1 Client with auto-retry
├── run_switch_server.py              # Switch Core local server runner
├── simulate_switch_interaction.py    # End-to-end Switch WebSocket multi-agent handoff simulation
├── test_switch_handoff.py            # Targeted room message WebSocket verification test
├── test_multi_agent.py               # Offline unit test suite
├── triage-pair.template.yaml         # Root copy of Switch room template
├── requirements.txt                  # Python dependencies
└── README.md
```

---

## ⚡ Quickstart & Setup

### 1. Prerequisites
* Python 3.12+
* Switch Console / Switch Core (local or remote server)
* Google Gemini API Key (or OpenAI / Anthropic keys for fallback)
* Todoist Account & API Token

### 2. Installation
```bash
git clone https://github.com/An16og/switch-taskflow.git
cd switch-taskflow

python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

pip install -r requirements.txt
```

### 3. Configure Environment Variables
Copy `.env.example` to `.env`:
```env
# Switch Server
SWITCH_SERVER_URL=http://127.0.0.1:52422
AGENT_REGISTRATION_TOKEN=8EQjhvEdXZst0NB_7HGKjcjJO5itH_sx
SWITCH_ROOM_ID=a327cb7f-9cd0-45f8-8872-9a96022f8522
AMBIENT_TRIAGE_POLICY=action_keywords

# Agent 1: TaskFlow Triage (The Ear)
SWITCH_TRIAGE_AGENT_NAME=taskflow-triage
SWITCH_TRIAGE_AGENT_ID=8e390fbb-737c-4018-9960-a36bc1457fd6
SWITCH_TRIAGE_AGENT_API_KEY=3w37cL1CxA0U9fnvU7SVZOeq96TQ-6GGCaBhwbOmKy0

# Agent 2: TaskFlow Worker (The Hands)
SWITCH_WORKER_AGENT_NAME=taskflow-worker
SWITCH_WORKER_AGENT_ID=deb3d8b9-c6f3-47b2-be0a-3f272d9ae425
SWITCH_WORKER_AGENT_API_KEY=1uz2H6a5LiO9BfK_gEB8buN1qU-j1CYOww1v48w04-4

# LLM Keys
GEMINI_API_KEY=AIzaSy...
TODOIST_API_TOKEN=...
```

### 4. Start Switch Core & TaskFlow
In Terminal 1 (Start Switch Server if not running via Console):
```bash
python run_switch_server.py
```

In Terminal 2 (Start Both TaskFlow Agents):
```bash
python bot.py
# Or run individually:
# python bot.py --agent triage
# python bot.py --agent worker
```

### 5. Run the Simulation Test Suite
Verify the entire two-agent targeted handoff over Switch WebSocket:
```bash
python simulate_switch_interaction.py
```

---

## 🏆 Hackathon Submission Details
* **Hackathon:** Flip the Switch Hackathon (Nov 2026)
* **Hosts:** WeMakeDevs & DevTools Academy
* **Powered by:** Switch AI (`switchagents.ai` / SandboxAQ)
* **GitHub Repository:** [https://github.com/An16og/switch-taskflow](https://github.com/An16og/switch-taskflow)
* **Room Template:** [`templates/triage-pair.template.yaml`](file:///C:/Users/anujj/OneDrive/Desktop/switch-taskflow/templates/triage-pair.template.yaml) (Switch V2)
