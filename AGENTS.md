# AGENTS.md — Agora Data Driven (canonical agent manual)

This is the canonical manual — the single source of truth for fixed facts, the data contract, the
deploy procedure, and the guardrails. The root pointer chain is now `CLAUDE.md` → `AGENTS.md`: the
root `CLAUDE.md` (the file Claude Code auto-loads) is exactly one line, `@AGENTS.md`, which pulls
this file in; the pointer at `.claude/CLAUDE.md` also defers here. If any pointer ever disagrees
with this file, **this file wins — update the pointers so they agree again.**

Per-area `CLAUDE.md` files (in `services/portal/dash/`, `clients/client_template/`, `services/ingest/`,
`tools/`, and the per-client `clients/*/CLAUDE.md`) give Claude local context for a subtree and
**defer to this AGENTS.md** for the rules — so every developer's Claude follows the same contract
without re-reading the whole repo. (Those files still carry their old "defer to the root CLAUDE.md"
wording; the root CLAUDE.md now resolves here, so the chain still holds.)

## Overview

Agora Data Driven is a marketing agency that self-hosts password-gated client marketing dashboards
on Google Cloud Platform, fronted by a client portal that is growing into a full CRM.

- **One repeatable pattern, many clients.** Every client is fully derived from a short key `<c>`
  (see the derivation rule below). One GCP project, one region, one shared Artifact Registry repo.
- **`template` is the worked example.** `clients/client_template/` is the canonical pattern every
  new client copies — three SQL views, an export job, and a dashboard web service.
- **The portal/CRM front-door** (`services/portal/`, served at `portal.agoradatadriven.com`) is a
  reverse proxy + single login over all dashboards, with a registry stored as one private JSON in
  GCS. It is designed to grow into a CRM (see the `# CRM:` markers in `services/portal/dash/main.py`).
  **Agora Atrium** — the co-branded client workspace — is built into this same `platform-dash`
  service (see the Agora Atrium section below).
- **Windsor.ai is the only data source.** Connector loaders in `services/ingest/` land
  source data into the shared `raw_windsor` BigQuery dataset; per-client SQL views read from there.

## Fixed facts (use literally — never invent alternatives)

| Fact | Value |
|------|-------|
| GCP project | `agora-data-driven` |
| Region | `asia-southeast1` (Singapore) — **everything lives here, one region, never another** |
| Artifact Registry repo | `agora` |
| Shared raw dataset | `raw_windsor` (the only raw layer; written by Windsor connectors) |
| Portal host | `portal.agoradatadriven.com` |
| Client dashboards | `<c>.agoradatadriven.com` |
| SSO cookie scope | `.agoradatadriven.com` (leading dot) |
| Local dev | Windows + PowerShell; repo venv python at `.\.venv\Scripts\python.exe` |

`PROJECT_NUMBER` is **never hardcoded** — resolve it at runtime:
`gcloud projects describe agora-data-driven --format='value(projectNumber)'`.

**Per-client derivation rule** (derive, never re-type) for a key `<c>`: dataset `client_<c>`,
bucket `agora-data-driven-<c>-dash`, export job `<c>-export`, web service `<c>-dash`, job SA
`<c>-dash-job@agora-data-driven.iam.gserviceaccount.com`, web SA `<c>-dash-web@…`, password secret
`<c>-dash-password`, session secret `<c>-dash-session-key`, subdomain `<c>.agoradatadriven.com`,
data object `<c>.json` + freshness sidecar `_freshness.json` in the client's bucket.

## Repo layout

```
ROOT/
├── services/                — every deployable Cloud Run service / job
│   ├── portal/              — portal/CRM front-door + Agora Atrium (Cloud Run service `platform-dash`)
│   │   ├── dash/            — the Flask app (main.py, workspace.py, store.py, templates/, …)
│   │   └── deploy.ps1       — one-shot portal standup (formerly deploy_platform.ps1)
│   ├── ingest/              — Windsor connector loaders (ga4, google_ads, meta, tradedesk, reddit,
│   │                          hubspot, fields) that write raw_windsor.* — scheduled API pulls
│   └── status-dashboard/    — meta freshness monitor over every client (no dataset/views)
├── clients/                 — one folder per client; client_template/ is the worked pattern
│   └── client_template/       sql/ · job/ · dash/ · deploy scripts · README
├── assets/                  — brand kit: logo set, brand.json/brand.md, clients/<c>.svg
├── tools/                   — operator tooling: setup.ps1, start_day.ps1, deploy_ingest_jobs.ps1,
│                              enable_platform_sso.ps1, enable_super_admin.ps1, _validate_dash_js.py,
│                              push-branch.ps1, merge-branches.ps1
├── preview/                 — double-click local-preview launchers (admin / client-login)
├── docs/                    — deeper docs; docs/dev-workflow.md = the branch → PR → CI → merge flow
└── AGENTS.md (this manual) · CLAUDE.md (@AGENTS.md pointer) · README.md · ONBOARDING.md
```

`tools/_validate_dash_js.py` is the shared pre-deploy JS gate; `assets/` is the brand kit the seed
inlines into each workspace (the deployed container only bundles `dash/`, so logos are embedded).

## Dashboard edits

Each dashboard is **one big self-contained `dash/dashboard.html`** (no build step, no external JS).
Grep for the metric or label you want to change and edit in place. Theme colors are CSS custom
properties in `:root`. Inline JS must stay **esprima-4.x-safe**: no optional
chaining `?.` and no nullish coalescing `??` (the pre-deploy gate `tools/_validate_dash_js.py`
parses it with esprima, which predates those tokens). Use classic `&&`/`||` guards.

### 🔴 Every dashboard follows the dashboard standard (since 2026-07-30)

**Read [`clients/_standard/STANDARD.md`](clients/_standard/STANDARD.md) before editing any
dashboard.** All eight client dashboards now follow **one of two layouts** — **Leads** (the product
of advertising is an enquiry: CPL, no revenue) or **Sales** (an order or booking: revenue, AOV,
ROAS) — over **one shared shell**: the same chrome ids, the same class vocabulary, the same helper
names, the same section order, two independent date ranges, and a "Reading of the …" insight strip.
Client extras are untouched — the standard is a **floor, never a ceiling**.

- **A new client copies a reference implementation**, not another client:
  `clients/_standard/dash/dashboard-{leads,sales}.html`. `clients/client_template/` is the worked
  BigQuery instance of the Sales standard.
- **`clients/_standard/dash/_lib.js` and `_conform.css` are VENDORED** into every dashboard between
  sentinel comments — the same posture as `freshness.py` / `platform_sso.py`: **fix them everywhere
  or nowhere.** Edit the source and run `py -3 clients/_standard/vendor_lib.py`. Never edit a
  vendored block in place; `vendor_lib.py --check` fails if a copy is stale.
- **`_shell.css` is copied and re-branded, not vendored.** Identity is meant to differ per client;
  structure is not. A client edits only the identity tokens in `:root` — **encoding** (`--s1..--s8`
  series ramp) must stay distinguishable, and **status** (`--ok/--warn/--crit`) never inherits a
  brand hue.
- **Two gates before any dash deploy** (both exit non-zero on failure):

```powershell
py -3 tools\_validate_dash_js.py       clients\client_<c>\dash\dashboard.html
py -3 clients\_standard\check_standard.py                # 21 rules, every client
py -3 clients\_standard\vendor_lib.py --check            # shared blocks in sync
```

- **Waivers are attributes, not exceptions in a script.** Three dashboards legitimately skip a rule
  (an uptime monitor should not grow a KPI-benchmark average); each states its reason in a
  `data-no-benchmark` / `data-single-view` attribute on `<body>`, and the gate prints it every run.

### 🔴 A dashboard change does NOT deploy itself for 5 of 8 clients

`tools/merge-branches.ps1` maps `clients/<c>/dash/**` → `clients/<c>/dash/deploy_dash_*.ps1`. Only
**TCS and agora** have one. `honeytribe`, `MeloYelo`, `RHE`, `S7000` and `riverdance` keep a **full
standup** at the client root instead — and `merge-branches` deliberately will **not** call it,
because those scripts re-read secrets they will not be given and **mint a fresh `SESSION_SECRET`**
(logging every client out). Before 2026-07-30 that was a quiet yellow `[skip]`, so a dashboard could
land on `main` while production kept serving the old build and the ship still reported success.

It now ends the run with a red **NEEDS YOU** block, exits non-zero, and prints the right recipe per
client (`-DashOnly` where the standup has it, otherwise the image-only path). To ship one by hand
**without touching secrets or env**:

```powershell
gcloud builds submit --tag asia-southeast1-docker.pkg.dev/agora-data-driven/agora/<svc>:<sha> `
    --project agora-data-driven clients/client_<c>/dash
gcloud run services update <svc> --project agora-data-driven --region asia-southeast1 `
    --image asia-southeast1-docker.pkg.dev/agora-data-driven/agora/<svc>:<sha>
```

`services update --image` changes the image and **nothing else** — every env var, secret binding and
service account survives. Confirm the service name first (`gcloud run services list …`): one client
can back several (**S7000 runs three** from one `dash/`). The durable fix for a client on that list
is to give it its own `clients/<c>/dash/deploy_dash_<c>.ps1`.

**`clients/_standard/` and `client_template` are not deployable clients** and are excluded from the
map. `template-dash` has never existed in Cloud Run, so its dash script tries to *create* a service,
which needs standup permissions — that hard-failed a whole ship once and starved the real clients
queued behind it.
- Two traps that cost real time when the standard went in, both now guarded: a **`*/` inside a JS
  block comment** (e.g. a `clients/*/dash` glob) closes the comment early and fails the esprima
  gate a thousand lines later; and a **literal script-src tag inside an HTML comment** makes the
  gate parse the rest of the file as JavaScript.

## Agora Atrium (client workspace in the portal)

Atrium is the co-branded client workspace built **into** `platform-dash` — **additive**, reusing the
existing session auth, bucket, and runtime SA. **No new infra/IAM/bucket/secret/service** — except for
a few deliberately **opt-in** features that stay dormant and infra-free unless an operator enables them:
the Google-Doc → AI strategy feature, large-creative signed uploads, and the daily Market-Intelligence
auto-refresh (see those bullets below). Product name is one constant:
`WORKSPACE_NAME` in `services/portal/dash/main.py`.

- **State = one private JSON per client (no database):** `workspace/<c>.json` in the **registry
  bucket** `agora-data-driven-platform-dash`. `dash/workspace.py` is the only reader/writer
  (last-write-wins, mirrors `store.py`); it imports `google-cloud-storage` lazily and supports a
  local-fs backend via `WORKSPACE_LOCAL_DIR` (+ `WORKSPACE_BUCKET`/`WORKSPACE_PREFIX`) so it is
  testable off-cloud. Shape: `metrics`, `today`, `split`, `series`, `activity`, `campaigns[]`
  (`strategy`/`ai_summary`/`strategy_doc` + `content[]` with status `awaiting|approved|changes`,
  `client_note`, an optional publish `date`, threaded `comments[]` (each `id`/`sender`/`body`/`kind`;
  a `kind:"changes"` comment is a "Request changes" comment that flips status and carries `resolved`),
  and optional uploaded-creative `image_object`/`image_mime`),
  `calendar[]`, `conversations[]` (`client`/`agora` messages), `intel`
  (`business_research[]`/`media_buying[]`, each entry `heading`/`title`/`body`/`source`/`link`/`date`)
  for the Market Intelligence tab, `reports[]` (the Reports tab's small index — each entry
  `id`/`title`/`date`/`origin`/`payload`; the rendered deck HTML is its OWN object, see the
  Reports bullet), per-user `notify` prefs,
  and `website_health` (`url`/`notes`/`last_check`) for the team-only Website Health tab.
- **Website Health is a TEAM-ONLY tab (admins see it, THE super admin edits):** an extra nav tab +
  pane rendered ONLY for `is_superadmin()` (never shown to clients — the nav, the pane, AND the
  `/w/<c>/website-health` route all gate on it; a client hitting the URL is bounced to Dashboard).
  Editing (set URL, run check, notes) is gated `is_root_admin()` via `_atrium_root_json_gate` and the
  `can_edit_health` template flag, so a non-root admin gets a READ-ONLY view ("the admin can just see
  it"). `dash/atrium_health.py` (pure, infra-free) fetches the client's live site server-side and
  reports reachability/errors + the marketing tags installed on the page (GTM containers, GA4, UA,
  Google Ads, Meta/TikTok/LinkedIn/Hotjar/Clarity… — detected by scanning the returned HTML, NOT the
  GTM API, so no new infra/credentials; deeper in-container introspection would need the GTM API and
  stays out of scope). It degrades gracefully (a dead site is recorded in the result, never a 500).
  Routes: `POST /w/<c>/admin/website-health/{save,check}` (root-only). State lives under
  `ws["website_health"]` via `workspace.set_website_url`/`set_website_notes`/`save_website_check`.
- **Watcher is a TEAM-ONLY tab (creator/competitor content archive):** paste a channel link and
  Watcher lists EVERY video, then pulls each video's raw transcript — or paste a **website** and it
  lists EVERY blog post, then pulls each post's full article text (AI summaries are a later step).
  Rendered/gated exactly like Website Health (`ATRIUM_TEAM_TABS`, never shown to clients), but
  editing is any-admin (`is_superadmin()`), not root-only. **Two source types share ONE tab, ONE
  archive shape and ONE set of routes** — a registry entry's `platform` (`youtube`|`blog`) is the
  only thing that picks a fetcher; a blog post is stored in the very field a video transcript is
  (`transcript`), so the cards, the reader modal, the counts and the Assistant index never branch.
  **Websites (`dash/watcher_blog.py`, op `add_site`):** resolve the site (its origin becomes
  `channel_id`) → list EVERY post **sitemap-first** (robots.txt `Sitemap:` → index → blog-named
  children only; index-page crawl only as a fallback) → extract each post's readable text with a
  stdlib readability-lite scorer (no new dependency), healing the title/date from the page's own
  og:/JSON-LD metadata. robots.txt is honored with real wildcard + `Allow`-precedence matching
  (🔴 a prefix-only reading of Shopify's `Disallow: /blogs/*+*` silently bans every post on the
  site), and a **TLS cipher pin** in `_session` is load-bearing: Python's default handshake gets
  `429 local_rate_limited` forever from Cloudflare-fronted Shopify while curl gets 200. Blogs need
  NO proxy and NO Safe pull (Cloud Run isn't blocked by ordinary sites), so that button is hidden on
  their cards. **YouTube (`dash/watcher.py`)** does the fetching with
  NO YouTube API key: channel page scrape → the public web `youtubei/v1/browse` endpoint pages the
  uploads playlist (handles BOTH the classic `playlistVideoRenderer` and the 2025+ `lockupViewModel`
  shapes, and captures each video's relative upload age → `published_text` +
  `watcher.published_estimate` ISO date) → `youtube-transcript-api` (pinned in dash requirements,
  imported LAZILY so tests/CI run without it) per video. **Classification:** each channel carries
  `platform` (the SOURCE TYPE, and what decides which fetcher runs: `youtube` or `blog`), `industry`
  (auto-labeled on add from the video/post titles via `intel_ai.classify_text` — the intel brain's
  default model — and hand-editable), and `kind` creator|competitor. State: the small channel
  registry lives in `ws["watcher"]["channels"]` (counts + classification only); each channel's full
  archive is its OWN object `workspace/watcher/<c>/<channel_id>.json` (transcripts run to MBs —
  same posture as creatives). Routes: `POST /w/<c>/admin/watcher` (`op`
  add|**add_site**|add_video|fetch|refresh|meta|label|delete — **add_site** is the website twin of
  add (list every blog post; the SAME fetch loop then pulls each article's text), **add_video
  auto-detects** (a link with no YouTube video id is scraped as a blog post into a separate "Saved
  articles" loose channel), fetch pulls MISSING bodies in batches
  (parallel `FETCH_WORKERS` waves behind a rotating proxy, else the serial politely-paced path) and
  the page JS loops it with a progress bar; **a YouTube rate-limit reports `blocked` WITHOUT marking
  any video failed** and the loop AUTO-RETRIES with backoff (~20s→2min, resets on progress) instead
  of stopping, so the archive fills without re-clicking (the button toggles to Stop to cancel);
  **add_video scrapes ONE pasted video link** (resolve title via keyless oEmbed, falling back to a
  watch-page og:title scrape when oEmbed 401s → fetch transcript
  inline → save under the per-client "Saved videos" pseudo-channel, marked `loose`, created by
  `workspace.ensure_loose_channel`; a rate-limit saves it pending + reports `blocked`, so the card's
  Fetch missing / Safe pull can finish it — the loose channel is fetched/safe-pulled/indexed like
  any other, only its Check-new/Auto-label actions are hidden since it has no real channel_id);
  refresh also backfills upload dates; meta hand-edits industry/kind; label re-runs the AI label)
  and `GET /w/<c>/watcher/video/<channel_id>/<video_id>` (the click-to-expand full transcript; the
  page itself only inlines previews). **UI = a filterable creator grid:** three creator cards per
  row (collapsed = classification chips + the 4 most recent videos; expand = the full uniform video
  grid + per-channel title search), with a top bar filtering by creator search / platform /
  industry / creator-vs-competition and sorting by newest upload / recently added / name. Every
  failure degrades to a friendly message (`ok:false`); permanent no-transcript videos are recorded
  and skipped. ⚠️ YouTube blocks datacenter IPs, so Cloud Run fetches usually need the OPT-IN
  egress proxy: create Secret `watcher-proxy-url` (full proxy URL, e.g. Webshare rotating
  residential) and redeploy — `deploy_dash_platform.ps1` mounts it as `WATCHER_PROXY_URL` only when
  it exists. **Safe pull (the no-proxy path):** each card's "Safe pull" button (`op=safe_pull`)
  queues the channel in `ws["watcher"]["safe_pull"]`; the operator machine's scheduled task
  ("Agora Watcher Safe Pull", installed by `dash/install_safe_pull_task.ps1` →
  `dash/safe_pull_agent.vbs`, every 5 min, hidden, single-instance) runs
  `dash/safe_scrape_local.py --queue` from a residential IP with 12–20s pacing + a 5→60 min
  rate-limit ladder, syncing transcripts back to the bucket as it goes and clearing each queue
  entry on completion (no args = full sweep of every client; a %TEMP% PID lock keeps every mode
  single-instance). It is SLOW by design (≤5-min tick latency + ~15s/video + cooldowns). **Live
  status:** the scraper writes a per-video heartbeat to `workspace/watcher_safe_pull_status.json`
  (`safe_scrape_local.write_status` / `workspace.read_safe_pull_status`); `GET
  /w/<c>/watcher/safe-pull-status` fuses it with the queued channels' counts and the Watcher tab
  polls it (~12s) to show what's fetching now / cooldowns / idle-since + a progress bar, instead of
  "check back later". **Sentinel imports from this archive** over `GET /api/internal/watcher/
  {channels,videos,transcript}` (HMAC-gated by `_internal_gate`, same scheme as the task bridge) —
  its Growth hub → Mentor Library pulls a transcript instead of hand-pasting one. READ-ONLY (Sentinel
  copies the text into its own table) and **cross-workspace by default like `/api/internal/tasks`**:
  a mentor is nobody's client, so `?client=` is an optional filter, and channel ids come back
  namespaced `"<client_key>:<channel_id>"` so the follow-up calls can find the archive object.
  Off-cloud test: `dash/_watcher_localtest.py` (in CI; stubs GCS + the YouTube
  fetchers).
  - **The source TEMPLATE — default sources EVERY client gets, automatically (2026-07-30):**
    Watcher used to start empty, so what we monitored per client was folklore. `watcher_template.py`
    is the catalog that fixes it (the twin of `service_templates.py`): a code-defined,
    git-versioned source list applied at **onboarding** (`onboard_client.onboard`, the ONE funnel
    both `/admin/atrium/new` and `/admin/accounts/approve` use) and **back-filled to every existing
    client** by `main._watcher_reconcile` on any team render. Segmented — `UNIVERSAL` (ad-platform
    news) for everyone, plus industry segments matched loosely against the Company tab's free-text
    `industry` (`segments_for`); a day-one workspace has no Company profile, so onboarding seeds
    only UNIVERSAL and the reconcile adds the industry sources on the first render after the
    Company tab is filled in. Adding a source = one dict + a `TEMPLATE_VERSION` bump.
    🔴 **SHARED archives — one copy for the whole estate.** An archive is stored PER CLIENT, so 15
    clients watching Search Engine Land would mean 15 copies of a multi-MB object, 15× the
    publisher traffic and 15× the embedding bill. A template source marked `shared` is fetched and
    stored ONCE in the **house workspace** (`workspace.HOUSE_CLIENT`, default `agora` — the same one
    Sentinel's Mentor Library already reads). The sharing is encoded in the **ENTRY ID**
    (`workspace.SHARED_PREFIX` = `wsh_`, id derived by `shared_channel_id`), so the single redirect
    inside `workspace.watcher_object_name` makes every existing caller — tab render, fetch loop,
    Sentinel bridge, Assistant index — resolve correctly with no signature change.
    ⚠️ **`safe_scrape_local.py` builds that path BY HAND** and never calls `watcher_object_name`:
    teach it the `wsh_` rule before shipping a shared YOUTUBE source, or Safe pull writes transcripts
    where nothing reads them.
    🔴 **Reconcile invariants** (all four have regression tests): registry-only (it creates entries
    and never fetches — the existing Fetch-missing / Safe-pull loops fill archives, which is what
    makes it cheap enough to run on every render with no job and no infra); additive (a hand-added
    source is never touched, and a site the team added by hand is matched on `channel_id` so the
    template never plants a duplicate); idempotent; and **a deleted template source stays deleted** —
    `delete_watcher_channel` records the opt-out in `ws["watcher"]["template"]["removed"]`, because
    without it the team deletes an irrelevant source and the next render puts it back forever. That
    same delete NEVER removes a shared archive (it still serves every other client).
    Template sources are **blog-only on purpose**: sites serve Cloud Run fine, while YouTube blocks
    datacenter IPs and would route the whole template through the one-residential-IP Safe-pull queue.
    `kind` stays within the existing `creator|competitor` pair — the UI treats it as a two-state
    toggle with a hardcoded dropdown and one CSS class per value, so a third kind (`authority` for a
    county alerts page) is a template change, not a data change.
    Entries appear everywhere automatically; the first **listing** of each shared source is still one
    "Check for new posts" click (it lists from an empty archive) — once per source for the whole
    estate, not once per client. Tests: `_run_template_checks` in `dash/_watcher_localtest.py`.
  - **Internal read bridge (Sentinel's Mentor Library, 2026-07-28):** three HMAC-gated,
    server-to-server, READ-ONLY routes let Sentinel's Growth hub import a transcript Watcher
    already archived instead of hand-pasting one: `GET /api/internal/watcher/channels?client=<c>`
    (light channel list), `GET /api/internal/watcher/videos?client=<c>&channel=<id>` (light video
    list, no transcript body), `GET /api/internal/watcher/transcript?client=<c>&channel=<id>&video=<id>`
    (the one video's full text, on demand). Same `_internal_gate` HMAC scheme as the task bridge
    just above (`platform-sso-key`, X-Academy-Ts/X-Academy-Sig) — no new secret. Sentinel COPIES
    the transcript in on import; these routes never write to a workspace. Mentor content isn't any
    one client's, so Sentinel is configured (`ATRIUM_WATCHER_CLIENT_KEY`, default `agora`) to read
    the **`agora`** workspace's Watcher archive — add creators like Nic Saraev / Carson Reed there
    for them to show up in Sentinel's picker. See `sentinel/backend/app/services/atrium_watcher.py`
    (the caller) and `atrium_bridge.py` (the shared signing transport, also used by `atrium_tasks.py`).
- **Assistant is a TEAM-ONLY tab (RAG chat over the WHOLE workspace):** grounded Q&A across every
  source the portal holds for a client — **the Company profile** (who they are, their brand guide,
  their products), campaigns + content (incl. comments), workspace metrics,
  Market Intelligence, the calendar, client conversations, website health, every Watcher
  transcript, and (opt-in) the client's dashboard `<c>.json` KPI export. `dash/assistant_ai.py`:
  `build_chunks` flattens the sources, `build_index` stores a pure-Python BM25 index as ONE private
  object `workspace/assistant/<c>/index.json` (rebuilt lazily via `fingerprint` whenever data
  moves). **Retrieval is HYBRID** (production-RAG shape): BM25 (keyword) **and** a semantic leg
  (`embed_index` embeds every chunk once via Vertex `text-embedding-005` — same SA/auth as the
  Gemini brain, NO new API/IAM — and packs unit vectors compactly into the same index object) are
  each ranked per query and fused with **Reciprocal Rank Fusion** (`_rrf`, rank-only so the
  incompatible BM25/cosine scales never fight); the fused pool is then optionally sorted by a
  **cross-encoder reranker** (Vertex Ranking API `semantic-ranker-fast-004`) — "retrieve wide, keep
  few". Every chunk is indexed AND embedded by its **title + body** (`_searchable`), so the entity
  name a user searches by (creator/channel name, campaign name, email subject) is retrievable even
  though it never appears in the body — the transcript never says its own channel name (this fixed
  the 2026-07 "no Fuel Your Wander content" miss; the index is `INDEX_VERSION`-stamped so the shape
  change forces a one-time rebuild). A **metadata pre-filter** (`_infer_kinds`) scopes retrieval to
  one source kind ONLY for an unambiguous single-source question (relaxed if it would empty the set,
  or if the question NAMES a watched creator — `_question_names_creator` keeps `video` in scope so
  "what would <creator> say about <campaign>" stays cross-source), on top of the date range
  (dated sources only). Both the semantic leg and the reranker are gated + graceful: with them off
  (or on any call failure) retrieval is exactly the old BM25 path. Embeddings are ON by default when
  Vertex is wired (`ASSISTANT_EMBED_ENABLED=1`, `VERTEX_EMBED_LOCATION` = the project region so
  private chunk text stays in-region); reranking is opt-in
  (`ASSISTANT_RERANK_ENABLED=1`, needs `enable_assistant_reranking.ps1` — enables
  discoveryengine.googleapis.com + grants the web SA `roles/discoveryengine.user`; the deploy
  auto-detects the API and flips the flag). `intel_ai.embed_texts`/`embed_query`/`rerank` are the
  transport (injected into `ask` as `query_embedder`/`reranker`, so tests run with no network).
  `ask` answers with the intel brain's provider plumbing (`intel_ai._call`, the
  client's configured model or the default; prompts for `{"answer": ...}` JSON, parsed leniently —
  `_parse_answer` also SALVAGES nearly-JSON (trailing junk, truncation, raw newlines) so the chat
  never displays a raw JSON envelope; the UI renders answers as markdown via `mdToHtml`).
  The admin's **Detail control** (`assistant_ai.DEPTHS` quick|standard|deep, saved via
  `op=settings` → `ws["assistant"]["depth"]`, dropdown beside the model picker in both surfaces)
  shapes the pipeline: deep first has the model PLAN extra BM25 queries (`plan_queries`, so a
  comparative question retrieves each entity's actual positions), retrieves wider (30 excerpts),
  turns provider thinking ON (`intel_ai._call(..., think=True)` — Gemini gets a 4096 thinking
  budget, DeepSeek gets `thinking:{type:enabled}`; quick/standard pin the fast no-thinking path),
  and asks for a structured analysis; quick trims to a few sentences. Every depth's prompt allows
  cross-source synthesis (differing recommendations count as disagreement).
  Answers cite sources; the UI shows them as chips. **The chat STREAMS** (Server-Sent Events via
  `POST /w/<c>/admin/assistant/stream`): the model's reasoning shows live in a collapsible thinking
  panel, then the answer streams in. `intel_ai.stream_call` normalises Vertex/DeepSeek SSE to
  thinking/answer/usage deltas; `assistant_ai.ask_stream` streams plain markdown (not the JSON
  envelope). Two Claude-style controls: **Pause & steer** (abort the stream mid-thinking and restart
  with guidance) and a **Plan first** toggle (`stage=plan` → `assistant_ai.plan_stage` shows the
  planned sub-queries + sources and PAUSES for approve/steer BEFORE answering). Conversations are
  **session-scoped** now (`sessionStorage` — a new session/tab starts fresh). The non-streamed
  `op=ask` stays for tests/fallback. **THREE model providers** back every AI surface in Atrium
  (`intel_ai.MODELS` is the ONE registry — the Assistant, the intel brain, the Mail digest and the
  Watcher auto-label all dispatch through `intel_ai._call`/`stream_call`): **gemini** (Vertex,
  GCP-billed, the only one that can ground on live Google Search), **deepseek** (`DEEPSEEK_API_KEY`)
  and **kimi** (`KIMI_API_KEY` — the Kimi Code coding-plan host `api.kimi.com/coding/v1`, a flat
  weekly-quota subscription so it prices at $0/token; the lower-case `kimi-api-key` secret is the
  separate VS Code / Claude Code launcher key — do NOT mount that one). Adding a model is a MODELS
  entry + a `provider_configured` gate + a `_call_*`/`_stream_*` pair; both dropdowns render from
  `available_models()`, so an unconfigured provider shows greyed-out "(not set up)" and nothing else
  changes. Kimi is listed LAST so `default_model()` (first available) still resolves to Gemini Flash.
  Routes: `POST /w/<c>/admin/assistant` (`op`
  ask|settings|reindex) + `POST /w/<c>/admin/assistant/stream`, gated `is_superadmin()`; tab gated
  like the other team tabs. The dashboard-data
  source needs a one-time grant: `services/portal/dash/enable_assistant_dash_data.ps1` gives the
  portal SA objectViewer on each client dash bucket (run 2026-07-12; re-run for new clients) —
  without it that source is silently skipped. `VERTEX_ACCESS_TOKEN` env (dev-only) lets the same
  Vertex code paths run off-cloud with a `gcloud auth print-access-token` token. Off-cloud test:
  `dash/_assistant_localtest.py` (in CI). The Watcher tab also gained a Looker-style upload-date
  range control (presets + custom from/to) that filters videos and creators client-side.
  - **The distilled layer (`digest.py`, 2026-07-29, INDEX_VERSION 4):** the Assistant is fed
    INSIGHTS, not raw dumps. `digest.py` (pure, stdlib) derives titled sections from raw data:
    the dashboard export becomes overview/campaigns/creatives/trend/audience/email sections
    (handles BOTH shapes — the template `kpis`/`daily` contract AND the Windsor-live per-ad/day
    `rows` export, which previously indexed as ONE opaque JSON dump the retriever matched on
    noise); the Communications timeline, the task board (each task chunk carries its id so the
    Assistant can act on it) and the Reports payloads are now indexed too, plus rolling
    intel/comms/board snapshot chunks for broad questions. Chunks carry `level`
    (`digest`|`full`) + `parent`: a video's cached AI summary or an email card is the DIGEST, the
    transcript/thread chunks are its FULL siblings, and `_expand_hits` (small-to-big retrieval)
    unfolds the best full siblings behind a top-ranked digest hit — compact context by default,
    the whole document when the question needs it. Per-video summaries are written by
    `assistant_ai.summarize_videos` ONLY on the explicit Reindex (capped batch/run, stored into
    the archive objects) so chat latency never pays for them. `fingerprint` also stamps the
    dashboard blob's last-modified (`main._dash_stamp`) — before that a refreshed dashboard never
    re-indexed until some other source moved.
  - **Assistant ACTIONS (propose → approve → execute, 2026-07-29):** the Assistant can do
    anything an admin can do — but ONLY by proposing. `assistant_actions.py` is the one registry
    (add/move/complete/comment task, calendar event, intel add/edit/delete, log communication,
    website check + notes (root-gated), generate/edit/rename/delete report, reindex; adding an
    action = one `_ACTIONS` entry + one executor). The model emits proposals after its answer via
    the `===ATRIUM_ACTIONS===` marker protocol (`assistant_ai.split_actions` / the `_ActionTail`
    stream filter keep it off-screen); the server VALIDATES each against the registry and the UI
    renders approval cards — **nothing executes until a human clicks Approve** (`op=execute` on
    `/w/<c>/admin/assistant` re-validates + re-checks the role gate; root-gated actions require
    `is_root_admin`). Every executor calls the SAME `workspace.py` writers the human forms call
    (the Sentinel-bridge rule), and every approval is `_audit`ed.
  The same chat is ALSO a **floating bubble** (team-only FAB bottom-right, Mastery-Engine style,
  brand-green 72px since 2026-07-13 — and the PRIMARY surface now that the nav tab is gone)
  reachable from every tab: one `wireAssistantChat` wiring in `atrium.html` serves both surfaces
  (the tab keeps the date-range + reindex controls), the conversation is persistent **chat
  history** (localStorage, per client + surface, last 40 turns, per-browser; replayed on the next
  visit, "New chat" on either surface clears it), and the bubble hides on the Assistant tab itself
  (CSS on the root's `data-tab`, which `showTab` keeps current). The Assistant has its **own model choice** (dropdown in the tab bar +
  the bubble's gear strip; `op=settings` → `ws["assistant"]["model"]`, "" = automatic: the intel
  brain's model, else the deploy default) and an on-screen **spend tally** (mastery-style cost
  pill above the FAB: session + all-time + by-model detail). Both provider calls in `intel_ai`
  accept a `usage_out` dict that captures token counts; `intel_ai.PRICING`/`cost_of` price them
  (approximate, editable) and `workspace.add_assistant_usage` persists the all-time tally in
  `ws["assistant"]["usage"]`; each `op=ask` response carries `usage` + `totals`.
- **Communications is ONE unified, channel-tagged, date-filterable timeline** (`conversations` tab,
  rebuilt 2026-07-15): every conversation -- email, Upwork, Slack, meeting, call, note -- is a single
  card in a date-sorted feed, each with a coloured **channel badge** and an **audience** (`client`
  or `team`). State is ONE list `ws["communications"]` (`{id,channel,audience,title,summary,date,
  people,origin,thread_key}`); the legacy split lists `email_summaries[]`/`meeting_summaries[]`
  migrate into it in place via `workspace._ensure_communications` (called by every read/mutation;
  `add_communication`/`update_communication`/`delete_communication`/`upsert_email_summary` are the
  writers, `add_email_summary`/`add_meeting_summary` kept as thin wrappers). **Client/team split is
  enforced SERVER-SIDE:** `main._communications_view(ws, client, is_admin, mailview)` filters a
  client render to `audience=="client"` BEFORE the template (team cards never reach the client HTML,
  same posture as `_progress_tasks`); admins additionally get the non-client email threads projected
  from Mail as team-only cards, plus an All/Client-sees/Team-only toggle + per-card visibility pill.
  Channel chips + counts, the date range, and the audience toggle all filter client-side in
  `atrium.html`. Route `POST /w/<c>/admin/communication` (op add|edit|delete, `channel`+`audience`).
  **Upwork import (op `import_upwork`, team-only):** paste a raw Upwork message thread and
  `dash/upwork_import.py` (pure, infra-free — no network/storage/AI) parses it into an ordered,
  role-tagged (agora vs client by matching the team's Upwork display name), de-duplicated (quoted
  reply-backs dropped) message list. The route stores it like a Mail thread archive object (key
  `up_<id>` via `workspace.write_mail_thread`) so the SAME thread-reader modal + `GET
  /w/<c>/mail/thread/<key>` route render it (the modal role-tints chat messages, which carry no
  `to`); the timeline gets an `upwork`-channel card whose summary is the Mail brain's recap
  (`mailroom.summarize_thread` via `_mail_model`, client/internal voice by audience — falls back to
  a plain `upwork_import.fallback_summary` when no model is configured). The card is a LIVING
  record: op `update_upwork` (the reader's "＋ Add newer messages") folds a re-paste in
  de-duplicated, stamps the card with the UPDATE date (its date means "last updated") and re-writes
  the recap; bare "Today"/"Yesterday" day separators and Upwork's meeting/Zoom/milestone event
  lines parse correctly. Deleting the card also
  removes its thread object (no orphan). Test: `dash/_upwork_import_localtest.py` (in CI).
- **Mail is FOLDED INTO Communications (team-only machinery; client email archive + AI digest,
  `mailroom.py`):** there is no standalone Mail tab anymore -- its contacts editor, Sync/Refresh
  buttons, the AI briefing, and the response-stats strip live in the Communications tab's collapsible
  **Email intelligence** panel (admin-only), and email threads appear as email-channel cards in the
  timeline (client-tier via the mirror below; other tiers as team-only cards with a "Read full
  thread" reader). `/w/<c>/mail` now renders Communications; the `POST /w/<c>/admin/mail` +
  `GET /w/<c>/mail/thread/<key>` routes are unchanged (invoked from within Communications). Connect
  the agency's mailboxes ONCE in the console (`/admin/atrium` -> **Mailboxes**; add/remove/test is
  root-admin only -- entries carry live credentials); the Email intelligence panel then lists that
  client's contact emails/domains and the sync pulls ONLY correspondence with those contacts (the
  Gmail query is BUILT from the contact list -- from:/to: each address or bare domain -- so
  unrelated mail never leaves the mailbox). Two connector kinds: **dwd** -- our own Workspace
  mailboxes via the Gmail API + domain-wide delegation, KEYLESS (the runtime SA signJwt's as the
  dedicated `mail-sync` SA; one-time `enable_atrium_mail.ps1` + a 2-minute Workspace-admin grant,
  nothing stored per mailbox) -- and **imap** -- ANY other Google account the team holds an app
  password for (stdlib `imaplib` + `email`, Gmail's `X-GM-RAW` runs the SAME query, All Mail covers
  sent + received). State mirrors Watcher exactly: the global mailbox registry is ONE private
  object `workspace/mail/_mailboxes.json` (an imap app password lives there verbatim, is required
  to log in, and is NEVER rendered back -- `workspace.public_mailboxes` strips it); each thread's
  full messages are their OWN object `workspace/mail/<c>/<key>.json` (key = mailbox id + Gmail
  thread id; quoted-reply tails stripped, message-id dedup makes re-runs cheap); `ws["mail"]`
  keeps only the small index (contacts, subjects/participants/summaries, the digest,
  last_sync/last_error). **Triage, not deletion (nothing important is ever dropped):** every thread is KEPT and tiered
  by `mailroom.classify_thread` -- **security** (account/password/sign-in alerts -> shown first, can
  bypass a VA), **client** (human mail involving the client, or ANY human mail in a client-owned
  mailbox), **operations** (human mail not from the client -- vendors/partners/leads), **noise**
  (newsletters/bulk/automated, via `is_automated`; List-Unsubscribe alone never counts, Google-
  Groups-safe). Only Gmail's SPAM folder is skipped at ingest. The Email intelligence panel defaults to hiding noise
  and flags security; the hourly AI summarizes every tier EXCEPT noise (cost control). **Per-mailbox
  scope:** a mailbox can be ASSIGNED to one client (a dedicated inbox they gave us) -> its WHOLE
  contents are ingested, no contact list needed; an unassigned/**shared** mailbox is routed to each
  client by that client's contact list. **Response stats are computed, not
  guessed** (`thread_stats`/`stats_line`): per thread `awaiting_reply` (is the last word the
  client's) + average AGORA reply hours (a sender matching a connected mailbox address, or a dwd
  mailbox's domain, counts as agency -- so a VA answering from info@ is "us"); shown as the Mail
  tab's stats strip + per-row chips. The intel brain (`intel_ai._call`; model = Assistant's ->
  intel's -> default) writes TWO voices per changed thread in ONE call
  (`summarize_thread` -> internal summary + `client_summary`): the internal one (blunt, includes
  reply-quality observations) runs the Email intelligence panel and the digest; the client one (for CLIENT-tier threads only) is MIRRORED into
  the client-visible Communications timeline as an **email-channel card** (`workspace.upsert_email_summary`,
  stable id `mail_<key>` so re-summarizing updates in place; deleting the thread retracts the
  mirror -- safe by construction, the client was on every thread). The rolling digest is STATUS /
  NEEDS ACTION / RECENT / **REPLIES** -- the REPLIES section judges reply speed + quality against
  the computed `stats_line` numbers, explicitly treating AGORA replies as possibly written by an
  assistant (the VA-accountability view). AI spend folds into the Assistant cost tally, and the
  **Assistant indexes the email archive too** (`build_chunks` `mail_threads`, kind `email`, plus a
  computed `mail:responsiveness` snapshot chunk -- so "how well are we handling this client's
  email?" retrieves real numbers). Routes: `POST /w/<c>/admin/mail`
  (op contacts|sync|digest|delete, gated `is_superadmin()`), `GET /w/<c>/mail/thread/<key>` (the
  click-to-read full thread), `POST /admin/mail` (op add|delete|test, gated `is_root_admin()`).
  **Auto-pull:** Cloud Run job `mail-refresh` (`mail_refresh.py`, Cloud Scheduler
  `mail-refresh-hourly`, gated `MAIL_SYNC_ENABLED=1`, REUSES the platform-dash image + web SA --
  `deploy_mail_refresh.ps1`; rerun after any mailroom/mail_refresh change, image-pinned). First
  sync backfills `MAIL_FIRST_SYNC_DAYS` (90); later runs re-query a short `MAIL_SYNC_DAYS` (7)
  overlap. A default deploy stays infra-free: imap mailboxes work immediately; only the dwd
  connector needs the enable script. Off-cloud test: `dash/_mail_localtest.py` (in CI).
- **Content with a date mirrors onto the Content Calendar (linked event):** when an admin gives a
  content piece a `date` (in the add/edit-content form), `workspace.add_content`/`update_content`
  mirror it into `calendar[]` as a linked event carrying `content_id` + `tab` (paid→`leadgen`,
  organic→`organic`); the piece is the source of truth (editing date/title/channel OVERWRITES the
  event, clearing the date or deleting the piece removes it), while the calendar keeps its own
  mark-as-done `status`. The calendar day-popup shows linked events with a "Paid Media /
  Organic Content" source tag and a **→** arrow that jumps to the piece on its tab. Done/colour
  logic (`atrium_view._event_done`/`_event_overdue`): a content-linked event is green only once
  **explicitly marked done**, **red (overdue)** if past its date and unmarked, green-ahead if a
  future date is already done; **plain** (non-content) calendar events keep the original
  green-forward rule (past ⇒ done). The JS in `atrium.html` (day-popup + month-history grid) mirrors
  this exact logic.
- **Uploaded creatives = separate private objects (NOT inline in the JSON):** an admin-uploaded
  creative (image OR video) is stored as its own object `workspace/creatives/<c>/<content_id>` in the
  **same registry bucket** (keeps the rewrite-in-full workspace JSON small) and is served ONLY through
  the authed proxy `GET /w/<c>/creative/<content_id>` (mirrors the `/data.json` posture — never made
  public). The serve route honors HTTP **Range** (a `Range` request → `206` windowed stream, 8 MiB
  cap, for video seeking; no range → `200` **chunked** full stream with NO `Content-Length`, since
  Cloud Run caps fixed-length responses at ~32 MiB but streams chunked ones unbounded). `workspace.py`
  streams via `blob.open("rb")` (one seekable download), never loading the whole object into memory.
- **Attached documents preview in place (no download required):** a per-piece attachment (the
  `images[]` row, served at `GET /w/<c>/creative/<content_id>/<image_id>`) that is a document renders
  a clean file-type icon (PDF/DOC/XLS/CSV/PPT/TXT band, color per format — `doc_icon` macro) that
  opens a scrollable doc lightbox with a transparent download button. The
  serve route is **inline by default** (so a PDF previews in an `<iframe>`); `?dl=1` forces an
  attachment download with the original filename. PDFs preview natively; Word/Excel/PowerPoint/CSV/
  text are rendered to scrollable HTML by `dash/atrium_docview.py` (**stdlib only** — `zipfile` +
  `ElementTree`, no CDN/no new deps) served at `GET /w/<c>/docview/<content_id>/<image_id>`; an
  unsupported/corrupt file degrades to a friendly "download to view" page. Classification is by
  filename extension AND mime, so an empty-mime upload no longer renders as a broken `<img>`.
- **Large creatives bypass the ~32 MiB request cap via a SIGNED URL (opt-in infra):** small files
  still POST through the app (`/w/<c>/admin/upload-creative`); files >30 MiB upload **directly to GCS**.
  The browser asks `POST /w/<c>/admin/creative-upload-url` for a V4 signed PUT URL
  (`workspace.signed_upload_url`, **keyless** — signs via the IAM signBlob API using a cloud-platform-
  scoped runtime-SA token; storage-scoped tokens fail with `ACCESS_TOKEN_SCOPE_INSUFFICIENT`), `PUT`s
  the file straight to the bucket, then `POST /w/<c>/admin/creative-confirm` records it. ⚠️ Needs
  one-time infra (run `services/portal/dash/enable_atrium_uploads.ps1`, idempotent): the
  `iamcredentials` API on, the runtime SA granted `roles/iam.serviceAccountTokenCreator` **on itself**,
  and CORS on the registry bucket. If signing is unavailable the route returns `ok:false` and the UI
  falls back to the in-app POST path (so a default deploy still serves ≤30 MiB uploads with no infra).
- **In-workspace admin editing = the team edits the REAL `/w/<c>/` in place.** When `is_superadmin()`
  opens a workspace, the SAME client UI renders extra edit affordances (`{% if is_superadmin %}` +
  `data-admin="1"`), posting JSON to `/w/<c>/admin/*`: `strategy`, `strategy-doc`, `generate-summary`,
  `summary`, `campaign`, `delete-campaign`, `content`, `edit-content`, `delete-content`,
  `content-comment`, `delete-comment` (delete any thread comment, on paid AND organic), `add-images`,
  `remove-image`, `upload-creative`, `creative-upload-url`,
  `creative-confirm`, `remove-creative`, `metrics`, `calendar`, `intel` (add/edit/delete a Market
  Intelligence briefing entry — `op`+`section`), `reply`. This in-place surface is the
  ONLY editing path — the old per-client `/admin/atrium/<c>` console page (and its
  password/campaign/content/conversation/reply/metrics POSTs) has been removed. **Clients** approve in place (`/approve`) and
  post threaded `/w/<c>/comment`s; "Request changes" now lives IN the comment thread as a
  `kind:"changes"` comment (light-red, flagged) that flips status to `changes`. Raising a change
  request is a CLIENT power; **resolving it is TEAM-ONLY** — the **Resolve** button (`/resolve-comment`,
  gated `is_superadmin()`) renders only for the team, and resolving the last open one returns the piece
  to `awaiting`. All of it updates in place (no reload), so the organic dropdown stays open.
- **Clients can set their OWN logo from inside the workspace:** the side-panel crest is a hover-to-upload
  control — hovering reveals a "Change logo" overlay; clicking opens a file picker that POSTs to
  `/w/<c>/logo` (client-facing, gated `authed()`+`can_open(<c>)`, image-only ≤512 KB). It is the
  client-facing twin of the team console's `/admin/atrium/<c>/logo`: the image is embedded INLINE as a
  `brand.client_logo` `<img>` data-URI (same posture as seeded logos — no new infra/object), and the
  crest swaps in place on success.
- **Market Intelligence is a CLIENT-VISIBLE, TEAM-CURATED tab (the weekly briefing):** a `/w/<c>/intel`
  nav tab + pane every client sees, holding two fixed sections — **Business Research** (competitor +
  industry news) and **Media Buying News** (Google/Meta/Instagram updates). State is one key
  `ws["intel"]` = `{business_research[], media_buying[]}`, each a list of entries (newest first)
  `{id, heading, title, body, source, link, date}`. `workspace.add_intel_entry`/`update_intel_entry`/
  `delete_intel_entry` are the only writers (`workspace.INTEL_SECTIONS` is the valid-section guard).
  The team writes/edits/deletes entries IN PLACE via `POST /w/<c>/admin/intel` (`op` add|edit|delete +
  `section`, gated `is_superadmin()`); clients read only. `atrium_view.intel_sections(ws)` decorates
  the two lists with their display label/lede/icon for the template. No new infra (one more workspace
  JSON key, mirrors Client Communications).
  - **Daily AI auto-refresh — GROUNDED web research (an AI 'brain', LIVE):** a Cloud Run job
    `intel-refresh` (`dash/intel_refresh.py`, Cloud Scheduler `intel-refresh-daily` 07:00 SGT) runs
    **grounded research** (`intel_ai.research`): the selected **Vertex Gemini** model, with the live
    **Google Search grounding** tool (`tools:[{googleSearch:{}}]`), PLANS the angles that matter to
    THIS client → SEARCHES the whole web → CURATES the strongest items, each with a REAL source URL
    (from `groundingMetadata.groundingChunks[].web.uri`) and a **`relevance`** ("why this matters for
    <client>") line. Same engine as Gemini chat — broad + on-topic, NOT a Google-News re-rank.
    `research` returns `(entries, error)`; **NO fallback** — a failure shows the reason and adds
    nothing. **Grounding is Gemini-only** (`intel_ai.model_supports_grounding`): a non-Gemini model
    (DeepSeek or Kimi) reports "can't do live web research — pick a Gemini model" and adds nothing. (The old
    retrieve-then-curate `intel_ai.curate` + `intel_feed` RSS scrape is LEGACY — kept as a helper +
    for tests, no longer wired into the refresh.) Vertex Gemini (`gemini-2.5-flash`/`-pro`) is
    GCP-billed via the runtime SA's metadata token, gated `VERTEX_GEMINI_ENABLED=1` +
    `VERTEX_PROJECT`/`VERTEX_LOCATION` (grounding works at `global`); ⚠️ grounded search bills extra
    per prompt, and we do NOT set `responseMimeType` (JSON mode is unreliable with the search tool —
    we prompt for JSON and parse leniently). Per-client config `ws["intel_ai"]` = `{model,
    business_prompt, media_prompt, window, count, show_thinking}` (admin-set in the **AI Research
    Brain** panel; `intel_ai.window_of`/`count_of`/`window_label` validate the recency `7d…12m` +
    target 1–25; keywords `ws["intel_topics"]` are SEEDS the model expands, not literal queries).
    **Business Research is keyed ENTIRELY off `ws["intel_topics"]` with NO fallback** — no keywords ⇒
    empty section + "set keywords" reason, never filler. **Media Buying News** is universal (runs for
    every client). The two sections research **concurrently** (writes stay serial). Intel entries gain
    a `relevance` field (`workspace._INTEL_FIELDS`), rendered as "Why this matters for <client>" under
    each summary. **`show_thinking`** (admin toggle, default off) captures the model's reasoning + the
    **search plan** (`groundingMetadata.webSearchQueries`) + grounded **sources** + Google **Search
    Suggestions** (`searchEntryPoint.renderedContent`) + raw output into `ws["intel_ai"]["last_trace"]`
    (per section), shown in the panel — a debugging aid; it enables Gemini `includeThoughts` (slower).
    ⚠️ Google's grounding ToS asks that Search Suggestions be shown to end-users; currently rendered
    only in the admin trace panel (client-facing display is a TODO). Each run is **ADDITIVE**:
    `workspace.add_auto_intel` de-dupes new stories and APPENDS them (list grows, never wiped;
    plain-auto capped 60/section, manual + favourited always kept). Team edits via `POST
    /w/<c>/admin/intel` ops: `ai_settings` (model/prompts/window/count/show_thinking), `topics`,
    `suggest` (the panel's "Write these for me" — `intel_ai.suggest_config` AI-drafts the keywords +
    both focus prompts from what the workspace knows about the client — campaigns/website/watcher
    industries via `main._intel_client_context` — grounded on a live Google lookup when the model is
    Gemini; returns the drafts WITHOUT saving, the panel fills the fields for review + Save), `refresh-now`,
    `bulk` (mass delete / favourite — favourite stars + pins), plus add/edit/delete. **Gated:** the
    job no-ops unless `INTEL_AUTO_ENABLED=1`; it REUSES the platform-dash image + web SA. New infra:
    the scheduler job (impersonates the **web SA**, not the cloudscheduler service agent — owners
    can't actAs that agent) + `roles/aiplatform.user` on the web SA + the optional `DEEPSEEK_API_KEY`
    and `KIMI_API_KEY` secrets. Redeploy `services/portal/dash/deploy_intel_refresh.ps1` (`-Disable` OFF, `-Run` fires
    now; **rerun after any `intel_feed`/`intel_refresh`/`intel_ai` change** — image-pinned) AND
    `deploy_dash_platform.ps1` (the web service's Refresh-now runs `refresh_client` in-process).
    Off-cloud tests: `dash/_intel_feed_localtest.py` + `dash/_intel_ai_localtest.py` (inject fetchers).
- **Reports is a CLIENT-VISIBLE, TEAM-GENERATED tab (every meeting deck, date-first; 2026-07-29):**
  a `reports` nav tab (between Communications and Tasks) listing every presentation as a card whose
  face is the presentation DATE (newest first); clicking opens a **self-contained HTML deck** — a
  fixed **1280x720 stage** scaled to the window, one slide at a time, arrow keys / click / dots to
  move and `p` to print to PDF; no external assets, and its ONE inline script (the slide navigator)
  is esprima-4.x-safe like every other script here. Served ONLY through the authed
  `GET /w/<c>/report/<id>` (no-store; a missing object re-renders lazily from the stored payload,
  so a Trash restore never 404s).
  - **The payload is slides of BLOCKS (rebuilt 2026-07-29 — the old fixed six-slide shape could
    only hold `{title, body}` bullets, so every chart, table and before/after collapsed into
    prose):** `{meta, facts, slides:[{kind, eyebrow, title, subtitle, tone, source, blocks}]}`.
    `kind` = cover | section | content | closing; `blocks` = text | bullets | cards | callout |
    **action** ("We'll action: …") | **panel** (a titled reading of the figure next to it) |
    **split** (two evidence objects side by side, one level deep) | chips | kpis | chart | table |
    compare.
  - **Density is the point.** A hand-built deck runs ~160 words and ~19 numbers per slide because
    every figure is paired with what it MEANS; the first rebuild averaged 94 because the prompt
    capped a slide at "one idea, at most 4 blocks" and the renderer could only stack blocks
    vertically. `split` + `panel` + a prompt that asks for 4-6 blocks and 120-180 words per content
    slide closed most of that gap (measured 136/17). If you touch the prompt, keep the density
    instruction: it is the difference between a deck and a list of charts.
  - 🔴 **A number in a visual is never model-written.** `build_facts(dash_data)` derives the whole
    numeric pack in plain Python from BOTH dashboard shapes — headline tiles (primary + a secondary
    strip), weekly series (revenue / ROAS / order value / spend / CTR), a last-14-days-vs-prior-14
    **compare**, ranked tables for ads, campaigns, age, gender, region and ActiveCampaign email,
    and the **opportunity facts** a media buyer actually acts on: `reallocation` (what the
    expensive half of the age curve would buy at the cheap half's rate — "the same $715, +377
    clicks"), `segments` (age crossed with gender, so the best cell in the account is findable),
    `pressure` (last week's CPM/CPC/CTR against the flight average) and `bench` (how many
    creatives carry the account, and how concentrated). **The template `kpis`/`daily` shape — every
    client except the Windsor-live ones — gets the same treatment:** headline tiles, a weekly series
    per metric (formatted by what the column NAME says it is: money, rate or count), a generic
    like-for-like window and a `momentum` table (every metric's last complete week against its own
    average). Before that it had only tiles plus four charts, and a normal client's deck came out
    FOUR slides long.
  - 🔴 **The workspace key is NOT the dashboard key — this is why a live deck came out 3 slides.**
    The console derives a workspace key from the display name (`Riverdance RV` → `riverdance-rv`)
    while the dashboard stack was stood up under a short key (`riverdance`). So
    `agora-data-driven-<client>-dash` existed for **no client**, `read_client_dash_data` swallowed
    the 404 (it degrades silently by design), and the KPI export vanished from BOTH the report fact
    pack and the Assistant's index for every client. `assistant_ai.dash_data_key(client,
    dashboard_url)` now resolves it from `ws["dashboard_url"]` — the embed the Dashboard tab already
    renders, and already correct — parsing both Cloud Run host forms and a `<c>.agoradatadriven.com`
    custom domain, falling back to the client key. Both call sites in `main.py` pass the URL. Fixing
    this took the riverdance deck from 3 slides to 14 off the real export.
  - 🔴 **Ranked tables need a volume floor** (`_MIN_SHARE` 1% of spend / `_MIN_CLICKS` 30). On the
    real breakdown, `18-24, unknown` — 0.0% of spend, 4 clicks — won "best audience cell" at $0.18
    a click, which is a targeting recommendation built on noise. Cells under the floor are dropped
    from `segments` (the subtitle says so) and can never be crowned best/worst anywhere. The floor
    is opt-in per table: a row declaring no volume signal stays eligible, so spend-ranked tables
    keep their marks.
  - 🔴 **Brand marks are declared ONCE as CSS custom properties** (`--crest` / `--agoramark` via
    `mark_css_url`), never markup repeated in each slide's chrome. Inlining them per slide made a
    3-slide deck **1.9 MB** — a 14-slide one would have been ~8 MB, on a `no-store` route. After the
    fix, 14 slides weigh 705 KB.
  - 🔴 **Two arithmetic traps, both fixed with regression tests — do not reintroduce them.**
    (1) A flight almost always ends mid-week, so the final weekly bucket is PARTIAL. Charting it is
    right; comparing it to a full-week average printed "-80%" on every metric. Everything that
    compares weeks (`pressure`, `momentum`) filters to complete weeks, which is why `_weeks` /
    `_daily_weeks` return day counts. (2) A fixed weekly budget is a FLAT series where every point
    ties for max, so `_series` marked all thirteen weeks "BEST". A standout now needs a real spread
    and a single winner.
    A `chart`/`table`/`compare`/`kpis` block carries only a **fact KEY**; the renderer draws from
    the stored fact, and `normalize_payload` DROPS a block whose key is unknown or whose kind does
    not match (an invented key renders nothing, never a wrong number). The facts ride inside the
    payload, so the lazy re-render is identical forever. Table tone (best/worst) is stamped in the
    fact, and every tone also carries text, so the deck reads in grayscale.
  - **It wears the CLIENT's identity:** `brand_kit(ws)` takes their crest from
    `ws["brand"]["client_logo"]` and parses the deck palette out of the Company tab's brand guide
    (`company.brand.colors` — the hex codes in that free text; blank = the AGORA house palette).
    🔴 **Sorted by ROLE, not by position:** a brand list almost always includes a cream/off-white,
    and taking "the second colour" as the secondary accent puts unreadable near-white type on the
    slide. Only colours dark enough to read on paper become accents; a very light one becomes the
    canvas the slide sits on. Logo markup is inlined verbatim, so `_mark` gates it to our own
    self-contained `<svg>`/`data:` `<img>`.
  - `report_ai.py` owns all of it: `gather` pulls the fact pack + the SAME distilled layer the
    Assistant reads (digest sections, company brief, intel entries, watcher summaries, board asks),
    `generate` has the configured model write the slides (it is told to make every title a CLAIM,
    one opportunity per slide, each with its own `action`), `revise` applies an edit instruction
    (the Assistant's edit-report action; the fact pack is stripped from what the model sees and
    re-attached, so an edit can never lose the numbers), `render_html(…, brand=)` renders any
    payload. **No model ⇒ a real deterministic deck** (the facts alone carry the numeric story),
    with no invented analysis — and it PAIRS facts into `split` slides, then sweeps up every fact
    its running order does not name, so a metric that is computed always reaches the deck (the
    first version's hardcoded Windsor key list silently dropped every template-shape series). A deck stored under the pre-rebuild payload still renders
    (`_legacy_slides`). State: `ws["reports"]` index entries (payload included, so decks re-render
    and the Assistant indexes what we told the client) + per-deck HTML objects
    `workspace/reports/<c>/<id>.html` (`workspace.add/update/delete/insert_report`,
    `read/write_report_html`). Team ops `POST /w/<c>/admin/report` (op generate|rename|delete —
    delete soft-deletes to the Bin, kind `report`); clients read only. Off-cloud test:
    `dash/_report_localtest.py` (in CI, also covers `digest.py`; it parses the deck's navigator
    with esprima too).
- **Task tracker = the internal Delivery board + the client Tasks tab, ONE data source**
  (spec: `TASK_TRACKER_INTEGRATION.md`, extended 2026-07-14 with the two-level breakdown +
  dates/charge): `ws["tasks"]` per client — a task ("service") is a deliverable travelling
  `todo → in_progress → blocked → revision → completed` (stage KEYS are canonical, never rename;
  both surfaces show the same `TASK_STAGE_META` labels. For Review + Waiting for Client were
  REMOVED 2026-07-29 — both just meant "blocked on someone" — and Blocked moved up beside
  In Progress; retired keys, incl. the pre-2026-07-27 four-stage set, land on a live column via
  `workspace._STAGE_ALIASES`). `workspace.py` is
  the only writer (`add_task`/`update_task`/`move_task_stage`/`delete_task`/`insert_task` +
  main-task, sub-task and comment helpers). Work is a **two-level breakdown**: `maintasks[]`, each
  a named group with its own owner and its own `subs[]` (sub-tasks that each carry their own
  owner); a legacy flat `subtasks[]` is migrated in place by `workspace.normalize_task` (called by
  `_find_task`, so every mutation persists it) and `task_subtasks()` flattens for counts/guards.
  Each task also carries a **lead + support people** (`lead_id` / `support_ids`, roster = active
  admin accounts from `store.py`; the lead is never duplicated into support; support is assigned
  AFTER creation — the picker renders only on the Edit form, guarded by a `has_support` form
  field), **`start_date` + `due_date`** (the LAUNCH date — key canonical, UI label "Launch date"),
  a **service-template-seeded work breakdown** (`services/portal/dash/service_templates.py`: the
  New-Service form's **Service type** picker — Acquisition = ONE type "Google / Meta Campaign" with
  a Video/Static/Carousel **ad-production picker**, everything else a fixed recipe with optional
  qty/platform params — auto-generates `maintasks[]`+`subs[]`, each sub with an INTERNAL `dod`
  "done when"; seeded on op=add only, "Custom (blank)" keeps the empty-card path; the client
  Progress shape strips `dod`),
  an internal-only **`service_charge`**, and a single **label AUTO-derived from the department**
  (`main.TASK_DEPT_LABEL`: Acquisition→Paid Media, Lifecycle→Organic, rest→Website — no manual
  label picker; the form's one name field is LABELED "Campaign" but stores as `title`). **Stage
  moves are UNGUARDED (2026-07-28):** a move to `completed` used to be refused while a sub-task was
  open, a change request unresolved, or the service had no steps at all — a refused drop reads as a
  broken board, so the blockers are now only SURFACED (progress bar, "Changes requested" tag), never
  enforced. The **team board** is a console pane (Delivery → Task
  Board in `admin_atrium.html`): cross-client stage columns collected in `admin_atrium()` from the
  already-loaded workspaces (no extra reads), columns sorted **Urgent-first then launch date**,
  drag-to-move, client/department/person/priority filters, and per-task detail/edit/new overlays
  SERVER-RENDERED into a hidden store (no JSON-in-JS; plain forms post with `redirect=console`).
  The detail overlay is **tabbed** — a persistent summary (stage pill + glance chips: priority /
  start / launch / charge / progress) above **Details | Tasks | Comments** panels (`data-tktab`
  buttons, wired in the console script); the New/Edit form tucks optional fields into a
  collapsible **"Additional details"** `<details>` (auto-open when an edited task uses them).
  Team routes: `POST /w/<c>/admin/task{,/move,/delete,/maintask,/subtask,/comment}` gated
  `is_superadmin()` (`/maintask` op=add|assign|delete; `/subtask` op=add takes a `maintask_id`);
  deletes soft-delete to the Bin (`kind:"task"`, restored via `workspace.insert_task`) and every
  mutation `_audit`s. The **client Tasks tab** (nav LABEL "Tasks" since 2026-07-29; the tab key
  stays `progress` in ATRIUM_TABS/routes — canonical, never rename, same posture as `leadgen`;
  pane in `atrium.html`) renders `main._progress_tasks(ws)` — SERVER-FILTERED to `client_facing` tasks and
  client-safe fields only (lead/support/main-task/sub-task owners, priority, `service_charge`,
  `internal_notes`, and the account manager NEVER reach the client's HTML); the two-level
  breakdown reaches the client as **phases** (name + steps, no owners), the detail modal shows a
  **Started → Going live timeline**, cards say **"Launching <date>"** ("Live" once launched), and
  columns sort by soonest launch. **The TEAM also gets drag-to-move + a per-card delete ✕ on this
  same board (2026-07-28)** — `{% if is_superadmin %}` markup only (draggable `.ax-pg-cardwrap`
  wrappers, `data-pgcol` drop targets, `data-pgdel` buttons), posting the EXISTING
  `/w/<c>/admin/task/move` + `/admin/task/delete` routes; a client's HTML carries none of it, so
  their board is as read-only as it ever was (asserted in `_atrium_smoketest.py`). The row of
  per-stage count TILES above the board was removed in the same change — the column heads already
  showed the same numbers. ⚠️ Don't style team affordances with an `[data-admin="1"]` selector: the
  stylesheet ships to every viewer, so that literal string then appears in a client's HTML and trips
  the no-leak check. TWO client-surface writes: `POST /w/<c>/task-comment`
  (comment / request-changes — a `kind:"changes"` comment flags the task on BOTH surfaces;
  resolving is team-only, `op=resolve`, which also notifies via the `notify.py` task functions)
  and `POST /w/<c>/task-add` (the Progress tab's **quick-add composer**, rendered for client AND
  team — built for live-call capture: share the workspace on screen and type requests as the
  client says them. ⚠️ **The composer is a REAL `<form>`, not a JS widget** — a native post
  carries `redirect=progress` and gets a redirect back to the tab; the fetch path omits it and
  gets JSON. Both are first-class ON PURPOSE: the first build wired the composer inside the
  Progress-board IIFE, so it shared that block's `if (!root || !veil || !storeBox) return;`
  guard and died SILENTLY — no request, no error, no alert — whenever that board furniture was
  missing. Filing a request must never depend on JS running at all. The **reporter is
  auto-tagged from the session** (`reporter` agora|client +
  `reporter_name`, stored by `workspace.add_task`, never a form choice); quick-added tasks are
  always client_facing, start in_process with no breakdown, and accept NO internal fields —
  priority/charge/owners stay console-only. Client-filed requests show a "Requested by <name>"
  chip on Progress and a "Client req" pill + overlay chip on the console board; a client add
  fires `notify.client_task_added`).
  **SENTINEL edits these cards too — the internal task bridge is two-way** (`/api/internal/task*` in
  `main.py`, HMAC-gated by `_internal_gate`, same scheme as the Watcher bridge): Sentinel's board
  LISTS them (`GET /api/internal/tasks`, cross-workspace), and since 2026-07-29 also **opens, edits,
  deletes and comments on** them — `GET /api/internal/task` (full card + the roster/department/stage
  vocabularies), `POST /api/internal/task-{update,delete,comment}` (+ the older `-move`, `-add`).
  Purposes: `tasks` · `task-detail` · `task-update` · `task-delete` · `task-move` · `task-add` ·
  `task-comment`. Every one goes through the **same `workspace.py` helpers the console's own forms
  call**, so the stored shape, the derived label, the history entries and the Bin behave identically
  whichever app the edit came from (a delete soft-deletes to the console Bin, credited to the
  Sentinel user; `set_task_maintasks` is the array-shaped breakdown setter Sentinel's drawer needs,
  and it re-mints foreign ids + preserves the internal `dod` the other side can't see).
  Fail-CLOSED like every internal route. Covered in `_atrium_smoketest.py` + `_workspace_localtest.py`.
- **Company is a CLIENT-VISIBLE, TEAM-WRITTEN tab (who the client actually is; 2026-07-29):** ONE
  workspace key `ws["company"]` holding the agency's answer to "who are we working for?" — the
  **profile** (at-a-glance facts: `one_liner`/`industry`/`founded`/`hq`/`website`/`size`/`customers`),
  the **brand** guide (`tagline`/`voice`/`tone`/`personality`/`colors`/`fonts`/`dos`/`donts`/
  `assets_url`), an ORDERED **`sections[]`** story (`{id,heading,body}` — About / History / Mission /
  Positioning) and an ORDERED **`products[]`** catalogue (`{id,name,summary,price,audience,url,
  status}`). The two lists are **hand-ordered, not date-sorted** (a company story reads top to
  bottom), so `add_company_item` APPENDS and `workspace.move_company_item` is a first-class writer.
  `workspace._ensure_company` normalizes on every read, so an older workspace upgrades silently and
  the template never needs a `default` filter (`company_profile` is the shaped read helper,
  `company_is_empty` drives the empty state). Same posture as Market Intelligence: the tab and every
  field are **client-visible in full** (it is the client's OWN company); only the EDIT affordances
  and the route are team-gated. Route `POST /w/<c>/admin/company` (`op`
  profile|brand|add|edit|delete|move|**draft**, `kind` sections|products, gated `is_superadmin()`);
  `profile`/`brand` patch **only the fields the form carried**, so a partial post can never blank the
  rest, and a delete **soft-deletes to the Bin** (kinds `company_section`/`company_product`,
  restored via `insert_company_item` — a story section is real written work).
  **`op=draft` = "Draft with AI"** (`intel_ai.draft_company`, mirrors intel's `suggest`): the model
  researches the company — grounded on a live Google lookup when the model is Gemini — and returns
  drafts that the panel writes INTO the on-screen fields, **saving nothing**; the team reviews and
  hits Save per block. A field it cannot establish comes back empty ON PURPOSE (a plausible guess is
  a lie the team would act on).
  🔴 **This tab is what the AI knows the client BY.** `digest.company_sections` derives it into
  titled chunks (facts / brand / each story section / the catalogue) consumed by BOTH the Assistant
  index (kind `company`, `INDEX_VERSION` 5) and `report_ai.gather`, so "what the AI knows" and "what
  we present" stay in agreement. The chunks are deliberately UNDATED — a date-range scope on
  transcripts must never make the client's own identity invisible. The Assistant can also PROPOSE
  `add_company_section` / `add_company_product` / `set_company_facts` (approval-gated like every
  other action). Tests: `_workspace_localtest.py` (writers, patch semantics, ordering, Bin round
  trip) + `_atrium_smoketest.py` (routes, the client no-leak render, indexing, the nav grouping).
- **Nav labels vs tab keys:** the sidebar was regrouped 2026-07-13 and again **2026-07-29** — the
  `leadgen` tab is LABELED **"Paid Media"** and the `progress` tab is LABELED **"Tasks"** (the keys
  `leadgen` / `progress` stay in every route/data shape, never rename them). The top level is
  **FOUR** rows, organised by the question each answers — one flat link plus three groups:

  | Row | Answers | Holds |
  |---|---|---|
  | **Working Together** | how is it going? | Dashboard · Communications · Tasks |
  | **Company** | who are we working for? | (flat) |
  | **Campaigns** | what is going out? | Paid Media · Organic Content · Content Calendar |
  | **Insights** | what have we learned? | Market Intelligence · Reports · team-only Website Health + Watcher |

  Three moves got it from eleven flat surfaces to four rows: the Content Calendar joined Campaigns
  (it IS the campaign content plotted by date — a dated content piece literally mirrors into it),
  Reports joined Insights (a deck is the synthesis of what that group holds), and Dashboard +
  Communications + Tasks became **Working Together** — the live state of the engagement (results,
  work in flight, the conversation) as against the static who-you-are and the content pipeline.
  🔴 **Working Together is FIRST because it holds `dashboard`, the LANDING tab** (a bare `/w/<c>/`
  resolves to it): moving the group down the list would bury the landing tab inside a collapsed
  group, and the rail would open with no active item on it. Campaigns' head badge is the combined
  awaiting count. Group heads are expand/collapse buttons only (auto-open when a child tab is
  active, in Jinja via each group's `*_open` guard and client-side in `showTab`); the collapsed icon
  rail and the phone strip flatten the groups away. The **Assistant nav tab was removed** — the
  floating bubble (FAB) is the chat surface; the `/w/<c>/assistant` route + pane still exist
  (reachable by URL, keeps the date-range + reindex controls).
- **Routes (all behind existing session auth):** client `GET /w/<c>/` + `/w/<c>/<tab>` (overview,
  dashboard, company, leadgen, organic, calendar, conversations, intel, reports, progress, settings)
  gated `authed()`+`can_open(<c>)`;
  client POSTs `/w/<c>/{approve,request-changes,save-note,comment,send-message,save-notify,logo}` +
  creative GET above; team-only POSTs `/w/<c>/resolve-comment` + `/w/<c>/admin/*` gated `is_superadmin()`. The team console
  (`GET /admin/atrium`, gated `is_superadmin()`) is a **focused console** (the old "Your Agora suite"
  Home hub was removed 2026-07-17 — the console is the only view now). The **app switcher** (Atrium /
  Sentinel / Website Editor — Skill Mastery lives inside Sentinel, so it is not listed) lives in the
  **account dropdown under the username** at the bottom of the rail, and the **Agora logo links back
  to `agoradatadriven.com`**. Grouped rail *Workspaces* (Clients · Activity · Bin) / *People & access*
  (Accounts with subtabs Requests · People · Add new) — all client-side view state, so `?section=`/flash
  redirects still land on the right pane. The Clients pane shows one card per client (the worked-example `template` client is
  filtered out) with an attention chip (purple **"N awaiting approval"**, attention-first sort, or
  green **"All caught up"**). **Clicking a card opens that
  client's workspace `/w/<c>/` directly** (where all editing happens in place). Each card also carries
  an **Upload logo** control (POST `/admin/atrium/<c>/logo` — embeds the image inline as a
  `brand.client_logo` `<img>` data-URI, ≤512 KB; same posture as seeded logos) and a confirmed
  **Delete** control (POST `/admin/atrium/<c>/delete` — `store.remove_client` +
  `workspace.delete_workspace`) and a **Rename** control (POST `/admin/atrium/<c>/rename` —
  display name only, updates the registry `name` + workspace `display_name`; the key `<c>` and
  every derived resource never change). **Add a new client** (`POST /admin/atrium/new`) asks ONLY for a
  display name (key auto-derives, password auto-generates) and on success redirects STRAIGHT to the
  new client's blank `/w/<c>/`. The
  portal landing (`/`) shows **Open dashboard** per client; the workspace `/w/<c>/` stays reachable
  directly and from the console.
- **Auth foundation (central Google sign-in + impersonation):** the portal is the ONE app that runs
  Google OAuth (`google_oauth.py`, `/auth/google/{login,callback}`) and, on a verified email, mints
  the SAME session + shared `ag_sso` cookie as a password login — so the website editor and every
  dashboard trust a Google login identically. OPT-IN: off unless `GOOGLE_OAUTH_CLIENT_ID`/`_SECRET`
  are set. **Sentinel is the source of truth for staff:** on a verified email with no active portal
  account, the callback defers to Sentinel (`sentinel_directory.py` → Sentinel's HMAC-gated
  `/api/internal/user-lookup`) and signs in any **active Sentinel user** with no client-dashboard keys
  — so adding someone in Sentinel (People → Add Employee) enables their Google login with no portal
  record to maintain, and deactivating them there blocks it. An email authorized nowhere files a
  **passwordless pending request** (`/auth/request-access`) an admin approves in the console's
  Access-requests tab via `POST /admin/accounts/grant-google` (assign to a new/existing client OR a role). `/admin/atrium` IS the admin landing (`/` redirects here; the legacy
  `/admin` + `/superadmin` pages now just redirect here too). THE super admin (`info@…` / role
  `superadmin`) can **act as any user** (`/admin/impersonate`; a site-wide "Stop acting as" banner is
  injected by the `after_request` hook). Full details + OAuth/secret setup: `services/portal/dash/CLAUDE.md`.
- **Strategy doc → AI strategy (optional, opt-in):** an admin attaches a Google Doc to a campaign and
  clicks "Generate strategy". `dash/atrium_docs.py` reads it (public-export fetch by default, or the
  **Google Drive API** when `ATRIUM_DOCS_ENABLED=1`) and `feedback_ai.summarize_strategy_sections`
  (Claude `claude-opus-4-8`, the existing `FEEDBACK_AI_ENABLED`+`ANTHROPIC_API_KEY` gate) writes the
  three **What / Why / What-next** strategy sections; they stay hand-editable. Every step degrades
  gracefully (no AI → doc excerpt in "What happened"; unreadable doc → ok:false with share guidance;
  no doc → empty, the admin types it). ⚠️ The Drive-API path is a **deliberate, opt-in deviation** from
  "no new infra": it needs the Docs/Drive API on + `google-api-python-client` in `requirements.txt` +
  the doc shared with the runtime SA. **A default deploy stays infra-free.**
- **Notifications are optional & graceful** (`dash/notify.py`, mirrors `feedback_ai.py`): default
  records an activity entry + logs to stdout; real email only when **both** `ATRIUM_EMAIL_ENABLED=1`
  and `ATRIUM_EMAIL_API_KEY` (Secret-Manager) are set, SDK imported lazily. **No provider key
  committed.** Team inbox `ATRIUM_TEAM_EMAIL` (default `info@agoradatadriven.com`).
- **Super-admin audit feed + restorable Trash (`dash/audit.py`):** ONE new private JSON
  `audit.json` in the SAME registry bucket (no new bucket/service/IAM — mirrors `store.py`: GCS
  default, local-fs via `REGISTRY_LOCAL_DIR`). Two lists: **`activity[]`** — every admin/client
  action across all workspaces (`{ts,client,actor,role,action,detail}`, capped 500, newest first),
  written by a one-line `_audit(client, action, detail)` call from each mutation route in `main.py`
  and surfaced in the super-admin console's **Activity** tab (each `_audit` also fires
  `notify.activity_alert`, an OPTIONAL email reusing the dormant transport). **`trash[]`** — major
  deletions (content, campaign, personal calendar event, whole client) are soft-deleted: the delete
  route stashes the removed payload via `_trash(...)` before deleting, and the **Trash** tab lists
  them with a **Restore** button (`POST /admin/atrium/restore` → `workspace.insert_content`/
  `insert_campaign`/`insert_calendar_event` or `store.restore_client` + `save_workspace`). Entries
  older than **30 days** are purged automatically whenever the trash is read/written (lazy purge —
  the no-infra equivalent of a scheduled job, since the app is request-driven). Both lists are
  best-effort (swallow storage errors) so logging/trashing can never break the action.
- **Theme/JS:** the official brand **light** theme, standardized 2026-07 on the WEBSITE design system —
  Data Green `#4FA84A` + Accent Purple `#6A6AEA` (deep companion `#5A54DD` for white-text fills), on a
  white canvas with bold black type; green = primary action, purple = informational. The whole
  front-door (login, portal, team console) shares it (`dash/brand.py` + `assets/brand.json` are the
  palette source); the Atrium **client workspace** keeps its original design for the PANE BODIES by
  decision (2026-07-10), scoping every selector under `.atrium` so it stays self-contained — **except
  the chrome (top bar + sidebar), re-skinned to the admin theme 2026-07-21** at the user's request:
  admin fonts (Archivo display / Lato sans via `--ax-font-display`/`--ax-font-sans` chrome tokens),
  Data Green `#4FA84A` active-nav tint + brand-700 text, Accent Purple `#6A6AEA/#5A54DD` counts/avatar,
  the header underline gradient green→purple (`--ax-grad-chrome`), larger admin radii. The pane bodies
  still use the original `--ax-violet` blue accent (full body re-skin stays parked). Same round: the
  **top bar became a title + subtitle header** (`#ax-subtitle` + `tab_subtitles` Jinja map / `subtitles`
  JS map) and the **duplicate per-pane `<h1>` was removed** where it merely repeated the tab title
  (dashboard/conversations/intel/progress/settings moved their lede into the subtitle; website-health/
  watcher/assistant kept their verbose in-pane lede, only the `<h1>` dropped). Overview (greeting) and
  calendar (project name) keep their distinct `.ax-greet`. The logo is `ws.brand.agora_logo`
  (seeded) in Atrium and `dash/brand.py`
  elsewhere. Inline JS is esprima-4.x-safe and reads state from the DOM (no Jinja in any script block).
- **Ships via the SAME deploy as the portal:** `services/portal/dash/deploy_dash_platform.ps1` (build
  as yourself → `gcloud run deploy platform-dash --no-invoker-iam-check`). Validate templates with
  `tools/_validate_dash_js.py` first. Seed the demo once:
  `.\.venv\Scripts\python.exe services\portal\dash\seed_workspace.py` (idempotent; writes
  `workspace/riverdance.json`, refuses to clobber). Local tests: `dash/_workspace_localtest.py`
  (data) and `dash/_atrium_smoketest.py` (full route+template, stubs GCS).
- **Local preview (no-password, for devs):** double-click `preview/Preview Portal (admin).cmd` — or run
  `services/portal/dash/run_local.ps1`. It serves the whole front-door at `http://localhost:8080` from
  an isolated `.venv-portal` + throwaway `.local_portal_data` (never the real bucket/ADC), seeds demo
  clients (`dash/seed_local.py`), and auto-signs-in as super-admin so there is NO login and every
  workspace is editable in place. `preview/Preview Portal (client login).cmd` shows the real login on
  `:8081`. The no-auth is `PORTAL_DEV_NOAUTH=1`, honored by a `before_request` hook in `main.py`
  **only when `PORTAL_SECURE_COOKIES=0`** — so it can never activate in the https deploy.

## The data contract (three stages, matched BY NAME)

```
sql/*.sql  (view column)  ->  job/main.py  (assembled `data` dict key)  ->  dash/dashboard.html  (data.* key)
```

Adding a metric is usually three edits, one per stage. **Renaming a key in one stage breaks the
next** — the names must match exactly. For `template` the chain is: `kpi_overview` /
`daily_performance` columns → `data["kpis"].*` / `data["daily"][].*` → `data.kpis.*` / `data.daily`.

**Exception — `client_riverdance` is a Windsor-LIVE client (no BigQuery/SQL views).** Meta's rich
fields (reach, link clicks, pixel-purchase bookings, revenue) and the creative images/copy are NOT in
the shared `raw_windsor` mirror, and Meta rejects revenue on a breakdown query — so this client's
export job pulls the Windsor connector API **directly** each run (main per-ad/day pull + separate
age×gender + region breakdown pulls) and writes `riverdance.json` itself. There is no `sql/`, no
dataset, and no freshness watermark; refresh is **automatic** — the `sync-refresh` Cloud Run job
(`services/portal/dash/sync_refresh.py` → `sync_dash.trigger_all`) runs on a Cloud Scheduler tick
(every 6h, `sync-refresh-6h`) and triggers every `<c>-export` job via the Run Admin API. This
REPLACED the console's manual "Sync all dashboards" button (removed 2026-07: a browser refresh must
never trigger paid Windsor/Meta pulls); the console now shows a read-only "Last synced: Xh ago" from
the same `sync_state.json`. Deploy/schedule it with `services/portal/dash/deploy_sync_refresh.ps1`
(gated `SYNC_AUTO_ENABLED=1`, reuses the platform-dash image + web SA; `-Run` fires once now,
`-Disable` turns it off). ⚠️ The trigger POSTs `:run` **with env overrides** (`FORCE_REBUILD=1`),
which needs `run.jobs.runWithOverrides` — `roles/run.invoker` does NOT carry it, so the web SA
must hold **`roles/run.developer` on each `<c>-export` job** or every tick 403s while the IAM
policy looks correct (riverdance sat 13 days stale this way, found 2026-07-27; the standup
scripts' Step 6 now grants run.developer). The dash service runs OPEN (no login) so it embeds in
the gated Atrium. Stand it up with `clients/client_riverdance/deploy_riverdance.ps1`. Treat it as the
pattern for any connector whose data isn't (yet) flowing through `raw_windsor`.

## Redeploy after an edit — MANUAL, never cloudbuild from a laptop

Deploys are manual: build the image as yourself, then deploy. A laptop must **never** trigger Cloud
Build to deploy, because the Cloud Build SA cannot `iam.serviceAccounts.actAs` the runtime SA
(`gcloud builds submit --tag` to build an image is fine; it is the *deploy-as-the-runtime-SA* step
that fails). Use the per-stage scripts (all resolve paths from `$PSScriptRoot`, all idempotent):

- **View/SQL change** → `clients/client_template/sql/deploy_views_template.ps1`
  (reapplies views via `create_views.py`, then re-runs the export job with `FORCE_REBUILD=1`).
- **Job / data-assembly change** → `clients/client_template/job/deploy_job_template.ps1`
  (build image → `gcloud run jobs deploy template-export` → execute with `FORCE_REBUILD=1`).
- **Dashboard / web change** → `clients/client_template/dash/deploy_dash_template.ps1`
  (validate JS → build → `gcloud run deploy template-dash … --no-invoker-iam-check`).
- **Full standup of a new client** → copy `client_template`, then `deploy_template.ps1`.
- **Portal / Atrium change** → `services/portal/dash/deploy_dash_platform.ps1` (fast redeploy) or
  `services/portal/deploy.ps1` (full standup). **Ingest jobs** → `tools/deploy_ingest_jobs.ps1`.
  **Status dashboard** → `services/status-dashboard/deploy_status.ps1`.
  **Automatic dashboard sync** → `services/portal/dash/deploy_sync_refresh.ps1` (the `sync-refresh`
  Cloud Run job + 6-hourly scheduler that replaced the manual "Sync all dashboards" button; rerun
  after any `sync_refresh`/`sync_dash` change, image-pinned).

`FORCE_REBUILD=1` is mandatory for view-only / code / seed changes: they do **not** advance the
upstream watermark, so without it the freshness gate no-ops and keeps serving stale JSON.

Org policy (Domain Restricted Sharing) rejects `--allow-unauthenticated`; all web services deploy
with `--no-invoker-iam-check` and do their own password/SSO auth in-process.

## Team workflow (branch → PR → CI → merge)

Multiple developers (each with their own Claude Code) work in parallel. To keep merges clean, follow
**`docs/dev-workflow.md`**: each machine pushes to its own branch with `tools/push-branch.ps1`, opens a
PR (CI runs the gates in `.github/workflows/ci.yml` — esprima JS gate, `py_compile`, the off-cloud
Atrium tests), and only green PRs merge to `main`.

**The release SOP is agent-driven:** a developer drops `tools/merge-branches.ps1` into Claude Code and
asks it to merge + deploy. The script runs the whole pipeline to live — fetch → `integration/merge`
off `origin/main` → run the CI tests → **land on `main`** → **auto-detect which services changed and
deploy each** (the path → deploy-script mapping lives in the script's `Resolve-DeployPlan`) → prune the
merged branches. It STOPS only where judgment is needed — a real merge conflict or a red test — and
hands off to the agent (see the AGENT RUNBOOK header in the script); the agent resolves it and re-runs.
**On a conflict the script leaves the conflict IN THE TREE and stops; resolve it, then
`git add -A; git commit --no-edit`, then re-run with `-Resume`** — `-Resume` continues the stopped
run from the integration branch (it expects a clean tree on `integration/merge`; never bypass the
conflict any other way). **`wip/*` branches are PARKED work** (shelved via `agora-park.ps1` in
`agora-devtools`): merge-branches explicitly skips them and never prunes them — a parked branch's
existence on origin IS the parked state, so parked work is visible everywhere and shipped nowhere.
`-DryRun` previews the land+deploy plan without changing anything; `-NoPush`/`-NoDeploy` recover the
review-first behavior; `-DeleteMerged` is the standalone prune. **Note:** enabling GitHub branch
protection on `main` (PR-required, per `docs/dev-workflow.md` step 5) would block this direct-to-main
land — keep protection off, or run with `-NoPush` and merge via PR, if you turn it on.

## Freshness contract (binding)

1. **Self-gating on a tick.** Each client export job (and the status dashboard) runs on its Cloud
   Scheduler tick (`*/10 * * * *` for exports, `*/15` for status) but only rebuilds when the shared
   `raw_windsor` mirror tables it reads advanced past a stored watermark. The Windsor ingest jobs
   are NOT self-gating — they are scheduled API pulls that WRITE `raw_windsor`.
2. **The watermark is a sidecar in the client's OWN bucket** — a `_freshness.json` object in
   `agora-data-driven-<c>-dash`. There is no separate freshness store and no database.
3. **Probe the BASE/MIRROR tables the views read — never watermark a VIEW.** A view has no
   last-modified time; watermark the `raw_windsor` mirror/base tables the views select from.
4. **`is_stale(observed, watermark)` returns True** if any observed upstream timestamp is newer than
   the watermark OR a probed key is absent. An **empty** observation set returns **False**, so a
   broken/empty probe never burns a rebuild.
5. **Write the watermark only AFTER a successful data upload.** `FORCE_REBUILD=1` bypasses the gate.

`freshness.py` signature (vendored identically into every export job):

```python
probe_bq_last_modified(bq, tables, location)       # __TABLES__.last_modified_time, keyed "dataset.table"
read_watermark(bucket, object_name)                # GCS JSON sidecar -> dict
write_watermark(bucket, object_name, observed)     # GCS JSON sidecar <- dict
is_stale(observed, watermark)                       # True if anything advanced or a key is missing
```

## Debugging — symptom → cause (checked before you go hunting)

These are the recurring ones. Each cost real hours; check here before reading code.

- **"My change isn't live" / "the browser is caching."** Probably neither. Deploys are
  **last-deploy-wins**: a teammate deploying a stale or divergent local tree silently overwrites
  your revision. **Check what is actually serving before blaming cache:**
  `gcloud run services describe platform-dash --project agora-data-driven --region asia-southeast1 --format='value(status.traffic[0].revisionName)'`
  Then confirm that revision's image is the one you built. (Same applies to every `<c>-dash`.)
- **SSE "network error" in the browser.** Almost always a **500 raised BEFORE the stream opened**,
  not a transport fault — the client only sees a dead connection. Build expensive context (index
  rebuild, retrieval) *after* the stream is open, behind a heartbeat, so failures arrive as an
  `error` frame instead of a broken pipe. This is why `ask_stream` retrieves inside the stream.
- **`/admin/atrium` 500s after someone hand-edited a workspace JSON.** On Windows, writing
  `workspace/*.json` in **text mode** encodes as cp1252, so a smart quote lands as `0x92` and the
  UTF-8 read (`store.py:87`) blows up. The app itself always writes bytes
  (`json.dumps(...).encode("utf-8")`, `store.py:102`) — the corruption comes from ad-hoc edits.
  **Always write these objects as UTF-8 bytes**, never via a text-mode handle or a shell redirect.
- **A Vertex-backed feature 429s or dies mid-run.** Two distinct causes: per-region quota
  (429 `RESOURCE_EXHAUSTED` — retry in another region or back off) and **context-cache expiry**
  (a cached handle silently stops resolving). Treat both as expected and degrade, don't crash;
  every AI leg in this repo is written to fall back to the non-AI path.
- **Watcher fetches return nothing / `blocked` from Cloud Run.** **YouTube blocks datacenter IPs.**
  That is the whole reason `WATCHER_PROXY_URL` (Secret `watcher-proxy-url`) and the operator-machine
  **Safe pull** path exist. A bare Cloud Run fetch is expected to fail — it is not a code bug.
- **Mail (dwd) returns nothing.** Domain-wide delegation needs a **Workspace-admin grant** that is
  separate from any IAM you can set from here. Unset/ungranted degrades to empty, by design.
- **`/go` reported success but a repo didn't ship.** `/go` **exits 0 on partial failure** — read the
  Summary block, not the exit code. See the root [`../CLAUDE.md`](../CLAUDE.md).
- **The esprima JS gate fails on valid-looking JS.** Inline JS must be **esprima-4.x-safe**: no `?.`,
  no `??`. Use classic `&&`/`||`. This applies to every `dashboard.html` and every portal template.
- **A view-only or seed-only change serves stale JSON.** Those changes don't advance the upstream
  watermark, so the freshness gate no-ops. `FORCE_REBUILD=1` is **mandatory** for them.

## Never

- **Never commit secrets.** Keys, `.p8`/`.pem`, `*credentials*.json`, `.env` are gitignored — keep
  it that way. Write secret material via UTF-8 (no BOM, no trailing newline) temp files.
- **Never write `workspace/*.json` or `platform.json` in text mode on Windows** — UTF-8 bytes only
  (see Debugging above).
- **Never make the data JSON public.** It is served only through the authenticated `/data.json`
  proxy. Buckets stay private.
- **Never edit views in the BigQuery console.** Views are code: edit `sql/*.sql` and reapply with
  `create_views.py`. The console is not the source of truth.
- **Never deploy via Cloud Build from a laptop**, and never use `--allow-unauthenticated`.

## Keep this file current

Updating docs is part of finishing a task — if a change alters the contract, the layout, or the
deploy steps, update this file in the same change. The root `CLAUDE.md` stays exactly one line
(`@AGENTS.md`) and `.claude/CLAUDE.md` stays a pointer to `../AGENTS.md` — never grow prose back
into either pointer. **Volatile status** (live URLs, dates, per-client deploy state) belongs in a
README, never in this file.
