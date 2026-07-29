# `honeytribe` — Honey Tribe dashboard

Honey Tribe is a US women's fashion brand (African prints — *"Bold prints. Modern cuts."*) selling
on **Shopify** and advertising on **Meta**. This directory replaces the client's Looker Studio
report + Google Sheet + Colab pipeline with one self-hosted dashboard on GCP.

**Windsor/Shopify-LIVE client** — like [`client_riverdance`](../client_riverdance/) and *unlike* the
BigQuery-fed [`client_template`](../client_template/): there is **no dataset and no SQL views**. The
export job pulls every API directly each run and writes the private `honeytribe.json` the gated
dash service serves.

```
Meta ads, per ad per day     -> Windsor.ai `all`                   \
Meta age x gender, per day   -> Windsor.ai + breakdown fields       \
Meta region, per month       -> Windsor.ai + breakdown fields        >- job/main.py -> honeytribe.json -> dashboard.html
Shopify orders + line items  -> Shopify Admin REST (paginated)      /
Shopify sessions by referrer -> Shopify Admin GraphQL / ShopifyQL  /
```

**Live as of 2026-07-27:** 9,222 orders (2019-05-24 → 2026-07-26), 12,160 line items, 2,256 Meta
ad-days (2023-09-30 →), 5,517 age×gender rows, 681 region rows, 580 session-months. ~4.4 MB,
~80 s to build.

---

## The three tabs

They mirror the client's Looker report page for page, then go further.

**01 · Sales Overview** — the weekly stand-up view.

Six scorecards — Sales · Customers · AOV · First-time · Returning · Units — that double as
**series toggles**: click one to add or remove its line from the *Metrics over time* chart directly
beneath it, and each card's accent bar and dot **is** that line's colour. The chart uses the
resetdata axis model:

- **Relative** indexes every line to its own peak on a shared 0–100% axis, so metrics on wildly
  different scales (sales in $M against a few thousand customers) can be compared by *shape*.
- **Absolute** shows real values with a dedicated axis per family — counts on the left, money
  **totals** on the right (long dashes), **per-order** rates on a second right axis (dotted).
  AOV needs that third axis: on the totals axis a ~$230 average order is a flat line under a
  ~$5K sales peak.

Tooltips always give the real value, plus "% of peak" in relative mode. Grain is Auto / Week /
Month. Below the chart: the **Weekly** and **Month-to-date** cards (*actual vs. the benchmark
average*), a clickable US **tile map**, best sellers, new-vs-returning by month, and a weekly
customers/AOV/sales trend. Filters: date range, benchmark range, customer type, product, state.

**02 · Shopify × Meta** — the funnel. **Awareness / Consideration / Conversion / Investment &
efficiency**, each as *Current* beside the *per-week KPI average* with a delta. Plus an
impression→order funnel, revenue-vs-spend, ROAS/CPM, frequency (with a 2.5 fatigue line) and a
campaign table showing the **top 10** by whichever column you sort on — click a row to cross-filter
the whole tab. The footer always reports totals across *all* campaigns, not just the ten shown.

**03 · Product & Audience** — the seasonality planner. Opens with **Where our customers come from**:
sessions (visitors) beside orders (buyers) per platform with a conversion rate on each row, or the
two as trend lines over time. Then Meta **age × gender** and ad reach by state (metric switchable
between reach / impressions / link clicks / spend); category donut and category-by-month stacked
bars (both click to cross-filter); **best sellers by month** with a *By month* view whose accordion
lists **every** product sold in that month, not just the top ranked; the US seasonal retail guide;
sizes bought, order-value bands, day-of-week, and units by state × month. A **Source** toggle
switches between the full order history and the client's curated trend sheet.

Every tab ends with a **"Reading of the…"** strip — plain-language findings that recompute with the
filters. The tab is in the URL hash (`#funnel`), so a view can be linked to.

### Metric definitions (reverse-engineered from the client's Looker report, verified against it)

| Metric | Definition |
|---|---|
| Sales | `SUM(total_price)` — matches Looker's `$1.18M` |
| CTR | link clicks ÷ impressions |
| CPM | spend ÷ impressions × 1000 |
| Conversion rate | purchases ÷ link clicks |
| ROAS | attributed purchase value ÷ spend |
| Frequency | mean of Meta's own per-row `frequency` (reach is **not** additive across days, so impressions ÷ reach would understate it) |
| AOV | sales ÷ **customers** (see below) · `Per order` is sales ÷ orders |
| KPI / benchmark column | the **per-week** (or per-month) average across the benchmark range |

The **reporting period** and the **KPI benchmark** are two independent date ranges, each with its
own presets *and* its own from/to pickers. The period drives every tile, chart and table; the
benchmark drives only the Avg-per-week / Avg-per-month / KPI-avg columns. The benchmark control is
tinted clay end-to-end so the two are never confused, and the line under the controls spells out
which is which.

The benchmark defaults to **Last year** — the whole of the previous calendar year (currently
2025-01-01 → 2025-12-31) on both tabs. A complete calendar year is seasonally balanced, which a
rolling 12 months is not: for a clothing brand whose demand swings with the US retail seasons, a
rolling window silently over-weights whichever season it happens to start in.

**AOV is sales ÷ customers** (confirmed by the client 2026-07-27): the metric exists to compare what
a first-timer spends against what a returning customer spends, so the denominator has to be people,
not orders. Sales ÷ orders is still shown as `Per order` under the Units tile. The **First-time**
and **Returning** cards each carry that type's spend per customer — currently **$139** against
**$342**, i.e. a returning customer is worth ~2.5× a new one.

---

## Local preview

```powershell
$env:WINDSOR_API_KEY="…"; $env:SHOPIFY_ACCESS_TOKEN="…"       # rotated keys
$env:HONEYTRIBE_LOCAL_OUT="clients\client_honeytribe\data\honeytribe.json"
python clients\client_honeytribe\job\main.py                  # the REAL pull, ~80 s
python clients\client_honeytribe\preview_local.py 8137        # -> http://localhost:8137
```

or double-click **`preview\Preview Honey Tribe dashboard.cmd`** once the data file exists.

`build_local.py` is the **no-credentials fallback**: it reads the client's context workbook and
emits the same JSON shape, minus what only an API can give (referrer attribution, sessions, Meta
demographics — those render their wired-empty states). `job/main.py` is the real thing and is what
production runs.

### Freshness and the Sync button

The header shows **Updated *n* min ago** and **Data through *date***, and turns the date red when
the feed is more than 2 days behind. **Sync** re-pulls: it POSTs `/refresh` (which triggers the
`honeytribe-export` Cloud Run job when `REFRESH_JOB` is set) then reloads `data.json`, with a
spinner throughout. Unconfigured, it degrades to a plain reload — the button always does something
useful and needs no extra IAM by default.

**Sync is the ONLY refresh path** — there is no Cloud Scheduler (removed 2026-07-29 at the
client's request; each run costs paid Windsor/Meta and Shopify calls). Repeat clicks are held off
by a 10-minute cooldown keyed on the data object's age, so it is shared across instances.

⚠️ Wiring `/refresh` needs the web SA to hold **`roles/run.developer`** on the export job.
`roles/run.invoker` does *not* carry `run.jobs.runWithOverrides` — the exact trap that left
riverdance 13 days stale (see the root `CLAUDE.md`). With no scheduler as a backstop, this grant
is load-bearing: lose it and the dashboard silently stops updating altogether.

---

## ⚠️ `context/` is git-ignored — it holds live credentials

The context drop contains a **live Shopify Admin token** (`shpat_…`, in the notebook — which even
says *"Recommendation: Rotate this token!"*), a **live Windsor API key** (in the connector URL in the
docx and in the workbook's `Queries` sheet), and **customer PII** (emails, names, addresses, phones).
[`.gitignore`](.gitignore) keeps the whole folder out of git. **Rotate both keys** before standing
this up, and put the new ones in Secret Manager — never in a file in this repo.

Nothing in the emitted JSON carries PII: customers are reduced to a salted 12-char hash used only to
count distinct people and to mark an order first-time vs returning.

---

## Layout

```
clients/client_honeytribe/
  README.md · CLAUDE.md · .gitignore
  preview_local.py           local server: dashboard.html at / and honeytribe.json at /data.json
  context/                   the client's source material (GIT-IGNORED — live keys + PII)
  data/                      locally built honeytribe.json (git-ignored)
  job/                       Stage 2
    main.py                  LIVE pull: Windsor (Meta) + Shopify Admin + ShopifyQL sessions
    build_local.py           off-cloud stand-in: context workbook -> the same JSON
    categorize.py            the shared product-title -> category classifier
    assets/                  honeytribe-mark.png · agora.png · categories.json (frozen taxonomy)
  dash/                      Stage 3
    dashboard.html           one self-contained file, esprima-4.x-safe inline JS
    main.py                  auth + /data.json proxy   platform_sso.py   Dockerfile
```

### The category taxonomy

The client's taxonomy lives only in their curated trend sheet, which names ~240 of the ~580 titles in
the order history. Its labels are driven by the garment-type word in the title, so
[`job/categorize.py`](job/categorize.py) **learns** that rule (exact match → a type token that is in
≥2 titles *and* ≥60% pure → a hand-written fallback list) and freezes it into
`job/assets/categories.json`, which the live job then applies without needing the sheet. Purity
matters: *denim* spans jackets, skirts and palazzos, so it is rejected as a predictor and falls
through to the fallback.

That takes uncategorised units from **72.6% → 18.2%**. The remainder are one-word product names with
no garment type at all (`SAPPHIRE`, `Lazuli`, `Adaugo`, `Gia`, `HOLLY`) — genuinely unclassifiable
without a product catalogue export. **Ask the client for a Shopify product export with
product_type/tags** and this drops to ~0.

---

## Data audit (2026-07-27)

Every check is recomputed independently of the dashboard and cross-checked against the Shopify API.
**All pass.** Two real bugs were found and fixed by it:

1. **Sessions were silently garbage.** ShopifyQL returns each row as an *object* keyed by column
   name, but the reader zipped it against the column list — pairing column names with column names
   and yielding 580 rows of literal header text. Fixed in `_shopifyql`, which now handles both the
   object and positional-array shapes.
2. **Self-referrals were counted as traffic.** This store also sells on `shopmidgetgiraffe.com`, and
   the hyphen in its `midget-giraffe` myshopify handle defeated the substring match — so **1,412
   orders and 5,381 sessions** were credited to an "external" source. Fixed by normalising
   punctuation out of both sides and reading the real storefront domains from `shop.json` at
   runtime. Orders and sessions now share **one** vocabulary (`normalize_platform`), which is what
   makes the visitors-vs-buyers chart legitimate.

| Check | Result |
|---|---|
| Freshness | data through **2026-07-26**, 1 day behind — PASS |
| Order count vs Shopify API | API 9,333 total; ours 9,222; difference 111 = cancelled (51) + refunded (60) — PASS |
| Single-day spot check | 2026-07-20: API 3, ours 3 — PASS |
| Line → order integrity | 0 bad indexes, 0 date/type mismatches — PASS |
| First-time / returning derivation | 0 customers misclassified — PASS |
| Funnel monotonicity | impressions ≥ link clicks ≥ purchases — PASS |
| Trend units == line units | 12,217 == 12,217 — PASS |
| Platform vocabulary shared | 8 labels common to orders and sessions — PASS |
| **PII leak** | 0 emails, no tokens, no phones, ids are 12-char hashes — PASS |

Known, accepted quirks: 107 orders (1.2%) carry no customer id and cannot be resolved to a person;
12 Meta rows show revenue with no purchase in the same row (an attribution-window artefact);
Shopify sessions only exist from **2024-01**, so the traffic panel clips its window and says so on
screen.

## Live

| | |
|---|---|
| Dashboard | **https://honeytribe-dash-585951669065.asia-southeast1.run.app** — no login (`DASH_OPEN=1`) |
| Service | `honeytribe-dash` (asia-southeast1, `--no-invoker-iam-check`, password + SSO-ready) |
| Export job | `honeytribe-export` — full backfill **133 s**, incremental run **~25 s** |
| Refresh | **In-page Sync button only — no scheduler** (removed 2026-07-29; 10-min cooldown). `deploy_honeytribe.ps1 -WithScheduler` adds a 6-hourly tick back |
| Data | private `gs://agora-data-driven-honeytribe-dash/honeytribe.json`, served only via the authed `/data.json` proxy |

**Access is OPEN** — no password, at the operator's request (2026-07-27), because the dashboard is
embedded in the gated Atrium workspace where a login prompt inside the iframe is a dead end. The
BUCKET stays private in every posture; `/data.json` is only ever reachable through this service.
⚠️ Anyone holding the Cloud Run URL can read the data — the URL is unguessable, not secret. To
re-gate, set `DASH_OPEN=0`: the password secret, `/login` and the session cookie are all still
wired, so it is one environment variable and no code change.

Because `/refresh` is now un-gated too, it carries a **cooldown** (`REFRESH_COOLDOWN_SECONDS`,
default 600) keyed on the age of the data object — a repeat click reports "refreshed N min ago"
instead of firing another paid Windsor/Shopify pull.

Verified end to end after deploy: `/` → 200 with no cookies, `/data.json` → 200 (4.9 MB),
`POST /refresh` → `{"ok":true}` and the triggered execution succeeded and republished the JSON,
and the scorecard toggles + both axis modes work on the live service with no JS errors.
Re-verified 2026-07-29 after the scheduler was removed, since Sync became the only refresh path.

### Two traps that already bit this client

- **IPv6.** Cloud Run has no IPv6 egress route, but Shopify and Windsor publish AAAA records, and
  `socket.create_connection` applies the full timeout to *each* address in turn. Every request
  burned its entire 120 s connect timeout on the v6 address before falling back to v4 — 120 s per
  call, which turned the 38-page backfill into a 76-minute job and blew the 30-minute task timeout.
  `FORCE_IPV4` in `job/main.py` filters resolution to A records; the same backfill is now **133 s**.
- **Full crawls don't scale.** The Shopify pull is now **incremental** — `updated_at_min` over the
  last 30 days, merged by Shopify order id into the previous publication (`SHOPIFY_FULL_SYNC=1`
  forces a backfill). Orders and lines both carry `oid` for that merge. First-time/returning and
  the line→order index are still recomputed across the whole merged set, so a returning customer
  whose first order predates the window is never misclassified.

## Windows and limits worth knowing

- **Windsor caps a wide-field pull at ~12 months** — the full field list 400s at `last_730d`, and
  `date_from`/`date_to` and `maximum` are rejected outright. So each run fetches 365 days and
  `merge_history()` carries older rows forward, keyed by (date, campaign, adset, ad). History
  **accumulates in the bucket** instead of being capped, and the fresh pull stays authoritative for
  every day it covers, so restatements land correctly.
- **Meta breakdowns are separate pulls** — Meta will not return age/gender/region alongside the
  per-ad/day rows and rejects revenue on a breakdown query, so those carry delivery metrics only
  (spend / impressions / clicks / link clicks / reach). They use a 365-day window
  (`WINDSOR_BREAKDOWN_PRESET`) because age×gender is per-day.
- **Shopify sessions are month-granular**, so the traffic panel filters by month.
- **Buyer attribution** comes from each order's `referring_site`, overridden by `utm_source` on the
  landing URL when present (that is the paid-campaign truth).

---

## Deploy

```powershell
.\clients\client_honeytribe\deploy_honeytribe.ps1 -Password "<client pw>" `
    -WindsorKey "<rotated key>" -ShopifyToken "<rotated token>"
```

One-shot and idempotent: APIs → Artifact Registry + private bucket → job/web service accounts +
IAM → password / session / Windsor / Shopify secrets → export job (build, deploy, first run) →
`run.developer` for the portal SA **and** the web SA so the dashboard's Sync button can trigger it
(and it REMOVES any Cloud Scheduler unless you pass `-WithScheduler`) → the dash service (`--no-invoker-iam-check`, app-level
auth). No dataset, no SQL views. Re-running it rotates the secrets to whatever you pass in.

`-SkipJobRun` deploys without executing the export job (that run pages through ~9,300 Shopify
orders and takes ~90 s).

**After the standup, rotate the keys** — the ones in `context/` have been in a shared docx and a
shared notebook. Re-run the script with the new values; nothing else changes.
