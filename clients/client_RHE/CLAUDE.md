# CLAUDE.md — clients/client_RHE (Meta + ActiveCampaign, API-LIVE)

**Rules live in the repo-root [`/CLAUDE.md`](../../CLAUDE.md)** — read it first; this file only adds
local context. If they disagree, root wins. Build guidance:
[`docs/CLIENT_DASHBOARD_PLAYBOOK.md`](../../docs/CLIENT_DASHBOARD_PLAYBOOK.md).

Rooming House Expert is an **API-LIVE** client — the [`client_honeytribe`](../client_honeytribe/) /
[`client_riverdance`](../client_riverdance/) pattern, **not** the BigQuery-fed
[`client_template`](../client_template/). There is **no dataset and no SQL views**; `job/main.py`
pulls Meta (Windsor `all`, 3 ad accounts) and ActiveCampaign (REST v3) directly each run and writes
`rhe.json` itself (the data object is **lowercase** — `deploy_RHE.ps1` derives `$KEY.json` from
`$KEY = $CLIENT.ToLowerInvariant()` and `dash/main.py` defaults `DATA_OBJECT` to `rhe.json`;
`job/main.py`'s docstring still says `RHE.json`, which is only the prose being stale — the local
fixture at `data/RHE.json` genuinely IS uppercase). Two-stage contract:

```
job/main.py (data dict key)  ->  dash/dashboard.html (DATA.* key)
```

**This is a LEAD-GEN client, not e-commerce.** No revenue, no ROAS, no AOV — the whole dashboard is
built around **CPL, lead volume, and email engagement as the sales-readiness proxy**. If you find
yourself adding a revenue metric, you are on the wrong client.

## The three modules

- **`job/main.py`** — Windsor Meta across 3 accounts + the 5 breakdown pulls + history merge.
- **`job/activecampaign.py`** — EXTENDED from riverdance's module (campaigns/lists/automations are
  that module's shape, unchanged). RHE adds contacts+quiz, the per-day send series and the
  open/click event series.
- **`dash/dashboard.html`** — one self-contained file, four tabs (`funnel` · `email` · `magnet` ·
  `demo`) selected by URL hash. Inline JS must be **esprima-4.x-safe** (no `?.`, no `??`).

## Dashboard conventions (keep these when extending)

- **`ACCT` at the top of the script scopes the dashboard to one ad account; it is `""` (BLEND) by
  the client's decision 2026-07-28.** The three accounts are not parallel businesses — they run
  back to back (Stuart Baker 2024-01→2025-11, Super Cashflow 2025-10→2026-03, RHE 2026-01→), which
  is one advertiser moving between ad accounts. Blending restores 2.5 years and is what makes the
  previous-calendar-year benchmark usable at all; scoping to `rhe` left it with zero rows and every
  benchmark column rendered empty. Set `ACCT` to an `account_name` to isolate one era.
- **Sorting is declarative, not per-table.** A `<table>` carries `data-sort="<stateKey>"` and each
  sortable `<th>` carries `data-k="<field>"`; `wireSorts()` walks every such table on load and
  `markSort()` paints the active column. Adding a table needs no new wiring — just the attributes
  and a `SORT` entry (auto-created if missing).
- **The card accent bar is along the BOTTOM**, not the left edge (`.kpi::after` / `.stage::after` /
  `.card::after`). Series scorecards override it with an inline `<span>` strip painted the series
  colour, because the tile's bar IS the line colour on the chart below.
- **One segment builder feeds the whole demographics tab.** `demoSegments()` reads the `Split by`
  control and returns the rows the value map, the rank chart, the share bars and the table all
  render — so those four views can never disagree, and adding a split is one entry in `DEMO_SPEC`.
- **The funnel is framed as carry-through, not loss** — "X% carried through", plus a conversion
  KPI rail. Same arithmetic, actionable framing; do not revert it to "% drop".
- **Percent axes use `pctAxis(max)`**, which picks decimals from the range. Rounding to whole
  percent printed "1% 1% 1% 0% 0%" on a 1.2% axis.
- **The creative gallery has ONE source of numbers — `meta.rows`, not the creative pull.** Every
  Meta row carries a `cid` (`creative_id` rides on the main pull for free: identical row count and
  identical spend with and without it), and `fetch_creatives()` pulls only the ad's *text and
  image* keyed by that id. So the gallery aggregates delivery over whatever period / campaign /
  ad-name filter is active and can never disagree with the tiles above it. A static side panel
  fed by its own metrics pull would.

## Six traps that already bit this client (do not undo these)

- **`FORCE_IPV4` at the top of `job/main.py` is load-bearing.** Cloud Run has no IPv6 egress route
  but Windsor and ActiveCampaign publish AAAA records; without it every request burns its whole
  connect timeout on v6 first. See the playbook §3.1.
- **Meta rows are SUMMED on key collision, never overwritten.** Windsor returns ~60 rows sharing
  even `(date, account, campaign, adset, ad)` (they differ only on a descriptive field such as
  `title`). Overwriting silently dropped **$321** of spend; the audit caught it against the
  two-field lifetime total. Frequency is carried impression-weighted.
- **`unique_actions_*` cap the Windsor window at 365 days.** That, not the field count, is what
  400s at `last_730d`. The job therefore runs a **deep pull without them at `last_1095d`**
  (history from 2024-01-15) plus a **unique pull at `last_365d`**, joined on `(date, campaign,
  ad)`. Unique counts are not additive, so each group's value is attached to its largest row by
  impressions and zeroed on the rest — any aggregate spanning the group is then exact.
- **A full sync must NOT merge into the previous publication's daily series.** `AC_FULL_SYNC=1`
  re-reads rows already counted; merging them doubled sends 6,000 → 12,000 in the first local
  verification. `fetch()` discards `prev` when `full` is set (the `carry` variable).
- **The event classification took THREE attempts. Do not change `EVENT_KINDS` without redoing the
  campaign-level cross-check.** The verified truth:
  * **`log` = the SEND/delivery record**, not an open and not a click. `/logs` totals 54,304 against
    `/emailActivities`' 54,243, and a `/logs/<id>` row carries `sendid`, `campaignid`, `messageid`,
    `successful: 1`. It is **excluded** (sends come from `/emailActivities`; counting it again
    doubles them). Mapping it to "open" made the dashboard report a **100.00% open rate** —
    opens exactly equalled sends, 10,285 = 10,285.
  * **`link-data` = an OPEN** (tracking-pixel read; the record has an `isread` flag, a `ua` like
    `"GMail"` and a mail-proxy IP). Across the 12 sent campaigns: **6,070** link-data rows against
    **7,765** total opens (0.78×) and 5,155 unique opens (1.18×) — but only **133** link clicks
    (45.6×). It is an open, categorically not a click. Calling it a click produced a 21.78% "click
    rate" against 0.69% at campaign level, which is what exposed it.
  * **`mpp-link-data` = an Apple Mail Privacy machine prefetch**, on its own endpoint
    (`/mppLinkData`, 23,379). It is **NOT a subset of opens**: campaign 537 has linkData 585 against
    AC's own `opens` 615, while mppData is 429 — 585+429 would be 1,014, far past 615. AC keeps
    these out of its open counter, so the dashboard shows `mpp` as **its own line**, never as a
    share of opens.
  * **CLICKS are not in the activity stream at all.** The only trustworthy figures are campaign-level
    (`linkclicks`/`uniquelinkclicks`), so every click figure is scoped to **broadcasts** and the UI
    says so. The all-mail block renders clicks as `n/a` rather than inventing a number.
  * `DAILY_VERSION` (currently **3**) guards the shape: an older publication has its engagement
    counts dropped by `_merge_daily` and its events watermark cleared, so the corrected
    classification re-pulls them. Sends survive, so a migration costs one event crawl, not the
    543-page send crawl. Bump it whenever a stored field changes MEANING.
  * `job/audit.py` FAILS on a `v<3` series, on any surviving daily `clicks` field, on
    **opens ≥ sends**, and on a broadcast open rate ≥ 99.9%; it warns above a 90% open rate.
    **A rate that pins at exactly 100% is a misclassification, not a triumph** — that is the tell.
- **Events MUST page newest-first (`orders[tstamp]=DESC`).** `/activities` is oldest-first by
  default, and this bug bit **twice**: paging from offset 0 returned 2025 events and nothing
  recent; then paging with an `after=<floor>` cold start was still oldest-first *within* the
  window, so 600 pages covered 2026-03-29 → 05-28 and the latest two months were empty — the
  dashboard's default 30-day view showed zero opens both times. `orders[tstamp]=DESC` **is**
  honoured (unlike every `filters[...]`), so events now walk back from the newest and stop at the
  watermark, exactly like sends. The page cap then only limits how far BACK a cold start reaches,
  which `watermark.events_oldest` records so the horizon can be stated honestly.
- **ActiveCampaign 503s after a long crawl, and a bare crawl loses the whole leg.** The first full
  live run did 543 send pages and then the events leg 503'd on its *first* request — best-effort
  swallowed it and published a year of `opens: 0`. Two defences, both load-bearing: `_get()`
  retries `429/500/502/503/504` with exponential backoff (honouring `Retry-After`), and each
  crawl **returns the pages it already has** on a late failure instead of raising out. There is
  also a small `AC_PAGE_PAUSE` between pages. `job/audit.py` now FAILS if `email.error` is set or
  if a period has sends but zero opens, so this can never pass silently again.
- **Meta's grey "no preview" tile is a VALID image, so `onerror` never saves you.** When a creative
  has no real image, Meta returns its external image PROXY —
  `external-<edge>.xx.fbcdn.net/emg1/…?url=<page>` — which is a link preview of the destination
  page, not the ad: a near-blank grey square. Because it loads successfully the browser fires no
  `error` event, the branded-tile fallback never runs, and the client stares at grey boxes.
  `_is_link_preview()`/`_usable_image()` reject it **at the source** so the card falls back to the
  headline tile. ⚠️ The first fix for this was "any image whose bytes are shared by 2+ creatives is
  the placeholder" — **wrong, and it deleted real artwork**: advertisers legitimately reuse one
  image across ad variants (Honey Tribe had a 1080×1080 with 65k colours shared by two creatives).
  Duplication is not the signal; the URL is. `tools/_creative_gallery_test.py` guards both halves.
- **Quiz answers arrive in TWO codings** — the Meta lead form posts `some_experience` while the
  AC dropdown stores `Some experience`. Left alone every quiz chart splits each category in two.
  `_fold()` + `canonical()` in `activecampaign.py` are the ONE normaliser, keyed off the field's
  own option labels; the audit asserts no value is double-coded. Same class of bug as honeytribe's
  self-referral split.

## API limits (the API's, not ours — do not "fix")

- Windsor `all` takes a **`date_preset` only**; `date_from`/`date_to`/`maximum` all 400.
- **`unique_actions_lead` is identically 0** account-wide although the column is returned;
  `unique_actions_link_click` works. The dashboard renders it **n/a**, never `0`.
- **The region breakdown returns no leads** on any field combination — geography is delivery-only.
  Age × gender does carry leads and reconciles exactly to the main pull.
  ⚠️ **Doc/behavior drift (audited 2026-07-29):** `fetch_breakdowns()` emits a per-breakdown
  `has_leads` flag (false for region) and `job/main.py`'s docstring says the dashboard uses it to
  hide lead/CPL cuts — but **`dash/dashboard.html` never reads `has_leads`**. Region stays hidden
  simply because `region` has no entry in `DEMO_SPEC` (`dash/dashboard.html` ~line 3384, the one
  Split-by registry); only `job/audit.py` consumes the flag today. If you ever add a `region` split
  to `DEMO_SPEC`, wire `has_leads` first or the lead/CPL columns render as zeros.
- **ActiveCampaign `limit` is hard-capped at 100** everywhere.
- **`/activities` ignores every `filters[...]`**; only `after=<ISO>` narrows it.
- **`/emailActivities` ignores `filters[tstamp][gt]`** but honours `orders[tstamp]=DESC`.
- **`automation_name` is always null** — the subject line is the only template identifier.
- **CRM `/deals` 403s** ("Upgrade your account"); degrades to `crm_enabled:false`.
- Only **11 of 312 campaigns** were ever sent — automations carry most of the volume, so never
  build the email tab on `/campaigns` alone.

## ⚠️ Free text from the source can carry PII — `redact()` is load-bearing

Hashing the contact was not enough. A live **subject line** read
`"Did you get the Warragul brochure, j.smith@example.com" (address anonymised here)` — a merge tag fell back to the
recipient's email instead of their first name, and that address rode into the published payload
inside a *template name*. `job/audit.py` caught it (playbook §9); nothing on screen would have.

So **every free-text field from ActiveCampaign goes through `redact()`** before it is published:
subject lines (`template_of`), campaign names, list names, automation names, and the free-text
`Campaign` contact field. `redact()` strips email addresses and phone numbers by pattern, so it
does not depend on any length or word-count heuristic (the original personalisation-strip only
removed tails ≤24 chars, which is exactly why a 27-char email got through).
`_merge_templates` **re-redacts carried-forward names**, so a publication written before the
redaction existed is cleaned on the next run instead of re-publishing the leak forever.

Anything new that surfaces source-authored text must go through `redact()` too.

## ⚠️ `context/` is git-ignored and holds LIVE credentials

A live Windsor API key, a live ActiveCampaign Api-Token, and customer emails/phones/addresses.
Never commit it, never echo a key into a file, and **rotate both** — they were circulated in a
shared document. The emitted JSON contains **no PII**: contacts are a salted hash plus an email
domain. `job/audit.py` asserts this on every run; keep it that way.

**Brand:** `context/RHE Brandkit.docx` supplies the mark and palette. The hex values in `:root` were
**sampled from the brand-board pixels**, not read off the labels (they render too small to trust):
`#BE5BF6` purple · `#038810` green · `#153DFD` blue · `#02B6FD` cyan · `#C064F5` lilac. `--lilac` is
deliberately NOT in the series ramp — it is indistinguishable from `--purple` at 2px. The logo is
cropped from the board into `job/assets/rhe-mark.png` and embedded as a data-URI; it is drawn for a
LIGHT ground, which is why the chrome is light. Oswald stands in for Agency Gothic Condensed.

## Verify before deploying

```powershell
py -3 tools\_validate_dash_js.py clients\client_RHE\dash\dashboard.html   # esprima gate
py -3 clients\client_RHE\job\make_fixture.py clients\client_RHE\data\RHE.json  # no creds needed
py -3 clients\client_RHE\job\audit.py --live                              # playbook 9
py -3 clients\client_RHE\preview_local.py 8140 --no-open                  # then drive it
```

`job/make_fixture.py` emits a synthetic payload in the published shape so every code path can be
exercised without a 30-minute live crawl. **Keep it in step with `main.py`'s `build()`** — a key
that moves in one and not the other means the smoke test stops covering the real shape.
`job/audit.py` is the independent recompute (playbook §9); it asserts the PII posture, that the
breakdowns reconcile, that no quiz value is double-coded, and that the recent window actually has
engagement (the cold-start regression).

`preview_local.py` is **threaded on purpose** — the payload is ~5 MB and a single-threaded server
blocks every other request while one browser downloads it.

## Deploy

`deploy_RHE.ps1` — one-shot, idempotent, no dataset and no views. Grants **`roles/run.developer`**
(not `run.invoker`) on the export job, which is the ONLY thing making the Sync button work.

**There is deliberately NO Cloud Scheduler** (client decision 2026-07-28): refresh runs off the
dashboard's Sync button, because each run costs paid Windsor/Meta calls. Running the script without
`-WithScheduler` REMOVES any scheduler it finds, so re-running converges on that state instead of
silently leaving a timer behind. The `/refresh` route's 10-minute cooldown (keyed on the data
object's age, so it is shared across instances) is what stops repeat clicks running up the bill —
do not remove it while `DASH_OPEN=1`, since there is no login in front of that route.
The export job gets `--task-timeout 3600` because ActiveCampaign's 100-row page cap makes the first
crawl long; later runs are incremental off the stored watermark.

Never Cloud Build from a laptop; never `--allow-unauthenticated` (org policy forbids it — the app
does its own password/SSO auth behind `--no-invoker-iam-check`).

## Dashboard standard (applied 2026-07-30)

This dashboard follows [`clients/_standard/STANDARD.md`](../_standard/STANDARD.md) — the **Leads**
layout over the shared shell. Client extras are untouched: the standard is a floor, never a ceiling.

Most of the standard's *helper library* is this file's, generalised — `grainOf`/`bucketOf`/`segWire`/
`wireSorts`/`presetRange` were lifted from here into `clients/_standard/dash/_lib.js`.

What changed on 2026-07-30, and why:
- **Chrome ids renamed to the standard names** so a shared feature can find them:
  `#mark`→`#logo`, `#upd`→`#updated`, `#through`→`#thru`, `#sync`→`#syncBtn`,
  `#synclbl`→`#syncLbl`. Every JS reference moved with them — grep before assuming an old id exists.
- **A `#boot` / `#app` swap was added.** The page used to render straight into `<main>`, so a slow
  or failed fetch showed an empty dashboard rather than a loading state; a fetch failure now lands
  inside `#boot` instead of being prepended to `<body>`.
- **Removed a dead `segWire("seg-agmet", …)`** — there is no `#seg-agmet` control on the page, so it
  silently no-oped. `SEG.agmet` is still in the state object; wire a control to it or drop it.

**`clients/_standard/dash/_conform.css` is VENDORED into this file** between sentinel comments (the
print + reduced-motion + screen-reader block). Never edit inside the sentinels — re-sync with
`py -3 clients/_standard/vendor_lib.py`. Both gates before any dash deploy:

```powershell
py -3 tools\_validate_dash_js.py       clients\client_RHE\dash\dashboard.html
py -3 clients\_standard\check_standard.py clients\client_RHE\dash\dashboard.html
py -3 clients\_standard\vendor_lib.py --check
```
