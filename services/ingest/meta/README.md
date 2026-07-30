# Windsor Meta connector — `raw_windsor.perf_meta`

Pulls Meta (Facebook Ads) performance from the **Windsor.ai** `/all` endpoint for **every Agora
client in one job** and loads it into the shared raw layer table **`raw_windsor.perf_meta`**
(project `agora-data-driven`, location `asia-southeast1`). Scheduled daily; **not** self-gating —
the client export jobs self-gate downstream.

```
Meta ──► Windsor API ──► windsor-meta-ingest (THIS dir) ──► raw_windsor.perf_meta
                                                                └──► client view (WHERE client_slug = '<c>')
                                                                        └──► client export job ──► <c>.json
```

> ## ✅ STATUS: BUILT AND RUN (2026-07-30). `raw_windsor.perf_meta` exists and holds data.
>
> The stub is gone. The loader is the bidbrain port described below, and **TCS is the first client
> on the canonical path** — `raw_windsor.perf_meta` → `client_tcs.paid_media_*` views → the
> `tcs-export` job → the dashboard's **Lead Gen** tab, with **no client-side Windsor call at all**.
>
> **Loaded so far: TCS only** (2,276 rows, 31 ads, 6 campaigns, 2025-07-30 → 2026-07-29,
> $13,528.52 — reconciles to the dollar against the inventory below). The other 11 Meta accounts
> have **not** been backfilled yet; run the loader with no `--only` to sweep the estate.
>
> Why it mattered: because this loader was left a stub, `client_RHE`, `client_riverdance`,
> `client_honeytribe` and `client_MeloYelo` each grew their **own private Windsor pull** inside
> their export job — four implementations, four row vocabularies, every trap rediscovered
> separately, and one copy that overwrote rows on key collision and silently lost **$321** of spend
> until an audit caught it. Those four can now migrate onto views. **Do not add a fifth.**

### What the live probe changed about the plan (2026-07-30)

Everything here was measured against the real agora Windsor account, not assumed:

| Question | Answer |
|---|---|
| Does `/all` accept `date_from`/`date_to`? | **YES.** `windsor_api.py`'s docstring claim that it is `date_preset`-only is **wrong for this account** — so bidbrain's chunked-date-range model ports directly, and history does not have to "accumulate". ⚠️ Send one or the other, never both: with both present **`date_preset` silently wins**. |
| Is `/all` blended across connectors? | **YES.** `ASL Logistics` (`106-434-7699`) is **`google_ads`**, not Meta, and its rows carry **no `ad_id`** — they would have put NULL in the REQUIRED merge-key column. The loader filters on `datasource == "facebook"`. So the "13 accounts" are **12 Meta + 1 Google Ads**. |
| Is the 13-month unique-count wall real? | **YES.** With `unique_actions_*`, `last_730d` → HTTP 400 *"breakdowns for unique-count fields are only available for the last 13 months"*. Without them, `last_1095d` returns 5,248 rows. `UniqueCountHorizonError` handles it as the natural backfill horizon. |
| Are the five per-client keys one credential? | **YES** — `rhe-`, `honeytribe-`, `meloyelo-` and `riverdance-windsor-key` are **byte-identical** (same SHA-256). |
| Which purchase label? | **`actions_omni_purchase`** — the widest. Sabbath Spa reports 28 purchases on omni and **zero** on `actions_purchase`; every other account agrees across both. `actions_complete_registration` is served but **identically 0 everywhere** — treat as "not measured", never "none happened". |

### 🔴 Blockers that are still open

- **The `windsor-api-key` secret does not exist.** The four per-client secrets do. Create the
  consolidated one (and **rotate** it — a key was pasted into a chat on 2026-07-30), then delete the
  four. Until then, run locally with `WINDSOR_SECRET=rhe-windsor-key` or pass `WINDSOR_API_KEY`.
  ⚠️ `tools/deploy_ingest_jobs.ps1` mounts `windsor-api-key` and will **fail at the IAM step**
  until it exists — which is why `windsor-meta-ingest` is **not deployed yet**.
- The loader has only been run **from a laptop**. The Cloud Run job is still unbuilt.

---

## Build it by PORTING, not from scratch

A complete, production-proven sibling exists in the other estate. **Port it; do not reinvent it.**

**Source:** `C:\Users\Ian\Desktop\bidbrain\bidbrain-analytics\ingest\windsor_data_pull\meta\`
(`meta_loader.py` 635 lines · `create_meta_table.py` 188 lines · plus its `README.md`)

Its shared design, all of which we want:

| Behaviour | Detail |
|---|---|
| **Date chunking** | `CHUNK_DAYS = 3` for Meta. Capped-backoff retry on timeout/429/5xx; **fail-fast on permanent 4xx** (bad field / auth) so a scheduled run can't hang forever. |
| **Chunk cache** | Each chunk cached to disk so a re-run doesn't re-fetch. `--force` overrides. |
| **Fidelity** | Typed columns **plus** the whole original row kept in a `raw_row` JSON column. |
| **Idempotent load** | NDJSON → GCS → staging table → **`MERGE` on `ad_id + metric_date`**. Re-pulling a day never duplicates; revised metrics overwrite. |
| **Incremental** | No args = forward from each account's own last day (`latest_dates_per_account`); a brand-new account gets a full backward-walk backfill. Two dates = fixed-range re-pull. |
| **Client tagging** | Every row tagged `client_slug` / `agency_slug`, inferred from the account name with an explicit override map. |
| **Physical layout** | Partitioned by `metric_date`, clustered. |
| **Runtime artifacts** | Chunk cache, logs, temp NDJSON under `_run/` anchored to `__file__` — never the repo root. `_run/` is gitignored. |

### What to change in the port

| bidbrain | atrium |
|---|---|
| its own project | **`agora-data-driven`** |
| dataset | **`raw_windsor`** |
| table `perf_meta` | **`perf_meta`** — kept identical on purpose, see the naming note |
| staging bucket `bidbrain-analytics-staging` | ⚠️ **atrium has no staging bucket yet — create one** |
| `CLIENT_TO_AGENCY` keyword map | **`ACCOUNT_TO_CLIENT`** — see the inventory below |
| secret `windsor-api-key` | **`windsor-api-key`** (same name; see the secret note) |

The request + transform helpers are **already written and tested** in
[`windsor_api.py`](windsor_api.py) — `force_ipv4()`, `get()` (with `select_accounts` + retry),
`canonicalise()`, `sum_on_collision()`, `split_pull()`, `merge_history()`, `fetch_breakdowns()`,
`totals()`. Build the loader's `transform()` on top of those rather than duplicating them.

### Naming: `perf_meta`, not `meta`

The stub targeted `raw_windsor.meta`. Renamed to **`perf_meta`** on 2026-07-30 to match bidbrain, so
the port stays close to a copy and the two estates don't diverge. The rename was **free** — the
loader had never run, so nothing downstream referenced the old name (verified by grep across
`*.sql`, `*.py`, `*.ps1`).

---

## 🔴 ONE Windsor key sees EVERY account

Verified live 2026-07-30: a single key returned **13 accounts / 4,751 rows** over `last_365d` with
`select_accounts` omitted. So this really is **one job for the whole estate** — no per-client loop,
no per-client key.

**Therefore the per-client Windsor secrets are redundant.** `rhe-windsor-key`,
`honeytribe-windsor-key`, `meloyelo-windsor-key`, `riverdance-windsor-key` (+ a TCS key) are almost
certainly the same credential stored five times — five rotation liabilities for one secret.
Consolidate onto the single `windsor-api-key` this unit already expects.

### Account inventory → `ACCOUNT_TO_CLIENT`

Windsor `account_name` on the left, the client key that owns it on the right. Figures are
`last_365d` as at 2026-07-30, kept only to show which accounts are actually live.

| Windsor `account_name` | rows | spend | `client_slug` | Notes |
|---|---:|---:|---|---|
| `rhe` | 1,113 | $72,126 | `rhe` | |
| `Stuart Baker` | 336 | $25,117 | `rhe` | RHE account 2 of 3 — a sequential era, not a separate brand |
| `Super Cashflow Development` | 137 | $14,231 | `rhe` | RHE account 3 of 3 |
| `The Contract Shop` | 885 | $13,529 | `tcs` | **the reason this loader is being finished** |
| `HoneyTribe` | 677 | $11,182 | `honeytribe` | |
| `MeloYelo` | 397 | $16,578 | `meloyelo` | |
| `S7000` | 267 | $2,325 | **`service7000`** | ⚠️ see the isolation warning |
| `INTO Schüleraustausch` | 20 | $470 | **`into`** | ⚠️ same warning — and its own docs list this account as unverified; it is live |
| `Riverdance Ad Account` | 94 | $2,871 | `riverdance` | |
| `Agora Data Driven` | 6 | $1,750 | `agora` | internal, not a customer |
| `4786451891457735, PHP` | 549 | **$147,546** | **`None` (explicit)** | **biggest spender in the estate — no client folder, no dashboard** |
| `Sabbath Spa` | 161 | **$32,041** | **`None` (explicit)** | no client folder, no dashboard |
| `ASL Logistics` | 109 | $4,892 | **n/a — NOT META** | `datasource=google_ads`; excluded by the connector filter, not by the slug map |

The map is keyed on **`account_id`** (in `meta_loader.py`), with an `account_name` fallback for the
re-granted-connector case. An entry mapping to **`None`** means *known, deliberately unmapped* —
its rows load with `client_slug` NULL, which matches no view. An account **absent** from the map
gets the same NULL slug **plus a loud warning**, so "new account" and "decided against" never look
alike.

> ### 🔴 S7000 needs TWO slugs, never one
>
> `client_S7000`'s binding requirement is that **INTO and Service 7000 must never see each other's
> data** — enforced today by three separate payload objects and per-scope IAM conditions on
> `resource.name`. A single `s7000` slug would collapse that boundary at the view layer and leak one
> brand's spend into the other's dashboard. Map `S7000 → service7000` and
> `INTO Schüleraustausch → into`, and give each its own view.

> ### ⚠️ Three unmapped accounts with live spend
>
> `PHP`, `Sabbath Spa` and `ASL Logistics` total **~$184k/yr** and have no client folder at all.
> Decide deliberately: either map them to new clients, or exclude them explicitly in
> `ACCOUNT_TO_CLIENT` so their rows land with a known-null slug rather than silently defaulting into
> somebody else's view.

> ### ⚠️ A re-granted connector can change the account id
>
> bidbrain hit this: a lapsed-then-re-granted Windsor connector minted a **new** opaque account id
> and the loader kept skipping until `SELECT_ACCOUNTS` was repointed. Prefer matching on
> `account_name` where you can, and check the "configured accounts are: …" hint after any re-grant.

---

## How to run

```powershell
# 1. shared dataset (idempotent, once per project)
.\.venv\Scripts\python.exe services\ingest\create_dataset.py

# 2. the shared staging bucket (idempotent) -- NEW 2026-07-30; every loader stages NDJSON here
.\.venv\Scripts\python.exe services\ingest\create_staging_bucket.py

# 3. the raw table (idempotent)
.\.venv\Scripts\python.exe services\ingest\meta\create_meta_table.py

# 4. the loader -- no args = incremental per account, whole estate
.\.venv\Scripts\python.exe services\ingest\meta\meta_loader.py
#    fixed range:   ... meta_loader.py 2026-05-25 2026-05-30
#    one client:    ... meta_loader.py --only tcs          (slug, account id, or name substring)
#    bigger chunks: ... meta_loader.py 2025-07-30 2026-07-29 --only tcs --chunk-days 60
#    no writes:     ... meta_loader.py --only tcs --dry-run
#    ignore cache:  ... meta_loader.py --force
```

**On `--chunk-days`.** The default stays bidbrain's `CHUNK_DAYS = 3`. A Windsor call costs ~25-30s
almost regardless of range, so on a low-volume account a 3-day chunk makes a year-long backfill
~120 calls / ~1 hour; TCS's whole year is 2,276 rows and backfilled in **3.5 minutes** at
`--chunk-days 60`. The MERGE is idempotent either way, so the only risk of a bigger chunk is a
slower single request. Leave the daily scheduled run on the default.

**Accounts are DISCOVERED, not hardcoded** (a deliberate difference from bidbrain's
`SELECT_ACCOUNTS`). One key sees the whole estate, and the estate has already been surprised by a
live account nobody knew about — PHP, at $147k/yr. Every run asks Windsor what exists and prints a
loud `🔴 NEW ACCOUNT` warning for anything missing from `ACCOUNT_TO_CLIENT`, loading it with a NULL
slug so it cannot land in another client's view.

Auth is **Application Default Credentials** for BigQuery + Storage, and the Windsor key comes from
Secret Manager (`windsor-api-key`) — so the same code runs locally after
`gcloud auth application-default login` and on Cloud Run unchanged.

In production: Cloud Run job **`windsor-meta-ingest`**, cron **`20 1 * * *`**, deployed by
[`tools/deploy_ingest_jobs.ps1`](../../../tools/deploy_ingest_jobs.ps1), whose `$JOBS` array is the
single source of truth for which connectors exist. Its `windsor-meta` row is already present.

**Schedule ordering matters:** the raw loaders must land **before** the client `*-export` jobs run,
or every dashboard's rebuild reads yesterday's raw data.

## Probe before you hardcode a field list

`windsor_api.py` is import-safe, so a throwaway probe is the fastest way to answer "what does this
account actually return?". Two measured reasons never to assume:

- The window cap is caused by **specific fields** (`unique_actions_*`), not the field count — the
  same 18-field pull reached `last_1095d` once those two were split into their own request.
- **Purchase-field labels differ per account.** `riverdance` returns
  `actions_offsite_conversion_fb_pixel_purchase` and `action_values_omni_purchase`, not
  `actions_purchase`. Confirm the label; don't guess it.
