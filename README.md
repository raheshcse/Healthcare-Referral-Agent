# Healthcare Referral Agent

<p align="center">
  <strong>Governed AI for Consequential Healthcare Workflows</strong><br/>
  <sub>Microsoft Agent Framework · OpenAI API ·   · FastAPI · SQLite · React · Synthea</sub>
</p>

<p align="center">

![Python](https://img.shields.io/badge/Python-3.10-3776AB?logo=python&logoColor=white)
![Microsoft Agent Framework](https://img.shields.io/badge/Microsoft%20Agent%20Framework-Agent%20Orchestration-5C2D91)
![OpenAI](https://img.shields.io/badge/OpenAI-API-412991)
![ ](https://img.shields.io/badge/X--Verba-%20Governance-0B8F55)
![FastAPI](https://img.shields.io/badge/FastAPI-API-009688?logo=fastapi&logoColor=white)
![SQLite](https://img.shields.io/badge/SQLite-Data%20Layer-003B57?logo=sqlite&logoColor=white)
![Synthea](https://img.shields.io/badge/Synthea-Synthetic%20Data-F59E0B)

</p>

> **Demonstration build (Phases 1–3).** A governed referral assistant for clinical staff, running entirely locally on **synthetic Synthea patient data**. It is not a clinical system: no real patient data, no diagnosis, and not for patient care.


---

## Contents

1. [Executive summary](#executive-summary)
2. [What the product does](#what-the-product-does)
3. [Architecture](#architecture)
4. [Conversation and tool flow](#conversation-and-tool-flow)
6. [Clinical review workflow (AI proposals)](#clinical-review-workflow-ai-proposals)
7. [User interfaces](#user-interfaces)
8. [API](#api)
9. [Governance scenarios tested](#governance-scenarios-tested)
11. [Setup and running locally](#setup-and-running-locally)
12. [Client demo guide](#client-demo-guide)
13. [Testing](#testing)
14. [Project structure](#project-structure)
15. [Design principles](#design-principles)
16. [Known limitations](#known-limitations)
17. [Real-world applicability](#real-world-applicability)
18. [Roadmap](#roadmap)

---

## Executive summary

The **Healthcare Referral Agent** shows a practical architecture for AI agents in workflows where an AI-initiated action changes system state, here creating, updating or cancelling a patient referral.

Clinical staff talk to the assistant in natural language. The configured **OpenAI** model, running through **Microsoft Agent Framework (MAF)**, interprets the request and chooses tools. ** ** governance then decides, before anything happens, whether each tool call and each consequential action is allowed. Every decision is written to an append-only, hash-chained ledger.

```text
AI interprets intent
        ↓
 governs the model's tool call          (pre-tool governance)
        ↓
Application deterministically prepares the action
        ↓
 governs the action itself               (action governance)
        ↓
Only ALLOW reaches the side effect  →  ledger evidence for every decision
```

> **Reasoning is not execution authority.** The model can propose; the governed system decides.

---

## What the product does

| Capability | How |
|---|---|
| Multi-turn referral assistant | Remembers the patient, department and reason across turns, asks only for what is missing, and resolves "she/her" to the patient being discussed |
| Explicit confirmation | A referral built up over several messages is submitted only after "yes, go ahead". "No", "cancel" or "never mind" withdraws it |
| Patient lookup and clinical data | Read-only tools for the patient summary, conditions, medications, allergies, observations and encounters |
| Governed referral actions | Create, update and cancel referrals, and request a human clinical review |
| AI clinical review (decision support) | The configured OpenAI model reviews the record and may propose a referral, which is then validated and governed |
| Review Console | A separate `/engineering` view of every decision, with its ledger entries, causal links and hash-chain integrity |
| Fail-closed behaviour | Unknown, ambiguous or invented inputs, governance errors and tool failures never produce a write or a success message |

---

## Architecture

```text
┌──────────────────────────────────────────────────────────────────┐
│                     AUTHORISED CLINICAL STAFF                    │
└───────────────────────────────┬──────────────────────────────────┘
                                ▼
┌──────────────────────────────────────────────────────────────────┐
│ React frontend   Clinician app /   ·   Review console /engineering│
└───────────────────────────────┬──────────────────────────────────┘
                                ▼
┌──────────────────────────────────────────────────────────────────┐
│ FastAPI (backend/app.py)                                         │
│   /chat → ChatService                                            │
│   conversation state + deterministic patient / referral capture  │
└───────────────────────────────┬──────────────────────────────────┘
                                ▼
┌──────────────────────────────────────────────────────────────────┐
│ Microsoft Agent Framework · OpenAI API · gpt-4.1-mini             │
│   one AgentSession per conversation · 12 tools                   │
└───────────────────────────────┬──────────────────────────────────┘
                                ▼
┌──────────────────────────────────────────────────────────────────┐
│ TOOL VALIDATION                                                   │
│   proposed tool calls are checked before execution                │
│   invalid requests are blocked before the tool runs              │
└───────────────────────────────┬──────────────────────────────────┘
                                ▼
┌──────────────────────────────────────────────────────────────────┐
│ DETERMINISTIC WORKFLOWS                                          │
│   patient resolution → candidate → AI proposal validation         │
└───────────────────────────────┬──────────────────────────────────┘
                                ▼
┌──────────────────────────────────────────────────────────────────┐
│ APPLICATION ACTIONS (backend/actions.py)                         │
│   validated actions → SQLite                                     │
└──────────────────────────────────────────────────────────────────┘
```

| Layer | Responsibility |
|---|---|
| **OpenAI / gpt-4.1-mini** | Understanding language, choosing tools and writing replies |
| **Microsoft Agent Framework** | Agent loop, tool invocation, session history and middleware |
| **Conversation layer** | Structured referral state, deterministic slot filling, confirmation and pronoun resolution |
| **Application workflows** | Deterministic patient resolution, candidate construction and AI-output validation |
| **Tool validation** | Prevents invalid or unsupported tool requests from executing |
| **`backend/actions.py`** | Controlled writer for referrals and review requests |
| **SQLite + Synthea** | Synthetic patient records and referral persistence |

---

## Conversation and tool flow

### Conversation state

- `POST /chat` takes an optional `conversation_id`. If it is omitted, a new conversation starts; the response returns the id together with a clinician-safe `context` (patient name, department, reason, what is still missing, stage). The UI keeps the id and offers **New conversation**.
- Each conversation has one MAF `AgentSession`, so the configured OpenAI model sees the earlier turns. `ReferralContext` (`backend/conversation.py`) holds the validated facts: patient, department, reason and stage `IDLE → COLLECTING → READY → CONFIRMED → SUBMITTED`. These facts are injected into the model's instructions on every turn.
- Clearly supplied answers ("Aisha Wiegand.", "Cardiology.", "She has chest pain.") are captured deterministically, and the next missing item is asked for without calling the model. Known facts are never asked for again, and a reason is never invented.
- Department aliases are understood ("haematology", "ENT", "cardiac clinic"). An unsupported department ("XYZ") is answered with the list of supported departments.
- **Confirmation:**
  - A single complete instruction ("Refer Aisha Wiegand to cardiology because of chest pain") counts as confirmed.
  - Details collected over several turns need an explicit "yes".
  - "No", "cancel" or "never mind" withdraws the referral.
  - Changing the department ("actually, make it neurology") requires confirmation again.
- **Structured patient identification:** patient identity is held as separate fields: `patient_name`, `date_of_birth` and (after deterministic resolution) `patient_id`. They are never concatenated.
  - "show me patient information for Ram", where several patients match, gets a clear list of candidates by name and date of birth.
  - The reply "Ram Kumar, 02/10/2003" is understood as the answer. It resolves the patient, and the original request continues with clean, separate tool arguments.
  - Dates are read day-first (NZ): 02/10/2003 is 2 October 2003. Impossible dates are rejected with a "DD/MM/YYYY" hint.
  - A date of birth is used only if the clinician typed it, so the model cannot narrow a search with an invented date.
  - Not-found patients and tool errors get clear, fixed replies.
- **Patient context:**
  - Every new referral request starts with a clean department and reason.
  - The previous patient is kept only for back-references ("refer **her** to neurology too").
  - A new name that cannot be resolved never inherits the previous patient.
- Greetings and small talk use a tool-less agent that shares the same history.
- Conversations are held in memory (2-hour TTL, at most 500).

### Agent tools

| Tool | Kind | Pre-tool PreNodes |
|---|---|---|
| `search_patient(name)` | read-only | `PATIENT_SEARCH_REQUEST_VALID` |
| `get_patient_information(patient_name, date_of_birth)` | read-only summary | `CLINICAL_DATA_ACCESS_VALID` |
| `get_patient_conditions`, `_medications`, `_allergies`, `_observations`, `_encounters` | read-only, one category (≤ 25 items) | `CLINICAL_DATA_ACCESS_VALID` |
| `review_patient_for_referral(patient_name, department)` | AI clinical review workflow | `CLINICAL_DATA_ACCESS_VALID` |
| `create_referral(patient_name, department, reason)` | governed side effect | `REFERRAL_INTENT_COMPLETE`, `ACTION_INPUT_GROUNDED`, `REFERRAL_CONFIRMED_BY_STAFF` |
| `update_referral(referral_number, patient_name, department, reason)` | governed side effect | `ACTION_INPUT_GROUNDED` |
| `cancel_referral(referral_number, patient_name, cancellation_reason)` | governed side effect | `ACTION_INPUT_GROUNDED` |
| `request_clinical_review(patient_name, reason, department, referral_number)` | governed side effect | `ACTION_INPUT_GROUNDED` |

Every patient tool also takes an optional, separate `date_of_birth` (only when the clinician gave one). Tools take patient **names**, never IDs. Resolution to an internal UUID is deterministic (`backend/patient_resolution.py`): exactly one match continues, and no match or several matches stop without guessing. Tool results never contain patient UUIDs.

### Replies come from outcomes, not from the model

When a governed workflow runs, the clinician's reply is built from its actual outcome. When a side-effecting tool was attempted but produced no outcome (denied before it ran, or failed), the reply is fixed text, for example *"No referral or change was made. Please confirm the patient, department and reason before I submit the referral."* The model's own wording can never claim a result that did not happen. Denials given to the model contain guidance only; internal rule names stay in the ledger.

---

## Clinical review workflow (AI proposals)

*"Review Aisha Wiegand and determine whether a cardiology referral is appropriate."*

```text
Patient resolution (deterministic) → clinical record (Synthea) → bounded context (no IDs)
   → OpenAI analysis (JSON, UNTRUSTED) → validated ActionProposal (backend/proposals.py)
   →  action governance (origin = AI_PROPOSAL) → ALLOW: referral written · DENY: REVIEW_REQUIRED · TERMINAL
```

- **Proposal validation.** Only `CREATE_REFERRAL` or `NO_ACTION` and only supported departments are accepted. A department the clinician named cannot be changed by the AI. Any `patient_id`, `uuid` or `referral_id` the AI supplies is ignored and recorded. Text containing a UUID is rejected. Malformed output becomes `INVALID_PROPOSAL`, and governance is never called.
- **Workflow states.** `REQUESTED → PATIENT_RESOLVED → DATA_RETRIEVED → ANALYSIS_COMPLETED → ACTION_PROPOSED → GOVERNANCE_CHECK → ACTION_EXECUTED → COMPLETED`, ending in `REVIEW_REQUIRED`, `TERMINAL` or `FAILED` on the failure paths. Runs are stored in `clinical_workflow_runs`.
- **Idempotency.** `POST /clinical-workflows` accepts an `idempotency_key`; a repeated key returns the recorded run without running again.
- **Decision support only.** The UI labels the AI recommendation *"decision support only, not a diagnosis"*. If governance allows the proposal, the review creates the referral it proposed. This is the Phase 2 design, and it is still governed by every rule above.

| Final state | `status` (HTTP) |
|---|---|
| `COMPLETED` | `REFERRAL_CREATED` (201), `NO_ACTION_RECOMMENDED` (200) |
| `REVIEW_REQUIRED` | `GOVERNANCE_DENIED` (403) |
| `TERMINAL` | `GOVERNANCE_TERMINAL` (403) |
| `FAILED` | `PATIENT_NOT_FOUND` 404 · `MULTIPLE_PATIENT_MATCHES` 409 · `INVALID_REQUEST` / `INVALID_PATIENT_ID` 422 · `INVALID_PROPOSAL` 502 · `ANALYSIS_UNAVAILABLE` 503 · `INTERNAL_ERROR` 500 |

---

## User interfaces

The React/Vite frontend (`frontend/`) has two **separate** surfaces. Both talk only to FastAPI, through `src/api.js`, and contain no governance logic.

### Clinician app: `/`

- **Assistant tab**: multi-turn chat, with **New conversation**.
- **Patient review tab**: runs the AI clinical review directly (patient plus optional department).
- **Current conversation**: what has been established (patient, department, reason), what is still needed, and whether it is awaiting confirmation.
- **Request status**: *Request received → Patient identified → Safety & governance checks → Referral / Change recorded*, driven by the backend's real outcome.
- **Referral result** and **This session**: outcome cards for referrals, updates, cancellations and review requests.

What the clinician sees is derived from backend **status codes** (`src/lib/outcomes.js`, unit tested). HTTP 200 alone is never treated as success, and the clinician view never shows patient UUIDs, decision IDs, hashes,  rule names or stack traces. Blocked outcomes use plain language, for example *"Duplicate referral blocked"*, *"Department not supported"* or *"The referral was blocked by the governance workflow."*

### Review Console: `/engineering`

For developers and reviewers:

- Recent workflow activity and application outcomes.
- Clinical workflow runs with state transitions and validated proposals.
- Read-only operational information for troubleshooting and demonstrations.

The console is not access-controlled in this demonstration build.

---

## API

Swagger UI: `http://127.0.0.1:8000/docs`

| Method | Endpoint | Purpose |
|---|---|---|
| `POST` | `/chat` | Clinician conversation (`message`, optional `conversation_id`) |
| `POST` | `/referrals` | Governed referral creation (direct, no LLM; optional `date_of_birth` with `patient_name`) |
| `POST` | `/clinical-workflows` | AI clinical review (`patient_name`, optional `date_of_birth`, `department`, `question`, `idempotency_key`) |
| `GET` | `/clinical-workflows?limit=` · `/clinical-workflows/{workflow_id}` | Workflow runs and transitions |
| `GET` | `/clinical-review-requests?limit=` | Human clinical review queue (read-only) |
| `GET` | `/patients?name=&limit=` · `/patients/{patient_id}` | Patient search and summary |
| `GET` | `/health` | Liveness of the API process. It does not check OpenAI connectivity |

Updates, cancellations and review requests are made through the assistant's governed tools. They have no separate REST endpoint.

`POST /referrals` outcomes: `REFERRAL_CREATED` 201 · `PATIENT_NOT_FOUND` 404 · `MULTIPLE_PATIENT_MATCHES` 409 · `INVALID_REQUEST` / `INVALID_PATIENT_ID` 422 · `GOVERNANCE_DENIED` / `GOVERNANCE_TERMINAL` 403 · `INTERNAL_ERROR` 500. `/chat` returns `503 LLM_UNAVAILABLE` when OpenAI is unavailable or `OPENAI_API_KEY` is not configured.

`POST /chat` response:

| Field | Meaning |
|---|---|
| `reply` | Clinician-facing text. Built from authoritative outcomes when a governed workflow ran; fixed text when an action was denied before running or failed |
| `agent_reply` | Raw LLM text, for transparency |
| `referral_attempted`, `workflow_results[]` | Referral workflow outcomes |
| `clinical_review_attempted`, `clinical_reviews[]` | AI clinical review outcomes |
| `action_attempted`, `action_results[]` | Update / cancel / review-request outcomes |
| `conversation_id`, `context` | Conversation to continue; clinician-safe state |
| `success` | `false` if any outcome failed |

---

## Governance scenarios tested

Each scenario is covered by automated tests that run against a temporary database and ledger. **Browser** means it is also exercised through the real UI in the development browser checks (see Testing).

| # | Scenario | Expected result | Rule | Tests |
|---|---|---|---|---|
| 1 | Valid referral (one complete instruction, or confirmed multi-turn) | ALLOW, one referral written | all create rules | `test_action_governance`, `test_conversation` · browser |
| 2 | Invented / unknown patient | Workflow `PATIENT_NOT_FOUND`, or pre-tool DENY when the model invents a name; no write | `CLINICAL_DATA_ACCESS_VALID`, `PATIENT_SEARCH_REQUEST_VALID` | `test_chat_intent`, `test_tool_governance` · browser |
| 3 | Clinical data with no patient established ("what medications is she on?") | DENY; tool body never runs | `CLINICAL_DATA_ACCESS_VALID` | `test_tool_governance`, `test_conversation` · browser |
| 4 | Unsupported department | DENY via API/agent; in conversation, the assistant lists the supported departments | `REFERRAL_TARGET_VALID` | `test_action_governance`, `test_conversation` |
| 5 | Duplicate active referral, including via a department alias or an update | DENY, recorded with the duplicate ids | `REFERRAL_NOT_DUPLICATE` | `test_action_governance` · browser |
| 6 | Update without a staff-supplied referral number, a non-existent or inactive referral, or no real change | DENY | `ACTION_INPUT_GROUNDED`, `REFERRAL_UPDATE_VALID` | `test_tool_governance`, `test_action_governance` |
| 7 | Cancellation without a reason, or of a cancelled / non-existent referral | DENY | `REFERRAL_CANCEL_VALID` | `test_action_governance` |
| 8 | Changing another patient's referral | TERMINAL `patient-context-integrity-violation` | `REFERRAL_MUST_BELONG_TO_PATIENT` | `test_action_governance` |
| 9 | Action for a non-existent patient record | TERMINAL `referral-action-suspended` | `PATIENT_MUST_EXIST` | `test_action_governance`, `test_gates` · browser |
| 10 | AI cites evidence missing from, or belonging to another patient's, record | DENY, workflow `REVIEW_REQUIRED` | `AI_PROPOSAL_GROUNDED` | `test_clinical_workflow`, `test_action_governance` · browser |
| 11 | AI proposal for a different patient than its workflow | TERMINAL | `GOVERNANCE_CONTEXT_MUST_MATCH_PATIENT` | `test_action_governance` |
| 12 | Placeholder or invented reason ("Unknown", "N/A") | Pre-tool DENY | `ACTION_INPUT_GROUNDED` | `test_chat_intent` |
| 13 | Model tries to create a referral the clinician declined or never confirmed | Pre-tool DENY; nothing created | `REFERRAL_CONFIRMED_BY_STAFF` | `test_conversation` · browser |
| 14 | Model text claims success after a denial or a tool failure | Fixed "No referral or change was made…" reply | reply policy | `test_conversation`, `test_chat_intent` |
| 15 | Malformed AI output / AI-supplied UUIDs | `INVALID_PROPOSAL` / fields ignored; governance not called | proposal validation | `test_proposals`, `test_clinical_api` |
| 16 | Clinical review request: valid, duplicate, missing reason, unknown referral number | ALLOW / DENY | `CLINICAL_REVIEW_REQUEST_VALID` | `test_action_governance` |
| 17 | Ledger integrity and causal links for every decision type | Chain intact; links resolve within the decision |  ledger | `test_ledger`, `test_action_governance`, `test_tool_governance` |

---

## Setup and running locally

Commands are for **Windows PowerShell** from the project root.

### Prerequisites

- Python 3.10+
- Node.js 20.19+ or 22.12+ (required by Vite)
- An [OpenAI API key](https://platform.openai.com/api-keys)
- Git, which is needed to install `-maf` from GitHub

### 1. Python environment

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env      # Set OPENAI_API_KEY; model defaults to gpt-4.1-mini
```

### 2. Synthetic patient database

The processed Synthea CSVs ship in `data/processed/`. Build the SQLite database from them:

```powershell
python -m scripts.create_database
python -m scripts.load_database
python -m scripts.validate_data       # optional sanity report
```

To regenerate the population from Synthea, generate FHIR output into `synthea/output/fhir/`, run `python -m scripts.extract_synthea_data`, and then load the database again as above.

### 3. OpenAI configuration

```powershell
notepad .env
```

Set `OPENAI_API_KEY` in `.env`. `OPENAI_CHAT_MODEL` is optional and defaults to
`gpt-4.1-mini`. The backend is the only component that reads this key.

### 4. API

```powershell
python main.py              # or: uvicorn main:app --reload
```

### 5. Frontend (second terminal)

```powershell
cd frontend
npm install                 # first time only
npm run dev
```

- Clinician app: `http://localhost:5173/`
- Review console: `http://localhost:5173/engineering`

The Vite server proxies `/api/*` to `http://127.0.0.1:8000`; set `XVERBA_API_TARGET` to change this.

### 6. Reset the demo state (recommended before a client demo)

Stop the API first, then run:

```powershell
python -m scripts.reset_demo_data --yes
```

This **renames** `data/` to `data/application audit data`, so earlier evidence is kept. It also removes referrals, review requests and workflow runs, so referral numbers, workflow links and the ledger start fresh together. Patient data is not touched.

### 7. Live end-to-end script (optional)

```powershell
python -m scripts.run_e2e_demo
```

This runs referral, not-found, TERMINAL and AI-review scenarios against the configured OpenAI model, and writes to `data/`.

---

## Client demo guide

A suggested flow after a reset, all on synthetic data. Keep the console open in a second tab.

1. **Multi-turn referral.** Run `reset_demo_data` first, so referral numbers start at 1.
   - "I want to refer a patient" → "Aisha Wiegand" → "Cardiology" → "She has been experiencing chest pain" → "Yes, go ahead".
   - The *Current conversation* panel fills in. The referral is created only after the "yes".
2. **Follow-up with a pronoun.** "What medications is she on?" Then show the `TOOL_CALL get_patient_medications` ALLOW in the console.
3. **Duplicate protection.** "Refer Aisha Wiegand to cardiology because of chest pain" → *Duplicate referral blocked*. In the console: DENY `REFERRAL_NOT_DUPLICATE`, with 7 linked ledger entries.
4. **Decline.** Build a referral over several turns ("Refer Aisha Wiegand to dermatology", then give the reason when asked). When asked to confirm, answer "No". Nothing is created. A single complete instruction is treated as already confirmed, so use the multi-turn form here.
5. **Unsupported department.** "Refer Aisha Wiegand to XYZ department for chest pain" → the assistant lists the supported departments. For the governed denial itself, use `POST /referrals` in Swagger with `"department": "XYZ"` → DENY `REFERRAL_TARGET_VALID`.
6. **Fail closed.**
   - Start a **New conversation**, then ask "What medications is she on?". The model usually either asks which patient you mean, or calls the tool and gets a pre-tool DENY `CLINICAL_DATA_ACCESS_VALID`, which is visible in the console. Either way, no data is read.
   - "Refer Zebulon Nobody to neurology for migraines" → patient not found.
7. **TERMINAL.** In Swagger, `POST /referrals` with `"patient_id": "00000000-0000-0000-0000-000000000000"` → TERMINAL `PATIENT_MUST_EXIST`. Show the TERMINAL entry and its causal chain in the console.
8. **AI review.** On the *Patient review* tab, choose Aisha Wiegand / Endocrinology, and show the AI recommendation.
   - If the AI proposes a referral, show its governance decision (`AI_PROPOSAL_GROUNDED`, origin `AI_PROPOSAL`).
   - A `NO_ACTION` recommendation creates no governance decision.
   - Then open the run in *Clinical workflows*.

Two notes for presenters:
- Synthea names carry numeric suffixes (for example *Aisha756 Melina208 Wiegand701*); typing "Aisha Wiegand" is enough.
- "Aisha" on its own matches several synthetic patients, which is a good way to show ambiguous-patient handling.

---

## Testing

| Suite | Command | Current result |
|---|---|---|
| Backend (pytest) | `python -m pytest` | **221 passed** |
| Frontend unit tests | `cd frontend; npm test` | **31 passed** |
| Frontend lint | `cd frontend; npm run lint` | clean |
| Frontend build | `cd frontend; npm run build` | succeeds |

The backend suite runs against a **temporary database and ledger** (`tests/conftest.py`), so `data/` is never touched. It exercises the real MAF function-invocation loop, the real -maf middleware, the real workflows and the real  gates and ledger. OpenAI is replaced by scripted chat clients.

| Test file | Focus |
|---|---|
| `test_patient_tools.py` | Deterministic resolution, not found, multiple matches, invalid IDs |
| `test_gates.py`, `test_governance.py`, `test_ledger.py` | ALLOW / DENY / TERMINAL, decision retrieval, causal chain, integrity |
| `test_referral_tools.py`, `test_api.py` | Referral workflow, fail-closed behaviour, API contract |
| `test_agent.py` | MAF agent loop, governed middleware, tool invocation |
| `test_chat_intent.py` | Small talk vs actions, invented patients, placeholder reasons |
| `test_conversation.py` | Multi-turn state, confirmation and decline, pronouns, department changes and aliases, no false success |
| `test_tool_governance.py` | Pre-tool governance for every tool; a denied tool never runs |
| `test_patient_identification.py` | Structured name + date of birth, ambiguous / unknown patients, invented dates, tool failure |
| `test_action_governance.py` | Create / update / cancel / review-request governance, duplicates, TERMINAL invariants |
| `test_proposals.py`, `test_clinical_workflow.py`, `test_clinical_api.py` | AI output validation, workflow states, idempotency |

**Browser checks.** During development the UI was also exercised end to end in a headless Chromium (Playwright): 170 checks across the clinician app and the console, covering Phases 1–3, against the real backend with a scripted model client. That harness is not part of this repository.

**Live model.** OpenAI model behaviour itself is not covered by the automated suites. Verify it with `OPENAI_API_KEY` configured, using the demo guide or `scripts/run_e2e_demo.py`.

---

## Project structure

```text
Healthcare-Referral-Agent/
├── backend/
│   ├── app.py                  # FastAPI endpoints
│   ├── chat.py                 # ChatService: routing, authoritative replies
│   ├── conversation.py         # conversation state, slot filling, confirmation
│   ├── agent.py                # MAF agent (OpenAI API · gpt-4.1-mini) + instructions
│   ├── agent_tools.py          # the 12 agent tools
│   ├── workflow.py             # ReferralWorkflow, GovernedActionWorkflow
│   ├── actions.py              # the single governed writer
│   ├── patient_resolution.py   # deterministic name → patient
│   ├── clinical_workflow.py    # AI clinical review workflow + states
│   ├── clinical_context.py     # record retrieval, bounded context
│   ├── analysis.py             # OpenAI analysis (untrusted)
│   ├── proposals.py            # AI output → validated proposal; department catalogue
│   ├── clinical_runs.py        # workflow run persistence
│   ├── domain.py, schemas.py   # domain types, API schemas
│   ├── tools/patient_tools.py  # read-only clinical data access
│   └── database/               # SQLAlchemy models, connection, schema
├── frontend/src/
│   ├── api.js                  # the only backend client
│   ├── lib/                    # outcome mapping, ledger helpers (+ tests)
│   ├── clinician/              # /             clinician app
│   └── engineering/            # /engineering  Review console
├── scripts/
│   ├── create_database.py, load_database.py, validate_data.py, extract_synthea_data.py
│   ├── reset_demo_data.py      # archive ledger + clear governed records
│   ├── run_e2e_demo.py         # live OpenAI demonstration
│   └── legacy/                 # early ad-hoc scripts, not maintained
├── tests/                      # pytest suites (temporary DB and ledger)
├── docs/                       # technical report (.docx) + screenshots (see Evidence collected)
├── data/                       # processed CSVs, healthcare.db, application audit data
├── main.py · requirements.txt · requirements-lock.txt · pytest.ini · .env.example
```

---

## Design principles

- **Validate before side effects.** Consequential actions are checked before they execute.
- **Fail closed.** Unresolved patients, invalid inputs and tool failures do not become successful actions.
- **Deterministic identity.** The LLM never supplies or selects patient identifiers.
- **No guessing.** Ambiguous patients are never resolved arbitrarily, and reasons are never invented.
- **The clinician confirms.** Conversational referrals require confirmation where appropriate.
- **Truthful replies.** User-facing results come from actual backend outcomes, not model claims.
- **Separation of concerns.** AI reasoning, application workflows and persistence remain separate.

---

## Known limitations

**Not production-ready by design:**
- No authentication or authorisation. The `/engineering` console is not access-controlled.
- Synthetic data only; no healthcare interoperability (FHIR APIs, EHR integration).
- No human authorisation workflow. TERMINAL decisions and `REVIEW_REQUIRED` outcomes are recorded but cannot be approved, overridden or re-enabled (`HUMAN_AUTHORISED_TRANSITION` / `SPECIFICATION_UPDATE` are not written). Ledger audit checks 4 and 5 therefore fail for TERMINAL decisions.
- `clinical_review_requests` is a queue only; there is no reviewer screen.

**Behavioural boundaries:**
- Any LLM can over-call tools or phrase things poorly. Governance and the fixed replies contain this, but live model behaviour is not covered by the automated suites.
- Slot filling is deterministic and English-only. Anything it doesn't recognise goes to the model, which is still governed.
- A **patient review** that governance allows creates the referral it proposes without a separate confirmation step (Phase 2 design).
- Grounding rules check *where* an input came from and *whether* evidence exists. They do not judge clinical appropriateness.
- Duplicate protection treats "same patient + same department + PENDING" as a duplicate and has no cross-request locking. Two simultaneous identical requests could both pass.
- Updates and cancellations are traced through the ledger MONITOR (referral number and change). The referral row keeps the decision id of its creation.

**Operational:**
- Conversations live in server memory: they are lost on restart and expire after 2 hours. An unknown or expired `conversation_id` silently starts a new conversation.
- `/health` reports API liveness only. It does not check OpenAI connectivity or the database.
- The `/chat` response also carries patient IDs inside its structured result objects for API consumers. The clinician UI never displays them.
- Synthetic records can contain sensitive findings, as Synthea generates them. Choose demo questions accordingly.

---

## Roadmap

| Phase | Scope | Status |
|---|---|---|
| 1. Healthcare Referral Agent | Referral workflow, patient resolution and UI | ✅ Implemented |
| 2. Clinical Workflow / Clinical Review | AI clinical review, proposal validation and workflow tracking | ✅ Implemented |
| 3. Conversation and tool handling | Multi-turn state, confirmation, structured name + DOB, update/cancel/review actions | ✅ Implemented |
| 4. Identity / RBAC | Authentication, roles and service identity | 🔜 Proposed |
| 5. Human-in-the-loop | Review queue, reviewer decisions and escalation | 🔜 Proposed |
| 6. Production data integration | EHR/EMR via FHIR and enterprise identity | 🔜 Proposed |
| 7. Observability and analytics | Operational dashboards, model/version tracking and evidence lineage | 🔜 Proposed |
| 8. Cloud / DevOps | Containers, CI/CD, secrets, monitoring and disaster recovery | 🔜 Proposed |

---

## Disclaimer

This project uses synthetic healthcare data generated for development and demonstration purposes. It is not a production clinical system and must not be used for real patient care, diagnosis or clinical decision-making.

---

## Healthcare Referral Agent

Built as a demonstration of AI-assisted healthcare referral workflows using Microsoft Agent Framework, OpenAI, FastAPI, React and synthetic Synthea data.

> **AI can reason about an action. The application validates and controls whether that action can execute.**
