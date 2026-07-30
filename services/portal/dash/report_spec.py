"""The report SPINE: the fixed slide order every client's deck follows, per campaign objective.

WHY THIS IS CODE AND NOT PROMPT TEXT
    The deck's structure used to be *requested* in the system prompt, which means it varied by
    client, by model and by run -- the opposite of a standard report. Here the spine is data: nine
    slots, in order, each declaring what it needs and what it is for. The model fills in the
    argument; it does not get a vote on the running order. That makes "every sales client receives
    these slides" a testable claim rather than a hope.

THE NINE SLOTS (identical for every objective; only the wording of 7 differs)
     1. delivery         -- what we are doing, what shipped, what is stuck  (blockers live here,
                            which is why there is no "what we need from you" slide at the end)
     2. landscape        -- industry + platform news + operating conditions + competitor publishing
     3. scorecard        -- campaign to date, against target
     4. funnel           -- where the path leaks
     5. trend            -- week by week
     6. analysis         -- WHY the number moved (two-factor decomposition) + what is working
     7. quality          -- is the conversion genuine (lead quality / revenue quality)
     8. conversion_step  -- the last step we control (form, checkout, booking -- never named after
                            one business model, see TITLES below)
     9. opportunities    -- the headroom and what we will do about it

TITLES
    A slot carries a fixed `eyebrow` (the stable navigational label the client learns to expect) and
    the model writes the `title` as a CLAIM with the number in it. Eight of the nine eyebrows are
    identical across objectives; only `quality` differs ("Lead quality" vs "Revenue quality"),
    because leads and revenue are genuinely different subjects. `conversion_step` is deliberately
    NOT "booking path" or "checkout" -- a sales client might sell a product, a stay, or an hour.

REQUIRED vs CONDITIONAL
    A slot with `required: True` always renders (the deck says plainly when the data is missing).
    A conditional slot renders only when one of its `facts` exists, so one template covers a client
    with an email programme and a client without, and neither gets an empty slide.

FRAMING
    🔴 A slide is NEVER named after a deficiency. "Where we're overpaying" is not a nicer sentence,
    it is a slide that does not exist -- the finding belongs under `opportunities` as the gain it
    unlocks, with the deficiency as evidence underneath. The reallocation fact already computes it
    in that direction ("the same $814, +377 clicks").
"""

OBJECTIVES = ("leadgen", "sales", "awareness")

# The conversion vocabulary per objective: what the headline number IS, and which fact keys carry
# it. `build_facts` reads this so the same code produces a leads-shaped or a revenue-shaped pack.
CONVERSION = {
    "leadgen": {
        "label": "Leads", "unit": "leads",
        "headline": ("leads", "cpl", "cvr"),
        "decompose": ("clicks", "cvr"),          # leads = clicks x conversion rate
        "quality_eyebrow": "Lead quality",
        "quality_asks": "what share of the leads are qualified, reachable and in market",
    },
    "sales": {
        "label": "Revenue", "unit": "purchases",
        "headline": ("revenue", "roas", "aov"),
        "decompose": ("purchases", "aov"),       # revenue = purchases x average order value
        "quality_eyebrow": "Revenue quality",
        "quality_asks": "whether the revenue is real (platform against the source of truth), kept "
                        "(refunds, cancellations) and worth having (new against returning, margin)",
    },
    "awareness": {
        "label": "Reach", "unit": "people",
        "headline": ("reach", "cpm", "frequency"),
        "decompose": ("impressions", "frequency"),
        "quality_eyebrow": "Attention quality",
        "quality_asks": "whether the reach was real attention (view-through, dwell, frequency)",
    },
}


def _slot(key, eyebrow, purpose, facts=(), required=False, guidance=""):
    return {"key": key, "eyebrow": eyebrow, "purpose": purpose, "facts": tuple(facts),
            "required": required, "guidance": guidance}


# The spine. `facts` names the computed keys a slot wants -- the FIRST one that exists anchors the
# slide; the rest ride along when present. Order here IS the deck order.
SPINE = (
    _slot("delivery", "Delivery",
          "what we are doing, what shipped since the last report, and what is stuck",
          facts=("delivery_live", "delivery_next", "delivery_shipped", "delivery_waiting"),
          required=True,
          guidance="Open on the work, not the numbers: it is what the client is paying for and it "
                   "raises blockers while there is still time in the call to clear them. Name the "
                   "single thing waiting on the client in the title when there is one. This slide "
                   "REPLACES a closing asks slide, so nothing here may be left implied."),
    _slot("landscape", "Landscape",
          "what is happening around the client: their market, the ad platforms, the conditions "
          "they trade in, and what their competitors are publishing",
          facts=(), required=True,
          guidance="Four blocks at most, whichever the material supports: industry and competitor "
                   "news, platform and media-buying changes, OPERATING CONDITIONS (weather, "
                   "wildfire, road closures, local events, regulation -- the things that move a "
                   "number without anyone touching the account), and what watched competitors are "
                   "publishing. If a condition overlaps a week in the data, say so plainly: it is "
                   "the difference between a client losing confidence and a client understanding "
                   "their own market."),
    _slot("scorecard", "Campaign to date",
          "the headline numbers for the whole flight, against the targets we agreed",
          facts=("vs_target", "totals"), required=True,
          guidance="Lead with the agreed target when one is set: on track or not, and by how much. "
                   "With no target set, report the numbers and say plainly that no target is on "
                   "file -- never imply a judgement the client never agreed to."),
    _slot("funnel", "The funnel",
          "every step from spend to conversion, and which step is leaking",
          facts=("funnel",), required=True,
          guidance="Name the weakest step and quantify it. One step is always the constraint; the "
                   "slide is worthless if it does not say which."),
    _slot("trend", "Week by week",
          "which way the numbers are moving, on complete weeks only",
          # `weekly_*` is a PREFIX claim: the template dashboard shape names its series after
          # whatever columns the client's export happens to carry (weekly_sessions,
          # weekly_conversions, ...), so a fixed list could never enumerate them.
          facts=("weekly_revenue", "weekly_roas", "weekly_leads", "weekly_cpl", "weekly_aov",
                 "recent_vs_prior", "momentum", "weekly_*"),
          required=True,
          guidance="A partial trailing week is charted but never compared. Point at the turn, and "
                   "at what we changed around it."),
    _slot("analysis", "Analysis",
          "WHY the headline number moved -- volume or value -- and what is working",
          facts=("decomposition", "ads", "segments", "age", "gender", "region", "campaigns"),
          required=True,
          guidance="The decomposition first: the headline is the product of two factors (purchases "
                   "x order value, or clicks x conversion rate) and exactly one of them usually "
                   "moved. Say which, because the two imply completely different next actions. "
                   "Then the winners -- creative, audience, placement, geography -- with the "
                   "evidence beside them."),
    _slot("quality", "Quality",
          "whether the conversions we are counting are genuine",
          facts=("quality", "revenue_truth", "lead_quality"), required=True,
          guidance="This is the slide that keeps the report honest: a falling cost per conversion "
                   "with collapsing quality is the classic way a good-looking account hides a bad "
                   "one. If the data to answer it is not wired up yet, SAY SO on the slide and say "
                   "what wiring it takes -- an explicit gap is worth more than a confident number "
                   "nobody can stand behind."),
    _slot("conversion_step", "The conversion step",
          "the last step before conversion, the one we control",
          facts=("checkout", "landing_page"), required=False,
          guidance="Zoom into the final step the funnel slide only summarised: the form, the cart, "
                   "the booking screen. Quantify the drop-off and attribute it to something we can "
                   "change this month."),
    _slot("opportunities", "Opportunities",
          "the headroom we can still reach, and the work we will do to reach it",
          facts=("reallocation", "bench", "pressure", "region_share", "email"), required=True,
          guidance="One slide per opportunity, up to three, each ending in an `action` block that "
                   "starts with the work WE will do. Frame every one as the gain, never the fault: "
                   "'+377 clicks from the same budget', not 'we are overpaying'. The evidence goes "
                   "underneath. The last one closes with the running order, so the deck needs no "
                   "separate plan slide."),
)

_BY_KEY = {s["key"]: s for s in SPINE}


def expand(slot, facts):
    """The concrete fact keys `slot` claims out of an actual pack, in the slot's own order.

    An entry ending in `*` is a PREFIX claim (`weekly_*`), which is what lets one spine cover a
    dashboard export whose metric columns are only known at runtime."""
    out = []
    for want in slot.get("facts") or ():
        if want.endswith("*"):
            out.extend(sorted(k for k in (facts or {})
                              if k.startswith(want[:-1]) and k not in out))
        elif want in (facts or {}) and want not in out:
            out.append(want)
    return out


def claims(key):
    """True when some slot in the spine would show `key` -- the guard against an orphaned fact."""
    for slot in SPINE:
        for want in slot.get("facts") or ():
            if key == want or (want.endswith("*") and key.startswith(want[:-1])):
                return True
    return False


def slots(objectives=("sales",)):
    """The spine for a client, with per-objective wording resolved.

    `objectives` is ordered by priority (engagement.objectives); the first one supplies the
    conversion vocabulary that names the quality slot. A client running BOTH gets one deck with one
    spine -- the slots are the same questions, the facts underneath carry both answers."""
    objs = [o for o in (objectives or ()) if o in CONVERSION] or ["sales"]
    primary = CONVERSION[objs[0]]
    out = []
    for slot in SPINE:
        row = dict(slot)
        if slot["key"] == "quality":
            row["eyebrow"] = primary["quality_eyebrow"]
            row["purpose"] = "whether the conversions we are counting are genuine: %s" \
                             % primary["quality_asks"]
        out.append(row)
    return out


def conversion(objectives=("sales",)):
    """The primary objective's conversion vocabulary (label, headline metrics, decomposition)."""
    objs = [o for o in (objectives or ()) if o in CONVERSION] or ["sales"]
    return CONVERSION[objs[0]]


def brief(objectives=("sales",)):
    """The spine as instructions for the model: one numbered line per slot, in order.

    This is what replaces "please structure the deck like this" -- the model is told the running
    order as a contract, and which computed facts each slide is expected to reach for."""
    lines = []
    for i, slot in enumerate(slots(objectives), 1):
        need = ("  Facts available for it: %s." % ", ".join(slot["facts"])) if slot["facts"] else ""
        lines.append("%d. %s (slot `%s`%s) -- %s%s\n   %s"
                     % (i, slot["eyebrow"], slot["key"],
                        "" if slot["required"] else ", include only if its facts exist",
                        slot["purpose"], need, slot["guidance"]))
    return "\n".join(lines)
