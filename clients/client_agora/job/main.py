# -*- coding: utf-8 -*-
"""Cloud Run job `agora-upwork-refresh` — the fully-in-GCP daily refresh.

Runs the SAME two scripts the laptop uses (telegram_pull.py + process_upwork.py,
vendored into this image by the deploy script), pointed at cloud paths:

  /data                gcsfuse volume = gs://agora-data-driven-agora-dash
    raw/result.json      base Desktop export (uploaded once by the deploy script)
    raw/pulled/*.jsonl   incremental pulls (this job appends)
    raw/pull_state.json  watermark
    upwork/              processed outputs (this job uploads; the dash service's
                         refresher thread hot-swaps them within ~2 min — no restart)
  /secrets/api/telegram_api.json  Secret Manager: agora-telegram-api
  /secrets/session/b64            Secret Manager: agora-telegram-session (base64)
  (separate dirs: Cloud Run mounts one secret per directory)

Steps: decode session -> pull (skip everything if 0 new) -> process (streams the
~1 GB base straight off the volume) -> upload jobs.sqlite + aggregates.json.
job_scores.sqlite is never touched here (separate store, scorer owns it).

Args: --force  rebuild/upload even when the pull finds nothing new.
"""

import base64
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("DATA_MOUNT", "/data")
BUCKET = os.environ.get("DATA_BUCKET", "agora-data-driven-agora-dash")
OUT_DIR = "/tmp/out"
SESSION_B64 = "/secrets/session/b64"
API_FILE = "/secrets/api/telegram_api.json"


def run_step(script, env_extra, args=()):
    """Run a pipeline script as a subprocess (keeps its CLI contract + prints),
    streaming output; returns (exit_code, captured_stdout)."""
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1", **env_extra)
    p = subprocess.Popen([sys.executable, os.path.join(HERE, script)] + list(args),
                         env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, encoding="utf-8", errors="replace")
    lines = []
    for line in p.stdout:
        print(line, end="", flush=True)
        lines.append(line)
    p.wait()
    return p.returncode, "".join(lines)


def main():
    force = "--force" in sys.argv

    # session: secret is base64 of the telethon sqlite file; /secrets is read-only
    with open(SESSION_B64, "r", encoding="ascii") as f:
        session = base64.b64decode(f.read().strip())
    with open("/tmp/telegram.session", "wb") as f:
        f.write(session)

    raw_dir = os.path.join(DATA, "raw")
    pull_env = {
        "TG_RAW_DIR": raw_dir,
        "TG_BASE_EXPORT": os.path.join(raw_dir, "result.json"),
        "TG_API_FILE": API_FILE,
        "TG_SESSION": "/tmp/telegram",           # telethon appends .session
        "TG_STATE_FILE": os.path.join(raw_dir, "pull_state.json"),
        "TG_PULLED_DIR": os.path.join(raw_dir, "pulled"),
    }
    code, out = run_step("telegram_pull.py", pull_env)
    if code != 0:
        sys.exit("telegram_pull.py failed (exit %d)" % code)
    m = re.search(r"NEW_MESSAGES=(\d+)", out)
    new_msgs = int(m.group(1)) if m else -1
    if new_msgs == 0 and not force:
        print("no new messages - done.")
        return

    os.makedirs(OUT_DIR, exist_ok=True)
    code, _ = run_step("process_upwork.py", {
        "UPWORK_RAW": os.path.join(raw_dir, "result.json"),
        "UPWORK_PULLED_DIR": os.path.join(raw_dir, "pulled"),
        "UPWORK_OUT_DIR": OUT_DIR,
    })
    if code != 0:
        sys.exit("process_upwork.py failed (exit %d)" % code)

    # upload via the storage client (not the fuse mount: bigger writes, retries)
    from google.cloud import storage
    bucket = storage.Client().bucket(BUCKET)
    for name in ("jobs.sqlite", "aggregates.json"):
        src = os.path.join(OUT_DIR, name)
        print("uploading %s (%.1f MB) ..." % (name, os.path.getsize(src) / 1e6), flush=True)
        bucket.blob("upwork/%s" % name).upload_from_filename(src, timeout=600)
    print("DONE - dash hot-reloads within ~2 min.")


if __name__ == "__main__":
    main()
