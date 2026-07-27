"""ActiveCampaign pull for the Rooming House Expert export job.

EXTENDED from `clients/client_riverdance/job/activecampaign.py` — the campaign/list/automation
legs are that module's, unchanged in shape. What RHE adds is everything the lead-nurture story
needs and riverdance did not: per-contact quiz answers, contact-level engagement counters, the
per-day send series, and per-day open/click events.

WHY four legs instead of one (measured against this account, 2026-07-27):

  A. contacts  `/contacts?include=fieldValues`   49 pages, ~46 s   <- quiz + engagement + growth
  B. campaigns `/campaigns?filters[status]=5`     1 page,   ~2 s   <- authoritative broadcast stats
                                                                     (the ONLY source of clicks)
  C. sends     `/emailActivities`               543 pages, ~23 min <- per-day send series (watermarked)
  D. events    `/activities` newest-first        pages vary        <- per-day OPENS (watermarked)

Only 11 of 312 campaigns were ever SENT (14,976 sends) but `/emailActivities` holds 54,243 — this
account's volume is overwhelmingly AUTOMATION mail, so a campaign-only backbone would understate
send volume 3.5x. Hence leg C. Legs C and D are the expensive ones and are therefore INCREMENTAL:
each run reads only what is newer than the watermark carried in the previous publication and merges
forward (playbook 3.3/3.4). `AC_FULL_SYNC=1` forces a full crawl.

API constraints found by probing — do not "fix" these, they are the API's:
  * `limit` is hard-capped at 100 on every endpoint (200/500/1000 all return 100).
  * `/activities` IGNORES every `filters[...]` form (all return the unfiltered 220,897 total).
    Only `after=<ISO8601>` narrows it. So event ingestion is watermark-based, not filtered.
  * `/emailActivities` ignores `filters[tstamp][gt]` but DOES honour `orders[tstamp]=DESC`, so it
    is paged newest-first and stopped at the watermark.
  * `automation_name` is always null on emailActivities — the SUBJECT LINE is the only template
    identifier, so templates are grouped by a normalised subject (see `template_of`).
  * Deals/CRM 403 "Upgrade your account to enable the CRM" — expected, degrades to crm_enabled:false.

PII: this account's rows carry emails, names and phone numbers. NONE of it is emitted. A contact is
reduced to a salted hash used only for distinct counts and joins, plus its email DOMAIN (kept
because deliverability by provider is actionable and a domain identifies nobody).

Shape returned — matched BY NAME to dash/dashboard.html's DATA.email.* keys:
  { enabled, account, url, fetched, crm_enabled, error, partial,
    totals{contacts,campaigns,campaigns_sent,lists,automations,sends,opens,mpp,engaged,converted,
           campaign_sent,campaign_opens,campaign_clicks},
    campaigns[]  (SENT only, newest first — the only source of CLICK figures)
    lists[] · automations[]
    contacts[]   (hashed: cid, cdate, domain, sent, opened, clicked, bounced, quiz fields, status)
    daily[]      ({d, sends, opens, mpp, v})   <- NO `clicks`: see EVENT_KINDS
    templates[]  ({name, sends, first, last})
    quiz{...}    (option -> label maps, straight from /fields/<id>/options)
    watermark{sends, events, events_oldest} }
"""
import datetime
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from urllib.parse import urlencode, urlparse

PROJECT = "agora-data-driven"
CLIENT = "RHE"
AC_SECRET = "%s-activecampaign-key" % CLIENT.lower()   # secret ids follow the lowercase resource key
_TIMEOUT = 90
_PAGE = 100                       # hard API cap; larger values are silently clamped

_SALT = os.environ.get("RHE_SALT", "agora-rhe-v1")

# Custom-field ids on THIS account (probed 2026-07-27). Values are read from
# /fields/<id>/options at runtime, so a re-labelled option needs no code change.
FIELD_EXPERIENCE = "6"     # First-time investor / Some experience / Experienced / Portfolio builder
FIELD_TIMELINE = "10"      # Within 3 months / 3-6 months / 6-12 months / Just exploring
FIELD_FIRST_TIME = "2"     # Yes / No
FIELD_STATUS = "3"         # Lead / Client / Returning Client / Listing Lead
FIELD_PATHWAY = "15"       # Stratos / Conversion / Manual Review
FIELD_CAMPAIGN = "11"      # free-text campaign name the automation stamps on the contact
QUIZ_FIELDS = {
    FIELD_EXPERIENCE: "experience",
    FIELD_TIMELINE: "timeline",
    FIELD_FIRST_TIME: "first_time",
    FIELD_STATUS: "status",
    FIELD_PATHWAY: "pathway",
    FIELD_CAMPAIGN: "campaign",
}

# The client's decision (2026-07-27): a contact is CONVERTED when its Status field says so.
# This replaces the Colab's phone-number join against a Google Sheet, which was matching zero
# rows — `final_df[final_df['is_converted']==1]` returned an empty Series.
# NOTE: as of the first build NO contact carries Client / Returning Client (2,807 Lead +
# 2,016 Listing Lead), so this metric is legitimately 0 until the team starts tagging. The
# dashboard therefore LEADS with the behavioural proxy (an ENGAGED lead — one that has opened at
# least one email) and renders Converted as a wired-empty state rather than a broken zero.
CONVERTED_STATUSES = {"client", "returning client"}

# referenceModelName -> the engagement event it represents. VERIFIED against campaign-level
# aggregates across all 12 sent campaigns (do not change without redoing that check):
#
#   log            SEND / delivery record — NOT an open, NOT a click.
#                  /logs totals 54,304 vs /emailActivities' 54,243, and a /logs/<id> row carries
#                  `sendid`, `campaignid`, `messageid`, `successful: 1`. EXCLUDED here because
#                  sends already come from /emailActivities; counting it again doubles them.
#                  Mapping this to "open" made the dashboard report a 100.00% open rate.
#
#   link-data      OPEN (tracking-pixel read). The record has an `isread` flag, a `ua` of e.g.
#                  "GMail" and a mail-proxy IP. Across the 12 campaigns: 6,070 link-data rows
#                  against 7,765 total opens (0.78x) and 5,155 unique opens (1.18x) — but against
#                  only 133 link clicks (45.6x). It is an open, categorically not a click.
#
#   mpp-link-data  Apple Mail Privacy Protection prefetch — a MACHINE fetch of the tracking pixel
#                  (`isread: 2`), on its OWN endpoint (/mppLinkData, 23,379). It is NOT a subset of
#                  `opens`: for campaign 537, linkData=585 against AC's own opens=615, while
#                  mppData=429 — 585+429 would be 1,014, far past 615. ActiveCampaign keeps these
#                  out of its open counter, so `mpp` is reported as its own line, never as a share
#                  of opens.
#
# CLICKS are NOT in the activity stream. The only trustworthy click figures are campaign-level
# (`linkclicks` / `uniquelinkclicks`), so the dashboard reports clicks for broadcasts only and
# says so. Do not synthesise a per-day click series.
EVENT_KINDS = {"link-data": "open", "mpp-link-data": "mpp"}

# Bumped when the MEANING of a stored field changes, so a carried-forward publication is migrated
# rather than silently mixing old (wrong) numbers with corrected ones.
#   v2 — dropped daily `opens` derived from `log` (the 100%-open-rate bug)
#   v3 — `link-data` reclassified from click to OPEN; daily `clicks` removed (campaign-level only)
DAILY_VERSION = 3

# Bound the FIRST crawl so a cold start cannot run past the job's task timeout. Later runs are
# incremental and cheap, and history accumulates in the bucket (playbook 3.4).
MAX_SEND_PAGES = int(os.environ.get("AC_MAX_SEND_PAGES", "600"))
MAX_EVENT_PAGES = int(os.environ.get("AC_MAX_EVENT_PAGES", "600"))
MAX_CONTACT_PAGES = int(os.environ.get("AC_MAX_CONTACT_PAGES", "120"))
# COLD START: `/activities` pages oldest-first from offset 0, so a page-capped first run returns
# only ancient events and the dashboard shows zero opens for every recent day (caught in the
# first browser check). Bound the first crawl by TIME instead — the run then covers the recent
# window that matters, and later runs walk forward from the watermark. Raise it (or set
# AC_EVENT_FIRST_DAYS=0 for no bound) when doing a deliberate full backfill.
EVENT_FIRST_DAYS = int(os.environ.get("AC_EVENT_FIRST_DAYS", "120"))


def _num(x):
    try:
        return int(float(x)) if x is not None and x != "" else 0
    except (TypeError, ValueError):
        return 0


def _hash(v):
    """A contact reduced to a salted hash — used only for distinct counts and joins."""
    if not v:
        return None
    return hashlib.sha1((_SALT + str(v).strip().lower()).encode("utf-8")).hexdigest()[:12]


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


# Transient statuses worth retrying. ActiveCampaign starts returning 503 once a long crawl has
# been hammering it — the first full run did 543 send pages and then the event leg 503'd on its
# first request, losing the whole leg. 429 is the documented rate limit; 500/502/504 are the
# usual transient gateway noise.
_RETRY_STATUS = (429, 500, 502, 503, 504)
_MAX_TRIES = int(os.environ.get("AC_MAX_TRIES", "5"))
_PAGE_PAUSE = float(os.environ.get("AC_PAGE_PAUSE", "0.12"))


def _get(base, key, path, params=None):
    """One GET with exponential backoff on rate limiting / transient gateway errors.

    Raises only after `_MAX_TRIES` attempts, so a single blip can never sink a whole leg.
    """
    q = urlencode(params or {})
    full = "%s/api/3/%s%s%s" % (base, path.lstrip("/"), "?" if q else "", q)
    req = urllib.request.Request(full, headers={"Api-Token": key, "User-Agent": "agora-rhe/1.0"})
    last = None
    for attempt in range(_MAX_TRIES):
        try:
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            last = e
            if e.code not in _RETRY_STATUS or attempt == _MAX_TRIES - 1:
                raise
            # honour Retry-After when the server offers one
            wait = 0
            try:
                wait = float(e.headers.get("Retry-After") or 0)
            except (TypeError, ValueError):
                wait = 0
            if wait <= 0:
                wait = min(60.0, 2.0 * (2 ** attempt))
            print("    ac: HTTP %d — backing off %.0fs (attempt %d/%d)"
                  % (e.code, wait, attempt + 1, _MAX_TRIES))
            time.sleep(wait)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = e
            if attempt == _MAX_TRIES - 1:
                raise
            wait = min(30.0, 2.0 * (2 ** attempt))
            print("    ac: %s — retrying in %.0fs (attempt %d/%d)"
                  % (type(e).__name__, wait, attempt + 1, _MAX_TRIES))
            time.sleep(wait)
    raise last if last else RuntimeError("ActiveCampaign request failed")


def _meta_total(payload):
    try:
        return _num((payload.get("meta") or {}).get("total"))
    except Exception:  # noqa: BLE001
        return 0


def _iso_date(s):
    """'2026-07-10T14:30:00-05:00' -> '2026-07-10' (date part only; None -> '')."""
    return str(s).split("T", 1)[0] if s else ""


# --- template naming ---------------------------------------------------------
# The Colab stripped a trailing ", <FirstName>" personalisation off the subject so Looker could
# group sends. Same idea, but done without a regex over a pandas Series: emoji and trailing
# punctuation are also stripped, because "The $20,000 'Cheap' Mistake" and
# "The $20,000 'Cheap' Mistake [rocket]" are one template, not two.
# A subject line is USER-GENERATED text that has been through a merge-tag renderer, so it can
# contain whatever the tag resolved to — including the recipient's email address or phone number.
# One live subject was "Did you get the Warragul brochure, j.smith@example.com" (address anonymised here): the tag
# fell back to the email instead of the first name, and that address then rode into the published
# payload inside a template NAME. The audit caught it (playbook 9). Redaction here is the root
# fix; it does not depend on any length or word-count heuristic.
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"\+?\d[\d\s().\-]{7,}\d")


def redact(text):
    """Strip anything that identifies a person from free text before it is published."""
    s = str(text or "")
    s = _EMAIL_RE.sub("<recipient>", s)
    s = _PHONE_RE.sub("<number>", s)
    return s


def template_of(subject):
    s = redact(subject).strip()
    if not s:
        return "(no subject)"
    if "," in s:
        head, _, tail = s.rpartition(",")
        t = tail.strip().rstrip("?!.… ")
        # A trailing personalisation token: a single word, or the redaction placeholder that a
        # resolved email/phone merge tag left behind.
        if head.strip() and t and (t in ("<recipient>", "<number>")
                                   or (" " not in t and len(t) <= 40)):
            s = head.strip()
    out = "".join(ch for ch in s if ord(ch) < 0x2000 or ch.isalnum() or ch in "‘’“”–—…<>")
    return out.strip().rstrip("?!.… ") or s[:80]


# --- leg B: campaigns (riverdance's, unchanged in shape) ---------------------
def _campaign_row(c):
    sent = _num(c.get("send_amt"))
    uo, uc = _num(c.get("uniqueopens")), _num(c.get("uniquelinkclicks"))
    return {
        "id": c.get("id"),
        # campaign names are free text too — same redaction as subjects
        "name": redact(c.get("name") or "Untitled").strip(),
        "date": _iso_date(c.get("sdate") or c.get("ldate") or c.get("mdate")),
        "sent": sent,
        "opens": uo,
        "opens_total": _num(c.get("opens")),
        "clicks": uc,
        "clicks_total": _num(c.get("linkclicks")),
        "unsubs": _num(c.get("unsubscribes")),
        "bounces": _num(c.get("hardbounces")) + _num(c.get("softbounces")),
        "forwards": _num(c.get("uniqueforwards")),
        "replies": _num(c.get("uniquereplies")),
        "socialshares": _num(c.get("socialshares")),
    }


def _fetch_campaigns(base, key, max_pages=20):
    rows, total, offset = [], 0, 0
    for _ in range(max_pages):
        payload = _get(base, key, "campaigns", {
            "limit": _PAGE, "offset": offset, "orders[sdate]": "DESC", "filters[status]": 5,
        })
        batch = payload.get("campaigns") or []
        total = _meta_total(payload) or total
        rows.extend(_campaign_row(c) for c in batch)
        offset += _PAGE
        if len(batch) < _PAGE or (total and offset >= total):
            break
    rows = [r for r in rows if r["sent"] > 0 or r["date"]]
    return rows, total or len(rows)


def _fetch_lists(base, key):
    payload = _get(base, key, "lists", {"limit": _PAGE})
    out = [{
        "id": l.get("id"), "name": redact(l.get("name") or "Untitled").strip(),
        "subscribers": _num(l.get("active_subscribers")),
        "total": _num(l.get("non_deleted_subscribers")) or _num(l.get("subscriber_count")),
    } for l in (payload.get("lists") or [])]
    out.sort(key=lambda r: r["subscribers"], reverse=True)
    return out, _meta_total(payload)


def _fetch_automations(base, key):
    payload = _get(base, key, "automations", {"limit": _PAGE})
    out = [{
        "id": a.get("id"), "name": redact(a.get("name") or "Untitled").strip(),
        "entered": _num(a.get("entered")), "exited": _num(a.get("exited")),
        "active": str(a.get("status")) == "1",
    } for a in (payload.get("automations") or [])]
    out.sort(key=lambda r: r["entered"], reverse=True)
    return out, _meta_total(payload)


# --- quiz option labels ------------------------------------------------------
def _fetch_quiz_options(base, key):
    """{slug: [label, ...]} straight from the account, so a re-labelled option needs no redeploy."""
    out = {}
    for fid, slug in QUIZ_FIELDS.items():
        try:
            payload = _get(base, key, "fields/%s/options" % fid, {"limit": 60})
            labels = [str(o.get("label")).strip() for o in (payload.get("fieldOptions") or [])
                      if o.get("label")]
            if labels:
                out[slug] = labels
        except Exception:  # noqa: BLE001 — a free-text field has no options; that is fine
            continue
    return out


# --- ONE quiz vocabulary -----------------------------------------------------
# The same answer arrives in TWO codings because two intake paths write it: the Meta lead form
# posts the raw machine value (`some_experience`, `within_3_months`, `3–6_months`) while the
# ActiveCampaign dropdown stores its display label (`Some experience`, `Within 3 months`,
# `3–6 months`). Left alone, every quiz chart splits each category in two and understates the
# larger segments by roughly half — the same class of bug as honeytribe's self-referral split.
# So every value goes through ONE normaliser keyed off the field's own option labels.
def _fold(s):
    """'first-time_investor' / 'First-time investor' -> 'first time investor'."""
    s = (s or "").strip().lower().replace("_", " ").replace("-", " ")
    s = s.replace("–", " ").replace("—", " ")   # en/em dash, as in '3–6 months'
    return " ".join(s.split())


def _canon_map(quiz):
    """{slug: {folded_value: canonical_label}} built from the account's own option labels."""
    return {slug: {_fold(lbl): lbl for lbl in labels} for slug, labels in (quiz or {}).items()}


def canonical(canon, slug, value):
    """Map one raw answer onto the account's canonical label; unknown values pass through
    title-cased so a new option shows up as itself rather than vanishing."""
    v = (value or "").strip()
    if not v:
        return None
    folded = _fold(v)
    hit = (canon.get(slug) or {}).get(folded)
    if hit:
        return hit
    return v if v[:1].isupper() else folded.capitalize()


# --- leg A: contacts + quiz answers + engagement counters --------------------
def _fetch_contacts(base, key, quiz):
    """Every contact, hashed, with its quiz answers and engagement counters.

    `include=fieldValues` returns the answers alongside the page (~305 per 100 contacts), so the
    whole quiz cross-tab costs one 49-page pass instead of a per-contact fan-out. The Colab spent
    50 threads and ~275k requests to get less than this.
    """
    canon = _canon_map(quiz)
    rows, offset, total = [], 0, 0
    for _ in range(MAX_CONTACT_PAGES):
        payload = _get(base, key, "contacts",
                       {"limit": _PAGE, "offset": offset, "include": "fieldValues",
                        "orders[cdate]": "ASC"})
        batch = payload.get("contacts") or []
        total = _meta_total(payload) or total
        # field values arrive as a sibling list keyed by contact id
        answers = {}
        for fv in (payload.get("fieldValues") or []):
            slug = QUIZ_FIELDS.get(str(fv.get("field")))
            val = (fv.get("value") or "").strip()
            if slug and val:
                answers.setdefault(str(fv.get("contact")), {})[slug] = val
        for c in batch:
            cid = str(c.get("id"))
            a = answers.get(cid, {})
            status = canonical(canon, "status", a.get("status"))
            opened = _iso_date(c.get("last_open_date")) or None
            clicked = _iso_date(c.get("last_click_date")) or None
            rows.append({
                "cid": _hash(c.get("email")) or _hash(cid),
                "cdate": _iso_date(c.get("cdate")),
                "domain": (c.get("email_domain") or "").lower() or None,
                "sent": _num(c.get("sentcnt")),
                "opened": opened,
                "clicked": clicked,
                "bounced": _num(c.get("bounced_hard")) + _num(c.get("bounced_soft")),
                "experience": canonical(canon, "experience", a.get("experience")),
                "timeline": canonical(canon, "timeline", a.get("timeline")),
                "first_time": canonical(canon, "first_time", a.get("first_time")),
                "status": status,
                "pathway": canonical(canon, "pathway", a.get("pathway")),
                "campaign": redact(a.get("campaign")).strip() or None,
                # An ENGAGED lead has opened at least one email. This is the behavioural proxy the
                # brief asked for ("Cost per Engaged Lead"), and it is what the dashboard leads
                # with while `converted` is still all-zero across the account.
                "engaged": 1 if opened else 0,
                "converted": 1 if (status or "").lower() in CONVERTED_STATUSES else 0,
            })
        offset += _PAGE
        if len(batch) < _PAGE or (total and offset >= total):
            break
    return rows, total or len(rows)


# --- leg C: sends (incremental, newest-first to a watermark) -----------------
def _fetch_sends(base, key, since):
    """Per-send rows newer than `since`, paged newest-first.

    `/emailActivities` ignores `filters[tstamp][gt]` but honours `orders[tstamp]=DESC`, so the
    only way to be incremental is to page from the newest and stop once we cross the watermark.
    Returns (rows, newest_ts, hit_cap).
    """
    rows, offset, newest, hit_cap = [], 0, "", True
    for _ in range(MAX_SEND_PAGES):
        try:
            payload = _get(base, key, "emailActivities",
                           {"limit": _PAGE, "offset": offset, "orders[tstamp]": "DESC"})
        except Exception as e:  # noqa: BLE001
            # Keep every page already fetched rather than losing the leg to a late failure.
            # The watermark is NOT advanced past what we actually read, so the next run resumes.
            print("  ac: send crawl stopped early at offset %d (%s) — keeping %d rows"
                  % (offset, str(e)[:90], len(rows)))
            return rows, "", True
        batch = payload.get("emailActivities") or []
        if not batch:
            hit_cap = False
            break
        time.sleep(_PAGE_PAUSE)
        stop = False
        for r in batch:
            ts = str(r.get("tstamp") or "")
            if not ts:
                continue
            newest = max(newest, ts)
            if since and ts <= since:
                stop = True
                continue
            rows.append({
                "d": ts[:10],
                "cid": _hash(r.get("toAddress")) or _hash(r.get("subscriberid")),
                "tpl": template_of(r.get("subject")),
            })
        offset += _PAGE
        if stop or len(batch) < _PAGE:
            hit_cap = False
            break
    return rows, newest, hit_cap


# --- leg D: opens / clicks (incremental via `after=`) ------------------------
def _fetch_events(base, key, since):
    """Open/click events, paged NEWEST-FIRST and stopped at the watermark.

    `/activities` ignores every `filters[...]` form (all return the full 220,897) but it DOES
    honour `orders[tstamp]=DESC`, which is what makes this correct. Two earlier shapes both
    published a dashboard whose default view showed zero opens:

      1. offset-0 paging: `/activities` is oldest-first by default, so a page-capped run returned
         events from 2025 and nothing recent.
      2. `after=<cold-start floor>` paging: still oldest-first *within* that window, so 600 pages
         covered 2026-03-29 → 2026-05-28 and the latest two months were empty.

    Paging newest-first fixes it definitively: the recent window is always covered, and the page
    cap only limits how far BACK a cold start reaches. `oldest` is returned so the caller can
    report the horizon honestly.

    Returns (rows, newest_ts, oldest_ts, hit_cap).
    """
    rows, offset, newest, oldest, hit_cap = [], 0, "", "", True
    floor = ""
    if not since and EVENT_FIRST_DAYS > 0:
        floor = (datetime.datetime.now(datetime.timezone.utc)
                 - datetime.timedelta(days=EVENT_FIRST_DAYS)).strftime("%Y-%m-%dT%H:%M:%S-05:00")
        print("  ac: cold start — walking back from newest, floor %s" % floor[:10])
    for _ in range(MAX_EVENT_PAGES):
        try:
            payload = _get(base, key, "activities",
                           {"limit": _PAGE, "offset": offset, "orders[tstamp]": "DESC"})
        except Exception as e:  # noqa: BLE001
            # A 503 partway through must not discard the pages we already have. The first live
            # run lost the ENTIRE events leg to one 503.
            print("  ac: event crawl stopped early at offset %d (%s) — keeping %d events"
                  % (offset, str(e)[:90], len(rows)))
            return rows, newest, oldest, True
        batch = payload.get("activities") or []
        if not batch:
            hit_cap = False
            break
        time.sleep(_PAGE_PAUSE)
        stop = False
        for r in batch:
            ts = str(r.get("tstamp") or "")
            if not ts:
                continue
            if since and ts <= since:
                stop = True                    # caught up with the previous run
                continue
            if floor and ts < floor:
                stop = True                    # cold-start horizon reached
                continue
            newest = max(newest, ts) if newest else ts
            oldest = min(oldest, ts) if oldest else ts
            kind = EVENT_KINDS.get(str(r.get("referenceModelName")))
            if not kind:
                continue                       # automation start/complete, list subscribe
            rows.append({"d": ts[:10], "k": kind, "cid": _hash(r.get("subscriberid"))})
        offset += _PAGE
        if stop or len(batch) < _PAGE:
            hit_cap = False
            break
    return rows, newest, oldest, hit_cap


def _crm_enabled(base, key):
    """Deals require a CRM upgrade — a 403 means CRM is off. Best-effort True/False."""
    try:
        _get(base, key, "deals", {"limit": 1})
        return True
    except urllib.error.HTTPError as e:
        return e.code != 403
    except Exception:  # noqa: BLE001
        return False


# --- merge + rollup ----------------------------------------------------------
def _merge_daily(fresh_sends, fresh_events, previous):
    """Fold this run's rows into the previous publication's per-day series.

    The fresh pull is authoritative for every day it covers; older days are carried forward
    verbatim, so history grows in the bucket instead of being capped by the crawl bound.
    """
    def blank(d):
        return {"d": d, "sends": 0, "opens": 0, "mpp": 0, "v": DAILY_VERSION}

    daily = {}
    for row in ((previous or {}).get("daily") or []):
        d = row.get("d")
        if not d:
            continue
        cur = blank(d)
        cur["sends"] = _num(row.get("sends"))          # sends were always correct
        if _num(row.get("v")) >= DAILY_VERSION:
            cur["opens"] = _num(row.get("opens"))
            cur["mpp"] = _num(row.get("mpp"))
        # else: engagement counts from an earlier DAILY_VERSION were derived from a wrong event
        # classification and are DISCARDED. `fetch()` also clears the events watermark on
        # migration so the corrected classification re-pulls them.
        daily[d] = cur
    for r in fresh_sends:
        daily.setdefault(r["d"], blank(r["d"]))["sends"] += 1
    for r in fresh_events:
        cur = daily.setdefault(r["d"], blank(r["d"]))
        cur[{"open": "opens", "mpp": "mpp"}[r["k"]]] += 1
    return sorted(daily.values(), key=lambda x: x["d"])


def _merge_templates(fresh_sends, previous):
    tpl = {}
    for row in ((previous or {}).get("templates") or []):
        # Re-run the redaction over CARRIED-FORWARD names too: a publication written before the
        # redaction existed still holds the raw subject, and merging it forward would keep
        # re-publishing the leak. Two names that collapse to the same redacted form are summed.
        n = template_of(row.get("name"))
        if n and n != "(no subject)":
            cur = tpl.get(n)
            if cur is None:
                tpl[n] = {"name": n, "sends": _num(row.get("sends")),
                          "first": row.get("first") or "", "last": row.get("last") or ""}
            else:
                cur["sends"] += _num(row.get("sends"))
                f, l = row.get("first") or "", row.get("last") or ""
                cur["first"] = min(cur["first"], f) if (cur["first"] and f) else (cur["first"] or f)
                cur["last"] = max(cur["last"], l) if (cur["last"] and l) else (cur["last"] or l)
    for r in fresh_sends:
        t = tpl.setdefault(r["tpl"], {"name": r["tpl"], "sends": 0, "first": "", "last": ""})
        t["sends"] += 1
        t["first"] = min(t["first"], r["d"]) if t["first"] else r["d"]
        t["last"] = max(t["last"], r["d"]) if t["last"] else r["d"]
    out = sorted(tpl.values(), key=lambda x: -x["sends"])
    return out


def fetch(previous=None):
    """Assemble the `email` block. Never raises — returns {enabled:False,error:...} on failure."""
    base, key = _base_url(), _api_key()
    if not base or not key:
        return {"enabled": False, "error": "ActiveCampaign not configured (URL/key missing)",
                "totals": {}, "campaigns": [], "lists": [], "automations": [],
                "contacts": [], "daily": [], "templates": [], "quiz": {}, "watermark": {}}

    prev = previous or {}
    full = os.environ.get("AC_FULL_SYNC") == "1"
    wm = {} if full else dict(prev.get("watermark") or {})
    # Migration: a publication written before DAILY_VERSION 2 holds engagement counts derived from
    # the `log`-as-open misclassification. `_merge_daily` drops them, and the events watermark is
    # cleared here so the corrected classification re-pulls them. Sends are untouched (they were
    # always right), so this costs one event crawl, not the 543-page send crawl.
    _pd = prev.get("daily") or []
    if _pd and _num(_pd[0].get("v")) < DAILY_VERSION:
        print("  ac: migrating daily series v%s -> v%d — re-pulling engagement events"
              % (_pd[0].get("v") or 1, DAILY_VERSION))
        wm["events"] = ""
    # A full sync re-reads rows the previous publication ALREADY counted, so its per-day series
    # must be discarded rather than merged — otherwise every full run doubles the totals
    # (caught in the first local verification: 6,000 sends became 12,000).
    carry = {} if full else prev

    out = {
        "enabled": True, "account": _account_name(base), "url": base,
        "fetched": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "crm_enabled": False, "error": "", "partial": False,
        "totals": {"contacts": 0, "campaigns": 0, "campaigns_sent": 0, "lists": 0,
                   "automations": 0, "sends": 0, "opens": 0, "mpp": 0,
                   "engaged": 0, "converted": 0},
        "campaigns": [], "lists": [], "automations": [], "contacts": [],
        "daily": [], "templates": [], "quiz": {}, "watermark": {},
    }
    errs = []

    try:
        out["quiz"] = _fetch_quiz_options(base, key)
    except Exception as e:  # noqa: BLE001
        errs.append("quiz options: %s" % str(e)[:80])

    try:
        contacts, ctotal = _fetch_contacts(base, key, out["quiz"])
        out["contacts"] = contacts
        out["totals"]["contacts"] = ctotal or len(contacts)
        out["totals"]["converted"] = sum(c["converted"] for c in contacts)
        out["totals"]["engaged"] = sum(c["engaged"] for c in contacts)
        print("  ac: %d contacts (%d engaged, %d converted)"
              % (len(contacts), out["totals"]["engaged"], out["totals"]["converted"]))
    except Exception as e:  # noqa: BLE001
        errs.append("contacts: %s" % str(e)[:80])

    try:
        rows, sent_total = _fetch_campaigns(base, key)
        out["campaigns"] = rows
        out["totals"]["campaigns_sent"] = sent_total or len(rows)
        out["totals"]["campaigns"] = _meta_total(_get(base, key, "campaigns", {"limit": 1}))
        print("  ac: %d sent campaigns of %d" % (len(rows), out["totals"]["campaigns"]))
    except Exception as e:  # noqa: BLE001
        errs.append("campaigns: %s" % str(e)[:80])

    try:
        lists, ltot = _fetch_lists(base, key)
        out["lists"], out["totals"]["lists"] = lists, ltot or len(lists)
    except Exception as e:  # noqa: BLE001
        errs.append("lists: %s" % str(e)[:80])

    try:
        autos, atot = _fetch_automations(base, key)
        out["automations"], out["totals"]["automations"] = autos, atot or len(autos)
    except Exception as e:  # noqa: BLE001
        errs.append("automations: %s" % str(e)[:80])

    fresh_sends, fresh_events = [], []
    try:
        fresh_sends, newest, capped = _fetch_sends(base, key, wm.get("sends"))
        out["watermark"]["sends"] = newest or wm.get("sends") or ""
        out["partial"] = out["partial"] or capped
        print("  ac: %d new sends%s" % (len(fresh_sends), " (hit page cap)" if capped else ""))
    except Exception as e:  # noqa: BLE001
        errs.append("sends: %s" % str(e)[:80])
        out["watermark"]["sends"] = wm.get("sends") or ""
    try:
        fresh_events, newest, oldest, capped = _fetch_events(base, key, wm.get("events"))
        out["watermark"]["events"] = newest or wm.get("events") or ""
        # the oldest event this account has ever reached back to — never moves forward
        prev_old = (prev.get("watermark") or {}).get("events_oldest") or ""
        out["watermark"]["events_oldest"] = (min(prev_old, oldest) if (prev_old and oldest)
                                             else (oldest or prev_old))
        out["partial"] = out["partial"] or capped
        print("  ac: %d new open events%s%s"
              % (len(fresh_events), " (hit page cap)" if capped else "",
                 (" back to " + oldest[:10]) if oldest else ""))
    except Exception as e:  # noqa: BLE001
        errs.append("events: %s" % str(e)[:80])
        out["watermark"]["events"] = wm.get("events") or ""
        out["watermark"]["events_oldest"] = (prev.get("watermark") or {}).get("events_oldest") or ""

    out["daily"] = _merge_daily(fresh_sends, fresh_events, carry)
    out["templates"] = _merge_templates(fresh_sends, carry)
    out["totals"]["sends"] = sum(d["sends"] for d in out["daily"])
    out["totals"]["opens"] = sum(d["opens"] for d in out["daily"])
    out["totals"]["mpp"] = sum(d["mpp"] for d in out["daily"])
    # The true open rate is campaign-level (uniqueopens / send_amt) — there is no per-day open
    # event. These two are the authoritative broadcast aggregates the dashboard reports on.
    out["totals"]["campaign_sent"] = sum(c["sent"] for c in out["campaigns"])
    out["totals"]["campaign_opens"] = sum(c["opens"] for c in out["campaigns"])
    out["totals"]["campaign_clicks"] = sum(c["clicks"] for c in out["campaigns"])

    out["crm_enabled"] = _crm_enabled(base, key)
    if errs:
        out["error"] = "; ".join(errs)
    return out


if __name__ == "__main__":
    import sys
    blk = fetch()
    body = json.dumps(blk, indent=2)
    dst = os.environ.get("AC_LOCAL_OUT")
    if dst:
        with open(dst, "wb") as fh:                 # bytes, never text mode (playbook 3.7)
            fh.write(body.encode("utf-8"))
        print("wrote %s (enabled=%s, %d contacts, %d campaigns, %d days)"
              % (dst, blk.get("enabled"), len(blk.get("contacts", [])),
                 len(blk.get("campaigns", [])), len(blk.get("daily", []))), file=sys.stderr)
    else:
        print(body)
