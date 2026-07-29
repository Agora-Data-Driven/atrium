"""Off-cloud test for the distilled-insight layer + the Reports tab (no GCS, no network, no LLM).

Covers digest.py (dashboard sections for BOTH shapes, comms/tasks/intel snapshots), report_ai
(gather -> generate with and without a model -> revise -> the rendered deck), the workspace report
helpers (index entry + per-report HTML object), and the Flask routes (team generate/rename/delete,
the client-visible deck serve with lazy re-render, gating).

Run: python _report_localtest.py        # prints PASS / FAIL, exits 0 / 1
"""

import os
import shutil
import sys
import tempfile
import types

# Stub google.cloud.storage BEFORE importing main (store/feedback construct a client at import).
_g = types.ModuleType("google"); _g.__path__ = []
_gc = types.ModuleType("google.cloud"); _gc.__path__ = []
_gs = types.ModuleType("google.cloud.storage")


class _FakeClient:
    def __init__(self, *a, **k):
        pass

    def bucket(self, *a, **k):
        raise RuntimeError("GCS disabled in this test (use the local backend)")


_gs.Client = _FakeClient
sys.modules.setdefault("google", _g)
sys.modules.setdefault("google.cloud", _gc)
sys.modules["google.cloud.storage"] = _gs

_TMP = tempfile.mkdtemp(prefix="atrium_report_")
os.environ["WORKSPACE_LOCAL_DIR"] = _TMP
os.environ["REGISTRY_LOCAL_DIR"] = _TMP
os.environ["SESSION_SECRET"] = "test-secret"

import digest           # noqa: E402
import report_ai        # noqa: E402
import seed_workspace   # noqa: E402
import workspace        # noqa: E402
import main             # noqa: E402

CLIENT = "riverdance"
SUPER = {"ok": True, "user": "info@agoradatadriven.com", "clients": ["*"]}
CLIENT_LOGIN = {"ok": True, "user": "owner@riverdanceresort.com", "clients": [CLIENT]}


def _check(label, cond):
    if not cond:
        raise AssertionError(label)
    print("  [OK] %s" % label)


def _dash_rd():
    """A small Windsor-live (riverdance-shape) dashboard export: per-ad/day rows."""
    rows = [{"date": d, "ad": "V_Escape", "camp": "Summer", "spend": 100.0, "imps": 10000,
             "clicks": 150, "lclk": 120, "reach": 8000, "pur": 3, "rev": 900.0, "book_init": 5}
            for d in ("2026-07-01", "2026-07-02", "2026-07-08")]
    rows.append({"date": "2026-07-02", "ad": "S_Static", "camp": "Brand", "spend": 50.0,
                 "imps": 9000, "clicks": 30, "lclk": 20, "reach": 6000, "pur": 0, "rev": 0.0,
                 "book_init": 0})
    return {"rows": rows, "dates": ["2026-07-01", "2026-07-02", "2026-07-08"],
            "demographics": {"age_gender": [{"date": "2026-07-01", "age": "25-34",
                                             "gender": "female", "spend": 60.0, "imps": 5000,
                                             "clicks": 80, "lclk": 60}],
                             "region": [{"date": "2026-07-01", "region": "Colorado",
                                         "spend": 90.0, "imps": 7000, "clicks": 100,
                                         "lclk": 80}]},
            "activecampaign": {"enabled": True, "campaigns": [
                {"name": "July newsletter", "date": "2026-07-05", "sent": 1000, "opens": 400,
                 "clicks": 50}]}}


def _archives():
    ch = {"id": "wch_1", "title": "Fuel Your Wander", "kind": "creator", "industry": "RV travel"}
    return [(ch, [{"id": "v1", "title": "Best CO resorts", "published": "2026-07-03",
                   "transcript": "words", "summary": "- Colorado resorts are booming\n"
                                                     "Takeaway: lean into scenery content."}])]


def run():
    seed_workspace.seed(register_client=False)

    # --- digest.py: the dashboard insight sections, both shapes ----------------------------------
    sections = digest.dashboard_sections(_dash_rd())
    ids = [s[0] for s in sections]
    _check("riverdance-shape yields the full section set",
           {"overview", "campaigns", "creatives", "trend", "audience", "email"} <= set(ids))
    overview = next(text for sid, _t, text in sections if sid == "overview")
    _check("overview computes totals (spend, ROAS) from the raw rows",
           "$350.00" in overview and "ROAS" in overview)
    camp = next(text for sid, _t, text in sections if sid == "campaigns")
    _check("campaign rollup ranks by spend and carries CTR",
           camp.index("Summer") < camp.index("Brand") and "CTR" in camp)
    creatives = next(text for sid, _t, text in sections if sid == "creatives")
    _check("creative section flags spend without return",
           "S_Static" in creatives and "ROAS under 1x" in creatives)
    tmpl = digest.dashboard_sections({"kpis": {"leads": 42},
                                      "daily": [{"date": "2026-07-01", "leads": 3}]})
    _check("template-shape yields kpis + a weekly trend",
           {"kpis", "trend"} <= {s[0] for s in tmpl})
    _check("unknown/empty dashboard data -> no sections",
           digest.dashboard_sections(None) == [] and digest.dashboard_sections({}) == [])

    task = {"id": "tk1", "title": "Launch LP", "stage": "blocked", "priority": "High",
            "due_date": "2026-08-01", "hold_reason": "waiting on client copy",
            "maintasks": [{"title": "Build", "subs": [{"text": "wireframe", "done": True},
                                                      {"text": "copy", "done": False}]}],
            "comments": [{"kind": "changes", "body": "please tweak the header",
                          "resolved": False}]}
    ttext = digest.task_text(task)
    _check("task_text carries id, stage, progress, blockers and change requests",
           "tk1" in ttext and "blocked" in ttext and "1/2" in ttext
           and "ON HOLD" in ttext and "CHANGES REQUESTED" in ttext)
    snap = digest.tasks_snapshot({"tasks": [task]})
    _check("tasks_snapshot lists blocked work and upcoming launches",
           "Launch LP" in snap and "1 blocked" in snap)
    _check("comms_snapshot summarizes channel mix + latest cards",
           "1 meeting" in digest.comms_snapshot({"communications": [
               {"id": "c1", "channel": "meeting", "title": "Kickoff", "summary": "Notes",
                "date": "2026-07-01"}]}))
    _check("intel_digest lists the freshest items per section",
           "Business research" in digest.intel_digest({"intel": {"business_research": [
               {"id": "i1", "title": "Fuel prices drop", "body": "...", "date": "2026-07-02"}],
               "media_buying": []}}))

    # --- report_ai: gather -> generate (model + no-model draft) -> revise -> render --------------
    ws = workspace.load_workspace(CLIENT)
    ws["tasks"] = [task]
    inputs = report_ai.gather(ws, _archives(), _dash_rd())
    _check("gather assembles every source block",
           inputs["business"] and inputs["media"] and inputs["voices"] and inputs["dashboard"]
           and inputs["blocked"])
    _check("voices carry the creator's summary",
           any("Fuel Your Wander" in v and "scenery" in v for v in inputs["voices"]))

    draft = report_ai.draft_payload(inputs)
    _check("the no-AI draft fills the landscape + asks honestly and invents no analysis",
           draft["landscape"]["business"] and draft["asks"]["blocked"]
           and draft["why"] == [] and draft["recommendations"] == [])

    model_payload = ('{"landscape": {"business": [{"title": "B1", "body": "b"}], "media": [], '
                     '"voices": [{"title": "V1", "body": "v"}]}, '
                     '"what_happened": {"summary": "Strong month.", '
                     '"numbers": [{"label": "Spend", "value": "$350", "note": "+10%"}], '
                     '"whats_working": ["Video creative"]}, '
                     '"why": ["Seasonality"], "recommendations": ["Shift budget to video"], '
                     '"asks": {"needed": ["Approve August plan"], "blocked": []}}')
    payload, err = report_ai.generate("Riverdance", "2026-07-29", inputs,
                                      lambda s, u: (model_payload, ""))
    _check("generate parses the model payload", err == "" and payload["why"] == ["Seasonality"]
           and payload["what_happened"]["numbers"][0]["value"] == "$350")
    payload_no_ai, err2 = report_ai.generate("Riverdance", "2026-07-29", inputs, None)
    _check("generate without a model returns the draft + the reason",
           err2 == "no AI model configured" and payload_no_ai["landscape"]["business"])
    _bad, err3 = report_ai.generate("Riverdance", "2026-07-29", inputs,
                                    lambda s, u: ("not json at all", ""))
    _check("unusable model output degrades to the draft with a reason", "JSON" in err3)

    revised, rerr = report_ai.revise(payload, "add a bullet",
                                     lambda s, u: (model_payload.replace(
                                         '"Seasonality"', '"Seasonality", "New bullet"'), ""))
    _check("revise applies the instruction", rerr == "" and "New bullet" in revised["why"])
    same, rerr2 = report_ai.revise(payload, "x", lambda s, u: ("", "model down"))
    _check("a failing revise returns the ORIGINAL payload + the reason",
           rerr2 == "model down" and same == payload)

    html_doc = report_ai.render_html("Riverdance", payload, "2026-07-29",
                                     title="July <script>alert(1)</script> Review")
    _check("the deck renders every agreed slide",
           "July 29, 2026" in html_doc and "The Landscape" in html_doc
           and report_ai.VOICES_LABEL in html_doc and "What Happened" in html_doc
           and "What's Working" in html_doc and "Why It Happened" in html_doc
           and "What We Should Do" in html_doc and "What We Need From You" in html_doc)
    _check("deck HTML is escaped", "<script>alert(1)</script>" not in html_doc)
    _check("the deck is self-contained (no external assets, no JS)",
           "http" not in html_doc.split("</title>")[1] and "<script" not in html_doc)
    empty_doc = report_ai.render_html("Riverdance", {}, "2026-07-01")
    _check("an empty payload still renders cover + an honest asks slide",
           "July 1, 2026" in empty_doc and "Nothing is waiting on you" in empty_doc)

    # --- workspace report helpers ----------------------------------------------------------------
    entry = workspace.add_report(CLIENT, "July review", "2026-07-29",
                                 payload={"why": ["x"]}, origin="ai")
    _check("add_report stores the entry newest-first with its payload",
           workspace.reports_of(workspace.load_workspace(CLIENT))[0]["id"] == entry["id"])
    workspace.write_report_html(CLIENT, entry["id"], "<html>deck</html>")
    _check("deck HTML round-trips through its own object",
           workspace.read_report_html(CLIENT, entry["id"]) == "<html>deck</html>")
    workspace.update_report(CLIENT, entry["id"], {"title": "July review v2"})
    _check("update_report patches in place",
           workspace.find_report(workspace.load_workspace(CLIENT),
                                 entry["id"])["title"] == "July review v2")
    removed = workspace.delete_report(CLIENT, entry["id"])
    _check("delete_report returns the entry and removes the object",
           removed["id"] == entry["id"]
           and workspace.read_report_html(CLIENT, entry["id"]) is None
           and workspace.find_report(workspace.load_workspace(CLIENT), entry["id"]) is None)
    workspace.insert_report(CLIENT, removed)
    _check("insert_report restores the entry (Trash restore path)",
           workspace.find_report(workspace.load_workspace(CLIENT), entry["id"]) is not None)

    # --- Routes: team generate/rename/delete, the client-visible serve, gating -------------------
    main.app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False, SESSION_COOKIE_SAMESITE="Lax")
    c = main.app.test_client()
    with c.session_transaction() as s:
        s.update(SUPER)

    r = c.post("/w/%s/admin/report" % CLIENT, data={"op": "generate", "date": "2026-07-29"})
    data = r.get_json()
    _check("op=generate creates a deck even with no AI configured (honest draft)",
           data["ok"] is True and data["date"] == "2026-07-29"
           and data["note"].startswith("Drafted without AI"))
    rid = data["id"]
    r = c.get("/w/%s/report/%s" % (CLIENT, rid))
    _check("the deck serves through the authed route",
           r.status_code == 200 and "text/html" in r.mimetype
           and "What We Need From You" in r.get_data(as_text=True))

    r = c.post("/w/%s/admin/report" % CLIENT,
               data={"op": "rename", "id": rid, "title": "Renamed deck"})
    _check("op=rename retitles + keeps the deck serving",
           r.get_json()["ok"] is True
           and workspace.find_report(workspace.load_workspace(CLIENT),
                                     rid)["title"] == "Renamed deck")

    body = c.get("/w/%s/reports" % CLIENT).get_data(as_text=True)
    _check("the Reports tab renders the deck card + team controls for the team",
           'data-pane="reports"' in body and "Renamed deck" in body
           and "data-repgen>" in body and 'data-repdel="' in body)

    # A deleted deck object re-renders lazily from the stored payload (the restore path).
    workspace._delete_object(workspace.report_object_name(CLIENT, rid))
    r = c.get("/w/%s/report/%s" % (CLIENT, rid))
    _check("a missing deck object re-renders lazily from the stored payload",
           r.status_code == 200 and "What We Need From You" in r.get_data(as_text=True))

    with c.session_transaction() as s:
        s.clear(); s.update(CLIENT_LOGIN)
    # The generate/delete BUTTONS must never reach a client's HTML (the JS selector literals do
    # ship to every viewer, like the other admin wiring -- the markup is what matters).
    body = c.get("/w/%s/reports" % CLIENT).get_data(as_text=True)
    _check("the client sees the cards but NO team controls",
           "Renamed deck" in body and "data-repgen>" not in body
           and 'data-repdel="' not in body)
    _check("the client can open the deck",
           c.get("/w/%s/report/%s" % (CLIENT, rid)).status_code == 200)
    _check("client POST to /admin/report is forbidden",
           c.post("/w/%s/admin/report" % CLIENT,
                  data={"op": "generate"}).status_code == 403)

    with c.session_transaction() as s:
        s.clear(); s.update(SUPER)
    r = c.post("/w/%s/admin/report" % CLIENT, data={"op": "delete", "id": rid})
    _check("op=delete removes the deck",
           r.get_json()["ok"] is True
           and workspace.find_report(workspace.load_workspace(CLIENT), rid) is None)
    _check("a deleted deck 404s", c.get("/w/%s/report/%s" % (CLIENT, rid)).status_code == 404)


if __name__ == "__main__":
    try:
        run()
        print("PASS")
    except AssertionError as exc:
        print("FAIL: %s" % exc)
        sys.exit(1)
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
