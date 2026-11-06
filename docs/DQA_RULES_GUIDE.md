# Adding or changing DQA criteria

The DQA run number and ruleset version are different:

- **Run 1, Run 2, ...** are assessment attempts for one batch. The API assigns these numbers automatically. They are not criteria you enter in the dialog.
- **essi-mvp-v4** identifies a version of the assessment criteria. Selecting a historical run shows its saved results; it does not change the active rules.

## Where criteria live

Start with `bcmd_dqa_rules.py` in the API checkout.

1. Add the rule's unique code, severity, label and reference to `CHECKS`.
2. Add its user-facing correction message to `REMARKS`.
3. Implement the actual condition in `evaluate()`. Merely adding a CHECKS entry does not execute a validation.
4. Add the rule's relevant configuration to `CONFIG`, so changes affect the canonical ruleset hash. `CANONICAL` and `HASH` are computed from that configuration.
5. Bump `VERSION` when changing behavior, for example from `essi-mvp-v4` to `essi-mvp-v5`, and preserve prior versions in `PREVIOUS_VERSIONS`. Do not rename historical database runs.
6. Add unit tests in `tests/test_bcmd_dqa_rules.py` and database/worker tests where needed. Use synthetic fixtures only.

Payment-history criteria are shared across candidates, DQA, NAPA and Payroll. Their authoritative logic belongs in `bcmd_payment_history.py`; do not implement a different payment rule solely in the DQA evaluator. Candidate/category predicates live in `bcmd_candidates.py`.

## Data needed for a new rule

The worker in `bcmd_dqa.py` evaluates frozen batch members. Inspect `bcmd_store.py` for snapshot fields. If a criterion needs information not already saved, define how the API obtains and freezes authoritative evidence. A schema change needs an additive migration and tests. Do not fabricate missing evidence or treat it as verified.

Changing source information does not automatically refresh a historical frozen batch. Create a new batch when corrected source evidence must be captured. Duplicate-selection resolutions and current payment-history checks use their existing audited mechanisms.

## Publishing a new ruleset

Deploy the API and restart its DQA worker together so they use the same version/hash. Existing runs remain immutable and visible in history. A new current DQA run is required before creating new NAPA files or using current payroll eligibility. An already exported batch cannot be rerun; preserve its issued files and use a new batch when a new assessment is needed.

The current workflow automatically makes completed BCMD DQA batches visible in NAPA. There is no manual Proceed or owner-approval step. Payment-history, authorization and file-integrity checks still apply. Historical exports retain their recorded bytes and hashes.

## Dialog controls

- **Run DQA:** queues a new attempt using the active server ruleset. It is disabled while the latest run is active or the batch has issued NAPA files; the reason is shown.
- **Retry same DQA request:** safely retries an uncertain start with the original idempotency key.
- **Refresh DQA results:** reloads saved progress/findings without starting a new run.
- **DQA run history:** selects which historical attempt to inspect.
- **Show latest run:** returns from a historical attempt to the latest one.
- **Rules and criteria used in this run:** expands the criteria supplied by that saved run. This is read-only.
- **Open in NAPA:** navigates to the batch without recording another handoff.

There is currently no browser-based rule editor. Criteria are code-reviewed changes in the API, not values to type into the run-history selector.
