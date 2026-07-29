# Campaign Uptime Monitor: INTO Schüleraustausch · Service 7000 AG

A monitoring tool, not a report. Built from the handover brief in
[`INTO/s7000_ into.docx`](INTO/), branded from [`INTO/S700_into brandkit.docx`](INTO/).

The client's problem: **campaigns have been switched off when they weren't meant to be, and nobody
noticed.** So the job here is, in order of priority:

1. Confirm at a glance that every campaign that should be running **is running and actually
   delivering**.
2. Flag anything configured in a way that will make it stop later: end dates and lifetime budgets.
3. Simple performance numbers, kept deliberately light.

There is **no alerting** in this build (client decision). The dashboard is therefore the only
detection mechanism, which is why the landing verdict is unmissable and recent changes sit on the
landing page rather than in a tab.

---

## The three routes

One pipeline, three scoped outputs, three services. **The scoping happens server-side.** Each
payload contains only that scope's rows, so a client opening devtools cannot see the other client.

| Route | Serves | Sees | Brand |
|---|---|---|---|
| `s7000-internal-dash` | `internal.json` | both accounts | Agora green/purple |
| `s7000-into-dash` | `into.json` | INTO only | INTO teal `#0A6B63` |
| `s7000-service-dash` | `service7000.json` | Service 7000 only | S7000 blue `#0A4EA3` |

Four independent isolation layers, in `deploy_s7000.ps1`:

1. **Data**: three separate objects; no combined payload is ever published.
2. **Runtime**: `DATA_OBJECT` is an env var, never a request parameter.
3. **IAM**: each scope's service account holds `objectViewer` with an **IAM condition** on
   `resource.name`, so GCS itself refuses a cross-scope read even if the app had a bug.
4. **Auth**: a separate password and session secret per scope; `DASH_OPEN=0` everywhere.

`build_local.verify_isolation()` greps each client payload for the other client's account id, display
name and every campaign name as **raw text** before writing anything. A row-level check would pass
while a stray label in a summary string still leaked.

---

## The core insight the whole build rests on: status ≠ delivery

`campaign_status = ACTIVE` does not mean a campaign is running. It can report ACTIVE and deliver
nothing because:

- the ad set underneath it is paused (campaign ACTIVE + ad set PAUSED = zero delivery)
- the ads inside are paused or were rejected in review
- the lifetime budget is exhausted, so delivery stops silently
- the end date has passed
- the ad account has a payment failure or is disabled
- the audience is too small or the bid too low to win auctions

A dashboard reading only status fields shows all green while nothing spends. The reliable signal is
the **spend heartbeat**:

| Status | Spend in the heartbeat window | Verdict shown |
|---|---|---|
| ACTIVE | > 0 | ✓ **Running** |
| ACTIVE | 0 | ! **Not delivering**: highest priority |
| PAUSED | 0 | ✕ **Off** |
| PAUSED | > 0 | ‖ **Paused today**: spent earlier, check timing |

Expanding any campaign row shows the ad set / ad hierarchy **and a plain-English diagnosis** of why
it isn't delivering. That diagnosis is what this tool adds over Ads Manager: the client already knows
something is off, and what they need is which of the six causes it is.

### The heartbeat window, precisely

Windsor's status pull is **date-granular**, so "spent in the last 24 hours" is realised as **spent
today**, with one deliberate exception. Just after local midnight a healthy campaign has not spent
yet, so inside `heartbeat_grace_hours` (default 6) **yesterday's** spend also counts. Without that
grace window every campaign flags critical at 00:05 every night, which is the fastest way to teach a
client to ignore the tool.

If a true rolling 24-hour window is wanted, the hourly status pull must retain **hourly** spend
snapshots rather than a daily total. That is a pipeline change, not a dashboard change.

### The clock is the pull, not the browser

Every "today", "last 24 hours" and "days remaining" is measured from `pull.last_success`. The data
can never be fresher than the pull that produced it, and a viewer with a wrong system clock must not
be able to invent an outage or hide one.

---

## Rule 10 is the one that protects trust

If the Windsor pull fails, every campaign shows zero spend, and a naive dashboard reports that all
campaigns died when in fact the pipeline broke. So when the pull is stale:

- the verdict switches to slate **"Can't verify, the data is N hours old"**: not green, not red
- **rule 1** (active but not delivering) and **rule 3** (account-wide stop) are **suppressed**
- **rule 2** (switched off) still fires: a PAUSED status is a *configuration fact* from the last
  snapshot and stays trustworthy; "spent nothing" is a *delivery judgement* and does not
- zero-spend rows render **Unverified** in slate, never red
- one rule-10 finding appears, in its own visual language

Test it: `python job/build_local.py --stale`, then reload. The QA harness asserts every line of the
above.

---

## The four tabs

**01 Status board**: the verdict hero, the freshness panel (visually distinct, technical, mono),
account cards, the flag list sorted by severity, recent changes (7 days), and every campaign with
expandable hierarchy + a 30-day heartbeat strip. **Copy summary** puts the whole board on the
clipboard as plain text, which is the manual version of the weekly digest the brief mentions as the
low-effort mitigation if the client changes their mind about alerting.

**02 Scheduled stops**: the client explicitly asked for end-date visibility, so it is a first-class
panel. A **runway** chart (one bar per campaign, today → whatever stops it), then every end date and
lifetime budget at campaign *and* ad-set level, soonest first. **"No end date" renders green**: it
is the desired state. Lifetime budgets get a projected exhaustion date from the trailing spend rate.

**03 Status history**. First the **delivery record**: one cell per campaign per day, green if it spent,
red if it didn't, grey before it started, with an uptime percentage. This is what turns "the campaign
was off again" from an argument into a record. Below it, the chronological transition feed
(timestamp, campaign, from → to, duration off, estimated delivery lost) framed factually as *Status
history*, never "SILENT FAILURE". The client reads this and it reflects on the agency. The
information is identical; the tone is not.

**04 Performance**: light, as requested. Spend, impressions, clicks, CTR, leads/applications, CPL,
CPM, frequency. CTR and conversions over time, frequency against the fatigue threshold. No funnel
breakdown, no creative deep-dive, no demographics in phase 1.

---

## Metric definitions

| Metric | Definition | Note |
|---|---|---|
| CTR | `link_clicks ÷ impressions` | recomputed from summed components, never averaged per day |
| CPM | `spend ÷ impressions × 1000` | as above |
| CPL / cost per application | `spend ÷ conversions` | as above; blank when the action isn't tracked |
| Conversions | `actions_lead` (INTO) / `unique_actions_lead` (S7000) | **events** vs **people**: labelled on screen |
| Frequency | `impressions ÷ reach` | **never summed**; see the caveat below |
| Spend today | since 00:00 Europe/Zurich at the last pull | always a **partial** day |
| Spend 7d | the last 7 **complete** days | today is excluded, so it is comparable |
| Uptime % | days that spent ÷ days since the campaign started | days before the start are excluded |
| Lost delivery | hours off × trailing average hourly spend before the stop | an estimate, not a billed figure |

### Metric traps, and what was done about each (brief §8)

- **Never sum frequency.** Recomputed as impressions ÷ reach everywhere.
- **Reach doesn't sum across dates.** Summing daily reach overstates unique people, so the range
  denominator is explicitly labelled *summed daily reach* and frequency is presented as directional.
  Take a true unique reach from Ads Manager at the level you need it.
- **Never average daily CTR or CPL.** Every ratio comes from summed components: averaging weights a
  quiet Sunday the same as a busy Tuesday.
- **Timezone.** Everything is Europe/Zurich via `Intl`. ⚠️ Both accounts' reporting timezones are
  **still unverified** (see open questions) and the header says so.
- **Attribution lag.** The trailing 7 days are hatched on the charts as *still settling* and never
  used for a trend judgement. The last bucket of a series is usually short *and* inside the window,
  so it dips twice over.
- **`actions_` vs `unique_actions_`.** Carried per account as `conversion_basis` and labelled.
- **Level double-counting.** Metrics exist only at campaign grain. Ad sets and ads carry status
  only, so there is nothing to double-count.
- **Paused campaigns retain historical spend.** They are included and flagged by status; excluding
  them would shrink past totals and break every comparison.
- **`budget_remaining` differs by budget type.** Only lifetime budgets get an exhaustion
  projection. On a daily budget the table prints `n/a · daily` rather than the number, because
  printing it invites exactly this mistake.

---

## The flag rules (brief §4)

Config-driven from the payload's `thresholds` block, evaluated in one place (`evalFlags()`), so the
rendered verdict and any future alert email can never disagree.

| # | Condition | Severity |
|---|---|---|
| 1 | ACTIVE, zero spend in the heartbeat window | 🔴 critical (suppressed if the pipeline is down, or if rule 3 fired) |
| 2 | Status away from ACTIVE | 🔴 critical |
| 3 | Zero spend across **all** campaigns in an account | 🔴 critical, account-level, replaces the per-campaign rule 1s |
| 4 | An end date exists on any campaign or ad set | 🔴 within 14 days · 🟡 otherwise |
| 5 | A lifetime budget exists | 🟡 warning, it will stop eventually |
| 6 | Lifetime `budget_remaining` under 10% | 🔴 critical, imminent silent stop |
| 7 | Under 50% of daily budget for 2+ consecutive complete days | 🟡 warning |
| 8 | Last complete day down >40% vs the trailing 7-day average | 🟡 warning |
| 9 | Frequency above threshold **and** CTR falling | 🟡 warning, fatigue |
| 10 | No data since the last expected pull | 🔴 critical, **pipeline**, visually distinct |

**Expected state is bootstrapped, not configured.** Per the client there is no record of intended
state and every campaign currently live is meant to run **indefinitely**. Every campaign ACTIVE at
the first pull gets `should_be_active: true`, stored as editable config. That inverts two rules:
an **end date is itself a misconfiguration**, not just an approaching one, and a **lifetime budget is
a misconfiguration** because it guarantees a future silent stop. There is no acknowledge/suppress
mechanism in v1, nothing is supposed to be paused, so there are no known-good pauses to filter out.

Upstream supplies only `flag_state[<rule>:<entityId>]`, the first-detected timestamps, which one
snapshot cannot know. Rule 4 dedupes an ad-set end date that mirrors the campaign's, and lifetime
budgets reported at both campaign and ad-set level (campaign budget optimisation) count once.

---

## The data contract

`job/build_local.py` (and, later, `job/main.py`) writes exactly the keys `dash/dashboard.html`
reads. Renaming a key in one stage breaks the other.

```jsonc
{
  "scope": "into",                      // into | service7000 | internal
  "client": "INTO Schüleraustausch",
  "tagline": "the journey starts here",
  "demo": true,                         // renders a standing "Demo data" ribbon
  "currency": "CHF", "locale": "de-CH", "timezone": "Europe/Zurich",
  "generated_at": "2026-07-28T16:13:10Z",
  "brand": { "theme": "into", "mark": "data:image/png;base64,…",
             "lockup": null, "agora_logo": "data:…" },

  "pull": {                             // rule 10's input, its own indicator, on purpose
    "last_success": "2026-07-28T16:13:10Z",
    "expected_every_minutes": 60,
    "grace_multiple": 2.5,              // how late a pull may be before rule 10 fires
    "rows": 1184, "ok": true, "last_error": null,
    "history": [ { "ts": "…", "ok": true, "rows": 1184 } ]   // one tick per expected pull
  },

  "thresholds": { "heartbeat_hours": 24, "heartbeat_grace_hours": 6,
                  "end_date_critical_days": 14, "end_date_horizon_days": 30,
                  "budget_remaining_critical_pct": 10,
                  "underdelivery_pct": 50, "underdelivery_days": 2,
                  "spend_drop_pct": 40, "frequency_warn": 3.5,
                  "recent_changes_days": 7 },

  "accounts": [ { "key": "into", "id": "facebook__1395577394904072",
                  "name": "…", "business": "…", "purpose": "…",
                  "conversion_label": "Leads", "conversion_basis": "actions",
                  "reporting_timezone": "Europe/Zurich", "timezone_verified": false,
                  "payment_issue": false, "disabled": false } ],

  "campaigns": [ {
    "id": "…", "account": "into", "name": "…", "objective": "OUTCOME_LEADS",
    "status": "ACTIVE", "effective_status": "ACTIVE",
    "should_be_active": true,           // bootstrapped from the first snapshot
    "conversion_tracked": true,         // false -> "not tracked in Meta", never a zero
    "conversion_note": null,            // why, shown on hover
    "budget_type": "daily",             // daily | lifetime | adset
    "daily_budget": 45.0, "lifetime_budget": null, "budget_remaining": null,
    "start_time": "2025-09-15", "end_time": null,
    "adsets": [ { "id": "…", "name": "…", "status": "PAUSED", "effective_status": "PAUSED",
                  "budget_type": "daily", "daily_budget": 45.0,
                  "lifetime_budget": null, "budget_remaining": null,
                  "start_time": "…", "end_time": null,
                  "ads": [ { "id": "…", "name": "…", "status": "ACTIVE",
                             "effective_status": "DISAPPROVED" } ] } ],
    "daily": [ { "d": "2026-07-28", "spend": 41.2, "imps": 5120, "clicks": 88, "lclk": 61,
                 "reach": 4310, "freq": 1.19, "conv": 3, "partial": true } ]
  } ],

  "changes": [ { "ts": "…", "account": "into", "level": "adset",
                 "campaign_id": "…", "campaign": "…", "entity": "…",
                 "from": "ACTIVE", "to": "PAUSED", "resolved_ts": null,
                 "hours": 74.0, "lost_spend": 287.4 } ],

  "flag_state": { "r1:<campaignId>": "2026-07-25T14:13:10Z" }
}
```

`daily` carries 90 days. The **last row is always partial** (Meta reports the current day as it
happens) and is marked `partial: true`: nothing compares it to a complete day.

---

## Running it locally

```powershell
python clients\client_S7000\job\build_local.py            # all three payloads
python clients\client_S7000\job\build_local.py --stale    # fake a broken pull -> rule 10
python clients\client_S7000\preview_local.py 8150         # http://localhost:8150
```

The preview serves the **same three routes** as production (`/internal/`, `/into/`,
`/service7000/`), so `data.json` resolves relatively and the isolation stays server-side locally
too. The Sync button really rebuilds (20-second cooldown).

The demo dataset is synthetic and seeded, so two runs differ only in their timestamps, a moving
dataset makes a design review impossible. It is built to exercise **every** state:

- a campaign ACTIVE whose ad set is paused (the silent failure)
- a campaign switched off six days ago, with its change-log entry and lost-delivery estimate
- a lifetime budget at 7% remaining, with a projected stop date
- a **daily**-budget campaign at 10% `budget_remaining` that must **not** be flagged
- end dates 9 days out (critical) and 41 days out (warning)
- two Traffic campaigns that read "not tracked in Meta"
- under-delivery, a 51% spend collapse, creative fatigue, a rejected ad, a one-day historic outage

## Deploy

```powershell
gcloud auth login info@agoradatadriven.com          # tokens expire; this is interactive
.\clients\client_S7000\deploy_s7000.ps1 -SeedDemoData
```

One-shot and idempotent: APIs → Artifact Registry → private bucket → three service accounts →
per-scope secrets → **object-scoped conditional IAM** → one image → three services → verify. It
prints each URL and password. `-DashOnly` is the fast redeploy after a dashboard edit.

Never Cloud Build from a laptop; never `--allow-unauthenticated` (org policy forbids it, the app
does its own password/SSO auth behind `--no-invoker-iam-check`).

## QA

`tools/_validate_dash_js.py` is the pre-deploy JS gate (esprima 4: no `?.`, no `??`). Beyond that,
this build was checked in a real browser across all three routes at 1440×950 and 390×844: isolation
on the wire, zero console errors, every interaction (row expand, finding → campaign jump, sort,
search, tab switch, grain and range switches, copy summary), chart geometry, no `undefined`/`NaN`
leaks in visible text, no horizontal overflow on a phone, and the whole stale-pull matrix above.

---

## What is left

The dashboard, the contract, the three-scope isolation and the deploy are done. **The live Windsor
pull is not**, and it is blocked on decisions rather than code:

> **Status (verified 2026-07-29):** all three deployed dashes (`s7000-internal-dash` ·
> `s7000-into-dash` · `s7000-service-dash`) currently serve **demo-flagged data BY DESIGN** —
> `job/build_local.py` hardcodes `"demo": True` and `scope_payload()` carries it into every scope,
> which is what shows the on-page "Demo data" ribbon and the `*** DEMO DATA ***` line in the copy
> summary. This stays true until the live Windsor pull below is built. Also note:
> **`job/Dockerfile`'s `CMD` references `job/main.py`, which does not exist** (only
> `build_local.py` does) — building that image today yields a container that exits immediately;
> it is a placeholder for the future live-pull job.

### Blocking (brief §10)

1. **Which action carries conversions for each account.** Unresolved. Fastest route: check the
   campaign objective in Ads Manager (Leads + Instant Form → `actions_lead`; Leads + website form →
   a custom conversion or `actions_offsite_conversion.*`; Traffic → there are no leads at all and the
   right metrics are link clicks and landing-page views), check what the "Results" column is called
   per campaign, then empirically run one pull with `actions_lead`, `unique_actions_lead` and the
   offsite fields together and see which returns non-zero.
2. **What Service 7000 counts as an application**: form submit, careers-page visit, or message?
   Recruitment often runs as Traffic or Messages, in which case it is not measurable in Meta at all
   and the dashboard should say so plainly. It already does (`conversion_tracked: false`).
3. **Rotate the Windsor API key.** It was shared in plain text in the supplied connector URLs and is
   the same key used on other client accounts, one leak exposes every client. Secret Manager, read
   at runtime, never in the repo or the HTML.
4. **Verify both ad accounts' reporting timezone** in Ads Manager. A five-minute check that prevents
   a confusing class of midnight false alarm.

### Then, mechanically (brief §5)

Two pull cadences into one job that writes the three payloads:

| Pull | Fields | Cadence | Feeds |
|---|---|---|---|
| Status | account, campaign/adset/ad ids + names + statuses + effective statuses, budgets, budget type, `budget_remaining`, start/end times, spend, **`date`** | hourly | uptime, change log, end dates |
| Metrics | **`date`**, impressions, clicks, link_clicks, reach, frequency, spend, conversions, campaign, adset, ad | daily | CTR, leads, trends |

**`date` is non-negotiable in both.** Without it Windsor returns one aggregated row per campaign
across the whole window: no time series, no heartbeat, no change detection, nothing.

Also needed upstream, and not derivable in the browser:

- **Append a status snapshot on every pull, immutable, with a pull timestamp**, and diff consecutive
  snapshots into the change log. Keep the snapshot table **append-only**: the urge to tidy it is how
  you lose the ability to answer *when* something broke.
- **`flag_state`**: evaluate the same `thresholds` block server-side and persist each condition's
  first-detected timestamp.
- BigQuery tables per the brief: `snapshots_raw` (append-only audit trail), `status_change_log`,
  `daily_metrics` (date-partitioned), `flags`.

### Optional, later

The brief notes the alerting hook: the job already evaluates every rule on a schedule, so adding an
email or Slack send is a small addition rather than a re-architecture. A **weekly digest** is the
lower-commitment version, and it guarantees the dashboard gets looked at. The **Copy summary** button
produces exactly that text today.

⚠️ Google's grounding-style ToS point from the wider repo does not apply here, but one presentation
item does: with no alerting, **detection latency equals how often someone opens the page.** If a
campaign pauses on a Tuesday and nobody looks until Monday, that is six days of lost delivery, the
dashboard will show it accurately, but only after the fact. The end-date panel partly covers this,
because scheduled stops are predictable in advance rather than only detectable afterwards.
