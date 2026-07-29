# `tools/` — operator convenience scripts

## Plain English

These are convenience scripts for the **Windows operator** of the Agora Data Driven platform. You do
not need to understand the internals to use them — each one wraps a handful of `gcloud` calls so you
do not have to remember the flags.

There are three you will touch day to day:

- **`setup`** — prepares the laptop. **Run once** (and again only if your credentials expire or you
  move to a new machine). It logs you into the two `gcloud` credential systems, creates/points the
  repo `.venv`, and sanity-checks your toolchain.
- **`start_day`** — a **~10-second morning check**. Run it when you sit down. It verifies that both
  `gcloud` logins are still valid and reauths the one(s) that expired overnight, so the rest of your
  day's commands do not fail halfway through.
- **`deploy_ingest_jobs`** — the one that **TOUCHES PRODUCTION**. It builds and deploys the shared
  Windsor ingest jobs (the API pulls that WRITE the `raw_windsor` dataset) and their daily schedulers.
  Run it deliberately, not by reflex.

The other scripts are run only when you are standing up or extending the platform (`enable_*`), or are
launchers/validators the day-to-day scripts call for you.

## Files

| File | What it does | When to run |
|------|--------------|-------------|
| `setup.ps1` | Logs into both `gcloud` credential systems (CLI creds + ADC), creates/points the repo `.venv`, and verifies the toolchain. | **Once** per machine, and after a credential expiry you cannot reauth past with `start_day`. |
| `start_day.ps1` | Morning preflight: checks BOTH `gcloud` credential systems are still valid and reauths whichever expired overnight. | **Every morning** before you run anything else. |
| `setup.cmd` | Double-click launcher that runs `setup.ps1` (sets the PowerShell execution policy for that one process so you do not have to). | When you want to run `setup` without opening a terminal. |
| `start_day.cmd` | Double-click launcher that runs `start_day.ps1`. | When you want the morning check without opening a terminal. |
| `_validate_dash_js.py` | Pre-deploy esprima gate over ONE file's inline JS. **Takes an explicit path** (`python tools\_validate_dash_js.py <path\to\dashboard.html or template>`); a **bare run validates only `clients/client_template/dash/dashboard.html`** — it never walks the other clients, so pass the file you actually changed. Exit 0 = clean (the `?.`/`??` esprima-4.x notice is a warning), 1 = real syntax error. | Invoked by the deploy scripts and CI (which globs every dashboard + portal template); run by hand with your file's path when debugging. |
| `_creative_gallery_test.py` | Off-cloud creative-gallery test for the Meta-fed clients (RHE + honeytribe): link-preview rejection, reused-artwork preservation, headline cleaning. No network/GCS/API cost. | `python tools\_creative_gallery_test.py` before touching creative-gallery code; CI runs it too. |
| `push-branch.ps1` | Commits ALL local work and pushes it to THIS machine's own branch (name from `-Dev` → `tools/.devname` → machine name) for PR/merge. | End of a work session, per `docs/dev-workflow.md`. |
| `merge-branches.ps1` | **Touches production.** The agent-driven release pipeline: fetch → `integration/merge` → CI tests → land on `main` → auto-deploy changed services → prune. See the flags section below. | To ship this repo (the root AGENTS.md "Team workflow" section is the contract). |
| `deploy_ingest_jobs.ps1` | Builds + deploys the shared **Windsor** ingest jobs from its `$JOBS` table. ⚠️ As of 2026-07-29 effectively DORMANT — no Windsor jobs are deployed and the shared `windsor-api-key` secret no longer exists; the seven `tcs-*` jobs deploy via `services/ingest/deploy_tcs_ingest.ps1` instead (see [`services/ingest/README.md`](../services/ingest/README.md)). | Only for a future shared-Windsor standup. |
| `glm-bypass-mode.ps1`/`.cmd` · `kimi-bypass-mode.ps1`/`.cmd` | Claude Code launchers pinning the GLM / Kimi coding-plan endpoints. **Mirrored FROM `agora-devtools` — never edit the copies here**; edit in agora-devtools and let the sync mirror them. | Launching an agent session on the alternate providers. |
| `enable_platform_sso.ps1` | Wires deployed `<c>-dash` dashboards to additively trust the portal SSO cookie (grants + mounts `platform-sso-key`, sets `CLIENT_KEY`). | After portal standup, once dashboards are rebuilt on the SSO-capable image. |
| `enable_super_admin.ps1` | Grants the portal front-door (`platform-dash`) god-mode: a bootstrap super-admin password, `run.developer`, and per-dashboard password-rotation / act-as IAM. | Once, during platform standup; safe to re-run. |

## How to use

**The easy way (no terminal):** double-click the `.cmd` launchers in this folder —

- `setup.cmd` the first time you set up the machine,
- `start_day.cmd` each morning.

They set the PowerShell execution policy for that single process and then run the matching `.ps1`, so
you never have to fight Windows' script-blocking.

**From a terminal:** open PowerShell in the repo root and run the `.ps1` directly, e.g.

```powershell
.\tools\setup.ps1
.\tools\start_day.ps1
.\tools\deploy_ingest_jobs.ps1
.\tools\enable_platform_sso.ps1 -Keys "template"
.\tools\enable_super_admin.ps1
```

If PowerShell blocks the script, either use the `.cmd` launcher or run once:
`powershell -ExecutionPolicy Bypass -File .\tools\setup.ps1`.

> **Spaces in the path (this repo lives under `…\Agora Data Driven\…`).** Run the scripts by a
> *relative* path (`.\tools\setup.ps1`) or, if you must use a full path with the call operator,
> **quote it**: `& "C:\Users\you\…\Agora Data Driven\Portal\tools\setup.ps1"`. An **unquoted**
> absolute path — `& C:\Users\you\…\Agora Data Driven\Portal\tools\setup.ps1` — fails with
> *"The term 'C:\Users\you\…\Agora' is not recognized…"* because PowerShell splits the command on
> the space before `Data`. The `.cmd` launchers and the `-File` form already quote the path for you.

## Team workflow: `push-branch.ps1` / `merge-branches.ps1`

`push-branch.ps1` (params `-Dev`, `-Desc`, `-Message`) is one-branch-per-machine; `merge-branches.ps1`
is the release SOP (full contract + AGENT RUNBOOK in the script header and the root
[`AGENTS.md`](../AGENTS.md)). The switches that matter:

- **`-DryRun`** — preview the land + deploy plan, change nothing.
- **`-Resume`** — continue a run that STOPPED on a merge conflict or red gate: the conflict is left
  in the tree; resolve it, `git add -A; git commit --no-edit`, then re-run with `-Resume` (it
  expects a clean tree on `integration/merge`). Never bypass the conflict any other way.
- **`-DeleteMerged`** — the standalone prune of already-merged branches.
- **`-AllowReverts`** — acknowledge the reversion net's finding and proceed anyway (it otherwise
  stops when the integration would revert commits already on `main`).
- Also: `-NoPush` / `-NoDeploy` (review-first behavior), `-NoPrune`, `-Exclude <branches>`.

**`wip/*` branches are PARKED work** (`agora-park.ps1` in agora-devtools): merge-branches skips
them and never prunes them — parked work is visible on origin and shipped nowhere.

## Shared Windsor ingest jobs

`deploy_ingest_jobs.ps1` iterates the canonical `$JOBS` table below — it is the single source of
truth, and the `services/ingest/<x>` directories must match it exactly. All Windsor
connectors are **daily scheduled API pulls**, staggered just before the client export window (the
per-client exports self-gate on a `*/10` tick afterwards). There is **no `*/10` self-gating ingest
job** — that pattern lives downstream in the consumers (the exports + status dashboard), not in these
writers of `raw_windsor`.

| key | dir | job | mem | cpu | cron | status |
|-----|-----|-----|-----|-----|------|--------|
| `windsor-ga4` | `services/ingest/ga4` | `windsor-ga4-ingest` | `1Gi` | `1` | `10 1 * * *` | active |
| `windsor-google-ads` | `services/ingest/google_ads` | `windsor-google-ads-ingest` | `1Gi` | `1` | `15 1 * * *` | active |
| `windsor-meta` | `services/ingest/meta` | `windsor-meta-ingest` | `1Gi` | `1` | `20 1 * * *` | active |
| `windsor-tradedesk` | `services/ingest/tradedesk` | `windsor-tradedesk-ingest` | `1Gi` | `1` | `25 1 * * *` | commented — uncomment when built |
| `windsor-reddit` | `services/ingest/reddit` | `windsor-reddit-ingest` | `512Mi` | `1` | `30 1 * * *` | commented — uncomment when built |
| `windsor-hubspot` | `services/ingest/hubspot` | `windsor-hubspot-ingest` | `512Mi` | `1` | `35 1 * * *` | commented — uncomment when built |
| `windsor-fields` | `services/ingest/fields` | `windsor-fields-ingest` | `512Mi` | `1` | `40 1 * * *` | commented — uncomment when built |

The commented rows live in the `$JOBS` array, commented out, rather than being dropped — the array is
the single source of truth, so each connector stays listed and is uncommented as its loader is built.
All of these read the shared ingest API key from Secret Manager (`windsor-api-key`) and run as the
`ingest-runner@agora-data-driven.iam.gserviceaccount.com` service account.

## Why two credential systems are checked

`gcloud` keeps **two independent logins**, and the org enforces periodic reauth on each, so either can
expire without the other. A morning preflight (`start_day`) and the one-time `setup` must check
**BOTH**:

- **CLI creds** — used by `gcloud secrets ...` and the other `gcloud` commands these scripts run.
  Refreshed via `gcloud auth login`.
- **Application Default Credentials (ADC)** — used by the Python client libraries
  (`google-cloud-bigquery` / `google-cloud-storage` / `google-cloud-secret-manager`). Refreshed via
  `gcloud auth application-default login`.

Checking only one and proceeding is the classic way to have a deploy or a data pull die halfway
through with an auth error, so both are verified up front.

## Notes & gotchas

- **The committed source is portable as-is.** Everything needed to build and deploy is in the repo;
  there are no machine-specific paths baked in. Clone it, run `setup`, and go.
- **The `.venv` is a dev-only SUPERSET.** The repo `.venv` deliberately installs the union of the job
  and ingest requirements but **not** the dashboard (`dash`) requirements — the dash pins a possibly
  different `google-cloud-storage` on purpose, so it is excluded from the shared dev environment to
  avoid a version clash. Each Cloud Run unit still pins and installs its own requirements at build
  time; the `.venv` is only for local development convenience.
- **`Test-Probe` rationale.** `setup.ps1` runs under `$ErrorActionPreference = "Stop"`. With Stop set,
  redirecting a native command's stderr (`2>$null`) turns its error output into a terminating
  `NativeCommandError`, which would abort the whole script. `Test-Probe` drops to `Continue` for the
  probe and reports success purely from the exit code, so an "expected to fail" check (e.g. a
  not-logged-in `gcloud` call) falls through to the login step instead of killing the script.
  `start_day.ps1` and the deploy scripts, by contrast, stay on the default `Continue` for the opposite
  reason: `gcloud` writes ordinary progress to stderr, and under `Stop` PowerShell would wrap that as
  a terminating `NativeCommandError` and abort mid-script **even on success** — so they gate on
  `$LASTEXITCODE` explicitly instead.

## See also

- Repo-root [`README.md`](../README.md) — project overview, architecture, and the manual deploy
  procedure.
- [`services/ingest/README.md`](../services/ingest/README.md) — how the Windsor
  connector loaders work and how to add a new one.
