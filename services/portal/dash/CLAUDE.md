# CLAUDE.md — services/portal/dash (the portal/CRM Flask app + Agora Atrium)

**Rules live in the repo-root [`/CLAUDE.md`](../../../CLAUDE.md)** — read it first; this file only
adds local context for this subtree. If they disagree, root wins.

You are in the **`platform-dash`** Cloud Run service: the portal/CRM front-door **and** Agora Atrium
(the co-branded client workspace). One self-contained Flask app, no build step.

- **`main.py`** — all routes (portal, Atrium client `/w/<c>/*`, admin `/w/<c>/admin/*` + the dark
  `/admin/atrium/*` console). `WORKSPACE_NAME` is the Atrium product-name constant.
- **`store.py`** — the registry (one private `platform.json`): clients **and** `accounts` (real
  email+password logins; role admin/client, status active/pending). `verify_portal_login` resolves
  super-admin env → account → legacy per-client hash → bootstrap. **`workspace.py`** — per-client
  Atrium state (`workspace/<c>.json`). Both import `google-cloud-storage` lazily and have a local-fs
  backend (`REGISTRY_LOCAL_DIR` / `WORKSPACE_LOCAL_DIR`) so they run off-cloud.
- **Sign-up + approval:** `GET/POST /signup` (Agora-branded `signup.html`) creates a **pending**
  client account; an admin approves it from `/admin/atrium` (`POST /admin/accounts/{approve,reject}`),
  which creates the client + blank workspace and activates the login. No public self-service access.
- **Google Sign-In (central; OPT-IN via `google_oauth.py`):** the portal is the ONE app that runs the
  OAuth flow (`GET /auth/google/login` -> Google -> `GET /auth/google/callback`), resolves the
  *verified* email (`_resolve_login_email` -> `store.resolve_google_login`), then establishes the SAME
  session + shared `ag_sso` cookie a password login mints -- so every dashboard AND the website editor
  trust a Google login identically. Authorization-code flow, confidential client, **no new dependency**
  (token exchange via `requests`; the id_token came over TLS so we decode it + re-check iss/aud/exp/
  email_verified, no JWKS). OFF unless `GOOGLE_OAUTH_CLIENT_ID`/`_SECRET` are set (login page hides the
  button; routes fall back to password). Redirect URI: `${PORTAL_BASE_URL}/auth/google/callback` (or
  `GOOGLE_OAUTH_REDIRECT_URI`).
  - **Authorization = `_resolve_login_email` (main.py), resolve order:** THE super admin -> `["*"]`;
    an **active portal account** (`store.resolve_google_login`) -> its client keys; else **defer to
    Sentinel** — `sentinel_directory.user_status(email)` asks Sentinel (the source of truth for
    staff) whether the email is an active user and, if so, authorizes them with `[]` (a valid session
    + `ag_sso`, no client dashboards). So **adding someone in Sentinel (People -> Add Employee) is all
    it takes to enable their Google login**, with no copy duplicated into `platform.json`; deactivating
    them in Sentinel blocks it immediately. A grant may legitimately be `[]`, so the callback tests
    `granted is None` (authorized nowhere -> `request_access.html` / `POST /auth/request-access` files a
    **passwordless pending** account in the console's Access-requests tab), NOT falsiness.
  - 🔴 **"No answer" is NOT "denied" — the three-way return (fixed 2026-08-03).** The staff lookup
    used to be a boolean, so a Sentinel **cold start** (Cloud Run scale-from-zero + a Cloud SQL
    reconnect, routinely past the 3s timeout) was indistinguishable from "this email is nobody" and
    the callback showed a real staff member the **Request access** page — who then signed in fine on
    the next try. That flapping is the whole symptom; it is not an OAuth or a cookie bug. Now
    `user_status` returns **"active" / "denied" / "unknown"**, `_resolve_login_email` returns a key
    list / `None` / **`main.AUTH_UNKNOWN`** (a sentinel object, so `is None` still means exactly
    "authorized nowhere"), and **only a definitive `denied` routes to request-access**. `AUTH_UNKNOWN`
    renders the login page with a retry message and **503** — never a spurious pending account for
    someone who already has access. Both callers (`google_callback`, `admin_stop_impersonating`)
    handle it explicitly; a new caller MUST too.
  - **`sentinel_directory.py`** is the client: it HMAC-signs `"user-lookup:{ts}"` with `SSO_SECRET`
    (the shared `platform-sso-key`) and GETs `${SENTINEL_API_URL||SENTINEL_URL sans /login}/api/internal/
    user-lookup`. Same HMAC pattern the mastery engine uses against `/api/internal/people`; **no new
    secret**. Best-effort + gated: unset secret/URL, a non-200, a timeout, or an outage all return
    `None` from `lookup_user` (-> `"unknown"`), so a Sentinel outage can never break portal login.
    **A transport failure or a 5xx is retried ONCE** at `_RETRY_TIMEOUT_SECONDS` (12s vs the 3s fast
    path) — that retry is what absorbs the cold start. A definitive 4xx/200 is never retried, so a
    bad signature still fails fast (and as `"unknown"`, never as a denial).
- **Operator console (`/admin/atrium`, `admin_atrium.html`)** = a **focused console** styled to the
  website design system (green `#4FA84A` primary + purple `#6A6AEA` informational). The old Home-hub
  landing (the "Your Agora suite" card grid) was **removed 2026-07-17** — the console is now the ONLY
  view (`showView("console")` on load); the app switcher (Atrium / Sentinel / Website Editor — Skill
  Mastery lives inside Sentinel, so it is NOT listed) lives in the **account dropdown under the
  username** (`#acct-toggle`/`#acct-menu` in `.side-foot`, opens upward, also holds Your profile +
  Sign out) and the **Agora logo (top of the rail) links back to `agoradatadriven.com`**. Inside: the
  rail is ordered by frequency of use (2026-07-14 IA pass) — **Workspaces** (Clients) / **Delivery**
  (Task Board · Calendar) / **People & access** (**Accounts** — one pane with inner subtabs Requests ·
  People · Add new) / **System** (Activity · Mailboxes · **Bin**, restorable soft-deletes; utility
  items render muted via `.nav-item.util`, and the Bin count uses the neutral `.count.quiet` — purple
  badges are reserved for ACTIONABLE counts). Client cards carry an attention chip — purple **"N
  awaiting approval"** (count computed in `admin_atrium()` from each already-loaded workspace, cards
  needing attention sorted first) or green **"All caught up"**.
  It IS the admin landing: `/` redirects a super-admin here and the legacy `/admin` + `/superadmin`
  routes now just redirect here too (their client-add / password-reveal
  functions live in the console). **`/` has no page of its own at all since 2026-07-31** — the
  "Welcome back" `portal.html` card list was deleted and `index()` now just runs
  `_post_login_destination()`: `"*"` → the console, one client → `/w/<c>/dashboard`, anyone else →
  `WEBSITE_HOME_URL` (the marketing site). Signed out it still goes to `/login`, never the website.
  Knock-ons: no client switcher for a multi-client login, `atrium.html` lost its client-side
  "Back to portal" button, `_inject_portal_chrome` links **Back to workspace** instead of
  "All dashboards"/"Feedback", and `POST /feedback` + `feedback.py` now have NO caller. Account routes
  (`/admin/accounts/{create-client,create-admin,grant-google,set-password,reset-password,delete}` +
  `/admin/profile/password`) are gated `is_superadmin()`; **admin-account** creation/management +
  **granting a role** + **impersonation** are gated `is_root_admin()`. `POST /admin/accounts/grant-google`
  is the ONE 'give a Gmail access' action (used from Access-requests AND Create-account): assign to a
  **new client**, an **existing client key**, or a **role** (admin/superadmin) -> `store.upsert_google_account`
  (passwordless, upserts by email so it also activates a pending request in place).
- **Impersonation ("Act as user"):** `POST /admin/impersonate` lets THE super admin assume any active
  account's role + clients (real identity kept in `session["impersonator"]`); every page then carries a
  fixed **"Stop acting as"** banner injected by the `after_request` hook in `main.py` (so it reaches
  even the huge `atrium.html` without editing it). `GET|POST /admin/stop-impersonating` restores the
  real identity. Only `is_root_admin()` can START it (once acting-as you ARE that user, so the controls
  vanish). This is what "signing in as `info@` lets you act as any user" means.
- **Roles:** `client` < `admin` (clients `["*"]`) < `superadmin`. THE super admin is `SUPER_ADMIN_EMAIL`
  (default `info@agoradatadriven.com`, env-overridable) or any account with role `superadmin`; only they
  create/manage admin accounts, grant roles, impersonate, and can't be deleted. The no-password preview
  (`DEV_NOAUTH`) auto-signs in as `SUPER_ADMIN_EMAIL`.
- **`templates/*.html`** — big self-contained pages. Inline JS must be **esprima-4.x-safe** (no `?.`
  / `??`; classic `&&`/`||`). No Jinja inside `<script>` — JS reads state from the DOM.
  🔴 **One stylesheet, no scoping — a reused class name silently hijacks the older component.**
  `atrium.html` is ~1,900 lines of CSS in ONE `<style>`, so two rules with the same bare `.foo`
  selector have equal specificity and **later source order wins, property by property**. This bit
  us hard (fixed 2026-07-29): the content card's inline decision ribbon and the branded
  `window.confirm` replacement were BOTH `.ax-confirm`, so the dialog's
  `position:fixed; inset:0; z-index:200` leaked onto every "Approved / Changes requested" ribbon
  and turned it into a full-viewport click-eating veil — opening any approved content card left
  the whole workspace dark and dead (only Escape got out). The ribbon is now **`.ax-decided`**;
  `.ax-confirm` means the overlay dialog and nothing else. **Before adding a CSS rule, grep for a
  bare `.<name> {` already in the file** — and never "fix" a hijacked inline element by adding
  `hidden` to it (that just deletes it via the dialog's `[hidden]` rule). A known-latent twin of
  the same trap still exists: **`.ax-ch-meta`** is declared twice (the card-head ref line and the
  chat-message meta line — `ax-ch-` means two different namespaces); it is cosmetic only today
  because neither rule sets `position`/`z-index`, but adding a layout property to either one
  will break the other component.
- **`atrium_docs.py` / `feedback_ai.py`** — the opt-in Google-Doc → AI strategy feature (gated, degrades).
- **`atrium_health.py`** — the team-only Website Health tab: fetches the client's live site + detects
  installed marketing tags (GTM/GA4/pixels) by scanning the page HTML (no GTM API, infra-free, degrades).
- **`watcher.py`** — the team-only Watcher tab: paste a YouTube channel link, archive EVERY video's
  raw transcript. No YouTube API key: channel-page scrape → public `youtubei/v1/browse` playlist
  paging (classic renderer AND 2025+ lockupViewModel shapes; captures upload age →
  `published_estimate` ISO date) → `youtube-transcript-api` (pinned in requirements, lazy import).
  Channels are classified: `platform` / `industry` (auto-labeled via `intel_ai.classify_text`,
  hand-editable) / `kind` creator|competitor. Registry in `ws["watcher"]`; each channel's
  transcripts in its own `workspace/watcher/<c>/<id>.json` object. `POST /w/<c>/admin/watcher`
  (op add|**add_site**|add_video|fetch|safe_pull|refresh|meta|label|delete; **`add_site` = the
  website-blog twin of `add`** (see `watcher_blog.py` below), and **`add_video` auto-detects** — a
  link with no YouTube video id is scraped as a blog post into a separate "Saved articles" loose
  channel, so ONE box takes both; fetch = MISSING-only batches (parallel
  `FETCH_WORKERS`/`FETCH_BATCH` waves behind a proxy, else serial), page JS loops it and AUTO-RETRIES
  with backoff on a `blocked` rate-limit / network error instead of stopping (button toggles to Stop);
  a rate-limit reports `blocked` and never marks videos failed) +
  `GET /w/<c>/watcher/video/<id>/<vid>` (full transcript behind the click-to-expand cards) +
  `GET /w/<c>/watcher/safe-pull-status` (live Safe-pull progress the tab polls; see Safe pull below).
  **Single-video scraper (`op=add_video`):** paste ONE video link → `watcher.resolve_video`
  (`extract_video_id` handles watch/youtu.be/shorts/embed/live + a bare id; keyless oEmbed for the
  title, then a watch-page `og:title` scrape via `_scrape_video_meta` when oEmbed 401s — lots of
  videos block oEmbed but still carry a real title, so the archive shows the actual name, not
  "Video <id>"; re-adding heals an entry that saved before a title resolved) → fetch transcript
  inline → save under the per-client "Saved videos" pseudo-channel
  (`workspace.ensure_loose_channel`, marked `loose`, `channel_id=""`); the response carries the
  transcript so the reader pops immediately (the tab's 2nd add-card; on success it reloads + auto-
  opens the modal). A rate-limit saves the video pending + reports `blocked` (Fetch missing / Safe
  pull finish it). The loose channel renders/fetches/safe-pulls/indexes like any other; only its
  Check-new/Auto-label buttons are hidden (no real channel_id → `list_videos`/`refresh` never run
  on it). UI: 3-across creator grid, collapsed to the 4 newest videos, filter bar
  (search/platform/industry/type) + date sort. YouTube blocks datacenter IPs — for Cloud Run
  fetching create Secret `watcher-proxy-url` (mounted as `WATCHER_PROXY_URL` when present).
  **Safe pull** = the no-proxy path: `op=safe_pull` queues the channel in
  `ws["watcher"]["safe_pull"]` (helpers `workspace.queue/clear_watcher_safe_pull`); the operator
  machine's scheduled task (`install_safe_pull_task.ps1` → `safe_pull_agent.vbs`, 5-min tick,
  hidden) runs `safe_scrape_local.py --queue` — slow residential-IP scrape (12–20s pacing,
  5→60 min ladder, %TEMP% PID lock, syncs to the bucket every 5 transcripts, clears the queue
  entry per finished channel; no args = full sweep). **Why it's slow (by design):** up to a 5-min
  wait for the next scheduled tick + ~15s/video pacing + a 5→60 min cooldown on any YouTube
  rate-limit — deliberately gentle so the home IP never gets blocked. **Live status:** the scraper
  writes ONE global heartbeat object `workspace/watcher_safe_pull_status.json` per video (phase /
  current video / done-total / cooldown-until; `write_status` in `safe_scrape_local.py`, read by
  `workspace.read_safe_pull_status`). `GET /w/<c>/watcher/safe-pull-status` (team-only) fuses that
  heartbeat with the queued channels' registry counts; the Watcher tab polls it every ~12s while a
  card is queued and shows what's happening ("Fetching now: …", "cooling down ~10 min", "idle, last
  active 3 min ago", counts + a progress bar) instead of a static "check back later", auto-refreshing
  once a channel clears the queue. ⚠️ `scrape_channel` **skips `platform=="blog"` channels** — their
  archives hold page URLs, not video ids, and the app fetches them itself. Test:
  `python _watcher_localtest.py`.
  🔴 **The four adding ops are FUNCTIONS, not route branches** (2026-08-03): `_watcher_op_add`,
  `_watcher_op_add_site`, `_watcher_op_add_video` (which delegates to `_watcher_add_post`) and
  `_watcher_op_fetch` take explicit args and return plain dicts; the session route above and the
  HMAC bridge below are both thin callers. Keep them that way — the whole point is that a source
  added by the Academy is byte-identical to one the team pasted in. They do **not** call `_audit`
  (it stamps the SESSION actor, which a bridge call doesn't have): each caller writes its own entry.
  **The Academy WRITES to this archive** — `POST /api/internal/watcher/add` (purpose `watcher-add`,
  `_internal_gate`, fail-CLOSED, no session), JSON `{client, op, url|channel, retry, actor}` with
  `op` restricted to `add|add_site|add_video|fetch`. mastery-engine's Academy Admin uses it so
  "pull a transcript in" no longer requires opening Atrium to add the source first (see
  `mastery-engine/lib/watcher.js` `addSource`/`fetchBodies` — the caller). Deliberately NOT exposed:
  delete / meta / label / safe_pull. An unknown client is a 404, never a silent no-op; `add` and
  `add_site` register the listing only, and the caller loops `op=fetch` for the bodies just like
  the tab's own JS. `_internal_audit` gained an optional `role` (default `sentinel`) so these land
  in the Activity feed as **academy**, credited to the admin's email from `actor`.
  **Sentinel reads this archive over the internal bridge** (`GET /api/internal/watcher/{channels,
  videos,transcript}` in `main.py`, HMAC-gated by `_internal_gate` exactly like the task bridge):
  Sentinel's Growth hub → Mentor Library imports a transcript instead of making a worker paste one.
  READ-ONLY — Sentinel *copies* the text into its own table, so an Atrium outage never breaks an
  already-imported transcript. 🔴 **Cross-workspace by default, like `/api/internal/tasks`:** a
  MENTOR is nobody's client, so `?client=` is an OPTIONAL narrowing filter, never a required scope.
  Channel ids come back namespaced **`"<client_key>:<channel_id>"`** (`_internal_watcher_split`
  parses them back) because `videos`/`transcript` need the client key to locate
  `workspace/watcher/<c>/<channel_id>.json`. `videos` deliberately omits transcript bodies (an
  archive runs to MBs); an archived-but-unfetched item 404s `no_transcript` so Sentinel reports
  "not available yet" rather than importing an empty string.
  **`GET /api/internal/watcher/transcripts`** is the BULK leg behind Sentinel's "Import all": one
  channel's whole archive, bodies included, in a single call. The bodies are FREE here —
  `read_watcher_videos` already downloads the entire archive object to answer the light `videos`
  listing, so fetching one transcript at a time re-downloads those same megabytes **once per
  video** (199 GCS reads for one creator). Bounded by a **byte budget**
  (`_INTERNAL_TRANSCRIPTS_BUDGET`, 8 MiB) rather than a fixed page size, since item sizes vary
  hugely; a response that hits it returns `next_offset` to resume from (`0` = done) and never emits
  an empty page — so every response stays under Cloud Run's ~32 MiB fixed-length cap while usually
  finishing in ONE trip. Offsets index the RAW archive list so they stay stable across calls.
  Measured live: 104 transcripts / 6.3 MB of text → 2.1 MB gzipped in ~4 s.
  Gating, the namespacing and the paging walk are covered
  in `_watcher_localtest.py`.
- **`watcher_blog.py`** — the **WEBSITE twin of `watcher.py`**: paste a site, archive EVERY blog
  post's full article text. Same tab, same archive object, same UI, same Assistant index — the ONLY
  difference is which fetcher runs, chosen by the registry entry's `platform` (`youtube`|`blog`).
  Three helpers mirror watcher.py one-for-one: `resolve_site(url)` (→ origin + og:site_name; the
  **origin is the entry's `channel_id`**, so the duplicate check is unchanged), `list_posts(origin)`
  (**sitemap-first**: robots.txt `Sitemap:` → sitemap index → recurse ONLY into blog-named children,
  so a shop's 10k-product sitemap is never downloaded → conventional `/sitemap.xml`-style paths as a
  backstop → an index-page **crawl** only when no sitemap yields anything, reported in `source`), and
  `fetch_post(url)` (→ the body in a field literally named **`transcript`**, so every consumer works
  unchanged). A post's `id` is `bp<sha1(url)[:16]>` — stable (re-listing never duplicates) and
  URL-safe (it is a path segment in the reader route). Listing drops a URL that is a **parent of
  other collected URLs** — that is what removes a blog's own index pages without hardcoding any CMS.
  **Extraction is a readability-lite scorer, stdlib only** (`html.parser` tree → score every
  container by text length × (1 − link density) with class/id hints → **deepest of the near-ties**
  wins, so the article's own wrapper beats the page div that also holds the sidebar); title/date/
  author come from og:/JSON-LD/`<time>` metadata, and the fetch **heals** the listing's slug-title
  and lastmod with the page's real ones. **robots.txt is obeyed properly** — full wildcard/`$`
  matching, longest-match-wins, `Allow` precedence. 🔴 Prefix-matching the text before the first `*`
  is NOT good enough: Shopify ships `Disallow: /blogs/*+*`, whose literal prefix is `/blogs/`, which
  read as a prefix bans EVERY post on the site and silently returns zero (this exact bug cost a
  debugging round; there is a regression test for it). 🔴 **TLS compatibility (`_session`)**:
  Python's default SSL cipher list is fingerprinted by Cloudflare-fronted Shopify sites, which then
  answer every HTML request `429 local_rate_limited` forever while curl gets 200 — pinning an
  ordinary explicit cipher suite fixes it. We do NOT impersonate a browser (the UA still says
  AgoraAtriumWatcher, robots is obeyed, `Retry-After`/429 backs off, concurrency is 6 and paced).
  Websites don't block datacenter IPs, so blogs fetch fine FROM CLOUD RUN — no proxy, and **Safe
  pull is hidden on blog cards** (`can_safe_pull`; `op=safe_pull` refuses them). Verified live on
  thelegalpaige.com: 330 posts listed in ~2s, 24 full articles per fetch batch in ~2.6s, 0 errors.
  Tests: `_run_blog_checks` in `_watcher_localtest.py` (fetchers injected — no network in CI).
- **`watcher_template.py`** — the **default watched sources EVERY client gets, automatically**
  (2026-07-30). Pure catalog (no I/O, no workspace import), the twin of `service_templates.py`:
  a git-versioned source list keyed by segment — `UNIVERSAL` for everyone plus industry segments
  matched loosely against the Company tab's free-text `industry` (`segments_for`, substring hints,
  because a human types that field). `sources_for(segs)` returns copies; `TEMPLATE_VERSION` bumps
  when the catalog changes. Applied in TWO places: `onboard_client.onboard()` (the ONE funnel both
  client-creation routes use — universal only, since a day-one workspace has no Company profile) and
  `main._watcher_reconcile()` on every team render, which back-fills existing clients and picks up a
  client's industry segment the moment somebody fills the Company tab in.
  Workspace side: `workspace.pending_watcher_template(ws, sources)` (pure set-difference — the
  render's cheap pre-check, so the common path writes NOTHING), `apply_watcher_template()` (one
  atomic additive write; recomputes the missing set INSIDE the mutation because storage is
  last-write-wins) and `watcher_template_state()`.
  🔴 **SHARED archives — the reason this scales.** An archive is per client, so 15 clients watching
  Search Engine Land = 15 copies of a multi-MB object, 15× the publisher traffic, 15× the embedding
  bill. A `shared` source is stored ONCE under `workspace.HOUSE_CLIENT` (default `agora`), and the
  sharing is encoded in the **ENTRY ID** (`SHARED_PREFIX` = `wsh_`, minted by `shared_channel_id`)
  rather than a flag — so ONE redirect inside `watcher_object_name` fixes all 20+ archive call sites
  (tab render, fetch loop, Sentinel bridge, Assistant index) with no signature change and no extra
  read. It also makes the reconcile naturally idempotent (the id is deterministic).
  ⚠️ **`safe_scrape_local.py` hardcodes `workspace/watcher/<client>/<id>.json`** and never calls
  `watcher_object_name` — teach it the `wsh_` rule before adding a shared YOUTUBE source.
  🔴 **`delete_watcher_channel` gained two template rules:** removing a template source RECORDS the
  opt-out (`ws["watcher"]["template"]["removed"]`) so the reconcile never resurrects it — without
  that the team deletes an irrelevant source and fights us every render — and it NEVER deletes a
  shared archive (it still serves every other client). A per-client archive is still removed.
  🔴 `_watcher_entry()` is now the ONE definition of a registry entry (shared by the hand-add path
  and the template); it accepts `entry_id` to force a shared id and `template_id` to mark provenance.
  Constraints that are deliberate, not oversights: template sources are **blog-only** (YouTube blocks
  datacenter IPs and would swamp the one-residential-IP Safe-pull queue), and `kind` stays inside the
  existing `creator|competitor` pair (`atrium.html` treats it as a two-state toggle plus a hardcoded
  filter dropdown and one CSS chip class per value). Entries appear automatically; the first
  **listing** of a shared source is still one "Check for new posts" click — once per source for the
  whole estate. `main._client_industry()` reads the industry — note `workspace.company_profile()`
  returns the WHOLE company block, so the facts are one level down under `"profile"`.
  Tests: `_run_template_checks` in `_watcher_localtest.py` (catalog, the shared redirect, and all
  four reconcile invariants). ⚠️ Every client is pre-seeded now, so a test asserting "the registry is
  empty" must filter template entries — see the `_hand_channels` helper.
- 🔴 **The task board: client read-only; the TEAM manages it from the client Tasks pane again
  (D2 of `sentinel/docs/TASKBOARD_REBUILD.md`, AMENDED 2026-08-04 by owner decision — full
  read-only lasted one day).** `ws["tasks"]` is the STORE (D1); Sentinel's board lists it live over
  the bridge and writes through the same `workspace.py` helpers, so an edit on either surface is
  one record. ⚠️ Exception: a card **claimed** by a Sentinel row (`tasks.atrium_task_id`) is
  re-projected on Sentinel's next edit — Sentinel stays authoritative for those.
  - **Routes:** RESTORED — `POST /w/<c>/admin/task/move`, `/task/delete`, and `/task` as a narrow
    **`op=edit`** (title / client note / dates / priority / internal notes, each patched only when
    the form carried it; blank title refused). STILL RETIRED — `/task/{hold,subtask,maintask,
    comment}` and `op=add` on `/task`: the catch-all (`atrium_admin_task_retired`) +
    `_task_retired_reply` answer **410 Gone** after the auth gate (410 not 404 on purpose: a stale
    tab must read "managed in Sentinel", not "not found"). Creation stays with `/task-add` (the
    quick-add + the team's per-column add). `_task_fields_from_form` / `_task_template_seed` remain
    gone. Werkzeug routes the static `/move`/`/delete` rules before the `<path:_action>` catch-all.
  - **Console pane** (`admin_atrium.html`, Delivery → Task Board): still a READ-ONLY monitor —
    every VIEW kept, every WRITE gone; the overlay's one action is **Open in Sentinel →**
    (`sentinel_base` + `?open=atrium:<client_key>:<task_id>`). Team writes live on the client
    Tasks pane's team view, not here.
  - **Client Tasks pane** (`atrium.html`): the team's drag-to-move (`data-pgcol`/`data-pgdrag`) +
    delete-✕ (`data-pgdel`) and their wiring IIFE are RESTORED, plus a NEW team-only **✎ Edit** in
    the detail modal (`data-pgedit`/`data-pgeditform` — a collapsed form reusing `.ax-pg-addform`
    styling; Save posts `op=edit` then reloads). All `{% if is_superadmin %}`-gated; the client
    keeps `task-comment` and the quick-add composer (D3/D4) and their HTML carries none of the
    team markup.
  - **Assistant actions:** `add_task` / `move_task` / `complete_task` are BACK in `_ACTIONS`.
    `add_task` gained a `stage` param (canonicalised, junk→todo) so "here are the things I
    completed this week" proposes `add_task(stage=completed)` per item, and `client_facing`
    defaults TRUE (only an explicit false keeps a card internal — a proposal made looking at the
    client board that lands on the hidden internal list reads as "the AI didn't add it").
    `comment_task` unchanged.
  - **Still writes `ws["tasks"]`:** the internal bridge (`/api/internal/task*`), the team surfaces
    above, approved Assistant actions, and the client's two powers. `workspace.py` is still the
    only writer, and `move_task_stage` keeps its `ValueError` contract for the bridge.
  - `service_templates.py` is **kept but unwired** (`# noqa: F401` on the import): Sentinel's
    `ServiceTemplate` table owns the recipes now: the module is the written record of the shape both
    sides agreed, and `_atrium_smoketest` still checks `build_maintasks` produces it. Don't re-wire
    it into a form here.
  - Tests: `_atrium_smoketest.py` builds its task fixture through the `workspace.py` helpers,
    asserts the remaining 410s, exercises the three restored routes, and asserts the affordances
    are present in the TEAM's HTML and absent from the CLIENT's (the edit-form no-leak check
    matches the rendered task id — the wiring script's `'[data-pgeditform="'` literal ships to
    every viewer); `_assistant_localtest.py` proves the three restored actions end to end.
- **`assistant_ai.py`** — the team-only Assistant tab: RAG chat over EVERY workspace source
  (watcher transcripts, intel, campaigns/content, metrics, calendar, conversations, health, plus
  the opt-in client dashboard export — grant via `enable_assistant_dash_data.ps1`). Index stored as
  `workspace/assistant/<c>/index.json` (lazy rebuild on `fingerprint` change OR an `INDEX_VERSION`
  bump — the index shape is versioned so a chunking/indexing change forces a one-time rebuild).
  **Each chunk is indexed + embedded by its TITLE + body** (`_searchable`), so the ENTITY NAME a
  user searches by — the creator/channel name, campaign name, email subject — is retrievable even
  though it never appears in the body (a transcript never says its own channel name; that was the
  2026-07 "no Fuel Your Wander content" miss). **HYBRID retrieval:**
  BM25 (pure-Python, precomputed once per ask — the old per-query re-tokenisation is gone) + a
  SEMANTIC leg (`embed_index` → Vertex `text-embedding-005`, unit vectors packed as base64 float16
  into the same object; same SA auth as the Gemini brain, NO new API/IAM — **INCREMENTAL: a rebuild
  reuses the stored vector of any chunk whose id+content signature (`emb_sig`) is unchanged and
  embeds only new/changed chunks, so a Watcher fetch no longer re-embeds the whole corpus on the ask
  request's critical path**) are fused with **RRF**
  (`_rrf`, rank-only), the pool optionally **reranked** by a cross-encoder (Vertex Ranking API,
  `intel_ai.rerank` → `semantic-ranker-fast-004`), and a **metadata pre-filter** (`_infer_kinds`,
  single-source questions only — but a question that NAMES a watched creator, `_question_names_creator`,
  keeps `video` in scope so "what would <creator> say about <campaign>" stays cross-source instead of
  being scoped campaign-only) + date range narrow first. Transport lives in `intel_ai`
  (`embed_texts`/`embed_query`/`rerank`, gated by `embeddings_configured()`/`reranking_configured()`)
  and is INJECTED into `ask` as `query_embedder`/`reranker` — omit them (default deploy / tests) and
  it is exactly the old BM25 path. Gates: `ASSISTANT_EMBED_ENABLED=1` + `VERTEX_EMBED_LOCATION`
  (embeddings, ON by default in the deploy — reuses Vertex, region-pinned so private text stays
  in-region); `ASSISTANT_RERANK_ENABLED=1` (reranking, opt-in via `enable_assistant_reranking.ps1`
  → enables discoveryengine.googleapis.com + grants the web SA `roles/discoveryengine.user`; the
  deploy auto-detects + flips it). Every leg degrades to lexical/fused on any failure.
  Answers via `intel_ai._call` (JSON-mode; `_parse_answer` parses leniently AND salvages
  nearly-JSON — `strict=False`, `raw_decode` past trailing junk, then a hand-scan of the
  `"answer"` string literal that survives stray characters, truncation at the token cap, and raw
  newlines — so the UI is never handed a raw JSON envelope) with cited sources. Bot bubbles render
  the model's markdown via `mdToHtml` in `atrium.html` (headings/bold/lists; HTML-escaped first,
  esprima-safe).
  `POST /w/<c>/admin/assistant` (op ask|settings|reindex — the non-streamed path, still used by
  tests). **STREAMING chat (the live UI path):** `POST /w/<c>/admin/assistant/stream` returns
  **Server-Sent Events** (`text/event-stream`, `stream_with_context`) so the reasoning + answer
  arrive as deltas. `intel_ai.stream_call` normalises each provider's SSE (Vertex
  `:streamGenerateContent?alt=sse` with `includeThoughts`; DeepSeek + Kimi `stream:true`, both the
  OpenAI `reasoning_content`/`content` delta shape) to flat events
  `{thinking|answer|usage|error}`; `assistant_ai.ask_stream` retrieves (hybrid) then streams the
  answer as **plain markdown** (NOT the `{"answer":...}` envelope — a wrapper can't stream), honouring
  a `steer` string. `stage=plan` (`assistant_ai.plan_stage`) is the Claude-style **plan-mode
  checkpoint**: retrieve + (deep) plan the sub-queries and return them + the sources WITHOUT
  answering, so the team approves/steers BEFORE any token is written. Frame types: `plan`, `sources`,
  `thinking`, `answer`, `usage`, `error`, `done`. Frontend (`wireAssistantChat` in `atrium.html`,
  esprima-safe): a `fetch` + `ReadableStream` reader parses the SSE; a collapsible **thinking panel**
  streams live; **Pause & steer** aborts the stream (`AbortController`) and restarts with the steer;
  **Plan first** (a per-browser localStorage toggle, `.ax-as-planmode` in both surfaces) switches on
  the checkpoint. Conversations are now **session-scoped** (`sessionStorage`, fresh each new
  session/tab; the old localStorage design persisted forever). Dev: `VERTEX_ACCESS_TOKEN` env runs
  Vertex off-cloud (verified the full stream live via `run_local.ps1` + a real token).
  Test: `python _assistant_localtest.py`. UI: the team-only floating bubble
  (`ax-asfab` FAB + `ax-aspanel` pop-up in `atrium.html`, inside `.atrium` so the vars/font
  inherit; brand-green 72px since 2026-07-13) is the PRIMARY surface, available on every tab; the
  Assistant tab pane still exists but is no longer in the nav (reach `/w/<c>/assistant` by URL for
  the date-range + reindex controls) — both surfaces wired by ONE `wireAssistantChat`; the bubble
  hides on the Assistant tab via `.atrium[data-tab="assistant"]`. Each surface's conversation is
  persistent **chat history**: localStorage key `agora.aschat:/w/<c>:<log-id>` (last 40 turns,
  per-browser), replayed on load (greeting shows only when nothing is stored — it comes from the
  log's `data-greeting`), cleared by the "New chat" button on either surface; the saved turns also
  feed the model's multi-turn context (`history` field of op=ask, last 8). **Model choice:** `op=settings`
  saves `ws["assistant"]["model"]` ("" = automatic → intel model → deploy default; resolved by
  `main._assistant_model`); the dropdown renders via the shared `as_model_options()` macro (tab
  bar + the bubble's gear strip). **Detail (depth) control:** `op=settings` also saves
  `ws["assistant"]["depth"]` (quick|standard|deep, `assistant_ai.DEPTHS`, resolved by
  `main._assistant_depth`; `as_depth_options()` macro, same two surfaces — each dropdown posts
  only its own field so saving one never resets the other). Deep = the model plans extra BM25
  queries first (`plan_queries`), retrieval widens to 30 excerpts, provider thinking turns ON
  (`intel_ai._call(..., think=True)`: Gemini thinkingBudget 4096, DeepSeek
  `thinking:{type:enabled}`; quick/standard send the explicit fast path since DeepSeek V4 thinks
  by default server-side), and the prompt asks for a structured analysis. All depths may
  synthesize across excerpts (implicit disagreements count). **Spend tally:** `intel_ai` provider calls fill an optional
  `usage_out` dict (DeepSeek `usage`, Vertex `usageMetadata` incl. thinking tokens);
  `intel_ai.PRICING`/`cost_of` price it, `workspace.add_assistant_usage` accumulates
  `ws["assistant"]["usage"]`, and the cost pill (`ax-ascost`, seeded from data-* attrs, updated
  from each ask's `usage`/`totals`) shows session + all-time + by-model. **Client rename:**
  `POST /admin/atrium/<c>/rename` (superadmin) updates the registry name
  (`store.set_client_name`) AND the workspace `display_name` — display-only, the key/resources
  never change; the console cards have a Rename button (prompt-driven).
- **`assistant_reindex.py`** — the Cloud Run JOB `assistant-reindex`: the FULL Assistant index
  rebuild, deliberately OFF the request path. Reuses the platform-dash image + web SA (mirrors
  `intel_refresh` / `mail_refresh`), gated `ASSISTANT_REINDEX_ENABLED=1`, deployed by
  `deploy_assistant_reindex.ps1` (**2Gi memory + a 3600s task timeout — those are the fix, not
  boilerplate; do not trim them to match the other jobs**).
  🔴 **Why it exists (2026-07-31).** A full rebuild re-chunks every source and re-embeds the whole
  corpus — ~344s and a multi-hundred-MB peak on a 30 MiB / 6.8k-chunk workspace. Running that
  inside the ask blew both the 512 MiB memory limit and the 300s request timeout, and because the
  index is written **LAST** it persisted nothing; since the reuse check demands
  `stored v == INDEX_VERSION`, after a version bump **every** ask retried the identical doomed
  rebuild and chat was permanently dead rather than slow. Diagnosing it is unintuitive: the SSE
  stream logs **HTTP 200** (it opens before the model call, then the container is killed
  mid-stream) — the tells are a missing `asked the assistant (streamed)` activity line and a
  separate `severity>=ERROR` "Memory limit ... exceeded" entry.
  `main._assistant_index` is now bounded: reuse → cheap incremental rebuild (≤
  `assistant_ai.EMBED_MAX_NEW_INLINE` new vectors, remainder recorded as `emb_partial`) → or, when
  `assistant_ai.needs_full_embed` says nothing carries over, SERVE the existing index and
  `workspace.queue_assistant_reindex` the client. Selection: the 15-min tick takes only FLAGGED
  clients (the flag rides in the workspace JSON it already loads); `--sweep` also opens each stored
  index (🔴 downloads tens of MB per client — post-version-bump only); `--all` forces everything;
  `--client <key>` is what the Rebuild button triggers via `sync_dash.trigger_job`.
  ⚠️ Overrides need `run.jobs.runWithOverrides` — `roles/run.invoker` does NOT carry it, so the web
  SA gets `roles/run.developer` on the job. ⚠️ `assistant_ai.gather_sources` + `dash_stamp` are ONE
  definition shared by the ask path and the job **on purpose**: if the job derived a different
  fingerprint, the index it wrote would read as stale on the next ask and the client would requeue
  forever. ⚠️ **Bumping `INDEX_VERSION` now requires running the job with `--sweep`.** Covered by
  the bounded-rebuild checks in `_assistant_localtest.py`.
- **`mailroom.py` / `mail_refresh.py`** -- the team-only email machinery, now FOLDED INTO the
  Communications tab (2026-07-15; no standalone Mail tab -- its contacts/sync/briefing/stats live in
  the Communications **Email intelligence** panel and email threads render as email-channel cards in
  the timeline). Pull + archive + AI-summarize
  each client's email correspondence. Mailboxes connect ONCE in the console (Mailboxes pane;
  `POST /admin/mail` op add|delete|test, root-only): kind **dwd** = our Workspace domain via the
  Gmail API + domain-wide delegation, keyless signJwt as the `mail-sync` SA
  (`enable_atrium_mail.ps1` one-time; deploys auto-set `MAIL_DWD_SA` when the SA exists), or kind
  **imap** = any other Google account via an app password (stdlib imaplib, Gmail `X-GM-RAW`, the
  All-Mail folder -- so both connectors run the SAME Gmail query and normalize to the same thread
  shape). Per client: `ws["mail"]["contacts"]` drives the query (only client mail is pulled);
  thread archives are their own objects `workspace/mail/<c>/<key>.json`; the small index + digest +
  sync stamps live in `ws["mail"]`; the global registry is `workspace/mail/_mailboxes.json`
  (`public_mailboxes` strips passwords for templates). Machine mail is dropped per message
  (`is_automated`: noreply/bounce senders, Auto-Submitted, Precedence bulk/junk/auto_reply;
  List-Unsubscribe alone NEVER counts -- Google-Groups-safe; the query also excludes the
  Promotions/Social categories). `thread_stats`/`stats_line` compute `awaiting_reply` + average
  AGORA reply hours per thread (connected-mailbox addresses + dwd domains count as agency) -->
  the tab's stats strip, per-row chips, the digest's REPLIES judgement, and the Assistant's
  `mail:responsiveness` snapshot chunk. `summarize_thread` returns TWO voices in one call: the
  internal summary (blunt, reply-quality included) + a `client_summary` that is mirrored into the
  client-visible Communications timeline as an email-channel card (`workspace.upsert_email_summary`,
  channel "email"/audience "client", stable
  `mail_<key>` id, updated in place; thread delete retracts it). `build_digest(..., stats=...)`
  writes STATUS/NEEDS ACTION/RECENT/REPLIES; both reuse
  `intel_ai._call` (spend -> the Assistant tally); the Assistant's `build_chunks` indexes the
  archive (`mail_threads`). Routes: `POST /w/<c>/admin/mail` (op contacts|sync|digest|delete) +
  `GET /w/<c>/mail/thread/<key>`, gated `is_superadmin()` (invoked from within Communications;
  `/w/<c>/mail` now renders the Communications tab). The hourly
  job `mail-refresh` reuses THIS image (gated `MAIL_SYNC_ENABLED=1`; deploy
  `deploy_mail_refresh.ps1`, rerun after any mailroom/mail_refresh change). Test:
  `python _mail_localtest.py`.
- **`upwork_import.py`** — the Communications tab's **Upwork importer** (team-only, `POST
  /w/<c>/admin/communication` op `import_upwork`). Pure + infra-free (no network/storage/AI):
  `parse_upwork(raw, agora_names)` is a small state machine over pasted Upwork chat → an ordered
  (oldest→newest via `sort_messages`), role-tagged (agora vs client by the team's display name),
  de-duplicated (quoted reply-backs + avatar-initials + attachment-count lines stripped) list of
  `{from,to:"",date,role,body}` messages + `title`/`participants`/`latest_date`. **Upwork
  SYSTEM-EVENT lines** ("<Name> sent/withdrew/updated/accepted an offer", contract/invitation/
  milestone/payment — and the meeting family: "created a (recorded) Zoom meeting", "wants to
  schedule a 60-minute meeting", "re/scheduled|canceled a meeting", "activated the milestone") are
  matched by `RE_EVENT` and dropped — they used to be mis-parsed as chat messages (polluting the
  list + the derived participants/title). Upwork's most recent day separators are a bare
  **"Today"/"Yesterday"** line (`RE_DAY_REL`, resolved against the paste day) — unmatched, they made
  fresh messages inherit the previous dated header's stale day. `main.py` stores it as a Mail thread archive object (key
  `up_<id>` via `workspace.write_mail_thread`) so the EXISTING `/w/<c>/mail/thread/<key>` reader
  modal renders it, and adds an `upwork`-channel timeline card whose summary is
  `mailroom.summarize_thread` (`fallback_summary` when no model). **"Us" = the right side of the
  chat:** the import route builds `agora_names` from the typed team name PLUS the whole `_team_roster()`
  (a first name like "Ian" matches the full Upwork display "Ian Gabriel Fernandez"), so the team
  lands on the right even when the name field is left blank. **`normalize_chat_thread(thread,
  agora_names)`** re-cleans a STORED thread idempotently (drop event lines, RE-TAG roles from the
  roster, re-order, recompute participants/subject) — the thread route calls it on read for any
  `up_`/`origin=="upwork"` thread and persists only when it changed, **also mirroring the healed
  subject/participants onto the owning timeline card** (title/people only — a read never moves the
  card's date), so OLD imports render correctly with NO re-import. The reader modal (`atrium.html`) renders a
  chat thread oldest→newest, **opens scrolled to the newest message** with a floating **"↓ Latest"**
  jump pill, and **groups consecutive same-sender messages** (`ax-ch-cont`: follow-ups drop the
  avatar/name, stack tight). **Add newer messages to an existing conversation** (op `update_upwork`,
  team-only): the reader modal's **"＋ Add newer messages"** button (Upwork threads only) takes a
  re-paste of the fuller thread; `upwork_import.merge_messages(existing, incoming)` folds in ONLY the
  genuinely-new messages (dedupe by date+sender+trimmed-body signature, returns an `added` count),
  then `normalize_chat_thread` re-tags/re-orders and the card's people/subject are refreshed.
  **When messages were actually added the card is stamped with the UPDATE moment
  (`workspace.now_iso()`) — the card's date means "last updated", never the first import's date —
  and the recap is RE-WRITTEN over the whole merged thread** (same `summarize_thread` voice-by-
  audience flow as import; on AI failure the existing summary is kept, never downgraded). No
  duplicate card, idempotent (a re-paste adds 0 and leaves date/summary alone).
  Test: `python _upwork_import_localtest.py`.
- **`intel_ai.py`** — the ONE model registry + transport for every AI surface in this app (the
  Assistant, the intel research brain, the Mail digest, the Watcher auto-label). `MODELS` lists what
  the dropdowns offer; `provider_configured()` gates each provider on its env; `_call`/`stream_call`
  dispatch. **Three providers:** `gemini` (Vertex, SA-token auth, GCP-billed — the ONLY one that can
  ground on live Google Search), `deepseek` (`DEEPSEEK_API_KEY`, `api.deepseek.com`, JSON-mode) and
  `kimi` (`KIMI_API_KEY`, `api.kimi.com/coding/v1`). ⚠️ Kimi notes: it is the **Kimi Code
  subscription** — its `sk-kimi-…` key authenticates ONLY against that coding host (Moonshot's
  `api.moonshot.ai` 401s), it is flat weekly quota so `PRICING` is $0/token (real, not a missing
  price), it is sent **without** `response_format: json_object` (that mode forbids the top-level
  arrays some callers need — `_parse_json`/`assistant_ai._parse_answer` strip the resulting fence),
  and its models are thinking-first so only `think is False` sends the disable flag. 🔴 The
  Secret-Manager secret is the UPPER-case `KIMI_API_KEY`; the lower-case `kimi-api-key` secret is
  the VS Code / Claude Code launcher key and is a DIFFERENT value — never mount it here. Kimi sits
  LAST in `MODELS` so `default_model()` (first available) keeps resolving to Gemini Flash. Adding a
  provider = a MODELS entry + a `provider_configured` branch + `_call_*`/`_stream_*` + the secret in
  the three deploy scripts (`deploy_dash_platform.ps1`, `deploy_intel_refresh.ps1`,
  `deploy_mail_refresh.ps1` — the jobs run this same code and need the key too). Test:
  `python _intel_ai_localtest.py`.
- **The Company tab (`ws["company"]`, 2026-07-29)** — the client's own identity, and the grounding
  every AI surface in this app leans on. Four blocks in ONE workspace key: `profile` (at-a-glance
  facts), `brand` (voice/tone/personality/colours/fonts/dos/donts/assets link), an ORDERED
  `sections[]` story and an ORDERED `products[]` catalogue. Writers are the usual `workspace.py`
  monopoly: `_ensure_company` (normalizes on EVERY read, so an old workspace upgrades silently and
  the template needs no `default` filters), `company_profile`/`company_is_empty`/`company_items`
  (reads), `set_company_profile`/`set_company_brand` (patch ONLY the fields given -- a partial form
  post must never blank the rest) and the five list helpers keyed by `kind`
  (`add`/`update`/`delete`/`insert`/`move_company_item`, `COMPANY_LISTS` is the valid-kind guard).
  🔴 **The lists are HAND-ORDERED, not date-sorted** -- a company story reads top to bottom, so add
  APPENDS (unlike intel/communications, which insert newest-first) and `move_company_item` is a
  first-class writer with its own ↑/↓ controls. Route `POST /w/<c>/admin/company`
  (op profile|brand|add|edit|delete|move|draft, gated `is_superadmin()`); deletes soft-delete to the
  Bin as `company_section`/`company_product` (restored through `insert_company_item`, which appends
  -- the original index is long gone by restore time). **The tab itself is CLIENT-VISIBLE in full**
  (it is the client's own company): only the edit affordances are `{% if is_superadmin %}` and only
  the route is gated -- the inverse of Website Health/Watcher. `op=draft` calls
  `intel_ai.draft_company` (the Company twin of `suggest_config`): the model researches the company
  -- grounded on live Google Search when the model is Gemini -- and the panel writes the drafts INTO
  the on-screen inputs and opens the editors, **saving nothing**; a field it cannot establish comes
  back "" on purpose. **Everything on the tab is what the AI knows the client BY:**
  `digest.company_sections` derives it into titled chunks (facts / brand / each story section /
  catalogue) feeding BOTH `assistant_ai.build_chunks` (kind `company`, undated on purpose so a
  transcript date-range can't hide the client's identity; `INDEX_VERSION` 5 forces the one-time
  rebuild) and `report_ai.gather` -> the deck's "WHO THE CLIENT IS" block. The Assistant can also
  propose `add_company_section` / `add_company_product` / `set_company_facts`. Front end: the pane +
  `.ax-co-*` styles in `atrium.html`, wired inside the `if (isAdmin)` block next to the intel
  wiring (`data-coedit`/`data-coform` share a key -- "facts", "brand", "s-<id>", "p-<id>" -- so one
  pair of loops serves all four blocks). Tests: `_workspace_localtest.py` §12 +
  `_atrium_smoketest.py` (routes, gating, the client no-leak render, Bin round trip, indexing, and
  the nav grouping).
  **Published content + content gaps (2026-08-04), the tab's fifth section:** the client's OWN blog
  archived **through the Watcher machinery** -- the section's add form posts the existing
  `/w/<c>/admin/watcher` `op=add_site` with `own=1` (flagging the registry entry via
  `_watcher_entry`'s `own` field; `workspace.own_content_channels` reads it back, the Watcher tab
  shows a "Client's own" chip) and the same fetch/refresh/delete ops maintain the archive -- the
  add handler AUTO-RUNS the fetch loop after a successful add, so one paste captures the whole
  blog, article text included (a mid-loop failure just reloads; the card's Fetch missing button
  resumes where it stopped). Post
  LISTING is client-visible (`main._company_content_view`, titles/dates/links only, built every
  render -- bodies never leave the archive object); the controls + the **content-gap panel** are
  team-only. `op=gaps` on `/w/<c>/admin/company` compares own titles vs every `kind=competitor`
  Watcher source's titles (`_content_gap_corpus`, titles only, capped) through
  `intel_ai.content_gaps`, storing the snapshot in `ws["company"]["content_gaps"]`
  (`workspace.set_company_content_gaps`, replace-on-rerun). Indexed for the Assistant
  (`digest.company_sections` chunk `content_gaps`) but **excluded from `digest.company_brief`** --
  decks are client-facing and the analysis names competitor sources. Front end: `.ax-coc-*` styles
  + the `data-coc*`/`data-cogaps` wiring IIFE next to the company wiring.
  🔴 **The nav re-cut 2026-08-04:** THREE top-level rows, all groups -- Working Together now holds
  Dashboard / Communications / Tasks / **Reports** / **Company** (the flat Company link is gone,
  Reports left Insights); Insights keeps Market Intelligence + the team tools. `work_open` /
  `tools_open` guards updated to match; asserted in `_atrium_smoketest.py`.
- **`report_ai.py` / `digest.py`** — the Reports tab's deck maker. A deck is a **fixed 1280x720
  stage** (scaled to the window, one slide shown, arrow keys / dots / click to move, `p` prints;
  `@media print` reveals every slide). Payload = `{meta, facts, slides:[{kind, eyebrow, title,
  subtitle, tone, source, blocks}]}` — `kind` cover|section|content|closing, `blocks` text |
  bullets | cards | callout | **action** | **panel** | **split** | chips | kpis | chart | table |
  compare. **`split` (two evidence objects side by side, one level deep) + `panel` (a titled
  reading of the figure beside it) exist for DENSITY**: without them a slide could only stack, so
  the generated deck averaged 94 words/slide against a hand-built 162 (measured). The prompt's
  "4 to 6 blocks, 120 to 180 words, pair every figure with what it means" rule is load-bearing —
  do not trim it back to "one idea per slide". A `split` claims the slide's spare height only when
  it contains a column chart (`.split.grow`, decided in `_block_html`), and a table inside one
  switches to fixed layout with a 30% name column, because a cell `max-width` is advisory under
  auto layout and the last column slid off the edge. 🔴 **Charts,
  tables, before/afters and KPI tiles are drawn from `build_facts(dash_data)`, not from the
  model**: every series/table/compare/tile set is computed in Python (both dashboard shapes; the
  Windsor-live one yields totals, five weekly series, a 14-vs-14 compare, ranked ads / campaigns /
  age / gender / region / email tables, plus the OPPORTUNITY facts — `reallocation` (the expensive
  half of the age curve priced at the cheap half's rate: "same budget, +N clicks"), `segments`
  (age x gender cells), `pressure` (last week vs the flight average) and `bench` (creative depth);
  the TEMPLATE `kpis`/`daily` shape gets tiles, a series per metric, a generic like-for-like window
  and a `momentum` table — it used to get only tiles + 4 charts, i.e. a 4-slide deck for every
  non-Windsor client), the model emits only a **fact key**, and
  `normalize_payload` drops a block whose key is unknown or mismatched — so an invented key renders
  NOTHING rather than a wrong number. The facts are stored INSIDE the payload, which is what makes
  the lazy re-render (`GET /w/<c>/report/<id>` after a Trash restore) byte-identical, and what lets
  `revise` strip them from the model's view and re-attach them afterwards. `brand_kit(ws)` supplies
  the client crest (`ws["brand"]["client_logo"]`, gated by `_mark` to our own self-contained
  `<svg>`/`data:` markup, inlined NOT escaped) and a palette parsed from the Company tab's
  `company.brand.colors` free text (blank ⇒ `HOUSE_PALETTE`); `render_html(..., brand=kit)` takes
  it, and all four call sites (two in `main.py`, two in `assistant_actions.py`) pass it. The
  stylesheet injects the palette as CSS custom properties — do NOT go back to `%`-formatting the
  sheet, every literal `%` in it is a real percentage. The deck's ONE inline script is the slide
  navigator and must stay esprima-4.x-safe (`_report_localtest.py` parses it with esprima).
  🔴 **Two arithmetic traps with regression tests on them:** a flight ending mid-week leaves a
  PARTIAL final bucket (chart it, never compare it — `pressure`/`momentum` filter to complete weeks,
  which is why `_weeks`/`_daily_weeks` return day counts; a 1-day tail against a 7-day mean read
  "-80%" on every metric), and a FLAT series (a fixed weekly budget) has every point tied for max,
  which marked all thirteen weeks "BEST" until `_series` started requiring a real spread and a
  single winner. 🔴 **A generated deck is EXACTLY EIGHT SLIDES (2026-08-04):** a BARE cover
  (company name + date — `enforce_spine` discards any model-written cover) + one slide
  per `report_spec.SPINE` slot (Tasks · Research · The funnel · What happened · Why it happened ·
  What we'll do · Opportunities), enforced in code by `report_ai.enforce_spine` (slides map onto
  slots via their `slot` field or verbatim eyebrow; off-spine drops, missing slots backfill from
  the deterministic draft). Tasks renders as the Trello-style `board` block (the fact/kind is
  `board` — the old tiles+table pair is gone); a Research `cards` item carries its
  why-this-matters in the `why` field (rendered emphasized; a why-line embedded in `body` is
  split out by `_WHY_RE`); section eyebrows render large. The team also picks the reporting WINDOW on generate (`period` =
  mtd|last_week|'' → `report_window` + `build_facts(window=)`), and each deck card has an
  Edit-with-AI dialog (`op=revise`, whole-deck or one slide via `slide`; edits are deliberately
  NOT re-pinned so they may add slides). A new fact must be claimed by a spine slot or it can
  never reach any deck — `report_spec.claims` + the orphan check in the test guard this (the old
  draft "sweeps up unclaimed facts" behavior is gone).
  🔴 **The dashboard export is keyed by `assistant_ai.dash_data_key(client, ws["dashboard_url"])`,
  not by the client key** — in production the portal key (`riverdance-rv`, derived from the display
  name) and the dashboard stack key (`riverdance`) diverged for EVERY client, so
  `agora-data-driven-<c>-dash` never existed, `read_client_dash_data` swallowed the 404 and both the
  Assistant index and the deck's fact pack silently lost the KPI export (a live deck rendered 3
  slides). The resolver reads the Dashboard tab's own embed URL (both Cloud Run host forms + a
  custom domain) and falls back to the client key.
  🔴 **Ranked tables carry a volume floor** (`_MIN_SHARE`/`_MIN_CLICKS`, `_has_volume`): a 0.0%-of-
  spend cell won "best audience" on the real breakdown. The floor is opt-in per table — a row with
  no `_share`/`_clicks` stays eligible, so spend-ranked tables keep their best/worst marks.
  🔴 **The brand marks are CSS custom properties declared once** (`mark_css_url` -> `--crest` /
  `--agoramark`). Repeating the markup per slide made a 3-slide deck 1.9 MB.
  Test: `python _report_localtest.py` (+ `_assistant_localtest.py` covers the key resolver).
- **`intel_feed.py` / `intel_refresh.py`** — the DAILY Market Intelligence auto-refresh (opt-in,
  `INTEL_AUTO_ENABLED=1`). `intel_feed` parses Google News RSS + publisher feeds (keyless, stdlib
  `xml.etree` + lazy `requests`, degrades to `[]`); `intel_refresh.main()` is the Cloud Run **job**
  entry point — it reuses THIS image + the web SA to write `ws["intel"]` (auto entries only; hand-
  added/edited ones are preserved). Deploy: `deploy_intel_refresh.ps1`. Test: `_intel_feed_localtest.py`.
- **`sync_dash.py` / `sync_refresh.py`** — dashboard sync. `sync_dash.trigger_all()` DISCOVERS every
  `<c>-export` Cloud Run job and POSTs each `:run` (Run Admin API, as the web SA), recording the
  stamp in `sync_state.json`. `sync_refresh.main()` is the Cloud Run **job** entry point (gated
  `SYNC_AUTO_ENABLED=1`) that calls it on a 6-hourly Cloud Scheduler tick — this is now the ONLY
  sync trigger: the manual "Sync all dashboards" button was removed (a browser refresh must never
  fire the paid Windsor/Meta pulls); the console shows a read-only "Last synced: Xh ago" via
  `GET /admin/atrium/sync-status`. Deploy: `deploy_sync_refresh.ps1` (`-Run` once now, `-Disable` off).
- **Task tracker (Delivery board + client Tasks tab):** `ws["tasks"]` per client, helpers in
  `workspace.py` (stages `todo|in_progress|blocked|revision|completed` — keys canonical; For
  Review + Waiting for Client were removed 2026-07-29, both fold into Blocked, and retired keys
  incl. the old `in_process|for_launch|launched|closed` set land on a live column via
  `_STAGE_ALIASES`/`canon_stage`; lead +
  `support_ids` never overlap; `move_task_stage` is UNGUARDED since 2026-07-28 — it used to refuse
  `completed` while sub-tasks/change-requests were open, but a bounced drop reads as a broken board,
  so blockers are surfaced, not enforced). Work is TWO-LEVEL: `maintasks[]` (named groups, each with an `assignee_id` + its own
  `subs[]` of owner-carrying sub-tasks); legacy flat `subtasks[]` migrates in place via
  `normalize_task` (called by `_find_task`) and `task_subtasks()` flattens for counts/guards.
  **Service templates auto-build the breakdown (`service_templates.py`):** the New-Service form's
  **Service type** picker (a department filter — **Acquisition = ONE type, "Google / Meta
  Campaign"**, whose creative work is chosen from the Video/Static/Carousel **ad-production picker**;
  Lifecycle/Data/Dev are fixed recipes, some with qty/platform/tool params) drives
  `build_maintasks(key, params, added, id_factory=workspace._new_id)`, which seeds the whole
  `maintasks[]` (per-unit `{n}` steps multiplied by their qty) + sets `content_type` +
  auto-derives the department/label. `_task_template_seed()` in `main.py` reads `service_key` /
  `p_<param>` / `ad_type`+`ad_qty` and injects it — **ONLY on op=add** (edits never regenerate).
  "Custom (blank)" = the old empty-card path. The catalog reaches the page as hidden DOM
  (`#svc-catalog`/`#adprod-catalog`, no Jinja in `<script>`); passed via `task_services` /
  `task_adprod`. Templates are a SEED, not a lock — the maintasks are ordinary data afterwards.
  Each sub-task carries an optional **`dod`** ("done when") — an INTERNAL definition of done shown
  in the team subrow only; `_progress_tasks` strips steps to text+done so it NEVER reaches the
  client. `add_subtask(..., dod=)` + the manual add-sub form's "Done when…" input persist it.
  Tasks also carry `start_date` + `due_date` (LAUNCH date — UI says "Launch date"), an
  internal-only `service_charge`, a boolean **`on_hold`** + internal `hold_reason`
  (`workspace.set_task_hold`, ongoing = not held; `POST /w/<c>/admin/task/hold`; a held
  client-facing task shows the client a plain "Paused", reason never crosses), and ONE label
  auto-derived from the department (`main.TASK_DEPT_LABEL` — no label picker; the form's name field
  is LABELED "Campaign" but stores as `title`). Support people are Edit-only (`has_support` guard,
  so op=add never clears). Team board = the console's Delivery → Task Board pane in
  `admin_atrium.html` (tasks collected in `admin_atrium()` from the workspaces it already loads;
  columns sort Urgent-first, active-before-held, then launch date; filter+sort persist in
  localStorage `agora.tkprefs`. **Board-scaling controls (all client-side, in `agora.tkprefs`):** a
  **text search** (`#tk-f-search`, matches title/client/lead — folded into `applyFilters`); a
  **density toggle** (DENSITY segment Comfortable/Compact → `.tk-board.tk-compact`, trims padding/type
  + hides the card foot for ~2× density); and **Cap columns** (`#tk-f-cap`, ON by default →
  `applyCap`: shows the first `TK_CAP`=8 MATCHING cards/column + a "Show all N" button, Closed caps
  harder at `TK_CAP_CLOSED`=3; overflow hidden via `.tk-card.tk-capped`, "Show all" per-column +
  session-only so a reload re-caps). `applyCap` runs after filter/sort (order-dependent). **Two-row
  control area (2026-07-20):** a **filter row** (`.tk-filters`: Search + Client/Department/Person +
  Clear) and a separate **display row** (`.tk-display`) with labelled **segmented toggles** — **VIEW**
  (`#tk-view-seg` Flat board / Swimlanes) + **DENSITY** (`#tk-dens-seg` Comfortable / Compact) — plus
  the **Sort** select, the **Cap** checkbox, and an **"N shown"** count (`#tk-shown-n`). The **Priority
  filter was removed** (Urgent auto-floats + Sort-by-priority covers it). Everything runs through ONE
  `refresh()` (filter → then flat-board sort+caps OR swimlanes); prefs (`view`/`dens`/`sort`/`cap` +
  filters) persist in `agora.tkprefs` (search text excluded); `segGet`/`segSet`/`segWire` drive the
  segmented controls. **Swimlanes (`#tk-swim`, VIEW=Swimlanes)** is built CLIENT-SIDE from the same
  `.tk-card` nodes' `data-*` (grouped per client → the 4 stage columns as chips); it respects the
  active filters/search (skips hidden cards) and chips reuse the board's detail overlay via
  `data-open`+`tkOpen` — no server render. `[data-flatonly]` controls (Density/Cap) hide in Swimlanes.
  The main-task rename input (`.tk-main-rename`) carries a faint dashed underline at rest so it reads
  as editable (was fully transparent → looked like static text).
  Overlays server-rendered into `#tk-store`, forms post
  `redirect=console` + `back_task`/`back_tab` so the overlay REOPENS on the same tab after the
  reload). The detail overlay is TABBED: persistent glance chips (priority/hold/start/launch/charge/progress) above
  Details | Tasks | Comments panels (`data-tktab`); the New/Edit form's optional fields live in a
  collapsible `<details class="tk-extra">`. Team routes
  `POST /w/<c>/admin/task{,/move,/delete,/maintask,/subtask,/comment}` (`is_superadmin()`;
  `/maintask` op=add|assign|rename|delete — rename = the overlay's edit-in-place title input
  (`.tk-main-rename`); `/subtask` op=add|toggle|edit|assign|delete — **op=edit inline-renames a
  sub-task + edits its INTERNAL `dod` "done when"** (`workspace.edit_subtask`, each field patched
  only when the form carried it; the `.tk-sub-name`/`.tk-sub-dod` inputs autosubmit on blur like the
  main-task rename), op=add takes `maintask_id`; delete →
  Bin `kind:"task"` → `workspace.insert_task` on restore). **Detail-overlay chrome is now sticky**
  (2026-07-20): `.tkd` is a capped flex column (`max-height: calc(100vh - 72px)`) with only
  `.tkd-body` scrolling, so `.tkd-head`, the `.tkd-tabs` bar (sticky, full-bleed), and the `.tkd-foot`
  (Archive / advance-stage) stay visible on a long task. **Edit lives ONLY in the header**
  (`.tkd-headedit`, `data-tkedit-open`) — the duplicate footer Edit was removed 2026-07-21 now that
  the footer never scrolls off, leaving the footer with one primary action per stage.
  Overlay forms carry
  `back_task`/`back_tab` and `_task_reply` forwards them as `?task=<c>:<id>&tab=` so the console
  script REOPENS the same detail overlay on the same tab after the redirect (params scrubbed via
  replaceState; the Delete form deliberately carries none). The filter bar has a client-side
  **sort selector** (`#tk-f-sort` Priority / Launch date / Client, reordering cards via
  `data-priority/data-due/data-cname`; the default matches the server order). Client side: the `progress` tab (nav LABEL
  "Tasks" since 2026-07-29 — the key stays `progress` in every route, never rename it) renders
  `main._progress_tasks(ws)` (client_facing + client-safe fields ONLY — owners/priority/charge/
  internal notes never reach the client HTML; the breakdown arrives as owner-less **phases**;
  the modal shows a Started → Going live timeline; cards say "Launching <date>" / "Live"; columns
  sort by soonest launch; the client's column names come from **`TASK_CLIENT_STAGES`**, the team's
  from `TASK_STAGE_META`). 🔴 **Those two are different tuples again since 2026-08-04** (Sentinel
  WP 1.2): the `blocked` stage reads **"Parked"** to the team and **"Paused"** to the client. It had
  degenerated into `TASK_CLIENT_STAGES = TASK_STAGE_META`, so the comment promising a one-line
  client-only relabel was wrong and the first attempt at one would have renamed the team's board too.
  Only the labels differ — the KEY is `blocked` on both surfaces, in every stored row and across the
  bridge; `_atrium_smoketest` asserts the client render never contains the word "Blocked".
  🔴 **Read-only for the CLIENT; the TEAM's board controls are BACK (2026-08-04, D2 amended —
  full read-only lasted one day; see the task-board bullet near the top of this file).** The
  drag-to-move + per-card delete ✕ (2026-07-28) are restored — markup (`data-pgdrag` /
  `data-pgcol` / `data-pgdel`), CSS and the separate wiring IIFE — plus the modal's team-only
  **✎ Edit** (`data-pgedit` / `data-pgeditform` → `POST /admin/task op=edit`). `ws["tasks"]` is
  the store both surfaces write through `workspace.py`, so this is one record, not two writers on
  a fork; a card CLAIMED by a Sentinel row is the exception (re-projected on Sentinel's next
  edit). `_atrium_smoketest.py` asserts the affordances present in the TEAM's HTML and absent
  from the CLIENT's — that check has flipped twice; its comment says why. (The per-stage count
  TILES went with D2 and stay gone; the column heads already show the counts.)
  🔴 **Never key team-only CSS off `[data-admin="1"]`** — the stylesheet ships to every viewer, so
  the literal string lands in a client's HTML and trips `_atrium_smoketest`'s no-leak assertion
  (cost a test round; the drag CSS keyed off `[data-pgcol]` for exactly this reason, back when it
  existed). The
  client-surface writes are `POST /w/<c>/task-comment` (comment / request-changes; resolve is
  team-only) and `POST /w/<c>/task-add` (the Progress quick-add composer, client AND team — the
  reporter is AUTO-TAGGED from the session as agora|client + a derived `reporter_name`, never a
  form choice; always client_facing, starts in_process, no internal fields accepted; client adds
  fire `notify.client_task_added`, and client-filed tasks carry a "Requested by" chip on Progress
  + a "Client req" pill on the console board). ⚠️ **The composer is a REAL `<form
  method="post" action="/w/<c>/task-add">`, wired in its OWN IIFE placed FIRST in the trailing
  `<script>` block** — deliberately independent of the Progress-board IIFE below it. The original
  build put the quick-add inside that board IIFE, where it inherited
  `if (!root || !veil || !storeBox) { return; }` (veil + hidden detail store = board furniture)
  and failed SILENTLY — the composer rendered, clicking did nothing, and Cloud Run logged no POST
  at all. Now: no-JS native post → `redirect=progress` → 302 back to the tab; JS present → fetch →
  JSON → reload with the input refocused; fetch failure → `form.submit()` so a typed request is
  never lost. Covered by the "no-JS form post" checks in `_atrium_smoketest.py`.
  Notifications: `notify.client_task_commented/client_task_changes/
  team_task_commented/team_task_resolved`.
  **THE INTERNAL BRIDGE IS TWO-WAY (2026-07-29) — Sentinel edits these cards in place.** Its board
  already LISTED them (`GET /api/internal/tasks`); it can now open, edit, delete and comment on one
  without leaving its own board, because "open it in Atrium to edit" is a dead end, not an answer.
  Routes (all `_internal_gate`-HMAC'd, fail-CLOSED, **no session**): `GET /api/internal/task`
  (`_internal_task_detail` = the board view PLUS every internal field, the two-level breakdown, the
  comment thread and the history — wrapped by `_internal_task_envelope` with the roster/department/
  stage vocabularies so the caller's form can render Atrium's OWN pickers), and `POST
  /api/internal/task-{update,delete,comment}` alongside the older `-move`/`-add`. Purposes:
  `task-detail` · `task-update` · `task-delete` · `task-comment`.
  🔴 **Every one of them calls the SAME `workspace.py` helper the console form calls** —
  `update_task` / `set_task_hold` / `set_task_maintasks` / `move_task_stage` / `delete_task` /
  `add_task_comment` / `resolve_task_comment` — so the stored shape, the auto-derived
  `TASK_DEPT_LABEL`, the history entries, the client notifications and the Bin are identical
  whichever app the edit came from. Two details that only matter for a foreign caller: the delete
  calls `audit.trash_put` DIRECTLY (not `_trash`, which stamps the session actor — there is no
  session, so the Bin would credit "system"), and `_internal_audit` logs the Sentinel user's email
  with role `sentinel`. `workspace.set_task_maintasks` is the array-shaped breakdown setter that
  exists FOR this caller (Sentinel PATCHes the whole breakdown): an id this task doesn't already
  hold is **re-minted** here, and a sub-task keeping its id **keeps its internal `dod`**, which
  Sentinel neither sees nor sends. Covered end-to-end in `_atrium_smoketest.py` (gating, field
  round-trip, label derivation, dod preservation, Bin) + `_workspace_localtest.py` (the setter).
  **Delivery Calendar** = a 2nd Delivery nav pane
  (`data-section/pane="calendar"`) in `admin_atrium.html`: a month grid built CLIENT-SIDE from a
  hidden `#cal-store` of `.cal-ev` nodes (one per service WITH a `due_date`, server-rendered from
  `task_cols`), plotting each service on its **launch date**, discipline-tinted, ⏸ for on-hold;
  prev/next/today + a client filter; clicking an event reuses the Task Board's `tkOpen` to open the
  SAME detail overlay (undated services simply don't appear). New services always start In Process
  (no stage picker on the New form; add route hardcodes `in_process`). `_task_fields_from_form`
  patches dates/charge/support ONLY when the form carried them (a partial POST can't wipe them).
  Spec: `/TASK_TRACKER_INTEGRATION.md`; tests live in
  `_workspace_localtest.py` (helpers) + `_atrium_smoketest.py` (routes, gating, no-leak render, calendar).
- **`audit.py`** — super-admin activity feed + restorable Trash; ONE private `audit.json` in the
  registry bucket (no new infra). `main.py` calls `_audit()`/`_trash()` from the mutation/delete
  routes; the console **Activity**/**Trash** tabs read it; deletes are restorable for 30 days (lazy
  auto-purge). Off-cloud test: `python _audit_localtest.py`.
- **`brand.py`** — bundled palette + AGORA mark (the container can't read repo-root `assets/`).
- **Google Tag Manager (site-wide, opt-in):** the `_inject_gtm` `after_request` hook in `main.py`
  injects the GTM container (`<head>` loader + `<body>` `<noscript>`) into **every** portal HTML page
  when env `GTM_CONTAINER_ID` is set — unset = no tag (so local preview stays untracked). GA4 is
  configured INSIDE the container in the GTM UI. The container ID ships from `deploy_dash_platform.ps1`
  (`$GTM_CONTAINER_ID`); reverse-proxied client dashboards (`/d/<c>/`) are skipped.
- **Response pipeline / performance:** the single `@app.after_request` (`_finalize_response`) runs
  `_inject_head` (the brand font + GTM + impersonation banner injection above; skips `/d/`), then
  **`_no_store_html`**, then `_maybe_gzip`.
  🔴 **`_no_store_html` is why a deploy actually reaches users — do not remove it.** This app has
  NO build step and NO asset hashing: every line of CSS/JS is INLINE in the one HTML document, so
  a cached HTML page is a cached copy of the WHOLE APP and that browser keeps running the old
  markup + old scripts forever. Portal HTML previously shipped with NO `Cache-Control` at all
  (only `Vary: Cookie`), leaving browsers free to heuristically cache it — the "I deployed but it
  didn't roll out" symptom (2026-07-27; Sentinel hit the identical bug, see its no-cache
  middleware). Now every `text/html` response gets
  `no-store, no-cache, must-revalidate, max-age=0` + `Pragma`/`Expires`. It is **HTML-only** and
  never overrides an existing `Cache-Control`, so the authed `/creative/` + `/data.json` proxies
  keep their `private, max-age=…`. Covered by the "no-store" + "creatives keep their own cache
  policy" checks in `_atrium_smoketest.py`. **gzip** compresses every text response (html/json/js/css/svg/xml ≥1 KB) when the
  client sends `Accept-Encoding: gzip` — the biggest load-time win, since the atrium shell (~456 KB)
  and the ~1 MB client dashboards were shipped uncompressed (Cloud Run/gunicorn don't compress). It
  applies to the proxied `/d/` dashboards too (which `_inject_head` skips), sets `Content-Encoding` +
  `Vary: Accept-Encoding`, and no-ops on already-encoded/tiny/non-text/streamed responses. The
  per-client dash (`clients/client_template/dash/main.py`) has the SAME `_maybe_gzip` hook (so direct
  `<c>.agoradatadriven.com` hits AND the portal→upstream proxy hop are compressed). GCS reads on the
  request hot path (`store.load_registry`, `workspace._read_object`) do ONE round-trip — download +
  catch `NotFound` — not the old `exists()`-then-`download()` two-trip pattern.
  🔴 **LAZY PANES — a tab whose render model costs GCS reads is built ONLY when it is active
  (2026-08-07).** `_watcher_view` reads EVERY watched source's archive object (one download each,
  transcripts/article text run to MBs) and `watcher_template` pre-seeds **5 sources into every
  client**, so building it on every render made a Dashboard / Communications / Tasks load pay 5+
  downloads for a pane it never showed — and since Atrium HTML is `no-store`, every refresh paid
  again. Now **three things move together, and all three are asserted** by
  `_run_lazy_pane_checks` in `_watcher_localtest.py` (which COUNTS `read_watcher_videos` calls, so
  it guards the performance contract, not just the markup):
  1. `main.atrium` passes `watcher=[]` unless `tab == "watcher"` — get this wrong and you render a
     pane with no data, i.e. a **blank tab**;
  2. `atrium.html` wraps the pane in `{% if view.active_tab == 'watcher' %}` (inside the existing
     `is_superadmin` guard, which also spans the Assistant pane — do not close that one early);
  3. the `#ax-nav` click handler **skips `preventDefault` when the pane is absent from the
     document** so the browser follows the link's own href — get this wrong and it is a **dead
     link**. Absence of the pane IS the signal, so no per-tab list is hardcoded, and a real
     navigation gives real history. `popstate` reloads for the same reason.
  This is safe only because every Watcher wiring block is null-guarded (`qsa(...).forEach` over an
  empty NodeList, `if (!cgrid …) return`, `if (!modal) return`) — a lazy pane whose JS binds
  unguarded at load time would throw. **`_company_content_view` is deliberately NOT lazy:** the
  Company tab is CLIENT-visible, and it reads only `own`-flagged sources (0–1 per client), so
  gating it would trade a full page load for at most one read.
  🔴 **Walking the WHOLE estate = `workspace.load_workspaces(keys)`, never a `for key:
  load_workspace` loop (2026-08-07).** Each load is one blocking GCS GET of a 50–150 KB object, so a
  serial loop costs N round-trips end to end. Five routes did it: the operator console
  `admin_atrium()` (the admin **landing page** — `/` redirects a super-admin there), the task-board
  export, and the three `/api/internal/*` legs (`tasks`, `clients`, `watcher/channels`) — where
  **Atrium's serial latency WAS Sentinel's page latency**, because its board and Mentor Library block
  on those calls. `load_workspaces` fans out over a `ThreadPoolExecutor` (`LOAD_WORKERS` = 8, sized
  for the estate) and returns `{key: ws_or_None}`. Reads only, so last-write-wins ordering is not in
  play; the shared GCS client was **already** used from 8 threads at once in production (gunicorn
  `--threads 8`), so this adds no new sharing hazard — but it PRE-WARMS the client on the calling
  thread so workers never race to build the singleton. ⚠️ A read that FAILS returns None,
  indistinguishable from "not seeded": deliberate, so one unreadable workspace degrades to one
  incomplete row instead of a 500. That also **fixed a real fragility** — `admin_atrium()` had no
  `try/except` at all, so a single transient GCS blip 500-ed the entire console; it now renders that
  one client as unseeded. `_workspace_localtest.py` §13 covers the mapping, the None-degradation, the
  single-key path *and* asserts it is genuinely concurrent by injecting a per-read delay (6 × 50 ms
  landed at 61 ms, vs 300 ms serial) — nothing else in the suite can catch a regression to a serial
  loop, because the local-fs backend answers instantly.
  **Templates are compiled at IMPORT** (`_warm_templates`, bottom of `main.py`). Jinja compiles a
  template on first render, and that is not cheap here: **measured 266 ms for `atrium.html` + 74 ms
  for `admin_atrium.html`** (~58% of the 585 ms module import). ⚠️ Be precise about what this buys —
  it **redistributes** the cost, it does not remove it: the very first request after a scale-from-zero
  still waits for startup either way. What it fixes is that the spike no longer lands *inside* an
  arbitrary request — and with gunicorn `--workers 2` each worker compiles lazily on ITS own first
  request, so **two** unlucky users used to eat it. Best-effort by design: a broken template must
  still surface as the normal render-time traceback, never as a container that won't boot.
  (A build-time Jinja bytecode cache would genuinely cut it from every cold start, but it needs
  `main` to import during `docker build` — which constructs a GCS client — so it was judged too
  fragile to be worth ~300 ms.) `_mail_view` reads the mailbox registry object ONCE
  (`public_mailboxes()` already calls `mail_mailboxes()` internally; it used to do both).
- **Local hot reload (`_DEV_RELOAD`, LOCAL ONLY):** `TEMPLATES_AUTO_RELOAD` + `use_reloader`, gated
  on the local-fs backend **AND** relaxed cookies — the same interlock `DEV_NOAUTH` uses, and it can
  only take effect through the `app.run` at the bottom of `main.py`, which gunicorn never calls. So
  it is inert in production two ways over. Before this, Jinja compiled `atrium.html` once per
  process, so **editing the template did nothing until you restarted** `run_local.ps1` (re-seeding
  the demo data on the way) — the slowest edit loop in the repo. The Werkzeug **debugger** is
  deliberately NOT enabled (`debug=True` is absent): its interactive traceback console executes
  arbitrary code, and reloading is all that was wanted. Verify it is live by the `* Restarting with
  stat` line plus `Debug mode: off`.

**Deploy:** `deploy_dash_platform.ps1` (build → `gcloud run deploy platform-dash --no-invoker-iam-check`).
It mounts the Google sign-in secrets (`google-oauth-client-id` / `google-oauth-client-secret`) ONLY if
they exist, so a default deploy stays unaffected (button off) until you create them + grant the web SA
`secretmanager.secretAccessor` on each. Register the redirect URI
`https://portal.agoradatadriven.com/auth/google/callback` on the OAuth client.
**Test (off-cloud, what CI runs):** `python _workspace_localtest.py`, `python _accounts_localtest.py`,
`python _google_oauth_localtest.py`, `python _atrium_smoketest.py`, `python _auth_smoketest.py`,
`python _audit_localtest.py`, `python _watcher_localtest.py`, `python _slashid_creative_test.py`,
`python _assistant_localtest.py` (hybrid retrieval), `python _intel_ai_localtest.py` (AI brain +
embeddings/reranking transport), `python _mail_localtest.py`, and `python _upwork_import_localtest.py`
from this dir.
**Preview:** `run_local.ps1` (or `preview/Preview Portal (admin).cmd` at repo root).
