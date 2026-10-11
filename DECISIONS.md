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

## D006 — Typed ingestion and ZIP3

- ZIP codes in these samples contain three characters; `000` is unknown.
- Preserve categorical codes as text, including `00`, `01` and `RA`; map `XX` to NULL.
- Trim whitespace and replace empty fields and documented sentinels before strict casts.
- Convert YYYYMM to the first day of the month.
- Write one ZSTD Parquet per table and source year through a temporary file,
  with cleanup on failure. Replacement is atomic per file.
- Initial 2005 validation: 50,000 origination rows and 3,877,176 performance rows.
- D003 supersedes the original file-based definition of `vintage`.

## D007 — Phase 1 Python environment

- Python 3.12 via uv; Ruff E/F/I/B/UP, line length 100.
- Add ingestion and notebook dependencies only at this stage.
- Keep distribution name `climate-mortgage-risk` and import package `climrisk`.
- Environment setup was previously numbered D003, then D005; use D007 to keep IDs unique.

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

## D004 — Default definition

- Labels require a complete 24-month window (checked before any outcome): avoids
  selection bias towards early defaults in recent vintages. Complete here means
  the window ends by the cutoff; terminal exits need no subsequent monthly rows.

- Window: first 24 monthly observations from the first payment date, inclusive
  of that month and exclusive of first payment plus 24 months.
- Default (1): 90+ days past due (numeric status >= 03), REO acquisition (RA),
  or loss exit (zero balance 02, 03, 09).
- Non-default (0): voluntary payoff (01) before any default (competing risk),
  or no observed default and performance observed at or after the window end
  (missing months allowed provisionally, pending threshold review).
- Censored (NULL): transfer out (15, 16, 96), window beyond the March 2026
  cutoff regardless of any earlier terminal event, or missing months without a terminal event.
- Honor chronological ordering of default, payoff and transfer events; same-month
  ties prioritize default, then payoff, then transfer.
- Ignore performance before first payment, after the window, or after the cutoff.
- Count internal and leading gaps in `window_months_missing`; gaps alone no longer
  cause censoring if the history reaches or extends beyond the window end.
- Config: `configs/target.yaml`. The output label is `default_24m` for the default
  24-month setting; test fixtures use a shorter window with the same column name.

## D005 — PD modeling sample

- Keep all origination loans and labels in `data/processed/loans.parquet`.
- Exclude from the PD sample: unlabelled loans; HARP relief refinances (HARP flag
  or populated pre-HARP identifier); date anomalies; observation windows overlapping
  March 2020 through December 2021; unknown ZIP3.
- Phase 1 identified 336 date anomalies (0.032%, cause undetermined) and 3 unknown
  ZIP3 values. Counts among labelled loans may differ.
- Keep `is_harp`, `date_anomaly`, `overlaps_covid` and `in_pd_sample` flags.
- Drop the entirely missing VantageScore column from processed loans only.
- The panel is a separate additive ZIP3-month aggregate: keep COVID months and
  loans outside the PD sample, but omit unknown ZIP3 and months before first payment
  or after the cutoff. Apply causal-sample restrictions at analysis time.
- This panel contains delinquency stocks and exit flows, not first-default incidence.
- Eligibility is evaluated per loan window: do not exclude whole vintages by assumption.
- D006 and D007 preserve the earlier ingestion/environment decisions under unique IDs.

## Phase 2 cutoff correction and gap review (pending)

- Moved administrative cutoff censoring ahead of every terminal outcome; tests
  cover observed early defaults and payoffs in incomplete windows.
- No labelled loan has a window ending after March 2026.
- 2024 retains 7,403 labelled loans with first payments in February-April 2024;
  their windows end in January-March 2026. No blanket vintage exclusion was applied.
- The 31,100 gap cases remain censored pending review. Of these, 24,458 have
  their final window month present; 24,457 start late within the window; none
  has an observed last window month strictly before window end. Some have no
  observations in the window. Median missing window months: 2.
- Full-history diagnostics find 635 payoff dates and 14 underwriting-defect exit
  dates within the window despite no corresponding terminal event observed there.
- Full-history exit-code aggregation uses MAX(code), not necessarily the final
  event code. Negative median months missing indicates observations after the
  window and must not be interpreted as a negative number of missing observations.
- No gap reclassification or commit performed while reviewing these diagnostics.

## Phase 2 gap revision — provisional, pending threshold review

- Use effective `zero_balance_date` for exits, falling back to reporting period
  only when that date is missing. Effective exits must be inside the window.
- Read history only through the configured cutoff, including late-reported exits;
  observations after that cutoff cannot resolve gaps or supply labels.
- Select the latest non-null exit code by reporting month with `arg_max`, with
  its corresponding effective date; do not select codes lexicographically.
- Allow performing labels when the loan is observed at or after window end,
  even with missing monthly observations. This assumes no unobserved default
  occurred in those gaps; its reliability is not established by this diagnostic.
- Keep `window_months_observed`, `window_months_missing` and `default_date`.
- No missing-month threshold has been applied; choose it after report review.
- Rebuild: 649 censored gaps, down from 31,100; 781,524 performing loans.
- Performing missing-month counts: 0 = 751,073; 1-2 = 15,910;
  3-6 = 3,349; 7+ = 11,192. Large gaps need particular review.
- This revision supersedes the prior internal-gap censoring rule and the
  earlier pending gap-review observations. No commit or push yet.
