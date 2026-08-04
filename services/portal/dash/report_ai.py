"""Report maker: the client meeting deck, drafted from the same distilled layer the Assistant reads.

One report = one self-contained HTML deck (a fixed 16:9 stage, scaled to the window, arrow-key
navigable, printable to PDF) stored as its own object and listed date-first on the Reports tab.
HTML over Google Slides on purpose: no new infra/APIs/credentials, it opens in one click inside the
authed portal, and it matches the house pattern (the hand-built Riverdance deck is HTML too).

WHAT A DECK IS MADE OF (rebuilt 2026-07-29 -- the old fixed six-slide shape could only hold
`{title, body}` bullets, so every chart, table and before/after in a hand-built deck collapsed into
prose):

  payload = {"meta":   {headline, subhead, period, sources},
             "facts":  {key: fact, ...},        # server-COMPUTED datasets (see below)
             "slides": [{kind, eyebrow, title, subtitle, tone, source, blocks: [...]}]}

  kind   = cover | section | content | closing        (one layout each, same block list)
  blocks = text | bullets | cards | callout | action | chips | kpis | chart | table | compare
           | funnel (the drawn funnel graph) -- and each slide carries `slot`, the report_spec
           spine slot it fills ("" = off-spine, e.g. a slide an AI edit added on purpose)

🔴 **A generated deck is EXACTLY EIGHT SLIDES** (2026-08-04): cover + one slide per report_spec
slot (tasks, research, funnel, what_happened, why_happened, next_steps, opportunities). The model
is told the contract AND `enforce_spine` pins the result to it in code -- off-spine slides drop,
missing slots backfill from the deterministic draft. `revise` deliberately does NOT re-enforce, so
an explicit edit ("add a slide about project X") can grow a stored deck past eight.

🔴 **Numbers in a visual are never model-written.** `build_facts` derives every series, table,
before/after and KPI tile from the dashboard export in plain Python; a `chart`/`table`/`compare`
block only carries a `fact` KEY, and the renderer draws from the stored fact. The model chooses
WHICH fact a slide shows and writes the argument around it. A hallucinated key renders nothing
rather than a wrong number, and because the facts ride inside the payload the deck re-renders
identically forever (the lazy re-render path after a Trash restore).

Split of labor (mirrors the intel brain): `gather` assembles source material from digest.py + the
workspace + `build_facts` (pure), `generate` asks the configured model for the slide payload (JSON
contract, parsed leniently), `draft_payload` is the NO-AI fallback -- and it is a real deck now, not
a stub, because the facts alone carry the whole numeric story -- `revise` applies a team instruction
to an existing payload (the Assistant's edit-report action), and `render_html` turns any payload
into the deck. Everything degrades, nothing raises: (payload, error) out of every AI path.

BRANDING: the deck wears the client's identity -- their crest from `ws["brand"]["client_logo"]` and
a palette parsed out of the Company tab's brand guide (`company.brand.colors`), falling back to the
AGORA house palette. `brand_kit(ws)` is the one call that assembles it.

LEGACY: decks written before the rebuild stored the old `{landscape, what_happened, why,
recommendations, asks}` payload. `normalize_payload` converts that shape to slides on read, so every
stored report keeps rendering (`_legacy_slides`).
"""

import base64
import datetime
import html
import json
import math
import re

import digest
import report_spec

# The client-facing name for the watched-creators/competitors section of the Landscape slide.
VOICES_LABEL = "Market Voices"

# The deck is a fixed 16:9 stage scaled to the viewer's window -- a slide is a slide, not a
# reflowing web page, so what the team previews is what the client sees on the call.
STAGE_W = 1280
STAGE_H = 720

_MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August",
           "September", "October", "November", "December")
_MON_SHORT = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def date_label(iso):
    """'2026-07-29' -> 'July 29, 2026' (falls back to the raw string)."""
    try:
        d = datetime.date.fromisoformat((iso or "")[:10])
    except ValueError:
        return iso or ""
    return "%s %d, %d" % (_MONTHS[d.month - 1], d.day, d.year)


def short_date(iso):
    """'2026-07-20' -> '20 Jul' (chart axis labels; falls back to the raw string)."""
    try:
        d = datetime.date.fromisoformat((iso or "")[:10])
    except ValueError:
        return iso or ""
    return "%d %s" % (d.day, _MON_SHORT[d.month - 1])


def period_label(first, last):
    """'13 May - 27 Jul 2026' from two ISO dates (either side may be missing)."""
    if not first or not last:
        return short_date(first) or short_date(last) or ""
    try:
        a = datetime.date.fromisoformat(first[:10])
        b = datetime.date.fromisoformat(last[:10])
    except ValueError:
        return "%s - %s" % (first, last)
    if a.year == b.year:
        return "%d %s - %d %s %d" % (a.day, _MON_SHORT[a.month - 1],
                                     b.day, _MON_SHORT[b.month - 1], b.year)
    return "%s - %s" % (date_label(first), date_label(last))


# --- 0. Number formatting (the deck's own, so a tile and a chart label always agree) --------------
def _num(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _money(v, cents=True):
    return "$%s" % format(round(_num(v), 2 if cents else 0), ",.2f" if cents else ",.0f")


def _int_fmt(v):
    return format(int(round(_num(v))), ",d")


def _pct(part, whole, places=2):
    return ("%.*f%%" % (places, 100.0 * part / whole)) if whole else "n/a"


def _ratio(a, b):
    return ("%.2fx" % (a / b)) if b else "n/a"


def _delta_pct(now, before):
    """Signed percent change 'now vs before', or '' when there is no baseline."""
    if not before:
        return ""
    return "%+.0f%%" % (100.0 * (now - before) / before)


def _div(a, b):
    return (a / b) if b else 0.0


# --- 1. Facts: the deterministic datasets every visual is drawn from ------------------------------
# A fact is a small self-describing dataset:
#   {"key", "kind": series|table|compare|tiles, "title", "subtitle", "summary", ...payload}
# `summary` is the one-line form the MODEL reads (so it can quote the numbers in prose); the rest is
# what the RENDERER draws. Nothing here is ever asked of the model.
_FACT_KINDS = ("series", "table", "compare", "tiles", "list", "funnel")


def _roll(rows, key=None):
    """Aggregate Windsor-live rows, optionally grouped by `key`.

    digest._roll exists for the same shape, but the deck quotes reach and booking-initiated on its
    KPI tiles and digest's line format carries neither -- so this one keeps the wider field set.
    Returns {name: agg} when grouped, a single agg dict otherwise."""
    def blank():
        return {"spend": 0.0, "imps": 0, "clicks": 0, "lclk": 0, "reach": 0,
                "pur": 0.0, "rev": 0.0, "init": 0.0}

    out = {}
    for r in rows or []:
        name = (r.get(key) or "(unlabeled)") if key else "all"
        agg = out.setdefault(name, blank())
        agg["spend"] += _num(r.get("spend"))
        agg["imps"] += int(_num(r.get("imps")))
        agg["clicks"] += int(_num(r.get("clicks")))
        agg["lclk"] += int(_num(r.get("lclk")))
        agg["reach"] += int(_num(r.get("reach")))
        agg["pur"] += _num(r.get("pur"))
        agg["rev"] += _num(r.get("rev"))
        agg["init"] += _num(r.get("book_init"))
    if key:
        return out
    return out.get("all") or {"spend": 0.0, "imps": 0, "clicks": 0, "lclk": 0, "reach": 0,
                              "pur": 0.0, "rev": 0.0, "init": 0.0}


def _derive(a):
    """Every derived metric the deck quotes, from one aggregate."""
    clicks = a["lclk"] or a["clicks"]        # link clicks when Meta reports them, else all clicks
    return {
        "spend": a["spend"], "revenue": a["rev"], "purchases": a["pur"],
        "impressions": a["imps"], "clicks": a["clicks"], "link_clicks": a["lclk"],
        "reach": a["reach"], "initiated": a["init"],
        "roas": _div(a["rev"], a["spend"]),
        "ctr": _div(a["clicks"], a["imps"]),
        "cpc": _div(a["spend"], clicks),
        "cpm": _div(1000.0 * a["spend"], a["imps"]),
        "aov": _div(a["rev"], a["pur"]),
        "cpa": _div(a["spend"], a["pur"]),
        "rev_per_click": _div(a["rev"], clicks),
    }


def _weeks(rows):
    """[(monday_iso, agg, days_in_bucket)] over Windsor-live rows, chronological.

    The day count is load-bearing: a flight almost always ends mid-week, so the final bucket is a
    PARTIAL week. Charting it is fine (it is real data), but comparing it to a full-week average
    reads as a collapse -- a 1-day tail against a 7-day mean printed "-80%" on a client's deck.
    Anything that COMPARES weeks must filter on this count."""
    buckets = {}
    for r in rows or []:
        try:
            d = datetime.date.fromisoformat((r.get("date") or "")[:10])
        except ValueError:
            continue
        wk = (d - datetime.timedelta(days=d.weekday())).isoformat()
        buckets.setdefault(wk, {"rows": [], "days": set()})
        buckets[wk]["rows"].append(r)
        buckets[wk]["days"].add(d.isoformat())
    return [(wk, _roll(buckets[wk]["rows"]), len(buckets[wk]["days"])) for wk in sorted(buckets)]


def _series(key, title, subtitle, points, orient="columns", best="high"):
    """A chartable series. `points` = [{label, value, text, note}]; the best point is marked so the
    renderer can highlight it without deciding anything."""
    points = [p for p in points if p.get("text")]
    if len(points) < 2:
        return None
    values = [_num(p.get("value")) for p in points]
    target = max(values) if best == "high" else min(values)
    # A standout needs to actually stand out: a flat series (every week the same fixed budget) has
    # no best week, and a tie is not a winner. Marking all of them "BEST" was the giveaway.
    winners = [i for i, v in enumerate(values) if v == target]
    single = bool(best) and len(winners) == 1 and max(values) > min(values)
    for i, p in enumerate(points):
        p["best"] = single and i == winners[0]
    return {"key": key, "kind": "series", "title": title, "subtitle": subtitle,
            "orient": orient, "points": points,
            "summary": "%s: %s." % (title, ", ".join("%s %s" % (p["label"], p["text"])
                                                     for p in points))}


def _table(key, title, subtitle, columns, rows, note=""):
    """A tabular fact. `columns` = [{key,label,align}], `rows` = [{colkey: text, "_tone": ...}]."""
    if not rows:
        return None
    head = " | ".join(c["label"] for c in columns)
    lines = [" | ".join(str(r.get(c["key"], "")) for c in columns) for r in rows]
    return {"key": key, "kind": "table", "title": title, "subtitle": subtitle,
            "columns": columns, "rows": rows, "note": note,
            "summary": "%s: %s -- %s" % (title, head, "; ".join(lines))}


def _tiles(key, title, items, subtitle=""):
    items = [i for i in items if i.get("value")]
    if not items:
        return None
    return {"key": key, "kind": "tiles", "title": title, "subtitle": subtitle, "items": items,
            "summary": "%s: %s." % (title, ", ".join("%s %s" % (i["label"], i["value"])
                                                     for i in items))}


# 🔴 A cell holding a rounding error of the budget cannot be "the best audience in the account".
# Ranked by cost per click alone, "18-24, unknown" at 0.0% of spend and 4 clicks won the real
# Riverdance breakdown -- a recommendation built on noise. Anything ranked needs real volume behind
# it: 1% of spend, or 30 link clicks.
_MIN_SHARE = 0.01
_MIN_CLICKS = 30


def _has_volume(row):
    """True when a row carries enough delivery to be ranked or crowned.

    The floor is OPT-IN per table: a row that declares no volume signal at all is always eligible,
    so a table which is already ranked by spend (ads, campaigns) keeps its best/worst marks.""" 
    if "_share" not in row and "_clicks" not in row:
        return True
    return (_num(row.get("_share")) >= _MIN_SHARE
            or _num(row.get("_clicks")) >= _MIN_CLICKS)


def _list_fact(key, title, subtitle, items):
    """An ordered enumeration (delivery status, blockers, shipped work) as a fact.

    A fact and not model prose because these strings are the CLIENT-SAFE projection of the task
    board -- the moment a model rewrites them it can invent a reason or leak an internal one."""
    items = [str(i).strip() for i in (items or []) if str(i).strip()]
    if not items:
        return None
    return {"key": key, "kind": "list", "title": title, "subtitle": subtitle, "items": items,
            "summary": "%s: %s" % (title, "; ".join(items))}


def _tone_rows(rows, tone_key, direction="low_good"):
    """Mark the best/worst row on a numeric column so a table reads at a glance. The renderer never
    decides tone -- it just paints what is stamped here. Rows without meaningful volume are never
    crowned (see _MIN_SHARE)."""
    vals = [(_num(r.get("_" + tone_key)), r) for r in rows
            if r.get("_" + tone_key) is not None and _has_volume(r)]
    if len(vals) < 2:
        return rows
    ordered = sorted(vals, key=lambda kv: kv[0])
    best, worst = (ordered[0], ordered[-1]) if direction == "low_good" else (ordered[-1], ordered[0])
    best[1]["_tone"] = "good"
    worst[1]["_tone"] = "bad"
    return rows


def _window_compare(rows, dates, span=14):
    """The last `span` days against the `span` before them -- the like-for-like default."""
    if len(dates) < span + 2:
        return None
    after_days, before_days = set(dates[-span:]), set(dates[-2 * span:-span])
    if not before_days:
        return None
    return _compare_days(rows, before_days, after_days)


def _compare_days(rows, before_days, after_days):
    """The before/after compare fact over two EXPLICIT day sets (the windowed report path derives
    its own: this window against the equal days before it)."""
    after = _derive(_roll([r for r in rows if (r.get("date") or "")[:10] in after_days]))
    before = _derive(_roll([r for r in rows if (r.get("date") or "")[:10] in before_days]))
    n_before, n_after = len(before_days), len(after_days)

    def side(d, days, first, last):
        rev_day = _div(d["revenue"], days)
        out = [{"label": "Spend", "value": _money(d["spend"], cents=False)}]
        if d["revenue"]:
            out += [{"label": "Revenue", "value": _money(d["revenue"], cents=False)},
                    {"label": "Revenue / day", "value": _money(rev_day)},
                    {"label": "Average order", "value": _money(d["aov"], cents=False)},
                    {"label": "ROAS", "value": "%.2fx" % d["roas"]}]
        out.append({"label": "Link CTR", "value": "%.2f%%" % (100.0 * d["ctr"])})
        return {"title": period_label(first, last), "subtitle": "%d days" % days, "rows": out}

    b_days = sorted(before_days)
    a_days = sorted(after_days)
    headline = _delta_pct(_div(after["revenue"], n_after), _div(before["revenue"], n_before))
    metric = "revenue per day"
    if not after["revenue"] and not before["revenue"]:
        headline = _delta_pct(_div(after["clicks"], n_after), _div(before["clicks"], n_before))
        metric = "clicks per day"
    return {"key": "recent_vs_prior", "kind": "compare",
            "title": "The last %d days against the %d before" % (n_after, n_before),
            "subtitle": "Like for like, %s spend against %s"
                        % (_money(after["spend"], cents=False), _money(before["spend"], cents=False)),
            "before": side(before, n_before, b_days[0], b_days[-1]),
            "after": side(after, n_after, a_days[0], a_days[-1]),
            "delta": {"headline": headline or "flat", "label": metric,
                      "tone": "good" if headline.startswith("+") else
                              ("bad" if headline.startswith("-") else "neutral")},
            "summary": "Last %d days vs the %d before: %s %s (spend %s vs %s)."
                       % (n_after, n_before, headline or "flat", metric,
                          _money(after["spend"], cents=False), _money(before["spend"], cents=False))}


def tasks_facts(tasks_view):
    """The Tasks slide's facts: the client-safe board VERBATIM -- a count per column plus every
    card under the column name the Tasks tab itself shows (To do / In progress / Paused /
    Revision / Completed for a client viewer). Nothing renamed, nothing editorialized.

    🔴 Never from `ws["tasks"]` directly. The raw task carries owners, `service_charge`,
    `internal_notes` and `hold_reason`, all of which are deliberately stripped before reaching a
    client's HTML -- and this deck IS client-visible. `main._progress_tasks(ws)` is the one
    client-safe projection, and its column `name` labels are the Tasks tab's own."""
    counts, rows, dropped = [], [], 0
    for col in tasks_view or []:
        label = (col.get("name") or col.get("key") or "").strip() or "?"
        bucket = col.get("tasks") or []
        counts.append({"key": col.get("key") or label, "label": label,
                       "value": str(len(bucket)), "note": "",
                       "tier": "primary" if bucket else "secondary"})
        if (col.get("key") == "completed") and len(bucket) > _TASKS_DONE_CAP:
            # A board accumulates finished work forever; the slide shows the freshest few and
            # says so, the count tile above still carries the true total.
            bucket = sorted(bucket, key=lambda t: t.get("completed_at") or "",
                            reverse=True)[:_TASKS_DONE_CAP]
            dropped += 1
        for t in bucket:
            title = (t.get("title") or "").strip() or "(untitled)"
            status = label
            if t.get("on_hold") and col.get("key") != "blocked":
                status += ", paused"
            when = ""
            if col.get("key") == "completed" and t.get("completed_at"):
                when = "shipped %s" % short_date((t.get("completed_at") or "")[:10])
            elif t.get("due_date"):
                when = "launching %s" % short_date(t.get("due_date"))
            rows.append({"task": title, "status": status, "when": when})
    board = _table("tasks_board", "The board, as it stands",
                   "Straight from the Tasks tab",
                   [{"key": "task", "label": "Task"},
                    {"key": "status", "label": "Status"},
                    {"key": "when", "label": "Timing", "align": "right"}], rows[:_TASKS_ROW_CAP],
                   note=("Completed work shows the %d most recent items." % _TASKS_DONE_CAP)
                        if dropped else "")
    tiles = _tiles("tasks_counts", "Where the work stands", counts,
                   subtitle="One tile per Tasks-tab column")
    return [tiles, board]


_TASKS_DONE_CAP = 6
_TASKS_ROW_CAP = 14


def _funnel_fact(total, objective, engagement_label=""):
    """Every step from impression to conversion, as a DRAWN funnel (kind `funnel`).

    No best/worst tone: step rates are not comparable to each other (impression-to-click is always
    ~1%, click-to-purchase always lower), so crowning one would be meaningless. The companion
    `funnel_notes` fact carries the computed observations for the bullets beside the graph."""
    spec = report_spec.CONVERSION.get(objective) or {}
    # The last step is a COUNT of conversions, so it wears the unit ("purchases"/"leads") or the
    # client's own word for it -- never the value label ("Revenue"), which is not a funnel step.
    conv_label = (engagement_label or spec.get("unit") or "conversions").strip().capitalize()
    clicks = total["link_clicks"] or total["clicks"]
    steps = [("Impressions", total["impressions"]), ("Link clicks", clicks)]
    if total.get("initiated"):
        steps.append(("Checkout started", total["initiated"]))
    steps.append((conv_label, total["purchases"]))
    rows, prev, inconsistent = [], None, False
    for name, value in steps:
        if not value:
            continue
        # A rate above 100% is not a funnel, it is two sources disagreeing (Meta's
        # checkout-started and the purchase count are measured differently). Say so rather than
        # printing an impossible percentage.
        rate = _pct(value, prev, 2) if prev else "-"
        if prev and value > prev:
            rate = "n/a"
            inconsistent = True
        rows.append({"step": name, "volume": _int_fmt(value), "rate": rate,
                     "_value": float(value)})
        prev = value
    if len(rows) < 3:
        return None
    note = ("One step counts higher than the step above it, so those two are measured differently "
            "and their rate is left out.") if inconsistent else ""
    head = " | ".join(("Step", "Volume", "From the step above"))
    lines = [" | ".join((r["step"], r["volume"], r["rate"])) for r in rows]
    return {"key": "funnel", "kind": "funnel",
            "title": "The path from impression to %s" % conv_label.lower(),
            "subtitle": "Each rate is the share of the step above it",
            "rows": rows, "note": note,
            "summary": "%s: %s -- %s" % ("The path from impression to %s" % conv_label.lower(),
                                         head, "; ".join(lines))}


def _funnel_notes(funnel_fact):
    """Computed observations beside the funnel graph -- arithmetic over the fact, never invention.

    The no-AI deck uses these verbatim; a model may write sharper ones from the same numbers."""
    if not funnel_fact:
        return None
    rows = funnel_fact.get("rows") or []
    notes = []
    pairs = []
    for above, below in zip(rows, rows[1:]):
        if below.get("rate") not in ("-", "n/a", ""):
            pairs.append((above["step"], below["step"], below["rate"],
                          float(str(below["rate"]).rstrip("%") or 0)))
    for above, below, rate, _v in pairs:
        notes.append("%s to %s converts at %s." % (above, below, rate))
    if len(pairs) > 1:
        weakest = min(pairs, key=lambda p: p[3])
        notes.append("The steepest drop is %s to %s (%s) - that step is the constraint."
                     % (weakest[0], weakest[1], weakest[2]))
    if funnel_fact.get("note"):
        notes.append(funnel_fact["note"])
    return _list_fact("funnel_notes", "What the funnel says",
                      "Computed from the steps beside it", notes)


def _decomposition_fact(rows, dates, objective, span=14):
    """WHY the headline moved: the two factors it is the product of.

    Revenue is purchases x average order value; leads are clicks x conversion rate. Exactly one of
    the two usually moved, and the two imply completely different next actions -- which is the
    single most useful thing a performance report can say and almost none of them do."""
    if len(dates) < span + 2:
        return None
    after_days, before_days = set(dates[-span:]), set(dates[-2 * span:-span])
    if not before_days:
        return None
    after = _derive(_roll([r for r in rows if (r.get("date") or "")[:10] in after_days]))
    before = _derive(_roll([r for r in rows if (r.get("date") or "")[:10] in before_days]))
    conv = report_spec.CONVERSION.get(objective) or report_spec.CONVERSION["sales"]
    if objective == "leadgen":
        factors = [("Link clicks", "clicks", _int_fmt),
                   ("Conversion rate", "cvr", lambda v: "%.2f%%" % (100.0 * v))]
        head = ("Leads", "purchases", _int_fmt)
        after["cvr"] = _div(after["purchases"], after["link_clicks"] or after["clicks"])
        before["cvr"] = _div(before["purchases"], before["link_clicks"] or before["clicks"])
    else:
        factors = [("Purchases", "purchases", _int_fmt),
                   ("Average order value", "aov", lambda v: _money(v, cents=False))]
        head = ("Revenue", "revenue", lambda v: _money(v, cents=False))
    trows = []
    for label, key, fmt in factors + [head]:
        trows.append({"factor": label, "before": fmt(before.get(key) or 0),
                      "after": fmt(after.get(key) or 0),
                      "change": _delta_pct(after.get(key) or 0, before.get(key) or 0) or "level"})
    moved = max(factors, key=lambda f: abs(_num(_delta_pct(after.get(f[1]) or 0,
                                                           before.get(f[1]) or 0).rstrip("%") or 0)))
    fact = _table("decomposition", "%s moved on %s, not on %s"
                  % (head[0], moved[0].lower(),
                     [f[0] for f in factors if f[1] != moved[1]][0].lower()),
                  "The last %d days against the %d before" % (len(after_days), len(before_days)),
                  [{"key": "factor", "label": "Factor"},
                   {"key": "before", "label": "Before", "align": "right"},
                   {"key": "after", "label": "After", "align": "right"},
                   {"key": "change", "label": "Change", "align": "right"}], trows)
    if fact:
        fact["summary"] = ("Decomposition of %s: %s"
                           % (head[0].lower(),
                              "; ".join("%s %s -> %s (%s)" % (r["factor"], r["before"], r["after"],
                                                              r["change"]) for r in trows)))
    return fact


def _vs_target_fact(total, engagement, objective):
    """The headline metrics against the targets on the engagement block.

    None when no target is on file -- the scorecard then reports the numbers and says plainly that
    no target is set, rather than implying a judgement the client never agreed to."""
    conv = ((engagement or {}).get("conversion") or {}).get(objective) or {}
    items = []

    def tile(key, label, actual, target, fmt, low_good=False):
        if not target:
            return
        try:
            tgt = float(str(target).replace("$", "").replace(",", "").replace("x", ""))
        except ValueError:
            return
        if not tgt:
            return
        hit = (actual <= tgt) if low_good else (actual >= tgt)
        items.append({"key": key, "label": label, "value": fmt(actual),
                      "note": "target %s, %s" % (fmt(tgt), "on track" if hit else "behind"),
                      "tier": "primary", "tone": "good" if hit else "bad"})

    if objective == "leadgen":
        cpl = _div(total["spend"], total["purchases"])
        tile("cpl", "Cost per lead", cpl, conv.get("target_cpl"), _money, low_good=True)
        tile("volume", "Leads", total["purchases"], conv.get("target_volume"), _int_fmt)
    else:
        tile("roas", "Return on ad spend", total["roas"], conv.get("target_roas"),
             lambda v: "%.2fx" % v)
        tile("aov", "Average order value", total["aov"], conv.get("target_aov"),
             lambda v: _money(v, cents=False))
    return _tiles("vs_target", "Against the targets we agreed", items,
                  subtitle="Whole flight to date")


def _facts_windsor(data, objective="sales", engagement=None):
    """Facts for the Windsor-live per-ad/day export (the riverdance shape)."""
    rows = data.get("rows") or []
    dates = sorted({(r.get("date") or "")[:10] for r in rows if r.get("date")})
    facts = []
    total = _derive(_roll(rows))
    has_rev = total["revenue"] > 0

    # Two tiers on purpose (the hand-built decks do the same): the money story as big tiles, the
    # delivery metrics as a compact strip underneath. Eleven equal tiles is a wall, not a headline.
    tiles = [{"key": "spend", "label": "Ad spend", "value": _money(total["spend"], cents=False),
              "note": "Total media investment", "tier": "primary"}]
    if has_rev:
        tiles = [{"key": "revenue", "label": "Revenue", "value": _money(total["revenue"], cents=False),
                  "note": "Tracked purchase value from ads", "tier": "primary"}] + tiles
        tiles += [{"key": "roas", "label": "Return on ad spend", "value": "%.2fx" % total["roas"],
                   "note": "%s back per $1 invested" % _money(total["roas"]), "tier": "primary"},
                  {"key": "purchases", "label": "Purchases", "value": _int_fmt(total["purchases"]),
                   "note": "%s each" % _money(total["cpa"], cents=False), "tier": "primary"},
                  {"key": "aov", "label": "Average order", "value": _money(total["aov"], cents=False),
                   "note": "Tracked purchase value", "tier": "primary"}]
    tiles += [{"key": "impressions", "label": "Impressions", "value": _int_fmt(total["impressions"]),
               "note": "", "tier": "secondary"},
              {"key": "reach", "label": "People reached", "value": _int_fmt(total["reach"]),
               "note": "", "tier": "secondary"},
              {"key": "clicks", "label": "Link clicks",
               "value": _int_fmt(total["link_clicks"] or total["clicks"]), "note": "",
               "tier": "secondary"},
              {"key": "ctr", "label": "CTR", "value": "%.2f%%" % (100.0 * total["ctr"]),
               "note": "", "tier": "secondary"},
              {"key": "cpc", "label": "Cost / click", "value": _money(total["cpc"]),
               "note": "", "tier": "secondary"},
              {"key": "cpm", "label": "CPM", "value": _money(total["cpm"]),
               "note": "", "tier": "secondary"}]
    facts.append(_tiles("totals", "The campaign to date", tiles,
                        subtitle=period_label(dates[0], dates[-1]) if dates else ""))

    weeks = _weeks(rows)
    if len(weeks) >= 2:
        wk_lab = [short_date(wk) for wk, _a, _n in weeks]
        d = [_derive(a) for _wk, a, _n in weeks]
        facts.append(_series("weekly_spend", "Ad spend, by week", "Weeks beginning Monday",
                             [{"label": lab, "value": x["spend"], "text": _money(x["spend"], cents=False)}
                              for lab, x in zip(wk_lab, d)], best=""))
        if has_rev:
            facts.append(_series("weekly_revenue", "Revenue, by week", "Weeks beginning Monday",
                                 [{"label": lab, "value": x["revenue"],
                                   "text": _money(x["revenue"], cents=False)}
                                  for lab, x in zip(wk_lab, d)]))
            facts.append(_series("weekly_roas", "Return on ad spend, by week",
                                 "Weeks beginning Monday",
                                 [{"label": lab, "value": x["roas"], "text": "%.2fx" % x["roas"]}
                                  for lab, x in zip(wk_lab, d)]))
            facts.append(_series("weekly_aov", "Average order value, by week",
                                 "Weeks beginning Monday",
                                 [{"label": lab, "value": x["aov"],
                                   "text": _money(x["aov"], cents=False)}
                                  for lab, x in zip(wk_lab, d) if x["aov"]]))
        facts.append(_series("weekly_ctr", "Click-through rate, by week", "Weeks beginning Monday",
                             [{"label": lab, "value": x["ctr"], "text": "%.2f%%" % (100.0 * x["ctr"])}
                              for lab, x in zip(wk_lab, d)]))

    facts.append(_window_compare(rows, dates))

    ads = _roll(rows, "ad")
    if len(ads) > 1:
        ranked = sorted(ads.items(), key=lambda kv: -kv[1]["spend"])[:8]
        cols = [{"key": "ad", "label": "Ad"},
                {"key": "spend", "label": "Spend", "align": "right"},
                {"key": "ctr", "label": "CTR", "align": "right"},
                {"key": "cpc", "label": "Cost / click", "align": "right"}]
        if has_rev:
            cols += [{"key": "rev", "label": "Revenue", "align": "right"},
                     {"key": "roas", "label": "ROAS", "align": "right"}]
        trows = []
        for name, a in ranked:
            x = _derive(a)
            trows.append({"ad": name, "spend": _money(x["spend"], cents=False),
                          "ctr": "%.2f%%" % (100.0 * x["ctr"]), "cpc": _money(x["cpc"]),
                          "rev": _money(x["revenue"], cents=False), "roas": "%.2fx" % x["roas"],
                          "_cpc": x["cpc"], "_roas": x["roas"],
                          "_share": _div(x["spend"], total["spend"]),
                          "_clicks": x["link_clicks"] or x["clicks"]})
        _tone_rows(trows, "roas" if has_rev else "cpc",
                   "high_good" if has_rev else "low_good")
        facts.append(_table("ads", "Every ad, ranked by spend", "Full flight", cols, trows))

    camps = _roll(rows, "camp")
    if len(camps) > 1:
        ranked = sorted(camps.items(), key=lambda kv: -kv[1]["spend"])[:8]
        trows = []
        for name, a in ranked:
            x = _derive(a)
            trows.append({"camp": name, "spend": _money(x["spend"], cents=False),
                          "ctr": "%.2f%%" % (100.0 * x["ctr"]), "cpc": _money(x["cpc"]),
                          "roas": "%.2fx" % x["roas"], "_roas": x["roas"],
                          "_share": _div(x["spend"], total["spend"]),
                          "_clicks": x["link_clicks"] or x["clicks"]})
        cols = [{"key": "camp", "label": "Campaign"},
                {"key": "spend", "label": "Spend", "align": "right"},
                {"key": "ctr", "label": "CTR", "align": "right"},
                {"key": "cpc", "label": "Cost / click", "align": "right"}]
        if has_rev:
            cols.append({"key": "roas", "label": "ROAS", "align": "right"})
            _tone_rows(trows, "roas", "high_good")
        facts.append(_table("campaigns", "Every campaign, ranked by spend", "Full flight",
                            cols, trows))

    facts.append(_vs_target_fact(total, engagement, objective))
    fun = _funnel_fact(total, objective, ((engagement or {}).get("conversion") or {})
                       .get(objective, {}).get("label", ""))
    facts.append(fun)
    facts.append(_funnel_notes(fun))
    facts.append(_decomposition_fact(rows, dates, objective))
    facts.append(_pressure_tiles(weeks))
    if ads:
        facts.append(_bench_tiles(rows, ads))

    demo = data.get("demographics") or {}
    facts.append(_breakdown_table("age", demo.get("age_gender"), "age",
                                  "Every age band, ranked by cost per click", "Age band"))
    facts.append(_segments_table(demo.get("age_gender")))
    facts.append(_reallocation(demo.get("age_gender")))
    facts.append(_breakdown_table("gender", demo.get("age_gender"), "gender",
                                  "Delivery by gender", "Gender"))
    facts.append(_breakdown_table("region", demo.get("region"), "region",
                                  "Where the budget goes", "Region"))
    if demo.get("region"):
        share = _roll(demo["region"], "region")
        spend_all = sum(a["spend"] for a in share.values())
        pts = [{"label": n, "value": a["spend"], "text": _pct(a["spend"], spend_all, 1)}
               for n, a in sorted(share.items(), key=lambda kv: -kv[1]["spend"])[:6]]
        facts.append(_series("region_share", "Share of spend by region", "Full flight",
                             pts, orient="rows", best=""))

    ac = data.get("activecampaign") or {}
    camps_ac = ac.get("campaigns") or []
    if ac.get("enabled") and camps_ac:
        trows = []
        for c in camps_ac[:8]:
            sent = _num(c.get("sent") or c.get("send_amt"))
            opens = _num(c.get("opens") or c.get("uniqueopens"))
            clicks = _num(c.get("clicks") or c.get("uniquelinkclicks"))
            trows.append({"name": c.get("name") or "(untitled)",
                          "date": short_date((c.get("date") or c.get("sdate") or "")[:10]),
                          "sent": _int_fmt(sent), "open": _pct(opens, sent, 1),
                          "click": _pct(clicks, sent, 1), "_click": _div(clicks, sent)})
        _tone_rows(trows, "click", "high_good")
        facts.append(_table("email", "Email sends, most recent first", "ActiveCampaign",
                            [{"key": "name", "label": "Campaign"},
                             {"key": "date", "label": "Sent", "align": "right"},
                             {"key": "sent", "label": "Recipients", "align": "right"},
                             {"key": "open", "label": "Open rate", "align": "right"},
                             {"key": "click", "label": "Click rate", "align": "right"}], trows))
    return facts


def _segments_table(raw, cap=8):
    """Age x gender cells ranked by cost per click -- the "best cell in the account" fact. A band
    and a gender each look average until you cross them; this is where the real spread lives."""
    if not raw:
        return None
    rows_in = [dict(r, seg="%s, %s" % (r.get("age") or "?", r.get("gender") or "?")) for r in raw]
    agg = _roll(rows_in, "seg")
    spend_all = sum(a["spend"] for a in agg.values())
    if len(agg) < 3 or not spend_all:
        return None
    rows = []
    for name, a in agg.items():
        clicks = a["lclk"] or a["clicks"]
        if not clicks:
            continue
        x = _derive(a)
        row = {"seg": name, "share": _pct(a["spend"], spend_all, 1),
               "ctr": "%.2f%%" % (100.0 * x["ctr"]), "cpc": _money(x["cpc"]),
               "_cpc": x["cpc"], "_share": _div(a["spend"], spend_all), "_clicks": clicks}
        if _has_volume(row):
            rows.append(row)
    if len(rows) < 3:
        return None
    rows.sort(key=lambda r: r["_cpc"])
    rows = rows[:cap]
    _tone_rows(rows, "cpc", "low_good")
    return _table("segments", "Every audience cell, ranked by cost per click",
                  "Age crossed with gender, delivery only. Cells under 1% of spend are left out, "
                  "they carry too little delivery to rank",
                  [{"key": "seg", "label": "Audience cell"},
                   {"key": "share", "label": "Share of spend", "align": "right"},
                   {"key": "ctr", "label": "CTR", "align": "right"},
                   {"key": "cpc", "label": "Cost / click", "align": "right"}], rows)


def _reallocation(raw):
    """What the expensive half of the age curve would buy at the cheap half's rate.

    This is the number a media buyer acts on -- "the same budget, +N clicks" -- and it is pure
    arithmetic over the breakdown, so the deck can state it without anyone estimating anything."""
    if not raw:
        return None
    agg = _roll(raw, "age")
    spend_all = sum(a["spend"] for a in agg.values())
    bands = []
    for band, a in agg.items():
        clicks = a["lclk"] or a["clicks"]
        # Same volume floor as the tables: a band that never really ran cannot set the rate we
        # promise to reallocate towards.
        if clicks and a["spend"] and _has_volume({"_share": _div(a["spend"], spend_all),
                                                  "_clicks": clicks}):
            bands.append((a["spend"] / clicks, band, a["spend"], clicks))
    if len(bands) < 4:
        return None
    bands.sort()
    half = len(bands) // 2
    strong, weak = bands[:half], bands[half:]
    strong_cpc = _div(sum(b[2] for b in strong), sum(b[3] for b in strong))
    weak_spend = sum(b[2] for b in weak)
    weak_clicks = sum(b[3] for b in weak)
    if not strong_cpc:
        return None
    would_buy = weak_spend / strong_cpc
    gain = would_buy - weak_clicks
    if gain < 1:
        return None
    weak_names = ", ".join(b[1] for b in weak)
    strong_names = ", ".join(b[1] for b in strong)
    fact = _series(
        "reallocation", "The same %s, reallocated" % _money(weak_spend, cents=False),
        "%s cost %s a click; %s deliver at %s"
        % (weak_names, _money(_div(weak_spend, weak_clicks)), strong_names,
           _money(strong_cpc)),
        # Short labels on purpose: the bands they refer to are named in the subtitle, and these
        # two rows sit in half a slide when the deck puts the table beside them.
        [{"label": "Today", "value": weak_clicks,
          "text": "%s clicks" % _int_fmt(weak_clicks)},
         {"label": "Reallocated", "value": would_buy,
          "text": "%s clicks" % _int_fmt(would_buy)}],
        orient="rows", best="")
    if fact:
        fact["summary"] = ("Reallocation: %s currently goes to %s at %s a click. At the %s rate of "
                           "%s that same money buys %s clicks instead of %s, a gain of %s (%s) for "
                           "no extra budget."
                           % (_money(weak_spend, cents=False), weak_names,
                              _money(_div(weak_spend, weak_clicks)), strong_names,
                              _money(strong_cpc), _int_fmt(would_buy), _int_fmt(weak_clicks),
                              _int_fmt(gain), _delta_pct(would_buy, weak_clicks)))
    return fact


def _pressure_tiles(weeks):
    """Is the buy getting more expensive? The last COMPLETE week against the flight average.

    Complete on both sides on purpose -- see _weeks: a partial trailing week compared against full
    weeks invents a cliff that is really just a short bucket."""
    full = [(wk, a) for wk, a, n in weeks if n >= 7]
    if len(weeks) < 3 or len(full) < 2:
        return None
    last = _derive(full[-1][1])
    totals = {"spend": 0.0, "imps": 0, "clicks": 0, "lclk": 0, "reach": 0, "pur": 0.0,
              "rev": 0.0, "init": 0.0}
    for _wk, a in full:
        for k in totals:
            totals[k] += a[k]
    avg = _derive(totals)
    items = []
    for key, label, fmt, better_low in (("cpm", "CPM, last week", _money, True),
                                        ("cpc", "Cost / click, last week", _money, True)):
        now, base = last[key], avg[key]
        if not base:
            continue
        delta = _delta_pct(now, base)
        rising = now > base
        items.append({"key": key, "label": label, "value": fmt(now),
                      "note": "%s vs the flight average of %s" % (delta or "level", fmt(base)),
                      "tier": "primary", "tone": ("bad" if rising else "good") if delta else ""})
    ctr_delta = _delta_pct(last["ctr"], avg["ctr"])
    if ctr_delta:
        items.append({"key": "ctr", "label": "CTR, last week",
                      "value": "%.2f%%" % (100.0 * last["ctr"]),
                      "note": "%s vs the flight average of %.2f%%" % (ctr_delta, 100.0 * avg["ctr"]),
                      "tier": "primary",
                      "tone": "good" if last["ctr"] >= avg["ctr"] else "bad"})
    return _tiles("pressure", "Is the buy getting more expensive?", items,
                  subtitle="The last complete week (%s) against the average of every complete week"
                           % short_date(full[-1][0]))


def _bench_tiles(rows, ads):
    """How deep the creative bench is: how many ads carry the account, and how concentrated."""
    if len(ads) < 1:
        return None
    ranked = sorted(ads.items(), key=lambda kv: -kv[1]["spend"])
    spend_all = sum(a["spend"] for a in ads.values())
    first_seen = {}
    for r in rows:
        name, d = r.get("ad") or "(unlabeled)", (r.get("date") or "")[:10]
        if d and (name not in first_seen or d < first_seen[name]):
            first_seen[name] = d
    newest = max(first_seen.values()) if first_seen else ""
    items = [{"key": "count", "label": "Ads carrying the account", "value": str(len(ads)),
              "note": "live in this flight", "tier": "primary"},
             {"key": "conc", "label": "Spend on the top ad",
              "value": _pct(ranked[0][1]["spend"], spend_all, 0),
              "note": ranked[0][0][:60], "tier": "primary"}]
    if newest:
        items.append({"key": "newest", "label": "Newest creative launched",
                      "value": short_date(newest), "note": "nothing newer has entered the account",
                      "tier": "primary"})
    return _tiles("bench", "How deep the creative bench is", items)


def _breakdown_table(key, raw, group, title, label):
    """A demographic/region breakdown as a table: share of spend, CTR and cost per click.

    Meta rejects revenue on a breakdown query, so these are DELIVERY metrics only -- the subtitle
    says so on the slide, because a client reading a table with no ROAS column deserves the reason.
    """
    if not raw:
        return None
    agg = _roll(raw, group)
    spend_all = sum(a["spend"] for a in agg.values())
    if not spend_all:
        return None
    rows = []
    for name, a in sorted(agg.items(), key=lambda kv: _div(kv[1]["spend"], kv[1]["lclk"] or
                                                           kv[1]["clicks"] or 1)):
        x = _derive(a)
        rows.append({group: name, "share": _pct(a["spend"], spend_all, 1),
                     "ctr": "%.2f%%" % (100.0 * x["ctr"]), "cpc": _money(x["cpc"]),
                     "_cpc": x["cpc"], "_share": _div(a["spend"], spend_all),
                     "_clicks": a["lclk"] or a["clicks"]})
    _tone_rows(rows, "cpc", "low_good")
    return _table(key, title, "Delivery only, no revenue (Meta does not report it on a breakdown)",
                  [{"key": group, "label": label},
                   {"key": "share", "label": "Share of spend", "align": "right"},
                   {"key": "ctr", "label": "CTR", "align": "right"},
                   {"key": "cpc", "label": "Cost / click", "align": "right"}], rows)


def _col_label(col):
    return str(col).replace("_", " ").strip().capitalize()


def _fmt_col(col, value):
    """Format a template-shape column by what its NAME says it is (money, rate, or a count)."""
    name = str(col).lower()
    if any(w in name for w in ("spend", "cost", "revenue", "value", "cpa", "cpc", "cpm", "aov")):
        return _money(value, cents=abs(value) < 100)
    if any(w in name for w in ("rate", "ctr", "pct", "percent", "share")):
        return "%.2f%%" % (value * 100 if abs(value) <= 1 else value)
    if float(value).is_integer():
        return _int_fmt(value)
    return format(round(value, 2), ",g")


def _daily_weeks(daily):
    """({monday_iso: {col: sum}}, ordered dates, {monday_iso: days_in_bucket}) over `daily` rows."""
    weeks, dates, days = {}, [], {}
    for row in daily:
        try:
            d = datetime.date.fromisoformat(str(row.get("date") or "")[:10])
        except ValueError:
            continue
        dates.append(d.isoformat())
        wk = (d - datetime.timedelta(days=d.weekday())).isoformat()
        agg = weeks.setdefault(wk, {})
        days.setdefault(wk, set()).add(d.isoformat())
        for k, v in row.items():
            if k != "date" and isinstance(v, (int, float)):
                agg[k] = agg.get(k, 0) + v
    return weeks, sorted(set(dates)), {w: len(v) for w, v in days.items()}


def _daily_compare(daily, dates, columns, span=14):
    """The generic like-for-like window for the template shape: the last `span` days against the
    `span` before, across whatever numeric columns the client's export happens to carry."""
    if len(dates) < span + 2 or not columns:
        return None
    after_days, before_days = set(dates[-span:]), set(dates[-2 * span:-span])
    if not before_days:
        return None
    return _daily_compare_days(daily, before_days, after_days, columns)


def _daily_compare_days(daily, before_days, after_days, columns):
    """The template-shape compare fact over two EXPLICIT day sets."""
    if not columns:
        return None
    sums = {"before": {}, "after": {}}
    for row in daily:
        day = str(row.get("date") or "")[:10]
        side = "after" if day in after_days else ("before" if day in before_days else None)
        if not side:
            continue
        for k, v in row.items():
            if k in columns and isinstance(v, (int, float)):
                sums[side][k] = sums[side].get(k, 0) + v
    if not sums["before"] or not sums["after"]:
        return None

    def side(which, days):
        ordered = sorted(days)
        return {"title": period_label(ordered[0], ordered[-1]), "subtitle": "%d days" % len(days),
                "rows": [{"label": _col_label(c), "value": _fmt_col(c, _num(sums[which].get(c)))}
                         for c in columns]}

    lead = columns[0]
    headline = _delta_pct(_num(sums["after"].get(lead)), _num(sums["before"].get(lead)))
    return {"key": "recent_vs_prior", "kind": "compare",
            "title": "The last %d days against the %d before" % (len(after_days), len(before_days)),
            "subtitle": "Like for like, the same length of window either side",
            "before": side("before", before_days), "after": side("after", after_days),
            "delta": {"headline": headline or "flat", "label": _col_label(lead).lower(),
                      "tone": "good" if headline.startswith("+") else
                              ("bad" if headline.startswith("-") else "neutral")},
            "summary": "Last %d days vs the %d before: %s"
                       % (len(after_days), len(before_days),
                          "; ".join("%s %s vs %s"
                                    % (_col_label(c), _fmt_col(c, _num(sums["after"].get(c))),
                                       _fmt_col(c, _num(sums["before"].get(c)))) for c in columns))}


def _momentum_table(weeks, ordered, columns, counts=None):
    """Every metric's last COMPLETE week against its own weekly average -- the template shape's
    answer to "which way is this going?", in one table instead of five charts.

    Partial weeks are excluded from both sides (see _weeks): a flight ending on a Monday would
    otherwise report every metric down 80%, which is a bucket artefact, not a result."""
    full = [w for w in ordered if (counts or {}).get(w, 7) >= 7]
    if len(ordered) < 3 or len(full) < 2 or not columns:
        return None
    last = weeks[full[-1]]
    rows = []
    for col in columns:
        avg = _div(sum(_num(weeks[w].get(col)) for w in full), len(full))
        now = _num(last.get(col))
        rows.append({"metric": _col_label(col), "last": _fmt_col(col, now),
                     "avg": _fmt_col(col, avg), "change": _delta_pct(now, avg) or "level"})
    return _table("momentum", "Every metric, last complete week against its own average",
                  "Week beginning %s, against the average of every complete week"
                  % short_date(full[-1]),
                  [{"key": "metric", "label": "Metric"},
                   {"key": "last", "label": "Last week", "align": "right"},
                   {"key": "avg", "label": "Weekly average", "align": "right"},
                   {"key": "change", "label": "Change", "align": "right"}], rows)


def _facts_template(data):
    """Facts for the standard client shape: {kpis: {...}, daily: [{date, ...}]}.

    This is the shape EVERY client except the Windsor-live ones uses, so it gets the same treatment:
    headline tiles, a weekly series per metric, a like-for-like window and a momentum table. Without
    the last two a normal client's deck was four slides long."""
    facts = []
    kpis = data.get("kpis")
    if isinstance(kpis, dict) and kpis:
        items = [{"key": str(k), "label": _col_label(k),
                  "value": _fmt_col(k, v) if isinstance(v, (int, float)) else str(v),
                  "note": "", "tier": "primary" if i < 5 else "secondary"}
                 for i, (k, v) in enumerate(list(kpis.items())[:12])]
        facts.append(_tiles("totals", "Headline numbers", items))
    daily = [r for r in (data.get("daily") or []) if isinstance(r, dict)]
    if not daily:
        return facts
    weeks, dates, counts = _daily_weeks(daily)
    ordered = sorted(weeks)
    # Biggest-magnitude columns first, so the lead metric of the compare is the one that matters.
    columns = sorted({k for agg in weeks.values() for k in agg},
                     key=lambda k: -abs(sum(_num(weeks[w].get(k)) for w in ordered)))
    for col in columns[:6]:
        pts = [{"label": short_date(w), "value": _num(weeks[w].get(col)),
                "text": _fmt_col(col, _num(weeks[w].get(col)))} for w in ordered]
        facts.append(_series("weekly_%s" % col, "%s, by week" % _col_label(col),
                             "Weeks beginning Monday", pts))
    facts.append(_daily_compare(daily, dates, columns[:6]))
    facts.append(_momentum_table(weeks, ordered, columns[:8], counts))
    return facts


def infer_objective(dash_data, engagement=None):
    """Which objective this client's deck is written for.

    The declared engagement wins. With nothing declared, infer from the data so a deck is correct
    before anyone fills the form in: revenue present -> sales, otherwise lead generation."""
    declared = (engagement or {}).get("primary") or ""
    if declared in report_spec.OBJECTIVES:
        return declared
    objs = [o for o in ((engagement or {}).get("objectives") or [])
            if o in report_spec.OBJECTIVES]
    if objs:
        return objs[0]
    data = dash_data if isinstance(dash_data, dict) else {}
    if data.get("rows"):
        if _derive(_roll(data["rows"]))["revenue"] > 0:
            return "sales"
        return "leadgen"
    kpis = data.get("kpis") if isinstance(data.get("kpis"), dict) else {}
    keys = " ".join(str(k).lower() for k in kpis)
    return "sales" if ("revenue" in keys or "roas" in keys) else "leadgen"


def build_facts(dash_data, engagement=None, objective="", window=None):
    """The deck's computed datasets, keyed: {key: fact}. Handles BOTH dashboard shapes (the
    Windsor-live per-ad/day export and the template `kpis`/`daily` contract); unknown/empty -> {}.

    `engagement` supplies the objective and the targets (workspace.engagement_of). `window` is an
    optional (first_iso, last_iso) pair (see `report_window`): dated rows outside it are dropped
    before any fact is computed, so every total, table and funnel answers for THE WINDOW -- and
    `recent_vs_prior` is recomputed as the window against the equal days before it (from the full
    export), because inside a 7-day window the default 14-vs-14 compare cannot exist."""
    data = dash_data if isinstance(dash_data, dict) else {}
    objective = objective or infer_objective(data, engagement)
    wdata = _window_data(data, window)
    facts = (_facts_windsor(wdata, objective, engagement) if data.get("rows")
             else _facts_template(wdata))
    out = {f["key"]: f for f in facts if f}
    if window:
        vs_prior = _window_vs_prior(data, window)
        if vs_prior:
            out["recent_vs_prior"] = vs_prior
        elif "recent_vs_prior" in out:
            # The windowed rows can't support their own default compare AND no prior period
            # exists -- an unwindowed leftover would silently answer a different question.
            del out["recent_vs_prior"]
        if data.get("rows"):
            # The Windsor totals were computed over the windowed rows -- only the wording lies.
            if "totals" in out:
                out["totals"]["title"] = "The window in numbers"
                out["totals"]["summary"] = out["totals"]["summary"].replace(
                    "The campaign to date", "The window in numbers", 1)
        else:
            # 🔴 The template shape's `kpis` block is a WHOLE-FLIGHT aggregate baked into the
            # export -- inside a window those tiles would lie. Recompute the totals from the
            # windowed daily rows instead (or drop them when there are none).
            out.pop("totals", None)
            sums = {}
            for row in (wdata.get("daily") or []):
                if not isinstance(row, dict):
                    continue
                for k, v in row.items():
                    if k != "date" and isinstance(v, (int, float)):
                        sums[k] = sums.get(k, 0) + v
            if sums:
                cols = sorted(sums, key=lambda k: -abs(sums[k]))[:8]
                items = [{"key": c, "label": _col_label(c), "value": _fmt_col(c, _num(sums[c])),
                          "note": "", "tier": "primary" if i < 5 else "secondary"}
                         for i, c in enumerate(cols)]
                tot = _tiles("totals", "The window in numbers", items)
                if tot:
                    out["totals"] = tot
    return out


# The Reports tab's reporting-window choices (the form's `period` field).
WINDOW_MODES = {"mtd": "Month to date", "last_week": "Last full week"}


def report_window(dash_data, mode, when=""):
    """The (first_iso, last_iso) calendar window `mode` resolves to, or None for the whole flight.

    'mtd' = the 1st of the month through the anchor day; 'last_week' = the most recent COMPLETE
    Monday-Sunday week. Anchored on the LAST date in the export (exports lag, so anchoring on
    today could window onto days with no data yet), falling back to `when`, then today."""
    if mode not in WINDOW_MODES:
        return None
    _first, last = _period_of(dash_data)
    anchor = last or (when or "")[:10] or datetime.date.today().isoformat()
    try:
        d = datetime.date.fromisoformat(anchor[:10])
    except ValueError:
        return None
    if mode == "mtd":
        return (d.replace(day=1).isoformat(), d.isoformat())
    end = d if d.weekday() == 6 else d - datetime.timedelta(days=d.weekday() + 1)
    return ((end - datetime.timedelta(days=6)).isoformat(), end.isoformat())


def _window_data(data, window):
    """A copy of the export filtered to `window`. Dated rows outside it are dropped from
    rows/daily/demographics/activecampaign; an UNDATED row is kept (a breakdown that carries no
    date column must not vanish from a windowed report)."""
    if not window:
        return data
    lo, hi = window

    def keep(r):
        d = (r.get("date") or "")[:10] if isinstance(r, dict) else ""
        return (not d) or (lo <= d <= hi)

    out = dict(data)
    for k in ("rows", "daily"):
        if isinstance(data.get(k), list):
            out[k] = [r for r in data[k] if keep(r)]
    demo = data.get("demographics")
    if isinstance(demo, dict):
        out["demographics"] = {k: ([r for r in v if keep(r)] if isinstance(v, list) else v)
                               for k, v in demo.items()}
    ac = data.get("activecampaign")
    if isinstance(ac, dict) and isinstance(ac.get("campaigns"), list):
        out["activecampaign"] = dict(ac, campaigns=[c for c in ac["campaigns"] if keep(c)])
    return out


def _window_vs_prior(data, window):
    """`recent_vs_prior` recomputed as THIS WINDOW against the equal days before it, from the FULL
    export. None when either side has under 2 data days (no fake compare, ever)."""
    lo, hi = window
    rows = data.get("rows") or []
    if rows:
        dates = sorted({(r.get("date") or "")[:10] for r in rows if r.get("date")})
        after = [d for d in dates if lo <= d <= hi]
        if len(after) < 2:
            return None
        before = [d for d in dates if d < lo][-len(after):]
        if len(before) < 2:
            return None
        return _compare_days(rows, set(before), set(after))
    daily = [r for r in (data.get("daily") or []) if isinstance(r, dict)]
    if daily:
        dates = sorted({str(r.get("date") or "")[:10] for r in daily if r.get("date")})
        after = [d for d in dates if lo <= d <= hi]
        if len(after) < 2:
            return None
        before = [d for d in dates if d < lo][-len(after):]
        if len(before) < 2:
            return None
        sums = {}
        for row in daily:
            for k, v in row.items():
                if k != "date" and isinstance(v, (int, float)):
                    sums[k] = sums.get(k, 0) + abs(v)
        columns = sorted(sums, key=lambda k: -sums[k])[:6]
        return _daily_compare_days(daily, set(before), set(after), columns)
    return None


def facts_catalogue(facts, cap=700):
    """The fact pack as text for the model: what each key holds, and its numbers to quote."""
    lines = []
    for key, f in (facts or {}).items():
        summary = (f.get("summary") or "").replace("\n", " ")
        if len(summary) > cap:
            summary = summary[:cap] + "..."
        lines.append("%s [%s] %s" % (key, f.get("kind") or "?", summary))
    return lines


# --- 2. Gather: the source pack -------------------------------------------------------------------
def _intel_lines(entries, cap=6):
    out = []
    for e in sorted(entries or [], key=lambda x: x.get("date") or "", reverse=True)[:cap]:
        body = (e.get("body") or "").strip().replace("\n", " ")
        if len(body) > 300:
            body = body[:300] + "..."
        out.append("%s (%s, %s): %s" % (e.get("title") or "(untitled)",
                                        e.get("source") or "no source",
                                        (e.get("date") or "")[:10], body))
    return out


def _voices_lines(archives, cap=8):
    """The freshest watched items, summaries preferred (titles alone when unsummarized)."""
    items = []
    for ch, videos in archives or []:
        who = "%s (%s%s)" % (ch.get("title") or "channel", ch.get("kind") or "creator",
                             (", " + ch["industry"]) if ch.get("industry") else "")
        for v in videos or []:
            items.append((v.get("published") or "", who, v.get("title") or "",
                          (v.get("summary") or "").strip()))
    items.sort(key=lambda it: it[0], reverse=True)
    out = []
    for published, who, title, summary in items:
        if len(out) >= cap:
            break
        if summary:
            if len(summary) > 400:
                summary = summary[:400] + "..."
            out.append("%s -- %s (%s): %s" % (who, title, published[:10] or "undated", summary))
        else:
            out.append("%s -- %s (%s): no summary yet." % (who, title, published[:10] or "undated"))
    return out


def _competitor_lines(archives, cap=6):
    """What WATCHED COMPETITORS are publishing (Landscape block four).

    Watcher already tags every channel creator|competitor, and per-video summaries are cached on the
    explicit reindex -- so this is a filter over data we hold, not new fetching. No competitor
    channels, no block: the slide simply has three blocks instead of four."""
    out = []
    for ch, videos in archives or []:
        if (ch.get("kind") or "") != "competitor":
            continue
        who = ch.get("title") or "competitor"
        for v in sorted(videos or [], key=lambda x: x.get("published") or "", reverse=True)[:2]:
            summary = (v.get("summary") or "").strip().replace("\n", " ")
            if len(summary) > 300:
                summary = summary[:300] + "..."
            out.append("%s -- %s (%s)%s" % (who, v.get("title") or "(untitled)",
                                            (v.get("published") or "")[:10] or "undated",
                                            (": " + summary) if summary else ""))
            if len(out) >= cap:
                return out
    return out


def _asks(ws):
    """(needed_from_client, blocked) -- the Delivery slide's client-safe asks.

    🔴 `hold_reason` is INTERNAL and must never appear here: this deck is client-visible, and a held
    client-facing task shows the client a plain "Paused" on the Tasks tab by design. The old version
    of this function appended the internal reason to every blocked line, which leaked it into the
    deck's closing slide. A client-safe blocker reason needs its own field; until then the client is
    told THAT it is paused, not why."""
    needed, blocked = [], []
    for t in ws.get("tasks") or []:
        title = t.get("title") or "(untitled)"
        stage = t.get("stage") or ""
        if stage == "blocked" or t.get("on_hold"):
            blocked.append("%s - paused" % title)      # never `reason`: it is internal
        open_changes = [c for c in t.get("comments") or []
                        if c.get("kind") == "changes" and not c.get("resolved")]
        if open_changes:
            needed.append("%s: change request open (%s)"
                          % (title, (open_changes[-1].get("body") or "")[:140]))
    for camp in ws.get("campaigns") or []:
        awaiting = [p for p in camp.get("content") or [] if p.get("status") == "awaiting"]
        if awaiting:
            needed.append("%s: %d content piece%s awaiting your approval"
                          % (camp.get("name") or "campaign", len(awaiting),
                             "" if len(awaiting) == 1 else "s"))
    return needed, blocked


def _period_of(dash_data):
    """The flight's first/last date out of either dashboard shape ('' when undated)."""
    data = dash_data if isinstance(dash_data, dict) else {}
    dates = sorted({(r.get("date") or "")[:10]
                    for r in (data.get("rows") or data.get("daily") or [])
                    if isinstance(r, dict) and r.get("date")})
    return (dates[0], dates[-1]) if dates else ("", "")


def _engagement_of(ws):
    """The engagement block, read defensively (no workspace import -- same posture as digest)."""
    eng = (ws or {}).get("engagement")
    if not isinstance(eng, dict):
        return {"objectives": [], "primary": "", "conversion": {}}
    return {"objectives": [o for o in (eng.get("objectives") or []) if isinstance(o, str)],
            "primary": eng.get("primary") or "",
            "conversion": eng.get("conversion") if isinstance(eng.get("conversion"), dict) else {}}


def gather(ws, archives, dash_data, tasks_view=None, window_mode=""):
    """The report's source material: the computed fact pack + text blocks from the distilled layer.

    Pure -- the caller loads archives/dash_data and passes `tasks_view`
    (`main._progress_tasks(ws)`, the CLIENT-SAFE task projection) plus `window_mode` (the Reports
    tab's period choice: 'mtd' / 'last_week' / '' for the whole flight -- see report_window).
    Every numeric fact answers for that window; the Tasks board is always current-state."""
    intel = ws.get("intel") or {}
    needed, blocked = _asks(ws)
    first, last = _period_of(dash_data)
    engagement = _engagement_of(ws)
    objective = infer_objective(dash_data, engagement)
    objectives = engagement["objectives"] or [objective]
    window = report_window(dash_data, window_mode)
    facts = build_facts(dash_data, engagement, objective, window=window)
    for fact in tasks_facts(tasks_view):
        if fact:
            facts[fact["key"]] = fact
    period = period_label(*(window or (first, last)))
    if window:
        period = "%s%s" % (WINDOW_MODES[window_mode], (", " + period) if period else "")
    return {
        # The numbers, computed. Every chart/table/before-after in the deck is drawn from these.
        "facts": facts,
        "objective": objective,
        "objectives": objectives,
        "engagement": engagement,
        "period": period,
        "window_mode": window_mode if window else "",
        # Who the client is (Company tab) -- so the deck speaks in their language about their
        # actual products, instead of generic agency prose. Same distilled layer the Assistant reads.
        "company": digest.company_brief(ws),
        "business": _intel_lines(intel.get("business_research")),
        "media": _intel_lines(intel.get("media_buying")),
        # The section that had no home: wildfire, heat wave, road closure, local event, regulation.
        "conditions": _intel_lines(intel.get("conditions")),
        "voices": _voices_lines(archives),
        "competitors": _competitor_lines(archives),
        "dashboard": digest.dashboard_sections(dash_data or {}),
        "tasks": digest.tasks_snapshot(ws),
        "comms": digest.comms_snapshot(ws),
        "needed": needed,
        "blocked": blocked,
    }


# --- 3. Generate: model -> slide payload (with a deterministic no-AI deck) ------------------------
_SHAPE = (
    '{"meta": {"headline": "...", "subhead": "...", "period": "...", "sources": "..."}, '
    '"slides": [ '
    '{"kind": "cover", "eyebrow": "Performance review", "title": "One sentence claim.", '
    '"subtitle": "...", "blocks": [{"type": "chips", "items": [{"label": "Window", '
    '"value": "..."}]}]}, '
    '{"kind": "content", "slot": "what_happened", "eyebrow": "What happened", '
    '"title": "A claim with the number in it", '
    '"subtitle": "...", "tone": "good", "source": "Meta Ads via Windsor.ai, 13 May - 27 Jul 2026", '
    '"blocks": [{"type": "text", "body": "..."}, {"type": "kpis", "fact": "totals"}, '
    '{"type": "chart", "fact": "weekly_roas", "caption": "..."}, '
    '{"type": "table", "fact": "age", "caption": "..."}, '
    '{"type": "compare", "fact": "recent_vs_prior", "caption": "..."}, '
    '{"type": "cards", "items": [{"eyebrow": "Decision one", "title": "...", "body": "..."}]}, '
    '{"type": "bullets", "items": ["..."], "ordered": false}, '
    '{"type": "panel", "title": "What we think is happening", "body": "..."}, '
    '{"type": "split", "left": [{"type": "funnel", "fact": "funnel"}], '
    '"right": [{"type": "bullets", "items": ["..."]}, '
    '{"type": "panel", "title": "The cheapest lever we have", "body": "..."}]}, '
    '{"type": "callout", "tone": "warn", "body": "..."}, '
    '{"type": "action", "body": "exclude 18-34 and skew delivery female, live this week"}]} ]}')

def _gen_system(objectives=("sales",)):
    """The system prompt for a deck, with the SPINE injected as a contract.

    The running order is not a suggestion the model may improve on -- it is the standard every
    client's deck follows. It is stated here as numbered slots AND enforced in code afterwards
    (`enforce_spine`), so a drifting model changes the wording of a slide, never the shape of
    the deck."""
    return _GEN_SYSTEM_HEAD + report_spec.brief(objectives) + _GEN_SYSTEM_TAIL


_GEN_SYSTEM_HEAD = (
    "You are the strategist presenting a marketing agency's performance review to the client. "
    "Write the deck. Return JSON ONLY, exactly this shape: " + _SHAPE + "\n"
    "HOW TO WRITE IT:\n"
    "1. Every slide title is a CLAIM, not a label. 'We doubled daily revenue in two weeks', not "
    "'Performance summary'. Put the number in the title when there is one. The cover title is the "
    "single most important thing you learned from the material. The `eyebrow` is the slot's fixed "
    "label, given below -- use it VERBATIM so every report this client receives looks the same.\n"
    "2. THE DECK IS EXACTLY EIGHT SLIDES: one cover, then ONE `content` slide per slot below, in "
    "this order. Every non-cover slide carries its slot key in `slot`. This is enforced in code "
    "after you answer -- an off-spine slide is dropped and a missing slot is backfilled with an "
    "unwritten data-only slide, so deviating never shortens the deck, it only makes it worse:\n")

_GEN_SYSTEM_TAIL = ("\n"
    "3. FILL THE SLIDE. This is a 16:9 presentation slide, not a bullet. Give each content slide "
    "4 to 6 blocks and roughly 120 to 180 words, and make it carry ONE argument end to end: the "
    "evidence, what it MEANS, and the consequence. A figure alone is half a slide -- pair every "
    "chart or table with a `panel` (a short titled reading of it) or a `text`, and when two pieces "
    "of evidence belong to the same argument, put them side by side with `split`. A slide holding "
    "one figure and one sentence is a wasted slide.\n"
    "4. THE NUMBERS ARE GIVEN TO YOU. The FACT PACK below lists computed datasets by key. To show "
    "one, emit a block with its `fact` key ({\"type\": \"chart\", \"fact\": \"weekly_roas\"}) and "
    "write the interpretation in a nearby `panel`/`text` block or the `caption`. NEVER retype a "
    "fact's numbers into a table or chart of your own, and never use a key that is not in the "
    "pack. The funnel fact draws as a graph via {\"type\": \"funnel\", \"fact\": \"funnel\"}.\n"
    "5. Any number in your PROSE must appear in the source material verbatim. Invent nothing. If "
    "the material does not support a claim, drop the claim.\n"
    "6. `source` on a content slide names where its numbers came from, in the client's words.\n"
    "7. Say what we will DO, not what could be considered. Imperative, specific, owned by us. "
    "What-we'll-do actions and Opportunities never overlap: the first is work on the current "
    "account, the second is NEW headroom.\n"
    "8. No jargon, no em dashes (use commas or hyphens), no filler.\n"
    "9. NEVER name a slide after a deficiency. 'Where we are overpaying' is not a slide -- the "
    "same finding belongs under Opportunities as the gain it unlocks ('+377 clicks from the same "
    "budget'), with the shortfall as evidence underneath.")

_REVISE_SYSTEM = (
    "You edit a marketing agency's client deck. You get the deck's current JSON payload and the "
    "team's edit instruction. Apply the instruction and return the FULL updated payload as JSON "
    "ONLY, keeping exactly this shape: " + _SHAPE + " Change only what the instruction asks for; "
    "keep every other slide and block verbatim, in the same order. The deck normally follows a "
    "fixed eight-slide spine, but an edit MAY add a slide when the instruction asks for one "
    "(give it \"slot\": \"\" and place it where the instruction says, or before the last slide); "
    "an edit scoped to one slide must leave every other slide untouched. Fact keys in "
    "chart/table/compare/funnel blocks refer to computed datasets you cannot see - keep them as "
    "they are unless the instruction is about them, and never invent a new key. No em dashes in "
    "any text.")


def _material_text(client_name, when, inputs):
    lines = ["Client: %s" % client_name, "Report date: %s" % when]
    if inputs.get("period"):
        lines.append("Reporting window: %s" % inputs["period"])
    lines.append("")

    def block(title, rows):
        if rows:
            lines.append("=== %s ===" % title)
            lines.extend(rows if isinstance(rows, list) else [rows])
            lines.append("")
    block("FACT PACK -- the computed datasets you may show with a `fact` key",
          facts_catalogue(inputs.get("facts")))
    block("WHO THE CLIENT IS (company profile, brand guide, products)", inputs.get("company"))
    for _sid, title, text in inputs.get("dashboard") or []:
        block("DASHBOARD: %s" % title.upper(), text)
    block("OPERATING CONDITIONS (weather, disruption, local events, regulation -- things that "
          "moved a number without anyone touching the account)", inputs.get("conditions"))
    block("MARKET INTELLIGENCE: BUSINESS RESEARCH", inputs.get("business"))
    block("MARKET INTELLIGENCE: MEDIA BUYING NEWS", inputs.get("media"))
    block("WHAT WATCHED COMPETITORS ARE PUBLISHING", inputs.get("competitors"))
    block("MARKET VOICES (watched creators)", inputs.get("voices"))
    block("DELIVERY BOARD", inputs.get("tasks"))
    block("RECENT COMMUNICATIONS", inputs.get("comms"))
    block("OPEN ASKS ON THE CLIENT", inputs.get("needed"))
    block("BLOCKED WORK", inputs.get("blocked"))
    return "\n".join(lines)


def _parse_json(raw):
    """A dict out of a model reply that SHOULD be JSON (fences/junk tolerated), or None."""
    raw = (raw or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-z]*\s*|\s*```$", "", raw, flags=re.I)
    try:
        parsed = json.loads(raw, strict=False)
        return parsed if isinstance(parsed, dict) else None
    except ValueError:
        pass
    start = raw.find("{")
    if start >= 0:
        try:
            parsed, _end = json.JSONDecoder(strict=False).raw_decode(raw, start)
            return parsed if isinstance(parsed, dict) else None
        except ValueError:
            pass
    return None


# --- Normalization: coerce anything into the canonical payload the renderer trusts ----------------
_SLIDE_KINDS = ("cover", "section", "content", "closing")
_TONES = ("good", "warn", "bad", "neutral")
# `bullets` is fact-backed too: the Tasks slide's board lines must reach the client VERBATIM out
# of the client-safe task projection, never paraphrased by a model. `funnel` is the drawn funnel.
_FACT_BLOCKS = {"chart": ("series",), "table": ("table",), "compare": ("compare",),
                "kpis": ("tiles",), "bullets": ("list",), "funnel": ("funnel",)}


def _s(v, cap=400):
    out = str(v if v is not None else "").strip()
    return out[:cap]


def _strs(v, cap=10, length=300):
    out = []
    for it in (v if isinstance(v, list) else [])[:cap]:
        if isinstance(it, str) and it.strip():
            out.append(_s(it, length))
        elif isinstance(it, dict):
            joined = " ".join(str(x) for x in it.values() if x)
            if joined.strip():
                out.append(_s(joined, length))
    return out


def _norm_block(b, facts):
    """One block, or None when it is unknown / references a fact that does not exist.

    Dropping a bad fact key is deliberate: an invented key must render NOTHING, never an empty
    chart frame that reads on the call as a missing number."""
    if not isinstance(b, dict):
        return None
    kind = _s(b.get("type"), 20).lower()
    if kind in _FACT_BLOCKS:
        key = _s(b.get("fact"), 60)
        fact = (facts or {}).get(key)
        if fact and fact.get("kind") in _FACT_BLOCKS[kind]:
            return {"type": kind, "fact": key, "caption": _s(b.get("caption"), 300)}
        if kind not in ("kpis", "bullets"):
            return None
        # `kpis` and `bullets` may ALSO carry hand-written content (a derived headline like "+168%",
        # or the model's own list), so they fall through to the item paths below.
    if kind == "kpis":
        items = []
        for it in (b.get("items") if isinstance(b.get("items"), list) else [])[:8]:
            if isinstance(it, dict) and (it.get("value") or it.get("label")):
                items.append({"label": _s(it.get("label"), 60), "value": _s(it.get("value"), 40),
                              "note": _s(it.get("note"), 120),
                              "tone": _s(it.get("tone"), 10) if it.get("tone") in _TONES else ""})
        return {"type": "kpis", "items": items} if items else None
    if kind == "text":
        body = _s(b.get("body") or b.get("text"), 700)
        return {"type": "text", "body": body} if body else None
    if kind == "bullets":
        items = _strs(b.get("items"), cap=8)
        return {"type": "bullets", "items": items,
                "ordered": bool(b.get("ordered"))} if items else None
    if kind == "chips":
        items = []
        for it in (b.get("items") if isinstance(b.get("items"), list) else [])[:6]:
            if isinstance(it, dict) and (it.get("value") or it.get("label")):
                items.append({"label": _s(it.get("label"), 40), "value": _s(it.get("value"), 120)})
        return {"type": "chips", "items": items} if items else None
    if kind == "cards":
        items = []
        for it in (b.get("items") if isinstance(b.get("items"), list) else [])[:4]:
            if not isinstance(it, dict):
                continue
            card = {"eyebrow": _s(it.get("eyebrow"), 40), "title": _s(it.get("title"), 120),
                    "subtitle": _s(it.get("subtitle"), 120), "body": _s(it.get("body"), 500)}
            if card["title"] or card["body"]:
                items.append(card)
        return {"type": "cards", "items": items} if items else None
    if kind == "panel":
        body = _s(b.get("body") or b.get("text"), 700)
        return {"type": "panel", "title": _s(b.get("title"), 120),
                "body": body} if body else None
    if kind == "split":
        # Two evidence objects side by side (a table beside its reading, a chart beside the
        # numbers behind it). ONE level deep -- a split inside a split is a layout, not an idea.
        cols = []
        for side in ("left", "right"):
            inner = []
            for ib in (b.get(side) if isinstance(b.get(side), list) else [])[:3]:
                nb = _norm_block(ib, facts)
                if nb and nb["type"] != "split":
                    inner.append(nb)
            cols.append(inner)
        return {"type": "split", "left": cols[0], "right": cols[1]} if any(cols) else None
    if kind == "callout":
        body = _s(b.get("body") or b.get("text"), 400)
        tone = _s(b.get("tone"), 10)
        return {"type": "callout", "tone": tone if tone in _TONES else "neutral",
                "body": body} if body else None
    if kind == "action":
        body = _s(b.get("body") or b.get("text"), 400)
        return {"type": "action", "body": body} if body else None
    return None


def _norm_slide(s, facts):
    if not isinstance(s, dict):
        return None
    kind = _s(s.get("kind"), 12).lower()
    if kind not in _SLIDE_KINDS:
        kind = "content"
    tone = _s(s.get("tone"), 10)
    blocks = []
    for b in (s.get("blocks") if isinstance(s.get("blocks"), list) else [])[:6]:
        nb = _norm_block(b, facts)
        if nb:
            blocks.append(nb)
    # The spine slot this slide fills (enforce_spine maps on it; "" = off-spine, e.g. a slide an
    # edit added on purpose). Normalized to the slot KEY whether the model wrote key or eyebrow.
    slot = report_spec.slot_of(_s(s.get("slot"), 40))
    slide = {"kind": kind, "slot": slot["key"] if slot else "",
             "eyebrow": _s(s.get("eyebrow"), 80), "title": _s(s.get("title"), 200),
             "subtitle": _s(s.get("subtitle"), 300),
             "tone": tone if tone in _TONES else "neutral",
             "source": _s(s.get("source"), 200), "blocks": blocks}
    return slide if (slide["title"] or blocks) else None


def _legacy_slides(p):
    """The pre-2026-07-29 payload ({landscape, what_happened, why, recommendations, asks}) as
    slides, so every deck stored under the old contract still renders."""
    def items(v):
        out = []
        for it in (v if isinstance(v, list) else [])[:6]:
            if isinstance(it, dict):
                out.append({"eyebrow": "", "title": _s(it.get("title"), 120), "subtitle": "",
                            "body": _s(it.get("body"), 500)})
            elif isinstance(it, str) and it.strip():
                out.append({"eyebrow": "", "title": "", "subtitle": "", "body": _s(it, 500)})
        return out

    land = p.get("landscape") if isinstance(p.get("landscape"), dict) else {}
    wh = p.get("what_happened") if isinstance(p.get("what_happened"), dict) else {}
    asks = p.get("asks") if isinstance(p.get("asks"), dict) else {}
    slides = []

    cards = items(land.get("business")) + items(land.get("media")) + items(land.get("voices"))
    if cards:
        slides.append({"kind": "content", "eyebrow": "", "title": "The Landscape", "subtitle": "",
                       "tone": "neutral", "source": "",
                       "blocks": [{"type": "cards", "items": cards[:4]}]})
    blocks = []
    if wh.get("summary"):
        blocks.append({"type": "text", "body": _s(wh["summary"], 700)})
    tiles = [{"label": _s(n.get("label"), 60), "value": _s(n.get("value"), 40),
              "note": _s(n.get("note"), 120), "tone": ""}
             for n in (wh.get("numbers") if isinstance(wh.get("numbers"), list) else [])[:8]
             if isinstance(n, dict) and (n.get("label") or n.get("value"))]
    if tiles:
        blocks.append({"type": "kpis", "items": tiles})
    working = _strs(wh.get("whats_working"))
    if working:
        blocks.append({"type": "bullets", "items": working, "ordered": False})
    if blocks:
        slides.append({"kind": "content", "eyebrow": "", "title": "What Happened", "subtitle": "",
                       "tone": "good", "source": "", "blocks": blocks})
    for title, rows, ordered in (("Why It Happened", _strs(p.get("why")), False),
                                 ("What We Should Do", _strs(p.get("recommendations")), True)):
        if rows:
            slides.append({"kind": "content", "eyebrow": "", "title": title, "subtitle": "",
                           "tone": "neutral", "source": "",
                           "blocks": [{"type": "bullets", "items": rows, "ordered": ordered}]})
    ask_blocks = []
    if _strs(asks.get("needed")):
        ask_blocks.append({"type": "bullets", "items": _strs(asks.get("needed")), "ordered": False})
    if _strs(asks.get("blocked")):
        ask_blocks.append({"type": "callout", "tone": "warn",
                           "body": "Currently blocked: " + "; ".join(_strs(asks.get("blocked")))})
    slides.append({"kind": "closing", "eyebrow": "", "title": "What We Need From You",
                   "subtitle": "" if ask_blocks else "Nothing is waiting on you right now.",
                   "tone": "neutral", "source": "", "blocks": ask_blocks})
    return slides


def normalize_payload(p, facts=None):
    """Coerce any parsed payload into the canonical shape the renderer trusts.

    Accepts the new slide payload, a legacy six-slide payload, or junk. `facts` seeds the fact pack
    when the payload does not carry one (a freshly generated deck); a stored payload keeps its own,
    which is what makes the lazy re-render byte-identical."""
    p = p if isinstance(p, dict) else {}
    pack = p.get("facts") if isinstance(p.get("facts"), dict) else (facts or {})
    pack = {k: v for k, v in pack.items()
            if isinstance(v, dict) and v.get("kind") in _FACT_KINDS}
    meta_in = p.get("meta") if isinstance(p.get("meta"), dict) else {}
    meta = {"headline": _s(meta_in.get("headline"), 200), "subhead": _s(meta_in.get("subhead"), 300),
            "period": _s(meta_in.get("period"), 120), "sources": _s(meta_in.get("sources"), 200)}

    raw_slides = p.get("slides") if isinstance(p.get("slides"), list) else None
    if raw_slides is None:
        raw_slides = _legacy_slides(p) if any(
            k in p for k in ("landscape", "what_happened", "why", "recommendations", "asks")) else []
    slides = []
    for s in raw_slides[:24]:
        ns = _norm_slide(s, pack)
        if ns:
            slides.append(ns)
    return {"meta": meta, "facts": pack, "slides": slides}


# --- The no-AI deck: honest, and now a real one --------------------------------------------------
_BLOCK_FOR_KIND = {"series": "chart", "table": "table", "compare": "compare",
                   "tiles": "kpis", "list": "bullets", "funnel": "funnel"}


def _fact_blocks(facts, keys):
    return [{"type": _BLOCK_FOR_KIND[facts[k]["kind"]], "fact": k}
            for k in keys if k in (facts or {})]


def _fact_slide(facts, eyebrow, title, left, right=()):
    """One content slide over one or two computed facts. With facts on both sides it renders as a
    `split`, so the no-AI deck fills its slides the same way a written one does. None when the
    workspace holds none of the facts named."""
    lb, rb = _fact_blocks(facts, left), _fact_blocks(facts, right)
    if not lb and not rb:
        return None
    if lb and rb:
        blocks = [{"type": "split", "left": lb, "right": rb}]
    else:
        blocks = lb or rb
    if not title:
        first = (left if lb else right)[0]
        title = facts[first].get("title") or ""
    return {"kind": "content", "eyebrow": eyebrow, "title": title, "subtitle": "",
            "tone": "neutral", "source": "", "blocks": blocks}


def _gap_slide(slot, body):
    """The honest empty-slot slide: the eight-slide standard never silently skips a chapter."""
    return {"kind": "content", "slot": slot["key"], "eyebrow": slot["eyebrow"],
            "title": "Nothing to show here yet", "subtitle": slot["purpose"],
            "tone": "warn", "source": "",
            "blocks": [{"type": "callout", "tone": "warn", "body": body}]}


_NOT_WIRED = ("No data source is wired up for this yet, so this report cannot answer it "
              "honestly. Naming the gap is the finding: wiring it is on the list.")


def draft_payload(inputs, client_name="", when=""):
    """The deterministic no-AI deck: cover + ONE slide per spine slot, filled from the facts each
    slot declares.

    This is the reference implementation of the strict eight-slide standard, and the backfill
    source `enforce_spine` reaches for when a model skips a slot. No written analysis -- nothing
    here is invented -- so the two prose slots (Research's why-it-matters lines, What-we'll-do's
    actions) state plainly that a strategist has not written them, rather than pretending."""
    inputs = inputs or {}
    facts = inputs.get("facts") or {}
    period = inputs.get("period") or ""
    client = client_name or ""
    slides = [{
        "kind": "cover", "eyebrow": "Performance review",
        "title": client or "Performance review",
        "subtitle": "What the numbers say for %s" % (period or date_label(when) or "this period"),
        "tone": "neutral", "source": "",
        "blocks": [{"type": "chips", "items": [c for c in (
            {"label": "Client", "value": _s(client, 120)} if client else None,
            {"label": "Window", "value": _s(period, 120)} if period else None,
            {"label": "Prepared", "value": date_label(when)} if when else None) if c]}],
    }]

    def cards(rows, eyebrow, cap=2):
        out = []
        for r in (rows or [])[:cap]:
            head, _sep, tail = r.partition(": ")
            out.append({"eyebrow": eyebrow, "title": _s(head if tail else "", 120),
                        "subtitle": "", "body": _s(tail or head, 400)})
        return out

    for slot in report_spec.SPINE:
        key = slot["key"]
        slide = None
        if key == "tasks":
            blocks = _fact_blocks(facts, [k for k in ("tasks_counts", "tasks_board")
                                          if k in facts])
            if blocks:
                slide = {"kind": "content", "eyebrow": slot["eyebrow"],
                         "title": "The board, exactly as it stands", "subtitle": "",
                         "tone": "neutral", "source": "", "blocks": blocks}
            else:
                slide = _gap_slide(slot, "No tasks are on the board yet.")
        elif key == "research":
            items = (cards(inputs.get("conditions"), "Conditions")
                     + cards(inputs.get("business"), "Industry")
                     + cards(inputs.get("media"), "Platforms")
                     + cards(inputs.get("competitors"), "Competitors"))
            if items:
                slide = {"kind": "content", "eyebrow": slot["eyebrow"],
                         "title": "What is happening around you", "subtitle":
                         "Drafted without AI: the why-this-matters readings are not written yet.",
                         "tone": "neutral", "source": "",
                         "blocks": [{"type": "cards", "items": items[:4]}]}
            else:
                slide = _gap_slide(slot, "No research is on file for this client yet.")
        elif key == "funnel":
            left = _fact_blocks(facts, ["funnel"])
            right = _fact_blocks(facts, ["funnel_notes"])
            if left:
                blocks = ([{"type": "split", "left": left, "right": right}] if right else left)
                slide = {"kind": "content", "eyebrow": slot["eyebrow"],
                         "title": (facts.get("funnel") or {}).get("title") or "The funnel",
                         "subtitle": "", "tone": "neutral", "source": "", "blocks": blocks}
            else:
                slide = _gap_slide(slot, _NOT_WIRED)
        elif key == "next_steps":
            slide = {"kind": "content", "slot": key, "eyebrow": slot["eyebrow"],
                     "title": "To be written by the strategist", "subtitle": slot["purpose"],
                     "tone": "neutral", "source": "",
                     "blocks": [{"type": "callout", "tone": "neutral",
                                 "body": "This draft was produced without AI, so it lists no "
                                         "actions -- the evidence slides carry the numbers, and "
                                         "the actions are agreed on the call."}]}
        else:
            have = report_spec.expand(slot, facts)
            if have:
                pair = have[:2]
                slide = _fact_slide(facts, slot["eyebrow"], "", (pair[0],), tuple(pair[1:]))
                if slide and len(pair) == 2:
                    slide["title"] = "%s, and %s" % (facts[pair[0]].get("title") or pair[0],
                                                     (facts[pair[1]].get("title")
                                                      or pair[1]).lower())
            if not slide:
                slide = _gap_slide(slot, _NOT_WIRED)
        slide["slot"] = key
        slides.append(slide)
    return normalize_payload({"meta": {"period": period}, "slides": slides}, facts)


def enforce_spine(payload, inputs, client_name="", when=""):
    """The strict eight-slide standard, enforced in CODE -- the prompt asks, this guarantees.

    Maps the model's slides onto the spine (by their `slot` field, else by a verbatim eyebrow),
    keeps the first cover, stamps every slide with its slot's canonical eyebrow and key, DROPS
    anything off-spine, and backfills a missing slot from the deterministic draft. Out: exactly
    `report_spec.DECK_SLIDES` slides where slide N is slot N-1 -- every client, every run."""
    ref = draft_payload(inputs, client_name=client_name, when=when)
    facts = payload.get("facts") or ref.get("facts") or {}
    ref_by_slot = {s.get("slot"): s for s in ref["slides"] if s.get("slot")}
    cover, by_slot = None, {}
    for s in payload.get("slides") or []:
        if s.get("kind") == "cover":
            if cover is None:
                cover = s
            continue
        slot = report_spec.slot_of(s.get("slot")) or report_spec.slot_of(s.get("eyebrow"))
        if slot and slot["key"] not in by_slot:
            by_slot[slot["key"]] = s
    slides = [cover or ref["slides"][0]]
    for slot in report_spec.SPINE:
        s = dict(by_slot.get(slot["key"]) or ref_by_slot[slot["key"]])
        s["kind"] = "content"           # a slot slide is always a content slide, never a divider
        s["slot"] = slot["key"]
        s["eyebrow"] = slot["eyebrow"]  # canonical, whatever the model wrote
        slides.append(s)
    return {"meta": payload.get("meta") or ref.get("meta") or {}, "facts": facts,
            "slides": slides}


def generate(client_name, when, inputs, caller):
    """The slide payload for `inputs`. Returns (payload, error): with a working `caller(system,
    user) -> (text, err)` the model writes it -- then `enforce_spine` pins the result to the
    eight-slide standard; on any failure (or caller None) the deterministic deck comes back with
    the reason -- a deck ALWAYS renders."""
    facts = (inputs or {}).get("facts") or {}
    if caller is not None:
        try:
            raw, err = caller(_gen_system((inputs or {}).get('objectives') or ('sales',)),
                              _material_text(client_name, when, inputs))
        except Exception as e:  # the report path never raises
            raw, err = "", str(e)
        if not err:
            parsed = _parse_json(raw)
            if parsed is not None:
                payload = normalize_payload(parsed, facts)
                if payload["slides"]:
                    return enforce_spine(payload, inputs, client_name, when), ""
                err = "the model returned no usable slides"
            else:
                err = "the model did not return usable JSON"
    else:
        err = "no AI model configured"
    return draft_payload(inputs, client_name=client_name, when=when), err


def revise(payload, instruction, caller, slide_no=None):
    """Apply a team edit instruction to an existing payload. Returns (new_payload, error);
    on any failure the ORIGINAL payload comes back with the reason (never a broken deck).

    `slide_no` (1-based, the deck's own numbering, cover = 1) scopes the instruction to ONE slide
    -- the per-slide path of the Reports tab's Edit-with-AI control. An edit is deliberately NOT
    re-run through enforce_spine: "add a slide about project X" is a legitimate instruction, and
    the extra slide (slot "") must survive.

    The fact pack is stripped from what the model sees (it is data, often large, and editing it is
    never the instruction) and re-attached to the result, so a revise can never lose the numbers."""
    if caller is None:
        return payload, "no AI model configured"
    current = normalize_payload(payload)
    facts = current["facts"]
    instruction = (instruction or "").strip()
    if slide_no:
        try:
            target = current["slides"][int(slide_no) - 1]
        except (IndexError, ValueError, TypeError):
            return payload, "no slide %s in this deck" % slide_no
        instruction = ("Apply this edit ONLY to slide %s (%s: %s). Keep every other slide "
                       "exactly as it is.\n%s"
                       % (slide_no, target.get("eyebrow") or target.get("kind"),
                          target.get("title") or "untitled", instruction))
    visible = {"meta": current["meta"], "slides": current["slides"],
               "available_fact_keys": sorted(facts)}
    user = "Current deck JSON:\n%s\n\nEdit instruction:\n%s" % (
        json.dumps(visible, indent=1), instruction)
    try:
        raw, err = caller(_REVISE_SYSTEM, user)
    except Exception as e:
        raw, err = "", str(e)
    if err:
        return payload, err
    parsed = _parse_json(raw)
    if parsed is None:
        return payload, "the model did not return usable JSON"
    revised = normalize_payload(parsed, facts)
    if not revised["slides"]:
        return payload, "the edit produced no usable slides"
    return revised, ""


# --- 4. Brand: the deck wears the client's identity ----------------------------------------------
# The AGORA house palette (website design system) is the floor; a client's own colours, when the
# Company tab's brand guide lists them, take the accent slots. Identity never rides on colour alone:
# every tone also carries text (a +/- delta, a "best week" tag), so the deck reads in grayscale.
HOUSE_PALETTE = {
    "ink": "#101410", "body": "#333c33", "muted": "#7b857b", "line": "#e2e8e2",
    "canvas": "#e8ebe6", "paper": "#ffffff", "panel": "#fbfdfb",
    "accent": "#4FA84A", "accent2": "#6A6AEA", "on_accent": "#ffffff",
    "good": "#2E7D43", "warn": "#9a6314", "bad": "#9F2D20",
}
_HEX_RE = re.compile(r"#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})\b")


def _rgb(hex_color):
    h = hex_color.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _luminance(hex_color):
    r, g, b = _rgb(hex_color)
    return (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255.0


def palette_of(colors_text):
    """The deck palette, from the free-text `colors` field of the Company tab's brand guide.

    The field is prose ("Deep pine #21582B, cream #F7F5E7, gold accent"), so we take the hex codes
    in the order they are written and sort them by ROLE rather than position.

    🔴 Position alone is not enough: a brand list almost always includes a near-white (cream, bone,
    off-white), and that colour is unreadable as type. Only colours dark enough to read on paper
    become accents; a very light one becomes the stage the slide sits on instead, which is where a
    brand cream actually belongs. Anything not supplied stays the house value, so a blank brand
    guide is still a perfectly good deck."""
    pal = dict(HOUSE_PALETTE)
    found = []
    for c in _HEX_RE.findall(colors_text or ""):
        if c.lower() not in [f.lower() for f in found]:
            found.append(c)
    if not found:
        return pal
    readable = [c for c in found if _luminance(c) < 0.62]     # legible as type on white
    light = [c for c in found if _luminance(c) >= 0.86]       # a canvas, never a text colour
    if readable:
        pal["accent"] = readable[0]
        if len(readable) > 1:
            pal["accent2"] = readable[1]
        dark = [c for c in readable if _luminance(c) < 0.28]
        if dark:
            pal["ink"] = dark[0]
    if light:
        pal["canvas"] = light[0]
    pal["on_accent"] = "#ffffff" if _luminance(pal["accent"]) < 0.62 else pal["ink"]
    return pal


_MARK_RE = re.compile(r"^\s*<(svg|img)\b", re.I)


def _mark(markup):
    """A stored logo, if it is the self-contained markup we wrote (an inline <svg> or a data: <img>).

    Logos live INSIDE the workspace JSON as markup (seed_workspace.brand_for / set_client_logo), so
    they are inlined verbatim rather than escaped -- this gate is what keeps that safe."""
    m = (markup or "").strip()
    if not _MARK_RE.match(m):
        return ""
    low = m.lower()
    if "<script" in low or "javascript:" in low or "onerror" in low or "onload" in low:
        return ""
    if low.startswith("<img") and "src=\"data:image/" not in low and "src='data:image/" not in low:
        return ""
    return m


def brand_kit(ws):
    """Everything the deck needs to wear the client's identity: their crest, the AGORA mark, and a
    palette from the Company tab's brand guide. Safe on a bare/None workspace."""
    ws = ws or {}
    b = ws.get("brand") if isinstance(ws.get("brand"), dict) else {}
    company = ws.get("company") if isinstance(ws.get("company"), dict) else {}
    cbrand = company.get("brand") if isinstance(company.get("brand"), dict) else {}
    client_logo, agora_logo = _mark(b.get("client_logo")), _mark(b.get("agora_logo"))
    return {"client_logo": client_logo, "agora_logo": agora_logo,
            "crest_css": mark_css_url(client_logo), "agora_css": mark_css_url(agora_logo),
            "palette": palette_of(cbrand.get("colors"))}


# --- 5. Render: payload -> the self-contained deck ------------------------------------------------
# A fixed 1280x720 stage scaled to the window (--k), one slide visible at a time, arrow keys / click
# / dots to move, `p` to print. The ONLY JS in the document is that navigator, and it is written
# esprima-4.x-safe (no `?.`, no `??`) like every other script in this repo. Everything else -- the
# charts, the tables, the tone colours -- is CSS over server-rendered markup, so the deck prints and
# survives with scripting off (@media print reveals every slide).
_CSS = """
*{margin:0;padding:0;box-sizing:border-box}
html,body{height:100%}
body{background:var(--canvas);color:var(--body);overflow:hidden;
  font-family:'Segoe UI',system-ui,-apple-system,'Helvetica Neue',Arial,sans-serif;
  -webkit-font-smoothing:antialiased}
h1,h2,h3{color:var(--ink);letter-spacing:-.02em;line-height:1.1;font-weight:800}
.stage{position:fixed;inset:0;display:flex;align-items:center;justify-content:center}
.slide{position:absolute;width:var(--sw);height:var(--sh);background:var(--paper);display:none;
  flex-direction:column;overflow:hidden;transform:scale(var(--k,1));transform-origin:center center;
  box-shadow:0 18px 60px rgba(0,0,0,.28)}
.slide.on{display:flex}
.chrome{flex:none;display:flex;align-items:center;gap:20px;padding:13px 44px;
  border-bottom:1px solid var(--line)}
.chrome .crest{width:38px;height:38px;flex:none;display:flex;align-items:center;
  justify-content:center;border:1px solid var(--line);border-radius:8px;background:#fff;padding:3px}
.chrome .crest{background:var(--crest) content-box center/contain no-repeat #fff}
.chrome .mid{flex:1;min-width:0}
.chrome .mid .t{font-weight:700;color:var(--ink);font-size:14px}
.chrome .mid .s{font-size:10px;letter-spacing:.09em;text-transform:uppercase;color:var(--muted);
  margin-top:2px}
.chrome .by{flex:none;display:flex;align-items:center;gap:9px}
.chrome .by .lbl{font-size:9px;letter-spacing:.1em;text-transform:uppercase;color:var(--muted);
  text-align:right;line-height:1.3}
.chrome .by .mark{width:88px;height:22px;flex:none;font-size:11px;font-weight:800;
  letter-spacing:.1em;color:var(--ink);text-align:right;
  background:var(--agoramark) center right/contain no-repeat}
.body{flex:1;min-height:0;padding:26px 44px 14px;display:flex;flex-direction:column;gap:14px}
.foot{flex:none;display:flex;align-items:center;justify-content:space-between;gap:20px;
  padding:0 44px 15px;font-size:10.5px;color:var(--muted)}
.foot .src{font-style:italic;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.foot .no{font-weight:800;color:var(--ink);font-variant-numeric:tabular-nums;flex:none}

.shead{flex:none;border-bottom:1px solid var(--line);padding-bottom:11px}
.shead .eyebrow{font-size:10.5px;letter-spacing:.16em;text-transform:uppercase;font-weight:800;
  color:var(--accent);margin-bottom:7px}
.shead h2{font-size:29px;display:flex;align-items:flex-start;gap:13px}
.shead h2::before{content:'';flex:none;width:4px;align-self:stretch;min-height:26px;
  background:var(--accent);border-radius:2px}
.shead.good h2::before{background:var(--good)}
.shead.warn h2::before{background:var(--warn)}
.shead.bad h2::before{background:var(--bad)}
.shead .sub{font-size:13px;color:var(--muted);margin-top:8px;max-width:96ch}
.blocks{flex:1;min-height:0;display:flex;flex-direction:column;justify-content:center;
  gap:13px;overflow:hidden}

/* Cover / section / closing */
.big{flex:1;display:flex;flex-direction:column;justify-content:center;gap:0}
.big .eyebrow{font-size:12px;letter-spacing:.19em;text-transform:uppercase;font-weight:800;
  color:var(--accent);margin-bottom:20px}
.big h1{font-size:64px;max-width:22ch}
.big .sub{font-size:17px;color:var(--body);margin-top:20px;max-width:74ch;line-height:1.5}
.big .rule{width:92px;height:6px;border-radius:3px;background:var(--accent);
  margin:26px 0}
.slide.section{background:var(--ink)}
.slide.section .chrome{border-bottom-color:rgba(255,255,255,.16)}
.slide.section .chrome .mid .t{color:#fff}
.slide.section h1,.slide.section .kpi .v{color:#fff}
.slide.section .sub{color:rgba(255,255,255,.78)}
.slide.section .kpi{background:rgba(255,255,255,.07);border-color:rgba(255,255,255,.18)}
.slide.section .kpi .l,.slide.section .kpi .n{color:rgba(255,255,255,.72)}
.slide.section .foot,.slide.section .foot .no{color:rgba(255,255,255,.6)}
/* The stored AGORA mark is the dark-on-light one; knock it out to white on the dark divider
   rather than shipping a second asset into the workspace JSON. */
.slide.section .chrome .by .mark{filter:brightness(0) invert(1)}
.slide.section .chrome .by .lbl{color:rgba(255,255,255,.6)}
.slide.section .big .rule{background:#fff;opacity:.85}

/* Blocks */
.text{font-size:15px;line-height:1.62;color:var(--body);max-width:104ch}
.text b,.text strong{color:var(--ink)}
.kpis{display:flex;flex-wrap:wrap;gap:12px}
.kpi{flex:1 1 0;min-width:150px;border:1px solid var(--line);border-radius:13px;padding:14px 16px;
  background:var(--panel)}
.kpi .l{font-size:10px;letter-spacing:.1em;text-transform:uppercase;font-weight:800;color:var(--muted)}
.kpi .v{font-size:31px;font-weight:800;color:var(--ink);line-height:1.06;margin-top:6px;
  letter-spacing:-.025em;font-variant-numeric:tabular-nums}
.kpi .n{font-size:11.5px;color:var(--muted);margin-top:5px;line-height:1.4}
.kpi.good .v{color:var(--good)}
.kpi.warn .v{color:var(--warn)}
.kpi.bad .v{color:var(--bad)}
.strip{display:flex;flex-wrap:wrap;gap:10px}
.strip .s{flex:1 1 0;min-width:112px;border:1px solid var(--line);border-radius:11px;
  padding:9px 12px;background:var(--panel)}
.strip .s .l{font-size:9px;letter-spacing:.1em;text-transform:uppercase;font-weight:800;
  color:var(--muted)}
.strip .s .v{font-size:17px;font-weight:800;color:var(--ink);margin-top:3px;
  letter-spacing:-.015em;font-variant-numeric:tabular-nums}
.chips{display:flex;flex-wrap:wrap;gap:34px}
.chip .l{font-size:10px;letter-spacing:.1em;text-transform:uppercase;font-weight:800;color:var(--muted)}
.chip .v{font-size:14px;color:var(--ink);font-weight:700;margin-top:4px}
ul.list,ol.list{padding-left:22px;display:flex;flex-direction:column;gap:9px}
ul.list li,ol.list li{font-size:14.5px;line-height:1.55;max-width:96ch}
ul.list li::marker{color:var(--accent)}
ol.list li::marker{color:var(--accent2);font-weight:800}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:14px}
.card{border:1px solid var(--line);border-radius:13px;padding:15px 16px;background:var(--panel)}
.card .e{font-size:9.5px;letter-spacing:.13em;text-transform:uppercase;font-weight:800;
  color:var(--accent2);margin-bottom:6px}
.card h3{font-size:15px;margin-bottom:4px}
.card .s{font-size:11.5px;color:var(--muted);margin-bottom:7px}
.card .b{font-size:13px;line-height:1.55;color:var(--body)}
.callout{display:flex;gap:11px;align-items:flex-start;border-radius:11px;padding:12px 15px;
  font-size:13.5px;line-height:1.5;border:1px solid var(--line);background:var(--panel);color:var(--body)}
.callout .tag{flex:none;font-size:9.5px;font-weight:800;letter-spacing:.11em;text-transform:uppercase;
  padding-top:2px;color:var(--muted)}
.callout.good{background:#eef5ef;border-color:#c6ddca;color:#1d5b30}
.callout.good .tag{color:var(--good)}
.callout.warn{background:#fdf4e8;border-color:#f0dcbb;color:#7a4e10}
.callout.warn .tag{color:var(--warn)}
.callout.bad{background:#f9eae8;border-color:#eec9c3;color:#84291d}
.callout.bad .tag{color:var(--bad)}
.action{display:flex;gap:11px;align-items:baseline;border-left:4px solid var(--accent);
  padding:7px 0 7px 16px;font-size:14.5px;line-height:1.5;color:var(--ink)}
.action .tag{flex:none;font-size:10px;font-weight:800;letter-spacing:.12em;text-transform:uppercase;
  color:var(--accent)}
.cap{font-size:11.5px;color:var(--muted)}
/* Two evidence objects side by side -- the layout that lets one slide carry a figure AND its
   reading, instead of spending a whole slide on each. */
.split{display:grid;grid-template-columns:1fr 1fr;gap:24px;align-items:stretch;
  flex:none;min-height:0}
.split.grow{flex:1}
.split .scol{display:flex;flex-direction:column;gap:12px;min-width:0;overflow:hidden}
.split .figure.fill{flex:1}
.split .cols{min-height:170px}
.split .kpis{flex-direction:column}
.split .kpi{min-width:0}
.split .text,.split li{font-size:13.5px}
/* A cell max-width is advisory under auto table layout, so a long ad name pushes the last
   column off the edge of a half-slide. Fixed layout with an explicit name column is the only
   thing that actually holds. */
.split table{font-size:11.5px;table-layout:fixed}
.split th:first-child,.split td:first-child{width:30%}
/* The full-width table sits flush to the slide margin; inside a split that same flush
   edge is the clipping boundary, so the last column needs real clearance. */
.split th.r,.split td.r{padding-right:5px}
.split th{font-size:9px;padding-right:9px}
.split td{padding:7px 9px 7px 0}
.split td.name{max-width:150px}
.panel{border-left:3px solid var(--accent2);padding:3px 0 3px 16px}
.panel .pt{font-size:10.5px;letter-spacing:.13em;text-transform:uppercase;font-weight:800;
  color:var(--accent2);margin-bottom:6px}
.panel .pb{font-size:14px;line-height:1.6;color:var(--body);max-width:96ch}

/* Chart: columns (a trend) or rows (a ranking). Bars are CSS, values are always written out too. */
.figure{display:flex;flex-direction:column;gap:9px;min-height:0;min-width:0;flex:none}
.figure.fill{flex:1}
.figure .ftitle{font-size:12.5px;font-weight:800;color:var(--ink)}
.figure .fsub{font-size:11px;color:var(--muted);margin-top:-6px}
.cols{flex:1;min-height:96px;display:flex;align-items:stretch;gap:12px}
.cols .col{flex:1;display:grid;grid-template-rows:auto 1fr auto;gap:6px;text-align:center}
.cols .cv{font-size:13px;font-weight:800;color:var(--ink);font-variant-numeric:tabular-nums}
.cols .barwrap{display:flex;align-items:flex-end;min-height:44px}
.cols .bar{width:100%;border-radius:5px 5px 0 0;background:var(--accent);opacity:.42;min-height:3px}
.cols .col.best .bar{opacity:1}
.cols .col.best .cv{color:var(--accent)}
.cols .cl{font-size:10.5px;color:var(--muted);letter-spacing:.02em}
.cols .cn{font-size:9.5px;color:var(--accent);font-weight:800;text-transform:uppercase;
  letter-spacing:.08em}
/* The drawn funnel: centered bars tapering on a log scale, the step-to-step rate between them.
   Server-computed widths -- CSS only paints. */
.funnel{flex:1;min-height:150px;display:flex;flex-direction:column;align-items:center;
  justify-content:center;gap:4px}
.funnel .fstep{display:flex;align-items:center;justify-content:space-between;gap:10px;
  min-width:150px;padding:9px 14px;border-radius:9px;background:var(--accent);color:var(--on_accent)}
.funnel .fstep .fl{font-size:11px;font-weight:800;letter-spacing:.06em;text-transform:uppercase;
  opacity:.85;white-space:nowrap}
.funnel .fstep .fv{font-size:15px;font-weight:800;font-variant-numeric:tabular-nums;
  white-space:nowrap}
.funnel .fstep:nth-child(n+3){opacity:.88}
.funnel .fstep:nth-child(n+5){opacity:.76}
.funnel .frate{font-size:10.5px;font-weight:800;color:var(--accent2);
  font-variant-numeric:tabular-nums;padding:1px 0}
.funnel .frate:empty{display:none}
.rows{display:flex;flex-direction:column;gap:8px}
.rows .row{display:grid;grid-template-columns:minmax(0,0.9fr) 1.5fr auto;
  align-items:center;gap:12px}
.rows .rl{font-size:12.5px;color:var(--ink);font-weight:600;line-height:1.3}
.rows .track{height:15px;border-radius:8px;background:var(--line);overflow:hidden}
.rows .fill{height:100%;border-radius:8px;background:var(--accent);opacity:.85;min-width:2px}
.rows .rv{font-size:12.5px;font-weight:800;color:var(--ink);text-align:right;
  font-variant-numeric:tabular-nums}

/* Table */
table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}
th{font-size:9.5px;letter-spacing:.11em;text-transform:uppercase;color:var(--muted);font-weight:800;
  text-align:left;padding:0 12px 8px 0;border-bottom:1px solid var(--line)}
td{font-size:13px;color:var(--body);padding:8px 12px 8px 0;border-bottom:1px solid var(--line)}
tr:last-child td{border-bottom:0}
th.r,td.r{text-align:right;padding-right:0}
td.name{color:var(--ink);font-weight:600;max-width:320px;overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap}
tr.good td.name::after{content:' BEST';font-size:9px;font-weight:800;color:var(--good);
  letter-spacing:.1em}
tr.bad td.name::after{content:' WEAKEST';font-size:9px;font-weight:800;color:var(--bad);
  letter-spacing:.1em}
tr.good td{background:rgba(46,125,67,.06)}
tr.bad td{background:rgba(159,45,32,.05)}

/* Before / after */
.compare{display:grid;grid-template-columns:1fr auto 1fr;align-items:stretch;gap:16px}
.side{border:1px solid var(--line);border-radius:13px;padding:14px 16px;background:var(--panel)}
.side .st{font-size:13px;font-weight:800;color:var(--ink)}
.side .ss{font-size:10.5px;color:var(--muted);margin-bottom:9px}
.side .r{display:flex;justify-content:space-between;gap:12px;padding:5px 0;font-size:13px;
  border-top:1px solid var(--line)}
.side .r .k{color:var(--muted)}
.side .r .v{font-weight:800;color:var(--ink);font-variant-numeric:tabular-nums}
.side.after{border-color:var(--accent)}
.delta{display:flex;flex-direction:column;align-items:center;justify-content:center;
  padding:0 6px;text-align:center}
.delta .d{font-size:30px;font-weight:800;letter-spacing:-.03em;color:var(--ink)}
.delta.good .d{color:var(--good)}
.delta.bad .d{color:var(--bad)}
.delta .dl{font-size:10.5px;color:var(--muted);max-width:12ch;line-height:1.3;margin-top:3px}

/* Navigator (the only JS in the document) */
.nav{position:fixed;left:0;right:0;bottom:0;height:52px;display:flex;align-items:center;
  justify-content:center;gap:14px}
.nav button{border:1px solid var(--line);background:var(--paper);color:var(--ink);width:30px;height:30px;
  border-radius:50%;cursor:pointer;font-size:14px;line-height:1}
.dots{display:flex;gap:6px;align-items:center}
.dots .dot{width:7px;height:7px;border-radius:50%;border:0;padding:0;cursor:pointer;
  background:rgba(0,0,0,.22)}
.dots .dot.on{background:var(--accent);transform:scale(1.35)}

@media print{
  body{overflow:visible;background:#fff}
  .stage{position:static;display:block}
  .nav{display:none}
  .slide{display:flex !important;position:relative;transform:none;box-shadow:none;
    page-break-after:always;break-after:page}
}
"""


_DATA_URI_RE = re.compile(r"src\s*=\s*[\"'](data:image/[^\"']+)[\"']", re.I)


def mark_css_url(markup):
    """A stored logo as ONE `url(...)` value for CSS, or "" when there is nothing usable.

    Logos are stored as markup (an inline `<svg>` or a `data:` `<img>`; see seed_workspace), but a
    deck needs the same image on every slide -- so it becomes a custom property declared once
    instead of markup duplicated per slide. An `<svg>` is base64'd rather than percent-escaped: its
    own `#` colour literals would terminate the url() token."""
    m = (markup or "").strip()
    if not m:
        return ""
    hit = _DATA_URI_RE.search(m)
    if hit:
        return "url(\"%s\")" % hit.group(1).replace("\"", "%22")
    if m.lower().startswith("<svg"):
        encoded = base64.b64encode(m.encode("utf-8")).decode("ascii")
        return "url(\"data:image/svg+xml;base64,%s\")" % encoded
    return ""


def _css(palette, crest_css="", agora_css=""):
    """The stylesheet for one deck: the palette, the stage size and the two brand marks as custom
    properties in front of the fixed sheet. Injecting via `var(--token)` rather than
    string-formatting the whole sheet keeps every literal `%` in the CSS a real percentage."""
    root = ";".join("--%s:%s" % (k, v) for k, v in sorted(palette.items()))
    marks = ""
    if crest_css:
        marks += ";--crest:%s" % crest_css
    if agora_css:
        marks += ";--agoramark:%s" % agora_css
    return ("@page{size:%dpx %dpx;margin:0}:root{%s;--sw:%dpx;--sh:%dpx%s}%s"
            % (STAGE_W, STAGE_H, root, STAGE_W, STAGE_H, marks, _CSS))


def _esc(s):
    return html.escape(str(s or ""), quote=True)


def _pctw(value, top):
    """A bar length as a percentage string, floored so a real-but-tiny value still shows."""
    if top <= 0:
        return "0%"
    return "%.1f%%" % max(1.5, min(100.0, 100.0 * value / top))


def _fig_head(fact, slide_title=""):
    """The figure's own caption line -- minus its title when the SLIDE is already titled with it
    (a slide built around one figure must not say the same thing twice)."""
    head = ""
    if (fact.get("title") or "").strip().lower() != (slide_title or "").strip().lower():
        head = "<div class=\"ftitle\">%s</div>" % _esc(fact.get("title"))
    if fact.get("subtitle"):
        head += "<div class=\"fsub\">%s</div>" % _esc(fact["subtitle"])
    return head


def _chart_html(fact, caption="", slide_title=""):
    points = fact.get("points") or []
    values = [_num(p.get("value")) for p in points]
    top = max(values + [0.0])
    if fact.get("orient") == "rows":
        rows = "".join(
            "<div class=\"row\"><div class=\"rl\">%s</div>"
            "<div class=\"track\"><div class=\"fill\" style=\"width:%s\"></div></div>"
            "<div class=\"rv\">%s</div></div>"
            % (_esc(p.get("label")), _pctw(_num(p.get("value")), top), _esc(p.get("text")))
            for p in points)
        return ("<div class=\"figure\">%s<div class=\"rows\">%s</div>%s</div>"
                % (_fig_head(fact, slide_title), rows, caption))
    cols = "".join(
        "<div class=\"col%s\"><div class=\"cv\">%s</div>"
        "<div class=\"barwrap\"><div class=\"bar\" style=\"height:%s\"></div></div>"
        "<div><div class=\"cl\">%s</div>%s</div></div>"
        % (" best" if p.get("best") else "", _esc(p.get("text")),
           _pctw(_num(p.get("value")), top), _esc(p.get("label")),
           ("<div class=\"cn\">best</div>" if p.get("best") else ""))
        for p in points)
    # Only a column chart claims the leftover height -- a ranking or a table must stay welded to
    # the prose under it, or the caption floats to the bottom of the slide on its own.
    return ("<div class=\"figure fill\">%s<div class=\"cols\">%s</div>%s</div>"
            % (_fig_head(fact, slide_title), cols, caption))


def _table_html(fact, caption="", slide_title=""):
    cols = fact.get("columns") or []
    head = "".join("<th%s>%s</th>" % (" class=\"r\"" if c.get("align") == "right" else "",
                                      _esc(c.get("label"))) for c in cols)
    body = []
    for r in fact.get("rows") or []:
        tone = r.get("_tone") or ""
        cells = []
        for i, c in enumerate(cols):
            cls = "r" if c.get("align") == "right" else ("name" if i == 0 else "")
            cells.append("<td%s>%s</td>" % ((" class=\"%s\"" % cls) if cls else "",
                                            _esc(r.get(c.get("key"), ""))))
        body.append("<tr%s>%s</tr>" % ((" class=\"%s\"" % tone) if tone else "", "".join(cells)))
    out = ["<div class=\"figure\">%s" % _fig_head(fact, slide_title)]
    out.append("<table><thead><tr>%s</tr></thead><tbody>%s</tbody></table>%s</div>"
               % (head, "".join(body), caption))
    return "".join(out)


def _funnel_html(fact, caption="", slide_title=""):
    """The drawn funnel: one centered bar per step, width on a log scale (linear width would make
    every step below Impressions invisible -- the volumes span orders of magnitude), the rate from
    the step above written between the bars. Values are always written out too, so the graph reads
    in grayscale and the widths never have to be trusted."""
    rows = fact.get("rows") or []
    vols = [max(1.0, _num(r.get("_value"))) for r in rows]
    if not vols:
        return ""
    logs = [math.log10(v) for v in vols]
    lo, hi = min(logs), max(logs)
    spread = (hi - lo) or 1.0
    parts = []
    for i, r in enumerate(rows):
        width = 34.0 + 66.0 * ((logs[i] - lo) / spread)
        if i:
            parts.append("<div class=\"frate\">%s</div>"
                         % _esc("" if r.get("rate") in ("-", "") else r.get("rate")))
        parts.append(
            "<div class=\"fstep\" style=\"width:%.1f%%\">"
            "<span class=\"fl\">%s</span><span class=\"fv\">%s</span></div>"
            % (width, _esc(r.get("step")), _esc(r.get("volume"))))
    note = ("<div class=\"fsub\">%s</div>" % _esc(fact["note"])) if fact.get("note") else ""
    return ("<div class=\"figure fill\">%s<div class=\"funnel\">%s</div>%s%s</div>"
            % (_fig_head(fact, slide_title), "".join(parts), note, caption))


def _compare_html(fact, caption="", slide_title=""):
    def side(d, cls):
        rows = "".join("<div class=\"r\"><span class=\"k\">%s</span><span class=\"v\">%s</span></div>"
                       % (_esc(r.get("label")), _esc(r.get("value"))) for r in d.get("rows") or [])
        return ("<div class=\"side %s\"><div class=\"st\">%s</div><div class=\"ss\">%s</div>%s</div>"
                % (cls, _esc(d.get("title")), _esc(d.get("subtitle")), rows))

    delta = fact.get("delta") or {}
    tone = delta.get("tone") if delta.get("tone") in _TONES else "neutral"
    return ("<div class=\"figure\">%s<div class=\"compare\">%s"
            "<div class=\"delta %s\"><div class=\"d\">%s</div><div class=\"dl\">%s</div></div>"
            "%s</div>%s</div>"
            % (_fig_head(fact, slide_title), side(fact.get("before") or {}, "before"), tone,
               _esc(delta.get("headline")), _esc(delta.get("label")),
               side(fact.get("after") or {}, "after"), caption))


def _kpis_html(items):
    """Headline tiles, plus a compact strip for anything marked `tier: secondary` (a computed fact
    can carry a dozen metrics; only the money story deserves a full tile)."""
    primary = [it for it in items if it.get("tier") != "secondary"]
    secondary = [it for it in items if it.get("tier") == "secondary"]
    out = ""
    if primary:
        out += "<div class=\"kpis\">%s</div>" % "".join(
            "<div class=\"kpi%s\"><div class=\"l\">%s</div><div class=\"v\">%s</div>"
            "<div class=\"n\">%s</div></div>"
            % ((" " + it["tone"]) if it.get("tone") else "", _esc(it.get("label")),
               _esc(it.get("value")), _esc(it.get("note")))
            for it in primary)
    if secondary:
        out += "<div class=\"strip\">%s</div>" % "".join(
            "<div class=\"s\"><div class=\"l\">%s</div><div class=\"v\">%s</div></div>"
            % (_esc(it.get("label")), _esc(it.get("value"))) for it in secondary)
    return out


def _block_html(b, facts, slide_title=""):
    kind = b.get("type")
    caption = ("<div class=\"cap\">%s</div>" % _esc(b["caption"])) if b.get("caption") else ""
    if kind in _FACT_BLOCKS and b.get("fact"):
        fact = (facts or {}).get(b["fact"])
        if not fact:
            return ""
        if kind == "chart":
            return _chart_html(fact, caption, slide_title)
        if kind == "funnel":
            return _funnel_html(fact, caption, slide_title)
        if kind == "table":
            return _table_html(fact, caption, slide_title)
        if kind == "compare":
            return _compare_html(fact, caption, slide_title)
        if kind == "kpis":
            return _kpis_html(fact.get("items") or []) + caption
        if kind == "bullets":
            return ("<div class=\"figure\">%s<ul class=\"list\">%s</ul>%s</div>"
                    % (_fig_head(fact, slide_title),
                       "".join("<li>%s</li>" % _esc(i) for i in fact.get("items") or []),
                       caption))
    if kind == "kpis":
        return _kpis_html(b.get("items") or [])
    if kind == "text":
        return "<div class=\"text\">%s</div>" % _esc(b.get("body"))
    if kind == "bullets":
        tag = "ol" if b.get("ordered") else "ul"
        return "<%s class=\"list\">%s</%s>" % (
            tag, "".join("<li>%s</li>" % _esc(x) for x in b.get("items") or []), tag)
    if kind == "chips":
        return "<div class=\"chips\">%s</div>" % "".join(
            "<div class=\"chip\"><div class=\"l\">%s</div><div class=\"v\">%s</div></div>"
            % (_esc(it.get("label")), _esc(it.get("value"))) for it in b.get("items") or [])
    if kind == "cards":
        cards = []
        for it in b.get("items") or []:
            bits = []
            if it.get("eyebrow"):
                bits.append("<div class=\"e\">%s</div>" % _esc(it["eyebrow"]))
            if it.get("title"):
                bits.append("<h3>%s</h3>" % _esc(it["title"]))
            if it.get("subtitle"):
                bits.append("<div class=\"s\">%s</div>" % _esc(it["subtitle"]))
            if it.get("body"):
                bits.append("<div class=\"b\">%s</div>" % _esc(it["body"]))
            cards.append("<div class=\"card\">%s</div>" % "".join(bits))
        return "<div class=\"cards\">%s</div>" % "".join(cards)
    if kind == "panel":
        title = ("<div class=\"pt\">%s</div>" % _esc(b["title"])) if b.get("title") else ""
        return "<div class=\"panel\">%s<div class=\"pb\">%s</div></div>" % (title,
                                                                            _esc(b.get("body")))
    if kind == "split":
        # A split claims the slide's spare height ONLY when something in it can use it -- a column
        # chart. Two panels side by side must stay their own size and centre, not stretch into a
        # half-empty slide.
        inner = (b.get("left") or []) + (b.get("right") or [])
        grow = any(x.get("type") == "funnel"
                   or (x.get("type") == "chart"
                       and ((facts or {}).get(x.get("fact")) or {}).get("orient") != "rows")
                   for x in inner)
        return ("<div class=\"split%s\"><div class=\"scol\">%s</div>"
                "<div class=\"scol\">%s</div></div>"
                % (" grow" if grow else "",
                   "".join(_block_html(x, facts, slide_title) for x in b.get("left") or []),
                   "".join(_block_html(x, facts, slide_title) for x in b.get("right") or [])))
    if kind == "callout":
        tone = b.get("tone") if b.get("tone") in _TONES else "neutral"
        tag = {"good": "Working", "warn": "Watch", "bad": "Risk"}.get(tone, "Note")
        return ("<div class=\"callout %s\"><span class=\"tag\">%s</span><span>%s</span></div>"
                % (tone, tag, _esc(b.get("body"))))
    if kind == "action":
        return ("<div class=\"action\"><span class=\"tag\">We'll action</span>"
                "<span>%s</span></div>" % _esc(b.get("body")))
    return ""


def _slide_html(slide, facts, number, total, client_name, deck_title, marks):
    blocks = "".join(_block_html(b, facts, slide.get("title"))
                     for b in slide.get("blocks") or [])
    if slide["kind"] in ("cover", "section", "closing"):
        head = []
        if slide.get("eyebrow"):
            head.append("<div class=\"eyebrow\">%s</div>" % _esc(slide["eyebrow"]))
        head.append("<h1>%s</h1>" % _esc(slide.get("title")))
        if slide.get("subtitle"):
            head.append("<div class=\"sub\">%s</div>" % _esc(slide["subtitle"]))
        head.append("<div class=\"rule\"></div>")
        # A cover's blocks are its meta strip and belong at the foot of the slide; a section or
        # closing slide's blocks are hero stats and belong with the title, not adrift below it.
        body = ("<div class=\"big\">%s</div>%s" % ("".join(head), blocks)
                if slide["kind"] == "cover"
                else "<div class=\"big\">%s%s</div>" % ("".join(head), blocks))
    else:
        head = []
        if slide.get("eyebrow"):
            head.append("<div class=\"eyebrow\">%s</div>" % _esc(slide["eyebrow"]))
        head.append("<h2>%s</h2>" % _esc(slide.get("title")))
        if slide.get("subtitle"):
            head.append("<div class=\"sub\">%s</div>" % _esc(slide["subtitle"]))
        tone = slide.get("tone") if slide.get("tone") in _TONES else "neutral"
        body = ("<div class=\"shead %s\">%s</div><div class=\"blocks\">%s</div>"
                % (tone, "".join(head), blocks))
    # 🔴 The marks are CSS backgrounds (declared ONCE in :root), never markup repeated per slide.
    # Inlining a logo into every chrome made a 3-slide deck 1.9 MB and would have made a 13-slide
    # one ~8 MB, on a route that is deliberately `no-store`.
    crest = ("<div class=\"crest\" role=\"img\" aria-label=\"%s logo\"></div>"
             % _esc(client_name)) if marks.get("crest_css") else "<div class=\"crest\"></div>"
    mark = ("<div class=\"mark\" role=\"img\" aria-label=\"AGORA Data Driven\"></div>"
            if marks.get("agora_css") else "<div class=\"mark\">AGORA</div>")
    chrome = ("<div class=\"chrome\">%s"
              "<div class=\"mid\"><div class=\"t\">%s</div><div class=\"s\">%s</div></div>"
              "<div class=\"by\"><div class=\"lbl\">Prepared<br>by</div>%s</div></div>"
              % (crest, _esc(client_name), _esc(deck_title), mark))
    foot = ("<div class=\"foot\"><div class=\"src\">%s</div><div class=\"no\">%02d / %02d</div></div>"
            % (_esc(slide.get("source")), number, total))
    return ("<section class=\"slide %s%s\">%s<div class=\"body\">%s</div>%s</section>"
            % (slide["kind"], " on" if number == 1 else "", chrome, body, foot))


# The navigator. esprima-4.x-safe on purpose (no `?.`, no `??`) -- the repo's JS gate parses every
# inline script with esprima 4, and the deck is validated the same way in _report_localtest.py.
_JS = """
(function(){
  var slides = Array.prototype.slice.call(document.querySelectorAll('.slide'));
  var dotsBox = document.getElementById('dots');
  var i = 0;
  if (!slides.length) { return; }
  slides.forEach(function(s, n){
    var b = document.createElement('button');
    b.className = 'dot' + (n === 0 ? ' on' : '');
    b.type = 'button';
    b.setAttribute('aria-label', 'Go to slide ' + (n + 1));
    b.addEventListener('click', function(){ go(n); });
    dotsBox.appendChild(b);
  });
  var dots = Array.prototype.slice.call(dotsBox.children);
  function go(n){
    if (n < 0) { n = 0; }
    if (n > slides.length - 1) { n = slides.length - 1; }
    slides[i].classList.remove('on');
    dots[i].classList.remove('on');
    i = n;
    slides[i].classList.add('on');
    dots[i].classList.add('on');
  }
  function fit(){
    var k = Math.min(window.innerWidth / %(w)d, (window.innerHeight - 62) / %(h)d);
    document.documentElement.style.setProperty('--k', k);
  }
  window.addEventListener('resize', fit);
  fit();
  document.getElementById('prev').addEventListener('click', function(){ go(i - 1); });
  document.getElementById('next').addEventListener('click', function(){ go(i + 1); });
  document.addEventListener('keydown', function(e){
    var k = e.key;
    if (k === 'ArrowRight' || k === 'PageDown' || k === ' ') { go(i + 1); e.preventDefault(); }
    else if (k === 'ArrowLeft' || k === 'PageUp') { go(i - 1); e.preventDefault(); }
    else if (k === 'Home') { go(0); e.preventDefault(); }
    else if (k === 'End') { go(slides.length - 1); e.preventDefault(); }
    else if (k === 'p' || k === 'P') { window.print(); }
  });
  document.querySelector('.stage').addEventListener('click', function(e){
    if (e.target.closest('.nav')) { return; }
    go(i + 1);
  });
})();
"""


def render_html(client_name, payload, when, title="", brand=None):
    """The full deck HTML for a payload (new shape, legacy shape or junk -- see normalize_payload).

    `when` is the presentation date (ISO), `title` the deck title, `brand` the client's identity kit
    from `brand_kit(ws)` (omit it and the deck wears the AGORA house palette with no crest)."""
    p = normalize_payload(payload)
    kit = brand if isinstance(brand, dict) else {}
    palette = kit.get("palette") if isinstance(kit.get("palette"), dict) else HOUSE_PALETTE
    palette = dict(HOUSE_PALETTE, **palette)
    when_label = date_label(when)
    deck_title = title or "Performance Review"
    slides = list(p["slides"])

    # The cover always exists: a deck with no slides at all still opens on something honest.
    if not slides or slides[0]["kind"] != "cover":
        meta = p["meta"]
        chips = [c for c in ({"label": "Window", "value": meta["period"]} if meta["period"] else None,
                             {"label": "Prepared", "value": when_label} if when_label else None,
                             {"label": "Sources", "value": meta["sources"]} if meta["sources"] else None)
                 if c]
        slides.insert(0, {
            "kind": "cover", "eyebrow": "Client performance review",
            "title": meta["headline"] or when_label or deck_title,
            "subtitle": meta["subhead"] or ("%s, %s" % (client_name, deck_title)),
            "tone": "neutral", "source": "",
            "blocks": [{"type": "chips", "items": chips}] if chips else []})
    if len(slides) == 1:
        slides.append({"kind": "closing", "eyebrow": "", "title": "Nothing to report yet",
                       "subtitle": "This deck was generated before the workspace held any data.",
                       "tone": "neutral", "source": "", "blocks": []})

    total = len(slides)
    body = "\n".join(_slide_html(s, p["facts"], n + 1, total, client_name, deck_title, kit)
                     for n, s in enumerate(slides))
    css = _css(palette, kit.get("crest_css", ""), kit.get("agora_css", ""))
    js = _JS % {"w": STAGE_W, "h": STAGE_H}
    return ("<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            "<meta name=\"robots\" content=\"noindex\">"
            "<title>%s - %s</title><style>%s</style></head><body>"
            "<div class=\"stage\">%s</div>"
            "<div class=\"nav\"><button id=\"prev\" type=\"button\" aria-label=\"Previous slide\">"
            "&#8249;</button><div class=\"dots\" id=\"dots\"></div>"
            "<button id=\"next\" type=\"button\" aria-label=\"Next slide\">&#8250;</button></div>"
            "<script>%s</script></body></html>"
            % (_esc(client_name), _esc(when_label), css, body, js))
