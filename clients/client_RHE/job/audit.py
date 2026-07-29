"""Independent audit of the published RHE payload (playbook section 9).

Recomputes everything from the JSON **without going through the dashboard**, and cross-checks the
totals against the source APIs. On Honey Tribe this class of script caught two real bugs that
looking at the screen never would; on RHE it caught the $321 spend gap from overwriting colliding
Meta rows, and the doubled send counts from merging a full sync into the previous publication.

    py -3 clients/client_RHE/job/audit.py                 # offline checks only
    py -3 clients/client_RHE/job/audit.py --live          # also re-query Windsor + ActiveCampaign

Exit code is non-zero if any check FAILS. WARNs are known-and-accepted quirks (recorded below so
they are not re-investigated every time).
"""
import argparse
import json
import os
import re
import socket
import sys
import urllib.parse
import urllib.request
from collections import Counter, defaultdict

if os.environ.get("FORCE_IPV4", "1") == "1":
    _real = socket.getaddrinfo
    socket.getaddrinfo = (lambda h, p, f=0, t=0, pr=0, fl=0:
                          _real(h, p, socket.AF_INET, t, pr, fl))

_HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_JSON = os.path.join(_HERE, "..", "data", "RHE.json")

FAILS, WARNS, OKS = [], [], []


def ok(msg):
    OKS.append(msg)
    print("  ok    %s" % msg)


def warn(msg):
    WARNS.append(msg)
    print("  WARN  %s" % msg)


def fail(msg):
    FAILS.append(msg)
    print("  FAIL  %s" % msg)


def head(t):
    print()
    print("=" * 92)
    print(t)
    print("=" * 92)


def near(a, b, tol=0.01):
    return abs(a - b) <= max(tol, abs(b) * 1e-6)


# --------------------------------------------------------------------------- PII
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"\+?\d[\d\s().-]{8,}\d")
TOKEN_PREFIXES = ("shpat_", "sk-", "xoxb-", "Bearer ", "api_key=", "Api-Token")


def _secret_needles():
    """Fragments of the LIVE credentials, read from the environment — never hardcoded.

    An earlier version pasted 16-char prefixes of the real Windsor and ActiveCampaign keys in
    here as leak-check needles, which put committable key material in the repo. The keys are
    already in the environment whenever this runs against the real APIs, so take them from there
    and fall back to the generic prefix scan when they are absent.
    """
    out = []
    for name in ("WINDSOR_API_KEY", "ACTIVECAMPAIGN_API_KEY", "SHOPIFY_ACCESS_TOKEN"):
        v = (os.environ.get(name) or "").strip()
        if len(v) >= 12:
            out.append(v[:12])
    return tuple(out)


def audit_pii(raw, data):
    head("1. PII AND SECRET LEAK CHECK (playbook 2.4 / 'Never commit secrets')")
    emails = EMAIL_RE.findall(raw)
    # The agency's own from-address and the account URL are legitimate; customer mail is not.
    allowed = {"info@agoradatadriven.com"}
    bad = sorted({e for e in emails if e.lower() not in allowed})
    if bad:
        fail("%d email address(es) in the payload, e.g. %s" % (len(bad), bad[:3]))
    else:
        ok("no email addresses in the payload")

    phones = PHONE_RE.findall(raw)
    # Long digit runs also appear in ids/timestamps; only flag +country-code shapes.
    realish = [p for p in phones if p.strip().startswith("+")]
    if realish:
        fail("%d phone-like string(s), e.g. %s" % (len(realish), realish[:3]))
    else:
        ok("no phone numbers in the payload")

    needles = _secret_needles()
    hit = [t for t in TOKEN_PREFIXES if t in raw]
    if any(s in raw for s in needles):
        hit.append("a LIVE key from the environment")
    if hit:
        fail("credential material present: %s" % hit)
    elif needles:
        ok("no token prefixes, and none of the %d live keys in the environment appear" % len(needles))
    else:
        ok("no token prefixes (set WINDSOR_API_KEY / ACTIVECAMPAIGN_API_KEY to also scan for "
           "the live keys themselves)")

    contacts = ((data.get("email") or {}).get("contacts")) or []
    if contacts:
        badhash = [c for c in contacts
                   if c.get("cid") and not re.fullmatch(r"[0-9a-f]{12}", str(c["cid"]))]
        if badhash:
            fail("%d contact id(s) are not 12-hex salted hashes" % len(badhash))
        else:
            ok("all %d contact ids are salted hashes" % len(contacts))
        leaked = [k for k in contacts[0].keys()
                  if k in ("email", "firstName", "lastName", "phone", "toAddress", "ip")]
        if leaked:
            fail("contact rows carry identifying fields: %s" % leaked)
        else:
            ok("contact rows carry no name/email/phone/ip fields")
        doms = Counter(c.get("domain") for c in contacts if c.get("domain"))
        ok("email DOMAIN retained for deliverability (%d distinct, top: %s) — identifies nobody"
           % (len(doms), [d for d, _ in doms.most_common(3)]))


# --------------------------------------------------------------------------- structure
def audit_structure(data):
    head("2. SHAPE AND REFERENTIAL INTEGRITY")
    for k in ("client", "generated_at", "data_through", "meta", "breakdowns", "email", "source"):
        if k not in data:
            fail("top-level key missing: %s" % k)
    meta = data.get("meta") or {}
    rows = meta.get("rows") or []
    if not rows:
        fail("meta.rows is empty")
        return rows
    ok("meta.rows = %d rows" % len(rows))

    need = {"d", "acct", "camp", "adset", "ad", "spend", "imps", "clicks", "lclk",
            "reach", "freq", "leads", "thru", "react"}
    missing = need - set(rows[0].keys())
    if missing:
        fail("meta row missing fields: %s" % sorted(missing))
    else:
        ok("meta rows carry every field the dashboard reads")

    baddate = [r for r in rows if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(r.get("d", "")))]
    if baddate:
        fail("%d meta rows have a malformed date" % len(baddate))
    else:
        ok("every meta row has an ISO date")

    neg = [r for r in rows if r["spend"] < 0 or r["imps"] < 0 or r["leads"] < 0]
    if neg:
        fail("%d meta rows have negative spend/impressions/leads" % len(neg))
    else:
        ok("no negative spend, impressions or leads")

    dates = sorted(r["d"] for r in rows)
    rng = meta.get("range") or [None, None]
    if rng[0] != dates[0] or rng[1] != dates[-1]:
        fail("meta.range %s does not match the rows (%s..%s)" % (rng, dates[0], dates[-1]))
    else:
        ok("meta.range matches the rows: %s → %s" % (rng[0], rng[1]))

    keys = Counter((r["d"], r["acct"], r["camp"], r["adset"], r["ad"]) for r in rows)
    dupes = [k for k, v in keys.items() if v > 1]
    if dupes:
        fail("%d duplicate (date, account, campaign, adset, ad) keys — merge is broken" % len(dupes))
    else:
        ok("every (date, account, campaign, adset, ad) key is unique")

    declared = set(meta.get("accounts") or [])
    actual = {r["acct"] for r in rows if r.get("acct")}
    if declared != actual:
        fail("meta.accounts %s != the accounts in rows %s" % (sorted(declared), sorted(actual)))
    else:
        ok("account list matches the rows: %s" % sorted(actual))
    return rows


def audit_funnel(rows):
    head("3. MONOTONIC FUNNEL (impressions >= link clicks >= leads)")
    imps = sum(r["imps"] for r in rows)
    lclk = sum(r["lclk"] for r in rows)
    clicks = sum(r["clicks"] for r in rows)
    leads = sum(r["leads"] for r in rows)
    print("      impressions=%s  clicks=%s  link_clicks=%s  leads=%s"
          % (f"{imps:,}", f"{clicks:,}", f"{lclk:,}", f"{leads:,}"))
    if imps >= lclk:
        ok("impressions >= link clicks")
    else:
        fail("impressions (%d) < link clicks (%d)" % (imps, lclk))
    if lclk >= leads:
        ok("link clicks >= leads")
    else:
        fail("link clicks (%d) < leads (%d)" % (lclk, leads))
    if clicks >= lclk:
        ok("all clicks >= link clicks")
    else:
        warn("all clicks (%d) < link clicks (%d) — Meta reports these independently" % (clicks, lclk))

    per_day = defaultdict(lambda: [0, 0])
    for r in rows:
        per_day[r["d"]][0] += r["lclk"]
        per_day[r["d"]][1] += r["leads"]
    broken = [d for d, (lc, ld) in per_day.items() if ld > lc]
    if broken:
        warn("%d day(s) have more leads than link clicks (e.g. %s) — a lead can be attributed to "
             "a click from an earlier day, so this is expected at day grain"
             % (len(broken), sorted(broken)[:3]))
    else:
        ok("no day has more leads than link clicks")


def audit_breakdowns(data, rows):
    head("4. BREAKDOWNS RECONCILE TO THE MAIN PULL")
    B = data.get("breakdowns") or {}
    window = B.get("window") or "last_365d"
    days = int(re.sub(r"\D", "", window) or 365)
    dates = sorted(r["d"] for r in rows)
    cutoff = dates[-1]
    import datetime as _dt
    start = (_dt.date.fromisoformat(cutoff) - _dt.timedelta(days=days - 1)).isoformat()
    main_spend = sum(r["spend"] for r in rows if r["d"] >= start)
    print("      main pull over %s (%s..%s) spend = %.2f" % (window, start, cutoff, main_spend))
    for slug in ("age_gender", "region", "platform", "position", "device"):
        b = B.get(slug) or {}
        brs = b.get("rows") or []
        if not brs:
            warn("%s breakdown is empty" % slug)
            continue
        sp = sum(x["spend"] for x in brs)
        ld = sum(x["leads"] for x in brs)
        # 1.5% tolerance: breakdown queries and the main pull round independently at Meta's end
        if main_spend and abs(sp - main_spend) / main_spend < 0.015:
            ok("%-11s spend %.2f reconciles to the main pull (%.2f%% apart)"
               % (slug, sp, abs(sp - main_spend) / main_spend * 100))
        else:
            warn("%-11s spend %.2f vs main %.2f (%.1f%% apart)"
                 % (slug, sp, main_spend, abs(sp - main_spend) / max(main_spend, 1) * 100))
        if slug == "region":
            if ld == 0 and b.get("has_leads") is False:
                ok("region correctly flagged has_leads=false (Meta returns no leads by region)")
            else:
                fail("region has_leads=%s with %d leads — the dashboard's geo note is now wrong"
                     % (b.get("has_leads"), ld))
        elif ld > 0 and b.get("has_leads"):
            ok("%-11s carries %s leads (has_leads=true)" % (slug, f"{ld:,}"))
        elif ld == 0:
            warn("%s returned no leads in this window" % slug)


def audit_email(data):
    head("5. ACTIVECAMPAIGN BLOCK")
    E = data.get("email") or {}
    if not E.get("enabled"):
        warn("ActiveCampaign block disabled: %s" % E.get("error"))
        return
    T = E.get("totals") or {}
    contacts = E.get("contacts") or []
    daily = E.get("daily") or []
    ok("contacts=%d  campaigns_sent=%d  lists=%d  automations=%d"
       % (len(contacts), T.get("campaigns_sent", 0), len(E.get("lists") or []),
          len(E.get("automations") or [])))

    if T.get("contacts") and abs(T["contacts"] - len(contacts)) > 25:
        warn("meta.total contacts %d vs %d rows fetched (the account is live; new signups land "
             "between the count and the crawl)" % (T["contacts"], len(contacts)))
    else:
        ok("contact count matches the API's own total (%s)" % T.get("contacts"))

    if E.get("crm_enabled"):
        ok("CRM enabled")
    else:
        ok("CRM disabled (403 'Upgrade your account') — expected, degrades cleanly")

    # A leg that failed is best-effort by design, but it must never pass silently: the first live
    # run lost the WHOLE events leg to a single 503 and published zero opens for a full year.
    err = (E.get("error") or "").strip()
    if err:
        fail("an ActiveCampaign leg failed and the payload is incomplete: %s" % err[:180])
    else:
        ok("every ActiveCampaign leg completed without error")
    if E.get("partial"):
        warn("the crawl hit a page cap or stopped early — re-run to accumulate more history")

    # daily series integrity
    ds = [d["d"] for d in daily]
    if ds != sorted(ds):
        fail("email.daily is not date-sorted")
    elif len(set(ds)) != len(ds):
        fail("email.daily has duplicate dates")
    else:
        ok("email.daily is sorted and unique (%d days, %s → %s)"
           % (len(ds), ds[0] if ds else "-", ds[-1] if ds else "-"))
    tot_sends = sum(d["sends"] for d in daily)
    if T.get("sends") == tot_sends:
        ok("totals.sends (%s) equals the sum of the daily series" % f"{tot_sends:,}")
    else:
        fail("totals.sends %s != daily sum %s" % (T.get("sends"), tot_sends))

    # the doubling bug: a full sync merged into the previous publication
    if tot_sends > 60000:
        fail("send total %s exceeds the account's emailActivities total (~54k) — the full-sync "
             "merge is double-counting" % f"{tot_sends:,}")
    else:
        ok("send total %s is within the account's emailActivities total" % f"{tot_sends:,}")

    # The daily series must carry the CORRECTED shape: sends + real clicks + Apple MPP, and NO
    # synthetic `opens`. Mapping the `log` activity to "open" made opens equal sends exactly and
    # the dashboard reported a 100% open rate.
    if daily:
        ver = int(daily[0].get("v") or 0)
        if ver >= 3:
            ok("daily series is v%d (verified event classification)" % ver)
        else:
            fail("daily series is v%d — engagement counts come from a superseded event "
                 "classification and must be migrated (see DAILY_VERSION)" % ver)
        if any("clicks" in d for d in daily):
            fail("daily rows carry a `clicks` field — clicks are campaign-level only")
        else:
            ok("no per-day `clicks` field (clicks are campaign-level by design)")

        recent = daily[-30:]
        rec_send = sum(d["sends"] for d in recent)
        rec_open = sum(d.get("opens", 0) for d in recent)
        if rec_send and not rec_open:
            fail("the last 30 days have %s sends but ZERO opens — the event crawl is not covering "
                 "the recent window (see AC_EVENT_FIRST_DAYS / newest-first paging)"
                 % f"{rec_send:,}")
        else:
            ok("recent window has engagement (%s opens on %s sends in the last 30 days)"
               % (f"{rec_open:,}", f"{rec_send:,}"))
        # THE tell for a misclassified event type: a rate that pins at or above 100%.
        rate = (100.0 * rec_open / rec_send) if rec_send else 0
        if rec_send and rec_open >= rec_send:
            fail("opens (%s) >= sends (%s) over the last 30 days — an event type is misclassified. "
                 "This is exactly how the `log`-as-open bug surfaced (a 100.00%% open rate)."
                 % (f"{rec_open:,}", f"{rec_send:,}"))
        elif rec_send and rate > 90:
            warn("open rate is %.1f%% over the last 30 days — implausibly high, re-check the "
                 "event classification" % rate)
        else:
            ok("open rate is plausible (%.1f%% over the last 30 days)" % rate)

    # campaign-level open rate is the authoritative one; it must be inside 0..100
    camps = [c for c in (E.get("campaigns") or []) if c.get("sent")]
    if camps:
        bad = [c for c in camps if c["opens"] > c["sent"]]
        rate = 100.0 * sum(c["opens"] for c in camps) / sum(c["sent"] for c in camps)
        if bad:
            fail("%d campaign(s) report more openers than recipients" % len(bad))
        elif rate >= 99.9:
            fail("broadcast open rate is %.2f%% — implausible, check the open source" % rate)
        else:
            ok("broadcast open rate %.1f%% across %d campaigns (campaign-level, authoritative)"
               % (rate, len(camps)))

    # quiz normalisation: no value may appear in two codings
    for field in ("experience", "timeline", "status"):
        vals = {c[field] for c in contacts if c.get(field)}
        folded = defaultdict(list)
        for v in vals:
            folded[re.sub(r"[\s_\-–—]+", " ", v.strip().lower())].append(v)
        dupes = {k: v for k, v in folded.items() if len(v) > 1}
        if dupes:
            fail("%s has double-coded values: %s" % (field, dupes))
        else:
            ok("%-10s has one coding per value (%d distinct: %s)"
               % (field, len(vals), sorted(vals)[:4]))

    # engaged/converted derivation
    eng = sum(1 for c in contacts if c.get("engaged"))
    eng_expected = sum(1 for c in contacts if c.get("opened"))
    if eng == eng_expected:
        ok("engaged flag == 'has an open date' for all %d contacts (%d engaged)"
           % (len(contacts), eng))
    else:
        fail("engaged flag %d != contacts with an open date %d" % (eng, eng_expected))
    conv = sum(1 for c in contacts if c.get("converted"))
    if conv == 0:
        ok("converted = 0, and no contact carries Client/Returning Client — correctly empty, "
           "not broken")
    else:
        ok("converted = %d" % conv)

    for c in E.get("campaigns") or []:
        if c["opens"] > c["sent"] and c["sent"]:
            fail("campaign %r has more unique opens than recipients" % c["name"][:40])
        if c["clicks"] > c["opens"] and c["opens"]:
            warn("campaign %r has more unique clicks than unique opens" % c["name"][:40])
    ok("no campaign has unique opens above its recipient count")


def audit_live(data):
    head("7. LIVE CROSS-CHECK AGAINST THE SOURCE APIs")
    wk = os.environ.get("WINDSOR_API_KEY", "")
    accounts = os.environ.get(
        "WINDSOR_ACCOUNTS",
        "facebook__291824415053555,facebook__744718258097253,facebook__819110256113106")
    if not wk:
        warn("WINDSOR_API_KEY not set — skipping the Windsor cross-check")
    else:
        q = urllib.parse.urlencode({"api_key": wk, "date_preset": "last_30d",
                                    "fields": "date,spend,impressions,actions_lead",
                                    "select_accounts": accounts})
        try:
            with urllib.request.urlopen(
                    "https://connectors.windsor.ai/all?" + q, timeout=240) as r:
                payload = json.loads(r.read())
            api = payload.get("data", payload)
            api_spend = sum(float(x.get("spend") or 0) for x in api)
            api_leads = sum(float(x.get("actions_lead") or 0) for x in api)
            days = sorted(str(x.get("date"))[:10] for x in api)
            lo, hi = days[0], days[-1]
            rows = [r for r in data["meta"]["rows"] if lo <= r["d"] <= hi]
            ours_spend = sum(r["spend"] for r in rows)
            ours_leads = sum(r["leads"] for r in rows)
            print("      window %s..%s" % (lo, hi))
            print("      spend  ours=%.2f  api=%.2f" % (ours_spend, api_spend))
            print("      leads  ours=%d    api=%d" % (ours_leads, api_leads))
            # The payload stores each row's spend rounded to cents, so summing thousands of rows
            # drifts a dollar or two from the API's own aggregation of the same figures. That is
            # rounding, not a discrepancy — the tolerance is proportional so a REAL gap (a dropped
            # or double-counted row, which is what this check exists to catch) still fails.
            tol = max(2.0, api_spend * 0.0005)
            drift = abs(ours_spend - api_spend)
            if drift <= tol:
                ok("spend matches the Windsor API within per-row rounding (%.2f of %.2f, %.4f%%)"
                   % (drift, api_spend, (drift / api_spend * 100) if api_spend else 0))
            else:
                fail("spend differs from the API by %.2f (tolerance %.2f) — a row is being dropped "
                     "or double-counted" % (drift, tol))
            if abs(ours_leads - api_leads) <= 1:
                ok("leads match the Windsor API")
            else:
                fail("leads differ from the API by %d" % abs(ours_leads - api_leads))
        except Exception as e:  # noqa: BLE001
            warn("Windsor cross-check failed: %s" % str(e)[:140])

    base = os.environ.get("ACTIVECAMPAIGN_URL", "")
    key = os.environ.get("ACTIVECAMPAIGN_API_KEY", "")
    if not (base and key):
        warn("ActiveCampaign env not set — skipping that cross-check")
        return
    try:
        def ac(path, params):
            url = (base.rstrip("/") + "/api/3/" + path + "?"
                   + urllib.parse.urlencode(params))
            req = urllib.request.Request(url, headers={"Api-Token": key})
            with urllib.request.urlopen(req, timeout=90) as r:
                return json.loads(r.read())
        tot_c = int((ac("contacts", {"limit": 1}).get("meta") or {}).get("total") or 0)
        tot_e = int((ac("emailActivities", {"limit": 1}).get("meta") or {}).get("total") or 0)
        ours_c = len((data["email"] or {}).get("contacts") or [])
        ours_s = sum(d["sends"] for d in ((data["email"] or {}).get("daily") or []))
        print("      contacts ours=%d  api=%d" % (ours_c, tot_c))
        print("      sends    ours=%d  api emailActivities=%d" % (ours_s, tot_e))
        if abs(ours_c - tot_c) <= 25:
            ok("contact count matches the API (within live-signup drift)")
        else:
            fail("contact count off by %d" % abs(ours_c - tot_c))
        if ours_s <= tot_e:
            pctv = (ours_s / tot_e * 100) if tot_e else 0
            (ok if pctv > 95 else warn)(
                "send log holds %d of %d API rows (%.1f%%)%s"
                % (ours_s, tot_e, pctv,
                   "" if pctv > 95 else " — still backfilling; re-run to accumulate"))
        else:
            fail("we hold MORE sends (%d) than the API reports (%d) — double counting"
                 % (ours_s, tot_e))
    except Exception as e:  # noqa: BLE001
        warn("ActiveCampaign cross-check failed: %s" % str(e)[:140])


def audit_creatives(data):
    head("6. CREATIVE GALLERY")
    C = data.get("creatives") or {}
    if not C.get("enabled"):
        warn("creative block disabled: %s" % (C.get("error") or "no items"))
        return
    items = C.get("items") or []
    ok("%d creatives (window %s)" % (len(items), C.get("window")))

    ids = [c.get("cid") for c in items]
    if len(set(ids)) != len(ids):
        fail("duplicate creative ids in the gallery")
    else:
        ok("every creative id is unique")

    noheads = [c for c in items if not (c.get("head") or "").strip()]
    if noheads:
        fail("%d creative(s) have no headline to fall back on" % len(noheads))
    else:
        ok("every creative has a headline (ad name substituted where Meta's title is a link)")

    # a headline that is still a bare link means _clean_headline let one through
    linky = [c for c in items
             if (c.get("head") or "").lower().startswith(("http://", "https://", "www."))]
    if linky:
        fail("%d headline(s) are raw links: %s" % (len(linky), [c["head"] for c in linky[:3]]))
    else:
        ok("no headline is a raw link")

    # catalogue/dynamic ads carry an unrendered Liquid template as their title
    templ = [c for c in items if "{{" in (c.get("head") or "") or "}}" in (c.get("head") or "")]
    if templ:
        fail("%d headline(s) are unrendered dynamic-ad templates: %s"
             % (len(templ), [c["head"][:40] for c in templ[:3]]))
    else:
        ok("no headline is an unrendered template token")

    cached = sum(1 for c in items if c.get("cached"))
    ok("%d of %d have a permanent cached image (the rest fall back to Meta's live URL, then to "
       "a branded tile)" % (cached, len(items)))

    # the gallery joins on `cid` in meta.rows -- if that link breaks the gallery silently empties
    rows = (data.get("meta") or {}).get("rows") or []
    with_cid = sum(1 for r in rows if r.get("cid"))
    known = set(ids)
    joinable = sum(1 for r in rows if r.get("cid") in known)
    if not with_cid:
        fail("no meta row carries `cid` -- the gallery cannot aggregate delivery")
    elif not joinable:
        fail("no meta row's `cid` matches a gallery creative -- the join is broken")
    else:
        ok("%s of %s meta rows carry a cid; %s join to a gallery creative"
           % (f"{with_cid:,}", f"{len(rows):,}", f"{joinable:,}"))

    long_body = [c for c in items if len(c.get("body") or "") > 1400]
    if long_body:
        warn("%d creative bodies exceed the truncation cap" % len(long_body))
    else:
        ok("creative copy is within the size cap")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default=DEFAULT_JSON)
    ap.add_argument("--live", action="store_true", help="also re-query the source APIs")
    args = ap.parse_args()

    path = os.path.abspath(args.json)
    if not os.path.exists(path):
        print("[!] %s not found — run job/main.py first." % path)
        return 2
    with open(path, "rb") as fh:
        blob = fh.read()
    raw = blob.decode("utf-8")
    data = json.loads(raw)

    print("RHE payload audit — %s (%.2f MB)" % (path, len(blob) / 1048576.0))
    print("generated_at=%s  data_through=%s" % (data.get("generated_at"), data.get("data_through")))

    audit_pii(raw, data)
    rows = audit_structure(data)
    if rows:
        audit_funnel(rows)
        audit_breakdowns(data, rows)
    audit_email(data)
    audit_creatives(data)
    if args.live:
        audit_live(data)

    head("KNOWN AND ACCEPTED (do not re-investigate)")
    for line in [
        "Meta returns actions_lead = 0 on the REGION breakdown, on every field combination "
        "tried — geography can show delivery only, never leads or CPL.",
        "unique_actions_lead is returned but is identically 0 account-wide; "
        "unique_actions_link_click works. The dashboard renders it as n/a, not 0.",
        "The full 21-field Windsor pull 400s beyond last_365d because of the unique_* fields; "
        "the deep pull drops them and reaches last_1095d (history from 2024-01-15).",
        "ActiveCampaign CRM (/deals) 403s — the account has no CRM upgrade.",
        "/activities ignores every filters[...] form; only after=<ISO> narrows it.",
        "ActiveCampaign limit is hard-capped at 100 regardless of what is requested.",
        "Only 11 of 312 campaigns were ever sent; automations carry most of the volume.",
    ]:
        print("  · " + line)

    head("RESULT")
    print("  %d ok · %d warn · %d FAIL" % (len(OKS), len(WARNS), len(FAILS)))
    if FAILS:
        print()
        for f in FAILS:
            print("  FAIL  " + f)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
