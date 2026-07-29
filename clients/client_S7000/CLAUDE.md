# CLAUDE.md: clients/client_S7000 (Campaign Uptime Monitor: INTO + Service 7000)

**Rules live in the repo-root [`/CLAUDE.md`](../../CLAUDE.md)**: read it first; this file only
adds local context. If they disagree, root wins.

This client folder is **not a performance dashboard**. It is a **campaign uptime monitor** for two
unrelated Swiss companies, built from the handover brief in `INTO/s7000_ into.docx`. The client's
problem is that campaigns have been switched off when they weren't meant to be and nobody noticed.
Status-first, traffic lights, verdict above the fold, metrics deliberately light. **Resist scope
creep into an analytics suite**, it buries the one signal the client wants.

Two-stage contract (there is no BigQuery dataset and no SQL views, the [`client_riverdance`](../client_riverdance/)
pattern, not [`client_template`](../client_template/)):

```
job/build_local.py  (data dict key)  ->  dash/dashboard.html  (DATA.* key)
```

## The one requirement that outranks everything else

**INTO and Service 7000 must never see each other's data.** One pipeline writes **three separate
objects**: `internal.json`, `into.json` and `service7000.json`. **Three** Cloud Run services each
pin `DATA_OBJECT` to one of them. Never ship a combined payload and filter in the browser: the full
thing stays visible in devtools and network logs, which is a data leak, not a cosmetic issue.
`build_local.scope_payload()` is the isolation boundary and `verify_isolation()` greps each client
payload for the other client's account id, name and campaign names before anything is written.
`deploy_s7000.ps1` adds a fourth layer: each scope's SA gets `objectViewer` with an **IAM condition**
on `resource.name`, so even an app bug cannot read the other payload.

## status ≠ delivery: the whole point

`campaign_status = ACTIVE` does **not** mean a campaign is running. It can report ACTIVE and deliver
nothing (ad set paused, ads rejected, lifetime budget exhausted, end date passed, account payment
failure, audience too small). The reliable signal is the **spend heartbeat**, and every verdict in
`dashboard.html` is built on it (`heartbeat()` → `verdictOf()`). `causesOf()` walks the
campaign → ad set → ad hierarchy to name *why* it isn't delivering, that diagnosis is the thing this
tool adds over Ads Manager.

## Five traps that are load-bearing: do not "simplify" these

- **`PULL` is the clock, not the browser.** Every "today", "last 24 hours" and "days remaining" is
  measured from `pull.last_success`. The data can never be fresher than the pull that produced it,
  and a viewer with a wrong system clock must not be able to invent an outage or hide one.
- **A pipeline failure must never render as dead campaigns.** When the pull is stale, rules 1 and 3
  (delivery judgements) are **suppressed**, rule 2 (a status fact from the snapshot) still fires,
  zero-spend rows go to `unverified` slate instead of red, and the verdict switches to "Can't
  verify". Confusing those two states once destroys trust in the tool, and sends a false panic
  email to the agency. `job/build_local.py --stale` exists to test exactly this.
- **`budget_remaining` means different things by budget type.** On a **lifetime** budget it is the
  remaining lifetime amount; on a **daily** budget it is what is left *today* and it resets every
  midnight. Only `lifetimeEntities()` may be used for exhaustion projections. The campaign table
  deliberately prints `n/a · daily` rather than the number, because printing it invites the mistake.
- **The midnight grace window is not optional.** Just after local midnight nothing has spent yet
  today, so inside `heartbeat_grace_hours` (6) yesterday's spend also satisfies the heartbeat.
  Without it every campaign flags critical at 00:05 every night.
- **Frequency is never summed**: it is impressions ÷ reach. Reach does not sum across dates either,
  so the range denominator is *summed daily reach* and every surface that shows it says so. The
  fatigue chart plots **two** lines (portfolio and worst single campaign): a portfolio average of
  fourteen campaigns at 1.2 and one at 5.2 is a flat 1.2 line that never crosses the threshold,
  which would report "no fatigue" while a campaign visibly burns out.

## Flags are evaluated in ONE place

`evalFlags()` in `dashboard.html` owns all ten rules, driven by the `thresholds` block in the
payload, so the rendered verdict and any future alert email read the same numbers. Upstream
contributes only `flag_state[<rule>:<entityId>]`, the **first-detected** timestamps, which a single
snapshot cannot know. Rule 4 dedupes an ad-set end date that merely mirrors the campaign's, and
`lifetimeEntities()` dedupes a campaign-budget-optimisation budget reported at both levels.
Without those, the same finding appears two or three times and the list looks padded.

## Brand: one file, three themes

`THEMES` in `dashboard.html` carries the brand slots for `into` / `service7000` / `internal`;
`applyTheme()` rewrites the CSS custom properties and lazily loads **only that theme's** fonts.
**The traffic lights (`--ok/--warn/--crit/--stale/--off`) are FIXED across all three themes.**
A client who learns "red means stopped" must not have red mean "Service 7000" on one route. Service
7000's brand red `#E30613` is therefore used for nothing but the logo. Colour is never the only
signal: every status carries a distinct glyph and a word. Logos are cropped from the supplied brand
boards into `job/assets/` and inlined as data URIs.

## Locale

CHF with the Swiss apostrophe separator (`de-CH` → `1'234.50`), all timestamps in
**Europe/Zurich** via `Intl`. ⚠️ **Both ad accounts' reporting timezones are still unverified in Ads
Manager.** Meta sets it at account creation and agency-created accounts are often UTC or
America/Los_Angeles; if it isn't Europe/Zurich the heartbeat throws midnight false alarms. The
dashboard surfaces the doubt as a header chip rather than hiding it, clear
`timezone_verified: false` once checked.

## Service 7000's campaign is RECRUITMENT

Label its conversion **Applications** throughout; never let it inherit a service-lead template.
Where a campaign's objective has no resolvable lead action (Traffic, Messages), the dashboard shows
**"not tracked in Meta"**, never a zero that looks like failure.

## ⚠️ `INTO/` is git-ignored and credential-bearing

The handover pack's Windsor connector URLs contain the API key **in plain text**, and it is the same
key used on other client accounts. Rotate it into Secret Manager before wiring the live pull; it must
never appear in the repo, the HTML, or a shared doc.

## Local preview and QA

```powershell
python clients\client_S7000\job\build_local.py            # writes all three payloads
python clients\client_S7000\job\build_local.py --stale    # fake a broken pull (rule 10)
python clients\client_S7000\preview_local.py 8150         # -> /internal/ /into/ /service7000/
```

`preview_local.py` serves the **same three routes** as production, so `data.json` resolves
relatively and the isolation stays server-side locally too. Validate JS with
`tools/_validate_dash_js.py` before any deploy (esprima 4: no `?.`, no `??`).

## Deploy

`deploy_s7000.ps1`: one-shot, idempotent, no dataset and no views. Builds ONE image and deploys
**three** services from it. `-SeedDemoData` publishes the synthetic payloads; `-DashOnly` is the fast
redeploy. Never Cloud Build from a laptop; never `--allow-unauthenticated` (org policy forbids
it, so the app does its own password/SSO auth behind `--no-invoker-iam-check`).

Derived names: bucket `agora-data-driven-s7000-dash` · services `s7000-{internal,into,service}-dash`
· SAs `s7000-{internal,into,service}-web@…` · secrets `s7000-<scope>-dash-{password,session-key}`.

**The live Windsor pull is not built yet**: see [README.md](README.md) "What is left" for the field
list, the two pull cadences, and the open questions that block it. Until it exists, **all three
deployed dashes serve demo-flagged data BY DESIGN** — `job/build_local.py` hardcodes `"demo": True`
(~line 657) and `scope_payload()` propagates it into every per-scope payload (~line 775), which is
what renders the standing "Demo data" ribbon (`dash/dashboard.html` ~line 633) and the
`*** DEMO DATA ***` line in the copy-summary export (~line 2333). That flag is the honesty guard —
do not strip it; it disappears only when a real pull writes payloads without it.

⚠️ **`job/Dockerfile` is a landmine (audited 2026-07-29): its `CMD ["python", "main.py"]` points at
`job/main.py`, which DOES NOT EXIST** — the job folder holds only `build_local.py` (the local demo
builder). Building and running that image today produces a container that exits immediately. It is
the placeholder for the future live-pull job: when you build the real pull, either create
`job/main.py` or fix the CMD — until then do not deploy the job image and expect it to run.
