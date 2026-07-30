# CLAUDE.md — clients/TCS (Business Quiz dashboard)

**Rules live in the repo-root [`/CLAUDE.md`](../../CLAUDE.md)** — read it first; this file only adds
local context. If they disagree, root wins.

TCS is a real client built on the `client_template` pattern (client key `tcs` — every resource name
derives from it). It differs from the template in ONE structural way:

**TCS ingests via DIRECT-API loaders, not Windsor** (`services/ingest/tcs_shopify` /
`tcs_klaviyo` / `tcs_quiz` → `raw_windsor.tcs_*`). This is a *sanctioned, documented exception* to
"Windsor is the only ingest source": the Business-Quiz diagnostic needs per-recipient Klaviyo
open/click events, a grain Windsor does not serve. The pull logic is ported from
`archive_code/analytics.py` (the old Colab notebook, kept read-only for reference).

**The data contract (matched BY NAME across three stages):**

```
sql/*.sql (view column) -> job/main.py (data dict key) -> dash/dashboard.html (data.* key)
```

- **`sql/`** — NINE views (not the template's three): quiz → conversion → engagement → monthly →
  cohort → kpi. Leads are keyed to their FIRST quiz submission (one row per email). Reapply with
  `create_views.py` (never the BQ console).
- **`job/`** — assembles `tcs.json` (`kpis` / `monthly` / `cohorts` / `leads`); self-gates on the
  `raw_windsor.tcs_*` tables. `freshness.py` is vendored identically.
- **`dash/`** — one self-contained `dashboard.html`, dark `--ag-*` theme, inline JS **esprima-4.x-safe**
  (no `?.` / `??`). The engagement chart deliberately avoids a dual axis: rates share one % axis,
  volume is a separate bar strip.

**Deploy (per stage, all idempotent):** `sql/deploy_views_tcs.ps1`, `job/deploy_job_tcs.ps1`,
`dash/deploy_dash_tcs.ps1`; full standup `deploy_tcs.ps1`; ingest via
`tools/deploy_ingest_jobs.ps1 -Only tcs-*`. Use `FORCE_REBUILD=1` for view/code/seed changes.
See [`README.md`](README.md) for the secret + quiz-sheet-sharing prerequisites.

## Dashboard standard (applied 2026-07-30)

This dashboard follows [`clients/_standard/STANDARD.md`](../_standard/STANDARD.md) — the **Leads**
layout over the shared shell. Client extras are untouched: the standard is a floor, never a ceiling.

It carries **two documented waivers** on `<body>`: `data-single-view` (one diagnostic question, so a
one-tab bar would be chrome rather than navigation) and `data-no-benchmark` (the comparison here is
**cohort**-based — a lead is judged against the year they took the quiz, because a recent cohort has
simply had less time to buy; a free-floating second date range would let a reader compare cohorts of
different ages and read a decline that is only age).

What changed on 2026-07-30 — this dashboard had the least of the standard of the eight:
- **Header freshness (`#updated` / `#thru`, red past three days) and a Sync button.** There was no
  generated-at stamp at all. ⚠️ **This service has no `/refresh` route**, so Sync degrades to
  re-fetching `/data.json` — which is the documented behaviour, not a bug.
- **A tuckable control bar with a real Period**, plus a grain control carrying **Auto**. The old
  Monthly/Weekly toggle inside the chart panel is gone; the control bar's `#seg-grain` replaced it.
  Auto picks between **week and month only** — the payload has no daily grain, and offering Day
  would fabricate precision the data cannot support.
- **The period is the OUTER filter on the lead list**; the existing year/month selects narrow
  inside it. Buckets outside the period are dropped from the trend series rather than zeroed,
  because a month with no quiz activity is a gap in the FEED here, not a quiet month.
- **A "Reading of the diagnostics" strip.** One card is load-bearing: it states plainly when leads
  in the period have not bought, which is the finding the whole page exists to explain. Open rate is
  deliberately excluded from every card — Apple Mail Privacy auto-opens make it unusable, and a rate
  that lands on exactly 100% is a misclassification, not a triumph.
- `#status`/`#content` became `#boot`/`#app`; a shared `#tip` was added beside the chart's own
  `#actTip`.

**`clients/_standard/dash/_conform.css` is VENDORED into this file** between sentinel comments (the
print + reduced-motion + screen-reader block). Never edit inside the sentinels — re-sync with
`py -3 clients/_standard/vendor_lib.py`. Both gates before any dash deploy:

```powershell
py -3 tools\_validate_dash_js.py       clients\client_TCS\dash\dashboard.html
py -3 clients\_standard\check_standard.py clients\client_TCS\dash\dashboard.html
py -3 clients\_standard\vendor_lib.py --check
```
