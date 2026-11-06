"""Every scope this API recognises. A grant for any other name is ignored (deny).

Partner scopes: approved Tuloy partner operation matrix (2026-10-18).
Legacy scopes: ratified by Martin, ADR 2026-10-26-tuloy-api-main-rbac-direct-scope-grants.
"""
SCOPE_VERIFY = "tuloy:member:verify"
SCOPE_ENROLL_SUBMIT = "tuloy:enrollment:submit"
SCOPE_ENROLL_READ = "tuloy:enrollment:read"
SCOPE_ENROLL_UPDATE = "tuloy:enrollment:update"
SCOPE_HISTORY_READ = "tuloy:history:read"

LEGACY_MEMBER_VERIFY = "tuloy:legacy:member:verify"         # POST /VerifyMemberBene
LEGACY_BENEFICIARY_READ = "tuloy:legacy:beneficiary:read"   # GET /GetEligibleBene (bulk feed)
LEGACY_CHED_WRITE = "tuloy:legacy:ched:write"               # POST /PostEnrolleeMatchedCHED
LEGACY_CHED_READ = "tuloy:legacy:ched:read"                 # GET /GetMatchedChedBene (bulk feed)

# Viewing the internal DSWD docs (/tuloydswd/docs). Ratified by Martin 2026-10-26; grants nothing else.
DOCS_INTERNAL = "tuloy:docs:internal"

# Staff web app: show every module to this account. Opens no API route; each module's own API call
# still needs its scope. Requested for tuloy_admin on staging, 2026-10-28.
STAFF_ADMIN = "tuloy:staff:admin"

# BCMD duplicate-selection review: read cross-batch conflicts and record which batch keeps a claim
# (docs/bcmd-duplicate-selection-review.md). BCMD affiliation alone is not enough. Granted to no
# account by default, and never conferred by the staging full-access override.
BCMD_SELECTION_REVIEW = "tuloy:bcmd:selection:review"

PARTNER_SCOPES = frozenset({SCOPE_VERIFY, SCOPE_ENROLL_SUBMIT, SCOPE_ENROLL_READ, SCOPE_ENROLL_UPDATE,
                            SCOPE_HISTORY_READ})
LEGACY_SCOPES = frozenset({LEGACY_MEMBER_VERIFY, LEGACY_BENEFICIARY_READ, LEGACY_CHED_WRITE, LEGACY_CHED_READ})
DOCS_SCOPES = frozenset({DOCS_INTERNAL})
STAFF_SCOPES = frozenset({STAFF_ADMIN})
BCMD_SCOPES = frozenset({BCMD_SELECTION_REVIEW})
KNOWN_SCOPES = PARTNER_SCOPES | LEGACY_SCOPES | DOCS_SCOPES | STAFF_SCOPES | BCMD_SCOPES

# Approved grant bindings for every scope (Martin, 2026-10-27, QA register G04): agency is one of these
# and partner_id equals agency. Enforced by scripts/grants.py and scripts/provision_verification.py.
APPROVED_AGENCIES = frozenset({"CHED", "TESDA", "DSWD"})

# Data-owner rulings (Martin, ADR 2026-10-27-tuloy-data-owner-access-rulings): DSWD administers the
# data, so nationwide verification and the bulk feeds are DSWD only; CHED or DSWD may write CHED
# matches. A grant bound to any other agency authorises nothing. Every other scope: APPROVED_AGENCIES.
SCOPE_AGENCIES = {
    LEGACY_MEMBER_VERIFY: frozenset({"DSWD"}),
    LEGACY_BENEFICIARY_READ: frozenset({"DSWD"}),
    LEGACY_CHED_READ: frozenset({"DSWD"}),
    LEGACY_CHED_WRITE: frozenset({"CHED", "DSWD"}),
    STAFF_ADMIN: frozenset({"DSWD"}),  # DSWD administers the staff app
    BCMD_SELECTION_REVIEW: frozenset({"DSWD"}),  # most restrictive until the holders are ruled (Q6)
}


def agencies_for(scope: str) -> frozenset:
    """The agencies a new grant for ``scope`` may be bound to."""
    return SCOPE_AGENCIES.get(scope, APPROVED_AGENCIES)


def agency_permitted(scope: str, agency: str) -> bool:
    """Whether an existing grant's agency may exercise ``scope``. Only the ruled scopes are narrowed."""
    return scope not in SCOPE_AGENCIES or agency in SCOPE_AGENCIES[scope]
