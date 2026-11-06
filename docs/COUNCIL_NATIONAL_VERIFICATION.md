# Council review: national verification account policy

Status: proposed for council record; not represented as ratified minutes.
User direction: 2026-10-26, multiple accounts per agency, all regions.

- Support multiple named accounts for each of CHED, TESDA and DSWD using existing per-user agency grants.
- Each account is provisioned for its assigned agency. Agency attributes who is calling; member verification is national and does not filter beneficiary records by region or agency.
- Preserve authentication, active grant checks, expiry, revocation and audit attribution per account.
- Original verification route: POST /VerifyMemberBene requires tuloy:legacy:member:verify.
- Partner verification route: POST /v1/api/tuloy/members/verify requires tuloy:member:verify.
- Grant either or both explicitly. Do not expand the staff web allow-list automatically or grant bulk access/enrollment access.
- Keep partner-owned enrollment isolation separate; national verification does not authorize cross-partner enrollment access.
- Reclassify the audit's missing agency/region filtering concern for verification as intentional national lookup under this policy. Eligibility criteria, match-key correctness and other audit findings remain unresolved.

Local implementation: verification-grant agency validation; account profile agency and national coverage display; account help text; synthetic tests for both routes and all three agencies. No database migrations or shared-database account changes were performed.

Activation inputs still required: named usernames, assigned agency, partner IDs, expiry and target environment. Review and record the decision through the existing council workflow; this document does not run or claim to run /council-execute.
