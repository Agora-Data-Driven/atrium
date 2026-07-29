"""TCS Klaviyo PROFILES loader (DIRECT-API, not Windsor).

Raw target : raw_windsor.tcs_klaviyo_profiles  (the shared raw layer; project
             agora-data-driven, dataset raw_windsor, location asia-southeast1).
Source     : Klaviyo Profiles API, with `additional-fields[profile]=predictive_analytics`.
Cadence    : daily scheduled pull (see services/ingest/deploy_tcs_ingest.ps1).

WHY: tcs_klaviyo_events is the EVENT stream (what was sent to whom); this is the PERSON
dimension (who they are and what they are worth). It is what lets a dashboard segment by
customer value or churn risk rather than only by email activity, and it is the backbone of
the old notebook's `DATABASE_email` master table. Ported from the "Klaviyo" section of
clients/TCS/archive_code/analytics.py.

Grain: one row per profile PER PULL. Rows are appended, never merged -- the staging view
keeps the newest row per profile id. Appending rather than MERGEing means the table doubles
as a slowly-changing history of CLV/churn scores, which the notebook's WRITE_TRUNCATE
approach threw away every run.

CUSTOM PROPERTIES ARE OPEN-ENDED, so they are stored two ways:
  * `properties_json` -- the whole custom-property bag, verbatim, as a JSON string. Nothing is
    ever silently dropped just because this loader had not heard of a property yet.
  * a handful of LIFTED columns for the properties the account actually uses today (Shopify
    tags, sign-up source, consent), so common queries do not have to JSON-parse.
Adding a lifted column later is a pure widening -- the value was in properties_json all along.

INCREMENTAL, RESUMABLE: Klaviyo bumps `updated` on any profile change, so the watermark is
MAX(updated) already loaded, minus WATERMARK_BUFFER_HOURS to tolerate clock skew and
late-settling writes. Results are sorted by `updated` ASCENDING so that when RUN_BUDGET_SEC
runs out mid-sync the next run resumes cleanly from the new high-water mark -- with a
descending sort an interrupted first run would never advance past the newest page.

Auth:
  * Klaviyo private API key from Secret Manager (secret ``tcs-klaviyo-key``) via ADC.
  * BigQuery via ADC (ingest-runner@ on Cloud Run).
"""

import json
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import requests
from google.cloud import bigquery, secretmanager
from urllib3.exceptions import ProtocolError

PROJECT = os.environ.get("GCP_PROJECT", "agora-data-driven")
RAW_DATASET = os.environ.get("RAW_DATASET", "raw_windsor")
LOCATION = "asia-southeast1"
TABLE = "tcs_klaviyo_profiles"
FQTN = f"{PROJECT}.{RAW_DATASET}.{TABLE}"

KLAVIYO_SECRET = "tcs-klaviyo-key"
KLAVIYO_BASE = "https://a.klaviyo.com/api"
KLAVIYO_REVISION = "2024-10-15"

PULL_VERSION = 1
PAGE_SIZE = int(os.environ.get("KLAVIYO_PAGE_SIZE", "100"))  # profiles max is 100
RUN_BUDGET_SEC = int(os.environ.get("RUN_BUDGET_SEC", "3000"))
MAX_RETRIES = int(os.environ.get("KLAVIYO_MAX_RETRIES", "6"))
# Re-ask for a little before the watermark: profile writes can settle out of order.
WATERMARK_BUFFER_HOURS = int(os.environ.get("WATERMARK_BUFFER_HOURS", "24"))
# Batch size for BigQuery appends -- flushing periodically means a timeout keeps its progress.
FLUSH_EVERY = int(os.environ.get("FLUSH_EVERY", "5000"))

SCHEMA = [
    bigquery.SchemaField("id", "STRING"),
    bigquery.SchemaField("email", "STRING"),
    bigquery.SchemaField("phone_number", "STRING"),
    bigquery.SchemaField("external_id", "STRING"),
    bigquery.SchemaField("first_name", "STRING"),
    bigquery.SchemaField("last_name", "STRING"),
    bigquery.SchemaField("organization", "STRING"),
    bigquery.SchemaField("title", "STRING"),
    bigquery.SchemaField("locale", "STRING"),
    bigquery.SchemaField("created", "TIMESTAMP"),
    bigquery.SchemaField("updated", "TIMESTAMP"),
    bigquery.SchemaField("last_event_date", "TIMESTAMP"),
    # Location.
    bigquery.SchemaField("city", "STRING"),
    bigquery.SchemaField("region", "STRING"),
    bigquery.SchemaField("country", "STRING"),
    bigquery.SchemaField("zip", "STRING"),
    bigquery.SchemaField("timezone", "STRING"),
    # Predictive analytics -- the customer-value dimension.
    bigquery.SchemaField("historic_number_of_orders", "INT64"),
    bigquery.SchemaField("predicted_number_of_orders", "FLOAT64"),
    bigquery.SchemaField("average_days_between_orders", "FLOAT64"),
    bigquery.SchemaField("average_order_value", "FLOAT64"),
    bigquery.SchemaField("historic_clv", "FLOAT64"),
    bigquery.SchemaField("predicted_clv", "FLOAT64"),
    bigquery.SchemaField("total_clv", "FLOAT64"),
    bigquery.SchemaField("churn_probability", "FLOAT64"),
    bigquery.SchemaField("expected_date_of_next_order", "TIMESTAMP"),
    # Lifted custom properties (see module docstring) + the full bag.
    bigquery.SchemaField("shopify_tags", "STRING"),
    bigquery.SchemaField("accepts_marketing", "STRING"),
    bigquery.SchemaField("signup_source", "STRING"),
    bigquery.SchemaField("klaviyo_source", "STRING"),
    bigquery.SchemaField("consent", "STRING"),
    bigquery.SchemaField("consent_timestamp", "STRING"),
    bigquery.SchemaField("business_url", "STRING"),
    bigquery.SchemaField("properties_json", "STRING"),
    bigquery.SchemaField("loaded_at", "TIMESTAMP"),
    bigquery.SchemaField("pull_version", "INT64"),
]

# Lifted column -> the Klaviyo custom-property key it comes from. These are the properties
# this account actually populates today (confirmed against the live profile payload).
LIFTED = {
    "shopify_tags": "Shopify Tags",
    "accepts_marketing": "Accepts Marketing",
    "signup_source": "Sign-Up Source",
    "klaviyo_source": "$source",
    "consent": "$consent",
    "consent_timestamp": "$consent_timestamp",
    "business_url": "Business URL",
}


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
    """GET with retry: honour 429 Retry-After, retry transient network / 5xx. A non-429 4xx is
    a hard failure. The body read is inside the try because a truncated stream raises during
    decode, not at connect time."""
    attempt = 0
    delay = 2.0
    while True:
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=90)
            if resp.status_code == 429:
                wait = float(resp.headers.get("Retry-After", 5) or 5)
                print(f"[tcs_klaviyo_profiles] 429 rate-limited; sleeping {wait:.0f}s")
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
                raise RuntimeError(f"Klaviyo GET failed after {attempt} attempts: {e}") from e
            print(f"[tcs_klaviyo_profiles] transient error ({e}); "
                  f"retry {attempt}/{MAX_RETRIES} in {delay:.0f}s")
            time.sleep(delay)
            delay = min(delay * 2, 60)


def _f(v) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _i(v) -> Optional[int]:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _s(v) -> Optional[str]:
    """Stringify a custom-property value for a lifted column.

    Lists (e.g. Shopify Tags) become comma-joined text, which is what consumers of these
    columns want. An EMPTY list/dict returns None, not "" -- Klaviyo sends `[]` for "no tags",
    and an empty string would look like a real value, quietly breaking `WHERE shopify_tags IS
    NULL` checks. Booleans are lowercased to match their JSON spelling in properties_json, so
    the two representations of the same property never disagree."""
    if v is None:
        return None
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, list):
        return ",".join(str(x) for x in v) if v else None
    if isinstance(v, dict):
        return json.dumps(v, separators=(",", ":")) if v else None
    s = str(v)
    return s if s.strip() else None


def transform(p: Dict[str, Any], loaded_at: str) -> Dict[str, Any]:
    a = p.get("attributes") or {}
    loc = a.get("location") or {}
    pa = a.get("predictive_analytics") or {}
    props = a.get("properties") or {}

    row: Dict[str, Any] = {
        "id": p.get("id"),
        "email": (a.get("email") or "").lower().strip() or None,
        "phone_number": a.get("phone_number"),
        "external_id": a.get("external_id"),
        "first_name": a.get("first_name"),
        "last_name": a.get("last_name"),
        "organization": a.get("organization"),
        "title": a.get("title"),
        "locale": a.get("locale"),
        "created": a.get("created"),
        "updated": a.get("updated"),
        "last_event_date": a.get("last_event_date"),

        "city": loc.get("city"),
        "region": loc.get("region"),
        "country": loc.get("country"),
        "zip": loc.get("zip"),
        "timezone": loc.get("timezone"),

        "historic_number_of_orders": _i(pa.get("historic_number_of_orders")),
        "predicted_number_of_orders": _f(pa.get("predicted_number_of_orders")),
        "average_days_between_orders": _f(pa.get("average_days_between_orders")),
        "average_order_value": _f(pa.get("average_order_value")),
        "historic_clv": _f(pa.get("historic_clv")),
        "predicted_clv": _f(pa.get("predicted_clv")),
        "total_clv": _f(pa.get("total_clv")),
        "churn_probability": _f(pa.get("churn_probability")),
        "expected_date_of_next_order": pa.get("expected_date_of_next_order"),

        # The whole custom-property bag, so a property this loader has never heard of is
        # still captured rather than lost.
        "properties_json": json.dumps(props, separators=(",", ":"), default=str) if props else None,
        "loaded_at": loaded_at,
        "pull_version": PULL_VERSION,
    }
    for col, key in LIFTED.items():
        row[col] = _s(props.get(key))
    return row


def ensure_table(bq: bigquery.Client) -> None:
    table = bq.create_table(bigquery.Table(FQTN, schema=SCHEMA), exists_ok=True)
    have = {f.name for f in table.schema}
    missing = [f for f in SCHEMA if f.name not in have]
    if missing:
        table.schema = list(table.schema) + missing
        bq.update_table(table, ["schema"])
        print(f"[tcs_klaviyo_profiles] schema widened: +{', '.join(f.name for f in missing)}")


def append_rows(bq: bigquery.Client, rows: List[Dict[str, Any]]) -> None:
    if not rows:
        return
    job_config = bigquery.LoadJobConfig(
        write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
        source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
        schema=SCHEMA,
    )
    bq.load_table_from_json(rows, FQTN, job_config=job_config).result()


def watermark(bq: bigquery.Client) -> Optional[datetime]:
    """MAX(updated) already loaded, less the safety buffer. None on an empty table (full sync)."""
    r = list(bq.query(f"SELECT MAX(updated) AS hi FROM `{FQTN}`", location=LOCATION).result())[0]
    hi = r["hi"]
    return None if hi is None else hi - timedelta(hours=WATERMARK_BUFFER_HOURS)


def fetch_profiles(bq, headers, since: Optional[datetime], started: float) -> int:
    """Page through profiles updated since the watermark, flushing to BigQuery as we go.

    ASCENDING sort is load-bearing for resumability -- see the module docstring."""
    url = f"{KLAVIYO_BASE}/profiles/"
    params: Optional[Dict[str, Any]] = {
        "page[size]": PAGE_SIZE,
        "additional-fields[profile]": "predictive_analytics",
        "sort": "updated",
    }
    if since is not None:
        ts = since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        params["filter"] = f"greater-than(updated,{ts})"
        print(f"[tcs_klaviyo_profiles] incremental: updated > {ts}")
    else:
        print("[tcs_klaviyo_profiles] empty table -> full profile sync")

    loaded_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    buffer: List[Dict[str, Any]] = []
    total = 0

    while url:
        data = _request_json(url, headers, params)
        batch = data.get("data", [])
        if not batch:
            break
        buffer.extend(transform(p, loaded_at) for p in batch)
        total += len(batch)

        if len(buffer) >= FLUSH_EVERY:
            append_rows(bq, buffer)
            print(f"[tcs_klaviyo_profiles] flushed {len(buffer)} (total {total})")
            buffer = []

        if time.monotonic() - started > RUN_BUDGET_SEC:
            print(f"[tcs_klaviyo_profiles] run budget ({RUN_BUDGET_SEC}s) reached after "
                  f"{total} profiles; the watermark advances so the next run resumes here.")
            break

        url = (data.get("links") or {}).get("next")
        params = None  # the next link already carries the query string

    append_rows(bq, buffer)
    return total


def main() -> None:
    started = time.monotonic()
    headers = _headers(read_secret(KLAVIYO_SECRET))
    bq = bigquery.Client(project=PROJECT, location=LOCATION)
    ensure_table(bq)

    total = fetch_profiles(bq, headers, watermark(bq), started)
    print(f"[tcs_klaviyo_profiles] done: +{total} profile snapshots this run.")


if __name__ == "__main__":
    main()
