.PHONY: help data features causal models report test

help:
	@echo "Étapes prévues : data, features, causal, models, report (à implémenter)."
	@echo "test : lancer pytest une fois les tests de transformations ajoutés."

data features causal models report:
	@echo "L'étape '$@' n'est pas encore implémentée."
	@exit 1

test:
	python -m pytest
