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

## D019 — PD model design (Phase 7)

**Decision.**
- **Target:** 24-month default (D004) on the PD sample.
- **Out-of-time split by vintage:**
  - train 2004–2014, which includes the 2007–2008 downturn;
  - validation 2015–2018, used for selection and tuning only;
  - test 2021–2024, evaluated once.
  2019–2020 have no eligible loans.
- **Features:** origination data only. Excluded from predictors: performance-file fields, window
  flags, `start_gap`, seller name, valuation method, vintage.
- **Main model: WoE scorecard.**
  - Quantile bins, with a 'missing' bin and a separate bin for a point mass at the minimum.
  - Selection on train only: IV ≥ 0.02; |WoE correlation| ≤ 0.8, keeping the higher IV;
    all coefficients negative.
- **Benchmark: LightGBM** on the same features, with early stopping on validation AUC.

**Why.**
- **Out-of-time, not random, split:** this is how a lender uses the model, and a random split
  would leak future information.
- **Scorecard over LightGBM:** LightGBM gained only +0.002 validation AUC (0.816 vs 0.814),
  with a larger train-validation gap. The scorecard is kept for interpretability and
  stability.

## D020 — Through-the-cycle PD anchoring (Phase 7)

**Decision.**
- Refit the scorecard on 2004–2018.
- Shift every logit(PD) by a constant (base-scorecard diagnostic a ≈ −0.14) so that the mean PD equals the simple
  average of the yearly default rates (1.105%), not the loan-weighted rate (1.264%).

**Why.**
- **No macroeconomic variables:** the model ranks loans but cannot forecast the cycle. Raw
  PDs over-predicted validation by about 2× (a = −0.77), uniformly across deciles
  (slope b = 0.91).
- **Sample-based TTC anchoring:** the gap by vintage can reflect the credit cycle, omitted
  variables and model error; it does not isolate a macroeconomic effect. Phase 8 explores
  stress scenarios. The shift is recomputed on the development sample for each final scorecard.
- This is a sample-based TTC proxy, not a regulatory calibration.

## D021 — Climate features of the final PD model (pre-registered before the test)

**Decision.**
- **Primary model:** scorecard with `near_coast_share` and `log_dist_coast` added, under the
  usual selection rules (no forced retention). Validation ablation forced climate features
  past IV/correlation filters; the final refit applies the standard filters to all features.
- **Excluded:** `exposure_main`, `exposure_broad` and `recent_events_12m`.
- **On test:** compared with the base scorecard (paired bootstrap), with LightGBM base as the
  benchmark.

**Why (validation results).**
- **Static coast features:** +0.0022 AUC for the scorecard (95% paired bootstrap
  [+0.0015, +0.0028]). This is the best of the climate sets, and the features are stable over
  time (PSI ≤ 0.021).
- **Exposure features drift:** PSI 0.15–0.21 from train to validation, partly acting as a
  possible proxy for the vintage (hypothesis, not established causally). They reduced LightGBM's AUC (−0.0068); static features alone halved
  the loss (−0.0028). `recent_events_12m` has weak univariate signal (IV 0.0025).
- **Caveat:** intervals resample loans, not ZIP3; they exclude within-ZIP3 dependence,
  model fitting and validation-based selection uncertainty and may understate uncertainty.
- This choice is fixed before any test-vintage evaluation (commit timestamp).

## D022 — Credit-default sensitivity target (pre-registered, implements D015)

**Decision.**
- **Target.** `credit_default_24m` equals 0 for every loan with `default_24m = 0`. For a D004
  default whose first default month is D, it equals 0 (temporary episode) only if, with no
  loss exit (02, 03, 09) or REO (RA) in (D, D+12]:
  - the loan is observed current (`00`) at D+12; or
  - it is paid off voluntarily (zero-balance code 01) in (D, D+12].
  Every other D004 default stays 1: loss or REO, still delinquent, transferred, or no
  observation at D+12. When in doubt the rule counts a default, so it is conservative.
- **Eligibility, applied to every loan, defaulted or not:** the PD sample, with window end
  + 12 months ≤ performance cutoff (2026-03-31). The 2024 vintage and part of 2023 drop out.
- **Planned analyses, fixed before estimation:**
  1. D004 and credit-default rates by vintage.
  2. The D021 primary scorecard procedure refitted on 2004–2018 with this target and
     through-the-cycle anchored to its own long-run rate, plus LightGBM base (early stopping
     on validation). Both are evaluated on the eligible test loans: AUC with a 95% bootstrap
     interval, and calibration overall and by vintage.

**Why.**
- **Flags are not comparable over time.** Disaster and forbearance flags are blank before
  about 2013, so a definition based on them would not be comparable across vintages. The
  payment path is.
- **The test gap may be partly temporary.** The test under-prediction (observed/TTC = 1.9) may
  partly reflect temporary, disaster- or forbearance-related delinquency. Phase 5 found
  1% loss for disaster-flagged defaults versus 16% for others.
- **D004 stays the primary target.** This variant is reported next to it and replaces
  nothing.
- **Same eligibility rule for everyone.** Excluding only defaults with incomplete follow-up
  would bias the default rate downwards.

## D023 — Test results of the PD models and their consequences (Phase 7)

**Results (test vintages 2021–2024, one evaluation each, pre-registered in D021–D022).**

- **Discrimination, D004 target:**
  - primary scorecard AUC 0.782 [0.774, 0.792];
  - LightGBM 0.797 [0.789, 0.806];
  - paired difference versus base scorecard +0.015 [+0.011, +0.020].
- **Discrimination, credit-default target (eligible loans):** LightGBM − base scorecard +0.030
  [+0.018, +0.041].
- **Climate features:** no gain with either target: −0.0002 [−0.0008, +0.0003] for D004 and
  +0.0002 [−0.0004, +0.0008] for credit default. The validation gain (+0.0022) did not
  replicate.
- **Calibration of the TTC PD on identical eligible test loans:** observed / predicted is 1.95
  for D004 and 1.41 for credit default. The log gap is roughly halved when the target and model are changed; this is a
  descriptive sensitivity comparison, not a causal attribution to temporary episodes.
  Among loans current at D+12, 52–71% in the reported post-2015 vintages have a modification
  or payment-deferral flag during follow-up. Under-prediction remains for 2022–2023; the
  credit-default target also includes transfers and unobserved endpoints conservatively.

**Consequences.**

- **Model choice:** D019's choice of the scorecard is not supported out of time. The scorecard
  stays the documented main model, because the pre-registered choice is not changed after
  seeing the test. LightGBM's consistent advantage is reported as a model-risk finding that
  would trigger a challenger review.
- **Possible cause of the gap:** interactions between risk factors ("risk layering", e.g.
  high DTI together with high CLTV), which an additive scorecard cannot represent. This is
  untested.
- **Climate features:** no incremental out-of-time ranking gain is demonstrated for these
  static coast variables, models and samples, conditional on borrower, loan and state.
  This does not establish that climate risk has no predictive value generally. Phase 5
  event-study results and Phase 6 conditional pricing/default associations address
  different questions; the Phase 6 associations are not causal effects.
- **Phase 8:** the TTC PD under-predicts recent vintages even for credit default (×1.41).
  Phase 8 scenarios must not treat TTC PDs as a current-conditions forecast.
- **Caveat:** intervals resample loans, not ZIP3; they omit within-ZIP3 dependence,
  fitting and model-selection uncertainty and may understate uncertainty.

## D024 — Loss given default measured in the data (Phase 8)

**Decision.**
- **Scope:** D004 defaults of the PD sample with a complete 60-month follow-up (D + 60
  ≤ cutoff, whatever the outcome).
- **EAD:** the balance at default. When the balance is masked in the first months of the
  loan, the original balance is used.
- **Loss:** Freddie Mac's `actual_loss` for loss exits 02, 03 and 09.
  - It equals balance + accrued interest + expenses − sale proceeds − recoveries, which was
    checked on example rows.
  - Loss exits with no published amount get the severity of the reported loss exits of the
    same period.
  - Payoffs and loans current at the end of the follow-up have a loss of 0.
- **Unresolved defaults:** transfers (15, 16, 96) and loans still delinquent. Scenario
  assumptions: low = no loss, high = a loss exit at the period's severity. These are not
  statistical confidence intervals or guaranteed bounds on actual losses.
- **LGD:** sum(loss) / sum(EAD).

**Results (central, with [low, high]).**
- **By default period:**
  - 2005–2008: 31.5% [23.0, 35.9];
  - 2009–2012: 26.3% [20.2, 31.5];
  - 2017–2021: 1.0% [0.8, 3.6].
- **Defaults from 2014 onwards:** disaster-flagged 0.04% [0.03, 3.08] versus neither flag 2.77%
  [2.11, 5.44]. Disaster-flagged defaults lose less under each matching scenario assumption.
  - Only about 180 disaster-flagged defaults are resolved, with one loss exit; the central
    estimate is therefore fragile.
- **Mortgage insurance:** loans with LTV ≥ 90 have a lower loss severity (32% versus 53% at
  LTV 80–90), consistent with mortgage-insurance recoveries.

**Why.** Excluding unresolved defaults can bias LGD downwards: note sales may be
loss-making but their losses are not published. Scenario ranges make that assumption visible.

## D025 — Stress-test design (Phase 8)

**Decision.**
- **Book:** the 2021–2024 PD-sample loans (114,822 loans, $35.1 bn), with EAD = original
  balance, over a 24-month horizon. EL = PD × LGD × EAD.
- **Baseline:** TTC PD from the D021 primary model, with the benign LGD of 2017–2021 defaults.
- **Recession:** PD × 2.42, the observed / TTC ratio of the 2007–2008 vintages, in-sample,
  with the downturn LGD of 2005–2012 defaults.
- **Hurricane:** extra PD = 1.146 pp (Phase 5 hurricane effect) × coastal share, with every
  coastal ZIP3 hit at once and every extra 60+ day delinquency counted as a default. The LGD is
  that of disaster-flagged defaults, and the high disaster LGD when the hurricane comes during
  a recession.

**Results (central EL, % of balance).**

| Scenario | Central EL | Range |
|---|---|---|
| Baseline | 0.009% | |
| Hurricane increment | +0.0001% | high: +0.010% |
| Recession | 0.59% | [0.45, 0.70] |
| Hurricane in a recession | 0.60% | |

**Why.**
- The scenarios are illustrative, transparent and built only from the project's own
  estimates. The hurricane incidence assumptions are deliberately severe. Hurricane credit
  loss remains small relative to recession loss under the specified LGD scenarios; this is
  conditional on the measured disaster LGD and its limited sample.

**Limits.**
- The TTC baseline is not a current-conditions forecast (D023); it under-predicts observed
  recent default rates, so the baseline absolute loss level may be understated.
- Only the lender's or guarantor's credit loss is covered. Physical damage, insurance costs
  and long-run coastal house-price risk are out of scope.

## D026 — Review fixes to Phase 8

**Changes.**
1. **LGD follow-up coverage.** A default without an exit counts as cured (or still
   delinquent) only if its records reach D + 60 months, within the 2-month gap tolerated
   by D006. Otherwise it is `incomplete_follow_up`, i.e. unresolved and covered by the
   low/high scenarios. Before this fix, a loan whose records stopped early while current would
   have received a zero loss. **Effect:** 1 default reclassified; all LGD estimates
   unchanged.
2. **Recent-conditions baseline.** The stress test also shows the baseline with the TTC PD
   × 1.90, the observed / predicted ratio of the D004 primary model on the test vintages
   (D023). The hurricane increment does not depend on the PD level. A higher PD level only
   raises the recession loss when applied to that scenario.
3. **Break-even of the hurricane scenario.** To match the recession's increment over the
   baseline, the hurricane's default effect would have to be:
   - about 4,000 times larger at the central disaster LGD. This is impossible, since it
     exceeds a 100% default probability;
   - 57 times larger at the high disaster LGD. That means about 65% extra default probability
     for fully coastal loans; partially coastal ZIP3 are scaled by their coastal share.

   These are loss-equivalent multiples, not forecasts.

**Why.** An external review raised these points:
- the PD calibration weakness spreads into the stress test;
- the hurricane mapping is illustrative;
- the LGD cure rule did not check follow-up coverage.

The first two are now quantified. The third was a real bug, now fixed and tested.
