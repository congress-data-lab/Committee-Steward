-- Remove redundant APPOINTED events when membership was already active
-- and no intervening REMOVED event exists before that appointment date.
BEGIN;

WITH ordered AS (
  SELECT
    event_id,
    action,
    SUM(
      CASE
        WHEN action = 'APPOINTED' THEN 1
        WHEN action = 'REMOVED' THEN -1
        ELSE 0
      END
    ) OVER (
      PARTITION BY congress_no, chamber, bioguide_id, committee_code
      ORDER BY effective_date, decision_date, event_id
      ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
    ) AS prior_balance
  FROM committee_event
),
redundant AS (
  SELECT event_id
  FROM ordered
  WHERE action = 'APPOINTED' AND COALESCE(prior_balance, 0) > 0
)
DELETE FROM committee_event ce
USING redundant r
WHERE ce.event_id = r.event_id;

COMMIT;
