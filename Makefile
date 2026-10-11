.PHONY: help setup data features causal models report lint format test check all

help:
	@echo "setup: install dependencies and pre-commit hooks"
	@echo "lint / format / test / check: project checks"
	@echo "data: ingest and validate; other pipeline stages: pending"

setup:
	uv sync --frozen
	uv run --frozen pre-commit install

data:
	uv run --frozen python -m climrisk.ingest.freddie --all
	uv run --frozen python -m climrisk.ingest.validate

features:
	uv run --frozen python -m climrisk.features.target
	uv run --frozen python -m climrisk.features.panel

causal:
	@echo "TODO phases 5-6: event study, pricing"

models:
	@echo "TODO phases 7-8: PD model, stress test"

report:
	@echo "TODO phase 9: figures and report"

lint:
	uv run --frozen ruff check .
	uv run --frozen ruff format --check .

format:
	uv run --frozen ruff format .

test:
	uv run --frozen pytest

check: lint test

all: data features causal models report
