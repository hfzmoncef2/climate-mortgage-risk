"""Ingestion des fichiers sample Freddie Mac (SFLLD) : texte brut -> Parquet typé.

Pour chaque millésime, lit sample_orig_YYYY.txt et sample_perf_YYYY.txt, applique le
schéma de configs/freddie_schema.yaml (noms, types, codes "inconnu" -> NULL) et écrit :
    data/interim/freddie/origination/YYYY.parquet
    data/interim/freddie/performance/YYYY.parquet

Usage :
    uv run --frozen python -m climrisk.ingest.freddie --years 2005
    uv run --frozen python -m climrisk.ingest.freddie --all
"""

from __future__ import annotations

import argparse
import logging
import time
from pathlib import Path

import duckdb
import yaml

ROOT = Path(__file__).resolve().parents[3]
SCHEMA_PATH = ROOT / "configs" / "freddie_schema.yaml"
RAW_DIR = ROOT / "data" / "raw" / "freddie"
OUT_DIR = ROOT / "data" / "interim" / "freddie"

TABLES = {
    "origination": "sample_orig_{year}.txt",
    "performance": "sample_perf_{year}.txt",
}

# Types du schéma -> types DuckDB. On choisit le plus petit type entier suffisant :
# moins de mémoire et des fichiers Parquet plus légers.
DUCKDB_TYPES = {
    "string": "VARCHAR",
    "Int8": "TINYINT",
    "Int16": "SMALLINT",
    "Int32": "INTEGER",
    "Float64": "DOUBLE",
}

log = logging.getLogger(__name__)


def load_schema(path: Path = SCHEMA_PATH) -> dict:
    """Charge le schéma YAML et vérifie que les positions vont de 1 à N sans trou."""
    with open(path, encoding="utf-8") as f:
        schema = yaml.safe_load(f)
    for table in TABLES:
        positions = [col["position"] for col in schema[table]]
        if positions != list(range(1, len(positions) + 1)):
            raise ValueError(f"{table} : positions manquantes ou dans le désordre")
        unknown = {col["dtype"] for col in schema[table]} - set(DUCKDB_TYPES) - {"yyyymm"}
        if unknown:
            raise ValueError(f"{table} : types inconnus {unknown}")
    return schema


def _sql_str(value: object) -> str:
    """Transforme une valeur Python en littéral SQL entre apostrophes."""
    return "'" + str(value).replace("'", "''") + "'"


def column_expression(col: dict) -> str:
    """Expression SQL qui nettoie et type une colonne lue en texte brut.

    1. TRIM : enlève les espaces parasites.
    2. Vide ou code "inconnu" (9999, 999, XX...) -> NULL.
    3. Conversion vers le type final ; YYYYMM -> DATE (1er du mois).
    Les conversions sont strictes : une valeur inattendue fait échouer l'ingestion
    au lieu de devenir silencieusement NULL.
    """
    name = col["name"]
    raw = f'TRIM("{name}")'
    null_if = [f"{raw} = ''"]
    if col["missing_codes"]:
        codes = ", ".join(_sql_str(code) for code in col["missing_codes"])
        null_if.append(f"{raw} IN ({codes})")
    value = f"CASE WHEN {' OR '.join(null_if)} THEN NULL ELSE {raw} END"

    if col["dtype"] == "yyyymm":
        value = f"CAST(strptime({value}, '%Y%m') AS DATE)"
    elif col["dtype"] != "string":
        value = f"CAST({value} AS {DUCKDB_TYPES[col['dtype']]})"
    return f'{value} AS "{name}"'


def build_query(columns: list[dict], source: Path, year: int) -> str:
    """Requête qui lit un fichier brut (tout en texte) et renvoie la table typée."""
    names = ", ".join(f"{_sql_str(col['name'])}: 'VARCHAR'" for col in columns)
    select = ",\n    ".join(column_expression(col) for col in columns)
    return f"""
SELECT
    {select},
    CAST({year} AS SMALLINT) AS vintage
FROM read_csv(
    {_sql_str(source)},
    delim = '|',
    header = false,
    columns = {{{names}}},
    auto_detect = false
)"""


def ingest_year(
    con: duckdb.DuckDBPyConnection,
    schema: dict,
    year: int,
    raw_dir: Path = RAW_DIR,
    out_dir: Path = OUT_DIR,
) -> dict[str, int]:
    """Convertit un millésime (origination + performance) en Parquet. Renvoie les lignes écrites."""
    counts = {}
    for table, pattern in TABLES.items():
        source = raw_dir / pattern.format(year=year)
        if not source.exists():
            raise FileNotFoundError(source)

        target = out_dir / table / f"{year}.parquet"
        target.parent.mkdir(parents=True, exist_ok=True)
        # On écrit dans un fichier temporaire puis on renomme : si le script plante,
        # aucun Parquet à moitié écrit ne reste dans data/interim/.
        tmp = target.with_name(target.name + ".tmp")

        start = time.perf_counter()
        query = build_query(schema[table], source, year)
        try:
            con.execute(f"COPY ({query}) TO {_sql_str(tmp)} (FORMAT PARQUET, COMPRESSION ZSTD)")
            tmp.replace(target)
        finally:
            tmp.unlink(missing_ok=True)

        counts[table] = con.execute(
            f"SELECT count(*) FROM read_parquet({_sql_str(target)})"
        ).fetchone()[0]
        elapsed = time.perf_counter() - start
        log.info("%s %d : %s lignes (%.1f s)", table, year, f"{counts[table]:,}", elapsed)
    return counts


def available_years(raw_dir: Path = RAW_DIR) -> list[int]:
    """Millésimes pour lesquels un fichier d'origination est présent."""
    return sorted(int(p.stem.split("_")[-1]) for p in raw_dir.glob("sample_orig_*.txt"))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Ingestion Freddie Mac -> Parquet")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--years", type=int, nargs="+", help="ex. --years 2005 2006")
    group.add_argument("--all", action="store_true", help="tous les millésimes de data/raw")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    schema = load_schema()
    years = available_years() if args.all else args.years
    if not years:
        raise SystemExit(f"Aucun fichier sample_orig_*.txt dans {RAW_DIR}")

    with duckdb.connect() as con:
        for year in years:
            ingest_year(con, schema, year)
    log.info("Terminé : %d millésime(s)", len(years))


if __name__ == "__main__":
    main()
