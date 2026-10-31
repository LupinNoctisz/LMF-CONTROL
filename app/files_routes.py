"""Anexos de garantia (contrato digitalizado, páginas, assinaturas) e comprovantes financeiros."""

from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from . import storage
from .database import get_db
from .models import FinanceReceipt, FinancialEntry, Guarantee, GuaranteeAttachment

router = APIRouter()
log = logging.getLogger("lmf.files")
MAX_ATTACH_PER_REQUEST = 20


def _read_uploads(files: list[UploadFile], allow_pdf: bool) -> list[tuple[str, bytes, str, str]]:
    if not files:
        raise HTTPException(400, "Nenhum arquivo enviado.")
    if len(files) > MAX_ATTACH_PER_REQUEST:
        raise HTTPException(400, f"Envie no máximo {MAX_ATTACH_PER_REQUEST} arquivos por vez.")
    prepared = []
    for upload in files:  # valida TUDO antes de gravar qualquer coisa
        name = storage.clean_name(upload.filename)
        data = upload.file.read(storage.MAX_FILE_BYTES + 1)
        if not data:
            raise HTTPException(400, f'O arquivo "{name}" está vazio. Nada foi anexado.')
        if len(data) > storage.MAX_FILE_BYTES:
            raise HTTPException(400, f'O arquivo "{name}" passa de {storage.MAX_FILE_BYTES // 1024 // 1024} MB. Nada foi anexado.')
        kind = storage.sniff(data, allow_pdf)
        if not kind:
            allowed = "PDF, JPG, JPEG ou PNG" if allow_pdf else "JPG, JPEG ou PNG"
            raise HTTPException(400, f'"{name}" não é um arquivo {allowed} válido. Nada foi anexado.')
        prepared.append((name, data, kind[0], kind[1]))
    return prepared


def _out(prefix: str, owner_id: int, row, segment: str) -> dict:
    return {"id": row.id, "original_name": row.original_name, "content_type": row.content_type, "size": row.size, "sha256": row.sha256,
            "kind": getattr(row, "kind", None), "url": f"{prefix}/{owner_id}/{segment}/{row.id}/file", "created_at": row.created_at.isoformat() if row.created_at else None}


def _serve(row, disposition: str):
    try:
        path = storage.resolve(row.relative_path)
    except ValueError:
        raise HTTPException(404, "Arquivo não encontrado.")
    if not path.is_file():
        raise HTTPException(404, "O arquivo deste anexo não está na pasta local (pode ter sido movido ou não veio no backup).")
    return FileResponse(path, media_type=row.content_type, filename=row.original_name, content_disposition_type=disposition)


# ---- garantias ---------------------------------------------------------------------------------
@router.get("/api/guarantees/{guarantee_id}/attachments")
def list_guarantee_attachments(guarantee_id: int, db: Session = Depends(get_db)):
    if not db.get(Guarantee, guarantee_id):
        raise HTTPException(404, "Garantia não encontrada")
    rows = db.query(GuaranteeAttachment).filter_by(guarantee_id=guarantee_id).order_by(GuaranteeAttachment.id).all()
    return {"items": [_out("/api/guarantees", guarantee_id, r, "attachments") for r in rows]}


@router.post("/api/guarantees/{guarantee_id}/attachments")
def upload_guarantee_attachments(guarantee_id: int, files: list[UploadFile] = File(...), kind: Literal["contrato", "assinatura", "documento"] = Form("documento"), db: Session = Depends(get_db)):
    """Anexa várias imagens (JPG/JPEG/PNG) à garantia. Funciona também para garantias já expiradas."""
    if not db.get(Guarantee, guarantee_id):
        raise HTTPException(404, "Garantia não encontrada")
    prepared = _read_uploads(files, allow_pdf=False)
    written, rows = [], []
    try:
        for name, data, ext, mime in prepared:
            relative = storage.save_bytes(f"garantias/{guarantee_id}", kind, data, ext)
            written.append(relative)
            row = GuaranteeAttachment(guarantee_id=guarantee_id, kind=kind, original_name=name, relative_path=relative, content_type=mime, size=len(data), sha256=storage.sha256(data))
            db.add(row); rows.append(row)
        db.commit()
    except Exception:
        db.rollback()
        for relative in written:
            storage.remove_quietly(relative)
        log.exception("Falha ao gravar anexos da garantia %s", guarantee_id)
        raise HTTPException(500, "Não foi possível salvar os anexos. Nada foi anexado.")
    return {"items": [_out("/api/guarantees", guarantee_id, r, "attachments") for r in rows]}


@router.get("/api/guarantees/{guarantee_id}/attachments/{attachment_id}/file")
def get_guarantee_attachment(guarantee_id: int, attachment_id: int, download: bool = False, db: Session = Depends(get_db)):
    row = db.get(GuaranteeAttachment, attachment_id)
    if not row or row.guarantee_id != guarantee_id:
        raise HTTPException(404, "Anexo não encontrado")
    return _serve(row, "attachment" if download else "inline")


# ---- comprovantes financeiros ----------------------------------------------------------------------
@router.get("/api/finance/{entry_id}/receipts")
def list_receipts(entry_id: int, db: Session = Depends(get_db)):
    if not db.get(FinancialEntry, entry_id):
        raise HTTPException(404, "Lançamento não encontrado")
    rows = db.query(FinanceReceipt).filter_by(financial_entry_id=entry_id).order_by(FinanceReceipt.id).all()
    return {"items": [_out("/api/finance", entry_id, r, "receipts") for r in rows]}


@router.post("/api/finance/{entry_id}/receipts")
def upload_receipts(entry_id: int, files: list[UploadFile] = File(...), db: Session = Depends(get_db)):
    if not db.get(FinancialEntry, entry_id):
        raise HTTPException(404, "Lançamento não encontrado")
    prepared = _read_uploads(files, allow_pdf=True)
    written, rows = [], []
    try:
        for name, data, ext, mime in prepared:
            relative = storage.save_bytes(f"financeiro/comprovantes/{entry_id}", "comprovante", data, ext)
            written.append(relative)
            row = FinanceReceipt(financial_entry_id=entry_id, original_name=name, relative_path=relative, content_type=mime, size=len(data), sha256=storage.sha256(data))
            db.add(row); rows.append(row)
        db.commit()
    except Exception:
        db.rollback()
        for relative in written:
            storage.remove_quietly(relative)
        log.exception("Falha ao gravar comprovantes do lançamento %s", entry_id)
        raise HTTPException(500, "Não foi possível salvar os comprovantes. Nada foi anexado.")
    return {"items": [_out("/api/finance", entry_id, r, "receipts") for r in rows]}


@router.get("/api/finance/{entry_id}/receipts/{receipt_id}/file")
def get_receipt(entry_id: int, receipt_id: int, download: bool = False, db: Session = Depends(get_db)):
    row = db.get(FinanceReceipt, receipt_id)
    if not row or row.financial_entry_id != entry_id:
        raise HTTPException(404, "Comprovante não encontrado")
    return _serve(row, "attachment" if download else "inline")


@router.delete("/api/finance/{entry_id}/receipts/{receipt_id}")
def remove_receipt(entry_id: int, receipt_id: int, db: Session = Depends(get_db)):
    """Remove o VÍNCULO do comprovante; o lançamento é preservado e o arquivo é guardado em _removidos (não apagado)."""
    row = db.get(FinanceReceipt, receipt_id)
    if not row or row.financial_entry_id != entry_id:
        raise HTTPException(404, "Comprovante não encontrado")
    source = row.relative_path
    moved = None
    try:
        origin = storage.resolve(source)
        if origin.is_file():
            data = origin.read_bytes()
            ext = origin.suffix
            moved = storage.save_bytes("financeiro/comprovantes/_removidos", f"lanc{entry_id}", data, ext)
            origin.unlink()
    except (OSError, ValueError):
        log.exception("Falha ao arquivar comprovante %s", receipt_id)
        raise HTTPException(500, "Não foi possível arquivar o comprovante. Nada foi removido.")
    db.delete(row)
    db.commit()
    return {"removed": True, "archived_as": moved}
