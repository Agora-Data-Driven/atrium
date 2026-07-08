"""TCS Klaviyo email-events loader (DIRECT-API, not Windsor).

Raw target : raw_windsor.tcs_klaviyo_events  (the shared raw layer; project
             agora-data-driven, dataset raw_windsor, location asia-southeast1).
Source     : Klaviyo Events API -- Received / Opened / Clicked Email metrics.
Cadence    : daily scheduled pull (see services/ingest/deploy_tcs_ingest.ps1).

WHY THIS IS A DIRECT-API LOADER (a documented exception to "Windsor is the only ingest
source"): the Business-Quiz diagnostic ("are these quiz leads opening/clicking LESS this
year?") needs PER-RECIPIENT open/click events, which Windsor's Klaviyo connector does not
expose (it serves campaign-level aggregates). This loader ports the "Email Activity" pull
from clients/TCS/archive_code/analytics.py: it produces ONE ROW PER SEND, flagged
is_open / is_click, joined to opens/clicks by Klaviyo's per-send $message id.

INCREMENTAL, NEWEST-FIRST, RESUMABLE (rewritten 2026-07-08):
  The account is large (tens of thousands of sends/month), so a single all-history pull
  used to crash mid-stream (urllib3 ProtocolError) and, because it wrote all-or-nothing at
  the very end, lost every row. This loader instead walks **calendar months newest-first**
  and APPENDS each month atomically (one BigQuery load job per month), so:
    * recent data lands first (something to show immediately), and
    * a crash / timeout only costs the current month -- the next run resumes.
  The **table itself is the checkpoint** (no sidecar/DB): MIN/MAX(sent_at) tell us how far
  back we have gone and how recent we are. Each run does two phases:
    1. FORWARD  -- pull sends newer than MAX(sent_at) (new activity since last run).
    2. BACKFILL -- walk months down from MIN(sent_at) to now-BACKFILL_MONTHS, stopping when
                   RUN_BUDGET_SEC is exhausted (the next scheduled tick continues).
  Every request retries on 429 + transient network/5xx errors. Rows carry the Klaviyo
  event_id so stg_email_events can de-dupe (a re-run of the same window is harmless).

Grain: one row per (recipient, message) send -> exactly what client_tcs.stg_email_events
reads.

Auth:
  * Klaviyo private API key from Secret Manager (secret ``tcs-klaviyo-key``) via ADC.
  * BigQuery via ADC (ingest-runner@ on Cloud Run).
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
TABLE = "tcs_klaviyo_events"
FQTN = f"{PROJECT}.{RAW_DATASET}.{TABLE}"

KLAVIYO_SECRET = "tcs-klaviyo-key"  # Secret Manager id holding the Klaviyo private key.
KLAVIYO_BASE = "https://a.klaviyo.com/api"
KLAVIYO_REVISION = "2024-10-15"

# How far back to backfill (calendar months, newest-first). The first run covers this
# window; later ticks walk further only if this is raised. Default 24mo = this-year vs
# prior-year, which is what the dashboard diagnostic compares.
BACKFILL_MONTHS = int(os.environ.get("BACKFILL_MONTHS", "24"))
# Soft wall-clock budget per run (< the 3600s Cloud Run task timeout). When exceeded we
# stop cleanly after the current month; the next scheduled tick resumes from the checkpoint.
RUN_BUDGET_SEC = int(os.environ.get("RUN_BUDGET_SEC", "3000"))
PAGE_SIZE = int(os.environ.get("KLAVIYO_PAGE_SIZE", "200"))  # Klaviyo events max is 200.
MAX_RETRIES = int(os.environ.get("KLAVIYO_MAX_RETRIES", "6"))

# Table schema (kept in sync with create_tcs_klaviyo_events_table.py). We load with an
# EXPLICIT schema so appends never depend on JSON autodetect (which can mistype an
# all-null column in a sparse month).
SCHEMA = [
    bigquery.SchemaField("event_id", "STRING"),
    bigquery.SchemaField("message_id", "STRING"),
    bigquery.SchemaField("email", "STRING"),
    bigquery.SchemaField("subject", "STRING"),
    bigquery.SchemaField("campaign", "STRING"),
    bigquery.SchemaField("flow", "STRING"),
    bigquery.SchemaField("sent_at", "TIMESTAMP"),
    bigquery.SchemaField("opened_at", "TIMESTAMP"),
    bigquery.SchemaField("clicked_at", "TIMESTAMP"),
    bigquery.SchemaField("is_open", "BOOL"),
    bigquery.SchemaField("is_click", "BOOL"),
]


def read_secret(secret_id: str) -> str:
    sm = secretmanager.SecretManagerServiceClient()
    name = f"projects/{PROJECT}/secrets/{secret_id}/versions/latest"
    return sm.access_secret_version(request={"name": name}).payload.data.decode("utf-8")


def _headers(api_key: str) -> Dict[str, str]:
    return {
        "Authorization": f"Klaviyo-API-Key {api_key}",
        "accept": "application/json",
        "revision": KLAVIYO_REVISION,
    }


def _request_json(url: str, headers: Dict[str, str], params: Optional[Dict] = None) -> Dict[str, Any]:
    """GET with retry: honour 429 Retry-After (does not consume an attempt) and retry
    transient network / 5xx errors with exponential backoff. A 4xx (other than 429) is a
    hard failure. Body reads (resp.json) are inside the try because the original crash was a
    urllib3 ProtocolError raised WHILE streaming the response body."""
    attempt = 0
    delay = 2.0
    while True:
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=90)
            if resp.status_code == 429:
                wait = float(resp.headers.get("Retry-After", 5) or 5)
                print(f"[tcs_klaviyo] 429 rate-limited; sleeping {wait:.0f}s")
                time.sleep(wait)
                continue
            if resp.status_code >= 500:
                raise requests.exceptions.HTTPError(f"HTTP {resp.status_code}")
            resp.raise_for_status()  # 4xx -> hard fail (below), not retried
            return resp.json()
        except (requests.exceptions.ConnectionError,
                requests.exceptions.ChunkedEncodingError,
                requests.exceptions.Timeout,
                requests.exceptions.HTTPError,
                ProtocolError,
                ValueError) as e:
            # ValueError covers a truncated body that fails JSON decode.
            if isinstance(e, requests.exceptions.HTTPError) and "HTTP 5" not in str(e):
                raise  # a real 4xx (auth/bad request) -- do not retry
            attempt += 1
            if attempt >= MAX_RETRIES:
                raise RuntimeError(f"Klaviyo GET failed after {attempt} attempts: {e}") from e
            print(f"[tcs_klaviyo] transient error ({e}); retry {attempt}/{MAX_RETRIES} in {delay:.0f}s")
            time.sleep(delay)
            delay = min(delay * 2, 60)


def get_metric_map(headers: Dict[str, str]) -> Dict[str, str]:
    """Return {metric_name: metric_id} for this Klaviyo account."""
    data = _request_json(f"{KLAVIYO_BASE}/metrics", headers)
    return {m["attributes"]["name"]: m["id"] for m in data.get("data", [])}


def fetch_events(headers, metric_id, start, end, fetch_profile=False) -> List[Dict[str, Any]]:
    """Paginate the Events API for one metric within [start, end)."""
    url = f"{KLAVIYO_BASE}/events"
    start_str = start.strftime("%Y-%m-%dT%H:%M:%S")
    end_str = end.strftime("%Y-%m-%dT%H:%M:%S")
    params = {
        "filter": f'equals(metric_id,"{metric_id}"),'
                  f"greater-than(datetime,{start_str}),less-than(datetime,{end_str})",
        "sort": "-datetime",
        "page[size]": PAGE_SIZE,
    }
    if fetch_profile:
        params["include"] = "profile"

    events: List[Dict[str, Any]] = []
    while url:
        data = _request_json(url, headers, params)
        batch = data.get("data", [])
        if not batch:
            break

        if fetch_profile and "included" in data:
            profile_email = {p["id"]: (p.get("attributes") or {}).get("email")
                             for p in data["included"]}
            for ev in batch:
                pid = (((ev.get("relationships") or {}).get("profile") or {})
                       .get("data") or {}).get("id")
                ev["_email"] = profile_email.get(pid)

        events.extend(batch)
        url = (data.get("links") or {}).get("next")
        params = None  # subsequent pages carry params in the next link
    return events


def _props(ev: Dict[str, Any]) -> Dict[str, Any]:
    return (ev.get("attributes") or {}).get("event_properties") or {}


def _email_of(ev: Dict[str, Any]) -> str:
    return (ev.get("_email") or "").lower().strip()


def collect_window(headers, metrics, start, end) -> List[Dict[str, Any]]:
    """Pull one [start, end) window and emit ONE ROW PER SEND, flagged is_open / is_click.

    ATTRIBUTION IS PER-RECIPIENT: opens/clicks are matched to a send by the pair
    (recipient email, $message), NOT by $message alone. $message is the CAMPAIGN message id,
    shared by every recipient of that campaign -- keying on it alone marked EVERY recipient of
    a campaign as having opened/clicked if ANYONE did (it produced ~99% open/click rates). So we
    fetch the profile on the opens/clicks pulls too and join on (email, message).
    Opens/clicks lag sends, so their fetch window extends +7d to catch late interactions."""
    received = metrics.get("Received Email")
    opened = metrics.get("Opened Email")
    clicked = metrics.get("Clicked Email")
    if not received:
        raise RuntimeError("Klaviyo metric 'Received Email' not found for this account.")

    lag_end = end + timedelta(days=7)
    sends = fetch_events(headers, received, start, end, fetch_profile=True)
    opens = fetch_events(headers, opened, start, lag_end, fetch_profile=True) if opened else []
    clicks = fetch_events(headers, clicked, start, lag_end, fetch_profile=True) if clicked else []

    # Maps keyed by (email, $message) -> earliest interaction datetime.
    open_at: Dict[tuple, str] = {}
    for ev in opens:
        mid = _props(ev).get("$message")
        if mid:
            k = (_email_of(ev), mid)
            if k not in open_at:
                open_at[k] = ev["attributes"]["datetime"]
    click_at: Dict[tuple, str] = {}
    for ev in clicks:
        mid = _props(ev).get("$message")
        if mid:
            k = (_email_of(ev), mid)
            if k not in click_at:
                click_at[k] = ev["attributes"]["datetime"]

    rows: List[Dict[str, Any]] = []
    for ev in sends:
        p = _props(ev)
        mid = p.get("$message")
        email = _email_of(ev)
        k = (email, mid)
        rows.append({
            "event_id": ev.get("id"),
            "message_id": mid,
            "email": email or None,
            "subject": p.get("Subject"),
            "campaign": p.get("Campaign Name"),
            "flow": p.get("$flow") or "Campaign",
            "sent_at": ev["attributes"]["datetime"],
            "opened_at": open_at.get(k),
            "clicked_at": click_at.get(k),
            "is_open": k in open_at,
            "is_click": k in click_at,
        })
    return rows


def ensure_table(bq: bigquery.Client) -> None:
    bq.create_table(bigquery.Table(FQTN, schema=SCHEMA), exists_ok=True)


def table_bounds(bq: bigquery.Client):
    """(min_sent_at, max_sent_at) already in the table, or (None, None) if empty."""
    r = list(bq.query(f"SELECT MIN(sent_at) AS lo, MAX(sent_at) AS hi FROM `{FQTN}`",
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


def _month_windows(now: datetime, floor: datetime):
    """Calendar-month [start, end) windows covering [floor, now), newest-first."""
    wins = []
    win_end = now
    while win_end > floor:
        win_start = max(_month_start(win_end - timedelta(microseconds=1)), floor)
        wins.append((win_start, win_end))
        win_end = win_start
    return wins


def _run_parallel_shard(bq, headers, metrics, now, floor, started, task_index, task_count) -> None:
    """PARALLEL BACKFILL: this execution was launched with --tasks N. Each task deterministically
    owns a stride of the month windows (index-strided so heavy recent months spread across tasks)
    and appends its months atomically. No checkpoint/forward -- the whole [floor, now) span is
    covered exactly once across the fleet; stg_email_events de-dupes by event_id as insurance."""
    windows = _month_windows(now, floor)
    mine = [w for i, w in enumerate(windows) if i % task_count == task_index]
    print(f"[tcs_klaviyo] task {task_index}/{task_count}: {len(mine)} of {len(windows)} months")
    total = 0
    for win_start, win_end in mine:
        if time.monotonic() - started > RUN_BUDGET_SEC:
            print(f"[tcs_klaviyo] task {task_index}: run budget reached at {win_end.date()}.")
            break
        rows = collect_window(headers, metrics, win_start, win_end)
        append_rows(bq, rows)
        total += len(rows)
        print(f"[tcs_klaviyo] task {task_index} {win_start.date()}..{win_end.date()}: "
              f"+{len(rows)} sends (task total {total})")
    print(f"[tcs_klaviyo] task {task_index} done: +{total} sends.")


def main() -> None:
    started = time.monotonic()
    headers = _headers(read_secret(KLAVIYO_SECRET))
    bq = bigquery.Client(project=PROJECT, location=LOCATION)
    ensure_table(bq)
    metrics = get_metric_map(headers)

    now = datetime.now(timezone.utc)
    floor = now - relativedelta(months=BACKFILL_MONTHS)

    # PARALLEL MODE: when launched with --tasks N (Cloud Run sets CLOUD_RUN_TASK_COUNT>1) shard
    # the whole backfill across the tasks for a fast one-shot load. The DAILY scheduled run uses
    # 1 task and falls through to the incremental forward + resume path below.
    task_count = int(os.environ.get("CLOUD_RUN_TASK_COUNT", "1"))
    task_index = int(os.environ.get("CLOUD_RUN_TASK_INDEX", "0"))
    if task_count > 1:
        _run_parallel_shard(bq, headers, metrics, now, floor, started, task_index, task_count)
        return

    lo, hi = table_bounds(bq)
    total = 0

    # -- Phase 1: FORWARD -- new sends since the newest we have (skip on an empty table).
    if hi is not None:
        rows = collect_window(headers, metrics, hi, now)
        append_rows(bq, rows)
        total += len(rows)
        print(f"[tcs_klaviyo] forward {hi.date()}..{now.date()}: +{len(rows)} sends")

    # -- Phase 2: BACKFILL -- walk months newest-first from the current oldest down to floor.
    #    An empty table starts at `now` (so the current partial month is captured too);
    #    a resume starts at MIN(sent_at)'s month (months at/above it are already loaded).
    win_end = now if lo is None else _month_start(lo)
    while win_end > floor:
        if time.monotonic() - started > RUN_BUDGET_SEC:
            print(f"[tcs_klaviyo] run budget ({RUN_BUDGET_SEC}s) reached at {win_end.date()}; "
                  f"backfill resumes next tick.")
            break
        win_start = max(_month_start(win_end - timedelta(microseconds=1)), floor)
        rows = collect_window(headers, metrics, win_start, win_end)
        append_rows(bq, rows)
        total += len(rows)
        print(f"[tcs_klaviyo] backfill {win_start.date()}..{win_end.date()}: +{len(rows)} sends "
              f"(run total {total})")
        win_end = win_start

    lo2, hi2 = table_bounds(bq)
    done = lo2 is not None and lo2 <= floor + timedelta(days=1)
    print(f"[tcs_klaviyo] done: +{total} sends this run; table now spans "
          f"{lo2.date() if lo2 else '-'}..{hi2.date() if hi2 else '-'}; "
          f"backfill_complete={done} (target floor {floor.date()}).")


if __name__ == "__main__":
    main()
