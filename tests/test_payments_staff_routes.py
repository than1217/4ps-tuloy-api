
"""Staging-proxy-compatible aliases reuse payment authentication and business handlers."""

import pytest

from conftest import bearer
import payroll_fakes as payroll
from scopes import SCOPE_VERIFY


PREFIX = "/v1/api/tuloy/staff/payments/"
BATCH_ID = "SYNTHETIC-BATCH-001"

OPERATIONS = [
    ("GET", "rates", None),
    ("GET", f"beneficiaries?batch_id={BATCH_ID}", None),
    ("POST", "compute", {"batch_id": BATCH_ID}),
    (
        "POST",
        "generate",
        {
            "batch_id": BATCH_ID,
            "academic_year_term": 1,
            "assistance_type": 1,
        },
    ),
    ("GET", f"export/csv?batch_id={BATCH_ID}", None),
    ("GET", f"export/summary?batch_id={BATCH_ID}", None),
    ("GET", f"export/pdf?batch_id={BATCH_ID}", None),
]


@pytest.mark.parametrize("method,path,body", OPERATIONS)
def test_aliases_reject_anonymous_and_non_dswd_test_users(
    client, fake_db, method, path, body
):
    anonymous = client.request(
        method,
        PREFIX + path,
        json=body,
    )
    assert anonymous.status_code == 401

    for agency in ("CHED", "TESDA"):
        fake_db.scopes[4] = [
            (SCOPE_VERIFY, agency, agency, "active")
        ]

        response = client.request(
            method,
            PREFIX + path,
            json=body,
            headers=bearer("no-grants"),
        )
        assert response.status_code == 403

    assert not fake_db.queries("tuloy_beneficiary_tbl")
    assert not fake_db.queries("payment_transaction")
    assert not fake_db.queries("INSERT INTO")
    assert fake_db.commits == 0


@pytest.mark.parametrize("agency", ["DSWD", "TEST"])
def test_alias_reads_and_computation_equal_original_handlers(
    client, fake_db, agency
):
    fake_db.scopes[4] = [
        (SCOPE_VERIFY, agency, agency, "active")
    ]

    # Synthetic beneficiary record for the requested batch, read through
    # the shared eligibility (latest DQA run, results, payment history).
    payroll.script(fake_db, [payroll.member(1)])

    for method, path, body in OPERATIONS[:3]:
        alias = client.request(
            method,
            PREFIX + path,
            json=body,
            headers=bearer("no-grants"),
        )

        root = client.request(
            method,
            "/api/payments/" + path,
            json=body,
            headers=bearer("no-grants"),
        )

        assert alias.status_code == root.status_code == 200, (
            f"\nOperation: {method} {path}"
            f"\nAlias: {alias.status_code} - {alias.text}"
            f"\nRoot: {root.status_code} - {root.text}"
        )

        assert alias.json() == root.json()

def test_alias_preserves_generation_journey_mapping_guard(
    client, fake_db
):
    fake_db.scopes[4] = [
        (SCOPE_VERIFY, "DSWD", "DSWD", "active")
    ]

    fake_db.on(
        r"INFORMATION_SCHEMA.COLUMNS",
        lambda sql, params: [
            {
                "COLUMN_NAME": "tuloy_journey_id",
                "IS_NULLABLE": "NO",
            }
        ],
    )

    result = client.post(
        PREFIX + "generate",
        headers=bearer("no-grants"),
        json={
            "batch_id": BATCH_ID,
            "academic_year_term": 1,
            "assistance_type": 1,
        },
    )

    assert result.status_code == 409
    assert fake_db.commits == 0
    assert not fake_db.queries("INSERT INTO payment_transaction")
