-- Add party to member_service for per-congress affiliation.
-- Backfill via scripts/backfill_member_service_party.py from legislators YAML.
ALTER TABLE member_service ADD COLUMN IF NOT EXISTS party_code int;
