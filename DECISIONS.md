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

## D002 — ZIP3 to county weights

- Source: 2010 ZCTA-to-County Relationship File (www2.census.gov), HUPT = housing
  units of each ZCTA-county part.
- Weight(ZIP3, county) = housing units of the ZIP3 in the county / housing units of the ZIP3.
- Why: actual housing counts, no area approximation or API key; 2010 is mid-sample.
- Rejected: HUD-USPS crosswalk and Geocorr (user-reported HTTP 403 from Switzerland);
  2020 relationship file plus Census API (key activation failed).
- Map post-2010 FEMA county codes to 2010 codes using `county_recode` in climate.yaml.
  For split areas, the predecessor mapping is an approximation, not an exact boundary match.
- Manual weight: ZIP3 772 (Houston P.O. boxes, no ZCTA) -> Harris County (48201).
  Territories 008 and 969 remain unmatched.
- Limits: ZCTA differs from USPS ZIP; static 2010 housing shares are applied to all years.

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

## D008 — Typed ingestion and ZIP3

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
- D008 and D007 preserve the earlier ingestion/environment decisions under unique IDs.

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

## D006 — Gaps in the monthly history

- Measure gaps for every loan, using records available through the cutoff.
- `start_gap_months`: months from first payment to the first record on or after
  first payment. Leading gaps are tolerated under the seasoned-acquisition
  assumption. The earlier diagnostic found 11,192 performing loans with 7+
  missing leading months and no internal gap; it does not prove their cause.
- `max_internal_gap_months`: longest missing run between records whose previous
  observation is inside the window. The run may extend beyond window end.
- Flag runs longer than 2 months as `long_internal_gap`; exclude only loans
  labelled 0 from `in_pd_sample`. Retain all loans, outcomes and labels in the dataset.
- Threshold: `configs/target.yaml`, `max_internal_gap_months: 2`.
  Counts are reported in `reports/target_summary.md`.
- Phase 7 caveat: seasoned acquisitions survive until acquisition (left truncation).
  Assess `start_gap_months` as a feature or sensitivity check; no claim that an
  unobserved early default is impossible is established by these data alone.
- This rule supersedes the pending threshold notes above. The earlier typed
  ingestion decision is renumbered D008 to keep decision IDs unique.

- The exclusion applies only to loans labelled 0: a gap can hide a default, but an
  observed default is certain. Currently 4 loans have internal gaps > 2 months,
  all observed defaults, so none is excluded by this rule.

## D009 — Disaster events (treatment)

- Source: OpenFEMA Disaster Declarations Summaries v2, downloaded and cached by
  `climrisk.ingest.fema`. Reference: https://www.fema.gov/openfema-data-page/disaster-declarations-summaries-v2
- Main definition: DR major disasters, incident types Hurricane, Tropical Storm,
  Coastal Storm, Typhoon, Flood, Dam/Levee Break and Fire, with IA or IH household
  assistance. Retain both assistance fields to cover their historical transition.
- Broad definition: add Severe Storm, Tornado, Straight-Line Winds and Mud/Landslide,
  with no household-assistance requirement.
- Exclude Biological, EM and FM declarations and statewide county code 000.
- Event month is the month of incidentBeginDate, preserving the UTC calendar date.
- Deduplicate disaster/county/start-month combinations before weighted aggregation.
- Hit share counts each affected county only once per month; event count preserves
  distinct disasters in the same county-month.

## D010 — Historical climate exposure (replaces NRI as the main measure)

- Use expected qualifying disasters per home over the preceding 120 months,
  weighted by ZIP3-county housing allocation; exclude the current month.
- Grid starts January 1990; with the three-month availability lag (D011), a full
  120-month lookback is available from April 2000 onward.
- The user reports NRI download restrictions from Switzerland; these have not
  been independently reproduced. A recent NRI snapshot also cannot represent
  information contemporaneously available for early loan vintages.
- NRI remains a possible later robustness source, rather than the primary measure.
- Limitations: 2010 housing/geography weights are static and retrospective; FEMA
  declarations can be revised or declared after incident onset. Excluding future
  incident months does not make this an exact reconstruction of information known
  at origination. Assess declaration-date lags and historical-weight sensitivity.
- ZCTA housing counts approximate USPS ZIP geography. ZIP3 aggregation cannot
  identify which individual properties were affected or establish causal effects.

## D011 — Information availability

- Exposure excludes the current month and the three most recent completed months
  (`availability_lag_months: 3`), allowing for FEMA county additions after an event.
- Exact window at month t: t-123 through t-4 inclusive (120 months). This conservative
  boundary follows the lagged rolling-window implementation; it does not include t-3.
- Census 2010 weights and the current FEMA file are used for all years: the analysis
  is retrospective, not a real-time replication of what lenders knew. A fixed lag
  does not reconstruct historical declaration revisions or guarantee availability.
- Declaration-based exposure reflects administrative decisions as well as physical
  risk; a physical measure (coastal counties / NOAA hurricane tracks) is planned for Phase 6.

## D012 — Event study design

- Stacked event study: one stack per event month, ZIP3 x month, loan-count weights,
  standard errors clustered by ZIP3.
- Treatment: main-definition hit share >= 0.5; robustness thresholds 0.75 and 0.9.
  Clean treated units have no other main-definition hit in [-12, +24], except
  the event and two following grace months. Grace months need not be the same disaster.
- Controls: same Census division, no main-definition hit anywhere in the window.
  Fixed effects: ZIP3 x stack and division x calendar month x stack; reference k=-1.
- Windows overlapping March 2020 through December 2021 are excluded; territories
  without a Census division are excluded. Monthly panels can remain unbalanced.
- Causal interpretation requires parallel trends and no spillovers. Loan weights and
  repeated controls across stacks define the pooled estimand; clean controls alone
  do not guarantee unbiased aggregation under heterogeneous treatment effects.

## D013 — Pre-trend tests

- Report two single-restriction tests: mean of leads k=-12 through -2 (level gap),
  and their linear slope (diverging trend), using cluster covariance and t with G-1 df.
- The supplied simulation reportedly found 20% rejection for an 11-lead joint Wald
  test at 5% nominal size, versus about 5% for these combinations. That simulation
  has not been independently reproduced in this repository; correct size is not guaranteed.
- Inspect all lead estimates and confidence intervals. Non-rejection of either
  combination is not proof of parallel trends and can miss nonlinear pre-trends.

## D014 — Event-study heterogeneity and default follow-up

- Re-estimate D012 by dominant disaster type and before/from January 2010. Type
  assignment counts each county's housing once per group-month; ties are alphabetical.
- Skip subsamples with fewer than 10 treated ZIP3-events. This is a screening rule,
  not a guarantee that clustered inference is reliable; events can share ZIP3 clusters.
- Cure cohort: existing D004 default labels from January 2014, with default date +24
  months no later than the performance cutoff. Defaults during COVID are excluded,
  but follow-up can overlap COVID. This is not a lifetime first-default cohort.
- Flags: default month and two preceding months; disaster takes precedence over
  forbearance. Loss/REO takes precedence over ever returning to current or prepaying,
  then transfer, then observed terminal delinquency. Missing terminal follow-up is unknown.
- Exit timing uses zero_balance_date, falling back to reporting month; late-reported
  exits known by the cutoff can qualify. REO is not necessarily realized accounting loss.
- Ever curing does not imply sustained performance. Follow-up gaps may hide events;
  unobserved outcomes after transfer remain unknown. These descriptive associations
  do not measure causal effects of assistance or determine a new PD target on their own.

## D015 — Interpretation by disaster type and PD target sensitivity

- Hurricane/tropical-storm estimates show a clear short-run increase. The flood
  lead-mean test rejects and the plotted leads show a pre-event pattern: the negative
  flood estimate is not causally identified under this design and must not be read
  as a protective effect. Fire estimates also require scrutiny of the lead profile;
  non-rejection of mean/slope tests alone does not establish parallel trends.
- Retain D004 (90+ delinquency, RA or loss exit) as the primary target. Disaster-related
  delinquency is a real payment interruption even when it later cures. High cure rates
  do not by themselves justify relabelling observed defaults as non-defaults.
- Plan a separately specified credit-default sensitivity target distinguishing
  persistent delinquency and loss/REO from temporary cured episodes. The precise
  persistence and follow-up requirements must be fixed before estimation; this variant
  is not implemented here and does not alter D004 or the current PD sample.
- Report ever-cured and current-at-end separately. Current-at-end requires an observed
  current status at default month +24, without loss/REO, payoff or transfer during
  follow-up; denominator = all eligible defaults in the group. Missing endpoint
  observations and prepaid loans do not count as current. This does not prove that
  the loan remained current continuously between cure and follow-up end.

## D016 — Physical hurricane-coast exposure (Phase 6)

- Decision: complement historical disaster exposure (`exposure_main`, D010/D011)
  with static physical coastal proximity. Measure minimum distance from Census
  `cb_2020_us_county_500k` county polygons to TIGER 2020 coastline segments named
  Atlantic or Gulf, in EPSG:5070 (metres), then convert to km.
- Aggregate with 2010 ZIP3 housing weights. Outputs: `dist_coast_km`,
  `shoreline_share` (county distance <= 1 km), and `near_coast_share` (<= 50 km,
  the main pricing variable). These shares describe housing in counties meeting
  the distance threshold, not individual homes measured within that distance.
- Rationale: capture physical mainland Atlantic/Gulf coastal proximity even where
  no recent disaster was declared. Other coasts are outside this proxy's scope;
  their exclusion is not a claim that they face no hurricane or other climate risk.
- Post-2010 codes map to their 2010 predecessors using climate.yaml. Bedford city,
  VA (51515), dissolved in 2013, takes Bedford County's (51019) distance. Mappings
  across changed boundaries are approximate.
- All 890 ZIP3 with housing weights have full matched coverage in the current run.
  This does not cover ZIP3 008 and 969, which have no weights.
- Limits: county minimum distance is not property-level distance or hurricane
  probability. Static geography avoids future disaster occurrences, but 2020
  boundaries/shoreline and 2010 housing weights remain retrospective.
- EPSG:5070 is intended for CONUS and distorts distances for Alaska, Hawaii and
  Puerto Rico. State x month fixed effects do not remove within-state measurement
  error; a CONUS-only sensitivity check remains appropriate.

## D017 — Spread definition and origination month (Phase 6)

- Decision: `spread_bp = 100 * (orig_rate - PMMS)`, with both rates in percent.
- PMMS: monthly average of the weekly Freddie Mac 30-year survey rate from FRED
  `MORTGAGE30US`; weeks marked `.` are skipped.
- Approximate origination month as first payment date minus two months; the exact
  origination month is not published. Sample: 360-month non-HARP loans, with known
  ZIP3 and origination rate. HARP relief refinances are a distinct population.
- Join audit: 753,856 eligible loans, 753,660 matched. The 196 unmatched loans are
  in ZIP3 008 (53) and 969 (143), lacking climate/coast weights; none lack PMMS.
- Rationale: remove the market-wide rate level. Fixed effects absorb common state
  x proxy-month levels, but cannot guarantee correction of loan-specific date
  errors or misassigned climate exposure. Check alternative origination lags later.
- Diagnostic: median spreads are 3-23 bp across broad vintage groups; the 2020-24
  group's fifth percentile is -86 bp. This is consistent with timing sensitivity
  during rapid rate changes, not proof of a particular loan's date error.

## D018 — Pricing design: same controls for spread and default (Phase 6)

- Estimate `y ~ exposure_main + near_coast_share + controls | state^orig_month`.
  CRV1 standard errors are clustered by ZIP3; coefficient confidence intervals
  use t(G-1), with G currently counted in the input subsample.
- Spread regressions: all eligible originations, before 2012, from 2012, and the
  PD sample. Default regressions: `100 * default_24m` on the PD sample, pooled
  and separately before/from 2012. Periods use identifier-derived vintage.
- Controls: FICO/LTV/DTI bins of 20/5/5 with missing categories; log original UPB;
  purpose, occupancy, property type, units, channel, borrowers, first-time buyer,
  mortgage-insurance indicator (missing MI treated as zero), super-conforming flag.
- The pooled PD spread/default regressions use the same input loans and controls;
  both retain 564,867 observations after dropping 91 singleton fixed effects.
  Verify retained membership and cluster counts when extending inference tests.
- Same controls and sample support comparison, but coefficients have different
  units (bp versus pp) and do not directly establish adequate risk compensation.
- Split default regressions check whether the coastal association persists beyond
  the housing-bust vintages. They do not eliminate all within-state confounding.
- Interpretation: conditional associations, not causal pricing effects. Event-study
  causal interpretation remains conditional on D012-D015 assumptions. Whether
  coastal spreads compensate expected losses will be assessed in Phase 8 using
  measured loss given default and aligned horizons, rather than assumed LGD alone.
