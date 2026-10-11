# Methodological decision log

Record the date, context, alternatives, rationale and consequences for each decision.

## Project setup — 2026-10-11

- Use the `src/` layout with the `climrisk` Python package.
- Exclude all of `data/` from Git, starting with the initial commit.
- Keep notebooks for exploration and reusable transformations in the package.
- Use uv with a committed lockfile, pytest, Ruff and pre-commit.
- License code under MIT. Public visibility was requested during setup but remains
  pending explicit confirmation; the repository was initially created private.
- Clarity and HUD USER registration must be completed personally on their portals.
- Target, causal design and model specifications remain to be implemented.

## D001 — Freddie Mac data version (2026-10-11)

- Confirmed locally: 21 sample archives for 2004-2024 and 42 origination/performance files.
- Reference documents: `file_layout_july_2026.xlsx` and `general_user_guide_july_2026.pdf`.
- Initial inspection: 31 origination and 35 performance fields on the first row of every year.
- Reported metadata requiring document verification: file date 2026-09-04,
  July 2026 layout and performance cutoff 2026-03-31.
- Rationale: freeze source versions before ingestion. Validation uses March 2026 as its cutoff.

## D002 — ZIP-to-county mapping (proposal, not final)

- Proposed source: Census 2020 ZCTA-to-county area relationships, weighted by
  ZCTA housing counts from the Census API.
- Alternatives: HUD-USPS Crosswalk and Geocorr 2022. Reported HTTP 403 errors
  from Switzerland have not been reproduced in this session.
- ZCTA differs from USPS ZIP; area allocation introduces measurement error that
  must be quantified rather than assumed small.
- Inspect empty ZCTA rows and validate compatibility with Freddie Mac ZIP3 data.

## D003 — Vintage definition

- Decision: derive origination year from the loan identifier (PYYQnXXXXXXX),
  rather than the source file year. Derive `orig_quarter` from the same identifier.
- Evidence: one of 1,050,000 loans has an F09Q1 identifier and first payment in
  March 2009, but appears in the 2008 source file. Both observed fields support 2009.
- Cause: undetermined. The user reports that Freddie Mac's FAQ mentions reporting
  anomalies; no specific cause has been established for this record.
- Traceability: retain the file year in `source_year`; report discrepancies as warnings.
- Keep filenames based on source year; group analyses and time splits by `vintage`.
- Century pivot: YY >= 90 maps to 19YY, otherwise 20YY (supports 1990-2089).
- Check first-payment calendar year against vintage; a difference of zero or one
  is expected. Other differences are warnings for review.

## D004 — Typed ingestion and ZIP3

- ZIP codes in these samples contain three characters; `000` is unknown.
- Preserve categorical codes as text, including `00`, `01` and `RA`; map `XX` to NULL.
- Trim whitespace and replace empty fields and documented sentinels before strict casts.
- Convert YYYYMM to the first day of the month.
- Write one ZSTD Parquet per table and source year through a temporary file,
  with cleanup on failure. Replacement is atomic per file.
- Initial 2005 validation: 50,000 origination rows and 3,877,176 performance rows.
- D003 supersedes the original file-based definition of `vintage`.

## D005 — Phase 1 Python environment

- Python 3.12 via uv; Ruff E/F/I/B/UP, line length 100.
- Add ingestion and notebook dependencies only at this stage.
- Keep distribution name `climate-mortgage-risk` and import package `climrisk`.
- This entry replaces the previous D003 environment heading to keep decision IDs unique.

## Repository language

- Use English for documentation, code comments, CLI messages, reports and new commits.

## Phase 1 validation results — 2026-10-11

- Regenerated all 42 Parquet files with the D003 columns; all blocking checks pass.
- Exactly one source-year mismatch; vintage counts are 49,999 (2008) and 50,001 (2009).
- First-payment warning: 336 of 1,050,000 loans (0.032%) have calendar-year offsets
  outside zero or one: 2 (232 loans), 3 (50), 4 (21), 5 (24), 6 (5), 7 (3), 10 (1).
- These records remain unchanged. Causes have not been established; review these
  anomalies before choosing a time origin or eligibility rules for phase 2.
- The quality report contains aggregate counts only; raw and derived loan data stay local.
