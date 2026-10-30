"""12.C - Proteção das operações financeiras destrutivas (autorização administrativa)."""
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault('LMF_DATA_DIR', os.path.abspath('work/json-auth-test-data-6char'))
os.environ.setdefault('LMF_BACKUP_DIR', os.path.abspath('work/json-auth-test-backups-6char'))
os.environ.setdefault('LMF_IMPORT_BACKUP_DIR', os.path.abspath('work/json-auth-import-backups-6char'))

from app.main import app  # noqa: E402
from app import authorization, security  # noqa: E402
from app.database import DATA_LOCK, SessionLocal  # noqa: E402
from app.models import AppUser, OperationAuthorization  # noqa: E402

USER = {'username': 'testadmin', 'password': 'Abc123'}
HEADER = authorization.AUTH_HEADER


def login(client, user=USER):
    status = client.get('/api/auth/status').json()
    route = '/api/auth/setup' if status['setup_required'] else '/api/auth/login'
    response = client.post(route, json=user)
    assert response.status_code == 200, response.text
    client.headers.update({'X-CSRF-Token': response.json()['csrf_token']})
    return client


@pytest.fixture
def client():
    with TestClient(app) as c:
        login(c)
        yield c
        authorization.drain_pending(10)
        assert not DATA_LOCK.locked()


class Clock:
    def __init__(self):
        self.offset = timedelta(0)

    def __call__(self):
        return datetime.now() + self.offset


@pytest.fixture
def clock(monkeypatch):
    fake = Clock()
    monkeypatch.setattr(authorization, '_now', fake)
    return fake


def _make_user(username, password, is_admin):
    with SessionLocal() as db:
        db.add(AppUser(username=username, password_hash=security.password_hash(password), is_admin=is_admin, active=True))
        db.commit()


def ask(client, operation, target, password=USER['password']):
    return client.post('/api/auth/authorize-operation', json={'operation': operation, 'target': str(target), 'password': password})


def authorize(client, operation, target):
    response = ask(client, operation, target)
    assert response.status_code == 200, response.text
    return {HEADER: response.json()['token']}


def audit(client, **match):
    rows = client.get('/api/audit?limit=500').json()
    return [row for row in rows if all(row.get(k) == v for k, v in match.items())]


def wait_for_audit(client, timeout=5, **match):
    """O resultado final é gravado depois da resposta, em segundo plano."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        found = audit(client, **match)
        if found:
            return found
        time.sleep(0.05)
    return []


# ---- uma "fábrica" por operação protegida -------------------------------------------------------

class Case:
    def __init__(self, operation, target, send, intact):
        self.operation, self.target, self.send, self.intact = operation, str(target), send, intact


def _adjustment_count():
    from app.models import AccountBalanceAdjustment
    with SessionLocal() as db:
        return db.query(AccountBalanceAdjustment).count()


def finance_entry(c):
    entry = c.post('/api/finance', json={'kind': 'receita', 'category': 'Teste', 'description': 'Lançamento ' + uuid4().hex[:6], 'amount': 10}).json()
    return Case('financeiro.lancamento.excluir', entry['id'],
                lambda h=None: c.delete(f"/api/finance/{entry['id']}?confirm=true", headers=h or {}),
                lambda: any(x['id'] == entry['id'] for x in c.get('/api/finance').json()))


def account_delete(c):
    account = c.post('/api/accounts', json={'name': 'Conta ' + uuid4().hex[:6]}).json()
    return Case('financeiro.conta.excluir', account['id'],
                lambda h=None: c.delete(f"/api/accounts/{account['id']}?confirm=true", headers=h or {}),
                lambda: any(x['id'] == account['id'] for x in c.get('/api/accounts').json()))


def _account_with_balance(c):
    return c.post('/api/accounts', json={'name': 'Conta ' + uuid4().hex[:6], 'opening_balance': 50, 'opening_balance_date': '2026-01-15'}).json()


def _balance(c, account):
    return next(x for x in c.get('/api/accounts').json() if x['id'] == account['id'])['opening_balance']


def opening_balance_remove(c):
    account = _account_with_balance(c)
    return Case('financeiro.conta.saldo_inicial.remover', account['id'],
                lambda h=None: c.post(f"/api/accounts/{account['id']}/remove-opening-balance", headers=h or {}, json={'account_name_confirmation': account['name']}),
                lambda: _balance(c, account) == 50)


def opening_balance_alter(c):
    account = _account_with_balance(c)
    return Case('financeiro.conta.saldo_inicial.alterar', account['id'],
                lambda h=None: c.put(f"/api/accounts/{account['id']}", headers=h or {}, json={'name': account['name'], 'opening_balance': 999}),
                lambda: _balance(c, account) == 50)


def _sale(c):
    product = c.post('/api/products', json={'name': 'Produto ' + uuid4().hex[:6], 'sku': uuid4().hex[:10], 'quantity': 10, 'cost': 5, 'price': 20}).json()
    sale = c.post('/api/sales', json={'items': [{'product_id': product['id'], 'quantity': 1, 'unit_price': 20}], 'received_amount': 20})
    assert sale.status_code == 200, sale.text
    return sale.json()['id']


def sale_cancel(c):
    sale_id = _sale(c)
    return Case('financeiro.venda.cancelar', sale_id,
                lambda h=None: c.post(f'/api/sales/{sale_id}/cancel', headers=h or {}),
                lambda: next(s for s in c.get('/api/sales').json() if s['id'] == sale_id)['status'] != 'cancelada')


def sale_delete(c):
    sale_id = _sale(c)
    return Case('financeiro.venda.excluir', sale_id,
                lambda h=None: c.delete(f'/api/sales/{sale_id}?confirm=true', headers=h or {}),
                lambda: any(s['id'] == sale_id for s in c.get('/api/sales').json()))


def backup_restore(c):
    name = c.post('/api/backups').json()['file']
    marker = c.post('/api/suppliers', json={'name': 'Marcador ' + uuid4().hex[:6]}).json()  # criado DEPOIS do backup
    return Case('backup.restaurar', name,
                lambda h=None: c.post(f'/api/backups/{name}/restore?confirm=true', headers=h or {}),
                lambda: any(x['id'] == marker['id'] for x in c.get('/api/suppliers').json()))


CASES = [finance_entry, account_delete, opening_balance_remove, opening_balance_alter, sale_cancel, sale_delete, backup_restore]
IDS = [f.__name__ for f in CASES]


@pytest.mark.parametrize('factory', CASES, ids=IDS)
def test_destructive_operation_without_authorization_is_refused_and_audited(client, factory):
    case = factory(client)
    response = case.send()
    assert response.status_code == 403, response.text
    assert case.intact(), 'a operação foi executada sem autorização!'
    denied = audit(client, action=case.operation, target=case.target, outcome='negado')
    assert denied and 'sem autorização' in denied[0]['details']


@pytest.mark.parametrize('factory', CASES, ids=IDS)
def test_authorized_operation_runs_once_and_both_steps_are_audited(client, factory):
    case = factory(client)
    headers = authorize(client, case.operation, case.target)
    response = case.send(headers)
    assert response.status_code == 200, response.text
    assert not case.intact(), 'a operação autorizada não surtiu efeito'
    if case.operation == 'backup.restaurar':  # o banco voltou no tempo: a linha "autorizada" só resta no arquivo de auditoria
        assert f'"action": "{case.operation}", "target": "{case.target}", "outcome": "autorizada"' in authorization.audit_file_path().read_text(encoding='utf-8')
    else:
        assert audit(client, action=case.operation, target=case.target, outcome='autorizada')
    done = wait_for_audit(client, action=case.operation, target=case.target, outcome='executada')
    assert done and done[0]['user'] == USER['username'] and done[0]['details'] == 'HTTP 200'


@pytest.mark.parametrize('factory', CASES, ids=IDS)
def test_replaying_the_same_authorized_request_is_refused(client, factory):
    case = factory(client)
    headers = authorize(client, case.operation, case.target)
    assert case.send(headers).status_code == 200
    adjustments = _adjustment_count()
    replay = case.send(headers)
    if factory in (opening_balance_remove, opening_balance_alter):
        # Depois do 1º uso a repetição é um "no-op" (o saldo já está no valor final) e nem chega a executar:
        # o que importa é que NÃO há segundo efeito nem novo ajuste registrado.
        assert replay.status_code in (200, 403) and _adjustment_count() == adjustments
        return
    assert replay.status_code == 403
    refused = audit(client, action=case.operation, target=case.target, outcome='negado')
    assert any('repetição' in (row['details'] or '') for row in refused)


def test_a_failed_operation_still_consumes_the_authorization_and_is_audited(client):
    account = client.post('/api/accounts', json={'name': 'Conta com vínculo ' + uuid4().hex[:6]}).json()
    client.post('/api/finance', json={'kind': 'receita', 'category': 'x', 'description': 'vínculo', 'amount': 5, 'bank_account_id': account['id']})
    headers = authorize(client, 'financeiro.conta.excluir', account['id'])
    first = client.delete(f"/api/accounts/{account['id']}?confirm=true", headers=headers)
    assert first.status_code == 409
    assert wait_for_audit(client, action='financeiro.conta.excluir', target=str(account['id']), outcome='falhou')
    assert client.delete(f"/api/accounts/{account['id']}?confirm=true", headers=headers).status_code == 403


def test_token_is_bound_to_one_operation_and_one_target(client):
    first, second = account_delete(client), account_delete(client)
    headers = authorize(client, first.operation, first.target)
    assert second.send(headers).status_code == 403 and second.intact()          # outro alvo
    other_op = finance_entry(client)
    assert other_op.send(headers).status_code == 403 and other_op.intact()      # outra operação
    assert first.send(headers).status_code == 200 and not first.intact()        # o alvo certo ainda funciona


def test_expired_authorization_is_refused(client, clock):
    case = account_delete(client)
    clock.offset = -timedelta(minutes=10)
    headers = authorize(client, case.operation, case.target)
    clock.offset = timedelta(0)
    assert case.send(headers).status_code == 403 and case.intact()
    assert any('expirado' in (r['details'] or '') for r in audit(client, action=case.operation, target=case.target, outcome='negado'))


def test_token_valid_inside_its_window(client, clock):
    case = account_delete(client)
    clock.offset = -timedelta(seconds=authorization.TOKEN_TTL_SECONDS - 30)
    headers = authorize(client, case.operation, case.target)
    clock.offset = timedelta(0)
    assert case.send(headers).status_code == 200


def test_forged_or_garbage_tokens_are_refused(client):
    case = account_delete(client)
    for forged in ('x', 'a' * 43, 'Bearer abc', "' OR 1=1 --", authorization._hash_token('qualquer')):
        assert case.send({HEADER: forged}).status_code == 403
    assert case.intact()


def test_two_simultaneous_requests_with_one_token_execute_only_once(client):
    case = finance_entry(client)
    headers = authorize(client, case.operation, case.target)
    with ThreadPoolExecutor(max_workers=2) as pool:
        codes = sorted(f.result(timeout=30) for f in [pool.submit(lambda: case.send(headers).status_code) for _ in range(2)])
    assert codes == [200, 403]


# ---- senha: tentativas, bloqueio e sigilo ----------------------------------------------------------

def _fresh_admin():
    """Administrador exclusivo do teste: o limite de tentativas é por usuário, então não contamina os demais."""
    name = 'admin' + uuid4().hex[:8]
    _make_user(name, 'Exclusivo1', is_admin=True)
    return login(TestClient(app), {'username': name, 'password': 'Exclusivo1'}), 'Exclusivo1'


def test_wrong_passwords_are_limited_then_locked_then_released(client, clock):
    client, good = _fresh_admin()
    op, target = 'financeiro.conta.excluir', 1
    codes = []
    for _ in range(authorization.MAX_PASSWORD_FAILURES):
        response = ask(client, op, target, password='senha-errada')
        codes.append(response.status_code)
    assert codes == [403] * authorization.MAX_PASSWORD_FAILURES
    assert 'bloqueado' in response.json()['detail']
    locked = ask(client, op, target, password=good)  # senha CERTA, mas já bloqueado
    assert locked.status_code == 429 and int(locked.headers['Retry-After']) > 0
    assert audit(client, action=authorization.PASSWORD_ACTION, outcome='bloqueado')
    clock.offset += timedelta(minutes=16)
    assert ask(client, op, target, password=good).status_code == 200


def test_successful_authorization_resets_the_failure_counter(client):
    client, good = _fresh_admin()
    for _ in range(authorization.MAX_PASSWORD_FAILURES - 1):
        assert ask(client, 'financeiro.conta.excluir', 1, password='errada').status_code == 403
    assert ask(client, 'financeiro.conta.excluir', 1, password=good).status_code == 200
    for _ in range(authorization.MAX_PASSWORD_FAILURES - 1):
        assert ask(client, 'financeiro.conta.excluir', 1, password='errada').status_code == 403
    assert ask(client, 'financeiro.conta.excluir', 1, password=good).status_code == 200  # nunca chegou a bloquear


def test_unknown_operation_cannot_be_authorized(client):
    assert ask(client, 'sistema.apagar_tudo', 1).status_code == 400


def test_password_and_tokens_are_never_stored_logged_or_returned(client):
    case = finance_entry(client)
    secret = USER['password']
    response = ask(client, case.operation, case.target)
    token = response.json()['token']
    assert secret not in response.text
    assert case.send({HEADER: token}).status_code == 200
    wait_for_audit(client, action=case.operation, target=case.target, outcome='executada')
    history = client.get('/api/audit?limit=500').text
    assert secret not in history and token not in history and 'pbkdf2' not in history
    log_text = authorization.audit_file_path().read_text(encoding='utf-8')
    assert secret not in log_text and token not in log_text
    with SessionLocal() as db:
        rows = db.query(OperationAuthorization).all()
        assert rows and all(token != r.token_hash and len(r.token_hash) == 64 for r in rows)
    for path in ('/api/auth/status', '/api/accounts', '/api/finance', '/api/backups'):
        assert 'pbkdf2' not in client.get(path).text and secret not in client.get(path).text


# ---- chamadas diretas à API não contornam a autenticação ---------------------------------------------

def test_direct_api_calls_cannot_bypass_session_or_csrf():
    with TestClient(app) as anonymous:
        for method, url in [('delete', '/api/finance/1?confirm=true'), ('delete', '/api/accounts/1?confirm=true'), ('post', '/api/sales/1/cancel'),
                            ('delete', '/api/sales/1?confirm=true'), ('post', '/api/accounts/1/remove-opening-balance'),
                            ('post', '/api/backups/x.zip/restore?confirm=true'), ('post', '/api/auth/authorize-operation'), ('get', '/api/audit')]:
            assert getattr(anonymous, method)(url).status_code == 401, (method, url)
        login(anonymous)
        csrf = anonymous.headers.pop('X-CSRF-Token')
        for method, url in [('delete', '/api/finance/1?confirm=true'), ('post', '/api/sales/1/cancel')]:
            assert getattr(anonymous, method)(url).status_code == 403  # sem CSRF
        anonymous.headers['X-CSRF-Token'] = csrf


def test_confirm_flag_alone_is_not_an_authorization(client):
    case = finance_entry(client)
    assert client.delete(f'/api/finance/{case.target}?confirm=true').status_code == 403
    assert client.delete(f'/api/finance/{case.target}?confirm=true&authorized=true&admin=1').status_code == 403
    assert case.intact()


def test_editing_an_account_without_changing_the_balance_needs_no_authorization(client):
    account = client.post('/api/accounts', json={'name': 'Conta ' + uuid4().hex[:6], 'opening_balance': 50, 'opening_balance_date': '2026-01-15'}).json()
    response = client.put(f"/api/accounts/{account['id']}", json={'name': account['name'] + ' renomeada', 'opening_balance': 50, 'notes': 'só texto'})
    assert response.status_code == 200 and response.json()['notes'] == 'só texto'


# ---- permissões e usuários -----------------------------------------------------------------------------

def test_non_admin_user_cannot_authorize_or_read_the_audit_log(client):
    name = 'operador' + uuid4().hex[:6]
    _make_user(name, 'Operador1', is_admin=False)
    other = login(TestClient(app), {'username': name, 'password': 'Operador1'})
    case = finance_entry(client)
    refused = other.post('/api/auth/authorize-operation', json={'operation': case.operation, 'target': case.target, 'password': 'Operador1'})
    assert refused.status_code == 403
    assert other.delete(f'/api/finance/{case.target}?confirm=true', headers={HEADER: authorize(client, case.operation, case.target)[HEADER]}).status_code == 403
    assert case.intact()
    assert other.get('/api/audit').status_code == 403


def test_authorization_of_one_admin_cannot_be_used_by_another_session(client):
    name = 'admin' + uuid4().hex[:6]
    _make_user(name, 'Segundo1', is_admin=True)
    other = login(TestClient(app), {'username': name, 'password': 'Segundo1'})
    case = finance_entry(client)
    headers = authorize(client, case.operation, case.target)
    assert other.delete(f'/api/finance/{case.target}?confirm=true', headers=headers).status_code == 403
    assert case.intact()
    assert any('outro usuário' in (r['details'] or '') for r in audit(client, action=case.operation, target=case.target, outcome='negado'))
    assert client.delete(f'/api/finance/{case.target}?confirm=true', headers=headers).status_code == 200


# ---- trilha de auditoria ---------------------------------------------------------------------------------

def test_audit_history_is_append_only_through_the_api(client):
    for method in ('put', 'patch', 'delete', 'post'):
        assert getattr(client, method)('/api/audit/1').status_code in (404, 405)
        assert getattr(client, method)('/api/audit').status_code in (404, 405, 422) or method == 'post'
    assert client.get('/api/audit?limit=5').status_code == 200


def test_audit_file_keeps_the_trail_even_after_a_restore(client):
    backup_name = client.post('/api/backups').json()['file']
    case = account_delete(client)
    assert case.send(authorize(client, case.operation, case.target)).status_code == 200
    wait_for_audit(client, action=case.operation, target=case.target, outcome='executada')
    restore = client.post(f'/api/backups/{backup_name}/restore?confirm=true', headers=authorize(client, 'backup.restaurar', backup_name))
    assert restore.status_code == 200, restore.text
    log_text = authorization.audit_file_path().read_text(encoding='utf-8')
    assert f'"target": "{case.target}"' in log_text and 'financeiro.conta.excluir' in log_text   # o banco voltou no tempo; o arquivo, não
    assert wait_for_audit(client, action='backup.restaurar', target=backup_name, outcome='executada')  # e o banco restaurado registra a própria restauração
