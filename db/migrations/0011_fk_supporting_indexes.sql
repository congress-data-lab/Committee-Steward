-- Add missing indexes that support FK joins and frequent lookups.
BEGIN;

CREATE INDEX IF NOT EXISTS committee_event_source_document_id_idx
ON committee_event (source_document_id);

CREATE INDEX IF NOT EXISTS committee_membership_start_event_id_idx
ON committee_membership (start_event_id);

CREATE INDEX IF NOT EXISTS committee_membership_end_event_id_idx
ON committee_membership (end_event_id);

CREATE INDEX IF NOT EXISTS member_service_bioguide_id_idx
ON member_service (bioguide_id);

CREATE INDEX IF NOT EXISTS member_service_congress_no_idx
ON member_service (congress_no);

CREATE INDEX IF NOT EXISTS member_service_chamber_idx
ON member_service (chamber);

CREATE INDEX IF NOT EXISTS member_service_state_idx
ON member_service (state);

CREATE INDEX IF NOT EXISTS member_service_source_id_idx
ON member_service (source_id);

CREATE INDEX IF NOT EXISTS source_document_source_id_idx
ON source_document (source_id);

CREATE INDEX IF NOT EXISTS source_document_doc_date_idx
ON source_document (doc_date);

CREATE INDEX IF NOT EXISTS committee_name_history_committee_code_idx
ON committee_name_history (committee_code);

CREATE INDEX IF NOT EXISTS committee_membership_role_committee_membership_id_idx
ON committee_membership_role (committee_membership_id);

CREATE INDEX IF NOT EXISTS committee_membership_role_role_code_idx
ON committee_membership_role (role_code);

COMMIT;
