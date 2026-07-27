"""ActiveCampaign email-marketing pull for the Riverdance export job — Stage 2 (opt-in add-on).

Riverdance also runs email + marketing automation in **ActiveCampaign**. This module pulls that
account straight from the ActiveCampaign REST API (v3) on each export run and returns a compact
`activecampaign` block that rides along in the same private `riverdance.json` the Meta dashboard
already serves. It is a deliberate, opt-in deviation from the "Windsor is the only source" rule
(mirrors the Windsor-live path already used for Meta) — the client asked to see their email numbers
next to their ads, and ActiveCampaign is not a Windsor connector here.

Everything is **best-effort**: any failure (no key, network, disabled feature such as the CRM)
degrades to an empty/partial block with an `error` string and NEVER fails the whole export. The
API token is read from the env (mounted secret) or Secret Manager and is never logged or persisted.

Config / secrets:
  ACTIVECAMPAIGN_URL      — account API base, e.g. https://<account>.api-us1.com   (env; deploy sets it)
  ACTIVECAMPAIGN_API_KEY  — the Api-Token (Secret Manager `riverdance-activecampaign-key`, mounted as env)

Shape returned (matched BY NAME to dash/dashboard.html's DATA.activecampaign.*):
  { enabled, account, url, fetched, crm_enabled, error,
    totals{contacts,campaigns,campaigns_sent,lists,automations},
    campaigns[]  (SENT only, newest first — id,name,date,sent,opens,opens_total,clicks,
                  clicks_total,unsubs,bounces,forwards,replies,socialshares),
    lists[]      (id,name,subscribers,total  — top by subscribers),
    automations[](id,name,entered,exited,active) }
"""
import json
import os
import urllib.request
from urllib.parse import urlencode, urlparse

PROJECT = "agora-data-driven"
AC_SECRET = "riverdance-activecampaign-key"   # Secret Manager id (fallback when env not set)
_TIMEOUT = 60


def _num(x):
    try:
        return int(float(x)) if x is not None and x != "" else 0
    except (TypeError, ValueError):
        return 0


def _base_url():
    return (os.environ.get("ACTIVECAMPAIGN_URL", "") or "").strip().rstrip("/")


def _api_key():
    """AC Api-Token from env (mounted secret) or, as a fallback, Secret Manager directly."""
    k = (os.environ.get("ACTIVECAMPAIGN_API_KEY", "") or "").strip()
    if k:
        return k
    try:
        from google.cloud import secretmanager
        sm = secretmanager.SecretManagerServiceClient()
        name = "projects/%s/secrets/%s/versions/latest" % (PROJECT, AC_SECRET)
        return sm.access_secret_version(name={"name": name}).payload.data.decode("utf-8").strip()
    except Exception:  # noqa: BLE001 — no key available is a graceful "disabled", not a crash
        return ""


def _account_name(url):
    try:
        host = urlparse(url).hostname or ""
        return host.split(".", 1)[0] if host else ""
    except Exception:  # noqa: BLE001
        return ""


def _get(base, key, path, params=None):
    """One GET against /api/3/<path>. Returns the parsed JSON dict (raises on transport error)."""
    q = urlencode(params or {})
    sep = "&" if "?" in path else "?"
    full = "%s/api/3/%s%s%s" % (base, path.lstrip("/"), sep if q else "", q)
    req = urllib.request.Request(full, headers={"Api-Token": key, "User-Agent": "agora-riverdance/1.0"})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _meta_total(payload):
    try:
        return _num((payload.get("meta") or {}).get("total"))
    except Exception:  # noqa: BLE001
        return 0


def _iso_date(s):
    """'2026-07-10T14:30:00-05:00' -> '2026-07-10' (date part only; None -> '')."""
    if not s:
        return ""
    return str(s).split("T", 1)[0]


def _campaign_row(c):
    return {
        "id": c.get("id"),
        "name": (c.get("name") or "Untitled").strip(),
        "date": _iso_date(c.get("sdate") or c.get("ldate") or c.get("mdate")),
        "sent": _num(c.get("send_amt")),
        "opens": _num(c.get("uniqueopens")),
        "opens_total": _num(c.get("opens")),
        "clicks": _num(c.get("uniquelinkclicks")),
        "clicks_total": _num(c.get("linkclicks")),
        "unsubs": _num(c.get("unsubscribes")),
        "bounces": _num(c.get("hardbounces")) + _num(c.get("softbounces")),
        "forwards": _num(c.get("uniqueforwards")),
        "replies": _num(c.get("uniquereplies")),
        "socialshares": _num(c.get("socialshares")),
    }


def _fetch_campaigns(base, key, limit=100, max_pages=20):
    """ALL sent campaigns (status 5), newest first — paged so nothing is capped. Returns
    (rows, total_sent)."""
    rows, total, offset = [], 0, 0
    for _ in range(max_pages):
        payload = _get(base, key, "campaigns", {
            "limit": limit, "offset": offset, "orders[sdate]": "DESC", "filters[status]": 5,
        })
        batch = payload.get("campaigns") or []
        total = _meta_total(payload) or total
        rows.extend(_campaign_row(c) for c in batch)
        offset += limit
        if len(batch) < limit or (total and offset >= total):
            break
    rows = [r for r in rows if r["sent"] > 0 or r["date"]]
    return rows, total or len(rows)


def _fetch_lists(base, key, cap=100):
    payload = _get(base, key, "lists", {"limit": cap})
    out = []
    for l in (payload.get("lists") or []):
        out.append({
            "id": l.get("id"), "name": (l.get("name") or "Untitled").strip(),
            "subscribers": _num(l.get("active_subscribers")),
            "total": _num(l.get("non_deleted_subscribers")) or _num(l.get("subscriber_count")),
        })
    out.sort(key=lambda r: r["subscribers"], reverse=True)
    return out, _meta_total(payload)


def _fetch_automations(base, key, cap=100):
    payload = _get(base, key, "automations", {"limit": cap})
    out = []
    for a in (payload.get("automations") or []):
        out.append({
            "id": a.get("id"), "name": (a.get("name") or "Untitled").strip(),
            "entered": _num(a.get("entered")), "exited": _num(a.get("exited")),
            "active": str(a.get("status")) == "1",
        })
    out.sort(key=lambda r: r["entered"], reverse=True)
    return out, _meta_total(payload)


def _fetch_contacts_total(base, key):
    return _meta_total(_get(base, key, "contacts", {"limit": 1}))


def _crm_enabled(base, key):
    """Deals require a CRM upgrade — a 403 means CRM is off. Best-effort True/False."""
    try:
        _get(base, key, "deals", {"limit": 1})
        return True
    except urllib.error.HTTPError as e:  # noqa: PERF203
        return e.code != 403
    except Exception:  # noqa: BLE001
        return False


def fetch():
    """Assemble the `activecampaign` block. Never raises — returns {enabled:False,error:...} on failure."""
    base, key = _base_url(), _api_key()
    if not base or not key:
        return {"enabled": False, "error": "ActiveCampaign not configured (URL/key missing)"}

    out = {
        "enabled": True, "account": _account_name(base), "url": base,
        "fetched": _iso_date(__import__("datetime").date.today().isoformat()),
        "crm_enabled": False, "error": "",
        "totals": {"contacts": 0, "campaigns": 0, "campaigns_sent": 0, "lists": 0, "automations": 0},
        "campaigns": [], "lists": [], "automations": [],
    }
    errs = []
    try:
        out["totals"]["contacts"] = _fetch_contacts_total(base, key)
    except Exception as e:  # noqa: BLE001
        errs.append("contacts: %s" % str(e)[:80])
    try:
        rows, sent_total = _fetch_campaigns(base, key)
        out["campaigns"] = rows
        out["totals"]["campaigns_sent"] = sent_total or len(rows)
        all_c = _get(base, key, "campaigns", {"limit": 1})
        out["totals"]["campaigns"] = _meta_total(all_c)
    except Exception as e:  # noqa: BLE001
        errs.append("campaigns: %s" % str(e)[:80])
    try:
        lists, ltot = _fetch_lists(base, key)
        out["lists"] = lists
        out["totals"]["lists"] = ltot or len(lists)
    except Exception as e:  # noqa: BLE001
        errs.append("lists: %s" % str(e)[:80])
    try:
        autos, atot = _fetch_automations(base, key)
        out["automations"] = autos
        out["totals"]["automations"] = atot or len(autos)
    except Exception as e:  # noqa: BLE001
        errs.append("automations: %s" % str(e)[:80])
    out["crm_enabled"] = _crm_enabled(base, key)
    if errs:
        out["error"] = "; ".join(errs)
    return out


if __name__ == "__main__":
    import sys
    blk = fetch()
    dst = os.environ.get("AC_LOCAL_OUT")
    body = json.dumps(blk, indent=2)
    if dst:
        with open(dst, "w", encoding="utf-8") as fh:
            fh.write(body)
        print("wrote %s (enabled=%s, %d campaigns, %d lists, %d automations)"
              % (dst, blk.get("enabled"), len(blk.get("campaigns", [])),
                 len(blk.get("lists", [])), len(blk.get("automations", []))), file=sys.stderr)
    else:
        print(body)
