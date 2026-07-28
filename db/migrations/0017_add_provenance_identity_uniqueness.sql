-- Apply only after scripts/consolidate_provenance.py has merged duplicate rows.
-- The guard fails before DDL so an incomplete consolidation cannot be hidden.
DO $$
DECLARE
  duplicate_groups bigint;
BEGIN
  SELECT count(*)
  INTO duplicate_groups
  FROM (
    SELECT 1
    FROM source
    GROUP BY source_type, source_name, version_tag
    HAVING count(*) > 1
  ) AS duplicates;

  IF duplicate_groups > 0 THEN
    RAISE EXCEPTION
      'Cannot add source_identity_key: % duplicate source identity group(s) remain',
      duplicate_groups
      USING HINT = 'Run scripts/consolidate_provenance.py before this migration.';
  END IF;
END;
$$;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conrelid = 'source'::regclass
      AND conname = 'source_identity_key'
  ) THEN
    ALTER TABLE source
      ADD CONSTRAINT source_identity_key
      UNIQUE NULLS NOT DISTINCT (source_type, source_name, version_tag);
  END IF;
END;
$$;
