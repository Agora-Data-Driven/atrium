"""Generate a small SYNTHETIC RHE payload with the exact published shape.

No credentials, no network — it exists so the dashboard's every code path (all four tabs, all
five demographic splits, the quiz donuts, the wired-empty conversion state, the n/a unique-leads
row) can be exercised in CI and in a browser check without a 30-minute live crawl.

    py -3 clients/client_RHE/job/make_fixture.py [out.json]

Keep this in step with job/main.py's `build()` — if a key moves there, move it here, or the
smoke test silently stops covering the real shape.
"""
import datetime
import hashlib
import json
import os
import random
import sys

random.seed(7)

_HERE = os.path.dirname(os.path.abspath(__file__))
OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(_HERE, "..", "data", "RHE.fixture.json")

TODAY = datetime.date(2026, 7, 27)
START = TODAY - datetime.timedelta(days=420)

ACCOUNTS = ["rhe", "Stuart Baker", "Super Cashflow Development"]
CAMPAIGNS = ["LEADS_RHE_URHCG - 12/2025", "LISTING LEADS_SCD_Pandanus Warragul-Dec2025",
             "LISTING LEADS_SCD_Illawonga Churchill-March2026 - Waitlist",
             "LISTING LEADS_SCD_3North Road Warragul-February2026 Campaign",
             "URGENCY LEADS_SCD_SMSF-July 2026 Campaign"]
ADS = ["Reel_StuartAI_Signup [jack]", "Reel1_ It's not a maybe one day development play_Download",
       "Static Images_LICENSED_Emma_0528", "Reel2_Waitlist_Learn More",
       "Static Image_Shipwright_0726", "Reel_CarSeat2_Download [brett]"]
AGES = ["18-24", "25-34", "35-44", "45-54", "55-64", "65+"]
GENDERS = ["male", "female", "unknown"]
REGIONS = ["Victoria", "New South Wales", "Queensland", "Western Australia", "South Australia",
           "Australian Capital Territory", "Tasmania", "Northern Territory"]
PLATFORMS = ["facebook", "instagram", "audience_network"]
POSITIONS = ["feed", "facebook_reels", "instagram_reels", "instream_video", "facebook_stories",
             "marketplace"]
DEVICES = ["iphone", "android_smartphone", "ipad", "desktop", "android_tablet", "other"]

EXPERIENCE = ["First-time investor", "Some experience", "Experienced", "Portfolio builder"]
TIMELINE = ["Within 3 months", "3–6 months", "6–12 months", "Just exploring"]
STATUSES = ["Lead", "Listing Lead"]          # deliberately NO Client — converted must read 0
TEMPLATES = ["Strong Rent On Paper Does Not Mean Strong Cash Flow",
             "The Final Compliance Gap Investors Often Miss",
             "Stronger Cash Flow Should Not Feel Like Another Job",
             "The $20,000 “Cheap” Mistake",
             "Gross Is a Vanity Metric. Let’s Talk Net.",
             "Is Your Investment a Legal Rooming House?"]


def days(n):
    return [(START + datetime.timedelta(days=i)).isoformat() for i in range(n)]


ALL_DAYS = days((TODAY - START).days + 1)


def h(s):
    return hashlib.sha1(("fixture" + str(s)).encode()).hexdigest()[:12]


# --- meta rows ---------------------------------------------------------------
meta_rows = []
for d in ALL_DAYS:
    for acct in ACCOUNTS:
        # only "rhe" runs every day; the other two are dormant recently, like the real account
        if acct != "rhe" and d > "2026-05-01":
            continue
        for camp in random.sample(CAMPAIGNS, 2):
            for ad in random.sample(ADS, 2):
                imps = random.randint(400, 9000)
                lclk = int(imps * random.uniform(0.006, 0.028))
                leads = int(lclk * random.uniform(0.06, 0.28))
                spend = round(imps * random.uniform(0.022, 0.055), 2)
                meta_rows.append({
                    "d": d, "acct": acct, "camp": camp,
                    "adset": camp + " / adset", "ad": ad,
                    "title": "Rooming house conversion", "created": "2026-01-29",
                    "bid": "LOWEST_COST_WITHOUT_CAP",
                    "spend": spend, "imps": imps,
                    "clicks": lclk + random.randint(0, 40), "lclk": lclk,
                    "reach": int(imps * random.uniform(0.55, 0.9)),
                    "freq": round(random.uniform(1.02, 2.9), 4),
                    "leads": leads, "thru": int(imps * random.uniform(0.02, 0.09)),
                    "react": random.randint(0, 30),
                    "ulead": 0,                       # Meta returns this empty — keep it empty
                    "ulclk": int(lclk * 0.9),
                })
mdates = sorted(r["d"] for r in meta_rows)


def bd(slug, dims, values, monthly=False, leads=True):
    rows = []
    for d in ALL_DAYS[::(30 if monthly else 3)]:
        key = d[:7] if monthly else d
        for combo in values:
            imps = random.randint(200, 4000)
            lclk = int(imps * random.uniform(0.005, 0.03))
            rec = {"d": key, "acct": random.choice(ACCOUNTS), "spend": round(imps * random.uniform(0.02, 0.05), 2),
                   "imps": imps, "clicks": lclk + random.randint(0, 20), "lclk": lclk,
                   "reach": int(imps * 0.7),
                   "leads": int(lclk * random.uniform(0.05, 0.3)) if leads else 0}
            for i, dim in enumerate(dims):
                rec[dim] = combo[i] if isinstance(combo, tuple) else combo
            rows.append(rec)
    return {"rows": rows, "has_leads": leads}


breakdowns = {
    "enabled": True, "window": "last_365d", "note": "",
    "age_gender": bd("age_gender", ["age", "gender"],
                     [(a, g) for a in AGES for g in GENDERS]),
    # region carries NO leads — the real Meta limitation this dashboard has to state
    "region": bd("region", ["region"], REGIONS, monthly=True, leads=False),
    "platform": bd("platform", ["publisher_platform"], PLATFORMS),
    "position": bd("position", ["platform_position"], POSITIONS),
    "device": bd("device", ["impression_device"], DEVICES),
}

# --- email -------------------------------------------------------------------
contacts = []
for i in range(1400):
    cd = random.choice(ALL_DAYS)
    answered = random.random() < 0.38
    opened = random.random() < 0.55
    contacts.append({
        "cid": h(i), "cdate": cd,
        "domain": random.choice(["gmail.com", "hotmail.com", "outlook.com", "yahoo.com.au",
                                 "bigpond.com", "icloud.com"]),
        "sent": random.randint(0, 24),
        "opened": random.choice(ALL_DAYS[-120:]) if opened else None,
        "clicked": random.choice(ALL_DAYS[-120:]) if (opened and random.random() < 0.4) else None,
        "bounced": 1 if random.random() < 0.03 else 0,
        "experience": random.choice(EXPERIENCE) if answered else None,
        "timeline": random.choice(TIMELINE) if answered else None,
        "first_time": random.choice(["Yes", "No"]) if answered else None,
        "status": random.choice(STATUSES),
        "pathway": None,
        "campaign": random.choice(CAMPAIGNS) if answered else None,
        "engaged": 1 if opened else 0,
        "converted": 0,
    })

# v3 shape: sends + OPENS (link-data pixel reads) + Apple MPP machine opens. There is
# deliberately NO per-day `clicks` — clicks are campaign-level only (see EVENT_KINDS).
daily = []
for d in ALL_DAYS:
    sends = random.choice([0, 0, 0, random.randint(20, 900)])
    opens = int(sends * random.uniform(0.22, 0.55)) if sends else 0
    mpp = int(opens * random.uniform(0.3, 0.8)) if opens else 0
    daily.append({"d": d, "sends": sends, "opens": opens, "mpp": mpp, "v": 3})

templates = []
for t in TEMPLATES:
    n = random.randint(60, 2100)
    templates.append({"name": t, "sends": n,
                      "first": random.choice(ALL_DAYS[:200]), "last": random.choice(ALL_DAYS[-120:])})
templates.sort(key=lambda x: -x["sends"])

campaigns = []
for i, t in enumerate(TEMPLATES):
    sent = random.randint(900, 3000)
    uo = int(sent * random.uniform(0.28, 0.40))
    ulc = int(uo * random.uniform(0.005, 0.05))
    campaigns.append({
        "id": str(100 + i), "name": "RHE Newsletter - " + t[:40],
        "date": random.choice(ALL_DAYS[-160:]), "sent": sent,
        "opens": uo, "opens_total": int(uo * 1.3), "clicks": ulc,
        "clicks_total": int(ulc * 1.25), "unsubs": random.randint(4, 60),
        "bounces": random.randint(2, 24), "forwards": 0, "replies": 0, "socialshares": 0,
    })
campaigns.sort(key=lambda c: c["date"], reverse=True)

email = {
    "enabled": True, "account": "roominghouse", "url": "https://roominghouse.api-us1.com",
    "fetched": TODAY.isoformat() + "T00:00:00Z", "crm_enabled": False, "error": "",
    "partial": False,
    "totals": {"contacts": len(contacts), "campaigns": 312, "campaigns_sent": len(campaigns),
               "lists": 11, "automations": 43,
               "sends": sum(d["sends"] for d in daily),
               "opens": sum(d["opens"] for d in daily),
               "mpp": sum(d["mpp"] for d in daily),
               "campaign_sent": sum(c["sent"] for c in campaigns),
               "campaign_opens": sum(c["opens"] for c in campaigns),
               "campaign_clicks": sum(c["clicks"] for c in campaigns),
               "engaged": sum(c["engaged"] for c in contacts), "converted": 0},
    "campaigns": campaigns,
    "lists": [{"id": str(i), "name": n, "subscribers": random.randint(1, 1800), "total": 0}
              for i, n in enumerate(["Master Contact List - Newsletter", "Leads",
                                     "LISTING_All Leads", "RHE_URHCG META Leads",
                                     "RHE_NEWSLETTER", "LISTING_12 Illawonga [Waitlist]"])],
    "automations": [{"id": str(i), "name": n, "entered": random.randint(200, 26000),
                     "exited": random.randint(180, 25000), "active": i % 3 != 0}
                    for i, n in enumerate(["Last Engaged Date - Part 1", "Lead Score: Opens",
                                           "Lead Score: Clicks", "URHCG LEADS_Meta Automation",
                                           "URHCG Email Automations", "LEADS_Microwave Emails"])],
    "contacts": contacts, "daily": daily, "templates": templates,
    "quiz": {"experience": EXPERIENCE, "timeline": TIMELINE, "first_time": ["Yes", "No"],
             "status": ["Lead", "Client", "Returning Client", "Listing Lead"],
             "pathway": ["Stratos", "Conversion", "Manual Review"]},
    "watermark": {"sends": "", "events": "", "events_oldest": ALL_DAYS[-150]},
}

data = {
    "client": "Rooming House Expert",
    "tagline": "Turning underperforming property into cash-flow.",
    "location": "Victoria, Australia", "currency": "AUD",
    "generated_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "build_seconds": 0.0, "data_through": mdates[-1],
    "source": {"meta": "SYNTHETIC FIXTURE", "email": "SYNTHETIC FIXTURE", "mode": "fixture",
               "meta_through": mdates[-1], "email_through": daily[-1]["d"]},
    "brand": {"mark": None, "agora_logo": None},
    "meta": {
        "rows": sorted(meta_rows, key=lambda r: (r["d"], r["acct"], r["camp"], r["ad"])),
        "range": [mdates[0], mdates[-1]],
        "accounts": sorted({r["acct"] for r in meta_rows}),
        "campaigns": sorted({r["camp"] for r in meta_rows}),
        "adsets": sorted({r["adset"] for r in meta_rows}),
        "ads": sorted({r["ad"] for r in meta_rows}),
        "unique_from": "last_365d",
    },
    "breakdowns": breakdowns,
    "email": email,
}

body = json.dumps(data, separators=(",", ":")).encode("utf-8")
os.makedirs(os.path.dirname(os.path.abspath(OUT)), exist_ok=True)
with open(OUT, "wb") as fh:                      # bytes, never text mode (playbook 3.7)
    fh.write(body)
print("wrote %s  (%d meta rows, %d contacts, %d days, %.2f MB)"
      % (OUT, len(meta_rows), len(contacts), len(daily), len(body) / 1048576.0))

