# CLAUDE.md — clients/client_riverdance (Meta via Windsor + ActiveCampaign, Windsor-LIVE)

**Rules live in the repo-root [`/CLAUDE.md`](../../CLAUDE.md)** — read it first; this file only adds
local context. If they disagree, root wins. Dashboard shape:
[`clients/_standard/STANDARD.md`](../_standard/STANDARD.md). Build guidance:
[`docs/CLIENT_DASHBOARD_PLAYBOOK.md`](../../docs/CLIENT_DASHBOARD_PLAYBOOK.md).

> ⚠️ **This file was a verbatim copy of `client_template/CLAUDE.md` until 2026-07-30** — it
> described three SQL views, a `client_riverdance` BigQuery dataset and a `*/10` scheduler, none of
> which this client has. If you acted on it, that is why. **`README.md` and `dash/LIVE_URL.md` in
> this directory are still stale template copies** — do not trust their prose; the facts below and
> the module docstrings in `job/` are the authority until someone rewrites them.

Riverdance RV Resort is a **Windsor-LIVE** client — the original of that pattern, which
[`client_honeytribe`](../client_honeytribe/) and [`client_RHE`](../client_RHE/) copied. There is
**no dataset, no `sql/` directory and no SQL views**: `job/main.py` pulls the Windsor connector API
directly on each run and writes `riverdance.json` itself. Two-stage contract:

```
job/main.py (data dict key)  ->  dash/dashboard.html (DATA.* key)
```

## The two modules

- **`job/main.py`** — the live Meta pull (`connectors.windsor.ai/all`, account
  `facebook__921953393594856`, `WINDSOR_DATE_PRESET` default `last_180d`). It writes
  `{ client, location, dates[], rows[] (per ad per day, `camp` carries the campaign name),
  creatives[] (image inlined), campaign{}, source{}, demographics{}, activecampaign{}, logo,
  agora_logo }`. **Demographic and geographic breakdowns are SEPARATE pulls** — Meta will not
  return them alongside per-ad/day rows. The API key lives only in Secret Manager
  (`riverdance-windsor-key`) and the job env; it is never logged or persisted.
- **`job/activecampaign.py`** — the email add-on, and the ORIGINAL of that connector: `client_RHE`
  extended this module rather than rewriting it, so **fix bugs here, not there**.
  [`ACTIVECAMPAIGN_TAB_GUIDE.md`](ACTIVECAMPAIGN_TAB_GUIDE.md) is the long-form guide to the tab it
  feeds. It is **best-effort**: no key, a network error or a disabled feature degrades to
  `{enabled:false, error:"…"}` and the Email tab renders a "not configured" state — the Meta export
  is unaffected and the job still succeeds.

## Dashboard conventions (keep these when extending)

Two tabs, `ads` · `email`, selected by URL hash. This dashboard was brought onto the **Sales**
standard on 2026-07-30 (`clients/_standard/STANDARD.md` §4); it had drifted furthest of the eight.
What changed, and why each one matters:

- **It had no freshness signal and no Sync button at all.** "Data through" appeared only in the
  footer and there was no generated-at stamp, so a stale payload was indistinguishable from a
  correct one. There are now standard `#updated` / `#thru` ids in the header, `#thru` turns red past
  three days, and `#syncBtn` POSTs `/refresh` then reloads `data.json` **either way**. ⚠️ **This
  service has no `/refresh` route** (`dash/main.py`), which is exactly why the fallback is the
  point — the button reloads the published payload rather than doing nothing.
- **The grain control had no `Auto`,** so a ten-day range bucketed weekly rendered a single dot.
  `Auto` is now the default and `grainOf()` resolves it from the span; an explicit choice is sticky
  but stepped coarser if the span cannot carry it. **The subtitle reports the RESOLVED grain**
  (`Week (auto)`), because "Auto" tells the reader nothing about what they are looking at.
- **Tabs are hash-routed** (`setTab` writes `location.hash`). The Email tab was unlinkable before.
- **There is now a KPI BENCHMARK range** beside the period — `state.bCode` / `benchRange()` /
  `benchRows()`. The four benchmark cards show the published industry figure **and** the client's
  own benchmark window, because the industry number answers "are we normal" and only the client's
  own history answers "are we improving". Reporting just the first tells a client nothing.
  `bCode:"prev"` is resolved, not stored, so "Previous window" follows the period as it moves; a
  hand-typed date pair sets `bCode:""` and pins it.
- **The period presets and the benchmark presets share the `.preset` class**, so the period handler
  is bound to `.preset[data-days]` specifically. Binding it to `.preset` would make picking a
  benchmark silently move the reporting period.
- The filter bar tucks (`.ctuck`, persisted in `localStorage` as `riverdance_tuck`), and nothing is
  visible until the payload has rendered (`#boot` → `#app`).
- `presetRange` / `autoGrain` / `grainOf` / `segWire` are the **standard helper names** over this
  file's own logic — the standard fixes the names so a control written on another dashboard resolves
  here unchanged. Relative windows anchor to the newest date in the **shared domain** (ad days ∪
  email send dates), never to `today`.
- **`DOMAIN` is that union on purpose:** eight of Riverdance's email campaigns predate the first ad
  day, and without the union they are unreachable even on "All time".
- **`excludeForeignAds()` is load-bearing** — do not remove it from the sync path; `doSync` re-runs
  it after every reload for that reason.

## Deploy

`deploy_riverdance.ps1` — one-shot, idempotent, **no dataset and no views**. It deliberately creates
**no scheduler of its own**: refresh is driven by the platform's 6-hourly `sync-refresh` job
(`services/portal/dash/sync_refresh.py`), which is why the script grants the portal SA
**`roles/run.developer`** (not `run.invoker`) on the export job — `run.invoker` does not carry
`runWithOverrides`, and an invoker-only grant left this client stale for 13 days once.

⚠️ **`/go`'s deploy map skips this client — deploy it by hand.** Never Cloud Build from a laptop;
never `--allow-unauthenticated` (org policy forbids it, so the app does its own auth behind
`--no-invoker-iam-check`).

Derived names: bucket `agora-data-driven-riverdance-dash` · job `riverdance-export` · service
`riverdance-dash` · secrets `riverdance-{windsor-key,activecampaign-key,dash-password,dash-session-key}`.

## Verify before deploying

```powershell
py -3 tools\_validate_dash_js.py       clients\client_riverdance\dash\dashboard.html
py -3 clients\_standard\check_standard.py clients\client_riverdance\dash\dashboard.html
py -3 clients\_standard\vendor_lib.py --check
```
