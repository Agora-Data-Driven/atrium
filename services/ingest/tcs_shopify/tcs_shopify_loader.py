"""TCS Shopify orders loader (DIRECT-API, not Windsor).

Raw target : raw_windsor.tcs_shopify_orders  (the shared raw layer; project
             agora-data-driven, dataset raw_windsor, location asia-southeast1).
Source     : Shopify Admin GraphQL API (the TCS / Contract Shop store).
Cadence    : daily scheduled pull (see services/ingest/deploy_tcs_ingest.ps1).

WHY THIS IS A DIRECT-API LOADER (a documented exception to "Windsor is the only
ingest source"): TCS's Business-Quiz diagnostic needs order-level Shopify data joined
to per-recipient Klaviyo events, a grain Windsor does not serve for this account. This
loader ports the proven pull from clients/TCS/archive_code/analytics.py.

WHAT IT PULLS (v2, 2026-07-28): the full order record the old notebook had, not just the
money columns. Specifically the MARKETING ATTRIBUTION block -- customerJourneySummary
(days-to-conversion, first/last visit source, landing page, referrer, UTM parameters) and the
sales channel -- which v1 dropped on the floor. Without those columns there is no channel
attribution, no UTM/campaign ROI and no landing-page analysis, so every marketing dashboard
in the notebook was unbuildable. Also added: customer geo + lifetime counters, financial /
fulfilment status, cancellation, order tags, and the refund / shipping / tax totals needed
for real AOV and net-revenue maths.

INCREMENTAL, HOLE-AWARE, RESUMABLE (rewritten 2026-07-28):
  The loader walks CALENDAR MONTHS of created_at and APPENDS each month atomically (one
  BigQuery load job per month), so recent orders land first and a timeout only ever costs the
  current month.

  >>> THE CHECKPOINT IS PER-MONTH COVERAGE, NOT MIN/MAX. <<<
  The previous version checkpointed on MIN(created_at) and only walked DOWNWARD from it, which
  cannot express "a month in the middle is missing": once MIN reached the floor the backfill
  loop was skipped entirely and any month an earlier budget-capped run had skipped stayed empty
  forever. (That failure mode cost the sibling Klaviyo loader 12 months of history.) Coverage is
  now read from the table as a SET of months, the work list is the SET DIFFERENCE against the
  target span, and repeated runs necessarily converge.

  Each run does FORWARD (orders updated since the last run) then BACKFILL (missing months,
  newest-first) until RUN_BUDGET_SEC is exhausted; the next tick resumes. Requests retry on
  transient errors and pace against Shopify's cost-based throttle. Rows carry the order id +
  updated_at so stg_orders can de-dupe / keep the latest version.

PULL VERSION: rows record the loader version that wrote them. v1 rows lack every attribution
column, and NULL there is indistinguishable from "no attribution recorded" -- so a month counts
as covered only at the CURRENT version, and stale months are automatically re-queued. stg_orders
keeps the highest-pull_version row per order id.

Auth:
  * Shopify Admin API token from Secret Manager (secret ``tcs-shopify-token``) via ADC.
  * BigQuery via ADC (the ingest-runner@ service account on Cloud Run).
"""

import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set

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

# Schema/semantics version of the rows this build writes. BUMP when a change makes older rows
# incomplete (v2 added the attribution block); months are re-pulled until every row matches.
PULL_VERSION = 2

# Earliest month worth asking for. The store's first order (#1001) is 2017-10-15, so this is
# the true beginning of history. Override to shorten a run (e.g. BACKFILL_START=2025-01).
BACKFILL_START = os.environ.get("BACKFILL_START", "2017-10")
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
        bigquery.SchemaField("variant_title", "STRING"),
        bigquery.SchemaField("sku", "STRING"),
        bigquery.SchemaField("quantity", "INT64"),
        bigquery.SchemaField("price", "NUMERIC"),
        bigquery.SchemaField("vendor", "STRING"),
    ]),
    # --- v2: MARKETING ATTRIBUTION (Shopify's customerJourneySummary) ---------------------
    # The block the whole "where do sales come from" question depends on.
    bigquery.SchemaField("days_to_convert", "INT64"),
    bigquery.SchemaField("moments_count", "INT64"),
    bigquery.SchemaField("first_visit_source", "STRING"),
    bigquery.SchemaField("first_visit_source_type", "STRING"),
    bigquery.SchemaField("first_visit_landing_page", "STRING"),
    bigquery.SchemaField("first_visit_referrer_url", "STRING"),
    bigquery.SchemaField("utm_source", "STRING"),
    bigquery.SchemaField("utm_medium", "STRING"),
    bigquery.SchemaField("utm_campaign", "STRING"),
    bigquery.SchemaField("utm_term", "STRING"),
    bigquery.SchemaField("last_visit_source", "STRING"),
    bigquery.SchemaField("last_visit_landing_page", "STRING"),
    bigquery.SchemaField("sales_channel", "STRING"),
    # --- v2: customer dimension ----------------------------------------------------------
    bigquery.SchemaField("customer_id", "INT64"),
    bigquery.SchemaField("customer_city", "STRING"),
    bigquery.SchemaField("customer_province", "STRING"),
    bigquery.SchemaField("customer_country", "STRING"),
    bigquery.SchemaField("customer_orders_count", "INT64"),
    bigquery.SchemaField("customer_total_spent", "NUMERIC"),
    # --- v2: order state + net-revenue components ----------------------------------------
    bigquery.SchemaField("financial_status", "STRING"),
    bigquery.SchemaField("fulfillment_status", "STRING"),
    bigquery.SchemaField("cancel_reason", "STRING"),
    bigquery.SchemaField("cancelled_at", "TIMESTAMP"),
    bigquery.SchemaField("tags", "STRING", mode="REPEATED"),
    bigquery.SchemaField("total_refunded", "NUMERIC"),
    bigquery.SchemaField("total_shipping", "NUMERIC"),
    bigquery.SchemaField("total_tax", "NUMERIC"),
    bigquery.SchemaField("pull_version", "INT64"),
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


def _int(val) -> Optional[int]:
    if val is None:
        return None
    try:
        return int(val)
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


def _money(node: Dict[str, Any], key: str) -> Optional[float]:
    """Shopify money fields are all `{ shopMoney { amount } }`; any level can be null."""
    return _num(((node.get(key) or {}).get("shopMoney") or {}).get("amount"))


# GraphQL: one page of orders (created_at window, oldest->newest within the window) with the
# fields the TCS models read downstream. customerJourneySummary is the attribution block.
QUERY = """
query($cursor: String, $q: String) {
  orders(first: %d, after: $cursor, query: $q, sortKey: CREATED_AT) {
    pageInfo { hasNextPage endCursor }
    edges {
      node {
        id name createdAt updatedAt currencyCode email
        cancelReason cancelledAt displayFinancialStatus displayFulfillmentStatus tags
        customer {
          id email firstName lastName numberOfOrders
          amountSpent { amount }
          defaultAddress { city province country }
        }
        totalPriceSet { shopMoney { amount } }
        subtotalPriceSet { shopMoney { amount } }
        totalDiscountsSet { shopMoney { amount } }
        totalRefundedSet { shopMoney { amount } }
        totalShippingPriceSet { shopMoney { amount } }
        totalTaxSet { shopMoney { amount } }
        discountCodes
        channelInformation { channelDefinition { handle } }
        customerJourneySummary {
          daysToConversion
          momentsCount { count }
          firstVisit {
            source sourceType landingPage referrerUrl
            utmParameters { source medium campaign term }
          }
          lastVisit { source landingPage }
        }
        lineItems(first: 25) {
          edges { node { title variantTitle sku quantity vendor originalUnitPriceSet { shopMoney { amount } } } }
        }
      }
    }
  }
}
""" % PAGE_SIZE


def transform(node: Dict[str, Any]) -> Dict[str, Any]:
    """Map a GraphQL order node -> a raw_windsor.tcs_shopify_orders row dict.

    Every nested read is `(x or {})`-guarded: Shopify returns null for whole blocks routinely
    -- customerJourneySummary is absent on orders that predate the journey feature or came in
    off-channel, and utmParameters is null on any visit without campaign tags."""
    cust = node.get("customer") or {}
    addr = cust.get("defaultAddress") or {}

    items: List[Dict[str, Any]] = []
    for edge in ((node.get("lineItems") or {}).get("edges") or []):
        i = edge.get("node") or {}
        items.append({
            "title": i.get("title"),
            "variant_title": i.get("variantTitle"),
            "sku": i.get("sku"),
            "quantity": i.get("quantity"),
            "price": _money(i, "originalUnitPriceSet"),
            "vendor": i.get("vendor"),
        })

    raw_codes = node.get("discountCodes") or []
    discount_codes = [{"code": c} for c in raw_codes]
    primary_discount_code = raw_codes[0] if raw_codes else None

    journey = node.get("customerJourneySummary") or {}
    first = journey.get("firstVisit") or {}
    last = journey.get("lastVisit") or {}
    utm = first.get("utmParameters") or {}
    channel_def = (node.get("channelInformation") or {}).get("channelDefinition") or {}

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
        "subtotal_price": _money(node, "subtotalPriceSet"),
        "total_discounts": _money(node, "totalDiscountsSet"),
        "total_price": _money(node, "totalPriceSet"),
        "primary_discount_code": primary_discount_code,
        "discount_codes": discount_codes,
        "line_items": items,

        # Attribution.
        "days_to_convert": _int(journey.get("daysToConversion")),
        "moments_count": _int((journey.get("momentsCount") or {}).get("count")),
        "first_visit_source": first.get("source"),
        "first_visit_source_type": first.get("sourceType"),
        "first_visit_landing_page": first.get("landingPage"),
        "first_visit_referrer_url": first.get("referrerUrl"),
        "utm_source": utm.get("source"),
        "utm_medium": utm.get("medium"),
        "utm_campaign": utm.get("campaign"),
        "utm_term": utm.get("term"),
        "last_visit_source": last.get("source"),
        "last_visit_landing_page": last.get("landingPage"),
        "sales_channel": channel_def.get("handle"),

        # Customer dimension.
        "customer_id": _parse_gid(cust.get("id")),
        "customer_city": addr.get("city"),
        "customer_province": addr.get("province"),
        "customer_country": addr.get("country"),
        "customer_orders_count": _int(cust.get("numberOfOrders")),
        "customer_total_spent": _num((cust.get("amountSpent") or {}).get("amount")),

        # Order state + net-revenue components.
        "financial_status": node.get("displayFinancialStatus"),
        "fulfillment_status": node.get("displayFulfillmentStatus"),
        "cancel_reason": node.get("cancelReason"),
        "cancelled_at": node.get("cancelledAt"),
        "tags": node.get("tags") or [],
        "total_refunded": _money(node, "totalRefundedSet"),
        "total_shipping": _money(node, "totalShippingPriceSet"),
        "total_tax": _money(node, "totalTaxSet"),

        "pull_version": PULL_VERSION,
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
    """Create the table if absent, and ADD any schema columns it is missing.

    The additive ALTER is what lets v2 ship against the existing production table: a load job
    with an explicit schema fails if the destination lacks a column. Widening is always safe --
    existing rows read the new columns as NULL, which is precisely why they are re-pulled."""
    table = bq.create_table(bigquery.Table(FQTN, schema=SCHEMA), exists_ok=True)
    have = {f.name for f in table.schema}
    missing = [f for f in SCHEMA if f.name not in have]
    if missing:
        table.schema = list(table.schema) + missing
        bq.update_table(table, ["schema"])
        print(f"[tcs_shopify] schema widened: +{', '.join(f.name for f in missing)}")


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


def covered_months(bq: bigquery.Client) -> Set[datetime]:
    """The set of created_at months ALREADY loaded AT THE CURRENT PULL_VERSION.

    This -- not MIN/MAX -- is the checkpoint, so an interior hole stays in the work list until
    it is actually filled. A month with any below-version row is NOT covered, which is what
    re-queues v1 months after the attribution columns were added.

    NOTE the empty-month subtlety: a month in which the store genuinely took no orders can
    never appear here, so it is re-requested on every run. That is intentional and cheap (one
    empty page), and it is the only honest option -- "no rows" and "not loaded" are the same
    observation from the table's point of view."""
    sql = f"""
        SELECT DATE_TRUNC(DATE(created_at), MONTH) AS m,
               MIN(COALESCE(pull_version, 1))      AS min_ver
        FROM `{FQTN}`
        GROUP BY m
    """
    out: Set[datetime] = set()
    for r in bq.query(sql, location=LOCATION).result():
        if r["min_ver"] >= PULL_VERSION:
            out.add(datetime(r["m"].year, r["m"].month, 1, tzinfo=timezone.utc))
    return out


def max_updated_at(bq: bigquery.Client) -> Optional[datetime]:
    """Newest updated_at already loaded -- the start of the FORWARD phase.

    updated_at (not created_at) so an OLD order edited today -- a refund, a fulfilment, a tag --
    is re-pulled and its row refreshed; stg_orders keeps the latest version per id."""
    r = list(bq.query(f"SELECT MAX(updated_at) AS hi FROM `{FQTN}`", location=LOCATION).result())[0]
    return r["hi"]


def _parse_floor() -> datetime:
    """BACKFILL_START ('YYYY-MM') -> the first month we will ever ask Shopify for."""
    year, month = (int(x) for x in BACKFILL_START.split("-")[:2])
    return datetime(year, month, 1, tzinfo=timezone.utc)


def target_months(now: datetime) -> List[datetime]:
    """Every month from the floor up to and including the current one, NEWEST FIRST."""
    floor = _parse_floor()
    months: List[datetime] = []
    cur = _month_start(now)
    while cur >= floor:
        months.append(cur)
        cur = _month_start(cur - timedelta(microseconds=1))
    return months


def missing_months(bq: bigquery.Client, now: datetime) -> List[datetime]:
    """The work list: target months not yet covered at the current version, newest-first.
    The CURRENT month is excluded -- the FORWARD phase owns it while it is still filling."""
    covered = covered_months(bq)
    this_month = _month_start(now)
    return [m for m in target_months(now) if m != this_month and m not in covered]


def _pull_month(bq, token, month: datetime, label: str) -> int:
    win_end = _month_start(month + relativedelta(months=1))
    rows = fetch_window(token, month, win_end)
    append_rows(bq, rows)
    print(f"[tcs_shopify] {label} {month.date()}: +{len(rows)} orders")
    return len(rows)


def main() -> None:
    started = time.monotonic()
    token = read_secret(SHOPIFY_SECRET)
    bq = bigquery.Client(project=PROJECT, location=LOCATION)
    ensure_table(bq)

    now = datetime.now(timezone.utc)
    total = 0

    # -- Phase 1: FORWARD -- orders created/updated since the newest we have.
    #    stg_orders de-dupes by id keeping the latest updated_at, so overlap is harmless.
    hi = max_updated_at(bq)
    if hi is not None:
        rows = fetch_window(token, _month_start(hi), now)
        append_rows(bq, rows)
        total += len(rows)
        print(f"[tcs_shopify] forward {_month_start(hi).date()}..{now.date()}: +{len(rows)} orders")

    # -- Phase 2: BACKFILL -- pull the months that are genuinely missing, newest-first.
    todo = missing_months(bq, now)
    print(f"[tcs_shopify] {len(todo)} month(s) missing at v{PULL_VERSION}"
          f"{' (includes order-free months, which are always re-checked)' if todo else ''}")
    done_now = 0
    for month in todo:
        if time.monotonic() - started > RUN_BUDGET_SEC:
            print(f"[tcs_shopify] run budget ({RUN_BUDGET_SEC}s) reached at {month.date()}; "
                  f"{len(todo) - done_now} month(s) resume next tick.")
            break
        total += _pull_month(bq, token, month, "backfill")
        done_now += 1

    print(f"[tcs_shopify] done: +{total} orders this run; {done_now} month(s) pulled, "
          f"{len(todo) - done_now} deferred (target span from {BACKFILL_START}).")


if __name__ == "__main__":
    main()
