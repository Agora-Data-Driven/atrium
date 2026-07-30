# The Agora dashboard standard (v1 — 2026-07-30)

Every client dashboard in `clients/` now follows **one** of two standard layouts:

| Standard | For | Headline question | Never shows |
|---|---|---|---|
| **[Leads](dash/dashboard-leads.html)** | lead-gen / enquiry / recruitment clients | *how many leads, at what cost, and where do they leak?* | revenue · ROAS · AOV |
| **[Sales](dash/dashboard-sales.html)** | e-commerce / booking / revenue clients | *how much revenue, at what return, and what sells?* | CPL as a headline |

Both are the **same shell** — same chrome, same controls, same section order, same class and id
vocabulary, same helper library. Only the metric spec differs. A client that needs both (MeloYelo,
Honey Tribe) uses the Sales shell and gives Leads its own tab; the tab still follows the Leads
section order.

> **This file is the spec. `dash/dashboard-{leads,sales}.html` are the reference implementations.**
> `check_standard.py` is the machine-checkable conformance gate — run it before any dash deploy.
> Build guidance (why each rule exists) stays in
> [`docs/CLIENT_DASHBOARD_PLAYBOOK.md`](../../docs/CLIENT_DASHBOARD_PLAYBOOK.md); this file is the
> shape that guidance resolves to.

---

## 1. What was already out there — the audit that produced this standard

Eight dashboards, audited 2026-07-30. The standard is not invented; it is the **union of what the
best of them already did**, with the naming unified so a feature built once works everywhere.

| Dashboard | Lines | Kind | Tabs |
|---|---|---|---|
| `client_RHE` | 3,775 | **Leads** (Meta ×3 + ActiveCampaign) | funnel · email · magnet · demo |
| `client_honeytribe` | 3,645 | **Sales** (Shopify + Meta) | sales · funnel · product |
| `client_S7000` | 3,435 | **Leads** (uptime monitor, 3 brands) | uptime · stops · history · perf |
| `client_MeloYelo` | 2,985 | **Sales + Leads** (Unleashed + CRM + Lark + Meta) | sales · riders · inventory · marketing |
| `client_riverdance` | 2,259 | **Sales** (Windsor-live Meta + ActiveCampaign) | ads · email |
| `client_agora` | 1,632 | internal (Upwork demand) | — |
| `client_TCS` | 732 | **Leads** (quiz diagnostics) | — |
| `client_template` | 222 | stub | — |

### 1.1 Features found, and where they came from

Every row is a real feature that existed in at least one dashboard. **"Std"** = promoted into the
standard shell (both Leads and Sales). **"L"/"S"** = standard for that side only. **"opt"** = kept as
an optional extra, never removed, never required.

#### Chrome and honesty

| Feature | Std? | Came from | Notes |
|---|---|---|---|
| Brand lockup + tagline, logo as inlined data-URI | **Std** | honeytribe, MeloYelo, RHE, S7000 | never a CDN fetch |
| "Prepared by / Built by Agora Data Driven" lockup | **Std** | all five modern ones | riverdance had it in the footer only |
| `Updated <n> ago` + `Data through <date>`, red past a threshold | **Std** | honeytribe, MeloYelo, RHE | the single most-missed feature (riverdance/TCS/template had none) |
| **Sync** button — trigger the export job, spinner, cooldown | **Std** | honeytribe, MeloYelo, RHE, S7000 | standard version **degrades**: no `/refresh` route ⇒ re-fetch `/data.json` |
| Per-feed freshness badges (`.srcbadge fresh/old`) | opt | RHE | multi-source clients |
| Ambient freshness chip that jumps to the pipeline panel | opt | S7000 | |
| Dedicated **pipeline-failure panel**, visually unlike the data | opt | S7000 | slate/mono, so "pull broke" ≠ "campaigns died" |
| Non-dismissible **demo-data ribbon** | opt | S7000 | honesty guard while data is synthetic |
| Full-screen button | opt | TCS, agora | |
| Runtime theming (`applyTheme` + `THEMES`) for a multi-brand file | opt | S7000 | |
| Fixed status ramp that never inherits brand hue | **Std** | S7000 | `--ok/--warn/--crit/--stale` |
| Glyph **and** word on every status, never colour alone | **Std** | S7000, MeloYelo | greyscale + CVD safe |

#### Controls

| Feature | Std? | Came from | Notes |
|---|---|---|---|
| Numbered tab bar (`.tab` + `.ix`), sticky | **Std** | honeytribe, MeloYelo, RHE, S7000 | riverdance used glyphs, no numbers |
| Tab state in the **URL hash** | **Std** | honeytribe, MeloYelo, RHE, S7000 | linkable, survives refresh |
| Per-tab badge count on the tab (`.cnt`) | opt | S7000 | |
| **Tuckable** filter bar, choice in `localStorage` | **Std** | honeytribe, MeloYelo, RHE | 130px → 33px |
| **Period** presets + explicit from/to, anchored to latest date **in the data** | **Std** | all | never `today` — lagging feeds render empty |
| **KPI benchmark** — a second, independently-set range, tinted differently end-to-end | **Std** | honeytribe, RHE | drives only the comparison column |
| `rangenote` line under the controls saying which range drives what | **Std** | honeytribe, RHE | |
| **Weekly review** button that *becomes* the two weeks it compares | **Std** | honeytribe | matches the Monday reporting ritual |
| Segmented control `.seg` (+ tiny `.cap` label), one `segWire` | **Std** | RHE, honeytribe, MeloYelo | |
| Time grain **Auto / Day / Week / Month** on every time series | **Std** | RHE, honeytribe | Auto scales with span; explicit choice sticky but guarded |
| **Absolute / Relative** axis toggle | **Std** | honeytribe, MeloYelo, riverdance | relative = index to own peak |
| Debounced free-text filter (~220 ms) | **Std** | honeytribe, RHE | |
| Account/brand toggle as first-class buttons, not a dropdown | opt | RHE, S7000 | multi-ad-account clients |
| Metrics **dropdown** with checkboxes for the hero chart | opt | riverdance | alternative to scorecard toggles |
| "More filters" progressive disclosure | opt | agora | |
| Chart ↔ Data-table view toggle | opt | agora | |
| CSV export of the current slice | opt | agora | |

#### Data display

| Feature | Std? | Came from | Notes |
|---|---|---|---|
| KPI tile rail (`.kpi` `.kl` `.kv` `.kd`) — one big number, label above, support line below | **Std** | all modern | |
| **Scorecards as series toggles** — click a tile to add/remove its line; tile accent *is* the line colour | **Std** | honeytribe, MeloYelo, RHE | never allow an empty chart |
| Hero tile variant for the headline number | opt | RHE, MeloYelo, riverdance | |
| **Verdict block** — colour + glyph + one plain sentence, above the fold | **Std** | S7000 | the five-second read |
| Target **meter** with a pace marker | opt | MeloYelo | needs a target in the payload |
| Period-vs-benchmark **compare card** (`.cmp`), volume = per-week avg, rates = aggregate ratio | **Std** | honeytribe, RHE | green always means *better* |
| Funnel **stage cards** (`.stage`) framed as carry-through, not loss | **Std** | RHE, honeytribe | |
| Funnel bar chart, `sqrt` stage widths | **Std** | RHE, riverdance, MeloYelo | or the tail vanishes |
| **Creative gallery** + lightbox, numbers from the main rows (never its own pull) | **S** + opt on L | honeytribe, RHE, riverdance | |
| Creative-fatigue table (frequency × CTR decay) | **Std** | RHE | |
| Industry **benchmark cards** with a cited source link | opt | riverdance | |
| Value map / scatter (spend vs leads, bubble = spend) | **L** | RHE | |
| Cost-per-lead **ranked** bar | **L** | RHE | |
| Budget-share vs lead-share bars | **L** | RHE | |
| Age × placement **cross-tab heatmap** | **L** | RHE | |
| Donut + clickable legend, cross-filters the tab | **Std** | honeytribe, MeloYelo, RHE | |
| US/NZ **tile map**, `sqrt` scale, click to filter | opt | honeytribe | |
| Month **accordion** with *Show all N* | opt | honeytribe | |
| Uptime **heartbeat strip** (one cell per day) | opt | S7000 | |
| **Runway** bars, hatched when projected not scheduled | opt | S7000, MeloYelo | |
| Collapsible **flag list**, criticals open on load | opt | S7000 | |
| **Change feed** of detected transitions | opt | S7000 | |
| Speed-to-lead by owner, median headline (`2D 12H 10M`) | **L** | MeloYelo | |
| Cohort table (by year/segment taken) | **L** | TCS, RHE | |
| Per-person drill-down (click a lead → their emails) | **L** | TCS | |
| Momentum block: last 4 weeks vs historical average | **Std** | agora | |
| Every column header a sort control, arrow on the active one | **Std** | RHE (declarative), all others per-table | RHE's `data-sort`/`data-k` is the standard |
| In-cell bar so outliers pop | **Std** | honeytribe, RHE | |
| Table capped + `tfoot` saying "Top 10 of 55 — totals cover all 55" | **Std** | honeytribe, RHE | |
| Row click cross-filters the tab, chip with an × to clear | **Std** | honeytribe, MeloYelo, RHE | |
| Responsive tables → stacked cards via `data-l` | **Std** | S7000 | one markup path, two layouts |
| Dark tooltip, brand-coloured title, right-aligned tabular values | **Std** | all modern | |
| **"Reading of the …"** insight strip, recomputed with the filters | **Std** | honeytribe, MeloYelo, RHE, S7000, riverdance | highest-value feature in the estate |
| Wired-looking **empty state** (striped card, says what it waits for) | **Std** | honeytribe, MeloYelo, RHE, S7000 | never a blank space |
| Collapsible **method notes** (`details.meth`) | **Std** | S7000 | reference material, one line until wanted |
| Print stylesheet (panes all open, chrome hidden) | **Std** | S7000 | |
| `prefers-reduced-motion` guard | **Std** | riverdance, S7000 | |

### 1.2 What the audit found broken or inconsistent

Fixed by this standard:

1. **Three different names for the same thing.** `.shead`/`.section-head`, `.csub`/`.ch-sub`,
   `.legend .lg`/`.legend span`, `.tblwrap`/`.scroll-x`/`.tablewrap`, `.ix`/`.ic`,
   `.insight`/`.bm`. A feature built on one dashboard could not be lifted to another.
2. **riverdance, TCS, template and agora had no freshness and no Sync** — the playbook's §4 rule
   ("a dashboard that can't tell you it's stale is worse than none") was unenforced.
3. **riverdance and TCS had no `Auto` grain**, so a 7-day range drew a single dot — the exact bug
   the playbook records as already shipped once.
4. **riverdance had no hash-routed tabs**, so its Email tab was unlinkable.
5. **`client_template` — the file every new client copies — was a 222-line stub** with none of it.
   It taught new clients the wrong shape.
6. **TCS, agora and template had no insight strip**, so they reported without an opinion.
7. `client_riverdance/CLAUDE.md` was a **verbatim copy of the template's**, describing SQL views
   that client does not have.

---

## 2. The standard shell

Identical in both standards. Ids and classes are **contract** — `check_standard.py` asserts them.

```
<header class="top">                      brand lockup · #syncBtn · Updated/#updated · Data through/#thru · Agora lockup
<div class="tabbar">  <div class="tabs">  .tab + .ix, numbered, sticky, hash-routed
<div id="boot" class="loading">           the only thing visible before data lands
<div id="app" hidden>
  <div class="controls">                  ONE per tab (or one global), .tucked persisted
    <button class="ctuck" data-tuck="1">
    .cgroup   Weekly review + .wkpair
    .cgroup   Period      presets + from/to
    .cgroup.bench-group  KPI benchmark presets + from/to      <- tinted differently
    .cgroup   client filters (select / search)
    .rangenote                                                 <- states which range drives what
  <main><div class="wrap">
    section 1  #verdict            the five-second read
    section 2  .kpis + one chart   scorecards are series toggles; .seg grain + .seg axis
    section 3  .cmp × 2            period vs benchmark
    section 4  .stages + funnel    carry-through framing
    section 5  breakdowns          gallery / audience / product / geography
    section 6  table[data-sort]    every th[data-k] sortable, .tfoot cap note
    section 7  "Reading of the …"  .insight strip
<div class="tip"> <div class="toast">
```

### 2.1 Required ids

`logo` · `agora` · `syncBtn` · `syncSpin` · `syncLbl` · `updated` · `thru` · `boot` · `app` · `tip`
· `tabs` · at least one `.controls` carrying a `.ctuck` and a `.rangenote`.

### 2.2 Required class vocabulary

`wrap` `tabbar` `tabs` `tab` `ix` `controls` `ctuck` `cgroup` `clabel` `preset` `bench-group`
`bench-date` `rangenote` `sep` `seg` `cap` `shead` `sub` `spacer` `grid` `card` `csub` `cardhead`
`kpis` `kpi` `kl` `kv` `kd` `cmp` `cmp-head` `cmp-row` `delta` `stages` `stage` `tblwrap` `srt` `ar`
`tfoot` `r` `rank` `bar-cell` `legend` `lg` `sw` `chips` `chip` `insight` `it` `ib` `empty` `note`
`tip` `loading` `err` `toast` `lamp` `verdict`

Anything else is a client extra and is fine. **Do not rename an extra to fit — add the standard
class alongside it.**

### 2.3 Required helper library (names and semantics are contract)

Lifted from `client_RHE` (the most complete implementation) and made source-agnostic.

| Helper | Contract |
|---|---|
| `byId setText setHTML esc el clear` | DOM basics |
| `num money compact compactN pct pctAxis safe` | formatters; `pctAxis` picks decimals from the range |
| `dObj isoOf shiftDays prettyDate monthKey monthLabel mday weekStart daysBetween weeksBetween clampIso` | dates, all UTC |
| `autoGrain grainOf bucketOf bucketLabel bucketTip denseBuckets grainNote` | the ONE grain rule |
| `presetRange(days, maxIso, minIso)` | anchors to the latest date **in the data** |
| `showTip hideTip tipRow tipTitle bindTip` | tooltip |
| `niceMax ensureDefs areaGrad` | chart frame |
| `insightCard(icon, title, body)` | insight strip |
| `kpiTile(spec)` / `cmpCard(...)` / `stageCard(...)` | the three repeated blocks |
| `segWire(id, apply)` | wires every `.seg` |
| `wireSorts() markSort(tblId, st) sortRows(rows, st)` | declarative table sorting |
| `setTab(name) currentTab() wireTabs()` | hash-routed tabs |
| `isTucked() setTuck(on)` | persisted filter tuck |
| `doSync()` | export job → else re-fetch |
| `renderAll()` | the single re-render entry point |

**Inline JS stays esprima-4.x-safe: no `?.`, no `??`.** Gate:
`py -3 tools\_validate_dash_js.py <path>`.

---

## 3. The **Leads** standard

For clients whose product of advertising is an **enquiry**, not an order: `client_RHE`,
`client_TCS`, `client_S7000`, and MeloYelo's Riders & Leads tab.

> **If you find yourself adding revenue, ROAS or AOV to a Leads dashboard, you are on the wrong
> standard.** (This sentence is lifted verbatim from `client_RHE/CLAUDE.md`, where it was learned.)

### 3.1 Tabs

| # | Tab | Answers |
|---|---|---|
| 01 | **Funnel** | are we getting leads, at what cost, and which stage leaks? |
| 02 | **Nurture** | once they are on the list, do they engage? |
| 03 | **Audience** | which segment delivers the cheapest lead? |
| 04 | **Pipeline** | what happens to a lead after it arrives? |

### 3.2 The metric spine — matched by name across all three stages

```
impressions → reach → clicks → link clicks → LEADS → qualified → booked/applied
```

### 3.3 KPI rail (in this order)

1. **Leads** — the headline. Volume.
2. **Cost per lead (CPL)** — the headline. Cost. *Down is green.*
3. **Spend**
4. **Clicks → leads (CTL)** — the conversion that matters most
5. **Click-through rate (CTR)** = link clicks ÷ impressions
6. **CPM** = spend ÷ impressions × 1000
7. **Frequency** — mean of the source's own per-row value, never impressions ÷ summed reach
8. **Speed to lead** — median, formatted `2D 12H 10M` *(where the client tracks first contact)*

Optional, client-dependent: qualified leads, cost per qualified lead, list size, awake share,
open rate, click-to-open rate, unsubscribes, bounces, automation completion, applications.

### 3.4 Required sections

| # | Section | Content |
|---|---|---|
| 1 | Verdict | one sentence: lead volume + CPL against the benchmark |
| 2 | Scorecard rail + one chart | leads / spend / clicks / CPL on one canvas, tiles toggle series |
| 3 | Period vs benchmark | two `.cmp` cards — volume per week, rates as aggregate ratio |
| 4 | Funnel | `.stage` cards *"X% carried through"* + `sqrt` funnel chart + conversion rail |
| 5 | Creative | gallery (numbers from the main rows) + **creative-fatigue** table |
| 6 | Segments | value map (spend vs leads, bubble = spend) · CPL ranked · budget-share vs lead-share · every-segment table · age × placement cross-tab |
| 7 | Nurture | sends/opens/clicks over time · send-time heatmap · broadcasts · automations |
| 8 | Table | campaigns — CPL as an in-cell bar so outliers pop |
| 9 | **Reading of the funnel** | insight strip |

### 3.5 Rules specific to Leads

- **A rate that lands on exactly 100% is a misclassification, not a triumph.** Assert it upstream.
- **A field that is returned but always zero renders `n/a`, never `0`** — `0` reads as "we got
  none", which is a lie.
- **A breakdown that cannot carry leads must hide its lead/CPL cuts** — flag it in the payload
  (`has_leads: false`) and read the flag; do not rely on the split simply being absent.
- **Ranked tables need a volume floor** (≥1% of spend or ≥30 clicks). Without it, a cell with 4
  clicks wins "cheapest lead" and becomes a targeting recommendation built on noise.
- Green always means *better*: for CPL and CPM that is **down**.

---

## 4. The **Sales** standard

For clients whose product of advertising is an **order or booking**: `client_honeytribe`,
`client_riverdance`, `client_template`, and MeloYelo's Sales tab.

### 4.1 Tabs

| # | Tab | Answers |
|---|---|---|
| 01 | **Sales overview** | how much did we sell, to whom, and is it ahead or behind? |
| 02 | **Ads × Store** | what did the ads contribute, and where does the funnel leak? |
| 03 | **Product & Audience** | what sells, when, and to which audience? |

### 4.2 The metric spine

```
impressions → reach → clicks → link clicks → add to cart → ORDERS → revenue
```

### 4.3 KPI rail (in this order)

1. **Revenue** — the headline
2. **Orders**
3. **AOV** — *state the denominator on screen.* Agora's agreed default is revenue ÷ **customers**
   (so first-timer vs returning spend is comparable); revenue ÷ orders ships beside it as
   **Per order**.
4. **Spend**
5. **ROAS** = revenue ÷ spend
6. **Customers** (+ first-time / returning split)
7. **CTR**
8. **CPM**

Optional: gross profit, GP %, units, sessions, conversion rate, frequency, cumulative
revenue-vs-spend gap, FY target attainment.

### 4.4 Required sections

| # | Section | Content |
|---|---|---|
| 1 | Verdict | one sentence: revenue + ROAS against the benchmark |
| 2 | Scorecard rail + one chart | revenue / orders / AOV / spend / ROAS, tiles toggle series |
| 3 | Weekly & month-to-date | two `.cmp` cards, actual vs benchmark average |
| 4 | Where the customers are | tile map (or region bars) + customers/AOV/sales trend |
| 5 | Funnel | `.stage` cards + impression→purchase chart + revenue-vs-spend |
| 6 | Creative | gallery + lightbox, ranked by impressions / orders / revenue / spend |
| 7 | Product | best sellers · category mix donut · category × month · new vs returning |
| 8 | Table | campaigns — spend · impr · reach · link clicks · CTR · CPM · ATC · orders · sales · ROAS |
| 9 | **Reading of the period** | insight strip |

### 4.5 Rules specific to Sales

- **Reach is not additive across days.** Frequency is the mean of the source's own per-row value.
- Derive first-time/returning at **order** level, never per line item.
- **Recompute derived state across the whole merged set**, never incrementally, or a returning
  customer whose first order predates the incremental window is misclassified.
- Attributed ad revenue and store revenue are **different numbers** — label which one a tile shows.
- Route every referrer through one normaliser, or "visitors vs buyers" is not comparable.

---

## 5. Conformance — how "they all follow the template" is checked

`py -3 clients/_standard/check_standard.py` walks every `clients/*/dash/dashboard.html` and
asserts the rules below. It prints one line per dashboard and exits non-zero on any **FAIL**.

| Rule | Asserts |
|---|---|
| `R01 shell-ids` | every id in §2.1 present |
| `R02 brand-lockup` | client brand element **and** the Agora "prepared by" lockup |
| `R03 freshness` | `#updated` + `#thru` present and written by the render path |
| `R04 sync` | `#syncBtn` + a `doSync` that falls back to re-fetching `/data.json` |
| `R05 boot-app` | `#boot` loading state and `#app hidden` |
| `R06 tabs-hash` | tabs numbered with `.ix` and `setTab` writes `location.hash` (skipped for a genuinely single-view dashboard, which must instead carry `data-single-view`) |
| `R07 tuck` | a `.ctuck` and a `localStorage` tuck key |
| `R08 two-ranges` | Period presets **and** a `.bench-group` benchmark, or an explicit `data-no-benchmark` reason |
| `R09 rangenote` | a `.rangenote` explaining which range drives what |
| `R10 anchored-presets` | `presetRange` exists and takes `maxIso`/`minIso` (never `Date.now`) |
| `R11 auto-grain` | `autoGrain` + `grainOf` present, `Auto` offered in a `.seg` |
| `R12 seg-wire` | one `segWire` |
| `R13 sortable` | `wireSorts` + `table[data-sort]` + `th[data-k]` |
| `R14 insights` | a "Reading of the …" section and an `insightCard` |
| `R15 empty-state` | an `.empty` class in the stylesheet |
| `R16 status-not-colour-alone` | every `.lamp`/`.st`/`.chip` status carries a glyph span |
| `R17 tooltip` | `#tip` + `showTip`/`hideTip` |
| `R18 esprima` | delegates to `tools/_validate_dash_js.py` |
| `R19 no-cdn-data` | no `<script src=` other than the sanctioned analytics/font tags |
| `R20 print+motion` | an `@media print` and a `prefers-reduced-motion` block |

Rules R08 and R06 have **documented opt-outs**, because two dashboards genuinely should not have
those controls (see §6). An opt-out is an attribute in the markup with a reason, not a silent gap —
the checker prints it as `WAIVED` and says why.

---

## 6. Sanctioned waivers

As applied 2026-07-30. Every waiver below is an attribute on that dashboard's `<body>`, and
`check_standard.py` prints it on every run.

| Dashboard | Waiver | Why |
|---|---|---|
| `client_S7000` | `data-no-benchmark` | It is an **uptime monitor**, not a performance report. Its question is "is this campaign delivering right now"; a KPI-benchmark average would invite exactly the analytics scope creep its brief forbids. Its Performance tab does carry Period. **It also diverges on R10 deliberately**: every relative window anchors to `PULL.day`, the pull clock, not to the latest date in the data — the data can never be fresher than the pull that produced it, and a viewer with a wrong system clock must not be able to invent an outage or hide one. |
| `client_TCS` | `data-single-view` + `data-no-benchmark` | One diagnostic question (why quiz-lead conversion changed), so a one-tab bar would be chrome rather than navigation. And its comparison is **cohort**-based, not window-based: a lead is judged against the year they took the quiz, because a recent cohort has simply had less time to buy. A second free-floating date range would let a reader compare cohorts of different ages and read a decline that is only age. |
| `client_agora` | `data-single-view` + `data-no-benchmark` | Internal Upwork demand board, not a client deliverable. Its **Comparable / Raw count / % of jobs** control *is* its benchmark mechanism — it chain-links each feed-pipeline era to today's coverage, because the collector over-delivered ~3× for one stretch and under-delivers now. A second date range would invite comparing two windows of different collector coverage and reading a market shift that is really a pipeline artefact. |
| `client_template` | none | It is the worked example — it must be fully conformant or it teaches the wrong shape to every client copied from it. |
| `client_honeytribe` · `client_RHE` · `client_MeloYelo` · `client_riverdance` | none | Fully conformant. |

### What a waiver is not

A waiver is not "this was expensive to retrofit". It is a statement that **the rule does not apply
to this dashboard's question**, and it has to read as one to a reviewer who has never seen the file.
If the honest reason is cost, the rule is still failing — leave it failing and record the work.

---

## 7. Standing up a new client

1. Decide **Leads or Sales** (§3 / §4). If genuinely both, take Sales and give Leads a tab.
2. Copy `dash/dashboard-leads.html` or `dash/dashboard-sales.html` into
   `clients/client_<c>/dash/dashboard.html`.
3. Replace the `:root` **brand tokens only**. Identity is the brand palette; **encoding**
   (`--s1..--s8`) must stay distinguishable — never five near-identical brand hues as series.
4. Fill in `SPEC` at the top of the script: the KPI list, the funnel stages, the tables, the
   insight rules. Adding a metric is one `SPEC` entry plus the matching key in the export job.
5. Delete the sections the client has no data for — **but leave the wired empty state**, so the
   page says what it is waiting for rather than going blank.
6. Run both gates:
   ```powershell
   py -3 tools\_validate_dash_js.py clients\client_<c>\dash\dashboard.html
   py -3 clients\_standard\check_standard.py clients\client_<c>\dash\dashboard.html
   ```

## 8. Changing the standard

The shell and the helper library are **vendored** into every dashboard, the same posture as
`freshness.py` and `platform_sso.py`: **fix them everywhere or nowhere.** A change to §2 means
editing every `clients/*/dash/dashboard.html` in the same commit and re-running
`check_standard.py` across all of them. If that is too expensive for the change you want, the
change belongs in a client extra, not in the standard.
