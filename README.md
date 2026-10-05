# Healthcare Referral Agent

<p align="center">
  <strong>AI-Assisted Healthcare Referral Workflow</strong><br/>
  <sub>Microsoft Agent Framework · OpenAI API · FastAPI · SQLite · React · Synthea</sub>
</p>

<p align="center">

![Python](https://img.shields.io/badge/Python-3.10-3776AB?logo=python&logoColor=white)
![Microsoft Agent Framework](https://img.shields.io/badge/Microsoft%20Agent%20Framework-Agent%20Orchestration-5C2D91)
![OpenAI](https://img.shields.io/badge/OpenAI-API-412991)
![FastAPI](https://img.shields.io/badge/FastAPI-API-009688?logo=fastapi&logoColor=white)
![SQLite](https://img.shields.io/badge/SQLite-Data%20Layer-003B57?logo=sqlite&logoColor=white)
![React](https://img.shields.io/badge/React-Frontend-61DAFB?logo=react&logoColor=black)
![Synthea](https://img.shields.io/badge/Synthea-Synthetic%20Data-F59E0B)

</p>

> **Demonstration build (Phases 1–3).** An AI-assisted referral application for clinical staff, running locally on **synthetic Synthea patient data**. It is not a clinical system: it does not use real patient data, provide diagnosis, or replace professional clinical judgement.

---

## Contents

1. [Executive summary](#executive-summary)
2. [What the product does](#what-the-product-does)
3. [Architecture](#architecture)
4. [Conversation and tool flow](#conversation-and-tool-flow)
5. [Clinical review workflow](#clinical-review-workflow)
6. [User interfaces](#user-interfaces)
7. [API](#api)
8. [Referral and workflow rules](#referral-and-workflow-rules)
9. [Evidence and testing scenarios](#evidence-and-testing-scenarios)
10. [Setup and running locally](#setup-and-running-locally)
11. [Client demo guide](#client-demo-guide)
12. [Testing](#testing)
13. [Project structure](#project-structure)
14. [Design principles](#design-principles)
15. [Known limitations](#known-limitations)
16. [Real-world applicability](#real-world-applicability)
17. [Roadmap](#roadmap)
18. [Disclaimer](#disclaimer)

---

## Executive summary

The **Healthcare Referral Agent** demonstrates a practical AI-assisted workflow for healthcare referral administration.

Clinical staff interact with the application using natural language. **Microsoft Agent Framework (MAF)** manages the agent loop and tool invocation, while an **OpenAI** model interprets requests, selects tools, and produces conversational responses.

The application layer provides deterministic patient resolution, conversation state, validation, clinical-data retrieval, referral workflows, and controlled database writes. The model is treated as an assistant rather than as the source of truth for patient identity or database state.

```text
Clinical staff
      ↓
React frontend
      ↓
FastAPI backend
      ↓
Microsoft Agent Framework + OpenAI
      ↓
Conversation / tool routing
      ↓
Deterministic application workflows
      ↓
Patient resolution + input validation
      ↓
Referral / clinical workflow
      ↓
SQLite database
```

The central design principle is:

> **The AI can interpret a request and propose an action, but the application remains responsible for validating the request and performing the actual database operation.**

---

## What the product does

| Capability | Description |
|---|---|
| Multi-turn referral assistant | Collects patient, department and referral reason across several messages |
| Explicit confirmation | Multi-turn referral requests can require an explicit confirmation before submission |
| Patient lookup | Finds synthetic patients using deterministic name matching |
| Clinical data access | Retrieves patient information, conditions, medications, allergies, observations and encounters |
| Referral creation | Creates a referral after the application validates the request |
| Referral updates | Updates an existing referral when the request contains the required information |
| Referral cancellation | Cancels an existing referral with a supplied cancellation reason |
| Clinical review request | Allows a clinical review request to be created |
| AI clinical review | Uses the configured OpenAI model to review bounded patient information and produce a structured proposal |
| Fail-closed application behaviour | Ambiguous patients, invalid inputs and failed operations do not silently create records |
| Synthetic healthcare data | Uses Synthea-generated data for development and demonstration |

---

## Architecture

```text
┌──────────────────────────────────────────────────────────────────┐
│                     CLINICAL STAFF                               │
└───────────────────────────────┬──────────────────────────────────┘
                                ▼
┌──────────────────────────────────────────────────────────────────┐
│ React Frontend                                                   │
│   • Clinician application                                        │
│   • Patient review                                               │
│   • Referral workflows                                           │
│   • Application monitoring / review views                        │
└───────────────────────────────┬──────────────────────────────────┘
                                ▼
┌──────────────────────────────────────────────────────────────────┐
│ FastAPI Backend                                                  │
│   /chat                                                          │
│   /patients                                                      │
│   /referrals                                                     │
│   /clinical workflows                                            │
│   /health                                                        │
└───────────────────────────────┬──────────────────────────────────┘
                                ▼
┌──────────────────────────────────────────────────────────────────┐
│ Microsoft Agent Framework + OpenAI                               │
│   • Agent session                                                │
│   • Natural-language understanding                               │
│   • Tool selection                                               │
│   • Conversation history                                         │
└───────────────────────────────┬──────────────────────────────────┘
                                ▼
┌──────────────────────────────────────────────────────────────────┐
│ Application Workflows                                            │
│   • Conversation state                                           │
│   • Deterministic patient resolution                             │
│   • Input validation                                             │
│   • Clinical data retrieval                                      │
│   • Referral workflow                                            │
│   • Clinical review workflow                                     │
└───────────────────────────────┬──────────────────────────────────┘
                                ▼
┌──────────────────────────────────────────────────────────────────┐
│ SQLite Database                                                  │
│   • Patients                                                     │
│   • Conditions                                                   │
│   • Medications                                                  │
│   • Allergies                                                    │
│   • Observations                                                 │
│   • Encounters                                                   │
│   • Referrals                                                    │
│   • Clinical workflow runs                                       │
└──────────────────────────────────────────────────────────────────┘
```

### Separation of responsibilities

| Layer | Responsibility |
|---|---|
| **OpenAI model** | Natural-language understanding, tool selection and response generation |
| **Microsoft Agent Framework** | Agent loop, tool invocation and conversation session |
| **Conversation layer** | Multi-turn state, slot collection, confirmation and patient context |
| **Application workflows** | Deterministic orchestration, validation and business rules |
| **Patient resolution** | Converts user-supplied patient information into one database record without guessing |
| **Actions layer** | Performs controlled database writes |
| **SQLite + Synthea** | Synthetic patient data and application persistence |
| **React** | Clinician-facing application interface |

---

## Conversation and tool flow

### Conversation state

`POST /chat` accepts an optional `conversation_id`.

If no conversation exists, the application starts a new conversation. The frontend keeps the conversation identifier and uses it for subsequent messages.

The referral conversation follows a simple state model:

```text
IDLE
  ↓
COLLECTING
  ↓
READY
  ↓
CONFIRMED
  ↓
SUBMITTED
```

The application tracks validated facts such as:

- patient name
- date of birth, when supplied
- department
- referral reason
- conversation stage

### Multi-turn example

```text
Clinician:
I want to refer a patient.

Assistant:
Sure. Which patient?

Clinician:
Aisha Wiegand.

Assistant:
Which department should receive the referral?

Clinician:
Cardiology.

Assistant:
What is the reason?

Clinician:
She has been experiencing chest pain.

Assistant:
Please confirm the referral to Cardiology for Aisha Wiegand
because of chest pain.

Clinician:
Yes, go ahead.

Assistant:
The referral has been created.
```

The application does not invent a missing reason or silently choose an ambiguous patient.

### Confirmation behaviour

- A complete instruction can be submitted directly.
- A referral collected across several turns can require explicit confirmation.
- `yes`, `go ahead`, and similar confirmations continue the workflow.
- `no`, `cancel`, or `never mind` withdraws the pending request.
- Changing important referral details can require confirmation again.

### Structured patient identification

Patient identity is kept as separate fields:

```text
patient_name
date_of_birth
patient_id
```

The model does not choose the internal database identifier.

Patient resolution is handled by `backend/patient_resolution.py`:

- zero matches → patient not found
- one match → workflow continues
- multiple matches → user must clarify
- the workflow never arbitrarily chooses one patient

For example:

```text
"Ram Kumar, 02/10/2003"
```

is treated as a patient name plus date of birth rather than as one combined value.

Dates are interpreted day-first for the application demonstration.

---

## Agent tools

The application exposes tools for patient lookup, clinical data access, referral operations and clinical review.

| Tool | Purpose | Type |
|---|---|---|
| `search_patient(name)` | Search for a patient by name | Read-only |
| `get_patient_information(patient_name, date_of_birth)` | Retrieve a patient summary | Read-only |
| `get_patient_conditions(...)` | Retrieve conditions | Read-only |
| `get_patient_medications(...)` | Retrieve medications | Read-only |
| `get_patient_allergies(...)` | Retrieve allergies | Read-only |
| `get_patient_observations(...)` | Retrieve observations | Read-only |
| `get_patient_encounters(...)` | Retrieve encounters | Read-only |
| `review_patient_for_referral(patient_name, department)` | Run the clinical review workflow | Workflow |
| `create_referral(patient_name, department, reason)` | Create a referral | Write |
| `update_referral(referral_number, patient_name, department, reason)` | Update a referral | Write |
| `cancel_referral(referral_number, patient_name, cancellation_reason)` | Cancel a referral | Write |
| `request_clinical_review(...)` | Create a clinical review request | Write |

The tools accept patient names rather than internal patient IDs. Internal identifiers are resolved by the application.

---

## Clinical review workflow

The clinical review workflow separates AI analysis from database execution.

```text
Patient request
      ↓
Patient resolution
      ↓
Retrieve bounded clinical context
      ↓
OpenAI analysis
      ↓
Structured proposal
      ↓
Application validation
      ↓
Referral / no-action decision
      ↓
Database action when appropriate
```

The workflow records its execution in the `clinical_workflow_runs` table.

### AI output is treated as untrusted input

The model can produce a recommendation, but the application validates the returned structure before using it.

The workflow checks items such as:

- patient identity
- department
- recommendation type
- reason
- evidence supplied by the model
- whether referenced clinical evidence exists in the patient's record

The application does not treat arbitrary model text as a database command.

### Example

```text
Clinician:
Review Aisha Wiegand's clinical information.

→ Patient is resolved.
→ Clinical information is retrieved.
→ AI analysis is performed.
→ A clinical review result is returned.
→ No referral is created unless the workflow explicitly produces
  a valid referral action.
```

A clinical review request and a referral creation request are treated as different application intents.

---

## User interfaces

### Clinician application

The main application provides:

- AI chat
- patient lookup
- clinical information
- referral creation
- referral updates
- referral cancellation
- clinical review
- workflow status
- conversation context

### Patient review

The patient review workflow allows staff to:

1. select a patient
2. select a referral department
3. request an AI-assisted clinical review
4. view the resulting recommendation
5. inspect the associated workflow information

### Application review views

The project also contains application-level views for inspecting workflow activity and referral results during development and demonstrations.

These views are intended for engineering/demo use and are **not an authentication or authorisation system**.

---

## API

The FastAPI backend provides the application API.

Typical endpoints include:

```text
GET  /health
POST /chat
POST /referrals
GET  /referrals
PATCH /referrals/{referral_number}
DELETE /referrals/{referral_number}
POST /clinical-review
GET  /clinical-workflows
```

The exact available routes are defined by `backend/app.py`.

### Example chat request

```json
{
  "message": "Create a cardiology referral for Aisha Wiegand because of chest pain.",
  "conversation_id": null
}
```

### Example referral request

```json
{
  "patient_name": "Aisha Wiegand",
  "department": "Cardiology",
  "reason": "Persistent chest pain"
}
```

The backend resolves the patient and validates the request before performing the database write.

---

## Referral and workflow rules

The application uses deterministic business rules around referral operations.

### Patient identity

The application must resolve the patient before a referral is created.

```text
0 matches  → stop
1 match    → continue
2+ matches → ask for clarification
```

### Supported departments

Referral departments are validated against the application's supported department catalogue.

An unsupported department such as:

```text
XYZ-INVALID-DEPARTMENT
```

does not become a valid referral target.

### Duplicate protection

The application checks for an existing active referral for the same patient and department.

A request such as:

```text
Create another Cardiology referral for the same patient.
```

can be rejected when an active matching referral already exists.

### Updates

An update requires enough information to identify the referral and describe the requested change.

### Cancellation

A cancellation requires:

- the referral being identified
- the patient context
- a cancellation reason

### Fail-closed behaviour

When required information cannot be validated, the application stops rather than guessing.

Examples include:

- unknown patient
- ambiguous patient
- missing referral reason
- unsupported department
- invalid referral number
- malformed AI proposal
- failed database operation

---

## Evidence and testing scenarios

The application is designed to demonstrate both successful and unsuccessful workflow paths.

### Scenario 1 — Valid referral

```text
Create a cardiology referral for Aisha Wiegand
because of persistent chest pain.
```

Expected:

```text
Patient resolved
      ↓
Input validated
      ↓
Referral created
```

### Scenario 2 — Missing reason

```text
Create a referral for Aisha Wiegand to Cardiology.
```

Expected:

```text
Request incomplete
      ↓
No referral created
      ↓
Assistant asks for the missing reason
```

### Scenario 3 — Unknown patient

```text
Create a referral for Zebulon Nobody
to Neurology for migraines.
```

Expected:

```text
Patient not found
      ↓
No referral created
```

### Scenario 4 — Ambiguous patient

```text
Show me information for Ram.
```

If several synthetic patients match, the application asks the clinician to identify the correct patient.

### Scenario 5 — Unsupported department

```text
Create a referral for Aisha Wiegand
to XYZ-INVALID-DEPARTMENT for chest pain.
```

Expected:

```text
Unsupported department
      ↓
No referral created
```

### Scenario 6 — Duplicate referral

If an active referral already exists for the same patient and department:

```text
Create another Cardiology referral for Aisha Wiegand.
```

Expected:

```text
Duplicate detected
      ↓
No additional referral created
```

### Scenario 7 — Declined confirmation

A multi-turn referral is prepared and the assistant asks for confirmation.

```text
Clinician:
No, cancel that.
```

Expected:

```text
Pending referral withdrawn
      ↓
No database write
```

### Scenario 8 — Clinical review

```text
Review Aisha Wiegand's clinical information.
```

Expected:

```text
Patient resolved
      ↓
Clinical information retrieved
      ↓
AI review performed
      ↓
Clinical review result returned
```

The review request should not automatically be interpreted as a direct referral request.

---

## Setup and running locally

Commands below assume **Windows PowerShell** and the project root.

### Prerequisites

- Python 3.10+
- Node.js 20.19+ or 22.12+
- Git
- OpenAI API key
- Microsoft Agent Framework dependencies
- SQLite

### 1. Create the Python environment

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

**What it does:** Creates an isolated Python environment and installs the backend dependencies.

### 2. Configure environment variables

Create the local environment file:

```powershell
Copy-Item .env.example .env
```

**What it does:** Creates `.env` from the example configuration.

Open it:

```powershell
notepad .env
```

Set:

```text
OPENAI_API_KEY=your_api_key_here
```

Do not commit `.env` to Git.

### 3. Create the database

```powershell
python -m scripts.create_database
```

**What it does:** Creates the SQLite database schema.

### 4. Load synthetic patient data

```powershell
python -m scripts.load_database
```

**What it does:** Loads the processed Synthea CSV data into SQLite.

### 5. Validate the dataset

```powershell
python -m scripts.validate_data
```

**What it does:** Runs sanity checks over the processed healthcare dataset.

### 6. Start the backend

From the project root:

```powershell
python main.py
```

Alternatively:

```powershell
uvicorn main:app --reload
```

The API should be available at:

```text
http://localhost:8000
```

### 7. Start the frontend

Open a second PowerShell terminal:

```powershell
cd frontend
npm install
npm run dev
```

**What it does:** Installs frontend dependencies and starts the Vite development server.

The frontend should be available at:

```text
http://localhost:5173/
```

### 8. Reset demonstration data

Before a clean demonstration, use:

```powershell
python -m scripts.reset_demo_data --yes
```

**What it does:** Resets referral/workflow demonstration state while preserving the synthetic patient dataset.

Use this only when you want a clean demo state.

### 9. Optional end-to-end demonstration

```powershell
python -m scripts.run_e2e_demo
```

**What it does:** Runs the project's live OpenAI demonstration scenarios against the configured application.

---

## Client demo guide

A simple demonstration flow:

### 1. Start the application

Run the backend and frontend.

### 2. Demonstrate patient lookup

Ask:

```text
Show me information for Aisha Wiegand.
```

Demonstrate that the application resolves the patient and retrieves the relevant clinical information.

### 3. Demonstrate multi-turn referral creation

Use:

```text
I want to refer a patient.
```

Then:

```text
Aisha Wiegand
```

Then:

```text
Cardiology
```

Then:

```text
She has persistent chest pain.
```

Finally:

```text
Yes, go ahead.
```

Show that the application creates the referral.

### 4. Demonstrate invalid input

Try:

```text
Create a referral for Aisha Wiegand to XYZ
for persistent chest pain.
```

Show that the unsupported department is rejected.

### 5. Demonstrate patient ambiguity

Ask:

```text
Show me information for Ram.
```

If several records match, the application asks for additional information rather than selecting a patient automatically.

### 6. Demonstrate duplicate protection

Create a valid referral first, then attempt another referral for the same patient and department.

Show that the second referral is rejected when it violates the application's duplicate rule.

### 7. Demonstrate clinical review

Open the patient review workflow and request an AI-assisted review.

Show:

```text
Patient
   ↓
Clinical data
   ↓
AI analysis
   ↓
Structured result
   ↓
Workflow result
```

---

## Testing

The project contains backend and frontend tests.

Typical commands:

### Backend

```powershell
python -m pytest
```

**What it does:** Runs the Python backend test suite.

### Frontend tests

```powershell
cd frontend
npm test
```

**What it does:** Runs the frontend unit tests.

### Frontend lint

```powershell
npm run lint
```

**What it does:** Checks the frontend source for linting problems.

### Frontend build

```powershell
npm run build
```

**What it does:** Creates the production frontend build and catches build-time issues.

### Test areas

| Test area | Purpose |
|---|---|
| Patient tools | Patient resolution, unknown patients and ambiguous matches |
| API | Endpoint behaviour and contracts |
| Agent | MAF agent loop and tool invocation |
| Conversation | Multi-turn state, confirmation and cancellation |
| Patient identification | Name + date of birth handling |
| Referral tools | Create, update and cancellation workflows |
| Clinical workflow | AI output validation and workflow state |
| Proposals | Structured AI output validation |
| Frontend | UI behaviour and component tests |

The backend tests use isolated test data where configured, so development tests should not depend on the production/demo database state.

---

## Project structure

```text
Healthcare-Referral-Agent/
├── backend/
│   ├── app.py                  # FastAPI endpoints
│   ├── chat.py                 # ChatService and response handling
│   ├── conversation.py         # Conversation state and confirmation
│   ├── agent.py                # Microsoft Agent Framework + OpenAI agent
│   ├── agent_tools.py          # Agent tools
│   ├── workflow.py             # Referral workflow
│   ├── actions.py              # Controlled database actions
│   ├── patient_resolution.py   # Deterministic name → patient resolution
│   ├── clinical_workflow.py    # AI clinical review workflow
│   ├── clinical_context.py     # Bounded clinical context retrieval
│   ├── analysis.py             # OpenAI clinical analysis
│   ├── proposals.py            # AI proposal validation
│   ├── clinical_runs.py        # Workflow run persistence
│   ├── domain.py               # Domain types
│   ├── schemas.py              # API schemas
│   ├── tools/
│   │   └── patient_tools.py    # Read-only patient data tools
│   └── database/
│       ├── connection.py
│       └── models.py
│
├── frontend/
│   └── src/
│       ├── api.js              # Backend API client
│       ├── clinician/          # Clinician-facing application
│       ├── components/         # Shared UI components
│       └── lib/                # Frontend utilities
│
├── scripts/
│   ├── create_database.py
│   ├── load_database.py
│   ├── validate_data.py
│   ├── extract_synthea_data.py
│   ├── reset_demo_data.py
│   ├── run_e2e_demo.py
│   └── legacy/
│
├── tests/
│   ├── test_agent.py
│   ├── test_api.py
│   ├── test_conversation.py
│   ├── test_patient_identification.py
│   ├── test_clinical_workflow.py
│   └── ...
│
├── data/
│   └── processed/
│
├── docs/
├── main.py
├── requirements.txt
├── requirements-lock.txt
├── pytest.ini
└── .env.example
```

---

## Design principles

### 1. AI is not the database

The model interprets natural language, but application code determines what is actually written to the database.

### 2. Deterministic patient identity

The model does not choose internal patient identifiers.

### 3. No guessing

The application does not arbitrarily select between multiple patients.

### 4. Validate before writing

Referral inputs are validated before the database is changed.

### 5. Separate read and write operations

Clinical data retrieval is separated from referral-changing operations.

### 6. Explicit workflow states

Multi-turn referral conversations maintain structured state instead of relying entirely on free-form model memory.

### 7. Truthful responses

The application should report what actually happened rather than allowing the model to claim a successful action that did not occur.

### 8. Synthetic data only

All patient information used for this demonstration comes from synthetic data.

---

## Known limitations

This project is a demonstration build and is **not production-ready**.

### Security and identity

- No production authentication or authorisation system is included.
- Role-based access control is not implemented.
- The application should not be exposed publicly with real healthcare data.
- Secrets must be supplied through environment configuration and never committed to Git.

### Healthcare integration

- The application uses SQLite.
- It does not currently integrate with a production EHR/EMR.
- FHIR-based interoperability is not implemented as a production integration.
- Synthetic Synthea data is used instead of real clinical records.

### Human review

The current application does not provide a complete production human-in-the-loop authorisation system.

A future implementation should provide:

- reviewer identity
- role-based permissions
- review queues
- explicit approval/rejection
- audit history
- escalation rules

### AI limitations

- LLM output can be incorrect or poorly phrased.
- AI recommendations are not a substitute for clinical judgement.
- Automated tests use controlled model clients where applicable and do not guarantee live-model behaviour.
- Clinical grounding checks whether cited information exists in the available patient record; they do not determine whether a recommendation is clinically appropriate.

### Operational limitations

- Conversations are stored in application memory.
- Conversations can be lost when the server restarts.
- `/health` is an application liveness check rather than a complete dependency health check.
- SQLite is appropriate for this demonstration but is not the target architecture for a high-scale production healthcare system.

---

## Real-world applicability

The architectural pattern demonstrated here can be extended to enterprise healthcare workflows where an AI assistant helps staff perform administrative or clinical-support tasks.

A production architecture could look like:

```text
EHR / EMR / FHIR
       ↓
Enterprise application
       ↓
AI agent
       ↓
Structured action proposal
       ↓
Application validation
       ↓
Identity + authorisation
       ↓
Risk assessment
       ↓
Human review where required
       ↓
Controlled business action
       ↓
Audit / monitoring
```

Potential production capabilities include:

- FHIR-based EHR integration
- enterprise identity
- RBAC
- human-in-the-loop review
- durable workflow state
- production database
- observability
- model/version tracking
- privacy controls
- security monitoring
- disaster recovery
- CI/CD
- cloud deployment

The current project should be viewed as a technical demonstration of an AI-assisted healthcare workflow rather than as a deployable clinical product.

---

## Roadmap

| Phase | Scope | Status |
|---|---|---|
| 1. Healthcare Referral Agent | Patient resolution, referral workflow, UI and database | Implemented |
| 2. Clinical Review | AI clinical review, proposal validation and workflow tracking | Implemented |
| 3. Conversation improvements | Multi-turn state, confirmation, structured patient identification, update/cancel/review actions | Implemented |
| 4. Identity / RBAC | Authentication, roles and enterprise identity | Planned |
| 5. Human-in-the-loop | Review queue, reviewer decisions and escalation | Planned |
| 6. Production data integration | EHR/EMR and FHIR integration | Planned |
| 7. Production database | Scalable relational database and durable workflow state | Planned |
| 8. Observability | Metrics, logging, tracing and operational dashboards | Planned |
| 9. Cloud / DevOps | Containers, CI/CD, secrets management and cloud deployment | Planned |
| 10. Enterprise security | Privacy, security controls, auditing and compliance processes | Planned |

---

## Disclaimer

This project uses synthetic healthcare data generated for development and demonstration purposes.

It is **not a production clinical system** and must not be used for:

- real patient care
- diagnosis
- treatment decisions
- medication decisions
- clinical decision-making without qualified professional oversight

Any future production deployment would require appropriate clinical validation, security review, privacy controls, regulatory assessment, human oversight and enterprise integration.

---

## License

Add the project's chosen license here before public distribution.

---

## Project purpose

This repository demonstrates how **Microsoft Agent Framework, OpenAI, FastAPI, React, SQLite and synthetic healthcare data** can be combined to build an AI-assisted healthcare referral workflow.

The focus is on:

```text
Natural-language interaction
        ↓
Agent orchestration
        ↓
Deterministic application logic
        ↓
Validated workflow
        ↓
Controlled database action
```

The goal is to make AI-assisted workflow automation more predictable, testable and understandable while keeping the final application responsible for business rules and system state.
