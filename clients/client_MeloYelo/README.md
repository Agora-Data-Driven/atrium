# `meloyelo` — MeloYelo E-Bikes dashboard

MeloYelo is a **Kiwi-owned e-bike brand** ("just mad about E-BIKES") selling bikes, parts and
accessories through a nationwide agent network, targeting older riders. This directory replaces
the client's ten-page Looker Studio report + Google Sheets + three Colab pipelines with one
self-hosted dashboard on GCP — the [`client_honeytribe`](../client_honeytribe/) pattern.

```
Unleashed (invoices/credits/COGS)  -> the client's reconciled "Sales Line Data" feed \
CRM master sheet (riders + leads)  -> dedupe/stage-event pipeline (Colab cell 17)     \
Unleashed stock join + Lark base   -> Available Now / On The Way                       >- job -> meloyelo.json -> dashboard.html
Campaign Monitor extract           -> email summary                                   /
Meta · Google Ads · GA4            -> wired empty states, awaiting Windsor connectors /
```

**Local as of 2026-07-28:** 20,852 sales lines (2021-05-23 → 2026-07-24), 4,541 riders,
3,047 lead requests, 6,695 stage events (CRM through 2026-07-27), 16 stock variants,
5 production orders, 394 email campaigns. ~5.9 MB JSON.

---

## The four tabs

**01 · Sales Performance** — the Looker *Sales Performance* page, rebuilt.
FY bike-target hero (157 of 900, pace marker, projection), FY-so-far vs last-FY compare card,
six scorecards that double as **series toggles** on a metrics-over-time chart (relative /
absolute axes, day/week/month grain — the Honey Tribe pattern), month scoreboard (this month vs
prev vs same month last year), bike sales by model stacked over time, customer-type donut
(click to cross-filter), sortable model + agent leaderboards (click a row to cross-filter),
product-group table, and a plain-language "Reading of the period".

**02 · Riders & Leads** — the Looker *Rider Community* + *Lead Generation* pages, fixed.
Rider community hero + organic-join tiles, community-over-time (the broken Looker chart plotted
riders against customer NAMES — now every rider sits on the date they joined), riders by
source/agent/region/model, the test-ride scoreboard (prev month / MTD / FYTD, COUNT-DISTINCT
people, warranty registrations excluded — the client's own QA logic), a **monotonic cohort
funnel** (furthest stage reached by everyone who requested in the window), requests over time by
source, **speed to lead by agent in the client's requested `2D 12H 10M` format** (median first,
mean + count in the tooltip), leads by region, and a current-stage spread of the pipeline.

**03 · Inventory & Production** — the Looker *Bike Inventory* page, extended.
Stock tiles (16/16 variants, 485 bikes, low-stock count with the same <15 threshold the Looker
page used), stock-by-variant bars with status colours + icon chips, a **runway table** (months
of cover at the last-180-day selling pace, plus what production adds — new here, nothing in the
Looker report answered "when do we run out?"), and the On-The-Way table with stage/timeliness
chips.

**04 · Marketing** — email LIVE, paid channels wired.
Campaign Monitor pulled **live from the API**: unique open/click rates per campaign (which the
old Looker report never had), campaign-performance table, whole-history tiles. Meta Ads,
Google Ads (account `668-008-6591`) and GA4 render **designed empty states** that specify
exactly what lands when each Windsor connector is wired into the export job.

Every tab ends with a "Reading of…" insight strip that recomputes with the filters. The tab is
in the URL hash (`#riders`), so a view can be linked to.

## Metric definitions (verified against the client's own artifacts)

| Metric | Definition |
|---|---|
| Bike units / revenue | product groups **Bikes + Special Bike Package** (the only set that reproduces both Looker's FYTD 157 and prior-FYTD 187), net of credit notes |
| Revenue / COGS / GP | the client's reconciled rules (Unleashed_API_Final notebook): revenue = invoice − credit LineTotal (tax-excl); COGS = AverageLandedPriceAtTimeOfSale × qty; credits subtract units only when ReturnToStock |
| FY | NZ financial year, 1 April; the label is the end year (Apr 2026 → FY2027) |
| Rider | CRM stage `MY Customer`; joined = first recorded transition into it, else CRM entry date |
| New riders | organic `MY Customer` transitions, **warranty imports excluded** (the QA-tab rule) |
| Lead metrics | COUNT-DISTINCT Unique ID per metric per window; `Warranty Registration` rows never count (the Colab cell-17 pipeline, replicated in `job/build_local.py`) |
| Funnel | cohort of requesters in the window, by furthest stage reached to date (monotonic by construction) |
| Speed to lead | `speed_to_lead_minutes` from the CRM, shown as `2D 12H 10M`; median headline (means are distorted by month-old leads) |
| Region | NZ postcode blocks (Excel-float-safe: `981.0` reads as 0981) with a city-name fallback — labelled approximate |
| Stock status | red < 15 · amber 15–24 · green ≥ 25 (reproduces Looker's "Low Stock 2") |
| Runway | stock ÷ (bike units in last 180 days ÷ 6), matched by model name |

### Numbers verified (2026-07-28 build)

| Check | Ours | Looker / client QA | |
|---|---|---|---|
| FYTD bikes / revenue | 157 / $510,609 | 157 / $510,609 | PASS |
| This month / prev month bikes | 16 / 34 | 16 / 34 | PASS |
| Prev FYTD bikes / revenue | 187 / $531,543 (to 27 Jul LY) | 187 / $531,543 | PASS |
| P&A FYTD / this / prev month | $31,876 / $9,432 / $7,780 | same | PASS |
| Avg bike value FYTD | $3,252 | $3,252 | PASS |
| Rider community | 4,541 distinct ids | 4,611 rows (70 rows carry no/duplicate Unique ID — distinct people is the honest count) | PASS* |
| Test-ride requests prev/MTD/FYTD | 28 / 27 / 184 | QA 28 / 25 / 182 as of 26 Jul (ours reads a day fresher) | PASS |
| Booked · Completed (prev/MTD/FYTD) | 11/1/59 · 7/0/35 | 11/1/59 · 7/0/35 | PASS |
| Inventory tiles | 16 variants / 485 / low 2 / 450 in production / 5 POs | same | PASS |

Looker's `SMLY 63` bike tile could not be reproduced from any consistent definition (July LY is
20 by the reconciled feed) — the old sheet's number, not ours; the month scoreboard computes SMLY
directly from the sales lines.

## What was deliberately changed from the Looker report

Kept: every page and metric the client asked for. Changed where the old chart was broken or
misleading — each with the reason:

1. **Riders Over Time** plotted rider counts against customer *surnames* — rebuilt as community
   size / monthly joins over actual join dates.
2. **New riders joined `MTD 16 / prev 15 / FYTD 13`** was mathematically impossible (FYTD <
   MTD) — replaced with the warranty-excluded organic joins the client's own QA tab defines
   (0 / 8 / 123).
3. **Riders-by-source donut said 93% Website Form** because it silently dropped the 4,300 riders
   with warranty/blank sources — now shows the honest split (53% warranty, 41% unattributed…)
   with the attribution gap called out as an insight.
4. **The funnel could show Completed > Booked** (sparse stage-history events) — now a cohort
   funnel by furthest stage reached, monotonic by construction.
5. **Speed to lead** was raw minutes (unreadable six-figure bars) — now `2D 12H 10M`, median
   headline, per the client's explicit request.
6. **Meta "Investment & Efficiency" (ROAS / purchase value) showed No data** and always will:
   sales close offline through test rides. The Meta pane is designed around **leads and
   cost-per-lead**, with the revenue side already measured properly in the CRM funnel.
7. **Four overlapping GA4 pages** (Website Visitors, Engagement Rate, GA Performance, Device &
   Category) consolidated into one Site-analytics pane spec — engagement rate, active users by
   channel, landing pages, device split. Browser/OS bar charts dropped (not actionable; say the
   word and they return when GA4 is wired).
8. **Agent bar chart + agent table** merged into one sortable leaderboard with inline bars;
   same for models.

## Local preview — living data

```powershell
py clients\client_MeloYelo\job\main.py             # the LIVE pull (~2-4 min): Unleashed + Campaign Monitor
py clients\client_MeloYelo\preview_local.py        # -> http://localhost:8146  (8140 is RHE's)
```

The dashboard's **Sync button works locally**: it POSTs `/refresh`, which runs `job/main.py`
again (cooldown `REFRESH_COOLDOWN_SECONDS`, default 300, so repeat clicks never hammer the
client's APIs), then reloads the JSON in place.

**What is live vs snapshot** (also in the `job/main.py` docstring):

| Source | Mode | Why |
|---|---|---|
| Unleashed sales lines | **LIVE** | invoices/credits/COGS pulled per run, the reconciled rules |
| Unleashed stock (Available now) | **LIVE** | Products (current range) ⋈ StockOnHand per run |
| Campaign Monitor | **LIVE** | campaign list + per-campaign unique opens/clicks |
| CRM (riders + leads) | snapshot | the master Google Sheet is private (anon export 401s) — share it with a service account, or keep dropping the xlsx into `context/` |
| Lark "On the way" | snapshot | the notebook's refresh token is dead (single-use, consumed); re-authorize Lark and set `LARK_*` env vars |

`job/build_local.py` remains the zero-credentials fallback (reads only the context workbooks;
same JSON shape). Credentials: env vars first (`UNLEASHED_API_ID`, `UNLEASHED_API_KEY`,
`CAMPAIGN_MONITOR_API_KEY`, `CAMPAIGN_MONITOR_CLIENT_ID`) — off-cloud they fall back to the
notebooks in `context/`, so a developer can run a real pull with zero setup.

## ⚠️ `context/` is git-ignored — it holds LIVE credentials and PII

The drop contains a **live Lark app secret + refresh token** and a **live Unleashed API id/key**
(`Lark_Unleashed_Dash.ipynb`), a **live Campaign Monitor API key**
(`Campaign_Monitor_Full_Email_Extract (3).ipynb`), and **customer PII** (names, emails, phones)
in both workbooks. [.gitignore](.gitignore) keeps the folder out of git. **Rotate all three keys
before standup** and put the new ones in Secret Manager — never in a file in this repo.

Nothing in the emitted JSON carries PII: a person is their numeric CRM id + dates + stage +
agent + source + region + bike model. Agent names are business identities and are kept.

## Layout

```
clients/client_MeloYelo/
  README.md · CLAUDE.md · .gitignore
  preview_local.py           local server: dashboard.html at / and meloyelo.json at /data.json
  context/                   the client's source drop (GIT-IGNORED — live keys + PII)
  data/                      locally built meloyelo.json (git-ignored)
  job/
    build_local.py           context workbooks -> meloyelo.json (the offline Stage 2)
    assets/                  meloyelo-mark.svg · agora.png
  dash/
    dashboard.html           one self-contained file, esprima-4.x-safe inline JS
    main.py                  auth + /data.json proxy + /refresh   platform_sso.py   Dockerfile
```

## Live (deployed 2026-07-28, tag b4851f7)

| | |
|---|---|
| Dashboard | **https://meloyelo-dash-c732u7m57a-as.a.run.app** — no login (`DASH_OPEN=1`) |
| Service | `meloyelo-dash` (asia-southeast1, `--no-invoker-iam-check`, password + SSO-ready) |
| Export job | `meloyelo-export` — live Unleashed + Campaign Monitor pull each run |
| Refresh | **Sync button only** (operator's decision 2026-07-28) — the in-page Sync POSTs `/refresh`, which triggers `meloyelo-export` behind a cooldown. No scheduler: every run costs Unleashed + Campaign Monitor API calls, so nothing fires unattended |
| Data | private `gs://agora-data-driven-meloyelo-dash/meloyelo.json`, served only via the authed `/data.json` proxy |

Access is OPEN at the operator's request (2026-07-28): the URL is unguessable, not secret. To
re-gate set `DASH_OPEN=0` — the password secret (`meloyelo-dash-password`), `/login` and the
session cookie are all still wired.

Verified after deploy: `/` 200, `/data.json` 200 (~6 MB, cloud-pulled, data through deploy day),
`POST /refresh` 200 with the cooldown reply, headless render with zero JS errors.

## Remaining to go 100% live (in order)

1. **Share the CRM master sheet** (and ideally the reporting sheet) as **Viewer** with
   `meloyelo-dash-job@agora-data-driven.iam.gserviceaccount.com` — riders/leads then pull live
   on the next run, no redeploy. Until then the job carries the last-known CRM forward.
2. **Lark one-time auth** — `job/lark_auth.py` (the token self-rotates afterwards; the job
   stores the newest refresh token in the bucket every run).
3. **Rotate the connector keys** (Unleashed, Campaign Monitor, Lark) — the current ones came
   from shared notebooks; re-run `deploy_meloyelo.ps1` with the new values to rotate the secrets.
4. **Windsor connectors** for Meta / Google Ads (`668-008-6591`) / GA4 — fill `meta_ads.rows`,
   `google_ads.rows`, `ga4.rows`; the Marketing tab lights up with no dashboard changes.
5. Optional: map `meloyelo.agoradatadriven.com` → `meloyelo-dash`, then
   `tools\enable_platform_sso.ps1 -Keys meloyelo`.
