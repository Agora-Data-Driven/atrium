# Client dashboard build playbook

How to build the next client dashboard correctly. Written after Honey Tribe (Shopify + Meta,
2026-07-27); most entries below cost real hours the first time.

**Sources differ per client.** Sections 1–4 and 6–10 are source-agnostic and always apply.
Section 5 is a per-connector appendix — **read only the connectors you are actually using.**

### Worked examples

| Need | Look at |
|---|---|
| Live API pull, no BigQuery | [`clients/client_honeytribe/`](../clients/client_honeytribe/) |
| The original Windsor-live pattern | [`clients/client_riverdance/`](../clients/client_riverdance/) |
| BigQuery-fed (views + export job) | [`clients/client_template/`](../clients/client_template/) |
| **ActiveCampaign connector (done)** | [`clients/client_riverdance/job/activecampaign.py`](../clients/client_riverdance/job/activecampaign.py) |
| **ActiveCampaign dashboard tab (done)** | [`clients/client_riverdance/ACTIVECAMPAIGN_TAB_GUIDE.md`](../clients/client_riverdance/ACTIVECAMPAIGN_TAB_GUIDE.md) |
| Absolute/Relative axis toggle | `clients/client_resetdata/dash/dashboard.html` (other repo) |

---

## 1. Pick the pattern first

| | **API-LIVE** | **BigQuery-fed** |
|---|---|---|
| Examples | `client_honeytribe`, `client_riverdance` | `client_template`, `client_TCS` |
| Infra | bucket + job + web service. **No dataset, no SQL views** | + dataset + `sql/*.sql` views |
| Use when | the fields you need are not in `raw_windsor`, or the source is not a Windsor connector at all | the data already flows through `raw_windsor` |

Default to **API-LIVE** unless you have checked `raw_windsor` actually holds what you need.

Everything derives from one short key `<c>`: bucket `agora-data-driven-<c>-dash`, job `<c>-export`,
service `<c>-dash`, SAs `<c>-dash-job` / `<c>-dash-web`, secrets `<c>-dash-password` /
`<c>-dash-session-key` / `<c>-<source>-key`. Never re-type these.

**One JSON, many sources.** Every source lands in one private `<c>.json` that the dashboard reads.
Each source is a top-level key, and **every source after the first is best-effort**: a failure
returns `{enabled: false, error: "..."}` and never sinks the export. Riverdance does exactly this —
Meta is required, ActiveCampaign rides along and degrades to a "not configured" tab.

---

## 2. Do these five things before writing any code

1. **Git-ignore the client's context drop immediately.** Create `clients/client_<c>/.gitignore` with
   `context/` and `data/` *before* anything else. Hand-offs routinely contain **live API tokens and
   customer PII** — Honey Tribe's had a live Shopify token, a live Windsor key, and 9,000 customers'
   emails and addresses. Verify with `git check-ignore -v <path>`; don't trust memory.
2. **Probe the credentials before designing anything.** A five-minute script that hits each endpoint
   and prints row counts + date ranges tells you what actually exists. On Honey Tribe this turned
   "what token do you still need?" into "none — all four already work", and revealed the client's
   spreadsheet was a stale snapshot while the API had 10 more months of data.
3. **Never trust a spreadsheet export as the source.** It is a point-in-time snapshot. Build against
   the API; keep the workbook only as a no-credentials fallback.
4. **Emit no PII.** Reduce people to a salted hash used only for distinct counts and
   first-time/returning. Then *assert* it in the audit (§8).
5. **Get ambiguous metric definitions in writing** (§6).

---

## 3. Universal engineering traps

### 3.1 IPv6 on Cloud Run — the expensive one
Cloud Run has **no IPv6 egress route**, but most SaaS APIs publish AAAA records.
`socket.create_connection` walks `getaddrinfo` in order applying the **full timeout to each
address**, so every request burns its entire connect timeout on v6 before falling back.

- Symptom: requests take *exactly* your timeout value. A 38-page crawl became a **76-minute** job
  and blew the task timeout; locally it was 80 s.
- Fix (put this in **every** job that calls a third-party API) — the same backfill then ran in 133 s:

```python
if os.environ.get("FORCE_IPV4", "1") == "1":
    _real = socket.getaddrinfo
    def _v4(host, port, family=0, type=0, proto=0, flags=0):
        return _real(host, port, socket.AF_INET, type, proto, flags)
    socket.getaddrinfo = _v4
```

### 3.2 Buffered stdout makes a job look hung
Without `PYTHONUNBUFFERED=1` Python holds every `print` until exit. The first Honey Tribe run logged
nothing for 15 minutes and was indistinguishable from a hang. Set it in the **Dockerfile** and the
job env.

### 3.3 Full crawls do not scale — go incremental
Fine at 1,000 records, fatal at 10,000.

- Fetch only `updated_at_min = now − N days` (30 is a good default) and **merge by the source's
  primary id** into the previous publication (read it back from the bucket).
- Keep the source id on **both** parent and child rows so the merge can rebuild.
- A record that has *become* invalid (refunded, cancelled, unsubscribed) must be **removed**, not
  left stale.
- **Recompute derived state across the WHOLE merged set** — never incrementally. Otherwise a
  returning customer whose first order predates the window is misclassified.
- Keep a `FULL_SYNC=1` escape hatch; use it on the first run.

### 3.4 Accumulate history the API won't give you in one call
Most APIs cap a wide-field pull to a recent window. Each run fetches what it can and **merges older
rows forward** from the previous publication, keyed by a stable composite (e.g.
`(date, campaign, adset, ad)`). The fresh pull stays authoritative for every day it covers, so
restatements land correctly and history grows in the bucket instead of being capped.

### 3.5 IAM: `run.developer`, not `run.invoker`
Triggering a Cloud Run job **with env overrides** needs `run.jobs.runWithOverrides`, which
`roles/run.invoker` does **not** carry. An invoker-only grant 403s every tick while the policy looks
correct — this left riverdance stale for 13 days.

### 3.6 The esprima gate
Inline dashboard JS is parsed by `tools/_validate_dash_js.py` (esprima 4.x): **no `?.`, no `??`** —
use classic `&&` / `||`. Run it before every deploy.

### 3.7 Windows + UTF-8
Never write JSON payloads in text mode on Windows — cp1252 turns a smart quote into `0x92` and the
UTF-8 read blows up. Write bytes. Secret temp files: UTF-8, no BOM, no trailing newline.

### 3.8 Small ones
- `/healthz` is intercepted by Google's frontend on Cloud Run and 404s before reaching the
  container. Harmless — test an unknown path and confirm you get *Flask's* 404 instead.
- Import `jsonify` if you use it: a route referencing a missing name imports fine and 500s on call.
- Cloud Run caps fixed-length responses at ~32 MiB; stream chunked above that.
- Check which git remote you're on. `Agora-Data-Driven/atrium` (active) and
  `Agoradatadriven/Agora-Data-Driven-Portal` (mirror, stale) share history and look identical.
  Confirm with `gh api repos/<owner>/<repo> --jq .permissions.push` before assuming a 403 is a
  credential problem.

---

## 4. Make the dashboard honest about time

**Anchor relative date presets to the latest date IN THE DATA, not `today`.** Feeds lag, and
different feeds lag differently. "Last 30 days" against `today` on a lagging feed renders an empty
dashboard and looks broken.

Show in the header **when the data was generated** ("Updated 20 min ago") and **what it covers**
("Data through Jul 26"), and turn the latter red past a threshold. A dashboard that can't tell you
it's stale is worse than none.

Give a **Sync** button: trigger the export job if it can, else re-fetch the payload; always show a
spinner and update the timestamp. If the endpoint is un-gated, add a **cooldown keyed on the data
object's age** so repeat clicks can't run up paid API calls.

---

## 5. Per-connector appendix — read only what you're using

### 5.1 ActiveCampaign — *already built, reuse it*
`clients/client_riverdance/job/activecampaign.py` is a complete, production REST v3 pull, and
`ACTIVECAMPAIGN_TAB_GUIDE.md` is a 973-line guide to the dashboard tab it feeds. **Start there —
do not rewrite.**

- Config: `ACTIVECAMPAIGN_URL` (account base, `https://<account>.api-us1.com`) +
  `ACTIVECAMPAIGN_API_KEY` (the Api-Token, from Secret Manager, mounted as env).
- Returns `{enabled, account, url, fetched, crm_enabled, error, totals{...}, campaigns[], lists[],
  automations[]}` — campaigns are **sent only**, newest first.
- **Everything is best-effort.** A missing key, a network error, or a disabled feature (the CRM is
  commonly off) degrades to a partial block with an `error` string. The tab renders a
  "not configured" state rather than breaking.
- Pagination is `limit`/`offset` with a `meta.total`; cap the page count so a runaway account can't
  hang the job.
- Metrics worth surfacing: recipients, opens, clicks, **click-to-open rate**, and — the ones clients
  actually act on — **unsubscribes and bounces** (deliverability/spam signals). Plus lists +
  subscriber counts, and automations (entered / completed / completion %).
- Rates need a denominator guard: an unsent or zero-recipient campaign must not produce `NaN`/`Inf`.

### 5.2 Meta ads via Windsor
- The `all` endpoint accepts a **`date_preset` only** — `date_from`/`date_to` and `maximum` return
  400. It also **caps the response**: the full ~19-field list works at `last_365d` but 400s at
  `last_730d`, while a two-field query works at `last_1095d`. **Test with your real field list.**
  Because of the cap, history must accumulate (§3.4).
- **Breakdowns are separate pulls.** Meta will not return age/gender/region alongside per-ad/day
  rows, and **rejects revenue fields on a breakdown query** — breakdowns carry delivery metrics only
  (spend / impressions / clicks / link clicks / reach). Use a shorter window for per-day breakdowns;
  roll region to month or the payload explodes.
- **Reach is not additive across days.** Summing it and dividing impressions by it understates
  frequency — use the mean of Meta's own per-row `frequency`.
- Definitions verified against a client's own report: `CTR = link clicks / impressions`,
  `CPM = spend / impressions × 1000`, `conversion rate = purchases / link clicks`,
  `ROAS = value / spend`.

### 5.3 Shopify (only if the client is on Shopify)
- Admin REST orders: paginate via the `Link` header; the CSV export instead repeats the order header
  **only on the first row** of a multi-line order — forward-fill before aggregating.
- Derive first-time/returning at **order** level, not per line. A row-wise `cumcount` mislabels the
  2nd..nth line of one order as "returning".
- ShopifyQL (`sessions`) returns `tableData.rows` as **objects keyed by column name**, not row
  arrays. Zipping against `columns` silently yields rows of literal header text — 580 rows of
  garbage shipped before an audit caught it. Handle both:
  `dict(r) if isinstance(r, dict) else dict(zip(cols, r))`. It is month-granular and needs a plan
  exposing the Analytics API.
- **Self-referrals are not traffic.** A store's own second domain appears as a referrer;
  `shopmidgetgiraffe.com` vs the `midget-giraffe` handle — the hyphen defeated a substring match and
  misattributed **1,412 orders and 5,381 sessions**. Read real domains from `shop.json` at runtime,
  strip punctuation before comparing, collapse own domains *and* on-site apps (wishlist, reviews,
  checkout) to `direct`, and route **all** sources through one shared normaliser so any
  "visitors vs buyers" chart is comparable.

### 5.4 Any new connector — the checklist
Best-effort wrapper · secret from env with Secret-Manager fallback, never logged · lazy import of
heavy SDKs · pagination cap · timeout on every call · rate-limit retry with backoff · normalise
identifiers into the shared vocabulary · return `{enabled, error, ...}` · document the returned shape
in the module docstring, matched **by name** to the `DATA.*` keys the dashboard reads.

---

## 6. Metric definitions — reverse-engineer, then challenge

The client's existing report is the spec **and** the trust baseline. Recompute its headline numbers
and match them before adding anything: Honey Tribe's `$1.18M` matched exactly, which bought
credibility for everything after.

**When a definition is ambiguous, show both and ask.** Their report said "AOV $211.18" — sales ÷
*customers* — while its own weekly card used sales ÷ *orders*. We shipped both, flagged it, and the
client chose ÷ customers, because the metric exists to compare first-timer against returning spend.
Guessing would have been wrong half the time on the number they look at most.

Write the agreed definitions into the client's `README.md` as a table.

---

## 7. Interaction patterns — the toggles

Build these as a small reusable vocabulary; one `segWire(id, attr, apply)` helper wires every
segmented control on the page.

**Segmented control (`.seg`)** — pill group, one active. Use for mutually exclusive *views*:
`Auto / Week / Month` (grain), `Relative / Absolute` (axis), `By platform / Over time`,
`Ranked / By month`, `Order history / Curated sheet`. Give it a tiny uppercase `.cap` label
("AXIS") when the meaning isn't obvious from the options.

**Scorecards as series toggles** — the strongest pattern we built. Click a KPI tile to add/remove
its line from the chart beneath it. Rules that make it work:
- The tile's accent bar and dot **are** the line colour. No legend lookup needed.
- Inactive tiles dim (~55% opacity, muted value, hollow dot) but stay readable.
- **Never allow an empty chart** — block deselecting the last active series.
- Mirror the toggles in a legend below, so both routes work.

**Absolute / Relative axis** (from the resetdata dashboard):
- **Relative** — index every series to its own peak on a shared 0–100% axis, so metrics on wildly
  different scales can be compared *by shape*. Tooltips still show real values plus "(N% of peak)".
- **Absolute** — real values on a **dedicated axis per family**. Counts left, money right. Add a
  third axis for *rates*: on a totals axis a ~$230 AOV is a flat line under a ~$5K sales peak.
  Distinguish which axis a line belongs to with dash patterns (solid / long-dash / dotted).
- Print a one-line explanation under the chart that changes with the mode.

**Cross-filtering** — any visual element that represents a category should filter the tab:
map tile, donut slice, stacked-bar month, table row. Rules: **clicking the same element again
clears it**; show the active filter as a **chip with an ×**; dim rather than hide the unselected
(context is the point); and keep the *source* chart unfiltered so the user can see what they picked
out of.

**Filter bar** — collapsible ("tuck"), because filters shouldn't own the screen. Collapsed, keep the
one-line summary of what's selected; persist the choice in `localStorage`; one toggle collapses every
tab's bar. Ours went 130px → 33px.

**Two independent date ranges** — *Period* drives every tile/chart/table; *KPI benchmark* drives only
the average columns. Each needs its own presets **and** its own from/to pickers, the benchmark tinted
differently end-to-end, and a line under the controls stating which drives what. Default the
benchmark to **the whole previous calendar year** — a rolling 12 months over-weights whichever season
it starts in.

**Also**: tab state in the URL hash (linkable, survives refresh) · sortable table headers with an
arrow on the active column · debounce free-text inputs (~220 ms) · one invisible hover band per index
for tooltips rather than per-point hit targets · accordions for "show me everything" (per-month
product lists) with a *Show all N* expander · cap long tables at ~10 rows and say
"Top 10 of 55 — totals below cover all 55".

**Empty states must look wired, not broken.** A not-yet-connected panel should say what it's waiting
for and what enables it, in a soft striped card — never a blank space or an error.

---

## 8. Aesthetics

**Brand tokens in `:root`, nothing hardcoded.** Pull the real hex values from the client's brand kit
(sample the pixels if you only have an image — don't eyeball). Honey Tribe:
`--honey #F6A144 · --flame #EE6839 · --gold #F2B84B · --clay #E2996B · --cocoa #2B1A14 · --cream #FFF6E8`.
Also define `--ink / --muted / --line / --good / --bad`, a radius, and two shadows.

**Typography** — three roles: a display face for headings and numbers (Lato Heavy), a body face, and
an *italic accent* for subtitles and captions (EB Garamond italic) which does most of the work in
making it feel designed rather than generated. Numbers always
`font-variant-numeric: tabular-nums` so they don't jitter when they update.

**Colour strategy — identity vs. encoding.** These are different jobs:
- *Identity* (header, accents, primary actions) = the brand palette, faithfully.
- *Encoding* (series, categories) = must be **distinguishable first**. Five near-identical brand
  oranges are unreadable as lines. Extend with analogous + a couple of cool accents that still feel
  in-family — we used teal `#2F6F6B`, olive `#6E7B3F`, plum `#8C5A7A` alongside the oranges.
- Assign category colours **once, by overall volume**, so a category keeps its colour across every
  filter, tab and date range.
- **A colour must mean the same thing everywhere on the page.** If the Sales scorecard is flame, the
  Sales line is flame.

**Layout** — a max-width wrap (~1400px); cards with generous padding on a warm off-white canvas;
sections separated by a heading + hairline rule, not boxes-in-boxes. Section heads take a bold title
plus an italic subtitle. Keep the first screen useful: scorecards + one chart should fit above the
fold (hence the tuckable filter bar and a ~260px chart, not 450px).

**Charts** — hand-rolled inline SVG, no CDN (the deploy is self-contained and the esprima gate
parses your JS). Specifics that made ours read well:
- Light hairline gridlines only on the value axis; no chart borders; no 3D, no gradients on data.
- `sqrt` widths for funnel stages, or the tail vanishes next to impressions.
- Thresholds as a dashed reference line (frequency 2.5 = fatigue) rather than a colour ramp.
- Bars in a diverging-from-brand ramp for choropleth-style tile maps; `sqrt` the scale so
  mid-volume values stay visible.
- Dark tooltip with a brand-coloured title, one row per metric, right-aligned tabular values.
- Label the axis units on the axis, not in a legend.

**Density and hierarchy** — one big number per card, a label above, a supporting line below. Resist
adding a fourth number. Use muted colour for anything the client won't act on.

**Insight strips.** Every tab ends with a "Reading of the…" row of small cards that state findings in
plain language and recompute with the filters ("Consideration is the weak link — CTR is 5.33% against
a KPI average of 6.00%"). This is the single highest-value thing you can add: it turns a report into
an opinion, and it's what gets quoted back to you in meetings.

---

## 9. Audit before calling it done

Write a script that recomputes everything **independently of the dashboard** and cross-checks the
source API. It caught two real bugs on Honey Tribe (§5.3) that looking at the screen never would.

- **Freshness** — lag vs today, per feed.
- **Row counts vs the source API**, with every difference explained (ours 9,222 vs API 9,333 = 51
  cancelled + 60 refunded).
- **A single-day spot check** against the API.
- **Referential integrity** — every child row resolves to a parent; dates/types agree.
- **Derivation correctness** — 0 records misclassified.
- **Monotonic funnels** — impressions ≥ link clicks ≥ purchases.
- **Cross-source totals agree** where they should.
- **PII leak check** — 0 emails, no token prefixes, no phone patterns, ids are hashes.

Record known-and-accepted quirks explicitly so they aren't re-investigated later.

---

## 10. Verify in a real browser, then deploy

Drive the page with Playwright (`channel:'chrome'` — no browser download) and assert every filter,
cross-filter, sort and tab switch, plus **zero `pageerror`/console errors**. Screenshot each tab and
actually look at it: two Honey Tribe bugs were only visible that way (Weekly and MTD cards showing
identical values; a mis-scaled axis). **Re-run the same checks against the live URL after deploying**,
and assert the auth posture in both directions.

Deploy with the standup script (`deploy_honeytribe.ps1` is the template) — idempotent: APIs →
Artifact Registry + private bucket → SAs + least-privilege IAM → secrets → export job (build, deploy,
first run) → `run.developer` grants → scheduler → dash service.

- **Build as yourself; never Cloud Build from a laptop** (the Cloud Build SA cannot `actAs` the
  runtime SA). `gcloud builds submit --tag` for the image only is fine.
- **Never `--allow-unauthenticated`** — org policy forbids it. Use `--no-invoker-iam-check` and do
  auth in-process.
- **The bucket is always private.** `DASH_OPEN` only controls whether the *service* asks for a
  password. Open is right when embedded in the gated Atrium workspace (a login inside the iframe is a
  dead end) — but tell the client plainly that anyone with the URL can then read it: a Cloud Run URL
  is unguessable, not secret.
- **Rotate any credential that arrived in a shared document** once live, then re-run the standup.

---

## 11. Ask the client up front

1. **Access to the existing report** (view access beats screenshots) + which page matters most.
2. **Which metrics they run the weekly meeting on**, and the exact definition of each ambiguous one.
3. **Credentials** — but check what's already in the handover first; they're often there.
4. **Account identifiers** — ad account id, ActiveCampaign account URL, store domain *and any
   additional storefront domains*.
5. **Brand kit** — logo (vector if possible), colours, type.
6. **Targets** — budget, target ROAS/CPA/open-rate — so the dashboard can say "ahead" or "behind"
   instead of only reporting numbers.
7. Anything that maps their internal taxonomy (product catalogue export, campaign naming convention).
