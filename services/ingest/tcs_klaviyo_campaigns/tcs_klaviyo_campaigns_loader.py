"""TCS Klaviyo CAMPAIGNS loader (DIRECT-API, not Windsor).

Raw target : raw_windsor.tcs_klaviyo_campaigns  (the shared raw layer; project
             agora-data-driven, dataset raw_windsor, location asia-southeast1).
Source     : Klaviyo Campaigns API (metadata) + Campaign Values Reports API (statistics).
Cadence    : daily scheduled pull (see services/ingest/deploy_tcs_ingest.ps1).

WHY: this finishes the job the notebook left at "[WIP] Email Campaigns". The per-send events
table can count opens and clicks, but it CANNOT give you Klaviyo's own attributed revenue --
`conversions` / `conversion_value`, computed against the Placed Order metric. That is the only
number that answers "what did this campaign earn?". The values report also serves the
deliverability rates (bounce / unsubscribe / spam) at campaign level.

Grain: one row per (campaign, campaign message) PER PULL. Rows are appended, never merged; the
staging view keeps the newest row per campaign_message_id. Appending keeps a history of how a
campaign's numbers matured after send, which a WRITE_TRUNCATE would erase.

TWO API SHAPES, ONE ROW:
  1. /campaigns  -- paginated metadata: name, status, send time, audiences, subject, preview.
     NOTE: this endpoint REJECTS `page[size]` ("not a valid field for the resource 'campaign'")
     -- an easy 400 to trip over. Pagination is cursor-only via links.next.
  2. /campaign-values-reports  -- a POST that returns statistics for MANY campaigns at once,
     grouped by campaign_message_id. It is heavily rate-limited, so it is called in batches
     with a deliberate pause between them.

TIMEFRAME LIMIT: the values report only accepts a bounded timeframe (we use `last_12_months`).
Campaigns older than that get their metadata row with NULL statistics rather than being
dropped -- a campaign we know about but cannot score is strictly more useful than a silent
absence, and the NULLs make the boundary visible.

Auth:
  * Klaviyo private API key from Secret Manager (secret ``tcs-klaviyo-key``) via ADC.
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
TABLE = "tcs_klaviyo_campaigns"
FQTN = f"{PROJECT}.{RAW_DATASET}.{TABLE}"

KLAVIYO_SECRET = "tcs-klaviyo-key"
KLAVIYO_BASE = "https://a.klaviyo.com/api"
KLAVIYO_REVISION = "2024-10-15"

PULL_VERSION = 1
RUN_BUDGET_SEC = int(os.environ.get("RUN_BUDGET_SEC", "3000"))
MAX_RETRIES = int(os.environ.get("KLAVIYO_MAX_RETRIES", "6"))
# Campaigns per values-report request, and the pause between them. The values-report endpoint
# is rate-limited far harder than the rest of the API; the notebook used 32s between batches.
STATS_BATCH = int(os.environ.get("STATS_BATCH", "50"))
STATS_PAUSE_SEC = float(os.environ.get("STATS_PAUSE_SEC", "32"))
STATS_TIMEFRAME = os.environ.get("STATS_TIMEFRAME", "last_12_months")

STATISTICS = [
    "recipients", "opens", "opens_unique", "open_rate",
    "clicks", "clicks_unique", "click_rate",
    "unsubscribes", "unsubscribe_rate",
    "bounced", "bounce_rate",
    "spam_complaints", "spam_complaint_rate",
    "delivered", "delivery_rate",
    "conversions", "conversion_value", "conversion_rate",
]

SCHEMA = [
    bigquery.SchemaField("campaign_id", "STRING"),
    bigquery.SchemaField("campaign_message_id", "STRING"),
    bigquery.SchemaField("name", "STRING"),
    bigquery.SchemaField("status", "STRING"),
    bigquery.SchemaField("archived", "BOOL"),
    bigquery.SchemaField("channel", "STRING"),
    bigquery.SchemaField("subject", "STRING"),
    bigquery.SchemaField("preview_text", "STRING"),
    bigquery.SchemaField("from_email", "STRING"),
    bigquery.SchemaField("from_label", "STRING"),
    bigquery.SchemaField("created_at", "TIMESTAMP"),
    bigquery.SchemaField("scheduled_at", "TIMESTAMP"),
    bigquery.SchemaField("send_time", "TIMESTAMP"),
    bigquery.SchemaField("included_audiences", "STRING"),
    bigquery.SchemaField("excluded_audiences", "STRING"),
    # Statistics from the values report. NULL means "outside the report timeframe", NOT zero.
    bigquery.SchemaField("recipients", "FLOAT64"),
    bigquery.SchemaField("delivered", "FLOAT64"),
    bigquery.SchemaField("delivery_rate", "FLOAT64"),
    bigquery.SchemaField("opens", "FLOAT64"),
    bigquery.SchemaField("opens_unique", "FLOAT64"),
    bigquery.SchemaField("open_rate", "FLOAT64"),
    bigquery.SchemaField("clicks", "FLOAT64"),
    bigquery.SchemaField("clicks_unique", "FLOAT64"),
    bigquery.SchemaField("click_rate", "FLOAT64"),
    bigquery.SchemaField("unsubscribes", "FLOAT64"),
    bigquery.SchemaField("unsubscribe_rate", "FLOAT64"),
    bigquery.SchemaField("bounced", "FLOAT64"),
    bigquery.SchemaField("bounce_rate", "FLOAT64"),
    bigquery.SchemaField("spam_complaints", "FLOAT64"),
    bigquery.SchemaField("spam_complaint_rate", "FLOAT64"),
    bigquery.SchemaField("conversions", "FLOAT64"),
    bigquery.SchemaField("conversion_value", "FLOAT64"),
    bigquery.SchemaField("conversion_rate", "FLOAT64"),
    bigquery.SchemaField("has_stats", "BOOL"),
    bigquery.SchemaField("loaded_at", "TIMESTAMP"),
    bigquery.SchemaField("pull_version", "INT64"),
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


def _req(method: str, url: str, headers: Dict[str, str], *, params=None, body=None) -> Dict[str, Any]:
    """GET/POST with retry: honour 429 Retry-After, retry transient network / 5xx."""
    attempt = 0
    delay = 2.0
    while True:
        try:
            if method == "GET":
                resp = requests.get(url, headers=headers, params=params, timeout=90)
            else:
                h = dict(headers)
                h["Content-Type"] = "application/json"
                resp = requests.post(url, headers=h, json=body, timeout=120)
            if resp.status_code == 429:
                wait = float(resp.headers.get("Retry-After", 10) or 10)
                print(f"[tcs_klaviyo_campaigns] 429 rate-limited; sleeping {wait:.0f}s")
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
                raise RuntimeError(f"Klaviyo {method} failed after {attempt} attempts: {e}") from e
            print(f"[tcs_klaviyo_campaigns] transient error ({e}); "
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


def _join(vals) -> Optional[str]:
    if not vals:
        return None
    return ",".join(str(v) for v in vals)


def fetch_campaign_metadata(headers) -> List[Dict[str, Any]]:
    """All email campaigns with their messages, flattened to one dict per campaign message.

    `page[size]` is deliberately NOT sent -- the campaigns resource rejects it with a 400."""
    url = (f"{KLAVIYO_BASE}/campaigns/"
           f"?filter=equals(messages.channel,'email')&include=campaign-messages")
    out: List[Dict[str, Any]] = []
    while url:
        data = _req("GET", url, headers)
        msg_attrs = {m["id"]: (m.get("attributes") or {})
                     for m in data.get("included", []) if m.get("type") == "campaign-message"}

        for camp in data.get("data", []):
            attrs = camp.get("attributes") or {}
            rel = camp.get("relationships") or {}
            audiences = attrs.get("audiences") or {}
            msg_ids = [r["id"] for r in ((rel.get("campaign-messages") or {}).get("data") or [])]
            # A campaign always has at least one message; fall back to the campaign id so a
            # metadata-only campaign still produces a row rather than vanishing.
            for mid in (msg_ids or [camp["id"]]):
                m = msg_attrs.get(mid, {})
                content = m.get("content") or {}
                out.append({
                    "campaign_id": camp["id"],
                    "campaign_message_id": mid,
                    "name": attrs.get("name"),
                    "status": attrs.get("status"),
                    "archived": attrs.get("archived"),
                    "channel": "email",
                    "subject": content.get("subject"),
                    "preview_text": content.get("preview_text"),
                    "from_email": content.get("from_email"),
                    "from_label": content.get("from_label"),
                    "created_at": attrs.get("created_at"),
                    "scheduled_at": attrs.get("scheduled_at"),
                    "send_time": attrs.get("send_time"),
                    "included_audiences": _join(audiences.get("included")),
                    "excluded_audiences": _join(audiences.get("excluded")),
                })
        url = (data.get("links") or {}).get("next")
    return out


def conversion_metric_id(headers) -> Optional[str]:
    """The 'Placed Order' metric id -- what conversions/conversion_value are measured against."""
    data = _req("GET", f"{KLAVIYO_BASE}/metrics", headers)
    for m in data.get("data", []):
        if (m.get("attributes") or {}).get("name") == "Placed Order":
            return m["id"]
    return None


def fetch_stats(headers, campaign_ids: List[str], conv_id: Optional[str],
                started: float) -> Dict[str, Dict[str, Any]]:
    """{campaign_message_id: statistics} from the values-report endpoint, in rate-limited batches.

    A failed batch is logged and skipped rather than aborting the run: those campaigns simply
    keep NULL statistics this cycle and are retried tomorrow. Losing one batch is much better
    than losing the whole metadata refresh."""
    stats: Dict[str, Dict[str, Any]] = {}
    for i in range(0, len(campaign_ids), STATS_BATCH):
        if time.monotonic() - started > RUN_BUDGET_SEC:
            print(f"[tcs_klaviyo_campaigns] run budget reached; "
                  f"{len(campaign_ids) - i} campaign(s) keep NULL stats this run.")
            break

        batch = campaign_ids[i:i + STATS_BATCH]
        ids = ",".join('"%s"' % c for c in batch)
        attributes: Dict[str, Any] = {
            "statistics": STATISTICS,
            "timeframe": {"key": STATS_TIMEFRAME},
            "filter": f"contains-any(campaign_id,[{ids}])",
            "group_by": ["campaign_message_id"],
        }
        if conv_id:
            attributes["conversion_metric_id"] = conv_id
        body = {"data": {"type": "campaign-values-report", "attributes": attributes}}

        try:
            data = _req("POST", f"{KLAVIYO_BASE}/campaign-values-reports/", headers, body=body)
            results = ((data.get("data") or {}).get("attributes") or {}).get("results") or []
            for item in results:
                key = (item.get("groupings") or {}).get("campaign_message_id")
                if key:
                    stats[key] = item.get("statistics") or {}
            print(f"[tcs_klaviyo_campaigns] stats batch {i // STATS_BATCH + 1}: "
                  f"{len(results)} result(s)")
        except Exception as e:  # noqa: BLE001 -- a bad batch must not kill the whole run
            print(f"[tcs_klaviyo_campaigns] stats batch {i // STATS_BATCH + 1} FAILED "
                  f"({e}); those campaigns keep NULL stats and retry next run.")

        if i + STATS_BATCH < len(campaign_ids):
            time.sleep(STATS_PAUSE_SEC)
    return stats


def ensure_table(bq: bigquery.Client) -> None:
    table = bq.create_table(bigquery.Table(FQTN, schema=SCHEMA), exists_ok=True)
    have = {f.name for f in table.schema}
    missing = [f for f in SCHEMA if f.name not in have]
    if missing:
        table.schema = list(table.schema) + missing
        bq.update_table(table, ["schema"])
        print(f"[tcs_klaviyo_campaigns] schema widened: +{', '.join(f.name for f in missing)}")


def append_rows(bq: bigquery.Client, rows: List[Dict[str, Any]]) -> None:
    if not rows:
        return
    job_config = bigquery.LoadJobConfig(
        write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
        source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
        schema=SCHEMA,
    )
    bq.load_table_from_json(rows, FQTN, job_config=job_config).result()


def main() -> None:
    started = time.monotonic()
    headers = _headers(read_secret(KLAVIYO_SECRET))
    bq = bigquery.Client(project=PROJECT, location=LOCATION)
    ensure_table(bq)

    meta = fetch_campaign_metadata(headers)
    print(f"[tcs_klaviyo_campaigns] {len(meta)} campaign message(s) from metadata.")
    if not meta:
        print("[tcs_klaviyo_campaigns] no campaigns; nothing to load.")
        return

    conv_id = conversion_metric_id(headers)
    print(f"[tcs_klaviyo_campaigns] conversion metric (Placed Order): {conv_id or 'NOT FOUND'}")

    campaign_ids = sorted({m["campaign_id"] for m in meta})
    stats = fetch_stats(headers, campaign_ids, conv_id, started)

    loaded_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    rows: List[Dict[str, Any]] = []
    for m in meta:
        s = stats.get(m["campaign_message_id"])
        row = dict(m)
        for stat in STATISTICS:
            row[stat] = _f((s or {}).get(stat))
        # Distinguishes "scored: all zeroes" from "outside the report timeframe" -- without it
        # every pre-timeframe campaign would read as a campaign that earned nothing.
        row["has_stats"] = s is not None
        row["loaded_at"] = loaded_at
        row["pull_version"] = PULL_VERSION
        rows.append(row)

    append_rows(bq, rows)
    scored = sum(1 for r in rows if r["has_stats"])
    print(f"[tcs_klaviyo_campaigns] done: +{len(rows)} row(s); "
          f"{scored} with statistics, {len(rows) - scored} outside the "
          f"{STATS_TIMEFRAME} report window (NULL stats).")


if __name__ == "__main__":
    main()
