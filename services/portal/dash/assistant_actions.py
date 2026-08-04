"""The Assistant's action registry: everything the AI may PROPOSE, and how an approval executes it.

The contract (the Sentinel approval posture, applied to AI):
  1. The model can only ever PROPOSE -- assistant_ai's ===ATRIUM_ACTIONS=== protocol emits raw
     {action, params, note} proposals with the answer.
  2. `validate` checks each proposal against THIS registry (known action, required params, legal
     choices) and attaches a human-readable label; invalid proposals are surfaced as errors on the
     approval card, never silently dropped or "fixed".
  3. NOTHING runs until a human clicks Approve: the route (`op=execute`) re-validates and calls
     `execute` -- the model has no path to this function. Root-gated actions (website health edits)
     additionally require the approver to be THE super admin, mirroring the human forms exactly.
  4. Every executor calls the SAME workspace.py writers the human routes call, so stored shape,
     history entries, audit trail and the Bin behave identically whichever hand made the edit
     (the same rule the Sentinel task bridge follows).

Adding an action = one _ACTIONS entry + one executor function. Keep executors tiny: resolve the
target, call the workspace helper, return a plain-English result line for the chat.
"""

import json


# --- the registry ---------------------------------------------------------------------------------
# name -> {desc: catalog line for the model, params: {name: spec}, gate: "admin"|"root"}.
# A param spec: {"req": bool, "choices": (...)} -- everything arrives as a string unless noted.
_ACTIONS = {
    # add_task / move_task / complete_task were retired by D2 (2026-08-03) and RESTORED 2026-08-04
    # (D2 amended — see the route comment in main.py): `ws["tasks"]` is the STORE (D1), Sentinel's
    # board lists it live over the bridge, and both sides write through the same workspace.py
    # helpers — so an approved proposal is the same record everywhere, not a racing second writer.
    # `stage` on add_task is what makes "here's what I finished this week" file straight into
    # Completed; client_facing defaults TRUE because these proposals are made looking at the client
    # Tasks board. (The one caveat, same as the team routes: a card CLAIMED by a Sentinel row is
    # re-projected on the next Sentinel-side edit — Sentinel stays authoritative for those.)
    "add_task": {
        "desc": "add_task(title*, stage?, department?, priority?, due_date?, start_date?, note?, "
                "client_facing?) - create a task on the delivery board, in a given column "
                "(stage todo|in_progress|blocked|revision|completed, default todo; priority "
                "Low|Medium|High|Urgent; dates YYYY-MM-DD; client_facing true|false, default true "
                "so it shows on the client's Tasks board). Use one add_task per item when the "
                "user lists several -- e.g. \"here's what I completed this week\" becomes one "
                "add_task(stage=completed) proposal per item",
        "params": {"title": {"req": True}, "stage": {}, "department": {}, "priority": {},
                   "due_date": {}, "start_date": {}, "note": {}, "client_facing": {}},
        "gate": "admin",
    },
    "move_task": {
        "desc": "move_task(task*, stage*) - move a task to a stage "
                "(todo|in_progress|blocked|revision|completed); task = its id or exact title",
        "params": {"task": {"req": True}, "stage": {"req": True}},
        "gate": "admin",
    },
    "complete_task": {
        "desc": "complete_task(task*) - mark a task done (moves it to completed)",
        "params": {"task": {"req": True}},
        "gate": "admin",
    },
    "comment_task": {
        "desc": "comment_task(task*, body*) - add a team comment to a task's thread",
        "params": {"task": {"req": True}, "body": {"req": True}},
        "gate": "admin",
    },
    "add_calendar_event": {
        "desc": "add_calendar_event(date*, label*, kind?) - add a content-calendar event "
                "(kind paid|organic|due|milestone, default milestone)",
        "params": {"date": {"req": True}, "label": {"req": True},
                   "kind": {"choices": ("paid", "organic", "due", "milestone")}},
        "gate": "admin",
    },
    "add_intel": {
        "desc": "add_intel(section*, title*, body?, source?, link?, date?) - add a Market "
                "Intelligence entry (section business_research|media_buying)",
        "params": {"section": {"req": True, "choices": ("business_research", "media_buying")},
                   "title": {"req": True}, "body": {}, "source": {}, "link": {}, "date": {}},
        "gate": "admin",
    },
    "edit_intel": {
        "desc": "edit_intel(section*, entry*, title?, body?, source?, link?, date?) - edit a "
                "Market Intelligence entry; entry = its id or exact title",
        "params": {"section": {"req": True, "choices": ("business_research", "media_buying")},
                   "entry": {"req": True}, "title": {}, "body": {}, "source": {}, "link": {},
                   "date": {}},
        "gate": "admin",
    },
    "delete_intel": {
        "desc": "delete_intel(section*, entry*) - delete a Market Intelligence entry",
        "params": {"section": {"req": True, "choices": ("business_research", "media_buying")},
                   "entry": {"req": True}},
        "gate": "admin",
    },
    "add_company_section": {
        "desc": "add_company_section(heading*, body*) - add a story section (About/History/"
                "Mission/Positioning...) to the client's Company profile",
        "params": {"heading": {"req": True}, "body": {"req": True}},
        "gate": "admin",
    },
    "add_company_product": {
        "desc": "add_company_product(name*, summary?, price?, audience?, url?, status?) - add a "
                "product or service to the client's Company profile",
        "params": {"name": {"req": True}, "summary": {}, "price": {}, "audience": {}, "url": {},
                   "status": {}},
        "gate": "admin",
    },
    "set_company_facts": {
        "desc": "set_company_facts(one_liner?, industry?, founded?, hq?, website?, size?, "
                "customers?) - update the Company profile's at-a-glance facts (only the ones you "
                "pass change)",
        "params": {"one_liner": {}, "industry": {}, "founded": {}, "hq": {}, "website": {},
                   "size": {}, "customers": {}},
        "gate": "admin",
    },
    "log_communication": {
        "desc": "log_communication(channel*, title*, summary?, date?, people?, audience?) - add a "
                "card to the Communications timeline (channel email|upwork|slack|meeting|call|"
                "note; audience client|team, default client)",
        "params": {"channel": {"req": True,
                               "choices": ("email", "upwork", "slack", "meeting", "call", "note")},
                   "title": {"req": True}, "summary": {}, "date": {}, "people": {},
                   "audience": {"choices": ("client", "team")}},
        "gate": "admin",
    },
    "run_website_check": {
        "desc": "run_website_check() - run the Website Health check against the client's site now "
                "(super-admin approval required)",
        "params": {},
        "gate": "root",
    },
    "set_website_notes": {
        "desc": "set_website_notes(notes*) - replace the Website Health notes "
                "(super-admin approval required)",
        "params": {"notes": {"req": True}},
        "gate": "root",
    },
    "generate_report": {
        "desc": "generate_report(date?, title?) - draft a new client meeting deck from everything "
                "the workspace knows (date YYYY-MM-DD, default today)",
        "params": {"date": {}, "title": {}},
        "gate": "admin",
    },
    "edit_report": {
        "desc": "edit_report(report*, instruction*) - revise an existing deck per the "
                "instruction; report = its id, exact title, or date YYYY-MM-DD",
        "params": {"report": {"req": True}, "instruction": {"req": True}},
        "gate": "admin",
    },
    "rename_report": {
        "desc": "rename_report(report*, title?, date?) - retitle and/or redate a deck",
        "params": {"report": {"req": True}, "title": {}, "date": {}},
        "gate": "admin",
    },
    "delete_report": {
        "desc": "delete_report(report*) - remove a deck from the Reports tab",
        "params": {"report": {"req": True}},
        "gate": "admin",
    },
    "reindex": {
        "desc": "reindex() - rebuild the assistant's knowledge index now (also summarizes newly "
                "fetched Watcher videos)",
        "params": {},
        "gate": "admin",
    },
}


def catalog_text():
    """The action catalog the model is taught (one line each; * marks required params)."""
    return "\n".join("- %s" % meta["desc"] for _name, meta in sorted(_ACTIONS.items()))


def validate(proposal):
    """Check one raw model proposal against the registry.

    Returns (clean, error): `clean` is {action, params, note, label, gate} with unknown params
    dropped and choice params verified; `error` is "" when valid. Never raises."""
    if not isinstance(proposal, dict):
        return None, "not an action object"
    name = str(proposal.get("action") or "").strip()
    meta = _ACTIONS.get(name)
    if meta is None:
        return None, "unknown action '%s'" % name
    raw = proposal.get("params") if isinstance(proposal.get("params"), dict) else {}
    params = {}
    for key, spec in meta["params"].items():
        val = raw.get(key)
        if val is None or val == "":
            if spec.get("req"):
                return None, "%s: missing required param '%s'" % (name, key)
            continue
        val = val if isinstance(val, str) else json.dumps(val) if isinstance(val, (dict, list)) \
            else str(val)
        val = val.strip()
        choices = spec.get("choices")
        if choices and val not in choices:
            return None, "%s: '%s' must be one of %s" % (name, key, "|".join(choices))
        params[key] = val
    clean = {"action": name, "params": params,
             "note": str(proposal.get("note") or "").strip()[:300],
             "label": _label(name, params), "gate": meta["gate"]}
    return clean, ""


def _label(name, params):
    """The approval card's one-line description of what Approve will do."""
    p = params
    if name == "add_task":
        col = (p.get("stage") or "").strip()
        return "Add task \"%s\" to the %s column" % (p.get("title", ""), col or "todo")
    if name == "move_task":
        return "Move task \"%s\" to %s" % (p.get("task", ""), p.get("stage", ""))
    if name == "complete_task":
        return "Mark task \"%s\" completed" % p.get("task", "")
    if name == "comment_task":
        return "Comment on task \"%s\"" % p.get("task", "")
    if name == "add_calendar_event":
        return "Add calendar event \"%s\" on %s" % (p.get("label", ""), p.get("date", ""))
    if name == "add_intel":
        return "Add Market Intelligence entry \"%s\"" % p.get("title", "")
    if name == "edit_intel":
        return "Edit Market Intelligence entry \"%s\"" % p.get("entry", "")
    if name == "delete_intel":
        return "Delete Market Intelligence entry \"%s\"" % p.get("entry", "")
    if name == "add_company_section":
        return "Add \"%s\" to the Company profile" % p.get("heading", "")
    if name == "add_company_product":
        return "Add product \"%s\" to the Company profile" % p.get("name", "")
    if name == "set_company_facts":
        return "Update the Company profile facts (%s)" % (", ".join(sorted(p)) or "nothing")
    if name == "log_communication":
        return "Log %s \"%s\" in Communications" % (p.get("channel", "note"), p.get("title", ""))
    if name == "run_website_check":
        return "Run the Website Health check now"
    if name == "set_website_notes":
        return "Update the Website Health notes"
    if name == "generate_report":
        return "Draft a new client report%s" % ((" dated " + p["date"]) if p.get("date") else "")
    if name == "edit_report":
        return "Edit report \"%s\"" % p.get("report", "")
    if name == "rename_report":
        return "Rename report \"%s\"" % p.get("report", "")
    if name == "delete_report":
        return "Delete report \"%s\"" % p.get("report", "")
    if name == "reindex":
        return "Rebuild the assistant's knowledge index"
    return name


# --- target resolution (id first, then exact/contained title -- ambiguity is an error) ------------
def _resolve(items, ref, id_key="id", title_key="title"):
    ref = (ref or "").strip()
    if not ref:
        return None, "empty reference"
    for it in items:
        if it.get(id_key) == ref:
            return it, ""
    low = ref.lower()
    exact = [it for it in items if (it.get(title_key) or "").strip().lower() == low]
    if len(exact) == 1:
        return exact[0], ""
    part = [it for it in items if low in (it.get(title_key) or "").lower()]
    if len(part) == 1:
        return part[0], ""
    if len(part) > 1:
        return None, "'%s' matches %d items - use the exact id" % (ref, len(part))
    return None, "nothing matches '%s'" % ref


def _resolve_report(ws, ref):
    import workspace
    reports = workspace.reports_of(ws)
    hit, err = _resolve(reports, ref)
    if hit is None and err.startswith("nothing"):
        dated = [r for r in reports if (r.get("date") or "")[:10] == (ref or "")[:10]]
        if len(dated) == 1:
            return dated[0], ""
    return hit, err


# --- execution ------------------------------------------------------------------------------------
def execute(client, clean, ctx):
    """Run one VALIDATED, HUMAN-APPROVED action. Returns (ok, message).

    `ctx` carries what executors need beyond the workspace writers:
      actor        -- the approving human's display name (stamped on history/comments)
      is_root      -- whether they are THE super admin (root-gated actions re-check this)
      caller       -- the model seam (system, user) -> (text, err), for report generate/edit
      report_inputs-- lazy () -> report_ai.gather(...) inputs (loads archives + dash data)
      reindex      -- lazy () -> chunk count, the index rebuild
    Never raises: every failure comes back as (False, reason)."""
    import workspace
    name = clean.get("action")
    params = clean.get("params") or {}
    meta = _ACTIONS.get(name)
    if meta is None:
        return False, "unknown action"
    if meta["gate"] == "root" and not ctx.get("is_root"):
        return False, "only the super admin can approve this action"
    actor = ctx.get("actor") or "Assistant"
    try:
        ws = workspace.load_workspace(client)
        if ws is None:
            return False, "no workspace for this client"

        if name == "add_task":
            # `stage` is canonicalised (junk lands in todo, legacy keys land on a live column) so a
            # model paraphrase like "Completed" can never 500 or drop the card off the board.
            stage = workspace.canon_stage(
                (params.get("stage") or "").strip().lower().replace(" ", "_"))
            fields = {"title": params.get("title"), "department": params.get("department", ""),
                      "priority": params.get("priority") or "Medium",
                      "due_date": params.get("due_date", ""),
                      "start_date": params.get("start_date", ""),
                      "internal_notes": params.get("note", ""),
                      "stage": stage,
                      # Default TRUE: these proposals are made looking at the client Tasks board,
                      # and a card that silently lands on the hidden internal list reads as "the AI
                      # didn't add it". Only an explicit false keeps it internal.
                      "client_facing": (params.get("client_facing", "").lower()
                                        not in ("0", "false", "no")),
                      "reporter": "agora", "reporter_name": actor}
            task = workspace.add_task(client, fields, actor=actor)
            return True, "Added task \"%s\" to %s (id %s)." % (task["title"], task["stage"],
                                                               task["id"])

        if name in ("move_task", "complete_task", "comment_task"):
            task, err = _resolve(ws.get("tasks") or [], params.get("task"))
            if task is None:
                return False, "task not found: %s" % err
            if name == "comment_task":
                workspace.add_task_comment(client, task["id"], "agora", actor,
                                           params.get("body", ""))
                return True, "Commented on \"%s\"." % task.get("title")
            stage = "completed" if name == "complete_task" else params.get("stage", "")
            try:
                moved = workspace.move_task_stage(client, task["id"], stage, actor=actor)
            except KeyError as e:
                return False, str(e).strip("'\"")
            return True, "Moved \"%s\" to %s." % (moved.get("title"), moved.get("stage"))

        if name == "add_calendar_event":
            workspace.add_calendar_event(client, params.get("date"), params.get("label"),
                                         params.get("kind") or "milestone")
            return True, "Added \"%s\" to the calendar on %s." % (params.get("label"),
                                                                  params.get("date"))

        if name == "add_company_section":
            item = workspace.add_company_item(
                client, "sections",
                {"heading": params.get("heading"), "body": params.get("body")})
            return True, "Added \"%s\" to the Company profile." % item.get("heading")

        if name == "add_company_product":
            fields = {k: params.get(k, "") for k in
                      ("name", "summary", "price", "audience", "url", "status")}
            item = workspace.add_company_item(client, "products", fields)
            return True, "Added product \"%s\" to the Company profile." % item.get("name")

        if name == "set_company_facts":
            # Only the facts the model actually proposed are passed through, so an approval never
            # silently blanks a field the team filled in by hand.
            given = {k: v for k, v in params.items() if k in workspace.COMPANY_PROFILE_FIELDS}
            if not given:
                return False, "no company facts were given"
            workspace.set_company_profile(client, given)
            return True, "Updated the Company profile facts: %s." % ", ".join(sorted(given))

        if name == "add_intel":
            entry = {k: params.get(k, "") for k in ("title", "body", "source", "link", "date")}
            workspace.add_intel_entry(client, params["section"], entry)
            return True, "Added \"%s\" to Market Intelligence." % params.get("title")

        if name in ("edit_intel", "delete_intel"):
            section = params["section"]
            entries = (ws.get("intel") or {}).get(section) or []
            hit, err = _resolve(entries, params.get("entry"))
            if hit is None:
                return False, "intel entry not found: %s" % err
            if name == "delete_intel":
                workspace.delete_intel_entry(client, section, hit["id"])
                return True, "Deleted \"%s\"." % (hit.get("title") or hit["id"])
            fields = {k: params[k] for k in ("title", "body", "source", "link", "date")
                      if k in params}
            if not fields:
                return False, "nothing to change - give at least one field"
            workspace.update_intel_entry(client, section, hit["id"], fields)
            return True, "Updated \"%s\"." % (fields.get("title") or hit.get("title") or hit["id"])

        if name == "log_communication":
            workspace.add_communication(
                client, params.get("channel"), params.get("title"), params.get("summary", ""),
                date=params.get("date") or None, people=params.get("people", ""),
                audience=params.get("audience") or "client")
            return True, "Logged \"%s\" in Communications." % params.get("title")

        if name == "run_website_check":
            import atrium_health
            url = ((ws.get("website_health") or {}).get("url") or "").strip()
            if not url:
                return False, "no website URL is set on the Website Health tab"
            result = atrium_health.check_website(url)
            workspace.save_website_check(client, result)
            status = "reachable" if result.get("ok") else (result.get("error") or "unreachable")
            return True, "Checked %s: %s." % (url, status)

        if name == "set_website_notes":
            workspace.set_website_notes(client, params.get("notes", ""))
            return True, "Website Health notes updated."

        if name == "generate_report":
            import report_ai
            inputs_fn = ctx.get("report_inputs")
            if inputs_fn is None:
                return False, "report generation is not wired"
            when = params.get("date") or workspace.now_iso()[:10]
            title = params.get("title") or "Performance Review"
            payload, gen_err = report_ai.generate(ws.get("display_name") or client, when,
                                                  inputs_fn(), ctx.get("caller"))
            entry = workspace.add_report(client, title, when, payload=payload,
                                         origin="assistant")
            html_doc = report_ai.render_html(ws.get("display_name") or client, payload,
                                             when, title=title, brand=report_ai.brand_kit(ws))
            workspace.write_report_html(client, entry["id"], html_doc)
            note = " (draft without AI: %s)" % gen_err if gen_err else ""
            return True, "Report \"%s\" (%s) is on the Reports tab%s." % (title, when, note)

        if name in ("edit_report", "rename_report", "delete_report"):
            import report_ai
            hit, err = _resolve_report(ws, params.get("report"))
            if hit is None:
                return False, "report not found: %s" % err
            if name == "delete_report":
                workspace.delete_report(client, hit["id"])
                return True, "Deleted report \"%s\"." % hit.get("title")
            if name == "rename_report":
                fields = {k: params[k] for k in ("title", "date") if params.get(k)}
                if not fields:
                    return False, "give a new title and/or date"
                entry = workspace.update_report(client, hit["id"], fields)
            else:
                payload, rev_err = report_ai.revise(hit.get("payload") or {},
                                                    params.get("instruction"), ctx.get("caller"))
                if rev_err:
                    return False, "could not edit the report: %s" % rev_err
                entry = workspace.update_report(client, hit["id"], {"payload": payload})
            html_doc = report_ai.render_html(ws.get("display_name") or client,
                                             entry.get("payload") or {}, entry.get("date") or "",
                                             title=entry.get("title") or "",
                                             brand=report_ai.brand_kit(ws))
            workspace.write_report_html(client, entry["id"], html_doc)
            verb = "Renamed" if name == "rename_report" else "Updated"
            return True, "%s report \"%s\"." % (verb, entry.get("title"))

        if name == "reindex":
            fn = ctx.get("reindex")
            if fn is None:
                return False, "reindex is not wired"
            chunks = fn()
            return True, "Rebuilt the knowledge index (%s chunks)." % chunks

        return False, "action '%s' has no executor" % name
    except Exception as e:  # an executor crash must surface as a friendly failure, never a 500
        return False, str(e)[:300]
