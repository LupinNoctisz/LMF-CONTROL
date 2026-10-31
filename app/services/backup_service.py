from __future__ import annotations

from datetime import datetime
from pathlib import Path, PurePosixPath
import hashlib
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
FORMAT_VERSION = 2  # v2: manifesto com somas SHA-256, lista de documentos, pastas e vínculos registro↔arquivo
SUPPORTED_FORMAT_VERSIONS = (1, 2)  # v1 (sem somas de verificação) continua restaurável
DOCUMENTS_PREFIX = "documents/"
# Tabelas cujos registros apontam para arquivos em DATA_DIR/documentos: (tabela, função -> caminho relativo).
FILE_REFERENCES = (
    ("service_order_photos", lambda r: f"ordens-servico/{int(r['service_order_id'])}/{r['stored_name']}"),
    ("guarantee_attachments", lambda r: r["relative_path"]),
    ("finance_receipts", lambda r: r["relative_path"]),
)
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


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_zip_member(archive: zipfile.ZipFile, name: str) -> str:
    digest = hashlib.sha256()
    with archive.open(name) as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def document_references(document: dict) -> list[dict]:
    """Vínculos registro -> arquivo, lidos do próprio banco JSON (fonte da verdade)."""
    found = []
    for table, to_path in FILE_REFERENCES:
        entry = document.get("tables", {}).get(table) or {}
        for record in entry.get("records", []):
            try:
                relative = to_path(record)
            except (KeyError, TypeError, ValueError):
                relative = None
            if not relative or not _safe_member(relative):
                found.append({"table": table, "record_id": record.get("id"), "document_path": None, "original_name": record.get("original_name")})
                continue
            found.append({"table": table, "record_id": record.get("id"), "document_path": relative, "original_name": record.get("original_name")})
    return found


def _verify_archive_contents(archive: zipfile.ZipFile, names: list[str], manifest: dict, document: dict, raw: bytes) -> None:
    """Confere banco, documentos, pastas e vínculos contra o manifesto (somas SHA-256)."""
    if manifest.get("database_sha256") != _sha256_bytes(raw) or manifest.get("database_size") != len(raw):
        raise ValueError("O banco JSON do backup não confere com a soma de verificação do manifesto.")
    expected = {item["path"]: item for item in manifest.get("documents", [])}
    in_zip = {name[len(DOCUMENTS_PREFIX):] for name in names if name.startswith(DOCUMENTS_PREFIX) and not name.endswith("/")}
    if set(expected) != in_zip:
        missing, extra = sorted(set(expected) - in_zip), sorted(in_zip - set(expected))
        raise ValueError(f"Os documentos do ZIP não correspondem ao manifesto (faltando: {missing[:3]}, sobrando: {extra[:3]}).")
    for relative, item in expected.items():
        if not _safe_member(relative):
            raise ValueError("Caminho de documento inválido no manifesto.")
        info = archive.getinfo(DOCUMENTS_PREFIX + relative)
        if info.file_size != item.get("size") or _sha256_zip_member(archive, DOCUMENTS_PREFIX + relative) != item.get("sha256"):
            raise ValueError(f"O documento {relative} está alterado ou corrompido (soma de verificação não confere).")
    settings = manifest.get("settings")
    if settings and (settings.get("path") not in names or _sha256_zip_member(archive, settings["path"]) != settings.get("sha256")):
        raise ValueError("As configurações do backup não conferem com o manifesto.")
    references = document_references(document)
    missing = [ref for ref in references if not ref["document_path"] or ref["document_path"] not in expected]
    if manifest.get("complete") is True and missing:
        raise ValueError(f"Backup declarado completo, mas {len(missing)} arquivo(s) referenciado(s) pelos registros não estão no ZIP.")
    if manifest.get("complete") is False and not manifest.get("missing_references"):
        raise ValueError("Manifesto inconsistente: backup incompleto sem lista de arquivos ausentes.")
    if manifest.get("complete") not in (True, False):
        raise ValueError("Manifesto sem a indicação de integridade dos documentos.")
    recorded = {(ref["table"], ref["record_id"]) for ref in manifest.get("references", [])}
    if recorded != {(ref["table"], ref["record_id"]) for ref in references}:
        raise ValueError("Os vínculos registro-documento do manifesto não correspondem ao banco do backup.")
    counts = manifest.get("table_counts", {})
    for table, data in document["tables"].items():
        if counts.get(table) != len(data["records"]):
            raise ValueError(f"A contagem de registros da tabela {table} não confere com o manifesto.")


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
            if manifest.get("format") != FORMAT_NAME or manifest.get("format_version") not in SUPPORTED_FORMAT_VERSIONS:
                raise ValueError("Formato de backup incompatível.")
            checked = _validate_json_document(document, required_revision)
            if manifest.get("schema_revision") != checked["schema_revision"]:
                raise ValueError("A versão do manifesto não corresponde ao banco JSON.")
            if manifest["format_version"] >= 2:
                _verify_archive_contents(archive, names, manifest, document, raw)
            else:
                manifest = {**manifest, "verified": False, "complete": None}
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
        if manifest.get("legacy"):
            status = "convertido ao restaurar"
        elif manifest.get("complete") is False:
            status = "incompleto"
        elif manifest.get("verified") is False:
            status = "válido (formato antigo, documentos sem verificação)"
        else:
            status = "válido"
        return {"name": path.name, "size": path.stat().st_size, "created_at": manifest.get("created_at"), "integrity": "íntegro", "schema_revision": manifest.get("schema_revision"), "status": status, "complete": manifest.get("complete"), "documents": len(manifest.get("documents", [])), "missing_references": len(manifest.get("missing_references", [])), "error": None}
    except Exception as error:
        return {"name": path.name, "size": path.stat().st_size if path.exists() else 0, "created_at": None, "integrity": "inválido", "schema_revision": None, "status": "não restaurável", "complete": None, "documents": 0, "missing_references": 0, "error": str(error)}


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


def _collect_documents(docs: Path) -> tuple[list[tuple[Path, dict, bytes]], list[str]]:
    """Lê cada documento UMA vez (os bytes gravados são os mesmos que geraram o SHA-256)."""
    files, directories = [], []
    if not docs.exists():
        return files, directories
    for entry in sorted(docs.rglob("*")):
        if entry.is_symlink():
            continue
        relative = entry.relative_to(docs).as_posix()
        if entry.is_dir():
            directories.append(relative)
        elif entry.is_file():
            data = entry.read_bytes()
            stat = entry.stat()
            files.append((entry, {"path": relative, "size": len(data), "sha256": _sha256_bytes(data), "modified_at": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds")}, data))
    return files, directories


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
    try:
        files, directories = _collect_documents(DATA_DIR / "documentos")
        present = {meta["path"] for _, meta, _ in files}
        references = document_references(document)
        by_path: dict[str, list[dict]] = {}
        for ref in references:
            if ref["document_path"]:
                by_path.setdefault(ref["document_path"], []).append({"table": ref["table"], "record_id": ref["record_id"], "original_name": ref["original_name"]})
        for _, meta, _ in files:
            meta["referenced_by"] = by_path.get(meta["path"], [])
        missing = [ref for ref in references if not ref["document_path"] or ref["document_path"] not in present]
        settings_bytes = SETTINGS_PATH.read_bytes() if SETTINGS_PATH.is_file() else None
        manifest = {
            "format": FORMAT_NAME, "format_version": FORMAT_VERSION, "created_at": datetime.now().isoformat(timespec="seconds"),
            "schema_revision": check["schema_revision"], "database": "lmf_control.json",
            "database_sha256": _sha256_bytes(raw), "database_size": len(raw),
            "table_counts": {table: len(data["records"]) for table, data in document["tables"].items()},
            "documents": [meta for _, meta, _ in files], "directories": directories,
            "references": [{"table": r["table"], "record_id": r["record_id"], "document_path": r["document_path"], "present": bool(r["document_path"] and r["document_path"] in present)} for r in references],
            "missing_references": [{"table": r["table"], "record_id": r["record_id"], "document_path": r["document_path"]} for r in missing],
            "unreferenced_documents": sorted(path for path in present if path not in by_path),
            "complete": not missing,
            "settings": {"path": "backup_settings.json", "sha256": _sha256_bytes(settings_bytes)} if settings_bytes is not None else None,
        }
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("lmf_control.json", raw)
            archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
            for relative in directories:
                archive.writestr(zipfile.ZipInfo(DOCUMENTS_PREFIX + relative + "/"), b"")
            for _, meta, data in files:
                archive.writestr(DOCUMENTS_PREFIX + meta["path"], data)
            if settings_bytes is not None:
                archive.writestr("backup_settings.json", settings_bytes)
        _read_and_validate_archive(temporary, schema_revision())  # relê o ZIP pronto e confere todas as somas
        os.replace(temporary, final)
        _prune_backups(read_settings())
        if missing:
            set_status("incompleto", f"Backup criado, mas {len(missing)} arquivo(s) referenciado(s) por registros não existem na pasta de documentos e NÃO estão no backup.", final.name)
        else:
            set_status("concluído", f"Backup criado e validado: banco, {len(files)} documento(s) e vínculos conferidos.", final.name)
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


def _stage_documents(archive: zipfile.ZipFile, manifest: dict, staging: Path) -> list[dict]:
    """Extrai documentos para uma pasta temporária e confere a soma de cada um ANTES de tocar nos dados reais."""
    items = manifest.get("documents")
    if items is None:  # backup antigo (v1): sem somas; usa o que está no ZIP
        items = [{"path": info.filename[len(DOCUMENTS_PREFIX):]} for info in archive.infolist() if info.filename.startswith(DOCUMENTS_PREFIX) and not info.is_dir()]
    root = staging.resolve()
    staged = []
    for item in items:
        relative = item["path"]
        if not _safe_member(relative):
            raise ValueError("Caminho de documento inválido no backup.")
        target = (root / Path(*PurePosixPath(relative).parts)).resolve()
        if root not in target.parents:
            raise ValueError("Documento ultrapassa a pasta permitida.")
        target.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        with archive.open(DOCUMENTS_PREFIX + relative) as source, target.open("wb") as out:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk); out.write(chunk)
        if item.get("sha256") and digest.hexdigest() != item["sha256"]:
            raise ValueError(f"O documento {relative} não confere com a soma de verificação.")
        staged.append({"path": relative, "staged": target, "sha256": digest.hexdigest()})
    return staged


def _apply_documents(staged: list[dict], directories: list[str], journal: list, stamp: str) -> dict:
    """Move os documentos conferidos para a pasta real. Nunca apaga: arquivo diferente já existente é
    renomeado (`.antes-restauracao-...`) e tudo fica registrado em `journal` para desfazer em caso de falha."""
    root = DATA_DIR / "documentos"
    result = {"created": 0, "unchanged": 0, "replaced": 0}
    for relative in directories:
        folder = root / Path(*PurePosixPath(relative).parts)
        if not folder.exists():
            folder.mkdir(parents=True, exist_ok=True); journal.append(("dir", folder))
    for item in staged:
        dest = root / Path(*PurePosixPath(item["path"]).parts)
        if dest.parent != root and not dest.parent.exists():
            missing_dirs = [p for p in reversed(dest.parents) if p != root and root in p.parents and not p.exists()]
            dest.parent.mkdir(parents=True, exist_ok=True)
            journal.extend(("dir", p) for p in missing_dirs)
        if dest.exists():
            if _sha256_file(dest) == item["sha256"]:
                result["unchanged"] += 1
                continue
            saved = dest.with_name(f"{dest.name}.antes-restauracao-{stamp}")
            os.replace(dest, saved); journal.append(("renamed", dest, saved)); result["replaced"] += 1
        os.replace(item["staged"], dest); journal.append(("created", dest)); result["created"] += 1
    return result


def _rollback_documents(journal: list) -> None:
    for entry in reversed(journal):
        try:
            if entry[0] == "created":
                entry[1].unlink(missing_ok=True)
            elif entry[0] == "renamed":
                os.replace(entry[2], entry[1])
            elif entry[0] == "dir":
                entry[1].rmdir()
        except OSError:
            pass


def _check_references_present(document: dict) -> list[dict]:
    root = DATA_DIR / "documentos"
    return [ref for ref in document_references(document) if not ref["document_path"] or not (root / Path(*PurePosixPath(ref["document_path"]).parts)).is_file()]


def _restore_backup_locked(path: Path, required_revision: str | None = None, allow_incomplete: bool = False) -> dict:
    manifest, json_bytes = _read_and_validate_archive(path, required_revision)
    if manifest.get("complete") is False and not allow_incomplete:
        raise ValueError(f"Este backup está INCOMPLETO: {len(manifest.get('missing_references', []))} arquivo(s) referenciado(s) pelos registros não foram salvos nele. Restaurá-lo deixaria registros sem arquivo. Use outro backup ou confirme a restauração de backup incompleto.")
    safety = create_backup(kind="pre-restauração")
    candidate = JSON_DB_PATH.with_suffix(".restore.json")
    rollback = JSON_DB_PATH.with_suffix(".previous.json")
    staging = DATA_DIR / f".restore-staging-{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}"
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    journal: list = []
    try:
        document = json.loads(json_bytes.decode("utf-8"))
        candidate.write_bytes(json_bytes)
        _validate_json_document(json.loads(candidate.read_text(encoding="utf-8")), required_revision)
        with zipfile.ZipFile(path) as archive:
            staged = _stage_documents(archive, manifest, staging)  # 1) extrai e confere somas, sem alterar nada real
        directories = manifest.get("directories", [])
        if JSON_DB_PATH.exists():
            shutil.copy2(JSON_DB_PATH, rollback)
        try:
            summary = _apply_documents(staged, directories, journal, stamp)  # 2) arquivos
            os.replace(candidate, JSON_DB_PATH)  # 3) banco
            reload_json_database()
            missing = _check_references_present(document)  # 4) vínculos: todo registro precisa ter o seu arquivo
            if missing and not allow_incomplete:
                raise ValueError(f"Restauração desfeita: {len(missing)} registro(s) ficariam sem o arquivo vinculado.")
        except Exception:
            if rollback.exists():
                os.replace(rollback, JSON_DB_PATH)
                reload_json_database()
            _rollback_documents(journal)
            raise
        rollback.unlink(missing_ok=True)
        note = f" {len(missing)} registro(s) continuam sem arquivo (backup incompleto)." if missing else ""
        set_status("concluído", f"Backup de {manifest.get('created_at')} restaurado: {summary['created']} documento(s) gravado(s), {summary['unchanged']} já idêntico(s), {summary['replaced']} substituído(s) (originais preservados com sufixo .antes-restauracao). Cópia de segurança: {safety.name}.{note}", path.name)
        return {"restored": path.name, "created_at": manifest.get("created_at"), "safety_backup": safety.name, "documents": summary, "missing_references": len(missing)}
    except Exception:
        candidate.unlink(missing_ok=True)
        if rollback.exists() and not JSON_DB_PATH.exists():
            os.replace(rollback, JSON_DB_PATH)
            reload_json_database()
        raise
    finally:
        candidate.unlink(missing_ok=True)
        rollback.unlink(missing_ok=True)
        shutil.rmtree(staging, ignore_errors=True)


def restore_backup(path: Path, required_revision: str | None = None, allow_incomplete: bool = False) -> dict:
    # A restore must wait for in-flight requests, but never forever: if the lock
    # cannot be acquired in time, DatabaseBusyError is raised and nothing changes.
    acquired_at = acquire_data_lock("restore_backup")
    try:
        return _restore_backup_locked(path, required_revision, allow_incomplete)
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
