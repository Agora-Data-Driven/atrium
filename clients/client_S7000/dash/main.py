"""Flask web service for ONE scope of the Campaign Uptime Monitor (Stage 3 of the contract).

THE DATA-ISOLATION POSTURE, read this before changing anything here.

    INTO Schüleraustausch and Service 7000 AG are unrelated companies, and both clients can reach
    this dashboard. Neither may see the other's spend, campaigns, budgets or performance. The
    brief calls that "the single most important technical requirement in this document".

    So the scoping is not done in this service and it is certainly not done in the browser. One
    export job writes THREE separate objects: `internal.json`, `into.json` and
    `service7000.json`, each containing only that scope's rows. THREE copies of this service are
    deployed, one per scope, each with `DATA_OBJECT` pinned to its own object by an env var. A client's
    service has no code path that can read another client's object:

        s7000-into-dash        DATA_OBJECT=into.json         -> into.agoradatadriven.com
        s7000-service-dash     DATA_OBJECT=service7000.json  -> service7000.agoradatadriven.com
        s7000-internal-dash    DATA_OBJECT=internal.json     -> s7000.agoradatadriven.com (team)

    Filtering a combined payload client-side would leave the full thing visible in devtools and in
    network logs. That is a data leak, not a cosmetic issue.

SERVING MODEL, private bucket, password-gated by default
    The data object lives in a PRIVATE GCS bucket and is NEVER public; it is only ever reachable
    through this service's `/data.json` proxy. Unlike the other client dashboards here, `DASH_OPEN`
    defaults to **0** for this build: these routes are handed to two clients directly rather than
    embedded in the gated Atrium workspace, so an unguessable URL is not good enough. The portal
    SSO cookie is trusted additively, and the local password always works, so a portal problem can
    never lock a client out.

    The internal route should ALSO stay gated, it is the only surface that shows both accounts.

The org forbids public Cloud Run, so this deploys with --no-invoker-iam-check (never
--allow-unauthenticated) and does its own password/SSO auth in-process.
"""

import datetime
import hmac
import os
from urllib.parse import quote

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
# SESSION_SECRET signs the Flask session cookie (mounted from Secret Manager at deploy time).
SESSION_SECRET = os.environ.get("SESSION_SECRET", "")
# This scope's password. compare_digest below is constant-time; never compare a secret with `==`.
DASH_PASSWORD = os.environ.get("DASH_PASSWORD", "")
# The PRIVATE bucket, shared by all three scopes...
GCS_BUCKET = os.environ.get("GCS_BUCKET", "agora-data-driven-s7000-dash")
# ...and the ONE object this deployment is allowed to serve. This single variable IS the isolation
# boundary at runtime: there is no request parameter that can change it.
DATA_OBJECT = os.environ.get("DATA_OBJECT", "internal.json")
# Which client this deployment is for, used only for the login page's wording.
SCOPE_NAME = os.environ.get("SCOPE_NAME", "Campaign Uptime Monitor")
# Default CLOSED for this build (see the docstring). Set DASH_OPEN=1 only for a scope that is
# embedded behind another gate.
DASH_OPEN = os.environ.get("DASH_OPEN", "0") == "1"

app = Flask(__name__)
app.secret_key = SESSION_SECRET

# SameSite=None + Secure is REQUIRED for the cross-subdomain portal flow
# (portal.agoradatadriven.com -> into.agoradatadriven.com): a Lax/Strict cookie would be dropped
# on that cross-site navigation. HttpOnly keeps it out of JS.
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_SAMESITE="None",
)
# The only POST bodies are a login form and an empty /refresh, so a small cap is plenty and
# rejects oversized bodies cheaply.
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024

# The brand colour for this scope, used only for the favicon and the login button. The dashboard
# itself themes from the payload; this exists because the LOGIN page renders before any payload
# has been fetched, and a generic grey login in front of a branded dashboard looks broken.
BRAND = {
    "into.json": "#0A6B63",
    "service7000.json": "#0A4EA3",
}.get(DATA_OBJECT, "#4FA84A")

# Served for /favicon.ico. Without it every first visit to the login page logs a 404 in the
# console, which is noise an operator has to learn to ignore, and learning to ignore console
# errors on a monitoring tool is a bad habit to teach.
FAVICON = ("<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'>"
           "<rect width='32' height='32' rx='7' fill='%s'/></svg>" % BRAND)

# dashboard.html is baked into the image; read it relative to THIS file so the working directory
# at runtime is irrelevant.
_HERE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(_HERE, "dashboard.html"), "r", encoding="utf-8") as _fh:
    DASHBOARD_HTML = _fh.read()

# One GCS client, reused across requests (thread-safe for reads).
_storage_client = storage.Client()


def authed():
    """True when the caller may see this scope's dashboard and its data.

    Three additive paths: the open-access opt-in, this dashboard's own password session, or a
    valid portal SSO cookie. The local password ALWAYS works regardless of SSO, so a portal
    problem never locks the client out.
    """
    if DASH_OPEN:
        return True
    if session.get("ok"):
        return True
    return bool(platform_sso.sso_allows(request))


# Self-contained login page: no external/CDN assets, so it renders on a dead network too.
LOGIN_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sign in: {{ scope }}</title>
<link rel="icon" href="data:image/svg+xml,{{ favicon }}">
<style>
  :root{ --brand:{{ brand }}; --ink:#12171D; --muted:#7C8794; --line:#E6EAEF; --crit:#C42B2B; }
  *{box-sizing:border-box}
  html,body{height:100%;margin:0}
  body{background:#F2F4F7;color:var(--ink);display:flex;align-items:center;justify-content:center;
       padding:24px;font-family:system-ui,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
  .card{background:#fff;border:1px solid var(--line);border-radius:16px;padding:30px 28px;
        width:100%;max-width:380px;box-shadow:0 18px 44px -18px rgba(16,24,40,.28)}
  .k{font-size:10px;font-weight:800;letter-spacing:1.1px;text-transform:uppercase;color:var(--muted)}
  h1{font-size:19px;margin:4px 0 4px;letter-spacing:-.02em}
  .sub{color:var(--muted);font-size:13px;margin:0 0 20px;line-height:1.5}
  label{display:block;font-size:12px;font-weight:700;color:var(--muted);margin-bottom:6px}
  input{width:100%;padding:11px 12px;border-radius:9px;border:1px solid var(--line);
        background:#fff;color:var(--ink);font-size:15px;outline:none}
  input:focus{border-color:var(--brand);box-shadow:0 0 0 3px rgba(10,78,163,.12)}
  button{margin-top:16px;width:100%;padding:11px 12px;border:0;border-radius:9px;
         background:var(--brand);color:#fff;font-size:15px;font-weight:700;cursor:pointer}
  button:hover{filter:brightness(1.07)}
  .err{margin:0 0 14px;padding:10px 12px;border-radius:9px;font-size:13px;
       background:#FDECEC;border:1px solid #F2C4C4;color:#8E2020}
  .ft{margin-top:18px;font-size:11px;color:var(--muted);text-align:center}
</style>
</head>
<body>
  <form class="card" method="POST" action="/login">
    <div class="k">Campaign uptime</div>
    <h1>{{ scope }}</h1>
    <p class="sub">Sign in to see whether every campaign that should be running is running.</p>
    {% if error %}<div class="err">{{ error }}</div>{% endif %}
    <label for="password">Password</label>
    <input id="password" name="password" type="password" autocomplete="current-password"
           autofocus required>
    <button type="submit">Sign in</button>
    <div class="ft">Prepared by Agora Data Driven</div>
  </form>
</body>
</html>
"""


@app.route("/", methods=["GET"])
def index():
    if authed():
        # no-store: never let an intermediary or the browser cache the authenticated page.
        return Response(DASHBOARD_HTML, mimetype="text/html",
                        headers={"Cache-Control": "no-store"})
    return render_template_string(LOGIN_HTML, error=None, scope=SCOPE_NAME,
                                  brand=BRAND, favicon=quote(FAVICON))


@app.route("/login", methods=["POST"])
def login():
    submitted = request.form.get("password", "")
    # Constant-time comparison: never use `==` on a secret.
    if DASH_PASSWORD and hmac.compare_digest(submitted, DASH_PASSWORD):
        session["ok"] = True
        return redirect("/")
    return render_template_string(LOGIN_HTML, error="Incorrect password.", scope=SCOPE_NAME,
                                  brand=BRAND, favicon=quote(FAVICON)), 401


@app.route("/logout", methods=["GET"])
def logout():
    session.clear()
    return redirect("/")


@app.route("/data.json", methods=["GET"])
def data_json():
    """The auth-gated proxy of THIS SCOPE'S private data object.

    `DATA_OBJECT` comes from the environment and is never taken from the request, so there is no
    parameter a client could tamper with to read another scope's payload. Unauthenticated -> 401,
    never the data.
    """
    if not authed():
        return Response('{"error":"unauthorized"}', status=401, mimetype="application/json")
    blob = _storage_client.bucket(GCS_BUCKET).blob(DATA_OBJECT)
    payload = blob.download_as_bytes()
    # no-store: the data is private; never cache it anywhere.
    return Response(payload, mimetype="application/json",
                    headers={"Cache-Control": "no-store"})


@app.route("/refresh", methods=["POST"])
def refresh():
    """Trigger a fresh Windsor pull, for the dashboard's Sync button.

    OPT-IN: only fires when REFRESH_JOB names the export job AND this service's SA holds
    `roles/run.developer` on it (`run.invoker` does NOT carry runWithOverrides, the trap that left
    riverdance 13 days stale; see the root CLAUDE.md). Unconfigured or failing, it returns ok:false
    and the UI simply reloads the existing data.json, so a default deploy still has a working
    button and needs no extra IAM.
    """
    if not authed():
        return Response('{"error":"unauthorized"}', status=401, mimetype="application/json")
    job = os.environ.get("REFRESH_JOB", "")
    if not job:
        return jsonify({"ok": False, "reason": "refresh not configured"})

    # Cooldown. Each rebuild costs paid Windsor calls, and the object's own freshness is the check
    # that every instance shares (unlike an in-process timer).
    cooldown = int(os.environ.get("REFRESH_COOLDOWN_SECONDS", "300"))
    try:
        blob = _storage_client.bucket(GCS_BUCKET).get_blob(DATA_OBJECT)
        if blob is not None and blob.updated is not None:
            age = (datetime.datetime.now(datetime.timezone.utc) - blob.updated).total_seconds()
            if age < cooldown:
                return jsonify({"ok": False, "cooldown": True,
                                "reason": "refreshed %d min ago" % (age // 60)})
    except Exception:  # noqa: BLE001, a freshness probe must never block the refresh
        pass

    try:
        import google.auth
        import google.auth.transport.requests
        import requests

        creds, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"])
        creds.refresh(google.auth.transport.requests.Request())
        url = ("https://run.googleapis.com/v2/projects/%s/locations/%s/jobs/%s:run"
               % (os.environ.get("GCP_PROJECT", "agora-data-driven"),
                  os.environ.get("REGION", "asia-southeast1"), job))
        r = requests.post(url, headers={"Authorization": "Bearer " + creds.token}, timeout=30)
        if r.status_code >= 300:
            return jsonify({"ok": False, "reason": "run API %d" % r.status_code})
        # The job is asynchronous: the page polls data.json, so we do not wait here.
        return jsonify({"ok": True, "job": job})
    except Exception as e:  # noqa: BLE001, Sync must degrade to a reload, never 500
        return jsonify({"ok": False, "reason": str(e)[:160]})


@app.route("/favicon.ico", methods=["GET"])
def favicon():
    # Public on purpose: it is a coloured square, not data. Without this route every first visit
    # to the login page logs a 404, and teaching an operator to ignore console errors on a
    # monitoring tool is a bad habit to build.
    return Response(FAVICON, mimetype="image/svg+xml",
                    headers={"Cache-Control": "public, max-age=86400"})


@app.route("/healthz", methods=["GET"])
def healthz():
    return Response("ok", mimetype="text/plain")


if __name__ == "__main__":
    # Local dev only; in Cloud Run gunicorn (see Dockerfile) serves main:app.
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
