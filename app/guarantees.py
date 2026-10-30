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

from sqlalchemy.orm import Session

from .models import Guarantee

GUARANTEE_DAYS = 90
STATUS_ACTIVE = "ativa"
STATUS_EXPIRED = "expirada"

logger = logging.getLogger("lmf.guarantees")


def warranty_end(starts_on: date) -> date:
    return starts_on + timedelta(days=GUARANTEE_DAYS)


def prepare_guarantee(values: dict, current: Guarantee | None) -> None:
    """Define vencimento e duração a partir da data de início (chamado em criar/atualizar)."""
    starts_on = values["starts_on"]
    if current is not None and current.starts_on == starts_on and current.ends_on and current.duration_days:
        # Início inalterado: preserva o vencimento já gravado (inclusive de registros antigos).
        values["ends_on"], values["duration_days"] = current.ends_on, current.duration_days
    else:
        values["ends_on"], values["duration_days"] = warranty_end(starts_on), GUARANTEE_DAYS
    if current is not None and current.status == STATUS_EXPIRED and values.get("status") == STATUS_ACTIVE:
        # Reativar exige um novo prazo válido; senão volta a expirar na próxima verificação.
        values["status"] = STATUS_ACTIVE if values["ends_on"] >= date.today() else STATUS_EXPIRED
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
