# clients/ — one folder per client (index)

One line per client so you open the right pattern first. Every client's own `CLAUDE.md`/`README.md`
is the authority for its quirks; the root [`AGENTS.md`](../AGENTS.md) holds the shared rules. All
Cloud Run resources live in `asia-southeast1`. (Inventory verified 2026-07-29; dashboard standard
applied 2026-07-30.)

## Two things to read before editing any dashboard

| | |
|---|---|
| **[`_standard/STANDARD.md`](_standard/STANDARD.md)** | **The dashboard standard.** Every dashboard follows one of two layouts — **Leads** or **Sales** — over one shared shell. It holds the feature inventory, the two metric specs, the required id/class vocabulary, and the conformance rules. |
| [`../docs/CLIENT_DASHBOARD_PLAYBOOK.md`](../docs/CLIENT_DASHBOARD_PLAYBOOK.md) | **Why** each rule exists, connector by connector. The playbook is the reasoning; the standard is the shape that reasoning resolves to. |

Two gates, both required before any dash deploy:

```powershell
py -3 tools\_validate_dash_js.py       clients\client_<c>\dash\dashboard.html   # esprima 4: no ?. no ??
py -3 clients\_standard\check_standard.py                                       # all clients, 21 rules
py -3 clients\_standard\vendor_lib.py --check                                   # shared blocks in sync
```

## The clients

| Dir | Key | Standard | Pattern | `sql/`? | Deploy script | Dash service | Refresh / notes |
|---|---|---|---|---|---|---|---|
| `client_template` | `template` | **Sales** | **BQ 3-stage** (the worked example every client copies) | yes | `deploy_template.ps1` | `template-dash` | Scheduler `*/10`, self-gating |
| `client_TCS` | `tcs` | **Leads** ¹ | BQ 3-stage over direct-API `tcs_*` ingest | yes (14 views) | `deploy_tcs.ps1` (+ per-stage scripts) | `tcs-dash` | **Mid-build — another dev owns it**; see its README audit notes |
| `client_riverdance` | `riverdance` | **Sales** | **Windsor-LIVE** (job pulls the Windsor API directly — no `sql/`, no dataset) | no | `deploy_riverdance.ps1` | `riverdance-dash` | Automatic via the `sync-refresh` job (6h); ⚠️ /go's deploy map skips it — deploy by hand |
| `client_honeytribe` | `honeytribe` | **Sales** | API-LIVE (Windsor + Shopify Admin) | no | `deploy_honeytribe.ps1` | `honeytribe-dash` | **Sync-button-only BY CLIENT DECISION** (no scheduler; deploy script removes one) |
| `client_MeloYelo` | `meloyelo` | **Sales** + a Leads tab | API-LIVE (Unleashed + Campaign Monitor + Sheets CRM + Lark) | no | `deploy_meloyelo.ps1` | `meloyelo-dash` | **Sync-button-only BY DECISION** (same posture) |
| `client_RHE` | `rhe` | **Leads** | API-LIVE (Windsor Meta ×3 accounts + ActiveCampaign) | no | `deploy_RHE.ps1` | `rhe-dash` | **Sync-button-only BY CLIENT DECISION** (same posture) |
| `client_S7000` | `s7000` | **Leads** ¹ | **Local-build only** (`job/build_local.py`; no export job yet) | no | `deploy_s7000.ps1` | THREE: `s7000-internal-dash` · `s7000-into-dash` · `s7000-service-dash` | **Serves demo-flagged data BY DESIGN** until the live pull is built; `job/Dockerfile` CMD is a known placeholder |
| `client_agora` | `agora` | internal ¹ | **Internal** — Agora's own Upwork demand dashboard (vendored Telegram→sqlite pipeline) | no | `dash/deploy_dash_agora.ps1` + `job/deploy_job_agora.ps1` | `agora-dash` | Fully automated refresh in GCP; not a customer |

¹ carries a **documented waiver** in its `<body>` tag (a `data-single-view` and/or
`data-no-benchmark` attribute stating the reason). A waiver is how the standard records "this rule
does not apply here, and here is why" — it is never a silent gap, and `check_standard.py` prints it
on every run. See [`_standard/STANDARD.md`](_standard/STANDARD.md) §6.

Pattern cheat-sheet: **BQ 3-stage** = `sql/` views → export job → dashboard (the freshness-gated
template contract); **Windsor-LIVE / API-LIVE** = the job pulls the source APIs directly each run
and writes `<key>.json` itself (no dataset, no views, no watermark); **local-build** = a laptop
script publishes the payloads. Sync-button-only clients cost real API money per refresh — never
add a scheduler to them without the client's say-so.

## Which standard does a client take?

Decide by **what the product of advertising is**, not by the client's industry:

- an **enquiry** → **Leads** (`_standard/dash/dashboard-leads.html`). CPL and lead volume are the
  headline. It has no revenue, no ROAS, no AOV — if you find yourself adding one, you are on the
  wrong standard.
- an **order or booking** → **Sales** (`_standard/dash/dashboard-sales.html`). Revenue, AOV and
  ROAS are the headline; CPL is never a headline here.
- genuinely **both** → take Sales and give Leads its own tab, following the Leads section order
  inside it (this is what `client_MeloYelo` does).

## `_standard/` — what is in it

| Path | What it is |
|---|---|
| `STANDARD.md` | The spec: feature inventory, both metric specs, the vocabulary, the 21 conformance rules, the sanctioned waivers. |
| `dash/dashboard-leads.html` | The **Leads** reference implementation. Complete and working; copy it. |
| `dash/dashboard-sales.html` | The **Sales** reference implementation. |
| `dash/_lib.js` | The shared helper library. **Vendored byte-identically** into every dashboard — fix it everywhere or nowhere. |
| `dash/_shell.css` | The reference stylesheet. **Copied and re-branded** per client, not vendored: identity is meant to differ, structure is not. |
| `dash/_conform.css` | The print + reduced-motion + screen-reader block. Vendored verbatim into every dashboard, because it is the one part that is identical for every brand and cannot fight a client's existing CSS. |
| `vendor_lib.py` | Re-vendors the shared blocks. `--check` fails if any copy is stale. |
| `check_standard.py` | The conformance gate. Run it before any dash deploy. |
