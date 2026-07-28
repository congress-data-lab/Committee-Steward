ALTER TABLE member_service
  ADD COLUMN IF NOT EXISTS caucus_party_code int;

UPDATE member_service
SET caucus_party_code = party_code
WHERE caucus_party_code IS NULL
  AND party_code IS NOT NULL;

CREATE TABLE IF NOT EXISTS committee_rank_observation (
  rank_observation_id text PRIMARY KEY,
  congress_no int NOT NULL REFERENCES congress,
  chamber char(1) NOT NULL REFERENCES chamber,
  committee_code text NOT NULL REFERENCES committee,
  bioguide_id text REFERENCES member,
  raw_member_name text NOT NULL,
  caucus_party_code int,
  decision_date date NOT NULL,
  source_document_id bigint NOT NULL REFERENCES source_document,
  source_locator text NOT NULL,
  source_block_ordinal int NOT NULL CHECK (source_block_ordinal >= 0),
  source_member_ordinal int NOT NULL CHECK (source_member_ordinal > 0),
  resolution_number int,
  rank_after_raw_name text,
  rank_after_bioguide_id text REFERENCES member,
  observation_kind text NOT NULL CHECK (
    observation_kind IN ('ORDERED_LIST', 'FULL_ROSTER', 'RELATIVE_ORDER')
  ),
  extraction_mode text NOT NULL,
  parsed_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT committee_rank_observation_source_slot
    UNIQUE (source_document_id, source_locator, source_member_ordinal)
);

CREATE TABLE IF NOT EXISTS committee_membership_rank (
  committee_membership_rank_id bigserial PRIMARY KEY,
  committee_membership_id bigint NOT NULL
    REFERENCES committee_membership(committee_membership_id) ON DELETE CASCADE,
  caucus_party_code int NOT NULL,
  rank_in_party int NOT NULL CHECK (rank_in_party > 0),
  unresolved_slots_before int NOT NULL DEFAULT 0 CHECK (unresolved_slots_before >= 0),
  valid_daterange daterange NOT NULL,
  rank_basis text NOT NULL CHECK (
    rank_basis IN ('resolution_order', 'relative_instruction')
  ),
  source_rank_observation_id text
    REFERENCES committee_rank_observation(rank_observation_id),
  parsed_source text NOT NULL,
  parser_id text NOT NULL,
  parsed_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT committee_membership_rank_one_interval
    UNIQUE (committee_membership_id, valid_daterange)
);

ALTER TABLE committee_membership_rank
  ADD CONSTRAINT committee_membership_rank_no_overlap
  EXCLUDE USING gist (
    committee_membership_id WITH =,
    valid_daterange WITH &&
  );

CREATE INDEX IF NOT EXISTS committee_rank_observation_lookup_idx
  ON committee_rank_observation (
    congress_no, chamber, committee_code, caucus_party_code, decision_date
  );

CREATE INDEX IF NOT EXISTS committee_membership_rank_lookup_idx
  ON committee_membership_rank (committee_membership_id, rank_in_party);
