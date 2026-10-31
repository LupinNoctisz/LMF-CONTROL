"""Regras de garantia.

* A garantia dura 90 dias CONTADOS A PARTIR DA DATA DE INÍCIO DA GARANTIA (`starts_on`), e não da
  data de cadastro/emissão. O vencimento (`ends_on`) é calculado pelo servidor e GRAVADO na criação.
  Valores de vencimento enviados pelo navegador são ignorados.
* O vencimento permanece válido até o fim do dia `ends_on` (inclusive); no dia seguinte a garantia expira.
* A expiração é automática: uma tarefa em segundo plano (e a abertura do sistema) muda o status de
  'ativa' para 'expirada' sem ninguém abrir a tela. A rotina altera SOMENTE o campo `status`:
  nunca apaga, move nem reescreve documentos, fotos ou os demais dados da garantia.
* Registros antigos mantêm o vencimento que já tinham gravado (nada é recalculado em lote).
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

from fastapi import HTTPException
from sqlalchemy.orm import Session

from .models import Customer, Guarantee, Product, Sale, SaleItem, ServiceOrder

GUARANTEE_DAYS = 90
STATUS_ACTIVE = "ativa"
STATUS_EXPIRED = "expirada"

logger = logging.getLogger("lmf.guarantees")


def warranty_end(starts_on: date) -> date:
    return starts_on + timedelta(days=GUARANTEE_DAYS)


def resolve_origin(db: Session, values: dict) -> dict:
    """Preenche os dados da garantia a partir da OS ou da venda (leitura apenas: NUNCA altera OS, venda ou estoque)."""
    origin = values.get("origin") or "manual"
    out = dict(values)
    out["origin"] = origin
    if origin == "ordem_servico":
        order = db.get(ServiceOrder, values.get("service_order_id")) if values.get("service_order_id") else None
        if not order:
            raise HTTPException(404, "Ordem de serviço não encontrada. Informe o identificador de uma OS existente.")
        out.update(
            service_order_id=order.id, sale_id=None, customer_id=order.customer_id,
            equipment_type=order.equipment_type, brand=order.brand, model=order.model, serial=order.serial,
            service_performed=order.service_executed,
        )
        out["item"] = (values.get("item") or "").strip() or f"{order.equipment_type or ''} {order.equipment}".strip()
        out["starts_on"] = values.get("starts_on") or order.delivered_on or order.completed_on or date.today()
    elif origin == "venda":
        sale = db.get(Sale, values.get("sale_id")) if values.get("sale_id") else None
        if not sale:
            raise HTTPException(404, "Venda não encontrada. Selecione uma venda existente.")
        if sale.status != "confirmada":
            raise HTTPException(409, "Só é possível criar garantia de uma venda confirmada.")
        lines = db.query(SaleItem).filter_by(sale_id=sale.id).order_by(SaleItem.id).all()
        wanted = values.get("sale_item_id")
        line = next((l for l in lines if l.id == wanted), None) if wanted else (lines[0] if len(lines) == 1 else None)
        if not line:
            raise HTTPException(400, "Esta venda tem mais de um item: selecione o produto que terá garantia.")
        product = db.get(Product, line.product_id)
        customer_id = values.get("customer_id") or sale.customer_id
        if not customer_id:
            raise HTTPException(400, "A venda não tem cliente: informe o cliente da garantia.")
        out.update(
            service_order_id=None, sale_id=sale.id, customer_id=customer_id,
            brand=product.brand if product else None, model=product.model if product else None,
            quantity=line.quantity, purchased_on=sale.sold_at.date() if sale.sold_at else None,
        )
        out["item"] = (values.get("item") or "").strip() or (product.name if product else f"Item da venda {sale.number}")
        out["starts_on"] = values.get("starts_on") or out["purchased_on"] or date.today()
    else:  # manual: não inventa relacionamentos; exige os dados
        out.update(service_order_id=None, sale_id=None)
        if not values.get("customer_id") or not (values.get("item") or "").strip() or not values.get("starts_on"):
            raise HTTPException(400, "Garantia manual exige cliente, item e data de início.")
    if not db.get(Customer, out["customer_id"]):
        raise HTTPException(400, "Cliente não encontrado")
    return out


def prepare_guarantee(values: dict, current: Guarantee | None, db=None) -> None:
    """Define origem, dados vinculados, vencimento e duração (chamado em criar/atualizar)."""
    if current is not None:  # a origem e os vínculos não mudam depois de criada
        values.update(origin=current.origin or "manual", service_order_id=current.service_order_id, sale_id=current.sale_id)
    resolved = resolve_origin(db, values)
    sale_item = resolved.pop("sale_item_id", None)  # só serve para escolher a linha da venda
    values.clear(); values.update(resolved)
    starts_on = values["starts_on"]
    duration = values.get("duration_days") or (current.duration_days if current is not None and current.duration_days else GUARANTEE_DAYS)
    if current is not None and current.starts_on == starts_on and current.duration_days == duration and current.ends_on:
        values["ends_on"], values["duration_days"] = current.ends_on, current.duration_days  # preserva o vencimento gravado
    else:
        values["ends_on"], values["duration_days"] = starts_on + timedelta(days=duration), duration
    if current is not None and current.status == STATUS_EXPIRED and values.get("status") == STATUS_ACTIVE and values["ends_on"] < date.today():
        values["status"] = STATUS_EXPIRED
    if values.get("status") == STATUS_ACTIVE and values["ends_on"] < date.today():
        values["status"] = STATUS_EXPIRED


def expire_due_guarantees(db: Session, today: date | None = None) -> int:
    """Marca como 'expirada' toda garantia ativa vencida. Idempotente; mexe só em `status`."""
    today = today or date.today()
    rows = db.query(Guarantee).filter(Guarantee.status == STATUS_ACTIVE, Guarantee.ends_on < today).all()
    for row in rows:
        row.status = STATUS_EXPIRED
    if rows:
        db.commit()
        logger.info("Garantias expiradas automaticamente: %s", [row.id for row in rows])
    return len(rows)
