# tcs_klaviyo_profiles — Klaviyo people + CLV (direct-API)

Pulls the Klaviyo **profile** (person) dimension, including `predictive_analytics`, into
`raw_windsor.tcs_klaviyo_profiles`.

`tcs_klaviyo_events` is the *event* stream (what was sent to whom). This is the *person*
dimension (who they are, what they are worth) — it is what lets a dashboard segment by
customer value or churn risk instead of only by email activity, and it is the backbone of the
old notebook's `DATABASE_email` master table.

| | |
|---|---|
| Raw table | `agora-data-driven.raw_windsor.tcs_klaviyo_profiles` (asia-southeast1) |
| Secret | `tcs-klaviyo-key` (read from Secret Manager via ADC) |
| Job | `tcs-klaviyo-profiles-ingest`, daily at 02:20 Asia/Singapore |
| Grain | one row per profile **per pull** (append-only; the staging view keeps the newest per `id`) |

## Why append instead of merge

The notebook did `WRITE_TRUNCATE` every run, which threw away history. Appending makes the
table double as a slowly-changing record of how CLV / churn scores moved over time, at
negligible storage cost. Consumers take the newest row per `id`.

## Custom properties are open-ended

Klaviyo custom properties are account-defined and change without notice, so they are stored
**two ways**:

- `properties_json` — the whole bag, verbatim. Nothing is silently dropped because the loader
  had not heard of a property yet.
- **lifted columns** for the ones this account actually populates, so common queries do not
  have to JSON-parse: `shopify_tags`, `accepts_marketing`, `signup_source`, `klaviyo_source`
  (`$source`), `consent`, `consent_timestamp`, `business_url`.

Adding a lifted column later is a pure widening — the value was in `properties_json` all along.

Note `_s()`: an **empty** list/dict becomes `NULL`, not `""`. Klaviyo sends `[]` for "no tags",
and an empty string would look like a real value and quietly break `IS NULL` checks.

## Incremental

Klaviyo bumps `updated` on any profile change, so the watermark is `MAX(updated)` already
loaded minus `WATERMARK_BUFFER_HOURS` (default 24) to tolerate clock skew and late-settling
writes.

Results are sorted **`updated` ASCENDING** — this is load-bearing, not cosmetic. With a
descending sort, a first run cut short by `RUN_BUDGET_SEC` would never advance past the newest
page and the watermark could never move. Ascending means an interrupted run still raises the
high-water mark and the next run resumes exactly where it stopped.

## Env

| Var | Default |
|---|---|
| `WATERMARK_BUFFER_HOURS` | `24` |
| `RUN_BUDGET_SEC` | `3000` |
| `FLUSH_EVERY` | `5000` rows per BigQuery append |
| `KLAVIYO_PAGE_SIZE` | `100` (the API maximum for profiles) |

## Run it

```powershell
.\services\ingest\deploy_tcs_ingest.ps1 -Only tcs-klaviyo-profiles -Run
```
