# X-Verba Healthcare Referral Agent

<p align="center">
  <strong>Governed AI for Consequential Healthcare Workflows</strong><br/>
  <sub>Microsoft Agent Framework · OpenAI API · X-Verba VSL · FastAPI · SQLite · React · Synthea</sub>
</p>

<p align="center">

![Python](https://img.shields.io/badge/Python-3.10-3776AB?logo=python&logoColor=white)
![Microsoft Agent Framework](https://img.shields.io/badge/Microsoft%20Agent%20Framework-Agent%20Orchestration-5C2D91)
![OpenAI](https://img.shields.io/badge/OpenAI-API-412991)
![X-Verba VSL](https://img.shields.io/badge/X--Verba-VSL%20Governance-0B8F55)
![FastAPI](https://img.shields.io/badge/FastAPI-API-009688?logo=fastapi&logoColor=white)
![SQLite](https://img.shields.io/badge/SQLite-Data%20Layer-003B57?logo=sqlite&logoColor=white)
![Synthea](https://img.shields.io/badge/Synthea-Synthetic%20Data-F59E0B)

</p>

> **Demonstration build (Phases 1–3).** A governed referral assistant for clinical staff, running entirely locally on **synthetic Synthea patient data**. It is not a clinical system: no real patient data, no diagnosis, and not for patient care.

> 📄 **Technical Architecture, Governance & Real-World Healthcare AI Safety Report (v1.0, October 2026):** [`docs/X-Verba-Healthcare-Referral-Agent-Technical-Governance-Report.docx`](docs/X-Verba-Healthcare-Referral-Agent-Technical-Governance-Report.docx). It is a 69-page architecture and governance document with diagrams, screenshot evidence analysis, a real-world healthcare AI risk analysis, and the proposed production architecture and roadmap.

---

## Contents

1. [Executive summary](#executive-summary)
2. [What the product does](#what-the-product-does)
3. [Architecture](#architecture)
4. [Conversation and tool flow](#conversation-and-tool-flow)
5. [Governance model](#governance-model)
6. [Clinical review workflow (AI proposals)](#clinical-review-workflow-ai-proposals)
7. [User interfaces](#user-interfaces)
8. [API](#api)
9. [Governance scenarios tested](#governance-scenarios-tested)
10. [Evidence collected](#evidence-collected)
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

The **X-Verba Healthcare Referral Agent** shows a practical architecture for AI agents in workflows where an AI-initiated action changes system state, here creating, updating or cancelling a patient referral.

Clinical staff talk to the assistant in natural language. The configured **OpenAI** model, running through **Microsoft Agent Framework (MAF)**, interprets the request and chooses tools. **X-Verba VSL** governance then decides, before anything happens, whether each tool call and each consequential action is allowed. Every decision is written to an append-only, hash-chained ledger.

```text
AI interprets intent
        ↓
X-Verba governs the model's tool call          (pre-tool governance)
        ↓
Application deterministically prepares the action
        ↓
X-Verba governs the action itself               (action governance)
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
| Governance & Review Console | A separate `/engineering` view of every decision, with its ledger entries, causal links and hash-chain integrity |
| Fail-closed behaviour | Unknown, ambiguous or invented inputs, governance errors and tool failures never produce a write or a success message |

---

## Architecture

```text
┌──────────────────────────────────────────────────────────────────┐
│                     AUTHORISED CLINICAL STAFF                    │
└───────────────────────────────┬──────────────────────────────────┘
                                ▼
┌──────────────────────────────────────────────────────────────────┐
│ React frontend   Clinician app  /     ·   Governance console /engineering │
└───────────────────────────────┬──────────────────────────────────┘
                                ▼  (frontend/src/api.js only)
┌──────────────────────────────────────────────────────────────────┐
│ FastAPI (backend/app.py)                                         │
│   /chat → ChatService (backend/chat.py)                          │
│     • conversation state (backend/conversation.py)               │
│     • deterministic capture of patient / department / reason     │
└───────────────────────────────┬──────────────────────────────────┘
                                ▼
┌──────────────────────────────────────────────────────────────────┐
│ Microsoft Agent Framework · OpenAI API · gpt-4.1-mini (backend/agent.py)│
│   one AgentSession per conversation · 12 tools                   │
└───────────────────────────────┬──────────────────────────────────┘
                                ▼  every proposed tool call
┌──────────────────────────────────────────────────────────────────┐
│ PRE-TOOL GOVERNANCE  vsl-maf VSLFunctionMiddleware               │
│   governance/maf_gates.py · PreNodes → ALLOW / DENY (ledgered)   │
│   DENY ⇒ the tool body never runs                                │
└───────────────────────────────┬──────────────────────────────────┘
                                ▼  ALLOW
┌──────────────────────────────────────────────────────────────────┐
│ DETERMINISTIC WORKFLOWS  backend/workflow.py, clinical_workflow.py│
│   patient resolution → candidate → (AI proposal validation)      │
└───────────────────────────────┬──────────────────────────────────┘
                                ▼
┌──────────────────────────────────────────────────────────────────┐
│ ACTION GOVERNANCE  governance/gates.py + governance/policy.py    │
│   PreNodes → Invariants → ALLOW / DENY / TERMINAL (ledgered)     │
└──────────────┬─────────────────────────────────┬─────────────────┘
             ALLOW                         DENY / TERMINAL
               ▼                                 ▼
┌────────────────────────────┐         ┌──────────────────────┐
│ backend/actions.py         │         │  No side effect      │
│ single writer → SQLite     │         │  Clinician told why  │
└────────────────────────────┘         └──────────────────────┘
               │
               ▼
     VSL ledger (data/ledger.jsonl): MONITOR → PRE_NODE → VERIFICATION → [TERMINAL]
```

| Layer | Responsibility |
|---|---|
| **OpenAI / gpt-4.1-mini** | Understanding language, choosing tools, writing replies. Untrusted |
| **Microsoft Agent Framework** | Agent loop, tool invocation, session history, middleware |
| **Conversation layer** | Structured referral state, deterministic slot filling, confirmation, pronoun resolution |
| **Application workflows** | Deterministic patient resolution, candidate construction, AI-output validation |
| **X-Verba VSL** | Pre-tool and action governance; ALLOW / DENY / TERMINAL; ledger evidence |
| **`backend/actions.py`** | The only code that writes referrals and review requests, and only with an ALLOW decision |
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

## Governance model

### Concepts

| VSL construct | Meaning here | Result when it fails |
|---|---|---|
| **PreNode** | Request validation: is this request well-formed, grounded and permitted? | **DENY**. Nothing happens; the clinician can correct the request |
| **Invariant** | Integrity condition that must never be violated | **TERMINAL**. The action is halted and a TerminalState is recorded |
| **TerminalState** | `referral-action-suspended`, `patient-context-integrity-violation` | Needs human attention |
| **ALLOW** | Every PreNode passed and every Invariant held | The side effect may run |

### Two governance layers

1. **Pre-tool governance** (`governance/maf_gates.py`). Every tool the model proposes passes through `GovernedToolMiddleware`, a `vsl-maf` `VSLFunctionMiddleware`, **before** the tool body runs. The decision is recorded as action `TOOL_CALL`; the MONITOR entry records the tool and, for clinical-data tools, the patient accessed. On DENY the tool body never executes.
2. **Action governance** (`governance/gates.py`). The resolved side-effect candidate is governed again with PreNodes and Invariants. The two layers evaluate different inputs: pre-tool checks the model's proposal, while action governance checks the resolved record.

### Rules and why each exists

| Rule | Type | Applies to | Why it exists |
|---|---|---|---|
| `PATIENT_SEARCH_REQUEST_VALID` | PreNode (pre-tool) | `search_patient` | The model may only search for a name the staff member typed, not trawl records with invented queries |
| `CLINICAL_DATA_ACCESS_VALID` | PreNode (pre-tool) | data tools, AI review | Clinical data is read only for a patient the staff member named who resolves to exactly one record |
| `ACTION_INPUT_GROUNDED` | PreNode (pre-tool) | create / update / cancel / review request | Patient, reason and referral number must come from the staff member, never from the model or a placeholder like "Unknown" |
| `REFERRAL_INTENT_COMPLETE` | PreNode (pre-tool) | `create_referral` | Patient, department and reason must all be present |
| `REFERRAL_CONFIRMED_BY_STAFF` | PreNode (pre-tool) | `create_referral` | A referral prepared in conversation is submitted only after the clinician confirmed it, and only for the confirmed patient and department |
| `REFERRAL_REQUEST_VALID` | PreNode | create | The candidate is complete and well-formed |
| `REFERRAL_TARGET_VALID` | PreNode | create | Only supported departments can receive referrals |
| `AI_PROPOSAL_GROUNDED` | PreNode | AI-originated create | Every piece of evidence the AI cites must exist **in this patient's** record |
| `REFERRAL_NOT_DUPLICATE` | PreNode | create; update that changes department | Prevents a second active (PENDING) referral to the same department |
| `REFERRAL_UPDATE_VALID` | PreNode | update | The referral exists, is still active, and a real, valid change was requested |
| `REFERRAL_CANCEL_VALID` | PreNode | cancel | The referral exists, is still active, and a cancellation reason was given |
| `CLINICAL_REVIEW_REQUEST_VALID` | PreNode | review request | Has a reason, does not duplicate an open request, and any cited referral exists |
| `PATIENT_MUST_EXIST` | Invariant → `referral-action-suspended` | all actions | An action for a patient record that cannot be verified is an integrity failure |
| `GOVERNANCE_CONTEXT_MUST_MATCH_PATIENT` | Invariant → `patient-context-integrity-violation` | AI-originated create | The AI proposal must concern the same patient its workflow resolved |
| `REFERRAL_MUST_BELONG_TO_PATIENT` | Invariant → `patient-context-integrity-violation` | update / cancel / review request | A referral can never be changed under another patient's name |

The rules are declared in `governance/policy.py`, which imports only `vsl_core`. The facts they evaluate are computed in `governance/gates.py` (database) and `governance/maf_gates.py` (conversation).

### Decision order per action

| Action | PreNodes (in order) | Invariants |
|---|---|---|
| `TOOL_CALL` | per tool (see Agent tools) | none |
| `CREATE_REFERRAL` | `REFERRAL_REQUEST_VALID` → `REFERRAL_TARGET_VALID` → [`AI_PROPOSAL_GROUNDED`] → `REFERRAL_NOT_DUPLICATE` | `PATIENT_MUST_EXIST` → [`GOVERNANCE_CONTEXT_MUST_MATCH_PATIENT`] |
| `UPDATE_REFERRAL` | `REFERRAL_UPDATE_VALID` → [`REFERRAL_NOT_DUPLICATE` if department changes] | `PATIENT_MUST_EXIST`, `REFERRAL_MUST_BELONG_TO_PATIENT` |
| `CANCEL_REFERRAL` | `REFERRAL_CANCEL_VALID` | `PATIENT_MUST_EXIST`, `REFERRAL_MUST_BELONG_TO_PATIENT` |
| `REQUEST_CLINICAL_REVIEW` | `CLINICAL_REVIEW_REQUEST_VALID` | `PATIENT_MUST_EXIST`, [`REFERRAL_MUST_BELONG_TO_PATIENT`] |

Bracketed items apply only to AI-originated proposals or when the condition is met.

### Ledger evidence

Every decision is written to the VSL ledger (`data/ledger.jsonl`, append-only and hash-chained) under one `decision_id`:

```text
MONITOR                         action, patient, tool / department / changes
 ├─ PRE_NODE  (caused_by MONITOR)        → VERIFICATION (caused_by PRE_NODE)  outcome: passed
 ├─ PRE_NODE  ...                        → VERIFICATION                       outcome: passed
 └─ PRE_NODE  (last)                     → VERIFICATION  outcome: approved | denied
                                           or  VERIFICATION INSUFFICIENT (outcome: invariant_violated)
                                                 └─ TERMINAL (caused_by VERIFICATION)  terminal_state
```

Observed shapes, verified against the current code:

| Decision | Entries |
|---|---|
| Pre-tool `search_patient` / data tool | `MONITOR, PRE_NODE, VERIFICATION` (3) |
| Pre-tool `create_referral` ALLOW | `MONITOR` + 3 × (`PRE_NODE, VERIFICATION`) (7) |
| Clinician referral ALLOW, or DENY on duplicate | `MONITOR` + 3 × (`PRE_NODE, VERIFICATION`) (7) |
| Referral DENY on unsupported department | `MONITOR` + 2 × (`PRE_NODE, VERIFICATION`) (5) |
| Referral TERMINAL (`PATIENT_MUST_EXIST`) | `MONITOR` + 3 × (`PRE_NODE, VERIFICATION`) + `TERMINAL` (8) |
| AI-originated referral ALLOW | `MONITOR` + 4 × (`PRE_NODE, VERIFICATION`) (9) |
| Update / cancel / review request ALLOW | `MONITOR, PRE_NODE, VERIFICATION` (3); 5 when an update changes department |
| Cancel under another patient's name (TERMINAL) | `MONITOR, PRE_NODE, VERIFICATION, TERMINAL` (4) |

**Traceability.** New referrals and review requests store their `governance_decision_id`. Clinical workflow runs store the decision id, and their MONITOR stores the `workflow_id`. For updates the MONITOR records the referral number and the before/after values; for cancellations, the referral number and the cancellation reason.

**Audit status.**
- `ledger.verify_integrity()` passes: the hash chain is intact.
- `ledger.audit()` passes these checks:
  - every drift-flagged MONITOR has a PreNode;
  - every PRE_NODE has a VERIFICATION.
- The monitoring-gap check runs only when a gap threshold is supplied.
- The two human-authorisation checks (`SPECIFICATION_UPDATE` after `INSUFFICIENT`, `HUMAN_AUTHORISED_TRANSITION` after `TERMINAL`) **fail whenever the ledger contains a TERMINAL decision**. This is by design, because human authorisation is not implemented yet (see Limitations).

### Enforcement boundary

`backend/actions.py` is the single writer:
- `create_referral_record` for new referrals
- `change_referral_record` for updates and cancellations
- `create_review_request_record` for review requests

Each accepts a `GovernanceDecision` (never raw fields) and refuses anything other than ALLOW for its own candidate type. Governance exceptions fail closed: no write, and an `INTERNAL_ERROR` outcome.

---

## Clinical review workflow (AI proposals)

*"Review Aisha Wiegand and determine whether a cardiology referral is appropriate."*

```text
Patient resolution (deterministic) → clinical record (Synthea) → bounded context (no IDs)
   → OpenAI analysis (JSON, UNTRUSTED) → validated ActionProposal (backend/proposals.py)
   → X-Verba action governance (origin = AI_PROPOSAL) → ALLOW: referral written · DENY: REVIEW_REQUIRED · TERMINAL
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

What the clinician sees is derived from backend **status codes** (`src/lib/outcomes.js`, unit tested). HTTP 200 alone is never treated as success, and the clinician view never shows patient UUIDs, decision IDs, hashes, VSL rule names or stack traces. Blocked outcomes use plain language, for example *"Duplicate referral blocked"*, *"Department not supported"* or *"The referral was blocked by the governance workflow."*

### Governance & Review Console: `/engineering`

For AI engineers and governance reviewers:

- **Governance decisions**: recent decisions (action, tool, department) with a hash-chain integrity badge. Lookup by decision id (deep link `/engineering?decision=<id>`) shows:
  - the ALLOW / DENY / TERMINAL badge, the action, the tool, the PreNode, the Invariant and the TerminalState;
  - every ledger entry, with `caused_by` links resolved, hashes and payloads;
  - an explanation matched to the action (pre-tool call, create, update, cancel or review request).
- **Clinical workflows**: AI review runs with state transitions, the validated proposal, ignored AI fields, the raw (untrusted) AI output, and links to the governance evidence.
- TERMINAL decisions state plainly that no human-authorised transition is recorded. The console is read-only and **not yet access-controlled**.

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
| `GET` | `/governance?limit=` · `/governance/{decision_id}` | Governance decisions and ledger evidence |
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
| 17 | Ledger integrity and causal links for every decision type | Chain intact; links resolve within the decision | VSL ledger | `test_ledger`, `test_action_governance`, `test_tool_governance` |

---

## Evidence collected

- **Automated evidence (current):** the test suites listed above, all passing (see Testing).
- **Detailed evidence analysis:** section 23 of the [technical report](docs/X-Verba-Healthcare-Referral-Agent-Technical-Governance-Report.docx) documents every screenshot scenario. For each it gives the input, the decision reached, the ledger entries, whether governance was reached, and what the screenshot does and does not prove.
- **UI screenshots, `docs/UI evidence/`.** Captured by hand during Phase 3 testing, **before** the client-readiness fixes. Panel wording such as "did not request a referral" and the console's "Direct request (Phase 1 path)" label have since been corrected, so recapture these before showing them to a client.

  | File | Shows |
  |---|---|
  | `ev 1.png`, `ev 1.1.png` | Patient search in chat; console: `TOOL_CALL search_patient` ALLOW (`PATIENT_SEARCH_REQUEST_VALID`) |
  | `Scenario 2*.png` | Invented patient: assistant reports not found; console shows the search decision |
  | `Scenario 3*.png` | Clinical data without a patient: DENY `CLINICAL_DATA_ACCESS_VALID` |
  | `Scenario 4*.png` | Unsupported department, before the fix: the assistant only asked "Which department?" (it now names the department as unsupported and lists the valid ones) |
  | `Scenario 5*.png`, `Scenario 8*.png` | Duplicate referral: DENY `REFERRAL_NOT_DUPLICATE` (7 ledger entries) |
  | `Scenario 6*.png` | Update without a referral number: DENY `ACTION_INPUT_GROUNDED` |
  | `Scenario 7*.png` | Cancellation without a referral number: DENY `ACTION_INPUT_GROUNDED` |
  | `Scenario 9*.png` | `pytest tests\test_action_governance.py -v` output, including the TERMINAL tests |

- **`docs/evidence/` (historical, Phase 1).** Early infographics and screenshots from Phase 1. They show ledger shapes and tool signatures that predate the current implementation (for example `create_referral(patient_id=…)` and 3- or 4-entry decisions). Some are illustrative rather than literal ledger output. Treat them as history, not current evidence.

---

## Setup and running locally

Commands are for **Windows PowerShell** from the project root.

### Prerequisites

- Python 3.10+
- Node.js 20.19+ or 22.12+ (required by Vite)
- An [OpenAI API key](https://platform.openai.com/api-keys)
- Git, which is needed to install `vsl-maf` from GitHub

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
- Governance console: `http://localhost:5173/engineering`

The Vite server proxies `/api/*` to `http://127.0.0.1:8000`; set `XVERBA_API_TARGET` to change this.

### 6. Reset the demo state (recommended before a client demo)

Stop the API first, then run:

```powershell
python -m scripts.reset_demo_data --yes
```

This **renames** `data/ledger.jsonl` to `data/ledger-archive-<timestamp>.jsonl`, so earlier evidence is kept. It also removes referrals, review requests and workflow runs, so referral numbers, workflow links and the ledger start fresh together. Patient data is not touched.

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

The backend suite runs against a **temporary database and ledger** (`tests/conftest.py`), so `data/` is never touched. It exercises the real MAF function-invocation loop, the real vsl-maf middleware, the real workflows and the real VSL gates and ledger. OpenAI is replaced by scripted chat clients.

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
X-Verba-Healthcare-Referral-Agent/
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
├── governance/
│   ├── policy.py               # VSL PreNodes, Invariants, TerminalStates (vsl_core only)
│   ├── gates.py                # action governance + decision engine (ledger writes)
│   ├── maf_gates.py            # pre-tool governance (vsl-maf middleware)
│   └── ledger.py               # VSL ledger + read-only evidence helpers
├── frontend/src/
│   ├── api.js                  # the only backend client
│   ├── lib/                    # outcome mapping, ledger helpers (+ tests)
│   ├── clinician/              # /             clinician app
│   └── engineering/            # /engineering  governance console
├── scripts/
│   ├── create_database.py, load_database.py, validate_data.py, extract_synthea_data.py
│   ├── reset_demo_data.py      # archive ledger + clear governed records
│   ├── run_e2e_demo.py         # live OpenAI demonstration
│   └── legacy/                 # early ad-hoc scripts, not maintained
├── tests/                      # pytest suites (temporary DB and ledger)
├── docs/                       # technical report (.docx) + screenshots (see Evidence collected)
├── data/                       # processed CSVs, healthcare.db, ledger.jsonl
├── main.py · requirements.txt · requirements-lock.txt · pytest.ini · .env.example
```

---

## Design principles

- **Governance before side effect.** Every consequential action is decided before it executes, and every tool call is decided before it runs.
- **Fail closed.** Governance errors, unresolved patients, invented inputs and tool failures never become successful actions or success messages.
- **Deterministic identity.** The LLM never supplies or selects patient identifiers.
- **No guessing.** An ambiguous patient is never resolved arbitrarily, and no reason is ever invented.
- **The clinician confirms.** A conversational referral is submitted only after explicit confirmation.
- **Truthful replies.** What the clinician reads comes from actual outcomes, not from model claims.
- **Traceability.** Every decision has linked ledger evidence; records link back to their decisions.
- **Separation of concerns.** Authentication and authorisation (future) stay distinct from AI governance.

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

## Real-world applicability

The same boundary pattern applies wherever an AI system proposes an action that changes a clinical or administrative system. In a production organisation, X-Verba would sit between the **AI proposal** and the **real-world side effect**:

```text
EHR / EMR → clinical / administrative workflow → AI agent → structured action proposal
   → X-Verba governance boundary
       ├── identity / context checks      (implemented: patient context)
       ├── policy PreNodes                (implemented)
       ├── safety Invariants              (implemented: integrity invariants)
       ├── evidence verification          (implemented: grounding)
       ├── risk classification            (proposed)
       ├── human review requirement       (proposed)
       └── authorisation / role controls  (proposed)
   → ALLOW / DENY / TERMINAL / HUMAN_REVIEW (proposed) → controlled action → audit / ledger
```

The technical report (section 32) analyses the Mayo Clinic Platform article *"Are We Deploying AI Algorithms Without Appropriate Oversight?"* (Halamka & Cerrato, 29 June 2026). Its examples include an LLM's advice in a breech/VBAC case, Stanford's ChatEHR evaluation, an AI sepsis alert recommending IV fluids for a patient with a dialysis catheter, and Mayo's review-before-use and proactive-human-review policies. The report maps each one to the type of control a production system would need, and states clearly what this prototype implements today.

An appropriately configured X-Verba governance boundary could provide controls relevant to this class of risk. It does not guarantee patient safety, and the current project does not handle treatment, medication or order workflows.

---

## Roadmap

| Phase | Scope | Status |
|---|---|---|
| 1. Healthcare Referral Agent | Governed referral workflow, patient resolution, VSL ledger, UI and console | ✅ Implemented |
| 2. Clinical Workflow / Clinical Review | AI clinical review, proposal validation and grounding, workflow tracking | ✅ Implemented |
| 3. Conversation and tool governance | Multi-turn state, confirmation, structured name + DOB, pre-tool governance on all tools, update/cancel/review actions | ✅ Implemented |
| 4. Identity / RBAC | Authentication, roles, service identity, governance authorisation context | 🔜 Proposed |
| 5. Human-in-the-loop governance | Risk classification, review queue and screen, auditable reviewer decisions, escalation | 🔜 Proposed |
| 6. Production data integration | EHR/EMR via FHIR, enterprise identity, durable ledger | 🔜 Proposed |
| 7. Advanced governance | Policy versioning, risk tiers, model/version tracking, evidence lineage, governance analytics | 🔜 Proposed |
| 8. Cloud / DevOps | Containers, CI/CD, cloud deployment, secrets, monitoring, disaster recovery | 🔜 Proposed |

Every enhancement should strengthen the governance boundary rather than route around it. New tools get pre-tool PreNodes. New side effects get a decision-carrying writer. New protected conditions become Invariants. New human decisions become ledger events.

---

## Disclaimer

This project uses synthetic healthcare data generated for development and demonstration purposes. It is not a production clinical system and must not be used for real patient care, diagnosis or clinical decision-making.

---

## X-Verba / Super Semantics

Built as part of the **X-Verba / Super Semantics** AI governance work. It demonstrates governed AI workflows on Microsoft Agent Framework, with an emphasis on deterministic execution, governance enforcement, auditability and safe control of consequential actions.

> **AI can reason about an action. The governed system decides whether that action can execute.**
#   H e a l t h c a r e - R e f e r r a l - A g e n t  
 