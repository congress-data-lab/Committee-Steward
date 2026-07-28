-- Initial Schema Migration
-- 0001_initial_schema.sql

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
  chamber char(1) PRIMARY KEY,
  name text NOT NULL
);

CREATE TABLE congress (
  congress_no int PRIMARY KEY,
  start_date date NOT NULL,
  end_date date NOT NULL
);

CREATE TABLE jurisdiction (
  code char(2) PRIMARY KEY,
  name text NOT NULL,
  type text NOT NULL CHECK (type IN ('state','territory','district'))
);

-- DOCUMENTARY SOURCE TRACKING
CREATE TABLE source (
  source_id bigserial PRIMARY KEY,
  source_type text NOT NULL,
  source_name text NOT NULL,
  version_tag text,
  retrieved_at timestamptz DEFAULT now()
);

CREATE TABLE source_document (
  source_document_id bigserial PRIMARY KEY,
  source_id bigint REFERENCES source,
  external_id text,
  doc_date date,
  url text,
  raw_text text,
  raw_json jsonb,
  content_hash text,
  created_at timestamptz DEFAULT now()
);

-- MEMBER IDENTITY
CREATE TABLE member (
  bioguide_id text PRIMARY KEY,
  first_name text,
  last_name text,
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
  source_id bigint REFERENCES source,

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

-- COMMITTEE MEMBERSHIP
CREATE TABLE committee_membership (
  committee_membership_id bigserial PRIMARY KEY,

  bioguide_id text NOT NULL REFERENCES member,
  committee_code text NOT NULL REFERENCES committee,
  congress_no int NOT NULL REFERENCES congress,

  valid_daterange daterange NOT NULL,

  start_event_id bigint,
  end_event_id bigint,
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

-- EVENTS
CREATE TABLE committee_event (
  committee_event_id bigserial PRIMARY KEY,

  event_date date NOT NULL,
  event_type text NOT NULL,

  bioguide_id text REFERENCES member,
  committee_code text REFERENCES committee,

  payload jsonb NOT NULL,
  source_document_id bigint REFERENCES source_document,

  parsed_source text NOT NULL,
  parser_id text NOT NULL,
  parsed_at timestamptz NOT NULL DEFAULT now()
);

CREATE TRIGGER trg_committee_event_parsed
BEFORE INSERT OR UPDATE ON committee_event
FOR EACH ROW EXECUTE FUNCTION trg_set_parsed_at();

-- CONGRESSIONAL DIRECTORIES
CREATE TABLE congressional_directory_entry (
  id bigserial PRIMARY KEY,

  congress_no int NOT NULL,
  chamber char(1) NOT NULL,
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
