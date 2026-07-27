# Rooming House Expert (RHE) — performance dashboard

Property / rooming-house conversion business in Victoria, Australia. **High ticket, long
consideration cycle**, so nothing here optimises for a direct purchase. The model is:

```
Meta ads (3 ad accounts) → lead magnet (quiz) → ActiveCampaign nurture → warmed lead → buyer
```

That is why there is **no ROAS and no revenue** anywhere in this dashboard. The metrics that matter
are **lead volume, cost per lead, and email engagement as the proxy for sales-readiness**.

- **Pattern:** API-LIVE (same as [`client_honeytribe`](../client_honeytribe/) /
  [`client_riverdance`](../client_riverdance/)). **No BigQuery dataset, no SQL views.**
- **Two-stage contract:** `job/main.py` (data dict key) → `dash/dashboard.html` (`DATA.*` key).
- **Sources:** Meta Ads via Windsor.ai (3 accounts) + ActiveCampaign REST v3.

## Live resources

| Thing | Name |
|---|---|
| **Live URL** | **https://rhe-dash-c732u7m57a-as.a.run.app** |
| Bucket (private) | `agora-data-driven-rhe-dash` |
| Data object | `rhe.json` |
| Export job | `rhe-export` (Cloud Run job) |
| Scheduler | **none** — refresh is the dashboard's **Sync** button (client decision, 2026-07-28) |
| Web service | `rhe-dash` |
| Service accounts | `rhe-dash-job@…` · `rhe-dash-web@…` |
| Secrets | `rhe-dash-password` · `rhe-dash-session-key` · `rhe-windsor-key` · `rhe-activecampaign-key` |

⚠️ **Cloud resource names are LOWERCASE** even though the client key and folder are `RHE` — GCS
buckets, Cloud Run services/jobs and service-account ids reject upper case. `deploy_RHE.ps1`
derives them from `$KEY = $CLIENT.ToLowerInvariant()`; `job/main.py` and `dash/main.py` mirror it.

Stand up / redeploy with **`.\deploy_RHE.ps1`** (idempotent).

**Refresh is manual, by design.** There is no Cloud Scheduler tick — the client asked for refresh
to run off the dashboard's **Sync** button, because every run costs paid Windsor/Meta API calls and
an unattended tick spends money on data nobody asked for. Sync POSTs `/refresh`, which fires the
`rhe-export` job (this is why the web SA needs **`roles/run.developer`**, not `run.invoker`) and
is rate-limited by a 10-minute cooldown keyed on the data object's age, so repeat clicks cannot run
up the bill. `.\deploy_RHE.ps1 -WithScheduler` re-enables the 6-hourly tick; without that flag the
script actively removes any scheduler left from an earlier standup.

## The four tabs

1. **Meta funnel** — Awareness / Consideration / Conversion / Investment, each as period-actuals
   against a KPI benchmark. One combined performance chart carrying **leads, cost per lead, spend
   and link clicks together** (the scorecards above it are the series filter), the funnel as
   **carry-through** with a conversion KPI rail beside it, a campaign table capped at 10, and a
   creative-fatigue table (seen 2.5+ times per person flagged red).
2. **Email performance** — list size; an **all-mail** block (sends, opens, open rate, Apple privacy
   prefetches) beside a **broadcast** block (recipients, open rate, click rate, openers→clickers)
   because those two have different scopes; deliverability; a sends-vs-opens chart with an
   Absolute/Relative axis toggle; weekday send/open windows; the broadcast-campaign table; and the
   automations that carry most of the volume.
3. **Lead magnet & sequence** — the quiz donuts (click a slice to cross-filter the tab),
   open/click rate by investor-experience segment, list growth, and the template/sequence table.
4. **Demographics & placement** — one **Split by** control (age · gender · platform · placement ·
   device) drives four linked views: a **value map** plotting spend against leads with an
   average-cost-per-lead diagonal (anything below the line beats your average), a **ranked cost
   per lead**, **budget share vs lead share** paired bars (wider than tall = overfunded), and a
   full sortable table. Plus a cost-per-lead age × placement cross-tab and geography.

## Global mechanics

- **Two independent date ranges.** *Period* drives every tile, chart and table. *KPI benchmark*
  (tinted brass end-to-end) drives only the right-hand comparison column. The benchmark prefers the
  **whole previous calendar year** — a rolling 12 months over-weights whichever season it starts in
  — but falls back to 365d/180d/all if the scoped account has no data that far back (the RHE account
  only starts 2026-01-01). An empty benchmark range renders "no data in the benchmark range" rather
  than `$0.00`.
- **Day / Week / Month grain on every time-series chart**, driven by the current date range.
  `Auto` resolves from the selected span (≤62 days → day, ≤400 → week, else month) so a 7-day
  range is never a single weekly dot and three years is never 1,100 unreadable points. An
  explicit choice is sticky but is stepped coarser if the span cannot carry it.
- **All three ad accounts are blended into one continuous timeline** (client decision,
  2026-07-28). They are not three parallel businesses — they run almost entirely back to back,
  which is the signature of one advertiser moving between ad accounts:
  Stuart Baker `2024-01-15 → 2025-11-29` ($49,506) · Super Cashflow Development
  `2025-10-16 → 2026-03-29` ($14,231) · RHE `2026-01-01 → 2026-07-27` ($70,918). Blending restores
  a **2.5-year** record and makes the ideal benchmark (the previous calendar year) usable — RHE
  alone has zero 2025 rows. Every row still carries `acct`, the campaign table shows which account
  paid for each campaign, and the `ACCT` constant at the top of the dashboard script isolates one.
- **Every column header on every table sorts.** A table declares `data-sort` and its columns
  `data-k`; `wireSorts()` walks them all, so a new table is sortable with no extra wiring. First
  click on a column ranks it highest-first; clicking the active column flips it.
- **Filter bar tucks away** (persisted in `localStorage`), scorecards double as chart series
  toggles (the tile's bottom accent bar *is* the line colour), table rows and donut slices
  cross-filter with an ×-chip, and every tab ends with a "Reading of the…" insight strip that
  recomputes with the filters.
- Relative date presets anchor to the **latest date in the data**, not `today`.

## Metric definitions (the agreed table)

**Leads vs unique leads.** `actions_lead` counts lead *actions* — one person submitting the form
twice counts twice. `unique_actions_lead` counts distinct *people*. For this account Meta returns
the unique column **identically zero** on every query shape, so the dashboard shows it as **n/a**
rather than a misleading `0`. Everything CPL-related therefore uses `actions_lead`, which is also
what the client's own Windsor URL was already reporting.

| Metric | Definition | Note |
|---|---|---|
| CTR | link clicks ÷ impressions | |
| CPM | spend ÷ impressions × 1000 | |
| CPC | spend ÷ link clicks | |
| CPL | spend ÷ leads | **the headline metric** |
| CTL (click-to-lead) | leads ÷ link clicks | |
| Lead rate | leads ÷ impressions | |
| Frequency | **impression-weighted mean of Meta's per-row frequency** | reach is *not* additive across days |
| Thruplay rate | thruplays ÷ impressions | |
| Open rate (all mail) | opens ÷ sends, from the pixel-read event | ~21.8% |
| **Open rate (broadcasts)** | **openers ÷ recipients, per campaign** | AC's own figure, authoritative (~33%) |
| Click rate | unique clickers ÷ recipients | **broadcasts only** — not in the event stream |
| **Openers → clickers** | **unique clicks ÷ unique opens** | decided 2026-07-27 — see below |
| Apple privacy prefetches | `mpp-link-data` count | machine, NOT part of opens |
| Engaged lead | a contact that has opened ≥ 1 email | the behavioural sales-readiness proxy |
| Converted | ActiveCampaign `Status` = **Client** or **Returning Client** | decided 2026-07-27 |

**KPI normalisation — two types, two rules, never mixed:**

- **Volume** (reach, impressions, clicks, leads, thruplays, sends, opens) → benchmark is the
  **average per week** over the benchmark range.
- **Ratio / cost** (CTR, CPM, CPC, CPL, CTL, frequency, open rate, open-to-click) → benchmark is
  the **aggregate ratio** across the whole range (sum numerator ÷ sum denominator). *Never* the
  average of daily ratios — that is wrong whenever spend is uneven.
- The **delta arrow** on a volume metric compares the period total against what the benchmark's
  weekly rate would have produced over the same number of weeks, so it stays like-for-like.
- Colour respects direction: for cost metrics **down is green**.

**⚠️ Opens and clicks have DIFFERENT scopes, and that is a hard constraint.** In ActiveCampaign's
activity stream: `log` is the send/delivery record (`/logs` 54,304 vs `/emailActivities` 54,243,
carrying `sendid` + `successful:1`), **`link-data` is an OPEN** (a tracking-pixel read — 6,070 rows
against 7,765 campaign opens, versus only 133 clicks), and `mpp-link-data` is an Apple Mail Privacy
**machine** prefetch on its own endpoint, which AC excludes from its open count. **Clicks are not in
the stream at all** — they exist only per campaign. So:

- **Opens** cover ALL mail (~21.8% over 30 days), from the pixel-read event
- **Clicks** are scoped to **broadcasts** only (~0.6%), and the all-mail block shows `n/a`
- **Apple privacy prefetches** are their own line, never a share of opens
- The **broadcast open rate** (~33%) is AC's own openers ÷ recipients, and is authoritative

An earlier build mapped `log` to "open" and reported a **100.00% open rate**. The audit now fails on
opens ≥ sends and on a broadcast open rate ≥ 99.9%, and warns above 90%.

**Openers → clickers was changed deliberately.** The old Looker report showed 125–144% because it
divided *total* clicks by *unique* opens. This dashboard uses unique ÷ unique, which caps at 100%.
Across the broadcasts that is ~2.0% (the old basis would read ~2.6%). Tell the client the number
changed basis — performance did not drop.

## API limits and data gaps (measured 2026-07-27 — do not "fix" these, they are the API's)

- **Windsor `all` takes a `date_preset` only** (`date_from`/`date_to`/`maximum` all 400).
- **The 400 beyond `last_730d` is caused by the two `unique_actions_*` fields.** Dropping them
  lets the main pull reach `last_1095d` — real history back to **2024-01-15**. So the job runs a
  **deep pull (no unique fields, 1095d)** plus a **unique pull (365d)**, joined on
  `(date, campaign, ad)`.
- **`unique_actions_lead` is returned but is identically 0** account-wide, on every query shape.
  `unique_actions_link_click` works fine. The dashboard renders unique leads as **n/a**, not `0`.
- **The region breakdown carries no leads.** `actions_lead` and `unique_actions_lead` are both
  identically 0 across all rows. Geography shows **delivery only** (spend / impressions / reach /
  CTR) — a lead or CPL map is not buildable. Age × gender *does* carry leads and reconciles
  exactly to the main pull, so it is the actionable cut.
- The build brief listed demographics as a **blocker needing a second Windsor destination task**.
  It is not — all five breakdowns work on the existing key today.
- **ActiveCampaign `limit` is hard-capped at 100** on every endpoint (200/500/1000 all return 100).
- **`/activities` ignores every `filters[...]` form** (all return the unfiltered 220,897 total).
  Only `after=<ISO8601>` narrows it, so event ingestion is watermark-based.
- **`/emailActivities` ignores `filters[tstamp][gt]`** but honours `orders[tstamp]=DESC`, so sends
  are paged newest-first and stopped at the watermark.
- **`automation_name` is always null** on emailActivities — the **subject line** is the only
  template identifier, so templates group by a normalised subject.
- **ActiveCampaign CRM is off** (`/deals` → 403 "Upgrade your account"). Degrades cleanly.
- **Only 11 of 312 campaigns were ever sent** (14,976 sends) against 54,243 emailActivities —
  this account's volume is overwhelmingly **automation** mail, so a campaign-only backbone would
  understate sends 3.5×.

## Brand

Applied from `context/RHE Brandkit.docx`. The hex values were **sampled from the pixels** of the
brand board (playbook §8 — the printed labels were too small to read reliably):

| | Hex | Used for |
|---|---|---|
| Purple | `#BE5BF6` | benchmark column, secondary series, accents |
| Green | `#038810` | headings, primary action, "good" |
| Blue | `#153DFD` | series |
| Cyan | `#02B6FD` | series |
| Lilac | `#C064F5` | tint only — too close to purple to use as a series colour |

The logo is cropped from the brand board into `job/assets/rhe-mark.png` (white knocked out,
4× upscaled) and embedded as a data-URI, so the container needs no external asset. It is drawn for
a **light** ground, which is why the header and tab bar are light — a dark chrome fought the mark.
Brand type is Agency Gothic Condensed + Connect; **Oswald** is the closest widely-available stand-in
for the condensed gothic, with Lato carrying the data. Drop a real logo into
`job/assets/rhe-mark.png` and it replaces the monogram crest automatically.

## Open items for the client

2. **Conversion is legitimately zero.** No contact carries `Client` / `Returning Client` yet
   (2,807 `Lead` + 2,016 `Listing Lead`). The tab shows a wired-empty state explaining this rather
   than a broken `0`. It populates as the team tags won business.
3. **The old Colab conversion join was already broken** — it matched leads to a conversions sheet
   on phone number and returned **zero matches**, so the previous report's "Converted" filter
   showed nothing.
4. **Google Sheets are unreachable from Cloud Run.** The Colab reads Facebook Leads (2,171),
   `[Automatic] Conversions` and `Calendly Call` from sheet `1tWAnP…` via *user* OAuth. Share that
   sheet with `RHE-dash-job@agora-data-driven.iam.gserviceaccount.com` and a best-effort connector
   can light it up; until then the funnel join uses the richer AC-side quiz fields.
5. **Rotate both credentials.** The Windsor key and the ActiveCampaign Api-Token were both
   circulated inside a shared document.
6. **Scoped to one ad account.** Stuart Baker ($25k/yr) and Super Cashflow Development ($14k/yr)
   are still pulled and still in the payload, but every tab shows the RHE account only. If those
   two brands should be reported on, say so and it is a one-line change (`ACCT` in the dashboard
   script) — or a second dashboard if they want them separated properly.
7. **Per-template open and click rates** need the send↔event join, which needs a couple more
   scheduled runs to accumulate. Template *volume* is accurate today.

## Local development

```powershell
$env:WINDSOR_API_KEY="…"
$env:ACTIVECAMPAIGN_URL="https://roominghouse.api-us1.com"
$env:ACTIVECAMPAIGN_API_KEY="…"
$env:RHE_LOCAL_OUT="clients\client_RHE\data\RHE.json"
py -3 clients\client_RHE\job\main.py                    # the real pull (~30 min cold, then fast)
py -3 clients\client_RHE\job\audit.py --live            # independent audit (playbook 9)
py -3 clients\client_RHE\preview_local.py 8140          # -> http://localhost:8140
py -3 tools\_validate_dash_js.py clients\client_RHE\dash\dashboard.html
```

**No credentials? Use the fixture.** `job/make_fixture.py` writes a synthetic payload in the exact
published shape — every tab, all five demographic splits, the quiz donuts, the wired-empty
conversion state and the n/a unique-leads row — so the dashboard can be driven in a browser or in
CI without a live crawl:

```powershell
py -3 clients\client_RHE\job\make_fixture.py clients\client_RHE\data\RHE.json
py -3 clients\client_RHE\preview_local.py 8140 --no-open
```

Keep `make_fixture.py` in step with `job/main.py`'s `build()` — if a key moves there and not here,
the smoke test silently stops covering the real shape.

Useful knobs for a bounded local run: `AC_MAX_SEND_PAGES`, `AC_MAX_EVENT_PAGES`,
`AC_EVENT_FIRST_DAYS` (cold-start event window, default 120 days), `AC_FULL_SYNC=1`.
⚠️ The first cold run is long — ActiveCampaign caps `limit` at 100, so the send log alone is 543
pages (~20 min). Later runs read only what is newer than the watermark.

**`context/` and `data/` are git-ignored** — the context drop holds live credentials.
The emitted JSON contains **no PII**: contacts are a salted hash plus an email domain. The audit
asserts it.
