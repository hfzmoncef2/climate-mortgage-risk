.PHONY: help setup data features causal models report lint format test check all

help:
	@echo "setup : installer l'environnement et les hooks pre-commit"
	@echo "lint / format / test / check : vérifications du projet"
	@echo "data / features / causal / models / report : étapes à implémenter"

setup:
	uv sync --frozen
	uv run --frozen pre-commit install

data:
	@echo "TODO phase 1 : ingestion Freddie Mac"

features:
	@echo "TODO phase 2-3 : cible, panel, exposition climat"

causal:
	@echo "TODO phase 5-6 : event study, pricing"

models:
	@echo "TODO phase 7-8 : modele de PD, stress test"

report:
	@echo "TODO phase 9 : figures et rapport"

lint:
	uv run --frozen ruff check .
	uv run --frozen ruff format --check .

format:
	uv run --frozen ruff format .

test:
	uv run --frozen pytest

check: lint test

all: data features causal models report
