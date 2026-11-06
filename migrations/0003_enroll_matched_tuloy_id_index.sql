-- 0003 (PROPOSED, not applied anywhere remote): index the column GetEligibleBene filters on
-- (tuloy_id NOT IN (SELECT tuloy_id FROM enroll_matched_CHED_tbl ...)). The table has ~81k rows
-- and no index on tuloy_id. Online DDL: ALGORITHM=INPLACE, LOCK=NONE fails instead of blocking
-- writes if the server can't build it online. Still schedule it with the DBA.
-- Not UNIQUE: existing rows can repeat a tuloy_id (the endpoint is not idempotent). A unique key
-- needs a dedupe decision first; see docs/PRODUCTION_READINESS.md.

CREATE INDEX `idx_enroll_matched_tuloy_id` ON `enroll_matched_CHED_tbl` (`tuloy_id`) ALGORITHM=INPLACE LOCK=NONE;
