"""Stage-2 builder for the Campaign Uptime Monitor, emits the THREE scoped JSON payloads.

WHAT THIS IS
    The handover brief (`INTO/s7000_ into.docx`) specifies one pipeline with three scoped
    outputs: `internal.json` (both accounts), `into.json` (INTO only) and `service7000.json`
    (Service 7000 only). The scoping happens HERE, server-side, never in the browser. That is
    the single most important technical requirement in the brief: each file contains only that
    client's rows, so a client opening devtools cannot see the other client's spend.

    Until the Windsor pulls are wired (blocked on §5/§10 of the brief, the conversion action is
    unresolved and the shared API key must be rotated into Secret Manager first), this builder
    generates a SYNTHETIC dataset in exactly the shape the live job must emit. Every payload it
    writes carries `"demo": true`, and the dashboard renders a standing "Demo data" ribbon
    whenever that flag is set, nobody can mistake these numbers for the client's.

    The dataset is deliberately built to exercise EVERY state the dashboard has to render, so
    the QA checklist in §11 of the brief can be walked without waiting for a live pull:
      · a campaign that is ACTIVE but not delivering because its ad set is paused (the silent
        failure the client is complaining about)
      · a campaign switched off six days ago, with the change log entry and lost-delivery estimate
      · a lifetime budget down to 7% remaining (imminent silent stop)
      · a DAILY-budget campaign whose `budget_remaining` is 9%, which must NOT be flagged, because
        on a daily budget that number resets every midnight (§4's trap)
      · an end date nine days out (critical) and one 41 days out (warning)
      · a Traffic campaign with no measurable conversion, which must read "not tracked in Meta"
        rather than a zero that looks like failure
      · under-delivery against daily budget, a spend collapse, and creative fatigue
      · `--stale`, which fakes a broken pull so you can confirm the dashboard reports a PIPELINE
        failure rather than "all campaigns died"

USAGE
    python clients/client_S7000/job/build_local.py                 # writes all three payloads
    python clients/client_S7000/job/build_local.py --stale         # + a broken/late pull
    python clients/client_S7000/job/build_local.py --out some/dir

CONTRACT
    The keys written here are exactly the keys `dash/dashboard.html` reads. Two stages:

        job/build_local.py (data dict key)  ->  dash/dashboard.html (DATA.* key)

    Renaming a key in one stage breaks the other. The full field list is in ../README.md.
"""

import argparse
import base64
import datetime
import json
import os
import random
import sys

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover, py<3.9 is not a supported dev box here
    ZoneInfo = None

_HERE = os.path.dirname(os.path.abspath(__file__))
_ASSETS = os.path.join(_HERE, "assets")
_DEFAULT_OUT = os.path.join(os.path.dirname(_HERE), "data")

# Locale of record for both accounts (brief: "Locale: CHF · Europe/Zurich").
TZ_NAME = "Europe/Zurich"
CURRENCY = "CHF"

# Seeded so two runs of the demo differ only in their timestamps. A moving dataset makes a
# design review impossible: you cannot tell a rendering change from a data change.
SEED = 70002026


# ==================================================================== the two accounts (§2)
ACCOUNTS = [
    {
        "key": "into",
        "id": "facebook__1395577394904072",
        "name": "INTO Schüleraustausch",
        "business": "Swiss student exchange, Chur. High-school programs in the USA, Canada, "
                    "Australia, New Zealand, Japan and Europe.",
        "purpose": "Reach students and parents",
        # Recruitment vs lead-gen is not cosmetic: it names the conversion everywhere (§2).
        "conversion_label": "Leads",
        "conversion_plural": "leads",
        "conversion_basis": "actions",          # actions_lead, events, not people (§8)
        "reporting_timezone": TZ_NAME,
        # Still unverified in Ads Manager (§2 build note). Meta sets this at account creation and
        # agency-created accounts are often UTC or America/Los_Angeles; if it is not Europe/Zurich
        # the 24-hour heartbeat throws false alarms around midnight. The dashboard shows the doubt
        # rather than hiding it.
        "timezone_verified": False,
        "payment_issue": False,
        "disabled": False,
    },
    {
        "key": "service7000",
        "id": "facebook__1721606312060475",
        "name": "Service 7000 AG",
        "business": "Swiss household-appliance repair, sales and installation, Netstal.",
        "purpose": "Hiring, recruitment, not service promotion",
        # Never let this account inherit a service-lead template (§2).
        "conversion_label": "Applications",
        "conversion_plural": "applications",
        "conversion_basis": "unique_actions",   # unique_actions_lead, people, not events (§8)
        "reporting_timezone": TZ_NAME,
        "timezone_verified": False,
        "payment_issue": False,
        "disabled": False,
    },
]

# ==================================================================== flag thresholds (§4)
# Config-driven, not hardcoded, so they are tunable without a rebuild. The dashboard reads these
# and evaluates the display rules from them; a live Function must use this SAME block so the
# rendered verdict and any future alert email can never disagree.
THRESHOLDS = {
    "heartbeat_hours": 24,
    # Around midnight in the account timezone a campaign legitimately has not spent yet today.
    # Inside this many hours of local midnight the heartbeat also accepts yesterday's spend,
    # which is what stops the "everything died at 00:05" class of false alarm (§8, timezone).
    "heartbeat_grace_hours": 6,
    "end_date_critical_days": 14,
    "end_date_horizon_days": 30,
    "budget_remaining_critical_pct": 10,
    "underdelivery_pct": 50,
    "underdelivery_days": 2,
    "spend_drop_pct": 40,
    "frequency_warn": 3.5,
    "recent_changes_days": 7,
}

# ==================================================================== the campaign specs
# `profile` drives the synthetic delivery curve; everything else is the campaign's real config
# shape. Campaign names stay German: the UI is English, the campaigns are not (brief header).
SPECS = [
    # ---------------------------------------------------------------- INTO
    {
        "account": "into",
        "name": "INTO | Leads | Highschool USA 26/27 | Eltern",
        "objective": "OUTCOME_LEADS",
        "conversion_tracked": True,
        "budget_type": "daily", "daily_budget": 45.0,
        "start_time": "2025-09-15", "end_time": None,
        "profile": "steady", "base_spend": 43.0, "cpm": 9.4, "ctr": 1.35, "cvr": 5.2,
        "adsets": [
            {"name": "Eltern 40-60 | DE-CH | Interesse Bildung", "status": "ACTIVE",
             "ads": [("Video | Erfahrungsbericht Familie", "ACTIVE"),
                     ("Static | Infobroschüre", "ACTIVE")]},
            {"name": "Eltern 40-60 | Lookalike 2% Leads", "status": "ACTIVE",
             "ads": [("Carousel | 6 Länder", "ACTIVE")]},
        ],
    },
    {
        "account": "into",
        "name": "INTO | Leads | Highschool USA 26/27 | Schüler 15-18",
        "objective": "OUTCOME_LEADS",
        "conversion_tracked": True,
        "budget_type": "daily", "daily_budget": 60.0,
        "start_time": "2025-09-15", "end_time": None,
        "profile": "steady", "base_spend": 57.5, "cpm": 7.1, "ctr": 1.9, "cvr": 3.4,
        "adsets": [
            {"name": "Schüler 15-18 | Reels | Broad", "status": "ACTIVE",
             "ads": [("Reel | Ein Jahr USA in 30s", "ACTIVE"),
                     ("Reel | Gastfamilie Vlog", "ACTIVE"),
                     ("Reel | Alte Version Q1", "PAUSED")]},
        ],
    },
    {
        "account": "into",
        "name": "INTO | Leads | Kanada & Neuseeland | Broad DE-CH",
        "objective": "OUTCOME_LEADS",
        "conversion_tracked": True,
        "budget_type": "daily", "daily_budget": 35.0,
        "start_time": "2025-11-03", "end_time": None,
        "profile": "steady", "base_spend": 33.4, "cpm": 8.2, "ctr": 1.42, "cvr": 4.1,
        "adsets": [
            {"name": "Broad DE-CH 25-55", "status": "ACTIVE",
             "ads": [("Static | Kanada Herbst", "ACTIVE"),
                     ("Static | Neuseeland Sommer", "ACTIVE")]},
        ],
    },
    {
        # THE flagship failure: campaign reports ACTIVE, the ad set beneath it is paused, so it
        # delivers nothing while every status field reads green. Exactly the failure the client
        # is complaining about (§3).
        "account": "into",
        "name": "INTO | Leads | Austauschjahr Japan | Interesse",
        "objective": "OUTCOME_LEADS",
        "conversion_tracked": True,
        "budget_type": "daily", "daily_budget": 25.0,
        "start_time": "2026-02-01", "end_time": None,
        "profile": "off_since", "off_days": 3, "base_spend": 24.1, "cpm": 10.8,
        "ctr": 1.18, "cvr": 3.9,
        "adsets": [
            {"name": "Japan | Interesse Sprachen & Kultur", "status": "PAUSED",
             "paused_days_ago": 3,
             "ads": [("Static | Japan Schuluniform", "ACTIVE"),
                     ("Video | Tokio Gastfamilie", "ACTIVE")]},
        ],
    },
    {
        # Lifetime budget down to 7%: a scheduled silent stop with no end date set (§4 rules 5+6).
        "account": "into",
        "name": "INTO | Leads | Infoabend Chur | Lead Form",
        "objective": "OUTCOME_LEADS",
        "conversion_tracked": True,
        "budget_type": "lifetime", "lifetime_budget": 1200.0, "budget_remaining": 84.0,
        "start_time": "2026-06-08", "end_time": None,
        "profile": "steady", "base_spend": 27.5, "cpm": 6.4, "ctr": 2.05, "cvr": 7.8,
        "adsets": [
            {"name": "Chur + 30km | Eltern & Schüler", "status": "ACTIVE",
             "budget_type": "lifetime", "lifetime_budget": 1200.0, "budget_remaining": 84.0,
             "ads": [("Static | Infoabend 14. August", "ACTIVE")]},
        ],
    },
    {
        # An end date nine days out (critical) AND a Traffic objective, so there is no lead action
        # to read: the dashboard must say "not tracked in Meta", never a zero (§5, §7.4).
        "account": "into",
        "name": "INTO | Traffic | Blog Retargeting | Website",
        "objective": "OUTCOME_TRAFFIC",
        "conversion_tracked": False,
        "conversion_note": "Traffic objective, Meta has no lead action to read. Link clicks and "
                           "landing-page views are the only honest metrics here.",
        "budget_type": "daily", "daily_budget": 20.0,
        "start_time": "2026-05-20", "end_time": "+9",
        "profile": "steady", "base_spend": 18.9, "cpm": 4.8, "ctr": 2.6, "cvr": 0.0,
        "adsets": [
            {"name": "Website-Besucher 180 Tage", "status": "ACTIVE", "end_time": "+9",
             "ads": [("Static | Jetzt bewerben", "ACTIVE")]},
        ],
    },
    {
        # Switched off six days ago. Nothing is meant to be paused, so this is unexpected by
        # definition and always flagged (§4). It also carries the change-log entry and the
        # lost-delivery estimate.
        "account": "into",
        "name": "INTO | Leads | Sommerprogramme Europa",
        "objective": "OUTCOME_LEADS",
        "conversion_tracked": True,
        "status": "PAUSED", "paused_days_ago": 6,
        "budget_type": "daily", "daily_budget": 30.0,
        "start_time": "2026-03-11", "end_time": None,
        "profile": "off_since", "off_days": 6, "base_spend": 28.8, "cpm": 7.7,
        "ctr": 1.51, "cvr": 4.4,
        "adsets": [
            {"name": "Sommer 2026 | 14-17 | DE-CH", "status": "PAUSED", "paused_days_ago": 6,
             "ads": [("Static | Sommercamp England", "ACTIVE")]},
        ],
    },
    {
        # Under-delivering against its daily budget for days, with frequency climbing and CTR
        # falling: the small-audience fatigue the brief warns about (§2, rules 7+9).
        "account": "into",
        "name": "INTO | Leads | Elternabend Zürich | Interesse",
        "objective": "OUTCOME_LEADS",
        "conversion_tracked": True,
        "budget_type": "daily", "daily_budget": 40.0,
        "start_time": "2026-04-22", "end_time": None,
        "profile": "underdeliver", "base_spend": 14.5, "cpm": 13.9, "ctr": 1.05, "cvr": 2.6,
        "fatigue": True,
        "adsets": [
            {"name": "Zürich 20km | Eltern | Interesse Auslandjahr", "status": "ACTIVE",
             "ads": [("Static | Elternabend Zürich", "ACTIVE"),
                     ("Static | Elternabend Zürich v2", "ACTIVE")]},
        ],
    },
    # ---------------------------------------------------------------- Service 7000
    {
        "account": "service7000",
        "name": "S7000 | Recruiting | Servicetechniker Glarus",
        "objective": "OUTCOME_LEADS",
        "conversion_tracked": True,
        "budget_type": "daily", "daily_budget": 30.0,
        "start_time": "2026-01-12", "end_time": None,
        "profile": "steady", "base_spend": 28.6, "cpm": 11.2, "ctr": 1.22, "cvr": 2.8,
        "adsets": [
            {"name": "Glarus + 40km | Handwerk & Technik", "status": "ACTIVE",
             "ads": [("Video | Ein Tag im Service", "ACTIVE"),
                     ("Static | Servicetechniker gesucht", "ACTIVE")]},
        ],
    },
    {
        "account": "service7000",
        "name": "S7000 | Recruiting | Kundendienst-Monteur | Region ZH",
        "objective": "OUTCOME_LEADS",
        "conversion_tracked": True,
        "budget_type": "daily", "daily_budget": 25.0,
        "start_time": "2026-02-24", "end_time": None,
        "profile": "declining", "base_spend": 23.9, "cpm": 12.6, "ctr": 1.09, "cvr": 2.1,
        # A one-day delivery gap nine days back: the ad rejection below took the whole ad set out
        # for a day before a replacement was approved. Far enough back not to touch the heartbeat
        # or the trailing-7 average, so it exercises the delivery RECORD (a red cell, an uptime
        # percentage under 100) on the client-facing route without inventing a second live alarm.
        "gaps": [9],
        "adsets": [
            {"name": "Zürich + Winterthur | Monteure", "status": "ACTIVE",
             "ads": [("Static | Monteur Waschmaschinen", "ACTIVE"),
                     ("Static | Lohn & Firmenauto", "REJECTED")]},
        ],
    },
    {
        # End date 41 days out: a misconfiguration, but not imminent: warning, not critical (§4.4).
        "account": "service7000",
        "name": "S7000 | Recruiting | Lehrstelle 2027",
        "objective": "OUTCOME_LEADS",
        "conversion_tracked": True,
        "budget_type": "daily", "daily_budget": 18.0,
        "start_time": "2026-06-01", "end_time": "+41",
        "profile": "steady", "base_spend": 17.2, "cpm": 6.9, "ctr": 1.74, "cvr": 3.6,
        "adsets": [
            {"name": "Glarus/SG | 15-19 | Schulabgänger", "status": "ACTIVE", "end_time": "+41",
             "ads": [("Reel | Lehre bei Service 7000", "ACTIVE")]},
        ],
    },
    {
        # Recruitment run as Traffic to the careers page: per §5 this is common, and it means
        # "Applications" is not measurable in Meta at all.
        "account": "service7000",
        "name": "S7000 | Traffic | Jobs-Seite",
        "objective": "OUTCOME_TRAFFIC",
        "conversion_tracked": False,
        "conversion_note": "Runs as Traffic to the careers page, so Meta never sees the "
                           "application. Confirm with the client what counts as an application "
                           "before this column can mean anything (§10).",
        "budget_type": "daily", "daily_budget": 15.0,
        "start_time": "2026-03-02", "end_time": None,
        "profile": "steady", "base_spend": 14.3, "cpm": 5.2, "ctr": 2.35, "cvr": 0.0,
        "adsets": [
            {"name": "Broad GL/SG/ZH 20-55", "status": "ACTIVE",
             "ads": [("Static | Offene Stellen", "ACTIVE")]},
        ],
    },
    {
        # Lifetime budget on the AD SET, not the campaign: the hierarchy matters, so the
        # scheduled-stops panel has to look at both levels (§4.5, §7.2).
        "account": "service7000",
        "name": "S7000 | Recruiting | Verkaufsberater Haushaltgeräte",
        "objective": "OUTCOME_LEADS",
        "conversion_tracked": True,
        "budget_type": "adset",
        "start_time": "2026-05-05", "end_time": None,
        "profile": "steady", "base_spend": 16.8, "cpm": 9.8, "ctr": 1.31, "cvr": 2.4,
        "adsets": [
            {"name": "Verkauf | Detailhandel-Erfahrung", "status": "ACTIVE",
             "budget_type": "lifetime", "lifetime_budget": 800.0, "budget_remaining": 341.0,
             "ads": [("Static | Verkaufsberater gesucht", "ACTIVE")]},
        ],
    },
    {
        # THE TRAP (§4): `budget_remaining` is 1.80 of an 18.00 DAILY budget: 10%. On a lifetime
        # budget that is a critical imminent stop; on a daily budget it is 4pm on a normal
        # Tuesday and resets at midnight. If the dashboard flags this, the rule is wrong.
        "account": "service7000",
        "name": "S7000 | Recruiting | Monteur Waschmaschinen | Interessen",
        "objective": "OUTCOME_LEADS",
        "conversion_tracked": True,
        "budget_type": "daily", "daily_budget": 18.0, "budget_remaining": 1.80,
        "start_time": "2026-04-14", "end_time": None,
        "profile": "steady", "base_spend": 16.2, "cpm": 10.4, "ctr": 1.28, "cvr": 2.6,
        "adsets": [
            {"name": "Interessen | Haushaltgeräte & Reparatur", "status": "ACTIVE",
             "budget_type": "daily", "daily_budget": 18.0, "budget_remaining": 1.80,
             "ads": [("Static | Monteur werden", "ACTIVE")]},
        ],
    },
]

DAYS = 90            # how much daily history each payload carries
PULL_HISTORY = 36    # hourly status pulls shown in the freshness strip


# ==================================================================== helpers
def _b64(path, mime):
    """Inline an asset as a data URI, or None when it is missing.

    Same posture as every other dashboard here: the deployed container only bundles `dash/`, so
    logos travel inside the JSON rather than as separate requests.
    """
    try:
        with open(path, "rb") as fh:
            return "data:%s;base64,%s" % (mime, base64.b64encode(fh.read()).decode())
    except OSError:
        return None


def _tz():
    """Europe/Zurich, or a fixed CEST offset when the IANA database is not installed.

    A bare Windows CPython has no `tzdata` package, so `zoneinfo` cannot resolve
    "Europe/Zurich" and raises. That is only ever a local-dev condition (the Cloud Run base image
    carries the system zone files, and `tzdata` is pinned in requirements.txt), and all this clock
    decides is which local DAY the synthetic rows land on, so degrade to a fixed +02:00 rather
    than refuse to build. The dashboard itself formats every timestamp with `Intl`, which knows
    Europe/Zurich in every browser regardless of what Python has.
    """
    if ZoneInfo is not None:
        try:
            return ZoneInfo(TZ_NAME)
        except Exception:  # noqa: BLE001, ZoneInfoNotFoundError and friends
            sys.stderr.write("[warn] no IANA tzdata for %s; using a fixed +02:00 (CEST) offset. "
                             "`pip install tzdata` for the real thing.\n" % TZ_NAME)
    return datetime.timezone(datetime.timedelta(hours=2), "CEST")


def _iso_z(dt):
    """UTC instant, second precision, always Z-suffixed."""
    return dt.astimezone(datetime.timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z")


def _rounded(v, nd=2):
    return round(v + 0.0, nd)


# ==================================================================== synthetic delivery
def _daily_rows(spec, local_today, rng):
    """One row per account-timezone day, oldest first. Today's row is PARTIAL by construction.

    Meta reports the current day as it happens, so the last row is always a fraction of a day.
    The dashboard has to know that (it marks it partial and never compares it to a full day), so
    the shape must be honest here too.
    """
    rows = []
    off_days = spec.get("off_days", 0)
    profile = spec.get("profile", "steady")
    base = spec.get("base_spend", 20.0)
    cpm = spec.get("cpm", 9.0)
    ctr = spec.get("ctr", 1.3)
    cvr = spec.get("cvr", 3.0)
    # Fraction of the local day already elapsed, so today's partial row is plausible rather
    # than arbitrary. Everything downstream keys off `partial`, not off this number.
    start = _date(spec["start_time"], local_today)

    for i in range(DAYS - 1, -1, -1):
        d = local_today - datetime.timedelta(days=i)
        partial = (i == 0)
        row = {"d": d.isoformat(), "spend": 0.0, "imps": 0, "clicks": 0, "lclk": 0,
               "reach": 0, "freq": 0.0, "conv": 0}
        if start and d < start:
            rows.append(row)
            continue
        if off_days and i < off_days:
            # Genuinely zero: the campaign (or its ad set) stopped delivering off_days ago.
            rows.append(row)
            continue
        if i in spec.get("gaps", ()):
            # A historic one-day outage that has since recovered: it belongs in the delivery
            # record, not in today's alarm list.
            rows.append(row)
            continue

        # weekday shape: Swiss B2C leads soften at the weekend
        dow = d.weekday()
        wk = 0.86 if dow >= 5 else 1.0
        drift = 1.0
        if profile == "declining":
            # a slow slide all period, then a sharp drop over the last two complete days: which
            # is what rule 8 (spend down >40% vs the trailing 7-day average) is looking for
            drift = 1.0 - 0.22 * ((DAYS - 1 - i) / float(DAYS - 1))
            if i <= 2:
                drift *= 0.44
        elif profile == "underdeliver":
            # sits well below its daily budget for the whole trailing stretch
            drift = 1.0 if i > 5 else 0.72
        spend = base * wk * drift * rng.uniform(0.9, 1.1)
        if partial:
            spend *= 0.42
        if spend < 0:
            spend = 0.0

        imps = int(spend / cpm * 1000.0 * rng.uniform(0.94, 1.06))
        this_ctr = ctr
        if spec.get("fatigue"):
            # CTR decays as the tiny audience is worn out; frequency climbs to match
            this_ctr = ctr * (1.0 - 0.42 * ((DAYS - 1 - i) / float(DAYS - 1)))
        lclk = int(imps * this_ctr / 100.0 * rng.uniform(0.92, 1.08))
        clicks = int(lclk * rng.uniform(1.22, 1.44))          # all clicks > link clicks
        freq = 1.14 + 0.10 * rng.random()
        if spec.get("fatigue"):
            freq = 1.9 + 3.4 * ((DAYS - 1 - i) / float(DAYS - 1)) + 0.2 * rng.random()
        reach = int(imps / max(freq, 1.0))
        conv = 0
        if spec.get("conversion_tracked") and cvr > 0:
            conv = int(round(lclk * cvr / 100.0 * rng.uniform(0.7, 1.3)))

        row.update({"spend": _rounded(spend), "imps": imps, "clicks": clicks, "lclk": lclk,
                    "reach": reach, "freq": _rounded(freq, 2), "conv": conv})
        if partial:
            row["partial"] = True
        rows.append(row)
    return rows


def _date(v, local_today):
    """Parse a spec date: an ISO date, a `+N`/`-N` offset from today, or None."""
    if not v:
        return None
    if isinstance(v, str) and (v.startswith("+") or v.startswith("-")):
        return local_today + datetime.timedelta(days=int(v))
    return datetime.date.fromisoformat(v)


def _iso_or_none(d):
    return d.isoformat() if d else None


# ==================================================================== assembly
def build(stale=False):
    rng = random.Random(SEED)
    tz = _tz()
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    now_local = now_utc.astimezone(tz)
    local_today = now_local.date()

    # The pull clock. Every "last 24 hours" judgement in the dashboard is measured from
    # `pull.last_success`, NOT from the browser clock: the data is only ever as current as the
    # pull that produced it, and a laptop with a wrong clock must not be able to invent an outage.
    if stale:
        last_success = now_utc - datetime.timedelta(hours=9, minutes=12)
        last_error = ("Windsor `all` endpoint returned 502 after 3 retries "
                      "(request id 7f3ab1), no rows written.")
    else:
        last_success = now_utc - datetime.timedelta(minutes=6)
        last_error = None

    campaigns = []
    changes = []
    flag_state = {}

    for idx, spec in enumerate(SPECS):
        cid = "2385%07d" % (1000 + idx * 137)
        status = spec.get("status", "ACTIVE")
        rows = _daily_rows(spec, local_today, rng)

        adsets = []
        for j, a in enumerate(spec["adsets"]):
            asid = cid + "%02d" % (j + 1)
            ads = []
            for k, (an, ast) in enumerate(a["ads"]):
                ads.append({
                    "id": asid + "%02d" % (k + 1),
                    "name": an,
                    "status": "ACTIVE" if ast == "ACTIVE" else ast,
                    # Meta's effective_status is where a review rejection actually shows up; the
                    # plain status can still read ACTIVE. The hierarchy view reads this one.
                    "effective_status": {"ACTIVE": "ACTIVE", "PAUSED": "PAUSED",
                                         "REJECTED": "DISAPPROVED"}.get(ast, ast),
                })
            adsets.append({
                "id": asid,
                "name": a["name"],
                "status": a.get("status", "ACTIVE"),
                "effective_status": a.get("status", "ACTIVE"),
                "budget_type": a.get("budget_type", spec.get("budget_type")),
                "daily_budget": a.get("daily_budget", spec.get("daily_budget")),
                "lifetime_budget": a.get("lifetime_budget", spec.get("lifetime_budget")),
                "budget_remaining": a.get("budget_remaining", spec.get("budget_remaining")),
                "start_time": _iso_or_none(_date(a.get("start_time", spec.get("start_time")),
                                                 local_today)),
                "end_time": _iso_or_none(_date(a.get("end_time", spec.get("end_time")),
                                               local_today)),
                "ads": ads,
            })

            # An ad set paused N days ago is a status transition: it belongs in the change log,
            # which is the only thing that can answer "when did this stop and what did it cost".
            if a.get("paused_days_ago"):
                changes.append(_change(spec, cid, "adset", a["name"], "ACTIVE", "PAUSED",
                                       a["paused_days_ago"], rows, now_utc))

        if spec.get("paused_days_ago"):
            changes.append(_change(spec, cid, "campaign", spec["name"], "ACTIVE", "PAUSED",
                                   spec["paused_days_ago"], rows, now_utc))

        campaigns.append({
            "id": cid,
            "account": spec["account"],
            "name": spec["name"],
            "objective": spec["objective"],
            "status": status,
            "effective_status": status,
            # Bootstrapped from the first snapshot: every campaign ACTIVE at first pull is meant
            # to run indefinitely (§4). Stored as editable config, not inferred each run: which
            # is what makes "it was switched off" answerable at all.
            "should_be_active": True,
            "conversion_tracked": bool(spec.get("conversion_tracked")),
            "conversion_note": spec.get("conversion_note"),
            "budget_type": spec.get("budget_type"),
            "daily_budget": spec.get("daily_budget"),
            "lifetime_budget": spec.get("lifetime_budget"),
            "budget_remaining": spec.get("budget_remaining"),
            "start_time": _iso_or_none(_date(spec.get("start_time"), local_today)),
            "end_time": _iso_or_none(_date(spec.get("end_time"), local_today)),
            "adsets": adsets,
            "daily": rows,
        })

    # A resolved transition, so the change log shows both directions rather than only bad news.
    changes.append({
        "ts": _iso_z(now_utc - datetime.timedelta(days=11, hours=3)),
        "account": "service7000",
        "level": "campaign",
        "campaign_id": campaigns[9]["id"],
        "campaign": campaigns[9]["name"],
        "entity": campaigns[9]["name"],
        "from": "ACTIVE", "to": "PAUSED",
        "resolved_ts": _iso_z(now_utc - datetime.timedelta(days=10, hours=6)),
        "hours": 21.0,
        "lost_spend": 20.9,
    })
    # An ad rejected in review: the campaign and ad set stay ACTIVE, so nothing upstream changes
    # colour: this is one of the ways a green dashboard hides a dead campaign (§3).
    changes.append({
        "ts": _iso_z(now_utc - datetime.timedelta(days=4, hours=9)),
        "account": "service7000",
        "level": "ad",
        "campaign_id": campaigns[9]["id"],
        "campaign": campaigns[9]["name"],
        "entity": "Static | Lohn & Firmenauto",
        "from": "ACTIVE", "to": "DISAPPROVED",
        "resolved_ts": None,
        "hours": 105.0,
        "lost_spend": 0.0,
    })
    changes.sort(key=lambda c: c["ts"], reverse=True)

    # first_detected for the flags that need persistence. The dashboard evaluates the RULES
    # itself (one implementation, driven by `thresholds`), but "how long has this been true"
    # cannot be derived from a single snapshot: it has to come from upstream state.
    flag_state["r2:" + campaigns[6]["id"]] = _iso_z(now_utc - datetime.timedelta(days=6))
    flag_state["r1:" + campaigns[3]["id"]] = _iso_z(now_utc - datetime.timedelta(days=3))
    flag_state["r6:" + campaigns[4]["id"]] = _iso_z(now_utc - datetime.timedelta(days=2, hours=4))
    flag_state["r7:" + campaigns[7]["id"]] = _iso_z(now_utc - datetime.timedelta(days=5))
    flag_state["r9:" + campaigns[7]["id"]] = _iso_z(now_utc - datetime.timedelta(days=8))
    flag_state["r4:" + campaigns[5]["id"]] = _iso_z(now_utc - datetime.timedelta(days=21))
    flag_state["r4:" + campaigns[10]["id"]] = _iso_z(now_utc - datetime.timedelta(days=17))
    flag_state["r5:" + campaigns[4]["id"]] = _iso_z(now_utc - datetime.timedelta(days=50))
    flag_state["r5:" + campaigns[12]["id"]] = _iso_z(now_utc - datetime.timedelta(days=33))
    flag_state["r8:" + campaigns[9]["id"]] = _iso_z(now_utc - datetime.timedelta(days=2))

    # Hourly status-pull history: the freshness strip. One tick per expected pull, so a gap is
    # visible as a gap rather than having to be inferred from a single "last updated" line.
    history = []
    for i in range(PULL_HISTORY - 1, -1, -1):
        ts = last_success - datetime.timedelta(hours=i)
        ok = True
        rows_n = 1180 + rng.randint(-40, 40)
        if i == 14:                     # one real historic blip, so "all green" means something
            ok, rows_n = False, 0
        history.append({"ts": _iso_z(ts), "ok": ok, "rows": rows_n})
    if stale:
        # the failing attempts SINCE the last success: this is what rule 10 is looking at
        for i in range(1, 10):
            history.append({"ts": _iso_z(last_success + datetime.timedelta(hours=i)),
                            "ok": False, "rows": 0})

    return {
        "generated_at": _iso_z(now_utc),
        "demo": True,
        "currency": CURRENCY,
        "locale": "de-CH",
        "timezone": TZ_NAME,
        "thresholds": THRESHOLDS,
        "pull": {
            "last_success": _iso_z(last_success),
            "expected_every_minutes": 60,
            # How late a pull may be before rule 10 fires. 2.5x the cadence tolerates one missed
            # tick and a slow run without crying wolf.
            "grace_multiple": 2.5,
            "rows": 0 if stale else 1184,
            "ok": not stale,
            "last_error": last_error,
            "history": history,
        },
        "accounts": ACCOUNTS,
        "campaigns": campaigns,
        "changes": changes,
        "flag_state": flag_state,
    }


def _change(spec, cid, level, entity, frm, to, days_ago, rows, now_utc):
    """One status transition, with the delivery it cost.

    Lost spend is hours-off x the trailing average hourly spend from BEFORE the stop, not the
    daily budget, which is a ceiling the campaign may never have reached.
    """
    ts = now_utc - datetime.timedelta(days=days_ago, hours=2)
    before = [r["spend"] for r in rows[-(days_ago + 15):-days_ago] if r["spend"] > 0]
    per_hour = (sum(before) / len(before) / 24.0) if before else 0.0
    hours = days_ago * 24 + 2
    return {
        "ts": _iso_z(ts),
        "account": spec["account"],
        "level": level,
        "campaign_id": cid,
        "campaign": spec["name"],
        "entity": entity,
        "from": frm,
        "to": to,
        "resolved_ts": None,
        "hours": _rounded(hours, 1),
        "lost_spend": _rounded(per_hour * hours),
    }


# ==================================================================== scoping (the isolation rule)
SCOPES = {
    "internal": {
        "accounts": ["into", "service7000"],
        "client": "INTO · Service 7000",
        "tagline": "Campaign uptime, both accounts",
        "theme": "internal",
        "mark": ("agora.png", "image/png"),
        "lockup": None,
    },
    "into": {
        "accounts": ["into"],
        "client": "INTO Schüleraustausch",
        "tagline": "the journey starts here",
        "theme": "into",
        "mark": ("into-mark.png", "image/png"),
        "lockup": None,
    },
    "service7000": {
        "accounts": ["service7000"],
        "client": "Service 7000 AG",
        "tagline": "Reparatur · Verkauf · Installation",
        "theme": "service7000",
        "mark": ("s7000-mark.png", "image/png"),
        "lockup": ("s7000-lockup.png", "image/png"),
    },
}


def scope_payload(full, scope):
    """Project the full payload down to ONE scope's rows.

    This function is the data-isolation boundary. It rebuilds the payload from scratch keeping
    only rows whose account is in scope, it never ships a combined payload with a flag on it,
    because a flag can be ignored by a browser and a missing row cannot.
    """
    cfg = SCOPES[scope]
    keep = set(cfg["accounts"])

    accounts = [a for a in full["accounts"] if a["key"] in keep]
    campaigns = [c for c in full["campaigns"] if c["account"] in keep]
    ids = set(c["id"] for c in campaigns)
    changes = [ch for ch in full["changes"] if ch["account"] in keep]
    # Every entity id in scope, not just campaign ids: a flag can be raised against an ad set
    # (an end date or a lifetime budget lives at either level), and keying only on campaigns
    # would silently drop those first-detected timestamps.
    keys = set()
    for c in campaigns:
        keys.add(c["id"])
        for a in c["adsets"]:
            keys.add(a["id"])
            for ad in a["ads"]:
                keys.add(ad["id"])
    flag_state = {k: v for k, v in full["flag_state"].items()
                  if k.split(":", 1)[-1] in keys}

    brand = {
        "theme": cfg["theme"],
        "mark": _b64(os.path.join(_ASSETS, cfg["mark"][0]), cfg["mark"][1]) if cfg["mark"] else None,
        "lockup": (_b64(os.path.join(_ASSETS, cfg["lockup"][0]), cfg["lockup"][1])
                   if cfg["lockup"] else None),
        "agora_logo": _b64(os.path.join(_ASSETS, "agora.png"), "image/png"),
    }

    out = {
        "scope": scope,
        "client": cfg["client"],
        "tagline": cfg["tagline"],
        "brand": brand,
        "generated_at": full["generated_at"],
        "demo": full["demo"],
        "currency": full["currency"],
        "locale": full["locale"],
        "timezone": full["timezone"],
        "thresholds": full["thresholds"],
        "pull": full["pull"],
        "accounts": accounts,
        "campaigns": campaigns,
        "changes": changes,
        "flag_state": flag_state,
    }
    # Belt and braces: the isolation claim is asserted, not assumed. `ids` is unused otherwise,
    # so this also keeps the check honest if the projection above is ever edited.
    for c in out["campaigns"]:
        assert c["account"] in keep, "scope leak: %s in %s" % (c["id"], scope)
    for ch in out["changes"]:
        assert ch["account"] in keep, "scope leak: change %s in %s" % (ch["ts"], scope)
        assert ch["campaign_id"] in ids, "scope leak: orphan change in %s" % scope
    return out


def verify_isolation(payloads):
    """QA §11, item one, the check that comes before every other check.

    Greps each client payload for the OTHER client's account id, name and campaign names as raw
    text. Checking the parsed rows is not enough: the requirement is that the bytes on the wire
    contain nothing about the other client, and a stray label in a summary string would pass a
    row-level check while still leaking.
    """
    problems = []
    other = {"into": "service7000", "service7000": "into"}
    by_key = {a["key"]: a for a in ACCOUNTS}
    for scope, other_scope in other.items():
        raw = json.dumps(payloads[scope], ensure_ascii=False)
        o = by_key[other_scope]
        needles = [o["id"], o["name"]]
        needles += [s["name"] for s in SPECS if s["account"] == other_scope]
        for n in needles:
            if n and n in raw:
                problems.append("%s.json contains %r from %s" % (scope, n, other_scope))
    return problems


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default=_DEFAULT_OUT, help="output directory (default: ../data)")
    ap.add_argument("--stale", action="store_true",
                    help="fake a broken/late Windsor pull, to prove the dashboard reports a "
                         "PIPELINE failure rather than 14 dead campaigns")
    args = ap.parse_args(argv[1:])

    os.makedirs(args.out, exist_ok=True)
    full = build(stale=args.stale)
    payloads = {s: scope_payload(full, s) for s in SCOPES}

    problems = verify_isolation(payloads)
    if problems:
        for p in problems:
            sys.stderr.write("[LEAK] %s\n" % p)
        sys.stderr.write("[ERROR] data isolation FAILED, nothing written.\n")
        return 1

    for scope, payload in payloads.items():
        path = os.path.join(args.out, "%s.json" % scope)
        # UTF-8 BYTES, never a text-mode handle: German campaign names and Schüleraustausch's
        # umlauts encode as cp1252 on Windows in text mode and the reader then blows up.
        with open(path, "wb") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        print("  %-14s %5d campaigns  %6.1f KB  %s"
              % (scope, len(payload["campaigns"]), os.path.getsize(path) / 1024.0, path))

    print("\n  data isolation verified: neither client payload mentions the other.")
    print("  pull: %s (%s)" % (full["pull"]["last_success"],
                               "STALE, rule 10 should fire" if args.stale else "fresh"))
    print("  demo data, every payload carries \"demo\": true and the dashboard says so on screen.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
