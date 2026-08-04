"""Flask route + template integration smoke test for Agora Atrium (off-cloud, no real GCS).

Stubs google.cloud.storage so main.py (which imports store/feedback) loads without ADC, points the
workspace store at a temp dir, seeds the Riverdance demo there, then drives the real Flask app with
its test client: every client tab renders, and every POST action persists. Proves the route wiring,
the Jinja template, the atrium_dt filter, and atrium_view all work together before any deploy.

Run with a Flask-capable interpreter:
    python _atrium_smoketest.py        # prints PASS / FAIL, exits 0 / 1
"""

import io
import json
import os
import shutil
import sys
import tempfile
import types

# 1. Stub google.cloud.storage BEFORE importing main (store/feedback construct a client at import).
_g = types.ModuleType("google"); _g.__path__ = []
_gc = types.ModuleType("google.cloud"); _gc.__path__ = []
_gs = types.ModuleType("google.cloud.storage")


class _FakeClient:
    def __init__(self, *a, **k):
        pass

    def bucket(self, *a, **k):
        raise RuntimeError("GCS disabled in smoke test (use the local backend)")


_gs.Client = _FakeClient
sys.modules.setdefault("google", _g)
sys.modules.setdefault("google.cloud", _gc)
sys.modules["google.cloud.storage"] = _gs

# 2. Point the workspace store at a temp dir and sign the session.
_TMP = tempfile.mkdtemp(prefix="atrium_smoke_")
os.environ["WORKSPACE_LOCAL_DIR"] = _TMP
os.environ["REGISTRY_LOCAL_DIR"] = _TMP   # admin_atrium console reads the registry (reveal_password)
os.environ["SESSION_SECRET"] = "test-secret"

import seed_workspace   # noqa: E402
import service_templates  # noqa: E402
import store            # noqa: E402
import contextlib
import sentinel_requests
import workspace        # noqa: E402
import main             # noqa: E402

CLIENT = "riverdance"
SUPER = {"ok": True, "user": "info@agoradatadriven.com", "clients": ["*"]}



@contextlib.contextmanager
def _filing_stubbed():
    """Run a block with Sentinel's intake bridge stubbed out, yielding the list of asks filed.

    The client's quick-add posts to Sentinel now (D3), and a smoketest must not depend on a live
    sister service — so the transport is replaced and the CALL is what gets asserted.
    """
    filed = []
    real = sentinel_requests.file_request

    def _fake(client_key, title, **kw):
        filed.append({"client": client_key, "title": title, **kw})
        return True, ""

    sentinel_requests.file_request = _fake
    try:
        yield filed
    finally:
        sentinel_requests.file_request = real

def _check(label, cond):
    if not cond:
        raise AssertionError(label)
    print("  [OK] %s" % label)


def _make_docx(text):
    """Build a minimal-but-valid .docx (a zip with one paragraph) so the docview extraction has a
    real OOXML file to parse -- no python-docx / external dep needed."""
    import zipfile

    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:body><w:p><w:r><w:t>%s</w:t></w:r></w:p></w:body></w:document>' % text
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Override PartName="/word/document.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        '</Types>'
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="word/document.xml"/></Relationships>'
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("_rels/.rels", rels)
        z.writestr("word/document.xml", document)
    return buf.getvalue()


def run():
    seed_workspace.seed(register_client=False)
    main.app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False, SESSION_COOKIE_SAMESITE="Lax")
    c = main.app.test_client()

    # Unauthenticated -> redirect to login.
    _check("unauthed /w redirects to login", c.get("/w/%s/" % CLIENT).status_code == 302)

    with c.session_transaction() as s:
        s.update(SUPER)

    # Every client tab renders.
    body = c.get("/w/%s/" % CLIENT).get_data(as_text=True)
    _check("overview renders", "Riverdance RV Resort" in body and "Agora Atrium" in body)
    # An unclosed <style>/<script> swallows the ENTIRE body into <head> (blank page in a browser)
    # while every string-presence check still passes -- so check the tags actually balance.
    for tag in ("style", "script"):
        _check("every <%s> is closed (page can render)" % tag,
               body.count("<" + tag) == body.count("</" + tag + ">"))
    # HTML must never be cached: all CSS/JS is INLINE, so a cached page is a cached copy of the
    # whole app and a deploy would silently never reach that browser.
    _check("HTML pages are no-store (a deploy always reaches the browser)",
           "no-store" in (c.get("/w/%s/" % CLIENT).headers.get("Cache-Control") or ""))
    _check("the login page is no-store too",
           "no-store" in (c.get("/login").headers.get("Cache-Control") or ""))
    # The old top bar was removed: the page header (eyebrow + title + lede) now lives in the content
    # area, admin-console style. Assert that header renders instead of the retired greeting.
    _check("page header present", 'class="ax-pagehead"' in body and 'class="ax-top-eyebrow"' in body)
    _check("overview subtitle present", "Everything, visible" in body)
    _check("leadgen content present in DOM", "Summer Paid Ads Push" in body)
    _check("organic content present in DOM", "June Nurture &amp; SEO" in body or "June Nurture" in body)
    _check("AI summary present", "AI summary" in body)
    for tab in ("dashboard", "leadgen", "organic", "calendar", "conversations", "settings"):
        _check("tab '%s' returns 200" % tab, c.get("/w/%s/%s" % (CLIENT, tab)).status_code == 200)

    # Communications: the unified timeline + the client/team no-leak guarantee (server-side filter).
    r = c.post("/w/%s/admin/communication" % CLIENT,
               data={"op": "add", "channel": "slack", "audience": "team",
                     "title": "Internal spend note", "summary": "TEAMSECRET reallocate budget"})
    _check("admin adds a team-only communication", r.status_code == 200 and r.get_json().get("ok"))
    r = c.post("/w/%s/admin/communication" % CLIENT,
               data={"op": "add", "channel": "meeting", "audience": "client",
                     "title": "Client kickoff", "summary": "CLIENTVISIBLE kickoff recap"})
    _check("admin adds a client-visible communication", r.get_json().get("ok"))
    admin_conv = c.get("/w/%s/conversations" % CLIENT).get_data(as_text=True)
    _check("admin sees BOTH cards with audience pills + channel badges",
           "TEAMSECRET" in admin_conv and "CLIENTVISIBLE" in admin_conv
           and "Team only" in admin_conv and "Client sees" in admin_conv
           and "ch-slack" in admin_conv and "ch-meeting" in admin_conv)
    with c.session_transaction() as s:
        s.update({"ok": True, "user": "owner@riverdanceresort.com", "clients": [CLIENT]})
    client_conv = c.get("/w/%s/conversations" % CLIENT).get_data(as_text=True)
    # "ax-cm-audseg"/"data-commaddform" also appear as JS string literals, so assert on rendered
    # signals: the team summary text is absent, and none of the admin-only affordances rendered.
    _check("the client sees ONLY their card -- a team-only summary never reaches the client HTML",
           "CLIENTVISIBLE" in client_conv and "TEAMSECRET" not in client_conv
           and 'data-admin="1"' not in client_conv
           and "+ Add a communication" not in client_conv
           and "Client sees" not in client_conv)
    with c.session_transaction() as s:
        s.update(SUPER)

    # Communications: import a pasted Upwork conversation -> a role-tagged thread + a timeline card.
    upw_raw = ("Saturday, Jul 11\nDM\nDaniela Marquez\n12:59 AM\nHi Ian, UPWORKPASTE please send.\n"
               "Ian Gabriel Fernandez\n10:55 PM\nHi Daniela, got it.\n")
    r = c.post("/w/%s/admin/communication" % CLIENT,
               data={"op": "import_upwork", "raw": upw_raw, "audience": "client",
                     "team_names": "Ian Gabriel Fernandez"})
    j = r.get_json()
    _check("Upwork import returns ok + a message count",
           r.status_code == 200 and j.get("ok") and j.get("messages") == 2)
    conv_after = c.get("/w/%s/conversations" % CLIENT).get_data(as_text=True)
    _check("imported Upwork card renders with an upwork badge + a Read-full-thread link",
           "ch-upwork" in conv_after and "Read full thread" in conv_after)
    # The stored thread is served by the EXISTING reader route, role-tagged and de-duplicated.
    up_item = next(it for it in workspace.communications_list(workspace.load_workspace(CLIENT))
                   if it.get("channel") == "upwork")
    tr = c.get("/w/%s/mail/thread/%s" % (CLIENT, up_item["thread_key"])).get_json()
    _check("Upwork thread reader returns the 2 parsed messages with roles",
           tr.get("ok") and len(tr.get("messages") or []) == 2
           and tr["messages"][0].get("role") == "client"
           and tr["messages"][1].get("role") == "agora")
    # "Add newer messages": a re-paste of the fuller thread (overlap + 1 new under a relative
    # "Today" separator) folds in ONLY the new message and stamps the card with the UPDATE date.
    import datetime as _dt
    upd_raw = upw_raw + "Today\nDaniela Marquez\n9:14 AM\nOne more thing NEWMSG.\n"
    r = c.post("/w/%s/admin/communication" % CLIENT,
               data={"op": "update_upwork", "thread_key": up_item["thread_key"], "raw": upd_raw,
                     "team_names": "Ian Gabriel Fernandez"})
    j = r.get_json()
    _check("Upwork update folds in only the genuinely-new message",
           r.status_code == 200 and j.get("ok") and j.get("added") == 1 and j.get("total") == 3)
    up_item2 = next(it for it in workspace.communications_list(workspace.load_workspace(CLIENT))
                    if it.get("channel") == "upwork")
    today_utc = _dt.datetime.now(_dt.timezone.utc).date().isoformat()
    _check("the card's date is the last-UPDATED date, not the import date",
           str(up_item2.get("date", ""))[:10] == today_utc)
    # Idempotent: pasting the same thread again adds nothing.
    r = c.post("/w/%s/admin/communication" % CLIENT,
               data={"op": "update_upwork", "thread_key": up_item["thread_key"], "raw": upd_raw,
                     "team_names": "Ian Gabriel Fernandez"})
    _check("re-pasting the same thread adds 0 (no duplicates)",
           r.get_json().get("added") == 0 and r.get_json().get("total") == 3)
    # Deleting the card also removes its thread archive object (no orphan).
    c.post("/w/%s/admin/communication" % CLIENT,
           data={"op": "delete", "item_id": up_item["id"]})
    _check("deleting the Upwork card cleans up its thread object",
           workspace.read_mail_thread(CLIENT, up_item["thread_key"]) is None)

    # Approve an awaiting piece -> persists + confirmation shows on reload.
    r = c.post("/w/%s/approve" % CLIENT, data={"content_id": "RVR-016", "note": "Ship it."})
    _check("approve returns ok json", r.status_code == 200 and r.get_json().get("ok") is True)
    _camp, item = workspace._find_content(workspace.load_workspace(CLIENT), "RVR-016")
    _check("approval persisted", item["status"] == "approved" and item["client_note"] == "Ship it.")
    _check("confirmation bar on reload",
           "Approved" in c.get("/w/%s/leadgen" % CLIENT).get_data(as_text=True))

    # Re-decide: an already-approved piece can be flipped to changes (status anytime).
    r = c.post("/w/%s/request-changes" % CLIENT, data={"content_id": "RVR-016"})
    _check("re-decide flips approved -> changes",
           r.status_code == 200 and r.get_json().get("status") == "changes")
    r = c.post("/w/%s/approve" % CLIENT, data={"content_id": "RVR-016", "note": "Ship it."})
    _check("re-decide flips back to approved", r.get_json().get("status") == "approved")

    # Request changes on the organic awaiting piece.
    r = c.post("/w/%s/request-changes" % CLIENT, data={"content_id": "RVR-017"})
    _check("request-changes ok", r.status_code == 200 and r.get_json().get("status") == "changes")

    # Client posts a threaded comment on a content piece.
    r = c.post("/w/%s/comment" % CLIENT, data={"content_id": "RVR-017", "body": "Add a guest quote?"})
    _check("client comment ok", r.status_code == 200 and r.get_json().get("ok") is True)
    _camp, c017 = workspace._find_content(workspace.load_workspace(CLIENT), "RVR-017")
    _check("client comment persisted (sender client)",
           c017["comments"][-1]["body"] == "Add a guest quote?" and c017["comments"][-1]["sender"] == "client")

    # Live-state endpoint (drives the no-reload polling): exposes per-content status + comments.
    r = c.get("/w/%s/state.json" % CLIENT)
    sj = r.get_json() if r.status_code == 200 else {}
    _check("state.json returns ok", r.status_code == 200 and sj.get("ok") is True)
    _st017 = (sj.get("content", {}) or {}).get("RVR-017", {})
    _check("state.json carries status + the new comment",
           _st017.get("status") == "changes"
           and any(cm.get("body") == "Add a guest quote?" and cm.get("sender") == "client"
                   for cm in _st017.get("comments", [])))
    _check("state.json gated (logged-out 401/403)",
           main.app.test_client().get("/w/%s/state.json" % CLIENT).status_code in (401, 403))

    # Save a note silently.
    _check("save-note ok",
           c.post("/w/%s/save-note" % CLIENT, data={"content_id": "RVR-014", "note": "Nice"}).status_code == 200)

    # Send a client message -> thread goes awaiting_reply.
    r = c.post("/w/%s/send-message" % CLIENT, data={"conversation_id": "cv_1", "body": "Thanks!"})
    _check("send-message ok", r.status_code == 200 and r.get_json().get("status") == "awaiting_reply")
    _check("message persisted",
           workspace.load_workspace(CLIENT)["conversations"][0]["messages"][-1]["body"] == "Thanks!")

    # Save notification prefs.
    r = c.post("/w/%s/save-notify" % CLIENT,
               data={"master": "1", "content": "0", "replies": "1", "summary": "1",
                     "status": "0", "news": "0", "frequency": "daily"})
    _check("save-notify ok", r.status_code == 200)
    prefs = workspace.get_notify(workspace.load_workspace(CLIENT), SUPER["user"])
    _check("notify persisted", prefs["content"] is False and prefs["frequency"] == "daily")

    # Team console is now the LANDING ONLY. The per-client manage page and its POST routes are GONE:
    # the team edits each workspace IN PLACE via /w/<c>/admin/* (exercised below), and a console card
    # opens /w/<c>/ directly.
    _check("old per-client manage page removed (404)",
           c.get("/admin/atrium/%s" % CLIENT).status_code == 404)
    for path in ("password", "campaign", "content", "conversation", "reply", "metrics"):
        _check("old console POST /%s removed" % path,
               c.post("/admin/atrium/%s/%s" % (CLIENT, path), data={}).status_code in (404, 405))

    # The console is the only landing now (the Home hub was removed): it lands straight on the
    # Clients pane, exposes the account/app-switcher dropdown (Switch app -> Sentinel / Website
    # Editor), links each client card straight to the workspace, and hides the `template` client.
    store.add_client(CLIENT, "Riverdance RV Resort")
    store.add_client("template", "Template")
    landing = c.get("/admin/atrium").get_data(as_text=True)
    _check("console landing renders the Clients console",
           "Atrium Admin" in landing and 'data-view="hub"' not in landing)
    _check("console exposes the app-switcher dropdown",
           "Switch app" in landing and 'id="acct-menu"' in landing)
    _check("console card opens the workspace directly", ('href="/w/%s/"' % CLIENT) in landing)
    _check("template client hidden from console", '<div class="name">Template</div>' not in landing)
    store.remove_client("template")

    # ---- In-workspace admin editing (/w/<c>/admin/*), all JSON, super-admin only ----
    # Admin notice bar renders for a super-admin in the real workspace.
    body = c.get("/w/%s/leadgen" % CLIENT).get_data(as_text=True)
    _check("admin edit bar renders for super-admin",
           'class="ax-adminbadge"' in body and 'data-admin="1"' in body)

    # Edit strategy in place.
    r = c.post("/w/%s/admin/strategy" % CLIENT,
               data={"campaign_id": "c_paid_1", "name": "Summer Paid Ads Push v2",
                     "eyebrow": "PAID ADS", "what": "W2", "why": "Y2", "next": "N2"})
    _check("inline strategy ok", r.status_code == 200 and r.get_json().get("ok") is True)
    camp = workspace._find_campaign(workspace.load_workspace(CLIENT), "c_paid_1")
    _check("strategy persisted", camp["strategy"]["what"] == "W2" and camp["name"] == "Summer Paid Ads Push v2")

    # Save a strategy doc link, then generate a summary (AI OFF -> graceful, never 500).
    r = c.post("/w/%s/admin/strategy-doc" % CLIENT,
               data={"campaign_id": "c_paid_1",
                     "doc_url": "https://docs.google.com/document/d/ABC123abc123abc123abc/edit"})
    _check("strategy-doc saved", r.status_code == 200 and r.get_json().get("strategy_doc", "").endswith("/edit"))
    r = c.post("/w/%s/admin/generate-summary" % CLIENT, data={"campaign_id": "c_paid_1"})
    _check("generate-summary degrades gracefully (no 500)", r.status_code == 200)
    _check("generate-summary reports unreadable doc when docs disabled",
           r.get_json().get("ok") is False and r.get_json().get("source") == "none")

    # Hand-edit the AI summary.
    r = c.post("/w/%s/admin/summary" % CLIENT,
               data={"campaign_id": "c_paid_1", "ai_summary": "Hand-written summary."})
    _check("manual summary saved",
           r.status_code == 200 and r.get_json().get("ai_summary") == "Hand-written summary.")

    # Add content in place, then edit + comment as the team + delete it.
    r = c.post("/w/%s/admin/content" % CLIENT,
               data={"campaign_id": "c_paid_1", "ref": "RVR-099", "type_tag": "Reel",
                     "platform": "Instagram", "caption": "A reel for review."})
    _check("inline add-content ok", r.status_code == 200 and r.get_json().get("id") == "RVR-099")

    # Content posted WITH a date mirrors onto the Content Calendar as a linked, paid/leadgen event.
    r = c.post("/w/%s/admin/content" % CLIENT,
               data={"campaign_id": "c_paid_1", "ref": "RVR-100", "type_tag": "Reel",
                     "caption": "Dated reel.", "date": "2026-08-20"})
    _check("inline add-content with date ok", r.status_code == 200)
    _linked = [e for e in workspace.load_workspace(CLIENT).get("calendar", [])
               if e.get("content_id") == "RVR-100"]
    _check("dated content mirrored onto the calendar via the route",
           len(_linked) == 1 and _linked[0]["date"] == "2026-08-20"
           and _linked[0]["kind"] == "paid" and _linked[0]["tab"] == "leadgen")
    c.post("/w/%s/admin/delete-content" % CLIENT, data={"content_id": "RVR-100"})
    _check("deleting dated content removes its calendar event via the route",
           not [e for e in workspace.load_workspace(CLIENT).get("calendar", [])
                if e.get("content_id") == "RVR-100"])
    r = c.post("/w/%s/admin/edit-content" % CLIENT,
               data={"content_id": "RVR-099", "caption": "An edited reel caption."})
    _check("inline edit-content ok", r.status_code == 200)
    _camp, v099 = workspace._find_content(workspace.load_workspace(CLIENT), "RVR-099")
    _check("content edit persisted", v099["caption"] == "An edited reel caption.")
    r = c.post("/w/%s/admin/content-comment" % CLIENT,
               data={"content_id": "RVR-099", "body": "Team note.", "sender_name": "Maya"})
    _check("team comment ok", r.status_code == 200)
    _camp, v099b = workspace._find_content(workspace.load_workspace(CLIENT), "RVR-099")
    _check("team comment persisted (sender agora)", v099b["comments"][-1]["sender"] == "agora")
    # The Delete-comment control renders for the team on PAID/paid-ads content too (not just organic):
    # RVR-099 lives on c_paid_1, so its comment's Delete button must appear in the leadgen render.
    _paid_cm = v099b["comments"][-1]["id"]
    _check("team Delete-comment button renders on paid content",
           ('data-comdelete="%s"' % _paid_cm) in c.get("/w/%s/leadgen" % CLIENT).get_data(as_text=True))

    # Upload a creative, fetch it back through the authed proxy, then remove it.
    png = b"\x89PNG\r\n\x1a\n" + b"riverdance-creative-bytes"
    r = c.post("/w/%s/admin/upload-creative" % CLIENT,
               data={"content_id": "RVR-099", "file": (io.BytesIO(png), "ad.png", "image/png")},
               content_type="multipart/form-data")
    _check("upload-creative ok", r.status_code == 200 and r.get_json().get("ok") is True)
    served = c.get("/w/%s/creative/RVR-099" % CLIENT)
    _check("creative served via authed proxy",
           served.status_code == 200 and served.get_data() == png and served.mimetype == "image/png")
    # The no-store rule is HTML-only: media keeps its own explicit caching policy.
    _check("creatives keep their own cache policy (no-store is HTML-only)",
           "max-age" in (served.headers.get("Cache-Control") or "")
           and "no-store" not in (served.headers.get("Cache-Control") or ""))
    r = c.post("/w/%s/admin/remove-creative" % CLIENT, data={"content_id": "RVR-099"})
    _check("remove-creative ok", r.status_code == 200)
    _check("creative 404 after removal", c.get("/w/%s/creative/RVR-099" % CLIENT).status_code == 404)

    # A short VIDEO creative is accepted, served with its mime, and rendered as a <video>.
    mp4 = b"\x00\x00\x00\x18ftypmp42" + b"riverdance-clip-bytes"
    r = c.post("/w/%s/admin/upload-creative" % CLIENT,
               data={"content_id": "RVR-099", "file": (io.BytesIO(mp4), "reel.mp4", "video/mp4")},
               content_type="multipart/form-data")
    _check("video upload ok", r.status_code == 200 and r.get_json().get("ok") is True)
    _camp, vitem = workspace._find_content(workspace.load_workspace(CLIENT), "RVR-099")
    _check("video mime stored", vitem.get("image_mime") == "video/mp4")
    served = c.get("/w/%s/creative/RVR-099" % CLIENT)
    _check("video served with mime", served.status_code == 200 and served.mimetype == "video/mp4")
    vpage = c.get("/w/%s/" % CLIENT).get_data(as_text=True)
    _check("workspace renders a playable video thumbnail for the clip",
           ('data-playvideo="/w/%s/creative/RVR-099"' % CLIENT) in vpage)
    _check("uploaded video creative shows a Remove-video button",
           'data-removecreative="RVR-099"' in vpage)
    c.post("/w/%s/admin/remove-creative" % CLIENT, data={"content_id": "RVR-099"})

    # Add-video "link" half: a pasted URL is stored on the piece, rendered for the client, then cleared.
    r = c.post("/w/%s/admin/video-link" % CLIENT,
               data={"content_id": "RVR-099", "url": "https://example.com/clip.mp4"})
    _check("video-link save ok", r.status_code == 200 and r.get_json().get("ok") is True)
    _camp, litem = workspace._find_content(workspace.load_workspace(CLIENT), "RVR-099")
    _check("video_url stored", litem.get("video_url") == "https://example.com/clip.mp4")
    page = c.get("/w/%s/" % CLIENT).get_data(as_text=True)
    _check("workspace renders a playable video thumbnail for a direct mp4 link",
           'data-playvideo="https://example.com/clip.mp4"' in page)
    _check("type thumbnail is a clickable play link when a video is attached",
           'ax-ch-playable' in page and 'href="https://example.com/clip.mp4"' in page)
    r = c.post("/w/%s/admin/video-link" % CLIENT,
               data={"content_id": "RVR-099", "url": "javascript:alert(1)"})
    _check("video-link rejects non-http url", r.status_code == 400 and r.get_json().get("ok") is False)
    r = c.post("/w/%s/admin/video-link" % CLIENT, data={"content_id": "RVR-099", "url": ""})
    _camp, litem = workspace._find_content(workspace.load_workspace(CLIENT), "RVR-099")
    _check("video-link clear ok", r.status_code == 200 and litem.get("video_url") == "")

    # Local backend: an in-app .mp4 upload OVER the 30 MB cloud cap is accepted (no Cloud Run cap
    # off-cloud), so the same Upload-.mp4 button works locally for big files via the in-app fallback.
    big = b"\x00" * (32 * 1024 * 1024)   # 32 MB > the 30 MB in-app cloud cap
    r = c.post("/w/%s/admin/upload-creative" % CLIENT,
               data={"content_id": "RVR-099", "file": (io.BytesIO(big), "big.mp4", "video/mp4")},
               content_type="multipart/form-data")
    _check("local backend accepts a >30 MB in-app .mp4", r.status_code == 200 and r.get_json().get("ok") is True)
    c.post("/w/%s/admin/remove-creative" % CLIENT, data={"content_id": "RVR-099"})

    # Reject a non-media upload on the LEGACY single-creative route (still image/video only).
    r = c.post("/w/%s/admin/upload-creative" % CLIENT,
               data={"content_id": "RVR-099", "file": (io.BytesIO(b"x"), "a.txt", "text/plain")},
               content_type="multipart/form-data")
    _check("non-media single-creative upload rejected", r.status_code == 400)

    # add-images now accepts ANY file type. A PDF is stored, served INLINE by default (so it previews
    # in an <iframe>) and as an attachment with its original name under ?dl=1, and renders as a live
    # document preview (the doc lightbox), NOT a bare download chip.
    r = c.post("/w/%s/admin/add-images" % CLIENT,
               data={"content_id": "RVR-014", "files": (io.BytesIO(b"%PDF-1.4 hi"), "brief.pdf", "application/pdf")},
               content_type="multipart/form-data")
    j = r.get_json()
    _check("add-images accepts a non-media file",
           r.status_code == 200 and j.get("ok") is True and bool(j.get("added")))
    fid = j["added"][0]["id"]
    served = c.get("/w/%s/creative/RVR-014/%s" % (CLIENT, fid))
    _check("PDF served inline by default (previewable)",
           served.status_code == 200 and served.mimetype == "application/pdf"
           and served.headers.get("Content-Disposition", "").startswith("inline"))
    dl = c.get("/w/%s/creative/RVR-014/%s?dl=1" % (CLIENT, fid))
    _check("PDF served as a download with its name under ?dl=1",
           'attachment; filename="brief.pdf"' in dl.headers.get("Content-Disposition", ""))
    page = c.get("/w/%s/" % CLIENT).get_data(as_text=True)
    _check("PDF renders as a doc tile (PDF icon + opens the doc lightbox)",
           'class="ax-shot-media ax-shot-doc"' in page and 'data-doc-kind="pdf"' in page
           and ">PDF</text>" in page and "brief.pdf" in page)
    c.post("/w/%s/admin/remove-image" % CLIENT, data={"content_id": "RVR-014", "image_id": fid})

    # An Office doc (docx) is rendered to a scrollable HTML preview by /docview -- stdlib extraction,
    # so its actual text shows "inside" the iframe; it renders as an 'office' doc preview in the card.
    docx_bytes = _make_docx("Riverdance summer brief. Eagle River access.")
    DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    r = c.post("/w/%s/admin/add-images" % CLIENT,
               data={"content_id": "RVR-014", "files": (io.BytesIO(docx_bytes), "brief.docx", DOCX_MIME)},
               content_type="multipart/form-data")
    j = r.get_json()
    _check("add-images accepts a docx", r.status_code == 200 and bool(j.get("added")))
    did = j["added"][0]["id"]
    dv = c.get("/w/%s/docview/RVR-014/%s" % (CLIENT, did))
    _check("docview renders the docx text inside a scrollable HTML page",
           dv.status_code == 200 and "text/html" in dv.mimetype
           and "Eagle River access" in dv.get_data(as_text=True))
    page = c.get("/w/%s/" % CLIENT).get_data(as_text=True)
    _check("docx renders as a Word doc tile pointing at /docview",
           'data-doc-kind="office"' in page and ">DOC</text>" in page
           and ("/w/%s/docview/RVR-014/%s" % (CLIENT, did)) in page)
    c.post("/w/%s/admin/remove-image" % CLIENT, data={"content_id": "RVR-014", "image_id": did})

    # Signed-URL "bypass the cap" flow (the GCS signing itself needs cloud; here we test the app side).
    # 1) upload-url degrades gracefully on the local backend (no signing) -> ok:false, never crashes.
    r = c.post("/w/%s/admin/creative-upload-url" % CLIENT,
               data={"content_id": "RVR-099", "content_type": "video/mp4"})
    _check("creative-upload-url responds gracefully", r.status_code == 200 and r.get_json().get("ok") is False)
    # 2) confirm records a creative uploaded out-of-band (simulating the direct-to-GCS PUT).
    workspace.write_creative(CLIENT, "RVR-099", b"\x00\x00\x00\x18ftypmp42" + b"0123456789" * 5000, content_type="video/mp4")
    r = c.post("/w/%s/admin/creative-confirm" % CLIENT, data={"content_id": "RVR-099", "content_type": "video/mp4"})
    _check("creative-confirm records the upload", r.status_code == 200 and r.get_json().get("ok") is True)
    # 3) a Range request streams a 206 partial (video seeking + bounded memory).
    served = c.get("/w/%s/creative/RVR-099" % CLIENT, headers={"Range": "bytes=0-1023"})
    body = served.get_data()  # drain the streaming generator so its file handle closes (Windows)
    _check("range request -> 206 partial",
           served.status_code == 206 and len(body) == 1024
           and served.headers.get("Content-Range", "").startswith("bytes 0-1023/"))
    c.post("/w/%s/admin/remove-creative" % CLIENT, data={"content_id": "RVR-099"})

    # Delete the content piece in place.
    r = c.post("/w/%s/admin/delete-content" % CLIENT, data={"content_id": "RVR-099"})
    _check("inline delete-content ok", r.status_code == 200)
    _camp, gone = workspace._find_content(workspace.load_workspace(CLIENT), "RVR-099")
    _check("content deleted", gone is None)

    # Add a campaign in place, then delete it.
    n_before = len(workspace.load_workspace(CLIENT)["campaigns"])
    r = c.post("/w/%s/admin/campaign" % CLIENT,
               data={"channel": "organic", "name": "Inline Organic", "eyebrow": "ORG"})
    _check("inline add-campaign ok", r.status_code == 200)
    new_cid = r.get_json().get("id")
    _check("campaign added", len(workspace.load_workspace(CLIENT)["campaigns"]) == n_before + 1)
    r = c.post("/w/%s/admin/delete-campaign" % CLIENT, data={"campaign_id": new_cid})
    _check("inline delete-campaign ok",
           r.status_code == 200 and len(workspace.load_workspace(CLIENT)["campaigns"]) == n_before)

    # Inline metrics + calendar edits.
    r = c.post("/w/%s/admin/metrics" % CLIENT,
               data={"today_leads": "33", "split_paid": "44", "metric_value_0": "999"})
    _check("inline metrics ok", r.status_code == 200 and workspace.load_workspace(CLIENT)["today"]["leads"] == 33)
    r = c.post("/w/%s/admin/calendar" % CLIENT,
               data={"op": "add", "date": "2026-07-04", "label": "July promo", "kind": "milestone"})
    _check("inline calendar add ok", r.status_code == 200)
    cal_n = len(workspace.load_workspace(CLIENT)["calendar"])
    # Mark the just-added event done, then clear it (the "Mark as done" toggle).
    r = c.post("/w/%s/admin/calendar" % CLIENT, data={"op": "status", "index": str(cal_n - 1), "status": "done"})
    _check("inline calendar mark-done ok",
           r.status_code == 200 and workspace.load_workspace(CLIENT)["calendar"][cal_n - 1].get("status") == "done")
    r = c.post("/w/%s/admin/calendar" % CLIENT, data={"op": "status", "index": str(cal_n - 1), "status": ""})
    _check("inline calendar clear-done ok",
           r.status_code == 200 and "status" not in workspace.load_workspace(CLIENT)["calendar"][cal_n - 1])
    r = c.post("/w/%s/admin/calendar" % CLIENT, data={"op": "delete", "index": str(cal_n - 1)})
    _check("inline calendar delete ok",
           r.status_code == 200 and len(workspace.load_workspace(CLIENT)["calendar"]) == cal_n - 1)

    # Inline reply to a conversation as AGORA.
    r = c.post("/w/%s/admin/reply" % CLIENT,
               data={"conversation_id": "cv_1", "body": "Inline team reply.", "resolve": "1"})
    _check("inline reply ok + resolved",
           r.status_code == 200 and r.get_json().get("status") == "resolved")

    # ---- Market Intelligence (team-written briefing, client-read) --------------------------------
    intel_page = c.get("/w/%s/intel" % CLIENT).get_data(as_text=True)
    _check("market intelligence pane + nav render",
           'data-pane="intel"' in intel_page and 'data-tab="intel"' in intel_page
           and "Market Intelligence" in intel_page)
    _check("seeded intel entry renders", "AI Search Ads expansion" in intel_page)
    _check("super-admin sees the per-section add form",
           "Add to Business Research" in intel_page and "Add to Media Buying News" in intel_page)
    # Add an entry in place, edit it, then delete it.
    r = c.post("/w/%s/admin/intel" % CLIENT,
               data={"op": "add", "section": "business_research", "heading": "Competitor Watch",
                     "title": "Cruise America expands fleet", "body": "More vehicles in Denver.",
                     "source": "Press release"})
    _check("inline add-intel ok", r.status_code == 200 and r.get_json().get("ok") is True)
    new_eid = r.get_json().get("id")
    _check("intel entry persisted newest-first",
           workspace.load_workspace(CLIENT)["intel"]["business_research"][0]["id"] == new_eid)
    r = c.post("/w/%s/admin/intel" % CLIENT,
               data={"op": "edit", "section": "business_research", "entry_id": new_eid,
                     "body": "Edited via route."})
    _check("inline edit-intel ok", r.status_code == 200)
    _check("intel edit persisted",
           workspace.load_workspace(CLIENT)["intel"]["business_research"][0]["body"] == "Edited via route.")
    # A bad section is rejected; an empty add is rejected.
    _check("intel rejects unknown section",
           c.post("/w/%s/admin/intel" % CLIENT,
                  data={"op": "add", "section": "nope", "body": "x"}).status_code == 400)
    _check("intel rejects an empty add",
           c.post("/w/%s/admin/intel" % CLIENT,
                  data={"op": "add", "section": "media_buying"}).status_code == 400)
    r = c.post("/w/%s/admin/intel" % CLIENT,
               data={"op": "delete", "section": "business_research", "entry_id": new_eid})
    _check("inline delete-intel ok",
           r.status_code == 200
           and new_eid not in [e["id"] for e in
                               workspace.load_workspace(CLIENT)["intel"]["business_research"]])

    # ---- Market Intelligence AI brain (model dropdown + tunable prompts + keywords) --------------
    _check("super-admin sees the AI Research Brain panel",
           "AI Research Brain" in intel_page and 'id="ax-intel-model"' in intel_page)
    _check("ai_settings rejects an unknown model",
           c.post("/w/%s/admin/intel" % CLIENT,
                  data={"op": "ai_settings", "model": "gpt-9"}).status_code == 400)
    _check("ai_settings rejects a model whose key isn't configured",
           c.post("/w/%s/admin/intel" % CLIENT,
                  data={"op": "ai_settings", "model": "gemini-2.5-pro"}).status_code == 400)
    r = c.post("/w/%s/admin/intel" % CLIENT,
               data={"op": "ai_settings", "model": "", "business_prompt": "Watch RV rentals.",
                     "media_prompt": ""})
    _check("ai_settings (off) ok + prompt persisted",
           r.status_code == 200
           and workspace.load_workspace(CLIENT)["intel_ai"]["business_prompt"] == "Watch RV rentals.")
    r = c.post("/w/%s/admin/intel" % CLIENT,
               data={"op": "topics", "topics": "RV rentals, campgrounds"})
    _check("intel topics saved",
           r.status_code == 200
           and workspace.get_intel_topics(workspace.load_workspace(CLIENT)) == ["RV rentals", "campgrounds"])
    # "Write these for me" (op=suggest): no provider configured -> a friendly reason, never a 500.
    _check("suggest button renders in the AI panel", 'id="ax-intel-suggest"' in intel_page)
    r = c.post("/w/%s/admin/intel" % CLIENT, data={"op": "suggest"})
    _check("suggest with no provider -> ok:false + reason",
           r.status_code == 200 and r.get_json().get("ok") is False
           and "model" in r.get_json().get("message", "").lower())
    # With the AI stubbed, the route returns the three drafts (fields only -- nothing saved).
    import intel_ai   # noqa: E402
    _real_suggest = intel_ai.suggest_config
    intel_ai.suggest_config = lambda name, context="", model=None: (
        {"topics": "boutique RV rentals, roadtrip travellers", "business_prompt": "Watch RV demand.",
         "media_prompt": "Watch travel-ad platforms."}, "")
    try:
        r = c.post("/w/%s/admin/intel" % CLIENT, data={"op": "suggest"})
        j = r.get_json()
        _check("suggest returns the three drafted fields",
               r.status_code == 200 and j.get("ok") is True
               and j.get("topics") == "boutique RV rentals, roadtrip travellers"
               and j.get("business_prompt") and j.get("media_prompt"))
        _check("suggest does NOT save (keywords unchanged until Save settings)",
               workspace.get_intel_topics(workspace.load_workspace(CLIENT)) == ["RV rentals", "campgrounds"])
    finally:
        intel_ai.suggest_config = _real_suggest
    # Bulk favourite + delete on selected entries.
    bid = workspace.add_intel_entry(CLIENT, "media_buying", {"title": "Bulk fav me"})["id"]
    r = c.post("/w/%s/admin/intel" % CLIENT,
               data={"op": "bulk", "action": "favourite", "section": "media_buying", "entry_ids": bid})
    _check("bulk favourite ok + pinned",
           r.status_code == 200
           and [e for e in workspace.load_workspace(CLIENT)["intel"]["media_buying"]
                if e["id"] == bid][0].get("favourite") is True)
    r = c.post("/w/%s/admin/intel" % CLIENT,
               data={"op": "bulk", "action": "delete", "section": "media_buying", "entry_ids": bid})
    _check("bulk delete ok",
           r.status_code == 200
           and bid not in [e["id"] for e in workspace.load_workspace(CLIENT)["intel"]["media_buying"]])
    _check("bulk rejects a bad action",
           c.post("/w/%s/admin/intel" % CLIENT,
                  data={"op": "bulk", "action": "nuke", "section": "media_buying", "entry_ids": "x"}).status_code == 400)

    # ---- Website Health (team-only tab: admins see it, THE super admin edits) --------------------
    import atrium_health   # noqa: E402
    # Pure tag detection: GTM container + GA4 + Meta pixel are recognised straight from page markup.
    _sample = ('<title>Demo</title>'
               '<script src="https://www.googletagmanager.com/gtm.js?id=GTM-ABC1234"></script>'
               'gtag("config","G-ABCDE12345"); fbq("init","123456789012345");')
    _types = set(t["type"] for t in atrium_health.detect_tags(_sample))
    _check("detect_tags finds GTM + GA4 + Meta", {"gtm", "ga4", "meta"} <= _types)
    _check("detect_tags captures the GTM container id",
           any(t["id"] == "GTM-ABC1234" for t in atrium_health.detect_tags(_sample)))
    # check_website never raises on a dead site (injected fetcher raises) -> graceful ok:false result.
    def _boom(url, timeout):
        raise RuntimeError("getaddrinfo failed")
    _dead = atrium_health.check_website("nope.invalid", fetcher=_boom)
    _check("check_website degrades on a dead site", _dead["ok"] is False and bool(_dead["error"]))

    # Patch the live check so the ROUTE uses a canned result (no real network in the smoke test).
    def _fake_check(url, timeout=10, fetcher=None):
        return {"url": "https://riverdanceresort.com", "input_url": url,
                "checked_at": workspace.now_iso(), "ok": True, "status_code": 200,
                "final_url": "https://riverdanceresort.com", "redirected": False, "https": True,
                "response_ms": 120, "page_title": "Riverdance", "error": "",
                "tags": [{"type": "gtm", "label": "Google Tag Manager", "id": "GTM-RVR123"}],
                "tag_count": 1, "gtm": ["GTM-RVR123"],
                "issues": [{"level": "ok", "text": "Site is online and tags were detected - no problems found."}]}
    atrium_health.check_website = _fake_check

    with c.session_transaction() as s:
        s.update(SUPER)
    wh = c.get("/w/%s/website-health" % CLIENT).get_data(as_text=True)
    _check("website-health pane renders for super-admin",
           'data-pane="website-health"' in wh and "Website Health" in wh)
    _check("website-health nav link present for team", 'data-tab="website-health"' in wh)
    _check("super admin gets the editable URL input", 'id="ax-wh-url"' in wh)
    r = c.post("/w/%s/admin/website-health/save" % CLIENT, data={"url": "riverdanceresort.com"})
    _check("save website url ok", r.status_code == 200 and r.get_json().get("ok") is True)
    _check("website url persisted",
           workspace.load_workspace(CLIENT).get("website_health", {}).get("url") == "riverdanceresort.com")
    r = c.post("/w/%s/admin/website-health/check" % CLIENT, data={"url": "riverdanceresort.com"})
    _check("run health check ok + result stored",
           r.status_code == 200 and r.get_json().get("ok") is True
           and workspace.load_workspace(CLIENT)["website_health"]["last_check"]["gtm"] == ["GTM-RVR123"])
    _check("check result renders (status + GTM container)",
           "GTM-RVR123" in c.get("/w/%s/website-health" % CLIENT).get_data(as_text=True))
    # Running the check normalised + stored the url (https://...); saving notes must NOT clobber it.
    r = c.post("/w/%s/admin/website-health/save" % CLIENT, data={"notes": "Pixel verified."})
    _check("save notes ok + does not clobber the url",
           r.status_code == 200
           and workspace.load_workspace(CLIENT)["website_health"]["url"] == "https://riverdanceresort.com"
           and workspace.load_workspace(CLIENT)["website_health"]["notes"] == "Pixel verified.")

    # An ADMIN who is NOT the root super admin: SEES the tab but it is READ-ONLY, and every edit route
    # is forbidden ("the admin can just see it").
    with c.session_transaction() as s:
        s.update({"ok": True, "user": "staff@agoradatadriven.com", "clients": ["*"]})
    ap = c.get("/w/%s/website-health" % CLIENT).get_data(as_text=True)
    _check("non-root admin sees the Website Health tab", 'data-pane="website-health"' in ap)
    _check("non-root admin view is read-only (no URL editor)", 'id="ax-wh-url"' not in ap)
    _check("non-root admin still sees the stored result", "GTM-RVR123" in ap)
    for path in ("save", "check"):
        _check("website-health/%s forbidden for non-root admin" % path,
               c.post("/w/%s/admin/website-health/%s" % (CLIENT, path), data={}).status_code == 403)

    # A CLIENT never sees the tab and cannot hit its routes.
    with c.session_transaction() as s:
        s.update({"ok": True, "user": "owner@riverdanceresort.com", "clients": [CLIENT]})
    cp = c.get("/w/%s/website-health" % CLIENT).get_data(as_text=True)
    _check("client never sees the Website Health nav/pane",
           'data-tab="website-health"' not in cp and 'data-pane="website-health"' not in cp)
    for path in ("save", "check"):
        _check("website-health/%s forbidden for client" % path,
               c.post("/w/%s/admin/website-health/%s" % (CLIENT, path), data={}).status_code == 403)
    with c.session_transaction() as s:
        s.update(SUPER)

    # A non-super-admin grantee can open the workspace but is FORBIDDEN on every admin route.
    with c.session_transaction() as s:
        s.update({"ok": True, "user": "owner@riverdanceresort.com", "clients": [CLIENT]})
    _check("grantee can open workspace", c.get("/w/%s/" % CLIENT).status_code == 200)
    _check("grantee cannot see admin bar", 'data-admin="1"' not in c.get("/w/%s/" % CLIENT).get_data(as_text=True))
    # Market Intelligence is client-visible (the nav + seeded entries render) but read-only (no add form).
    gi = c.get("/w/%s/intel" % CLIENT).get_data(as_text=True)
    _check("grantee sees the Market Intelligence tab + entries",
           'data-tab="intel"' in gi and "AI Search Ads expansion" in gi)
    _check("grantee gets a read-only intel view (no add form)", "Add to Business Research" not in gi)
    for path in ("strategy", "campaign", "content", "delete-content", "metrics", "calendar",
                 "generate-summary", "upload-creative", "reply", "intel"):
        _check("admin route /%s forbidden for grantee" % path,
               c.post("/w/%s/admin/%s" % (CLIENT, path), data={}).status_code == 403)
    # But a grantee CAN comment + re-decide (client powers). A "Request changes" comment is a client
    # power; RESOLVING it is TEAM-ONLY -- the grantee is forbidden, and the resolve button is not
    # rendered in their view (gated is_superadmin).
    rc = c.post("/w/%s/comment" % CLIENT,
                data={"content_id": "RVR-014", "body": "please tweak", "kind": "changes"})
    _check("grantee can request changes via comment", rc.status_code == 200)
    cm_id = rc.get_json()["comment"]["id"]
    _check("resolve-comment is team-only (grantee 403)",
           c.post("/w/%s/resolve-comment" % CLIENT,
                  data={"content_id": "RVR-014", "comment_id": cm_id}).status_code == 403)
    _check("resolve button NOT rendered for grantee",
           'data-comresolve="%s"' % cm_id not in c.get("/w/%s/organic" % CLIENT).get_data(as_text=True))

    # A grantee CAN set the client's own logo from inside the workspace (client-facing /w/<c>/logo).
    logo_png = b"\x89PNG\r\n\x1a\n" + b"riverdance-logo-bytes"
    rl = c.post("/w/%s/logo" % CLIENT,
                data={"logo": (io.BytesIO(logo_png), "logo.png", "image/png")},
                content_type="multipart/form-data")
    _check("grantee logo upload ok", rl.status_code == 200 and rl.get_json().get("ok") is True)
    _check("logo persisted inline as a data: URI img",
           "data:image/png;base64," in (workspace.load_workspace(CLIENT).get("brand", {}).get("client_logo") or ""))
    _check("non-image logo upload rejected (400)",
           c.post("/w/%s/logo" % CLIENT,
                  data={"logo": (io.BytesIO(b"x"), "a.txt", "text/plain")},
                  content_type="multipart/form-data").status_code == 400)

    # The team CAN resolve the grantee's change request.
    with c.session_transaction() as s:
        s.update(SUPER)
    _check("team can resolve a change request",
           c.post("/w/%s/resolve-comment" % CLIENT,
                  data={"content_id": "RVR-014", "comment_id": cm_id}).get_json().get("ok") is True)
    with c.session_transaction() as s:
        s.update({"ok": True, "user": "owner@riverdanceresort.com", "clients": [CLIENT]})

    # A user who cannot open the client is forbidden.
    with c.session_transaction() as s:
        s.update({"ok": True, "user": "x@y.com", "clients": ["someoneelse"]})
    _check("non-grantee forbidden", c.get("/w/%s/" % CLIENT).status_code == 403)
    _check("non-grantee creative forbidden", c.get("/w/%s/creative/RVR-014" % CLIENT).status_code == 403)

    # ---- Task tracker: the client Progress tab + the READ-ONLY console board. ----
    #
    # 🔴 REWRITTEN 2026-08-03 (decision D2). This block used to build its whole fixture by POSTing
    # the seven `/w/<c>/admin/task*` routes. Those routes are RETIRED: Atrium no longer writes a
    # task, because an Atrium card is a projection of a Sentinel row. The fixture is therefore built
    # through the `workspace.py` helpers directly — which is exactly what the internal bridge calls,
    # so this still exercises the real write path, just not a browser-facing one. The retirement
    # itself is asserted further down (410 for the team, 403 for a client).
    with c.session_transaction() as s:
        s.update(SUPER)
    made = workspace.add_task(CLIENT, {
        "title": "Park & Porch funnel", "department": "acquisition",
        "lead_id": "zhen@100.digital", "priority": "High",
        "start_date": "2026-07-10", "due_date": "2026-07-20",
        "service_charge": "4200", "client_facing": True,
        "client_note": "Funnel is live.", "deliverable_url": "https://drive.google.com/x",
        "internal_notes": "INTERNAL-ONLY-MARKER-XYZ",
        "labels": ["Paid Media"], "stage": "todo",
    }, actor="info@agoradatadriven.com")
    task_id = made["id"]
    _check("a task exists on the client's board", bool(task_id) and made["stage"] == "todo")
    _check("start date + service charge stored",
           made["start_date"] == "2026-07-10" and made["service_charge"] == "4200")
    workspace.update_task(CLIENT, task_id, {"support_ids": ["ehjay@agoradatadriven.com"]},
                          actor="info@agoradatadriven.com")
    _check("support people patched after creation",
           workspace._find_task(workspace.load_workspace(CLIENT),
                                task_id)["support_ids"] == ["ehjay@agoradatadriven.com"])
    workspace.add_task(CLIENT, {"title": "HIDDEN-INTERNAL-TASK"}, actor="info@agoradatadriven.com")
    # The service-template catalog still BUILDS a breakdown (Sentinel owns the recipes now and pushes
    # the result over the bridge; the module stays because this shape is what it must produce).
    seeded = workspace.add_task(CLIENT, {
        "title": "Seeded G/M Campaign", "department": "acquisition", "client_facing": True,
        "due_date": "2026-08-01", "content_type": "Campaign",
        "maintasks": service_templates.build_maintasks(
            "google_meta_campaign", {}, [("video", "3")], id_factory=workspace._new_id),
    }, actor="info@agoradatadriven.com")
    seeded_task = workspace._find_task(workspace.load_workspace(CLIENT), seeded["id"])
    _check("service type builds the breakdown (build + launch + 1 ad-production group)",
           len(seeded_task["maintasks"]) == 3)
    _check("qty=3 expanded the per-video step",
           len([s for s in seeded_task["maintasks"][2]["subs"] if "draft edit" in s["text"]]) == 3)
    _check("seeded sub-tasks carry an internal 'done when'",
           all(s.get("dod") for m in seeded_task["maintasks"] for s in m["subs"]))
    # The dod is INTERNAL: it must NOT reach the client Progress render.
    with c.session_transaction() as s:
        s.update({"ok": True, "user": "owner@riverdanceresort.com", "clients": [CLIENT]})
    prog = c.get("/w/%s/progress" % CLIENT).get_data(as_text=True)
    _check("client Progress never shows a 'done when' definition", "Done when" not in prog)
    with c.session_transaction() as s:
        s.update(SUPER)
    # The two-level breakdown, the hold and a team comment — same helpers, no routes.
    workspace.add_maintask(CLIENT, task_id, "SECRET-PHASE-RENAMED", "zhen@100.digital")
    main_id = workspace._find_task(workspace.load_workspace(CLIENT), task_id)["maintasks"][0]["id"]
    workspace.add_subtask(CLIENT, task_id, "Create the info pack", "ehjay@agoradatadriven.com",
                          maintask_id=main_id, dod="PDF filed on the card")
    sub_id = workspace.task_subtasks(
        workspace._find_task(workspace.load_workspace(CLIENT), task_id))[0]["id"]
    workspace.set_subtask_done(CLIENT, task_id, sub_id, True)
    _check("the breakdown holds a phase with an owned, done step",
           workspace._find_subtask(
               workspace._find_task(workspace.load_workspace(CLIENT), task_id), sub_id)["done"] is True)
    held = workspace.set_task_hold(CLIENT, task_id, True, "HOLD-REASON-INTERNAL-XYZ",
                                   actor="info@agoradatadriven.com")
    _check("a service can be put on hold (reason internal)", held["on_hold"] is True)
    _check("hold shows on the console card",
           "tk-hold" in c.get("/admin/atrium").get_data(as_text=True))
    workspace.add_task_comment(CLIENT, task_id, "agora", "AGORA", "First draft is up.")
    workspace.move_task_stage(CLIENT, task_id, "revision", actor="info@agoradatadriven.com")

    # 🔴 The console board is a READ-ONLY MONITOR now: it renders every client's tasks and opens the
    # detail overlay, and carries no form that writes one.
    console = c.get("/admin/atrium").get_data(as_text=True)
    _check("console still shows the Task Board nav + the task",
           'data-section="tasks"' in console and "Park &amp; Porch funnel" in console)
    _check("console has the Delivery Calendar tab + pane",
           'data-section="calendar"' in console and 'data-pane="calendar"' in console)
    _check("client creation = page-head button + overlay (inline panel gone)",
           'id="nc-new-btn"' in console and "data-ncnew" in console
           and "Add a new client" not in console)
    _check("scheduled (dated) service becomes a calendar event",
           '<div class="cal-ev" data-date="2026-07-20"' in console
           and 'data-open="%s:%s"' % (CLIENT, task_id) in console)
    # NB: match a form ACTION, not the bare path. `name="op" value="add"` also appears on the
    # Mailboxes connect form, and the path itself still appears in the script's own "removed 2026-08-03"
    # comments — which is exactly where a future reader should find out why it went.
    _check("the console board carries NO task-write form",
           ('action="/w/%s/admin/task' % CLIENT) not in console)
    _check("the overlay's only action is to open the task in Sentinel",
           "Open in Sentinel" in console and "/dashboard?open=atrium:%s:%s" % (CLIENT, task_id) in console)
    _check("no draggable cards on the console board", 'class="tk-card' in console
           and 'draggable="true"' not in console)
    # Every retired route answers 410 with the reason — never a 404, which would read as a routing
    # bug to whoever still has a stale tab open.
    for path in ("task", "task/move", "task/delete", "task/subtask", "task/maintask",
                 "task/comment", "task/hold"):
        r410 = c.post("/w/%s/admin/%s" % (CLIENT, path), data={"task_id": task_id})
        _check("retired route /%s answers 410 for the team" % path,
               r410.status_code == 410 and "Sentinel" in (r410.get_json() or {}).get("error", ""))
    _check("a retired route did NOT change the task",
           workspace._find_task(workspace.load_workspace(CLIENT), task_id)["stage"] == "revision")

    # The CLIENT sees the Progress tab: their client-facing task, client-safe fields ONLY.
    with c.session_transaction() as s:
        s.update({"ok": True, "user": "owner@riverdanceresort.com", "clients": [CLIENT]})
    pg = c.get("/w/%s/progress" % CLIENT).get_data(as_text=True)
    _check("client Progress tab renders the client-facing task",
           'data-pane="progress"' in pg and "Park &amp; Porch funnel" in pg)
    _check("internal notes never reach the client HTML", "INTERNAL-ONLY-MARKER-XYZ" not in pg)
    _check("lead/support identities never reach the client HTML", "zhen@100.digital" not in pg)
    _check("main-task owner identities never reach the client HTML",
           "ehjay@agoradatadriven.com" not in pg)
    _check("phases (main-task names) DO reach the client", "SECRET-PHASE-RENAMED" in pg)
    _check("campaign chip carries the discipline tint (dept=acquisition -> lb-paid)",
           "ax-pg-chip lb-paid" in pg)
    _check("the service charge never reaches the client HTML", "4200" not in pg and "$4,200" not in pg)
    _check("client sees a plain 'Paused' for a held service", "Paused" in pg)
    _check("the hold reason never reaches the client HTML", "HOLD-REASON-INTERNAL-XYZ" not in pg)
    _check("internal-only tasks never reach the client HTML", "HIDDEN-INTERNAL-TASK" not in pg)
    for path in ("task", "task/move", "task/delete", "task/subtask", "task/maintask", "task/comment"):
        _check("admin task route /%s forbidden for the client" % path,
               c.post("/w/%s/admin/%s" % (CLIENT, path), data={}).status_code == 403)
    # The client's ONE write: comment + request changes.
    _check("client comments on a task",
           c.post("/w/%s/task-comment" % CLIENT,
                  data={"task_id": task_id, "body": "Looks great!"}).get_json().get("ok") is True)
    rchg = c.post("/w/%s/task-comment" % CLIENT,
                  data={"task_id": task_id, "body": "Please swap the hero.", "kind": "changes"})
    _check("client raises a task change request", rchg.get_json().get("open_changes") == 1)
    chg_id = rchg.get_json()["comment"]["id"]
    hidden_id = next(t["id"] for t in workspace.load_workspace(CLIENT)["tasks"]
                     if t["title"] == "HIDDEN-INTERNAL-TASK")
    _check("client cannot comment on an internal-only task (404)",
           c.post("/w/%s/task-comment" % CLIENT,
                  data={"task_id": hidden_id, "body": "hi"}).status_code == 404)
    # The client's OTHER write: quick-add from the Progress tab.
    #
    # 🔴 REWRITTEN 2026-08-04 (decision D3 / WP 3.3). This used to assert that a client's ask
    # became a TASK in ws["tasks"] straight away — which is exactly the behaviour the taskboard
    # rebuild removed: anything typed on a live call landed on the delivery board unowned and
    # unestimated, indistinguishable from committed work. The ask is now FILED into Sentinel's
    # intake queue and becomes a task only when a manager accepts it there. So the assertion is
    # inverted: nothing may appear in ws["tasks"].
    calls = []

    def _fake_file(client_key, title, **kw):
        calls.append({"client": client_key, "title": title, **kw})
        return True, ""

    _real_file = sentinel_requests.file_request
    sentinel_requests.file_request = _fake_file
    try:  # noqa: SIM117 — mirrored by _filing_stubbed() below
        radd = c.post("/w/%s/task-add" % CLIENT, data={"title": "CLIENT-ASKED-FOR-THIS"})
        body = radd.get_json()
        _check("client quick-add files a REQUEST (not a task)",
               body.get("ok") is True and body.get("request") is True)
        _check("the ask reached Sentinel with the workspace key + title",
               len(calls) == 1 and calls[0]["client"] == CLIENT
               and calls[0]["title"] == "CLIENT-ASKED-FOR-THIS")
        _check("the requester is auto-tagged from the session, never a form field",
               calls[0].get("requester_name") == "Owner")
        _check("a client ask NEVER lands on the delivery board",
               not any(t.get("title") == "CLIENT-ASKED-FOR-THIS"
                       for t in workspace.load_workspace(CLIENT).get("tasks", [])))
        _check("empty quick-add rejected",
               c.post("/w/%s/task-add" % CLIENT, data={"title": "  "}).status_code == 400)

        # The composer is a REAL form, so an ask must file with NO JavaScript at all: a native post
        # carries redirect=progress and gets a redirect back to the tab instead of JSON.
        rform = c.post("/w/%s/task-add" % CLIENT,
                       data={"title": "FILED-WITHOUT-JS", "redirect": "progress"})
        _check("no-JS form post files the request and redirects back to Progress",
               rform.status_code in (301, 302, 303)
               and "/w/%s/progress" % CLIENT in rform.headers.get("Location", ""))
        _check("the no-JS ask reached Sentinel too", len(calls) == 2)

        # 🔴 A bridge failure must be TOLD to the client. Silently succeeding would say "sent" when
        # nobody has it; silently falling back to a local task would restore the very bug D3 fixed.
        sentinel_requests.file_request = lambda *a, **k: (False, "Sentinel unreachable")
        rfail = c.post("/w/%s/task-add" % CLIENT, data={"title": "BRIDGE-DOWN"})
        _check("a bridge failure surfaces to the client as an error", rfail.status_code == 502)
        _check("a failed ask does NOT fall back to writing a task",
               not any(t.get("title") == "BRIDGE-DOWN"
                       for t in workspace.load_workspace(CLIENT).get("tasks", [])))
    finally:
        sentinel_requests.file_request = _real_file
    _check("an empty no-JS post redirects back rather than erroring",
           c.post("/w/%s/task-add" % CLIENT,
                  data={"title": " ", "redirect": "progress"}).status_code in (301, 302, 303))
    # The "Requested by" chip marks a card the CLIENT asked for. Since D3 the composer no longer
    # creates one — the ask goes to Sentinel and comes back over the bridge once accepted — so the
    # fixture seeds one directly, which is exactly the shape the bridge writes.
    workspace.add_task(CLIENT, {
        "title": "ACCEPTED-CLIENT-ASK", "stage": "todo", "client_facing": True,
        "reporter": "client", "reporter_name": "Owner",
    }, actor="smoketest")
    pg2 = c.get("/w/%s/progress" % CLIENT).get_data(as_text=True)
    _check("progress still renders the quick-add composer", "data-pgadd-input" in pg2)
    _check("a client-reported card still carries its 'Requested by' chip", "Requested by" in pg2)
    _check("the composer is a real form that posts to task-add without JS",
           'method="post"' in pg2 and ('action="/w/%s/task-add"' % CLIENT) in pg2
           and 'name="title"' in pg2)
    # The 2026-07-29 stage trim: For Review + Waiting for Client are gone (both just meant
    # "blocked on someone") and Blocked sits right after In Progress.
    _check("Tasks board renders exactly the 5 stage columns, Blocked right after In Progress",
           all(name in pg2 for name in ("To Do", "In Progress", "Blocked",
                                        "Revision Needed", "Completed"))
           and "For Review" not in pg2 and "Waiting for Client" not in pg2
           and pg2.index("In Progress") < pg2.index("Blocked") < pg2.index("Revision Needed"))
    # 🔴 FLIPPED 2026-08-04 (D3 / WP 3.3): the per-column "+ Add card" is TEAM-ONLY now. A client
    # files a REQUEST, and a request has no column, so offering them the choice was offering a
    # control that could not do anything. `pg2` is a CLIENT render.
    # NB: match the RENDERED attribute (with a stage key), not the bare name -- the toggle script
    # builds the selector as a string literal and that script ships to every viewer, so the bare
    # substring is present in a client's HTML whatever the template does. Same trap as keying
    # team-only CSS off [data-admin]: the stylesheet/script is not role-scoped, the markup is.
    _check("the per-column '+ Add card' form is absent from the CLIENT's HTML",
           not any(('data-pgcol-form="%s"' % k) in pg2
                   for k in ("todo", "in_progress", "blocked", "revision", "completed"))
           and not any(('data-pgcol-open="%s"' % k) in pg2
                       for k in ("todo", "in_progress", "blocked", "revision", "completed")))
    # 🔴 REWRITTEN 2026-08-04 (D3 / WP 3.3). A per-column "+ Add card" used to place the client's
    # card directly into that column. A client's ASK has no column — it is not on the delivery
    # board at all until Sentinel accepts it — so the posted `stage` is now ignored for a client,
    # and nothing is written locally whatever column the form came from.
    # (Remaining polish, tracked in the roadmap: drop the per-column form from the CLIENT surface
    # entirely, since it now offers a choice that has no effect.)
    with _filing_stubbed() as filed:
        rcol = c.post("/w/%s/task-add" % CLIENT,
                      data={"title": "FILED-INTO-BLOCKED", "stage": "blocked",
                            "redirect": "progress"})
        _check("a client's per-column add still files a request",
               rcol.status_code in (301, 302, 303) and len(filed) == 1)
        _check("a client's ask never lands in a column",
               not any(t.get("title") == "FILED-INTO-BLOCKED"
                       for t in workspace.load_workspace(CLIENT).get("tasks", [])))
        _check("a junk stage can never 500 or lose the ask",
               c.post("/w/%s/task-add" % CLIENT,
                      data={"title": "JUNK-STAGE", "stage": "not-a-stage"}
                      ).get_json().get("ok") is True and len(filed) == 2)
    # The add form mirrors Sentinel's "New task": name + description + due date on show, the rest
    # collapsed. A CLIENT must never get the internal block, nor be able to forge those fields.
    # A request carries a title + context. NOT a due date: a request is not scheduled work, and
    # the team sets dates when they accept it (D3 / WP 3.3) -- the same reasoning that took the
    # column choice away.
    _check("the client's request form has a name and a description",
           'name="title"' in pg2 and 'name="note"' in pg2)
    _check("a client is not asked for a due date on a request", 'name="due_date"' not in pg2)
    # NB: match the ELEMENT, not the bare class -- ".ax-pg-more" also appears in the stylesheet,
    # which every page carries whatever the viewer's role is.
    _check("the internal 'More options' block is NOT in the client's HTML",
           '<details class="ax-pg-more"' not in pg2
           and 'name="internal_notes"' not in pg2 and 'name="priority"' not in pg2)
    # The team's board controls are markup, not just hidden CSS: a client's HTML must not carry the
    # draggable wrappers or the delete buttons at all (same no-leak posture as the internal fields).
    _check("drag + delete affordances are NOT in the client's HTML",
           "data-pgdrag=" not in pg2 and "data-pgdel=" not in pg2)
    _check("the duplicated per-stage count tiles are gone from the board",
           "ax-pg-summary" not in pg2)
    # Forging internal fields is now impossible by CONSTRUCTION rather than by filtering: a client
    # post creates no local task at all, and the intake bridge only ever carries title / details /
    # requester. Priority, internal notes and the rest have nowhere to land (D3 / WP 3.3).
    with _filing_stubbed() as forged:
        rforge = c.post("/w/%s/task-add" % CLIENT,
                        data={"title": "CLIENT-FORGERY", "priority": "Urgent",
                              "internal_notes": "should never stick", "due_date": "2026-09-01"})
        _check("a client's forged fields have nowhere to go", rforge.get_json().get("ok") is True)
        _check("the ask carries only title/details/requester over the bridge",
               set(forged[0]) <= {"client", "title", "details",
                                  "requester_name", "requester_email"})
        _check("no priority or internal notes cross the bridge",
               "priority" not in forged[0] and "internal_notes" not in forged[0])
        _check("and no local task is written for a client at all",
               not any(t.get("title") == "CLIENT-FORGERY"
                       for t in workspace.load_workspace(CLIENT).get("tasks", [])))
    # Rows written under a RETIRED stage key must still land in a real column: the old 4-stage
    # keys, and (since 2026-07-29) For Review / Waiting for Client, which fold into Blocked.
    _check("a legacy stage key is translated, not dropped",
           workspace.canon_stage("for_launch") == "blocked"
           and workspace.canon_stage("launched") == "completed"
           and workspace.canon_stage("for_review") == "blocked"
           and workspace.canon_stage("waiting_client") == "blocked"
           and workspace.canon_stage("") == "todo")

    # Back to the team: the open change request blocks closing, resolving unblocks it.
    with c.session_transaction() as s:
        s.update(SUPER)
    # Team quick-add from the same composer (live-call capture) files as agora.
    radd2 = c.post("/w/%s/task-add" % CLIENT, data={"title": "TYPED-LIVE-ON-CALL"})
    _check("team quick-add auto-tags reporter agora",
           radd2.get_json().get("reporter") == "agora"
           and workspace._find_task(workspace.load_workspace(CLIENT),
                                    radd2.get_json()["task_id"])["client_facing"] is True)
    _check("console board flags the client-filed request",
           "Client req" in c.get("/admin/atrium").get_data(as_text=True))
    # The TEAM does get the collapsed internal block, and those fields stick.
    pg_team = c.get("/w/%s/progress" % CLIENT).get_data(as_text=True)
    _check("the team's add form carries the collapsed 'More options' block",
           '<details class="ax-pg-more"' in pg_team and 'name="internal_notes"' in pg_team
           and 'name="priority"' in pg_team)
    # 🔴 REVERSED 2026-08-03 (decision D2). This used to assert the OPPOSITE — that the team's cards
    # were draggable and deletable here. The client Tasks board is now a READ-ONLY projection of
    # Sentinel's task rows for EVERYONE: a card is created, assigned, moved, parked, reviewed and
    # filed in Sentinel, and the internal bridge pushes the client-safe subset over. Two writers on
    # one record is exactly the model this replaced (sentinel/docs/TASKBOARD_REBUILD.md §4), so the
    # affordances must be absent from the TEAM's HTML too, not merely hidden from the client's.
    _check("the team's board carries NO write affordances either (read-only for everyone)",
           "data-pgdrag=" not in pg_team and "data-pgdel=" not in pg_team
           and 'data-pgcol="' not in pg_team)
    rteam = c.post("/w/%s/task-add" % CLIENT,
                   data={"title": "TEAM-WITH-EXTRAS", "priority": "Urgent",
                         "internal_notes": "keep this internal", "note": "client sees this",
                         "due_date": "2026-10-05"})
    _extra = workspace._find_task(workspace.load_workspace(CLIENT),
                                  rteam.get_json()["task_id"])
    _check("the team's priority, internal notes, description and due date all persist",
           _extra["priority"] == "Urgent" and _extra["internal_notes"] == "keep this internal"
           and _extra["client_note"] == "client sees this" and _extra["due_date"] == "2026-10-05")
    # (The no-leak rule itself is asserted against a real client session by the
    #  "internal notes never reach the client HTML" check earlier in this file.)
    # Stage moves are UNGUARDED (2026-07-28) -- an open change request no longer vetoes a move to
    # Completed, so a card never bounces back. It is still surfaced as a flag. (Driven through the
    # helper now: the route went with the rest of Atrium's task writers, and the internal bridge
    # calls exactly this. `move_task_stage` keeps its ValueError contract for a future guard.)
    _check("close is allowed while a change request is open",
           workspace.move_task_stage(CLIENT, task_id, "completed",
                                     actor="info@agoradatadriven.com")["stage"] == "completed")
    _check("team resolves the change request",
           workspace.resolve_task_comment(CLIENT, task_id, chg_id)[2] == 0)

    # Delete -> Bin -> restore round-trip. The Bin is written by whoever deletes; Sentinel's bridge
    # delete calls `audit.trash_put` directly (there is no session to credit), so mirror that here.
    _removed = workspace.delete_task(CLIENT, task_id)
    import audit as _audit_bin
    _audit_bin.trash_put(client=CLIENT, kind="task",
                         label=_removed.get("title") or task_id, payload=_removed,
                         actor="info@agoradatadriven.com", role="superadmin")
    _check("task delete is a soft-delete", _removed.get("id") == task_id)
    _check("task gone from the workspace",
           workspace._find_task(workspace.load_workspace(CLIENT), task_id) is None)
    _audit_mod = _audit_bin
    entry = next(t for t in _audit_mod.trash_list() if t.get("kind") == "task")
    _check("deleted task is in the Bin", entry["payload"]["id"] == task_id)
    _check("task restore returns it to the board",
           c.post("/admin/atrium/restore", data={"entry_id": entry["id"]}).status_code == 302
           and workspace._find_task(workspace.load_workspace(CLIENT), task_id) is not None)

    # ---- Task board Export / Import (JSON backup + non-destructive restore, super-admin). ----
    with c.session_transaction() as s:
        s.update(SUPER)
    ex = c.get("/admin/atrium/tasks/export")
    _check("task export returns a JSON attachment",
           ex.status_code == 200 and "attachment" in ex.headers.get("Content-Disposition", ""))
    exported = ex.get_json()
    _check("export payload carries this client's tasks",
           CLIENT in exported.get("clients", {}) and exported["clients"][CLIENT]["tasks"])
    with c.session_transaction() as s:
        s.update({"ok": True, "user": "owner@riverdanceresort.com", "clients": [CLIENT]})
    _check("client cannot export (super-admin only)",
           c.get("/admin/atrium/tasks/export").status_code == 403)
    _check("client cannot import (super-admin only)",
           c.post("/admin/atrium/tasks/import").status_code == 403)
    # Import an edited copy: change one task's title (update-by-id) + add a brand-new task.
    with c.session_transaction() as s:
        s.update(SUPER)
    imp = dict(exported)
    imp_tasks = list(exported["clients"][CLIENT]["tasks"])
    imp_tasks[0] = dict(imp_tasks[0], title="Imported title change")
    imp_tasks.append({"id": "tk_imported_new", "title": "Imported new task", "stage": "todo"})
    imp = {"version": 1, "clients": {CLIENT: {"name": "Riverdance", "tasks": imp_tasks},
                                     "ghostclient": {"tasks": [{"id": "x", "title": "skip me"}]}}}
    ri = c.post("/admin/atrium/tasks/import",
                data={"file": (io.BytesIO(json.dumps(imp).encode()), "backup.json", "application/json")},
                content_type="multipart/form-data")
    _check("import redirects back to the Tasks pane", ri.status_code == 302)
    after = {t["id"]: t for t in workspace.load_workspace(CLIENT)["tasks"]}
    _check("import UPDATED an existing task by id", after[imp_tasks[0]["id"]]["title"] == "Imported title change")
    _check("import ADDED the new task", "tk_imported_new" in after)
    _check("import skipped a client that no longer exists",
           workspace.load_workspace("ghostclient") is None)

    # ---- The internal task bridge, WRITE half (Sentinel edits these cards without leaving its own
    # board). HMAC-gated, server-to-server. Sentinel already READ the board over
    # /api/internal/tasks; these four routes are what replaced its "open it in Atrium to edit"
    # dead end, so they must go through the SAME workspace helpers the console forms use.
    import hashlib as _hashlib
    import hmac as _hmac
    import time as _time
    import audit as _audit_mod
    real_secret = main.SSO_SECRET
    main.SSO_SECRET = "bridge-test-secret"
    store.add_client(CLIENT, "Riverdance RV")     # the bridge names the client from the registry

    def _sign(purpose):
        ts = str(int(_time.time()))
        return {"X-Academy-Ts": ts,
                "X-Academy-Sig": _hmac.new(main.SSO_SECRET.encode(),
                                           ("%s:%s" % (purpose, ts)).encode(),
                                           _hashlib.sha256).hexdigest()}

    def _bridge(purpose, path, payload=None):
        if payload is None:
            return c.get(path, headers=_sign(purpose))
        return c.post(path, json=payload, headers=_sign(purpose))

    _check("every internal task write is refused unsigned",
           c.get("/api/internal/task?client=%s&task=x" % CLIENT).status_code == 401
           and c.post("/api/internal/task-update", json={}).status_code == 401
           and c.post("/api/internal/task-delete", json={}).status_code == 401
           and c.post("/api/internal/task-comment", json={}).status_code == 401)
    _check("a wrong signature is refused",
           c.get("/api/internal/task?client=%s&task=x" % CLIENT,
                 headers={"X-Academy-Ts": str(int(_time.time())),
                          "X-Academy-Sig": "0" * 64}).status_code == 401)

    btid = _bridge("task-add", "/api/internal/task-add",
                   {"client_key": CLIENT, "title": "Bridge-edited card", "client_facing": True,
                    "actor": "leo@agora.ph"}).get_json()["task_id"]

    det = _bridge("task-detail", "/api/internal/task?client=%s&task=%s" % (CLIENT, btid)).get_json()
    _check("task-detail returns the card PLUS the pickers' vocabularies",
           det["task"]["task_id"] == btid and det["task"]["client_name"]
           and det["roster"] and det["departments"] and det["stages"])
    _check("task-detail 404s on a card that no longer exists (never an empty answer)",
           _bridge("task-detail",
                   "/api/internal/task?client=%s&task=nope" % CLIENT).status_code == 404)

    upd = _bridge("task-update", "/api/internal/task-update", {
        "client_key": CLIENT, "task_id": btid, "actor": "leo@agora.ph",
        "fields": {"title": "Renamed from Sentinel", "department": "acquisition",
                   "priority": "Urgent", "due_date": "2026-09-30", "service_charge": "$4,200",
                   "internal_notes": "internal only", "client_note": "the client reads this",
                   "lead_id": "leo@agora.ph", "client_facing": True,
                   "on_hold": True, "hold_reason": "waiting on assets",
                   "maintasks": [{"id": "", "text": "Phase 1", "assignee_id": "",
                                  "subs": [{"id": "st_new_1", "text": "Draft", "done": False}]}]},
    }).get_json()["task"]
    _check("an edit over the bridge writes every field it was given",
           upd["title"] == "Renamed from Sentinel" and upd["priority"] == "Urgent"
           and upd["due_date"] == "2026-09-30" and upd["internal_notes"] == "internal only"
           and upd["client_note"] == "the client reads this" and upd["on_hold"] is True
           and upd["hold_reason"] == "waiting on assets")
    _check("the department drives the label, exactly as the console form derives it",
           upd["labels"] == ["Paid Media"])
    _check("the service charge is normalised, not stored with $ and commas",
           upd["service_charge"] == "4200")
    _sid = upd["maintasks"][0]["subs"][0]["id"]
    _check("a foreign placeholder id never becomes an Atrium id",
           _sid.startswith("st_") and _sid != "st_new_1")

    # `dod` is Atrium-internal: Sentinel neither shows nor sends it, so a breakdown edit from over
    # there must not quietly drop it off a sub-task that kept its id.
    workspace.edit_subtask(CLIENT, btid, _sid, dod="done when the client approves")
    kept = _bridge("task-update", "/api/internal/task-update", {
        "client_key": CLIENT, "task_id": btid, "actor": "leo@agora.ph",
        "fields": {"maintasks": [{"id": upd["maintasks"][0]["id"], "text": "Phase 1",
                                  "subs": [{"id": _sid, "text": "Draft v2", "done": True}]}]},
    }).get_json()["task"]
    _check("an edit that can't see 'done when' still preserves it",
           kept["maintasks"][0]["subs"][0]["dod"] == "done when the client approves"
           and kept["maintasks"][0]["subs"][0]["text"] == "Draft v2"
           and kept["maintasks"][0]["subs"][0]["done"] is True)

    moved = _bridge("task-update", "/api/internal/task-update",
                    {"client_key": CLIENT, "task_id": btid, "actor": "leo@agora.ph",
                     "fields": {"stage": "revision"}}).get_json()["task"]
    _check("a stage move over the bridge is a real move, with its own history entry",
           moved["stage"] == "revision" and moved["status"] == "Revision Needed"
           and any(h["field"] == "stage" for h in moved["history"]))
    # A Sentinel that still speaks the retired keys (its board keeps For Review / Waiting for
    # Client columns) must not error or invent a column: the write lands on Blocked.
    aliased = _bridge("task-update", "/api/internal/task-update",
                      {"client_key": CLIENT, "task_id": btid, "actor": "leo@agora.ph",
                       "fields": {"stage": "for_review"}}).get_json()["task"]
    _check("a retired stage key from the bridge lands on Blocked",
           aliased["stage"] == "blocked" and aliased["status"] == "Blocked")

    cm = _bridge("task-comment", "/api/internal/task-comment",
                 {"client_key": CLIENT, "task_id": btid, "body": "Posted from Sentinel",
                  "actor": "leo@agora.ph", "actor_name": "Leo"}).get_json()
    _check("a comment from the bridge is a TEAM comment on the client's own thread",
           cm["comment"]["sender"] == "agora" and cm["comment"]["sender_name"] == "Leo"
           and cm["comment_count"] == 1)

    workspace.add_task_comment(CLIENT, btid, "client", "Owner", "please redo", kind="changes")
    _open = workspace._find_task(workspace.load_workspace(CLIENT), btid)
    _cid = workspace.task_open_changes(_open)[0]["id"]
    res = _bridge("task-comment", "/api/internal/task-comment",
                  {"client_key": CLIENT, "task_id": btid, "op": "resolve", "comment_id": _cid,
                   "actor": "leo@agora.ph"}).get_json()
    _check("the team can clear a client's change request from the bridge too",
           res["ok"] is True and res["open_changes"] == 0)

    _check("delete over the bridge soft-deletes into the console Bin",
           _bridge("task-delete", "/api/internal/task-delete",
                   {"client_key": CLIENT, "task_id": btid,
                    "actor": "leo@agora.ph"}).get_json()["ok"] is True
           and not any(t["id"] == btid for t in workspace.load_workspace(CLIENT)["tasks"])
           and any(e.get("kind") == "task" and e.get("payload", {}).get("id") == btid
                   for e in _audit_mod.trash_list()))
    _check("a mutation on a card that is gone 404s rather than half-succeeding",
           _bridge("task-update", "/api/internal/task-update",
                   {"client_key": CLIENT, "task_id": btid,
                    "fields": {"title": "x"}}).status_code == 404
           and _bridge("task-delete", "/api/internal/task-delete",
                       {"client_key": CLIENT, "task_id": btid}).status_code == 404)
    main.SSO_SECRET = real_secret

    # --- The Company tab (2026-07-29): client-VISIBLE content, team-only WRITES ------------------
    # The inverse of the team tabs: a client sees the whole profile (it is their own company) but
    # none of the edit affordances, and every write is gated. Also proves the ordered lists survive
    # a round trip through the route and that a delete lands in the Bin.
    with c.session_transaction() as s:
        s.update(SUPER)
    _check("the company tab returns 200", c.get("/w/%s/company" % CLIENT).status_code == 200)
    _check("saving the at-a-glance facts persists",
           c.post("/w/%s/admin/company" % CLIENT,
                  data={"op": "profile", "one_liner": "COMPANYONELINER",
                        "industry": "Hospitality", "website": "riverdanceresort.com"}
                  ).get_json()["ok"] is True
           and workspace.company_profile(workspace.load_workspace(CLIENT))["profile"]["industry"]
           == "Hospitality")
    _check("saving the brand guide persists",
           c.post("/w/%s/admin/company" % CLIENT,
                  data={"op": "brand", "voice": "COMPANYVOICE", "colors": "#1F4D3A, #F4E9D8"}
                  ).get_json()["ok"] is True
           and workspace.company_profile(workspace.load_workspace(CLIENT))["brand"]["voice"]
           == "COMPANYVOICE")
    sec_id = c.post("/w/%s/admin/company" % CLIENT,
                    data={"op": "add", "kind": "sections", "heading": "COMPANYSTORY",
                          "body": "We started with one van."}).get_json()["item"]["id"]
    sec2_id = c.post("/w/%s/admin/company" % CLIENT,
                     data={"op": "add", "kind": "sections", "heading": "Our history",
                           "body": "Then twelve."}).get_json()["item"]["id"]
    prod_id = c.post("/w/%s/admin/company" % CLIENT,
                     data={"op": "add", "kind": "products", "name": "COMPANYPRODUCT",
                           "price": "from $249/night"}).get_json()["item"]["id"]
    body = c.get("/w/%s/company" % CLIENT).get_data(as_text=True)
    _check("the admin render shows the profile AND the edit affordances",
           "COMPANYONELINER" in body and "COMPANYVOICE" in body and "COMPANYSTORY" in body
           and "COMPANYPRODUCT" in body and 'data-coadd="sections"' in body
           and "Draft with AI" in body)
    _check("a colour token renders as a swatch chip", "ax-co-swatch" in body)
    _check("op=move reorders the story",
           c.post("/w/%s/admin/company" % CLIENT,
                  data={"op": "move", "kind": "sections", "item_id": sec2_id, "dir": "up"}
                  ).get_json()["order"][0] == sec2_id)
    _check("op=edit updates one item in place",
           c.post("/w/%s/admin/company" % CLIENT,
                  data={"op": "edit", "kind": "sections", "item_id": sec_id,
                        "body": "One van, then twelve."}).get_json()["item"]["body"]
           == "One van, then twelve.")
    _check("an unknown company list is rejected",
           c.post("/w/%s/admin/company" % CLIENT,
                  data={"op": "add", "kind": "nope", "name": "x"}).status_code == 400)

    # The client: sees everything, can change nothing.
    with c.session_transaction() as s:
        s.update({"ok": True, "user": "owner@riverdanceresort.com", "clients": [CLIENT]})
    cbody = c.get("/w/%s/company" % CLIENT).get_data(as_text=True)
    _check("the client sees their own company profile in full",
           "COMPANYONELINER" in cbody and "COMPANYVOICE" in cbody and "COMPANYSTORY" in cbody
           and "COMPANYPRODUCT" in cbody)
    # Assert on RENDERED markup only: the wiring's own selector strings ("[data-codelete]", ...)
    # are inline JS literals that ship to every viewer, so their presence proves nothing. Same
    # lesson the Communications no-leak check records just above.
    _check("the client's HTML carries NO company edit affordances",
           "Draft with AI" not in cbody and 'data-coform="facts"' not in cbody
           and 'data-coadd="sections"' not in cbody
           and "Add a product or service" not in cbody and 'title="Move up"' not in cbody)
    _check("a client POST to /admin/company is forbidden",
           c.post("/w/%s/admin/company" % CLIENT,
                  data={"op": "profile", "one_liner": "hacked"}).status_code == 403)
    _check("the forbidden POST changed nothing",
           workspace.company_profile(workspace.load_workspace(CLIENT))["profile"]["one_liner"]
           == "COMPANYONELINER")
    with c.session_transaction() as s:
        s.update(SUPER)
    _check("deleting a product soft-deletes it to the Bin (restorable, not lost)",
           c.post("/w/%s/admin/company" % CLIENT,
                  data={"op": "delete", "kind": "products", "item_id": prod_id}
                  ).get_json()["ok"] is True
           and not workspace.company_items(workspace.load_workspace(CLIENT), "products")
           and any(e.get("kind") == "company_product"
                   and e.get("payload", {}).get("id") == prod_id
                   for e in _audit_mod.trash_list()))
    _check("Restore brings the product back from the Bin",
           c.post("/admin/atrium/restore",
                  data={"entry_id": next(e["id"] for e in _audit_mod.trash_list()
                                         if e.get("kind") == "company_product")}
                  ).status_code in (302, 303)
           and workspace.company_items(workspace.load_workspace(CLIENT),
                                       "products")[0]["name"] == "COMPANYPRODUCT")

    # The whole point of the tab: the Assistant can answer from it. The profile must reach the
    # knowledge index as its own retrievable chunks, not be silently dropped.
    import assistant_ai as _assistant_ai
    chunks = _assistant_ai.build_chunks(workspace.load_workspace(CLIENT), [])
    company_chunks = [ch for ch in chunks if ch["kind"] == "company"]
    _check("the company profile is indexed for the Assistant",
           len(company_chunks) >= 3
           and any("COMPANYONELINER" in ch["text"] for ch in company_chunks)
           and any("COMPANYVOICE" in ch["text"] for ch in company_chunks)
           and any("COMPANYPRODUCT" in ch["text"] for ch in company_chunks))
    _check("the story section is its own chunk, titled by its heading",
           any("COMPANYSTORY" in ch["title"] for ch in company_chunks))

    # The regrouped nav (2026-07-29): FOUR top-level rows -- Working Together / Company /
    # Campaigns / Insights -- with every other surface a child. Assert each tab sits in the group
    # it belongs to, and that the top level really is four rows (one flat link + three groups).
    nav = c.get("/w/%s/company" % CLIENT).get_data(as_text=True)
    nav = nav[nav.index('<nav class="ax-nav"'):nav.index("</nav>")]
    _check("the nav has exactly 4 top-level rows (Company + 3 groups)",
           nav.count("ax-nav-group") == 3 and nav.count('class="ax-nav-ghead"') == 3
           and nav.count('data-tab="company"') == 1)
    _check("Dashboard, Communications and Tasks moved under Working Together",
           nav.index("Working Together") < nav.index('data-tab="dashboard"')
           < nav.index('data-tab="conversations"') < nav.index('data-tab="progress"')
           < nav.index('data-tab="company"'))
    _check("Working Together is FIRST -- it holds the landing tab",
           nav.index("Working Together") < nav.index("Campaigns")
           and nav.index("Working Together") < nav.index("Insights"))
    _check("the Content Calendar moved under the Campaigns group",
           nav.index("Campaigns") < nav.index('data-tab="calendar"') < nav.index("Insights"))
    _check("Reports moved under the Insights group",
           nav.index('data-tab="reports"') > nav.index("Insights"))
    # The landing tab lives inside a group now, so its group MUST render already open -- otherwise
    # a client arriving at /w/<c>/ sees a rail with no active item anywhere on it.
    land = c.get("/w/%s/" % CLIENT).get_data(as_text=True)
    land = land[land.index('<nav class="ax-nav"'):land.index("</nav>")]
    _check("landing on /w/<c>/ renders the Working Together group already open",
           land.index("ax-nav-group is-open") < land.index("Working Together"))

    print("[smoketest] PASS")
    return 0


def main_():
    try:
        return run()
    except AssertionError as exc:
        print("[smoketest] FAIL: %s" % exc)
        return 1
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main_())
