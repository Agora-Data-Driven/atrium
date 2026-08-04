"""Distilled-insight layer: turn raw workspace data into compact, retrieval-friendly text.

This is the "give the AI the insights, not the CSV" module. Every function here is PURE
(stdlib only, no network, no storage) and returns short titled text sections that read like an
analyst's notes -- the shape both BM25 and embeddings retrieve well, and the shape a model can
quote without wading through thousands of JSON rows. The assistant indexes these as first-class
chunks (assistant_ai.build_chunks) and the report generator builds decks from the same layer
(report_ai.gather), so "what the AI knows" and "what we present" always agree.

Why not hand the model the raw JSON? Two reasons, both measured here before this module existed:
  * Retrieval quality -- a 5,000-row `rows` array is ONE opaque chunk; a keyword query matches it
    on noise ("spend" appears 5,000 times) and an embedding of it is mush. Six focused sections
    ("Campaign performance", "Creative winners and losers", ...) each embed cleanly and rank on
    merit.
  * Context economy -- the packed prompt is capped (assistant_ai.MAX_CONTEXT_CHARS); one raw dump
    would eat the whole budget that should carry 15+ diverse excerpts.

Sections are DERIVED (sums, deltas, rankings) -- never invented -- so they are safe to cache and
cheap to recompute on every index rebuild. The IR name for this move is semantic compression /
feature extraction; the drill-down back to full documents lives in assistant_ai (small-to-big
expansion), not here.
"""

import datetime


# --- tiny formatting helpers ----------------------------------------------------------------------
def _num(v):
    """A float out of anything (None/str/int), 0.0 when it isn't one."""
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _money(v):
    return "$%s" % format(round(_num(v), 2), ",.2f")


def _int_fmt(v):
    return format(int(round(_num(v))), ",d")


def _pct(part, whole):
    return ("%.2f%%" % (100.0 * part / whole)) if whole else "n/a"


def _ratio(a, b, suffix="x"):
    return ("%.2f%s" % (a / b, suffix)) if b else "n/a"


def _delta_pct(now, before):
    """Signed percent change 'now vs before', or '' when there is no baseline."""
    if not before:
        return ""
    change = 100.0 * (now - before) / before
    return "%+.0f%%" % change


# --- dashboard: the Windsor-live per-ad/day shape (riverdance) + the template kpis/daily shape -----
def _roll(rows, key):
    """Aggregate riverdance-shape rows by `key` -> {name: {spend, imps, clicks, lclk, pur, rev}}."""
    out = {}
    for r in rows:
        name = r.get(key) or "(unlabeled)"
        agg = out.setdefault(name, {"spend": 0.0, "imps": 0, "clicks": 0, "lclk": 0,
                                    "pur": 0.0, "rev": 0.0})
        agg["spend"] += _num(r.get("spend"))
        agg["imps"] += int(_num(r.get("imps")))
        agg["clicks"] += int(_num(r.get("clicks")))
        agg["lclk"] += int(_num(r.get("lclk")))
        agg["pur"] += _num(r.get("pur"))
        agg["rev"] += _num(r.get("rev"))
    return out


def _perf_line(name, a):
    """One entity's performance as a single readable line (campaign, ad, week...)."""
    bits = ["spend %s" % _money(a["spend"]), "impressions %s" % _int_fmt(a["imps"]),
            "clicks %s" % _int_fmt(a["clicks"]), "CTR %s" % _pct(a["clicks"], a["imps"])]
    if a["rev"] or a["pur"]:
        bits.append("purchases %s" % _int_fmt(a["pur"]))
        bits.append("revenue %s" % _money(a["rev"]))
        bits.append("ROAS %s" % _ratio(a["rev"], a["spend"]))
    return "%s: %s." % (name, ", ".join(bits))


def _riverdance_sections(data):
    """Insight sections for the Windsor-live shape: {rows, dates, creatives, demographics, ...}."""
    rows = data.get("rows") or []
    if not rows:
        return []
    dates = sorted({r.get("date") for r in rows if r.get("date")})
    sections = []

    # Overview: whole-flight totals + a recent-30-days vs prior-30-days momentum read.
    total = _roll([dict(r, all="all") for r in rows], "all").get("all")
    head = ["Flight %s to %s (%d days)." % (dates[0], dates[-1], len(dates)) if dates else "",
            _perf_line("Totals", total),
            "Link clicks %s, CPC %s, CPM %s." % (
                _int_fmt(total["lclk"]),
                _money(total["spend"] / total["clicks"]) if total["clicks"] else "n/a",
                _money(1000.0 * total["spend"] / total["imps"]) if total["imps"] else "n/a")]
    if len(dates) > 40:
        recent, prior = set(dates[-30:]), set(dates[-60:-30])
        a = _roll([dict(r, w="w") for r in rows if r.get("date") in recent], "w").get("w")
        b = _roll([dict(r, w="w") for r in rows if r.get("date") in prior], "w").get("w")
        if a and b:
            head.append(
                "Momentum, last 30 days vs the 30 before: spend %s (%s), revenue %s (%s), "
                "purchases %s (%s), CTR %s vs %s." % (
                    _money(a["spend"]), _delta_pct(a["spend"], b["spend"]) or "flat",
                    _money(a["rev"]), _delta_pct(a["rev"], b["rev"]) or "flat",
                    _int_fmt(a["pur"]), _delta_pct(a["pur"], b["pur"]) or "flat",
                    _pct(a["clicks"], a["imps"]), _pct(b["clicks"], b["imps"])))
    sections.append(("overview", "Dashboard overview (totals and momentum)",
                     " ".join(s for s in head if s)))

    # Campaigns: one line each, ranked by spend.
    camps = _roll(rows, "camp")
    if len(camps) > 0:
        ranked = sorted(camps.items(), key=lambda kv: -kv[1]["spend"])
        lines = [_perf_line(name, a) for name, a in ranked[:12]]
        sections.append(("campaigns", "Dashboard campaign performance", "\n".join(lines)))

    # Creatives/ads: winners by revenue, plus spenders that aren't returning.
    ads = _roll(rows, "ad")
    if ads:
        ranked = sorted(ads.items(), key=lambda kv: -kv[1]["rev"])
        lines = ["Top ads by revenue:"]
        lines += ["  %s" % _perf_line(name, a) for name, a in ranked[:6] if a["spend"] > 0]
        spenders = sorted(ads.items(), key=lambda kv: -kv[1]["spend"])
        weak = [(n, a) for n, a in spenders[:10]
                if a["spend"] > 0 and a["rev"] / a["spend"] < 1.0][:4]
        if weak:
            lines.append("Spending without matching return (ROAS under 1x):")
            lines += ["  %s" % _perf_line(name, a) for name, a in weak]
        best_ctr = max((kv for kv in ads.items() if kv[1]["imps"] >= 1000),
                       key=lambda kv: kv[1]["clicks"] / kv[1]["imps"], default=None)
        if best_ctr:
            lines.append("Best CTR at meaningful volume: %s" % _perf_line(best_ctr[0], best_ctr[1]))
        sections.append(("creatives", "Dashboard creative performance (winners and losers)",
                         "\n".join(lines)))

    # Weekly trend: compact per-week rollup (Mon-anchored), most recent 10 weeks.
    weeks = {}
    for r in rows:
        try:
            d = datetime.date.fromisoformat(r.get("date") or "")
        except ValueError:
            continue
        wk = (d - datetime.timedelta(days=d.weekday())).isoformat()
        weeks.setdefault(wk, []).append(r)
    if weeks:
        lines = []
        for wk in sorted(weeks)[-10:]:
            agg = _roll([dict(r, w="w") for r in weeks[wk]], "w").get("w")
            lines.append(_perf_line("Week of %s" % wk, agg))
        sections.append(("trend", "Dashboard weekly trend", "\n".join(lines)))

    # Audience: age/gender + region highlights from the demographics breakdowns.
    demo = data.get("demographics") or {}
    lines = []
    if demo.get("age_gender"):
        # The breakdown rows carry age + gender separately; roll by their combined label.
        # (Breakdown pulls have no revenue -- Meta rejects it -- so these lines are delivery-only.)
        combo = _roll([dict(r, seg="%s %s" % (r.get("age") or "?", r.get("gender") or "?"))
                       for r in demo["age_gender"]], "seg")
        top = sorted(combo.items(), key=lambda kv: -kv[1]["spend"])[:6]
        lines.append("Top audience segments by spend:")
        lines += ["  %s" % _perf_line(name, a) for name, a in top]
    region = _roll(demo.get("region") or [], "region")
    if region:
        top = sorted(region.items(), key=lambda kv: -kv[1]["spend"])[:6]
        lines.append("Top regions by spend:")
        lines += ["  %s" % _perf_line(name, a) for name, a in top]
    if lines:
        sections.append(("audience", "Dashboard audience and region breakdown", "\n".join(lines)))

    # Email (ActiveCampaign): latest sends with open/click rates.
    ac = data.get("activecampaign") or {}
    camps = ac.get("campaigns") or []
    if ac.get("enabled") and camps:
        lines = ["Email campaigns (%d total). Most recent sends:" % len(camps)]
        for c in camps[:8]:
            sent = _num(c.get("sent") or c.get("send_amt"))
            opens = _num(c.get("opens") or c.get("uniqueopens"))
            clicks = _num(c.get("clicks") or c.get("uniquelinkclicks"))
            lines.append("  %s (%s): sent %s, open rate %s, click rate %s." % (
                c.get("name") or "(untitled)", (c.get("date") or c.get("sdate") or "")[:10],
                _int_fmt(sent), _pct(opens, sent), _pct(clicks, sent)))
        sections.append(("email", "Email marketing performance (ActiveCampaign)",
                         "\n".join(lines)))

    return sections


def _template_sections(data):
    """Insight sections for the standard client shape: {kpis: {...}, daily: [{date, ...}, ...]}."""
    sections = []
    kpis = data.get("kpis")
    if isinstance(kpis, dict) and kpis:
        lines = ["%s: %s." % (k, v) for k, v in kpis.items()]
        sections.append(("kpis", "Dashboard KPIs", " ".join(lines)))
    daily = data.get("daily")
    if isinstance(daily, list) and daily:
        # Weekly sums of every numeric column -- compact, and the deltas fall out on read.
        weeks = {}
        for row in daily:
            if not isinstance(row, dict):
                continue
            try:
                d = datetime.date.fromisoformat(str(row.get("date") or "")[:10])
            except ValueError:
                continue
            wk = (d - datetime.timedelta(days=d.weekday())).isoformat()
            agg = weeks.setdefault(wk, {})
            for k, v in row.items():
                if k != "date" and isinstance(v, (int, float)):
                    agg[k] = agg.get(k, 0) + v
        lines = []
        for wk in sorted(weeks)[-10:]:
            cells = ", ".join("%s %s" % (k, format(round(v, 2), ","))
                              for k, v in sorted(weeks[wk].items()))
            lines.append("Week of %s: %s." % (wk, cells))
        if lines:
            sections.append(("trend", "Dashboard weekly trend", "\n".join(lines)))
    return sections


def dashboard_sections(data):
    """The dashboard JSON as a list of (id, title, text) insight sections. Handles BOTH shapes:
    the template contract (`kpis`/`daily`) and the Windsor-live per-ad/day export (`rows`/`dates`/
    `creatives`/`demographics`/`activecampaign`, the riverdance pattern). Unknown/empty -> []."""
    if not isinstance(data, dict):
        return []
    if data.get("rows"):
        return _riverdance_sections(data)
    return _template_sections(data)


# --- communications --------------------------------------------------------------------------------
def comms_snapshot(ws):
    """One compact overview of the unified Communications timeline: channel mix + the latest cards.
    (Each card is ALSO indexed individually by assistant_ai; this snapshot answers the broad
    'what's the latest with this client?' in a single hit.)"""
    items = ws.get("communications") or []
    if not items:
        return ""
    by_channel = {}
    for it in items:
        ch = it.get("channel") or "note"
        by_channel[ch] = by_channel.get(ch, 0) + 1
    mix = ", ".join("%d %s" % (n, ch) for ch, n in sorted(by_channel.items(), key=lambda kv: -kv[1]))
    lines = ["%d communications on file (%s). Most recent:" % (len(items), mix)]
    newest = sorted(items, key=lambda it: it.get("date") or "", reverse=True)[:12]
    for it in newest:
        summary = (it.get("summary") or "").strip().replace("\n", " ")
        if len(summary) > 220:
            summary = summary[:220] + "..."
        lines.append("  %s [%s] %s -- %s" % ((it.get("date") or "")[:10], it.get("channel") or "note",
                                             it.get("title") or "(untitled)", summary))
    return "\n".join(lines)


# --- tasks -----------------------------------------------------------------------------------------
def task_text(task):
    """One task as a compact, self-contained description (id included so the assistant can act on
    it by exact id when proposing changes)."""
    if not task:
        return ""
    subs = []
    done = total = 0
    for m in task.get("maintasks") or []:
        for s in m.get("subs") or []:
            total += 1
            if s.get("done"):
                done += 1
        subs.append(m.get("text") or m.get("title") or "")   # maintasks carry `text`, not `title`
    bits = ["Task %s (id %s)" % (task.get("title") or "(untitled)", task.get("id") or "?"),
            "stage %s" % (task.get("stage") or "todo")]
    if task.get("department"):
        bits.append("department %s" % task["department"])
    if task.get("priority"):
        bits.append("priority %s" % task["priority"])
    if task.get("due_date"):
        bits.append("launch date %s" % task["due_date"])
    if total:
        bits.append("progress %d/%d steps done" % (done, total))
    if subs:
        bits.append("phases: %s" % ", ".join(s for s in subs if s))
    if task.get("on_hold") or task.get("hold_reason"):
        bits.append("ON HOLD: %s" % (task.get("hold_reason") or "no reason recorded"))
    open_changes = [c for c in task.get("comments") or []
                    if c.get("kind") == "changes" and not c.get("resolved")]
    if open_changes:
        bits.append("CHANGES REQUESTED: %s" % "; ".join(
            (c.get("body") or "")[:160] for c in open_changes))
    recent = [c for c in task.get("comments") or [] if c.get("kind") != "changes"][-2:]
    if recent:
        bits.append("recent comments: %s" % "; ".join(
            "%s: %s" % (c.get("sender_name") or c.get("sender") or "?",
                        (c.get("body") or "")[:160]) for c in recent))
    return ". ".join(bits) + "."


def tasks_snapshot(ws):
    """The delivery board at a glance: stage counts, blocked work, upcoming launches."""
    tasks = ws.get("tasks") or []
    if not tasks:
        return ""
    by_stage = {}
    for t in tasks:
        by_stage.setdefault(t.get("stage") or "todo", []).append(t)
    lines = ["Delivery board: " + ", ".join(
        "%d %s" % (len(v), k) for k, v in sorted(by_stage.items()))]
    blocked = by_stage.get("blocked") or []
    if blocked:
        # "Parked", not "Blocked": this text is what the Assistant quotes back to the team, and the
        # team's board calls that column Parked since 2026-08-04 (Sentinel WP 1.2). The grouping
        # above is by stage KEY, which is what actually has to be right; this is the wording.
        lines.append("Parked right now: %s." % "; ".join(
            "%s (%s)" % (t.get("title") or "?", t.get("hold_reason") or
                         (t.get("client_note") or "no reason recorded")[:120])
            for t in blocked[:8]))
    upcoming = sorted((t for t in tasks if t.get("due_date")
                       and (t.get("stage") or "") != "completed"),
                      key=lambda t: t["due_date"])[:6]
    if upcoming:
        lines.append("Next launches: %s." % "; ".join(
            "%s on %s" % (t.get("title") or "?", t["due_date"]) for t in upcoming))
    return "\n".join(lines)


# --- the company profile ---------------------------------------------------------------------------
# Who the client actually is. This is the one source that answers questions no metric can ("what do
# they sell?", "what tone do we write in?", "how long have they been trading?"), so it is derived
# ONCE here and consumed by both readers -- the Assistant index and the report deck -- keeping "what
# the AI knows" and "what we present" in agreement (the rule at the top of this module).
_COMPANY_FACT_LABELS = (
    ("one_liner", "What they do"), ("industry", "Industry"), ("founded", "Founded"),
    ("hq", "Based in"), ("size", "Size"), ("customers", "Who they serve"), ("website", "Website"),
)
_COMPANY_BRAND_LABELS = (
    ("tagline", "Tagline"), ("voice", "Brand voice"), ("tone", "Tone"),
    ("personality", "Personality"), ("colors", "Colours"), ("fonts", "Fonts"),
    ("dos", "Always"), ("donts", "Never"), ("assets_url", "Brand assets"),
)


def _company_labelled(block, labels):
    return ["  %s: %s" % (label, str(block.get(key) or "").strip().replace("\n", " "))
            for key, label in labels if str(block.get(key) or "").strip()]


def company_sections(ws, company=None):
    """The company profile as titled (id, title, text) sections -- the same tuple shape
    `dashboard_sections` returns, so the Assistant indexes each part as its own retrievable chunk.

    Four parts, each emitted only when it holds something: the facts, the brand guide, each story
    section under its own heading, and the product catalogue. Splitting them matters -- "what's
    their brand voice?" should retrieve the brand block, not a wall of company history.

    `company` is an already-normalized profile (workspace.company_profile) when the caller has one;
    otherwise the raw ws["company"] dict is read defensively so this stays pure and import-free."""
    comp = company if isinstance(company, dict) else (ws or {}).get("company") or {}
    profile = comp.get("profile") if isinstance(comp.get("profile"), dict) else {}
    brand = comp.get("brand") if isinstance(comp.get("brand"), dict) else {}
    sections = [s for s in (comp.get("sections") or []) if isinstance(s, dict)]
    products = [p for p in (comp.get("products") or []) if isinstance(p, dict)]
    name = (ws or {}).get("display_name") or "the client"
    out = []

    facts = _company_labelled(profile, _COMPANY_FACT_LABELS)
    if facts:
        out.append(("facts", "Company profile: %s at a glance" % name,
                    "\n".join(["%s, the essentials:" % name] + facts)))

    brand_lines = _company_labelled(brand, _COMPANY_BRAND_LABELS)
    if brand_lines:
        out.append(("brand", "Brand guide: %s (voice, tone, look)" % name,
                    "\n".join(["How %s presents itself:" % name] + brand_lines)))

    for i, s in enumerate(sections):
        body = (s.get("body") or "").strip()
        if not body:
            continue
        heading = (s.get("heading") or "About").strip()
        out.append(("section:%s" % (s.get("id") or i),
                    "About %s: %s" % (name, heading), "%s -- %s\n%s" % (name, heading, body)))

    rows = []
    for p in products:
        pname = (p.get("name") or "").strip()
        if not pname:
            continue
        bits = [b for b in (
            (p.get("summary") or "").strip().replace("\n", " "),
            ("price %s" % p["price"].strip()) if (p.get("price") or "").strip() else "",
            ("for %s" % p["audience"].strip()) if (p.get("audience") or "").strip() else "",
            ("status %s" % p["status"].strip()) if (p.get("status") or "").strip() else "",
            (p.get("url") or "").strip(),
        ) if b]
        rows.append("  %s -- %s" % (pname, "; ".join(bits) if bits else "no description recorded"))
    if rows:
        out.append(("products", "What %s sells (products and services)" % name,
                    "\n".join(["%s's product and service catalogue:" % name] + rows)))

    # The content-gap snapshot (the Company tab's team-only analysis): indexed so "what content
    # gaps did we find for <client>?" retrieves the actual findings. One chunk -- the items are a
    # single reading, not independent facts.
    gaps = comp.get("content_gaps") if isinstance(comp.get("content_gaps"), dict) else {}
    gap_items = [g for g in (gaps.get("items") or []) if isinstance(g, dict)
                 and (g.get("topic") or "").strip()]
    if gap_items:
        lines = ["Content-gap analysis for %s (run %s): topics competitors cover that %s has not "
                 "published on." % (name, (gaps.get("date") or "")[:10] or "undated", name)]
        summary = (gaps.get("summary") or "").strip()
        if summary:
            lines.append(summary)
        for g in gap_items:
            bits = [b for b in ((g.get("why") or "").strip(), (g.get("angle") or "").strip(),
                    ("covered by %s" % g["inspired_by"].strip())
                    if (g.get("inspired_by") or "").strip() else "") if b]
            lines.append("  %s -- %s" % (g["topic"].strip(), "; ".join(bits) or "no detail"))
        out.append(("content_gaps", "Content gaps: what %s has not covered yet" % name,
                    "\n".join(lines)))
    return out


def company_brief(ws, company=None):
    """The whole company profile as one block of text (the report deck's client context).

    The Assistant indexes `company_sections` separately for retrieval; a deck writer wants the lot
    at once, so it gets the same derived material joined. The content-gap chunk is EXCLUDED: it is
    the team's internal analysis (it names competitor sources from the watchlist), and a deck is a
    client-facing document."""
    return "\n\n".join(text for sid, _title, text in company_sections(ws, company)
                       if sid != "content_gaps")


# --- market intelligence ---------------------------------------------------------------------------
def intel_digest(ws):
    """The freshest briefing items from both sections in one hit -- the 'what's happening out
    there?' summary chunk. (Every entry stays individually indexed too.)"""
    intel = ws.get("intel") or {}
    lines = []
    for section, label in (("business_research", "Business research"),
                           ("media_buying", "Media buying news")):
        entries = sorted(intel.get(section) or [], key=lambda e: e.get("date") or "",
                         reverse=True)[:8]
        if not entries:
            continue
        lines.append("%s, most recent items:" % label)
        for e in entries:
            body = (e.get("body") or "").strip().replace("\n", " ")
            if len(body) > 180:
                body = body[:180] + "..."
            lines.append("  %s %s -- %s" % ((e.get("date") or "")[:10],
                                            e.get("title") or "(untitled)", body))
    return "\n".join(lines)
