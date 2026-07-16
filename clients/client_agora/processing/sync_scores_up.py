# -*- coding: utf-8 -*-
"""Live-sync job_scores.sqlite to GCS while score_jobs.py runs.

Every --interval seconds: snapshot the scores db (sqlite backup API, so the
copy is consistent while the scorer is mid-write), and upload it to the dash
bucket if the row count grew. The dash service's scores refresher (see
dash/main.py) picks the new object up within ~2 minutes — the live dashboard
fills in as the run progresses, no redeploys.

Run alongside the scorer; Ctrl-C / kill to stop. Uploads as
info@agoradatadriven.com (the account with bucket write, same as deploys).
"""

import argparse
import os
import sqlite3
import subprocess
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SCORES_DB = os.path.join(HERE, "..", "dash", "data", "job_scores.sqlite")
SNAP = os.path.join(HERE, "..", "dash", "data", "job_scores.upload.sqlite")
DEST = "gs://agora-data-driven-agora-dash/upwork/job_scores.sqlite"


def snapshot():
    src = sqlite3.connect(SCORES_DB)
    if os.path.exists(SNAP):
        os.remove(SNAP)
    dst = sqlite3.connect(SNAP)
    with dst:
        src.backup(dst)
    n, = dst.execute("SELECT COUNT(*) FROM scores").fetchone()
    dst.close()
    src.close()
    return n


def upload():
    env = dict(os.environ, CLOUDSDK_CORE_ACCOUNT="info@agoradatadriven.com")
    r = subprocess.run('gcloud storage cp "%s" "%s"' % (SNAP, DEST),
                       shell=True, env=env, capture_output=True, text=True, timeout=300)
    return r.returncode == 0, (r.stderr or "")[-200:]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=int, default=120)
    args = ap.parse_args()
    last_n = -1
    while True:
        try:
            n = snapshot()
            if n != last_n:
                ok, err = upload()
                if ok:
                    last_n = n
                    print("synced %d scores -> %s" % (n, DEST), flush=True)
                else:
                    print("upload failed: %s" % err.replace("\n", " "), flush=True)
            else:
                print("no change (%d rows)" % n, flush=True)
        except Exception as exc:
            print("sync error: %s" % exc, flush=True)
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
