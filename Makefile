.PHONY: help setup data features causal models report lint format test check

help:
	@echo "setup : installer l'environnement et les hooks pre-commit"
	@echo "lint / format / test / check : vérifications du projet"
	@echo "data / features / causal / models / report : étapes à implémenter"

setup:
	uv sync --frozen
	uv run --frozen pre-commit install

data features causal models report:
	@echo "L'étape '$@' n'est pas encore implémentée."
	@exit 1

lint:
	uv run --frozen ruff check .
	uv run --frozen ruff format --check .

format:
	uv run --frozen ruff format .

test:
	uv run --frozen pytest

check: lint test
