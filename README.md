# Kiro usage value-for-money analyser

A local, ephemeral web application that securely accepts a Kiro usage ZIP
export and (in later tasks) analyses recorded usage, estimated Amazon Bedrock
equivalent cost, and per-person subscription value for money.

> **Status:** Tasks 1–3 — secure local archive upload/validation; parsing of
> Kiro daily CSV reports into immutable canonical records with a data-quality
> report; and **Level 1 recorded-usage analysis** (exact recorded facts with
> filters and breakdowns). No cost, token, or value-for-money analysis is
> implemented yet. See `plan.md`.

## Privacy and safety

- Runs **only on `127.0.0.1`** by default. Nothing is sent over the network.
- Uploaded archives are validated **in memory** and **never extracted to
  disk**. Members are inspected through `zipfile` metadata only.
- Analysis sessions are **in memory**, keyed by an unguessable token, and
  expire after a configurable period of inactivity. There is **no database**.
  Restarting the process clears all state.
- Parsed **canonical records may contain personal data** (user ids, emails)
  because later analysis needs it. That data lives **only in memory** for the
  session's lifetime — it is never written to disk, never logged, and dropped
  on session expiry or process restart. Data-quality issues shown on the
  summary page are generic and contain **no** email, user id, profile id, or
  raw cell values.
- Upload content and PII are **never logged**.
- Report archives and CSV data are excluded from source control via
  `.gitignore`. **Never commit a real Kiro export** — it contains personal data.

## Requirements

- Python 3.12 or newer
- [uv](https://docs.astral.sh/uv/)

## Install

```bash
uv sync --frozen
```

## Run

```bash
uv run kiro-vfm
```

Then open <http://127.0.0.1:8000> in your browser.

Options:

```bash
uv run kiro-vfm --host 127.0.0.1 --port 8000
uv run kiro-vfm --check   # construct the app and exit (startup smoke test)
```

## Configuration

All limits are configurable via environment variables (defaults shown):

| Variable | Default | Meaning |
| --- | --- | --- |
| `KIRO_VFM_HOST` | `127.0.0.1` | Bind host |
| `KIRO_VFM_PORT` | `8000` | Bind port |
| `KIRO_VFM_SESSION_TTL_SECONDS` | `1800` | Session inactivity expiry |
| `KIRO_VFM_MAX_COMPRESSED_BYTES` | `104857600` | Max upload size (100 MB) |
| `KIRO_VFM_MAX_MEMBER_COUNT` | `10000` | Max members in the archive |
| `KIRO_VFM_MAX_MEMBER_EXPANDED_BYTES` | `52428800` | Max expanded size per member |
| `KIRO_VFM_MAX_TOTAL_EXPANDED_BYTES` | `524288000` | Max total expanded size |
| `KIRO_VFM_MAX_COMPRESSION_RATIO` | `200` | Max expanded/compressed ratio per member |
| `KIRO_VFM_MAX_ROWS_PER_CSV` | `100000` | Max data rows parsed from one CSV member |
| `KIRO_VFM_MAX_ROWS_PER_IMPORT` | `1000000` | Max data rows parsed across the whole import |
| `KIRO_VFM_MAX_MEMBER_READ_BYTES` | `52428800` | Hard cap on actual decompressed bytes read per member |

## What validation enforces

The archive is rejected as a whole if it is empty, not a valid ZIP, malformed,
over the compressed-size limit, has too many members, exceeds the total
expanded-size budget, or contains **no CSV reports**.

Individual members are rejected (and reported) if they use path traversal,
absolute or Windows drive paths, backslashes, or NUL bytes in their names; are
symlinks or other special files; are encrypted; exceed the per-member expanded
size or compression ratio; or are an unsupported (non-CSV) type. Harmless
metadata such as `.DS_Store`, `Thumbs.db` and `__MACOSX/` entries is ignored.

## What parsing produces

Accepted CSV members are parsed into immutable canonical usage records. The
report schema changes between days and clients, so parsing:

- resolves the fixed required headers case-insensitively and tolerates column
  reordering and unknown extra columns;
- treats any `*_messages` column (case-insensitive) as a dynamic per-model
  message count, preserving the raw model identifier and adding a mechanical
  lowercase slug (separators normalised only — no aliasing or pricing);
- decodes UTF-8 (BOM-tolerant), handles quoting and embedded newlines, and
  rejects invalid UTF-8, duplicate/blank headers, missing/extra cells, and
  invalid values (non-finite/negative decimals, non-integer counts, non
  `true`/`false` booleans, non-ISO dates, empty identities) with safe,
  generic per-file/per-row issues;
- ignores exact-duplicate rows (counted and warned) and resolves rows that
  share a daily key `(ProfileId, UserId, Date, Client_Type)` by keeping the
  latest report (deterministic tie-break by member name then index) — never
  summing conflicting rows.

The summary page shows archive counts, accepted/rejected rows, duplicate and
conflict counts, issue totals by severity, the date range, distinct users,
observed subscriptions, client types, and the canonical models used, plus an
accessible table of generic data-quality issues.

## Level 1: recorded usage

From the import summary you can open the **recorded usage** page
(`/analysis/{token}`). It shows **exact recorded facts only** — no estimates,
pricing, token counts, or value-for-money figures:

- totals: credits used, overage credits, total messages, chat conversations,
  recorded per-model messages, distinct users, observed subscriptions, and
  distinct days (all credit sums use exact `Decimal` arithmetic);
- breakdowns by date, by person (subscription), by client, and by model;
- filters by date range and client type (invalid filter values are ignored
  and simply widen the view rather than erroring).

The person breakdown is keyed by the subscription key `(ProfileId, UserId)`
and displays the email to help you reconcile a specific person. The page
states clearly that `Total messages` counts prompts, responses, and tool
calls — it is **not** an API-invocation count — and that credits are recorded
workload units, not tokens. Bedrock cost estimates and value-for-money
analysis are not implemented yet.

## Development

```bash
uv run pytest -q          # run all tests
uv run ruff check .       # lint
uv run mypy src           # type-check
```
