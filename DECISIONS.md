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
