# clients/ — one folder per client (index)

One line per client so you open the right pattern first. Every client's own `CLAUDE.md`/`README.md`
is the authority for its quirks; the root [`AGENTS.md`](../AGENTS.md) holds the shared rules. All
Cloud Run resources live in `asia-southeast1`. (Inventory verified 2026-07-29.)

| Dir | Key | Pattern | `sql/`? | Deploy script | Dash service | Refresh / notes |
|---|---|---|---|---|---|---|
| `client_template` | `template` | **BQ 3-stage** (the worked example every client copies) | yes | `deploy_template.ps1` | `template-dash` | Scheduler `*/10`, self-gating |
| `client_TCS` | `tcs` | BQ 3-stage over direct-API `tcs_*` ingest | yes (14 views) | `deploy_tcs.ps1` (+ per-stage scripts) | `tcs-dash` | **Mid-build — another dev owns it**; see its README audit notes |
| `client_riverdance` | `riverdance` | **Windsor-LIVE** (job pulls the Windsor API directly — no `sql/`, no dataset) | no | `deploy_riverdance.ps1` | `riverdance-dash` | Automatic via the `sync-refresh` job (6h); ⚠️ /go's deploy map skips it — deploy by hand |
| `client_honeytribe` | `honeytribe` | API-LIVE (Windsor + Shopify Admin) | no | `deploy_honeytribe.ps1` | `honeytribe-dash` | **Sync-button-only BY CLIENT DECISION** (no scheduler; deploy script removes one) |
| `client_MeloYelo` | `meloyelo` | API-LIVE (Unleashed + Campaign Monitor + Sheets CRM + Lark) | no | `deploy_meloyelo.ps1` | `meloyelo-dash` | **Sync-button-only BY DECISION** (same posture) |
| `client_RHE` | `rhe` | API-LIVE (Windsor Meta ×3 accounts + ActiveCampaign) | no | `deploy_RHE.ps1` | `rhe-dash` | **Sync-button-only BY CLIENT DECISION** (same posture) |
| `client_S7000` | `s7000` | **Local-build only** (`job/build_local.py`; no export job yet) | no | `deploy_s7000.ps1` | THREE: `s7000-internal-dash` · `s7000-into-dash` · `s7000-service-dash` | **Serves demo-flagged data BY DESIGN** until the live pull is built; `job/Dockerfile` CMD is a known placeholder |
| `client_agora` | `agora` | **Internal** — Agora's own Upwork demand dashboard (vendored Telegram→sqlite pipeline) | no | `dash/deploy_dash_agora.ps1` + `job/deploy_job_agora.ps1` | `agora-dash` | Fully automated refresh in GCP; not a customer |

Pattern cheat-sheet: **BQ 3-stage** = `sql/` views → export job → dashboard (the freshness-gated
template contract); **Windsor-LIVE / API-LIVE** = the job pulls the source APIs directly each run
and writes `<key>.json` itself (no dataset, no views, no watermark); **local-build** = a laptop
script publishes the payloads. Sync-button-only clients cost real API money per refresh — never
add a scheduler to them without the client's say-so.
