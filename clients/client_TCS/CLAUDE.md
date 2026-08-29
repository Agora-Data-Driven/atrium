# CLAUDE.md — clients/TCS (Business Quiz dashboard)

**Rules live in the repo-root [`/CLAUDE.md`](../../CLAUDE.md)** — read it first; this file only adds
local context. If they disagree, root wins.

TCS is a real client built on the `client_template` pattern (client key `tcs` — every resource name
derives from it). It differs from the template in ONE structural way:

**TCS ingests via DIRECT-API loaders, not Windsor** — **SEVEN** of them (`tcs_shopify`,
`tcs_klaviyo`, `tcs_quiz`, `tcs_sessions`, `tcs_klaviyo_profiles`, `tcs_klaviyo_campaigns`,
`tcs_affiliates` → `raw_windsor.tcs_*`). This is a *sanctioned, documented exception* to
"Windsor is the only ingest source": the Business-Quiz diagnostic needs per-recipient Klaviyo
open/click events, a grain Windsor does not serve. The pull logic is ported from
`archive_code/analytics.py` (the old Colab notebook, kept read-only for reference).

🔴 **Only three of the seven reach this dashboard.** `GATING_TABLES` is `tcs_quiz` +
`tcs_shopify_orders` + `tcs_klaviyo_events` + `perf_meta`; `tcs_sessions` /
`tcs_klaviyo_profiles` / `tcs_klaviyo_campaigns` / `tcs_affiliates` land raw data that no `sql/`
view reads yet. Running them will not refresh the dashboard, and their stalling will not stale it.
🔴 **They are deployed by `services/ingest/deploy_tcs_ingest.ps1`, never by
`tools/deploy_ingest_jobs.ps1`** — the shared `$JOBS` array has no `tcs-*` row, so
`-Only tcs-*` there matches nothing and exits 0 having deployed nothing.

**The data contract (matched BY NAME across three stages):**

```
sql/*.sql (view column) -> job/main.py (data dict key) -> dash/dashboard.html (data.* key)
```

- **`sql/`** — TWENTY views. Quiz side (`01_`–`14_`): quiz → conversion → engagement → monthly →
  cohort → kpi; leads keyed to their FIRST quiz submission (one row per email). Paid side
  (`15_`–`20_`, added 2026-07-30): `paid_media_daily` / `_ads` / `_campaigns` / `_kpis` / `_funnel`
  / `_funnel_stages`. Reapply with `create_views.py` (never the BQ console).
- **`job/`** — assembles `tcs.json` (`kpis` / `monthly` / `cohorts` / `leads` / **`paid`**);
  self-gates on the `raw_windsor.tcs_*` tables **and `raw_windsor.perf_meta`**. `freshness.py` is
  vendored identically.

## 🔴 TCS is the FIRST client on the canonical Windsor path (2026-07-30)

Paid media reaches this dashboard as **`raw_windsor.perf_meta` → a `WHERE client_slug = 'tcs'`
view → the export job**. There is **no Windsor API call anywhere in this client's stack**, unlike
the four legacy API-LIVE clients (RHE, riverdance, honeytribe, MeloYelo). Never add one here —
if paid data looks wrong, fix the shared loader (`services/ingest/meta/`) or the view, never this job.

- **`WHERE client_slug = 'tcs'` is the isolation boundary.** It is the only thing between this
  dashboard and twelve other accounts' spend in a shared table. Never relax it to an
  `account_name LIKE`.
- **Non-additive metrics are handled in SQL, and must stay that way**: `reach` is unique people
  (never SUM across days), `frequency` is carried impression-weighted, and every rate (CTR, CPC,
  CPL, ROAS) is recomputed from summed totals rather than averaged across rows — which is why the
  chart and the KPI tiles agree.
- **The funnel view is BLENDED, not attributed.** There is no click-id/UTM join between a Meta ad
  and a quiz submission, so `blended_cost_per_quiz_lead` / `_per_sale` divide *all* Meta spend by
  *all* quiz activity. Every column name and the on-screen copy say "blended"; Meta's own count sits
  beside it as `meta_reported_leads`. Do not quietly rename these to look attributed.
- **Two arithmetic guards, both load-bearing**: a month where ads ran <50% of the days is flagged
  `is_complete_month=false` and greyed (TCS's first month ran 2 days and printed a $2.48 blended CPL
  against a real ~$20-40); and an ad below the volume floor is marked **thin** (four clicks can post
  a $0.18 CPC and win the creative table on noise).
- **The KPI comparison windows anchor to the last day WITH DATA**, never `CURRENT_DATE()` — Windsor
  lands yesterday overnight, so anchoring on today would print a fake decline every morning.
- 🔴 **`objective LIKE '%LEAD%'` in EVERY paid_media_* view.** The tab is Lead Gen; TCS also runs
  OUTCOME_SALES campaigns, and folding their spend into a cost-per-lead makes the number
  meaningless. Filtering narrowed the tab from $13,529/653 leads/31 ads to **$9,375/538/11**, and
  CPL from $20.72 to $17.43. It is a `LIKE` because Meta renamed objectives in 2022-23
  (`LEAD_GENERATION` → `OUTCOME_LEADS`) and only those two contain "LEAD" — an exact match would
  silently drop a legacy lead campaign. **Change it in one view and the tab contradicts itself.**

### The Lead Gen tab's spine (reading order is the design)

Summary tiles → trend line → **where the funnel leaks** → **creative** → the reading.

- **The heatmap (`paid_media_funnel_stages`)** is reach → impressions → clicks → page views →
  leads, one row per ad. 🔴 **Shading is normalised PER COLUMN**, because a 1.8% CTR and a 15%
  lead rate cannot share a scale — a cell only ever means "better/worse than the other ads at
  *this* step". Thin ads are shaded but **excluded from each column's min/max**, so a four-click
  ad cannot define the scale everyone else is judged against. The panel also states its finding
  in words, ranked by *relative spread* rather than level (a low CTR is normal; a step where the
  ads wildly disagree is where the money is). On the real data it correctly names **page-view
  rate** — clicks that never become a page load, spanning 11%–96% across ads.
- 🔴 **`reach_daily_sum` is NOT unique people.** Meta dedupes reach only within a queried window
  and our grain is (ad × day), so someone reached on three days counts three times. There is no
  way to derive true multi-day unique reach from this table. The column is named, labelled and
  captioned as a daily sum — rates stay comparable between ads, which is what a heatmap is for.
- **The creative grid** prefers **`thumbnail_data`** — the export job's DURABLE copy of each
  creative. Meta's `thumbnail_url` is **signed and expires** (the whole grid went "Creative
  preview unavailable" in 2026-08 when ingest paused), so the job's `_attach_thumbs` downloads
  each image while its link is alive and embeds it as a small data URI in `tcs.json`, inheriting
  the previous export's capture for any URL that has already died — once captured, an image never
  rots. `thumbnail_url` stays as the fallback for pre-capture payloads, with the `onerror` handler
  swapping in a readable tile instead of a broken-image icon. 🔴 **`object-fit: contain`, never
  `cover`**: Meta serves square, portrait *and* wide banner creatives, and `cover` cropped the
  wide ones to a headless strip of lettering. The panel exists to judge the creative, so the whole
  frame must be visible.
- **`dash/`** — one self-contained `dashboard.html`, dark `--ag-*` theme, inline JS **esprima-4.x-safe**
  (no `?.` / `??`). The engagement chart deliberately avoids a dual axis: rates share one % axis,
  volume is a separate bar strip.

**Deploy (per stage, all idempotent):** `sql/deploy_views_tcs.ps1`, `job/deploy_job_tcs.ps1`,
`dash/deploy_dash_tcs.ps1`; full standup `deploy_tcs.ps1`; ingest via
`services/ingest/deploy_tcs_ingest.ps1 [-Only tcs-<key>]`. Use `FORCE_REBUILD=1` for
view/code/seed changes.
See [`README.md`](README.md) for the secret + quiz-sheet-sharing prerequisites.

## Dashboard standard (applied 2026-07-30)

This dashboard follows [`clients/_standard/STANDARD.md`](../_standard/STANDARD.md) — the **Leads**
layout over the shared shell. Client extras are untouched: the standard is a floor, never a ceiling.

It carries **one documented waiver** on `<body>`: `data-no-benchmark` (the comparison on the Quiz
tab is **cohort**-based — a lead is judged against the year they took the quiz, because a recent
cohort has simply had less time to buy; a free-floating second date range would let a reader compare
cohorts of different ages and read a decline that is only age).

⚠️ **`data-single-view` was REMOVED on 2026-07-30** when the Lead Gen tab landed: there are now two
genuinely different questions (*why did quiz conversion change?* / *what is the paid media buying
us?*), so a tab bar is navigation rather than chrome. Removing that waiver activates **R06
tabs-hash**, which is why the view switcher numbers its tabs (`.ix`) and writes state to the URL
hash via `history.replaceState` — so a `#paid` link opens straight onto Lead Gen. Two traps that
cost time here, both now commented in place: `setActive()` toggles class `active` while a `.seg`
button's selected state is class `on` (using the wrong one left the pill highlighting the wrong tab
on the hash-load path), and the paid chart must render **on tab show**, because an SVG laid out
inside a `hidden` container measures zero width and draws as a flat line at the origin.

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
