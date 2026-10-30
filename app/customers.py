"""Clientes: identificador público sequencial e reaproveitamento por CPF.

O identificador público (`public_seq`, exibido com no mínimo 2 dígitos: 01, 02 ... 100) vem de um
contador persistente (tabela `counters`), NÃO de MAX(id): assim um número de cliente excluído
jamais é reutilizado. O contador é lido e incrementado dentro da mesma transação do cadastro
(com bloqueio de linha em bancos que o suportam), e todo acesso à base passa pelo DATA_LOCK.
"""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from .models import Counter, Customer
from .validation import normalize_cpf

COUNTER_NAME = "customer_public_seq"


def public_code(seq: int | None) -> str | None:
    return None if seq is None else f"{seq:02d}"


def _counter(db: Session) -> Counter:
    row = db.query(Counter).filter(Counter.name == COUNTER_NAME).with_for_update().first()
    if row is None:
        highest = db.query(func.max(Customer.public_seq)).scalar() or 0
        row = Counter(name=COUNTER_NAME, value=highest)
        db.add(row)
        db.flush()
    return row


def next_public_seq(db: Session) -> int:
    row = _counter(db)
    highest = db.query(func.max(Customer.public_seq)).scalar() or 0
    row.value = max(row.value, highest) + 1
    db.flush()
    return row.value


def assign_missing_public_seq(db: Session) -> int:
    """Dá número aos clientes antigos (ordem de cadastro). Preserva os já numerados."""
    pending = db.query(Customer).filter(Customer.public_seq.is_(None)).order_by(Customer.id).all()
    for customer in pending:
        customer.public_seq = next_public_seq(db)
    if pending:
        db.commit()
    return len(pending)


def find_by_document(db: Session, document: str | None, exclude_id: int | None = None) -> Customer | None:
    if not document:
        return None
    query = db.query(Customer).filter(Customer.document == document)
    if exclude_id is not None:
        query = query.filter(Customer.id != exclude_id)
    return query.order_by(Customer.id).first()


def ensure_unique_document(db: Session, document: str | None, exclude_id: int | None = None) -> None:
    other = find_by_document(db, document, exclude_id)
    if other:
        raise HTTPException(409, f"Já existe um cliente com este CPF: nº {public_code(other.public_seq) or other.id} - {other.name}.")


def prepare_customer(values: dict, current: Customer | None, db: Session) -> None:
    ensure_unique_document(db, values.get("document"), current.id if current else None)
    if current is None:
        values["public_seq"] = next_public_seq(db)


def get_or_create_for_order(db: Session, data: dict) -> Customer:
    """Cliente informado dentro da OS: reaproveita pelo CPF ou cadastra (sem duplicar)."""
    document = normalize_cpf(data.get("document")) if data.get("document") else None
    if not document:
        raise HTTPException(400, "Informe o CPF do cliente para localizar ou cadastrar.")
    existing = find_by_document(db, document)
    if existing:
        return existing
    if not (data.get("name") or "").strip():
        raise HTTPException(400, "Cliente não encontrado. Informe o nome para cadastrá-lo.")
    customer = Customer(name=data["name"].strip(), document=document, phone=data.get("phone"), whatsapp=data.get("whatsapp"), email=data.get("email"), public_seq=next_public_seq(db))
    db.add(customer)
    db.flush()
    return customer
