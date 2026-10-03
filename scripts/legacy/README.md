# Legacy scripts (not maintained)

Ad-hoc scripts from early Phase 1 development, kept for reference only.

- They are **not** part of the automated test suite (`pytest` collects `tests/` only).
- Several pre-date the current design (for example, asking the model for a
  patient UUID, or importing `check_referral_governance`, which no longer
  exists) and will not run against the current code.
- Some write to the real `data/healthcare.db` and `data/ledger.jsonl`.

Use `python -m pytest` for verification and `python -m scripts.run_e2e_demo`
for the live OpenAI demonstration instead.
