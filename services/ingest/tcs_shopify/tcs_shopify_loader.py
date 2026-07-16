"""TCS Shopify orders loader (DIRECT-API, not Windsor).

Raw target : raw_windsor.tcs_shopify_orders  (the shared raw layer; project
             agora-data-driven, dataset raw_windsor, location asia-southeast1).
Source     : Shopify Admin GraphQL API (the TCS / Contract Shop store).
Cadence    : daily scheduled pull (see services/ingest/deploy_tcs_ingest.ps1).

WHY THIS IS A DIRECT-API LOADER (a documented exception to "Windsor is the only
ingest source"): TCS's Business-Quiz diagnostic needs order-level Shopify data joined
to per-recipient Klaviyo events, a grain Windsor does not serve for this account. This
loader ports the proven pull from clients/TCS/archive_code/analytics.py.

INCREMENTAL, NEWEST-FIRST, RESUMABLE (rewritten 2026-07-08):
  The store has enough order history that paging it all in one shot hit Cloud Run's 3600s
  task timeout and, because it wrote all-or-nothing at the end, landed ZERO rows. This
  loader instead walks **calendar months of created_at newest-first** and APPENDS each
  month atomically (one BigQuery load job per month), so recent orders land first and a
  timeout only costs the current month. The **table itself is the checkpoint** (no
  sidecar/DB): MIN/MAX(created_at) say how far back we have gone / how recent we are.
  Each run does FORWARD (orders created since MAX) then BACKFILL (months down from MIN to
  now-BACKFILL_MONTHS) until RUN_BUDGET_SEC is exhausted; the next tick resumes.
  Requests retry on transient errors and pace against Shopify's cost-based throttle. Rows
  carry the order id + updated_at so stg_orders can de-dupe / keep the latest version.

Auth:
  * Shopify Admin API token from Secret Manager (secret ``tcs-shopify-token``) via ADC.
  * BigQuery via ADC (the ingest-runner@ service account on Cloud Run).
"""

import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import requests
from dateutil.relativedelta import relativedelta
from google.cloud import bigquery, secretmanager
from urllib3.exceptions import ProtocolError

PROJECT = os.environ.get("GCP_PROJECT", "agora-data-driven")
RAW_DATASET = os.environ.get("RAW_DATASET", "raw_windsor")
LOCATION = "asia-southeast1"
TABLE = "tcs_shopify_orders"
FQTN = f"{PROJECT}.{RAW_DATASET}.{TABLE}"

SHOPIFY_SECRET = "tcs-shopify-token"  # Secret Manager id holding the Admin API token.
SHOPIFY_STORE_DOMAIN = os.environ.get("SHOPIFY_STORE_DOMAIN", "contractshop.myshopify.com")
SHOPIFY_API_VERSION = os.environ.get("SHOPIFY_API_VERSION", "2024-01")
PAGE_SIZE = int(os.environ.get("SHOPIFY_PAGE_SIZE", "50"))

# How far back to backfill (calendar months, newest-first). 24mo = this-year vs prior-year.
BACKFILL_MONTHS = int(os.environ.get("BACKFILL_MONTHS", "24"))
# Soft wall-clock budget per run (< the 3600s Cloud Run task timeout); stop cleanly after
# the current month when exceeded and resume from the checkpoint next tick.
RUN_BUDGET_SEC = int(os.environ.get("RUN_BUDGET_SEC", "3000"))
MAX_RETRIES = int(os.environ.get("SHOPIFY_MAX_RETRIES", "6"))

# Table schema (kept in sync with create_tcs_shopify_orders_table.py). Load with an
# EXPLICIT schema so per-month appends never depend on JSON autodetect.
SCHEMA = [
    bigquery.SchemaField("id", "INT64"),
    bigquery.SchemaField("name", "STRING"),
    bigquery.SchemaField("contact_email", "STRING"),
    bigquery.SchemaField("customer_email", "STRING"),
    bigquery.SchemaField("customer_first_name", "STRING"),
    bigquery.SchemaField("customer_last_name", "STRING"),
    bigquery.SchemaField("created_at", "TIMESTAMP"),
    bigquery.SchemaField("updated_at", "TIMESTAMP"),
    bigquery.SchemaField("currency", "STRING"),
    bigquery.SchemaField("subtotal_price", "NUMERIC"),
    bigquery.SchemaField("total_discounts", "NUMERIC"),
    bigquery.SchemaField("total_price", "NUMERIC"),
    bigquery.SchemaField("primary_discount_code", "STRING"),
    bigquery.SchemaField("discount_codes", "RECORD", mode="REPEATED", fields=[
        bigquery.SchemaField("code", "STRING"),
    ]),
    bigquery.SchemaField("line_items", "RECORD", mode="REPEATED", fields=[
        bigquery.SchemaField("title", "STRING"),
        bigquery.SchemaField("sku", "STRING"),
        bigquery.SchemaField("quantity", "INT64"),
        bigquery.SchemaField("price", "NUMERIC"),
        bigquery.SchemaField("vendor", "STRING"),
    ]),
]


def read_secret(secret_id: str) -> str:
    sm = secretmanager.SecretManagerServiceClient()
    name = f"projects/{PROJECT}/secrets/{secret_id}/versions/latest"
    return sm.access_secret_version(request={"name": name}).payload.data.decode("utf-8")


def _num(val) -> Optional[float]:
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _parse_gid(gid: Optional[str]) -> Optional[int]:
    """gid://shopify/Order/12345 -> 12345."""
    if not gid:
        return None
    try:
        return int(str(gid).split("/")[-1])
    except (TypeError, ValueError):
        return None


# GraphQL: one page of orders (created_at window, oldest->newest within the window) with the
# fields the TCS quiz model reads downstream.
QUERY = """
query($cursor: String, $q: String) {
  orders(first: %d, after: $cursor, query: $q, sortKey: CREATED_AT) {
    pageInfo { hasNextPage endCursor }
    edges {
      node {
        id name createdAt updatedAt currencyCode email
        customer { id email firstName lastName }
        totalPriceSet { shopMoney { amount } }
        subtotalPriceSet { shopMoney { amount } }
        totalDiscountsSet { shopMoney { amount } }
        discountCodes
        lineItems(first: 25) {
          edges { node { title sku quantity vendor originalUnitPriceSet { shopMoney { amount } } } }
        }
      }
    }
  }
}
""" % PAGE_SIZE


def transform(node: Dict[str, Any]) -> Dict[str, Any]:
    """Map a GraphQL order node -> a raw_windsor.tcs_shopify_orders row dict."""
    cust = node.get("customer") or {}
    total = node.get("totalPriceSet") or {}
    subtotal = node.get("subtotalPriceSet") or {}
    discounts_total = node.get("totalDiscountsSet") or {}

    items: List[Dict[str, Any]] = []
    for edge in ((node.get("lineItems") or {}).get("edges") or []):
        i = edge.get("node") or {}
        price_set = i.get("originalUnitPriceSet") or {}
        items.append({
            "title": i.get("title"),
            "sku": i.get("sku"),
            "quantity": i.get("quantity"),
            "price": _num((price_set.get("shopMoney") or {}).get("amount")),
            "vendor": i.get("vendor"),
        })

    raw_codes = node.get("discountCodes") or []
    discount_codes = [{"code": c} for c in raw_codes]
    primary_discount_code = raw_codes[0] if raw_codes else None

    return {
        "id": _parse_gid(node.get("id")),
        "name": node.get("name"),
        "contact_email": node.get("email"),
        "customer_email": cust.get("email"),
        "customer_first_name": cust.get("firstName"),
        "customer_last_name": cust.get("lastName"),
        "created_at": node.get("createdAt"),
        "updated_at": node.get("updatedAt"),
        "currency": node.get("currencyCode"),
        "subtotal_price": _num((subtotal.get("shopMoney") or {}).get("amount")),
        "total_discounts": _num((discounts_total.get("shopMoney") or {}).get("amount")),
        "total_price": _num((total.get("shopMoney") or {}).get("amount")),
        "primary_discount_code": primary_discount_code,
        "discount_codes": discount_codes,
        "line_items": items,
    }


def _graphql(url, headers, variables) -> Dict[str, Any]:
    """POST the orders query with retry (429 + transient network/5xx) and cost-based throttle
    handling -- Shopify returns 200 with a THROTTLED error when the query-cost bucket is
    empty, and reports the bucket in extensions.cost.throttleStatus."""
    attempt = 0
    delay = 2.0
    while True:
        try:
            resp = requests.post(url, headers=headers,
                                 json={"query": QUERY, "variables": variables}, timeout=90)
            if resp.status_code == 429:
                wait = float(resp.headers.get("Retry-After", 4) or 4)
                print(f"[tcs_shopify] 429 rate-limited; sleeping {wait:.0f}s")
                time.sleep(wait)
                continue
            if resp.status_code >= 500:
                raise requests.exceptions.HTTPError(f"HTTP {resp.status_code}")
            resp.raise_for_status()
            payload = resp.json()
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
                raise RuntimeError(f"Shopify POST failed after {attempt} attempts: {e}") from e
            print(f"[tcs_shopify] transient error ({e}); retry {attempt}/{MAX_RETRIES} in {delay:.0f}s")
            time.sleep(delay)
            delay = min(delay * 2, 60)
            continue

        errors = payload.get("errors")
        if errors:
            throttled = any((e.get("extensions") or {}).get("code") == "THROTTLED"
                            for e in errors if isinstance(e, dict))
            if throttled:
                print("[tcs_shopify] THROTTLED; backing off 3s")
                time.sleep(3)
                continue
            raise RuntimeError(f"Shopify GraphQL error: {errors}")

        # Pace against the cost bucket: if we are running low, wait for it to refill.
        cost = (payload.get("extensions") or {}).get("cost") or {}
        ts = cost.get("throttleStatus") or {}
        avail, restore = ts.get("currentlyAvailable"), ts.get("restoreRate")
        if isinstance(avail, (int, float)) and restore and avail < 300:
            nap = min((300 - avail) / restore, 10)
            if nap > 0:
                time.sleep(nap)
        return payload


def fetch_window(token: str, start: datetime, end: datetime) -> List[Dict[str, Any]]:
    """Paginate all orders with created_at in [start, end) and return transformed rows."""
    url = f"https://{SHOPIFY_STORE_DOMAIN}/admin/api/{SHOPIFY_API_VERSION}/graphql.json"
    headers = {"X-Shopify-Access-Token": token, "Content-Type": "application/json"}
    q = (f"created_at:>={start.strftime('%Y-%m-%dT%H:%M:%SZ')} "
         f"created_at:<{end.strftime('%Y-%m-%dT%H:%M:%SZ')} status:any")

    rows: List[Dict[str, Any]] = []
    cursor: Optional[str] = None
    while True:
        payload = _graphql(url, headers, {"cursor": cursor, "q": q})
        orders = (payload.get("data") or {}).get("orders") or {}
        for edge in orders.get("edges", []):
            rows.append(transform(edge["node"]))
        page = orders.get("pageInfo") or {}
        if not page.get("hasNextPage"):
            break
        cursor = page.get("endCursor")
    return rows


def ensure_table(bq: bigquery.Client) -> None:
    bq.create_table(bigquery.Table(FQTN, schema=SCHEMA), exists_ok=True)


def table_bounds(bq: bigquery.Client):
    """(min_created_at, max_created_at) already in the table, or (None, None) if empty."""
    r = list(bq.query(f"SELECT MIN(created_at) AS lo, MAX(created_at) AS hi FROM `{FQTN}`",
                      location=LOCATION).result())[0]
    return r["lo"], r["hi"]


def append_rows(bq: bigquery.Client, rows: List[Dict[str, Any]]) -> None:
    if not rows:
        return
    job_config = bigquery.LoadJobConfig(
        write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
        source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
        schema=SCHEMA,
    )
    bq.load_table_from_json(rows, FQTN, job_config=job_config).result()


def _month_start(dt: datetime) -> datetime:
    return dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def main() -> None:
    started = time.monotonic()
    token = read_secret(SHOPIFY_SECRET)
    bq = bigquery.Client(project=PROJECT, location=LOCATION)
    ensure_table(bq)

    now = datetime.now(timezone.utc)
    floor = now - relativedelta(months=BACKFILL_MONTHS)
    lo, hi = table_bounds(bq)
    total = 0

    # -- Phase 1: FORWARD -- orders created since the newest we have (skip on an empty table).
    #    stg_orders de-dupes by id, so re-seeing the boundary order is harmless.
    if hi is not None:
        rows = fetch_window(token, hi, now)
        append_rows(bq, rows)
        total += len(rows)
        print(f"[tcs_shopify] forward {hi.date()}..{now.date()}: +{len(rows)} orders")

    # -- Phase 2: BACKFILL -- walk months newest-first from the current oldest down to floor.
    win_end = now if lo is None else _month_start(lo)
    while win_end > floor:
        if time.monotonic() - started > RUN_BUDGET_SEC:
            print(f"[tcs_shopify] run budget ({RUN_BUDGET_SEC}s) reached at {win_end.date()}; "
                  f"backfill resumes next tick.")
            break
        win_start = max(_month_start(win_end - timedelta(microseconds=1)), floor)
        rows = fetch_window(token, win_start, win_end)
        append_rows(bq, rows)
        total += len(rows)
        print(f"[tcs_shopify] backfill {win_start.date()}..{win_end.date()}: +{len(rows)} orders "
              f"(run total {total})")
        win_end = win_start

    lo2, hi2 = table_bounds(bq)
    done = lo2 is not None and lo2 <= floor + timedelta(days=1)
    print(f"[tcs_shopify] done: +{total} orders this run; table now spans "
          f"{lo2.date() if lo2 else '-'}..{hi2.date() if hi2 else '-'}; "
          f"backfill_complete={done} (target floor {floor.date()}).")


if __name__ == "__main__":
    main()
