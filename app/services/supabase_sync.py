"""Optional, manual transport of the local JSON database to/from Supabase.

Importing this module performs no I/O. There is deliberately no startup hook and
no automatic synchronization. The local JSON file remains authoritative unless
an operator explicitly runs the pull command.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import tempfile
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request, urlopen

from ..database import JSON_DB_PATH, JSON_FORMAT, JSON_FORMAT_VERSION, JSON_SCHEMA_REVISION

TABLE_NAME = "lmf_json_state"
STATE_ID = "lmf-control"
REQUEST_TIMEOUT_SECONDS = 20


class SupabaseSyncError(RuntimeError):
    pass


def is_enabled() -> bool:
    return os.getenv("LMF_SUPABASE_SYNC_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}


def configuration_status() -> dict:
    url = os.getenv("SUPABASE_URL", "").strip()
    key = os.getenv("SUPABASE_SECRET_KEY", "").strip()
    return {
        "enabled": is_enabled(),
        "url_configured": bool(url),
        "secret_key_configured": bool(key),
        "ready": is_enabled() and bool(url) and bool(key),
    }


def _configuration() -> tuple[str, str]:
    if not is_enabled():
        raise SupabaseSyncError("Integração Supabase desativada. Defina LMF_SUPABASE_SYNC_ENABLED=true para habilitá-la.")
    url = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
    key = os.getenv("SUPABASE_SECRET_KEY", "").strip()
    parsed = urlparse(url)
    if not parsed.hostname or (parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1"}):
        raise SupabaseSyncError("SUPABASE_URL deve usar HTTPS (HTTP só é aceito para localhost).")
    if not key:
        raise SupabaseSyncError("SUPABASE_SECRET_KEY não foi configurada.")
    return url, key


def _validate_document(document: object) -> dict:
    if not isinstance(document, dict):
        raise SupabaseSyncError("O conteúdo remoto não é um objeto JSON.")
    if document.get("format") not in {JSON_FORMAT, "lmf-control-json-database-export", None}:
        raise SupabaseSyncError("Formato remoto incompatível com o banco JSON do LMF Control.")
    tables = document.get("tables")
    if not isinstance(tables, dict):
        raise SupabaseSyncError("O banco JSON remoto não possui tabelas válidas.")
    for name, table in tables.items():
        if not isinstance(name, str) or not isinstance(table, dict) or not isinstance(table.get("records", []), list):
            raise SupabaseSyncError("O banco JSON remoto contém uma tabela inválida.")
        records = table.get("records", [])
        row_count = table.get("row_count", len(records))
        if row_count != len(records):
            raise SupabaseSyncError(f"A contagem de registros da tabela {name} não confere.")
    return document


def _request(method: str, query: dict, body: dict | None = None) -> bytes:
    base_url, key = _configuration()
    target = f"{base_url}/rest/v1/{quote(TABLE_NAME)}?{urlencode(query)}"
    headers = {"apikey": key, "Accept": "application/json"}
    # Legacy service_role keys are JWTs and PostgREST expects them as the bearer token.
    # Current sb_secret_* API keys are opaque and must be sent as apikey only.
    if key.count(".") == 2:
        headers["Authorization"] = f"Bearer {key}"
    if body is not None:
        headers["Content-Type"] = "application/json"
        headers["Prefer"] = "resolution=merge-duplicates,return=minimal"
    request = Request(target, data=json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None, headers=headers, method=method)
    try:
        with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            return response.read()
    except HTTPError as error:
        detail = error.read(2048).decode("utf-8", errors="replace")
        raise SupabaseSyncError(f"Supabase respondeu HTTP {error.code}: {detail}") from error
    except (URLError, TimeoutError, OSError) as error:
        raise SupabaseSyncError(f"Não foi possível conectar ao Supabase: {error}") from error


def push_local_json() -> dict:
    """Upload the current local JSON document as one versioned JSONB row."""
    _configuration()  # Fail before reading or mutating anything if disabled.
    document = _validate_document(json.loads(JSON_DB_PATH.read_text(encoding="utf-8")))
    row = {
        "id": STATE_ID,
        "payload": document,
        "schema_revision": document.get("schema_revision", JSON_SCHEMA_REVISION),
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    _request("POST", {"on_conflict": "id"}, row)
    return {"direction": "local-to-supabase", "id": STATE_ID, "tables": len(document["tables"]), "records": sum(len(t.get("records", [])) for t in document["tables"].values())}


def pull_to_local_json(create_backup) -> dict:
    """Replace local JSON only after an explicit CLI action and validated fetch."""
    _configuration()
    query = {"id": f"eq.{STATE_ID}", "select": "payload,schema_revision,updated_at"}
    raw = _request("GET", query)
    try:
        rows = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SupabaseSyncError("A resposta do Supabase não contém JSON válido.") from error
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise SupabaseSyncError("Não foi encontrado exatamente um snapshot remoto lmf-control.")
    document = _validate_document(rows[0].get("payload"))
    # Validate serialization before the safety backup or any local file mutation.
    content = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    backup_path = create_backup("pre-supabase-pull")
    JSON_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="\n", dir=JSON_DB_PATH.parent, suffix=".supabase.tmp", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        json.loads(temporary.read_text(encoding="utf-8"))
        try:
            os.replace(temporary, JSON_DB_PATH)
        except PermissionError:
            # Windows profiles may allow in-place writes but deny replacement.
            with temporary.open("rb") as source, JSON_DB_PATH.open("wb") as destination:
                shutil.copyfileobj(source, destination)
                destination.flush()
                os.fsync(destination.fileno())
            _validate_document(json.loads(JSON_DB_PATH.read_text(encoding="utf-8")))
    except Exception as error:
        raise SupabaseSyncError(f"Não foi possível gravar o snapshot local; backup de segurança: {backup_path}. Erro: {error}") from error
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)
    return {"direction": "supabase-to-local", "backup": str(backup_path), "updated_at": rows[0].get("updated_at"), "tables": len(document["tables"])}
