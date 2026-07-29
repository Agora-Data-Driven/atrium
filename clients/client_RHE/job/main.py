"""Rooming House Expert (RHE) export job (Cloud Run job) — LIVE pull from Windsor + ActiveCampaign.

RHE is an API-LIVE client (the `client_honeytribe` / `client_riverdance` pattern, NOT the
BigQuery-fed `client_template`): there is no dataset and no SQL views. Each scheduled run pulls
every source from its API and assembles the private `RHE.json` the gated dash service serves.

  Meta ads, per ad per day (3 accounts)  -> Windsor.ai `all`                  -> data["meta"]
  Meta age x gender / region / platform  -> Windsor.ai `all` + breakdowns     -> data["breakdowns"]
  ActiveCampaign email + quiz + contacts -> AC REST v3 (see activecampaign.py)-> data["email"]

THE BUSINESS MODEL DRIVES THE SHAPE. This is not an e-commerce client: Meta ads buy a lead magnet,
ActiveCampaign nurtures the lead, and a sale happens months later offline. So there is no ROAS and
no revenue anywhere in here. The metrics that matter are lead volume, COST PER LEAD, and email
engagement as the proxy for sales-readiness.

Windows and API limits — measured against this account 2026-07-27, do not "fix" them:
  * The `all` endpoint takes a `date_preset` ONLY (`date_from`/`date_to`/`maximum` all 400).
  * The 400 at `last_730d` is caused SPECIFICALLY by the two `unique_actions_*` fields
    ("breakdowns for unique-count fields are only available for..."). Dropping them lets the main
    pull reach `last_1095d` — real history back to 2024-01-15, 7,016 rows, $133,984 lifetime.
    So the main pull is split in two: DEEP (no unique fields, 1095d) + UNIQUE (365d), joined on
    (date, campaign, adset, ad). This is why RHE has deeper history than honeytribe.
  * Breakdowns are SEPARATE pulls — Meta will not return them alongside per-ad/day rows.
  * THE REGION BREAKDOWN CARRIES NO LEADS. `actions_lead` and `unique_actions_lead` are both
    identically 0 across all 253 rows, on every field combination tried. Geo therefore shows
    delivery metrics only (spend/impressions/clicks/reach) and the dashboard must NOT offer
    "CPL by region". Age x gender DOES carry leads and reconciles exactly to the main pull.
  * All THREE ad accounts are live and are three DIFFERENT brands (RHE / Stuart Baker /
    Super Cashflow Development), so rows carry `acct` and the dashboard defaults to splitting
    them. Only "rhe" spent in the last 30 days, which is why a 30-day probe looks single-account.

PII: none is emitted. See activecampaign.py — contacts are a salted hash plus an email domain.

Secrets/config (never logged, never persisted):
  WINDSOR_API_KEY         Windsor connector key   (Secret Manager `RHE-windsor-key`)
  ACTIVECAMPAIGN_API_KEY  AC Api-Token            (Secret Manager `RHE-activecampaign-key`)
  ACTIVECAMPAIGN_URL · WINDSOR_ACCOUNTS · GCS_BUCKET · DATA_OBJECT
  RHE_LOCAL_OUT           write to this local path instead of GCS (off-cloud test)
"""
import base64
import collections
import datetime
import json
import os
import socket
import time
import urllib.error
import urllib.request
from urllib.parse import urlencode

import activecampaign

# --- Force IPv4 (playbook 3.1) ----------------------------------------------
# Cloud Run has no IPv6 egress route but Windsor and ActiveCampaign both publish AAAA records,
# and `socket.create_connection` applies the FULL timeout to each address in turn — so every
# request burns its whole connect timeout on v6 before falling back. On honeytribe this turned a
# 38-page crawl into a 76-minute job. Filtering to A records makes each call sub-second.
if os.environ.get("FORCE_IPV4", "1") == "1":
    _real_getaddrinfo = socket.getaddrinfo

    def _ipv4_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):  # noqa: A002
        return _real_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)

    socket.getaddrinfo = _ipv4_getaddrinfo

# --- Per-client derivation (change only CLIENT) ------------------------------
PROJECT = "agora-data-driven"
CLIENT = "RHE"
# GCS buckets, Cloud Run services/jobs and service-account ids are LOWERCASE-ONLY, and this
# client's key is upper-case — so every derived CLOUD resource uses the lower-cased key while
# CLIENT keeps its casing for display. (The first standup failed on
# "Invalid bucket name: 'agora-data-driven-RHE-dash'".) Keep this in step with deploy_RHE.ps1.
KEY = CLIENT.lower()
BUCKET = os.environ.get("GCS_BUCKET", "agora-data-driven-%s-dash" % KEY)
DATA_OBJECT = os.environ.get("DATA_OBJECT", "%s.json" % KEY)

_HERE = os.path.dirname(os.path.abspath(__file__))

# --- Windsor (Meta) ----------------------------------------------------------
WINDSOR_URL = "https://connectors.windsor.ai/all"
WINDSOR_SECRET = "%s-windsor-key" % KEY
WINDSOR_ACCOUNTS = [a.strip() for a in os.environ.get(
    "WINDSOR_ACCOUNTS",
    "facebook__291824415053555,facebook__744718258097253,facebook__819110256113106"
).split(",") if a.strip()]

# Friendly labels for the account toggle. Windsor's own `account_name` is authoritative and
# overrides these at runtime; this is only the fallback for an account that returns no rows.
ACCOUNT_LABELS = {
    "facebook__291824415053555": "Stuart Baker",
    "facebook__744718258097253": "RHE",
    "facebook__819110256113106": "Super Cashflow Development",
}

DEEP_PRESET = os.environ.get("WINDSOR_DEEP_PRESET", "last_1095d")
UNIQUE_PRESET = os.environ.get("WINDSOR_UNIQUE_PRESET", "last_365d")
BREAKDOWN_PRESET = os.environ.get("WINDSOR_BREAKDOWN_PRESET", "last_365d")

# The client's own connector field list MINUS the two unique_* fields (which cap the window at
# 365d) — those come back in a second, shallower pull and are joined on.
DEEP_FIELDS = ["account_name", "actions_lead", "actions_like", "actions_post_reaction",
               "ad_created_time", "adset_bid_strategy", "adset_name", "campaign", "clicks",
               "creative_id", "datasource", "date", "frequency", "impressions", "link_clicks",
               "name", "reach", "spend", "title",
               "video_thruplay_watched_actions_video_view"]

# --- Creative gallery ---------------------------------------------------------
# `creative_id` rides along on the DEEP pull for free — measured: 7,105 rows and identical spend
# with and without it. That matters, because it lets the gallery aggregate over whatever date
# range / campaign / ad-search the user has selected instead of being a static side panel.
#
# The creative's TEXT and IMAGE come from a second, small pull keyed by creative_id. Field names
# were probed against these accounts (2026-07-28) — Windsor's `creative_*` family is EMPTY here,
# the populated ones are the bare names:
#     creative_id 100% · thumbnail_url 100% (fetchable) · image_url 30% · body 100%
#     title 85% (unreliable — often the display link, e.g. "fb.me") · instagram_permalink_url 87%
#   NOT populated: creative_title, creative_body, creative_thumbnail_url, creative_link_url,
#                  link_url, permalink_url, object_story_id, video_id, preview_shareable_link
CREATIVE_FIELDS = ["creative_id", "name", "title", "body", "thumbnail_url", "image_url",
                   "instagram_permalink_url", "date", "impressions"]
CREATIVE_PRESET = os.environ.get("WINDSOR_CREATIVE_PRESET", "last_365d")
# Meta's CDN links expire once an ad stops, so the top creatives by impressions get their image
# copied into our own bucket. Bounded because it is a download per creative.
CREATIVE_CACHE_MAX = int(os.environ.get("CREATIVE_CACHE_MAX", "60"))
CREATIVE_BODY_MAX = 1200
UNIQUE_FIELDS = ["date", "campaign", "name", "unique_actions_lead",
                 "unique_actions_link_click", "unique_actions_post_reaction"]

# Each breakdown is (slug, dimension-field, extra-fields). Leads are requested everywhere but
# only age/gender actually returns them — see the module docstring.
BREAKDOWNS = [
    ("age_gender", ["age", "gender"], True),
    ("region", ["region"], False),
    ("platform", ["publisher_platform"], True),
    ("position", ["platform_position"], True),
    ("device", ["impression_device"], True),
]
BD_METRICS = ["spend", "impressions", "clicks", "link_clicks", "reach"]
# Every breakdown row carries its ACCOUNT. Without it the demographics tab silently blends all
# three brands while every other tab is scoped to one — roughly double the spend and leads, which
# is the kind of inconsistency nobody notices until a client does.
BD_ACCOUNT_FIELD = "account_name"


def _num(x):
    try:
        return 0.0 if x is None or x == "" else float(x)
    except (TypeError, ValueError):
        return 0.0


def _secret(env_name, secret_id):
    v = os.environ.get(env_name, "").strip()
    if v:
        return v
    from google.cloud import secretmanager
    sm = secretmanager.SecretManagerServiceClient()
    name = "projects/%s/secrets/%s/versions/latest" % (PROJECT, secret_id)
    return sm.access_secret_version(name={"name": name}).payload.data.decode("utf-8").strip()


def _windsor(api_key, fields, preset, accounts=None):
    q = urlencode({"api_key": api_key, "date_preset": preset, "fields": ",".join(fields),
                   "select_accounts": ",".join(accounts or WINDSOR_ACCOUNTS)})
    req = urllib.request.Request(WINDSOR_URL + "?" + q, headers={"User-Agent": "agora-rhe/1.0"})
    with urllib.request.urlopen(req, timeout=300) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    return (payload["data"] if isinstance(payload, dict) and "data" in payload else payload) or []


# --- Meta --------------------------------------------------------------------
def fetch_meta(api_key):
    """Per-ad/day rows across all three accounts, deep history + the unique-count join.

    Two pulls because the `unique_actions_*` fields cap the window at 365 days while everything
    else reaches 1095. Keyed by (date, campaign, ad) for the join — adset is not in the unique
    pull's field list, so it is not part of the key.
    """
    deep = _windsor(api_key, DEEP_FIELDS, DEEP_PRESET)
    print("  meta: %d deep rows @ %s" % (len(deep), DEEP_PRESET))

    uniq = {}
    try:
        for r in _windsor(api_key, UNIQUE_FIELDS, UNIQUE_PRESET):
            d = str(r.get("date") or "")[:10]
            if not d:
                continue
            uniq[(d, r.get("campaign") or "", r.get("name") or "")] = {
                "ulead": int(_num(r.get("unique_actions_lead"))),
                "ulclk": int(_num(r.get("unique_actions_link_click"))),
                "ureact": int(_num(r.get("unique_actions_post_reaction"))),
            }
        print("  meta: %d unique-count rows @ %s" % (len(uniq), UNIQUE_PRESET))
    except Exception as e:  # noqa: BLE001 — the deep pull alone is still a usable dashboard
        print("  meta: unique-count pull skipped (%s)" % str(e)[:140])

    # Key on the FULL Meta hierarchy (date x account x campaign x adset x ad) and SUM on
    # collision. Windsor still returns ~60 rows that share even that key (they differ only on a
    # descriptive field such as `title`), and overwriting them silently dropped $321 of spend in
    # the first build — the audit caught it against the two-field lifetime total. Summing makes
    # the reconciliation exact regardless of which dimension Windsor happens to split on.
    seen = {}
    for r in deep:
        d = str(r.get("date") or "")[:10]
        if not d:
            continue
        camp = (r.get("campaign") or "").strip()
        ad = (r.get("name") or "").strip()
        acct = (r.get("account_name") or "").strip() or "Unknown"
        adset = (r.get("adset_name") or "").strip()
        key = (d, acct, camp, adset, ad)
        imps = int(_num(r.get("impressions")))
        cur = seen.get(key)
        if cur is None:
            cur = seen[key] = {
                "d": d, "acct": acct, "camp": camp, "adset": adset, "ad": ad,
                "cid": str(r.get("creative_id") or "") or None,
                "title": (r.get("title") or "").strip(),
                "created": str(r.get("ad_created_time") or "")[:10],
                "bid": (r.get("adset_bid_strategy") or "").strip(),
                "spend": 0.0, "imps": 0, "clicks": 0, "lclk": 0, "reach": 0,
                "leads": 0, "thru": 0, "react": 0, "ulead": 0, "ulclk": 0,
                "_fw": 0.0,
            }
        cur["spend"] += _num(r.get("spend"))
        cur["imps"] += imps
        cur["clicks"] += int(_num(r.get("clicks")))
        cur["lclk"] += int(_num(r.get("link_clicks")))
        cur["reach"] += int(_num(r.get("reach")))
        cur["leads"] += int(_num(r.get("actions_lead")))
        cur["thru"] += int(_num(r.get("video_thruplay_watched_actions_video_view")))
        cur["react"] += (int(_num(r.get("actions_post_reaction")))
                         + int(_num(r.get("actions_like"))))
        # Reach is NOT additive across days, so frequency is carried as Meta's own per-row value
        # (impression-weighted when rows merge) and the dashboard AVERAGES it rather than
        # dividing summed impressions by summed reach, which understates it.
        cur["_fw"] += _num(r.get("frequency")) * imps

    # unique_* counts are not additive, so they cannot be split across the adset rows of one ad.
    # Each (date, campaign, ad) group's value is attached to its LARGEST row by impressions and
    # zeroed on the rest: any aggregate that spans the whole group is then exactly right.
    groups = {}
    for key, v in seen.items():
        g = (v["d"], v["camp"], v["ad"])
        best = groups.get(g)
        if best is None or v["imps"] > seen[best]["imps"]:
            groups[g] = key
    for g, key in groups.items():
        u = uniq.get(g)
        if u:
            seen[key]["ulead"] = u["ulead"]
            seen[key]["ulclk"] = u["ulclk"]

    for v in seen.values():
        v["spend"] = round(v["spend"], 2)
        v["freq"] = round(v.pop("_fw") / v["imps"], 4) if v["imps"] else 0.0
    rows = sorted(seen.values(), key=lambda x: (x["d"], x["acct"], x["camp"], x["ad"]))
    dates = [r["d"] for r in rows]
    return {
        "rows": rows,
        "range": [min(dates), max(dates)] if dates else [None, None],
        "accounts": sorted({r["acct"] for r in rows if r["acct"]}),
        "campaigns": sorted({r["camp"] for r in rows if r["camp"]}),
        "adsets": sorted({r["adset"] for r in rows if r["adset"]}),
        "ads": sorted({r["ad"] for r in rows if r["ad"]}),
        "unique_from": UNIQUE_PRESET,
    }


def merge_history(meta, previous):
    """Carry forward Meta rows older than this pull's window (playbook 3.4).

    Keyed by (date, account, campaign, ad): the fresh pull is authoritative for every day it
    covers, so restatements land correctly, and anything older is kept verbatim.
    """
    old = ((previous or {}).get("meta") or {}).get("rows") or []
    fresh_from = meta["range"][0]
    if not old or not fresh_from:
        return meta
    def k(r):
        return (r.get("d"), r.get("acct"), r.get("camp"), r.get("adset", ""), r.get("ad"))

    keyed = {k(r): r for r in old if r.get("d") < fresh_from}
    if not keyed:
        return meta
    for r in meta["rows"]:
        keyed[k(r)] = r
    rows = sorted(keyed.values(), key=lambda x: (x["d"], x.get("acct", ""), x["camp"], x["ad"]))
    dates = [r["d"] for r in rows]
    print("  meta history: %d fresh + %d carried forward = %d rows"
          % (len(meta["rows"]), len(rows) - len(meta["rows"]), len(rows)))
    out = dict(meta)
    out.update({
        "rows": rows, "range": [min(dates), max(dates)], "fresh_from": fresh_from,
        "accounts": sorted({r["acct"] for r in rows if r.get("acct")}),
        "campaigns": sorted({r["camp"] for r in rows if r["camp"]}),
        "adsets": sorted({r.get("adset") for r in rows if r.get("adset")}),
        "ads": sorted({r["ad"] for r in rows if r["ad"]}),
    })
    return out


def fetch_breakdowns(api_key):
    """Every Meta breakdown the demographics tab needs, each a separate pull, each best-effort.

    The build brief called this a blocker needing a second Windsor destination task. It is not —
    all five work on the existing key today. But `region` returns leads identically 0, so it is
    flagged `has_leads: false` and the dashboard hides lead/CPL cuts for it.
    """
    out = {"enabled": False, "window": BREAKDOWN_PRESET, "note": ""}
    problems = []
    for slug, dims, want_leads in BREAKDOWNS:
        fields = ([BD_ACCOUNT_FIELD, "date"] + dims + BD_METRICS
                  + (["actions_lead"] if want_leads else []))
        try:
            rows = _windsor(api_key, fields, BREAKDOWN_PRESET)
            agg = collections.defaultdict(lambda: [0.0, 0, 0, 0, 0, 0])
            for r in rows:
                d = str(r.get("date") or "")[:10]
                if not d:
                    continue
                # Region is rolled to MONTH — 8 states x 365 days is payload for nothing, and the
                # geo read is a "where are we buying" question, not a daily one.
                bucket = d[:7] if slug == "region" else d
                acct = (r.get(BD_ACCOUNT_FIELD) or "").strip() or "Unknown"
                k = tuple([bucket, acct] + [str(r.get(x) or "Unknown") for x in dims])
                a = agg[k]
                a[0] += _num(r.get("spend"))
                a[1] += int(_num(r.get("impressions")))
                a[2] += int(_num(r.get("clicks")))
                a[3] += int(_num(r.get("link_clicks")))
                a[4] += int(_num(r.get("reach")))
                a[5] += int(_num(r.get("actions_lead")))
            recs = []
            for k, v in sorted(agg.items()):
                rec = {"d": k[0], "acct": k[1], "spend": round(v[0], 2), "imps": v[1],
                       "clicks": v[2], "lclk": v[3], "reach": v[4], "leads": v[5]}
                for i, dim in enumerate(dims):
                    rec[dim] = k[i + 2]
                recs.append(rec)
            total_leads = sum(r["leads"] for r in recs)
            out[slug] = {"rows": recs, "has_leads": total_leads > 0}
            print("  breakdown %-11s %5d rows  leads=%d%s"
                  % (slug, len(recs), total_leads,
                     "  (no leads on this breakdown)" if not total_leads else ""))
        except Exception as e:  # noqa: BLE001 — one dead breakdown must not sink the export
            out[slug] = {"rows": [], "has_leads": False}
            problems.append("%s: %s" % (slug, str(e)[:100]))
            print("  breakdown %-11s SKIP %s" % (slug, str(e)[:120]))
    out["enabled"] = any((out.get(s) or {}).get("rows") for s, _d, _w in BREAKDOWNS)
    out["note"] = "; ".join(problems)
    return out


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
    """Meta's `title` is often the display LINK ("fb.me", "roominghouse.expert"), not a headline.
    Those are useless as a card heading, so fall back to the ad name when the title looks like a
    domain or is absurdly short."""
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
    """One row per creative_id: the ad's text and image, so the gallery can show the ACTUAL ads.

    Metrics deliberately are NOT stored here — the main `meta.rows` already carry `cid`, so the
    dashboard aggregates delivery per creative over whatever period/campaign/ad-search is
    selected. Keeping metrics in one place is what stops the gallery disagreeing with the tiles
    above it.

    Per creative the NEWEST non-empty value wins for each field, because Meta's CDN links rotate
    and a stale row would otherwise pin an expired URL.
    """
    try:
        rows = _windsor(api_key, CREATIVE_FIELDS, CREATIVE_PRESET)
    except Exception as e:  # noqa: BLE001 — the gallery is a nice-to-have, never sink the export
        print("  creatives: pull skipped (%s)" % str(e)[:140])
        return {"enabled": False, "items": [], "error": str(e)[:200], "window": CREATIVE_PRESET}

    best = {}
    for r in sorted(rows, key=lambda x: str(x.get("date") or "")):   # oldest first, newest wins
        cid = str(r.get("creative_id") or "").strip()
        if not cid:
            continue
        cur = best.setdefault(cid, {"cid": cid, "name": "", "title": "", "body": "",
                                    "thumb": "", "image": "", "link": ""})
        for src, dst in (("name", "name"), ("title", "title"), ("body", "body"),
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
            "body": body[:CREATIVE_BODY_MAX] + ("…" if len(body) > CREATIVE_BODY_MAX else ""),
            # keep the live CDN url as a fallback for creatives we have not cached
            "thumb": _usable_image(c["image"], c["thumb"]),
            "link": c["link"],
        })
    print("  creatives: %d distinct (window %s)" % (len(items), CREATIVE_PRESET))
    return {"enabled": bool(items), "items": items, "error": "", "window": CREATIVE_PRESET}


def cache_creative_images(creatives, meta_rows):
    """Copy the busiest creatives' images into our OWN bucket, because Meta's CDN links expire
    the moment an ad stops running — after which the gallery would be a wall of grey boxes.

    Only the top `CREATIVE_CACHE_MAX` by lifetime impressions are fetched (one download each), and
    anything already cached is left alone: the copy we took while the link was live is the good
    one. Marks each item `cached: true` so the dashboard knows to use /creative-img/<cid>.
    Entirely best-effort — a miss just falls back to the live URL, then to a branded text tile.
    """
    items = creatives.get("items") or []
    if not items:
        return creatives
    imps = collections.Counter()
    for r in meta_rows:
        if r.get("cid"):
            imps[r["cid"]] += r["imps"]
    order = sorted(items, key=lambda c: -imps.get(c["cid"], 0))[:CREATIVE_CACHE_MAX]

    local_dir = os.environ.get("RHE_CREATIVE_LOCAL_DIR")
    bucket = None
    have = set()
    if local_dir:
        os.makedirs(local_dir, exist_ok=True)
        have = {n for n in os.listdir(local_dir)}
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
        except Exception as e:  # noqa: BLE001 — an expired link simply 403s; skip it
            print("  creative %s: cache skip (%s)" % (cid, str(e)[:80]))
    for c in items:
        c.setdefault("cached", c["cid"] in have)
    print("  creatives: %d newly cached, %d of %d have a permanent image"
          % (fetched, sum(1 for c in items if c.get("cached")), len(items)))
    return creatives


def load_previous():
    """The JSON this job published last time, or None. Feeds history merge + AC watermarks."""
    local_out = os.environ.get("RHE_LOCAL_OUT")
    try:
        if local_out:
            if not os.path.exists(local_out):
                return None
            with open(local_out, "rb") as fh:
                return json.loads(fh.read().decode("utf-8"))
        from google.cloud import storage
        blob = storage.Client(project=PROJECT).bucket(BUCKET).blob(DATA_OBJECT)
        if not blob.exists():
            return None
        return json.loads(blob.download_as_bytes().decode("utf-8"))
    except Exception as e:  # noqa: BLE001 — no previous publication is a normal first run
        print("  previous publication unreadable (%s) — starting fresh" % str(e)[:120])
        return None


def _b64(path, mime):
    try:
        with open(path, "rb") as fh:
            return "data:%s;base64,%s" % (mime, base64.b64encode(fh.read()).decode())
    except OSError:
        return None


def build():
    started = time.time()
    previous = load_previous()

    wkey = _secret("WINDSOR_API_KEY", WINDSOR_SECRET)
    print("[%s] Meta via Windsor (%d accounts, deep=%s unique=%s)"
          % (CLIENT, len(WINDSOR_ACCOUNTS), DEEP_PRESET, UNIQUE_PRESET))
    meta = merge_history(fetch_meta(wkey), previous)
    breakdowns = fetch_breakdowns(wkey)
    creatives = cache_creative_images(fetch_creatives(wkey), meta["rows"])

    print("[%s] ActiveCampaign" % CLIENT)
    email = activecampaign.fetch(previous=(previous or {}).get("email"))

    mthru = meta["range"][1]
    ethru = (email.get("daily") or [{}])[-1].get("d") if email.get("daily") else None
    through = max([x for x in (mthru, ethru) if x] or [None])

    return {
        "client": "Rooming House Expert",
        "tagline": "Turning underperforming property into cash-flow.",
        "location": "Victoria, Australia",
        "currency": "AUD",
        "generated_at": datetime.datetime.now(datetime.timezone.utc)
                                 .strftime("%Y-%m-%dT%H:%M:%SZ"),
        "build_seconds": round(time.time() - started, 1),
        "data_through": through,
        "source": {
            "meta": "Meta (Facebook) Ads via Windsor.ai — %d accounts" % len(WINDSOR_ACCOUNTS),
            "email": "ActiveCampaign REST v3 (%s)" % (email.get("account") or "not configured"),
            "mode": "live",
            "meta_through": mthru,
            "email_through": ethru,
        },
        "brand": {
            "mark": _b64(os.path.join(_HERE, "assets", "rhe-mark.png"), "image/png"),
            "agora_logo": _b64(os.path.join(_HERE, "assets", "agora.png"), "image/png"),
        },
        "meta": meta,
        "breakdowns": breakdowns,
        "creatives": creatives,
        "email": email,
    }


def main():
    data = build()
    body = json.dumps(data, separators=(",", ":"))

    local_out = os.environ.get("RHE_LOCAL_OUT")
    if local_out:
        os.makedirs(os.path.dirname(local_out) or ".", exist_ok=True)
        with open(local_out, "wb") as fh:            # bytes, never text mode (playbook 3.7)
            fh.write(body.encode("utf-8"))
        print("[%s] wrote %s — LOCAL" % (CLIENT, local_out))
    else:
        from google.cloud import storage
        blob = storage.Client(project=PROJECT).bucket(BUCKET).blob(DATA_OBJECT)
        blob.cache_control = "no-store"
        blob.upload_from_string(body, content_type="application/json")
        print("[%s] uploaded gs://%s/%s" % (CLIENT, BUCKET, DATA_OBJECT))

    m, e = data["meta"], data["email"]
    print("   meta %d rows (%s .. %s) · accounts %s"
          % (len(m["rows"]), m["range"][0], m["range"][1], m["accounts"]))
    et = e.get("totals") or {}
    # NOTE: there is no per-day CLICK figure — clicks are campaign-level only (see EVENT_KINDS in
    # activecampaign.py), which is why the broadcast line below is printed separately.
    print("   email contacts %d · sends %d · opens %d · apple-mpp %d · days %d"
          % (len(e.get("contacts") or []), et.get("sends", 0), et.get("opens", 0),
             et.get("mpp", 0), len(e.get("daily") or [])))
    csent, copens = et.get("campaign_sent", 0), et.get("campaign_opens", 0)
    print("   broadcasts %d · recipients %d · openers %d · open rate %s"
          % (et.get("campaigns_sent", 0), csent, copens,
             ("%.1f%%" % (100.0 * copens / csent)) if csent else "n/a"))
    print("   %d KB · %.1fs" % (len(body) // 1024, data["build_seconds"]))


if __name__ == "__main__":
    main()
