# Implementation Plan — Kiro Usage Value-for-Money Analyser

> The application will live in a new repository; all paths are relative to that repository. The supplied archive must not be copied or committed because it contains personal data.

## 1. Problem Statement
Kiro’s daily user-activity reports expose credits, messages, models, clients, subscription tiers, and user identity, but not input, output, or cache token counts. This makes it difficult to assess subscription utilisation and compare Kiro spend with the hypothetical cost of equivalent Amazon Bedrock API usage.

Build a local, ephemeral website that securely accepts a Kiro usage ZIP and provides three analysis levels: (1) recorded usage facts, (2) estimated Amazon Bedrock API costs, and (3) per-person subscription value-for-money analysis. The application must distinguish recorded facts from assumptions and never present reconstructed token counts or API costs as exact.

## 2. Current State
The supplied sample contains 36 CSV files covering multiple days and KIRO_CLI, KIRO_IDE, and PLUGIN. Each row is a daily aggregate for a user and client. Fields include Date, UserId, User_Email, ProfileId, Client_Type, Subscription_Tier, Credits_Used, overage fields, usage limits, conversations, total messages, and dynamic *_messages model columns. Headers change between dates and clients, and legacy aliases exist. There are no input, output, cache-read, or cache-write token counts. Total_Messages includes prompts, responses, and tool calls, so it is not equivalent to API invocations. The application is standalone and is not part of ai-sandbox.

## 3. Target State
A user runs one local command and opens a site bound only to 127.0.0.1. They upload a ZIP, review import quality, and select an analysis level.

### Level 1: Recorded usage
Exact credits, overage, messages, conversations, users, observed subscriptions, breakdowns by date/user/profile/tier/client/model, and quality warnings. No estimates mixed into recorded totals.

### Level 2: Bedrock-equivalent cost
Two labelled methods: configurable low/typical/high token assumptions per model message; and credit-weighted scenarios using documented Kiro model multipliers plus configurable token-per-base-credit assumptions. Show pricing coverage, exclusions, unmapped models, auto uncertainty, and USD ranges.

### Level 3: Value for money
Configurable Kiro seat/overage prices, monthly utilisation by observed subscription, Kiro spend versus estimated Bedrock range, person drill-down, trends, heavy/light utilisation, and statistical outliers. Warn that usage does not measure productivity or quality.

## 4. Existing Patterns and Research Findings
Official Kiro documentation says reports are daily around 02:00 UTC, separate for IDE/CLI/plugin, with static and dynamic model columns. Credits are fractional workload units influenced by complexity and model multipliers, not tokens. A subscription is billed once for a user within a profile; another profile can be another billable subscription. Public list prices researched 14 October 2026: Pro 1,000 credits/$20; Pro+ 2,000/$40; Pro Max 5,000/$100; Power 10,000/$200; overage/add-ons $0.04/credit. Values must be configurable for enterprise pricing.

AWS documents four separately priced token types: input, output, cache read, cache write; rates can vary by model, tier, region, and routing. Calculations are estimates unless reconciled with CUR. Implementation must manually verify current eu-west-2 standard on-demand rates against official AWS pricing; no unverified Bedrock rates. Only exact equivalents map automatically. Legacy aliases may map to exact models; unavailable/unknown/auto remain unpriced unless an explicit user assumption is selected. Show pricing coverage.

## 5. Requirements
Functional: accept ZIPs up to 100 MB; process nested per-client CSV without extracting; ignore harmless metadata; support changing headers and future model columns; display emails; subscription key (ProfileId, UserId); preserve raw names and versioned mappings; deduplicate without double count; all three levels; USD; Bedrock eu-west-2 standard on-demand only; configurable token/Kiro assumptions; ephemeral with no DB/export; no runtime network requirement.

Non-functional: bind localhost; do not log PII/content; prevent archive bombs/traversal/encryption/member abuse/malformed CSV/XSS; escape values; local assets and CSP; discard temp uploads; expire sessions; WCAG 2.2 AA; exact dependency pins and lockfile; show price source/date/assumptions/coverage.

Acceptance: representative synthetic ZIP imports; exact recorded reconciliation; unknown columns tolerated; no invented model costs; deterministic assumptions; traceable estimates; restart clears data; hostile archives fail safely; tests/lint/types pass cleanly.

## 6. Proposed Architecture
Python 3.12+, FastAPI, Jinja, server-rendered semantic HTML, no frontend framework/CDN, uv dependency management. Browser -> localhost FastAPI -> safe ZIP reader -> Kiro CSV adapter -> canonical usage facts -> ephemeral memory. Versioned Bedrock catalogue/model aliases/Kiro multipliers plus user assumptions feed cost estimators and VFM analysis; server-rendered views return to browser.

Canonical records retain date, user ID/email, profile, client, tier, credits/overage, conversations/messages, raw/canonical model, model-message counts, source member/report timestamp. Subscription key is (ProfileId, UserId); logical daily key is (ProfileId, UserId, Date, Client_Type). Exact duplicates ignored; conflicts use deterministic latest-report rule and are warned.

Cost formula sums each estimated token type times its per-million rate. Pricing coverage is included model messages divided by total model messages. Auto and unmapped models excluded by default; explicit proxy assumptions remain labelled.

## 7. Dependencies
Create/select the new repository before Task 1 and save plan.md there. Never copy production reports. Use synthetic fixtures. Task 4 needs manual AWS pricing verification. Tasks 1-3 do not depend on pricing. No ai-sandbox runtime dependency.

## 8. Chapters and Tasks

### Chapter 1 — Establish a trustworthy local ingestion path

### Task 1: Securely accept a Kiro report archive
Story: As an analyst, I need to open the application locally and safely upload a Kiro ZIP so sensitive usage data never leaves my machine.
Objective: runnable local app validating an archive and showing safe import summary. Size: medium, up to two days.
Files: pyproject.toml, uv.lock, .gitignore, README.md, .github/workflows/test.yml, src/kiro_vfm/__init__.py, app.py, config.py, adapters/archive.py, services/sessions.py, web/routes.py, templates/base.html, upload.html, import_summary.html, static/app.css, tests/helpers/archive_factory.py, tests/test_app.py, test_archive.py, test_upload.py.
Changes: bootstrap; exact pins; localhost; archive limits; direct ZIP reads; ephemeral IDs/expiry; host/origin/CSRF/CSP/security; safe summary; CI.
Tests: `uv sync --frozen`; `uv run pytest tests/test_app.py tests/test_archive.py tests/test_upload.py -q`; `uv run ruff check .`; `uv run mypy src`.
Demo: run `uv run kiro-vfm`, upload synthetic ZIP, see validation summary without persistence.
Release: 0.1.0-alpha.1. Commit: `feat: securely accept Kiro usage archives`. Checkpoint: wait for approval.

### Task 2: Parse changing Kiro CSV schemas into canonical facts
Story: normalise days/clients reliably as columns change. Objective: parse reports and show row/date/client/user/model quality. Size medium.
Files: domain/usage.py, adapters/kiro_csv.py, services/import_usage.py, routes.py, import_summary.html, synthetic fixtures, parser/import tests, upload tests, README.
Changes: immutable records, strict required/optional parsing, dynamic *_messages, raw names, data validation, duplicate/conflict handling, safe rejected-row reporting.
Tests: parser/import/upload tests plus Ruff/mypy. Demo multi-day summary. Release alpha. Commit `feat: normalise Kiro daily usage reports`. Checkpoint.

### Chapter 2 — Deliver analysis progressively

### Task 3: Deliver Level 1 recorded-usage analysis
Story: see exact report facts before estimates. Objective: auditable recorded dashboard. Size medium.
Files: services/recorded_analysis.py, web/view_models.py, routes/templates/static, tests, README.
Changes: filters, exact KPIs, daily/person/client/model views, person email/profile/tier, accessible tables/charts, message distinction.
Tests: analysis/routes/full/lint/types. Demo reconcile one email. Release 0.1.0. Commit `feat: show recorded Kiro usage breakdowns`. Checkpoint.

### Task 4: Add auditable Bedrock pricing and mapping coverage
Story: know exact Bedrock model coverage. Objective: versioned eu-west-2 catalogue and coverage, no costs yet. Size medium with pricing review.
Files: domain/pricing.py, adapters/catalog.py, data pricing/mapping JSON and schemas, pricing coverage template/routes/tests, docs/updating-prices.md.
Changes: all token rate types, region/tier/routing/currency/effective/retrieved/source metadata, exact mappings/aliases, unknown/unavailable/auto unpriced, coverage, two-person update process, offline deterministic data.
Tests: catalog/mapping/routes/lint/types. Demo coverage and sources. Commit `feat: add auditable Bedrock price coverage`. Checkpoint with AWS review.

### Task 5: Deliver Level 2 message-based token scenarios
Story: transparent range instead of false precision. Objective: low/typical/high costs from per-model-message assumptions. Size medium.
Files: domain/estimates.py, services/cost_analysis.py, web/forms/routes/templates/static, estimate/route tests, README.
Changes: four token assumptions plus optional calls/message, dated price calculation, explicit auto handling, ranges/coverage/exclusions, ephemeral settings, estimate labels.
Tests: fictitious rates, all token types, unknowns, dates, precision, malicious forms. Demo live assumption change. Commit `feat: estimate Bedrock cost from token scenarios`. Checkpoint.

### Task 6: Add credit-weighted interpolation
Story: use recorded credits and documented multipliers as an alternative estimate. Objective: compare estimators. Size medium.
Files: estimates/cost/catalog, Kiro multiplier JSON/schema, forms/templates, tests, docs/methodology.md.
Changes: dated sourced multipliers, weighted allocation, base work units, token-per-base-credit assumptions, exclude unavailable history, side-by-side results, clear non-reconstruction label.
Tests: mixed/missing/auto/zero/decimal/time cases. Commit `feat: add credit-weighted cost interpolation`. Checkpoint.

### Task 7: Deliver Level 3 subscription value-for-money analysis
Story: compare observed Kiro spend and Bedrock range per person/month. Objective: subscription analysis/trends/outliers. Size medium.
Files: domain/subscriptions.py, services/vfm_analysis.py, forms/routes/templates/static, subscription/VFM/route tests, README.
Changes: configurable Kiro price defaults, monthly spend per profile/user highest tier, utilisation/overage, per-email delta/ratio/range, trends/outliers, partial period and inactive-seat warnings, neutral terminology.
Tests: tiers/upgrades/profiles/months/partial/overage/zero coverage. Commit `feat: compare Kiro subscription spend with Bedrock estimates`. Checkpoint.

### Chapter 3 — Make analysis safe to interpret and operate

### Task 8: Expose methodology and complete release validation
Story: funding decisions need visible evidence/uncertainty. Objective: auditable accessible local release. Size small.
Files: routes/base/methodology/data_quality/static/tests, README, SECURITY.md, methodology/pricing/operations docs, CI.
Changes: on-screen formulas/provenance/limitations, quality page, no PII logs, local lifecycle docs, accessibility tests, clean smoke, source-control exclusions.
Tests: full frozen sync, pytest, Ruff, mypy, `uv run kiro-vfm --check`, manual browser accessibility smoke. Release 1.0.0. Commit `docs: make Kiro cost analysis auditable and releasable`. Checkpoint.

## 9. Summary Table
1 secure upload/foundation; 2 normalization/data ingestion; 3 Level 1; 4 pricing foundation; 5 Level 2 scenarios; 6 Level 2 interpolation; 7 Level 3 VFM; 8 release trust/hardening.

## 10. Test Strategy
TDD in each task; synthetic fixtures only; Decimal calculations; unit tests for parser/aggregation/pricing/interpolation/subscriptions; route tests for uploads/filters/forms/sessions/escaping/errors; configurable reduced limits for hostile ZIP tests; regression fixtures for schema changes; full tests/lint/types at checkpoints; independent manual price verification.

## 11. Technical Debt
Static pricing is reproducible but manual; in-memory storage is local-only; activity reports omit inactive paid seats; future token telemetry should add a measured estimator; missing historical multipliers remain visible, never guessed.

## 12. Risks and Mitigations
False precision -> labels/ranges/formulas/coverage. Missing tokens -> two scenarios. Auto -> exclude/default explicit assumptions. Unavailable model -> no substitution. Price drift -> dated catalogue/review. Duplicates -> conflict handling. Partial months/inactive seats -> warnings. Archive attack -> limits/no extraction/escaping/tests. PII -> localhost/ephemeral/no logging. Performance misuse -> ethical warning. Schema drift -> dynamic parsing and quality report.

## 13. Rollback Strategy
Each task independently revertible; hide unfinished levels; isolate pricing changes. Current execution stops after Task 1 and no commit is made.
