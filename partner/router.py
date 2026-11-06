"""Partner API: the six business operations under /v1/api/tuloy.

Every operation authenticates, checks its scope and validates its input. Member verification
uses the existing beneficiary lookup; the other five operations remain 501 placeholders.

Scope names follow the approved Tuloy partner operation matrix (2026-10-18).
"""
import re
from datetime import date, datetime
from typing import Annotated, Literal, Optional

from fastapi import APIRouter, Depends, Header, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from pydantic import (AwareDatetime, BaseModel, ConfigDict, Field, PlainValidator, StringConstraints, TypeAdapter,
                      ValidationError, WithJsonSchema, field_validator, model_validator)

from config import MAX_BODY_BYTES
from errors import ApiError
from observability import audit
from schemas import _real_date, UserInput
from database import get_ods_connection
from routers.beneficiaries import verify_member_match
from security import Principal, require_scope
from scopes import APPROVED_AGENCIES

PREFIX = "/v1/api/tuloy"

from scopes import (  # noqa: E402  (names kept here for existing imports)
    SCOPE_ENROLL_READ,
    SCOPE_ENROLL_SUBMIT,
    SCOPE_ENROLL_UPDATE,
    SCOPE_HISTORY_READ,
    SCOPE_VERIFY,
)

router = APIRouter(prefix=PREFIX, tags=["Partner API"])

TransactionId = Annotated[str, Path(pattern=r"^[A-Za-z0-9-]{1,64}$", description="ID of the enrollment. Letters, digits and dashes, up to 64 characters.",
                                    examples=["example-enrollment-0001"])]
# One identifier for a person across the partner operations. The format is PROVISIONAL: it waits for agency
# samples (ADR 2026-10-27). Source columns are varchar(50)/varchar(100). Not applied to the R3 body (non-empty only).
_TUID_DOC = ("Tuloy ID of the person: letters, digits and dashes, 1 to 50 characters. "
             "This format is provisional, pending agency samples.")
TuloyIdPath = Annotated[str, Path(pattern=r"^[A-Za-z0-9-]{1,50}$", description=_TUID_DOC, examples=["4ps-1234"])]
TuloyIdQuery = Annotated[str, Query(pattern=r"^[A-Za-z0-9-]{1,50}$",
                                              description=_TUID_DOC + " Leave out to list all.", examples=["4ps-1234"])]
_UUID_PATTERN = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
IdempotencyKey = Annotated[
    str,
    Header(
        alias="Idempotency-Key",
        pattern=_UUID_PATTERN,
        description="A UUID you generate for this request. Retrying with the same key and the same body "
                    "returns the first result; the same key with a different body returns 409.",
        examples=["123e4567-e89b-12d3-a456-426614174000"],
    ),
]
SubmitIdempotencyKey = Annotated[
    str,
    Header(
        alias="Idempotency-Key",
        pattern=_UUID_PATTERN,
        description="A UUID you generate for this request. Required today and format-checked, but nothing is "
                    "stored yet: every valid request returns 501. Once the operation is active (after activation), "
                    "retrying with the same key and the same body will return the first result and the same key "
                    "with a different body will return 409.",
        examples=["123e4567-e89b-12d3-a456-426614174000"],
    ),
]
Limit = Annotated[int, Query(ge=1, le=100, description="Items per page, 1 to 100.", examples=[50])]
# Optional query strings are typed `str` with a None default: absent stays None, and the schema is a plain
# string (no anyOf wrapper, so docs show one title).
After = Annotated[str, Query(max_length=128, pattern=r"^[A-Za-z0-9_-]+$",
                                       description="Cursor from the previous page. Leave out for the first page.")]

ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
OptText = Optional[Annotated[str, StringConstraints(strip_whitespace=True, max_length=100)]]
OptCode = Optional[Annotated[str, StringConstraints(strip_whitespace=True, max_length=50)]]

def _error(description: str, code: str, detail) -> dict:
    """Response entry with a synthetic example of the standard error body."""
    return {"description": description,
            "content": {"application/json": {"example": {"detail": detail, "error_code": code,
                                                         "request_id": "example-request-0001"}}}}


def _errors(description: str, *examples: tuple) -> dict:
    """Response entry with one named synthetic example per (error_code, detail)."""
    return {"description": description, "content": {"application/json": {"examples": {
        code: {"value": {"detail": detail, "error_code": code, "request_id": "example-request-0001"}}
        for code, detail in examples}}}}


def _invalid(loc: list, msg: str, type_: str) -> dict:
    return _error("Invalid input", "VALIDATION_ERROR", [{"loc": loc, "msg": msg, "type": type_}])


# Messages below are the ones the app emits (security.py, database.py, errors.py, main.py).
COMMON = {
    401: _error("Token missing, invalid or expired", "UNAUTHENTICATED", "Not authenticated"),
    403: _error("Account lacks the scope, or its grant does not allow this operation", "FORBIDDEN",
                "Not permitted for this operation"),
    429: _error("Too many requests", "RATE_LIMITED", "Too many requests"),
    500: _errors("Database or unexpected server error", ("DB_ERROR", "Database error"),
                 ("INTERNAL_ERROR", "Internal server error")),
    503: _errors("Database unavailable or too slow", ("DB_UNAVAILABLE", "Service temporarily unavailable"),
                 ("DB_TIMEOUT", "Query took too long, narrow the request and retry")),
    501: _error("Operation contract awaiting approval", "NOT_IMPLEMENTED",
                "This operation's contract awaits approval. Nothing was processed or stored."),
}
_ID_MSG = "String should match pattern '{}'"
UNSUPPORTED_MEDIA = _error("Content-Type is not application/json", "UNSUPPORTED_MEDIA_TYPE",
                           "Content-Type must be application/json")
TOO_LARGE = _error("Body larger than 1,000,000 bytes", "PAYLOAD_TOO_LARGE", "Request body too large")


def _responses(invalid: dict, extra: dict | None = None, implemented: bool = False) -> dict:
    """Only what the operation can return today. Placeholders never return 200, 404 or 409."""
    out = {**COMMON, 422: invalid, **(extra or {})}
    if implemented:
        out.pop(501)
    return dict(sorted(out.items()))


# Rows shared by every operation: (status, code, meaning, what to do).
_COMMON_ROWS = (
    (401, "UNAUTHENTICATED", "Token missing, invalid or expired", "Sign in again at `POST /token`."),
    (403, "FORBIDDEN", "Your account has no active scope grant, or its grant does not allow this operation",
     "Request access through the contact who issued your API account. Quote the scope above this table "
     "and the request ID from the `X-Request-ID` response header."),
    (422, "VALIDATION_ERROR", "A field is missing or invalid", "Fix the fields listed in `detail`."),
    (429, "RATE_LIMITED", "Too many requests", "Wait `Retry-After` seconds, then retry."),
    (500, "DB_ERROR", "A database or unexpected server error (`INTERNAL_ERROR`)",
     "Retry later. If it persists, report the request ID from the `X-Request-ID` header."),
    (501, "NOT_IMPLEMENTED", "This operation's contract awaits approval. Nothing was processed or stored.",
     "You can safely retry later. Do not build production flows on this operation yet."),
    (503, "DB_UNAVAILABLE", "The database is unavailable, or the query was too slow (`DB_TIMEOUT`)",
     "Retry later, after `Retry-After` seconds when it is sent."),
)
_TOO_LARGE_ROW = (413, "PAYLOAD_TOO_LARGE", "The body is larger than 1,000,000 bytes", "Send a smaller body.")


def _doc(summary: str, scope: str, rules: tuple = (), extra_rows: tuple = (), implemented: bool = False) -> str:
    """Operation description: one sentence, short rules, then the error table."""
    rows = sorted(tuple(r for r in _COMMON_ROWS if not implemented or r[0] != 501) + extra_rows, key=lambda r: r[0])
    bullets = "\n".join(f"- {r}" for r in (f"Needs the `{scope}` scope.", *rules))
    table = "\n".join(f"| {st} | `{code}` | {meaning} | {action} |" for st, code, meaning, action in rows)
    head = "| Status | Code | Meaning | What to do |\n|---|---|---|---|"
    return f"{summary}\n\n{bullets}\n\n**Errors**\n\n{head}\n{table}"


def _one_title(schema: dict, *_) -> dict:
    """Nullable fields: drop the title on the anyOf wrapper so docs label the field once (schema only)."""
    for prop in schema.get("properties", {}).values():
        if "anyOf" in prop:
            prop.pop("title", None)
    return schema


class MemberVerifyRequest(BaseModel):
    """The person to look up. Names and date of birth are required; the other fields narrow the match."""
    # Field list is a candidate set (see docs/PARTNER_API_CONTRACT.md); this docstring is partner-facing.
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra=lambda s: _one_title(s).update(examples=[{"first_name": "Juan", "last_name": "Dela Cruz",
                                                                        "date_of_birth": "1990-01-31"}]),
    )

    first_name: ShortText = Field(description="Given name, 1 to 100 characters.", examples=["Juan"])
    middle_name: OptText = Field(None, description="Middle name, up to 100 characters.", examples=["Santos"])
    last_name: ShortText = Field(description="Family name, 1 to 100 characters.", examples=["Dela Cruz"])
    suffix: Optional[Annotated[str, StringConstraints(strip_whitespace=True, max_length=10)]] = Field(
        None, description="Not supported: leave out or send null. Any value returns 422, because the "
                          "beneficiary view has no suffix column.")
    date_of_birth: Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^\d{4}-\d{2}-\d{2}$")] = Field(
        description="Date of birth as `YYYY-MM-DD`. Must be a real calendar date.", examples=["1990-01-31"])
    sex: OptCode = Field(None, description="Sex code, up to 50 characters.")
    member_id: OptCode = Field(None, description="Member ID, if you already hold one. Up to 50 characters.",
                               examples=["example-member-0001"])
    household_id: OptCode = Field(None, description="Household ID, if you hold one. Up to 50 characters.",
                                  examples=["example-household-0001"])
    lrn: Optional[Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^\d{12}$")]] = Field(
        None, description="Learner Reference Number (LRN): exactly 12 digits.", examples=["000000000000"])
    pcn: Optional[Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^[0-9-]{12,19}$")]] = Field(
        None, description="PhilSys Card Number (PCN): 12 to 19 digits or dashes.", examples=["0000-0000-0000"])

    _dob = field_validator("date_of_birth")(_real_date)


class MemberVerifyResponse(BaseModel):
    """Result of matching one person. IDs are set only when the status is Matched."""
    # Partner-only model: the legacy /VerifyMemberBene keeps schemas.VerifyResult.
    model_config = ConfigDict(extra="forbid", json_schema_extra=_one_title)

    status: Literal["Matched", "Not Found", "Multiple Match Found"] = Field(description="Match outcome.")
    tuloy_id: Optional[str] = Field(description="Tuloy ID of the matched person; null unless Matched.",
                                    examples=["4ps-1234"])
    member_id: Optional[str] = Field(description="4Ps member ID of the matched person, as text; null unless Matched.",
                                     examples=["example-member-0001"])


def _as_text(value) -> Optional[str]:
    return None if value is None else str(value)


_VERIFY_EXAMPLES = {"application/json": {"examples": {
    "Matched": {"value": {"status": "Matched", "tuloy_id": "4ps-1234", "member_id": "example-member-0001"}},
    "Not Found": {"value": {"status": "Not Found", "tuloy_id": None, "member_id": None}},
    "Multiple Match Found": {"value": {"status": "Multiple Match Found", "tuloy_id": None, "member_id": None}},
}}}


# --- R3 request bodies (ADR 2026-10-27 tuloy-enrollment-submit-grant-bound-body) ---------------------
# One strict body per agency. The caller's grant picks the model; the body never names the agency.
# Bounds reuse schemas.py (Degree, OptCode). Ones marked "provisional" wait for agency/DPO rulings.
_STRICT = ConfigDict(extra="forbid", strict=True, json_schema_extra=_one_title)
# IDs: strict, non-empty strings, kept exactly as sent. Format and length wait for agency samples (ADR);
# request size is bounded by the 1 MB body limit.
SubmitId = Annotated[str, StringConstraints(min_length=1)]
ProgramName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=150)]
# Pydantic alone also accepts a space separator, missing seconds, "+0800" and a comma fraction; none is RFC 3339.
_RFC3339 = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$")
_AWARE = TypeAdapter(AwareDatetime)


def _rfc3339(value) -> datetime:
    # PlainValidator: strict mode refuses a str for datetime once a before-validator has run, so parse here.
    if not isinstance(value, str) or not _RFC3339.fullmatch(value):
        raise ValueError("must be an RFC 3339 date-time with an offset")
    return _AWARE.validate_python(value)


Rfc3339 = Annotated[datetime, PlainValidator(_rfc3339), WithJsonSchema(
    {"type": "string", "format": "date-time", "examples": ["2026-01-31T08:30:00+08:00"]})]
_AY = re.compile(r"^(\d{4})-(\d{4})$")


class _SubmitCommon(BaseModel):
    model_config = _STRICT

    tuloy_id: SubmitId = Field(description="Tuloy ID of the verified member. A non-empty string; format and length pending agency samples.")
    mothers_maiden: None = Field(None, description="Not accepted yet: leave out or send null. Any object is rejected "
                                                   "until the DPO and data owner approve it.")
    enrollment_status: int = Field(description="Status code, an integer. The code list and any range await the program owners.")
    date_encoded: Rfc3339 = Field(description="When the agency recorded the enrollment. RFC 3339 with an offset, "
                                                    "for example `2026-01-31T08:30:00+08:00`.")
    date_updated: Optional[Rfc3339] = Field(None, description="Last agency update, RFC 3339 with an offset, or "
                                                                    "null. Not earlier than `date_encoded`.")

    @model_validator(mode="after")
    def _updated_not_before_encoded(self):
        if self.date_updated is not None and self.date_updated < self.date_encoded:
            raise ValueError("date_updated is earlier than date_encoded")
        return self


class SubmitDegree(BaseModel):
    model_config = _STRICT

    program_name: ProgramName = Field(description="Program name, 1 to 150 characters.")
    date_enrolled: date = Field(description="Date enrolled, `YYYY-MM-DD`. Must be a real calendar date.")
    program_duration: int = Field(ge=0, le=20, description="Program length in years, 0 to 20.")
    academic_year: Annotated[str, StringConstraints(max_length=20)] = Field(
        description="Academic year as `YYYY-YYYY`; the second year is the first plus one.", examples=["2026-2027"])
    year_level: int = Field(ge=0, le=10, description="Year level, 0 to 10.")
    semester: int = Field(ge=0, le=4, description="Semester, 0 to 4.")

    @field_validator("academic_year")
    @classmethod
    def _academic_year(cls, value: str) -> str:
        m = _AY.match(value)
        if not m or int(m.group(2)) != int(m.group(1)) + 1:
            raise ValueError("academic_year must be YYYY-YYYY with consecutive years")
        return value


class SubmitShortCourse(BaseModel):
    model_config = _STRICT

    program_name: ProgramName = Field(description="Course name, 1 to 150 characters.")
    start_date: date = Field(description="Start date, `YYYY-MM-DD`.")
    end_date: date = Field(description="End date, `YYYY-MM-DD`. Not before `start_date`.")
    training_status: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=50)] = Field(
        description="Training status, 1 to 50 characters. PROVISIONAL: the code list awaits TESDA.")

    @model_validator(mode="after")
    def _end_not_before_start(self):
        if self.end_date < self.start_date:
            raise ValueError("end_date is earlier than start_date")
        return self


class EnrollmentSubmitCHED(_SubmitCommon):
    """Body for an account whose grant is bound to CHED."""
    student_id: SubmitId = Field(description="CHED student ID. A non-empty string; format and length pending agency samples.")
    degree: SubmitDegree


class EnrollmentSubmitTESDA(_SubmitCommon):
    """Body for an account whose grant is bound to TESDA."""
    uli: SubmitId = Field(description="Unique Learner Identifier. A non-empty string; format and length pending agency samples.")
    short_course: SubmitShortCourse


# DSWD is an approved agency for grants but has no submit payload yet, so it has no model: denied.
SUBMIT_MODELS = {"CHED": EnrollmentSubmitCHED, "TESDA": EnrollmentSubmitTESDA}


def _inline(schema: dict) -> dict:
    """Resolve local $defs so the schema can sit inside openapi_extra without components."""
    defs = schema.pop("$defs", {})

    def walk(node):
        if isinstance(node, dict):
            if "$ref" in node and node["$ref"].startswith("#/$defs/"):
                return walk(defs[node["$ref"].rsplit("/", 1)[1]])
            return {k: walk(v) for k, v in node.items()}
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node
    return walk(schema)


SUBMIT_REQUEST_BODY = {"requestBody": {"required": True, "content": {"application/json": {"schema": {
    "oneOf": [_inline(EnrollmentSubmitCHED.model_json_schema()), _inline(EnrollmentSubmitTESDA.model_json_schema())],
    "description": "Exactly one shape applies: the one for the agency your account is bound to. "
                   "The agency is never sent in the body."}}}}}


SUBMIT_OPERATION = "partner.enrollments.submit"


def _submit_binding(principal: Principal):
    """The one approved grant this account submits under, or a 403. Never picks among several."""
    grants = principal.grants_for(SCOPE_ENROLL_SUBMIT)
    actor = f"user:{principal.id}"
    base = {"actor": actor, "scope": SCOPE_ENROLL_SUBMIT, "operation": SUBMIT_OPERATION}
    if len(grants) != 1:  # zero is already denied by require_scope; kept so the check stands alone
        # Binding reference: the sorted distinct (partner_id, agency) pairs, nothing else.
        pairs = sorted({(g.partner_id, g.agency) for g in grants})
        audit("authz.scope", "denied", reason="ambiguous_binding" if grants else "no_binding",
              bindings=[{"partner_id": p, "agency": a} for p, a in pairs], **base)
        raise ApiError(403, "FORBIDDEN", "Not permitted for this operation")
    (grant,) = grants
    if grant.agency not in APPROVED_AGENCIES or grant.partner_id != grant.agency:
        reason = "unapproved_binding"
    elif grant.agency not in SUBMIT_MODELS:
        reason = "payload_not_approved"
    else:
        return grant
    audit("authz.scope", "denied", reason=reason, partner_id=grant.partner_id, agency=grant.agency, **base)
    raise ApiError(403, "FORBIDDEN", "Not permitted for this operation")


def _require_json_media_type(request: Request) -> None:
    """415 unless the media type is application/json (parameters such as charset are allowed)."""
    media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if media_type != "application/json":
        raise ApiError(415, "UNSUPPORTED_MEDIA_TYPE", "Content-Type must be application/json")


async def _read_limited_body(request: Request) -> bytes:
    """Read the body as a stream and stop at MAX_BODY_BYTES (same 413 body as the middleware's)."""
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY_BYTES:
            raise ApiError(413, "PAYLOAD_TOO_LARGE", "Request body too large")
        chunks.append(chunk)
    return b"".join(chunks)


def _validate_submit_body(raw: bytes, model):
    """Validate the raw JSON; failures use the standard VALIDATION_ERROR envelope (no input echoed)."""
    try:
        return model.model_validate_json(raw, strict=True)
    except ValidationError as exc:
        raise RequestValidationError([
            {"loc": ("body", *err["loc"]), "msg": err["msg"], "type": err["type"]}
            for err in exc.errors(include_input=False, include_url=False, include_context=False)])


def _not_implemented(operation: str, principal: Principal, **fields) -> ApiError:
    audit(f"partner.{operation}", "not_implemented", actor=f"user:{principal.id}", **fields)
    return ApiError(501, "NOT_IMPLEMENTED", f"{operation}: contract awaits approval. Nothing was processed or stored.")


_BODY_INVALID = _invalid(["body", "date_of_birth"], "Value error, invalid date", "value_error")
_TUID_PATTERN = r"^[A-Za-z0-9-]{1,50}$"
_TXN_INVALID = _invalid(["path", "transaction_id"], _ID_MSG.format(r"^[A-Za-z0-9-]{1,64}$"), "string_pattern_mismatch")


@router.post("/members/verify", operation_id="tuloy.members.verify", summary="Verify a member", response_model=MemberVerifyResponse,
             description=_doc(
                 "Matches a person against the existing beneficiary view using the legacy verification rules. "
                 "Returns Matched, Not Found or Multiple Match Found; it does not confirm enrollment or additional eligibility.",
                 SCOPE_VERIFY,
                 ("Send the person's name and date of birth. Optional fields narrow the match.",
                  "`suffix` is not supported: any value returns 422.",
                  "Fields not listed in the request body are rejected.",
                  "`tuloy_id` and `member_id` are always strings, or null unless the status is Matched."),
                 (_TOO_LARGE_ROW,), implemented=True),
             responses={200: {"content": _VERIFY_EXAMPLES},
                        **_responses(_BODY_INVALID, {413: TOO_LARGE}, implemented=True)})
def verify_member(body: MemberVerifyRequest, principal: Principal = Depends(require_scope(SCOPE_VERIFY)),
                  db=Depends(get_ods_connection)):
    if body.suffix is not None:
        raise ApiError(422, "VALIDATION_ERROR", "Suffix filtering is not supported by the beneficiary view.")
    user = UserInput.model_validate(body.model_dump(exclude={"suffix"}))
    outcome = verify_member_match(user, principal, db, audit_action="partner.members.verify")
    # The view can return member_id as an integer; partners always get text.
    return MemberVerifyResponse(status=outcome["status"], tuloy_id=_as_text(outcome["tuloy_id"]),
                                member_id=_as_text(outcome["member_id"]))


# Placeholders declare status_code=501: they return nothing else on success, so no 200 is documented.
@router.post("/enrollments", operation_id="tuloy.enrollments.submit", summary="Submit an enrollment", status_code=501,
             description=_doc(
                 "Submits an agency-approved enrollment for a verified member. Today the request is only validated: "
                 "every valid request returns 501 and nothing is stored.",
                 SCOPE_ENROLL_SUBMIT,
                 ("Send an `Idempotency-Key` header (a UUID). It is checked for format today but not yet used: "
                  "after activation, a retry with the same key and body will return the first result instead of "
                  "creating a second enrollment, and the same key with a different body will return 409.",
                  "Send the body as `application/json` (a `charset` parameter is fine). Anything else is refused with 415.",
                  "The body is limited to 1,000,000 bytes, counted as it arrives. A larger one is refused with 413.",
                  "The agency comes from your account's grant, never from the body. Send the CHED body "
                  "(`student_id`, `degree`) or the TESDA body (`uli`, `short_course`) that matches it. "
                  "Fields of the other agency are rejected, even as null.",
                  "An account needs exactly one active submit grant. None, several or an unapproved one is refused with 403."),
                 (_TOO_LARGE_ROW,
                  (415, "UNSUPPORTED_MEDIA_TYPE", "The `Content-Type` is not `application/json` (or is missing)",
                   "Send `Content-Type: application/json`."))),
             responses=_responses(_invalid(["body", "student_id"], "Field required", "missing"),
                                  {413: TOO_LARGE, 415: UNSUPPORTED_MEDIA}),
             openapi_extra=SUBMIT_REQUEST_BODY)
async def submit_enrollment(request: Request, idempotency_key: SubmitIdempotencyKey,
                            principal: Principal = Depends(require_scope(
                                SCOPE_ENROLL_SUBMIT,
                                denial_fields={"operation": SUBMIT_OPERATION, "reason": "no_grant"}))):
    grant = _submit_binding(principal)  # decided before the body is looked at
    _require_json_media_type(request)
    _validate_submit_body(await _read_limited_body(request), SUBMIT_MODELS[grant.agency])
    raise _not_implemented("enrollments.submit", principal, scope=grant.scope,
                           partner_id=grant.partner_id, agency=grant.agency)


@router.get("/enrollments", operation_id="tuloy.enrollments.list", summary="List enrollments", status_code=501,
            description=_doc(
                "Lists the enrollments you submitted, a page at a time.",
                SCOPE_ENROLL_READ,
                ("Use `limit` for the page size and `after` to continue from the previous page.",
                 "Use `tuloy_id` to list only one person's enrollments.")),
            responses=_responses(_invalid(["query", "tuloy_id"], _ID_MSG.format(_TUID_PATTERN), "string_pattern_mismatch")))
def list_enrollments(limit: Limit = 50, after: After = None, tuloy_id: TuloyIdQuery = None,
                     principal: Principal = Depends(require_scope(SCOPE_ENROLL_READ))):
    raise _not_implemented("enrollments.list", principal)


@router.get("/enrollments/{transaction_id}", operation_id="tuloy.enrollments.get", summary="Get an enrollment",
            status_code=501,
            description=_doc(
                "Returns one enrollment that you submitted.",
                SCOPE_ENROLL_READ,
                ("You can only read your own enrollments. After activation, an enrollment that does not exist "
                 "or belongs to another partner will return 404; the two will look the same.",)),
            responses=_responses(_TXN_INVALID))
def get_enrollment(transaction_id: TransactionId, principal: Principal = Depends(require_scope(SCOPE_ENROLL_READ))):
    raise _not_implemented("enrollments.get", principal)


@router.post("/enrollments/{transaction_id}/events", operation_id="tuloy.enrollments.record_event",
             summary="Record an enrollment event", status_code=501,
             description=_doc(
                 "Records a change to one of your enrollments.",
                 SCOPE_ENROLL_UPDATE,
                 ("Send an `Idempotency-Key` header so a retry is not recorded twice.",
                  "After activation, an enrollment that is not yours will return 404, and a reused key with a "
                  "different body, or a changed enrollment, will return 409."),
                 (_TOO_LARGE_ROW,)),
             responses=_responses(_TXN_INVALID, {413: TOO_LARGE}))
def record_enrollment_event(transaction_id: TransactionId, idempotency_key: IdempotencyKey,
                            principal: Principal = Depends(require_scope(SCOPE_ENROLL_UPDATE))):
    raise _not_implemented("enrollments.record_event", principal)


@router.get("/members/{tuloy_id}/enrollment-history", operation_id="tuloy.enrollment_history.list",
            summary="List a member's enrollment history", status_code=501,
            description=_doc(
                "Lists the enrollment history of one person, identified by Tuloy ID, as visible to you, a page at a time.",
                SCOPE_HISTORY_READ,
                ("Use `limit` for the page size and `after` to continue from the previous page.",
                 "After activation, a person who does not exist or is not visible to you will return 404.")),
            responses=_responses(_invalid(["path", "tuloy_id"], _ID_MSG.format(_TUID_PATTERN), "string_pattern_mismatch")))
def enrollment_history(tuloy_id: TuloyIdPath, limit: Limit = 50, after: After = None,
                       principal: Principal = Depends(require_scope(SCOPE_HISTORY_READ))):
    raise _not_implemented("enrollment_history.list", principal)


PARTNER_OPERATIONS = (
    ("POST", f"{PREFIX}/members/verify"),
    ("POST", f"{PREFIX}/enrollments"),
    ("GET", f"{PREFIX}/enrollments"),
    ("GET", f"{PREFIX}/enrollments/{{transaction_id}}"),
    ("POST", f"{PREFIX}/enrollments/{{transaction_id}}/events"),
    ("GET", f"{PREFIX}/members/{{tuloy_id}}/enrollment-history"),
)
