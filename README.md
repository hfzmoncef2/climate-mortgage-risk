# Is Climate Risk Priced in Mortgage Credit?

Work in progress: Freddie Mac loan-level data, FEMA disaster declarations,
and the FEMA National Risk Index. No empirical modeling results are available yet.

## Reproduce

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then run:

```sh
make setup
make check
```

Python 3.12 is selected by `.python-version`. Dependencies are pinned in `uv.lock`.
Phase 1 uses DuckDB, Polars, PyArrow, PyYAML and openpyxl. Development tools include
pytest, Ruff, pre-commit and ipykernel. Later modeling libraries will be added as needed.

## Data access

Register for [Freddie Mac Clarity](https://capitalmarkets.freddiemac.com/clarity)
to access the [Single-Family Loan-Level Dataset](https://www.freddiemac.com/research/datasets/sf-loanlevel-dataset).
Place the 2004-2024 sample files in `data/raw/freddie/`:
`sample_orig_YYYY.txt` and `sample_perf_YYYY.txt`.
Reference documents belong in `data/raw/freddie/docs/`.
Data is not redistributed in this repository and retains its original terms.

The [HUD-USPS crosswalk page](https://www.huduser.gov/portal/datasets/usps_crosswalk.html)
provides registration and API access instructions for a potential geographic mapping source.
Geographic allocation remains subject to validation; Freddie Mac samples expose ZIP3 only.
Keep credentials in an ignored local `.env` file and downloads under `data/`.

## Ingestion and quality checks

```sh
uv run --frozen python -m climrisk.ingest.freddie --years 2005
make data
```

`make data` ingests every available source year and runs quality checks.
Outputs are ZSTD-compressed Parquet files under
`data/interim/freddie/{origination,performance}/YYYY.parquet`.
Filenames retain the source year. Every row carries identifier-derived `vintage`
and `orig_quarter`, plus `source_year` for traceability (decision D003).

The YAML schema defines strict types, monthly dates and unknown-value sentinels.
Whitespace and missing codes become NULL before conversion. Categorical codes and
ZIP3 remain text. Monthly dates use the first day of the month.
Each output is written to a temporary file and atomically renamed; temporary files
are cleaned up on errors. Rerunning replaces existing outputs. Atomicity applies
to each file, not the whole run.

Validation writes aggregate statistics to `reports/data_quality.md`.
Blocking failures cause a nonzero exit status; warnings remain visible for review.
Data and the virtual environment are excluded from Git.

## Project structure

- `configs/`: source schemas, future split dates and method parameters.
- `data/{raw,interim,processed}/`: local data, entirely excluded from Git.
- `src/climrisk/ingest/`: Freddie Mac ingestion and validation; FEMA, crosswalk and FRED placeholders.
- `src/climrisk/features/`: target, panel and exposure placeholders.
- `src/climrisk/causal/`: event study and pricing placeholders.
- `src/climrisk/models/`: scorecard, GBM, calibration and evaluation placeholders.
- `src/climrisk/stress/`: scenario and expected loss placeholders.
- `notebooks/`: exploration only, with `01_`, `02_`, etc. filename prefixes.
- `tests/`: synthetic-data transformation tests and repository safeguards.
- `reports/`: figures, aggregate quality report and LaTeX report skeleton.

`make all` runs ingestion and validation, then prints TODO messages for the
remaining pipeline stages. It does not yet run a complete research pipeline.
Methodological choices and unresolved questions are recorded in `DECISIONS.md`.

## Development

Use `make test`, `make lint` and `make format`.
Pre-commit checks lint, formatting, trailing whitespace, final newlines and newly
added files larger than 1000 KB. The size guard does not detect all sensitive data;
keep `data/` ignored and review staged changes.
VS Code defaults to `.venv/bin/python`; select it explicitly if an old interpreter persists.
Conda automatic base activation is disabled locally; an existing terminal may still
need `deactivate` followed by `conda deactivate`.

## License

Code is licensed under MIT. Source datasets retain their own access and licensing terms.
