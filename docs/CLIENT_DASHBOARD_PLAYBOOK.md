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
| **Lead-gen client (CPL, no revenue)** | [`clients/client_RHE/`](../clients/client_RHE/) — Meta ×3 accounts + ActiveCampaign |
| **ActiveCampaign connector (done)** | [`clients/client_riverdance/job/activecampaign.py`](../clients/client_riverdance/job/activecampaign.py) — extended in [`client_RHE`](../clients/client_RHE/job/activecampaign.py) with contacts/quiz + per-day sends and events |
| **ActiveCampaign dashboard tab (done)** | [`clients/client_riverdance/ACTIVECAMPAIGN_TAB_GUIDE.md`](../clients/client_riverdance/ACTIVECAMPAIGN_TAB_GUIDE.md) |
| **Day/Week/Month grain on every chart** | [`clients/client_RHE/dash/dashboard.html`](../clients/client_RHE/dash/dashboard.html) (`GRAIN` + `grainOf` + `segWire`) |
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
   first-time/returning. Then *assert* it in the audit (§8). **Hashing the person is not enough:
   free text authored in the source can carry PII too.** On RHE a live subject line read
   `"Did you get the Warragul brochure, j.smith@example.com" (address anonymised here)` — a merge tag fell back to
   the recipient's email instead of their first name, and that address rode into the payload
   inside a *template name*. Only the audit caught it. Run every source-authored string (subjects,
   campaign/list/automation names, free-text custom fields) through one `redact()` that strips
   email and phone **patterns** — not a length heuristic; the original "strip a short trailing
   name" rule let a 27-character address straight through. Re-redact carried-forward values on
   merge, or a publication written before the fix keeps re-publishing the leak.
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

### 3.2 Thread the local preview server
`socketserver.TCPServer` serves **one request at a time**, so a single browser holding a keep-alive
connection — or a client that walks away mid-download of a multi-MB payload — wedges it forever.
The failure is nasty because it does not look like a failure: the port still reports LISTENING and
new connections still reach ESTABLISHED, they just never receive a byte. Use
`ThreadingTCPServer` with `daemon_threads = True`, and test it with concurrent requests
(one page load plus one full `data.json` download) rather than one at a time.

### 3.3 Buffered stdout makes a job look hung
Without `PYTHONUNBUFFERED=1` Python holds every `print` until exit. The first Honey Tribe run logged
nothing for 15 minutes and was indistinguishable from a hang. Set it in the **Dockerfile** and the
job env. Same applies to any long-running script you background: `print(..., flush=True)`, or its
startup banner never appears and a healthy process looks dead.

### 3.4 Full crawls do not scale — go incremental
Fine at 1,000 records, fatal at 10,000.

- Fetch only `updated_at_min = now − N days` (30 is a good default) and **merge by the source's
  primary id** into the previous publication (read it back from the bucket).
- Keep the source id on **both** parent and child rows so the merge can rebuild.
- A record that has *become* invalid (refunded, cancelled, unsubscribed) must be **removed**, not
  left stale.
- **Recompute derived state across the WHOLE merged set** — never incrementally. Otherwise a
  returning customer whose first order predates the window is misclassified.
- Keep a `FULL_SYNC=1` escape hatch; use it on the first run.

### 3.5 Accumulate history the API won't give you in one call
Most APIs cap a wide-field pull to a recent window. Each run fetches what it can and **merges older
rows forward** from the previous publication, keyed by a stable composite (e.g.
`(date, campaign, adset, ad)`). The fresh pull stays authoritative for every day it covers, so
restatements land correctly and history grows in the bucket instead of being capped.

### 3.6 IAM: `run.developer`, not `run.invoker`
Triggering a Cloud Run job **with env overrides** needs `run.jobs.runWithOverrides`, which
`roles/run.invoker` does **not** carry. An invoker-only grant 403s every tick while the policy looks
correct — this left riverdance stale for 13 days.

### 3.7 The esprima gate
Inline dashboard JS is parsed by `tools/_validate_dash_js.py` (esprima 4.x): **no `?.`, no `??`** —
use classic `&&` / `||`. Run it before every deploy.

### 3.8 Windows + UTF-8
Never write JSON payloads in text mode on Windows — cp1252 turns a smart quote into `0x92` and the
UTF-8 read blows up. Write bytes. Secret temp files: UTF-8, no BOM, no trailing newline.

### 3.9 Small ones
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
  hang the job. **`limit` is hard-capped at 100** — 200/500/1000 all silently return 100.
- **Campaigns are usually a minority of the volume.** On RHE only 11 of 312 campaigns were ever
  sent (14,976 sends) against 54,243 `emailActivities` — the rest is automation mail. A
  campaign-only backbone understated sends 3.5×. Use `/emailActivities` for the send series.
- **Only `after=<ISO8601>` narrows `/activities`.** Every `filters[...]` form is silently ignored
  and returns the full unfiltered total. `/emailActivities` ignores `filters[tstamp][gt]` too, but
  honours `orders[tstamp]=DESC`, so page it newest-first and stop at a watermark.
- **Do not assume an event type means what its name suggests — and sanity-check the RATIO.**
  RHE's `/activities` stream has `referenceModelName == "log"`, which looks like an engagement log
  and is actually the **send/delivery record** (`/logs` totals 54,304 against `/emailActivities`'
  54,243; a `/logs/<id>` row carries `sendid` and `successful: 1`). Mapping it to "open" produced a
  **100.00% open rate** — opens exactly equalled sends. That equality is the tell: **any rate that
  lands on exactly 100% is a misclassification, not a triumph.** Assert it in the audit.
  **Verify every event type against an aggregate you already trust, and keep going until the ratios
  are sane.** It took three attempts on RHE: `link-data` looked like a click but is an **open**
  (6,070 rows against 7,765 campaign opens, versus 133 actual clicks — 45x off), and calling it a
  click produced a 21.78% "click rate" where the campaign aggregate said 0.69%. Meanwhile
  `mpp-link-data` (Apple Mail Privacy prefetches) is on its own endpoint and is **not a subset** of
  opens, so it must be reported as its own line, not as a share. And clicks turned out not to be in
  the stream at all — they exist only per campaign, so every click figure had to be scoped to
  broadcasts with the UI saying so. Version the stored shape (`DAILY_VERSION`) so a publication
  written under an earlier, wrong meaning is migrated rather than silently mixed with corrected
  numbers, and make the audit assert the ratio bounds so a future regression fails loudly.
- **Page event streams NEWEST-first, and verify which end you are reading from.** `/activities` is
  oldest-first by default. This cost two full rebuild cycles on RHE: paging from offset 0 returned
  2025 events and nothing recent; adding an `after=<floor>` cold start was still oldest-first
  *within* the window, so the page cap covered two old months and left the latest two empty. Both
  times the dashboard's default view showed **zero opens** and looked like real data.
  `orders[tstamp]=DESC` **is** honoured even though every `filters[...]` is ignored — so walk back
  from newest and stop at the watermark. Record the oldest timestamp reached so the history
  horizon can be stated rather than guessed. **General rule: when a paginated source is capped,
  make sure the cap costs you the OLDEST data, never the newest.**
- **A long crawl earns you a 503, and "best-effort" will hide it.** After 543 send pages RHE's
  event leg 503'd on its first request; the wrapper swallowed it and published a year of
  `opens: 0` that looked like real data. Retry `429/500/502/503/504` with backoff (honour
  `Retry-After`), **return the pages already fetched** rather than raising out of the whole leg,
  and make the audit FAIL on a non-empty `error` field or on "sends but zero opens". A
  best-effort source that degrades silently to zeros is worse than one that fails loudly.
- **`/contacts?include=fieldValues`** returns custom-field answers alongside the page (~305 per 100
  contacts) — one cheap 49-page pass replaces a per-contact fan-out. Contact rows also carry
  `sentcnt`, `last_open_date`, `last_click_date` and bounce counters.
- **`automation_name` is always null** on emailActivities — the SUBJECT LINE is the only template
  identifier, so group templates by a normalised subject.
- **Watch for double-coded custom-field values.** A Meta lead form writes the raw machine value
  (`some_experience`) while the AC dropdown stores the display label (`Some experience`). Left
  alone every segment chart splits in two. Normalise against the field's own
  `/fields/<id>/options` labels, and assert in the audit that no value is double-coded.
- Metrics worth surfacing: recipients, opens, clicks, **click-to-open rate**, and — the ones clients
  actually act on — **unsubscribes and bounces** (deliverability/spam signals). Plus lists +
  subscriber counts, and automations (entered / completed / completion %).
- Rates need a denominator guard: an unsent or zero-recipient campaign must not produce `NaN`/`Inf`.

### 5.2 Meta ads via Windsor
- The `all` endpoint accepts a **`date_preset` only** — `date_from`/`date_to` and `maximum` return
  400. It also **caps the response**: the full ~19-field list works at `last_365d` but 400s at
  `last_730d`, while a two-field query works at `last_1095d`. **Test with your real field list.**
  Because of the cap, history must accumulate (§3.4).
- **The window cap is caused by specific FIELDS, not the field count.** On RHE the 400 came
  entirely from the two `unique_actions_*` fields ("breakdowns for unique-count fields are only
  available for…"). Dropping them let the same 18-field pull reach `last_1095d` — 2.5 years of
  real history instead of one. **Split the pull**: a deep one without the unique fields, and a
  shallow one with them, joined on `(date, campaign, ad)`. Unique counts are not additive, so
  attach each group's value to its largest row and zero the rest.
- **Sum on key collision, never overwrite.** Windsor returns rows that share even
  `(date, account, campaign, adset, ad)`, differing only on a descriptive field. Overwriting them
  silently dropped $321 of spend on RHE; the audit caught it against the lifetime total.
- **A field can be returned and always empty.** `unique_actions_lead` is present but identically 0
  account-wide while `unique_actions_link_click` works. Render that as **n/a**, never `0` — `0`
  reads as "we got none", which is a lie.
- **Breakdowns do not all carry the same metrics.** On RHE, age × gender returns leads and
  reconciles exactly to the main pull, but the **region breakdown returns leads identically 0** on
  every field combination — so a "CPL by state" map is not buildable, however reasonable the ask.
  Check each breakdown for what it actually populates and flag it in the payload
  (`has_leads: false`) so the dashboard can hide the cuts it cannot support.
- **A client with several ad accounts may have several BRANDS.** RHE's three accounts are three
  different businesses; blending them by default hid that one had a much worse CPL. Carry the
  account on every row and make the toggle first-class. Also check more than one window before
  concluding an account is dead — two of the three looked empty at `last_30d` and were merely
  dormant.
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
`Auto / Day / Week / Month` (grain), `Relative / Absolute` (axis), `By platform / Over time`,
`Ranked / By month`, `Order history / Curated sheet`. Give it a tiny uppercase `.cap` label
("AXIS") when the meaning isn't obvious from the options.

**Time grain on EVERY time-series chart** — `Auto / Day / Week / Month`, re-bucketing the current
date-range selection. Make it a shared helper (`grainOf(key, from, to)` + `bucketOf(iso, grain)`),
not per-chart logic, so every chart behaves identically and a new chart is one line.
- **`Auto` must scale with the span**: day ≤ ~62 days, week ≤ ~400, month beyond. Without this a
  7-day range bucketed by week renders **a single dot** — which is exactly the bug we shipped and
  had to fix. Never hardcode a grain.
- Say the resolved grain in the subtitle ("29 days in the selected period") so the user knows what
  they're looking at.
- Bar charts need room: clip to the most recent ~40 buckets and label it, rather than drawing 1,100
  slivers.
- **Do not offer a grain the data cannot support.** Shopify sessions are month-granular, so that
  chart stays monthly — offering Day there would fabricate precision.
- **Grain is sticky, so guard it.** If an explicitly chosen grain would draw too many buckets for
  the new span (Day + All time = ~2,100 points), step to the next coarser one and let the subtitle
  report what was actually used. Don't silently reset the user's choice either.
- **Zero-fill the buckets.** A day with no orders must plot as a zero, not vanish — otherwise a
  7-day week renders 6 points and the quiet day silently disappears, which on a weekly review is
  the opposite of useful. Cap the fill (~400 buckets) so forcing Day over years can't explode.
- **Label day buckets with the weekday** ("Mon 20 Jul"), and the full name in tooltips. "Was that
  spike a Monday or a Saturday" is usually the actual question.

**Match the client's reporting ritual with one button.** Ask how they present, then build that
exact view as a control. Honey Tribe presents every Monday: last week against the week before.

`Weekly review` is a **button that becomes the thing it does**. Collapsed it is one button; clicking
it *arms the reporting view* and replaces itself with the two weeks being compared —
`2 weeks ago | 1 week ago`, ordered left-to-right in time. Picking either sets the period to that
complete Mon–Sun week **and** the benchmark to the week before it, plus day grain, so every number
on screen is week-over-week and you can step between weeks live while presenting.

Four subtleties that make it correct:
- Snap to **complete ISO weeks** — walk back to the most recent Sunday the data actually covers, so
  a partial current week never sneaks in.
- Make "previous week" relative to the **selected period start**, not to today, so the pairing
  follows the period wherever it moves.
- **Don't hijack the rolling presets.** `7d` should stay a rolling 7 days; the weekly control is a
  separate, explicit thing.
- Selecting an ordinary preset should drop the pair's highlight but **leave it on screen** — it is
  the control they reach for, not a mode to be exited.

⚠️ `[hidden]` loses to an explicit `display:` in author CSS. A control styled
`display:inline-flex` will render even with the attribute set — add `.thing[hidden]{display:none}`.
Ours shipped visible-from-load until a screenshot caught it.

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

- **A client key with UPPER CASE breaks the derivation.** GCS bucket names, Cloud Run service and
  job names, and service-account ids are all **lowercase-only**, so `<c>` = `RHE` fails at the
  first bucket create (`Invalid bucket name: 'agora-data-driven-RHE-dash'`). Derive every cloud
  resource from a lower-cased key and keep the original casing only for display and the data
  object. Cheapest fix of all: **choose a lowercase key**.
- **`--set-env-vars` splits on commas, so a value that CONTAINS commas breaks it.** RHE passes
  three Windsor ad-account ids in one variable and gcloud read the second as a stray key
  ("Bad syntax for dict arg"). Switch the delimiter with a leading `^@^`:
  `--set-env-vars "^@^K1=v1@K2=a,b,c"`. Single-account clients never hit this, so it will surprise
  you on the first multi-value client.
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
