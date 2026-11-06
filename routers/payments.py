"""Payment computation and generation routes using the project's mysql.connector connections.

Mount this router directly in main.py. Routes remain /api/payments/....
No SQLAlchemy dependency is used.
"""
from __future__ import annotations

import csv
import io
import re
import uuid
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from mysql.connector import Error

from database import get_app_connection, get_ods_connection, query_failed
from errors import ApiError
from observability import audit, request_id_var
from security import Principal, get_beneficiary_principal
# Payroll eligibility is the shared downstream contract (docs/bcmd-napa-workflow.md): PASS in the
# batch's latest usable DQA run, no blocking recorded payment, not excluded by a duplicate-selection
# decision. The status saved with the batch snapshot never makes a member payable.
import bcmd_eligibility as eligibility
import bcmd_payment_history as history
from bcmd_export import refusal as napa_refusal


def require_payment_user(request: Request, principal: Principal = Depends(get_beneficiary_principal)) -> Principal:
    """Payments are for authenticated accounts with active DSWD/DSWD or TEST/TEST grants.

    The selected validator reloads account state and recognized, unexpired, unrevoked grants on
    every request. Never infer affiliation from a username or from JWT-supplied agencies.
    This payment-only policy does not change other routes' scope/agency rules.
    """
    # Keep the legacy service routes separate from the staff web API.
    if principal.kind == "staff" and not request.url.path.startswith("/v1/api/tuloy/staff/payments/"):
        raise ApiError(401, "UNAUTHENTICATED", "Not authenticated")
    allowed = any(g.agency in {"DSWD", "TEST"} and g.partner_id == g.agency
                  for g in principal.grants)
    audit("authz.payments", "allowed" if allowed else "denied", actor=principal.actor)
    if not allowed:
        raise ApiError(403, "FORBIDDEN", "Not permitted for this operation")
    return principal


router = APIRouter(tags=["Payments"], dependencies=[Depends(require_payment_user)])

HOUSEHOLD_CAP = 3
TRACK_BY_ID = {0: "CHED", 1: "TESDA"}
TRACK_ID_BY_NAME = {v: k for k, v in TRACK_BY_ID.items()}

DQA_PASS = "PASS"
DQA_PENDING = "PENDING REVIEW"
DQA_STATUSES = {DQA_PASS, DQA_PENDING}

def normalize_dqa_status(value):
    if value is None:
        return ""
    return str(value).strip().upper()

CATEGORY_BY_TRACK = {"CHED": "TULOY KOLEHIYO", "TESDA": "TULOY TRABAHO"}
PAYMENT_STATUS_FOR_PAYROLL = 1
DEFAULT_PAYMENT_MODE_ID = 45
SEX_MALE_ID, SEX_FEMALE_ID = 1, 2
# Fixed rates; no rate-library table dependency.
DEFAULT_RATES = {"CHED": Decimal("5000.00"), "TESDA": Decimal("5000.00")}
BATCH_SIZE = 1000

class PaymentComputeRequest(BaseModel):
    batch_id: str

class PaymentGenerateRequest(PaymentComputeRequest):
    academic_year_term: int
    assistance_type: int
    payroll_type: Optional[int] = None
    payment_mode_id: Optional[int] = None


def to_int(value):
    if value is None:
        return None
    digits = re.sub(r"\D", "", str(value))
    return int(digits) if digits else None



def amount_for(track_id) -> Decimal:
    try:
        track_id = int(track_id)
    except (TypeError, ValueError):
        return Decimal("0.00")

    track = TRACK_BY_ID.get(track_id)

    if track is None:
        return Decimal("0.00")

    rate = DEFAULT_RATES.get(track)

    if rate is None:
        return Decimal("0.00")

    return Decimal(str(rate)).quantize(
        Decimal("0.01"),
        rounding=ROUND_HALF_UP
    )

def _dict_rows(cursor):
    return cursor.fetchall()


def _table_columns(db, table_name: str) -> dict:
    """Return actual column metadata for a table in the current schema."""
    cur = db.cursor(dictionary=True)
    try:
        cur.execute(
            "SELECT COLUMN_NAME, IS_NULLABLE, COLUMN_TYPE, COLUMN_DEFAULT "
            "FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s",
            (table_name,),
        )
        return {r["COLUMN_NAME"]: r for r in cur.fetchall()}
    except Error as exc:
        raise query_failed(exc, "payments_schema_check_failed")
    finally:
        cur.close()


def _where_filters(search: str, status: str, track: str, alias: str = "b"):
    clauses, params = [], []
    if search and search.strip():
        term = f"%{search.strip()}%"
        searchable = [
            "given_name", "last_name", "fourps_id", "fourps_member_id", "student_id",
            "enrolled_tertiary", "enrolled_course_program", "complete_program_name",
            "lrn", "philsys_id",
        ]
        clauses.append("(" + " OR ".join(f"CAST({alias}.`{c}` AS CHAR) LIKE %s" for c in searchable) + ")")
        params.extend([term] * len(searchable))
    if status in STATUS_ID_BY_LABEL:
        clauses.append(f"{alias}.`enrollment_status_id` = %s")
        params.append(STATUS_ID_BY_LABEL[status])
    if track in TRACK_ID_BY_NAME:
        clauses.append(f"{alias}.`target_track_id` = %s")
        params.append(TRACK_ID_BY_NAME[track])
    return (" WHERE " + " AND ".join(clauses) if clauses else ""), params


def _payable_cte(where_sql: str = "", include_all_statuses: bool = False):
    """Rank matched beneficiaries within each household; NULL household IDs rank separately."""
    status_filter = "" if include_all_statuses else "WHERE b.enrollment_status_id = 1"
    return f"""
        SELECT b.*,
               ROW_NUMBER() OVER (
                   PARTITION BY COALESCE(NULLIF(b.fourps_id, ''), CONCAT('__NO_HH__', b.id))
                   ORDER BY b.id
               ) AS household_rank
        FROM tuloy_beneficiary_tbl b
        {status_filter}
    """


def _execute_all(db, sql: str, params=()):
    cur = db.cursor(dictionary=True)
    try:
        cur.execute(sql, tuple(params))
        return cur.fetchall()
    except Error as exc:
        raise query_failed(exc, "payments_query_failed")
    finally:
        cur.close()


def _base_select(where_sql: str = "", include_all_statuses: bool = True):
    status_sql = "" if include_all_statuses else "WHERE b.enrollment_status_id = 1"
    if where_sql:
        status_sql += (" AND " if status_sql else "WHERE ") + where_sql.removeprefix(" WHERE ")
    return f"SELECT b.* FROM tuloy_beneficiary_tbl b {status_sql}"


def _fetch_filtered(db, search: str, status: str, track: str, *, matched_only=False):
    where, params = _where_filters(search, status, track)
    sql = _base_select(where, include_all_statuses=not matched_only)
    return _execute_all(db, sql, params)


def _household_ranks(rows):
    groups = defaultdict(list)
    for row in rows:
        hh = row.get("fourps_id")
        key = str(hh) if hh not in (None, "") else f"__NO_HH__{row['id']}"
        groups[key].append(row)
    rank = {}
    for members in groups.values():
        members.sort(key=lambda x: x["id"])
        for i, row in enumerate(members, start=1):
            rank[row["id"]] = i
    return rank, groups


def _json_safe(row):
    out = {}
    for key, value in row.items():
        if isinstance(value, (date, datetime)):
            out[key] = value.isoformat()
        elif isinstance(value, Decimal):
            out[key] = float(value)
        elif isinstance(value, bytes):
            out[key] = value.hex()
        else:
            out[key] = value
    return out


# Safe notes for payment-history findings (bcmd_payment_history). Pending is never called paid.
PAYMENT_NOTES = {
    "ALREADY_PAID_TK": "Already paid: TK is paid once",
    "ALREADY_PAID_TT": "Already paid: TT is paid once",
    "TA_BLOCKED_BY_TT_PAYMENT": "Not payable: a TT payment is recorded",
    "ALREADY_PAID_SAME_PERIOD": "Already paid for this academic year and semester",
    "PAYMENT_UNMAPPED": "Held: a recorded payment could not be matched to a category and period",
    "PAYMENT_PENDING": "Payment recorded, not yet confirmed paid",
}


def _payment_annotation(row, rank: int, months: int):
    amount = amount_for(row.get("target_track_id"))
    dqa_status = normalize_dqa_status(row.get("dqa_status"))
    codes = [f["code"] for f in row.get("payment_findings") or []]

    if dqa_status == DQA_PASS and codes:
        code = next((c for c in PAYMENT_NOTES if c in codes), codes[0])
        pay = {
            "payable": 0,
            "potential": 0,
            "state": "recorded" if code == "PAYMENT_PENDING" else "locked",
            "note": PAYMENT_NOTES.get(code, "Not payable: payment history")
        }

    elif dqa_status == DQA_PASS and rank <= HOUSEHOLD_CAP:
        pay = {
            "payable": float(amount),
            "potential": float(amount),
            "state": "payable",
            "note": f"₱{amount / months:,.2f} × {months} mo"
        }

    elif dqa_status == DQA_PASS:
        pay = {
            "payable": 0,
            "potential": 0,
            "state": "capped",
            "note": f"Over household cap ({HOUSEHOLD_CAP})"
        }

    elif dqa_status == DQA_PENDING:
        pay = {
            "payable": 0,
            "potential": float(amount),
            "state": "hold",
            "note": "Pending DQA review"
        }

    else:
        pay = {
            "payable": 0,
            "potential": 0,
            "state": "locked",
            "note": "Not eligible for payment"
        }

    return pay

def _filter_batch_beneficiaries(rows, search="", status="", track=""):
    filtered = []

    selected_status = normalize_dqa_status(status)
    track_id = TRACK_ID_BY_NAME.get(track)

    term = search.strip().lower() if search else ""

    for row in rows:
        row_status = normalize_dqa_status(row.get("dqa_status"))

        if selected_status in DQA_STATUSES and row_status != selected_status:
            continue

        if track_id is not None:
            if row.get("target_track_id") != track_id:
                continue

        if term:
            searchable = [
                row.get("id"),
                row.get("fourps_member_id"),
                row.get("fourps_id"),
                row.get("studentName"),
            ]

            if not any(
                term in str(value).lower()
                for value in searchable
                if value is not None
            ):
                continue

        filtered.append(row)

    return filtered

@router.get("/api/payments/rates")
@router.get("/v1/api/tuloy/staff/payments/rates", include_in_schema=False)
def payment_rates():
    return {k: float(v) for k, v in DEFAULT_RATES.items()}

@router.get("/api/payments/batches")
@router.get("/v1/api/tuloy/staff/payments/batches", include_in_schema=False)
def list_payment_batches(
    db=Depends(get_ods_connection),
):
    sql = """
        SELECT
            name AS batch_name,
            public_id AS batch_id
        FROM bcmd_batch b
        WHERE public_id IS NOT NULL AND b.status<>'PURGED'
          AND EXISTS (SELECT 1 FROM bcmd_dqa_run r WHERE r.batch_id=b.id AND r.status='COMPLETED'
                      AND r.run_no=(SELECT MAX(r2.run_no) FROM bcmd_dqa_run r2 WHERE r2.batch_id=b.id))
        ORDER BY b.id DESC
    """
    rows = _execute_all(db, sql)

    return {"items": rows}

# Payroll status of a member: PASS, PENDING REVIEW (held), FAIL, EXCLUDED or DQA REQUIRED.
RUN_STATUS = {"PASS": DQA_PASS, "REVIEW": DQA_PENDING, "FAIL": "FAIL", "EXCLUDED": "EXCLUDED"}
DQA_REQUIRED = "DQA REQUIRED"


def _payroll_rows(result):
    """Rows in the shape the payment routes already use, from the shared eligibility result."""
    batch = result["batch"]
    rows = []
    for m in result["members"]:
        names = (m["first_name"], m["middle_name"], m["last_name"])
        source = (m.get("source_agency") or batch["source_agency"] or "").strip().upper()
        rows.append({
            # Same value as the previous SQL CONCAT (NULL when any part is NULL).
            "studentName": None if None in names else " ".join(names),
            "batch_id": batch["id"],
            "category": batch["category"],
            "id": m["id"],
            "line_no": m["line_no"],
            "fourps_member_id": m["member_ref"],
            "sex_id": m["sex"],
            "target_track_id": TRACK_ID_BY_NAME.get(source),
            "dqa_status": DQA_REQUIRED if result["reasons"] else RUN_STATUS.get(m["outcome"], DQA_REQUIRED),
            "fourps_id": m["hhid"],
            "payment_findings": m["payment_findings"],
            "eligible": eligibility.eligible(result, m),
        })
    return rows


def _eligibility(db, batch_id: str):
    """Shared eligibility for one batch; None when it does not exist. Fails closed."""
    cur = db.cursor(dictionary=True)
    try:
        return eligibility.evaluate(cur, batch_id)
    except history.Unavailable:
        raise history.unavailable() from None
    except Error as exc:
        raise query_failed(exc, "payments_query_failed")
    finally:
        cur.close()


def _eligibility_summary(result):
    run = result["run"] if result else None
    members = result["members"] if result else []
    return {
        "blocking_reasons": list(result["reasons"]) if result else ["NOT_FOUND"],
        "dqa_run_id": run["public_id"] if run and result and not result["reasons"] else None,
        "payment_findings": eligibility.finding_counts(members),
        "payment_blocked_count": sum(1 for m in members if m["payment_findings"]),
    }


def _fetch_batch_beneficiaries(db, batch_id: str):
    result = _eligibility(db, batch_id)
    return (_payroll_rows(result) if result else []), result

@router.get("/api/payments/beneficiaries")
@router.get("/v1/api/tuloy/staff/payments/beneficiaries", include_in_schema=False)
def list_beneficiaries(
    batch_id: str = Query(...),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    search: str = "",
    status: str = "",
    track: str = "",
    db=Depends(get_ods_connection),
):
    # Fixed computation period
    FIXED_MONTHS = 1

    rows, result = _fetch_batch_beneficiaries(db, batch_id)

    # Counts only: no search text or beneficiary values in logs.
    print(
        f"[PAYMENTS BENEFICIARIES] "
        f"batch_id={batch_id}, fetched_rows={len(rows)}, "
        f"page={page}, page_size={page_size}"
    )

    if not rows:
        return {
            "batch_id": batch_id,
            "items": [],
            "total": 0,
            "page": page,
            "page_size": page_size,
            "eligibility": _eligibility_summary(result),
        }

    # Normalize fields
    for row in rows:
        row["id"] = to_int(row.get("id"))
        row["target_track_id"] = to_int(row.get("target_track_id"))
        row["dqa_status"] = normalize_dqa_status(
            row.get("dqa_status")
        )

    # Rank all eligible beneficiaries before filtering: current DQA PASS, no blocking payment.
    all_matched = [
        row for row in rows
        if row.get("eligible")
    ]

    ranks, groups = _household_ranks(all_matched)

    # Apply filters
    filtered_rows = _filter_batch_beneficiaries(
        rows,
        search=search,
        status=status,
        track=track,
    )

    print(
        f"[PAYMENTS BENEFICIARIES] "
        f"after_filter={len(filtered_rows)}"
    )

    # Sort and paginate
    filtered_rows.sort(
        key=lambda r: r.get("id") or 0
    )

    total = len(filtered_rows)
    start = (page - 1) * page_size
    page_rows = filtered_rows[start:start + page_size]

    items = []

    for row in page_rows:
        item = _json_safe({k: v for k, v in row.items() if k != "eligible"})

        # Always calculate per-row payment using 1 month
        rank = ranks.get(row["id"], 1)

        item["payment"] = _payment_annotation(
            row,
            rank,
            FIXED_MONTHS,
        )

        household_id = row.get("fourps_id")

        household_key = (
            str(household_id)
            if household_id not in (None, "")
            else f"__NO_HH__{row['id']}"
        )

        household_members = groups.get(household_key, [])

        item["household_total"] = float(
            sum(
                (
                    amount_for(member.get("target_track_id"))
                    for member in household_members[:HOUSEHOLD_CAP]
                ),
                Decimal("0.00"),
            )
        )

        items.append(item)

    print(
        f"[PAYMENTS BENEFICIARIES] "
        f"returned_items={len(items)}, total={total}"
    )

    return {
        "batch_id": batch_id,
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "eligibility": _eligibility_summary(result),
    }

@router.post("/api/payments/compute")
@router.post("/v1/api/tuloy/staff/payments/compute", include_in_schema=False)
def compute_payments(
    body: PaymentComputeRequest,
    db=Depends(get_ods_connection)
):
    rows, result = _fetch_batch_beneficiaries(db, body.batch_id)

    if not rows:
        raise HTTPException(
            status_code=404,
            detail="No beneficiary records found for the specified batch."
        )

    # Normalize beneficiary data
    for row in rows:
        row["dqa_status"] = normalize_dqa_status(
            row.get("dqa_status")
        )
        row["target_track_id"] = to_int(
            row.get("target_track_id")
        )

    # DQA classification: current run PASS with no blocking recorded payment.
    matched = [
        row for row in rows
        if row.get("eligible")
    ]

    held = [
        row for row in rows
        if row.get("dqa_status") == DQA_PENDING
    ]

    other_status = [
        row for row in rows
        if row.get("dqa_status") not in DQA_STATUSES
    ]
    summary = _eligibility_summary(result)

    # Apply household cap to matched beneficiaries
    ranks, _ = _household_ranks(matched)

    payable = [
        row for row in matched
        if ranks.get(row["id"], 1) <= HOUSEHOLD_CAP
    ]

    # Fixed computation: 1 month
    payable_total = sum(
        (
            amount_for(row.get("target_track_id"))
            for row in payable
        ),
        Decimal("0.00")
    )

    hold_total = sum(
        (
            amount_for(row.get("target_track_id"))
            for row in held
        ),
        Decimal("0.00")
    )

    return {
        "batch_id": body.batch_id,
        "payable": float(payable_total),
        "on_hold": float(hold_total),
        "payable_count": len(payable),
        "capped_count": len(matched) - len(payable),
        "pending_count": len(held),
        "not_matched_count": len(other_status),
        "payment_blocked_count": summary["payment_blocked_count"],
        "payment_findings": summary["payment_findings"],
        "blocking_reasons": summary["blocking_reasons"],
        "dqa_run_id": summary["dqa_run_id"],
        "months": 1,
        "rates": {
            key: float(value)
            for key, value in DEFAULT_RATES.items()
        }
    }

import uuid


@router.post("/api/payments/generate")
@router.post("/v1/api/tuloy/staff/payments/generate", include_in_schema=False)
def generate_payments(
    body: PaymentGenerateRequest,
    db=Depends(get_app_connection)
):
    """Generate payment transactions for PASS beneficiaries only.
    Fixed computation period: 1 month.
    """

    import uuid
    import traceback

    cur = None
    locked = False
    transaction_started = False

    lock_name = (
        f"tuloy_pay_{body.academic_year_term}_{body.assistance_type}"
    )

    def safe_rollback():
        try:
            db.rollback()
        except Exception:
            traceback.print_exc()

    try:
        cur = db.cursor(dictionary=True)

        # --------------------------------------------------
        # 1. Acquire advisory lock.
        # --------------------------------------------------
        cur.execute(
            "SELECT GET_LOCK(%s, 10) AS acquired",
            (lock_name,)
        )

        lock_row = cur.fetchone() or {}

        try:
            locked = int(lock_row.get("acquired") or 0) == 1
        except (TypeError, ValueError):
            locked = False

        if not locked:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Another payment-generation request is running "
                    "for this term/assistance type. Try again shortly."
                )
            )

        # GET_LOCK is session-level and survives COMMIT.
        # Clear any implicit transaction created by the lock query.
        db.commit()

        # --------------------------------------------------
        # 2. Validate destination table BEFORE transaction.
        # --------------------------------------------------
        txn_cols = _table_columns(db, "payment_transaction")

        if not txn_cols:
            raise HTTPException(
                status_code=500,
                detail="payment_transaction table was not found."
            )

        required_cols = {
            "batch_id",
            "member_id",
            "academic_year_term",
            "assistance_type",
            "amount",
            "status",
            "tuloy_payroll_type",
            "payment_mode_id",
            "sex",
        }

        missing_cols = required_cols - set(txn_cols)

        if missing_cols:
            raise HTTPException(
                status_code=500,
                detail={
                    "message": "Missing required payment_transaction columns.",
                    "missing_columns": sorted(missing_cols)
                }
            )

        # _table_columns may execute metadata queries.
        # End any implicit transaction before explicit transaction.
        db.commit()

        # --------------------------------------------------
        # 3. Start one explicit READ COMMITTED transaction. After the
        #    beneficiary locks below, each read sees payments that
        #    other generations committed while this one waited.
        # --------------------------------------------------
        db.start_transaction(isolation_level="READ COMMITTED")
        transaction_started = True

        # --------------------------------------------------
        # 4. Prevent duplicate generation.
        # --------------------------------------------------
        cur.execute(
            """
            SELECT COUNT(*) AS n
            FROM payment_transaction
            WHERE academic_year_term = %s
              AND assistance_type = %s
            """,
            (
                body.academic_year_term,
                body.assistance_type
            )
        )

        already = int((cur.fetchone() or {}).get("n", 0))

        if already:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"May {already} na payment transaction(s) "
                    "para sa term at assistance type na ito. "
                    "Walang bagong rows na isinulat."
                )
            )

        # --------------------------------------------------
        # 5. Shared eligibility. The batch row is read FOR SHARE, so
        #    DQA runs, review decisions and exports wait for this
        #    transaction. Only current-run PASS members can be paid.
        # --------------------------------------------------
        result = eligibility.evaluate(cur, body.batch_id, lock=True, check_history=False)

        if not result or not result["members"]:
            raise HTTPException(
                status_code=404,
                detail=(
                    "No beneficiary records found "
                    "for the specified batch."
                )
            )

        if result["reasons"]:
            raise _payroll_refusal(result["reasons"])

        # --------------------------------------------------
        # 6. Serialize per beneficiary, then re-read payment history.
        #    Key rows are locked in key order and held until commit or
        #    rollback; a concurrent generation for the same member
        #    (any term or assistance type) waits here, then sees this
        #    one's committed payments. The term GET_LOCK above does not
        #    cover a different term or assistance type.
        # --------------------------------------------------
        passed = [m for m in result["members"] if m["outcome"] == "PASS"]
        history.lock_members(cur, [m["member_ref"] for m in passed])
        eligibility.apply_history(cur, result)

        # The same member twice in this request: the later line is
        # checked against the earlier one as a recorded payment.
        category = result["batch"]["category"]
        in_request = defaultdict(list)
        for m in passed:
            if not m["payment_findings"]:
                m["payment_findings"] = history.findings(
                    category, m["academic_year"], m["semester"],
                    in_request[m["member_ref"]])
            if not m["payment_findings"]:
                in_request[m["member_ref"]].append(dict(
                    ref=f"request:line:{m['line_no']}", state="PENDING",
                    category=category, mapped=True,
                    period=(m["academic_year"] or "", int(m["semester"] or 0))))

        rows = _payroll_rows(result)

        for row in rows:
            row["target_track_id"] = to_int(row.get("target_track_id"))
            row["id"] = to_int(row.get("id"))
            row["actual_batch_id"] = result["batch"]["id"]

        # Eligible beneficiaries only.
        matched = [
            row for row in rows
            if row.get("eligible")
        ]

        # Household ranking and cap.
        ranks, _ = _household_ranks(matched)

        payable = [
            row for row in matched
            if ranks.get(row["id"], 1) <= HOUSEHOLD_CAP
        ]

        # Audit why PASS members were not paid: finding codes, payment
        # references and line numbers only, never beneficiary values.
        blocked = [row for row in rows if row["payment_findings"]]
        if blocked:
            cur.executemany(
                "INSERT INTO bcmd_payment_exclusion "
                "(batch_id, stage, request_id, line_no, finding_code, payment_ref) "
                "VALUES (%s, 'GENERATION', %s, %s, %s, %s)",
                [(result["batch"]["id"], request_id_var.get(), row["line_no"],
                  f["code"], f["payment_ref"])
                 for row in blocked for f in row["payment_findings"]])
        finding_counts = eligibility.finding_counts(result["members"])

        if not payable:
            # Keep the exclusion audit; nothing is paid.
            db.commit()
            transaction_started = False
            raise HTTPException(
                status_code=400,
                detail=(
                    "Walang payable beneficiary na may "
                    "DQA status na PASS."
                    + (f" Hindi kasama ang {len(blocked)} dahil sa payment history."
                       if blocked else "")
                )
            )

        # --------------------------------------------------
        # 7. Prepare payment insert.
        # --------------------------------------------------
        insert_sql = """
            INSERT INTO payment_transaction
            (
                batch_id,
                member_id,
                academic_year_term,
                assistance_type,
                amount,
                status,
                tuloy_payroll_type,
                payment_mode_id,
                sex
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """

        created = 0
        total = Decimal("0.00")
        batch = []

        for row in payable:

            # The identity payment history reads (bcmd_payment_history).
            member_id = history.payment_id(
                row.get("fourps_member_id")
            )

            if member_id is None:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Invalid member_ref for beneficiary "
                        f"record ID {row.get('id')}."
                    )
                )

            # Fixed 1-month amount.
            amount = amount_for(
                row.get("target_track_id")
            )

            if amount <= 0:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Invalid payment amount for beneficiary "
                        f"record ID {row.get('id')}. "
                        "Please check source_agency and track mapping."
                    )
                )

            batch.append((
                row.get("actual_batch_id"),  # Actual bcmd_batch.id
                member_id,
                body.academic_year_term,
                body.assistance_type,
                amount,
                PAYMENT_STATUS_FOR_PAYROLL,
                1 if body.payroll_type is None else body.payroll_type,
                DEFAULT_PAYMENT_MODE_ID if body.payment_mode_id is None else body.payment_mode_id,
                row.get("sex_id")
            ))

            total += amount

            if len(batch) >= BATCH_SIZE:
                cur.executemany(insert_sql, batch)
                created += cur.rowcount
                batch.clear()

        # Insert remaining records.
        if batch:
            cur.executemany(insert_sql, batch)
            created += cur.rowcount

        if created == 0:
            raise HTTPException(
                status_code=400,
                detail="No valid payment transactions were generated."
            )

        # --------------------------------------------------
        # 8. Commit all generated transactions.
        # --------------------------------------------------
        db.commit()
        transaction_started = False

        return {
            "batch_id": body.batch_id,
            "created": created,
            "skipped_existing": 0,
            "total_amount": str(
                total.quantize(Decimal("0.01"))
            ),
            "months": 1,
            "dqa_status": DQA_PASS,
            "pending_review_count": sum(
                1 for row in rows
                if row.get("dqa_status") == DQA_PENDING
            ),
            "excluded_count": len(rows) - len(payable),
            "payment_blocked_count": len(blocked),
            "payment_findings": finding_counts,
        }

    except HTTPException:
        if transaction_started:
            safe_rollback()
            transaction_started = False
        raise

    except history.Unavailable:
        safe_rollback()
        transaction_started = False
        raise history.unavailable() from None

    except Error as exc:
        safe_rollback()
        transaction_started = False

        # Error text can carry SQL values; never return or print it.
        if exc.errno in (1205, 1213):  # lock wait timeout, deadlock
            raise ApiError(
                409, "PAYMENT_GENERATION_BUSY",
                "Another payment generation holds these beneficiaries; try again"
            ) from None
        raise query_failed(exc, "payment_generation_failed") from None

    except Exception:
        safe_rollback()
        transaction_started = False

        raise ApiError(500, "PAYMENT_GENERATION_FAILED", "Payment generation failed") from None

    finally:
        if cur:
            try:
                cur.close()
            except Exception:
                pass

        # --------------------------------------------------
        # 9. Release advisory lock.
        # --------------------------------------------------
        if locked:
            try:
                rel = db.cursor()
                rel.execute(
                    "SELECT RELEASE_LOCK(%s)",
                    (lock_name,)
                )
                rel.fetchone()
                rel.close()

                # Clear any implicit transaction from RELEASE_LOCK.
                db.commit()

            except Exception:
                traceback.print_exc()

def _payroll_refusal(reasons):
    """Generation needs the same usable DQA run as NAPA."""
    if "BATCH_PURGED" in reasons:
        return ApiError(410, "BATCH_PURGED", "Batch has been purged")
    if "DQA_RESULTS_INCONSISTENT" in reasons:
        return ApiError(409, "NAPA_BATCH_BLOCKED", "Stored DQA results are inconsistent with the run")
    return napa_refusal(reasons)


# Export helpers use only confirmed beneficiary columns.
# Geographic names/account details not present in tuloy_beneficiary_tbl
# are left blank rather than inferred from PSGC codes.

def _export_rows(db, batch_id, search, status, track):
    rows, _ = _fetch_batch_beneficiaries(db, batch_id)

    if not rows:
        return []

    for row in rows:
        row["id"] = to_int(row.get("id"))
        row["target_track_id"] = to_int(
            row.get("target_track_id")
        )
        row["dqa_status"] = normalize_dqa_status(
            row.get("dqa_status")
        )

    # Eligible beneficiaries only: current run PASS, no blocking payment.
    matched = [
        row for row in rows
        if row.get("eligible")
    ]

    # Compute household ranks before applying display filters.
    ranks, _ = _household_ranks(matched)

    filtered = _filter_batch_beneficiaries(
        matched,
        search,
        status,
        track
    )

    filtered.sort(key=lambda r: r["id"])

    return [
        row for row in filtered
        if ranks.get(row["id"], 1) <= HOUSEHOLD_CAP
    ]


def _dmy(v):
    if not v:
        return ""

    if isinstance(v, (date, datetime)):
        return v.strftime("%d-%m-%Y")

    s = str(v)
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", s)

    return (
        f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
        if m else s
    )


def _sex_label(v):
    return (
        "Male" if v == SEX_MALE_ID
        else "Female" if v == SEX_FEMALE_ID
        else ""
    )


CSV_HEADERS = [
    "Processing Month",
    "YEAR",
    "REGION",
    "PROVINCE",
    "MUNICIPALITY",
    "BARANGAY",
    "BARANGAY PSGC",
    "LAST NAME",
    "FIRST NAME",
    "MIDDLE NAME",
    "Date of Birth",
    "HOUSEHOLD ID",
    "Member ID",
    "GENDER",
    "PAYMENT MODE CLASSIFICATION",
    "TRANSACTION ACCOUNT GROUP",
    "TRANSACTION ACCOUNT TYPE",
    "ACCOUNT NUMBER",
    "PAYROLL DATE",
    "FINANCE APPROVAL",
    "Category",
    "VALIDATED SOURCE OF ORIGIN",
    "Academic Year",
    "Semester",
    "Start Date",
    "End Date",
    "GROSS_AMOUNT",
]


def _csv_record(r, now):
    track_name = TRACK_BY_ID.get(
        r.get("target_track_id"), ""
    )

    amount = amount_for(r.get("target_track_id"))

    return [
        now.strftime("%B"),
        now.year,
        r.get("region_psgc") or "",
        r.get("province_psgc") or "",
        r.get("city_municipality_psgc") or "",
        r.get("street_and_barangay") or "",
        "",
        r.get("last_name") or "",
        r.get("given_name") or "",
        r.get("middle_name") or "",
        _dmy(r.get("birthdate")),
        r.get("fourps_id") or "",
        r.get("fourps_member_id") or "",
        _sex_label(r.get("sex_id")),
        "Transaction Accounts",
        "",
        "",
        "",
        now.strftime("%d-%m-%Y"),
        "",
        CATEGORY_BY_TRACK.get(track_name, ""),
        track_name,
        "",
        "",
        "",
        "",
        str(amount)
    ]


# ============================================================
# CSV EXPORT
# ============================================================

@router.get("/api/payments/export/csv")
@router.get(
    "/v1/api/tuloy/staff/payments/export/csv",
    include_in_schema=False
)
def export_csv(
    batch_id: str = Query(...),
    search: str = "",
    status: str = "",
    track: str = "",
    db=Depends(get_ods_connection)
):
    rows = _export_rows(
        db,
        batch_id,
        search,
        status,
        track
    )

    now = datetime.now()
    buf = io.StringIO()
    buf.write("\ufeff")

    writer = csv.writer(
        buf,
        quoting=csv.QUOTE_ALL
    )

    writer.writerow(CSV_HEADERS)

    for row in rows:
        writer.writerow(_csv_record(row, now))

    data = buf.getvalue().encode("utf-8")

    return StreamingResponse(
        io.BytesIO(data),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition":
                f'attachment; filename="Payroll_Files_{now:%Y-%m-%d}.csv"'
        }
    )


# ============================================================
# EXCEL SUMMARY EXPORT
# No months parameter required.
# ============================================================

@router.get("/api/payments/export/summary")
@router.get(
    "/v1/api/tuloy/staff/payments/export/summary",
    include_in_schema=False
)
def export_summary(
    batch_id: str = Query(...),
    search: str = "",
    status: str = "",
    track: str = "",
    db=Depends(get_ods_connection)
):
    try:
        from openpyxl import Workbook
    except ImportError:
        raise HTTPException(
            status_code=500,
            detail=(
                "Excel export dependency missing. "
                "Install openpyxl in the API environment."
            )
        )

    rows = _export_rows(
        db,
        batch_id,
        search,
        status,
        track
    )

    groups = defaultdict(list)

    for r in rows:
        key = (
            r.get("region_psgc") or "",
            r.get("province_psgc") or "",
            r.get("city_municipality_psgc") or "",
            r.get("target_track_id")
        )
        groups[key].append(r)

    now = datetime.now()

    wb = Workbook(write_only=True)
    ws = wb.create_sheet("1. Approved Payroll Summary")

    ws.append([
        "Processing Month",
        "Academic year",
        "Category",
        "REGION PSGC",
        "PROVINCE PSGC",
        "CITY/MUNICIPALITY PSGC",
        "PAYMENT MODE",
        "HHs",
        "Gender MALE",
        "Gender Female",
        "GROSS_AMOUNT",
        "FINANCE_APPROVAL"
    ])

    for key, group in sorted(
        groups.items(),
        key=lambda item: tuple(str(v) for v in item[0])
    ):
        region, province, municipality, track_id = key

        track_name = TRACK_BY_ID.get(track_id, "")

        households = len({
            r.get("fourps_id") or f"__NO_HH__{r['id']}"
            for r in group
        })

        male = sum(
            1 for r in group
            if r.get("sex_id") == SEX_MALE_ID
        )

        female = sum(
            1 for r in group
            if r.get("sex_id") == SEX_FEMALE_ID
        )

        # Use the default amount per beneficiary.
        # No months parameter is required.
        total = amount_for(track_id) * len(group)

        ws.append([
            now.strftime("%B"),
            now.year,
            CATEGORY_BY_TRACK.get(track_name, ""),
            region,
            province,
            municipality,
            "TRANSACTION ACCOUNTS",
            households,
            male or None,
            female or None,
            float(total),
            ""
        ])

    out = io.BytesIO()
    wb.save(out)
    out.seek(0)

    return StreamingResponse(
        out,
        media_type=(
            "application/vnd.openxmlformats-officedocument."
            "spreadsheetml.sheet"
        ),
        headers={
            "Content-Disposition":
                f'attachment; filename="Approved_Payroll_Summary_{now:%Y-%m-%d}.xlsx"'
        }
    )


# ============================================================
# PDF PAYROLL LIST EXPORT
# No months parameter required.
# ============================================================

@router.get("/api/payments/export/pdf")
@router.get(
    "/v1/api/tuloy/staff/payments/export/pdf",
    include_in_schema=False
)
def export_pdf(
    batch_id: str = Query(...),
    search: str = "",
    status: str = "",
    track: str = "",
    db=Depends(get_ods_connection)
):
    try:
        from reportlab.lib import colors
        from reportlab.lib.enums import TA_CENTER
        from reportlab.lib.pagesizes import landscape, A4
        from reportlab.lib.styles import (
            getSampleStyleSheet,
            ParagraphStyle
        )
        from reportlab.lib.units import mm
        from reportlab.platypus import (
            SimpleDocTemplate,
            Table,
            TableStyle,
            Paragraph,
            Spacer
        )
    except ImportError:
        raise HTTPException(
            status_code=500,
            detail=(
                "PDF export dependency missing. "
                "Install reportlab in the API environment."
            )
        )

    rows = _export_rows(
        db,
        batch_id,
        search,
        status,
        track
    )

    now = datetime.now()
    output = io.BytesIO()

    page_width, _ = landscape(A4)

    doc = SimpleDocTemplate(
        output,
        pagesize=landscape(A4),
        rightMargin=8 * mm,
        leftMargin=8 * mm,
        topMargin=12 * mm,
        bottomMargin=15 * mm,
        title="TULOY Payroll List",
        author="DSWD"
    )

    styles = getSampleStyleSheet()

    styles.add(
        ParagraphStyle(
            name="PayrollSmallX",
            fontName="Helvetica",
            fontSize=6.5,
            leading=8
        )
    )

    styles.add(
        ParagraphStyle(
            name="PayrollHeadX",
            fontName="Helvetica-Bold",
            fontSize=7,
            leading=8,
            alignment=TA_CENTER
        )
    )

    def p(v, style="PayrollSmallX"):
        s = str(v if v is not None else "")

        s = (
            s.replace("&", "&amp;")
             .replace("<", "&lt;")
             .replace(">", "&gt;")
        )

        return Paragraph(s, styles[style])

    data = [[
        p(x, "PayrollHeadX")
        for x in [
            "Region PSGC",
            "Province PSGC",
            "Municipality PSGC",
            "Barangay",
            "4Ps ID",
            "Member ID",
            "Last Name",
            "First Name",
            "Sex",
            "Track",
            "Amount"
        ]
    ]]

    grand_total = Decimal("0.00")

    for r in rows:
        track_name = TRACK_BY_ID.get(
            r.get("target_track_id"), ""
        )

        # Use the default amount per beneficiary.
        # No months parameter is required.
        amount = amount_for(r.get("target_track_id"))

        grand_total += amount

        data.append([
            p(r.get("region_psgc")),
            p(r.get("province_psgc")),
            p(r.get("city_municipality_psgc")),
            p(r.get("street_and_barangay")),
            p(r.get("fourps_id")),
            p(r.get("fourps_member_id")),
            p(r.get("last_name")),
            p(r.get("given_name")),
            p(_sex_label(r.get("sex_id"))),
            p(track_name),
            p(f"{amount:,.2f}")
        ])

    data.append([
        p("GRAND TOTAL"),
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        p(f"Records: {len(rows):,}"),
        p(f"{grand_total:,.2f}")
    ])

    widths = [
        22 * mm,
        22 * mm,
        25 * mm,
        32 * mm,
        22 * mm,
        22 * mm,
        23 * mm,
        23 * mm,
        12 * mm,
        20 * mm,
        22 * mm
    ]

    table = Table(
        data,
        colWidths=widths,
        repeatRows=1,
        hAlign="LEFT"
    )

    table.setStyle(
        TableStyle([
            (
                "BACKGROUND",
                (0, 0),
                (-1, 0),
                colors.HexColor("#e4e4e4")
            ),
            (
                "GRID",
                (0, 0),
                (-1, -1),
                0.35,
                colors.HexColor("#4b4b4b")
            ),
            (
                "VALIGN",
                (0, 0),
                (-1, -1),
                "MIDDLE"
            ),
            (
                "ALIGN",
                (0, 0),
                (-1, 0),
                "CENTER"
            ),
            (
                "ALIGN",
                (-1, 1),
                (-1, -1),
                "RIGHT"
            ),
            (
                "BACKGROUND",
                (0, -1),
                (-1, -1),
                colors.HexColor("#fce4d6")
            )
        ])
    )

    story = [
        Spacer(1, 5 * mm),
        p("REPUBLIC OF THE PHILIPPINES", "PayrollHeadX"),
        p(
            "DEPARTMENT OF SOCIAL WELFARE AND DEVELOPMENT",
            "PayrollHeadX"
        ),
        p(
            "Pantawid Pamilyang Pilipino Program",
            "PayrollSmallX"
        ),
        Spacer(1, 3 * mm),
        p("PAYROLL LIST", "PayrollHeadX"),
        p(
            f"Processing Month: {now:%B %Y} | "
            f"Generated: {now:%d-%m-%Y %H:%M}"
        ),
        Spacer(1, 4 * mm),
        table
    ]

    def page_number(canvas, document):
        canvas.saveState()
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(
            colors.HexColor("#555555")
        )
        canvas.drawRightString(
            page_width - 8 * mm,
            6 * mm,
            f"Page {document.page}"
        )
        canvas.restoreState()

    doc.build(
        story,
        onFirstPage=page_number,
        onLaterPages=page_number
    )

    output.seek(0)

    return StreamingResponse(
        output,
        media_type="application/pdf",
        headers={
            "Content-Disposition":
                f'attachment; filename="Payroll_List_{now:%Y-%m-%d}.pdf"'
        }
    )
