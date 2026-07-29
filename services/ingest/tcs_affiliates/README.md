# tcs_affiliates — Tapfiliate roster + conversions (direct-API)

Pulls the Tapfiliate **affiliate roster** and **affiliate-attributed conversions** into
`raw_windsor.tcs_affiliates` and `raw_windsor.tcs_affiliate_conversions`.

| | |
|---|---|
| Raw tables | `raw_windsor.tcs_affiliates`, `raw_windsor.tcs_affiliate_conversions` (asia-southeast1) |
| Secret | `tcs-tapfiliate-key` (read from Secret Manager via ADC) |
| Job | `tcs-affiliates-ingest`, daily at 02:45 Asia/Singapore |
| Grain | one row per affiliate / per conversion **per pull** (append-only) |

## This replaces a manual spreadsheet paste

The old notebook gave up on this API — its comments record that conversions came back with no
customer email — and fell back to a human pasting CSVs into the `[Insert Here] Affiliate Sales`
and `[Insert Here] Affiliate sign up` tabs each month. That was a standing source of staleness.

The API serves the roster and the conversions perfectly well. What it does *not* reliably carry
is buyer identity — which is a Shopify→Tapfiliate integration gap, not a reason to keep pasting
spreadsheets.

## 🔴 The join key is weak — do not assume otherwise

`external_id` (the Shopify order number) and `customer` are **frequently null** on this
account, so affiliate conversions **cannot** be reliably joined to Shopify orders by id alone.

The loader deliberately stores `external_id` raw and un-coerced rather than inventing a key,
and **logs the fill rate on every run** — that percentage is the single number deciding whether
affiliate revenue can be tied to orders exactly or only heuristically:

```
external_id present on 41/4400 conversions (0.9%) -- the rest cannot be joined to a Shopify order by id.
```

The notebook's workaround, which any downstream model should reuse, was a two-part match:
exact order-number match where present, otherwise a same-day (±1 day) amount/email proximity
match. The `click` block (`click_referrer`, `click_landing_page`) is kept because it is often
the only usable signal about where an affiliate sale actually originated.

## Commission totals

A conversion can carry several commissions (tiers, sub-affiliates). The loader stores:

- `commission_total` — every commission on the conversion
- `commission_approved_total` — only those with `approved: true`
- `commissions_json` — the raw array

Unapproved commission is a **liability, not earned payout**; collapsing the two would overstate
affiliate cost, so they are kept apart.

## Pagination

Tapfiliate paginates with a `page` query param and advertises the last page in a `Link` header.
The loader walks pages until one comes back **short** (fewer rows than `per_page`) or empty —
a termination condition that does not depend on parsing `Link`, whose format has changed
between API revisions.

## Run it

```powershell
.\services\ingest\deploy_tcs_ingest.ps1 -Only tcs-affiliates -Run
```
