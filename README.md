# Healthcare Referral Agent

A standalone healthcare referral assistant for synthetic Synthea data. Clinicians can find patients, inspect clinical information, create and manage referrals, request clinical review, and use a Microsoft Agent Framework assistant backed by OpenAI.

## Architecture

React frontend → FastAPI → Microsoft Agent Framework + OpenAI → validated application workflows → SQLite.

Patient resolution, required referral details, supported departments, duplicate prevention, referral ownership, clinical proposal grounding, and workflow state are validated by the application before database writes.

## Run locally

1. Create a virtual environment and install `requirements.txt`.
2. Copy `.env.example` to `.env` and set `OPENAI_API_KEY`.
3. Start the API with `uvicorn main:app --reload`.
4. In `frontend`, run `npm install` then `npm run dev`.

Alternatively run `docker compose up --build` after setting `OPENAI_API_KEY` in `.env`.

## Testing

Run backend tests with `pytest`, and frontend checks with `npm test`, `npm run lint`, and `npm run build` from `frontend`.
