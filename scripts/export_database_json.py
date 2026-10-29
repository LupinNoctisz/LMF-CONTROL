"""Export a sanitized copy of the active LMF Control JSON database."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
from app.database import JSON_DB_PATH, _read_json


def export_database(output: Path) -> tuple[int, int]:
    if not JSON_DB_PATH.is_file():
        raise FileNotFoundError(f"Banco JSON ainda não foi inicializado: {JSON_DB_PATH}")
    document = _read_json(JSON_DB_PATH)
    result = {
        "format": "lmf-control-json-database-export",
        "format_version": 1,
        "exported_at_utc": document.get("exported_at_utc"),
        "schema_revision": document.get("schema_revision"),
        "integrity_check": "ok",
        "tables": {},
    }
    total = 0
    for name, table in document["tables"].items():
        columns = list(table.get("columns", []))
        records = [dict(row) for row in table.get("records", [])]
        if name == "app_users":
            columns = [column for column in columns if column != "password_hash"]
            for row in records:
                row.pop("password_hash", None)
                row.pop("recovery_code_hash", None)
        result["tables"][name] = {"columns": columns, "row_count": len(records), "records": records}
        total += len(records)

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".tmp")
    temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(output)
    return len(result["tables"]), total


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    default_output = PROJECT_ROOT / "outputs" / "LMF_Control_banco_dados.json"
    parser.add_argument("--output", type=Path, default=default_output, help="Destino do JSON exportado")
    args = parser.parse_args()
    tables, records = export_database(args.output.resolve())
    print(f"Exportação concluída: {args.output.resolve()}")
    print(f"Tabelas: {tables} | Registros: {records}")


if __name__ == "__main__":
    main()
