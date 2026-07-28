-- Add gender to member (canonical person-level attribute from legislators YAML).
-- Backfill via scripts/backfill_member_gender.py from legislators-current/historical YAML.
ALTER TABLE member ADD COLUMN IF NOT EXISTS gender char(1);
