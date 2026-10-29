"""Migração idempotente do banco JSON do LMF Control para PostgreSQL (Supabase).

Garantias:
  * O arquivo JSON de origem é aberto SOMENTE para leitura; nunca é alterado nem apagado.
  * Sem --apply, nada é gravado no destino (modo de simulação / dry-run).
  * Só insere registros ausentes. Nunca atualiza nem apaga linhas já existentes no destino:
    linhas com o mesmo id e conteúdo diferente são apenas relatadas ("divergentes").
  * Todas as inserções ocorrem em uma única transação: se algo falhar, nada é gravado.
  * Antes de gravar, uma cópia do JSON é salva (sem sobrescrever) na pasta de backup.
  * Após gravar, valida contagens e o conteúdo de cada linha lido de volta do destino.
  * Destinos do Supabase exigem --allow-remote além de --apply.

Uso:
  python scripts/migrate_json_to_postgres.py --json CAMINHO.json --database-url URL            # simula
  python scripts/migrate_json_to_postgres.py --json CAMINHO.json --database-url URL --apply    # grava
A URL também pode vir de LMF_DATABASE_URL (ex.: postgresql+psycopg://usuario:senha@host:5432/banco).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

# Importar app.database cria pastas de backup; aponta tudo para um diretório temporário
# para que a migração não deixe efeitos colaterais na máquina do usuário.
_SCRATCH = tempfile.mkdtemp(prefix="lmf-migrate-")
for _name in ("LMF_DATA_DIR", "LMF_BACKUP_DIR", "LMF_IMPORT_BACKUP_DIR"):
    os.environ[_name] = _SCRATCH

from sqlalchemy import create_engine, func, select, text  # noqa: E402
from sqlalchemy.engine import Engine, make_url  # noqa: E402

from app import models  # noqa: E402,F401  (registra as tabelas no metadata)
from app.database import Base, _normalize_loaded_value, _read_json  # noqa: E402

REMOTE_MARKERS = ("supabase.co", "supabase.com", "pooler.supabase")


class MigrationError(RuntimeError):
    pass


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalized_rows(table, document: dict) -> list[dict]:
    entry = document.get("tables", {}).get(table.name, {})
    records = entry.get("records", []) if isinstance(entry, dict) else []
    columns = {column.name: column for column in table.columns}
    return [
        {name: _normalize_loaded_value(columns[name], record.get(name)) for name in columns if name in record}
        for record in records
    ]


def schema_warnings(document: dict) -> list[str]:
    """Dados do JSON que o esquema atual NÃO consegue receber (seriam perdidos)."""
    warnings = []
    known = {table.name: {column.name for column in table.columns} for table in Base.metadata.sorted_tables}
    for name, entry in document.get("tables", {}).items():
        if name not in known:
            count = len(entry.get("records", [])) if isinstance(entry, dict) else 0
            warnings.append(f"Tabela '{name}' do JSON não existe no esquema ({count} registro(s) NÃO serão migrados).")
            continue
        extra = set(entry.get("columns", [])) - known[name] if isinstance(entry, dict) else set()
        if extra:
            warnings.append(f"Tabela '{name}': colunas {sorted(extra)} não existem no esquema e NÃO serão migradas.")
    return warnings


def is_remote(url: str) -> bool:
    host = (make_url(url).host or "").lower()
    return any(marker in host for marker in REMOTE_MARKERS)


def _group_by_keys(rows: list[dict]):
    groups = defaultdict(list)
    for row in rows:
        groups[tuple(sorted(row))].append(row)
    return groups.values()


def _existing_rows(connection, table) -> dict:
    return {row._mapping["id"]: dict(row._mapping) for row in connection.execute(table.select())}


def _same(source: dict, target: dict) -> bool:
    return all(target.get(name) == value for name, value in source.items())


def plan(connection, document: dict, tables_exist: bool) -> dict:
    result = {}
    for table in Base.metadata.sorted_tables:
        rows = normalized_rows(table, document)
        existing = _existing_rows(connection, table) if tables_exist else {}
        to_insert, same, diverged = [], 0, []
        for row in rows:
            current = existing.get(row.get("id"))
            if current is None:
                to_insert.append(row)
            elif _same(row, current):
                same += 1
            else:
                diverged.append(row.get("id"))
        result[table.name] = {"source": len(rows), "to_insert": to_insert, "already_present": same, "diverged": diverged}
    return result


def reset_sequences(connection) -> None:
    if connection.dialect.name != "postgresql":
        return
    for table in Base.metadata.sorted_tables:
        if "id" not in table.columns:
            continue
        connection.execute(
            text("SELECT setval(pg_get_serial_sequence(:t, 'id'), COALESCE((SELECT MAX(id) FROM " + f'"{table.name}"' + "), 1), (SELECT MAX(id) IS NOT NULL FROM " + f'"{table.name}"' + "))"),
            {"t": table.name},
        )


def validate(engine: Engine, document: dict) -> dict:
    """Relê o destino e confere cada linha de origem. Retorna as falhas encontradas."""
    failures = []
    with engine.connect() as connection:
        for table in Base.metadata.sorted_tables:
            rows = normalized_rows(table, document)
            existing = _existing_rows(connection, table)
            missing = [row.get("id") for row in rows if row.get("id") not in existing]
            different = [row.get("id") for row in rows if row.get("id") in existing and not _same(row, existing[row["id"]])]
            if missing:
                failures.append(f"{table.name}: {len(missing)} registro(s) ausentes no destino (ids {missing[:5]}...)")
            if different:
                failures.append(f"{table.name}: {len(different)} registro(s) com conteúdo diferente (ids {different[:5]}...)")
            total = connection.execute(select(func.count()).select_from(table)).scalar_one()
            if total < len(rows):
                failures.append(f"{table.name}: destino tem {total} linhas, origem tem {len(rows)}")
    return {"ok": not failures, "failures": failures}


def backup_source(json_path: Path, backup_dir: Path, digest: str) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    # "xb" falha se o arquivo já existir: nunca sobrescreve uma cópia anterior. Em caso de
    # colisão de nome, tenta um sufixo numérico em vez de abortar uma execução legítima.
    for attempt in range(1, 1000):
        suffix = "" if attempt == 1 else f"_{attempt}"
        target = backup_dir / f"lmf_control_pre-migracao_{stamp}_{digest[:8]}{suffix}.json"
        try:
            with json_path.open("rb") as source, target.open("xb") as destination:
                shutil.copyfileobj(source, destination)
            break
        except FileExistsError:
            continue
    else:
        raise MigrationError("Não foi possível criar um nome único para a cópia de segurança.")
    if sha256_of(target) != digest:
        raise MigrationError("A cópia de segurança do JSON não confere com o original; migração abortada.")
    return target


def migrate(json_path: Path, database_url: str, *, apply: bool, allow_remote: bool, backup_dir: Path | None) -> dict:
    if not json_path.is_file():
        raise MigrationError(f"Arquivo JSON não encontrado: {json_path}")
    if apply and is_remote(database_url) and not allow_remote:
        raise MigrationError("O destino parece ser o Supabase remoto. Para gravar, repita com --allow-remote (autorização explícita).")
    digest_before = sha256_of(json_path)
    document = _read_json(json_path)
    engine = create_engine(database_url, future=True)
    report = {
        "mode": "apply" if apply else "dry-run",
        "source": str(json_path),
        "source_sha256": digest_before,
        "target": make_url(database_url).render_as_string(hide_password=True),
        "warnings": schema_warnings(document),
        "tables": {},
        "backup": None,
        "validation": None,
    }
    try:
        with engine.connect() as probe:
            existing_tables = set(probe.dialect.get_table_names(probe))
        tables_exist = {t.name for t in Base.metadata.sorted_tables} <= existing_tables
        with engine.connect() as connection:
            if not tables_exist and not apply:
                planned = {t.name: {"source": len(normalized_rows(t, document)), "to_insert": normalized_rows(t, document), "already_present": 0, "diverged": []} for t in Base.metadata.sorted_tables}
            else:
                planned = plan(connection, document, tables_exist) if tables_exist else None
        if planned is None:  # apply com esquema ausente: tudo será inserido
            planned = {t.name: {"source": len(normalized_rows(t, document)), "to_insert": normalized_rows(t, document), "already_present": 0, "diverged": []} for t in Base.metadata.sorted_tables}
        for name, info in planned.items():
            report["tables"][name] = {k: (len(v) if k == "to_insert" else v) for k, v in info.items()}
        diverged = {n: i["diverged"] for n, i in planned.items() if i["diverged"]}
        if diverged:
            report["warnings"].append(f"Linhas já existentes no destino com conteúdo diferente (NÃO sobrescritas): {diverged}")
        report["schema_created"] = (not tables_exist) if apply else None
        report["would_create_schema"] = not tables_exist

        if apply:
            report["backup"] = str(backup_source(json_path, backup_dir or (PROJECT_ROOT / "data" / "backups" / "pre-migracao"), digest_before))
            with engine.begin() as connection:  # transação única
                Base.metadata.create_all(connection)
                for table in Base.metadata.sorted_tables:
                    for group in _group_by_keys(planned[table.name]["to_insert"]):
                        connection.execute(table.insert(), group)
                reset_sequences(connection)
            report["validation"] = validate(engine, document)
    finally:
        engine.dispose()
    if sha256_of(json_path) != digest_before:
        raise MigrationError("ERRO GRAVE: o JSON de origem mudou durante a migração.")
    report["source_unchanged"] = True
    report["inserted_total"] = sum(t["to_insert"] for t in report["tables"].values()) if apply else 0
    report["would_insert_total"] = sum(t["to_insert"] for t in report["tables"].values())
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--json", required=True, type=Path, help="Caminho do lmf_control.json (somente leitura).")
    parser.add_argument("--database-url", default=os.getenv("LMF_DATABASE_URL"), help="URL SQLAlchemy do destino (ou LMF_DATABASE_URL).")
    parser.add_argument("--apply", action="store_true", help="Grava no destino. Sem isso, apenas simula.")
    parser.add_argument("--allow-remote", action="store_true", help="Autoriza gravar em destino Supabase remoto.")
    parser.add_argument("--backup-dir", type=Path, help="Onde salvar a cópia do JSON antes de gravar.")
    parser.add_argument("--report", type=Path, help="Salva o relatório JSON neste arquivo.")
    args = parser.parse_args()
    if not args.database_url:
        parser.error("informe --database-url ou defina LMF_DATABASE_URL.")
    try:
        report = migrate(args.json, args.database_url, apply=args.apply, allow_remote=args.allow_remote, backup_dir=args.backup_dir)
    except (MigrationError, OSError, ValueError) as error:
        print(f"Erro: {error}", file=sys.stderr)
        return 1
    except Exception as error:  # falha do banco: a transação foi revertida
        print(f"Erro no banco de dados (nada foi gravado): {type(error).__name__}: {error}", file=sys.stderr)
        return 1
    text_report = json.dumps(report, ensure_ascii=False, indent=2, default=str)
    if args.report:
        args.report.write_text(text_report + "\n", encoding="utf-8")
    print(text_report)
    return 0 if (report["validation"] is None or report["validation"]["ok"]) else 2


if __name__ == "__main__":
    raise SystemExit(main())
