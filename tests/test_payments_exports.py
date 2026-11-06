
"""Exercise real export libraries with synthetic DB rows; no live records or writes."""

import io

import pytest

from conftest import bearer
import payroll_fakes as payroll
from scopes import LEGACY_MEMBER_VERIFY

PREFIX = "/v1/api/tuloy/staff/payments/export/"

BATCH_ID = "SYNTHETIC-BATCH-001"

PDF_MIME = "application/pdf"
XLSX_MIME = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
)

EXPORT_CASES = [
    (
        "pdf",
        PDF_MIME,
        "Payroll_List_",
    ),
    (
        "summary",
        XLSX_MIME,
        "Approved_Payroll_Summary_",
    ),
]


@pytest.mark.parametrize(
    "kind,mime,filename",
    EXPORT_CASES,
)
@pytest.mark.parametrize("populated", [False, True])
def test_payroll_download_generates_real_document(
    client,
    fake_db,
    kind,
    mime,
    filename,
    populated,
):
    # --------------------------------------------------
    # 1. Authorization
    # --------------------------------------------------
    fake_db.scopes[4] = [
        (LEGACY_MEMBER_VERIFY, "DSWD", "DSWD", "active")
    ]

    # --------------------------------------------------
    # 2. Synthetic batch beneficiary records
    # --------------------------------------------------
    # Payroll reads the shared eligibility: the batch, its latest DQA run,
    # the run's results joined to bcmd_batch_member, and payment history.
    members = [payroll.member(i, sex=i) for i in (1, 2)] if populated else []
    payroll.script(fake_db, members)

    # --------------------------------------------------
    # 3. Execute export
    # --------------------------------------------------
    response = client.get(
        PREFIX + kind,
        params={
            "batch_id": BATCH_ID,
        },
        headers=bearer("no-grants"),
    )

    # --------------------------------------------------
    # 4. HTTP response validation
    # --------------------------------------------------
    assert response.status_code == 200, (
        f"Export {kind} failed: "
        f"HTTP {response.status_code}: {response.text}"
    )

    assert response.headers["content-type"].split(";")[0] == mime

    content_disposition = response.headers.get(
        "content-disposition", ""
    )

    assert content_disposition.startswith(
        f'attachment; filename="{filename}'
    )

    # --------------------------------------------------
    # 5. Database read-only validation
    # --------------------------------------------------
    # Members are read only for a batch that exists.
    assert fake_db.queries("FROM bcmd_batch WHERE public_id")
    assert bool(fake_db.queries("FROM bcmd_batch_member")) == populated

    assert fake_db.commits == 0

    assert not fake_db.queries("INSERT INTO")
    assert not fake_db.queries("UPDATE ")
    assert not fake_db.queries("DELETE FROM")

    # --------------------------------------------------
    # 6. PDF validation
    # --------------------------------------------------
    if kind == "pdf":

        assert response.content.startswith(b"%PDF-")
        assert response.content.rstrip().endswith(b"%%EOF")

        assert b"/Type /Page" in response.content

        # Ensure PDF contains actual page content.
        assert len(response.content) > 500

    # --------------------------------------------------
    # 7. XLSX validation
    # --------------------------------------------------
    elif kind == "summary":

        from openpyxl import load_workbook

        workbook = load_workbook(
            io.BytesIO(response.content),
            read_only=True,
            data_only=True,
        )

        try:
            assert workbook.sheetnames == [
                "1. Approved Payroll Summary"
            ]

            values = list(workbook.active.values)

            assert len(values) == (2 if populated else 1)

            # Validate header.
            assert values[0][10] == "GROSS_AMOUNT"

            if populated:

                # Category and geographic PSGC fields. Batch members carry no
                # legacy region_psgc/province_psgc/city_municipality_psgc
                # fields, so these stay blank, as they were in production.
                assert values[1][2:6] == (
                    "TULOY KOLEHIYO",
                    None,
                    None,
                    None,
                )

                # Household count, male, female, gross amount.
                assert values[1][7:11] == (
                    1,
                    1,
                    1,
                    10000,
                )

        finally:
            workbook.close()
