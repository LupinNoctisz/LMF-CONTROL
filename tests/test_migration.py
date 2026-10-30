"""Testes da migração JSON -> banco relacional (scripts/migrate_json_to_postgres.py).

Sempre rodam contra um SQLite de arquivo temporário. Para rodar também contra PostgreSQL de teste,
defina LMF_TEST_DATABASE_URL (ex.: postgresql+psycopg://usuario@127.0.0.1:54329/lmf_test). O banco
indicado é ESVAZIADO (DROP das tabelas do LMF) antes de cada teste: use apenas um banco descartável.
"""
import hashlib
import importlib.util
import json
import os
from datetime import datetime, date
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("migrate_script", ROOT / "scripts" / "migrate_json_to_postgres.py")
migrate_script = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migrate_script)

from app.database import Base, JSON_FORMAT, JSON_FORMAT_VERSION  # noqa: E402
from app import models  # noqa: E402

PG_URL = os.getenv("LMF_TEST_DATABASE_URL")


@pytest.fixture(params=["sqlite"] + (["postgres"] if PG_URL else []))
def target_url(request, tmp_path):
    if request.param == "sqlite":
        yield f"sqlite:///{(tmp_path / 'destino.db').as_posix()}"
        return
    engine = create_engine(PG_URL)
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
    engine.dispose()
    yield PG_URL


def make_source_json(path: Path) -> dict:
    """Cria um JSON no mesmo formato gravado pelo app, com dados que costumam quebrar migrações."""
    source = create_engine("sqlite://")
    Base.metadata.create_all(source)
    from sqlalchemy.orm import Session
    with Session(source) as db:
        db.add_all([
            models.Supplier(id=1, name="Distribuidora São João Ltda", contact="José d'Ávila", lead_time_days=3, active=True, notes="Entrega às terças — não aceita \"cheque\""),
            models.Supplier(id=7, name="Fornecedor inativo", active=False),
            models.Customer(id=1, name="Maria da Conceição", document="123.456.789-00", active=True),
        ])
        db.flush()
        db.add_all([
            models.Product(id=1, sku="SKU-001", name="Cabo HDMI 2m", supplier_id=1, quantity=10, min_quantity=2, cost=0.1 + 0.2, price=19.9, active=True),
            models.Product(id=42, sku=None, name="Pelicula 3D", supplier_id=7, quantity=0, cost=1 / 3, price=1e-9, active=False, notes=None),
        ])
        db.commit()
    tables = {}
    with source.connect() as connection:
        for table in Base.metadata.sorted_tables:
            rows = [dict(row._mapping) for row in connection.execute(table.select())]
            tables[table.name] = {"columns": [c.name for c in table.columns], "row_count": len(rows), "records": rows}
    document = {"format": JSON_FORMAT, "format_version": JSON_FORMAT_VERSION, "schema_revision": "json-1", "tables": tables}
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    source.dispose()
    return document


def run(json_path, url, tmp_path, **kwargs):
    options = dict(apply=False, allow_remote=False, backup_dir=tmp_path / "backups")
    options.update(kwargs)
    return migrate_script.migrate(json_path, url, **options)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def table_exists(url, name):
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            return name in connection.dialect.get_table_names(connection)
    finally:
        engine.dispose()


def count(url, table):
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            return connection.execute(text(f'SELECT COUNT(*) FROM "{table}"')).scalar_one()
    finally:
        engine.dispose()


def test_dry_run_writes_nothing_to_the_target(tmp_path, target_url):
    source = tmp_path / "lmf_control.json"
    make_source_json(source)
    report = run(source, target_url, tmp_path)
    assert report["mode"] == "dry-run" and report["would_insert_total"] == 5 and report["inserted_total"] == 0
    assert not table_exists(target_url, "products")
    assert not (tmp_path / "backups").exists()


def test_apply_migrates_validates_and_never_touches_the_source(tmp_path, target_url):
    source = tmp_path / "lmf_control.json"
    make_source_json(source)
    before, mtime = sha(source), source.stat().st_mtime_ns
    report = run(source, target_url, tmp_path, apply=True)
    assert report["validation"] == {"ok": True, "failures": []}
    assert report["inserted_total"] == 5 and report["source_unchanged"] is True
    assert sha(source) == before and source.stat().st_mtime_ns == mtime
    backup = Path(report["backup"])
    assert backup.is_file() and sha(backup) == before
    assert count(target_url, "suppliers") == 2 and count(target_url, "products") == 2


def test_values_survive_exactly(tmp_path, target_url):
    source = tmp_path / "lmf_control.json"
    make_source_json(source)
    run(source, target_url, tmp_path, apply=True)
    engine = create_engine(target_url)
    with engine.connect() as connection:
        products = {row.id: row for row in connection.execute(models.Product.__table__.select())}
        supplier = connection.execute(models.Supplier.__table__.select().where(models.Supplier.id == 1)).one()
    engine.dispose()
    assert products[1].cost == 0.1 + 0.2 and products[42].cost == 1 / 3 and products[42].price == 1e-9
    assert products[42].sku is None and products[42].active is False and products[1].active is True
    assert supplier.name == "Distribuidora São João Ltda" and supplier.contact == "José d'Ávila"
    assert supplier.notes == 'Entrega às terças — não aceita "cheque"'
    assert isinstance(supplier.created_at, datetime)


def test_second_run_is_idempotent(tmp_path, target_url):
    source = tmp_path / "lmf_control.json"
    make_source_json(source)
    run(source, target_url, tmp_path, apply=True)
    second = run(source, target_url, tmp_path, apply=True)
    assert second["inserted_total"] == 0 and second["validation"]["ok"] is True
    assert count(target_url, "products") == 2 and count(target_url, "suppliers") == 2


def test_existing_different_rows_are_reported_and_never_overwritten(tmp_path, target_url):
    source = tmp_path / "lmf_control.json"
    make_source_json(source)
    run(source, target_url, tmp_path, apply=True)
    engine = create_engine(target_url)
    with engine.begin() as connection:
        connection.execute(text("UPDATE suppliers SET name = 'EDITADO NO DESTINO' WHERE id = 1"))
    engine.dispose()
    report = run(source, target_url, tmp_path, apply=True)
    assert any("NÃO sobrescritas" in warning for warning in report["warnings"])
    assert report["validation"]["ok"] is False  # a validação acusa a divergência
    engine = create_engine(target_url)
    with engine.connect() as connection:
        assert connection.execute(text("SELECT name FROM suppliers WHERE id = 1")).scalar_one() == "EDITADO NO DESTINO"
    engine.dispose()


def test_failure_rolls_back_everything(tmp_path, target_url):
    source = tmp_path / "lmf_control.json"
    document = make_source_json(source)
    # Duplica um SKU com outro id: viola a restrição UNIQUE no meio da migração.
    document["tables"]["products"]["records"].append({**document["tables"]["products"]["records"][0], "id": 99})
    source.write_text(json.dumps(document, ensure_ascii=False, default=str), encoding="utf-8")
    with pytest.raises(Exception):
        run(source, target_url, tmp_path, apply=True)
    # Transação única: nem os fornecedores (inseridos antes dos produtos) devem ter ficado.
    assert not table_exists(target_url, "suppliers") or count(target_url, "suppliers") == 0


def test_refuses_remote_supabase_without_explicit_flag(tmp_path):
    source = tmp_path / "lmf_control.json"
    make_source_json(source)
    url = "postgresql+psycopg://postgres:segredo@db.abcdefgh.supabase.co:5432/postgres"
    with pytest.raises(migrate_script.MigrationError, match="allow-remote"):
        run(source, url, tmp_path, apply=True)
    assert not (tmp_path / "backups").exists()


def test_schema_warnings_report_data_that_would_be_lost(tmp_path):
    source = tmp_path / "lmf_control.json"
    document = make_source_json(source)
    document["tables"]["tabela_antiga"] = {"columns": ["id"], "row_count": 1, "records": [{"id": 1}]}
    document["tables"]["suppliers"]["columns"].append("coluna_removida")
    warnings = migrate_script.schema_warnings(document)
    assert any("tabela_antiga" in w for w in warnings) and any("coluna_removida" in w for w in warnings)


def test_missing_source_file_is_a_clear_error(tmp_path):
    with pytest.raises(migrate_script.MigrationError):
        run(tmp_path / "nao-existe.json", f"sqlite:///{(tmp_path / 'x.db').as_posix()}", tmp_path)


def test_new_rows_after_migration_get_ids_above_the_migrated_ones(tmp_path, target_url):
    """No PostgreSQL, ids explícitos não avançam a sequência sozinhos: ela precisa ser reajustada."""
    source = tmp_path / "lmf_control.json"
    make_source_json(source)
    run(source, target_url, tmp_path, apply=True)
    engine = create_engine(target_url)
    with engine.begin() as connection:
        connection.execute(models.Supplier.__table__.insert(), {"name": "Novo fornecedor", "active": True})
        connection.execute(models.Product.__table__.insert(), {"name": "Novo produto", "quantity": 0, "min_quantity": 0, "cost": 0, "price": 0, "active": True})
        new_supplier = connection.execute(text("SELECT MAX(id) FROM suppliers")).scalar_one()
        new_product = connection.execute(text("SELECT MAX(id) FROM products")).scalar_one()
    engine.dispose()
    assert new_supplier == 8 and new_product == 43
