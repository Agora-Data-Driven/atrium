"""Off-cloud test for the distilled-insight layer + the Reports tab (no GCS, no network, no LLM).

Covers digest.py (dashboard sections for BOTH shapes, comms/tasks/intel snapshots), report_ai
(gather -> generate with and without a model -> revise -> the rendered deck), the workspace report
helpers (index entry + per-report HTML object), and the Flask routes (team generate/rename/delete,
the client-visible deck serve with lazy re-render, gating).

Run: python _report_localtest.py        # prints PASS / FAIL, exits 0 / 1
"""

import json
import os
import re
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

    # --- report_ai: facts -> gather -> generate (model + no-model deck) -> revise -> render ------
    ws = workspace.load_workspace(CLIENT)
    ws["tasks"] = [task]
    # A brand guide with hex codes in it: the deck palette is parsed straight out of this text.
    ws.setdefault("company", {})["brand"] = {"colors": "Deep pine #21582B on cream #F7F5E7"}

    facts = report_ai.build_facts(_dash_rd())
    _check("build_facts derives the whole numeric pack from the raw export",
           {"totals", "ads", "age", "region", "email"} <= set(facts)
           and facts["totals"]["kind"] == "tiles" and facts["ads"]["kind"] == "table")
    _check("computed totals match the rows (spend $350, revenue $2,700)",
           "$350" in facts["totals"]["summary"] and "$2,700" in facts["totals"]["summary"])
    _check("weekly series carry >= 2 points and mark the best one",
           len(facts["weekly_roas"]["points"]) >= 2
           and any(p["best"] for p in facts["weekly_roas"]["points"]))
    _check("a ranked table stamps its own best/worst row (the renderer never decides tone)",
           {r.get("_tone") for r in facts["ads"]["rows"]} >= {"good", "bad"})
    _check("too short a flight yields NO before/after fact rather than a fake one",
           "recent_vs_prior" not in facts)
    _check("the fact catalogue names every key for the model",
           any(line.startswith("ads [table]") for line in report_ai.facts_catalogue(facts)))

    inputs = report_ai.gather(ws, _archives(), _dash_rd())
    _check("gather assembles every source block, facts included",
           inputs["business"] and inputs["media"] and inputs["voices"] and inputs["dashboard"]
           and inputs["blocked"] and inputs["facts"]["totals"] and inputs["period"])
    _check("voices carry the creator's summary",
           any("Fuel Your Wander" in v and "scenery" in v for v in inputs["voices"]))
    _check("the material handed to the model leads with the fact pack",
           "FACT PACK" in report_ai._material_text("Riverdance", "2026-07-29", inputs))

    draft = report_ai.draft_payload(inputs, client_name="Riverdance", when="2026-07-29")
    draft_blocks = [b for s in draft["slides"] for b in s["blocks"]]
    _check("the no-AI deck is a REAL deck: cover, fact-backed visuals, honest asks",
           draft["slides"][0]["kind"] == "cover"
           and any(b["type"] in ("chart", "table", "kpis") and b.get("fact") for b in draft_blocks)
           and draft["slides"][-1]["title"] == "What We Need From You")
    _check("the no-AI deck invents no analysis (no actions, no recommendations)",
           not any(b["type"] == "action" for b in draft_blocks))

    model_payload = json.dumps({
        "meta": {"headline": "We doubled daily revenue", "period": "1 - 8 Jul 2026",
                 "sources": "Meta Ads via Windsor.ai"},
        "slides": [
            {"kind": "cover", "title": "We doubled daily revenue in two weeks.",
             "subtitle": "The destination change did it.",
             "blocks": [{"type": "chips", "items": [{"label": "Window", "value": "Jul 2026"}]}]},
            {"kind": "content", "eyebrow": "The result", "title": "ROAS reached 7.71x",
             "tone": "good", "source": "Meta Ads via Windsor.ai",
             "blocks": [{"type": "text", "body": "Every dollar returned $7.71."},
                        {"type": "kpis", "fact": "totals"},
                        {"type": "chart", "fact": "weekly_roas", "caption": "By week"},
                        {"type": "chart", "fact": "invented_key"},
                        {"type": "table", "fact": "weekly_roas"},
                        {"type": "wormhole", "body": "nope"}]},
            {"kind": "section", "eyebrow": "Part two", "title": "Where the next gains are."},
            {"kind": "content", "title": "We overpay for the youngest band",
             "blocks": [{"type": "table", "fact": "age"},
                        {"type": "action", "body": "exclude 18-24, live this week"}]},
            {"kind": "closing", "title": "What we need from you",
             "blocks": [{"type": "bullets", "items": ["Approve the August plan"]}]},
        ]})
    payload, err = report_ai.generate("Riverdance", "2026-07-29", inputs,
                                      lambda s, u: (model_payload, ""))
    _check("generate parses the model deck", err == "" and len(payload["slides"]) == 5
           and payload["slides"][0]["kind"] == "cover")
    result_blocks = payload["slides"][1]["blocks"]
    _check("a block referencing an UNKNOWN fact key is dropped, not rendered empty",
           not any(b.get("fact") == "invented_key" for b in result_blocks))
    _check("a fact used by the WRONG block type is dropped (a series is not a table)",
           not any(b["type"] == "table" for b in result_blocks))
    _check("an unknown block type is dropped",
           not any(b["type"] == "wormhole" for b in result_blocks))
    _check("the computed facts ride inside the payload (so a re-render is identical)",
           payload["facts"]["totals"]["summary"] == facts["totals"]["summary"])

    payload_no_ai, err2 = report_ai.generate("Riverdance", "2026-07-29", inputs, None)
    _check("generate without a model returns the deterministic deck + the reason",
           err2 == "no AI model configured" and payload_no_ai["slides"])
    _bad, err3 = report_ai.generate("Riverdance", "2026-07-29", inputs,
                                    lambda s, u: ("not json at all", ""))
    _check("unusable model output degrades to the deterministic deck with a reason", "JSON" in err3)
    _empty, err4 = report_ai.generate("Riverdance", "2026-07-29", inputs,
                                      lambda s, u: ('{"slides": []}', ""))
    _check("a model deck with no usable slides degrades too", "slides" in err4)

    revised, rerr = report_ai.revise(payload, "add a bullet",
                                     lambda s, u: (model_payload.replace(
                                         "Approve the August plan",
                                         "Approve the August plan\", \"Send the Q4 dates"), ""))
    _check("revise applies the instruction",
           rerr == "" and "Send the Q4 dates" in revised["slides"][-1]["blocks"][0]["items"])
    _check("revise never loses the fact pack",
           revised["facts"]["totals"]["summary"] == facts["totals"]["summary"])
    same, rerr2 = report_ai.revise(payload, "x", lambda s, u: ("", "model down"))
    _check("a failing revise returns the ORIGINAL payload + the reason",
           rerr2 == "model down" and same == payload)

    # --- Brand kit: the client's crest + a palette parsed from their own brand guide -------------
    kit = report_ai.brand_kit(ws)
    _check("brand_kit takes the client crest and the brand guide's colours",
           kit["client_logo"].startswith("<svg") and kit["palette"]["accent"] == "#21582B")
    _check("a blank brand guide falls back to the AGORA house palette",
           report_ai.brand_kit({})["palette"] == report_ai.HOUSE_PALETTE)
    _check("a logo that is not our own self-contained markup is refused",
           report_ai.brand_kit({"brand": {"client_logo": "<img src=\"http://evil/x.png\">"}})
           ["client_logo"] == "")

    html_doc = report_ai.render_html("Riverdance", payload, "2026-07-29",
                                     title="July <script>alert(1)</script> Review", brand=kit)
    _check("the deck renders one slide per payload slide, numbered",
           html_doc.count("<section class=\"slide") == 5 and "01 / 05" in html_doc
           and "05 / 05" in html_doc)
    _check("the model's claims and the computed numbers both reach the deck",
           "We doubled daily revenue in two weeks." in html_doc and "7.71x" in html_doc
           and "Where the next gains are." in html_doc and "We'll action" in html_doc)
    _check("the deck wears the client's crest and palette",
           "#21582B" in html_doc and kit["client_logo"][:40] in html_doc)
    _check("deck HTML is escaped", "<script>alert(1)</script>" not in html_doc)
    _check("the deck is self-contained (no remote assets)",
           not re.search(r"(?:src|href)=\"https?://|url\(\s*['\"]?https?://", html_doc))

    legacy = {"landscape": {"business": [{"title": "B1", "body": "b"}], "media": [],
                            "voices": [{"title": "V1", "body": "v"}]},
              "what_happened": {"summary": "Strong month.",
                                "numbers": [{"label": "Spend", "value": "$350"}],
                                "whats_working": ["Video creative"]},
              "why": ["Seasonality"], "recommendations": ["Shift budget to video"],
              "asks": {"needed": ["Approve August plan"], "blocked": []}}
    legacy_doc = report_ai.render_html("Riverdance", legacy, "2026-07-29")
    _check("a deck stored under the OLD payload shape still renders",
           "The Landscape" in legacy_doc and "What Happened" in legacy_doc
           and "$350" in legacy_doc and "What We Need From You" in legacy_doc)

    empty_doc = report_ai.render_html("Riverdance", {}, "2026-07-01")
    _check("an empty payload still opens on an honest cover",
           "July 1, 2026" in empty_doc and "Nothing to report yet" in empty_doc)

    # The deck ships one inline script (the slide navigator). It must clear the SAME esprima gate
    # every template in this repo clears -- no `?.`, no `??` (CI installs esprima; skip locally).
    try:
        import esprima
        script = html_doc.split("<script>")[1].split("</script>")[0]
        esprima.parseScript(script)
        _check("the deck's navigator parses under the esprima 4 gate", True)
    except ImportError:
        print("  [SKIP] esprima not installed - navigator syntax not checked here")

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
