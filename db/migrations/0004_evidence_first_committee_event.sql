-- Evidence-first committee_event: immutable event ledger with content-addressed id
-- Requires: source_document for every event; no interpretation columns; idempotent upsert via event_id

-- 1. Drop dependent objects (DB is empty; safe)
DROP TRIGGER IF EXISTS trg_committee_event_parsed ON committee_event;
DROP TRIGGER IF EXISTS trg_committee_membership_parsed ON committee_membership;
DROP TRIGGER IF EXISTS trg_membership_role_parsed ON committee_membership_role;

DROP TABLE IF EXISTS committee_membership_role;
DROP TABLE IF EXISTS committee_membership;
DROP TABLE IF EXISTS committee_event;

-- 2. Create committee_event as immutable event ledger
CREATE TABLE committee_event (
  event_id text PRIMARY KEY,  -- hash(congress, chamber, bioguide_id, committee_code, action, decision_date, source_document_id, source_locator)

  congress_no int NOT NULL REFERENCES congress,
  chamber char(1) NOT NULL REFERENCES chamber,
  bioguide_id text NOT NULL REFERENCES member,
  committee_code text NOT NULL REFERENCES committee,

  action text NOT NULL,  -- APPOINTED, REMOVED (controlled vocabulary)
  decision_date date NOT NULL,
  effective_date date NOT NULL,

  source_document_id bigint NOT NULL REFERENCES source_document,
  source_locator text NOT NULL,
  text_span text NOT NULL,
  extraction_mode text NOT NULL,  -- resolution_structured, record_pattern, explicit

  created_at timestamptz NOT NULL DEFAULT now(),
  parsed_at timestamptz NOT NULL DEFAULT now()
);

CREATE TRIGGER trg_committee_event_parsed
BEFORE INSERT OR UPDATE ON committee_event
FOR EACH ROW EXECUTE FUNCTION trg_set_parsed_at();

-- 3. Recreate committee_membership with event_id references
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

-- 4. Recreate committee_membership_role
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

-- 5. Lock source_document to content addressing (UNIQUE allows multiple NULLs)
CREATE UNIQUE INDEX IF NOT EXISTS source_document_content_hash_key
ON source_document (content_hash) WHERE content_hash IS NOT NULL;
