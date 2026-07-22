-- Committee Tracking Schema
-- Consolidated design with historical membership reconstruction

CREATE EXTENSION IF NOT EXISTS btree_gist;

-- AUTO-TIMESTAMP TRIGGER
CREATE OR REPLACE FUNCTION trg_set_parsed_at()
RETURNS trigger AS $$
BEGIN
  NEW.parsed_at := now();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- REFERENCE TABLES
CREATE TABLE chamber (
  chamber char(1) PRIMARY KEY, -- H / S
  name text NOT NULL
);

CREATE TABLE congress (
  congress_no int PRIMARY KEY,
  start_date date NOT NULL,
  end_date date NOT NULL
);

CREATE TABLE jurisdiction (
  code char(2) PRIMARY KEY, -- AL, AK, DC, PR, GU, etc.
  name text NOT NULL,
  type text NOT NULL CHECK (type IN ('state','territory','district'))
);

-- DOCUMENTARY SOURCE TRACKING
CREATE TABLE source (
  source_id bigserial PRIMARY KEY,
  source_type text NOT NULL, -- CR, resolution, YAML, directory
  source_name text NOT NULL,
  version_tag text,
  retrieved_at timestamptz DEFAULT now(),
  CONSTRAINT source_identity_key UNIQUE NULLS NOT DISTINCT (
    source_type, source_name, version_tag
  )
);

CREATE TABLE source_document (
  source_document_id bigserial PRIMARY KEY,
  source_id bigint REFERENCES source,
  external_id text,
  doc_date date,
  url text,
  raw_json jsonb,
  content_hash text,
  created_at timestamptz DEFAULT now()
);

-- Content addressing: no duplicate documents (allows multiple NULLs)
CREATE UNIQUE INDEX source_document_content_hash_key
ON source_document (content_hash) WHERE content_hash IS NOT NULL;

-- MEMBER IDENTITY
CREATE TABLE member (
  bioguide_id text PRIMARY KEY,
  first_name text,
  last_name text,
  nickname text,
  official_full_name text,
  icpsr_id int,
  govtrack_id int,
  gender char(1),

  parsed_source text NOT NULL,
  parser_id text NOT NULL,
  parsed_at timestamptz NOT NULL DEFAULT now()
);

CREATE TRIGGER trg_member_parsed
BEFORE INSERT OR UPDATE ON member
FOR EACH ROW EXECUTE FUNCTION trg_set_parsed_at();

-- MEMBER SERVICE
CREATE TABLE member_service (
  member_service_id bigserial PRIMARY KEY,

  bioguide_id text NOT NULL REFERENCES member,
  chamber char(1) NOT NULL REFERENCES chamber,
  state char(2) NOT NULL REFERENCES jurisdiction(code),
  district int,
  congress_no int NOT NULL REFERENCES congress,

  valid_daterange daterange NOT NULL,

  exit_reason text,
  party_code int,
  caucus_party_code int,
  source_id bigint REFERENCES source,
  source_document_id bigint REFERENCES source_document,

  parsed_source text NOT NULL,
  parser_id text NOT NULL,
  parsed_at timestamptz NOT NULL DEFAULT now()
);

CREATE TRIGGER trg_member_service_parsed
BEFORE INSERT OR UPDATE ON member_service
FOR EACH ROW EXECUTE FUNCTION trg_set_parsed_at();

-- COMMITTEE
CREATE TABLE committee (
  committee_code text PRIMARY KEY,
  CHECK (committee_code ~ '^[A-Z0-9]{3,10}$'),
  chamber char(1) NOT NULL REFERENCES chamber,
  is_joint boolean DEFAULT false,

  valid_daterange daterange NOT NULL,

  parsed_source text NOT NULL,
  parser_id text NOT NULL,
  parsed_at timestamptz NOT NULL DEFAULT now()
);

CREATE TRIGGER trg_committee_parsed
BEFORE INSERT OR UPDATE ON committee
FOR EACH ROW EXECUTE FUNCTION trg_set_parsed_at();

-- COMMITTEE NAME HISTORY
CREATE TABLE committee_name_history (
  committee_name_id bigserial PRIMARY KEY,
  committee_code text NOT NULL REFERENCES committee,
  name text NOT NULL,
  valid_daterange daterange NOT NULL,

  parsed_source text NOT NULL,
  parser_id text NOT NULL,
  parsed_at timestamptz NOT NULL DEFAULT now()
);

CREATE TRIGGER trg_committee_name_parsed
BEFORE INSERT OR UPDATE ON committee_name_history
FOR EACH ROW EXECUTE FUNCTION trg_set_parsed_at();

-- EVENTS (evidence-first immutable ledger)
-- event_id = hash(congress_no, chamber, bioguide_id, committee_code, action, decision_date [, source_document_id, source_locator])
-- Use INSERT ... ON CONFLICT (event_id) DO NOTHING for idempotent upsert.
-- One row per (congress_no, chamber, bioguide_id, committee_code, action, decision_date).
CREATE TABLE committee_event (
  event_id text PRIMARY KEY,

  congress_no int NOT NULL REFERENCES congress,
  chamber char(1) NOT NULL REFERENCES chamber,
  bioguide_id text NOT NULL REFERENCES member,
  committee_code text NOT NULL REFERENCES committee,

  action text NOT NULL
    CONSTRAINT committee_event_action_valid CHECK (action IN ('APPOINTED', 'REMOVED')),
  decision_date date NOT NULL,
  effective_date date NOT NULL
    CONSTRAINT committee_event_effective_not_before_decision CHECK (effective_date >= decision_date),

  source_document_id bigint NOT NULL REFERENCES source_document,
  source_locator text NOT NULL,
  text_span text NOT NULL,
  extraction_mode text NOT NULL,  -- resolution_structured, record_pattern, explicit

  created_at timestamptz NOT NULL DEFAULT now(),
  parsed_at timestamptz NOT NULL DEFAULT now(),

  CONSTRAINT committee_event_one_per_member_committee_action_date
    UNIQUE (congress_no, chamber, bioguide_id, committee_code, action, decision_date)
);

CREATE TRIGGER trg_committee_event_parsed
BEFORE INSERT OR UPDATE ON committee_event
FOR EACH ROW EXECUTE FUNCTION trg_set_parsed_at();

CREATE TABLE committee_event_note (
  event_id text NOT NULL REFERENCES committee_event(event_id) ON DELETE CASCADE,
  note_type text NOT NULL,
  interpretation_basis text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  created_by text NOT NULL DEFAULT 'parser',
  PRIMARY KEY (event_id, note_type),
  CONSTRAINT committee_event_note_type_check CHECK (
    note_type IN (
      'explicit_resignation',
      'implicit_leave',
      'full_exit_house',
      'multi_committee_statement',
      'header_body_split',
      'effective_date_explicit',
      'nonstandard_language'
    )
  )
);

-- COMMITTEE MEMBERSHIP
CREATE TABLE committee_membership (
  committee_membership_id bigserial PRIMARY KEY,

  bioguide_id text NOT NULL REFERENCES member,
  committee_code text NOT NULL REFERENCES committee,
  congress_no int NOT NULL REFERENCES congress,

  valid_daterange daterange NOT NULL,

  start_event_id text REFERENCES committee_event(event_id),
  end_event_id text REFERENCES committee_event(event_id),
  source_id bigint REFERENCES source,

  parsed_source text NOT NULL,
  parser_id text NOT NULL,
  parsed_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE committee_membership
ADD CONSTRAINT committee_membership_no_overlap
EXCLUDE USING gist (
  bioguide_id WITH =,
  committee_code WITH =,
  valid_daterange WITH &&
);

CREATE TRIGGER trg_committee_membership_parsed
BEFORE INSERT OR UPDATE ON committee_membership
FOR EACH ROW EXECUTE FUNCTION trg_set_parsed_at();

-- PARTY RANK EVIDENCE
CREATE TABLE committee_rank_observation (
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

CREATE TABLE committee_membership_rank (
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
    UNIQUE (committee_membership_id, valid_daterange),
  CONSTRAINT committee_membership_rank_no_overlap
    EXCLUDE USING gist (
      committee_membership_id WITH =,
      valid_daterange WITH &&
    )
);

CREATE INDEX committee_rank_observation_lookup_idx
ON committee_rank_observation (
  congress_no, chamber, committee_code, caucus_party_code, decision_date
);

CREATE INDEX committee_membership_rank_lookup_idx
ON committee_membership_rank (committee_membership_id, rank_in_party);

-- ROLE TYPES
CREATE TABLE committee_role_type (
  role_code text PRIMARY KEY,
  description text
);

-- ROLE INTERVALS
CREATE TABLE committee_membership_role (
  committee_membership_role_id bigserial PRIMARY KEY,
  committee_membership_id bigint NOT NULL REFERENCES committee_membership,
  role_code text NOT NULL REFERENCES committee_role_type,

  valid_daterange daterange NOT NULL,

  parsed_source text NOT NULL,
  parser_id text NOT NULL,
  parsed_at timestamptz NOT NULL DEFAULT now()
);

CREATE TRIGGER trg_membership_role_parsed
BEFORE INSERT OR UPDATE ON committee_membership_role
FOR EACH ROW EXECUTE FUNCTION trg_set_parsed_at();

-- CONGRESSIONAL DIRECTORIES
CREATE TABLE congressional_directory_entry (
  id bigserial PRIMARY KEY,

  congress_no int NOT NULL,
  chamber char(1) NOT NULL
    CONSTRAINT congressional_directory_entry_chamber_fkey REFERENCES chamber(chamber),
  bioguide_id text,

  committee_text text NOT NULL,
  normalized_committees jsonb,

  publication_date date,
  page_reference text,
  raw_text text NOT NULL,

  parsed_source text NOT NULL,
  parser_id text NOT NULL,
  parsed_at timestamptz NOT NULL DEFAULT now()
);

-- FK and lookup indexes
CREATE INDEX committee_event_source_document_id_idx
ON committee_event (source_document_id);

CREATE INDEX committee_event_note_event_id_idx
ON committee_event_note (event_id);

CREATE INDEX committee_membership_start_event_id_idx
ON committee_membership (start_event_id);

CREATE INDEX committee_membership_end_event_id_idx
ON committee_membership (end_event_id);

CREATE INDEX member_service_bioguide_id_idx
ON member_service (bioguide_id);

CREATE INDEX member_service_congress_no_idx
ON member_service (congress_no);

CREATE INDEX member_service_chamber_idx
ON member_service (chamber);

CREATE INDEX member_service_state_idx
ON member_service (state);

CREATE INDEX member_service_source_id_idx
ON member_service (source_id);

CREATE INDEX member_service_source_document_id_idx
ON member_service (source_document_id);

-- A service interval is a logical fact.  Database surrogate IDs and NULL
-- Senate districts must not make repeated reference loads create duplicates.
CREATE UNIQUE INDEX member_service_logical_key
ON member_service (
  bioguide_id,
  chamber,
  state,
  COALESCE(district, -1),
  congress_no,
  valid_daterange
);

CREATE INDEX source_document_source_id_idx
ON source_document (source_id);

CREATE INDEX source_document_doc_date_idx
ON source_document (doc_date);

CREATE INDEX committee_name_history_committee_code_idx
ON committee_name_history (committee_code);

CREATE INDEX committee_membership_role_committee_membership_id_idx
ON committee_membership_role (committee_membership_id);

CREATE INDEX committee_membership_role_role_code_idx
ON committee_membership_role (role_code);
