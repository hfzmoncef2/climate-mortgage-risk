# Journal des décisions méthodologiques

Pour chaque choix, consigner la date, le contexte, les options envisagées,
la décision, sa justification et ses conséquences.

## Initialisation

- Architecture : package Python `climrisk` sous `src/`.
- Données : `data/` entièrement exclu du versionnement.
- Notebooks : exploration uniquement ; transformations réutilisables dans le package.
- Schémas, splits et paramètres : à définir dans `configs/` avant les analyses.
- Aucun choix de cible, d'échantillon ou de méthode statistique arrêté à ce stade.

## 2026-10-11 — Environnement et diffusion

- Utiliser uv et versionner uv.lock pour reproduire les dépendances.
- Installer Ruff, pytest et pre-commit dans le groupe de développement.
- Vérifier lint et formatage avant les commits via des hooks locaux utilisant uv.lock.
- Diffuser le code sous licence MIT et rendre le dépôt public selon la phase 0.
- Conserver les étapes du pipeline comme squelettes explicites.
- Les inscriptions Clarity et HUD USER restent personnelles et à finaliser sur leurs portails.

## D001 — Version des données Freddie Mac (2026-10-11)

- Confirmé localement : 21 archives sample 2004–2024, avec 42 fichiers origination/performance.
- Documents locaux : `file_layout_july_2026.xlsx` et `general_user_guide_july_2026.pdf`.
- Contrôle initial : 31 champs origination et 35 performance sur la première ligne de chacune des 21 années.
- Métadonnées annoncées, à vérifier dans les documents : fichiers datés du 2026-09-04,
  layout juillet 2026 et cutoff 2026-03-31.
- Pourquoi : figer la version des sources avant l'ingestion.

## D002 — Correspondance ZIP → comté (proposition à valider)

- Proposition : relation Census 2020 ZCTA → county (part de surface), pondérée
  par les logements de chaque ZCTA (Census 2020, API).
- Alternatives : HUD-USPS Crosswalk et Geocorr 2022. Les erreurs 403 signalées
  depuis la Suisse ne sont pas vérifiées dans cette session.
- Limite : ZCTA ≠ ZIP USPS ; l'approximation surfacique et son erreur restent à quantifier.
- À faire : examiner les ZCTA vides et la compatibilité des ZIP disponibles chez Freddie Mac.
- Aucun choix final ni hypothèse d'erreur faible sans validation empirique.

## D003 — Configuration Python de phase 1

- Python 3.12 via uv ; Ruff E/F/I/B/UP, longueur de ligne 100.
- Ajouter uniquement les outils d'ingestion et de notebooks à ce stade.
- Conserver le nom de distribution `climate-mortgage-risk` et le package `climrisk`.

## D004 — Ingestion typée et ZIP3

- Corriger le code postal : les samples publient un ZIP3 de 3 caractères ; `000` est inconnu.
- Conserver les codes catégoriels en texte, dont `00`, `01`, `RA` ; convertir `XX` en NULL.
- Nettoyer les espaces, vides et sentinelles avant les conversions strictes DuckDB.
- Convertir YYYYMM en DATE au premier du mois et ajouter `vintage` (année du fichier).
- Écrire un Parquet ZSTD par table et année via un fichier temporaire, nettoyé en cas d’erreur.
- Validation 2005 : 50 000 lignes origination et 3 877 176 lignes performance.
