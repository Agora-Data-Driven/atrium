# tcs_klaviyo_campaigns — campaign performance + attributed revenue (direct-API)

Pulls Klaviyo **campaign metadata** and **campaign-values-report statistics** into
`raw_windsor.tcs_klaviyo_campaigns`. This finishes the job the old notebook left at
`## [WIP] Email Campaigns`.

| | |
|---|---|
| Raw table | `agora-data-driven.raw_windsor.tcs_klaviyo_campaigns` (asia-southeast1) |
| Secret | `tcs-klaviyo-key` (read from Secret Manager via ADC) |
| Job | `tcs-klaviyo-campaigns-ingest`, daily at 02:35 Asia/Singapore |
| Grain | one row per `(campaign, campaign message)` **per pull** (append-only) |

## Why this exists when we already have per-send events

`tcs_klaviyo_events` can count opens and clicks, but it **cannot** produce Klaviyo's own
attributed revenue — `conversions` and `conversion_value`, computed against the *Placed Order*
metric. That is the only number that answers "what did this campaign earn?". The values report
also gives campaign-level deliverability (bounce / unsubscribe / spam) directly.

## Two API shapes, one row

1. **`/campaigns`** — paginated metadata: name, status, send time, audiences, subject, preview.
   ⚠️ This endpoint **rejects `page[size]`** (`'page_size' is not a valid field for the
   resource 'campaign'` → HTTP 400). Pagination is cursor-only via `links.next`.
2. **`/campaign-values-reports`** — a POST returning statistics for many campaigns at once,
   grouped by `campaign_message_id`. Heavily rate-limited, so it is called in batches of
   `STATS_BATCH` (50) with a `STATS_PAUSE_SEC` (32s) pause between them.

## `has_stats` — read this before charting revenue

The values report only accepts a **bounded timeframe** (`last_12_months`). Campaigns older
than that get their metadata row with **NULL statistics** rather than being dropped, because a
campaign we know about but cannot score is more useful than a silent absence.

`has_stats` distinguishes **"scored: all zeroes"** from **"outside the report window"**.
Without it, every pre-timeframe campaign reads as a campaign that earned nothing — which would
badly understate historical performance. Always filter on `has_stats` before averaging rates.

A failed stats batch is logged and skipped rather than aborting the run: those campaigns keep
NULL statistics for that cycle and are retried tomorrow. Losing one batch beats losing the
whole metadata refresh.

## Env

| Var | Default |
|---|---|
| `STATS_BATCH` | `50` campaigns per values-report request |
| `STATS_PAUSE_SEC` | `32` seconds between batches |
| `STATS_TIMEFRAME` | `last_12_months` |
| `RUN_BUDGET_SEC` | `3000` |

## Run it

```powershell
.\services\ingest\deploy_tcs_ingest.ps1 -Only tcs-klaviyo-campaigns -Run
```
