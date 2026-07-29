-- 03_stg_email_events.sql -> view stg_email_events
--
-- Typed per-recipient email events from the direct-API mirror raw_windsor.tcs_klaviyo_events
-- (one row per SEND, flagged is_open / is_click plus the deliverability signals). This is the
-- grain the diagnostic needs to ask "are THESE quiz leads opening/clicking less?". event_at is
-- the send timestamp -- the reference point for "post-quiz" engagement downstream.
--
-- DE-DUPE, VERSION-AWARE: the loader appends incrementally AND re-pulls months whose rows were
-- written by an older loader version, so the same event_id can legitimately exist twice: once
-- from pull_version 1 (no deliverability columns -- is_bounce/is_unsub/... are NULL) and once
-- from v2 (complete). Ordering by pull_version DESC keeps the COMPLETE row. Ordering by sent_at
-- alone (as this view used to) TIES between the two copies, so BigQuery would pick one at
-- random and silently serve NULL deliverability flags for part of the table.
--
-- is_open / is_click are COALESCEd to FALSE because there "no event" genuinely means "not
-- opened". The deliverability flags are deliberately NOT coalesced: NULL there means "this row
-- predates deliverability tracking", which is a different claim from "did not bounce" --
-- collapsing the two would understate bounce/unsub rates on older data.
CREATE OR REPLACE VIEW `agora-data-driven.client_tcs.stg_email_events` AS
SELECT
  email, message_id, subject, campaign, flow, event_at, is_open, is_click,
  is_bounce, is_unsub, is_spam, is_dropped, pull_version
FROM (
  SELECT
    LOWER(TRIM(email))        AS email,
    message_id, subject, campaign, flow,
    sent_at                   AS event_at,
    COALESCE(is_open,  FALSE) AS is_open,
    COALESCE(is_click, FALSE) AS is_click,
    is_bounce, is_unsub, is_spam, is_dropped,
    COALESCE(pull_version, 1) AS pull_version,
    ROW_NUMBER() OVER (
      PARTITION BY event_id
      ORDER BY COALESCE(pull_version, 1) DESC, sent_at DESC
    ) AS _rn
  FROM `agora-data-driven.raw_windsor.tcs_klaviyo_events`
  WHERE email IS NOT NULL AND sent_at IS NOT NULL
)
WHERE _rn = 1;
