r"""Windsor -> BigQuery loader for Meta / Facebook Ads performance.

    Meta --> Windsor /all --> THIS JOB --> raw_windsor.perf_meta
                                                |
                                                +--> per-client view (WHERE client_slug = '<c>')
                                                          +--> <c>-export --> <c>.json --> dashboard

ONE job for the WHOLE estate. A single Windsor key returns every account (verified live
2026-07-30: 13 accounts with select_accounts omitted), so there is no per-client loop and
no per-client key. A client export job that calls the Windsor API itself is LEGACY -- see
../CLAUDE.md. Do not add a fifth.

PORTED from bidbrain-analytics/ingest/windsor_data_pull/meta/meta_loader.py, which has run
in production for months. Table name and column vocabulary are kept identical so the two
estates do not diverge. Everything below marked "atrium:" is a deliberate difference, and
every one of them is backed by a live probe against the real agora Windsor account on
2026-07-30 -- not an assumption.

GRAIN + IDEMPOTENCE
    One row per (ad_id x metric_date). MERGE key = (ad_id, metric_date). NDJSON -> GCS ->
    staging table -> MERGE, so re-pulling a day never duplicates and Meta's late revisions
    overwrite cleanly.

MODES
    1. INCREMENTAL PER-ACCOUNT (no date args) -- the normal / scheduled run. Each account
       forward-loads from its own MAX(metric_date) in BigQuery; an account with no rows yet
       gets a full backward-walk backfill. So a new account is discovered from scratch
       without re-pulling history for accounts already current.

           python meta_loader.py

    2. FIXED RANGE (two date args) -- targeted re-pull, all accounts together.

           python meta_loader.py 2026-05-25 2026-05-30

    FLAGS
       --force             re-fetch even cached chunks (the MERGE stays idempotent)
       --only <sel>        restrict to accounts matching <sel> -- a client_slug ('tcs'), an
                           account_id, or a case-insensitive account-name substring.
                           Comma-separate for several.
       --dry-run           fetch + transform + report, write NOTHING to BigQuery

RETRIES
    Transient errors (timeout, 429, 5xx) retry with capped backoff up to MAX_ATTEMPTS, then
    fail loudly so an unattended scheduled run cannot hang forever. A permanent 4xx (bad
    field / auth) fails FAST with the response body.

AUTH
    Windsor key: env WINDSOR_API_KEY (mounted from Secret Manager by
    tools/deploy_ingest_jobs.ps1) else Secret Manager directly. BigQuery + Storage via ADC.
    Identical locally (after `gcloud auth application-default login`) and on Cloud Run.
"""
import json
import logging
import os
import re
import sys
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests
from google.cloud import bigquery, secretmanager, storage

# The tested Windsor request layer lives next door. We use its force_ipv4() -- Cloud Run has
# NO IPv6 egress route while Windsor publishes AAAA records, and socket.create_connection
# applies the FULL timeout to EACH address, so every call burns its whole timeout on the v6
# address first. Measured on honeytribe: 120s per call, a 38-page backfill turned into 76
# minutes. It patches socket.getaddrinfo globally, so `requests` inherits the fix.
import windsor_api

# ---------- Config ----------
PROJECT_ID = os.environ.get("GCP_PROJECT", "agora-data-driven")
DATASET = os.environ.get("RAW_DATASET", "raw_windsor")
MAIN_TABLE = "perf_meta"
STAGING_TABLE = "perf_meta_staging"
# atrium: bidbrain stages through bidbrain-analytics-staging. This project had NO staging
# bucket -- create it once with create_staging_bucket.py. The name matches the STAGING_BUCKET
# env that tools/deploy_ingest_jobs.ps1 already sets.
GCS_BUCKET = os.environ.get("STAGING_BUCKET", "agora-data-driven-staging")
LOCATION = "asia-southeast1"  # Singapore -- one region for everything in this project.

WINDSOR_URL = windsor_api.WINDSOR_URL  # https://connectors.windsor.ai/all
# atrium: the shared ingest secret. The four per-client secrets (rhe-/honeytribe-/meloyelo-/
# riverdance-windsor-key) were verified byte-identical on 2026-07-30 -- one credential stored
# four times, four rotation liabilities. WINDSOR_SECRET lets you point at one of them locally
# before the consolidation lands.
WINDSOR_SECRET = os.environ.get("WINDSOR_SECRET", "windsor-api-key")

# atrium: the two `signup_button` custom-pixel fields in bidbrain's list are specific to one
# of ITS accounts (resetdata) and are dropped here. actions_purchase -> actions_omni_purchase
# and the pixel-purchase pair are added: probed live 2026-07-30 over last_90d, Sabbath Spa
# reports 28 purchases on OMNI and ZERO on actions_purchase, while HoneyTribe / riverdance /
# TCS agree across both labels. Omni is the widest label on this account. The whole 79-field
# list below was confirmed to return all 79 keys on a 3-day chunk -- no 400, nothing dropped.
WINDSOR_FIELDS = (
    "account_id,account_name,campaign_id,campaign,objective,"
    "adset_id,adset_name,ad_id,ad_name,effective_status,date,account_currency,"
    "campaign_spend_cap,"
    "impressions,reach,frequency,spend,cpc,cpm,cpp,"
    "clicks,unique_clicks,"
    "link_clicks,actions_link_click,unique_actions_link_click,unique_link_clicks_ctr,"
    "cost_per_action_type_link_click,cost_per_unique_action_type_link_click,"
    "outbound_clicks_outbound_click,unique_outbound_clicks_outbound_click,"
    "outbound_clicks_ctr_outbound_click,unique_outbound_clicks_ctr_outbound_click,"
    "cost_per_outbound_click_outbound_click,cost_per_unique_outbound_click_outbound_click,"
    "actions_post_engagement,unique_actions_post_engagement,actions_page_engagement,"
    "actions_post_reaction,actions_comment,actions_post,"
    "actions_onsite_conversion_post_save,actions_video_view,"
    "estimated_ad_recallers,estimated_ad_recall_rate,instagram_profile_visits,"
    "actions_lead,actions_offsite_conversion_fb_pixel_lead,"
    "actions_onsite_conversion_lead_grouped,unique_actions_lead,cost_per_action_type_lead,"
    "actions_landing_page_view,actions_add_to_cart,actions_initiate_checkout,"
    "actions_omni_purchase,actions_offsite_conversion_fb_pixel_purchase,"
    "actions_complete_registration,"
    "action_values_omni_purchase,action_values_offsite_conversion_fb_pixel_purchase,"
    "purchase_roas_omni_purchase,website_purchase_roas_offsite_conversion_fb_pixel_purchase,"
    "video_play_actions_video_view,video_p25_watched_actions_video_view,"
    "video_p50_watched_actions_video_view,video_p75_watched_actions_video_view,"
    "video_p95_watched_actions_video_view,video_p100_watched_actions_video_view,"
    "video_thruplay_watched_actions_video_view,video_avg_time_watched_actions_video_view,"
    "quality_ranking,engagement_rate_ranking,conversion_rate_ranking,"
    "creative_id,thumbnail_url,effective_instagram_media__thumbnail_url,"
    "placement_ad_thumbnail_url,title,body,link_url,link,"
    "datasource,source"
)

# The two fields that cap the fetch window. Verified live 2026-07-30: with them,
# last_730d returns HTTP 400 "breakdowns for unique-count fields are only available for the
# last 13 months"; without them last_1095d returns 5,248 rows fine. The window cap is caused
# by these SPECIFIC FIELDS, never by the field count -- see UniqueCountHorizonError.
UNIQUE_COUNT_FIELDS = ("unique_actions_link_click", "unique_actions_lead")

# bidbrain's Meta chunk size, kept as the DEFAULT so the scheduled run behaves identically.
# It is sized for bidbrain's row volume; a small account (TCS returns ~885 rows for a whole
# year) spends ~28s per call regardless of range, so a 3-day chunk makes a year-long backfill
# ~120 calls / ~1 hour. --chunk-days raises it for a one-off backfill; the MERGE is idempotent
# either way, so the only risk of a bigger chunk is a slower single request.
CHUNK_DAYS = int(os.environ.get("CHUNK_DAYS", "3"))
STOP_AFTER_EMPTY_CHUNKS = 5
MIN_DATE = date(2015, 1, 1)
TIMEOUT_SEC = 120
RETRY_SLEEP_BASE = 5
RETRY_SLEEP_MAX = 60
MAX_ATTEMPTS = 30
INTER_CHUNK_SLEEP = 1
# Re-pull this many days BEFORE each account's last BQ day, to recapture late-arriving Meta
# conversions. 0 = re-pull only the last day itself (staging + MERGE dedup either way).
INCREMENTAL_LOOKBACK_DAYS = 0

# Runtime artifacts under _run/ NEXT TO THIS FILE (anchored to __file__, not the cwd) so
# nothing ever scatters into the repo root. _run/ is gitignored.
BASE_DIR = Path(__file__).resolve().parent
WORK_DIR = BASE_DIR / "_run"
WORK_DIR.mkdir(parents=True, exist_ok=True)
CHUNKS_DIR = WORK_DIR / "chunks"
LOG_FILE = WORK_DIR / "meta_loader.log"

# =================================================================================
# ACCOUNT_TO_CLIENT -- the slug map. THIS IS THE ISOLATION BOUNDARY.
# =================================================================================
# A per-client view is literally `WHERE client_slug = '<c>'`, so this map decides whose
# dashboard sees whose spend. Keyed on account_id (stable, and the ONLY way to split S7000
# correctly) with an account_name fallback, because a lapsed-then-re-granted Windsor
# connector can mint a NEW account id -- bidbrain hit exactly that on Reddit.
#
# Every value is deliberate. `None` means "known account, deliberately NOT mapped": its rows
# land with client_slug NULL, which can never match any client's view. An account that is
# absent from this map entirely gets the same NULL slug PLUS a loud NEW ACCOUNT warning,
# so nobody's spend can quietly default into somebody else's dashboard.
#
# All ids + spend verified live 2026-07-30 over last_365d.
ACCOUNT_TO_CLIENT = {
    # -- RHE is THREE accounts: a sequential era of one brand, not three brands. --------
    "744718258097253":  "rhe",           # 'rhe'                        $72,126
    "291824415053555":  "rhe",           # 'Stuart Baker'               $25,117
    "819110256113106":  "rhe",           # 'Super Cashflow Development' $14,231
    # -- one account, one client -------------------------------------------------------
    "1834792266844322": "tcs",           # 'The Contract Shop'          $13,529
    "380023369290925":  "honeytribe",    # 'HoneyTribe'                 $11,182
    "465444904516684":  "meloyelo",      # 'MeloYelo'                   $16,578
    "921953393594856":  "riverdance",    # 'Riverdance Ad Account'       $2,871
    "831243410062412":  "agora",         # 'Agora Data Driven'  internal, not a customer
    # -- 🔴 S7000 IS TWO SLUGS, NEVER ONE ----------------------------------------------
    # client_S7000's binding requirement is that INTO and Service 7000 must NEVER see each
    # other's data -- enforced today by separate payload objects and per-scope IAM
    # conditions. A single 's7000' slug would collapse that boundary at the view layer and
    # leak one brand's spend into the other's dashboard. Two accounts -> two slugs -> two
    # views. Do not "simplify" these into one.
    "1721606312060475": "service7000",   # 'S7000'                       $2,325
    "1395577394904072": "into",          # 'INTO Schuleraustausch'         $470
    # -- 🔴 LIVE SPEND, NO CLIENT FOLDER: explicitly unmapped, never guessed -----------
    # ~$180k/yr between them. Mapping one of these to a neighbouring client would put six
    # figures of somebody else's spend on a client's dashboard. They stay NULL until a
    # human decides. Give one a client folder, then add its slug here.
    "4786451891457735": None,            # '4786451891457735, PHP'     $147,546  <-- the
                                         #    estate's BIGGEST spender, and nothing reads it
    "1945134529694403": None,            # 'Sabbath Spa'                $32,041
}

# Name fallback, used only when an account_id is unknown (a re-granted connector mints a new
# id). Lower-cased exact match on account_name.
NAME_TO_CLIENT = {
    "rhe": "rhe",
    "stuart baker": "rhe",
    "super cashflow development": "rhe",
    "the contract shop": "tcs",
    "honeytribe": "honeytribe",
    "meloyelo": "meloyelo",
    "riverdance ad account": "riverdance",
    "agora data driven": "agora",
    "s7000": "service7000",
    "into schüleraustausch": "into",
    "into schuleraustausch": "into",
}

AGENCY_SLUG = "agora"  # single-agency estate; kept for schema parity with bidbrain.

# 🔴 Windsor's /all endpoint is BLENDED ACROSS CONNECTORS. Verified live 2026-07-30:
# 'ASL Logistics' (account 106-434-7699, $4,892) comes back with datasource='google_ads' and
# carries NO ad_id at all -- 3 such rows appeared in a 209-row sample. Loading them here
# would put NULL into the REQUIRED ad_id column and poison the (ad_id, metric_date) MERGE
# key. perf_meta is the META table: everything else is dropped, loudly.
DATASOURCE = "facebook"


# ---------- Logging ----------
class FlushingStreamHandler(logging.StreamHandler):
    def emit(self, record):
        super().emit(record)
        self.flush()


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.FileHandler(str(LOG_FILE), mode="a", encoding="utf-8"),
        FlushingStreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("meta_loader")


# ---------- Errors ----------
class AccountUnavailableError(Exception):
    """Windsor 400: a requested account is not granted to this connector. Not retryable but
    SKIPPABLE -- per-account runs catch it and move on, so one misconfigured account never
    aborts the whole loader (or the nightly job)."""


class UniqueCountHorizonError(Exception):
    """Windsor/Facebook 400: 'breakdowns for unique-count fields are only available for the
    last 13 months'. Our field set carries unique-count fields, so a full backfill inevitably
    hits this ~13-month wall on the OLDEST chunks. It is the natural backfill horizon, NOT a
    bug -- the date-walk loops catch it and stop gracefully, like reaching MIN_DATE. The daily
    incremental run never reaches it. Confirmed live on this account 2026-07-30."""


# ---------- Helpers ----------
def get_api_key():
    """env WINDSOR_API_KEY (how Cloud Run mounts it) else Secret Manager via ADC."""
    env = (os.environ.get("WINDSOR_API_KEY") or "").strip()
    if env:
        log.info("Windsor key from env WINDSOR_API_KEY (length %d)", len(env))
        return env
    log.info("Fetching secret '%s' from Secret Manager...", WINDSOR_SECRET)
    client = secretmanager.SecretManagerServiceClient()
    path = f"projects/{PROJECT_ID}/secrets/{WINDSOR_SECRET}/versions/latest"
    val = client.access_secret_version(name=path).payload.data.decode("utf-8").strip()
    log.info("  got secret (length %d)", len(val))
    return val


def account_key(connector_or_id):
    """Bare account id from a connector string like 'facebook__1834792266844322'."""
    s = str(connector_or_id or "")
    return s.split("__", 1)[1] if "__" in s else s


def slugs_for(account_id, account_name):
    """(client_slug, agency_slug, is_known) for one account. NEVER guesses."""
    aid = str(account_id or "").strip()
    if aid in ACCOUNT_TO_CLIENT:
        slug = ACCOUNT_TO_CLIENT[aid]
        return slug, (AGENCY_SLUG if slug else None), True
    slug = NAME_TO_CLIENT.get(str(account_name or "").strip().lower())
    if slug:
        # The id changed (re-granted connector) but we recognise the name.
        return slug, AGENCY_SLUG, True
    return None, None, False


def discover_accounts(api_key):
    """Every account this key can see, filtered to the Meta connector.

    atrium: bidbrain hardcodes SELECT_ACCOUNTS. Here ONE key covers the whole estate, and the
    estate has already been surprised once by live accounts nobody knew about (PHP, at
    $147k/yr). So we ASK Windsor what exists on every run: a new account can then never be
    silently invisible -- it shows up as a loud NEW ACCOUNT warning instead.
    """
    log.info("Discovering accounts (one key, whole estate)...")
    params = {
        "api_key": api_key,
        "date_preset": "last_365d",
        "fields": "account_id,account_name,datasource,spend",
    }
    r = requests.get(WINDSOR_URL, params=params, timeout=TIMEOUT_SEC)
    r.raise_for_status()
    rows = (r.json() or {}).get("data", []) or []

    seen, dropped = {}, {}
    for row in rows:
        aid = str(row.get("account_id") or "").strip()
        if not aid:
            continue
        ds = str(row.get("datasource") or "").strip().lower()
        bucket = seen if ds == DATASOURCE else dropped
        acc = bucket.setdefault(aid, {"id": aid, "name": str(row.get("account_name") or "?"),
                                      "datasource": ds, "spend": 0.0})
        try:
            acc["spend"] += float(row.get("spend") or 0)
        except (TypeError, ValueError):
            pass

    for a in dropped.values():
        log.info("  skipping non-Meta account %-28s (datasource=%s, $%s) -- "
                 "/all is blended across connectors",
                 a["name"], a["datasource"], f"{a['spend']:,.0f}")

    out, unknown = [], []
    for a in sorted(seen.values(), key=lambda x: -x["spend"]):
        slug, _agency, known = slugs_for(a["id"], a["name"])
        a["client_slug"] = slug
        out.append(a)
        if not known:
            unknown.append(a)
        log.info("  %-30s %-18s -> client_slug=%s", a["name"], a["id"], slug or "NULL (unmapped)")

    if unknown:
        log.warning("=" * 70)
        for a in unknown:
            log.warning("🔴 NEW ACCOUNT not in ACCOUNT_TO_CLIENT: %r (%s, $%s last 365d). "
                        "Loading with client_slug NULL so it cannot leak into another client's "
                        "view. Add it to the map (or map it to None) to silence this.",
                        a["name"], a["id"], f"{a['spend']:,.0f}")
        log.warning("=" * 70)
    log.info("Discovered %d Meta account(s); %d non-Meta skipped.", len(out), len(dropped))
    return out


def select_accounts_for(account_ids):
    """Windsor wants the connector-prefixed form (note the DOUBLE underscore)."""
    return [f"{DATASOURCE}__{a}" for a in account_ids]


def latest_dates_per_account(bq):
    """MAX(metric_date) already loaded, keyed by account id. Absent = no rows yet."""
    sql = f"""
        SELECT account_id, MAX(metric_date) AS max_date
        FROM `{PROJECT_ID}.{DATASET}.{MAIN_TABLE}`
        WHERE account_id IS NOT NULL
        GROUP BY account_id
    """
    out = {}
    for r in bq.query(sql, location=LOCATION).result():
        md = r["max_date"]
        if md is None:
            continue
        if isinstance(md, str):
            md = date.fromisoformat(md[:10])
        out[account_key(r["account_id"])] = md
    return out


def chunk_filename(d_from, d_to, cache_tag):
    return CHUNKS_DIR / f"{cache_tag}_{d_from.isoformat()}_to_{d_to.isoformat()}.json"


def fetch_chunk(api_key, d_from, d_to, idx, total, select, cache_tag, force=False):
    """One chunk. Retries transient errors with capped backoff; fails FAST on permanent 4xx.

    ⚠️ Send date_from/date_to OR date_preset, never both: verified live 2026-07-30 that when
    both are present date_preset silently WINS (a 3-day range returned last_7d's 145 rows).
    """
    label = f"chunk {idx}/{total}" if total else f"chunk {idx}"
    if cache_tag != "all":
        label = f"{cache_tag} {label}"
    cache_file = chunk_filename(d_from, d_to, cache_tag)
    if cache_file.exists() and not force:
        rows = json.loads(cache_file.read_text(encoding="utf-8"))
        log.info("  [%s] CACHED %s..%s: %d rows", label, d_from, d_to, len(rows))
        return rows

    params = {
        "api_key": api_key,
        "date_from": d_from.isoformat(),
        "date_to": d_to.isoformat(),
        "fields": WINDSOR_FIELDS,
    }
    if select:
        params["select_accounts"] = ",".join(select)

    log.info("  [%s] Fetching %s..%s%s", label, d_from, d_to, " (FORCE)" if force else "")
    start = time.monotonic()
    attempt = 0
    while True:
        attempt += 1
        attempt_start = time.monotonic()
        try:
            r = requests.get(WINDSOR_URL, params=params, timeout=TIMEOUT_SEC)
            log.info("    attempt %d: HTTP %s in %.1fs", attempt, r.status_code,
                     time.monotonic() - attempt_start)
            r.raise_for_status()
            payload = r.json()
            rows = payload.get("data", [])
            if "data" not in payload:
                log.warning("    no 'data' key (keys: %s) -- treating as 0 rows", list(payload)[:5])
            log.info("  [%s] SUCCESS: %d rows in %.1fs", label, len(rows), time.monotonic() - start)
            CHUNKS_DIR.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps(rows), encoding="utf-8")
            return rows
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else None
            if status == 429 or (status is not None and status >= 500):
                if attempt >= MAX_ATTEMPTS:
                    raise RuntimeError(
                        f"Chunk {d_from}..{d_to}: gave up after {attempt} attempts on transient "
                        f"HTTP {status} (Windsor still failing).")
                sleep = min(RETRY_SLEEP_BASE * attempt, RETRY_SLEEP_MAX)
                log.warning("    attempt %d/%d transient HTTP %s; retrying in %ds",
                            attempt, MAX_ATTEMPTS, status, sleep)
                time.sleep(sleep)
                continue
            body = e.response.text[:500] if e.response is not None else ""
            low = body.lower()
            if status == 400 and "not available" in low:
                raise AccountUnavailableError(
                    f"Account(s) {','.join(select or ['<all>'])} not available in Windsor. "
                    f"Body:\n{body}")
            if status == 400 and "13 months" in low:
                raise UniqueCountHorizonError(
                    f"Chunk {d_from}..{d_to} is beyond Facebook's 13-month unique-count horizon. "
                    f"Body:\n{body}")
            raise RuntimeError(
                f"Chunk {d_from}..{d_to} got permanent HTTP {status}. This will NOT recover by "
                f"retrying -- likely a bad field name or auth. Body:\n{body}")
        except requests.exceptions.RequestException as e:
            if attempt >= MAX_ATTEMPTS:
                raise RuntimeError(f"Chunk {d_from}..{d_to}: gave up after {attempt} attempts "
                                   f"({type(e).__name__}: {e}).")
            sleep = min(RETRY_SLEEP_BASE * attempt, RETRY_SLEEP_MAX)
            log.warning("    attempt %d/%d FAILED (%s); retrying in %ds",
                        attempt, MAX_ATTEMPTS, type(e).__name__, sleep)
            time.sleep(sleep)


def to_int(v):
    if v in (None, "", "null"):
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def to_num(v):
    if v in (None, "", "null"):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# =================================================================================
# 🔴 SUM ON KEY COLLISION -- NEVER OVERWRITE. This one cost $321 before it was caught.
# =================================================================================
# Windsor can return several rows sharing the same natural key, differing only on a
# descriptive field such as `title`. On RHE there were ~60 such groups, and a client job that
# merged them by dict assignment (`by[k] = row`) silently discarded $321 of spend -- only an
# independent audit found it.
#
# atrium: bidbrain has no equivalent step, because on ITS accounts the key happens not to
# collide. It is added here for two reasons: the estate has already paid for this bug once,
# and BigQuery MERGE *errors* ("UPDATE/MERGE must match at most one source row for each target
# row") the first time a staging table carries a duplicate key -- so without this the nightly
# job would start failing the day Windsor first returns a collision.
#
# Additive counts are SUMMED. Rates/ratios cannot be summed, so they are carried
# impression-weighted. Unique counts (reach, unique_*) are NOT additive even within a day --
# the same person can appear in both variants -- so they take the MAX, which is the least
# wrong answer available. Nothing is dropped: the merged raw_row records every variant.
_ADDITIVE = {
    "impressions", "clicks", "link_clicks", "actions_link_click",
    "outbound_clicks_outbound_click", "actions_post_engagement", "actions_page_engagement",
    "actions_post_reaction", "actions_comment", "actions_post",
    "actions_onsite_conversion_post_save", "actions_video_view", "estimated_ad_recallers",
    "instagram_profile_visits", "actions_lead", "actions_offsite_conversion_fb_pixel_lead",
    "actions_onsite_conversion_lead_grouped", "actions_landing_page_view", "actions_add_to_cart",
    "actions_initiate_checkout", "actions_omni_purchase",
    "actions_offsite_conversion_fb_pixel_purchase", "actions_complete_registration",
    "action_values_omni_purchase", "action_values_offsite_conversion_fb_pixel_purchase",
    "video_play_actions_video_view", "video_p25_watched_actions_video_view",
    "video_p50_watched_actions_video_view", "video_p75_watched_actions_video_view",
    "video_p95_watched_actions_video_view", "video_p100_watched_actions_video_view",
    "video_thruplay_watched_actions_video_view", "spend",
}
_MAXED = {
    "reach", "unique_clicks", "unique_actions_link_click", "unique_actions_lead",
    "unique_outbound_clicks_outbound_click", "unique_actions_post_engagement",
}
_WEIGHTED = {
    "frequency", "cpc", "cpm", "cpp", "unique_link_clicks_ctr",
    "cost_per_action_type_link_click", "cost_per_unique_action_type_link_click",
    "outbound_clicks_ctr_outbound_click", "unique_outbound_clicks_ctr_outbound_click",
    "cost_per_outbound_click_outbound_click", "cost_per_unique_outbound_click_outbound_click",
    "estimated_ad_recall_rate", "cost_per_action_type_lead", "purchase_roas_omni_purchase",
    "website_purchase_roas_offsite_conversion_fb_pixel_purchase",
    "video_avg_time_watched_actions_video_view",
}


def _n(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def collapse_collisions(rows):
    """Collapse raw Windsor rows sharing (ad_id, date) by SUMMING. Never overwrites."""
    by, order = {}, []
    for r in rows:
        k = (str(r.get("ad_id") or ""), str(r.get("date") or "")[:10])
        cur = by.get(k)
        if cur is None:
            cur = dict(r)
            cur["_variants"] = [r]
            cur["_w_num"] = {f: _n(r.get(f)) * _n(r.get("impressions")) for f in _WEIGHTED}
            cur["_w_den"] = _n(r.get("impressions"))
            by[k] = cur
            order.append(k)
            continue
        cur["_variants"].append(r)
        for f in _ADDITIVE:
            if f in r or f in cur:
                cur[f] = _n(cur.get(f)) + _n(r.get(f))
        for f in _MAXED:
            if f in r or f in cur:
                cur[f] = max(_n(cur.get(f)), _n(r.get(f)))
        for f in _WEIGHTED:
            cur["_w_num"][f] = cur["_w_num"].get(f, 0.0) + _n(r.get(f)) * _n(r.get("impressions"))
        cur["_w_den"] += _n(r.get("impressions"))
        # Keep any descriptive value the first variant lacked -- never drop information.
        for kk, vv in r.items():
            if cur.get(kk) in (None, "") and vv not in (None, ""):
                cur[kk] = vv

    out, collapsed = [], 0
    for k in order:
        row = by[k]
        variants = row.pop("_variants")
        num = row.pop("_w_num")
        den = row.pop("_w_den") or 0.0
        if len(variants) > 1:
            collapsed += 1
            for f in _WEIGHTED:
                row[f] = (num.get(f, 0.0) / den) if den else 0.0
            row["_collapsed_from"] = variants          # kept verbatim in raw_row for fidelity
        row["_raw_row_count"] = len(variants)
        out.append(row)
    if collapsed:
        log.warning("  🔴 %d (ad_id, date) key collision(s) SUMMED (never overwritten) -- "
                    "%d raw rows -> %d loaded rows", collapsed, len(rows), len(out))
    return out


def transform(row, ingested_at_iso):
    client_slug, agency_slug, _known = slugs_for(row.get("account_id"), row.get("account_name"))
    g = row.get
    variants = row.get("_collapsed_from")
    raw = dict(row)
    raw.pop("_collapsed_from", None)
    raw.pop("_raw_row_count", None)
    if variants:
        raw["_collapsed_from"] = variants
    return {
        "platform": "meta",
        "account_id": g("account_id"),
        "account_name": g("account_name"),
        "campaign_id": g("campaign_id"),
        "campaign_name": g("campaign"),
        "objective": g("objective"),
        "adset_id": g("adset_id"),
        "adset_name": g("adset_name"),
        "ad_id": g("ad_id"),
        "ad_name": g("ad_name"),
        "effective_status": g("effective_status"),
        "client_slug": client_slug,
        "agency_slug": agency_slug,
        "metric_date": str(g("date") or "")[:10],
        "currency": g("account_currency"),
        "campaign_spend_cap": to_num(g("campaign_spend_cap")),
        "impressions": to_int(g("impressions")) or 0,
        "reach": to_int(g("reach")),
        "frequency": to_num(g("frequency")),
        "cost": to_num(g("spend")) or 0,
        "cpc": to_num(g("cpc")),
        "cpm": to_num(g("cpm")),
        "cpp": to_num(g("cpp")),
        "clicks": to_int(g("clicks")) or 0,
        "unique_clicks": to_int(g("unique_clicks")),
        "link_clicks": to_int(g("link_clicks")),
        "link_clicks_actions": to_int(g("actions_link_click")),
        "unique_link_clicks": to_int(g("unique_actions_link_click")),
        "unique_link_clicks_ctr": to_num(g("unique_link_clicks_ctr")),
        "cost_per_link_click": to_num(g("cost_per_action_type_link_click")),
        "cost_per_unique_link_click": to_num(g("cost_per_unique_action_type_link_click")),
        "outbound_clicks": to_int(g("outbound_clicks_outbound_click")),
        "unique_outbound_clicks": to_int(g("unique_outbound_clicks_outbound_click")),
        "outbound_ctr": to_num(g("outbound_clicks_ctr_outbound_click")),
        "unique_outbound_ctr": to_num(g("unique_outbound_clicks_ctr_outbound_click")),
        "cost_per_outbound_click": to_num(g("cost_per_outbound_click_outbound_click")),
        "cost_per_unique_outbound_click": to_num(g("cost_per_unique_outbound_click_outbound_click")),
        "post_engagement": to_int(g("actions_post_engagement")),
        "unique_post_engagement": to_int(g("unique_actions_post_engagement")),
        "page_engagement": to_int(g("actions_page_engagement")),
        "reactions": to_int(g("actions_post_reaction")),
        "comments": to_int(g("actions_comment")),
        "shares": to_int(g("actions_post")),
        "saves": to_int(g("actions_onsite_conversion_post_save")),
        "video_3s_views": to_int(g("actions_video_view")),
        "est_ad_recall_lift": to_num(g("estimated_ad_recallers")),
        "est_ad_recall_rate": to_num(g("estimated_ad_recall_rate")),
        "instagram_profile_visits": to_int(g("instagram_profile_visits")),
        "leads": to_int(g("actions_lead")),
        "leads_website": to_int(g("actions_offsite_conversion_fb_pixel_lead")),
        "leads_onfacebook": to_int(g("actions_onsite_conversion_lead_grouped")),
        "unique_leads": to_int(g("unique_actions_lead")),
        "cost_per_lead": to_num(g("cost_per_action_type_lead")),
        "landing_page_views": to_int(g("actions_landing_page_view")),
        "add_to_cart": to_int(g("actions_add_to_cart")),
        "initiate_checkout": to_int(g("actions_initiate_checkout")),
        "purchases": to_int(g("actions_omni_purchase")),
        "purchases_website": to_int(g("actions_offsite_conversion_fb_pixel_purchase")),
        "registrations": to_int(g("actions_complete_registration")),
        "purchase_value": to_num(g("action_values_omni_purchase")),
        "purchase_value_website": to_num(g("action_values_offsite_conversion_fb_pixel_purchase")),
        "purchase_roas": to_num(g("purchase_roas_omni_purchase")),
        "purchase_roas_website": to_num(g("website_purchase_roas_offsite_conversion_fb_pixel_purchase")),
        "video_starts": to_int(g("video_play_actions_video_view")),
        "video_25": to_int(g("video_p25_watched_actions_video_view")),
        "video_50": to_int(g("video_p50_watched_actions_video_view")),
        "video_75": to_int(g("video_p75_watched_actions_video_view")),
        "video_95": to_int(g("video_p95_watched_actions_video_view")),
        "video_completes": to_int(g("video_p100_watched_actions_video_view")),
        "thruplays": to_int(g("video_thruplay_watched_actions_video_view")),
        "video_avg_watch_time": to_num(g("video_avg_time_watched_actions_video_view")),
        "quality_ranking": g("quality_ranking"),
        "engagement_rate_ranking": g("engagement_rate_ranking"),
        "conversion_rate_ranking": g("conversion_rate_ranking"),
        "creative_id": g("creative_id"),
        "creative_thumbnail_url": g("thumbnail_url"),
        "ig_thumbnail_url": g("effective_instagram_media__thumbnail_url"),
        "placement_thumbnail_url": g("placement_ad_thumbnail_url"),
        "creative_title": g("title"),
        "creative_body": g("body"),
        "creative_link_url": g("link_url"),
        "destination_url": g("link"),
        "datasource": g("datasource"),
        "ingested_at": ingested_at_iso,
        "source": "windsor.facebook",
        "raw_row_count": int(row.get("_raw_row_count") or 1),
        "raw_row": json.dumps(raw, default=str),
    }


# Every column the MERGE updates -- i.e. everything except the two key columns
# (ad_id, metric_date). Kept as an explicit list so adding a column to the schema without
# adding it here is a visible omission rather than a silent one.
_MERGE_SET_COLS = [
    "platform", "account_id", "account_name", "campaign_id", "campaign_name", "objective",
    "adset_id", "adset_name", "ad_name", "effective_status", "client_slug", "agency_slug",
    "currency", "campaign_spend_cap", "impressions", "reach", "frequency", "cost", "cpc",
    "cpm", "cpp", "clicks", "unique_clicks", "link_clicks", "link_clicks_actions",
    "unique_link_clicks", "unique_link_clicks_ctr", "cost_per_link_click",
    "cost_per_unique_link_click", "outbound_clicks", "unique_outbound_clicks", "outbound_ctr",
    "unique_outbound_ctr", "cost_per_outbound_click", "cost_per_unique_outbound_click",
    "post_engagement", "unique_post_engagement", "page_engagement", "reactions", "comments",
    "shares", "saves", "video_3s_views", "est_ad_recall_lift", "est_ad_recall_rate",
    "instagram_profile_visits", "leads", "leads_website", "leads_onfacebook", "unique_leads",
    "cost_per_lead", "landing_page_views", "add_to_cart", "initiate_checkout", "purchases",
    "purchases_website", "registrations", "purchase_value", "purchase_value_website",
    "purchase_roas", "purchase_roas_website", "video_starts", "video_25", "video_50",
    "video_75", "video_95", "video_completes", "thruplays", "video_avg_watch_time",
    "quality_ranking", "engagement_rate_ranking", "conversion_rate_ranking", "creative_id",
    "creative_thumbnail_url", "ig_thumbnail_url", "placement_thumbnail_url", "creative_title",
    "creative_body", "creative_link_url", "destination_url", "datasource", "ingested_at",
    "source", "raw_row_count", "raw_row",
]


def prepare_rows(raw_rows):
    """datasource filter -> collision collapse -> drop rows the MERGE key cannot hold."""
    meta, other = [], {}
    for r in raw_rows:
        ds = str(r.get("datasource") or "").strip().lower()
        if ds == DATASOURCE:
            meta.append(r)
        else:
            other[ds or "?"] = other.get(ds or "?", 0) + 1
    for ds, n in other.items():
        log.info("  dropped %d row(s) from non-Meta datasource %r (/all is blended)", n, ds)

    kept, no_key = [], 0
    for r in meta:
        if not str(r.get("ad_id") or "").strip() or not str(r.get("date") or "").strip():
            no_key += 1
            continue
        kept.append(r)
    if no_key:
        log.warning("  dropped %d Meta row(s) with no ad_id/date -- they cannot hold the "
                    "(ad_id, metric_date) MERGE key", no_key)
    return collapse_collisions(kept)


def load_chunk_to_bq(bq, storage_client, schema, raw_rows, ingested_at, d_from, d_to,
                     dry_run=False):
    rows = prepare_rows(raw_rows)
    if not rows:
        log.info("  (no loadable rows for %s..%s, skipping BQ load)", d_from, d_to)
        return 0, 0

    transformed = [transform(r, ingested_at) for r in rows]
    if dry_run:
        spend = sum(_n(t["cost"]) for t in transformed)
        by_slug = {}
        for t in transformed:
            by_slug[t["client_slug"]] = by_slug.get(t["client_slug"], 0.0) + _n(t["cost"])
        log.info("  DRY RUN: %d rows, $%.2f spend, by slug: %s",
                 len(transformed), spend,
                 ", ".join(f"{k or 'NULL'}=${v:,.0f}" for k, v in sorted(
                     by_slug.items(), key=lambda kv: -kv[1])))
        return 0, 0

    run_id = uuid.uuid4().hex[:8]
    local_path = WORK_DIR / f"load_{run_id}.ndjson"
    with local_path.open("w", encoding="utf-8") as f:
        for row in transformed:
            f.write(json.dumps(row) + "\n")
    log.info("  Wrote %d rows (%.1f KB) to %s",
             len(transformed), local_path.stat().st_size / 1024, local_path.name)

    gcs_path = f"loads/meta/{d_from.isoformat()}_to_{d_to.isoformat()}_{run_id}.ndjson"
    bucket = storage_client.bucket(GCS_BUCKET)
    bucket.blob(gcs_path).upload_from_filename(str(local_path))
    gcs_uri = f"gs://{GCS_BUCKET}/{gcs_path}"
    log.info("  Uploaded to %s", gcs_uri)

    staging_ref = f"{PROJECT_ID}.{DATASET}.{STAGING_TABLE}"
    job_config = bigquery.LoadJobConfig(
        source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
        schema=schema,
        write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
        create_disposition=bigquery.CreateDisposition.CREATE_IF_NEEDED,
    )
    load_job = bq.load_table_from_uri(gcs_uri, staging_ref, job_config=job_config,
                                      location=LOCATION)
    log.info("  Load job %s started, waiting...", load_job.job_id)
    load_job.result()
    log.info("  Loaded %s rows into staging", load_job.output_rows)

    set_clause = ",\n        ".join(f"{c} = S.{c}" for c in _MERGE_SET_COLS)
    merge_sql = f"""
    MERGE `{PROJECT_ID}.{DATASET}.{MAIN_TABLE}` T
    USING `{staging_ref}` S
    ON  T.ad_id       = S.ad_id
    AND T.metric_date = S.metric_date
    WHEN MATCHED THEN UPDATE SET
        {set_clause}
    WHEN NOT MATCHED THEN INSERT ROW
    """
    job = bq.query(merge_sql, location=LOCATION)
    job.result()
    stats = job.dml_stats
    log.info("  MERGE: inserted %s, updated %s", stats.inserted_row_count, stats.updated_row_count)

    bq.delete_table(staging_ref, not_found_ok=True)
    local_path.unlink(missing_ok=True)
    return stats.inserted_row_count, stats.updated_row_count


# ---------- Main ----------
def main():
    global CHUNK_DAYS
    overall_start = time.monotonic()
    argv = sys.argv[1:]
    force = "--force" in argv
    dry_run = "--dry-run" in argv

    def _valued(flag):
        """--flag VALUE or --flag=VALUE -> (value, tokens_to_drop)."""
        for i, a in enumerate(argv):
            if a == flag and i + 1 < len(argv):
                return argv[i + 1], {flag, argv[i + 1]}
            if a.startswith(flag + "="):
                return a.split("=", 1)[1], {a}
        return "", set()

    only, only_tokens = _valued("--only")
    chunk_days, chunk_tokens = _valued("--chunk-days")
    if chunk_days:
        CHUNK_DAYS = max(1, int(chunk_days))

    skip = {"--force", "--dry-run"} | only_tokens | chunk_tokens
    pos = [a for a in argv if a not in skip]
    fixed_range = len(pos) == 2

    windsor_api.force_ipv4()  # Cloud Run has no IPv6 egress -- see the import note.

    log.info("=" * 70)
    if fixed_range:
        start_d, end_d = date.fromisoformat(pos[0]), date.fromisoformat(pos[1])
        log.info("META LOADER START (fixed range): %s to %s%s",
                 start_d, end_d, "  (FORCE)" if force else "")
    else:
        end_d = date.today() - timedelta(days=1)
        log.info("META LOADER START (incremental per-account): refresh each account from its "
                 "last BQ day (lookback %dd) up to %s; accounts with no data get a full "
                 "backward-walk backfill%s",
                 INCREMENTAL_LOOKBACK_DAYS, end_d, "  (FORCE)" if force else "")
    log.info("Target: %s.%s.%s (%s) | staging gs://%s",
             PROJECT_ID, DATASET, MAIN_TABLE, LOCATION, GCS_BUCKET)
    log.info("Artifacts dir: %s | chunk size: %dd%s",
             WORK_DIR, CHUNK_DAYS, " | DRY RUN (no writes)" if dry_run else "")
    log.info("=" * 70)

    CHUNKS_DIR.mkdir(parents=True, exist_ok=True)
    api_key = get_api_key()
    ingested_at = datetime.now(timezone.utc).isoformat()

    accounts = discover_accounts(api_key)
    if only:
        wanted = [s.strip().lower() for s in only.split(",") if s.strip()]
        before = len(accounts)
        accounts = [a for a in accounts
                    if any(w == (a.get("client_slug") or "").lower()
                           or w == a["id"].lower()
                           or w in a["name"].lower() for w in wanted)]
        log.info("--only %r -> %d of %d account(s): %s", only, len(accounts), before,
                 ", ".join(f"{a['name']} ({a.get('client_slug') or 'NULL'})" for a in accounts))
        if not accounts:
            raise SystemExit(f"[ERROR] --only {only!r} matched no account.")

    bq = bigquery.Client(project=PROJECT_ID, location=LOCATION)
    storage_client = storage.Client(project=PROJECT_ID)
    main_table = bq.get_table(f"{PROJECT_ID}.{DATASET}.{MAIN_TABLE}")
    schema = main_table.schema
    log.info("BQ ready. Main table: %s (%d cols)", main_table.full_table_id, len(schema))

    grand = {"fetched": 0, "inserted": 0, "updated": 0}

    def run_chunk(d_from, d_to, idx, total, select, cache_tag):
        log.info("-" * 70)
        rows = fetch_chunk(api_key, d_from, d_to, idx, total, select, cache_tag, force=force)
        grand["fetched"] += len(rows)
        try:
            ins, upd = load_chunk_to_bq(bq, storage_client, schema, rows, ingested_at,
                                        d_from, d_to, dry_run=dry_run)
            grand["inserted"] += ins
            grand["updated"] += upd
        except Exception as e:  # noqa: BLE001 -- the chunk JSON is cached; retry the BQ side alone
            log.error("  BQ LOAD FAILED for %s..%s: %s: %s", d_from, d_to, type(e).__name__, e)
            log.error("  Chunk JSON is cached; re-run to retry just the BQ side.")
        log.info("  RUNNING TOTAL: fetched=%d, inserted=%d, updated=%d, elapsed=%.1f min",
                 grand["fetched"], grand["inserted"], grand["updated"],
                 (time.monotonic() - overall_start) / 60)
        return len(rows)

    def process_forward_range(d_start, d_end, select, cache_tag):
        chunks, cur = [], d_start
        while cur <= d_end:
            ce = min(cur + timedelta(days=CHUNK_DAYS - 1), d_end)
            chunks.append((cur, ce))
            cur = ce + timedelta(days=1)
        chunks.reverse()  # newest first
        total = len(chunks)
        log.info("  %d chunk(s), newest first: %s..%s", total, d_start, d_end)
        for i, (d_from, d_to) in enumerate(chunks, start=1):
            try:
                run_chunk(d_from, d_to, i, total, select, cache_tag)
            except UniqueCountHorizonError:
                # Chunks are newest-first, so everything still to come is older and would
                # fail identically. Stop this range cleanly.
                log.warning("  >>> Reached Facebook's 13-month unique-count horizon at %s..%s; "
                            "older chunks cannot be served with the wide field set. Stopping "
                            "this range gracefully.", d_from, d_to)
                break
            time.sleep(INTER_CHUNK_SLEEP)

    def process_backward_walk(d_end, select, cache_tag):
        consecutive_empty, idx, cur_to = 0, 0, d_end
        while True:
            idx += 1
            cur_from = cur_to - timedelta(days=CHUNK_DAYS - 1)
            floor_hit = cur_from <= MIN_DATE
            if floor_hit:
                cur_from = MIN_DATE
            try:
                n = run_chunk(cur_from, cur_to, idx, None, select, cache_tag)
            except UniqueCountHorizonError:
                log.warning("  >>> Reached Facebook's 13-month unique-count horizon at %s..%s. "
                            "Stopping backfill (servable history exhausted).", cur_from, cur_to)
                break
            if n == 0:
                consecutive_empty += 1
                log.info("  empty chunk #%d of %d before stopping",
                         consecutive_empty, STOP_AFTER_EMPTY_CHUNKS)
                if consecutive_empty >= STOP_AFTER_EMPTY_CHUNKS:
                    log.info("  >>> %d consecutive empty chunks. Assuming start of history. "
                             "Stopping.", STOP_AFTER_EMPTY_CHUNKS)
                    break
            else:
                consecutive_empty = 0
            if floor_hit:
                log.info("  >>> Reached MIN_DATE floor (%s). Stopping.", MIN_DATE)
                break
            cur_to = cur_from - timedelta(days=1)
            time.sleep(INTER_CHUNK_SLEEP)

    if fixed_range:
        sel = select_accounts_for([a["id"] for a in accounts])
        log.info("Forward-loading %s..%s (%d account(s) together)", start_d, end_d, len(accounts))
        process_forward_range(start_d, end_d, sel, "all")
    else:
        last_dates = latest_dates_per_account(bq)
        log.info("Existing data for %d/%d account(s)", len(last_dates), len(accounts))
        skipped = []
        for acc in accounts:
            aid = acc["id"]
            last = last_dates.get(aid)
            sel = select_accounts_for([aid])
            log.info("=" * 70)
            try:
                if last is None:
                    log.info("ACCOUNT %s (%s): no rows in BQ -> full backfill (backward walk)",
                             acc["name"], aid)
                    process_backward_walk(end_d, sel, aid)
                else:
                    start = max(last - timedelta(days=INCREMENTAL_LOOKBACK_DAYS), MIN_DATE)
                    if start > end_d:
                        start = end_d      # already current; still re-pull its last day
                    log.info("ACCOUNT %s (%s): last BQ day %s -> incremental %s..%s",
                             acc["name"], aid, last, start, end_d)
                    process_forward_range(start, end_d, sel, aid)
            except AccountUnavailableError as e:
                log.warning("SKIPPING account %s (%s): %s", acc["name"], aid, e)
                skipped.append(acc["name"])
        if skipped:
            log.warning("%d account(s) skipped (unavailable in Windsor): %s",
                        len(skipped), ", ".join(skipped))

    log.info("=" * 70)
    log.info("META LOADER DONE in %.1f min", (time.monotonic() - overall_start) / 60)
    log.info("  Rows fetched:  %d", grand["fetched"])
    log.info("  Rows inserted: %d", grand["inserted"])
    log.info("  Rows updated:  %d", grand["updated"])
    log.info("=" * 70)


if __name__ == "__main__":
    main()
