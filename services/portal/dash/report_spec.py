"""The report SPINE: the fixed slide order every client's deck follows.

WHY THIS IS CODE AND NOT PROMPT TEXT
    The deck's structure used to be *requested* in the system prompt, which means it varied by
    client, by model and by run -- the opposite of a standard report. Here the spine is data: seven
    slots, in order, each declaring what it needs and what it is for. The model fills in the
    argument; it does not get a vote on the running order OR the slide count. That makes "every
    client receives exactly these eight slides" a testable claim rather than a hope --
    `report_ai.enforce_spine` maps whatever the model returned onto these slots, stamps the
    canonical eyebrow, drops extras and backfills a missing slot from the deterministic draft.

THE DECK IS EXACTLY EIGHT SLIDES (2026-08-04 redesign; was nine slots x 1-2 slides)
     0. cover            -- branded cover (not a slot; report_ai owns it)
     1. tasks            -- the Tasks tab's board, verbatim, under its own column names
     2. research         -- what the sources say + why it matters FOR THIS CLIENT
     3. funnel           -- the funnel graph beside bullet insights (one split slide)
     4. what_happened    -- the window's numbers: what actually happened
     5. why_happened     -- WHY the number moved (decomposition + the winners/losers)
     6. next_steps       -- what we will do about it (actions on the current work)
     7. opportunities    -- headroom OUTSIDE the next-steps work: new gains, not fixes

    Campaign-to-date, Week-by-week, Quality and The-conversion-step were retired in the redesign:
    scorecard/trend facts now feed `what_happened`, and quality arguments belong inside
    `why_happened` when the data supports them.

TITLES
    A slot carries a fixed `eyebrow` (the stable navigational label the client learns to expect)
    and the model writes the `title` as a CLAIM with the number in it. Eyebrows are identical for
    every objective -- the objective (leadgen/sales/awareness) only changes which conversion
    vocabulary the FACTS are computed in (see CONVERSION).

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


def _slot(key, eyebrow, purpose, facts=(), guidance=""):
    # Every slot is REQUIRED and owns EXACTLY ONE slide -- that is the whole point of the strict
    # eight-slide standard. A slot whose data is missing renders an honest gap slide, never nothing.
    return {"key": key, "eyebrow": eyebrow, "purpose": purpose, "facts": tuple(facts),
            "required": True, "guidance": guidance}


# The spine. `facts` names the computed keys a slot wants -- an entry ending in `*` is a prefix
# claim (`weekly_*`). Order here IS the deck order; slide N of the deck IS slot N-1 of this tuple.
SPINE = (
    _slot("tasks", "Tasks",
          "the task board exactly as the Tasks tab shows it",
          facts=("tasks_counts", "tasks_board"),
          guidance="ONE slide, the board VERBATIM. Show the `tasks_counts` and `tasks_board` "
                   "facts and nothing else invented: the column names are the Tasks tab's own "
                   "(To do, In progress, Paused, Revision, Completed as the fact spells them) -- "
                   "never rename a column, never editorialize a task, never add or drop one. The "
                   "title may summarize the board's state ('Two launches in progress, one waiting "
                   "on review')."),
    _slot("research", "Research",
          "what is happening around the client, and why each item matters to THEM",
          facts=(),
          guidance="ONE slide of cards from the research material (market intelligence, platform "
                   "news, operating conditions, competitor publishing). Each card: a VERY brief "
                   "summary of what the source is saying, then one line beginning 'Why this "
                   "matters for <client name>:' that ties it to their account specifically. An "
                   "item you cannot tie to the client does not make the slide."),
    _slot("funnel", "The funnel",
          "every step from impression to conversion, drawn, with the insights beside it",
          facts=("funnel", "funnel_notes"),
          guidance="ONE slide, a `split`: the funnel GRAPH on the left ({\"type\": \"funnel\", "
                   "\"fact\": \"funnel\"}), bullet-point insights on the right. The bullets name "
                   "the weakest step and quantify it -- one step is always the constraint; the "
                   "slide is worthless if it does not say which."),
    _slot("what_happened", "What happened",
          "the reporting window's numbers: what actually happened",
          facts=("totals", "vs_target", "recent_vs_prior", "momentum", "weekly_*"),
          guidance="ONE slide. The headline numbers for the WINDOW this report covers, against "
                   "the agreed target when one is set (with no target on file, say so plainly -- "
                   "never imply a judgement the client never agreed to). Lead the title with the "
                   "single most important number."),
    _slot("why_happened", "Why it happened",
          "WHY the headline number moved, with the evidence",
          facts=("decomposition", "ads", "campaigns", "segments", "age", "gender", "region",
                 "pressure", "email"),
          guidance="ONE slide. The decomposition first: the headline is the product of two "
                   "factors (purchases x order value, or clicks x conversion rate) and exactly "
                   "one of them usually moved -- say which, because the two imply completely "
                   "different next actions. Then the strongest evidence: the winning or losing "
                   "creative, audience, placement or geography."),
    _slot("next_steps", "What we'll do",
          "the work we will do about it, imperative and owned by us",
          facts=(),
          guidance="ONE slide of actions on the CURRENT work, each an `action` block: specific, "
                   "imperative, owned by us, dated where possible. These follow from the why "
                   "slide -- fixes and follow-through, not new bets."),
    _slot("opportunities", "Opportunities",
          "the headroom OUTSIDE the next-steps work, and what it is worth",
          facts=("reallocation", "bench", "region_share"),
          guidance="ONE slide. Headroom that is NOT already on the What-we'll-do slide: a "
                   "reallocation, an untapped segment, a thin creative bench. Frame every one as "
                   "the gain, never the fault: '+377 clicks from the same budget', not 'we are "
                   "overpaying'. The evidence goes underneath."),
)

_BY_KEY = {s["key"]: s for s in SPINE}
SLOT_KEYS = tuple(s["key"] for s in SPINE)

# Cover + one slide per slot: the strict deck length every generated report must come out at.
DECK_SLIDES = 1 + len(SPINE)


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


def slot_of(key_or_eyebrow):
    """The slot a slide belongs to, matched by slot key OR eyebrow (case-insensitive), else None.

    The eyebrow fallback is what maps a model deck that ignored the `slot` field but kept the
    verbatim labels -- the common failure -- back onto the spine."""
    want = (key_or_eyebrow or "").strip().lower()
    if not want:
        return None
    for slot in SPINE:
        if want == slot["key"] or want == slot["eyebrow"].lower():
            return slot
    return None


def slots(objectives=("sales",)):
    """The spine, one dict per slot. `objectives` is accepted for signature stability (the wording
    no longer varies by objective -- only the FACTS do, via CONVERSION)."""
    return [dict(s) for s in SPINE]


def conversion(objectives=("sales",)):
    """The primary objective's conversion vocabulary (label, headline metrics, decomposition)."""
    objs = [o for o in (objectives or ()) if o in CONVERSION] or ["sales"]
    return CONVERSION[objs[0]]


def brief(objectives=("sales",)):
    """The spine as instructions for the model: one numbered line per slot, in order.

    This is what replaces "please structure the deck like this" -- the model is told the running
    order as a contract (and it is ALSO enforced in code after parsing, so drift cannot ship)."""
    lines = []
    for i, slot in enumerate(slots(objectives), 1):
        need = ("  Facts available for it: %s." % ", ".join(slot["facts"])) if slot["facts"] else ""
        lines.append("%d. %s (slot `%s`) -- %s%s\n   %s"
                     % (i, slot["eyebrow"], slot["key"], slot["purpose"], need, slot["guidance"]))
    return "\n".join(lines)
