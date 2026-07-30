r"""Conformance gate for the Agora dashboard standard (clients/_standard/STANDARD.md).

WHY THIS EXISTS
    "Every dashboard follows the template" is a claim until something checks it. Eight client
    dashboards drifted into three vocabularies for the same component, and four of them silently
    lacked the freshness/Sync honesty guards the playbook has required since Honey Tribe. A
    reviewer cannot hold that in their head; a script can.

WHAT IT IS NOT
    It is not a linter for taste, and it does not diff markup. Each rule asserts that a
    dashboard CARRIES a required capability -- an id, a class, a helper, a section. Client extras
    are invisible to it by design: the standard is a floor, never a ceiling.

WAIVERS
    Two dashboards genuinely should not have some controls (an uptime monitor has no business
    growing a KPI-benchmark average). A waiver is an ATTRIBUTE IN THE MARKUP with a reason --
    never a silent gap, and never a list of exceptions hidden in this file:

        <body data-no-benchmark="uptime monitor: a benchmark average invites analytics creep">
        <body data-single-view="one diagnostic question; a one-tab bar is chrome, not navigation">

    A waived rule prints WAIVED plus the stated reason, so it stays visible in every run.

USAGE
    py -3 clients/_standard/check_standard.py                 # every clients/*/dash/dashboard.html
    py -3 clients/_standard/check_standard.py <path> [...]    # just these
    py -3 clients/_standard/check_standard.py --verbose       # list every rule, not just failures

EXIT CODES
    0  every dashboard conformant (waivers count as conformant)
    1  at least one FAIL
"""

import glob
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CLIENTS = os.path.dirname(HERE)
REPO = os.path.dirname(CLIENTS)

# ---------------------------------------------------------------------------------------------
# STANDARD.md §2.1 -- the chrome ids. These are contract: a feature built against #syncBtn on
# one dashboard has to find #syncBtn on every other one.
SHELL_IDS = ("logo", "agora", "syncBtn", "updated", "thru", "boot", "app", "tip")

# STANDARD.md §2.2 -- the class vocabulary. Checked as a set, because a dashboard legitimately
# may not have (say) a funnel; what matters is that when it does, it calls it .stage.
CORE_CLASSES = (
    "wrap", "tabbar", "tab", "controls", "ctuck", "cgroup", "clabel", "preset", "rangenote",
    "seg", "shead", "card", "csub", "kpis", "kpi", "kl", "kv", "kd", "tblwrap", "srt", "tfoot",
    "insight", "empty", "note", "tip", "loading",
)


def rule(rid, title, ok, detail="", waived_reason=None):
    return {"id": rid, "title": title, "ok": ok, "detail": detail, "waived": waived_reason}


def check(path):
    with open(path, "r", encoding="utf-8") as fh:
        html = fh.read()
    low = html.lower()

    # Waivers are read from the <body> tag so they sit next to the thing they excuse.
    body = re.search(r"<body\b([^>]*)>", html, re.I)
    body_attrs = body.group(1) if body else ""

    def waiver(name):
        m = re.search(r'data-%s\s*=\s*"([^"]*)"' % name, body_attrs, re.I)
        if m:
            return m.group(1) or "(no reason given)"
        return None

    single_view = waiver("single-view")
    no_benchmark = waiver("no-benchmark")

    out = []

    # ---- R01 shell ids -----------------------------------------------------------------------
    missing = [i for i in SHELL_IDS if ('id="%s"' % i) not in html]
    out.append(rule("R01", "shell-ids", not missing,
                    "missing: " + ", ".join(missing) if missing else "all present"))

    # ---- R02 both lockups: the client's brand AND "prepared by Agora" -----------------------
    has_agency = 'class="agency"' in html or "prepared by" in low or "built by" in low
    has_brand = 'class="brand"' in html or 'id="logo"' in html
    out.append(rule("R02", "brand-lockup", has_agency and has_brand,
                    "client brand + Agora attribution" if (has_agency and has_brand)
                    else "missing the Agora attribution lockup" if has_brand
                    else "missing the client brand lockup"))

    # ---- R03 freshness: generated-at AND covers-through, and something writes them ----------
    # The estate writes these several equivalent ways (setText / setHTML / byId().textContent),
    # so the rule asks whether the STANDARD IDS are written, not which helper does it.
    writes_fresh = ("renderFreshness" in html
                    or re.search(r'(setText|setHTML|getElementById|byId|el)\(\s*"(updated|thru)"', html) is not None
                    or re.search(r'"(updated|thru)"\s*\)\s*\.\s*(textContent|innerHTML)', html) is not None)
    has_stale = "stale" in low
    out.append(rule("R03", "freshness", writes_fresh and has_stale,
                    "#updated + #thru are written, with a stale treatment"
                    if (writes_fresh and has_stale) else
                    "nothing writes #updated / #thru (a non-standard id does not count -- a "
                    "shared feature has to be able to find them)" if has_stale
                    else "no stale treatment past a lag threshold"))

    # ---- R04 sync, degrading gracefully when there is no /refresh route ---------------------
    # The playbook's rule is "trigger the export job IF IT CAN, else re-fetch the payload".
    # What makes that true in code is that the /refresh call is best-effort: a .catch (or an
    # ok:false branch) inside doSync, followed by a data re-fetch regardless.
    has_btn = 'id="syncBtn"' in html
    m = re.search(r"function doSync\s*\(", html)
    body = html[m.end(): m.end() + 2200] if m else ""
    degrades = bool(body) and ("catch(" in body or "ok:false" in body.replace(" ", "")) \
               and re.search(r"fetchData|loadData|load\(|reload|data\.json|api/|refreshStats", body) is not None
    out.append(rule("R04", "sync", has_btn and degrades,
                    "sync triggers the job best-effort and re-fetches either way"
                    if (has_btn and degrades) else
                    "#syncBtn exists but its handler does not degrade to a plain re-fetch"
                    if has_btn else "no #syncBtn control"))

    # ---- R05 boot / app swap ---------------------------------------------------------------
    boot_ok = 'id="boot"' in html and 'id="app"' in html and re.search(r'id="app"[^>]*hidden', html) is not None
    out.append(rule("R05", "boot-app", boot_ok,
                    "#boot loading state and a hidden #app" if boot_ok
                    else "#app must start hidden behind a #boot loading state"))

    # ---- R06 numbered, hash-routed tabs (waivable for a genuine single view) ----------------
    if single_view:
        out.append(rule("R06", "tabs-hash", True, "single view", single_view))
    else:
        numbered = 'class="ix"' in html
        hashed = "location.hash" in html or "history.replaceState" in html
        out.append(rule("R06", "tabs-hash", numbered and hashed,
                        "numbered tabs, state in the URL hash" if (numbered and hashed)
                        else "tabs are not numbered (.ix)" if hashed
                        else "tab state is not in the URL hash"))

    # ---- R07 tuckable filter bar, persisted -------------------------------------------------
    tuck_ok = "ctuck" in html and "localStorage" in html
    out.append(rule("R07", "tuck", tuck_ok,
                    "collapsible filter bar, choice persisted" if tuck_ok
                    else "a .ctuck exists but the choice is not persisted" if "ctuck" in html
                    else "no collapsible filter bar"))

    # ---- R08 two independent date ranges (waivable) -----------------------------------------
    if no_benchmark:
        # A waiver excuses the BENCHMARK, not the period. Report which period control was found so
        # the waiver stays honest, but do not re-fail the rule it waives.
        has_period = re.search(r'data-p\w*=|data-days=|id="fDate"|id="from"', html) is not None
        out.append(rule("R08", "two-ranges", True,
                        "period control present, benchmark waived" if has_period
                        else "benchmark waived; no preset-style period control found either",
                        no_benchmark))
    else:
        has_period = re.search(r'data-p\w*=|data-days=', html) is not None
        has_bench = "bench-group" in html or re.search(r'data-b\w*=', html) is not None
        out.append(rule("R08", "two-ranges", has_period and has_bench,
                        "period + KPI benchmark, independently set" if (has_period and has_bench)
                        else "no KPI benchmark range (add one, or waive with data-no-benchmark)"
                        if has_period else "no period control"))

    # ---- R09 the line that says which range drives what -------------------------------------
    has_note = "rangenote" in html or "range-label" in html or 'class="gnote"' in html
    out.append(rule("R09", "rangenote", has_note,
                    "a summary line states what the ranges drive" if has_note
                    else "no .rangenote under the controls"))

    # ---- R10 presets anchored to the latest date IN THE DATA, not today ---------------------
    anchored = "presetRange" in html and re.search(r"presetRange\s*\([^)]*(maxIso|ALL_MAX|max)", html) is not None
    out.append(rule("R10", "anchored-presets", anchored,
                    "presets resolve against the newest date in the data" if anchored
                    else "presets must anchor to the data's latest date, never to today"))

    # ---- R11 Auto/Day/Week/Month on every time series ---------------------------------------
    # Any data-* attribute offering "auto" counts -- the estate spells the attribute several ways
    # (data-v / data-gran / data-kgrain / data-pg); what the rule cares about is that Auto is
    # OFFERED and that a span-aware resolver exists behind it.
    grain_ok = ("autoGrain" in html or "function grainOf" in html) \
               and re.search(r'data-[\w-]+="auto"', html) is not None
    out.append(rule("R11", "auto-grain", grain_ok,
                    "Auto/Day/Week/Month with a span-aware Auto" if grain_ok
                    else "Auto grain missing: a 7-day range must not render one weekly dot"))

    # ---- R12 one segWire for every segmented control ----------------------------------------
    seg_ok = "function segWire" in html or "segWire(" in html
    out.append(rule("R12", "seg-wire", seg_ok,
                    "one helper wires every .seg" if seg_ok else "no segWire helper"))

    # ---- R13 declarative table sorting -----------------------------------------------------
    sort_ok = "wireSorts" in html and "data-sort=" in html and "data-k=" in html
    legacy = re.search(r'class="[^"]*\bsrt\b|class="sortable"|data-cs=|data-ss=|data-ps=|data-sb=|data-cb=', html) is not None
    out.append(rule("R13", "sortable", sort_ok or legacy,
                    "declarative: table[data-sort] + th[data-k]" if sort_ok
                    else "sortable, but per-table rather than declarative" if legacy
                    else "table headers are not sort controls"))

    # ---- R14 the insight strip -------------------------------------------------------------
    has_reading = re.search(r"Reading of the|Key takeaways", html, re.I) is not None
    has_card = "insightCard" in html or 'class="insight' in html
    out.append(rule("R14", "insights", has_reading and has_card,
                    "a 'Reading of the ...' strip that recomputes with the filters"
                    if (has_reading and has_card)
                    else "insight cards exist but no 'Reading of the ...' section" if has_card
                    else "no insight strip -- the dashboard reports without an opinion"))

    # ---- R15 wired-looking empty states ----------------------------------------------------
    empty_ok = ".empty{" in html.replace(" ", "") or ".empty {" in html or "emptyState(" in html
    out.append(rule("R15", "empty-state", empty_ok,
                    "empty panels say what they are waiting for" if empty_ok
                    else "no .empty treatment -- an unwired panel will render blank"))

    # ---- R16 status never encoded by colour alone -------------------------------------------
    status_markup = re.search(r'class="(lamp|st|chip|badge)\b', html) is not None
    glyphed = re.search(r'class="(b|ic)"|function lamp\(|aria-hidden="true"', html) is not None
    out.append(rule("R16", "status-not-colour-alone", (not status_markup) or glyphed,
                    "status pills carry a glyph and a word" if glyphed
                    else "no status pills" if not status_markup
                    else "a status is encoded by colour alone"))

    # ---- R17 tooltip -----------------------------------------------------------------------
    tip_ok = 'id="tip"' in html and ("showTip" in html or "hideTip" in html or "tip.style.opacity" in html)
    out.append(rule("R17", "tooltip", tip_ok,
                    "a shared #tip with show/hide" if tip_ok else "no shared tooltip"))

    # ---- R18 no external script carrying data ----------------------------------------------
    # Analytics and font tags are sanctioned; anything else pulling JS breaks the
    # self-contained deploy and would not be parsed by the esprima gate.
    srcs = re.findall(r'<script\b[^>]*\bsrc\s*=\s*"([^"]+)"', html, re.I)
    bad = [s for s in srcs if "googletagmanager.com" not in s and "google-analytics.com" not in s]
    out.append(rule("R18", "no-cdn-js", not bad,
                    "self-contained" if not bad else "external script(s): " + ", ".join(bad)))

    # ---- R19 print + reduced motion --------------------------------------------------------
    printable = "@media print" in low
    motion = "prefers-reduced-motion" in low
    out.append(rule("R19", "print+motion", printable and motion,
                    "prints, and respects reduced motion" if (printable and motion)
                    else "no @media print" if motion else "no prefers-reduced-motion guard"
                    if printable else "neither a print stylesheet nor a motion guard"))

    # ---- R20 the class vocabulary -----------------------------------------------------------
    absent = [c for c in CORE_CLASSES if ("." + c) not in html and ('"' + c) not in html
              and (" " + c + '"') not in html and (" " + c + " ") not in html]
    out.append(rule("R20", "class-vocabulary", not absent,
                    "uses the standard names" if not absent
                    else "not using the standard name for: " + ", ".join(absent)))

    # ---- R21 every id the standard accessors write to actually exists ------------------------
    # A byId("typo") is a SILENT no-op: the panel simply never fills in, and nothing errors. This
    # is the one class of bug neither gate would otherwise catch -- the esprima gate only proves
    # the script parses, not that it addresses anything real.
    ids = set(re.findall(r'id="([^"]+)"', html))
    used = set()
    # The trailing group is deliberate: a COMPUTED id -- byId("tab-" + name) -- is not a literal
    # this rule can resolve, so it is skipped rather than reported as dangling.
    accessors = (
        r'byId\(\s*"([^"]+)"\s*(.)', r'setText\(\s*"([^"]+)"\s*(.)', r'setHTML\(\s*"([^"]+)"\s*(.)',
        r'show\(\s*"([^"]+)"\s*(.)', r'segWire\(\s*"([^"]+)"\s*(.)', r'markSort\(\s*"([^"]+)"\s*(.)',
        r'wireChips\(\s*"([^"]+)"\s*(.)',
        r'draw(?:Series|Funnel|Ranked|Scatter|Heat)\(\s*"([^"]+)"\s*(.)',
        r'drawDonut\(\s*"([^"]+)"\s*,\s*"([^"]+)"\s*(.)',
    )
    for pat in accessors:
        for m in re.finditer(pat, html):
            groups = list(m.groups())
            if groups[-1] == "+":
                continue  # computed id, not resolvable here
            for g in groups[:-1]:
                if g:
                    used.add(g)
    # Ids the chart primitives derive at runtime (e.g. "<svgId>Note"), never in the markup.
    runtime = set()
    for i in ids:
        runtime.add(i + "Note")
    dangling = sorted(u for u in used if u not in ids and u not in runtime)
    out.append(rule("R21", "ids-resolve", not dangling,
                    "every id the script writes to exists in the markup" if not dangling
                    else "writes to ids that do not exist: " + ", ".join(dangling)))

    return out


def main(argv):
    verbose = "--verbose" in argv
    args = [a for a in argv if not a.startswith("--")]
    files = [os.path.abspath(a) for a in args] if args else sorted(
        glob.glob(os.path.join(CLIENTS, "*", "dash", "dashboard.html")))
    if not files:
        sys.stderr.write("[ERROR] no dashboards found\n")
        return 1

    total_fail = 0
    for path in files:
        rel = os.path.relpath(path, REPO)
        rules = check(path)
        waived = [r for r in rules if r["waived"]]
        # A WAIVED rule is satisfied by its stated reason -- it is never also a failure. Counting it
        # as both is how a dashboard reported 20/21 with no visible FAIL line.
        fails = [r for r in rules if not r["ok"] and not r["waived"]]
        total_fail += len(fails)
        status = "PASS" if not fails else "FAIL"
        print("%-52s %s  (%d/%d rules, %d waived)"
              % (rel, status, len(rules) - len(fails), len(rules), len(waived)))
        for r in rules:
            if r["waived"]:
                print("    WAIVED %s %-24s %s" % (r["id"], r["title"], r["waived"]))
            elif not r["ok"]:
                print("    FAIL   %s %-24s %s" % (r["id"], r["title"], r["detail"]))
            elif verbose:
                print("    ok     %s %-24s %s" % (r["id"], r["title"], r["detail"]))
    print("")
    if total_fail:
        sys.stderr.write("[ERROR] %d rule failure(s) across %d dashboard(s)\n" % (total_fail, len(files)))
        return 1
    print("[OK] every dashboard conforms to clients/_standard/STANDARD.md")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
