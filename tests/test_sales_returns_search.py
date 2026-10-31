"""Seção 7 (venda/estoque/cancelamento/devolução) e pesquisa global (seção 4)."""
import os
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault('LMF_DATA_DIR', os.path.abspath('work/json-auth-test-data-6char'))
os.environ.setdefault('LMF_BACKUP_DIR', os.path.abspath('work/json-auth-test-backups-6char'))
os.environ.setdefault('LMF_IMPORT_BACKUP_DIR', os.path.abspath('work/json-auth-import-backups-6char'))

from app.main import app  # noqa: E402
from app import authorization  # noqa: E402
from app.database import DATA_LOCK, SessionLocal  # noqa: E402
from app.models import Movement, Product, SaleReturn  # noqa: E402
from tests.test_validation_photos import authenticate  # noqa: E402


@pytest.fixture
def client():
    with TestClient(app) as c:
        authenticate(c)
        yield c
        authorization.drain_pending(10)
        assert not DATA_LOCK.locked()


def authz(c, op, target):
    r = c.post('/api/auth/authorize-operation', json={'operation': op, 'target': str(target), 'password': 'Abc123'})
    assert r.status_code == 200, r.text
    return {authorization.AUTH_HEADER: r.json()['token']}


def product(c, qty=10, price=20, name=None):
    r = c.post('/api/products', json={'sku': 'T-' + uuid4().hex[:10], 'name': name or 'Produto ' + uuid4().hex[:6], 'quantity': qty, 'price': price, 'cost': 5, 'min_quantity': 0})
    assert r.status_code == 200, r.text
    return r.json()


def qty(c, pid):
    return next(p for p in c.get('/api/products').json() if p['id'] == pid)['quantity']


def sell(c, pid, q, key=None, **extra):
    headers = {'Idempotency-Key': key} if key else {}
    return c.post('/api/sales', json={'items': [{'product_id': pid, 'quantity': q, 'unit_price': 20}], 'received_amount': 0, **extra}, headers=headers)


def sale_items(c, sale_id):
    return c.get(f'/api/sales/{sale_id}/returns').json()['items']


# ---- venda e estoque -------------------------------------------------------------------------
def test_confirmed_sale_decrements_stock_and_records_movement(client):
    p = product(client, 10)
    r = sell(client, p['id'], 3)
    assert r.status_code == 200 and qty(client, p['id']) == 7
    with SessionLocal() as db:
        mv = db.query(Movement).filter_by(product_id=p['id'], kind='saida').all()
        assert [m.quantity for m in mv] == [3] and mv[0].reference == r.json()['number']


def test_sale_without_enough_stock_is_blocked_and_stock_never_negative(client):
    p = product(client, 2)
    r = sell(client, p['id'], 3)
    assert r.status_code == 409 and qty(client, p['id']) == 2
    assert client.post(f"/api/products/{p['id']}/movement", params={'kind': 'saida', 'quantity': 5, 'reason': 'teste'}).status_code == 409
    assert qty(client, p['id']) == 2


def test_same_sale_is_not_processed_twice(client):
    p = product(client, 10)
    key = 'k-' + uuid4().hex
    a = sell(client, p['id'], 4, key).json()
    b = sell(client, p['id'], 4, key).json()
    assert b.get('duplicate_request') is True and a['id'] == b['id'] and qty(client, p['id']) == 6


def test_concurrent_sales_never_oversell(client):
    p = product(client, 5)
    with ThreadPoolExecutor(6) as pool:
        codes = list(pool.map(lambda i: sell(client, p['id'], 1).status_code, range(10)))
    assert codes.count(200) == 5 and codes.count(409) == 5 and qty(client, p['id']) == 0


# ---- cancelamento x devolução física ----------------------------------------------------------
def test_cancel_does_not_return_stock(client):
    p = product(client, 10)
    sale = sell(client, p['id'], 4, received_amount=80).json()
    r = client.post(f"/api/sales/{sale['id']}/cancel", headers=authz(client, 'financeiro.venda.cancelar', sale['id']))
    assert r.status_code == 200 and r.json()['stock_returned'] is False and r.json()['refunded'] == 80
    assert qty(client, p['id']) == 6


def test_resellable_return_goes_back_to_stock_and_is_logged_separately(client):
    p = product(client, 10)
    sale = sell(client, p['id'], 4).json(); item = sale_items(client, sale['id'])[0]
    r = client.post(f"/api/sales/{sale['id']}/return", json={'items': [{'sale_item_id': item['sale_item_id'], 'quantity': 3, 'condition': 'revendavel'}]})
    assert r.status_code == 200 and qty(client, p['id']) == 9
    with SessionLocal() as db:
        assert [m.reason for m in db.query(Movement).filter_by(product_id=p['id'], kind='entrada').all() if 'Devolução' in m.reason]
        assert db.query(SaleReturn).filter_by(sale_id=sale['id']).count() == 1


def test_damaged_return_is_recorded_but_not_restocked(client):
    p = product(client, 10)
    sale = sell(client, p['id'], 2).json(); item = sale_items(client, sale['id'])[0]
    r = client.post(f"/api/sales/{sale['id']}/return", json={'items': [{'sale_item_id': item['sale_item_id'], 'quantity': 2, 'condition': 'danificado'}]})
    assert r.status_code == 200 and r.json()['returned'][0]['restocked'] is False and qty(client, p['id']) == 8
    assert sale_items(client, sale['id'])[0]['returned'] == 2


def test_return_cannot_exceed_sold_and_is_not_duplicated(client):
    p = product(client, 10)
    sale = sell(client, p['id'], 2).json(); item = sale_items(client, sale['id'])[0]['sale_item_id']
    body = {'items': [{'sale_item_id': item, 'quantity': 2, 'condition': 'revendavel'}]}
    key = {'Idempotency-Key': 'ret-' + uuid4().hex}
    assert client.post(f"/api/sales/{sale['id']}/return", json=body, headers=key).status_code == 200
    assert client.post(f"/api/sales/{sale['id']}/return", json=body, headers=key).json().get('duplicate_request') is True
    assert qty(client, p['id']) == 10
    assert client.post(f"/api/sales/{sale['id']}/return", json=body).status_code == 409  # já devolvido por completo
    assert qty(client, p['id']) == 10


def test_return_after_cancellation_and_invalid_cases(client):
    p = product(client, 10)
    sale = sell(client, p['id'], 2).json(); item = sale_items(client, sale['id'])[0]['sale_item_id']
    client.post(f"/api/sales/{sale['id']}/cancel", headers=authz(client, 'financeiro.venda.cancelar', sale['id']))
    assert qty(client, p['id']) == 8
    assert client.post(f"/api/sales/{sale['id']}/return", json={'items': [{'sale_item_id': item, 'quantity': 2, 'condition': 'revendavel'}]}).status_code == 200
    assert qty(client, p['id']) == 10
    assert client.post(f"/api/sales/{sale['id']}/return", json={'items': [{'sale_item_id': 999999, 'quantity': 1, 'condition': 'revendavel'}]}).status_code in (400, 409)
    assert client.post('/api/sales/99999999/return', json={'items': [{'sale_item_id': 1, 'quantity': 1, 'condition': 'revendavel'}]}).status_code == 404
    assert client.post(f"/api/sales/{sale['id']}/return", json={'items': [{'sale_item_id': item, 'quantity': 1, 'condition': 'quebrado'}]}).status_code == 422


def test_deleting_sale_does_not_restock_units_already_returned(client):
    p = product(client, 10)
    sale = sell(client, p['id'], 4).json(); item = sale_items(client, sale['id'])[0]['sale_item_id']
    client.post(f"/api/sales/{sale['id']}/return", json={'items': [{'sale_item_id': item, 'quantity': 1, 'condition': 'revendavel'}]})
    assert qty(client, p['id']) == 7
    assert client.delete(f"/api/sales/{sale['id']}?confirm=true", headers=authz(client, 'financeiro.venda.excluir', sale['id'])).status_code == 200
    assert qty(client, p['id']) == 10


# ---- indicadores e pesquisa global --------------------------------------------------------------
def test_dashboard_reflects_real_product_and_unit_counts(client):
    before = client.get('/api/dashboard').json()
    p = product(client, 7)
    after = client.get('/api/dashboard').json()
    assert after['products'] == before['products'] + 1 and after['stock_units'] == before['stock_units'] + 7
    sell(client, p['id'], 2)
    assert client.get('/api/dashboard').json()['stock_units'] == before['stock_units'] + 5


def test_global_search_groups_results_by_category(client):
    tag = 'zq' + uuid4().hex[:8]
    prod = product(client, 1, name=f'Cabo {tag}')
    sup = client.post('/api/suppliers', json={'name': f'Fornecedor {tag}'}).json()
    cust = client.post('/api/customers', json={'name': f'Cliente {tag}'}).json()
    so = client.post('/api/service-orders', json={'customer_id': cust['id'], 'equipment': f'Equip {tag}', 'equipment_type': 'Celular', 'serial': '000000000000001', 'reported_fault': 'x'}).json()
    ped = client.post('/api/orders', json={'description': f'Pedido {tag}'}).json()
    gar = client.post('/api/guarantees', json={'customer_id': cust['id'], 'item': f'Garantia {tag}', 'starts_on': '2026-10-01'}).json()
    r = client.get('/api/search', params={'q': tag})
    assert r.status_code == 200
    groups = {g['category']: g for g in r.json()['groups']}
    assert set(groups) == {'Estoque', 'Fornecedores', 'Pedidos', 'Ordens de serviço', 'Garantias'}
    assert groups['Estoque']['items'][0]['id'] == prod['id'] and groups['Fornecedores']['items'][0]['id'] == sup['id']
    assert groups['Ordens de serviço']['items'][0]['id'] == so['id'] and groups['Pedidos']['items'][0]['id'] == ped['id']
    assert groups['Garantias']['items'][0]['id'] == gar['id']
    assert all(g['module'] and all('title' in i for i in g['items']) for g in groups.values())


def test_global_search_empty_and_short_queries(client):
    assert client.get('/api/search', params={'q': ''}).json()['groups'] == []
    assert client.get('/api/search', params={'q': 'nada-' + uuid4().hex}).json()['groups'] == []
