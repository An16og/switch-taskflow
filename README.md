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
**TaskFlow** operates as a native **Switch AI Workroom Agent**. Rather than opening a fragile direct socket to Slack, TaskFlow speaks Switch's native Agent Protocol over WebSocket (`/agents/{id}/connection/ws`), binds to persistent rooms, processes real-time event frames, and replies with Switch operations (`post_message`, `send_targeted_message`). Switch's collaboration bridges (Slack, Mattermost) transparently translate these room events to team chat!

TaskFlow introduces a **Two-Agent Collaborative Assembly Line**:
1. **Agent 1: Triage & Intent Classifier (The Ear):** Ambiently listens to the Switch Room, extracts the task metadata (Assignee, Assigner, Deadline, Context), and categorizes tasks by **AI Autonomy**.
2. **Agent 2: Autonomous Worker Agent (The Hands):**
   * **`AUTO_EXECUTE` (Routine Knowledge Work):** If the ask is routine (drafting release notes, writing documentation, generating incident postmortems, synthesizing meeting notes), the Worker Agent **actually does the work on the spot** and publishes the completed markdown artifact directly to the room!
   * **`HUMAN_TODO` (Complex Engineering / Action):** If the task requires human expertise (code review, database migrations, client calls), the Worker Agent scaffolds a **concrete 3-step action checklist** and dispatches it to **Todoist** with targeted receipts.

---

## 🏛️ System Architecture

```
                 [ Team Chat: Slack / Teams / Mattermost ]
                                │
                                ▼
                     [ SWITCH LOCAL SERVER ]
                     • Core Gateway & REST API (Port 52422)
                     • Collaboration Bridge (Slack Adapter)
                     • Room & Role State Management
                                │
                    WebSocket Stream & Ops API
          (/agents/{id}/connection/ws  +  /ops/post_message)
                                │
                                ▼
               ┌─────────────────────────────────┐
               │    TASKFLOW SWITCH AGENT CLIENT │
               │   • Persistent WS Connection    │
               │   • Real-Time Heartbeat (Pong)  │
               │   • Ambient & Addressed Policy  │
               └────────────────┬────────────────┘
                                │
                ┌───────────────┴───────────────┐
                ▼                               ▼
    [ AGENT 1: TRIAGE AGENT ]     [ RESILIENT MULTI-MODEL GATEWAY ]
    • Ambient stream listener     • Primary: Gemini 3.5 Flash
    • Intent & Entity resolution  • Fallback 1: OpenAI GPT-4o
    • High-recall task detection  • Fallback 2: Claude 3.5 Sonnet
                │
                ▼
      Is it AI-Actionable?
        ├── YES (AUTO_EXECUTE) ──► [ AGENT 2: WORKER AGENT ]
        │                          • Autonomously drafts the artifact (Docs, Release Notes)
        │                          • Publishes via Switch: `post_message`
        │
        └── NO (HUMAN_TODO)   ──► [ AGENT 2: WORKER AGENT ]
                                   • Scaffolds 3-step Action Checklist
                                   • Dispatches to Todoist REST API v1
                                   • Targeted receipt via: `send_targeted_message`
```

---

## 🛡️ Switch Native Protocol Highlights

* **Pure Native Switch Agent Protocol:** No standalone Slack Socket Mode bypass. TaskFlow registers via `POST /agents`, authenticates with bearer tokens, and connects over `/agents/{id}/connection/ws`.
* **Zero-Lapse Heartbeat Engine:** Switch expects heartbeat `ping` frames and requires `pong` beats every 2 seconds. TaskFlow uses an asynchronous non-blocking event pump with `asyncio.to_thread` for LLM tasks so heartbeats are never delayed.
* **Ambient vs. Addressed Triage Policy:**
  - **`addressed` (`@taskflow`):** Always activates full triage & worker execution.
  - **`ambient` (room chatter):** Analyzes conversational background without interrupting unless explicit action item triggers ("todo", "deploy", "audit", "fix") are detected.
* **Ops Routing:**
  - `post_message(body, thread_id)` for public artifacts and room deliverables.
  - `send_targeted_message(body, target_names)` for targeted task receipts and handoffs.

---

## 📂 Project Structure

```
switch-taskflow/
├── agents/
│   ├── __init__.py
│   ├── triage_agent.py               # Agent 1: Triage, Entity Extraction & Autonomy Classifier
│   └── worker_agent.py               # Agent 2: Autonomous Artifact Generation & Action Scaffolder
├── switch_client.py                  # Native Switch Agent Client (WS, Heartbeat, Ops)
├── bot.py                            # Main TaskFlow Agent Daemon
├── config.py                         # Environment and Switch configuration
├── models.py                         # Pydantic schemas (TaskTriageResult, WorkerExecutionResult)
├── llm_gateway.py                    # Multi-Provider Resilient LLM Gateway (Gemini -> OpenAI -> Claude)
├── todoist_client.py                 # Todoist REST API v1 Client with auto-retry
├── run_switch_server.py              # Switch Core local server runner
├── simulate_switch_interaction.py    # End-to-end Switch WebSocket multi-agent simulation
├── test_multi_agent.py               # Offline unit test suite
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
SWITCH_AGENT_ID=b8e848ea-edf6-4f14-8368-412b0745b88e
SWITCH_AGENT_API_KEY=u5PplbAq_PAnj-qXobDroWakOzCgA7htWAXCTO1-ejs
SWITCH_ROOM_ID=a327cb7f-9cd0-45f8-8872-9a96022f8522
AMBIENT_TRIAGE_POLICY=action_keywords

# LLM Keys
GEMINI_API_KEY=AIzaSy...
TODOIST_API_TOKEN=...
```

### 4. Start Switch Core & TaskFlow
In Terminal 1 (Start Switch Server if not running via Console):
```bash
python run_switch_server.py
```

In Terminal 2 (Start TaskFlow Agent):
```bash
python bot.py
```

### 5. Run the Simulation Test Suite
Verify the entire two-agent pipeline over Switch WebSocket:
```bash
python simulate_switch_interaction.py
```

---

## 🏆 Hackathon Submission Details
* **Hackathon:** Flip the Switch Hackathon (Nov 2026)
* **Hosts:** WeMakeDevs & DevTools Academy
* **Powered by:** Switch AI (`switchagents.ai` / SandboxAQ)
* **GitHub Repository:** [https://github.com/An16og/switch-taskflow](https://github.com/An16og/switch-taskflow)
