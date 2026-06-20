"""Flask route + template integration smoke test for Agora Atrium (off-cloud, no real GCS).

Stubs google.cloud.storage so main.py (which imports store/feedback) loads without ADC, points the
workspace store at a temp dir, seeds the Riverdance demo there, then drives the real Flask app with
its test client: every client tab renders, and every POST action persists. Proves the route wiring,
the Jinja template, the atrium_dt filter, and atrium_view all work together before any deploy.

Run with a Flask-capable interpreter:
    python _atrium_smoketest.py        # prints PASS / FAIL, exits 0 / 1
"""

import os
import shutil
import sys
import tempfile
import types

# 1. Stub google.cloud.storage BEFORE importing main (store/feedback construct a client at import).
_g = types.ModuleType("google"); _g.__path__ = []
_gc = types.ModuleType("google.cloud"); _gc.__path__ = []
_gs = types.ModuleType("google.cloud.storage")


class _FakeClient:
    def __init__(self, *a, **k):
        pass

    def bucket(self, *a, **k):
        raise RuntimeError("GCS disabled in smoke test (use the local backend)")


_gs.Client = _FakeClient
sys.modules.setdefault("google", _g)
sys.modules.setdefault("google.cloud", _gc)
sys.modules["google.cloud.storage"] = _gs

# 2. Point the workspace store at a temp dir and sign the session.
_TMP = tempfile.mkdtemp(prefix="atrium_smoke_")
os.environ["WORKSPACE_LOCAL_DIR"] = _TMP
os.environ["SESSION_SECRET"] = "test-secret"

import seed_workspace   # noqa: E402
import workspace        # noqa: E402
import main             # noqa: E402

CLIENT = "riverdance"
SUPER = {"ok": True, "user": "info@agoradatadriven.com", "clients": ["*"]}


def _check(label, cond):
    if not cond:
        raise AssertionError(label)
    print("  [OK] %s" % label)


def run():
    seed_workspace.seed(register_client=False)
    main.app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False, SESSION_COOKIE_SAMESITE="Lax")
    c = main.app.test_client()

    # Unauthenticated -> redirect to login.
    _check("unauthed /w redirects to login", c.get("/w/%s/" % CLIENT).status_code == 302)

    with c.session_transaction() as s:
        s.update(SUPER)

    # Every client tab renders.
    body = c.get("/w/%s/" % CLIENT).get_data(as_text=True)
    _check("overview renders", "Riverdance RV Resort" in body and "Agora Atrium" in body)
    _check("greeting present", "Good <span" in body)
    _check("leadgen content present in DOM", "Summer Lead-Gen Push" in body)
    _check("organic content present in DOM", "June Nurture &amp; SEO" in body or "June Nurture" in body)
    _check("AI summary present", "AI summary" in body)
    for tab in ("dashboard", "leadgen", "organic", "calendar", "conversations", "settings"):
        _check("tab '%s' returns 200" % tab, c.get("/w/%s/%s" % (CLIENT, tab)).status_code == 200)

    # Approve an awaiting piece -> persists + confirmation shows on reload.
    r = c.post("/w/%s/approve" % CLIENT, data={"content_id": "RVR-016", "note": "Ship it."})
    _check("approve returns ok json", r.status_code == 200 and r.get_json().get("ok") is True)
    _camp, item = workspace._find_content(workspace.load_workspace(CLIENT), "RVR-016")
    _check("approval persisted", item["status"] == "approved" and item["client_note"] == "Ship it.")
    _check("confirmation bar on reload",
           "You approved this" in c.get("/w/%s/leadgen" % CLIENT).get_data(as_text=True))

    # Request changes on the organic awaiting piece.
    r = c.post("/w/%s/request-changes" % CLIENT, data={"content_id": "RVR-017"})
    _check("request-changes ok", r.status_code == 200 and r.get_json().get("status") == "changes")

    # Save a note silently.
    _check("save-note ok",
           c.post("/w/%s/save-note" % CLIENT, data={"content_id": "RVR-014", "note": "Nice"}).status_code == 200)

    # Send a client message -> thread goes awaiting_reply.
    r = c.post("/w/%s/send-message" % CLIENT, data={"conversation_id": "cv_1", "body": "Thanks!"})
    _check("send-message ok", r.status_code == 200 and r.get_json().get("status") == "awaiting_reply")
    _check("message persisted",
           workspace.load_workspace(CLIENT)["conversations"][0]["messages"][-1]["body"] == "Thanks!")

    # Save notification prefs.
    r = c.post("/w/%s/save-notify" % CLIENT,
               data={"master": "1", "content": "0", "replies": "1", "summary": "1",
                     "status": "0", "news": "0", "frequency": "daily"})
    _check("save-notify ok", r.status_code == 200)
    prefs = workspace.get_notify(workspace.load_workspace(CLIENT), SUPER["user"])
    _check("notify persisted", prefs["content"] is False and prefs["frequency"] == "daily")

    # Team management: manage page renders, add content + reply persist.
    _check("admin manage page renders",
           "Managing" in c.get("/admin/atrium/%s" % CLIENT).get_data(as_text=True))
    r = c.post("/admin/atrium/%s/content" % CLIENT,
               data={"campaign_id": "c_paid_1", "ref": "RVR-018", "type_tag": "Static Post",
                     "platform": "Instagram", "caption": "A brand new ad for review."})
    _check("admin add-content redirects", r.status_code == 302)
    _camp, new_item = workspace._find_content(workspace.load_workspace(CLIENT), "RVR-018")
    _check("added content is awaiting", new_item is not None and new_item["status"] == "awaiting")
    _check("client saw activity for new content",
           any("RVR-018" in a["text"] for a in workspace.load_workspace(CLIENT)["activity"]))
    r = c.post("/admin/atrium/%s/reply" % CLIENT,
               data={"conversation_id": "cv_1", "sender_name": "Maya", "body": "On it!", "resolve": "1"})
    _check("admin reply redirects", r.status_code == 302)
    conv = workspace._find_conversation(workspace.load_workspace(CLIENT), "cv_1")
    _check("reply persisted + resolved", conv["messages"][-1]["body"] == "On it!" and conv["status"] == "resolved")

    # Admin: add a campaign, edit its strategy, start a conversation, edit metrics.
    before_n = len(workspace.load_workspace(CLIENT)["campaigns"])
    r = c.post("/admin/atrium/%s/campaign" % CLIENT,
               data={"channel": "paid", "name": "Autumn Retargeting", "eyebrow": "PAID · RETARGETING",
                     "what": "w", "why": "y", "next": "n", "ai_summary": "s"})
    _check("admin add-campaign redirects", r.status_code == 302)
    camps = workspace.load_workspace(CLIENT)["campaigns"]
    _check("campaign added", len(camps) == before_n + 1)
    new_id = camps[-1]["id"]
    r = c.post("/admin/atrium/%s/campaign" % CLIENT,
               data={"campaign_id": new_id, "what": "updated what", "why": "uy", "next": "un", "ai_summary": "us"})
    _check("admin update-campaign redirects", r.status_code == 302)
    _check("campaign strategy updated",
           workspace._find_campaign(workspace.load_workspace(CLIENT), new_id)["strategy"]["what"] == "updated what")

    conv_before = len(workspace.load_workspace(CLIENT)["conversations"])
    r = c.post("/admin/atrium/%s/conversation" % CLIENT,
               data={"subject": "July creative", "sender_name": "Maya", "body": "Kicking off July."})
    _check("admin start-conversation redirects", r.status_code == 302)
    _check("conversation added", len(workspace.load_workspace(CLIENT)["conversations"]) == conv_before + 1)

    r = c.post("/admin/atrium/%s/metrics" % CLIENT,
               data={"today_leads": "12", "today_visitors": "400", "today_bookings": "5",
                     "split_paid": "90", "split_organic": "70",
                     "metric_value_0": "200", "metric_trend_0": "+30%", "metric_up_0": "1"})
    _check("admin metrics redirects", r.status_code == 302)
    ws2 = workspace.load_workspace(CLIENT)
    _check("today updated", ws2["today"]["leads"] == 12)
    _check("split updated", ws2["split"]["paid"] == 90)
    _check("metric 0 value updated", ws2["metrics"][0]["value"] == "200")

    # A user who cannot open the client is forbidden.
    with c.session_transaction() as s:
        s.update({"ok": True, "user": "x@y.com", "clients": ["someoneelse"]})
    _check("non-grantee forbidden", c.get("/w/%s/" % CLIENT).status_code == 403)

    print("[smoketest] PASS")
    return 0


def main_():
    try:
        return run()
    except AssertionError as exc:
        print("[smoketest] FAIL: %s" % exc)
        return 1
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main_())
