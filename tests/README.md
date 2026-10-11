# Tests

`make test` lance pytest. Les premiers tests vérifient que les données sont
ignorées et qu'aucun fichier de `data/` n'est suivi par Git.

Ajouter ensuite des tests pour chaque transformation implémentée : schémas,
clés de jointure, valeurs manquantes, cible et absence de fuite temporelle.
