-- One event per (congress_no, chamber, bioguide_id, committee_code, action, decision_date).
-- Dedupe existing rows (keep one per key), then enforce via UNIQUE.

-- 1. Remove duplicate events: keep the row with the smallest event_id per logical key.
DELETE FROM committee_event a
USING committee_event b
WHERE a.congress_no = b.congress_no
  AND a.chamber = b.chamber
  AND a.bioguide_id = b.bioguide_id
  AND a.committee_code = b.committee_code
  AND a.action = b.action
  AND a.decision_date = b.decision_date
  AND a.event_id > b.event_id;

-- 2. Enforce one row per logical appointment.
ALTER TABLE committee_event
ADD CONSTRAINT committee_event_one_per_member_committee_action_date
UNIQUE (congress_no, chamber, bioguide_id, committee_code, action, decision_date);
