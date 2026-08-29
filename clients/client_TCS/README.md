# TCS — Business Quiz dashboard (Agora Atrium client)

TCS (Contract Shop) rebuilt on the Atrium three-stage contract. This first dashboard is
**diagnostic**: understand the behaviour of quiz-takers — especially those who bought — and
explain why this year's leads aren't converting (**are they opening less? clicking less?**).

The old pipeline (a Colab notebook, preserved read-only in [`archive_code/analytics.py`](archive_code/analytics.py))
pulled everything by direct API into a personal BigQuery project. This rebuild keeps the proven
pull logic but lands it on the shared platform and models it as views → export job → dashboard.

## Data source — a documented exception to "Windsor is the only source"

TCS's sources do not flow through Windsor, and the diagnostic needs per-recipient open/click
events — a grain Windsor's Klaviyo connector does not serve. So TCS uses **direct-API ingest
loaders** that write the shared raw layer, `raw_windsor.tcs_*`.

**There are SEVEN loaders**, all deployed and scheduled by
[`services/ingest/deploy_tcs_ingest.ps1`](../../services/ingest/deploy_tcs_ingest.ps1) — which is
that array's single source of truth, not `tools/deploy_ingest_jobs.ps1`:

| loader | pulls | raw table |
|---|---|---|
| [`tcs_shopify`](../../services/ingest/tcs_shopify) | orders + marketing attribution | `raw_windsor.tcs_shopify_orders` |
| [`tcs_klaviyo`](../../services/ingest/tcs_klaviyo) | **per-recipient** email events | `raw_windsor.tcs_klaviyo_events` |
| [`tcs_quiz`](../../services/ingest/tcs_quiz) | Business-Quiz submissions | `raw_windsor.tcs_quiz` |
| [`tcs_sessions`](../../services/ingest/tcs_sessions) | storefront traffic (ShopifyQL) | `raw_windsor.tcs_shopify_sessions` |
| [`tcs_klaviyo_profiles`](../../services/ingest/tcs_klaviyo_profiles) | people + CLV / churn scores | `raw_windsor.tcs_klaviyo_profiles` |
| [`tcs_klaviyo_campaigns`](../../services/ingest/tcs_klaviyo_campaigns) | campaign stats + attributed revenue | `raw_windsor.tcs_klaviyo_campaigns` |
| [`tcs_affiliates`](../../services/ingest/tcs_affiliates) | Tapfiliate roster + conversions | `raw_windsor.tcs_affiliate*` |

⚠️ **Only the first three feed this dashboard.** `job/main.py`'s `GATING_TABLES` are `tcs_quiz`,
`tcs_shopify_orders`, `tcs_klaviyo_events` and `perf_meta` — the other four loaders land raw data
that no `sql/` view reads yet (they are groundwork for the Orders / Sessions / affiliate standups
under [Later](#later)). A stalled `tcs-sessions` therefore does **not** stale this dashboard, and
running it will **not** refresh it either.

## The three-stage contract (matched BY NAME)

```
services/ingest/tcs_*  ->  raw_windsor.tcs_*   (direct-API mirrors)
        |
   sql/*.sql (view column)  ->  job/main.py (data dict key)  ->  dash/dashboard.html (data.* key)
```

### Stage 1 — SQL views (`sql/`, applied in NN_ order by `create_views.py`)

| view | what it is |
|------|-----------|
| `01_stg_quiz`          | one row per LEAD (email), first quiz submission + cohort fields |
| `02_stg_orders`        | typed Shopify orders, keyed on buyer email |
| `03_stg_email_events`  | per-recipient Klaviyo sends, flagged is_open / is_click |
| `04_quiz_conversion`   | per lead: orders at/after quiz (5-min buffer), revenue, days-to-buy |
| `05_quiz_engagement`   | per lead: post-quiz sends/opens/clicks + rates |
| `06_quiz_leads`        | the FACT view (one row per lead) = old DASHBOARD_quiz_nurture_analysis |
| `07_engagement_monthly`| **the diagnostic time series**: open/click rate by month, split by converted |
| `08_cohort_performance`| per quiz-cohort year: conversion + engagement |
| `09_kpi_overview`      | single-row headline KPIs (incl. this-year-vs-prior open/click rate) |
| `10_conversion_trend`  | conversion rate by quiz-cohort MONTH beside that cohort's engagement |
| `11_activity_monthly`  | monthly leads / sales / click rate, all scoped to the quiz-lead cohort |
| `12_activity_weekly`   | weekly companion to `11_` (Monday weeks, last 52, complete periods only) |
| `13_lead_campaigns`    | per email SUBJECT sent to leads: reach + click rate (drops < 10 sends) |
| `14_lead_emails`       | one row per (lead, email): send date, subject, opened, clicked |

**Paid side (`15_`–`20_`, added 2026-07-30)** — sourced from `raw_windsor.perf_meta`, *not* from a
`tcs_*` loader. 🔴 `WHERE client_slug = 'tcs'` in every one of these is the isolation boundary
against twelve other accounts' spend in that shared table, and `objective LIKE '%LEAD%'` keeps
OUTCOME_SALES spend out of every cost-per-lead. See [`CLAUDE.md`](CLAUDE.md) before touching them.

| view | what it is |
|------|-----------|
| `15_paid_media_daily`         | spend/impressions/clicks by day (the isolation-boundary view) |
| `16_paid_media_ads`           | one row per AD over its flight — the creative table |
| `17_paid_media_campaigns`     | one row per CAMPAIGN, objective carried through |
| `18_paid_media_kpis`          | one row: headline tiles + last-30 vs prior-30 comparison |
| `19_paid_media_funnel`        | Meta spend beside the quiz funnel it buys, by month (**blended**) |
| `20_paid_media_funnel_stages` | reach → leads step-through rates PER AD — the heatmap |

> Grain note: leads are keyed to their FIRST quiz submission (unlike the old per-submission
> model), so repeat submitters don't double-count engagement.

### Stage 2 — export job (`job/main.py`)

Reads the views and assembles `tcs.json` (`kpis`, `monthly`, `cohorts`, `leads`, **`paid`**),
uploaded to the private bucket `agora-data-driven-tcs-dash`. Self-gates on `_freshness.json` vs the
GATING_TABLES (`tcs_quiz`, `tcs_shopify_orders`, `tcs_klaviyo_events`, **`perf_meta`**);
`FORCE_REBUILD=1` bypasses for view/code changes. Cloud Scheduler fires `tcs-export-daily`
**every 10 minutes** ([`scheduler.ps1`](scheduler.ps1)) and the gate decides whether there is
anything to do — so a stale dashboard means a stalled *loader*, not a missed export.

The job also **captures creative thumbnails durably** (`_attach_thumbs` → `thumbnail_data`),
because Meta's `thumbnail_url` is signed and expires. See [`CLAUDE.md`](CLAUDE.md).

### Stage 3 — dashboard (`dash/dashboard.html`)

Self-contained, dark `--ag-*` theme, esprima-4.x-safe inline JS (no `?.` / `??`). **TWO tabs** since
2026-07-30, switched by `#seg-view` and reflected into the URL hash (so `#paid` deep-links):

- **Quiz** — KPI strip + this-year callout; **open/click-rate-over-time** (two lines, one % axis)
  with a volume bar strip below; **buyers vs non-buyers** open-rate lines; cohort table; leads
  drill-down; a "Reading of the diagnostics" strip.
- **Lead Gen** — summary tiles → trend line → the funnel-leak heatmap → the creative grid → the
  reading. Meta paid media only, `objective LIKE '%LEAD%'`.

Conforms to [`clients/_standard/STANDARD.md`](../_standard/STANDARD.md) with one waiver
(`data-no-benchmark`). Three gates must pass before any dash deploy — see [`CLAUDE.md`](CLAUDE.md).

## Audit notes (informational — this client is mid-build, owned by another dev)

*Raised 2026-07-29; **all three re-verified against the code on 2026-08-26** and still open. Line
numbers below are refreshed to the current files — the originals had drifted by ~250 lines.*

- **`data.conversion_trend` and `data.monthly` are DEAD on the render path** — each costs its own
  BigQuery query per job run (`job/main.py` `_read_conversion_trend`:103 / `_read_monthly`:128,
  assigned at :554/:560-561) and neither key is read anywhere in `dash/dashboard.html` (still zero
  matches). Before deleting: `_data_through()` (`job/main.py`:531-536) falls back to
  `monthly[-1]["month"]`, and both arrays appear in the completion log line (:592-593) — rehome
  those two uses first.
- **Near-miss hazard:** the dashboard's `ACT.monthly` variable is fed from **`data.activity_monthly`**,
  NOT `data.monthly` — a casual grep for "monthly" makes the dead key look consumed. They are
  different views (`engagement_monthly` vs `activity_monthly`).
- **`lead_emails` is a POSITIONAL contract:** the job appends compact arrays
  `[sent_at, subject, is_open, is_click]` (`job/main.py`:283-288, off the `SELECT` at :277) and the
  dashboard maps positions to `{d, s, o, c}` (`dashboard.html`:1221) read by `EM.leadCols`
  (`dashboard.html`:1140). Reordering either end silently mislabels the Opened/Clicked pills;
  nothing asserts the order anywhere.

## Prerequisites before deploy

1. **Secrets:** run [`services/ingest/tcs_provision_secrets.ps1`](../../services/ingest/tcs_provision_secrets.ps1)
   to create `tcs-shopify-token` + `tcs-klaviyo-key` (+ `tcs-tapfiliate-key`) and grant
   `ingest-runner@` access.
2. **Quiz sheet:** enable Sheets + Drive APIs and share the Business-Quiz sheet (Viewer) with
   `ingest-runner@agora-data-driven.iam.gserviceaccount.com`.
3. **Ingest jobs need no registration.** 🔴 The TCS loaders are **not** in
   `tools/deploy_ingest_jobs.ps1`'s `$JOBS` and must never be added there — they are direct-API,
   not Windsor, and [`services/ingest/deploy_tcs_ingest.ps1`](../../services/ingest/deploy_tcs_ingest.ps1)
   owns all seven. `tools\deploy_ingest_jobs.ps1 -Only tcs-shopify` matches no row, so it exits
   cleanly having deployed **nothing** — which reads exactly like a successful run.

## Deploy (manual, run-as-yourself)

```powershell
# 0. secrets + sheet share (once)               -> services/ingest/tcs_provision_secrets.ps1
# 1. land the raw layer (all seven loaders, or -Only <key>)
.\services\ingest\deploy_tcs_ingest.ps1 -Run
.\services\ingest\deploy_tcs_ingest.ps1 -Only tcs-klaviyo -Run
# 2. full client standup (dataset, bucket, SAs, secrets, views, job, scheduler, dash)
.\clients\client_TCS\deploy_tcs.ps1
# per-stage iteration afterwards:
.\clients\client_TCS\sql\deploy_views_tcs.ps1     # views changed  (FORCE_REBUILD)
.\clients\client_TCS\job\deploy_job_tcs.ps1       # data logic changed
.\clients\client_TCS\dash\deploy_dash_tcs.ps1     # dashboard changed (runs the JS gate first)
```

All paths are `clients\client_TCS\` — there is no `clients\TCS\` directory (the docs said so until
2026-08-26). Every script is run **from the repo root**, as `info@agoradatadriven.com`.

⚠️ **`tcs.agoradatadriven.com` is NOT mapped** (DNS does not resolve as of 2026-08-26). The
dashboard is reached at the Cloud Run URL of the `tcs-dash` service in **`asia-southeast1`**; SSO
is inert on a `*.run.app` host by design (see [`dash/platform_sso.py`](dash/platform_sso.py)), so
that URL falls back to the `tcs-dash-password` secret. To finish the mapping, point the domain at
`tcs-dash` and wire SSO with `tools\enable_platform_sso.ps1 -Keys tcs`.

## Later

Orders / Sessions / full-email dashboards from the old notebook are a later standup on this same
`tcs` client (more views + more `data.*` keys + more panels — same contract).
