# services/portal/dash — the platform-dash Flask app (fast map)

The portal/CRM front-door + **Agora Atrium** client workspace, one Cloud Run service
(`platform-dash`, region `asia-southeast1`): single login over every client dashboard, reverse
proxy at `/d/<c>/…`, and the whole Atrium workspace at `/w/<c>/`.

This README is the **fast map** — file map, contract rows, cookbook, anchors. The deep
feature-by-feature engineering context stays in [`CLAUDE.md`](CLAUDE.md) (this dir); the repo
rules live in the root [`AGENTS.md`](../../../AGENTS.md). Don't duplicate prose between them.

## File map

App / library (grep the anchor to land in the right place):

| File | One line | Key anchors |
|---|---|---|
| `main.py` (~5,500 ln) | Every route: login, portal, proxy, Atrium, console, internal bridges | `WORKSPACE_NAME`:67 · `_maybe_gzip`:361 · `_no_store_html`:394 · `ATRIUM_TABS`:990 · `ATRIUM_TEAM_TABS`:1000 · `_progress_tasks`:1297 · `_internal_gate`:1376 · `_communications_view`:3994 · `TASK_DEPT_LABEL`:4294 · `TASK_STAGE_META`:4316 |
| `workspace.py` (~2,600 ln) | THE only reader/writer of `workspace/<c>.json` (no DB) | `TASK_STAGES`:1588 · `_STAGE_ALIASES`:1593 · `add_task`:1687 · `add_intel_entry`:2261 · `add_campaign`:1159 · `add_content`:1281 |
| `store.py` | Registry CRUD over the ONE private `platform.json`; resolves logins | UTF-8-bytes write at :102 |
| `atrium_view.py` | Pure presentation helpers (sparklines, calendar grid, awaiting rollup) | `_event_done` / `intel_sections` |
| `service_templates.py` | Recipe book seeding the Delivery board's work breakdown | `TEMPLATES`:34 · `AD_PRODUCTION`:168 · `build_maintasks`:234 |
| `assistant_ai.py` | Team-only RAG chat over the whole workspace (hybrid BM25+embed+rerank) | `build_chunks` · `ask_stream` · `DEPTHS` |
| `intel_ai.py` / `intel_feed.py` / `intel_refresh.py` | AI brain (Gemini/DeepSeek/Kimi registry) / legacy RSS / daily `intel-refresh` job | `MODELS` · `_call` / `stream_call` |
| `mailroom.py` / `mail_refresh.py` | Client email archive + AI digest / hourly `mail-refresh` job | `classify_thread` · `summarize_thread` |
| `watcher.py` / `watcher_blog.py` / `safe_scrape_local.py` | YouTube archive / blog twin / residential-IP safe-pull agent | `resolve_channel` · `resolve_site` |
| `atrium_health.py` / `atrium_docs.py` / `atrium_docview.py` | Site+tag check / Google-Doc reader / Office-doc HTML preview | stdlib-only, all graceful |
| `audit.py` / `notify.py` / `feedback.py` / `feedback_ai.py` | Activity+Trash (`audit.json`) / optional email / feedback store+AI | `_audit` callers in main.py |
| `google_oauth.py` / `platform_sso.py` / `sentinel_directory.py` | Google sign-in / shared HMAC SSO cookie (vendored to every dash) / Sentinel user-lookup | |
| `upwork_import.py` / `sync_dash.py` / `sync_refresh.py` | Upwork thread parser / export-job trigger / 6-hourly `sync-refresh` job | `trigger_all` |
| `brand.py` / `config.py` / `logo_to_svg.py` | Brand kit / registry seed data / logo wrapper | |
| `onboard_client.py` / `seed_registry.py` / `seed_workspace.py` / `seed_local.py` | One-step onboarding / one-time seeds (refuse to clobber) / local demo seed | |

Templates (`templates/`): `atrium.html` (~8,300 ln — the whole client workspace; Jinja
`tab_titles`:115 / `tab_subtitles`:119, JS `titles`:3982 / `subtitles`:3990 mirrors,
`wireAssistantChat`:5368), `admin_atrium.html` (~3,400 ln — operator console + Delivery board),
`portal.html`, `login.html`, `admin.html`, `superadmin.html`, `signup.html`,
`request_access.html`, `profile.html`, `dashboard_view.html`, `recap.html`.

Ops scripts (all in this dir unless noted): `deploy_dash_platform.ps1` (fast redeploy),
`../deploy.ps1` (full portal standup), `deploy_intel_refresh.ps1` / `deploy_mail_refresh.ps1` /
`deploy_sync_refresh.ps1` (the three jobs — **image-pinned: rerun after editing their modules**),
`enable_assistant_dash_data.ps1`, `enable_assistant_reranking.ps1`, `enable_atrium_mail.ps1`,
`enable_atrium_uploads.ps1` (opt-in infra), `install_safe_pull_task.ps1` + `safe_pull_agent.vbs`
(operator machine), `run_local.ps1` (local no-password preview).

## Contract rows (writer → route → consumer)

The pattern for every feature: `workspace.py` is the only writer, `main.py` owns the route,
the template consumes. Verified examples:

| Data | workspace.py writer | main.py route | Consumer |
|---|---|---|---|
| Task | `add_task`:1687 | `POST /w/<c>/admin/task` (:4572) | `admin_atrium.html`:1753 board · `atrium.html` Tasks pane via `_progress_tasks`:1297 |
| Intel entry | `add_intel_entry`:2261 | `POST /w/<c>/admin/intel` (:2912) | `atrium.html`:3129 (`view.intel`) |
| Content piece | `add_content`:1281 | `POST /w/<c>/admin/content` (:2291) | `atrium.html`:2402 (content loop) |

Client-safe filtering happens **server-side before the template** (`_progress_tasks`,
`_communications_view`) — that is the no-leak boundary; never move it into Jinja/JS.

## Cookbook

1. **Add a client-visible tab** — key into `ATRIUM_TABS` (`main.py:990`), title/subtitle into
   BOTH the Jinja maps (`atrium.html:115/119`) AND the JS mirrors (`atrium.html:3982/3990`), add
   the pane + nav entry in `atrium.html`. Team-only instead → add the key to `ATRIUM_TEAM_TABS`
   (`main.py:1000`) and gate nav+pane+route on `is_superadmin()`. Verify: `_atrium_smoketest.py`.
   Deploy: `deploy_dash_platform.ps1`.
2. **Change a tab LABEL (never the key)** — edit the label in all four maps above; the keys
   (`leadgen`, `progress`, …) stay in every route/data shape forever.
3. **Add an `/w/<c>/admin/*` op** — route in `main.py` gated `is_superadmin()` (copy the shape of
   `atrium_admin_intel`, `main.py:2912`), writer in `workspace.py` (the ONLY file that mutates
   `workspace/<c>.json`), `_audit(...)` the mutation. Verify: `_workspace_localtest.py` +
   `_atrium_smoketest.py`.
4. **Add an internal HMAC route (Sentinel bridge)** — `@app.route("/api/internal/…")` guarded by
   `_internal_gate("<purpose>")` (`main.py:1376`; `platform-sso-key`, X-Academy-Ts/Sig). Fail
   CLOSED, reuse `workspace.py` helpers so both apps produce identical state. Verify:
   `_atrium_smoketest.py`.
5. **Change a task stage LABEL (NEVER the key)** — `TASK_STAGE_META` (`main.py:4316`). Stage keys
   are canonical in `workspace.TASK_STAGES` (:1588); a retired key must be mapped to a live one in
   `workspace._STAGE_ALIASES` (:1593), never deleted.
6. **Edit the Assistant chat surfaces** — one `wireAssistantChat` (`atrium.html:5368`) serves the
   tab AND the FAB bubble; touch it once. Verify: `_assistant_localtest.py`.
7. **Run the full off-cloud suite** (all in this dir, plain `python <file>` from `dash/`; needs a
   Flask-capable interpreter — `run_local.ps1` builds `.venv-portal`; CI runs every one):
   `_workspace_localtest.py` · `_accounts_localtest.py` · `_google_oauth_localtest.py` ·
   `_audit_localtest.py` · `_intel_feed_localtest.py` · `_intel_ai_localtest.py` ·
   `_watcher_localtest.py` · `_assistant_localtest.py` · `_mail_localtest.py` ·
   `_upwork_import_localtest.py` — plus smoketests `_atrium_smoketest.py` (every tab renders,
   every POST persists) and `_auth_smoketest.py`, and the regression `_slashid_creative_test.py`.
8. **Redeploy** — validate first, then:
   ```powershell
   .\.venv\Scripts\python.exe tools\_validate_dash_js.py services\portal\dash\templates\atrium.html
   .\services\portal\dash\deploy_dash_platform.ps1   # gcloud run deploy platform-dash --region asia-southeast1 --no-invoker-iam-check
   ```
   Check what serves (whole traffic array — `traffic[0]` can be a tagged old revision):
   ```powershell
   gcloud run services describe platform-dash --project agora-data-driven --region asia-southeast1 --format="yaml(status.traffic)"
   ```

## Gotchas / DO-NOT-TOUCH

- Inline JS is **esprima-4.x-safe** (no `?.` / `??`) in every template; the CI gate enforces it.
- **Never style team affordances via `[data-admin="1"]` selectors** — CSS ships to every viewer
  and the literal string trips the no-leak check.
- **Tab keys and task-stage keys are canonical** — labels change, keys never do.
- The Jinja `tab_titles` map deliberately has no `mail` entry while the JS `titles` map still
  carries one (Mail folded into Communications 2026-07-15) — known cosmetic drift, don't "fix"
  one side without the other.
- The three Cloud Run jobs (`intel-refresh`, `mail-refresh`, `sync-refresh`) **reuse the
  platform-dash image but are image-pinned** — editing their modules requires rerunning their own
  deploy script, not just `deploy_dash_platform.ps1`.
- `workspace/*.json` / `platform.json` are written as **UTF-8 bytes only** (root AGENTS.md).

## Status (volatile — verify before trusting)

- Serving revision: **platform-dash-00178-7qp** (verified 2026-07-29).
- Registry bucket: `agora-data-driven-platform-dash` (workspaces, creatives, watcher archives,
  mail archives, assistant indexes, `audit.json`, `platform.json`).
