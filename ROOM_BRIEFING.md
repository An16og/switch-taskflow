# 📋 TaskFlow Room Briefing & Operational Guide

> **Workroom:** TaskFlow Operations  
> **Architecture:** Native Switch Dual-Agent Workroom (`taskflow-triage` + `taskflow-worker`)  
> **Template:** [`triage-pair.template.yaml`](file:///C:/Users/anujj/OneDrive/Desktop/switch-taskflow/templates/triage-pair.template.yaml) (Switch V2)  

---

## 🎯 1. What TaskFlow Does

TaskFlow turns everyday team chat channels (Slack, Microsoft Teams, Discord, Mattermost) into an **autonomous multi-agent workroom**. It monitors conversations in real-time, extracts implicit and explicit action items, determines the exact level of autonomy required, and drives tasks to completion.

Rather than human engineers spending hours chasing meeting notes, translating Slack chatter into Jira/Todoist cards, or drafting boilerplate release notes, TaskFlow provides an autonomous co-pilot duo operating right in the chat room.

### The Dual-Agent Workroom Pair

| Agent | Switch ID | Core Responsibilities |
| :--- | :--- | :--- |
| **`taskflow-triage`**<br>*(The Listener)* | `8e390fbb-737c-4018-9960-a36bc1457fd6` | • Ambient & addressed conversation monitoring over WebSocket.<br>• High-recall task detection & context sliding-window analysis.<br>• Autonomy classification (`AUTO_EXECUTE` vs `HUMAN_TODO`).<br>• Zero-in-process handoff via Switch `send_targeted_message`. |
| **`taskflow-worker`**<br>*(The Executor)* | `deb3d8b9-c6f3-47b2-be0a-3f272d9ae425` | • Listens on WebSocket for targeted handoff frames from triage.<br>• Autonomous knowledge artifact drafting (`AUTO_EXECUTE`).<br>• Action checklist scaffolding & Todoist sync (`HUMAN_TODO`).<br>• Deliverable broadcasting & targeted receipt delivery. |

---

## ⚖️ 2. The Autonomy Decision Matrix

Every detected conversation item is classified by `taskflow-triage` into one of two execution paths based on human risk and required authority.

```
                    ┌───────────────────────────────┐
                    │  Incoming Channel Message(s)  │
                    └───────────────┬───────────────┘
                                    │
                         [ Prompt Sanitization ]
                         [ LLM Intent Extraction ]
                                    │
                                    ▼
                     Is this an actionable task?
                                    │
                    ┌───────────────┴───────────────┐
                   Yes                              No
                    │                               │
                    ▼                               ▼
       Does it require external human           [ Passive ]
       authority, credentials, or prod?        Ignore chatter
                    │
         ┌──────────┴──────────┐
         │                     │
         No                   Yes
         ▼                     ▼
 ┌───────────────┐     ┌───────────────┐
 │ AUTO_EXECUTE  │     │  HUMAN_TODO   │
 │ (Auto-Draft)  │     │ (Ask a Human) │
 └───────────────┘     └───────────────┘
```

---

### 📝 When TaskFlow Auto-Drafts (`AUTO_EXECUTE`)

**Criteria:** The task consists of self-contained knowledge creation or synthesis. It requires no production credentials, no real-world side effects, and poses zero operational risk. Generating the artifact directly saves immediate human engineering hours.

**Supported Categories:**
1. **Release Notes & Changelogs:** Synthesizing recent git tags, PR descriptions, or feature summaries into user-facing release notes.
2. **Technical Documentation & API Specs:** Drafting Markdown architecture overviews, OpenAPI endpoint definitions, or README tutorials.
3. **Incident Postmortems & Root Cause Summaries:** Formatting outage timelines, contributing factors, and follow-up remediation drafts.
4. **Meeting Minutes & Executive Summaries:** Distilling rambling channel threads into concise key decisions and next steps.
5. **Bug Reproduction & Test Checklists:** Structuring reproduction steps and QA test matrices based on error logs or user reports.

**Artifact Destination:**
- The complete, fully-formatted deliverable is posted directly into the room / Slack thread using Markdown.
- No human delay is introduced — the draft is immediately available for team review, copy-pasting, or iteration.

---

### 👤 When TaskFlow Asks a Human (`HUMAN_TODO`)

**Criteria:** The task involves production systems, specialized human authority, sensitive credentials, physical actions, or subjective decision-making that an AI cannot or should not execute unilaterally.

**Supported Categories:**
1. **Production Operations & Deployments:** Staging/prod deployments, rollbacks, infrastructure scaling, container reboots.
2. **Database & Systems Maintenance:** Index creations, connection pool parameter tuning, migration scripts, backup validations.
3. **Security & Compliance Audits:** SSL certificate renewals, credential rotations, penetration testing reviews, IAM permissions.
4. **Approvals & Governance:** Stakeholder sign-offs, vendor contract reviews, budget approvals, external customer communications.
5. **Manual Code & QA Reviews:** Approving pull requests, running hardware verification, conducting subjective UX reviews.

**Action Taken:**
1. **Natural Due Date Parsing:** Infers realistic deadlines from conversational context (e.g., *"before Friday"*, *"by tomorrow 5pm"*, *"today"*).
2. **3-Step Action Scaffolding:** Generates concrete, non-trivial execution steps formatted as interactive checkboxes (`* [ ] Step 1: ...`, `* [ ] Step 2: ...`, `* [ ] Step 3: ...`).
3. **Todoist API Sync:** Automatically creates the task on Todoist with the parsed due date and checklist embedded in the task description.
4. **Targeted Delivery Receipt:** Issues a targeted message in the room back to the assigning user with the direct deep link (`https://todoist.com/app/task/<id>`).

---

## 📂 3. Where Artifacts Go

| Artifact Type | Destination | Delivery Mechanism | Visibility |
| :--- | :--- | :--- | :--- |
| **Knowledge Deliverables**<br>*(Release notes, docs, postmortems)* | Channel / Thread | Switch `post_message`<br>$\rightarrow$ Slack / Teams broadcast | Entire room & channel |
| **Action Checklists & Tasks**<br>*(Infra changes, audits, reviews)* | Todoist API v1 | `todoist-api-python`<br>REST API Sync | Assignee's Todoist Inbox & Projects |
| **Task Delivery Receipts**<br>*(Confirmation + deep link)* | Channel / Thread | Switch `send_targeted_message`<br>targeted to assignee | Assignee & Room |
| **Channel Triage Digests**<br>*(Comprehensive backlog analysis)* | Channel Root | Switch `post_message`<br>(Markdown Digest Card) | Entire room & team |

---

## 💬 4. How to Interact with TaskFlow in Chat

### 1. Ambient Mode (Zero Effort)
Simply converse naturally. When a team member commits to work or notes an action item, TaskFlow captures it automatically:
> *"Todo: Need someone to audit the database connection pool settings and fix connection timeouts before tomorrow."*  
> ➡️ **Result:** `taskflow-triage` detects the todo $\rightarrow$ `taskflow-worker` scaffolds checklist, creates Todoist task, and posts receipt.

### 2. Direct Mentions (Immediate Knowledge Work)
Explicitly command the agent to produce a deliverable:
> *"@taskflow-triage please draft release notes for v1.3 with dark mode and search"*  
> ➡️ **Result:** `taskflow-triage` validates intent $\rightarrow$ `taskflow-worker` drafts complete release notes in Markdown and replies in thread.

### 3. On-Demand Channel Triage Job
Instruct TaskFlow to analyze the channel's recent backlog:
> *"@taskflow-triage triage this channel"*  
> ➡️ **Result:** TaskFlow executes the **Channel Triage Job**, scans recent unaddressed chatter, auto-executes all knowledge drafts, tracks human actions in Todoist, and posts a consolidated **Channel Triage Digest**.
