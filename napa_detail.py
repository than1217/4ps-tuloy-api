"""NAPA detail serialization of an already mapped, frozen batch snapshot.

This module does not query databases, infer source columns, authorize disclosure,
create payment transactions, or expose a download endpoint. Those integrations must
supply confirmed mappings and policy decisions before a live export is enabled.
"""
import csv
import io
import unicodedata
from datetime import date, datetime
from pathlib import Path

HEADER_FILE = Path(__file__).resolve().parent / "docs" / "templates" / "napa-detail-headers.csv"
with HEADER_FILE.open(encoding="utf-8", newline="") as _file:
    DETAIL_HEADERS = tuple(next(csv.reader(_file)))

FIELDS = (
    "region_name", "region_psgc", "province_name", "province_psgc", "municipality_name",
    "municipality_psgc", "barangay_name", "barangay_psgc", "hhid", "family_id", "member_id",
    "member_last_name", "member_first_name", "member_middle_name", "member_ext_name",
    "sex", "ip_indicator", "date_of_birth", "source_agency",
)
TEXT_CODES = frozenset({"region_psgc", "province_psgc", "municipality_psgc", "barangay_psgc"})
FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r", "＝", "＋", "－", "＠")

# Martin's 2026-10-30 decision. NULL is retained in snapshots and represented as
# an empty CSV cell, not the literal string "NULL" or an invented negative value.
APPROVED_NULLABLE_FIELDS = frozenset({'family_id', 'ip_indicator', 'member_middle_name', 'member_ext_name'})


class NapaValidationError(ValueError):
    """Diagnostic identifiers only: never include a beneficiary's field value."""
    def __init__(self, line, field, code):
        self.line, self.field, self.code = line, field, code
        super().__init__(f"NAPA line {line}, field {field}: {code}")


def serialize_detail(members, category, *, nullable_fields=APPROVED_NULLABLE_FIELDS, formula_policy="reject", max_rows=1000):
    """Return UTF-8 CSV bytes in frozen line order, with the uploaded 20 exact headers.

    Missing mappings always fail. A supplied null/empty value is permitted only when
    the caller's approved policy explicitly lists that field as nullable. Defaults
    apply the recorded family/IP and optional middle-name/suffix policy. Other nullable fields and formula
    prefix behavior still require an explicit caller policy.
    """
    if category not in {"TK", "TA", "TT"}:
        raise NapaValidationError(0, "category", "INVALID_CATEGORY")
    if not set(nullable_fields) <= set(FIELDS) or formula_policy not in {"reject", "prefix"}:
        raise NapaValidationError(0, "policy", "INVALID_POLICY")
    if not isinstance(max_rows, int) or isinstance(max_rows, bool) or max_rows < 1:
        raise NapaValidationError(0, "policy", "INVALID_LIMIT")
    rows = []
    for member in members:
        if len(rows) >= max_rows:
            raise NapaValidationError(0, "members", "ROW_LIMIT_EXCEEDED")
        rows.append(member)
    line_numbers = [row.get("line_no") for row in rows]
    if any(type(number) is not int for number in line_numbers) or sorted(line_numbers) != list(range(1, len(rows) + 1)):
        raise NapaValidationError(0, "line_no", "INVALID_FROZEN_ORDER")
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n", quoting=csv.QUOTE_MINIMAL)
    writer.writerow(DETAIL_HEADERS)
    for member in sorted(rows, key=lambda row: row["line_no"]):
        line = member["line_no"]
        values = []
        for field in FIELDS:
            if field not in member:
                raise NapaValidationError(line, field, "MAPPING_REQUIRED")
            value = member[field]
            if value is None or value == "":
                if field not in nullable_fields:
                    raise NapaValidationError(line, field, "VALUE_REQUIRED")
                values.append("")
                continue
            if field == "date_of_birth":
                try:
                    if isinstance(value, datetime):
                        raise ValueError()
                    parsed = value if type(value) is date else date.fromisoformat(value)
                except (ValueError, TypeError):
                    raise NapaValidationError(line, field, "INVALID_DATE") from None
                values.append(parsed.strftime("%d-%m-%Y"))
                continue
            if not isinstance(value, str):
                # Codes must arrive as text; coercing numeric IDs cannot recover leading zeros.
                raise NapaValidationError(line, field, "TEXT_REQUIRED")
            normalized = unicodedata.normalize("NFC", value)
            risky = normalized.lstrip(" ").startswith(FORMULA_PREFIXES) or normalized.strip().startswith(FORMULA_PREFIXES)
            normalized = normalized.strip()
            if not normalized and field not in nullable_fields:
                raise NapaValidationError(line, field, "VALUE_REQUIRED")
            # The verified ODS PSA columns are char(10). Never pad, truncate or
            # substitute the internal region/province/city/barangay primary keys.
            if field in TEXT_CODES and (len(normalized) != 10 or not normalized.isascii() or not normalized.isdecimal()):
                raise NapaValidationError(line, field, "INVALID_CODE")
            if field == "sex" and normalized not in {"FEMALE", "MALE"}:
                raise NapaValidationError(line, field, "INVALID_SEX")
            if field == "ip_indicator" and normalized not in {"YES", "NO"}:
                raise NapaValidationError(line, field, "INVALID_IP_VALUE")
            if field == "source_agency" and normalized not in {"CHED", "TESDA"}:
                raise NapaValidationError(line, field, "INVALID_SOURCE_AGENCY")
            if risky:
                if formula_policy == "reject":
                    raise NapaValidationError(line, field, "FORMULA_POLICY_REQUIRED")
                normalized = "'" + normalized
            values.append(normalized)
        writer.writerow([*values, category])
    return output.getvalue().encode("utf-8")
