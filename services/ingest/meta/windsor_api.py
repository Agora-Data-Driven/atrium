r"""Windsor.ai request + transform layer for the SHARED Meta ingest loader.

WHERE THIS LIVES AND WHY
    services/ingest/meta/windsor_api.py -- the INGEST layer, not a client.

    🔴 This module is NOT vendored into client jobs, and must never be. The canonical
    architecture is ONE shared loader writing the raw layer, and per-client SQL views reading
    their slice out of it:

        Meta -> Windsor API -> windsor-meta-ingest (this dir) -> raw_windsor.perf_meta
                                                                      -> client view (WHERE client_slug=...)
                                                                            -> client export job

    It was briefly written as clients/_standard/job/windsor.py -- a per-client module vendored
    into each job. That was the wrong place: it would have entrenched the very API-LIVE pattern
    the estate is migrating OFF. Moved here 2026-07-30. If you find yourself importing this from
    a client job, stop -- the client should be reading a view.

STDLIB ONLY : urllib + json, no `requests`, matching every other loader here.

WHY IT EXISTS
    Four clients (RHE, riverdance, honeytribe, MeloYelo) each grew their own copy of this pull,
    because meta_loader.py was left a stub. Every one of them independently rediscovered the same
    Windsor behaviours, and the estate paid for it: one copy overwrote rows on key collision and
    silently dropped **$321** of spend, which only an independent audit caught. Consolidating the
    traps here fixes each one once, for every client.

THE CANONICAL ROW
    One row shape regardless of account. `transform()` in meta_loader.py builds the typed
    BigQuery row on top of these names, and the standard dashboards
    (clients/_standard/dash/dashboard-{leads,sales}.html) read the same vocabulary -- which is
    what lets a client view feed a standard dashboard with no mapping layer.

        date  account  campaign  adset  ad  cid
        impressions  reach  clicks  link_clicks  spend  frequency
        leads  qualified            (lead-gen clients)
        atc  orders  revenue        (e-commerce clients)

    Anything else Windsor returns is kept under row["extra"], never dropped silently.

WINDSOR BEHAVIOURS THAT ARE THE API'S, NOT OURS -- do not "fix" these
    * ⚠️ CORRECTED 2026-07-30: this module used to claim the `all` endpoint takes a
      **date_preset ONLY** and that `date_from`/`date_to` return 400. **They do not** -- probed
      live against the agora account, a `date_from`/`date_to` request returns exactly the days
      asked for, which is what lets meta_loader.py chunk history properly instead of
      ACCUMULATING it (see merge_history, now needed only by the LEGACY client jobs).
      The real trap is subtler: send a preset AND a range together and the **preset silently
      wins** -- a 3-day range came back with last_7d's rows and no error at all.
    * The window cap is caused by SPECIFIC FIELDS, not the field count. On RHE the 400 at
      `last_730d` came entirely from the two `unique_actions_*` fields; dropping them let the same
      18-field pull reach `last_1095d`. Hence split_pull().
    * A field can be returned and always empty. `unique_actions_lead` is present but identically 0
      account-wide while `unique_actions_link_click` works. Render that as **n/a**, never 0 -- 0
      reads as "we got none", which is a lie.
    * Breakdowns are SEPARATE pulls. Meta will not return age/gender/region alongside per-ad/day
      rows, and it REJECTS revenue fields on a breakdown query.
    * Reach is NOT additive across days. Summing it and dividing impressions by it understates
      frequency -- carry Windsor's own per-row `frequency`, impression-weighted.
"""

import json
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

WINDSOR_URL = "https://connectors.windsor.ai/all"
DEFAULT_TIMEOUT = int(os.environ.get("WINDSOR_TIMEOUT", "300"))


# =============================================================================================
# 1. FORCE_IPV4 -- the single most expensive trap in the estate. Install it before any request.
# =============================================================================================
def force_ipv4(enabled=None):
    """Pin getaddrinfo to IPv4. Call once at import time in the job.

    Cloud Run has **no IPv6 egress route**, but Windsor (and most SaaS APIs) publish AAAA records.
    `socket.create_connection` walks getaddrinfo in order applying the FULL timeout to EACH
    address, so every request burns its entire connect timeout on the v6 address before falling
    back. Measured on honeytribe: 120 s *per call*, turning a 38-page backfill into a 76-minute
    job that blew the task timeout -- while the same backfill ran in 133 s with A-records only.

    Symptom to recognise: requests take *exactly* your timeout value.
    """
    if enabled is None:
        enabled = os.environ.get("FORCE_IPV4", "1") == "1"
    if not enabled or getattr(socket, "_agora_ipv4_pinned", False):
        return False
    _real = socket.getaddrinfo

    def _v4(host, port, family=0, type=0, proto=0, flags=0):  # noqa: A002
        return _real(host, port, socket.AF_INET, type, proto, flags)

    socket.getaddrinfo = _v4
    socket._agora_ipv4_pinned = True  # idempotent: never wrap twice
    return True


# =============================================================================================
# 2. THE REQUEST. One builder, so select_accounts and retry behaviour cannot drift per client.
# =============================================================================================
RETRY_STATUS = (429, 500, 502, 503, 504)


def get(api_key, fields, preset, accounts=None, timeout=None, retries=4, url=WINDSOR_URL):
    """One Windsor `all` request. Returns a list of raw row dicts.

    `accounts` is a list of Windsor account ids (`facebook__<adAccountId>` -- note the DOUBLE
    underscore). It is sent as a comma-separated **select_accounts**, so ONE call returns every
    listed account at once; RHE has pulled three Facebook accounts this way since 2026-07.
    To tell them apart you MUST include "account_name" in `fields` -- there is no other way.

    Retries 429/5xx with exponential backoff, honouring Retry-After. A long crawl earns you a
    503 eventually, and on RHE a bare crawl swallowed one and published a year of zeros.
    """
    if timeout is None:
        timeout = DEFAULT_TIMEOUT
    q = {
        "api_key": api_key,
        "date_preset": preset,
        "fields": ",".join(fields),
    }
    if accounts:
        q["select_accounts"] = ",".join(accounts)
    full = url + "?" + urllib.parse.urlencode(q)
    delay = 2.0
    last = None
    for attempt in range(retries):
        req = urllib.request.Request(full, headers={"User-Agent": "agora-windsor/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            if isinstance(payload, dict) and "data" in payload:
                return payload["data"] or []
            return payload or []
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", "replace")[:300]
            except Exception:  # noqa: BLE001
                pass
            last = "HTTP %s %s" % (e.code, body)
            if e.code not in RETRY_STATUS:
                # A 400 here is almost always the field list or the window -- not transient.
                raise WindsorError(last)
            wait = delay
            ra = e.headers.get("Retry-After") if e.headers else None
            if ra:
                try:
                    wait = max(wait, float(ra))
                except (TypeError, ValueError):
                    pass
            time.sleep(wait)
            delay *= 2
        except Exception as e:  # noqa: BLE001 -- socket/timeout/JSON
            last = str(e)
            time.sleep(delay)
            delay *= 2
    raise WindsorError("gave up after %d attempts: %s" % (retries, last))


class WindsorError(RuntimeError):
    pass


# =============================================================================================
# 3. FIELD SETS. The window cap is caused by specific FIELDS, so they are grouped by that fact.
# =============================================================================================
# Safe at a long preset (RHE reached last_1095d with this shape).
DEEP_FIELDS = [
    "account_name", "date", "campaign", "adset_name", "name", "title",
    "impressions", "reach", "clicks", "link_clicks", "spend", "frequency",
    "actions_lead", "ctr", "cpp",
]
# These two are what cap the window at 365d. Pull them SEPARATELY and join.
UNIQUE_FIELDS = [
    "account_name", "date", "campaign", "name",
    "unique_actions_lead", "unique_actions_link_click",
]
# E-commerce adds purchase/value; Meta rejects these on a breakdown query.
COMMERCE_FIELDS = ["actions_purchase", "action_values_purchase", "actions_add_to_cart"]

# Windsor label -> canonical name. Anything unmapped is preserved under row["extra"].
CANON = {
    "account_name": "account",
    "campaign": "campaign",
    "adset_name": "adset",
    "name": "ad",
    "date": "date",
    "impressions": "impressions",
    "reach": "reach",
    "clicks": "clicks",
    "link_clicks": "link_clicks",
    "spend": "spend",
    "frequency": "frequency",
    "actions_lead": "leads",
    "actions_purchase": "orders",
    "action_values_purchase": "revenue",
    "actions_add_to_cart": "atc",
    "creative_id": "cid",
}
NUMERIC = ("impressions", "reach", "clicks", "link_clicks", "spend", "frequency",
           "leads", "qualified", "orders", "revenue", "atc")
# The natural key of a Meta per-ad/day row -- the FULL hierarchy.
ROW_KEY = ("date", "account", "campaign", "adset", "ad")


def _num(v):
    if v in (None, "", "null"):
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def canonicalise(raw_rows):
    """Map Windsor labels to the canonical row shape. Never drops a field."""
    out = []
    for r in raw_rows:
        row = {"extra": {}}
        for k, v in r.items():
            key = CANON.get(k)
            if key:
                row[key] = v
            else:
                row["extra"][k] = v
        for k in NUMERIC:
            if k in row:
                row[k] = _num(row[k])
        row["date"] = str(row.get("date") or "")[:10]
        row["account"] = (row.get("account") or "").strip() or "Unknown"
        for k in ("campaign", "adset", "ad"):
            row[k] = (row.get(k) or "").strip()
        out.append(row)
    return out


# =============================================================================================
# 4. SUM ON KEY COLLISION -- NEVER OVERWRITE. This one cost $321 before it was caught.
# =============================================================================================
def sum_on_collision(rows, key=ROW_KEY):
    """Collapse rows sharing `key` by SUMMING, with frequency impression-weighted.

    🔴 Windsor returns rows that share even (date, account, campaign, adset, ad) -- they differ
    only on a descriptive field such as `title`. On RHE there were ~60 such groups, and a
    dict-assignment merge (`by[k] = row`) silently discarded **$321** of spend. Only an audit
    against the two-field lifetime total found it. Always sum; never assign.

    Frequency is a RATE, so it cannot be summed: it is carried impression-weighted, because reach
    is not additive across days and impressions ÷ summed-reach understates it.
    """
    by = {}
    order = []
    for r in rows:
        k = tuple(r.get(f, "") for f in key)
        cur = by.get(k)
        if cur is None:
            cur = dict(r)
            cur["_freq_num"] = _num(r.get("frequency")) * _num(r.get("impressions"))
            cur["_freq_den"] = _num(r.get("impressions"))
            by[k] = cur
            order.append(k)
            continue
        for f in NUMERIC:
            if f == "frequency":
                continue
            if f in r:
                cur[f] = _num(cur.get(f)) + _num(r.get(f))
        cur["_freq_num"] += _num(r.get("frequency")) * _num(r.get("impressions"))
        cur["_freq_den"] += _num(r.get("impressions"))
        for ek, ev in (r.get("extra") or {}).items():
            cur.setdefault("extra", {}).setdefault(ek, ev)
    out = []
    for k in order:
        row = by[k]
        den = row.pop("_freq_den", 0.0)
        num = row.pop("_freq_num", 0.0)
        row["frequency"] = (num / den) if den else 0.0
        out.append(row)
    return out


# =============================================================================================
# 5. THE SPLIT PULL -- deep history without the fields that cap the window.
# =============================================================================================
def split_pull(api_key, accounts, deep_preset="last_1095d", unique_preset="last_365d",
               deep_fields=None, unique_fields=None, commerce=False, log=print):
    """Deep pull (no unique_* fields) joined to a shallow unique pull.

    Unique counts are NOT additive, so each group's value is attached to its LARGEST row by
    impressions and zeroed on the rest -- any aggregate spanning the group is then exact.
    Falls back to a single 365d pull if the deep preset 400s, so a new account still works.
    """
    fields = list(deep_fields or DEEP_FIELDS)
    if commerce:
        fields += [f for f in COMMERCE_FIELDS if f not in fields]
    try:
        deep = canonicalise(get(api_key, fields, deep_preset, accounts))
    except WindsorError as e:
        log("[windsor] deep preset %s failed (%s) -- falling back to last_365d" % (deep_preset, e))
        deep = canonicalise(get(api_key, fields, "last_365d", accounts))
    deep = sum_on_collision(deep)

    try:
        uniq = canonicalise(get(api_key, list(unique_fields or UNIQUE_FIELDS), unique_preset, accounts))
    except WindsorError as e:
        log("[windsor] unique pull skipped (%s)" % e)
        return deep

    # Join on the coarser key the unique pull actually carries.
    ukey = ("date", "account", "campaign", "ad")
    groups = {}
    for r in deep:
        groups.setdefault(tuple(r.get(f, "") for f in ukey), []).append(r)
    for u in uniq:
        k = tuple(u.get(f, "") for f in ukey)
        g = groups.get(k)
        if not g:
            continue
        biggest = max(g, key=lambda x: _num(x.get("impressions")))
        for name in ("unique_actions_lead", "unique_actions_link_click"):
            val = _num((u.get("extra") or {}).get(name))
            for row in g:
                row.setdefault("extra", {})[name] = val if row is biggest else 0.0
    return deep


# =============================================================================================
# 6. HISTORY ACCUMULATION -- because the API will not give you the whole timeline in one call.
# =============================================================================================
def merge_history(prev_rows, fresh_rows, key=ROW_KEY):
    """Carry older rows forward. The FRESH pull is authoritative for every day it covers.

    Because the endpoint caps the window, each run fetches what it can and merges older rows
    forward from the previous publication. Keying on the full hierarchy means restatements land
    correctly and history grows in the bucket instead of being truncated at the cap.
    """
    fresh_days = {r.get("date") for r in fresh_rows if r.get("date")}
    out = list(fresh_rows)
    seen = {tuple(r.get(f, "") for f in key) for r in fresh_rows}
    for r in prev_rows or []:
        d = r.get("date")
        if d in fresh_days:
            continue                      # the fresh pull owns this day entirely
        k = tuple(r.get(f, "") for f in key)
        if k in seen:
            continue
        out.append(r)
        seen.add(k)
    out.sort(key=lambda r: (r.get("date") or "", r.get("campaign") or "", r.get("ad") or ""))
    return out


# =============================================================================================
# 7. BREAKDOWNS -- separate pulls, and not all of them carry leads.
# =============================================================================================
BREAKDOWN_DIMS = {
    "age": "age",
    "gender": "gender",
    "platform": "publisher_platform",
    "position": "platform_position",
    "device": "impression_device",
    "region": "region",
}


def fetch_breakdowns(api_key, accounts, dims=None, preset="last_365d", log=print):
    """One pull per dimension. Returns {dim: {"rows": [...], "has_leads": bool}}.

    🔴 A breakdown that returns leads identically 0 must be FLAGGED, not rendered as 0. On RHE the
    region breakdown returns no leads on any field combination, so a "CPL by state" map is simply
    not buildable however reasonable the ask -- while age x gender does carry leads and reconciles
    exactly to the main pull. The dashboard reads has_leads to hide the cuts it cannot support.
    """
    out = {}
    for name in (dims or list(BREAKDOWN_DIMS)):
        field = BREAKDOWN_DIMS.get(name, name)
        fields = ["account_name", "date", field, "impressions", "clicks", "link_clicks",
                  "spend", "actions_lead"]
        try:
            rows = canonicalise(get(api_key, fields, preset, accounts))
        except WindsorError as e:
            log("[windsor] breakdown %s failed: %s" % (name, e))
            out[name] = {"rows": [], "has_leads": False, "error": str(e)}
            continue
        for r in rows:
            r["seg"] = str((r.get("extra") or {}).get(field) or "unknown")
        has_leads = any(_num(r.get("leads")) > 0 for r in rows)
        if rows and not has_leads:
            log("[windsor] breakdown %s carries NO leads -- lead/CPL cuts must render n/a" % name)
        out[name] = {"rows": rows, "has_leads": has_leads}
    return out


# =============================================================================================
# 8. THE BEST-EFFORT WRAPPER. Every source after the first must degrade, never sink the export.
# =============================================================================================
def fetch_media(api_key, accounts, prev_rows=None, commerce=False, breakdowns=True,
                deep_preset="last_1095d", log=print):
    """The whole Meta pull as one call. Returns the payload block the dashboards read.

    Shape: {enabled, error, rows[], accounts[], range[lo,hi], breakdowns{}, breakdown_has_leads{}}
    A failure returns enabled=False with the reason and NO rows -- the dashboard then renders its
    wired empty state and the rest of the export is unaffected.
    """
    if not api_key:
        return {"enabled": False, "error": "no Windsor API key configured", "rows": []}
    try:
        rows = split_pull(api_key, accounts, deep_preset=deep_preset, commerce=commerce, log=log)
    except Exception as e:  # noqa: BLE001 -- best-effort by contract
        log("[windsor] media pull FAILED: %s" % e)
        return {"enabled": False, "error": str(e), "rows": []}

    if prev_rows:
        before = len(rows)
        rows = merge_history(prev_rows, rows)
        log("[windsor] history merge: %d fresh + carried -> %d rows" % (before, len(rows)))

    days = sorted({r["date"] for r in rows if r.get("date")})
    block = {
        "enabled": True,
        "error": "",
        "rows": rows,
        "accounts": sorted({r["account"] for r in rows if r.get("account")}),
        "range": [days[0], days[-1]] if days else [None, None],
    }
    if breakdowns:
        bd = fetch_breakdowns(api_key, accounts, log=log)
        block["breakdowns"] = {k: v["rows"] for k, v in bd.items()}
        block["breakdown_has_leads"] = {k: v["has_leads"] for k, v in bd.items()}
    return block


def totals(rows):
    """Aggregate for an audit line. Rates are derived from TOTALS, never averaged per day."""
    t = {k: 0.0 for k in NUMERIC if k != "frequency"}
    fn = fd = 0.0
    for r in rows:
        for k in t:
            t[k] += _num(r.get(k))
        fn += _num(r.get("frequency")) * _num(r.get("impressions"))
        fd += _num(r.get("impressions"))
    t["frequency"] = (fn / fd) if fd else 0.0
    t["ctr"] = (t["link_clicks"] / t["impressions"] * 100) if t["impressions"] else 0.0
    t["cpm"] = (t["spend"] / t["impressions"] * 1000) if t["impressions"] else 0.0
    t["cpl"] = (t["spend"] / t["leads"]) if t["leads"] else 0.0
    t["roas"] = (t["revenue"] / t["spend"]) if t["spend"] else 0.0
    return t
