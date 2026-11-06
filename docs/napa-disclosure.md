# NAPA disclosure contract v2

DSWD and TEST signed-in users with an active matching affiliation may export three
CSV files from a frozen BCMD batch (Tuloy Kolehiyo, Tuloy Aral or Tuloy Trabaho;
there is no fixed batch size) once its DQA run has completed on the current ruleset:

- NAPA DETAILED: the 20 approved columns for each record that passed DQA, including
  names, birth date, sex, IP indicator, household, family, member and geographic
  identifiers.
- NAPA V2 SUMMARY: counts per barangay of passed records (households, families,
  members, sex, IP, validated source, category) with a TOTAL row. No names or
  member-level identifiers.
- NAPA FAILED: records that failed or need verification, with geography names,
  HHID, MemberID, names, academic year, semester, rule codes and correction remarks.
  No birth date, sex or IP indicator.

Missing family association and unknown IP remain blank; optional middle names and
suffixes remain blank. IP zero means NO.
No automatic retention or scheduled purge applies. An operator may purge manually.
Download admission counts as disclosure: bytes admitted before purge cannot be recalled.
Downloads record admission and a conservative UNCERTAIN result; this does not claim
client receipt. No file is persisted by the API. A downloaded copy is outside purge.
Exporting does not create, approve, release or mark any payment as paid, and passing
DQA does not make a member payment-eligible.
Required recorded approvals are template, mapping, DQA, DPO disclosure and Martin's
payroll separation, for every file type. Code deployment does not itself record any
approval.
