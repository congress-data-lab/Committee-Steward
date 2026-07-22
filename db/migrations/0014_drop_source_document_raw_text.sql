-- Citation-only: full text is not stored; point back to source document (external_id, doc_date) and event text_span.
ALTER TABLE source_document DROP COLUMN IF EXISTS raw_text;
