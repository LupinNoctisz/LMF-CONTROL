from __future__ import annotations

from datetime import datetime
from pathlib import Path, PurePosixPath
import json
import os
import shutil
import sqlite3  # read-only compatibility for old SQLite backups
import tempfile
import zipfile

from ..database import (
    BACKUP_DIR, DATA_DIR, JSON_DB_PATH, IMPORT_BACKUP_DIR, DATA_LOCK, JSON_FORMAT,
    JSON_FORMAT_VERSION, JSON_SCHEMA_REVISION, _legacy_sqlite_export,
    _read_json, reload_json_database, acquire_data_lock, release_data_lock,
)

FORMAT_NAME = "lmf-control-json-backup"
FORMAT_VERSION = 1
SETTINGS_PATH = DATA_DIR / "backup_settings.json"
STATUS_PATH = DATA_DIR / "backup_status.json"
MAX_BACKUP_BYTES = 2 * 1024 * 1024 * 1024


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    try:
        os.replace(tmp, path)
    except PermissionError:
        last_good = path.with_suffix(path.suffix + ".last-good")
        if path.exists():
            shutil.copy2(path, last_good)
        try:
            with tmp.open("rb") as source, path.open("wb") as destination:
                shutil.copyfileobj(source, destination)
                destination.flush()
                os.fsync(destination.fileno())
            json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            if last_good.exists():
                with last_good.open("rb") as source, path.open("wb") as destination:
                    shutil.copyfileobj(source, destination)
            raise
        finally:
            last_good.unlink(missing_ok=True)
    finally:
        tmp.unlink(missing_ok=True)


def read_settings() -> dict:
    defaults = {"backup_time": "02:00", "retention_count": 30, "retention_days": 180}
    if not SETTINGS_PATH.exists():
        return defaults
    try:
        saved = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        return {**defaults, **saved}
    except (OSError, ValueError, TypeError):
        return defaults


def write_settings(settings: dict) -> dict:
    _atomic_json(SETTINGS_PATH, settings)
    return settings


def last_status() -> dict:
    if not STATUS_PATH.exists():
        return {"status": "ainda não executado", "completed_at": None, "message": ""}
    try:
        return json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"status": "desconhecido", "completed_at": None, "message": "Não foi possível ler o status anterior."}


def set_status(status: str, message: str = "", filename: str | None = None) -> dict:
    data = {"status": status, "completed_at": datetime.now().isoformat(timespec="seconds"), "message": message, "filename": filename}
    _atomic_json(STATUS_PATH, data)
    return data


def schema_revision() -> str | None:
    try:
        return _read_json(JSON_DB_PATH).get("schema_revision") or JSON_SCHEMA_REVISION
    except (OSError, ValueError, TypeError):
        return JSON_SCHEMA_REVISION


def _validate_json_document(document: dict, required_revision: str | None = None) -> dict:
    if document.get("format") not in (JSON_FORMAT, "lmf-control-json-database-export"):
        raise ValueError("Formato de banco JSON incompatível.")
    if not isinstance(document.get("tables"), dict):
        raise ValueError("O arquivo não contém as tabelas do LMF Control.")
    tables = document["tables"]
    if not {"products", "customers", "bank_accounts"}.issubset(tables):
        raise ValueError("O banco JSON não contém tabelas essenciais do sistema.")
    for table, data in tables.items():
        if not isinstance(data, dict) or not isinstance(data.get("records"), list):
            raise ValueError(f"A tabela {table} possui estrutura inválida.")
        if data.get("row_count", len(data["records"])) != len(data["records"]):
            raise ValueError(f"A contagem de registros da tabela {table} não confere.")
    revision = document.get("schema_revision") or JSON_SCHEMA_REVISION
    if required_revision and revision not in (required_revision, JSON_SCHEMA_REVISION, "20261028_0005", "20261028_0004", "20261028_0003", "20261028_0002", "20261028_0001", None):
        raise ValueError(f"Versão do banco incompatível: {revision}.")
    return {"integrity": "íntegro", "schema_revision": revision, "table_count": len(tables)}


def _safe_member(name: str) -> bool:
    normalized = name.replace("\\", "/")
    path = PurePosixPath(normalized)
    return not path.is_absolute() and ".." not in path.parts and not normalized.startswith("/")


def _read_and_validate_archive(path: Path, required_revision: str | None = None) -> tuple[dict, bytes]:
    if path.suffix.lower() != ".zip" or not path.is_file() or path.is_symlink():
        raise ValueError("Selecione um arquivo ZIP de backup válido.")
    if path.stat().st_size > MAX_BACKUP_BYTES:
        raise ValueError("Arquivo de backup excede o limite permitido.")
    with zipfile.ZipFile(path, "r") as archive:
        if archive.testzip() is not None:
            raise ValueError("O ZIP está corrompido.")
        members = archive.infolist()
        if any(not _safe_member(item.filename) for item in members):
            raise ValueError("O backup contém caminhos de arquivo inválidos.")
        names = [item.filename for item in members]
        if "lmf_control.json" in names and "manifest.json" in names:
            raw = archive.read("lmf_control.json")
            manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
            document = json.loads(raw.decode("utf-8"))
            if manifest.get("format") != FORMAT_NAME or manifest.get("format_version") != FORMAT_VERSION:
                raise ValueError("Formato de backup incompatível.")
            checked = _validate_json_document(document, required_revision)
            if manifest.get("schema_revision") != checked["schema_revision"]:
                raise ValueError("A versão do manifesto não corresponde ao banco JSON.")
            return manifest, raw

        # Existing SQLite backup archives remain importable; restored data is converted to JSON.
        legacy_name = "lmf_control.db" if "lmf_control.db" in names else "lmf.db" if "lmf.db" in names else None
        if not legacy_name:
            raise ValueError("O backup não contém banco JSON ou um backup SQLite legado reconhecido.")
        raw_db = archive.read(legacy_name)
    with tempfile.NamedTemporaryFile(prefix="lmf-legacy-", suffix=".db", dir=DATA_DIR, delete=False) as stream:
        temp_db = Path(stream.name)
        stream.write(raw_db)
    try:
        document = _legacy_sqlite_export(temp_db)
        checked = _validate_json_document(document, required_revision)
    finally:
        temp_db.unlink(missing_ok=True)
    manifest = {"format": "legacy-lmf-sqlite-backup", "format_version": 0, "created_at": datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds"), "schema_revision": checked["schema_revision"], "legacy": True}
    return manifest, json.dumps(document, ensure_ascii=False, indent=2, default=str).encode("utf-8")


def inspect_archive(path: Path, required_revision: str | None = None) -> dict:
    try:
        manifest, _ = _read_and_validate_archive(path, required_revision)
        return {"name": path.name, "size": path.stat().st_size, "created_at": manifest.get("created_at"), "integrity": "íntegro", "schema_revision": manifest.get("schema_revision"), "status": "convertido ao restaurar" if manifest.get("legacy") else "válido", "error": None}
    except Exception as error:
        return {"name": path.name, "size": path.stat().st_size if path.exists() else 0, "created_at": None, "integrity": "inválido", "schema_revision": None, "status": "não restaurável", "error": str(error)}


def _prune_backups(settings: dict) -> list[str]:
    candidates = sorted(BACKUP_DIR.glob("*.zip"), key=lambda path: path.stat().st_mtime, reverse=True)
    valid = [path for path in candidates if inspect_archive(path).get("integrity") == "íntegro"]
    if len(valid) <= 1:
        return []
    count = max(1, int(settings.get("retention_count", 30)))
    days = max(1, int(settings.get("retention_days", 180)))
    cutoff = datetime.now().timestamp() - days * 86400
    deleted = []
    for index, path in enumerate(valid):
        if index == 0:
            continue
        if index >= count or path.stat().st_mtime < cutoff:
            if len(valid) - len(deleted) <= 1:
                break
            path.unlink()
            deleted.append(path.name)
    return deleted


def create_backup(kind: str = "manual") -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not JSON_DB_PATH.is_file():
        from ..database import initialize_json_database
        initialize_json_database()
    raw = JSON_DB_PATH.read_bytes()
    document = json.loads(raw.decode("utf-8"))
    check = _validate_json_document(document, schema_revision())
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    final = BACKUP_DIR / f"lmf_control_{stamp}_{kind}.zip"
    temporary = BACKUP_DIR / f".{final.name}.tmp.zip"
    manifest = {"format": FORMAT_NAME, "format_version": FORMAT_VERSION, "created_at": datetime.now().isoformat(timespec="seconds"), "schema_revision": check["schema_revision"], "database": "lmf_control.json"}
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("lmf_control.json", raw)
            archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
            docs = DATA_DIR / "documentos"
            if docs.exists():
                for file in docs.rglob("*"):
                    if file.is_file() and not file.is_symlink():
                        archive.write(file, (PurePosixPath("documents") / file.relative_to(docs).as_posix()).as_posix())
            if SETTINGS_PATH.is_file():
                archive.write(SETTINGS_PATH, "backup_settings.json")
        _read_and_validate_archive(temporary, schema_revision())
        os.replace(temporary, final)
        _prune_backups(read_settings())
        set_status("concluído", "Backup JSON criado e validado.", final.name)
        return final
    except Exception as error:
        temporary.unlink(missing_ok=True)
        set_status("falhou", str(error))
        raise


def list_backups(directory: Path = BACKUP_DIR) -> list[dict]:
    if not directory.exists():
        return []
    return [inspect_archive(path, schema_revision()) for path in sorted(directory.glob("*.zip"), key=lambda item: item.stat().st_mtime, reverse=True) if path.is_file()]


def importable_backups() -> list[dict]:
    return list_backups(IMPORT_BACKUP_DIR)


def copy_imported_backup(filename: str) -> Path:
    if Path(filename).name != filename:
        raise ValueError("Nome de arquivo inválido.")
    source = IMPORT_BACKUP_DIR / filename
    _read_and_validate_archive(source, schema_revision())
    destination = BACKUP_DIR / filename
    if destination.exists():
        raise FileExistsError("Já existe um backup com esse nome; renomeie o arquivo e tente novamente.")
    shutil.copy2(source, destination)
    _read_and_validate_archive(destination, schema_revision())
    return destination


def _restore_documents(archive: zipfile.ZipFile) -> None:
    root = (DATA_DIR / "documentos").resolve()
    for info in archive.infolist():
        if not info.filename.startswith("documents/") or info.is_dir():
            continue
        relative = PurePosixPath(info.filename).relative_to("documents")
        if not _safe_member(relative.as_posix()):
            raise ValueError("Caminho de documento inválido no backup.")
        dest = (root / Path(*relative.parts)).resolve()
        if root not in dest.parents:
            raise ValueError("Documento ultrapassa a pasta permitida.")
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.exists():
            dest.write_bytes(archive.read(info))


def _restore_backup_locked(path: Path, required_revision: str | None = None) -> dict:
    manifest, json_bytes = _read_and_validate_archive(path, required_revision)
    safety = create_backup(kind="pre-restauração")
    candidate = JSON_DB_PATH.with_suffix(".restore.json")
    rollback = JSON_DB_PATH.with_suffix(".previous.json")
    try:
        candidate.write_bytes(json_bytes)
        _validate_json_document(json.loads(candidate.read_text(encoding="utf-8")), required_revision)
        if JSON_DB_PATH.exists():
            shutil.copy2(JSON_DB_PATH, rollback)
        os.replace(candidate, JSON_DB_PATH)
        try:
            reload_json_database()
            with zipfile.ZipFile(path) as archive:
                _restore_documents(archive)
        except Exception:
            if rollback.exists():
                os.replace(rollback, JSON_DB_PATH)
                reload_json_database()
            raise
        rollback.unlink(missing_ok=True)
        set_status("concluído", f"Backup de {manifest.get('created_at')} restaurado. Cópia de segurança criada: {safety.name}.", path.name)
        return {"restored": path.name, "created_at": manifest.get("created_at"), "safety_backup": safety.name}
    except Exception:
        candidate.unlink(missing_ok=True)
        if rollback.exists() and not JSON_DB_PATH.exists():
            os.replace(rollback, JSON_DB_PATH)
            reload_json_database()
        raise
    finally:
        candidate.unlink(missing_ok=True)
        rollback.unlink(missing_ok=True)


def restore_backup(path: Path, required_revision: str | None = None) -> dict:
    # A restore must wait for in-flight requests, but never forever: if the lock
    # cannot be acquired in time, DatabaseBusyError is raised and nothing changes.
    acquired_at = acquire_data_lock("restore_backup")
    try:
        return _restore_backup_locked(path, required_revision)
    finally:
        release_data_lock("restore_backup", acquired_at)


def delete_backup(filename: str) -> None:
    if Path(filename).name != filename:
        raise ValueError("Nome de arquivo inválido.")
    target = (BACKUP_DIR / filename).resolve()
    if target.parent != BACKUP_DIR.resolve() or target.suffix.lower() != ".zip" or not target.is_file():
        raise FileNotFoundError("Backup não encontrado.")
    rows = list_backups(BACKUP_DIR)
    row = next((item for item in rows if item["name"] == target.name), None)
    if not row:
        raise FileNotFoundError("Backup não encontrado.")
    if row["integrity"] == "íntegro" and sum(item["integrity"] == "íntegro" for item in rows) <= 1:
        raise ValueError("Este é o último backup íntegro. Crie e valide outro antes de excluí-lo.")
    target.unlink()
