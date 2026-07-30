# `client_template` — the per-client dashboard template

This directory is the **repeatable template** for one Agora Data Driven client. To
stand up a new client, copy this directory, set the client key everywhere it is
derived from (the deploy scripts derive every resource name from one `$CLIENT`
constant), and run the standup. The shipped key is `template`.

Everything for this project lives in **one GCP project** (`agora-data-driven`),
**one region** (`asia-southeast1`, Singapore), and **one shared Artifact Registry
repo** (`agora`). The only ingest source is **Windsor.ai**, which lands source data
into the shared **`raw_windsor`** BigQuery dataset; per-client SQL views read from
there.

---

## The three-stage data contract (end to end)

A metric flows through **three stages**, and the stages are matched **by name**.
Renaming a key in one stage silently breaks the next, so treat these names as a
contract:

```
  STAGE 1                     STAGE 2                         STAGE 3
  sql/*.sql                   job/main.py                     dash/dashboard.html
  (BigQuery view column)  ->  (assembled `data` dict key) ->  (`data.*` key in JS)
```

### Stage 1 — SQL views (`sql/*.sql`)

The views live in the `client_template` dataset and read from the shared Windsor
mirror. Applied in filename order by `create_views.py`:

- **`01_stg_source.sql`** → view `stg_source`. Typed/filtered rows from the shared
  Windsor mirror `agora-data-driven.raw_windsor.metrics_daily` (Windsor's blended
  daily export — point it at the real Windsor connector table(s) for this client).
  Columns: `metric_date, channel, sessions, users, conversions, spend, revenue`.
- **`02_model.sql`** → view `daily_performance`. Per-day rollup from `stg_source`.
  Columns: `metric_date, sessions, users, conversions, spend, revenue, roas`
  (`roas = revenue / NULLIF(spend, 0)`).
- **`03_kpi.sql`** → view `kpi_overview`. Single-row grand totals over the last 30
  days. Columns: `sessions, users, conversions, spend, revenue, roas, days_covered`.

### Stage 2 — the export job (`job/main.py`)

`job/main.py` queries the views and assembles a `data` dict, then uploads it to the
private bucket as `template.json`. The dict keys **must** match the dashboard:

```python
data = {
  "client": "template",
  "last_updated": <ISO build time, UTC>,
  "data_through": <newest upstream timestamp, ISO>,
  "kpis":  { "sessions", "users", "conversions", "spend", "revenue", "roas", "days_covered" },
  "daily": [ { "metric_date", "sessions", "users", "conversions", "spend", "revenue", "roas" }, ... ],
}
```

The `kpis.*` keys come from `kpi_overview`; each row of `daily` comes from
`daily_performance`.

### Stage 3 — the dashboard (`dash/dashboard.html`)

`dash/dashboard.html` fetches `/data.json` (the auth-gated proxy served by
`dash/main.py`) and reads `data.client`, `data.last_updated`, `data.data_through`,
`data.kpis.sessions|users|conversions|spend|revenue|roas|days_covered`, and iterates
`data.daily` for the chart/table. It shows "Loading dashboard…" until the fetch
resolves. Those keys are **the entire contract** — nothing else is required, and
nothing has been renamed.

**Rebuilt 2026-07-30 as a conformant instance of the Agora dashboard standard**
([`clients/_standard/STANDARD.md`](../_standard/STANDARD.md) §4, the **Sales** layout).
It used to be a 222-line stub, which meant every client copied the wrong shape. It now
carries the full shell — header freshness (`Updated` / `Data through`, red when stale)
and a Sync button, a tuckable filter bar with **two independent date ranges**, hash-routed
tabs, scorecards that toggle series on the chart below them, funnel stage cards, sortable
tables, wired empty states, and a "Reading of the period" insight strip — over the same
`kpis` + `daily` contract as before. Two tabs: **01 Performance** and **02 Daily record**.

These blocks are **VENDORED, not linked** (a dashboard is one self-contained file, so it
loads no external stylesheet or script):

| Block | Source | Posture |
|---|---|---|
| the helper library | `clients/_standard/dash/_lib.js` | byte-identical in every dashboard — **fix it everywhere or nowhere** |
| the stylesheet | `clients/_standard/dash/_shell.css` | copied here as the reference; a real client **re-brands `:root` only** |

Re-sync them with `py -3 clients/_standard/vendor_lib.py`; never edit a vendored block in
place. Adding a metric to the dashboard is one entry in the `SPEC` object at the top of the
script — plus the matching view column and job key, because the three stages match **by name**.

> The inline dashboard JS must parse under **esprima 4.x** (the pre-deploy JS gate).
> Esprima 4.x predates optional chaining (`?.`) and nullish coalescing (`??`) — they
> are its one known false-positive. Use classic `&&` / `||` guards instead.
>
> Two traps that are not obvious and both fail the gate a long way from their cause: a
> **`*/` sequence inside a JS block comment** (a `clients/*/dash` style glob will do it)
> closes the comment early, and a **literal script-src tag written inside an HTML comment**
> makes the gate parse the rest of the file as JavaScript.

### Adding a metric is usually three edits

Because the stages are matched by name, **adding one metric is three coordinated
edits, one per stage** — and you must keep the name identical across all three:

1. **Stage 1:** add the column to the relevant `sql/*.sql` view(s).
2. **Stage 2:** add the matching key to the `data` dict in `job/main.py`.
3. **Stage 3:** read the new `data.*` key in `dash/dashboard.html`.

**Renaming** a key counts as a breaking change: rename it in **all three** stages in
the same change, or the downstream stage reads a missing key.

---

## Security model (why the data is private)

The data JSON is **never public**. It lives in the PRIVATE bucket
`agora-data-driven-template-dash`. The dash web service (`dash/main.py`) renders a
login, holds a session, and proxies the private `template.json` from GCS at
`/data.json` **only to authenticated sessions**. The web SA has read-only access to
the bucket; the job SA writes it.

Org policy (Domain Restricted Sharing) forbids public Cloud Run, so the dash service
is deployed with `--no-invoker-iam-check` (never `--allow-unauthenticated`) — the
Flask app does its own password/SSO auth.

---

## Which deploy script for which edit

Use the **narrowest** script for the change you made; full standup is rarely needed
after the first time. All scripts are idempotent (create-or-update).

| You changed…                              | Run…                          |
|-------------------------------------------|-------------------------------|
| `sql/*.sql` (views only)                  | `sql/deploy_views_template.ps1` |
| `job/main.py` / data logic                | `job/deploy_job_template.ps1`   |
| `dash/dashboard.html` / web layer         | `dash/deploy_dash_template.ps1` |
| Full standup (new client, from nothing)   | `deploy_template.ps1`           |
| (Re)create/refresh the `*/10` trigger     | `scheduler.ps1`                 |

### `FORCE_REBUILD=1` for view-only / code / seed changes

The export job is **self-gating**: on each `*/10` tick it only rebuilds when the
`raw_windsor` mirror tables it reads advanced past its `_freshness.json` watermark.
A change to a **view, the job code, or seed data does NOT advance the upstream
watermark**, so a normal run would no-op and keep serving stale JSON. To force the
job to rebuild after such a change, run it with **`FORCE_REBUILD=1`** (the job-level
deploy script and the standup's first run both do this). It is the documented bypass
for view-only / code / seed edits.

---

## Fresh-project order (do this once, in order)

On a brand-new project the views and export job have nothing to read until Windsor
has landed data. Run these in order:

1. **Windsor ingest loaders land `raw_windsor`.** The shared daily Windsor ingest
   jobs (deployed by `tools/deploy_ingest_jobs.ps1`) must have run at least once so
   `raw_windsor.*` tables exist and contain rows. The exports/views read from there;
   they cannot produce anything before this.
2. **Create the views** — `create_views.py` (applied via the repo `.venv` python),
   which `CREATE OR REPLACE`s the `sql/*.sql` views into the `client_template`
   dataset.
3. **Run the export job** to build the first `template.json`. The first run uses
   `FORCE_REBUILD=1` because no watermark exists yet; later `*/10` ticks self-gate
   normally.

`deploy_template.ps1` performs the full standup (APIs, bucket, dataset, service
accounts + IAM, secrets, views, export job, `*/10` scheduler, and the dash service)
and handles step 2 and 3 for you — but **step 1 (Windsor ingest) is a prerequisite**
owned by the shared ingest deploy, not by this directory.

---

## Layout

```
clients/client_template/
  README.md                  this file
  deploy_template.ps1        one-shot idempotent full standup
  scheduler.ps1              create/refresh the */10 export trigger
  sql/                       Stage 1: view DDL (01_stg_source, 02_model, 03_kpi)
                             + deploy_views_template.ps1
  job/                       Stage 2: main.py (assembles data) + freshness.py
                             + Dockerfile + deploy_job_template.ps1
  dash/                      Stage 3: dashboard.html + main.py (auth + /data.json proxy)
                             + platform_sso.py + Dockerfile + deploy_dash_template.ps1
  data/                      local scratch for the JSON during dev (gitignored)
```

## Gotchas / DO-NOT-TOUCH

- **Never rename a contract key in one stage only** — the three stages match BY NAME (see the
  contract section above); a rename is a three-file change or a breakage.
- **Inline JS is esprima-4.x-safe** (no `?.` / `??`) — `tools/_validate_dash_js.py` gates it; pass
  THIS client's `dash/dashboard.html` path explicitly (a bare run only checks the template copy —
  which here is the same file, but copies must pass their own path).
- **Never edit a vendored block in place.** The `AGORA STANDARD LIB` and `AGORA STANDARD SHELL CSS`
  sentinel pairs are overwritten wholesale by `clients/_standard/vendor_lib.py`; an edit inside them
  is lost on the next sync and silently diverges the estate until then. Edit the source under
  `clients/_standard/dash/` instead.
- **A dashboard must pass BOTH gates before deploy**, not just the JS one:
  ```powershell
  py -3 tools\_validate_dash_js.py       clients\client_template\dash\dashboard.html
  py -3 clients\_standard\check_standard.py clients\client_template\dash\dashboard.html
  ```
- **`FORCE_REBUILD=1` is mandatory for view-only / code / seed changes** (they don't advance the
  watermark — see the dedicated section above).
- **Never edit views in the BigQuery console** — `sql/*.sql` + `create_views.py` are the source of
  truth. Never `--allow-unauthenticated`; never Cloud Build a deploy from a laptop.
- `freshness.py` and `platform_sso.py` are **vendored byte-identically** into every client — fix
  them everywhere or nowhere.

## Status (volatile)

The shipped `template` client (dataset `client_template`, service `template-dash`) is the worked
example, filtered out of the portal console's client cards. Copying this directory + running
`deploy_template.ps1` is the standup path for every new BQ 3-stage client. Per-client live status
belongs in each client's own README (see [`../README.md`](../README.md) for the index; verified
2026-07-29).
