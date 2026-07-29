"""TCS Tapfiliate affiliates + conversions loader (DIRECT-API, not Windsor).

Raw targets: raw_windsor.tcs_affiliates              (the affiliate roster / sign-ups)
             raw_windsor.tcs_affiliate_conversions   (affiliate-attributed sales + commissions)
             (shared raw layer; project agora-data-driven, dataset raw_windsor,
             location asia-southeast1).
Source     : Tapfiliate REST API v1.6.
Cadence    : daily scheduled pull (see services/ingest/deploy_tcs_ingest.ps1).

WHY THIS REPLACES A MANUAL PASTE: the old notebook gave up on this API (its comments record
that conversions came back with no customer email) and fell back to a human pasting CSVs into
`[Insert Here] Affiliate Sales` / `[Insert Here] Affiliate sign up` tabs each month. That is a
standing source of staleness. The API serves the roster and the conversions perfectly well --
what it does NOT reliably carry is the buyer identity, which is a Shopify->Tapfiliate
integration gap, not a reason to keep pasting spreadsheets. So this loader takes everything
the API has and is honest about the join key (below).

>>> THE JOIN KEY IS WEAK -- DO NOT ASSUME OTHERWISE. <<<
  `external_id` (the Shopify order number) and `customer` are frequently null on this account,
  so affiliate conversions CANNOT be reliably joined to Shopify orders by id alone. The old
  notebook worked around this with a two-part match: exact order-number match where present,
  otherwise a same-day (+/- 1 day) amount/email proximity match. Any downstream model must keep
  that caveat; the loader deliberately stores `external_id` raw and un-coerced rather than
  inventing a key. The `click` block (referrer + landing page) is kept because it is often the
  only usable signal about where an affiliate sale actually came from.

Grain: one row per affiliate / per conversion PER PULL, appended. Staging views keep the newest
row per id, so a conversion whose commissions later get approved shows its latest state while
the history of that change is preserved.

Auth:
  * Tapfiliate API key from Secret Manager (secret ``tcs-tapfiliate-key``) via ADC.
  * BigQuery via ADC (ingest-runner@ on Cloud Run).
"""

import json
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests
from google.cloud import bigquery, secretmanager
from urllib3.exceptions import ProtocolError

PROJECT = os.environ.get("GCP_PROJECT", "agora-data-driven")
RAW_DATASET = os.environ.get("RAW_DATASET", "raw_windsor")
LOCATION = "asia-southeast1"
AFFILIATES_TABLE = f"{PROJECT}.{RAW_DATASET}.tcs_affiliates"
CONVERSIONS_TABLE = f"{PROJECT}.{RAW_DATASET}.tcs_affiliate_conversions"

TAPFILIATE_SECRET = "tcs-tapfiliate-key"
TAPFILIATE_BASE = "https://api.tapfiliate.com/1.6"

PULL_VERSION = 1
RUN_BUDGET_SEC = int(os.environ.get("RUN_BUDGET_SEC", "3000"))
MAX_RETRIES = int(os.environ.get("TAPFILIATE_MAX_RETRIES", "6"))
PAGE_SIZE = int(os.environ.get("TAPFILIATE_PAGE_SIZE", "100"))

AFFILIATES_SCHEMA = [
    bigquery.SchemaField("id", "STRING"),
    bigquery.SchemaField("email", "STRING"),
    bigquery.SchemaField("firstname", "STRING"),
    bigquery.SchemaField("lastname", "STRING"),
    bigquery.SchemaField("company_name", "STRING"),
    bigquery.SchemaField("company_description", "STRING"),
    bigquery.SchemaField("address", "STRING"),
    bigquery.SchemaField("address_two", "STRING"),
    bigquery.SchemaField("postal_code", "STRING"),
    bigquery.SchemaField("city", "STRING"),
    bigquery.SchemaField("state", "STRING"),
    bigquery.SchemaField("country_code", "STRING"),
    bigquery.SchemaField("country_name", "STRING"),
    bigquery.SchemaField("created_at", "TIMESTAMP"),
    bigquery.SchemaField("promoted_at", "TIMESTAMP"),
    bigquery.SchemaField("promotion_method", "STRING"),
    bigquery.SchemaField("parent_id", "STRING"),
    bigquery.SchemaField("affiliate_group_id", "STRING"),
    bigquery.SchemaField("meta_data_json", "STRING"),
    bigquery.SchemaField("custom_fields_json", "STRING"),
    bigquery.SchemaField("loaded_at", "TIMESTAMP"),
    bigquery.SchemaField("pull_version", "INT64"),
]

CONVERSIONS_SCHEMA = [
    bigquery.SchemaField("id", "STRING"),
    bigquery.SchemaField("created_at", "TIMESTAMP"),
    bigquery.SchemaField("amount", "FLOAT64"),
    bigquery.SchemaField("currency", "STRING"),
    # The Shopify order number -- OFTEN NULL on this account. See the module docstring.
    bigquery.SchemaField("external_id", "STRING"),
    bigquery.SchemaField("customer_id", "STRING"),
    bigquery.SchemaField("affiliate_id", "STRING"),
    bigquery.SchemaField("affiliate_firstname", "STRING"),
    bigquery.SchemaField("affiliate_lastname", "STRING"),
    bigquery.SchemaField("program_id", "STRING"),
    bigquery.SchemaField("program_title", "STRING"),
    bigquery.SchemaField("click_created_at", "TIMESTAMP"),
    bigquery.SchemaField("click_referrer", "STRING"),
    bigquery.SchemaField("click_landing_page", "STRING"),
    bigquery.SchemaField("commission_total", "FLOAT64"),
    bigquery.SchemaField("commission_approved_total", "FLOAT64"),
    bigquery.SchemaField("commission_count", "INT64"),
    bigquery.SchemaField("commissions_json", "STRING"),
    bigquery.SchemaField("meta_data_json", "STRING"),
    bigquery.SchemaField("loaded_at", "TIMESTAMP"),
    bigquery.SchemaField("pull_version", "INT64"),
]


def read_secret(secret_id: str) -> str:
    sm = secretmanager.SecretManagerServiceClient()
    name = f"projects/{PROJECT}/secrets/{secret_id}/versions/latest"
    return sm.access_secret_version(request={"name": name}).payload.data.decode("utf-8")


def _get(url: str, headers: Dict[str, str], params: Optional[Dict] = None) -> Any:
    """GET with retry: honour 429 Retry-After, retry transient network / 5xx."""
    attempt = 0
    delay = 2.0
    while True:
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=90)
            if resp.status_code == 429:
                wait = float(resp.headers.get("Retry-After", 5) or 5)
                print(f"[tcs_affiliates] 429 rate-limited; sleeping {wait:.0f}s")
                time.sleep(wait)
                continue
            if resp.status_code >= 500:
                raise requests.exceptions.HTTPError(f"HTTP {resp.status_code}")
            resp.raise_for_status()
            return resp.json()
        except (requests.exceptions.ConnectionError,
                requests.exceptions.ChunkedEncodingError,
                requests.exceptions.Timeout,
                requests.exceptions.HTTPError,
                ProtocolError,
                ValueError) as e:
            if isinstance(e, requests.exceptions.HTTPError) and "HTTP 5" not in str(e):
                raise
            attempt += 1
            if attempt >= MAX_RETRIES:
                raise RuntimeError(f"Tapfiliate GET failed after {attempt} attempts: {e}") from e
            print(f"[tcs_affiliates] transient error ({e}); retry {attempt}/{MAX_RETRIES} in {delay:.0f}s")
            time.sleep(delay)
            delay = min(delay * 2, 60)


def fetch_all(endpoint: str, headers: Dict[str, str], started: float) -> List[Dict[str, Any]]:
    """Page through a Tapfiliate collection endpoint.

    Tapfiliate paginates with a `page` query param and advertises the last page in a Link
    header. We simply walk pages until one comes back SHORT (fewer rows than requested) or
    empty, which is the robust termination condition -- it does not depend on parsing the
    Link header, whose format has changed between API revisions."""
    rows: List[Dict[str, Any]] = []
    page = 1
    while True:
        if time.monotonic() - started > RUN_BUDGET_SEC:
            print(f"[tcs_affiliates] run budget reached while paging {endpoint} "
                  f"at page {page}; partial pull kept.")
            break
        batch = _get(f"{TAPFILIATE_BASE}/{endpoint}/", headers,
                     {"page": page, "per_page": PAGE_SIZE})
        if not isinstance(batch, list) or not batch:
            break
        rows.extend(batch)
        if len(batch) < PAGE_SIZE:
            break  # short page == last page
        page += 1
        if page % 10 == 0:
            print(f"[tcs_affiliates] {endpoint}: {len(rows)} rows so far (page {page})")
    print(f"[tcs_affiliates] {endpoint}: {len(rows)} rows total")
    return rows


def _f(v) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _js(v) -> Optional[str]:
    """JSON-encode a nested blob, or None when it is empty -- an empty `{}` string would look
    like real content in the warehouse."""
    if not v:
        return None
    return json.dumps(v, separators=(",", ":"), default=str)


def transform_affiliate(a: Dict[str, Any], loaded_at: str) -> Dict[str, Any]:
    company = a.get("company") or {}
    addr = a.get("address") or {}
    country = addr.get("country") or {}
    return {
        "id": a.get("id"),
        "email": (a.get("email") or "").lower().strip() or None,
        "firstname": a.get("firstname"),
        "lastname": a.get("lastname"),
        "company_name": company.get("name"),
        "company_description": company.get("description"),
        "address": addr.get("address"),
        "address_two": addr.get("address_two"),
        "postal_code": addr.get("postal_code"),
        "city": addr.get("city"),
        "state": addr.get("state"),
        "country_code": country.get("code"),
        "country_name": country.get("name"),
        "created_at": a.get("created_at"),
        "promoted_at": a.get("promoted_at"),
        "promotion_method": a.get("promotion_method"),
        "parent_id": (a.get("parent") or {}).get("id") or a.get("parent_id"),
        "affiliate_group_id": a.get("affiliate_group_id"),
        "meta_data_json": _js(a.get("meta_data")),
        "custom_fields_json": _js(a.get("custom_fields")),
        "loaded_at": loaded_at,
        "pull_version": PULL_VERSION,
    }


def transform_conversion(c: Dict[str, Any], loaded_at: str) -> Dict[str, Any]:
    click = c.get("click") or {}
    program = c.get("program") or {}
    affiliate = c.get("affiliate") or {}
    commissions = c.get("commissions") or []

    # A conversion can carry several commissions (tiers, sub-affiliates). Total them, and total
    # the APPROVED ones separately -- unapproved commission is a liability, not earned payout,
    # and collapsing the two would overstate affiliate cost.
    total = sum(_f(x.get("amount")) or 0.0 for x in commissions)
    approved = sum(_f(x.get("amount")) or 0.0 for x in commissions if x.get("approved"))

    customer = c.get("customer")
    customer_id = None
    if isinstance(customer, dict):
        customer_id = customer.get("id") or customer.get("email")
    elif customer:
        customer_id = str(customer)

    return {
        "id": str(c.get("id")) if c.get("id") is not None else None,
        "created_at": c.get("created_at"),
        "amount": _f(c.get("amount")),
        "currency": program.get("currency"),
        "external_id": c.get("external_id"),
        "customer_id": customer_id,
        "affiliate_id": affiliate.get("id"),
        "affiliate_firstname": affiliate.get("firstname"),
        "affiliate_lastname": affiliate.get("lastname"),
        "program_id": program.get("id"),
        "program_title": program.get("title"),
        "click_created_at": click.get("created_at"),
        "click_referrer": click.get("referrer"),
        "click_landing_page": click.get("landing_page"),
        "commission_total": total,
        "commission_approved_total": approved,
        "commission_count": len(commissions),
        "commissions_json": _js(commissions),
        "meta_data_json": _js(c.get("meta_data")),
        "loaded_at": loaded_at,
        "pull_version": PULL_VERSION,
    }


def ensure_table(bq: bigquery.Client, fqtn: str, schema) -> None:
    table = bq.create_table(bigquery.Table(fqtn, schema=schema), exists_ok=True)
    have = {f.name for f in table.schema}
    missing = [f for f in schema if f.name not in have]
    if missing:
        table.schema = list(table.schema) + missing
        bq.update_table(table, ["schema"])
        print(f"[tcs_affiliates] {fqtn} schema widened: +{', '.join(f.name for f in missing)}")


def append_rows(bq: bigquery.Client, fqtn: str, schema, rows: List[Dict[str, Any]]) -> None:
    if not rows:
        return
    job_config = bigquery.LoadJobConfig(
        write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
        source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
        schema=schema,
    )
    bq.load_table_from_json(rows, fqtn, job_config=job_config).result()


def main() -> None:
    started = time.monotonic()
    key = read_secret(TAPFILIATE_SECRET)
    headers = {"X-Api-Key": key, "Content-Type": "application/json"}
    bq = bigquery.Client(project=PROJECT, location=LOCATION)

    ensure_table(bq, AFFILIATES_TABLE, AFFILIATES_SCHEMA)
    ensure_table(bq, CONVERSIONS_TABLE, CONVERSIONS_SCHEMA)

    loaded_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    affiliates = fetch_all("affiliates", headers, started)
    append_rows(bq, AFFILIATES_TABLE, AFFILIATES_SCHEMA,
                [transform_affiliate(a, loaded_at) for a in affiliates])

    conversions = fetch_all("conversions", headers, started)
    conv_rows = [transform_conversion(c, loaded_at) for c in conversions]
    append_rows(bq, CONVERSIONS_TABLE, CONVERSIONS_SCHEMA, conv_rows)

    # Surface the join-key fill rate every run: it is the single number that decides whether
    # affiliate revenue can be tied to Shopify orders exactly or only heuristically.
    with_ext = sum(1 for r in conv_rows if r["external_id"])
    print(f"[tcs_affiliates] done: {len(affiliates)} affiliate(s), {len(conv_rows)} conversion(s). "
          f"external_id present on {with_ext}/{len(conv_rows)} conversions "
          f"({(100.0 * with_ext / len(conv_rows)) if conv_rows else 0:.1f}%) -- the rest cannot be "
          f"joined to a Shopify order by id.")


if __name__ == "__main__":
    main()
