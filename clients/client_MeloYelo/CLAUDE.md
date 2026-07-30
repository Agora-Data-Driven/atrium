# CLAUDE.md — clients/client_MeloYelo (Unleashed + CRM + Lark, honeytribe pattern)

**Rules live in the repo-root [`/CLAUDE.md`](../../CLAUDE.md)** — read it first; this file only
adds local context. If they disagree, root wins.

MeloYelo has TWO Stage-2 builders that must emit the SAME `data/meloyelo.json` shape —
**`job/main.py` (the real thing: Unleashed + Campaign Monitor LIVE; CRM + Lark from the
freshest context snapshot)** and `job/build_local.py` (zero-credential fallback, context
workbooks only). `main.py` imports its shared transforms FROM `build_local.py` — extend there
first. Two-stage contract:

```
job/main.py | job/build_local.py (data dict key)  ->  dash/dashboard.html (DATA.* key)
```

The local preview's Sync button is LIVE: `preview_local.py` handles `POST /refresh` by running
`job/main.py` (5-min cooldown via the data file's mtime). Credentials resolve env-vars-first,
then fall back to parsing the notebooks in `context/` — never print or commit them. The CRM
Google Sheet is private (anon export 401s) and the Lark refresh token is dead (single-use,
consumed) — that is WHY those two sources read from the snapshot; don't chase it again.

- **Bike counts = product groups `Bikes` + `Special Bike Package`** (`BIKE_GROUPS`, defined in
  BOTH files). Only this set reproduces the client's numbers (FYTD 157 AND prior-FYTD 187).
  Never count bikes off `Bikes` alone.
- **The lead-gen pipeline replicates the client's Colab cell 17** (in
  `context/Campaign_Monitor_Full_Email_Extract (3).ipynb`): `Warranty Registration` is excluded
  from every lead metric, requests dedupe by Unique ID keeping the earliest date, stage events =
  Stage history + an All-data fallback per (id, stage), counts are COUNT-DISTINCT ids. The
  workbook's `Lead Gen QA Summary` tab is the validation oracle — `build_local.py` prints the
  matching counts at the end of every build.
- **Riders** = CRM stage `MY Customer`; joined = first transition into it, else CRM entry date.
  "New riders" tiles use the warranty-EXCLUDED `MY Customer` metric — bulk warranty imports are
  riders but never "new joins" (the Looker chart this fixes was plotting riders against
  customer surnames).
- **The funnel is a cohort funnel** (requesters in window × furthest stage reached to date),
  NOT per-stage event counts — stage-history events are sparse and non-monotonic.
- **Speed to lead renders as `2D 12H 10M`** (`fmtDHM`) — the client asked for this format
  explicitly. Median headline; mean only in tooltips.
- **NZ FY starts 1 April; the FY label is the END year** (Apr 2026 → FY2027). `fyStartOf`/
  `fy()` exist in both files — keep them in step.
- **Postcodes arrive Excel-mangled** (`0981` → `981.0`); `region_of()` strips the float tail and
  re-pads to 4 digits, then falls back to a city-name lookup. Don't "simplify" it back to a
  digit-strip — that misfiled Northland riders into Southland.
- **`dash/dashboard.html`** is one self-contained file; inline JS must be **esprima-4.x-safe**
  (no `?.`, no `??`). Four tabs: `sales` · `riders` · `inventory` · `marketing`, in the URL hash.
  Chart palette is the validated categorical set in `PALETTE` (adjacent-CVD-checked on white);
  status colours (`STATUS`) are reserved for stock/timeliness and always ship with icon + label.
- **`meta_ads` is LIVE via Windsor.ai since 2026-07-29** (secret `meloyelo-windsor-key`, account
  `facebook__465444904516684`, `job/main.py pull_meta`): per-ad/day rows with `actions_lead` +
  `actions_landing_page_view`, WINDSOR_PRESET last_365d with older rows carried forward from the
  previous publication (the honeytribe merge). The Marketing tab renders the full Meta section
  (tiles, click funnel, trends, campaign table) whenever `meta_ads.enabled` — and falls back to
  the designed empty state otherwise. (An earlier note here said this client had NO Windsor
  connector at all — true until 2026-07-29, superseded by the connector landing that day.)
- **`google_ads` / `ga4` / `email` remain wired-but-dormant JSON keys** — the Marketing tab
  renders designed empty states until the export job fills them. The Google Ads account
  (`668-008-6591`) and GA4 property (`312428782`) are NOT connected in Windsor yet; the user is
  adding them — wire them the same way `meta_ads` is when they appear.
- A **creative gallery** is buildable now that the connector exists — the `client_RHE` /
  `client_honeytribe` pattern: put `creative_id` on the main Meta pull (it rides along free),
  add `fetch_creatives` + `cache_creative_images`, and serve images through an authed
  `/creative-img/<cid>` route so the bucket stays private.
- Local preview port is **8146** (8140 belongs to client_RHE's preview).

## ⚠️ `context/` is git-ignored and holds LIVE credentials

A live Lark app secret + refresh token, a live Unleashed API id/key, a live Campaign Monitor API
key, and customer PII. Never commit it, never echo a key into a file, and **rotate all three
before standup**. The emitted JSON contains NO PII — people are numeric CRM ids only.

## Local preview

```powershell
py clients\client_MeloYelo\job\build_local.py     # rebuild data/meloyelo.json from context/
py clients\client_MeloYelo\preview_local.py       # -> http://localhost:8146
```

Validate before any deploy: `py tools\_validate_dash_js.py clients\client_MeloYelo\dash\dashboard.html`.

## Deployed (2026-07-28)

LIVE at https://meloyelo-dash-c732u7m57a-as.a.run.app (open access, `DASH_OPEN=1`). Redeploy
with `deploy_meloyelo.ps1` (idempotent; re-running rotates the secrets to whatever you pass).
**Refresh is Sync-button-only BY DECISION (operator, 2026-07-28)** — there is deliberately NO
scheduler (every export run costs Unleashed + Campaign Monitor API calls); the deploy script
now DELETES `meloyelo-export-6h` if it exists. Don't re-add one without asking.
Remaining to go fully live: share the CRM sheet with the job SA (riders/leads), one-time Lark
auth via `job/lark_auth.py` (production orders), rotate the connector keys. Full metric
definitions and the verified-numbers table: [README.md](README.md).

## Dashboard standard (applied 2026-07-30)

This dashboard follows [`clients/_standard/STANDARD.md`](../_standard/STANDARD.md) — the **Sales**
layout over the shared shell. Client extras are untouched: the standard is a floor, never a ceiling.

The Riders & Leads tab follows the **Leads** section order inside the Sales shell — that is the
sanctioned shape for a client that genuinely needs both (STANDARD.md §1).

What changed on 2026-07-30: the Sales tab gained a real **KPI benchmark** range. It previously
compared every scorecard against a hardcoded equal-length window immediately before the period.
That is now the `prev` preset (so the default view is unchanged), joined by 90d / rolling 12m /
last FY / all time and a hand-typed pair.
- **`benchRange()` resolves from a CODE, not stored dates**, so "Previous window" follows the period
  wherever the period moves. Typing a date pair sets `S1.bcode = ""` and pins it.
- **Volume metrics are scaled to the period's length; rates are not.** Comparing a 90-day period
  against a 365-day benchmark raw would report a collapse every time. The tile's sub-line says which
  it is doing (`benchmark ×0.25`).
- **`lastfy` is the whole previous financial year**, not a rolling 12 months, which over-weights
  whichever season it starts in — on a seasonal e-bike business that is most of the signal. NZ FY
  starts 1 April and the FY label is the END year, so read `fyStartOf` before touching it.

**`clients/_standard/dash/_conform.css` is VENDORED into this file** between sentinel comments (the
print + reduced-motion + screen-reader block). Never edit inside the sentinels — re-sync with
`py -3 clients/_standard/vendor_lib.py`. Both gates before any dash deploy:

```powershell
py -3 tools\_validate_dash_js.py       clients\client_MeloYelo\dash\dashboard.html
py -3 clients\_standard\check_standard.py clients\client_MeloYelo\dash\dashboard.html
py -3 clients\_standard\vendor_lib.py --check
```
