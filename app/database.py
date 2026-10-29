"""JSON is the durable LMF Control database; an in-memory ORM cache serves requests."""

from __future__ import annotations

import base64
import json
import os
import shutil
import sqlite3  # one-time migration and legacy-backup compatibility only
import threading
from datetime import date, datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool

PROJECT_ROOT = Path(__file__).resolve().parents[1]
_custom_data_dir = os.getenv("LMF_DATA_DIR")
APP_HOME = Path(os.getenv("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "LMF-Control"
DATA_DIR = Path(_custom_data_dir) if _custom_data_dir else APP_HOME / "data"
JSON_DB_PATH = Path(os.getenv("LMF_JSON_DB_PATH", DATA_DIR / "lmf_control.json"))
DB_PATH = JSON_DB_PATH  # backwards-compatible name; now always points to JSON
LEGACY_SQLITE_PATH = Path(os.getenv("LMF_DB_PATH", DATA_DIR / "lmf_control.db"))
DATABASE_URL = "json://" + JSON_DB_PATH.as_posix()
CUSTOM_PATHS = bool(_custom_data_dir or os.getenv("LMF_JSON_DB_PATH"))
JSON_FORMAT = "lmf-control-json-database"
JSON_FORMAT_VERSION = 1
JSON_SCHEMA_REVISION = "json-1"
_json_lock = threading.RLock()
# FastAPI may enter and exit a sync generator dependency on different worker
# threads, so this lock must not have RLock's thread ownership requirement.
DATA_LOCK = threading.Lock()
_json_loaded = False

_configured_backup_dir = os.getenv("LMF_BACKUP_DIR")
_documents_backup_dir = Path(os.getenv("USERPROFILE", str(Path.home()))) / "Documents" / "LMF-Control" / "Backups"
_backup_path_notice = ""


def resolve_backup_directory(primary: Path, fallback: Path) -> tuple[Path, str]:
    try:
        primary.mkdir(parents=True, exist_ok=True)
        return primary, ""
    except OSError as error:
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback, f"A pasta Documentos não estava disponível ({error}). Backups usando {fallback}."


if _configured_backup_dir:
    BACKUP_DIR = Path(_configured_backup_dir)
else:
    BACKUP_DIR, _backup_path_notice = resolve_backup_directory(_documents_backup_dir, APP_HOME / "Backups")
IMPORT_BACKUP_DIR = Path(os.getenv("LMF_IMPORT_BACKUP_DIR", Path(os.getenv("USERPROFILE", str(Path.home()))) / "Documents" / "LMF-Control" / "Importar-Backups"))


class Base(DeclarativeBase):
    pass


engine = create_engine(
    "sqlite://",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@event.listens_for(engine, "connect")
def configure_memory_engine(connection, record):
    cursor = connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def _normalize_loaded_value(column, value):
    if value is None:
        return None
    python_type = column.type.python_type
    if python_type is date and isinstance(value, str):
        return date.fromisoformat(value)
    if python_type is datetime and isinstance(value, str):
        return datetime.fromisoformat(value)
    if python_type is bool:
        return bool(value)
    if isinstance(value, dict) and value.get("encoding") == "base64":
        return base64.b64decode(value["data"])
    return value


def _legacy_sqlite_export(path: Path) -> dict:
    """Read an existing SQLite database once, without making it the active store."""
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("O banco SQLite anterior não passou na verificação de integridade.")
        tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        result = {"format": JSON_FORMAT, "format_version": JSON_FORMAT_VERSION, "schema_revision": JSON_SCHEMA_REVISION, "exported_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"), "tables": {}}
        for name in tables:
            columns = [row[1] for row in connection.execute(f'PRAGMA table_info("{name}")')]
            rows = [dict(row) for row in connection.execute(f'SELECT * FROM "{name}"')]
            result["tables"][name] = {"columns": columns, "row_count": len(rows), "records": rows}
        return result
    finally:
        connection.close()


def _read_json(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("format") not in (JSON_FORMAT, "lmf-control-json-database-export", None):
        raise ValueError("O arquivo não é um banco JSON válido do LMF Control.")
    if not isinstance(data.get("tables"), dict):
        raise ValueError("O banco JSON não possui a estrutura de tabelas esperada.")
    return data


def _write_json_snapshot() -> None:
    with _json_lock, engine.connect() as connection:
        tables = {}
        for table in Base.metadata.sorted_tables:
            columns = [column.name for column in table.columns]
            rows = [dict(row._mapping) for row in connection.execute(table.select())]
            tables[table.name] = {"columns": columns, "row_count": len(rows), "records": rows}
        data = {"format": JSON_FORMAT, "format_version": JSON_FORMAT_VERSION, "schema_revision": JSON_SCHEMA_REVISION, "exported_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"), "tables": tables}
        JSON_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        temporary = JSON_DB_PATH.with_suffix(JSON_DB_PATH.suffix + ".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
        try:
            os.replace(temporary, JSON_DB_PATH)
        except PermissionError:
            # Some Windows profile ACLs allow modifying an existing file but deny replacing it.
            last_good = JSON_DB_PATH.with_suffix(JSON_DB_PATH.suffix + ".last-good")
            if JSON_DB_PATH.exists():
                shutil.copy2(JSON_DB_PATH, last_good)
            try:
                with temporary.open("rb") as source, JSON_DB_PATH.open("wb") as target:
                    shutil.copyfileobj(source, target)
                    target.flush()
                    os.fsync(target.fileno())
                json.loads(JSON_DB_PATH.read_text(encoding="utf-8"))
            except Exception:
                if last_good.exists():
                    with last_good.open("rb") as source, JSON_DB_PATH.open("wb") as target:
                        shutil.copyfileobj(source, target)
                raise
            finally:
                last_good.unlink(missing_ok=True)
        finally:
            temporary.unlink(missing_ok=True)


def initialize_json_database() -> dict:
    """Load JSON into the in-memory ORM and migrate a legacy SQLite file once if needed."""
    global _json_loaded
    from . import models  # register all ORM tables before metadata operations

    with _json_lock:
        if _json_loaded:
            return {"path": str(JSON_DB_PATH), "migrated": False}
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        JSON_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        IMPORT_BACKUP_DIR.mkdir(parents=True, exist_ok=True)

        migrated = False
        if JSON_DB_PATH.is_file():
            document = _read_json(JSON_DB_PATH)
        elif LEGACY_SQLITE_PATH.is_file():
            document = _legacy_sqlite_export(LEGACY_SQLITE_PATH)
            migrated = True
        else:
            document = {"format": JSON_FORMAT, "format_version": JSON_FORMAT_VERSION, "tables": {}}

        Base.metadata.create_all(engine)
        with engine.begin() as connection:
            for table in Base.metadata.sorted_tables:
                entry = document.get("tables", {}).get(table.name, {})
                records = entry.get("records", []) if isinstance(entry, dict) else []
                if not records:
                    continue
                rows = []
                for record in records:
                    rows.append({column.name: _normalize_loaded_value(column, record.get(column.name)) for column in table.columns if column.name in record})
                if rows:
                    connection.execute(table.insert(), rows)
        _json_loaded = True
        if migrated or not JSON_DB_PATH.exists():
            _write_json_snapshot()
        if migrated:
            saved = _read_json(JSON_DB_PATH)
            for table in Base.metadata.sorted_tables:
                old_rows = document.get("tables", {}).get(table.name, {}).get("records", [])
                new_rows = saved.get("tables", {}).get(table.name, {}).get("records", [])
                if len(old_rows) != len(new_rows):
                    raise RuntimeError(f"A migração JSON não preservou os registros da tabela {table.name}; o banco anterior foi mantido.")
            # The validated JSON snapshot is now authoritative; remove the old active database.
            LEGACY_SQLITE_PATH.unlink(missing_ok=True)
            for suffix in ("-wal", "-shm"):
                Path(str(LEGACY_SQLITE_PATH) + suffix).unlink(missing_ok=True)
        elif LEGACY_SQLITE_PATH.is_file():
            # A JSON database already exists, so preserve any older SQLite file as an archive,
            # outside the active data path, rather than treating it as a second live database.
            archive_dir = DATA_DIR / "migration-archive"
            archive_dir.mkdir(parents=True, exist_ok=True)
            archived = archive_dir / LEGACY_SQLITE_PATH.name
            if archived.exists():
                archived = archive_dir / f"{LEGACY_SQLITE_PATH.stem}_{datetime.now().strftime('%Y%m%d_%H%M%S')}{LEGACY_SQLITE_PATH.suffix}"
            shutil.move(str(LEGACY_SQLITE_PATH), str(archived))
            for suffix in ("-wal", "-shm"):
                sidecar = Path(str(LEGACY_SQLITE_PATH) + suffix)
                if sidecar.exists():
                    shutil.move(str(sidecar), str(archive_dir / (archived.name + suffix)))
        return {"path": str(JSON_DB_PATH), "migrated": migrated}


def reload_json_database() -> None:
    """Replace the in-memory ORM view with the just-restored JSON document."""
    global _json_loaded
    # Callers performing a restore hold DATA_LOCK around the whole operation.
    # Do not reacquire it here: it is a non-reentrant lock shared by requests.
    with _json_lock:
        _json_loaded = False
        engine.dispose()
        initialize_json_database()


@event.listens_for(Session, "after_commit")
def persist_json_after_commit(session):
    if session.info.get("skip_json_persist") or not _json_loaded:
        return
    _write_json_snapshot()


def get_db():
    DATA_LOCK.acquire()
    db = None
    try:
        db = SessionLocal()
        yield db
    finally:
        if db is not None:
            db.close()
        DATA_LOCK.release()
