# -*- coding: utf-8 -*-
"""Alerts mode for the agora-upwork-refresh image (job `agora-upwork-alerts`,
every 10 min): pull new Zenfl messages -> parse -> AI fit-score ONLY the new
jobs (same brief+rubric as score_jobs.py) -> Slack-alert every job scoring
>= SCORE_MIN (default 90) -> persist the scores into the shared
upwork/job_scores.sqlite so the dashboard's Fit column has them and the batch
scorer never re-pays for them.

The pull advances the SAME watermark the nightly rebuild reads, so alert pulls
feed the dashboard automatically (the nightly job runs --force and rebuilds
from every increment). Slack webhook comes from Secret Manager env
SLACK_WEBHOOK; a placeholder value (anything not https://hooks.slack.com/...)
disables the whole run gracefully so this can deploy before the webhook exists.
"""

import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

BUCKET = os.environ.get("DATA_BUCKET", "agora-data-driven-agora-dash")
SCORE_MIN = int(os.environ.get("SCORE_MIN", "90"))
MAX_ALERTS_PER_RUN = 20
SCORES_OBJ = "upwork/job_scores.sqlite"
LOCAL_SCORES = "/tmp/job_scores.sqlite"

import requests


def _metadata_token():
    r = requests.get(
        "http://metadata.google.internal/computeMetadata/v1/instance/"
        "service-accounts/default/token",
        headers={"Metadata-Flavor": "Google"}, timeout=10)
    r.raise_for_status()
    return r.json()["access_token"]


_tok = {"v": "", "ts": 0.0}


def metadata_token(force=False):
    """Drop-in replacement for score_jobs.get_token (no gcloud CLI in the image)."""
    if force or not _tok["v"] or time.time() - _tok["ts"] > 2400:
        _tok.update(v=_metadata_token(), ts=time.time())
    return _tok["v"]


def job_row(job):
    """parse_job() dict -> the JOB_COLS-ordered tuple score_jobs.job_text expects."""
    return (job["url"], job["date"], job["title"], job["category"],
            job["budget_type"], job["rate_min"], job["rate_max"],
            job["fixed_budget"], job["level"], " • ".join(job["skills"]),
            job["description"], job.get("country"), job.get("rating"),
            job.get("reviews"), job.get("client_jobs"), job.get("hire_rate"),
            job.get("avg_rate"), job.get("spent"), job.get("verified", 0))


def slack_post(webhook, text):
    r = requests.post(webhook, json={"text": text}, timeout=15)
    return r.status_code < 300


def budget_str(job):
    if job["rate_min"] or job["rate_max"]:
        return "$%g-%g/hr" % (job["rate_min"] or 0, job["rate_max"] or job["rate_min"] or 0)
    if job["fixed_budget"]:
        return "$%g fixed" % job["fixed_budget"]
    return "budget not stated"


def alert_text(job, score, reason):
    bits = [budget_str(job)]
    if job.get("country"):
        bits.append(job["country"])
    if job.get("spent"):
        bits.append("client spent $%s" % "{:,.0f}".format(job["spent"]))
    return (":rotating_light: *%d/100* — %s\n%s\n_%s_\n<%s|Open on Upwork>"
            % (score, job["title"], " · ".join(bits), reason, job["url"]))


def load_scores_db(bucket):
    """Download the shared scores db (returns its generation for the
    conditional write-back; 0 = object doesn't exist yet)."""
    blob = bucket.get_blob(SCORES_OBJ)
    gen = 0
    if blob is not None:
        blob.download_to_filename(LOCAL_SCORES)
        gen = blob.generation
    db = sqlite3.connect(LOCAL_SCORES)
    db.execute("""CREATE TABLE IF NOT EXISTS scores (
        url TEXT PRIMARY KEY, score INTEGER NOT NULL, reason TEXT,
        model TEXT, scored_at TEXT, in_tokens INTEGER, out_tokens INTEGER)""")
    return db, gen


def upload_scores_db(bucket, gen):
    """Conditional upload: loses gracefully to a concurrent batch-scorer sync
    (the batch run re-scores anything clobbered, so nothing is ever lost)."""
    try:
        bucket.blob(SCORES_OBJ).upload_from_filename(
            LOCAL_SCORES, if_generation_match=gen, timeout=120)
        return True
    except Exception as exc:
        print("scores upload skipped (%s) - batch scorer will backfill"
              % type(exc).__name__)
        return False


def run_alerts(pull_output):
    webhook = os.environ.get("SLACK_WEBHOOK", "").strip()
    if not webhook.startswith("https://hooks.slack.com/"):
        print("SLACK_WEBHOOK not configured (placeholder) - alerts disabled.")
        return

    m = re.search(r"wrote (\S+\.jsonl)", pull_output)
    if not m:
        print("no new messages - nothing to alert.")
        return
    import process_upwork as pu
    import score_jobs as sj
    sj.get_token = metadata_token           # image has no gcloud CLI
    sj.ACTIVE = ["global", "us-central1", "asia-southeast1"]

    jobs, seen = [], set()
    with open(m.group(1), "r", encoding="utf-8") as f:
        for line in f:
            msg = json.loads(line)
            job = pu.parse_job(msg) if msg.get("from") == pu.BOT_NAME else None
            if job and job["url"] not in seen:
                seen.add(job["url"])
                jobs.append(job)
    print("%d new job posts in this pull" % len(jobs))
    if not jobs:
        return

    from google.cloud import storage
    bucket = storage.Client().bucket(BUCKET)
    db, gen = load_scores_db(bucket)
    done = {u for (u,) in db.execute("SELECT url FROM scores")}
    fresh = [j for j in jobs if j["url"] not in done]
    print("%d unscored (rest already scored = already known)" % len(fresh))
    if not fresh:
        return

    system_prompt = open(sj.BRIEF_PATH, encoding="utf-8").read() + "\n\n" + sj.RUBRIC
    session = requests.Session()
    hits, n_err = [], 0
    for i, job in enumerate(fresh):
        try:
            score, reason, tin, tout, _ = sj.score_one(
                system_prompt, sj.job_text(job_row(job)), session, hint=i)
        except Exception as exc:
            n_err += 1
            print("  score failed: %s" % str(exc)[:150])
            continue
        db.execute("INSERT OR REPLACE INTO scores VALUES (?,?,?,?,?,?,?)",
                   (job["url"], score, reason, sj.MODEL,
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    tin, tout))
        if score >= SCORE_MIN:
            hits.append((job, score, reason))
    db.commit()
    db.close()

    hits.sort(key=lambda h: -h[1])
    posted = 0
    for job, score, reason in hits[:MAX_ALERTS_PER_RUN]:
        if slack_post(webhook, alert_text(job, score, reason)):
            posted += 1
    if len(hits) > MAX_ALERTS_PER_RUN:
        slack_post(webhook, "…plus %d more %d+ jobs this tick (see the dashboard)."
                   % (len(hits) - MAX_ALERTS_PER_RUN, SCORE_MIN))

    upload_scores_db(bucket, gen)
    print("scored %d (errors %d) | %d hit >=%d | %d alerted"
          % (len(fresh) - n_err, n_err, len(hits), SCORE_MIN, posted))
