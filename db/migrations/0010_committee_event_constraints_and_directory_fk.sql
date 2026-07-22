-- Add integrity coverage for committee_event and directory entries.
BEGIN;

-- Align effective dates before adding the check that forbids earlier values.
UPDATE committee_event
SET effective_date = decision_date
WHERE effective_date < decision_date;

-- Prevent future rows from violating the effective >= decision invariant.
ALTER TABLE committee_event
ADD CONSTRAINT committee_event_effective_not_before_decision
CHECK (effective_date >= decision_date);

-- Lock the action vocabulary.
ALTER TABLE committee_event
ADD CONSTRAINT committee_event_action_valid CHECK (action IN ('APPOINTED', 'REMOVED'));

-- Ensure directory entries refer to the canonical chamber list.
ALTER TABLE congressional_directory_entry
ADD CONSTRAINT congressional_directory_entry_chamber_fkey
FOREIGN KEY (chamber) REFERENCES chamber(chamber);

COMMIT;
