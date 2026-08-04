"""Off-cloud test for the distilled-insight layer + the Reports tab (no GCS, no network, no LLM).

Covers digest.py (dashboard sections for BOTH shapes, comms/tasks/intel snapshots), report_ai
(gather -> generate with and without a model -> revise -> the rendered deck), the workspace report
helpers (index entry + per-report HTML object), and the Flask routes (team generate/rename/delete,
the client-visible deck serve with lazy re-render, gating).

Run: python _report_localtest.py        # prints PASS / FAIL, exits 0 / 1
"""

import datetime
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


def _dash_rich():
    """A full-length flight with a real age x gender breakdown -- the shape the OPPORTUNITY facts
    need (a 14-vs-14 window, weekly cost pressure, the creative bench, the reallocation math)."""
    rows = []
    day = datetime.date(2026, 5, 1)
    while day <= datetime.date(2026, 7, 27):
        for ad, share in (("Video ad", 0.6), ("Static ad", 0.4)):
            rows.append({"date": day.isoformat(), "ad": ad, "camp": "Summer",
                         "spend": 40.0 * share, "imps": int(4000 * share),
                         "clicks": int(52 * share), "lclk": int(44 * share),
                         "reach": int(2600 * share),
                         "pur": 1 if day.day % 5 == 0 else 0,
                         "rev": (520.0 if day >= datetime.date(2026, 7, 14) else 210.0)
                                if day.day % 5 == 0 else 0.0})
        day += datetime.timedelta(days=1)
    age_gender = []
    for band, spend, cpc in (("65+", 700.0, 0.63), ("45-54", 600.0, 0.75), ("55-64", 760.0, 0.84),
                             ("35-44", 540.0, 1.19), ("25-34", 210.0, 1.23), ("18-24", 30.0, 0.90)):
        for gender, part in (("female", 0.58), ("male", 0.42)):
            clicks = spend * part / cpc
            age_gender.append({"date": "2026-07-01", "age": band, "gender": gender,
                               "spend": spend * part, "imps": clicks / 0.013,
                               "clicks": clicks, "lclk": clicks})
    return {"rows": rows, "demographics": {"age_gender": age_gender}}


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

    # The opportunity facts -- the derived numbers a media buyer acts on, not just descriptions.
    rich = report_ai.build_facts(_dash_rich())
    _check("a full flight yields the opportunity facts as well as the descriptive ones",
           {"recent_vs_prior", "pressure", "bench", "segments", "reallocation"} <= set(rich))
    _check("the before/after window is like for like and signed",
           rich["recent_vs_prior"]["delta"]["headline"].startswith(("+", "-"))
           and len(rich["recent_vs_prior"]["before"]["rows"]) > 1)
    _check("reallocation states the gain from the SAME budget, computed not estimated",
           len(rich["reallocation"]["points"]) == 2
           and "no extra budget" in rich["reallocation"]["summary"]
           and rich["reallocation"]["points"][1]["value"]
           > rich["reallocation"]["points"][0]["value"])
    _check("segments crosses age with gender and ranks the cells",
           len(rich["segments"]["rows"]) >= 3
           and {r.get("_tone") for r in rich["segments"]["rows"]} >= {"good", "bad"})

    # 🔴 A rounding error of the budget must never be crowned the best audience. Ranked on cost per
    # click alone, a 4-click cell won the REAL Riverdance breakdown at $0.18.
    noisy = _dash_rich()
    noisy["demographics"]["age_gender"].append(
        {"date": "2026-07-01", "age": "18-24", "gender": "unknown", "spend": 0.60,
         "imps": 40, "clicks": 4, "lclk": 4})
    nf = report_ai.build_facts(noisy)
    seg_cells = [r["seg"] for r in nf["segments"]["rows"]]
    _check("a cell with negligible spend is excluded from the ranked table",
           "18-24, unknown" not in seg_cells and "1% of spend" in nf["segments"]["subtitle"])
    crowned = [r for r in nf["age"]["rows"] if r.get("_tone")]
    _check("and it is never crowned best/worst where it IS shown",
           all(report_ai._has_volume(r) for r in crowned))
    _check("the bench fact counts the creatives carrying the account",
           any(i["key"] == "count" and i["value"] == "2" for i in rich["bench"]["items"]))
    _check("cost pressure reads the last week against the flight average",
           any("flight average" in i["note"] for i in rich["pressure"]["items"]))
    _check("week-over-week comparisons use the last COMPLETE week only",
           "complete week" in rich["pressure"]["subtitle"])

    # --- The reporting window: month-to-date / last-full-week ------------------------------------
    _check("report_window resolves both modes off the DATA's last day (2026-07-27, a Monday)",
           report_ai.report_window(_dash_rich(), "mtd") == ("2026-07-01", "2026-07-27")
           and report_ai.report_window(_dash_rich(), "last_week") == ("2026-07-20", "2026-07-26")
           and report_ai.report_window(_dash_rich(), "") is None)
    wf = report_ai.build_facts(_dash_rich(), window=("2026-07-20", "2026-07-26"))
    _check("a windowed pack answers for the WINDOW, not the flight ($280 = 7 days x $40)",
           "$280" in wf["totals"]["summary"]
           and wf["totals"]["title"] == "The window in numbers")
    _check("the windowed compare is THIS window against the equal days before it",
           "recent_vs_prior" in wf
           and "7 days" in wf["recent_vs_prior"]["before"]["subtitle"]
           and "7 days" in wf["recent_vs_prior"]["after"]["subtitle"])

    # --- The TEMPLATE dashboard shape: every client except the Windsor-live ones -----------------
    tdaily = []
    day = datetime.date(2026, 5, 1)
    n = 0
    while day <= datetime.date(2026, 7, 27):        # ends on a MONDAY: a 1-day trailing bucket
        n += 1
        tdaily.append({"date": day.isoformat(), "sessions": 300 + n * 3, "leads": 8 + (n % 5),
                       "spend": 120.0, "revenue": 900.0 + n * 8})
        day += datetime.timedelta(days=1)
    tf = report_ai.build_facts({"kpis": {"revenue": 98000, "spend": 10440, "leads": 740},
                                "daily": tdaily})
    _check("the template shape gets a series per metric, a like-for-like window AND a momentum table",
           {"totals", "recent_vs_prior", "momentum"} <= set(tf)
           and len([k for k in tf if k.startswith("weekly_")]) >= 3)
    _check("template columns are formatted by what their name says they are",
           any(i["value"].startswith("$") for i in tf["totals"]["items"]))
    # 🔴 The regression that would have shipped: a flight ending mid-week made a 1-day bucket look
    # like a collapse against a 7-day mean.
    _check("a PARTIAL trailing week never becomes a fake collapse in a comparison",
           all(not r["change"].startswith("-8") and not r["change"].startswith("-9")
               for r in tf["momentum"]["rows"])
           and "complete week" in tf["momentum"]["subtitle"])
    _check("the partial week is still CHARTED (it is real data, just not comparable)",
           tf["weekly_revenue"]["points"][-1]["label"] == "27 Jul")
    # A fixed weekly budget is a flat series: every point ties for max, so nothing is the best week.
    _check("a flat series marks NO best point (a tie is not a standout)",
           not any(p["best"] for p in tf["weekly_spend"]["points"])
           and any(p["best"] for p in tf["weekly_sessions"]["points"]))

    tdraft, _terr = report_ai.generate("Acme Co", "2026-07-29",
                                       report_ai.gather({}, [], {"kpis": {"revenue": 98000},
                                                                 "daily": tdaily}), None)
    # What MUST hold is that no computed fact is homeless -- a fact the spine never names could
    # never reach any deck, for any client.
    import report_spec
    homeless = {k for k in tf if not report_spec.claims(k)}
    _check("every computed fact is claimed by a spine slot (none can be orphaned)",
           not homeless)
    order = [sl["eyebrow"] for sl in report_spec.slots(("sales",))]
    _check("the no-AI deck IS the strict standard: cover + one slide per slot, in spine order",
           len(tdraft["slides"]) == report_spec.DECK_SLIDES
           and tdraft["slides"][0]["kind"] == "cover"
           and [sl["eyebrow"] for sl in tdraft["slides"][1:]] == order
           and [sl["slot"] for sl in tdraft["slides"][1:]] == list(report_spec.SLOT_KEYS))
    _check("there is no closing asks slide (asks live on the Tasks board)",
           not any((sl["title"] or "").lower().startswith("what we need") for sl in tdraft["slides"]))

    inputs = report_ai.gather(ws, _archives(), _dash_rd())
    _check("gather assembles every source block, facts included",
           inputs["business"] and inputs["media"] and inputs["voices"] and inputs["dashboard"]
           and inputs["blocked"] and inputs["facts"]["totals"] and inputs["period"])
    _check("voices carry the creator's summary",
           any("Fuel Your Wander" in v and "scenery" in v for v in inputs["voices"]))
    _check("the material handed to the model leads with the fact pack",
           "FACT PACK" in report_ai._material_text("Riverdance", "2026-07-29", inputs))
    winputs = report_ai.gather({}, [], _dash_rich(), window_mode="mtd")
    _check("gather stamps the chosen window on the period label",
           winputs["period"].startswith("Month to date") and winputs["window_mode"] == "mtd")

    draft = report_ai.draft_payload(inputs, client_name="Riverdance", when="2026-07-29")
    draft_blocks = []
    for _sl in draft["slides"]:
        for _b in _sl["blocks"]:
            draft_blocks.extend((_b["left"] + _b["right"]) if _b["type"] == "split" else [_b])
    _check("the no-AI deck is a REAL deck: cover + fact-backed visuals, one slide per slot",
           draft["slides"][0]["kind"] == "cover"
           and len(draft["slides"]) == report_spec.DECK_SLIDES
           and any(b["type"] in ("chart", "table", "kpis", "bullets") and b.get("fact")
                   for b in draft_blocks))
    _check("the no-AI deck draws the funnel beside its computed notes",
           any(b["type"] == "funnel" and b.get("fact") == "funnel" for b in draft_blocks))
    _check("the no-AI deck invents no analysis (no actions, no recommendations)",
           not any(b["type"] == "action" for b in draft_blocks))

    # A model deck that drifts EVERY way at once: a mislabeled eyebrow on a slot-tagged slide, a
    # slot recoverable only from its verbatim eyebrow, an off-spine section, missing slots, and
    # bad blocks. enforce_spine must pin all of it back to the strict eight.
    model_payload = json.dumps({
        "meta": {"headline": "We doubled daily revenue", "period": "1 - 8 Jul 2026",
                 "sources": "Meta Ads via Windsor.ai"},
        "slides": [
            {"kind": "cover", "title": "We doubled daily revenue in two weeks.",
             "subtitle": "The destination change did it.",
             "blocks": [{"type": "chips", "items": [{"label": "Window", "value": "Jul 2026"}]}]},
            {"kind": "content", "slot": "what_happened", "eyebrow": "The result",
             "title": "ROAS reached 7.71x",
             "tone": "good", "source": "Meta Ads via Windsor.ai",
             "blocks": [{"type": "text", "body": "Every dollar returned $7.71."},
                        {"type": "kpis", "fact": "totals"},
                        {"type": "chart", "fact": "weekly_roas", "caption": "By week"},
                        {"type": "chart", "fact": "invented_key"},
                        {"type": "table", "fact": "weekly_roas"},
                        {"type": "wormhole", "body": "nope"}]},
            {"kind": "content", "eyebrow": "Why it happened",
             "title": "The 45+ bands carry the account",
             "blocks": [{"type": "table", "fact": "age"}]},
            {"kind": "content", "slot": "next_steps", "eyebrow": "What we'll do",
             "title": "Three moves this week",
             "blocks": [{"type": "action", "body": "exclude 18-24, live this week"}]},
            {"kind": "section", "eyebrow": "Bonus thoughts", "title": "Off the spine."},
        ]})
    payload, err = report_ai.generate("Riverdance", "2026-07-29", inputs,
                                      lambda s, u: (model_payload, ""))
    order = [sl["eyebrow"] for sl in report_spec.slots(("sales",))]
    _check("generate pins the model deck to the strict eight, in spine order",
           err == "" and len(payload["slides"]) == report_spec.DECK_SLIDES
           and payload["slides"][0]["kind"] == "cover"
           and payload["slides"][0]["title"] == "We doubled daily revenue in two weeks."
           and [sl["eyebrow"] for sl in payload["slides"][1:]] == order)
    by_slot = {sl["slot"]: sl for sl in payload["slides"][1:]}
    _check("a slot-tagged slide keeps its argument but gets the CANONICAL eyebrow",
           by_slot["what_happened"]["title"] == "ROAS reached 7.71x"
           and by_slot["what_happened"]["eyebrow"] == "What happened")
    _check("a slide with only the verbatim eyebrow still lands on its slot",
           by_slot["why_happened"]["title"] == "The 45+ bands carry the account")
    _check("an off-spine slide is dropped and a missing slot is backfilled from the draft",
           not any("Bonus" in (sl["title"] or "") + (sl["eyebrow"] or "")
                   for sl in payload["slides"])
           and by_slot["tasks"]["eyebrow"] == "Tasks"
           and by_slot["funnel"]["eyebrow"] == "The funnel")
    result_blocks = by_slot["what_happened"]["blocks"]
    _check("a block referencing an UNKNOWN fact key is dropped, not rendered empty",
           not any(b.get("fact") == "invented_key" for b in result_blocks))
    _check("a fact used by the WRONG block type is dropped (a series is not a table)",
           not any(b["type"] == "table" for b in result_blocks))
    _check("an unknown block type is dropped",
           not any(b["type"] == "wormhole" for b in result_blocks))

    # `split` is what lets one slide carry a figure AND its reading -- the density fix.
    dense, derr = report_ai.generate("Riverdance", "2026-07-29", inputs, lambda s, u: (json.dumps({
        "slides": [{"kind": "content", "slot": "why_happened", "title": "Two things at once",
                    "blocks": [
            {"type": "split",
             "left": [{"type": "table", "fact": "age"}, {"type": "split", "left": [], "right": []}],
             "right": [{"type": "panel", "title": "What it means", "body": "The 45+ bands deliver."},
                       {"type": "chart", "fact": "nope"}]}]}]}), ""))
    split = next(sl for sl in dense["slides"] if sl["slot"] == "why_happened")["blocks"][0]
    _check("a split keeps a block per side and refuses to nest another split",
           derr == "" and split["type"] == "split" and len(split["left"]) == 1
           and len(split["right"]) == 1 and split["right"][0]["type"] == "panel")
    split_doc = report_ai.render_html("Riverdance", dense, "2026-07-29")
    _check("the split and its panel reach the deck",
           'class="split' in split_doc and 'class="panel"' in split_doc
           and "What it means" in split_doc)
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

    # Edit-with-AI: revise is deliberately NOT re-pinned to the spine (an instruction may add a
    # slide), and a slide-scoped edit pins the instruction to that one slide.
    def _edit_caller(store):
        def caller(system, user):
            store["user"] = user
            return (json.dumps({"slides": [
                {"kind": "content", "slot": "", "title": "Project X",
                 "blocks": [{"type": "bullets", "items": ["Send the Q4 dates"]}]}]}), "")
        return caller

    seen = {}
    revised, rerr = report_ai.revise(payload, "add a slide about project X", _edit_caller(seen))
    _check("revise applies the instruction and an ADDED slide survives (no spine re-pin)",
           rerr == "" and revised["slides"][0]["title"] == "Project X"
           and "Send the Q4 dates" in revised["slides"][0]["blocks"][0]["items"])
    _check("revise never loses the fact pack",
           revised["facts"]["totals"]["summary"] == facts["totals"]["summary"])
    scoped = {}
    report_ai.revise(payload, "tighten the title", _edit_caller(scoped), slide_no=2)
    _check("a slide-scoped revise pins the instruction to that slide",
           "ONLY to slide 2" in scoped["user"])
    _same, rerr_range = report_ai.revise(payload, "x", _edit_caller({}), slide_no=99)
    _check("an out-of-range slide number refuses cleanly", "no slide" in rerr_range)
    same, rerr2 = report_ai.revise(payload, "x", lambda s, u: ("", "model down"))
    _check("a failing revise returns the ORIGINAL payload + the reason",
           rerr2 == "model down" and same == payload)

    # --- The spine: Tasks, funnel, decomposition, targets ----------------------------------------
    import report_spec
    _check("the spine is seven slots and the deck is a strict eight, Tasks first",
           [sl["key"] for sl in report_spec.slots(("sales",))]
           == ["tasks", "research", "funnel", "what_happened", "why_happened",
               "next_steps", "opportunities"]
           and report_spec.DECK_SLIDES == 8)
    _check("eyebrows are identical for every objective (only the FACTS change vocabulary)",
           [sl["eyebrow"] for sl in report_spec.slots(("sales",))]
           == [sl["eyebrow"] for sl in report_spec.slots(("leadgen",))])
    _check("slot_of maps both the key and the verbatim eyebrow",
           report_spec.slot_of("tasks")["key"] == "tasks"
           and report_spec.slot_of("What we'll do")["key"] == "next_steps"
           and report_spec.slot_of("nonsense") is None)
    _check("the objective is inferred from the data when nothing is declared",
           report_ai.infer_objective(_dash_rd(), None) == "sales"
           and report_ai.infer_objective({"kpis": {"leads": 4}}, None) == "leadgen"
           and report_ai.infer_objective(_dash_rd(), {"primary": "leadgen"}) == "leadgen")

    # The Tasks slide: the client-safe board VERBATIM, under the Tasks tab's own column names.
    tv = [{"key": "todo", "name": "To do", "tasks": [{"title": "Email sequence"}]},
          {"key": "in_progress", "name": "In progress",
           "tasks": [{"title": "Ad set build", "due_date": "2026-08-08"}]},
          {"key": "blocked", "name": "Paused",
           "tasks": [{"title": "Geo expansion", "on_hold": True,
                      "hold_reason": "INTERNAL waiting on Ian"}]},
          {"key": "revision", "name": "Revision", "tasks": []},
          {"key": "completed", "name": "Completed",
           "tasks": [{"title": "LP switch", "completed_at": "2026-07-24"}]}]
    tfacts = {f["key"]: f for f in report_ai.tasks_facts(tv) if f}
    _check("the Tasks slide carries a count per column and the board verbatim",
           {"tasks_counts", "tasks_board"} <= set(tfacts)
           and any(i["label"] == "Paused" and i["value"] == "1"
                   for i in tfacts["tasks_counts"]["items"])
           and any(r["task"] == "Ad set build" and r["status"] == "In progress"
                   for r in tfacts["tasks_board"]["rows"]))
    # 🔴 The deck is CLIENT-VISIBLE. hold_reason is internal by design and must never cross, and
    # the column labels are the tab's own (a held card reads "Paused", never "Blocked").
    _check("the board wears the tab's own labels and an internal reason never crosses",
           "INTERNAL" not in json.dumps(tfacts)
           and any(r["status"] == "Paused" for r in tfacts["tasks_board"]["rows"]))
    _check("completed work is dated on the board",
           any("shipped" in r["when"] for r in tfacts["tasks_board"]["rows"]))

    _check("no target on file -> no vs_target fact (never an unagreed judgement)",
           "vs_target" not in report_ai.build_facts(_dash_rich(), None))
    with_target = report_ai.build_facts(_dash_rich(), {"primary": "sales", "conversion": {
        "sales": {"target_roas": "2.0", "target_aov": "300"}}})
    _check("a declared target produces the scorecard comparison, toned on/behind",
           "vs_target" in with_target
           and any(i.get("tone") in ("good", "bad") for i in with_target["vs_target"]["items"]))
    _check("the decomposition names WHICH factor moved",
           "moved on" in with_target["decomposition"]["title"])
    funnel = with_target.get("funnel")
    _check("the funnel is a DRAWN fact: ordered steps with a rate from the step above",
           funnel and funnel["kind"] == "funnel"
           and funnel["rows"][0]["rate"] == "-" and len(funnel["rows"]) >= 3)
    _check("the funnel notes name the constraint (computed, never invented)",
           "constraint" in " ".join(with_target["funnel_notes"]["items"]))

    # --- Brand kit: the client's crest + a palette parsed from their own brand guide -------------
    kit = report_ai.brand_kit(ws)
    _check("brand_kit takes the client crest and the brand guide's colours",
           kit["client_logo"].startswith("<svg") and kit["palette"]["accent"] == "#21582B")
    _check("a blank brand guide falls back to the AGORA house palette",
           report_ai.brand_kit({})["palette"] == report_ai.HOUSE_PALETTE)
    # A brand list nearly always includes a cream/off-white. It must never become type.
    cream = report_ai.palette_of("Deep pine #21582B, cream #F7F5E7, river green #2E7D43")
    _check("a near-white brand colour becomes the canvas, never an accent (it is unreadable as type)",
           cream["accent"] == "#21582B" and cream["accent2"] == "#2E7D43"
           and cream["canvas"] == "#F7F5E7")
    _check("a logo that is not our own self-contained markup is refused",
           report_ai.brand_kit({"brand": {"client_logo": "<img src=\"http://evil/x.png\">"}})
           ["client_logo"] == "")

    html_doc = report_ai.render_html("Riverdance", payload, "2026-07-29",
                                     title="July <script>alert(1)</script> Review", brand=kit)
    _check("the deck renders the strict eight, numbered",
           html_doc.count("<section class=\"slide") == 8 and "01 / 08" in html_doc
           and "08 / 08" in html_doc)
    _check("the model's claims and the computed numbers both reach the deck",
           "We doubled daily revenue in two weeks." in html_doc and "7.71x" in html_doc
           and "We'll action" in html_doc)
    _check("the funnel draws as a graph (bars + step rates), not a table",
           'class="funnel"' in html_doc and 'class="fstep"' in html_doc
           and 'class="frate"' in html_doc)
    _check("the deck wears the client's crest and palette",
           "#21582B" in html_doc and "--crest:url(" in html_doc)
    # 🔴 The marks are declared ONCE as CSS custom properties. Inlining the markup into every
    # slide's chrome made a 3-slide deck 1.9 MB (and a 14-slide one would have been ~8 MB) on a
    # route that is deliberately no-store.
    _check("each brand mark appears exactly once, however many slides there are",
           html_doc.count("--crest:url(") == 1 and html_doc.count("--agoramark:url(") == 1
           and html_doc.count("<svg") == 0)
    _check("mark_css_url handles both stored logo forms and refuses anything else",
           report_ai.mark_css_url("<svg xmlns='x'><rect/></svg>").startswith(
               "url(\"data:image/svg+xml;base64,")
           and report_ai.mark_css_url('<img src="data:image/png;base64,AAA">')
           == 'url("data:image/png;base64,AAA")'
           and report_ai.mark_css_url("<p>hello</p>") == ""
           and report_ai.mark_css_url("") == "")
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
           and "Riverdance" in r.get_data(as_text=True)
           and "01 /" in r.get_data(as_text=True))

    r = c.post("/w/%s/admin/report" % CLIENT,
               data={"op": "rename", "id": rid, "title": "Renamed deck"})
    _check("op=rename retitles + keeps the deck serving",
           r.get_json()["ok"] is True
           and workspace.find_report(workspace.load_workspace(CLIENT),
                                     rid)["title"] == "Renamed deck")

    r = c.post("/w/%s/admin/report" % CLIENT,
               data={"op": "revise", "id": rid, "instruction": "add a slide about project X"})
    _check("op=revise without an AI model refuses honestly and changes nothing",
           r.get_json()["ok"] is False and "no AI model" in r.get_json()["message"])
    r = c.post("/w/%s/admin/report" % CLIENT, data={"op": "revise", "id": rid})
    _check("op=revise without an instruction asks for one",
           r.get_json()["ok"] is False and "Describe" in r.get_json()["message"])

    body = c.get("/w/%s/reports" % CLIENT).get_data(as_text=True)
    _check("the Reports tab renders the deck card + team controls for the team",
           'data-pane="reports"' in body and "Renamed deck" in body
           and "data-repgen>" in body and 'data-repdel="' in body
           and "data-repperiod" in body and 'data-repedit="' in body
           and "data-repai" in body)

    # A deleted deck object re-renders lazily from the stored payload (the restore path).
    workspace._delete_object(workspace.report_object_name(CLIENT, rid))
    r = c.get("/w/%s/report/%s" % (CLIENT, rid))
    _check("a missing deck object re-renders lazily from the stored payload",
           r.status_code == 200 and "Riverdance" in r.get_data(as_text=True)
           and "01 /" in r.get_data(as_text=True))

    with c.session_transaction() as s:
        s.clear(); s.update(CLIENT_LOGIN)
    # The generate/delete BUTTONS must never reach a client's HTML (the JS selector literals do
    # ship to every viewer, like the other admin wiring -- the markup is what matters).
    body = c.get("/w/%s/reports" % CLIENT).get_data(as_text=True)
    # (The JS selector literal `[data-repslides="` ships to every viewer like the rest of the
    # admin wiring -- the MARKUP form `hidden data-repslides=` is what must never leak.)
    _check("the client sees the cards but NO team controls",
           "Renamed deck" in body and "data-repgen>" not in body
           and 'data-repdel="' not in body and 'data-repedit="' not in body
           and "hidden data-repslides=" not in body)
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
