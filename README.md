# Is Climate Risk Priced in Mortgage Credit?

## Résultats

Projet en cours d'initialisation. Aucun résultat empirique disponible à ce stade.

## Reproduire

Python 3.12, installé et sélectionné par uv via `.python-version`.

Installer [uv](https://docs.astral.sh/uv/getting-started/installation/), puis :

```sh
uv sync --frozen
uv run --frozen pre-commit install
make check
```

`uv.lock` fixe les versions des dépendances. `make setup` réinstalle cet
environnement et les hooks. Ruff contrôle le style et pytest les tests.

## Phase 0 — Setup

- [x] Architecture, README et journal des décisions.
- [x] Licence MIT pour le code ; les données conservent leurs conditions propres.
- [x] Données exclues de Git dès le premier commit.
- [x] Environnement uv, Ruff, pytest et hooks pre-commit.
- [x] Cibles Make du pipeline réservées.
- [ ] Inscription personnelle Clarity (Freddie Mac).
- [ ] Inscription personnelle HUD USER.

### Accès aux données

Pour Freddie Mac, ouvrir [Clarity](https://capitalmarkets.freddiemac.com/clarity)
et créer un compte personnel pour accéder au
[Single-Family Loan-Level Dataset](https://www.freddiemac.com/research/datasets/sf-loanlevel-dataset).
Terminer la validation de l'adresse e-mail et les conditions d'accès sur le site.

Pour les correspondances géographiques, ouvrir la page
[HUD-USPS Crosswalk](https://www.huduser.gov/portal/datasets/usps_crosswalk.html)
et suivre « Register Here ». Pour une utilisation par API, suivre le lien
vers la page API afin de créer le compte et obtenir un jeton.
Stocker les téléchargements sous `data/raw/` et les jetons dans un fichier
`.env` local (ignoré par Git). Ne jamais les placer dans un notebook versionné.



Les commandes `make data`, `make features`, `make causal`, `make models` et
`make report` sont réservées aux étapes du pipeline à implémenter.
Elles affichent un TODO. `make all` les énumère, mais ne produit encore aucun résultat.

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

## Développement

`make setup`, puis `make test` et `make lint`. Dans VS Code, sélectionner
`.venv/bin/python` avec « Python: Select Interpreter » si un ancien interpréteur
reste sélectionné. Le chemin par défaut est configuré dans `.vscode/settings.json`.

Les données locales sont les samples Freddie Mac 2004–2024. Les analyses futures
combineront ces prêts, les déclarations FEMA et le FEMA National Risk Index.
Les dépendances de phase 1 sont DuckDB, Polars, PyArrow, PyYAML et openpyxl ;
ipykernel est disponible pour les notebooks.

Les hooks corrigent le style, les espaces et les fins de fichiers, et refusent
les fichiers ajoutés de plus de 1000 Ko. Cette limite ne détecte pas toutes les
données : `data/` doit rester ignoré, et le contenu des commits doit être vérifié.
