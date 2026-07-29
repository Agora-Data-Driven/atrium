"""TCS Klaviyo email-events loader (DIRECT-API, not Windsor).

Raw target : raw_windsor.tcs_klaviyo_events  (the shared raw layer; project
             agora-data-driven, dataset raw_windsor, location asia-southeast1).
Source     : Klaviyo Events API -- Received / Opened / Clicked / Bounced / Dropped /
             Unsubscribed / Marked-as-Spam Email metrics.
Cadence    : daily scheduled pull (see services/ingest/deploy_tcs_ingest.ps1).

WHY THIS IS A DIRECT-API LOADER (a documented exception to "Windsor is the only ingest
source"): the Business-Quiz diagnostic ("are these quiz leads opening/clicking LESS this
year?") needs PER-RECIPIENT open/click events, which Windsor's Klaviyo connector does not
expose (it serves campaign-level aggregates). This loader ports the "Email Activity" pull
from clients/TCS/archive_code/analytics.py: it produces ONE ROW PER SEND, flagged
is_open / is_click / is_bounce / is_unsub / is_spam / is_dropped.

INCREMENTAL, HOLE-AWARE, RESUMABLE (rewritten 2026-07-28):
  The account is large (up to ~270k sends/month), so the whole history cannot land in one
  Cloud Run task. This loader walks CALENDAR MONTHS and APPENDS each month atomically (one
  BigQuery load job per month), so a crash / timeout only ever costs the current month.

  >>> THE CHECKPOINT IS PER-MONTH COVERAGE, NOT MIN/MAX. <<<
  The previous version checkpointed on MIN(sent_at) and only ever walked DOWNWARD from it.
  That could not express "month X in the middle is missing": once MIN reached the floor the
  backfill loop was skipped entirely, and 12 interior months (2024-09..2025-05, 2025-07,
  2025-11, 2026-04) stayed permanently empty -- they had been skipped when the parallel
  --tasks shards each ran out of RUN_BUDGET_SEC partway through their stride. Coverage is now
  read from the table as a SET of months (see covered_months), the work list is the SET
  DIFFERENCE against the target span, and every run chips away at whatever is genuinely
  missing. Holes cannot survive repeated runs, whatever order months land in.

  Each run does two phases:
    1. FORWARD  -- pull sends newer than MAX(sent_at) (new activity since the last run).
    2. BACKFILL -- pull MISSING months, newest-first, until RUN_BUDGET_SEC is exhausted.
  Every request retries on 429 + transient network/5xx errors. Rows carry the Klaviyo
  event_id so stg_email_events can de-dupe (a re-run of the same window is harmless).

PULL VERSION (why rows carry `pull_version`):
  The deliverability metrics (bounce / unsub / spam / dropped) were added in v2. Rows loaded
  by v1 have those columns NULL, which is INDISTINGUISHABLE from "this send did not bounce"
  -- a silent correctness trap for any list-health metric. So every row records the
  PULL_VERSION that produced it, and a month only counts as covered at the CURRENT version
  (see covered_months). Old v1 months are therefore re-pulled by later runs and converge to
  v2; stg_email_events keeps the highest-pull_version row per event_id.

Grain: one row per (recipient, message) send -> exactly what client_tcs.stg_email_events
reads.

Auth:
  * Klaviyo private API key from Secret Manager (secret ``tcs-klaviyo-key``) via ADC.
  * BigQuery via ADC (ingest-runner@ on Cloud Run).
"""

import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

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

# Schema/semantics version of the rows this build writes. BUMP THIS whenever a change makes
# older rows wrong or incomplete (e.g. a new flag column) -- months are re-pulled until every
# row is at the current version. See covered_months.
PULL_VERSION = 2

# Earliest month worth asking for. The account's first "Received Email" event is 2023-06-10;
# anything before that is guaranteed empty, so we do not waste a request on it. Override to
# shorten a run (e.g. BACKFILL_START=2025-01 for a quick catch-up).
BACKFILL_START = os.environ.get("BACKFILL_START", "2023-06")
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
    # --- v2: deliverability / list health -------------------------------------------------
    bigquery.SchemaField("bounced_at", "TIMESTAMP"),
    bigquery.SchemaField("unsubscribed_at", "TIMESTAMP"),
    bigquery.SchemaField("spam_at", "TIMESTAMP"),
    bigquery.SchemaField("dropped_at", "TIMESTAMP"),
    bigquery.SchemaField("is_bounce", "BOOL"),
    bigquery.SchemaField("is_unsub", "BOOL"),
    bigquery.SchemaField("is_spam", "BOOL"),
    bigquery.SchemaField("is_dropped", "BOOL"),
    bigquery.SchemaField("pull_version", "INT64"),
]

# The Klaviyo metric names we join onto each send, and the row fields they populate.
# "Received Email" is the SPINE (one row per send); the rest are interactions matched back
# to a send by (recipient email, $message). Missing metrics are simply skipped.
INTERACTIONS: List[Tuple[str, str, str]] = [
    # (Klaviyo metric name,               timestamp column, boolean column)
    ("Opened Email",                      "opened_at",      "is_open"),
    ("Clicked Email",                     "clicked_at",     "is_click"),
    ("Bounced Email",                     "bounced_at",     "is_bounce"),
    ("Unsubscribed from Email Marketing", "unsubscribed_at", "is_unsub"),
    ("Marked Email as Spam",              "spam_at",        "is_spam"),
    ("Dropped Email",                     "dropped_at",     "is_dropped"),
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
    """Pull one [start, end) window and emit ONE ROW PER SEND, flagged with each interaction.

    ATTRIBUTION IS PER-RECIPIENT: interactions are matched to a send by the pair
    (recipient email, $message), NOT by $message alone. $message is the CAMPAIGN message id,
    shared by every recipient of that campaign -- keying on it alone marked EVERY recipient of
    a campaign as having opened/clicked if ANYONE did (it produced ~99% open/click rates). So we
    fetch the profile on the interaction pulls too and join on (email, message).
    Interactions lag sends, so their fetch window extends +7d to catch late activity (a bounce
    is near-immediate, but an open/click/unsubscribe can arrive days later)."""
    received = metrics.get("Received Email")
    if not received:
        raise RuntimeError("Klaviyo metric 'Received Email' not found for this account.")

    lag_end = end + timedelta(days=7)
    sends = fetch_events(headers, received, start, end, fetch_profile=True)

    # For each interaction metric, map (email, $message) -> earliest occurrence.
    seen_at: Dict[str, Dict[tuple, str]] = {}
    for metric_name, ts_col, _flag_col in INTERACTIONS:
        mid = metrics.get(metric_name)
        found: Dict[tuple, str] = {}
        if mid:
            for ev in fetch_events(headers, mid, start, lag_end, fetch_profile=True):
                msg = _props(ev).get("$message")
                if msg:
                    k = (_email_of(ev), msg)
                    if k not in found:
                        found[k] = ev["attributes"]["datetime"]
        seen_at[ts_col] = found

    rows: List[Dict[str, Any]] = []
    for ev in sends:
        p = _props(ev)
        mid = p.get("$message")
        email = _email_of(ev)
        k = (email, mid)
        row = {
            "event_id": ev.get("id"),
            "message_id": mid,
            "email": email or None,
            "subject": p.get("Subject"),
            "campaign": p.get("Campaign Name"),
            "flow": p.get("$flow") or "Campaign",
            "sent_at": ev["attributes"]["datetime"],
            "pull_version": PULL_VERSION,
        }
        for _metric_name, ts_col, flag_col in INTERACTIONS:
            hit = seen_at[ts_col].get(k)
            row[ts_col] = hit
            row[flag_col] = hit is not None
        rows.append(row)
    return rows


def ensure_table(bq: bigquery.Client) -> None:
    """Create the table if absent, and ADD any schema columns it is missing.

    The additive ALTER matters on every version bump: the table already exists in production
    with the v1 columns, and a load job with an explicit schema fails if the destination lacks
    a column. Widening is always safe -- existing rows read the new columns as NULL, which is
    exactly why those rows are also re-pulled (see PULL_VERSION)."""
    table = bq.create_table(bigquery.Table(FQTN, schema=SCHEMA), exists_ok=True)
    have = {f.name for f in table.schema}
    missing = [f for f in SCHEMA if f.name not in have]
    if missing:
        table.schema = list(table.schema) + missing
        bq.update_table(table, ["schema"])
        print(f"[tcs_klaviyo] schema widened: +{', '.join(f.name for f in missing)}")


def _month_start(dt: datetime) -> datetime:
    return dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def month_state(bq: bigquery.Client) -> Dict[datetime, int]:
    """{month: lowest pull_version present} for every month that has ANY rows.

    This -- not MIN/MAX -- is the checkpoint. Returning the full map is what makes interior
    holes expressible: a month absent from this map has NO data at all, and a month whose
    lowest version is below PULL_VERSION has data that is stale/incomplete. `target months`
    minus this map is the work list, so a month skipped by an earlier crashed or
    budget-capped run is simply still in it next time."""
    sql = f"""
        SELECT DATE_TRUNC(DATE(sent_at), MONTH) AS m,
               MIN(COALESCE(pull_version, 1))   AS min_ver
        FROM `{FQTN}`
        GROUP BY m
    """
    return {datetime(r["m"].year, r["m"].month, 1, tzinfo=timezone.utc): int(r["min_ver"])
            for r in bq.query(sql, location=LOCATION).result()}


def covered_months(bq: bigquery.Client) -> Set[datetime]:
    """Months already loaded AT THE CURRENT PULL_VERSION -- i.e. nothing left to do for them."""
    return {m for m, ver in month_state(bq).items() if ver >= PULL_VERSION}


def max_sent_at(bq: bigquery.Client) -> Optional[datetime]:
    """Newest send already loaded (any version) -- the start of the FORWARD phase."""
    r = list(bq.query(f"SELECT MAX(sent_at) AS hi FROM `{FQTN}`", location=LOCATION).result())[0]
    return r["hi"]


def _parse_floor() -> datetime:
    """BACKFILL_START ('YYYY-MM') -> the first month we will ever ask Klaviyo for."""
    year, month = (int(x) for x in BACKFILL_START.split("-")[:2])
    return datetime(year, month, 1, tzinfo=timezone.utc)


def target_months(now: datetime) -> List[datetime]:
    """Every month from the floor up to and including the current one, NEWEST FIRST.

    Newest-first is deliberate: recent data is the most valuable, so a run that exhausts its
    budget still leaves the dashboard with the freshest possible history."""
    floor = _parse_floor()
    months: List[datetime] = []
    cur = _month_start(now)
    while cur >= floor:
        months.append(cur)
        cur = _month_start(cur - timedelta(microseconds=1))
    return months


def missing_months(bq: bigquery.Client, now: datetime) -> List[datetime]:
    """The work list, in the order the months are worth doing.

    EMPTY MONTHS FIRST, then version-stale ones -- each group newest-first. The ordering is not
    cosmetic: a month with NO rows is a visible hole that breaks every time series (the monthly
    views drop such months outright), whereas a version-stale month already has usable
    open/click data and is only missing the newer deliverability flags. When a run is cut short
    by RUN_BUDGET_SEC -- which, on a full 37-month rebuild, it always is -- filling holes buys
    far more than upgrading rows that already work.

    The CURRENT month is always excluded: it is still accumulating, so the FORWARD phase owns
    it (treating it as 'covered' mid-month would freeze it half-loaded forever)."""
    state = month_state(bq)
    this_month = _month_start(now)
    candidates = [m for m in target_months(now)
                  if m != this_month and state.get(m, 0) < PULL_VERSION]
    empty = [m for m in candidates if m not in state]
    stale = [m for m in candidates if m in state]
    return empty + stale


def append_rows(bq: bigquery.Client, rows: List[Dict[str, Any]]) -> None:
    if not rows:
        return
    job_config = bigquery.LoadJobConfig(
        write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
        source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
        schema=SCHEMA,
    )
    bq.load_table_from_json(rows, FQTN, job_config=job_config).result()


def _pull_month(bq, headers, metrics, month: datetime, label: str) -> int:
    """Pull one calendar month and append it atomically. Returns the row count."""
    win_end = _month_start(month + relativedelta(months=1))
    rows = collect_window(headers, metrics, month, win_end)
    append_rows(bq, rows)
    print(f"[tcs_klaviyo] {label} {month.date()}: +{len(rows)} sends")
    return len(rows)


def main() -> None:
    started = time.monotonic()
    headers = _headers(read_secret(KLAVIYO_SECRET))
    bq = bigquery.Client(project=PROJECT, location=LOCATION)
    ensure_table(bq)
    metrics = get_metric_map(headers)

    now = datetime.now(timezone.utc)
    total = 0

    todo = missing_months(bq, now)
    task_count = int(os.environ.get("CLOUD_RUN_TASK_COUNT", "1"))
    task_index = int(os.environ.get("CLOUD_RUN_TASK_INDEX", "0"))

    # PARALLEL MODE: when launched with --tasks N, shard the MISSING months across tasks for a
    # fast bulk backfill. Sharding the *missing* list (rather than the whole span, as the old
    # version did) means repeated parallel runs converge instead of re-doing loaded work -- and
    # any month a task drops on the floor is simply picked up by the next run.
    if task_count > 1:
        mine = [m for i, m in enumerate(todo) if i % task_count == task_index]
        print(f"[tcs_klaviyo] task {task_index}/{task_count}: {len(mine)} of {len(todo)} missing months")
        for month in mine:
            if time.monotonic() - started > RUN_BUDGET_SEC:
                print(f"[tcs_klaviyo] task {task_index}: run budget reached at {month.date()}; "
                      f"remaining months resume next run.")
                break
            total += _pull_month(bq, headers, metrics, month, f"task {task_index} backfill")
        print(f"[tcs_klaviyo] task {task_index} done: +{total} sends.")
        return

    # -- Phase 1: FORWARD -- new sends since the newest we have (skip on an empty table).
    #    This also keeps the CURRENT month up to date, which missing_months deliberately skips.
    hi = max_sent_at(bq)
    if hi is not None:
        rows = collect_window(headers, metrics, hi, now)
        append_rows(bq, rows)
        total += len(rows)
        print(f"[tcs_klaviyo] forward {hi.date()}..{now.date()}: +{len(rows)} sends")

    # -- Phase 2: BACKFILL -- empty months first, then version-stale ones (see missing_months).
    print(f"[tcs_klaviyo] {len(todo)} month(s) to load at v{PULL_VERSION}, in priority order: "
          f"{', '.join(m.strftime('%Y-%m') for m in todo[:12])}"
          f"{' ...' if len(todo) > 12 else ''}")
    done_now = 0
    for month in todo:
        if time.monotonic() - started > RUN_BUDGET_SEC:
            print(f"[tcs_klaviyo] run budget ({RUN_BUDGET_SEC}s) reached at {month.date()}; "
                  f"{len(todo) - done_now} month(s) resume next tick.")
            break
        total += _pull_month(bq, headers, metrics, month, "backfill")
        done_now += 1

    remaining = len(todo) - done_now
    print(f"[tcs_klaviyo] done: +{total} sends this run; "
          f"{done_now} month(s) filled, {remaining} still missing "
          f"(target span from {BACKFILL_START}).")


if __name__ == "__main__":
    main()
