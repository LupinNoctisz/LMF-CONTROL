"""Autorização administrativa para operações destrutivas (principalmente financeiras).

Fluxo (toda a validação acontece aqui, no backend):
  1. O administrador logado envia a senha para POST /api/auth/authorize-operation, informando a
     operação e o alvo (ex.: conta 3). A senha é conferida contra o hash PBKDF2 já existente e nunca
     é armazenada, registrada nem devolvida.
  2. Se correta, o servidor devolve um token aleatório de USO ÚNICO, válido por poucos minutos e
     amarrado a (usuário, operação, alvo). Só o hash SHA-256 do token fica guardado.
  3. A operação destrutiva exige esse token no cabeçalho X-Operation-Authorization. O token é
     consumido (e gravado como usado) ANTES de a operação rodar: repetir a mesma requisição, usar o
     token em outro alvo/operação, ou usá-lo depois de expirar, é recusado.
  4. Tentativas de senha erradas são limitadas por usuário (persistidas, valem após reinício).
  5. Cada etapa grava uma linha em audit_log (e em DATA_DIR/audit.log, que não é afetado por
     restaurações de backup). Nada disso contém senha, token ou hash.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import threading
from datetime import datetime, timedelta

from fastapi import HTTPException, Request
from sqlalchemy import func
from sqlalchemy.orm import Session

from . import security
from .database import DATA_DIR, SessionLocal, acquire_data_lock, release_data_lock
from .models import AppUser, AuditLog, OperationAuthorization

logger = logging.getLogger("lmf.audit")

AUTH_HEADER = "X-Operation-Authorization"
TOKEN_TTL_SECONDS = 120
MAX_PASSWORD_FAILURES = 5
LOCKOUT_SECONDS = 15 * 60
PASSWORD_ACTION = "autorizacao.senha"

# Operações que exigem autorização. Chave = nome técnico; valor = descrição mostrada ao administrador.
OPERATIONS = {
    "financeiro.lancamento.excluir": "Excluir lançamento financeiro",
    "financeiro.conta.excluir": "Excluir conta financeira",
    "financeiro.conta.saldo_inicial.alterar": "Alterar saldo inicial da conta",
    "financeiro.conta.saldo_inicial.remover": "Remover saldo inicial da conta",
    "financeiro.venda.cancelar": "Cancelar venda e estornar valores",
    "financeiro.venda.excluir": "Excluir venda e seus lançamentos",
    "backup.restaurar": "Restaurar backup (substitui todos os dados)",
}


def _now() -> datetime:  # isolado para os testes poderem avançar o relógio
    return datetime.now()


# Tokens já consumidos, também em memória: restaurar um backup devolve o banco a um estado em que o token
# ainda constava como "não usado"; esta lista (que sobrevive à troca do banco) impede a repetição mesmo assim.
_CONSUMED: dict[str, datetime] = {}
_CONSUMED_KEEP = timedelta(seconds=TOKEN_TTL_SECONDS + 300)


def _remember_consumed(digest: str, now: datetime) -> None:
    for old in [h for h, at in _CONSUMED.items() if now - at > _CONSUMED_KEEP]:
        _CONSUMED.pop(old, None)
    _CONSUMED[digest] = now


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "local"


def audit_file_path():
    return DATA_DIR / "audit.log"


def _append_audit_file(row: AuditLog) -> None:
    """Cópia em arquivo (uma linha JSON por evento). Restaurar um backup não apaga esta trilha."""
    line = json.dumps({
        "at": row.created_at.isoformat(timespec="seconds"), "user_id": row.user_id, "username": row.username,
        "action": row.action, "target": row.target, "outcome": row.outcome, "ip": row.ip, "details": row.details,
    }, ensure_ascii=False)
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with audit_file_path().open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    except OSError:
        logger.exception("Não foi possível gravar o arquivo de auditoria")


def record_audit(db: Session, *, user_id, username, action: str, target, outcome: str, ip, details: str | None = None) -> AuditLog:
    """Acrescenta um evento (sem commit: quem chama decide). Nunca receba senha/token aqui."""
    row = AuditLog(user_id=user_id, username=username or "sistema", action=action, target=None if target is None else str(target)[:120],
                   outcome=outcome, ip=ip, details=(details or "")[:500] or None, created_at=_now())
    db.add(row)
    _append_audit_file(row)
    return row


def _failure_state(db: Session, user_id: int, now: datetime) -> tuple[int, datetime | None]:
    """Falhas de senha na janela, contadas a partir do último sucesso."""
    floor = now - timedelta(seconds=LOCKOUT_SECONDS)
    last_ok = db.query(func.max(AuditLog.created_at)).filter(
        AuditLog.user_id == user_id, AuditLog.action == PASSWORD_ACTION, AuditLog.outcome == "autorizada").scalar()
    if last_ok and last_ok > floor:
        floor = last_ok
    query = db.query(AuditLog).filter(AuditLog.user_id == user_id, AuditLog.action == PASSWORD_ACTION,
                                      AuditLog.outcome == "negado", AuditLog.created_at > floor)
    count = query.count()
    oldest = query.order_by(AuditLog.created_at).first()
    return count, oldest.created_at if oldest else None


def issue_authorization(db: Session, request: Request, operation: str, target: str, password: str) -> dict:
    user_id = getattr(request.state, "user_id", None)
    username = getattr(request.state, "username", None)
    ip = client_ip(request)
    label = f"{operation}:{target}"
    now = _now()

    def refuse(status: int, message: str, outcome: str, reason: str, headers: dict | None = None):
        record_audit(db, user_id=user_id, username=username, action=PASSWORD_ACTION, target=label, outcome=outcome, ip=ip, details=reason)
        db.commit()
        raise HTTPException(status, message, headers=headers)

    user = db.get(AppUser, user_id) if user_id is not None else None
    if not user or not user.active:
        refuse(401, "Sessão inválida. Entre novamente.", "negado", "usuário inexistente ou inativo")
    if not user.is_admin:
        refuse(403, "Apenas o administrador pode autorizar esta operação.", "negado", "usuário não é administrador")
    if operation not in OPERATIONS:
        refuse(400, "Operação desconhecida.", "negado", f"operação não reconhecida: {operation[:60]}")

    failures, oldest = _failure_state(db, user.id, now)
    if failures >= MAX_PASSWORD_FAILURES:
        wait = max(1, int((oldest + timedelta(seconds=LOCKOUT_SECONDS) - now).total_seconds()))
        refuse(429, f"Muitas tentativas de senha. Aguarde {max(1, wait // 60)} minuto(s) e tente novamente.", "bloqueado",
               f"bloqueado após {failures} senhas incorretas", headers={"Retry-After": str(wait)})

    if not security.verify_password(password, user.password_hash):
        left = MAX_PASSWORD_FAILURES - failures - 1
        refuse(403, f"Senha do administrador incorreta. {left} tentativa(s) restante(s) antes do bloqueio." if left > 0
               else "Senha do administrador incorreta. Acesso bloqueado por alguns minutos.", "negado", "senha incorreta")

    # Limpa autorizações antigas (usadas ou vencidas há mais de um dia).
    db.query(OperationAuthorization).filter(OperationAuthorization.created_at < now - timedelta(days=1)).delete(synchronize_session=False)
    token = secrets.token_urlsafe(32)
    db.add(OperationAuthorization(user_id=user.id, operation=operation, target=str(target), token_hash=_hash_token(token),
                                  expires_at=now + timedelta(seconds=TOKEN_TTL_SECONDS), ip=ip, created_at=now))
    record_audit(db, user_id=user.id, username=user.username, action=PASSWORD_ACTION, target=label, outcome="autorizada", ip=ip,
                 details=f"autorização emitida por {TOKEN_TTL_SECONDS}s, uso único")
    db.commit()
    return {"token": token, "expires_in": TOKEN_TTL_SECONDS, "operation": operation, "target": str(target), "description": OPERATIONS[operation]}


def require_authorization(db: Session, request: Request, operation: str, target) -> None:
    """Chame no INÍCIO de toda operação destrutiva. Levanta HTTPException se não houver autorização válida."""
    if operation not in OPERATIONS:
        raise RuntimeError(f"Operação protegida sem cadastro em OPERATIONS: {operation}")
    user_id = getattr(request.state, "user_id", None)
    username = getattr(request.state, "username", None)
    ip = client_ip(request)
    target = str(target)

    def deny(reason: str, message: str = "Autorização do administrador ausente, inválida, expirada ou já utilizada.", status: int = 403):
        record_audit(db, user_id=user_id, username=username, action=operation, target=target, outcome="negado", ip=ip, details=reason)
        db.commit()
        raise HTTPException(status, message)

    if user_id is None:
        deny("sem sessão", "Autenticação necessária.", 401)
    if not getattr(request.state, "is_admin", False):
        deny("usuário não é administrador", "Apenas o administrador pode executar esta operação.")
    token = (request.headers.get(AUTH_HEADER) or "").strip()
    if not token:
        deny("sem autorização informada", "Esta operação exige a autorização do administrador (senha).")
    digest = _hash_token(token)
    now = _now()
    if digest in _CONSUMED:
        deny(f"token já utilizado em {_CONSUMED[digest].isoformat(timespec='seconds')} (tentativa de repetição)")
    row = db.query(OperationAuthorization).filter_by(token_hash=digest).first()
    if row is None:
        deny("token desconhecido")
    if row.used_at is not None:
        deny(f"token já utilizado em {row.used_at.isoformat(timespec='seconds')} (tentativa de repetição)")
    if row.expires_at < now:
        deny("token expirado")
    if row.user_id != user_id:
        deny("token emitido para outro usuário")
    if row.operation != operation or row.target != target:
        deny(f"token emitido para {row.operation}:{row.target}")
    row.used_at = now  # consumido e gravado ANTES de a operação rodar
    _remember_consumed(digest, now)
    record_audit(db, user_id=user_id, username=username, action=operation, target=target, outcome="autorizada", ip=ip,
                 details=f"autorização #{row.id} consumida")
    db.commit()
    request.state.audit_operation = (operation, target)


def require_authorization_standalone(request: Request, operation: str, target) -> None:
    """Para rotas que não usam get_db (ex.: restaurar backup, que toma o bloqueio do banco por conta própria)."""
    acquired = acquire_data_lock("authorization")
    try:
        with SessionLocal() as db:
            require_authorization(db, request, operation, target)
    finally:
        release_data_lock("authorization", acquired)


def record_outcome(operation: str, target: str, user_id, username, ip, status_code: int) -> None:
    """Resultado final da operação autorizada (executada ou falhou). Roda em thread; nunca levanta."""
    try:
        acquired = acquire_data_lock("audit-outcome", timeout=15)
    except Exception:
        logger.error("Não foi possível registrar o resultado de %s %s: banco ocupado", operation, target)
        return
    try:
        with SessionLocal() as db:
            record_audit(db, user_id=user_id, username=username, action=operation, target=target,
                         outcome="executada" if status_code < 400 else "falhou", ip=ip, details=f"HTTP {status_code}")
            db.commit()
    except Exception:
        logger.exception("Falha ao registrar o resultado de %s %s", operation, target)
    finally:
        release_data_lock("audit-outcome", acquired)


_OUTCOME_THREADS: list[threading.Thread] = []


def start_outcome_thread(*args) -> None:
    """Registra o resultado FINAL em segundo plano (depois que a resposta saiu e o bloqueio do banco foi liberado)."""
    thread = threading.Thread(target=record_outcome, args=args, name="lmf-audit-outcome", daemon=True)
    _OUTCOME_THREADS[:] = [t for t in _OUTCOME_THREADS if t.is_alive()]
    _OUTCOME_THREADS.append(thread)
    thread.start()


def drain_pending(timeout: float = 10.0) -> None:
    """Espera os registros em segundo plano terminarem (encerramento do app e testes)."""
    deadline = datetime.now() + timedelta(seconds=timeout)
    for thread in list(_OUTCOME_THREADS):
        thread.join(max(0.0, (deadline - datetime.now()).total_seconds()))


def list_audit(db: Session, limit: int = 100, outcome: str | None = None) -> list[dict]:
    query = db.query(AuditLog)
    if outcome:
        query = query.filter(AuditLog.outcome == outcome)
    rows = query.order_by(AuditLog.id.desc()).limit(max(1, min(limit, 500))).all()
    return [{"id": r.id, "at": r.created_at.isoformat(timespec="seconds"), "user": r.username, "action": r.action, "target": r.target,
             "outcome": r.outcome, "ip": r.ip, "details": r.details} for r in rows]
