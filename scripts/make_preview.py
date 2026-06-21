"""Regenerate the local, no-server previews from the CURRENT code.

Outputs (double-clickable, open straight in a browser, never go live):
  Preview.html              - one client dashboard (sample data baked in)
  Portal Preview/*.html     - the portal/CRM front-door, all four pages, linked

Run via scripts/Rebuild Preview.bat, or:  python scripts/make_preview.py
"""

import json
import os
import datetime

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def build_dashboard_preview():
    src = os.path.join(REPO, "clients", "client_template", "dash", "dashboard.html")
    with open(src, "r", encoding="utf-8") as f:
        html = f.read()

    daily = []
    start = datetime.date(2026, 5, 22)
    tot_s = tot_u = tot_c = 0
    tot_spend = tot_rev = 0.0
    for i in range(30):
        d = start + datetime.timedelta(days=i)
        sessions = 1200 + (i * 37) % 900 + (i % 7) * 120
        users = int(sessions * 0.74)
        conversions = 20 + (i * 5) % 45
        spend = 300 + (i * 13) % 250 + (i % 5) * 40
        revenue = spend * (2.6 + ((i * 7) % 18) / 10.0)
        daily.append({
            "metric_date": d.isoformat(),
            "sessions": sessions, "users": users, "conversions": conversions,
            "spend": round(spend, 2), "revenue": round(revenue, 2),
            "roas": round(revenue / spend, 2),
        })
        tot_s += sessions; tot_u += users; tot_c += conversions
        tot_spend += spend; tot_rev += revenue

    data = {
        "client": "Template Client (SAMPLE DATA)",
        "data_through": daily[-1]["metric_date"],
        "last_updated": "preview - not live data",
        "kpis": {
            "sessions": tot_s, "users": tot_u, "conversions": tot_c,
            "spend": round(tot_spend, 2), "revenue": round(tot_rev, 2),
            "roas": round(tot_rev / tot_spend, 2), "days_covered": len(daily),
        },
        "daily": daily,
    }

    if "load();" not in html:
        raise SystemExit("dashboard.html no longer ends with load(); update make_preview.py")
    html = html.replace("load();", "render(" + json.dumps(data) + ");")
    out = os.path.join(REPO, "Preview.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    print("wrote", out)


def build_portal_preview():
    from jinja2 import Environment, FileSystemLoader

    tpl_dir = os.path.join(REPO, "agora-platform", "dash", "templates")
    out_dir = os.path.join(REPO, "Portal Preview")
    os.makedirs(out_dir, exist_ok=True)
    env = Environment(loader=FileSystemLoader(tpl_dir), autoescape=True)

    clients = [
        {"key": "template", "name": "Template Client"},
        {"key": "acme", "name": "Acme Pty Ltd"},
        {"key": "northwind", "name": "Northwind Trading"},
        {"key": "globex", "name": "Globex Marketing"},
    ]
    accounts = [{"key": c["key"], "name": c["name"], "secret": c["key"] + "-dash-password"} for c in clients]
    accounts.append({"key": "platform-super-admin", "name": "Portal Super Admin", "secret": "platform-super-admin-password"})

    contexts = {
        "login.html": dict(error=None, email="", next="/"),
        "portal.html": dict(user="info@agoradatadriven.com", is_admin=True, is_superadmin=True, clients=clients),
        "admin.html": dict(clients=clients, message="Sample preview - changes are not saved.", message_is_error=False, is_superadmin=True),
        "superadmin.html": dict(accounts=accounts),
    }
    names = {
        "login.html": "1-Login.html",
        "portal.html": "2-Portal.html",
        "admin.html": "3-Admin.html",
        "superadmin.html": "4-Super-Admin.html",
    }

    def localize(html):
        reps = [
            ('action="/login"', 'action="2-Portal.html" method="get"'),
            ('method="post" action="2-Portal.html" method="get"', 'method="get" action="2-Portal.html"'),
            ('href="/superadmin"', 'href="4-Super-Admin.html"'),
            ('href="/admin"', 'href="3-Admin.html"'),
            ('href="/logout"', 'href="1-Login.html"'),
            ('href="/"', 'href="2-Portal.html"'),
        ]
        for a, b in reps:
            html = html.replace(a, b)
        return html

    # Only render the templates that still exist (tolerate added/removed pages).
    available = {f for f in os.listdir(tpl_dir) if f.endswith(".html")}
    for tpl, ctx in contexts.items():
        if tpl not in available:
            print("skip (not found):", tpl)
            continue
        html = env.get_template(tpl).render(**ctx)
        html = localize(html)
        for c in clients:
            html = html.replace('href="/d/%s/"' % c["key"], 'href="../Preview.html"')
        out = os.path.join(out_dir, names[tpl])
        with open(out, "w", encoding="utf-8") as f:
            f.write(html)
        print("wrote", out)


if __name__ == "__main__":
    build_dashboard_preview()
    build_portal_preview()
    print("\nDone. Double-click Preview.html or 'Portal Preview/1-Login.html'.")
