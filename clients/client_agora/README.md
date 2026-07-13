# client_agora — Agora's own internal dashboard

Tab 1: **Upwork job-demand analytics** over the Zenfl Upwork Bot Telegram feed
archive. Built for spotting what services are in demand (Paid Media, AI/ML,
Automation, …) and for interns to browse real jobs (skills, description, link).

## Pipeline (raw is never visualized directly — and fully in GCP since 2026-07-14)

```
Telegram (Zenfl Upwork Bot chat)
   │  Cloud Run job `agora-upwork-refresh` (daily 07:15 Manila; Telethon pull)
   ▼
gs://…-agora-dash/raw/         result.json base export + pulled/*.jsonl increments + watermark
   │  same job: processing/process_upwork.py (streams base+increments off the volume)
   ▼
gs://…-agora-dash/upwork/      jobs.sqlite (unique job URLs + skills/tags + FTS5),
   │                           aggregates.json, job_scores.sqlite (scorer-owned,
   │                           SEPARATE so it survives jobs.sqlite rebuilds)
   ▼  (dash hot-reloads newer objects within ~2 min — no restart)
Cloud Run service `agora-dash`               (asia-southeast1, SA agora-dash-web@)
```

Laptop equivalents (fallback): `raw_files/` + `refresh_upwork.ps1` +
`dash/deploy_dash_agora.ps1` (see the refresh section below).

## Agora-fit scoring (processing/score_jobs.py)

Every job gets an LLM score 0–100 for "is this a good fit for Agora" + a 1–2 sentence
reason, using **gemini-2.5-flash-lite on Vertex** (project agora-data-driven, thinking
off, JSON schema output). The system prompt = `processing/agora_job_fit_brief.md`
(company profile, edit to change judging) + the rubric inside the script. Run:

```
python processing/score_jobs.py --smoke        # 3 jobs, verbose
python processing/score_jobs.py --limit 500    # seeded random sample
python processing/score_jobs.py --all          # every unscored job (~$43 sync @ 16 workers)
python processing/score_jobs.py --report       # distribution + samples
```

Resumable (scored URLs are skipped); auth = VERTEX_ACCESS_TOKEN env or `gcloud auth
print-access-token`. Scores land in `dash/data/job_scores.sqlite`; the dash attaches it
read-only and exposes a **Fit** column (sortable, filterable, reason on row-expand),
`fit`/`title`/`skill`/`min_rate` URL params, and `GET /api/export.csv` (filtered slice,
20k-row cap). The whole dashboard (charts/KPIs) follows the fit filter like any other.

- **Processor** parses each bot message's `text_entities` (title, category,
  budget/rate, level, skills, client stats, description, feed name, job URL),
  dedupes by URL, and classifies every job into demand tags
  (`TAG_PATTERNS` in `process_upwork.py` — edit there to add a tag, then re-run).
- **Dashboard** (`dash/main.py` + `dash/dashboard.html`) is a small Flask API
  (`/api/aggregates`, `/api/stats`, `/api/jobs`) + one self-contained HTML page:
  weekly demand chart with tag comparison, top skills/categories/countries,
  momentum (last 4 full weeks vs prior 4), and the filterable jobs table.
  Filters live in the URL, so a pre-filtered view can be linked/iframed.
- The service sets `Content-Security-Policy: frame-ancestors *` — it is meant
  to be **iframed anywhere** and carries no auth (aggregated public job posts).

## Known data artifact — the Oct 2025 cliff (investigated 2026-07-13)

Weekly volume fell ~5,800 → ~1,500 between 2025-10-13 and 2025-11-03. This is
**not market demand and not a feed-setting change** (the chat audit trail shows
no feed edits anywhere near the drop — the last change was creating the "Data
Science" feed on 2025-08-08, which caused the *rise*). Zenfl itself was down
2025-10-17 → 10-28 (it apologized and extended subscriptions 7 days) and came
back with a rebuilt pipeline: it stopped forwarding jobs without a posted rate
(~27% of volume → 0%) and delivers ~4× fewer matching jobs overall (hourly hit
hardest; low-volume niche feeds like Paralegal were unaffected; the "USA"
country-label variant also vanished — backend change). Rate/country/category
mixes are otherwise unchanged, so it is a coverage cut, not a filter.

The dashboard handles this three ways (all shipped 2026-07-13):

1. **Chain-linked coverage calibration** (`calibrate()` in the processor):
   every feed stream gets a delivery factor per pipeline era, measured in
   6-week windows adjacent to each outage boundary (a demand level cannot
   jump 3× in two weeks — the boundary jump isolates the pipeline change).
   Each job carries `weight = 1/factor`; summing weights instead of counting
   rows gives the **Comparable** series the chart shows by default (default
   range: since 2025-03-31). KPI baselines and Momentum use adjusted numbers.
   Raw counts stay one click away ("Raw count").
2. **"% of jobs" share mode** per tag — fully assumption-free comparability.
3. **Every outage banded + feed creations marked** on the chart, so any
   remaining step has a visible explanation.

## Refresh — FULLY AUTOMATED IN GCP (since 2026-07-14)

Cloud Run **job `agora-upwork-refresh`** (`job/`) runs daily at **07:15
Asia/Manila** (Cloud Scheduler `agora-upwork-refresh-daily`): it pulls new bot
messages straight from Telegram (`processing/telegram_pull.py`, Telethon as a
user account — the Bot API can't read another bot's chat; session + api creds
from Secret Manager `agora-telegram-session` / `agora-telegram-api`),
reprocesses (streaming the base export off a gcsfuse mount of the dash
bucket), and uploads `upwork/jobs.sqlite` + `aggregates.json`. The dash
service **hot-reloads** any newer data object within ~2 min (`_data_refresher`
in `dash/main.py` — per-worker thread, atomic swap, stats-cache clear), so the
job needs zero permissions on the service and there is no restart.

Cloud state layout (all in `gs://agora-data-driven-agora-dash/`):
`raw/result.json` (base Desktop export), `raw/pulled/*.jsonl` (increments),
`raw/pull_state.json` (watermark), `upwork/*` (processed outputs). No laptop
involved. Redeploy after edits: `job/deploy_job_agora.ps1` (`-Run` also
executes once); it stages `processing/{telegram_pull,process_upwork}.py` into
the image, so **rerun it after editing either script**. Run on demand:
`gcloud run jobs execute agora-upwork-refresh --region asia-southeast1`.

### Laptop fallback (kept, normally OFF)

`refresh_upwork.ps1` runs the same loop locally against `raw_files/`
(`-Score` also runs the fit scorer; `-SkipPull` for a manual export;
`-Force` rebuilds with 0 new). `fetch_telegram_secrets.ps1` restores the
Telegram creds/session from Secret Manager onto a new machine;
`install_refresh_task.ps1` registers the daily local task — do NOT run it
while the cloud job is on (two watermarks diverge; harmless but wasteful).
⚠️ Local and cloud keep SEPARATE pull state — if you ever switch back,
delete the stale side's `pull_state.json`+`pulled/` or expect a re-pull.

From-scratch Telegram setup (session invalidated / new account):
https://my.telegram.org → *API development tools* → create an app → write
`raw_files/telegram_api.json` as `{"api_id": <id>, "api_hash": "<hash>"}` →
`..\..\.venv\Scripts\python.exe processing\telegram_pull.py --login` (phone +
code, once) → re-upload the secrets (commands in `fetch_telegram_secrets.ps1`'s
header). The venv needs `telethon` + `ijson` (installed 2026-07-14).

How the increments work: the puller bootstraps its watermark from the last
message id in the base `raw_files/result.json`, then appends each pull as
`raw_files/pulled/*.jsonl` in the SAME shape as the Desktop export
(`text_entities` rebuilt from Telethon's UTF-16 entity offsets), so
`process_upwork.py` parses base + increments with one code path and URL
dedupe absorbs any overlap. If you ever redo a full Desktop export, replace
`result.json`, delete `raw_files/pull_state.json` + `raw_files/pulled/*`, and
the next pull re-bootstraps. `refresh_upwork.ps1 -Score` also runs the fit
scorer (resumable, unscored jobs only); `-SkipPull` / `-Force` cover the
manual-export and rebuild-anyway cases.

Deploys/uploads run as `info@agoradatadriven.com` (the scripts set
`CLOUDSDK_CORE_ACCOUNT`). Full image rebuild: `dash/deploy_dash_agora.ps1`.

## Next (the real data-engineering treatment)

Move the processed store into BigQuery per the repo's three-stage contract
(`sql/` views → export job → dash). The pull+process loop is already a Cloud
Run job (see above).
