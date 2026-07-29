"""Report maker: the client meeting deck, drafted from the same distilled layer the Assistant reads.

One report = one self-contained HTML deck (scroll-snap slides, zero JS, no external assets) stored
as its own object and listed date-first on the Reports tab. HTML over Google Slides on purpose:
no new infra/APIs/credentials, it opens in one click inside the authed portal, and it matches the
house pattern (the Riverdance media-buying deck is already a scroll-snap HTML deck).

The fixed slide structure (the agreed meeting flow):
  1. Cover                -- the presentation DATE front and center, client + deck title under it.
  2. The Landscape        -- Business Research | Media Buying News | Market Voices (what watched
                             competitors/creators are talking about) -- one slide, three blocks.
  3. What Happened        -- the numbers (stat tiles + a short narrative), with a clearly marked
                             "What's Working" subsection (creative/audience wins).
  4. Why It Happened      -- the drivers: what we did, what the industry did, what we noticed.
  5. What We Should Do    -- the optimization recommendations.
  6. What We Need From You -- asks on the client + anything currently blocked.

Split of labor (mirrors the intel brain): `gather` assembles source material from digest.py +
the workspace (pure), `generate` asks the configured model for the slide payload (JSON contract,
parsed leniently), `draft_payload` is the NO-AI fallback so a default deploy still produces an
honest draft, `revise` applies a team instruction to an existing payload (the Assistant's
edit-report action), and `render_html` turns any payload into the deck. Everything degrades,
nothing raises: (payload, error) out of every AI path.
"""

import datetime
import html
import json
import re

import digest

# The client-facing name for the watched-creators/competitors section of the Landscape slide.
VOICES_LABEL = "Market Voices"

_MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August",
           "September", "October", "November", "December")


def date_label(iso):
    """'2026-07-29' -> 'July 29, 2026' (falls back to the raw string)."""
    try:
        d = datetime.date.fromisoformat((iso or "")[:10])
    except ValueError:
        return iso or ""
    return "%s %d, %d" % (_MONTHS[d.month - 1], d.day, d.year)


# --- 1. Gather: the source pack -------------------------------------------------------------------
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


def _asks(ws):
    """(needed_from_client, blocked) -- the task-slide raw material."""
    needed, blocked = [], []
    for t in ws.get("tasks") or []:
        title = t.get("title") or "(untitled)"
        stage = t.get("stage") or ""
        reason = t.get("hold_reason") or ""
        if stage == "blocked" or t.get("on_hold"):
            blocked.append("%s%s" % (title, (" - " + reason) if reason else ""))
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


def gather(ws, archives, dash_data):
    """The report's source material: text blocks from the distilled layer + the structured bits
    the no-AI fallback needs. Pure -- the caller loads archives/dash_data."""
    intel = ws.get("intel") or {}
    needed, blocked = _asks(ws)
    return {
        "business": _intel_lines(intel.get("business_research")),
        "media": _intel_lines(intel.get("media_buying")),
        "voices": _voices_lines(archives),
        "dashboard": digest.dashboard_sections(dash_data or {}),
        "tasks": digest.tasks_snapshot(ws),
        "comms": digest.comms_snapshot(ws),
        "needed": needed,
        "blocked": blocked,
    }


# --- 2. Generate: model -> slide payload (with a deterministic no-AI draft) -----------------------
_SHAPE = ("{\"landscape\": {\"business\": [{\"title\": \"...\", \"body\": \"...\"}], "
          "\"media\": [{\"title\": \"...\", \"body\": \"...\"}], "
          "\"voices\": [{\"title\": \"...\", \"body\": \"...\"}]}, "
          "\"what_happened\": {\"summary\": \"...\", "
          "\"numbers\": [{\"label\": \"Spend\", \"value\": \"$12,345\", \"note\": \"+8% vs prior\"}], "
          "\"whats_working\": [\"...\"]}, "
          "\"why\": [\"...\"], \"recommendations\": [\"...\"], "
          "\"asks\": {\"needed\": [\"...\"], \"blocked\": [\"...\"]}}")

_GEN_SYSTEM = (
    "You write a marketing agency's client performance-review deck. From the source material "
    "produce JSON ONLY, exactly this shape: " + _SHAPE + " Rules: every number must come from the "
    "source material, never invented, and quote the strongest few (spend, revenue, ROAS, CTR, "
    "purchases) as the `numbers` tiles with a short comparison note when the material gives one. "
    "landscape.business = the most impactful industry/competitor items; landscape.media = the "
    "most impactful media-buying/platform news; landscape.voices = what watched creators and "
    "competitors are talking about (2-4 items each, one tight sentence of body). what_happened."
    "summary = 2-3 sentences on the period in plain client-facing language; whats_working = the "
    "creative/audience/channel wins with their evidence. why = 3-5 bullets connecting the results "
    "to causes (what we changed, what the market did, what the data shows). recommendations = "
    "3-5 concrete next optimizations, imperative voice. asks.needed = what we need FROM the "
    "client; asks.blocked = work stuck and why. Keep bullets under 30 words, no jargon, no em "
    "dashes (use commas or hyphens), and leave a list empty when the sources give nothing -- "
    "never pad.")


def _material_text(client_name, when, inputs):
    lines = ["Client: %s" % client_name, "Report date: %s" % when, ""]

    def block(title, rows):
        if rows:
            lines.append("=== %s ===" % title)
            lines.extend(rows if isinstance(rows, list) else [rows])
            lines.append("")
    block("MARKET INTELLIGENCE: BUSINESS RESEARCH", inputs.get("business"))
    block("MARKET INTELLIGENCE: MEDIA BUYING NEWS", inputs.get("media"))
    block("MARKET VOICES (watched creators and competitors)", inputs.get("voices"))
    for _sid, title, text in inputs.get("dashboard") or []:
        block("DASHBOARD: %s" % title.upper(), text)
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


def _items(v, cap=6):
    """A list of {title, body} out of whatever the model produced (strings tolerated)."""
    out = []
    for it in (v if isinstance(v, list) else [])[:cap]:
        if isinstance(it, dict):
            title = str(it.get("title") or "").strip()
            body = str(it.get("body") or "").strip()
            if title or body:
                out.append({"title": title, "body": body})
        elif isinstance(it, str) and it.strip():
            out.append({"title": "", "body": it.strip()})
    return out


def _strs(v, cap=8):
    out = []
    for it in (v if isinstance(v, list) else [])[:cap]:
        if isinstance(it, str) and it.strip():
            out.append(it.strip())
        elif isinstance(it, dict):
            s = " ".join(str(x) for x in it.values() if x)
            if s.strip():
                out.append(s.strip())
    return out


def normalize_payload(p):
    """Coerce any parsed payload into the canonical shape the renderer trusts."""
    p = p if isinstance(p, dict) else {}
    landscape = p.get("landscape") if isinstance(p.get("landscape"), dict) else {}
    wh = p.get("what_happened") if isinstance(p.get("what_happened"), dict) else {}
    asks = p.get("asks") if isinstance(p.get("asks"), dict) else {}
    numbers = []
    for n in (wh.get("numbers") if isinstance(wh.get("numbers"), list) else [])[:8]:
        if isinstance(n, dict) and (n.get("label") or n.get("value")):
            numbers.append({"label": str(n.get("label") or "").strip(),
                            "value": str(n.get("value") or "").strip(),
                            "note": str(n.get("note") or "").strip()})
    return {
        "landscape": {"business": _items(landscape.get("business")),
                      "media": _items(landscape.get("media")),
                      "voices": _items(landscape.get("voices"))},
        "what_happened": {"summary": str(wh.get("summary") or "").strip(),
                          "numbers": numbers,
                          "whats_working": _strs(wh.get("whats_working"))},
        "why": _strs(p.get("why")),
        "recommendations": _strs(p.get("recommendations")),
        "asks": {"needed": _strs(asks.get("needed")),
                 "blocked": _strs(asks.get("blocked"))},
    }


def draft_payload(inputs):
    """The deterministic no-AI draft: honest source material in the right slots, no invented
    analysis (why/recommendations stay empty rather than fake)."""
    def to_items(rows):
        out = []
        for r in rows or []:
            head, _sep, tail = r.partition(": ")
            out.append({"title": head if tail else "", "body": tail or head})
        return out[:4]
    summary = ""
    numbers = []
    for sid, _title, text in inputs.get("dashboard") or []:
        if sid in ("overview", "kpis"):
            summary = text
            break
    return normalize_payload({
        "landscape": {"business": to_items(inputs.get("business")),
                      "media": to_items(inputs.get("media")),
                      "voices": to_items(inputs.get("voices"))},
        "what_happened": {"summary": summary, "numbers": numbers, "whats_working": []},
        "why": [], "recommendations": [],
        "asks": {"needed": inputs.get("needed") or [], "blocked": inputs.get("blocked") or []},
    })


def generate(client_name, when, inputs, caller):
    """The slide payload for `inputs`. Returns (payload, error): with a working `caller(system,
    user) -> (text, err)` the model writes it; on any failure (or caller None) the deterministic
    draft comes back with the reason -- a deck ALWAYS renders."""
    if caller is not None:
        try:
            raw, err = caller(_GEN_SYSTEM, _material_text(client_name, when, inputs))
        except Exception as e:  # the report path never raises
            raw, err = "", str(e)
        if not err:
            parsed = _parse_json(raw)
            if parsed is not None:
                return normalize_payload(parsed), ""
            err = "the model did not return usable JSON"
    else:
        err = "no AI model configured"
    return draft_payload(inputs), err


_REVISE_SYSTEM = (
    "You edit a marketing agency's client report. You get the report's current JSON payload and "
    "the team's edit instruction. Apply the instruction and return the FULL updated payload as "
    "JSON ONLY, keeping exactly this shape: " + _SHAPE + " Change only what the instruction asks "
    "for; keep everything else verbatim. No em dashes in any text.")


def revise(payload, instruction, caller):
    """Apply a team edit instruction to an existing payload. Returns (new_payload, error);
    on any failure the ORIGINAL payload comes back with the reason (never a broken deck)."""
    if caller is None:
        return payload, "no AI model configured"
    user = "Current report JSON:\n%s\n\nEdit instruction:\n%s" % (
        json.dumps(payload, indent=1), (instruction or "").strip())
    try:
        raw, err = caller(_REVISE_SYSTEM, user)
    except Exception as e:
        raw, err = "", str(e)
    if err:
        return payload, err
    parsed = _parse_json(raw)
    if parsed is None:
        return payload, "the model did not return usable JSON"
    return normalize_payload(parsed), ""


# --- 3. Render: payload -> the self-contained deck ------------------------------------------------
# Zero JS (nothing for the esprima gate, nothing to break), pure CSS scroll-snap, system-safe font
# stack, brand accents only as decoration: values wear ink, labels wear muted gray -- identity and
# meaning never ride on color alone (deltas carry explicit +/- text).
_CSS = """
*{margin:0;padding:0;box-sizing:border-box}
html{scroll-snap-type:y mandatory}
body{font-family:Archivo,Lato,'Segoe UI',system-ui,sans-serif;color:#101410;background:#fff}
.slide{min-height:100vh;scroll-snap-align:start;display:flex;flex-direction:column;
  justify-content:center;padding:8vh 9vw;position:relative}
.slide+.slide{border-top:1px solid #e7ece7}
.eyebrow{font-size:13px;letter-spacing:.18em;text-transform:uppercase;color:#4FA84A;
  font-weight:700;margin-bottom:14px}
h1{font-size:clamp(44px,9vw,110px);line-height:1.02;font-weight:800;letter-spacing:-.02em}
h2{font-size:clamp(30px,4.6vw,54px);font-weight:800;letter-spacing:-.02em;margin-bottom:26px}
h3{font-size:15px;letter-spacing:.14em;text-transform:uppercase;color:#6A6AEA;font-weight:700;
  margin:0 0 10px}
.cover .who{font-size:clamp(20px,2.6vw,30px);font-weight:700;margin-top:26px}
.cover .what{font-size:17px;color:#5b675b;margin-top:6px}
.cover .rule{width:88px;height:6px;background:#4FA84A;border-radius:3px;margin-top:34px}
.cols{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:34px}
.item{margin-bottom:16px}
.item .t{font-weight:700;margin-bottom:3px}
.item .b,li,.summary{font-size:16px;line-height:1.55;color:#333c33}
.lede{font-size:14px;color:#8a948a;margin:-18px 0 24px}
.summary{font-size:18px;max-width:62ch;margin-bottom:30px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:14px;
  margin-bottom:34px}
.tile{border:1px solid #e2e8e2;border-radius:14px;padding:18px 20px;background:#fbfdfb}
.tile .l{font-size:12px;letter-spacing:.1em;text-transform:uppercase;color:#788278;
  font-weight:700}
.tile .v{font-size:clamp(24px,3vw,34px);font-weight:800;letter-spacing:-.01em;margin-top:6px}
.tile .n{font-size:13px;color:#5b675b;margin-top:4px}
.sub{margin-top:8px;border-left:4px solid #4FA84A;padding:6px 0 6px 22px}
.sub h3{color:#4FA84A}
ul,ol{padding-left:22px}
li{margin-bottom:12px;max-width:70ch}
ol li::marker{color:#6A6AEA;font-weight:700}
ul li::marker{color:#4FA84A}
.page{position:absolute;bottom:26px;right:9vw;font-size:12px;color:#aab3aa}
.foot{position:absolute;bottom:26px;left:9vw;font-size:12px;letter-spacing:.14em;
  text-transform:uppercase;color:#aab3aa;font-weight:700}
@media print{.slide{min-height:auto;page-break-after:always;padding:40px 48px}}
"""


def _esc(s):
    return html.escape(str(s or ""), quote=True)


def _item_html(items):
    out = []
    for it in items:
        title = ("<div class=\"t\">%s</div>" % _esc(it["title"])) if it.get("title") else ""
        out.append("<div class=\"item\">%s<div class=\"b\">%s</div></div>"
                   % (title, _esc(it.get("body"))))
    return "\n".join(out)


def _list_html(rows, ordered=False):
    tag = "ol" if ordered else "ul"
    return "<%s>%s</%s>" % (tag, "".join("<li>%s</li>" % _esc(r) for r in rows), tag)


def _slide(body, number, label="AGORA Data Driven"):
    return ("<section class=\"slide\">%s<div class=\"foot\">%s</div>"
            "<div class=\"page\">%02d</div></section>" % (body, _esc(label), number))


def render_html(client_name, payload, when, title=""):
    """The full deck HTML for a normalized `payload`. `when` is the presentation date (ISO),
    `title` the deck title shown under the date. Empty sections skip their slides; the cover and
    the asks slide always render (an empty asks slide honestly says nothing is needed)."""
    p = normalize_payload(payload)
    when_label = date_label(when)
    title = title or "Performance Review"
    n = 1
    slides = [_slide(
        "<div class=\"cover\"><div class=\"eyebrow\">Client performance review</div>"
        "<h1>%s</h1><div class=\"who\">%s</div><div class=\"what\">%s</div>"
        "<div class=\"rule\"></div></div>" % (_esc(when_label), _esc(client_name), _esc(title)),
        n)]

    land = p["landscape"]
    if land["business"] or land["media"] or land["voices"]:
        n += 1
        cols = []
        for key, label, lede in (("business", "Business Research", "Industry and competitor news"),
                                 ("media", "Media Buying News", "Platform and ad-product updates"),
                                 ("voices", VOICES_LABEL,
                                  "What competitors and creators are talking about")):
            if land[key]:
                cols.append("<div><h3>%s</h3><div class=\"lede\">%s</div>%s</div>"
                            % (_esc(label), _esc(lede), _item_html(land[key])))
        slides.append(_slide("<h2>The Landscape</h2><div class=\"cols\">%s</div>"
                             % "\n".join(cols), n))

    wh = p["what_happened"]
    if wh["summary"] or wh["numbers"] or wh["whats_working"]:
        n += 1
        tiles = "".join(
            "<div class=\"tile\"><div class=\"l\">%s</div><div class=\"v\">%s</div>"
            "<div class=\"n\">%s</div></div>"
            % (_esc(t["label"]), _esc(t["value"]), _esc(t["note"])) for t in wh["numbers"])
        body = "<h2>What Happened</h2>"
        if wh["summary"]:
            body += "<p class=\"summary\">%s</p>" % _esc(wh["summary"])
        if tiles:
            body += "<div class=\"tiles\">%s</div>" % tiles
        if wh["whats_working"]:
            body += ("<div class=\"sub\"><h3>What's Working</h3>%s</div>"
                     % _list_html(wh["whats_working"]))
        slides.append(_slide(body, n))

    if p["why"]:
        n += 1
        slides.append(_slide("<h2>Why It Happened</h2>%s" % _list_html(p["why"]), n))

    if p["recommendations"]:
        n += 1
        slides.append(_slide("<h2>What We Should Do</h2>%s"
                             % _list_html(p["recommendations"], ordered=True), n))

    n += 1
    asks = p["asks"]
    ask_body = "<h2>What We Need From You</h2>"
    if asks["needed"] or asks["blocked"]:
        cols = []
        if asks["needed"]:
            cols.append("<div><h3>Waiting on you</h3>%s</div>" % _list_html(asks["needed"]))
        if asks["blocked"]:
            cols.append("<div><h3>Currently blocked</h3>%s</div>" % _list_html(asks["blocked"]))
        ask_body += "<div class=\"cols\">%s</div>" % "\n".join(cols)
    else:
        ask_body += "<p class=\"summary\">Nothing is waiting on you right now.</p>"
    slides.append(_slide(ask_body, n))

    return ("<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            "<meta name=\"robots\" content=\"noindex\">"
            "<title>%s - %s</title><style>%s</style></head><body>%s</body></html>"
            % (_esc(client_name), _esc(when_label), _CSS, "\n".join(slides)))
