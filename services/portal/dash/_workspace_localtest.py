"""Local smoke test for the Atrium data layer -- runs entirely off-cloud, never touches prod.

It points the workspace store at a throwaway temp directory (WORKSPACE_LOCAL_DIR), seeds the
Riverdance demo there, then exercises every client-facing mutation and re-reads from disk to prove
persistence. No GCS, no ADC, and no `google-cloud-storage` package required.

    python _workspace_localtest.py        # prints PASS / FAIL and exits 0 / 1
"""

import os
import shutil
import sys
import tempfile

# Point the store at a temp dir BEFORE importing the module under test.
_TMP = tempfile.mkdtemp(prefix="atrium_localtest_")
os.environ["WORKSPACE_LOCAL_DIR"] = _TMP

import workspace            # noqa: E402  (must follow the env setup above)
import seed_workspace       # noqa: E402

CLIENT = "riverdance"
USER = "owner@riverdanceresort.com"


def _check(label, condition):
    if not condition:
        raise AssertionError(label)
    print("  [OK] %s" % label)


def run():
    print("[localtest] WORKSPACE_LOCAL_DIR = %s" % _TMP)

    # 1. Seed writes the object; re-seeding refuses to clobber.
    rc = seed_workspace.seed(register_client=False)
    _check("first seed returns 0", rc == 0)
    _check("workspace object exists after seed", workspace.workspace_exists(CLIENT))
    _check("re-seed refuses to clobber (returns 1)", seed_workspace.seed(register_client=False) == 1)

    # 2. The seeded shape matches the spec.
    ws = workspace.load_workspace(CLIENT)
    _check("display_name seeded", ws["display_name"] == "Riverdance RV Resort")
    _check("six KPI metrics", len(ws["metrics"]) == 6)
    _check("two campaigns (paid + organic)",
           [c["channel"] for c in ws["campaigns"]] == ["paid", "organic"])
    _check("14-point leads series", len(ws["series"]) == 14)
    _check("three conversations", len(ws["conversations"]) == 3)
    _check("intel seeded with both sections",
           len(ws["intel"]["business_research"]) == 3 and len(ws["intel"]["media_buying"]) == 2)

    # 3. Approve an awaiting piece with a note.
    item = workspace.decide_content(CLIENT, "RVR-016", "approved", note="Looks great, ship it.")
    _check("decide_content set status approved", item["status"] == "approved")
    _check("decide_content stamped decided_at", item["decided_at"].endswith("Z"))
    _check("decide_content saved the note", item["client_note"] == "Looks great, ship it.")

    # 4. Standalone note on another piece.
    workspace.set_content_note(CLIENT, "RVR-017", "Can we add a guest quote?")
    _camp, rvr017 = workspace._find_content(workspace.load_workspace(CLIENT), "RVR-017")
    _check("set_content_note persisted", rvr017["client_note"] == "Can we add a guest quote?")

    # 5. Client sends a message -> thread goes awaiting_reply.
    conv, msg = workspace.add_message(CLIENT, "cv_1", "client", "Sarah",
                                      "Thursday works great, thank you!", set_status="awaiting_reply")
    _check("message appended", conv["messages"][-1]["body"].startswith("Thursday works"))
    _check("message sender recorded", msg["sender"] == "client")
    _check("conversation status updated", conv["status"] == "awaiting_reply")

    # 6. Notification prefs: defaults for an unknown user, merge on save.
    defaults = workspace.get_notify(ws, "nobody@example.com")
    _check("default notify: master on", defaults["master"] is True)
    _check("default notify: status off", defaults["status"] is False)
    saved = workspace.set_notify(CLIENT, USER, {"status": True, "frequency": "daily"})
    _check("set_notify merged status", saved["status"] is True)
    _check("set_notify merged frequency", saved["frequency"] == "daily")
    _check("set_notify kept defaults", saved["replies"] is True)

    # 7. Activity prepends, most-recent first.
    workspace.add_activity(CLIENT, "check", "You approved RVR-016.", "Today, 10:00 AM")
    _check("activity prepended",
           workspace.load_workspace(CLIENT)["activity"][0]["text"] == "You approved RVR-016.")

    # 8a. Threaded comments on a content piece (client + team).
    item, comment = workspace.add_content_comment(CLIENT, "RVR-016", "client", "Sarah", "Can we brighten it?")
    _check("comment appended", comment["body"] == "Can we brighten it?")
    workspace.add_content_comment(CLIENT, "RVR-016", "agora", "Maya", "On it!")
    _camp, rvr016c = workspace._find_content(workspace.load_workspace(CLIENT), "RVR-016")
    _check("two comments persisted", len(rvr016c["comments"]) == 2 and rvr016c["comments"][-1]["sender"] == "agora")

    # 8b. Strategy doc attach + campaign update with new fields.
    workspace.set_strategy_doc(CLIENT, "c_paid_1", "https://docs.google.com/document/d/ABC123abc123abc123abc/edit")
    _check("strategy_doc saved",
           workspace._find_campaign(workspace.load_workspace(CLIENT), "c_paid_1")["strategy_doc"].endswith("/edit"))

    # 8c. Creative image bytes round-trip + pointer set/clear.
    obj = workspace.write_creative(CLIENT, "RVR-016", b"\x89PNG\r\n\x1a\nFAKE", content_type="image/png")
    _check("creative object name under prefix", obj == "workspace/creatives/riverdance/RVR-016")
    _check("creative bytes round-trip", workspace.read_creative_bytes(CLIENT, "RVR-016") == b"\x89PNG\r\n\x1a\nFAKE")
    workspace.set_content_image(CLIENT, "RVR-016", obj, "image/png")
    _camp, rvr016i = workspace._find_content(workspace.load_workspace(CLIENT), "RVR-016")
    _check("image pointer set", rvr016i["image_object"] == obj and rvr016i["image_mime"] == "image/png")
    workspace.clear_content_image(CLIENT, "RVR-016")
    workspace.delete_creative(CLIENT, "RVR-016")
    _camp, rvr016n = workspace._find_content(workspace.load_workspace(CLIENT), "RVR-016")
    _check("image pointer cleared", "image_object" not in rvr016n)
    _check("creative bytes deleted", workspace.read_creative_bytes(CLIENT, "RVR-016") is None)

    # 8d. Delete content + delete campaign.
    n_org = len(workspace._find_campaign(workspace.load_workspace(CLIENT), "c_org_1")["content"])
    workspace.delete_content(CLIENT, "RVR-017")
    _check("content removed",
           len(workspace._find_campaign(workspace.load_workspace(CLIENT), "c_org_1")["content"]) == n_org - 1)
    workspace.delete_campaign(CLIENT, "c_org_1")
    _check("campaign removed",
           workspace._find_campaign(workspace.load_workspace(CLIENT), "c_org_1") is None)

    # 8e. Calendar add + delete.
    before_cal = len(workspace.load_workspace(CLIENT).get("calendar", []))
    workspace.add_calendar_event(CLIENT, "2026-07-04", "Independence Day promo", "milestone")
    _check("calendar event added", len(workspace.load_workspace(CLIENT)["calendar"]) == before_cal + 1)
    workspace.delete_calendar_event(CLIENT, before_cal)
    _check("calendar event deleted", len(workspace.load_workspace(CLIENT)["calendar"]) == before_cal)

    # 8f. Content with a date mirrors onto the Content Calendar as a linked event; editing the date
    #     re-syncs it (paid -> 'paid'/leadgen); clearing the date removes it; deleting the piece too.
    base_cal = len(workspace.load_workspace(CLIENT).get("calendar", []))
    dated = workspace.add_content(CLIENT, "c_paid_1",
                                  {"ref": "Dated teaser", "date": "2026-08-01"})
    linked = [e for e in workspace.load_workspace(CLIENT)["calendar"]
              if e.get("content_id") == dated["id"]]
    _check("dated content mirrored to calendar", len(linked) == 1)
    _check("linked event is paid/leadgen",
           linked and linked[0]["kind"] == "paid" and linked[0]["tab"] == "leadgen")
    _check("linked event carries the date + label",
           linked and linked[0]["date"] == "2026-08-01" and linked[0]["label"] == "Dated teaser")
    workspace.update_content(CLIENT, dated["id"], {"date": "2026-08-15", "ref": "Renamed teaser"})
    relinked = [e for e in workspace.load_workspace(CLIENT)["calendar"]
                if e.get("content_id") == dated["id"]]
    _check("edit re-synced the linked event (no duplicate)", len(relinked) == 1)
    _check("edit overwrote date + label on the event",
           relinked and relinked[0]["date"] == "2026-08-15" and relinked[0]["label"] == "Renamed teaser")
    workspace.update_content(CLIENT, dated["id"], {"date": ""})
    _check("clearing the date removes the linked event",
           not [e for e in workspace.load_workspace(CLIENT)["calendar"]
                if e.get("content_id") == dated["id"]])
    # A piece WITHOUT a date never touches the calendar.
    undated = workspace.add_content(CLIENT, "c_paid_1", {"ref": "No date"})
    _check("undated content adds no calendar event",
           len(workspace.load_workspace(CLIENT)["calendar"]) == base_cal)
    # Re-date it, then delete the piece: the linked event goes with it.
    workspace.update_content(CLIENT, undated["id"], {"date": "2026-09-09"})
    _check("re-dating creates the linked event",
           len(workspace.load_workspace(CLIENT)["calendar"]) == base_cal + 1)
    workspace.delete_content(CLIENT, undated["id"])
    _check("deleting the piece removes its linked event",
           len(workspace.load_workspace(CLIENT)["calendar"]) == base_cal)

    # 8g. Market Intelligence: add (newest-first) + edit + delete an entry, and reject a bad section.
    before_mb = len(workspace.load_workspace(CLIENT)["intel"]["media_buying"])
    entry = workspace.add_intel_entry(CLIENT, "media_buying",
                                      {"heading": "Meta Updates", "title": "Advantage+ broadens",
                                       "body": "More automated placements.", "source": "Meta"})
    mb = workspace.load_workspace(CLIENT)["intel"]["media_buying"]
    _check("intel entry added newest-first",
           len(mb) == before_mb + 1 and mb[0]["id"] == entry["id"] and mb[0]["title"] == "Advantage+ broadens")
    workspace.update_intel_entry(CLIENT, "media_buying", entry["id"], {"body": "Edited body."})
    mb = workspace.load_workspace(CLIENT)["intel"]["media_buying"]
    _check("intel entry edited in place", mb[0]["body"] == "Edited body.")
    workspace.delete_intel_entry(CLIENT, "media_buying", entry["id"])
    _check("intel entry deleted",
           len(workspace.load_workspace(CLIENT)["intel"]["media_buying"]) == before_mb)
    try:
        workspace.add_intel_entry(CLIENT, "not_a_section", {"body": "x"})
        _check("unknown intel section raises", False)
    except KeyError:
        _check("unknown intel section raises", True)

    # 8h. Task tracker: the full add / edit / move / sub-task / comment / delete round-trip.
    task = workspace.add_task(CLIENT, {
        "title": "Park & Porch — lead-gen funnel", "department": "acquisition",
        "lead_id": "zhen@100.digital", "support_ids": ["ehjay@agoradatadriven.com", "zhen@100.digital"],
        "priority": "High", "labels": ["Paid Media"], "campaign": "Park & Porch | Leads",
        "content_type": "Funnel", "due_date": "2026-07-20", "client_facing": True,
        "client_note": "Funnel is live.", "deliverable_url": "https://drive.google.com/x",
        "internal_notes": "Watch CPL.",
    }, actor="info@agoradatadriven.com")
    _check("task created with id + default stage",
           task["id"].startswith("tk_") and task["stage"] == "todo")
    _check("reporter defaults to agora (console adds carry no reporter)",
           task["reporter"] == "agora" and task["reporter_name"] == "")
    _rep = workspace.add_task(CLIENT, {"title": "Client ask", "reporter": "client",
                                       "reporter_name": "Owner"})
    _check("reporter + name stored when supplied; junk reporter falls back to agora",
           _rep["reporter"] == "client" and _rep["reporter_name"] == "Owner"
           and workspace.add_task(CLIENT, {"title": "x", "reporter": "evil"})["reporter"] == "agora")
    _check("task lead never duplicated into support", task["support_ids"] == ["ehjay@agoradatadriven.com"])
    _check("task history stamped created", task["history"][0]["field"] == "created")
    try:
        workspace.add_task(CLIENT, {"title": "bad", "stage": "not_a_stage"})
        _check("unknown task stage raises", False)
    except KeyError:
        _check("unknown task stage raises", True)

    workspace.update_task(CLIENT, task["id"],
                          {"priority": "Urgent", "support_ids": ["zhen@100.digital", "ian@100.digital"]})
    t2 = workspace._find_task(workspace.load_workspace(CLIENT), task["id"])
    _check("task edit patched priority", t2["priority"] == "Urgent")
    _check("task edit re-enforced lead-not-in-support", t2["support_ids"] == ["ian@100.digital"])

    # Two-level breakdown: a sub-task with no main task grows a group named after the content
    # type; explicit main tasks carry their own owner and their own subs.
    _task, sub1 = workspace.add_subtask(CLIENT, task["id"], "Propose funnel", "zhen@100.digital")
    _task, sub2 = workspace.add_subtask(CLIENT, task["id"], "Create info pack")
    _task, mt_qa = workspace.add_maintask(CLIENT, task["id"], "QA", "ian@100.digital")
    _task, sub3 = workspace.add_subtask(CLIENT, task["id"], "QA events fire",
                                        "ian@100.digital", maintask_id=mt_qa["id"])
    workspace.set_subtask_done(CLIENT, task["id"], sub1["id"], True)
    workspace.set_subtask_owner(CLIENT, task["id"], sub2["id"], "ehjay@agoradatadriven.com")
    t3 = workspace._find_task(workspace.load_workspace(CLIENT), task["id"])
    mains = t3["maintasks"]
    _check("auto main task named after the content type",
           len(mains) == 2 and mains[0]["text"] == "Funnel" and mains[1]["text"] == "QA")
    _check("main task carries its own owner", mains[1]["assignee_id"] == "ian@100.digital")
    _check("sub-tasks persisted with done + owner across groups",
           mains[0]["subs"][0]["done"] is True
           and mains[0]["subs"][1]["assignee_id"] == "ehjay@agoradatadriven.com"
           and mains[1]["subs"][0]["id"] == sub3["id"])
    _check("task_subtasks flattens every group",
           [s["id"] for s in workspace.task_subtasks(t3)] == [sub1["id"], sub2["id"], sub3["id"]])
    workspace.set_maintask_owner(CLIENT, task["id"], mt_qa["id"], "ehjay@agoradatadriven.com")
    t3b = workspace._find_task(workspace.load_workspace(CLIENT), task["id"])
    _check("main-task owner reassigned", t3b["maintasks"][1]["assignee_id"] == "ehjay@agoradatadriven.com")

    # Legacy flat subtasks migrate in place the first time the task is touched.
    legacy = {"id": "tk_legacy", "title": "Old shape", "stage": "in_progress", "lead_id": "zhen@100.digital",
              "content_type": "Report", "subtasks": [{"id": "st_a", "text": "Old sub", "done": True,
                                                      "assignee_id": ""}]}
    workspace.normalize_task(legacy)
    _check("legacy flat subtasks migrate into one main task",
           "subtasks" not in legacy and len(legacy["maintasks"]) == 1
           and legacy["maintasks"][0]["text"] == "Report"
           and legacy["maintasks"][0]["assignee_id"] == "zhen@100.digital"
           and legacy["maintasks"][0]["subs"][0]["id"] == "st_a")
    _check("normalize is idempotent",
           workspace.normalize_task(legacy)["maintasks"][0]["subs"][0]["text"] == "Old sub")

    # Every service has a start date: created-without-one defaults to today; legacy tasks
    # backfill theirs from the creation day.
    dated = workspace.add_task(CLIENT, {"title": "No start given"})
    _check("new task defaults start_date to its creation day",
           dated["start_date"] == dated["created_at"][:10])
    old = workspace.normalize_task({"id": "tk_old", "title": "Pre-start-date task",
                                    "created_at": "2026-07-01T09:00:00Z"})
    _check("legacy task backfills start_date from created_at", old["start_date"] == "2026-07-01")

    # On hold <-> ongoing: a plain boolean + internal reason, with a hold/resume history entry.
    workspace.set_task_hold(CLIENT, task["id"], True, "Client asked to pause", actor="info@agoradatadriven.com")
    th = workspace._find_task(workspace.load_workspace(CLIENT), task["id"])
    _check("task put on hold with reason",
           th["on_hold"] is True and th["hold_reason"] == "Client asked to pause"
           and th["history"][-1]["field"] == "hold" and th["history"][-1]["new"] == "on hold")
    workspace.set_task_hold(CLIENT, task["id"], False, actor="info@agoradatadriven.com")
    th2 = workspace._find_task(workspace.load_workspace(CLIENT), task["id"])
    _check("task resumed clears the reason",
           th2["on_hold"] is False and th2["hold_reason"] == "" and th2["history"][-1]["new"] == "resumed")

    # A client change request is RECORDED and surfaced -- but it no longer vetoes a stage move
    # (2026-07-28: the board drags freely; see move_task_stage).
    _task, chg = workspace.add_task_comment(CLIENT, task["id"], "client", "Daniela",
                                            "Please swap the hero image.", kind="changes")
    _check("change request recorded unresolved", chg["resolved"] is False)
    _check("open-changes flag derives from the thread",
           len(workspace.task_open_changes(t3)) == 0 and
           len(workspace.task_open_changes(workspace._find_task(workspace.load_workspace(CLIENT), task["id"]))) == 1)
    workspace.move_task_stage(CLIENT, task["id"], "completed")
    _check("close allowed with an open sub-task + change request (no guard)",
           workspace._find_task(workspace.load_workspace(CLIENT), task["id"])["stage"] == "completed"
           and len(workspace.task_open_changes(
               workspace._find_task(workspace.load_workspace(CLIENT), task["id"]))) == 1)
    workspace.move_task_stage(CLIENT, task["id"], "in_progress", actor="info@agoradatadriven.com")
    t4 = workspace._find_task(workspace.load_workspace(CLIENT), task["id"])
    _check("stage move recorded in history",
           t4["stage"] == "in_progress" and t4["history"][-1]["old"] == "completed"
           and t4["history"][-1]["new"] == "in_progress")
    workspace.resolve_task_comment(CLIENT, task["id"], chg["id"])
    workspace.set_subtask_done(CLIENT, task["id"], sub2["id"], True)
    workspace.set_subtask_done(CLIENT, task["id"], sub3["id"], True)
    workspace.move_task_stage(CLIENT, task["id"], "closed")
    _check("the legacy 'closed' stage key still lands on completed",
           workspace._find_task(workspace.load_workspace(CLIENT), task["id"])["stage"] == "completed")

    # set_task_maintasks: the whole breakdown in ONE write, for a caller that edits it as an array
    # (Sentinel's drawer). Two rules make that safe to accept from another system: an id this task
    # doesn't already hold is re-minted here, and a sub-task that keeps its id keeps the internal
    # `dod` the other side can neither see nor send. On its own task, so nothing else is disturbed.
    bd_task = workspace.add_task(CLIENT, {"title": "Breakdown-by-array", "stage": "todo"})
    _bd, bd_main = workspace.add_maintask(CLIENT, bd_task["id"], "Build", "zhen@100.digital")
    _bd, bd_sub = workspace.add_subtask(CLIENT, bd_task["id"], "Draft it",
                                        maintask_id=bd_main["id"], dod="Signed off by the lead")
    t_bd = workspace.set_task_maintasks(CLIENT, bd_task["id"], [
        {"id": bd_main["id"], "text": "Build", "assignee_id": "ian@100.digital",
         "subs": [{"id": bd_sub["id"], "text": "Draft it v2", "done": True},
                  {"id": "st_new_from_sentinel", "text": "Brand new step"},
                  {"id": "", "text": "   "}]},
        {"id": "", "text": "", "subs": []},
    ], actor="leo@agora.ph")
    _check("set_task_maintasks replaces the whole breakdown in one write",
           [m["text"] for m in t_bd["maintasks"]] == ["Build", "Untitled"]
           and t_bd["maintasks"][0]["assignee_id"] == "ian@100.digital")
    _check("a sub-task that keeps its id keeps its dod, and takes the new text/done",
           t_bd["maintasks"][0]["subs"][0]["id"] == bd_sub["id"]
           and t_bd["maintasks"][0]["subs"][0]["dod"] == "Signed off by the lead"
           and t_bd["maintasks"][0]["subs"][0]["text"] == "Draft it v2"
           and t_bd["maintasks"][0]["subs"][0]["done"] is True)
    _check("a foreign placeholder id is re-minted, and a blank step is dropped",
           len(t_bd["maintasks"][0]["subs"]) == 2
           and t_bd["maintasks"][0]["subs"][1]["id"] != "st_new_from_sentinel"
           and t_bd["maintasks"][0]["subs"][1]["id"].startswith("st_"))
    _check("the breakdown edit is stamped in history",
           t_bd["history"][-1]["field"] == "breakdown"
           and t_bd["history"][-1]["actor"] == "leo@agora.ph")
    workspace.delete_task(CLIENT, bd_task["id"])

    workspace.delete_subtask(CLIENT, task["id"], sub2["id"])
    _check("sub-task deleted",
           len(workspace.task_subtasks(
               workspace._find_task(workspace.load_workspace(CLIENT), task["id"]))) == 2)
    workspace.delete_maintask(CLIENT, task["id"], mt_qa["id"])
    t5 = workspace._find_task(workspace.load_workspace(CLIENT), task["id"])
    _check("main task deleted with its sub-tasks",
           len(t5["maintasks"]) == 1 and len(workspace.task_subtasks(t5)) == 1)
    removed = workspace.delete_task(CLIENT, task["id"])
    _check("delete_task returns the payload for the Trash", removed["id"] == task["id"])
    _check("task gone after delete",
           workspace._find_task(workspace.load_workspace(CLIENT), task["id"]) is None)
    workspace.insert_task(CLIENT, removed)
    workspace.insert_task(CLIENT, removed)   # double-restore must not duplicate
    _check("insert_task restores once (idempotent)",
           len([t for t in workspace.load_workspace(CLIENT)["tasks"] if t["id"] == task["id"]]) == 1)

    # 8b. Communications: the unified multi-channel timeline (+ audience + legacy migration).
    m = workspace.add_communication(CLIENT, "meeting", "Kickoff call", "Walked the funnel.",
                                    people="Daniela, Mike", audience="client")
    s = workspace.add_communication(CLIENT, "slack", "Internal: reallocate spend",
                                    "Pull 15% off cold prospecting.", audience="team")
    _check("add_communication cleans channel + audience",
           m["channel"] == "meeting" and m["audience"] == "client"
           and s["channel"] == "slack" and s["audience"] == "team")
    _check("an unknown channel falls back to 'note'",
           workspace.add_communication(CLIENT, "carrier-pigeon", "x", "y")["channel"] == "note")
    items = workspace.communications_list(workspace.load_workspace(CLIENT))
    _check("communications_list returns the entries newest-first-ish (contains both)",
           any(i["id"] == m["id"] for i in items) and any(i["id"] == s["id"] for i in items))
    workspace.update_communication(CLIENT, m["id"], {"audience": "team", "title": "Kickoff (edited)"})
    edited = next(i for i in workspace.communications_list(workspace.load_workspace(CLIENT))
                  if i["id"] == m["id"])
    _check("update_communication edits channel-safe fields", edited["audience"] == "team"
           and edited["title"] == "Kickoff (edited)")
    workspace.delete_communication(CLIENT, s["id"])
    _check("delete_communication removes by id",
           not any(i["id"] == s["id"]
                   for i in workspace.communications_list(workspace.load_workspace(CLIENT))))
    # upsert_email_summary mirrors an email thread recap (channel email, audience client, thread_key).
    workspace.upsert_email_summary(CLIENT, "mail_abc", "Re: booking", "Client recap here.")
    mir = next(i for i in workspace.communications_list(workspace.load_workspace(CLIENT))
               if i["id"] == "mail_abc")
    _check("upsert_email_summary mirrors as a client email card with a thread_key",
           mir["channel"] == "email" and mir["audience"] == "client"
           and mir["origin"] == "mail" and mir["thread_key"] == "abc")
    # Legacy split lists migrate in place the first time communications is touched.
    legacy = {"display_name": "Legacy Co",
              "email_summaries": [{"id": "em1", "subject": "Old email", "summary": "E", "date": "2026-06-01"}],
              "meeting_summaries": [{"id": "mt1", "title": "Old meeting", "summary": "M",
                                     "attendees": "A, B", "date": "2026-06-02"}]}
    workspace.save_workspace("legacyco", legacy)
    workspace.add_communication("legacyco", "note", "trigger", "migration")  # touches the list
    lw = workspace.load_workspace("legacyco")
    lc = {i["id"]: i for i in lw.get("communications", [])}
    _check("legacy email_summaries migrated to channel email/audience client",
           lc.get("em1", {}).get("channel") == "email" and lc["em1"]["title"] == "Old email"
           and lc["em1"]["audience"] == "client")
    _check("legacy meeting_summaries migrated to channel meeting (attendees -> people)",
           lc.get("mt1", {}).get("channel") == "meeting" and lc["mt1"]["people"] == "A, B")
    _check("legacy split lists are removed after migration",
           "email_summaries" not in lw and "meeting_summaries" not in lw)

    # 9. Everything survived a reload from disk.
    reloaded = workspace.load_workspace(CLIENT)
    _camp, rvr016 = workspace._find_content(reloaded, "RVR-016")
    _check("approval persisted across reload", rvr016["status"] == "approved")
    _check("notify persisted across reload", reloaded["notify"][USER]["status"] is True)
    _check("comments persisted across reload", len(rvr016["comments"]) == 2)

    # 10. Service templates seed the whole work breakdown, and add_task stores it.
    import service_templates
    _check("acquisition has exactly one service type",
           service_templates.services_for("acquisition") == [("google_meta_campaign", "Google / Meta Campaign")])
    mts = service_templates.build_maintasks("google_meta_campaign", {}, [("video", "3"), ("static", "5")])
    _check("google/meta campaign yields build + launch + the 2 ad-production groups", len(mts) == 4)
    _check("ad-production groups appended after the automatic ones",
           [m["text"] for m in mts][2:] == ["Video ad production", "Static ad production"])
    vsteps = [s["text"] for s in mts[2]["subs"] if "draft edit" in s["text"]]
    _check("qty=3 expands the per-video step into 3", len(vsteps) == 3)
    _check("every seeded sub-task carries a 'done when' (dod)",
           all(s.get("dod") for m in mts for s in m["subs"]))
    esteps = service_templates.build_maintasks("email_automation", {"qty": "4"})
    eprod = [m for m in esteps if m["text"] == "Email production"][0]
    _check("qty param drives a lifecycle template (4 emails -> 8 per-unit steps)",
           len([s for s in eprod["subs"] if s["text"].startswith("Email ")]) == 8)
    _check("an unknown service key builds nothing", service_templates.build_maintasks("nope") == [])

    seeded = workspace.add_task(CLIENT, {"title": "Park & Porch | Leads", "department": "acquisition",
                                         "content_type": "Campaign", "maintasks": mts})
    _check("add_task stores a pre-built breakdown", len(seeded["maintasks"]) == 4)
    _check("seeded sub-tasks flatten for the close-guard", len(workspace.task_subtasks(seeded)) == len(
           [s for m in mts for s in m["subs"]]))
    _t, sub = workspace.add_subtask(CLIENT, seeded["id"], "Extra QA pass", dod="Signed off by the lead")
    _check("add_subtask persists a dod", sub.get("dod") == "Signed off by the lead")
    _t, edited = workspace.edit_subtask(CLIENT, seeded["id"], sub["id"], text="Final QA pass", dod="Lead + client sign-off")
    _check("edit_subtask renames the step", edited["text"] == "Final QA pass")
    _check("edit_subtask edits the done-when", edited["dod"] == "Lead + client sign-off")
    _t, edited = workspace.edit_subtask(CLIENT, seeded["id"], sub["id"], text="   ")
    _check("edit_subtask keeps text on a blank rename", edited["text"] == "Final QA pass")
    _t, edited = workspace.edit_subtask(CLIENT, seeded["id"], sub["id"], dod="")
    _check("edit_subtask can clear the done-when", edited["dod"] == "" and edited["text"] == "Final QA pass")

    # 11. Stage moves are UNGUARDED (2026-07-28): a service with no sub-tasks at all completes too.
    # This used to raise; the board drags freely now and the blockers are only surfaced, not enforced.
    empty = workspace.add_task(CLIENT, {"title": "Empty custom service", "department": "development"})
    workspace.move_task_stage(CLIENT, empty["id"], "completed")
    _check("an empty (no sub-task) service can be completed",
           workspace._find_task(workspace.load_workspace(CLIENT), empty["id"])["stage"] == "completed")
    workspace.move_task_stage(CLIENT, empty["id"], "revision")
    _check("an empty service can still move to Revision Needed",
           workspace._find_task(workspace.load_workspace(CLIENT), empty["id"])["stage"] == "revision")
    # The 2026-07-29 stage trim: For Review / Waiting for Client are retired keys that both meant
    # "blocked on someone" -- a move under either key (e.g. a stale Sentinel bridge write) lands
    # the task on Blocked instead of erroring or vanishing off the board.
    workspace.move_task_stage(CLIENT, empty["id"], "for_review")
    _check("the retired 'for_review' stage key lands on blocked",
           workspace._find_task(workspace.load_workspace(CLIENT), empty["id"])["stage"] == "blocked")
    workspace.move_task_stage(CLIENT, empty["id"], "waiting_client")
    _check("the retired 'waiting_client' stage key lands on blocked",
           workspace._find_task(workspace.load_workspace(CLIENT), empty["id"])["stage"] == "blocked")

    # 12. The Company profile (2026-07-29): the client's own identity -- facts, brand guide, an
    # ORDERED story, an ORDERED catalogue. The two rules that matter are (a) a patch only writes
    # the fields it was given, so a partial form post can never blank the rest, and (b) the lists
    # are hand-ordered, so add APPENDS and move_company_item is a first-class writer.
    blank = workspace.company_profile(workspace.load_workspace(CLIENT))
    _check("a workspace with no company key still reads a fully-shaped profile",
           set(blank) == {"profile", "brand", "sections", "products", "content_gaps"}
           and blank["profile"]["one_liner"] == "" and blank["sections"] == []
           and blank["content_gaps"]["items"] == [])
    _check("company_is_empty is true before anything is written",
           workspace.company_is_empty(workspace.load_workspace(CLIENT)))
    workspace.set_company_content_gaps(CLIENT, {
        "date": "2026-08-04T00:00:00Z", "model": "stub", "summary": "Thin on winter content.",
        "own_posts": 12, "competitor_posts": 40,
        "items": [{"topic": "Winter camping", "why": "rivals own it", "junk": "dropped"},
                  {"topic": "", "why": "no topic, dropped"}, "not-a-dict"]})
    gaps = workspace.company_profile(workspace.load_workspace(CLIENT))["content_gaps"]
    _check("set_company_content_gaps stores a shaped snapshot (topic-less/junk items dropped)",
           gaps["summary"] == "Thin on winter content." and gaps["own_posts"] == 12
           and len(gaps["items"]) == 1 and gaps["items"][0]["topic"] == "Winter camping"
           and set(gaps["items"][0]) == set(workspace.CONTENT_GAP_ITEM_FIELDS))
    _check("a stored gap snapshot does not make the company profile non-empty",
           workspace.company_is_empty(workspace.load_workspace(CLIENT)))

    workspace.set_company_profile(CLIENT, {"one_liner": "Boutique RV rentals in the Rockies",
                                           "industry": "Travel", "founded": "2016"})
    workspace.set_company_profile(CLIENT, {"hq": "Denver, CO"})     # a partial patch...
    prof = workspace.company_profile(workspace.load_workspace(CLIENT))["profile"]
    _check("set_company_profile persists the facts it was given",
           prof["one_liner"].startswith("Boutique") and prof["founded"] == "2016")
    _check("a partial profile patch never blanks the fields it omitted",
           prof["industry"] == "Travel" and prof["hq"] == "Denver, CO")
    _check("company_is_empty is false once anything is written",
           not workspace.company_is_empty(workspace.load_workspace(CLIENT)))

    workspace.set_company_brand(CLIENT, {"voice": "Warm, plain-spoken, never salesy",
                                         "colors": "#1F4D3A, #F4E9D8"})
    workspace.set_company_brand(CLIENT, {"tone": "Practical"})
    brand = workspace.company_profile(workspace.load_workspace(CLIENT))["brand"]
    _check("a partial brand patch keeps the rest of the brand guide",
           brand["voice"].startswith("Warm") and brand["tone"] == "Practical")

    about = workspace.add_company_item(CLIENT, "sections",
                                       {"heading": "About", "body": "We started with one van."})
    history = workspace.add_company_item(CLIENT, "sections",
                                         {"heading": "Our history", "body": "Then came twelve."})
    order = [s["heading"] for s in workspace.company_items(workspace.load_workspace(CLIENT), "sections")]
    _check("story sections APPEND in the order they were written (not newest-first)",
           order == ["About", "Our history"])
    workspace.move_company_item(CLIENT, "sections", history["id"], -1)
    order = [s["heading"] for s in workspace.company_items(workspace.load_workspace(CLIENT), "sections")]
    _check("move_company_item reorders the story", order == ["Our history", "About"])
    workspace.move_company_item(CLIENT, "sections", history["id"], -1)
    order = [s["heading"] for s in workspace.company_items(workspace.load_workspace(CLIENT), "sections")]
    _check("a move past the top edge is a no-op, never an error", order == ["Our history", "About"])

    workspace.update_company_item(CLIENT, "sections", about["id"], {"body": "One van, then twelve."})
    edited = [s for s in workspace.company_items(workspace.load_workspace(CLIENT), "sections")
              if s["id"] == about["id"]][0]
    _check("update_company_item edits in place and keeps the untouched fields",
           edited["body"] == "One van, then twelve." and edited["heading"] == "About")

    prod = workspace.add_company_item(CLIENT, "products",
                                      {"name": "Weekender", "price": "from $249/night"})
    _check("a product persists with its own field set",
           workspace.company_items(workspace.load_workspace(CLIENT), "products")[0]["price"]
           == "from $249/night")

    removed, rest = workspace.delete_company_item(CLIENT, "products", prod["id"])
    _check("delete returns the removed payload so the route can Bin it",
           removed["name"] == "Weekender" and rest == [])
    workspace.insert_company_item(CLIENT, "products", removed)
    _check("insert_company_item restores it from the Bin payload",
           workspace.company_items(workspace.load_workspace(CLIENT), "products")[0]["name"]
           == "Weekender")
    _check("an unknown company list raises rather than silently no-opping",
           _raises_keyerror(lambda: workspace.add_company_item(CLIENT, "nope", {})))

    # 13. load_workspaces -- the concurrent fan-out every whole-estate caller uses.
    print("  -- load_workspaces (concurrent fan-out) --")
    _run_fanout_checks()

    print("[localtest] PASS")
    return 0


def _run_fanout_checks():
    """workspace.load_workspaces: correct mapping, degrades to None, and is genuinely CONCURRENT.

    🔴 The timing check is the point of this block. load_workspaces exists ONLY to turn N serial GCS
    round-trips into one wall clock's worth (the super-admin console and the /api/internal/* legs
    Sentinel blocks on both walk every client), and nothing else in the suite can catch a regression
    to a serial loop: the local-fs backend answers instantly, so a `for` loop would still pass every
    correctness assertion here. Injecting a fixed per-read delay is what makes serial vs parallel
    observable off-cloud."""
    for key in ("fanout_a", "fanout_b", "fanout_c", "fanout_d", "fanout_e", "fanout_f"):
        workspace.save_workspace(key, {"display_name": "WS " + key})
    keys = ["fanout_a", "fanout_b", "fanout_c", "fanout_d", "fanout_e", "fanout_f"]

    got = workspace.load_workspaces(keys)
    _check("every key comes back mapped to its own workspace",
           sorted(got) == sorted(keys)
           and all(got[k]["display_name"] == "WS " + k for k in keys))
    _check("an empty / falsy key list is a no-op (never builds a pool)",
           workspace.load_workspaces([]) == {} and workspace.load_workspaces(None) == {})
    _check("a single key takes the direct path and still returns a dict",
           workspace.load_workspaces(["fanout_a"])["fanout_a"]["display_name"] == "WS fanout_a")
    _check("blank keys are dropped, not turned into bogus rows",
           workspace.load_workspaces(["", None, "fanout_a"]) == {
               "fanout_a": got["fanout_a"]})
    _check("an unseeded client maps to None (not a KeyError, not a missing key)",
           workspace.load_workspaces(["fanout_a", "nope_not_seeded"])["nope_not_seeded"] is None)

    # A read that RAISES must degrade to one None -- never take out the caller. This is what keeps
    # one unreadable workspace from 500-ing the console or blanking Sentinel's board.
    real_read = workspace._read_object

    def _boom(name):
        if "fanout_c" in name:
            raise RuntimeError("simulated storage failure")
        return real_read(name)

    workspace._read_object = _boom
    try:
        got2 = workspace.load_workspaces(keys)
        _check("a FAILED read degrades to None and leaves every sibling intact",
               got2["fanout_c"] is None
               and got2["fanout_a"]["display_name"] == "WS fanout_a"
               and len(got2) == len(keys))
    finally:
        workspace._read_object = real_read

    # The timing check: N reads each sleeping DELAY must finish in well under N*DELAY.
    import time
    delay = 0.05

    def _slow(name):
        time.sleep(delay)
        return real_read(name)

    workspace._read_object = _slow
    try:
        start = time.perf_counter()
        workspace.load_workspaces(keys)
        elapsed = time.perf_counter() - start
    finally:
        workspace._read_object = real_read
    serial = delay * len(keys)
    # Generous bound (half of serial) so a loaded CI machine can't flake it, while a regression to a
    # serial loop -- which would land at or above `serial` -- fails unambiguously.
    _check("%d reads x %.0fms ran CONCURRENTLY: %.0fms, well under the %.0fms a serial loop costs"
           % (len(keys), delay * 1000, elapsed * 1000, serial * 1000),
           elapsed < serial / 2)

    for key in keys:
        workspace._delete_object(workspace._object_name(key))


def _raises_keyerror(fn):
    try:
        fn()
    except KeyError:
        return True
    return False


def main():
    try:
        return run()
    except AssertionError as exc:
        print("[localtest] FAIL: %s" % exc)
        return 1
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
