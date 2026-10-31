"""Armazenamento local organizado de arquivos (anexos, comprovantes, fotos).

Raiz: DATA_DIR/documentos  (já incluída em todos os backups)

    documentos/
      ordens-servico/<id da OS>/            fotos da OS
      garantias/<id da garantia>/           contrato assinado, páginas e assinaturas digitalizadas
      fornecedores/                         (reservado)
      pedidos/                              (reservado)
      financeiro/comprovantes/<id lanç.>/   comprovantes (PDF/JPG/PNG)
      financeiro/comprovantes/_removidos/   comprovantes removidos pelo usuário (nunca apagados)
      geral/                                outros documentos
    (Backups ficam em LMF_BACKUP_DIR.)

O banco guarda só o caminho RELATIVO a `documentos` (nunca uma URL pública). Os arquivos são servidos
por rotas autenticadas. Nomes: <tipo>_<AAAAMMDD-HHMMSS>_<sha8>.<ext>, criados com modo exclusivo:
nunca sobrescrevem um arquivo existente.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from pathlib import Path, PurePosixPath

from .database import DATA_DIR

ROOT = DATA_DIR / "documentos"
LAYOUT = ("ordens-servico", "garantias", "fornecedores", "pedidos", "financeiro/comprovantes", "financeiro/comprovantes/_removidos", "geral")
MAX_FILE_BYTES = 12 * 1024 * 1024


def ensure_layout() -> None:
    for part in LAYOUT:
        (ROOT / Path(*PurePosixPath(part).parts)).mkdir(parents=True, exist_ok=True)


def clean_name(name: str | None, default: str = "arquivo") -> str:
    base = Path((name or default).replace("\\", "/")).name
    base = "".join(ch for ch in base if ch.isprintable()).strip()
    return (base or default)[:255]


def sniff(data: bytes, allow_pdf: bool = False) -> tuple[str, str] | None:
    """(extensão, content-type) pelo CONTEÚDO real; JPG/PNG sempre, PDF só quando permitido."""
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg", "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png", "image/png"
    if allow_pdf and data.startswith(b"%PDF-"):
        return ".pdf", "application/pdf"
    return None


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def resolve(relative: str) -> Path:
    """Caminho absoluto seguro (impede sair de `documentos`)."""
    pure = PurePosixPath(relative or "")
    if pure.is_absolute() or ".." in pure.parts or not pure.parts:
        raise ValueError("Caminho de arquivo inválido.")
    path = (ROOT / Path(*pure.parts)).resolve()
    if ROOT.resolve() not in path.parents:
        raise ValueError("Caminho de arquivo inválido.")
    return path


def save_bytes(folder: str, prefix: str, data: bytes, ext: str) -> str:
    """Grava SEM sobrescrever e devolve o caminho relativo (posix)."""
    prefix = re.sub(r"[^a-z0-9_-]", "", prefix.lower()) or "arq"
    target_dir = resolve(folder)
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    digest = sha256(data)[:8]
    for attempt in range(1, 1000):
        suffix = "" if attempt == 1 else f"-{attempt}"
        name = f"{prefix}_{stamp}_{digest}{suffix}{ext}"
        try:
            with (target_dir / name).open("xb") as handle:
                handle.write(data)
            return f"{folder}/{name}"
        except FileExistsError:
            continue
    raise OSError("Não foi possível criar um nome de arquivo único.")


def remove_quietly(relative: str) -> None:
    try:
        resolve(relative).unlink(missing_ok=True)
    except (OSError, ValueError):
        pass
