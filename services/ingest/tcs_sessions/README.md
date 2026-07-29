# tcs_sessions — Shopify storefront sessions (direct-API)

Pulls **storefront traffic** from the Shopify Admin GraphQL `shopifyqlQuery` endpoint
(`FROM sessions`) into `raw_windsor.tcs_shopify_sessions`.

This is the **denominator** the rest of the TCS stack was missing. Orders say who bought;
sessions say how many people showed up and where from — without it you cannot tell a
conversion-rate drop from a traffic-mix change.

| | |
|---|---|
| Raw table | `agora-data-driven.raw_windsor.tcs_shopify_sessions` (asia-southeast1) |
| Secret | `tcs-shopify-token` (read from Secret Manager via ADC) |
| Job | `tcs-sessions-ingest`, daily at 02:10 Asia/Singapore |
| Grain | one row per `(hour, referrer_source, referrer_name, referrer_url, session_city, landing_page_url)` |

## The 1000-row cap — the thing to know

`shopifyqlQuery` returns **at most 1000 rows and does not tell you it truncated**. A
month-wide query comes back with exactly 1000 rows and looks like a complete success.
Measured on this store:

| Window | Rows |
|---|---|
| 1 day | ~271 |
| 1 week | ~851 |
| 1 month | **1000 — silently cut** |

So the loader treats `rows >= 1000` as **proof of truncation** and recursively halves the
window until every piece is under the cap. Quiet history still costs one request per week;
only busy periods pay for the split. A single day that still hits the cap cannot be split
further by date — the loader keeps what it got and logs a loud WARNING rather than pretending
the day is complete.

## Checkpointing

Coverage is the **set of days already loaded**, read back from the table; the work list is the
set difference against `[BACKFILL_START, yesterday]`, newest-first. Interior holes therefore
cannot survive repeated runs. The trailing `RECHECK_DAYS` (default 3) are always re-pulled
because Shopify's session attribution keeps settling for a day or two; the staging view keeps
the newest `loaded_at` per key.

A day on which the store genuinely had **zero** sessions leaves no row and so is re-requested
every run. That is intentional — from the table's point of view "no rows" and "not loaded" are
identical, and one empty query is cheaper than silently treating a missing day as loaded.

## History

Shopify's sessions analytics only reaches back to **~2022-09** on this store (2021 and earlier
return nothing at all), which is why `BACKFILL_START=2022-09-01` rather than the store's 2017
founding.

## Env

| Var | Default | Meaning |
|---|---|---|
| `BACKFILL_START` | `2022-09-01` | floor date (not a window length) |
| `RECHECK_DAYS` | `3` | trailing days always re-pulled |
| `RUN_BUDGET_SEC` | `3000` | soft wall-clock budget; the rest resumes next tick |
| `SHOPIFY_SESSIONS_API_VERSION` | `2025-10` | both 2025-10 and 2024-01 serve this identically |

## Run it

```powershell
.\services\ingest\deploy_tcs_ingest.ps1 -Only tcs-sessions -Run
```
