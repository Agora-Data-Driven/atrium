# CLAUDE.md — services/ingest (Windsor connector loaders)

**Rules live in the repo-root [`/CLAUDE.md`](../../CLAUDE.md)** — read it first; this file only adds
local context. If they disagree, root wins.

These are the **writers of the shared `raw_windsor` BigQuery dataset** — the only raw layer. Each
connector (`ga4`, `google_ads`, `meta`, `tradedesk`, `reddit`, `hubspot`, `fields`) is a Cloud Run
job that pulls from the Windsor.ai REST API and loads `raw_windsor.*`.

- **Scheduled daily pulls, NOT self-gating.** They WRITE `raw_windsor`; the self-gating lives
  downstream in the client export jobs (`*/10`) and the status dashboard (`*/15`), which probe whether
  `raw_windsor` advanced before rebuilding.
- **`tools/deploy_ingest_jobs.ps1` is the only script that touches production ingest.** Its `$JOBS`
  array is the **single source of truth** for which connectors exist + their cron — the directories
  here must match it. Uncomment a `$JOBS` row as its loader is built.
- Each connector dir is self-contained (`<x>_loader.py`, `create_<x>_table.py`, `Dockerfile`,
  `requirements.txt`). The Windsor API key is mounted from Secret Manager (`windsor-api-key`).

## 🔴 This layer is CANONICAL. A client must never call Windsor itself.

The architecture is **one shared loader per connector → `raw_windsor.*` → a per-client SQL view →
the client's export job**. A client job that calls the Windsor API directly is **legacy to be
migrated off**, not a pattern to copy:

```
Meta ──► Windsor ──► windsor-meta-ingest ──► raw_windsor.perf_meta ──► client view ──► <c>-export
```

**`meta/meta_loader.py` is BUILT and has run (2026-07-30)** — `raw_windsor.perf_meta` exists and
holds data, and **TCS is the first client on the canonical path** (perf_meta → `client_tcs.paid_media_*`
views → `tcs-export` → the dashboard's Lead Gen tab, with no client-side Windsor call). Only TCS is
backfilled so far; run the loader with no `--only` to sweep the estate.

That stub is why `client_RHE`, `client_riverdance`, `client_honeytribe` and `client_MeloYelo` each
grew their **own** Windsor pull inside their export job — four implementations, four row
vocabularies, every trap rediscovered separately, and one that silently lost **$321** of spend by
overwriting rows on key collision instead of summing. Those four can now migrate onto views.
**Do not add a fifth.** [`meta/README.md`](meta/README.md) holds the full spec + the live probe results.

- **The staging bucket is new**: `create_staging_bucket.py` makes `agora-data-driven-staging`
  (private, uniform access, 30-day lifecycle). Every loader stages NDJSON there before the MERGE.
- 🔴 **`windsor-api-key` does not exist yet** — only the four per-client secrets do, and they are
  **byte-identical** (verified). `deploy_ingest_jobs.ps1` mounts `windsor-api-key` and will fail at
  the IAM step until it is created, which is why `windsor-meta-ingest` is **not deployed**. Create +
  rotate it, then delete the four. Locally: `WINDSOR_SECRET=rhe-windsor-key` or pass `WINDSOR_API_KEY`.
- ⚠️ **`/all` is BLENDED across connectors.** `ASL Logistics` is a `google_ads` account and carries
  no `ad_id`; the loader filters `datasource == "facebook"` so those rows can never poison the
  `(ad_id, metric_date)` merge key. 12 Meta accounts + 1 Google Ads, not 13 Meta.
- ⚠️ **`date_from`/`date_to` DO work** on this account (probed live), so the chunked port is valid —
  `meta/windsor_api.py`'s docstring says otherwise and is wrong on that one point. Never send a
  preset and a range together: **the preset silently wins**.

- The request/transform helpers are written and tested in **`meta/windsor_api.py`** — reuse them.
  ⚠️ **Never vendor that module into a client job.** It briefly lived at
  `clients/_standard/job/windsor.py`, which would have entrenched the very pattern above; it was
  moved here 2026-07-30.
- **ONE Windsor key sees EVERY account.** Verified live 2026-07-30: one key returned **13 accounts /
  4,751 rows** with `select_accounts` omitted. So one job covers the whole estate — and the
  per-client secrets (`rhe-windsor-key`, `honeytribe-windsor-key`, `meloyelo-windsor-key`,
  `riverdance-windsor-key`, + a TCS key) are almost certainly **the same credential five times**.
  Consolidate onto `windsor-api-key`.
- **Three live ad accounts have no client folder at all** — `PHP` ($147.5k/yr, the estate's biggest
  spender), `Sabbath Spa` ($32k) and `ASL Logistics` ($4.9k). Map them or exclude them explicitly;
  never let them default into another client's view. Inventory + slug map:
  [`meta/README.md`](meta/README.md).
- **`client_S7000` needs TWO slugs** (`into` and `service7000`), never one — INTO and Service 7000
  must never see each other's data, and a single slug would collapse that boundary at the view layer.
