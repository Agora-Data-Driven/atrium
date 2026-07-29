# CLAUDE.md — clients/client_honeytribe (Shopify + Meta, Windsor-LIVE)

**Rules live in the repo-root [`/CLAUDE.md`](../../CLAUDE.md)** — read it first; this file only adds
local context. If they disagree, root wins.

Honey Tribe is a **Windsor/Shopify-LIVE client** — the [`client_riverdance`](../client_riverdance/)
pattern, **not** the BigQuery-fed [`client_template`](../client_template/). There is **no dataset and
no SQL views**; `job/main.py` pulls Meta (Windsor `all` connector) and Shopify (Admin REST +
ShopifyQL) directly each run and writes `honeytribe.json` itself. Two-stage contract:

```
job/main.py (data dict key)  ->  dash/dashboard.html (DATA.* key)
```

- **`job/build_local.py`** is the no-credentials fallback: it reads `context/*.xlsx` and emits the
  **same** JSON shape minus what only an API can give. **Keep the two shapes in step** — changing
  one without the other breaks the local preview or the deploy.
- **`job/categorize.py`** is the one product-title → category classifier, learned from the client's
  trend sheet and frozen into `assets/categories.json`. Both callers use it; never fork the logic.
- **`normalize_platform()` in `job/main.py` is the ONE referrer vocabulary.** Orders (via
  `referring_site` / `utm_source`) and Shopify sessions (via `referrer_name`) both go through it,
  which is the only reason the visitors-vs-buyers chart is comparable. Own storefront domains and
  on-site apps collapse to `direct` — punctuation is stripped before matching, because
  `midget-giraffe` must match `shopmidgetgiraffe.com` (it did not, and 1,412 orders were
  misattributed until the audit caught it).
- **`dash/dashboard.html`** is one self-contained file; inline JS must be **esprima-4.x-safe**
  (no `?.`, no `??`). Three tabs: `sales` · `funnel` · `product`, selected by URL hash.
- **The creative gallery (funnel tab) takes its numbers from `meta.rows`, never its own pull.**
  `creative_id` rides on the main Meta pull for free (measured: 813 rows and identical spend with
  and without it) and lands on every row as `cid`; `fetch_creatives()` pulls only the ad's *text
  and image* keyed by that id, and `cache_creative_images()` copies the busiest
  `CREATIVE_CACHE_MAX` (60) images into our own bucket because Meta's CDN links expire when an ad
  stops. Served through the authed `/creative-img/<cid>` route — the bucket stays private. One
  source of numbers is what keeps the gallery from disagreeing with the tiles above it.

## Two traps that already bit this client (do not undo these)

- **`FORCE_IPV4` at the top of `job/main.py` is load-bearing.** Cloud Run has no IPv6 egress route
  but Shopify and Windsor publish AAAA records, and `socket.create_connection` applies the FULL
  timeout to each address in turn — so every request burned its whole 120 s connect timeout on the
  v6 address before falling back. Measured: 120 s *per call*, which turned the 38-page backfill
  into a 76-minute job that blew the 30-minute task timeout. With A-records-only the same backfill
  is **133 s**. If requests suddenly take exactly the connect-timeout value, this is why.
- **The Shopify pull is INCREMENTAL** (`updated_at_min`, last `SHOPIFY_INCREMENTAL_DAYS`=30),
  merged by Shopify order id into the previous publication. `SHOPIFY_FULL_SYNC=1` forces a
  backfill. Orders and lines therefore BOTH carry `oid` — do not drop it, or every run silently
  degrades to a full crawl. First-time/returning and the line→order index are always recomputed
  across the whole merged set, never incrementally.
- **Meta's grey "no preview" tile is a VALID image, so `onerror` never saves you.** For a creative
  with no real image Meta returns its external image PROXY —
  `external-<edge>.xx.fbcdn.net/emg1/…?url=<page>`, a link preview of the destination page rather
  than the ad. It loads fine, so no `error` event fires, the branded-tile fallback never runs, and
  three Honey Tribe cards rendered as grey boxes. `_is_link_preview()`/`_usable_image()` reject it
  at the source. ⚠️ The first fix was "any image whose bytes are shared by 2+ creatives is the
  placeholder" — **wrong, and it deleted real artwork**: this account genuinely reuses one
  1080×1080 image (65k colours) across two ad variants. Duplication is not the signal; the URL is.
  `tools/_creative_gallery_test.py` guards both halves.

## Windows and API limits (do not "fix" these — they are the API's)

- Windsor's `all` endpoint takes a **`date_preset` only** (`date_from`/`date_to` and `maximum` 400)
  and caps a wide-field pull at ~12 months (`last_730d` 400s with the full field list). Each run
  therefore fetches 365 days and **`merge_history()` carries older rows forward** so history
  accumulates in the bucket. Do not raise `DATE_PRESET` without re-testing the field list.
- Meta rejects age/gender/region alongside the per-ad/day rows, and rejects revenue on a breakdown
  query — breakdowns are separate pulls carrying delivery metrics only.
- Shopify sessions are month-granular and only exist from 2024-01 for this store.
- ShopifyQL returns rows as **objects keyed by column name** (not positional arrays). Zipping them
  against the column list silently produces rows of header text — `_shopifyql` handles both shapes.

## ⚠️ `context/` is git-ignored and holds LIVE credentials

A live Shopify Admin token, a live Windsor API key and customer PII. Never commit it, never echo a
key into a file, and rotate both before standup. The emitted JSON contains **no PII** — customers
are a salted hash only. Keep it that way.

## Local preview

```powershell
$env:WINDSOR_API_KEY="…"; $env:SHOPIFY_ACCESS_TOKEN="…"
$env:HONEYTRIBE_LOCAL_OUT="clients\client_honeytribe\data\honeytribe.json"
python clients\client_honeytribe\job\main.py            # the real pull, ~80 s
python clients\client_honeytribe\preview_local.py 8137  # -> http://localhost:8137
```

## Metric definitions

CTR = link clicks ÷ impressions · CPM = spend ÷ impressions × 1000 · conversion rate = purchases ÷
link clicks · ROAS = value ÷ spend · frequency = **mean of Meta's per-row frequency** (reach is not
additive across days). **`AOV` is sales ÷ CUSTOMERS** — the client's decision (2026-07-27), because
the metric exists to compare first-timer against returning spend; sales ÷ orders is shown as
`Per order`. The **reporting period** and the **KPI benchmark** are two independent ranges: the
period drives everything, the benchmark drives only the per-week/per-month average columns. Full
table in [README.md](README.md).

## Deploy

`deploy_honeytribe.ps1` — one-shot, idempotent, no dataset and no views. It validates the JS gate,
builds both images as you, and grants **`roles/run.developer`** (not `run.invoker`) on the export
job to the portal SA and the web SA. Re-running rotates the secrets to whatever you pass.

**There is deliberately NO Cloud Scheduler** (client decision 2026-07-29, matching `client_RHE`
and `client_MeloYelo`): refresh runs off the dashboard's **Sync button**, because each run costs
paid Windsor/Meta and Shopify calls. Honey Tribe is not in the portal registry either, so the
platform-wide `sync-refresh` never reached it — the Sync button is genuinely the only trigger,
which is what makes that `run.developer` grant load-bearing rather than a nicety
(`run.invoker` does NOT carry `runWithOverrides`). Running the script without `-WithScheduler`
REMOVES any scheduler it finds, so re-running converges on that state instead of quietly leaving
a timer behind. The `/refresh` route's 10-minute cooldown (keyed on the data object's age, so it
is shared across instances) is what stops repeat clicks running up the bill — do not remove it
while `DASH_OPEN=1`, since there is no login in front of that route.

Derived names: bucket `agora-data-driven-honeytribe-dash` · job `honeytribe-export` · service
`honeytribe-dash` · secrets `honeytribe-{dash-password,dash-session-key,windsor-key,shopify-token}`.

Never Cloud Build from a laptop; never `--allow-unauthenticated` (org policy forbids it — the app
does its own password/SSO auth behind `--no-invoker-iam-check`).
