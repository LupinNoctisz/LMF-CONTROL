"""12.B - Garantia de 90 dias contados da DATA DE INÍCIO, com expiração automática."""
import os
import threading
import time
from datetime import date, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault('LMF_DATA_DIR', os.path.abspath('work/json-auth-test-data-6char'))
os.environ.setdefault('LMF_BACKUP_DIR', os.path.abspath('work/json-auth-test-backups-6char'))
os.environ.setdefault('LMF_IMPORT_BACKUP_DIR', os.path.abspath('work/json-auth-import-backups-6char'))

from app.main import app  # noqa: E402
from app import authorization, guarantees  # noqa: E402
from app.database import DATA_LOCK, SessionLocal, DATA_DIR  # noqa: E402
from app.models import Guarantee  # noqa: E402

USER = {'username': 'testadmin', 'password': 'Abc123'}


@pytest.fixture
def client():
    with TestClient(app) as c:
        status = c.get('/api/auth/status').json()
        r = c.post('/api/auth/setup' if status['setup_required'] else '/api/auth/login', json=USER)
        assert r.status_code == 200, r.text
        c.headers.update({'X-CSRF-Token': r.json()['csrf_token']})
        yield c
        authorization.drain_pending(10)
        assert not DATA_LOCK.locked()


@pytest.fixture
def customer(client):
    return client.post('/api/customers', json={'name': 'Cliente garantia ' + uuid4().hex[:6]}).json()['id']


def make(client, customer, starts_on, **extra):
    r = client.post('/api/guarantees', json={'customer_id': customer, 'item': 'Tela', 'starts_on': str(starts_on), **extra})
    assert r.status_code == 200, r.text
    return r.json()


def test_due_date_is_start_plus_90_days_recorded_at_creation(client, customer):
    start = date(2026, 3, 1)
    g = make(client, customer, start)
    assert g['ends_on'] == str(start + timedelta(days=90)) == '2026-05-30'
    assert g['duration_days'] == 90
    stored = client.get('/api/guarantees', params={'scope': 'todas'}).json()
    assert next(x for x in stored if x['id'] == g['id'])['ends_on'] == '2026-05-30'


def test_counted_from_start_date_not_from_registration_date(client, customer):
    past = date.today() - timedelta(days=200)
    g = make(client, customer, past)
    assert g['ends_on'] == str(past + timedelta(days=90))
    assert g['ends_on'] != str(date.today() + timedelta(days=90))


def test_client_supplied_due_date_is_ignored(client, customer):
    start = date.today()
    g = make(client, customer, start, ends_on=str(start + timedelta(days=3650)))
    assert g['ends_on'] == str(start + timedelta(days=90)) and g['duration_days'] == 90


def test_custom_term_is_honored_when_registered_and_validated(client, customer):
    g = make(client, customer, date.today(), duration_days=180)
    assert g['duration_days'] == 180 and g['ends_on'] == str(date.today() + timedelta(days=180))
    assert client.post('/api/guarantees', json={'customer_id': customer, 'item': 'x', 'starts_on': str(date.today()), 'duration_days': 0}).status_code == 422


def test_update_without_changing_start_keeps_stored_due_date(client, customer):
    start = date.today()
    g = make(client, customer, start)
    body = {**g, 'notes': 'obs', 'ends_on': '2099-01-01'}
    r = client.put(f"/api/guarantees/{g['id']}", json=body)
    assert r.status_code == 200
    assert r.json()['ends_on'] == g['ends_on'] and r.json()['duration_days'] == 90 and r.json()['notes'] == 'obs'


def test_changing_start_date_recomputes_due_date(client, customer):
    g = make(client, customer, date.today())
    new_start = date.today() + timedelta(days=10)
    r = client.put(f"/api/guarantees/{g['id']}", json={**g, 'starts_on': str(new_start)})
    assert r.json()['ends_on'] == str(new_start + timedelta(days=90))


def test_legacy_due_date_is_preserved_on_unrelated_update(client, customer):
    g = make(client, customer, date.today())
    with SessionLocal() as db:  # simula registro antigo com prazo diferente
        row = db.get(Guarantee, g['id']); row.ends_on = date.today() + timedelta(days=365); db.commit()
    r = client.put(f"/api/guarantees/{g['id']}", json={**g, 'notes': 'x'})
    assert r.json()['ends_on'] == str(date.today() + timedelta(days=365))


def test_expired_on_creation_when_start_is_old(client, customer):
    g = make(client, customer, date.today() - timedelta(days=91))
    assert g['status'] == 'expirada'


def test_last_day_is_still_active_next_day_expires(client, customer):
    today = date.today()
    g = make(client, customer, today - timedelta(days=90))  # vence hoje
    assert g['ends_on'] == str(today) and g['status'] == 'ativa'
    with SessionLocal() as db:
        assert guarantees.expire_due_guarantees(db, today) == 0
        assert guarantees.expire_due_guarantees(db, today + timedelta(days=1)) >= 1
        assert db.get(Guarantee, g['id']).status == 'expirada'


def test_expiry_sweep_changes_only_status(client, customer):
    g = make(client, customer, date.today() - timedelta(days=89), conditions='cond', notes='nota')
    with SessionLocal() as db:
        before = {c.name: getattr(db.get(Guarantee, g['id']), c.name) for c in Guarantee.__table__.columns if c.name not in ('status', 'updated_at')}
        guarantees.expire_due_guarantees(db, date.today() + timedelta(days=5))
        row = db.get(Guarantee, g['id'])
        after = {c.name: getattr(row, c.name) for c in Guarantee.__table__.columns if c.name not in ('status', 'updated_at')}
        assert row.status == 'expirada' and before == after


def test_sweep_ignores_cancelled_and_is_idempotent(client, customer):
    g = make(client, customer, date.today() - timedelta(days=30), status='cancelada')
    with SessionLocal() as db:
        guarantees.expire_due_guarantees(db, date.today() + timedelta(days=400))
        assert db.get(Guarantee, g['id']).status == 'cancelada'
        assert guarantees.expire_due_guarantees(db, date.today() + timedelta(days=400)) == 0


def test_sweep_does_not_touch_document_files(client, customer):
    docs = DATA_DIR / 'documentos'; docs.mkdir(parents=True, exist_ok=True)
    f = docs / f'garantia-{uuid4().hex}.txt'; f.write_bytes(b'documento original')
    stat = (f.stat().st_mtime_ns, f.read_bytes())
    make(client, customer, date.today() - timedelta(days=89))
    with SessionLocal() as db:
        guarantees.expire_due_guarantees(db, date.today() + timedelta(days=30))
    assert (f.stat().st_mtime_ns, f.read_bytes()) == stat
    f.unlink()


def test_listing_shows_expired_without_any_manual_action(client, customer):
    g = make(client, customer, date.today() - timedelta(days=10))
    with SessionLocal() as db:  # envelhece o registro direto no banco, como se os dias tivessem passado
        row = db.get(Guarantee, g['id']); row.ends_on = date.today() - timedelta(days=1); db.commit()
    assert next(x for x in client.get('/api/guarantees', params={'scope': 'todas'}).json() if x['id'] == g['id'])['status'] == 'expirada'


def test_background_task_expires_without_opening_any_page(customer_name='bg'):
    """Sem nenhuma requisição de listagem: só a tarefa de fundo (iniciada no lifespan) pode mudar o status."""
    with TestClient(app) as c:
        status = c.get('/api/auth/status').json()
        r = c.post('/api/auth/setup' if status['setup_required'] else '/api/auth/login', json=USER)
        c.headers.update({'X-CSRF-Token': r.json()['csrf_token']})
        cid = c.post('/api/customers', json={'name': 'bg ' + uuid4().hex[:6]}).json()['id']
        g = make(c, cid, date.today())
        with SessionLocal() as db:
            row = db.get(Guarantee, g['id']); row.ends_on = date.today() - timedelta(days=1); db.commit()
    # reinicia o app: a tarefa de início/fundo deve expirar sem que ninguém liste garantias
    with TestClient(app):
        deadline = time.time() + 15
        status_now = None
        while time.time() < deadline:
            with SessionLocal() as db:
                status_now = db.get(Guarantee, g['id']).status
            if status_now == 'expirada':
                break
            time.sleep(0.3)
        assert status_now == 'expirada'


def test_cannot_open_occurrence_on_expired_guarantee(client, customer):
    g = make(client, customer, date.today() - timedelta(days=120))
    r = client.post(f"/api/guarantees/{g['id']}/occurrences", json={'issue': 'defeito'})
    assert r.status_code == 409
