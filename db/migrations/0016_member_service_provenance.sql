-- Make YAML-derived member service ranges content-addressed and rerun-safe.

ALTER TABLE member_service
ADD COLUMN IF NOT EXISTS source_document_id bigint REFERENCES source_document;

-- Keep the most recently parsed copy of each logical interval before adding
-- uniqueness. No committee membership row references member_service directly.
WITH ranked AS (
  SELECT
    member_service_id,
    row_number() OVER (
      PARTITION BY
        bioguide_id,
        chamber,
        state,
        COALESCE(district, -1),
        congress_no,
        valid_daterange
      ORDER BY parsed_at DESC, member_service_id DESC
    ) AS duplicate_rank
  FROM member_service
)
DELETE FROM member_service service
USING ranked
WHERE service.member_service_id = ranked.member_service_id
  AND ranked.duplicate_rank > 1;

CREATE INDEX IF NOT EXISTS member_service_source_document_id_idx
ON member_service (source_document_id);

CREATE UNIQUE INDEX IF NOT EXISTS member_service_logical_key
ON member_service (
  bioguide_id,
  chamber,
  state,
  COALESCE(district, -1),
  congress_no,
  valid_daterange
);
