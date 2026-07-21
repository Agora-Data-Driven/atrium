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

    print("upwork_import localtest: OK (%d messages parsed, quoted dup dropped)" % len(msgs))


if __name__ == "__main__":
    main()
