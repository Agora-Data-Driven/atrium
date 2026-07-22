"""Off-cloud test for upwork_import.parse_upwork (pure parser -- no network/storage/AI).

Run: python _upwork_import_localtest.py
"""

import upwork_import

# A representative slice of a real Upwork copy-paste: day headers, both senders, an avatar-initials
# line ("DM"), an attachment run, and a QUOTED reply block (full date + pipe, ending in "Show more")
# that must be dropped as a duplicate.
SAMPLE = """Saturday, Jul 11
DM
Daniela Marquez
12:59 AM
Hi Ian!
We have a storage place called Down Valley Storage.

this needs to be send ASAP.
Ian Gabriel Fernandez
10:55 PM
Hi Daniela, got it. Thank you for sending this over.

The meeting will be on Wednesday at 6:00 AM (GMT+8).
Monday, Jul 13
Ian Gabriel Fernandez
1:30 PM
We've completed the two Down Valley Storage email drafts.
2 files
email_2_leads_preview.pdf
443 kB
email_1_rd_pp_owners_preview.pdf
399 kB
DM
Daniela Marquez
11:13 PM
Hi! Is dmarquez@fortiuscap.com
Ian Gabriel Fernandez
Jul 11, 2026 | 10:55 PM
Hi Daniela, got it. Thank you for sending this over.

The meeting will be on Wednesday at 6:00 AM (GMT+8).
Show more
Tuesday, Jul 14
DM
Daniela Marquez
12:08 AM
The emails look great, a few comments.
"""


def main():
    parsed = upwork_import.parse_upwork(SAMPLE, agora_names=["Ian Gabriel Fernandez"], year=2026)
    msgs = parsed["messages"]

    # 5 real messages (the quoted Jul 11 reply is dropped as a duplicate).
    assert len(msgs) == 5, "expected 5 messages, got %d: %r" % (len(msgs), [m["from"] for m in msgs])

    # Order + role assignment.
    assert msgs[0]["from"] == "Daniela Marquez" and msgs[0]["role"] == "client", msgs[0]
    assert msgs[0]["date"] == "2026-07-11T00:59", msgs[0]["date"]           # 12:59 AM
    assert msgs[1]["from"] == "Ian Gabriel Fernandez" and msgs[1]["role"] == "agora", msgs[1]
    assert msgs[1]["date"] == "2026-07-11T22:55", msgs[1]["date"]           # 10:55 PM
    assert msgs[2]["date"] == "2026-07-13T13:30", msgs[2]["date"]           # 1:30 PM

    # Bodies keep their paragraphs; the avatar initials line ("DM") is dropped, not treated as a msg.
    assert "Down Valley Storage" in msgs[0]["body"], msgs[0]["body"]
    assert "DM" not in [m["from"] for m in msgs]

    # Attachments are captured as a paperclip note on the message they followed.
    assert "email_2_leads_preview.pdf" in msgs[2]["body"], msgs[2]["body"]
    assert "email_1_rd_pp_owners_preview.pdf" in msgs[2]["body"], msgs[2]["body"]
    assert "\U0001F4CE" in msgs[2]["body"], msgs[2]["body"]

    # The quoted duplicate did NOT create a 2nd "got it" message.
    got_it = [m for m in msgs if "got it" in m["body"]]
    assert len(got_it) == 1, "quoted reply not de-duplicated: %d matches" % len(got_it)

    # Participants + derived fields.
    assert parsed["client_participants"] == ["Daniela Marquez"], parsed["client_participants"]
    assert parsed["agora_participants"] == ["Ian Gabriel Fernandez"], parsed["agora_participants"]
    assert parsed["latest_date"] == "2026-07-14T00:08", parsed["latest_date"]
    assert "Daniela Marquez" in parsed["title"], parsed["title"]

    # First-name matching: "Ian" should also resolve to the team.
    p2 = upwork_import.parse_upwork(SAMPLE, agora_names="Ian", year=2026)
    assert p2["agora_participants"] == ["Ian Gabriel Fernandez"], p2["agora_participants"]

    # Fallback summary is non-empty and mentions the count.
    fb = upwork_import.fallback_summary(parsed)
    assert "5 messages" in fb, fb

    # Empty input degrades to nothing (never raises).
    empty = upwork_import.parse_upwork("", agora_names=["Ian"])
    assert empty["messages"] == [] and empty["latest_date"] == ""
    assert upwork_import.fallback_summary(empty) == ""

    # --- Upwork SYSTEM EVENTS are dropped, not parsed as messages/participants -------------------
    EVENTS = """Monday, Jul 13
Lorenzo Marchese
2:00 PM
Hi, sharing the offer now.
Lorenzo Marchese sent an offer
2:01 PM
THE ROLE

We're building out our analytics practice.
Ian Gabriel Fernandez accepted an offer
2:05 PM
View contract
Ian Gabriel Fernandez
2:06 PM
Great, excited to start!
"""
    ev = upwork_import.parse_upwork(EVENTS, agora_names=["Ian Gabriel Fernandez"], year=2026)
    froms = [m["from"] for m in ev["messages"]]
    assert froms == ["Lorenzo Marchese", "Ian Gabriel Fernandez"], froms
    assert not any("offer" in f.lower() for f in froms), froms       # no event line became a sender
    assert ev["participants"] == ["Lorenzo Marchese", "Ian Gabriel Fernandez"], ev["participants"]
    assert "sent an offer" not in ev["title"], ev["title"]

    # --- normalize_chat_thread heals an OLD import (all-client roles + an event msg + jumbled) ----
    stored = {
        "subject": "Upwork conversation with Lorenzo Marchese, Lorenzo Marchese sent an offer",
        "participants": ["Lorenzo Marchese", "Lorenzo Marchese sent an offer", "Ian Gabriel Fernandez"],
        "messages": [
            {"from": "Ian Gabriel Fernandez", "to": "", "date": "2026-07-13T14:06", "role": "client",
             "body": "Great, excited to start!"},
            {"from": "Lorenzo Marchese sent an offer", "to": "", "date": "2026-07-13T14:01",
             "role": "client", "body": "THE ROLE"},
            {"from": "Lorenzo Marchese", "to": "", "date": "2026-07-13T14:00", "role": "client",
             "body": "Hi, sharing the offer now."},
        ],
    }
    changed = upwork_import.normalize_chat_thread(stored, agora_names=["Ian Gabriel Fernandez"])
    assert changed is True
    nf = [m["from"] for m in stored["messages"]]
    assert nf == ["Lorenzo Marchese", "Ian Gabriel Fernandez"], nf   # event dropped + sorted
    roles = {m["from"]: m["role"] for m in stored["messages"]}
    assert roles["Ian Gabriel Fernandez"] == "agora", roles          # "me" now tagged agora (right)
    assert roles["Lorenzo Marchese"] == "client", roles
    assert stored["subject"] == "Upwork conversation with Lorenzo Marchese", stored["subject"]
    # Idempotent: a second pass changes nothing.
    assert upwork_import.normalize_chat_thread(stored, agora_names=["Ian Gabriel Fernandez"]) is False

    # --- merge_messages folds in only genuinely-new messages (the "add newer messages" flow) ------
    existing = [
        {"from": "Daniela Marquez", "date": "2026-07-11T00:59", "role": "client", "body": "Hi Ian!"},
        {"from": "Ian Gabriel Fernandez", "date": "2026-07-11T22:55", "role": "agora", "body": "Got it."},
    ]
    incoming = [
        # the same two (re-pasted) ...
        {"from": "Daniela Marquez", "date": "2026-07-11T00:59", "role": "client", "body": "Hi Ian!"},
        {"from": "Ian Gabriel Fernandez", "date": "2026-07-11T22:55", "role": "agora", "body": "Got it."},
        # ... plus two NEW ones from the same people, later.
        {"from": "Daniela Marquez", "date": "2026-07-12T09:00", "role": "client", "body": "One more thing."},
        {"from": "Ian Gabriel Fernandez", "date": "2026-07-12T09:30", "role": "agora", "body": "On it."},
    ]
    merged, added = upwork_import.merge_messages(existing, incoming)
    assert added == 2, added                                   # only the two new ones counted
    assert len(merged) == 4, len(merged)                       # no duplicates of the first two
    assert [m["date"] for m in merged] == sorted(m["date"] for m in merged)   # ordered
    assert merged[-1]["body"] == "On it.", merged[-1]
    # Re-merging the same incoming again adds nothing (idempotent).
    merged2, added2 = upwork_import.merge_messages(merged, incoming)
    assert added2 == 0 and len(merged2) == 4, (added2, len(merged2))

    print("upwork_import localtest: OK (%d messages parsed, quoted dup dropped, events skipped, "
          "merge verified)" % len(msgs))


if __name__ == "__main__":
    main()
