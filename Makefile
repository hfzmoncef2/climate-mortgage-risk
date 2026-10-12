.PHONY: help setup data features causal models report lint format test check all

help:
	@echo "setup: install dependencies and pre-commit hooks"
	@echo "lint / format / test / check: project checks"
	@echo "data / features / causal / models / report: reproduce pipeline stages"

setup:
	uv sync --frozen
	uv run --frozen pre-commit install

data:
	uv run --frozen python -m climrisk.ingest.freddie --all
	uv run --frozen python -m climrisk.ingest.validate
	uv run --frozen python -m climrisk.ingest.fema
	uv run --frozen python -m climrisk.ingest.fred
	uv run --frozen python -m climrisk.ingest.census

features:
	uv run --frozen python -m climrisk.features.target
	uv run --frozen python -m climrisk.features.panel
	uv run --frozen python -m climrisk.features.climate
	uv run --frozen python -m climrisk.features.coast

causal:
	uv run --frozen python -m climrisk.causal.event_study
	uv run --frozen python -m climrisk.causal.robustness
	uv run --frozen python -m climrisk.causal.pricing

models:
	uv run --frozen python -m climrisk.features.pd_features
	uv run --frozen python -m climrisk.features.credit_default
	uv run --frozen python -m climrisk.models.scorecard
	uv run --frozen python -m climrisk.models.gbm
	uv run --frozen python -m climrisk.models.calibration
	uv run --frozen python -m climrisk.models.ttc
	uv run --frozen python -m climrisk.models.ablation
	uv run --frozen python -m climrisk.models.final
	uv run --frozen python -m climrisk.models.sensitivity

report:
	uv run --frozen python -m climrisk.viz.eda

lint:
	uv run --frozen ruff check .
	uv run --frozen ruff format --check .

format:
	uv run --frozen ruff format .

test:
	uv run --frozen pytest

check: lint test

all: data features causal models report
