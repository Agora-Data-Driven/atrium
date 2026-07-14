"""Hourly client-mail refresh -- pull + archive + AI-summarize every client's email correspondence.

Runs as a Cloud Run JOB (`mail-refresh`) on an hourly Cloud Scheduler tick, REUSING the platform-dash
image + runtime SA (mirrors intel_refresh.py exactly). No new service/bucket/SA: it writes the SAME
workspace objects the app's Sync-now button does, via mailroom.sync_client -- per-client contact
matching, per-thread archive objects, AI thread summaries, and the rolling digest.

Gated + graceful: a logged no-op unless MAIL_SYNC_ENABLED=1; a client with no contacts (or no
connected mailboxes at all) is logged and skipped, never fatal. Mailbox credentials come from the
private registry-bucket object (workspace.mail_mailboxes); the Workspace-delegation connector also
needs MAIL_DWD_SA (set by deploy_mail_refresh.ps1 when the mail-sync SA exists).

Off-cloud testable via WORKSPACE_LOCAL_DIR + REGISTRY_LOCAL_DIR; refresh_all passes the same
injection seams mailroom.sync_client takes (see _mail_localtest.py).
"""

import os
import sys

import mailroom
import store
import workspace


def _enabled():
    """True iff the hourly sync is switched on. Fail-closed (default OFF), like intel_refresh."""
    return os.environ.get("MAIL_SYNC_ENABLED", "") in ("1", "true", "True")


def refresh_all(poster=None, getter=None, token_fetcher=None, imap_factory=None, ai_fetcher=None):
    """Sync every registered client (skipping the worked-example `template`). Returns a summary."""
    mailboxes = workspace.mail_mailboxes()
    if not mailboxes:
        print("[mail-refresh] no mailboxes connected -- nothing to pull.")
        return {}
    summary = {}
    for c in store.list_clients():
        key = c.get("key")
        if not key or key == "template":
            continue
        ws = workspace.load_workspace(key)
        if ws is None or not workspace.mail_contacts(ws):
            continue  # no workspace / no contacts -> nothing to match; not an error
        try:
            result = mailroom.sync_client(key, ws=ws, mailboxes=mailboxes, poster=poster,
                                          getter=getter, token_fetcher=token_fetcher,
                                          imap_factory=imap_factory, ai_fetcher=ai_fetcher)
        except Exception as exc:  # one bad client must not sink the whole run
            print("[mail-refresh] %s FAILED: %s" % (key, exc), file=sys.stderr)
            continue
        summary[key] = result
        print("[mail-refresh] %s -> +%d message(s), +%d thread(s), %d summarized%s"
              % (key, result["new_messages"], result["new_threads"], result["summarized"],
                 ("; " + "; ".join(result["errors"])) if result["errors"] else ""))
    return summary


def main():
    """Job entry point. No-op (logs why) unless MAIL_SYNC_ENABLED=1."""
    if not _enabled():
        print("[mail-refresh] disabled (set MAIL_SYNC_ENABLED=1 to run); nothing to do.")
        return
    print("[mail-refresh] starting client-mail sync (dwd sa: %s)"
          % (mailroom.dwd_sa() or "(not configured -- imap mailboxes only)"))
    summary = refresh_all()
    print("[mail-refresh] done -- %d client(s) synced" % len(summary))


if __name__ == "__main__":
    main()
