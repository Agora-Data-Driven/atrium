"""Honey Tribe export job (Cloud Run job) — Stage 2, LIVE pull from Windsor + Shopify.

Honey Tribe is a Windsor/Shopify-LIVE client (like riverdance, NOT the BigQuery-fed template):
there is no dataset and no SQL views. Each scheduled run pulls every source from its API and
assembles the private `honeytribe.json` the gated dash service serves.

  Meta ads, per ad per day     -> Windsor.ai `all`                     -> data["meta"]
  Meta age x gender, per day   -> Windsor.ai `all` + breakdown fields  -> data["demographics"]
  Meta region, per month       -> Windsor.ai `all` + breakdown fields  -> data["demographics"]
  Shopify orders + line items  -> Shopify Admin REST (paginated)       -> data["shopify"]
  Shopify sessions by referrer -> Shopify Admin GraphQL / ShopifyQL    -> data["sessions"]

This REPLACES the Google-Sheet + Colab pipeline the client's Looker report reads today: the
notebook's Shopify->BigQuery sync, its first-time/returning derivation, its product taxonomy and
its session pull are all reproduced here, writing one private JSON instead of a spreadsheet.
The shape is matched BY NAME to dash/dashboard.html's DATA.* keys.

Breakdowns are SEPARATE pulls on purpose: Meta will not return age/gender/region alongside the
per-ad/day rows, and rejects revenue fields on a breakdown query — so those carry delivery
metrics only (spend/impressions/clicks/link_clicks/reach). Same constraint riverdance hit.

PII: the API carries customer emails, names, addresses and phones. NONE of it is emitted —
customers are reduced to a salted hash used only to count distinct people and to mark an order
first-time vs returning.

Secrets/config (never logged, never persisted):
  WINDSOR_API_KEY        Windsor connector key   (Secret Manager `honeytribe-windsor-key`)
  SHOPIFY_ACCESS_TOKEN   Shopify Admin token     (Secret Manager `honeytribe-shopify-token`)
  SHOPIFY_STORE_DOMAIN · WINDSOR_ACCOUNT · GCS_BUCKET · DATA_OBJECT
  HONEYTRIBE_LOCAL_OUT   write to this local path instead of GCS (off-cloud test)
"""
import base64
import collections
import datetime
import hashlib
import json
import os
import re
import socket
import time
import urllib.error
import urllib.request

import categorize

# --- Force IPv4 -------------------------------------------------------------
# Cloud Run's default egress has no IPv6 route, but Shopify and Windsor both publish AAAA
# records. `socket.create_connection` walks getaddrinfo in order, applying the FULL timeout to
# each address, so every request burned its entire 120 s connect timeout on the v6 address
# before falling back to v4 — measured at ~120 s per call in the first cloud run, which turned
# a 38-page backfill into a 76-minute job and blew the 30-minute task timeout.
# Filtering resolution to A records makes each call sub-second. Harmless locally.
if os.environ.get("FORCE_IPV4", "1") == "1":
    _real_getaddrinfo = socket.getaddrinfo

    def _ipv4_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):  # noqa: A002
        return _real_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)

    socket.getaddrinfo = _ipv4_getaddrinfo

# --- Per-client derivation (change only CLIENT) ------------------------------
PROJECT = "agora-data-driven"
CLIENT = "honeytribe"
BUCKET = os.environ.get("GCS_BUCKET", "agora-data-driven-%s-dash" % CLIENT)
DATA_OBJECT = os.environ.get("DATA_OBJECT", "%s.json" % CLIENT)

# --- Windsor (Meta) ----------------------------------------------------------
WINDSOR_URL = "https://connectors.windsor.ai/all"
WINDSOR_ACCOUNT = os.environ.get("WINDSOR_ACCOUNT", "facebook__380023369290925")
# Windsor's `all` endpoint accepts a date_preset ONLY (date_from/date_to and `maximum` both
# 400), AND caps the response: the full field list works at last_365d but 400s at last_730d
# and beyond. So each run fetches 12 months and MERGES the previous publication forward
# (see `merge_history`) — history accumulates in the bucket instead of being capped at a year.
# Breakdowns use a shorter window on purpose: age x gender is per-day, so a wide window
# would be tens of thousands of rows for what is a recent-audience read.
DATE_PRESET = os.environ.get("WINDSOR_DATE_PRESET", "last_365d")
BREAKDOWN_PRESET = os.environ.get("WINDSOR_BREAKDOWN_PRESET", "last_365d")
WINDSOR_SECRET = "%s-windsor-key" % CLIENT
# The client's own connector field list, plus reach/frequency/cpp which their sheet pull omits
# but the funnel tab needs.
FIELDS = ["account_name", "action_values_omni_purchase", "actions_add_to_cart",
          "actions_offsite_conversion_fb_pixel_purchase", "ad_name", "adcontent", "adset_name",
          "campaign", "clicks", "cpp", "creative_id", "datasource", "date", "frequency",
          "impressions", "link_clicks", "reach", "source", "spend",
          "unique_actions_link_click"]

# --- Creative gallery ---------------------------------------------------------
# `creative_id` rides along on the main pull for FREE - measured against this account:
# 813 rows and identical spend with and without it. That is what lets the gallery aggregate
# delivery over the selected date range instead of being a static side panel.
#
# The creative's text and image come from a second, small pull keyed by creative_id. Windsor's
# `creative_*` field family is EMPTY on this account (probed 2026-07-28); the populated names
# are the bare ones: creative_id/thumbnail_url/body 100%, title 96%, image_url 52%,
# instagram_permalink_url 47%. 53 distinct creatives.
CREATIVE_FIELDS = ["creative_id", "ad_name", "title", "body", "thumbnail_url", "image_url",
                   "instagram_permalink_url", "date", "impressions"]
CREATIVE_PRESET = os.environ.get("WINDSOR_CREATIVE_PRESET", "last_365d")
# Meta CDN links expire when an ad stops, so the busiest creatives get copied into our bucket.
CREATIVE_CACHE_MAX = int(os.environ.get("CREATIVE_CACHE_MAX", "60"))
CREATIVE_BODY_MAX = 1200
AGE_GENDER_FIELDS = ["date", "age", "gender", "spend", "impressions", "clicks", "link_clicks", "reach"]
REGION_FIELDS = ["date", "region", "spend", "impressions", "clicks", "link_clicks", "reach"]

# --- Shopify -----------------------------------------------------------------
SHOPIFY_DOMAIN = os.environ.get("SHOPIFY_STORE_DOMAIN", "midget-giraffe.myshopify.com")
SHOPIFY_SECRET = "%s-shopify-token" % CLIENT
SHOPIFY_API_VERSION = os.environ.get("SHOPIFY_API_VERSION", "2024-10")
SHOPIFY_GQL_VERSION = os.environ.get("SHOPIFY_GQL_VERSION", "2025-10")
SHOPIFY_PAGE_LIMIT = 250
SHOPIFY_SLEEP = 0.4          # Shopify's REST bucket refills at 2/s
SESSIONS_FROM = os.environ.get("SESSIONS_FROM", "2024-01-01")

_HERE = os.path.dirname(os.path.abspath(__file__))
_SALT = os.environ.get("HONEYTRIBE_SALT", "agora-honeytribe-v1")

_SIZES = ["XX-Small", "X-Small", "Small", "Small/Medium", "Medium", "Medium/Large", "Large",
          "X-Large", "XX-Large", "XXX-Large", "OS", "One Size"]

SEASONS = [
    (1, "January", "Winter", "Winter / New Year / Resort",
     "New-year styling, vacation looks, clearance, fresh wardrobe energy"),
    (2, "February", "Winter", "Valentine / Early Spring",
     "Date-night, feminine looks, early spring styling"),
    (3, "March", "Spring", "Spring / Occasion",
     "Brunch, church, Easter, fresh colors, dresses"),
    (4, "April", "Spring", "Spring / Occasion",
     "Easter, church outfits, brunch, spring events"),
    (5, "May", "Spring", "Spring / Graduation / Mother's Day / Vacation Prep",
     "Dresses, sets, gifting, celebration looks"),
    (6, "June", "Summer", "Summer / Vacation",
     "Shorts, sets, bold prints, travel looks"),
    (7, "July", "Summer", "Summer / Vacation / Early Fall Preview",
     "Warm-weather pieces, denim launches, restocks"),
    (8, "August", "Summer", "Late Summer / Back-to-School / Fall Transition",
     "Transitional styling, denim, versatile pieces"),
    (9, "September", "Fall", "Fall Transition",
     "Denim, layering, statement outfits, event looks"),
    (10, "October", "Fall", "Fall / Event / Pre-Holiday",
     "Statement pieces, richer colors, early holiday prep"),
    (11, "November", "Fall", "Holiday / Black Friday-Cyber Monday",
     "Biggest promo period, gifting, best sellers"),
    (12, "December", "Winter", "Holiday / Gifting / Winter / Resort",
     "Holiday outfits, gifts, vacation/resort pieces"),
]
SEASON_OF_MONTH = {m: se for (m, _mn, se, _fs, _w) in SEASONS}
MONTH_NAME = {m: mn for (m, mn, _se, _fs, _w) in SEASONS}
FASHION_SEASON = {m: fs for (m, _mn, _se, fs, _w) in SEASONS}

# Referrer domain -> the platform label Shopify's own session reports use, so the "visitors"
# and "buyers" series on the traffic chart are counted the same way and can share an axis.
# Order matters: the first needle found in the referrer/UTM wins, so put the specific
# Instagram/Facebook surface names (igshopping, ig) before the generic ones.
_PLATFORM_RULES = [
    ("instagram", "instagram"), ("igshopping", "instagram"), ("ig_", "instagram"),
    ("facebook", "facebook"), ("fb.com", "facebook"), ("fbclid", "facebook"),
    ("tiktok", "tiktok"), ("pinterest", "pinterest"), ("threads", "threads"),
    ("youtube", "youtube"), ("twitter", "twitter"), ("x.com", "twitter"),
    ("linkedin", "linkedin"), ("snapchat", "snapchat"), ("reddit", "reddit"),
    ("google", "google"), ("bing", "bing"), ("duckduckgo", "duckduckgo"),
    ("yahoo", "yahoo"), ("baidu", "baidu"), ("ecosia", "ecosia"),
    ("gmail", "email"), ("outlook", "email"), ("mail.", "email"), ("klaviyo", "email"),
    ("omnisend", "email"), ("newsletter", "email"),
]
# Exact tokens that are a surface of a platform rather than a domain.
_EXACT_PLATFORM = {"ig": "instagram", "igshopping": "instagram", "fb": "facebook"}
# On-site apps (wishlist, reviews, search) — a self-referral, not a traffic source.
_APP_HOSTS = ("swym", "shopifypreview", "shopify", "recharge", "checkout")

# The store's own hostnames. Seeded with the myshopify domain and extended at runtime with the
# real storefront domains from shop.json — this account sells on shopmidgetgiraffe.com, which a
# hardcoded list would have counted as an external referrer (1,410 orders, found by the audit).
_OWN_HOSTS = ["myshopify", "honeytribe"]


def _flat(s):
    """Lowercase, strip punctuation — so `midget-giraffe` matches `shopmidgetgiraffe.com`."""
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def register_own_domains(domains):
    for d in domains:
        core = _flat(re.sub(r"^www\.", "", (d or "").lower().strip()).split(".")[0])
        if core and len(core) >= 4 and core not in _OWN_HOSTS:
            _OWN_HOSTS.append(core)


def normalize_platform(raw):
    """Map any referrer token — an order's referring domain or a Shopify session's
    `referrer_name` — onto ONE vocabulary, so "visitors" and "buyers" can share a chart.

    Own storefront domains and on-site apps collapse to "direct": a customer bouncing through
    the store's own second domain is not a new traffic source. (The audit caught 1,412 orders
    and 5,381 sessions being credited to the store's own hostnames.)
    """
    raw = (raw or "").strip().lower()
    if not raw:
        return "direct"
    if raw in _EXACT_PLATFORM:
        return _EXACT_PLATFORM[raw]
    flat = _flat(raw)
    if not flat:
        return "direct"
    if any(o in flat for o in _OWN_HOSTS) or any(_flat(a) in flat for a in _APP_HOSTS):
        return "direct"
    for needle, label in _PLATFORM_RULES:
        if _flat(needle) in flat:
            return label
    host = re.sub(r"^https?://", "", raw).split("/")[0].split(":")[0]
    host = re.sub(r"^(www|l|m|lm)\.", "", host)
    return (host or raw)[:24] or "direct"


def platform_of(referring_site, landing_site):
    """Normalise an order's referrer into the same vocabulary as Shopify's session report.

    Prefers the explicit UTM source on the landing URL (that is the paid-campaign truth) and
    falls back to the referring domain. Empty / own-domain / on-site app => "direct".
    """
    m = re.search(r"[?&]utm_source=([^&]+)", (landing_site or "").lower())
    if m:
        plat = normalize_platform(m.group(1))
        if plat != "direct":
            return plat
    rs = (referring_site or "").lower().strip()
    if not rs:
        return "direct"
    return normalize_platform(re.sub(r"^https?://", "", rs).split("/")[0])


def _num(x):
    try:
        if x is None or x == "":
            return 0.0
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def _hash(email):
    if not email:
        return None
    return hashlib.sha1((_SALT + str(email).strip().lower()).encode("utf-8")).hexdigest()[:12]


def _secret(env_name, secret_id):
    """Read a secret from the mounted env var, falling back to Secret Manager directly."""
    v = os.environ.get(env_name, "").strip()
    if v:
        return v
    from google.cloud import secretmanager
    sm = secretmanager.SecretManagerServiceClient()
    name = "projects/%s/secrets/%s/versions/latest" % (PROJECT, secret_id)
    return sm.access_secret_version(name={"name": name}).payload.data.decode("utf-8").strip()


# --- Windsor -----------------------------------------------------------------
def _windsor(api_key, fields, preset=None):
    from urllib.parse import urlencode
    q = urlencode({"api_key": api_key, "date_preset": preset or DATE_PRESET,
                   "fields": ",".join(fields), "select_accounts": WINDSOR_ACCOUNT})
    req = urllib.request.Request(WINDSOR_URL + "?" + q,
                                 headers={"User-Agent": "agora-honeytribe/1.0"})
    with urllib.request.urlopen(req, timeout=240) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    return (payload["data"] if isinstance(payload, dict) and "data" in payload else payload) or []


def fetch_meta(api_key):
    raw = _windsor(api_key, FIELDS)
    seen = {}
    for r in raw:
        d = str(r.get("date") or "")[:10]
        if not d:
            continue
        key = (d, r.get("campaign") or "", r.get("adset_name") or "", r.get("ad_name") or "")
        seen[key] = {
            "d": d,
            "cid": str(r.get("creative_id") or "") or None,
            "camp": (r.get("campaign") or "").strip(),
            "adset": (r.get("adset_name") or "").strip(),
            "ad": (r.get("ad_name") or "").strip(),
            "spend": round(_num(r.get("spend")), 2),
            "imps": int(_num(r.get("impressions"))),
            "clicks": int(_num(r.get("clicks"))),
            "lclk": int(_num(r.get("link_clicks"))),
            "reach": int(_num(r.get("reach"))),
            "freq": round(_num(r.get("frequency")), 4),
            "atc": _num(r.get("actions_add_to_cart")),
            "pur": _num(r.get("actions_offsite_conversion_fb_pixel_purchase")),
            "rev": round(_num(r.get("action_values_omni_purchase")), 2),
        }
    rows = sorted(seen.values(), key=lambda x: (x["d"], x["camp"], x["ad"]))
    dates = [r["d"] for r in rows]
    return {
        "rows": rows,
        "range": [min(dates), max(dates)] if dates else [None, None],
        "campaigns": sorted({r["camp"] for r in rows if r["camp"]}),
        "adsets": sorted({r["adset"] for r in rows if r["adset"]}),
        "ads": sorted({r["ad"] for r in rows if r["ad"]}),
    }


def _is_link_preview(url):
    """True for Meta's external image PROXY, which is a preview of the destination PAGE, not the
    ad. Meta serves it when a creative has no real image (video templates, some catalogue ads):
    `https://external-<edge>.xx.fbcdn.net/emg1/...?url=<page>`. It IS a valid image — a near-blank
    grey tile — so the browser's onerror never fires and the card would render a grey box. Real
    creative images come from `scontent-<edge>.xx.fbcdn.net/v/t39...` or `/v/t45...`.
    """
    u = (url or "").lower()
    return "//external-" in u and "/emg1/" in u


def _usable_image(image_url, thumb_url):
    """Best real image for a creative, or "" when Meta only offers a link preview (the card then
    falls back to the branded headline tile, which says more than a grey box)."""
    for u in (image_url, thumb_url):
        if u and not _is_link_preview(u):
            return u
    return ""


def _clean_headline(title, name):
    """Meta's `title` is often the display LINK, not a headline. Fall back to the ad name."""
    t = (title or "").strip()
    looks_like_link = (t.lower().startswith(("http://", "https://", "www."))
                       or (" " not in t and "." in t and len(t) < 40))
    # Catalogue / dynamic ads carry an UNRENDERED Liquid template as their title, e.g.
    # "{{product.name}}-{{product.price strip_zeros}}". Meta fills that per impression, so the
    # stored value is meaningless to a reader — fall back to the ad name.
    is_template = "{{" in t or "}}" in t
    if not t or looks_like_link or is_template:
        return (name or "").strip() or "(untitled creative)"
    return t


def fetch_creatives(api_key):
    """One row per creative_id: the ad's text and image, so the gallery shows the ACTUAL ads.

    No metrics here on purpose - `meta.rows` already carry `cid`, so the dashboard aggregates
    delivery per creative over whatever period/campaign/ad filter is active. One source for the
    numbers is what stops the gallery disagreeing with the tiles above it.
    """
    try:
        raw = _windsor(api_key, CREATIVE_FIELDS, CREATIVE_PRESET)
    except Exception as e:  # noqa: BLE001 - a gallery must never sink the export
        print("  creatives: pull skipped (%s)" % str(e)[:140])
        return {"enabled": False, "items": [], "error": str(e)[:200], "window": CREATIVE_PRESET}

    best = {}
    for r in sorted(raw, key=lambda x: str(x.get("date") or "")):   # newest non-empty wins
        cid = str(r.get("creative_id") or "").strip()
        if not cid:
            continue
        cur = best.setdefault(cid, {"cid": cid, "name": "", "title": "", "body": "",
                                    "thumb": "", "image": "", "link": ""})
        for src, dst in (("ad_name", "name"), ("title", "title"), ("body", "body"),
                         ("thumbnail_url", "thumb"), ("image_url", "image"),
                         ("instagram_permalink_url", "link")):
            v = str(r.get(src) or "").strip()
            if v:
                cur[dst] = v

    items = []
    for cid, c in best.items():
        body = c["body"]
        items.append({
            "cid": cid,
            "head": _clean_headline(c["title"], c["name"]),
            "ad": c["name"],
            "body": body[:CREATIVE_BODY_MAX] + ("\u2026" if len(body) > CREATIVE_BODY_MAX else ""),
            "thumb": _usable_image(c["image"], c["thumb"]),
            "link": c["link"],
        })
    print("  creatives: %d distinct (window %s)" % (len(items), CREATIVE_PRESET))
    return {"enabled": bool(items), "items": items, "error": "", "window": CREATIVE_PRESET}


def cache_creative_images(creatives, meta_rows):
    """Copy the busiest creatives' images into our OWN bucket - Meta's CDN links expire the moment
    an ad stops, after which the gallery would be a wall of grey boxes. Best-effort throughout: a
    miss falls back to the live URL, then to a branded text tile."""
    items = creatives.get("items") or []
    if not items:
        return creatives
    imps = collections.Counter()
    for r in meta_rows:
        if r.get("cid"):
            imps[r["cid"]] += r["imps"]
    order = sorted(items, key=lambda c: -imps.get(c["cid"], 0))[:CREATIVE_CACHE_MAX]

    local_dir = os.environ.get("HONEYTRIBE_CREATIVE_LOCAL_DIR")
    bucket = None
    have = set()
    if local_dir:
        os.makedirs(local_dir, exist_ok=True)
        have = set(os.listdir(local_dir))
    else:
        try:
            from google.cloud import storage
            bucket = storage.Client(project=PROJECT).bucket(BUCKET)
            have = {b.name[len("creatives/"):] for b in bucket.list_blobs(prefix="creatives/")
                    if b.name != "creatives/"}
        except Exception as e:  # noqa: BLE001
            print("  creatives: cache unavailable (%s)" % str(e)[:120])
            return creatives

    fetched = 0
    for c in order:
        cid, url = c["cid"], c.get("thumb")
        if cid in have:
            c["cached"] = True
            continue
        if not url:
            continue
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = resp.read()
                ctype = resp.headers.get("Content-Type", "image/jpeg")
            if not (data and ctype.startswith("image/")):
                continue
            if local_dir:
                with open(os.path.join(local_dir, cid), "wb") as fh:
                    fh.write(data)
            else:
                bucket.blob("creatives/" + cid).upload_from_string(data, content_type=ctype)
            c["cached"] = True
            have.add(cid)
            fetched += 1
        except Exception as e:  # noqa: BLE001 - an expired link just 403s
            print("  creative %s: cache skip (%s)" % (cid, str(e)[:80]))
    for c in items:
        c.setdefault("cached", c["cid"] in have)
    print("  creatives: %d newly cached, %d of %d have a permanent image"
          % (fetched, sum(1 for c in items if c.get("cached")), len(items)))
    return creatives


def load_previous():
    """The JSON this job published last time, or None. Used to accumulate Meta history."""
    local_out = os.environ.get("HONEYTRIBE_LOCAL_OUT")
    try:
        if local_out:
            if not os.path.exists(local_out):
                return None
            with open(local_out, encoding="utf-8") as fh:
                return json.load(fh)
        from google.cloud import storage
        blob = storage.Client(project=PROJECT).bucket(BUCKET).blob(DATA_OBJECT)
        if not blob.exists():
            return None
        return json.loads(blob.download_as_bytes().decode("utf-8"))
    except Exception as e:  # noqa: BLE001 — no previous publication is a normal first run
        print("  previous publication unreadable (%s) — starting fresh" % str(e)[:120])
        return None


def merge_history(meta, previous):
    """Carry forward Meta rows older than this pull's window.

    Windsor caps a wide-field pull at ~12 months, so a run alone can never hold more than a
    year. Rows are keyed by (date, campaign, adset, ad): the fresh pull is authoritative for
    every day it covers (restatements land correctly) and anything older is kept verbatim.
    """
    if not previous:
        return meta
    old = ((previous.get("meta") or {}).get("rows")) or []
    if not old:
        return meta
    fresh_from = meta["range"][0]
    if not fresh_from:
        return meta
    keyed = {(r["d"], r["camp"], r["adset"], r["ad"]): r for r in old if r.get("d") < fresh_from}
    if not keyed:
        return meta
    for r in meta["rows"]:
        keyed[(r["d"], r["camp"], r["adset"], r["ad"])] = r
    rows = sorted(keyed.values(), key=lambda x: (x["d"], x["camp"], x["ad"]))
    dates = [r["d"] for r in rows]
    print("  meta history: %d fresh + %d carried forward = %d rows"
          % (len(meta["rows"]), len(rows) - len(meta["rows"]), len(rows)))
    return {
        "rows": rows,
        "range": [min(dates), max(dates)],
        "fresh_from": fresh_from,
        "campaigns": sorted({r["camp"] for r in rows if r["camp"]}),
        "adsets": sorted({r["adset"] for r in rows if r["adset"]}),
        "ads": sorted({r["ad"] for r in rows if r["ad"]}),
    }


def fetch_demographics(api_key):
    """Meta age x gender (per day) and region (rolled to month, to keep the payload small).

    Best-effort per breakdown: a failure yields an empty list plus the reason rather than
    sinking the whole export.
    """
    out = {"enabled": False, "age_gender": [], "region": [], "note": ""}
    problems = []
    try:
        rows = _windsor(api_key, AGE_GENDER_FIELDS, BREAKDOWN_PRESET)
        agg = collections.defaultdict(lambda: [0.0, 0, 0, 0, 0])
        for r in rows:
            d = str(r.get("date") or "")[:10]
            if not d:
                continue
            k = (d, r.get("age") or "Unknown", r.get("gender") or "unknown")
            a = agg[k]
            a[0] += _num(r.get("spend")); a[1] += int(_num(r.get("impressions")))
            a[2] += int(_num(r.get("clicks"))); a[3] += int(_num(r.get("link_clicks")))
            a[4] += int(_num(r.get("reach")))
        out["age_gender"] = [{"d": k[0], "age": k[1], "g": k[2], "spend": round(v[0], 2),
                              "imps": v[1], "clicks": v[2], "lclk": v[3], "reach": v[4]}
                             for k, v in sorted(agg.items())]
    except Exception as e:  # noqa: BLE001
        problems.append("age/gender: %s" % str(e)[:120])
        print("  age/gender breakdown skip: %s" % str(e)[:160])
    try:
        rows = _windsor(api_key, REGION_FIELDS, BREAKDOWN_PRESET)
        agg = collections.defaultdict(lambda: [0.0, 0, 0, 0, 0])
        for r in rows:
            d = str(r.get("date") or "")[:10]
            if not d:
                continue
            k = (d[:7], r.get("region") or "Unknown")   # month-granular: 50 states x 12, not x 365
            a = agg[k]
            a[0] += _num(r.get("spend")); a[1] += int(_num(r.get("impressions")))
            a[2] += int(_num(r.get("clicks"))); a[3] += int(_num(r.get("link_clicks")))
            a[4] += int(_num(r.get("reach")))
        out["region"] = [{"m": k[0], "region": k[1], "spend": round(v[0], 2), "imps": v[1],
                          "clicks": v[2], "lclk": v[3], "reach": v[4]}
                         for k, v in sorted(agg.items())]
    except Exception as e:  # noqa: BLE001
        problems.append("region: %s" % str(e)[:120])
        print("  region breakdown skip: %s" % str(e)[:160])

    out["enabled"] = bool(out["age_gender"] or out["region"])
    out["note"] = "; ".join(problems)
    out["window"] = BREAKDOWN_PRESET
    return out


# --- Shopify orders ----------------------------------------------------------
def _shopify_get(url, token):
    req = urllib.request.Request(url, headers={
        "X-Shopify-Access-Token": token,
        "Accept": "application/json",
        "User-Agent": "agora-honeytribe/1.0",
    })
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode("utf-8")), resp.headers.get("Link", "")


ORDER_FIELDS = ("id,name,created_at,updated_at,cancelled_at,test,financial_status,currency,"
                "subtotal_price,total_price,total_discounts,line_items,billing_address,"
                "shipping_address,contact_email,email,referring_site,landing_site,source_name")

# How far back an incremental run re-reads, by `updated_at`. Generous enough to catch late
# refunds and fulfilments that restate an older order, small enough to stay a page or two.
INCREMENTAL_DAYS = int(os.environ.get("SHOPIFY_INCREMENTAL_DAYS", "30"))


def fetch_shop_domains(token):
    """The store's own hostnames, so a self-referral is never counted as a traffic source."""
    try:
        payload, _ = _shopify_get("https://%s/admin/api/%s/shop.json"
                                  % (SHOPIFY_DOMAIN, SHOPIFY_API_VERSION), token)
        shop = payload.get("shop") or {}
        doms = [shop.get("domain"), shop.get("myshopify_domain"), SHOPIFY_DOMAIN]
        register_own_domains(doms)
        print("  shopify: own domains %s" % [d for d in doms if d])
        return shop
    except Exception as e:  # noqa: BLE001
        print("  shop.json skip: %s" % str(e)[:120])
        return {}


def _shopify_pages(token, url, label):
    raw, page = [], 0
    while url:
        page += 1
        payload, link = _shopify_get(url, token)
        batch = payload.get("orders", [])
        raw.extend(batch)
        if page % 10 == 0:
            print("  shopify %s: page %d, %d orders so far" % (label, page, len(raw)))
        url = None
        for part in link.split(","):
            if 'rel="next"' in part:
                url = part.split(";")[0].strip("<> ")
                break
        if url:
            time.sleep(SHOPIFY_SLEEP)
    print("  shopify %s: %d orders over %d page(s)" % (label, len(raw), page))
    return raw


def _normalize_order(o, skipped):
    """One Shopify order -> the compact record the dashboard reads, or None if it is not revenue."""
    if o.get("test"):
        skipped["test"] += 1
        return None
    if o.get("cancelled_at"):
        skipped["cancelled"] += 1
        return None
    # A voided/fully-refunded order is not revenue. `pending` IS kept — the client's own Shopify
    # export counts it, and dropping it would silently understate recent days.
    if o.get("financial_status") in ("voided", "refunded"):
        skipped[str(o.get("financial_status"))] += 1
        return None
    created = str(o.get("created_at") or "")[:10]
    if not created:
        skipped["no_date"] += 1
        return None

    ba = o.get("billing_address") or {}
    sa = o.get("shipping_address") or {}
    email = o.get("contact_email") or o.get("email")
    qty, items = 0, []
    for li in (o.get("line_items") or []):
        title = li.get("title") or ""
        n = int(_num(li.get("quantity")))
        qty += n
        if categorize.NON_PRODUCT.search(title):
            continue
        variant = (li.get("variant_title") or "").strip()
        items.append({
            "p": title.strip(),
            "sz": variant if variant in _SIZES else None,
            "q": n,
            "amt": round(_num(li.get("price")) * n - _num(li.get("total_discount")), 2),
        })
    return {
        "oid": o.get("id"),
        "d": created,
        "sub": round(_num(o.get("subtotal_price")), 2),
        "tot": round(_num(o.get("total_price")), 2),
        "disc": round(_num(o.get("total_discounts")), 2),
        "q": qty,
        "pr": ba.get("province_code") or sa.get("province_code")
              or ba.get("province") or sa.get("province"),
        "cid": _hash(email),
        "plat": platform_of(o.get("referring_site"), o.get("landing_site")),
        "items": items,
    }


def _previous_records(previous):
    """Rebuild {oid: record} from the last publication, so an incremental run can merge into it."""
    sh = ((previous or {}).get("shopify") or {})
    orders = sh.get("orders") or []
    if not orders or not all(o.get("oid") for o in orders):
        return None                      # pre-`oid` publication: cannot merge, must backfill
    recs = {}
    for o in orders:
        r = dict(o)
        r["items"] = []
        recs[o["oid"]] = r
    for ln in (sh.get("lines") or []):
        r = recs.get(ln.get("oid"))
        if r is not None:
            r["items"].append({"p": ln["p"], "sz": ln["sz"], "q": ln["q"], "amt": ln["amt"]})
    return recs


def fetch_shopify_orders(token, classify, previous=None):
    """Order history reduced to the order + line rows the dashboard reads.

    INCREMENTAL by default. A full backfill is ~38 pages of 250 orders; from Cloud Run that ran
    past the job's 30-minute timeout (it is ~80 s from a laptop — the store is US-hosted and each
    page is a large payload). So when a mergeable previous publication exists this re-reads only
    the last `INCREMENTAL_DAYS` by `updated_at` and merges by Shopify order id. The first run, a
    publication predating `oid`, or `SHOPIFY_FULL_SYNC=1` all force the full crawl.

    Merging by id means a restated order (late refund, fulfilment) replaces its old row cleanly,
    and one that has BECOME non-revenue is removed rather than left stale. First-time/returning
    and the line->order index are always recomputed across the WHOLE merged set — never
    incrementally, which would misclassify a returning customer whose first order predates the
    window.
    """
    base = "https://%s/admin/api/%s/orders.json" % (SHOPIFY_DOMAIN, SHOPIFY_API_VERSION)
    recs = _previous_records(previous)
    full = os.environ.get("SHOPIFY_FULL_SYNC") == "1" or recs is None

    if full:
        url = base + "?status=any&limit=%d&order=created_at+asc&fields=%s" % (
            SHOPIFY_PAGE_LIMIT, ORDER_FIELDS)
        raw = _shopify_pages(token, url, "backfill")
        recs = {}
    else:
        since = (datetime.datetime.now(datetime.timezone.utc)
                 - datetime.timedelta(days=INCREMENTAL_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
        url = base + "?status=any&limit=%d&order=updated_at+asc&updated_at_min=%s&fields=%s" % (
            SHOPIFY_PAGE_LIMIT, since, ORDER_FIELDS)
        raw = _shopify_pages(token, url, "incremental since " + since[:10])
        print("  shopify: merging into %d carried-forward orders" % len(recs))

    skipped = collections.Counter()
    for o in raw:
        rec = _normalize_order(o, skipped)
        oid = o.get("id")
        if rec is None:
            # seen this run but no longer revenue — drop any stale copy we were carrying
            if oid in recs:
                del recs[oid]
                skipped["retracted"] += 1
        else:
            recs[oid] = rec

    tmp = sorted(recs.values(), key=lambda x: (x["d"], x["oid"] or 0))

    # Order-level first-time / returning — a customer's earliest order is first-time. The
    # upstream notebook derives this the same way (groupby(email).cumcount()).
    orders, lines = [], []
    seen = set()
    for i, o in enumerate(tmp):
        if o["cid"] and o["cid"] in seen:
            o["ct"] = "ret"
        else:
            o["ct"] = "first"
            if o["cid"]:
                seen.add(o["cid"])
        orders.append({"oid": o["oid"], "d": o["d"], "sub": o["sub"], "tot": o["tot"],
                       "q": o["q"], "pr": o["pr"], "ct": o["ct"], "cid": o["cid"],
                       "disc": o["disc"], "plat": o["plat"]})
        for it in o["items"]:
            lines.append({"d": o["d"], "oi": i, "oid": o["oid"], "p": it["p"], "sz": it["sz"],
                          "q": it["q"], "amt": it["amt"], "pr": o["pr"], "ct": o["ct"],
                          "cat": classify(it["p"])})

    if skipped:
        print("  shopify: skipped %s" % dict(skipped))
    dates = [o["d"] for o in orders]
    return orders, lines, [min(dates), max(dates)] if dates else [None, None]


# --- Shopify sessions --------------------------------------------------------
def _shopifyql(token, query, retries=3):
    url = "https://%s/admin/api/%s/graphql.json" % (SHOPIFY_DOMAIN, SHOPIFY_GQL_VERSION)
    body = json.dumps({"query": "query { shopifyqlQuery(query: %s) { tableData { "
                                "columns { name } rows } } }" % json.dumps(query)}).encode("utf-8")
    for attempt in range(retries):
        req = urllib.request.Request(url, data=body, headers={
            "X-Shopify-Access-Token": token,
            "Content-Type": "application/json",
            "User-Agent": "agora-honeytribe/1.0",
        })
        with urllib.request.urlopen(req, timeout=120) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        errs = payload.get("errors")
        if errs:
            if "THROTTLED" in str(errs) and attempt < retries - 1:
                time.sleep(8 * (attempt + 1))     # ShopifyQL has its own tight bucket
                continue
            raise RuntimeError(str(errs)[:200])
        table = ((payload.get("data") or {}).get("shopifyqlQuery") or {}).get("tableData")
        if table is None:
            raise RuntimeError("ShopifyQL returned no tableData (unsupported query or plan)")
        cols = [c["name"] for c in (table.get("columns") or [])]
        # ShopifyQL returns each row as an OBJECT keyed by column name, but the schema allows a
        # positional array too. Zipping a dict would pair column names with column names and
        # silently produce rows of literal header text — handle both shapes explicitly.
        out = []
        for r in (table.get("rows") or []):
            out.append(dict(r) if isinstance(r, dict) else dict(zip(cols, r)))
        return out
    raise RuntimeError("ShopifyQL throttled")


def fetch_sessions(token):
    """Storefront sessions by platform, monthly — the "where do visitors come from" series.

    Best-effort: ShopifyQL needs a plan that exposes the Analytics API, so a failure records
    the reason and leaves the dashboard's traffic panel in its wired-empty state.
    """
    today = datetime.date.today().isoformat()
    try:
        rows = _shopifyql(token, "FROM sessions SHOW sessions GROUP BY month, referrer_name "
                                 "SINCE %s UNTIL %s ORDER BY month LIMIT 1000"
                                 % (SESSIONS_FROM, today))
        out = []
        for r in rows:
            # Same normaliser as the order side — one vocabulary, so the two series line up.
            out.append({"m": str(r.get("month") or "")[:7],
                        "plat": normalize_platform(r.get("referrer_name")),
                        "sessions": int(_num(r.get("sessions")))})
        return {"enabled": bool(out), "rows": out, "since": SESSIONS_FROM,
                "note": "" if out else "ShopifyQL returned no session rows."}
    except Exception as e:  # noqa: BLE001 — an analytics-plan limitation must not sink the export
        print("  sessions skip: %s" % str(e)[:180])
        return {"enabled": False, "rows": [], "since": SESSIONS_FROM,
                "note": "Shopify sessions unavailable: %s" % str(e)[:180]}


# --- Product trend (computed, no spreadsheet) --------------------------------
def build_trend(lines):
    """product x month rollup — the same table the client curates by hand in the trend sheet."""
    agg = collections.defaultdict(lambda: {"u": 0, "s": 0.0, "orders": set()})
    cat_of = {}
    for ln in lines:
        m = int(ln["d"][5:7])
        k = (ln["p"], m)
        agg[k]["u"] += ln["q"]
        agg[k]["s"] += ln["amt"]
        agg[k]["orders"].add(ln["oi"])
        cat_of[ln["p"]] = ln["cat"]
    out = []
    for (title, m), v in agg.items():
        out.append({"t": title, "m": m, "mn": MONTH_NAME[m], "se": SEASON_OF_MONTH[m],
                    "fs": FASHION_SEASON[m], "u": v["u"], "s": round(v["s"], 2),
                    "o": len(v["orders"]), "cat": cat_of.get(title, "Uncategorized")})
    out.sort(key=lambda x: (-x["u"], x["m"], x["t"]))
    return out


def _b64(path, mime):
    try:
        with open(path, "rb") as fh:
            return "data:%s;base64,%s" % (mime, base64.b64encode(fh.read()).decode())
    except OSError as e:
        print("  logo skip: %s" % e)
        return None


def build():
    classify = categorize.Classifier().classify
    started = time.time()

    previous = load_previous()

    wkey = _secret("WINDSOR_API_KEY", WINDSOR_SECRET)
    print("[%s] Meta via Windsor (%s, %s)" % (CLIENT, WINDSOR_ACCOUNT, DATE_PRESET))
    meta = merge_history(fetch_meta(wkey), previous)
    demographics = fetch_demographics(wkey)
    creatives = cache_creative_images(fetch_creatives(wkey), meta["rows"])

    stok = _secret("SHOPIFY_ACCESS_TOKEN", SHOPIFY_SECRET)
    print("[%s] Shopify orders from %s" % (CLIENT, SHOPIFY_DOMAIN))
    fetch_shop_domains(stok)          # must precede the orders pull — it feeds platform_of()
    orders, lines, srange = fetch_shopify_orders(stok, classify, previous)
    sessions = fetch_sessions(stok)

    return {
        "client": "Honey Tribe",
        "tagline": "Bold prints. Modern cuts.",
        "location": "United States",
        "currency": "USD",
        "generated_at": datetime.datetime.now(datetime.timezone.utc)
                                 .strftime("%Y-%m-%dT%H:%M:%SZ"),
        "build_seconds": round(time.time() - started, 1),
        "data_through": max(x for x in [srange[1], meta["range"][1]] if x),
        "source": {
            "shopify": "Shopify Admin API (%s)" % SHOPIFY_DOMAIN,
            "meta": "Meta (Facebook) Ads via Windsor.ai",
            "mode": "live",
            "shopify_through": srange[1],
            "meta_through": meta["range"][1],
        },
        "brand": {
            "mark": _b64(os.path.join(_HERE, "assets", "honeytribe-mark.png"), "image/png"),
            "agora_logo": _b64(os.path.join(_HERE, "assets", "agora.png"), "image/png"),
        },
        "shopify": {"orders": orders, "lines": lines, "range": srange},
        "meta": meta,
        "trend": build_trend(lines),
        "seasons": [{"m": m, "mn": mn, "se": se, "fs": fs, "why": why}
                    for (m, mn, se, fs, why) in SEASONS],
        "sessions": sessions,
        "demographics": demographics,
        "creatives": creatives,
    }


def main():
    data = build()
    body = json.dumps(data, separators=(",", ":"))

    local_out = os.environ.get("HONEYTRIBE_LOCAL_OUT")
    if local_out:
        os.makedirs(os.path.dirname(local_out), exist_ok=True)
        with open(local_out, "w", encoding="utf-8") as fh:
            fh.write(body)
        print("[%s] wrote %s — LOCAL" % (CLIENT, local_out))
    else:
        from google.cloud import storage
        blob = storage.Client(project=PROJECT).bucket(BUCKET).blob(DATA_OBJECT)
        blob.cache_control = "no-store"
        blob.upload_from_string(body, content_type="application/json")
        print("[%s] uploaded gs://%s/%s" % (CLIENT, BUCKET, DATA_OBJECT))

    s = data["shopify"]
    print("   orders %d (%s .. %s) · lines %d · meta %d (%s .. %s)"
          % (len(s["orders"]), s["range"][0], s["range"][1], len(s["lines"]),
             len(data["meta"]["rows"]), data["meta"]["range"][0], data["meta"]["range"][1]))
    print("   age/gender %d · region %d · sessions %d (%s) · %d KB · %.1fs"
          % (len(data["demographics"]["age_gender"]), len(data["demographics"]["region"]),
             len(data["sessions"]["rows"]), data["sessions"]["enabled"],
             len(body) // 1024, data["build_seconds"]))


if __name__ == "__main__":
    main()
