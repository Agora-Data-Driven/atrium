"""Seed a LOCAL portal for click-through testing (no GCP, no ADC).

Run by run_local.ps1 after pointing REGISTRY_LOCAL_DIR + WORKSPACE_LOCAL_DIR at a throwaway folder.
It builds a portal you can actually log into on your laptop:

  * riverdance -- the full demo workspace (seed_workspace) + a known password, so you can see a
    single-client login drop STRAIGHT onto the company overview (/w/riverdance/overview).
  * five more clients (Honey Tribe, Melo Yelo, Rooming House Expert, ASL Logistics, The Contract Shop) onboarded via onboard_client, each with a
    starter workspace + known password, to compare against.

Every client here gets a DISTINCT password on purpose: portal login matches a password to a client
(the email is only a label), so each password must be unique. These are LOCAL dev passwords only --
never used in production. Reruns are safe (every step refuses to clobber existing data).
"""

import sys

import onboard_client
import seed_workspace
import store
import workspace

# (key, display name, LOCAL dev password). Riverdance reuses the rich demo workspace; the rest get a
# clean starter workspace from onboard_client.
DEMO_KEY = "riverdance"
DEMO_NAME = "Riverdance RV Resort"
DEMO_PW = "riverdance-demo"

OTHER_CLIENTS = [
    ("honeytribe", "Honey Tribe", "honeytribe-demo"),
    ("meloyelo", "Melo Yelo", "meloyelo-demo"),
    ("rhe", "Rooming House Expert", "rhe-demo"),
    ("asllogistics", "ASL Logistics", "asllogistics-demo"),
    ("contractshop", "The Contract Shop", "contractshop-demo"),
]


# Operator accounts (email + password) for the login-mode preview.
#  * info@agoradatadriven.com is THE super admin (can create/manage admin accounts). It's also the
#    identity the no-password preview auto-signs you in as, so Profile + admin management line up.
#  * dev@localhost is a regular admin, seeded so the Accounts list shows both tiers to play with.
SUPER_ADMIN_EMAIL = "info@agoradatadriven.com"
SUPER_ADMIN_PW = "agora-super"
ADMIN_EMAIL = "dev@localhost"
ADMIN_PW = "dev-admin"

# The real Agora team (mirrors the production accounts roster), seeded as ACTIVE admin accounts so
# the Task Board's lead / support / sub-task-owner pickers are populated exactly like production
# (the roster = active admin accounts -- see main._team_roster). Local dev passwords only.
TEAM = [
    ("charles@100.digital", "Charles"),
    ("christian@agoradatadriven.com", "Christian"),
    ("ehjay@agoradatadriven.com", "Ehjay"),
    ("ian@100.digital", "Ian"),
    ("jerome@agoradatadriven.com", "Jerome"),
    ("john@bidbrain.com", "John"),
    ("justine@agoradatadriven.com", "Justine"),
    ("lance@agoradatadriven.com", "Lance"),
    ("nico@agoradatadriven.com", "Nico"),
    ("paulo@agoradatadriven.com", "Paulo"),
    ("samuel@agoradatadriven.com", "Samuel"),
    ("zhen@100.digital", "Zhen"),
]

# Demo tasks for the Delivery -> Task Board (per client key). Each: (fields, subtasks, comments)
# where subtasks are (text, done, owner_email) and comments are (sender, name, body, kind).
# Real campaigns/deliverables so the board reads like the approved prototype, not lorem.
_T = "@agoradatadriven.com"
DEMO_TASKS = {
    "riverdance": [
        ({"title": "Park & Porch — lead-gen funnel", "stage": "launched", "department": "acquisition",
          "lead_id": "zhen@100.digital", "support_ids": ["ehjay" + _T, "lance" + _T],
          "priority": "High", "labels": ["Paid Media", "Strategy"], "campaign": "Park & Porch | Leads",
          "content_type": "Funnel", "due_date": "2026-07-18", "client_facing": True,
          "client_note": "Your funnel is live — first leads are coming through. We're watching cost-per-lead this week before scaling.",
          "deliverable_url": "https://drive.google.com/", "internal_notes": "Watching CPL closely before we scale spend."},
         [("Propose funnel", True, "zhen@100.digital"), ("Create info pack", True, "ehjay" + _T),
          ("Align with launch", True, "lance" + _T), ("Launch", True, "zhen@100.digital")],
         [("agora", "Zhen", "Funnel's live and the first leads are in — we'll report back on CPL Friday.", "comment")]),
        ({"title": "Campspot GTM tracking (with Yomesh)", "stage": "in_process", "department": "development",
          "lead_id": "nico" + _T, "support_ids": ["jerome" + _T],
          "priority": "Medium", "labels": ["Website"], "campaign": "Direct Booking (Campspot)",
          "content_type": "Tracking setup", "due_date": "2026-07-20", "client_facing": True,
          "client_note": "Waiting on write access to your tag manager — we'll confirm once it's granted.",
          "internal_notes": "GTM is read-only — write access requested; Daniela to confirm."},
         [("Review Yomesh reply", True, "nico" + _T), ("Get GTM write access", False, "nico" + _T),
          ("Verify tracking", False, "jerome" + _T)], []),
    ],
    "contractshop": [
        ({"title": "Switch reporting to July", "stage": "for_launch", "department": "data",
          "lead_id": "ian@100.digital", "support_ids": ["samuel" + _T],
          "priority": "High", "labels": ["Reporting"], "campaign": "TCS Placement / Ads Manager",
          "content_type": "Report", "due_date": "2026-07-15", "client_facing": True,
          "client_note": "Your July report is ready for a look — we'll walk you through it on the next call.",
          "internal_notes": "Double-check the logo before this goes near the client."},
         [("July numbers pulled", True, "ian@100.digital"), ("Visuals refreshed", True, "samuel" + _T),
          ("Final check", False, "ian@100.digital")],
         [("agora", "Ian", "July report is ready for your review — anything you'd like added before the call?", "comment")]),
        ({"title": "Get Clients (5 Unexpected Ways) — 6 statics", "stage": "in_process", "department": "acquisition",
          "lead_id": "justine" + _T, "support_ids": ["ehjay" + _T],
          "priority": "Medium", "labels": ["Creative", "Paid Media"], "campaign": "Get Clients (5 Unexpected Ways)",
          "content_type": "Static ad set", "due_date": "2026-07-21",
          "internal_notes": "Copy-led; the diagnostic quiz is the CTA."},
         [("Concept per static", False, "justine" + _T), ("Design 6 statics", False, "ehjay" + _T),
          ("Internal review", False, "justine" + _T)], []),
        ({"title": "July recap — approved & archived", "stage": "closed", "department": "data",
          "lead_id": "ian@100.digital", "priority": "Medium", "labels": ["Reporting"],
          "campaign": "TCS Placement / Ads Manager", "content_type": "Report", "client_facing": True,
          "client_note": "Signed off and filed — thank you!", "deliverable_url": "https://drive.google.com/"},
         [("Delivered", True, "ian@100.digital"), ("Client sign-off", True, None)],
         [("client", "Client", "Looks great, approved!", "comment")]),
    ],
    "honeytribe": [
        ({"title": "New Arrivals — social video", "stage": "for_launch", "department": "lifecycle",
          "lead_id": "paulo" + _T, "support_ids": ["charles@100.digital"],
          "priority": "High", "labels": ["Creative", "Organic"], "campaign": "New Arrivals",
          "content_type": "Video", "due_date": "2026-07-16", "client_facing": True,
          "client_note": "First New Arrivals video is in your workspace for approval — let us know if you'd like any tweaks.",
          "deliverable_url": "https://drive.google.com/",
          "internal_notes": "Client leans hard on UGC / BTS — keep it native."},
         [("Video drafted", True, "paulo" + _T), ("Story timeline statics", True, "paulo" + _T),
          ("Sent for approval", True, "charles@100.digital"), ("Client approval", False, None)],
         [("agora", "Paulo", "First New Arrivals cut is up for your approval.", "comment"),
          ("client", "Linda", "Can we swap the second clip for a guest testimonial?", "changes")]),
        ({"title": "Buzzbassador research", "stage": "in_process", "department": "data",
          "lead_id": "charles@100.digital", "support_ids": ["ian@100.digital"],
          "priority": "Low", "labels": ["Strategy"], "campaign": "Onboarding", "content_type": "Research",
          "due_date": "2026-07-24", "internal_notes": "Ask Linda what we actually need on Buzzbassador."},
         [("Ask Linda what we need", False, "charles@100.digital"),
          ("Benchmark a fair blogger cut", False, "ian@100.digital"),
          ("Pull 2024 collab data", False, "ian@100.digital")], []),
    ],
    "meloyelo": [
        ({"title": "Q Card campaign launch", "stage": "in_process", "department": "acquisition",
          "lead_id": "lance" + _T, "support_ids": ["justine" + _T],
          "priority": "Urgent", "labels": ["Paid Media", "Strategy"], "campaign": "Finance Page",
          "content_type": "Campaign", "due_date": "2026-07-27", "client_facing": True,
          "client_note": "On hold until Jul 24 at your request — we'll pick this back up right after.",
          "internal_notes": "Confirm the hold is lifted before launching."},
         [("Landing page from Kieren", False, "lance" + _T), ("2 statics from Kieren", False, "lance" + _T),
          ("Q Card funnel mapped end-to-end", False, "justine" + _T)], []),
        ({"title": "Meta Pixel + CAPI setup (GA4 / GTM)", "stage": "in_process", "department": "development",
          "lead_id": "samuel" + _T, "support_ids": ["christian" + _T, "john@bidbrain.com"],
          "priority": "High", "labels": ["Website"], "campaign": "Finance Page",
          "content_type": "Tracking setup", "due_date": "2026-07-17",
          "internal_notes": "Blocked on access — waiting on Andy, and Imran's reply on the Offroad page."},
         [("Ask Andy for access", True, "samuel" + _T), ("Install Pixel + CAPI", False, "samuel" + _T),
          ("Offroad page UTM setup", False, "john@bidbrain.com"), ("QA events fire", False, "christian" + _T)], []),
    ],
    "rhe": [
        ({"title": "6 blogs — Negative Gearing (1 anchor + 5 supporting)", "stage": "in_process",
          "department": "lifecycle", "lead_id": "zhen@100.digital", "support_ids": ["paulo" + _T],
          "priority": "High", "labels": ["Organic"], "campaign": "Negative Gearing",
          "content_type": "Blog batch", "due_date": "2026-07-18", "client_facing": True,
          "client_note": "Anchor blog is drafted and in review — supporting pieces are next.",
          "deliverable_url": "https://drive.google.com/",
          "internal_notes": "Apply Stuart's voice notes from the last batch."},
         [("Anchor blog drafted", True, "zhen@100.digital"), ("5 supporting blogs", False, "paulo" + _T),
          ("Blog graphics", False, "paulo" + _T), ("Publish + log URLs", False, "zhen@100.digital")],
         [("agora", "Zhen", "Anchor blog is drafted — take a look when you have a moment.", "comment")]),
        ({"title": "3 reel ads — Rent to Rent", "stage": "in_process", "department": "acquisition",
          "lead_id": "ehjay" + _T, "support_ids": ["justine" + _T],
          "priority": "Medium", "labels": ["Creative", "Paid Media"], "campaign": "Rent to Rent",
          "content_type": "Reel batch", "due_date": "2026-07-18",
          "internal_notes": "Blocked on scripts — chase Stuart."},
         [("Get reel scripts from Stuart", False, "ehjay" + _T), ("Draft edit per reel", False, "ehjay" + _T),
          ("Internal review + export", False, "justine" + _T)], []),
    ],
    "asllogistics": [
        ({"title": "SEO audit draft → Ian for approval", "stage": "in_process", "department": "data",
          "lead_id": "charles@100.digital", "support_ids": ["ian@100.digital"],
          "priority": "Medium", "labels": ["Website", "Strategy"], "campaign": "Onboarding",
          "content_type": "SEO audit", "due_date": "2026-07-17", "client_facing": True,
          "client_note": "First draft is in internal review — you'll get the prioritized recommendations shortly.",
          "internal_notes": "New client — keep recommendations prioritized, light on jargon."},
         [("Crawl + technical review", True, "charles@100.digital"),
          ("Draft prioritized recommendations", False, "charles@100.digital"),
          ("Ian approval", False, "ian@100.digital")], []),
    ],
}


def _seed_tasks():
    """Fill each demo workspace's Task Board once (skipped when it already has tasks)."""
    for key, entries in DEMO_TASKS.items():
        ws = workspace.load_workspace(key)
        if ws is None or ws.get("tasks"):
            continue  # no workspace yet, or already seeded -- never clobber
        for fields, subtasks, comments in entries:
            task = workspace.add_task(key, fields, actor=SUPER_ADMIN_EMAIL)
            for text, done, owner in subtasks:
                _t, sub = workspace.add_subtask(key, task["id"], text, owner or "")
                if done:
                    workspace.set_subtask_done(key, task["id"], sub["id"], True)
            for sender, name, body, kind in comments:
                workspace.add_task_comment(key, task["id"], sender, name, body, kind=kind)


def main():
    creds = []

    # Seed the operator accounts (idempotent: never clobbers a password you later change).
    store.ensure_super_admin_account(SUPER_ADMIN_EMAIL, SUPER_ADMIN_PW, name="Agora Data Driven")
    store.ensure_admin_account(ADMIN_EMAIL, ADMIN_PW, name="Dev Admin")
    # The real team as active admins -- populates the Task Board's assign pickers like production.
    for email, name in TEAM:
        store.ensure_admin_account(email, "%s-dev" % name.lower(), name=name)

    # Riverdance: rich demo workspace (refuses to clobber) + registry entry + a known password.
    if not workspace.workspace_exists(DEMO_KEY):
        seed_workspace.seed()  # writes workspace/riverdance.json and registers the client
    store.add_client(DEMO_KEY, DEMO_NAME)
    store.set_client_password(DEMO_KEY, DEMO_PW)
    creds.append((DEMO_KEY, DEMO_PW))

    # The other three via the one-step onboarding flow.
    for key, name, pw in OTHER_CLIENTS:
        onboard_client.onboard(key, name, pw)
        creds.append((key, pw))

    # Fill the Delivery -> Task Board with realistic work (skips workspaces that already have tasks).
    _seed_tasks()

    print("\n  Local portal seeded. Log in at http://localhost:8080/login")
    print("  SUPER ADMIN (manages admins; this is 'you'):")
    print("    %-24s  password: %s" % (SUPER_ADMIN_EMAIL, SUPER_ADMIN_PW))
    print("  ADMIN (sees every client):")
    print("    %-24s  password: %s" % (ADMIN_EMAIL, ADMIN_PW))
    print("\n  CLIENT logins -- use ANY email (e.g. owner@example.com) + one of these passwords:\n")
    for key, pw in creds:
        only = " <- single-client: lands straight on the overview" if key == DEMO_KEY else ""
        print("    %-12s  password: %s%s" % (key, pw, only))
    print("")
    return 0


if __name__ == "__main__":
    sys.exit(main())
