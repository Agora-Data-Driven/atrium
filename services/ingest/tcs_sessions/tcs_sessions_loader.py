"""TCS Shopify storefront-sessions loader (DIRECT-API, not Windsor).

Raw target : raw_windsor.tcs_shopify_sessions  (the shared raw layer; project
             agora-data-driven, dataset raw_windsor, location asia-southeast1).
Source     : Shopify Admin GraphQL `shopifyqlQuery` -- `FROM sessions`.
Cadence    : daily scheduled pull (see services/ingest/deploy_tcs_ingest.ps1).

WHY: nothing else in the TCS stack measures TRAFFIC. Orders tell you who bought; sessions
tell you how many people showed up and where they came from, which is the denominator of
every conversion-rate question ("is conversion down because fewer people buy, or because
different traffic arrives?"). Ported from the "Sessions" section of
clients/TCS/archive_code/analytics.py.

Grain: one row per (hour, referrer_source, referrer_name, referrer_url, session_city,
landing_page_url) with a session count -- exactly the ShopifyQL group-by.

>>> THE 1000-ROW CAP IS THE WHOLE DESIGN PROBLEM. <<<
  shopifyqlQuery returns AT MOST 1000 rows and does NOT tell you it truncated -- a month-wide
  query comes back with exactly 1000 rows and looks perfectly successful. Measured on this
  store: 1 day ~271 rows, 1 week ~851, 1 month 1000 (i.e. silently cut). So a fixed window
  size is unsafe: it is right for quiet history and lossy for busy weeks.
  fetch_window therefore treats "row count >= ROW_CAP" as PROOF OF TRUNCATION and recursively
  halves the window until every piece comes back under the cap. Quiet periods still cost one
  request per week; only busy ones pay for the split.

INCREMENTAL, HOLE-AWARE, RESUMABLE: coverage is the SET of days already loaded (read from the
table), the work list is the set difference against the target span, and each run chips away
at it newest-first within RUN_BUDGET_SEC. Interior holes cannot survive repeated runs. The
trailing RECHECK_DAYS are always re-pulled because Shopify's sessions data settles for a day
or two after the fact; the day-level de-dupe in the staging view keeps the newest load.

HISTORY: Shopify's sessions analytics only reaches back ~2022-09 on this store (2021 and
earlier return nothing at all), so BACKFILL_START defaults there rather than to the store's
2017 founding -- asking for 2017 sessions is not an error, it just silently returns zero rows
forever.

Auth:
  * Shopify Admin API token from Secret Manager (secret ``tcs-shopify-token``) via ADC.
  * BigQuery via ADC (ingest-runner@ on Cloud Run).
"""

import os
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set

import requests
from google.cloud import bigquery, secretmanager
from urllib3.exceptions import ProtocolError

PROJECT = os.environ.get("GCP_PROJECT", "agora-data-driven")
RAW_DATASET = os.environ.get("RAW_DATASET", "raw_windsor")
LOCATION = "asia-southeast1"
TABLE = "tcs_shopify_sessions"
FQTN = f"{PROJECT}.{RAW_DATASET}.{TABLE}"

SHOPIFY_SECRET = "tcs-shopify-token"
SHOPIFY_STORE_DOMAIN = os.environ.get("SHOPIFY_STORE_DOMAIN", "contractshop.myshopify.com")
# 2025-10 and 2024-01 both serve shopifyqlQuery identically; pin the newer one.
SHOPIFY_API_VERSION = os.environ.get("SHOPIFY_SESSIONS_API_VERSION", "2025-10")

PULL_VERSION = 1

# Earliest day worth asking for -- Shopify sessions analytics starts ~2022-09 on this store.
BACKFILL_START = os.environ.get("BACKFILL_START", "2022-09-01")
# Always re-pull this many trailing days: Shopify's session attribution keeps settling for a
# day or so, so yesterday's numbers can still move.
RECHECK_DAYS = int(os.environ.get("RECHECK_DAYS", "3"))
RUN_BUDGET_SEC = int(os.environ.get("RUN_BUDGET_SEC", "3000"))
MAX_RETRIES = int(os.environ.get("SHOPIFY_MAX_RETRIES", "6"))

# ShopifyQL's hard result cap. Hitting it EXACTLY means the result was truncated.
ROW_CAP = 1000

DIMENSIONS = ["hour", "referrer_source", "referrer_name", "referrer_url",
              "session_city", "landing_page_url"]

SCHEMA = [
    bigquery.SchemaField("hour", "TIMESTAMP"),
    bigquery.SchemaField("referrer_source", "STRING"),
    bigquery.SchemaField("referrer_name", "STRING"),
    bigquery.SchemaField("referrer_url", "STRING"),
    bigquery.SchemaField("session_city", "STRING"),
    bigquery.SchemaField("landing_page_url", "STRING"),
    bigquery.SchemaField("sessions", "INT64"),
    # When this row was pulled. The staging view keeps the newest load per (day, dimensions)
    # so a re-pulled trailing day supersedes rather than double-counts.
    bigquery.SchemaField("loaded_at", "TIMESTAMP"),
    bigquery.SchemaField("pull_version", "INT64"),
]


def read_secret(secret_id: str) -> str:
    sm = secretmanager.SecretManagerServiceClient()
    name = f"projects/{PROJECT}/secrets/{secret_id}/versions/latest"
    return sm.access_secret_version(request={"name": name}).payload.data.decode("utf-8")


def _shopifyql(token: str, query: str) -> Dict[str, Any]:
    """POST a ShopifyQL query with retry on 429 / transient network / 5xx."""
    url = f"https://{SHOPIFY_STORE_DOMAIN}/admin/api/{SHOPIFY_API_VERSION}/graphql.json"
    headers = {"X-Shopify-Access-Token": token, "Content-Type": "application/json"}
    gql = '{ shopifyqlQuery(query: "%s") { tableData { columns { name } rows } } }' % query
    attempt = 0
    delay = 2.0
    while True:
        try:
            resp = requests.post(url, headers=headers, json={"query": gql}, timeout=120)
            if resp.status_code == 429:
                wait = float(resp.headers.get("Retry-After", 4) or 4)
                print(f"[tcs_sessions] 429 rate-limited; sleeping {wait:.0f}s")
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
                raise RuntimeError(f"ShopifyQL POST failed after {attempt} attempts: {e}") from e
            print(f"[tcs_sessions] transient error ({e}); retry {attempt}/{MAX_RETRIES} in {delay:.0f}s")
            time.sleep(delay)
            delay = min(delay * 2, 60)
            continue

        errors = payload.get("errors")
        if errors:
            throttled = any((e.get("extensions") or {}).get("code") == "THROTTLED"
                            for e in errors if isinstance(e, dict))
            if throttled:
                print("[tcs_sessions] THROTTLED; backing off 3s")
                time.sleep(3)
                continue
            raise RuntimeError(f"ShopifyQL error: {errors}")
        return payload


def _query_range(token: str, start: date, end: date) -> List[Dict[str, Any]]:
    """One raw ShopifyQL call for [start, end). Returns the raw row dicts (may be truncated)."""
    # ShopifyQL's UNTIL is INCLUSIVE, so ask for the day before our exclusive end.
    until = end - timedelta(days=1)
    q = (f"FROM sessions SHOW sessions GROUP BY {', '.join(DIMENSIONS)} "
         f"SINCE {start.isoformat()} UNTIL {until.isoformat()}")
    payload = _shopifyql(token, q)
    table = (((payload.get("data") or {}).get("shopifyqlQuery") or {}).get("tableData") or {})
    return table.get("rows") or []


def fetch_window(token: str, start: date, end: date) -> List[Dict[str, Any]]:
    """All session rows for [start, end), splitting the window whenever ShopifyQL truncates.

    A result of exactly ROW_CAP rows is indistinguishable from a truncated one, so we treat it
    as truncated and halve the window. A SINGLE DAY that still hits the cap cannot be split any
    further by date -- we keep what came back and say so loudly, because silently returning a
    capped day would understate that day's traffic with no trace in the data."""
    rows = _query_range(token, start, end)
    span_days = (end - start).days

    if len(rows) < ROW_CAP:
        return rows

    if span_days <= 1:
        print(f"[tcs_sessions] WARNING: {start} hit the {ROW_CAP}-row cap as a single day; "
              f"that day's session detail is incomplete (cannot split a date range further).")
        return rows

    mid = start + timedelta(days=max(1, span_days // 2))
    print(f"[tcs_sessions] {start}..{end} hit the row cap; splitting at {mid}")
    return fetch_window(token, start, mid) + fetch_window(token, mid, end)


def _to_row(raw: Dict[str, Any], loaded_at: str) -> Dict[str, Any]:
    def s(key):
        v = raw.get(key)
        return None if v in (None, "") else str(v)

    sessions = raw.get("sessions")
    try:
        sessions = int(sessions) if sessions is not None else 0
    except (TypeError, ValueError):
        sessions = 0

    return {
        "hour": raw.get("hour"),
        "referrer_source": s("referrer_source"),
        "referrer_name": s("referrer_name"),
        "referrer_url": s("referrer_url"),
        "session_city": s("session_city"),
        "landing_page_url": s("landing_page_url"),
        "sessions": sessions,
        "loaded_at": loaded_at,
        "pull_version": PULL_VERSION,
    }


def ensure_table(bq: bigquery.Client) -> None:
    table = bq.create_table(bigquery.Table(FQTN, schema=SCHEMA), exists_ok=True)
    have = {f.name for f in table.schema}
    missing = [f for f in SCHEMA if f.name not in have]
    if missing:
        table.schema = list(table.schema) + missing
        bq.update_table(table, ["schema"])
        print(f"[tcs_sessions] schema widened: +{', '.join(f.name for f in missing)}")


def append_rows(bq: bigquery.Client, rows: List[Dict[str, Any]]) -> None:
    if not rows:
        return
    job_config = bigquery.LoadJobConfig(
        write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
        source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
        schema=SCHEMA,
    )
    bq.load_table_from_json(rows, FQTN, job_config=job_config).result()


def covered_days(bq: bigquery.Client) -> Set[date]:
    """Days already loaded at the current pull version -- the checkpoint.

    Caveat, same as the sibling loaders: a day on which the store genuinely had ZERO sessions
    leaves no row, so it can never look covered and is re-requested every run. On a live store
    that is essentially hypothetical, and one empty query is a cheap price for never silently
    treating a missing day as a loaded one."""
    sql = f"""
        SELECT DATE(hour) AS d, MIN(COALESCE(pull_version, 1)) AS min_ver
        FROM `{FQTN}`
        GROUP BY d
    """
    return {r["d"] for r in bq.query(sql, location=LOCATION).result()
            if r["min_ver"] >= PULL_VERSION}


def target_days(today: date) -> List[date]:
    """Every day from BACKFILL_START up to yesterday, NEWEST FIRST.

    Today is excluded: it is still accumulating sessions, and the trailing-recheck logic will
    pick it up tomorrow anyway."""
    start = date.fromisoformat(BACKFILL_START)
    days: List[date] = []
    cur = today - timedelta(days=1)
    while cur >= start:
        days.append(cur)
        cur -= timedelta(days=1)
    return days


def missing_days(bq: bigquery.Client, today: date) -> List[date]:
    """Work list: uncovered days, PLUS the trailing RECHECK_DAYS which are always re-pulled
    because Shopify's session numbers keep settling for a day or two after the fact."""
    covered = covered_days(bq)
    recheck_floor = today - timedelta(days=RECHECK_DAYS)
    return [d for d in target_days(today) if d not in covered or d >= recheck_floor]


def _contiguous_runs(days: List[date]) -> List[tuple]:
    """Group a NEWEST-FIRST day list into contiguous [start, end) ranges, newest range first.

    Batching matters: pulling 1,400 individual days would be 1,400 requests, but a quiet week
    of history fits in one call. fetch_window splits any range that turns out to be too busy,
    so grouping greedily costs nothing in correctness."""
    if not days:
        return []
    runs = []
    run_end = days[0] + timedelta(days=1)  # exclusive
    run_start = days[0]
    for d in days[1:]:
        if d == run_start - timedelta(days=1):
            run_start = d
        else:
            runs.append((run_start, run_end))
            run_end = d + timedelta(days=1)
            run_start = d
    runs.append((run_start, run_end))
    return runs


def _chunk(start: date, end: date, max_days: int = 7):
    """Split a contiguous range into <= max_days pieces so one failure costs little and each
    load job stays small. Yields newest piece first."""
    cur_end = end
    while cur_end > start:
        cur_start = max(start, cur_end - timedelta(days=max_days))
        yield cur_start, cur_end
        cur_end = cur_start


def main() -> None:
    started = time.monotonic()
    token = read_secret(SHOPIFY_SECRET)
    bq = bigquery.Client(project=PROJECT, location=LOCATION)
    ensure_table(bq)

    today = datetime.now(timezone.utc).date()
    todo = missing_days(bq, today)
    print(f"[tcs_sessions] {len(todo)} day(s) to load (incl. {RECHECK_DAYS}-day trailing recheck)")

    loaded_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    total = 0
    days_done = 0
    stopped_early = False

    for run_start, run_end in _contiguous_runs(todo):
        for win_start, win_end in _chunk(run_start, run_end):
            if time.monotonic() - started > RUN_BUDGET_SEC:
                print(f"[tcs_sessions] run budget ({RUN_BUDGET_SEC}s) reached at {win_start}; "
                      f"remaining days resume next tick.")
                stopped_early = True
                break
            raw = fetch_window(token, win_start, win_end)
            rows = [_to_row(r, loaded_at) for r in raw]
            append_rows(bq, rows)
            total += len(rows)
            days_done += (win_end - win_start).days
            print(f"[tcs_sessions] {win_start}..{win_end}: +{len(rows)} rows (run total {total})")
        if stopped_early:
            break

    print(f"[tcs_sessions] done: +{total} rows across ~{days_done} day(s); "
          f"{len(todo) - days_done} day(s) deferred (target span from {BACKFILL_START}).")


if __name__ == "__main__":
    main()
