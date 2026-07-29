# Ingest (`services/ingest/`) — the writers of `raw_windsor`

The loaders that land source data into the shared BigQuery raw layer `raw_windsor`.
Two families: **Windsor connector loaders** (the original pattern — one shared REST
API over every ad platform) and the **`tcs_*` direct-API loaders** (a sanctioned,
documented exception for grains Windsor doesn't serve).

## What Windsor.ai is

[Windsor.ai](https://windsor.ai) is a **marketing-data connector platform**: it
authenticates to the marketing sources an agency runs (Google Analytics 4, Google
Ads, Meta/Facebook Ads, and many more) and exposes their metrics through a single
REST API. Instead of integrating each ad platform's bespoke API ourselves, we pull
everything through Windsor with one shared API key.

Windsor is the **default** ingest source. If a new data source is needed, it should
arrive as a new Windsor *connector* — with ONE documented exception: the **`tcs_*`
family** pulls Shopify / Klaviyo / the quiz sheet / Tapfiliate **directly** because
the TCS diagnostic needs per-recipient Klaviyo open/click events, a grain Windsor's
connector does not serve. Direct-API loaders still write the same shared
`raw_windsor` layer and follow the same job pattern.

## The shared raw layer: `raw_windsor`

Every connector loader lands its rows into the **one shared** BigQuery dataset
`raw_windsor` (project `agora-data-driven`, location `asia-southeast1`). One table per
connector:

**Windsor connectors** (`<dir>/<dir>_loader.py` + `create_<x>_table.py` each):

| connector    | raw target               |
|--------------|--------------------------|
| `ga4`        | `raw_windsor.ga4`        |
| `google_ads` | `raw_windsor.google_ads` |
| `meta`       | `raw_windsor.meta`       |
| `tradedesk`  | `raw_windsor.tradedesk`  |
| `reddit`     | `raw_windsor.reddit`     |
| `hubspot`    | `raw_windsor.hubspot`    |
| `fields`     | `raw_windsor.fields` (Windsor's own field/metadata catalogue, not a marketing source) |

**`tcs_*` direct-API loaders** (each `<dir>/<dir>_loader.py`; `tcs_affiliates`,
`tcs_klaviyo_campaigns`, `tcs_klaviyo_profiles` and `tcs_sessions` create their
tables in-loader — no `create_*_table.py`):

| dir | what it pulls | raw target |
|---|---|---|
| `tcs_shopify/` | Shopify orders + marketing attribution | `raw_windsor.tcs_shopify_orders` |
| `tcs_klaviyo/` | Klaviyo email events — one row per send, per-recipient open/click flags | `raw_windsor.tcs_klaviyo_events` |
| `tcs_klaviyo_campaigns/` | Klaviyo campaign metadata + values report (attributed revenue) | `raw_windsor.tcs_klaviyo_campaigns` |
| `tcs_klaviyo_profiles/` | Klaviyo profiles — person dimension + CLV/churn predictions | `raw_windsor.tcs_klaviyo_profiles` |
| `tcs_quiz/` | Business-Quiz Google Sheet (Typeform archive + live Paperform tab) | `raw_windsor.tcs_quiz` |
| `tcs_sessions/` | Shopify storefront sessions (ShopifyQL) | `raw_windsor.tcs_shopify_sessions` |
| `tcs_affiliates/` | Tapfiliate affiliates + conversions | `raw_windsor.tcs_affiliates` + `raw_windsor.tcs_affiliate_conversions` |

`create_dataset.py` creates the `raw_windsor` dataset itself (idempotent). Each
Windsor connector sub-directory owns a `create_<x>_table.py` that creates its own table.

Per-client SQL views read **downstream** from these mirror tables (for example a
client's `stg_source` view UNIONs `raw_windsor.ga4` + `raw_windsor.google_ads`). The
connector loaders never know about individual clients -- they only write the shared
raw layer.

## Per-connector sub-directory layout

Each connector `x` lives in `services/ingest/x/` and contains:

```
x/
  x_loader.py        # entrypoint: pull from Windsor REST API -> load raw_windsor.x
  create_x_table.py  # idempotent: create the raw_windsor.x table with its schema
  Dockerfile         # job image; CMD ["python","x_loader.py"]; non-root appuser
  .dockerignore
  requirements.txt   # Windsor ingest pins (google-cloud-* + requests)
  README.md          # one-paragraph purpose + how to run + raw target
```

The loaders read the shared Windsor API key from **Secret Manager** (secret
`windsor-api-key`) via Application Default Credentials -- there is no machine-specific
key path. The Windsor-specific request/parse logic is intentionally left as `# TODO:`
markers: this is a skeleton that the operator adapts to the agency's real Windsor
account, connector ids, and field selections.

## Cadence: daily scheduled pulls

All Windsor connectors are **daily** scheduled pulls, staggered just before the client
export window (so the freshest raw data is present when exports run). They are plain
*writers* of `raw_windsor` -- they are **not** self-gating. Now that the only source is
Windsor (a scheduled REST API), there is no `*/10` self-gating ingest job.

The self-gating lives **downstream in the consumers**: each client EXPORT job (on a
`*/10` tick) and the status dashboard (`*/15`) probe whether `raw_windsor` advanced
past their `_freshness.json` watermark before rebuilding. The ingest jobs just keep the
raw layer fresh on their daily schedule.

## Deploy / schedule — TWO deployers, don't mix them up

🔴 **The wrong-deployer gotcha:** there are two deploy scripts and they own DIFFERENT
job families. Pointing the wrong one at a loader fails confusingly (and `/go`'s deploy
map has routed `services/ingest/**` changes to the Windsor script before):

- **`services/ingest/deploy_tcs_ingest.ps1`** owns the **seven `tcs-*` jobs**
  (`tcs-shopify-ingest` `45 1 * * *` · `tcs-klaviyo-ingest` `50 1` · `tcs-quiz-ingest`
  `55 1` · `tcs-sessions-ingest` `10 2` · `tcs-klaviyo-profiles-ingest` `20 2` ·
  `tcs-klaviyo-campaigns-ingest` `35 2` · `tcs-affiliates-ingest` `45 2`; same
  `-Only`/`-SkipBuild`/`-Run` switches). The loaders read their own secrets by id
  (`tcs-shopify-token` / `tcs-klaviyo-key` / `tcs-tapfiliate-key`) via ADC — no
  `--set-secrets`; run `tcs_provision_secrets.ps1` once to create them and grant
  `ingest-runner@` access.
- **`tools/deploy_ingest_jobs.ps1`** owns only the **Windsor `windsor-*` jobs** below.
  ⚠️ **Volatile status (audited 2026-07-29):** no Windsor ingest jobs are deployed in
  production and the shared `windsor-api-key` secret does not exist any more (clients
  moved to per-client Windsor keys), so running this script today fails on the missing
  secret. It is kept as the pattern for a future shared-Windsor standup; the `/go`
  deploy-map entry that still points at it is pending retirement.

For the Windsor family, the script's `$JOBS`
array is the **single source of truth** for which connectors exist; the sub-directories
here must match it exactly. Its rows are:

| key                  | dir                                    | job                         | mem   | cpu | cron          |
|----------------------|----------------------------------------|-----------------------------|-------|-----|---------------|
| `windsor-ga4`        | `services/ingest/ga4`         | `windsor-ga4-ingest`        | 1Gi   | 1   | `10 1 * * *`  |
| `windsor-google-ads` | `services/ingest/google_ads`  | `windsor-google-ads-ingest` | 1Gi   | 1   | `15 1 * * *`  |
| `windsor-meta`       | `services/ingest/meta`        | `windsor-meta-ingest`       | 1Gi   | 1   | `20 1 * * *`  |

Additional connector rows (tradedesk, reddit, hubspot, fields) live in the `$JOBS`
array **commented out** -- uncomment each row as its loader is built, rather than
dropping it, so the array stays the canonical list.

Run examples:

```powershell
.\tools\deploy_ingest_jobs.ps1                 # build + deploy + schedule all jobs
.\tools\deploy_ingest_jobs.ps1 -Only windsor-meta
.\tools\deploy_ingest_jobs.ps1 -Run            # also execute each job once after deploy
```

TCS run examples:

```powershell
.\services\ingest\tcs_provision_secrets.ps1        # once: secrets + ingest-runner@ access
.\services\ingest\deploy_tcs_ingest.ps1            # build + deploy + schedule all 7 tcs jobs
.\services\ingest\deploy_tcs_ingest.ps1 -Only tcs-klaviyo -Run
```

Create the shared dataset once (idempotent; safe to re-run):

```powershell
.\.venv\Scripts\python.exe services\ingest\create_dataset.py
```

## DO-NOT-TOUCH: the `raw_windsor` contract

The dataset name `raw_windsor` and the one-table-per-connector naming above are a
**binding contract**: every client's `sql/` views select from these exact
`agora-data-driven.raw_windsor.<table>` names, and every export job's freshness gate
probes them as its GATING_TABLES. There is no shared constants module — each loader
re-declares `RAW_DATASET = os.environ.get("RAW_DATASET", "raw_windsor")` (canonical
site: `create_dataset.py`; also hardcoded in both deploy scripts). Renaming a dataset,
table, or column here silently breaks views downstream AND stalls freshness gating.
Additive columns are fine; renames/moves are not.
