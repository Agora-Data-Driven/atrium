# Client dashboard build playbook

Everything learned building the **Honey Tribe** dashboard (Shopify + Meta, 2026-07-27), written so
the next client build does not re-discover it. Read this before starting; most of the entries below
cost real hours the first time.

Worked examples in the repo: [`clients/client_honeytribe/`](../clients/client_honeytribe/) (live
API pull) and [`clients/client_riverdance/`](../clients/client_riverdance/) (the original
Windsor-live pattern). The BigQuery-fed pattern is
[`clients/client_template/`](../clients/client_template/).

---

## 1. Pick the pattern first

| | **Windsor/API-LIVE** | **BigQuery-fed** |
|---|---|---|
| Examples | `client_honeytribe`, `client_riverdance` | `client_template`, `client_TCS` |
| Infra | bucket + job + web service. **No dataset, no SQL views** | + dataset + `sql/*.sql` views |
| Use when | the fields you need are not in `raw_windsor`, or the source is not Windsor at all (Shopify Admin API) | the data already flows through `raw_windsor` |

**Honey Tribe went LIVE** because Shopify order/line/session data and Meta's rich fields are not in
the shared mirror. Default to LIVE for any new e-commerce client unless you have checked
`raw_windsor` actually holds what you need.

Everything is derived from one short key `<c>`: dataset `client_<c>`, bucket
`agora-data-driven-<c>-dash`, job `<c>-export`, service `<c>-dash`, SAs `<c>-dash-job` / `<c>-dash-web`,
secrets `<c>-dash-password` / `<c>-dash-session-key` / `<c>-<source>-key`. Never re-type these.

---

## 2. Do these five things before writing any code

1. **Git-ignore the client's context drop immediately.** Before anything else, create
   `clients/client_<c>/.gitignore` with `context/` and `data/`. Client hand-offs routinely contain
   **live API tokens and customer PII**. Honey Tribe's drop had a live Shopify Admin token, a live
   Windsor key, and 9,000 customers' emails/addresses/phones. Verify with
   `git check-ignore -v <path>` — do not trust that you remembered.
2. **Test the credentials before designing anything.** A five-minute probe script that hits each API
   and prints row counts + date ranges tells you what is actually available. On Honey Tribe this
   turned "what token do you still need?" into "none — all four endpoints already work", and
   revealed that the client's spreadsheet was a **stale snapshot** while the live API had 10 more
   months of orders.
3. **Never trust a spreadsheet export as the data source.** It is a point-in-time snapshot. Honey
   Tribe's workbook ended 2025-09-08; the live API had orders through yesterday. Build against the
   API; use the workbook only as a no-credentials fallback.
4. **Emit no PII.** Reduce customers to a salted hash used only for distinct counts and
   first-time/returning. Then *assert* it: scan the built JSON for `@`, phone patterns and token
   prefixes as part of your audit.
5. **Get the metric definitions in writing.** See §6 — a metric that disagrees with the client's
   existing report destroys trust faster than a missing feature.

---

## 3. The traps that cost the most time

### 3.1 IPv6 on Cloud Run — the expensive one
Cloud Run has **no IPv6 egress route**, but Shopify and Windsor publish AAAA records.
`socket.create_connection` walks `getaddrinfo` in order and applies the **full timeout to each
address**, so every request burned its entire 120 s connect timeout on the v6 address before
falling back to v4.

- Symptom: requests take *exactly* your connect-timeout value. A 38-page crawl became a
  **76-minute** job and blew the 30-minute task timeout. Locally it was 80 seconds.
- Fix: force A-record resolution at import time. Same backfill then ran in **133 s**.

```python
if os.environ.get("FORCE_IPV4", "1") == "1":
    _real = socket.getaddrinfo
    def _v4(host, port, family=0, type=0, proto=0, flags=0):
        return _real(host, port, socket.AF_INET, type, proto, flags)
    socket.getaddrinfo = _v4
```

**Put this in every new job that talks to a third-party API.**

### 3.2 Python buffers stdout — the job looks hung
Without `PYTHONUNBUFFERED=1`, Python holds every `print` until exit. The first Honey Tribe run
logged *nothing* for 15 minutes and was indistinguishable from a hang. Set it in the Dockerfile
(`ENV PYTHONUNBUFFERED=1`) **and** the job env — you will be reading these logs under pressure.

### 3.3 Full crawls do not scale — go incremental
Re-pulling the whole order history every run is fine at 1,000 orders and fatal at 10,000.

- Fetch only `updated_at_min = now - N days` (30 is a good default) and **merge by the source's
  primary id** into the previous publication.
- Keep the source id (`oid`) on **both** orders and line rows so the merge can rebuild.
- An order that has *become* non-revenue (refunded, cancelled) must be **removed**, not left stale.
- **Always recompute first-time/returning and any index across the WHOLE merged set** — never
  incrementally, or a returning customer whose first order predates the window is misclassified.
- Keep a `FULL_SYNC=1` escape hatch and use it on the first run.

### 3.4 Know each API's real limits before designing the date model
- **Windsor `all`**: accepts a `date_preset` **only** — `date_from`/`date_to` and `maximum` return
  400. It also **caps the response**: the full 19-field list works at `last_365d` but 400s at
  `last_730d`. Two-field queries work at `last_1095d`, so *test with your real field list*.
- Because of that cap, **history must accumulate**: each run fetches 12 months and merges older rows
  forward from the previous publication, keyed by `(date, campaign, adset, ad)`. The fresh pull stays
  authoritative for every day it covers, so restatements land correctly.
- **Meta breakdowns are separate pulls.** Meta will not return age/gender/region alongside per-ad/day
  rows, and rejects revenue fields on a breakdown query. Breakdowns carry delivery metrics only
  (spend/impressions/clicks/link clicks/reach). Use a shorter window for per-day breakdowns.
- **Shopify sessions (ShopifyQL)** are month-granular and need a plan exposing the Analytics API.

### 3.5 ShopifyQL returns objects, not row arrays
`tableData.rows` comes back as objects keyed by column name. Zipping them against `columns` pairs
column names with column names and yields rows of **literal header text** — silently. 580 rows of
garbage shipped before an audit caught it. Handle both shapes:

```python
out.append(dict(r) if isinstance(r, dict) else dict(zip(cols, r)))
```

### 3.6 Self-referrals are not traffic
A store's own second domain shows up as a referrer. Honey Tribe sells on `shopmidgetgiraffe.com`
while its myshopify handle is `midget-giraffe` — **the hyphen defeated the substring match**, so
**1,412 orders and 5,381 sessions** were credited to an "external" source.

- Read the store's real domains at runtime (`shop.json`) rather than hardcoding.
- **Strip punctuation from both sides before comparing.**
- Collapse own domains *and* on-site apps (wishlist, reviews, checkout) to `direct`.
- Route **orders and sessions through one shared normaliser**. If the two series do not speak the
  same vocabulary, any "visitors vs buyers" chart is meaningless.

### 3.7 Derive customer type at ORDER level
The upstream notebook computed first-time/returning with a row-wise `cumcount`, which mislabels the
2nd..nth **line item** of a multi-line order as "Returning". Sort orders by (customer, date) and
assign per order.

### 3.8 Shopify CSV exports repeat the order header only on the first row
Later line rows carry the order name and blanks. Forward-fill per order before aggregating, or every
order-level total is undercounted.

### 3.9 IAM: `run.developer`, not `run.invoker`
Triggering a Cloud Run job **with env overrides** needs `run.jobs.runWithOverrides`, which
`roles/run.invoker` does **not** carry. An invoker-only grant 403s on every tick while the IAM policy
looks correct — this left riverdance stale for 13 days. Grant `roles/run.developer` on the job.

### 3.10 The esprima gate
Inline dashboard JS is parsed by `tools/_validate_dash_js.py` (esprima 4.x), which predates optional
chaining and nullish coalescing. **No `?.`, no `??`** — use classic `&&` / `||`. Run the gate before
every deploy; the standup script does it for you.

### 3.11 Windows + UTF-8
Never write `workspace/*.json` or any JSON payload in text mode on Windows — cp1252 turns a smart
quote into `0x92` and the UTF-8 read blows up. Write bytes. Same for secret temp files: UTF-8, no
BOM, no trailing newline.

### 3.12 Small ones worth knowing
- `/healthz` is intercepted by Google's frontend on Cloud Run and 404s before reaching the container.
  Harmless, but do not waste time debugging it — test an unknown path instead and confirm you get
  *Flask's* 404, which proves the app is serving.
- Import `jsonify` if you use it. A route referencing a missing name imports fine and 500s only when
  called.
- Cloud Run caps fixed-length responses at ~32 MiB; stream chunked for anything larger.

---

## 4. Make the dashboard honest about time

**Anchor relative date presets to the latest date IN THE DATA, not to `today`.** Feeds lag, and
different feeds lag differently — Honey Tribe's Shopify data ran a day behind while Meta ran to the
same day. "Last 30 days" against `today` on a lagging feed shows an empty dashboard and looks broken.

Show, in the header: **when the data was generated** ("Updated 20 min ago") and **what it covers**
("Data through Jul 26"), and turn the latter red past a threshold. A dashboard that cannot tell you
it is stale is worse than no dashboard.

Give the user a **Sync** button. If it can trigger the export job, do that; otherwise re-fetch the
payload. Either way it must show a spinner and update the timestamp. If the endpoint is un-gated,
give it a **cooldown keyed on the data object's age** so repeat clicks cannot run up paid API calls.

---

## 5. Two independent date ranges

Clients compare **actual vs. a benchmark average**. Those are two different questions and need two
different controls:

- **Period** — drives every tile, chart and table.
- **KPI benchmark** — drives only the per-week / per-month average columns.

Give each its own presets *and* its own from/to pickers, tint the benchmark control differently
end-to-end, and print a line under the controls saying which drives what. Default the benchmark to
**the whole of the previous calendar year**, not a rolling 12 months: for a seasonal business a
rolling window silently over-weights whichever season it starts in.

---

## 6. Reverse-engineer the client's existing report — and challenge it

Their Looker/Sheets report is the spec *and* the trust baseline. Recompute its headline numbers and
match them before adding anything. Honey Tribe's `$1.18M` matched exactly, which bought credibility
for everything else.

Verify each definition against their own printed values rather than assuming:
`CTR = link clicks / impressions` · `CPM = spend / impressions * 1000` ·
`conversion rate = purchases / link clicks` · `ROAS = value / spend` ·
`frequency = mean of the per-row frequency` (reach is **not** additive across days, so
impressions ÷ reach understates it).

**When a definition is ambiguous, show both and ask.** Honey Tribe's report labelled
"AOV $211.18", which was sales ÷ *customers*, while its own weekly card used sales ÷ *orders*. We
shipped both, flagged it, and the client chose sales ÷ customers — because the metric exists to
compare first-timer against returning spend, so the denominator has to be people. Guessing would
have been wrong 50% of the time on the number they look at most.

---

## 7. Taxonomies: learn them, do not hand-code them

The client's product categories lived in one curated sheet naming ~240 of ~580 products. Its labels
were driven by the garment-type word in the title, so we **learned** that rule instead of copying it:

1. exact title match;
2. else the type token — a token appearing in **≥2 distinct titles** *and* **≥60% pure**. Purity
   matters: "denim" spans jackets, skirts and palazzos, so it predicts nothing and must be rejected;
3. else a small hand-written fallback list;
4. else "Uncategorized" — a real bucket in their taxonomy, not a failure.

That took uncategorised units from **72.6% → 18.2%**. Freeze the learned model to a JSON asset so the
live job classifies identically without the sheet. Ask the client for a product export with
`product_type`/tags to close the remainder.

---

## 8. Audit before you call it done

Write a script that recomputes everything **independently of the dashboard** and cross-checks against
the source API. It found two real bugs on Honey Tribe (§3.5, §3.6) that no amount of looking at the
screen would have. Cover:

- **Freshness** — lag vs today, per feed.
- **Row counts vs the source API** — and explain every difference (ours 9,222 vs API 9,333 = 51
  cancelled + 60 refunded).
- **A single-day spot check** against the API.
- **Referential integrity** — every line resolves to an order; dates/types agree.
- **Derivation correctness** — 0 customers misclassified first-time/returning.
- **Monotonic funnels** — impressions ≥ link clicks ≥ purchases.
- **Cross-source totals** — trend units == line units.
- **PII leak check** — 0 emails, no tokens, no phone patterns, ids are hashes.

Record known-and-accepted quirks explicitly (orders with no customer id; revenue with no purchase in
the same row — an attribution-window artefact), so they are not re-investigated later.

---

## 9. Verify in a real browser, not by reading code

Drive the deployed page with Playwright (`channel:'chrome'`, no browser download needed) and assert:
every filter, every cross-filter, table sorts, tab switches — and **zero `pageerror`/console errors**.
Screenshot each tab and actually look at it. Two bugs on Honey Tribe were only visible this way: the
Weekly and MTD cards showing identical values, and a mis-scaled axis.

Test **against the live URL after deploying**, not just localhost. Assert the auth posture explicitly
in both directions (authenticated → 200, unauthenticated → 401).

---

## 10. Deploy

Use `clients/client_honeytribe/deploy_honeytribe.ps1` as the template — one-shot, idempotent:
APIs → Artifact Registry + private bucket → SAs + least-privilege IAM → secrets → export job (build,
deploy, first run) → `run.developer` grants → scheduler → dash service.

- **Build as yourself, never Cloud Build from a laptop** — the Cloud Build SA cannot `actAs` the
  runtime SA. `gcloud builds submit --tag` (image only) is fine.
- **Never `--allow-unauthenticated`** — org policy forbids it. Deploy with `--no-invoker-iam-check`
  and do auth in-process.
- **The bucket is always private.** What the `DASH_OPEN` flag controls is only whether the *service*
  asks for a password. Open access is right when the dashboard is embedded in the gated Atrium
  workspace (a login inside the iframe is a dead end) — but be explicit with the client that anyone
  with the URL can then read it: a Cloud Run URL is unguessable, not secret.
- Give the job a generous `--task-timeout` and enough memory; seed the bucket if you need history
  the first run cannot fetch.
- **Rotate any credential that arrived in a shared document** once you are live, then re-run the
  standup with the new values.

### Git
The repo pushes to a per-machine branch → PR → `main` (`tools/push-branch.ps1`,
`docs/dev-workflow.md`). Do not commit other people's in-progress work along with yours. Scan the
staged diff for token patterns before committing.

⚠️ **Check which remote you are actually on.** There are two similarly-named GitHub repos —
`Agora-Data-Driven/atrium` (active) and `Agoradatadriven/Agora-Data-Driven-Portal` (a mirror, stale
since 2026-07-20). They share history, so they look identical. Confirm push access with
`gh api repos/<owner>/<repo> --jq .permissions.push` before assuming a 403 is a credential problem.

---

## 11. What to ask the client up front

1. **Access to the existing report** (view access beats screenshots) + which page matters most.
2. **Which metrics they actually run the weekly meeting on**, and the exact definition of each
   ambiguous one (see §6).
3. **Credentials** — but check the ones you already have first; they are often in the handover.
4. **The account/store identifiers** (ad account id, myshopify domain, and any additional storefront
   domains).
5. **Brand kit** — logo (vector if possible), colours, type.
6. **Targets** — budget, target ROAS/CPA — so the dashboard can say "ahead" or "behind", not just
   report numbers.
7. **A product catalogue export** with `product_type`/tags if the client sells products.
