"""Flask web service for the `meloyelo` client dashboard (Stage 3 of the data contract).

Serving model -- private bucket, OPEN dashboard (no login), same as riverdance:
  The per-client data JSON (`meloyelo.json`) lives in a PRIVATE GCS bucket
  (`agora-data-driven-meloyelo-dash`) and is NEVER public -- it is only ever reachable
  through this service's `/data.json` proxy. The BUCKET stays private in every posture; what
  `DASH_OPEN` controls is whether this service asks for a password first.

  `DASH_OPEN` defaults to ON here (2026-07-27, operator's decision): the dashboard is embedded
  in an iframe inside the gated Agora Atrium workspace, where a password prompt would be a dead
  end. TRADE-OFF: anyone holding the exact Cloud Run URL can read the data -- the URL is
  unguessable, not secret.

  To re-gate, set `DASH_OPEN=0`. The password secret, the `/login` route and the session cookie
  are all still wired, and a portal SSO cookie is trusted additively (see `authed()`), so
  re-enabling is one environment variable with no redeploy of code.

The org forbids public Cloud Run, so this service is deployed with --no-invoker-iam-check
(never --allow-unauthenticated) and does its OWN password/SSO auth in-process.

Data contract: this client is Windsor/Shopify-LIVE (no BigQuery views) -- job/main.py assembles
`meloyelo.json` and the `data.*` keys it writes are exactly what dashboard.html reads.
dashboard.html is baked into the image and read relative to __file__, so there is no filesystem
dependency at runtime.
"""

import datetime
import hmac
import os

from flask import (
    Flask,
    Response,
    jsonify,
    redirect,
    render_template_string,
    request,
    session,
)
from google.cloud import storage

import platform_sso

# --- Configuration from the environment --------------------------------------------------
# SESSION_SECRET signs the Flask session cookie; it is mounted from Secret Manager
# (meloyelo-dash-session-key) at deploy time. A missing secret is a hard misconfig.
SESSION_SECRET = os.environ.get("SESSION_SECRET", "")
# The dashboard's own password (mounted from meloyelo-dash-password). compare_digest below
# is constant-time, so never compare it with `==`.
DASH_PASSWORD = os.environ.get("DASH_PASSWORD", "")
# The PRIVATE bucket + object holding this client's exported data JSON.
GCS_BUCKET = os.environ.get("GCS_BUCKET", "agora-data-driven-meloyelo-dash")
DATA_OBJECT = os.environ.get("DATA_OBJECT", "meloyelo.json")
# No-login mode. ON by default for MeloYelo (2026-07-27, at the operator's request): the
# dashboard is embedded in the gated Atrium workspace, where a password prompt inside the frame
# is a dead end. Set DASH_OPEN=0 to put the password back -- the secret and the /login route are
# left in place, so re-gating is one env var.
DASH_OPEN = os.environ.get("DASH_OPEN", "1") != "0"

app = Flask(__name__)
app.secret_key = SESSION_SECRET

# Cookie hardening. SameSite=None + Secure is REQUIRED for the cross-subdomain portal
# flow (portal.agoradatadriven.com -> meloyelo.agoradatadriven.com): a Lax/Strict cookie
# would be dropped on the cross-site navigation. HttpOnly keeps it out of JS.
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_SAMESITE="None",
)
# Cap request bodies: the only POST is a tiny login form, so a small cap is plenty and
# rejects oversized/abusive bodies cheaply.
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024  # 64 KiB

# dashboard.html is baked into the image; read it relative to THIS file so the working
# directory at runtime is irrelevant.
_HERE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(_HERE, "dashboard.html"), "r", encoding="utf-8") as _fh:
    DASHBOARD_HTML = _fh.read()

# A single GCS client, reused across requests (it is thread-safe for reads).
_storage_client = storage.Client()


def authed():
    """True when the caller may see the dashboard and its data.

    Three additive paths: the open-access opt-in, this dashboard's own password session, or a
    valid portal SSO cookie. The local password ALWAYS works regardless of SSO, so the client
    is never locked out by a portal problem.
    """
    if DASH_OPEN:
        return True
    if session.get("ok"):
        return True
    return bool(platform_sso.sso_allows(request))


# Self-contained login page, themed with the Agora CSS vars. No external/CDN assets.
LOGIN_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Agora Data Driven -- Sign in</title>
<style>
  :root {
    --ag-bg:#0b1020; --ag-surface:#141b33; --ag-ink:#eaf0ff; --ag-muted:#9aa7c7;
    --ag-accent:#5b8cff; --ag-accent-2:#27d3a2; --ag-danger:#ff5c7a; --ag-border:#26314f;
  }
  * { box-sizing: border-box; }
  html, body { height: 100%; margin: 0; }
  body {
    background: var(--ag-bg); color: var(--ag-ink);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    display: flex; align-items: center; justify-content: center; padding: 24px;
  }
  .card {
    background: var(--ag-surface); border: 1px solid var(--ag-border); border-radius: 14px;
    padding: 32px 28px; width: 100%; max-width: 380px;
    box-shadow: 0 18px 50px rgba(0,0,0,0.45);
  }
  .brand { font-size: 20px; font-weight: 700; letter-spacing: 0.2px; }
  .brand .dot { color: var(--ag-accent-2); }
  .sub { color: var(--ag-muted); font-size: 13px; margin: 6px 0 22px; }
  label { display: block; font-size: 13px; color: var(--ag-muted); margin-bottom: 6px; }
  input[type=password] {
    width: 100%; padding: 11px 12px; border-radius: 9px;
    border: 1px solid var(--ag-border); background: var(--ag-bg); color: var(--ag-ink);
    font-size: 15px; outline: none;
  }
  input[type=password]:focus { border-color: var(--ag-accent); }
  button {
    margin-top: 18px; width: 100%; padding: 11px 12px; border: 0; border-radius: 9px;
    background: var(--ag-accent); color: #fff; font-size: 15px; font-weight: 600; cursor: pointer;
  }
  button:hover { filter: brightness(1.06); }
  .err {
    margin: 0 0 16px; padding: 10px 12px; border-radius: 9px; font-size: 13px;
    background: rgba(255,92,122,0.12); border: 1px solid var(--ag-danger); color: var(--ag-danger);
  }
</style>
</head>
<body>
  <form class="card" method="POST" action="/login">
    <div class="brand">Agora Data Driven<span class="dot">.</span></div>
    <div class="sub">Sign in to view the MeloYelo dashboard.</div>
    {% if error %}<div class="err">{{ error }}</div>{% endif %}
    <label for="password">Password</label>
    <input id="password" name="password" type="password" autocomplete="current-password" autofocus required>
    <button type="submit">Sign in</button>
  </form>
</body>
</html>
"""


@app.route("/", methods=["GET"])
def index():
    if authed():
        # no-store: never let an intermediary/browser cache the authenticated page.
        return Response(DASHBOARD_HTML, mimetype="text/html",
                        headers={"Cache-Control": "no-store"})
    return render_template_string(LOGIN_HTML, error=None)


@app.route("/login", methods=["POST"])
def login():
    submitted = request.form.get("password", "")
    # Constant-time comparison -- never use `==` on a secret/password.
    if DASH_PASSWORD and hmac.compare_digest(submitted, DASH_PASSWORD):
        session["ok"] = True
        return redirect("/")
    return render_template_string(LOGIN_HTML, error="Incorrect password."), 401


@app.route("/logout", methods=["GET"])
def logout():
    session.clear()
    return redirect("/")


@app.route("/data.json", methods=["GET"])
def data_json():
    # The auth-gated proxy of the PRIVATE data object: only an authenticated session (or
    # an additive portal SSO cookie) may read it. Unauthenticated -> 401, never the data.
    if not authed():
        return Response('{"error":"unauthorized"}', status=401, mimetype="application/json")
    blob = _storage_client.bucket(GCS_BUCKET).blob(DATA_OBJECT)
    payload = blob.download_as_bytes()
    # no-store: the data is private; never cache it anywhere.
    return Response(payload, mimetype="application/json",
                    headers={"Cache-Control": "no-store"})


@app.route("/refresh", methods=["POST"])
def refresh():
    """Trigger a fresh Windsor/Shopify pull, for the dashboard's Sync button.

    OPT-IN: only fires when REFRESH_JOB names the export job AND the web SA holds
    `roles/run.developer` on it (run.invoker does NOT carry runWithOverrides — the same trap
    that left riverdance 13 days stale; see the root CLAUDE.md). Unconfigured or failing, it
    returns ok:false and the UI simply reloads the existing data.json, so a default deploy has
    a working button and no extra IAM.
    """
    if not authed():
        return Response('{"error":"unauthorized"}', status=401, mimetype="application/json")
    job = os.environ.get("REFRESH_JOB", "")
    if not job:
        return jsonify({"ok": False, "reason": "refresh not configured"})

    # Cooldown. With DASH_OPEN=1 there is no login in front of this route, so a rebuild must not
    # be triggerable on repeat -- each one costs paid Windsor/Shopify calls. The freshness of the
    # data object is the check (shared by every instance, unlike an in-process timer): if it was
    # rebuilt moments ago, report that instead of firing the job. The UI just reloads.
    cooldown = int(os.environ.get("REFRESH_COOLDOWN_SECONDS", "600"))
    try:
        blob = _storage_client.bucket(GCS_BUCKET).get_blob(DATA_OBJECT)
        if blob is not None and blob.updated is not None:
            age = (datetime.datetime.now(datetime.timezone.utc) - blob.updated).total_seconds()
            if age < cooldown:
                return jsonify({"ok": False, "reason": "refreshed %d min ago" % (age // 60),
                                "cooldown": True})
    except Exception:  # noqa: BLE001 -- a freshness probe must never block the refresh
        pass

    try:
        import google.auth
        import google.auth.transport.requests
        import requests

        creds, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"])
        creds.refresh(google.auth.transport.requests.Request())
        region = os.environ.get("REGION", "asia-southeast1")
        url = ("https://run.googleapis.com/v2/projects/%s/locations/%s/jobs/%s:run"
               % (os.environ.get("GCP_PROJECT", "agora-data-driven"), region, job))
        r = requests.post(url, headers={"Authorization": "Bearer " + creds.token}, timeout=30)
        if r.status_code >= 300:
            return jsonify({"ok": False, "reason": "run API %d" % r.status_code})
        # The job is asynchronous: the client polls data.json, so we do not wait here.
        return jsonify({"ok": True, "job": job})
    except Exception as e:  # noqa: BLE001 — Sync must degrade to a reload, never 500
        return jsonify({"ok": False, "reason": str(e)[:160]})


@app.route("/healthz", methods=["GET"])
def healthz():
    return Response("ok", mimetype="text/plain")


if __name__ == "__main__":
    # Local dev only; in Cloud Run gunicorn (see Dockerfile) serves main:app.
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
