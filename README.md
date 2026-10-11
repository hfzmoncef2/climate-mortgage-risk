# Climate Mortgage Risk

## Résultats

Projet en cours d'initialisation. Aucun résultat empirique disponible à ce stade.

## Reproduire

Python 3.11 ou supérieur.

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
make help
```

Les commandes `make data`, `make features`, `make causal`, `make models` et
`make report` sont réservées aux étapes du pipeline à implémenter.
Elles échouent explicitement tant que ces étapes ne sont pas disponibles.

## Organisation

- `configs/` : schémas des colonnes, dates de split et paramètres.
- `data/{raw,interim,processed}/` : données locales, entièrement exclues de Git.
- `src/climrisk/ingest/` : Freddie Mac, FEMA, correspondances géographiques et FRED.
- `src/climrisk/features/` : cible, panel et exposition.
- `src/climrisk/causal/` : event study et pricing.
- `src/climrisk/models/` : scorecard, GBM, calibration et évaluation.
- `src/climrisk/stress/` : scénarios et pertes attendues.
- `notebooks/` : exploration uniquement, fichiers préfixés `01_`, `02_`, etc.
- `tests/` : futurs tests de chaque transformation de données.
- `reports/` : figures et rapport LaTeX.

Les modules sont des squelettes ; aucune ingestion ni estimation n'est encore implémentée.
Les décisions méthodologiques sont consignées dans `DECISIONS.md`.
