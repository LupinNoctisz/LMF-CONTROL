"""Seções 6, 7.4 e 8: garantias por OS/venda, expiradas, contrato, anexos e comprovantes financeiros."""
import base64
import json
import os
import zipfile
from datetime import date, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault('LMF_DATA_DIR', os.path.abspath('work/json-auth-test-data-6char'))
os.environ.setdefault('LMF_BACKUP_DIR', os.path.abspath('work/json-auth-test-backups-6char'))
os.environ.setdefault('LMF_IMPORT_BACKUP_DIR', os.path.abspath('work/json-auth-import-backups-6char'))

from app.main import app  # noqa: E402
from app import authorization, storage  # noqa: E402
from app.database import DATA_LOCK, SessionLocal  # noqa: E402
from app.models import Guarantee, Movement, Product, Sale, ServiceOrder  # noqa: E402
from app.services import backup_service  # noqa: E402
from tests.test_validation_photos import authenticate, unique_cpf, PNG  # noqa: E402

JPG = b'\xff\xd8\xff\xe0' + b'\x00' * 40
PDF = b'%PDF-1.4\n%fake but valid header\n'


@pytest.fixture
def client():
    with TestClient(app) as c:
        authenticate(c)
        yield c
        authorization.drain_pending(10)
        assert not DATA_LOCK.locked()


def customer(c):
    return c.post('/api/customers', json={'name': 'Cli ' + uuid4().hex[:5], 'document': unique_cpf()}).json()


def order(c, cust=None, **extra):
    cust = cust or customer(c)
    body = {'customer_id': cust['id'], 'equipment': 'Notebook Dell Inspiron', 'equipment_type': 'Notebook', 'brand': 'Dell', 'model': 'Inspiron 15', 'serial': '000000000000007', 'reported_fault': 'Não liga', 'service_executed': 'Troca de SSD', 'completed_on': '2026-10-10'}
    body.update(extra)
    r = c.post('/api/service-orders', json=body); assert r.status_code == 200, r.text
    return r.json()


def product(c, qty=10):
    return c.post('/api/products', json={'sku': 'G-' + uuid4().hex[:8], 'name': 'Fone ' + uuid4().hex[:4], 'brand': 'JBL', 'model': 'T110', 'quantity': qty, 'price': 50, 'cost': 20, 'min_quantity': 0}).json()


def sale(c, items, cust=None):
    body = {'items': items, 'received_amount': 0}
    if cust: body['customer_id'] = cust['id']
    r = c.post('/api/sales', json=body); assert r.status_code == 200, r.text
    return r.json()


def snapshot_order(oid):
    with SessionLocal() as db:
        o = db.get(ServiceOrder, oid)
        return {c.name: str(getattr(o, c.name)) for c in ServiceOrder.__table__.columns}


# ---- origem: ordem de serviço ---------------------------------------------------------------
def test_guarantee_from_service_order_needs_only_the_order_id_and_autofills(client):
    cust = customer(client); o = order(client, cust)
    r = client.post('/api/guarantees', json={'origin': 'ordem_servico', 'service_order_id': o['id']})
    assert r.status_code == 200, r.text
    g = r.json()
    assert (g['origin'], g['service_order_id'], g['customer_id'], g['equipment_type'], g['brand'], g['model'], g['serial'], g['service_performed']) == ('ordem_servico', o['id'], cust['id'], 'Notebook', 'Dell', 'Inspiron 15', '000000000000007', 'Troca de SSD')
    assert g['starts_on'] == '2026-10-10' and g['duration_days'] == 90 and g['ends_on'] == '2027-01-08' and 'Notebook' in g['item']
    pre = client.get('/api/guarantees/prefill', params={'origin': 'ordem_servico', 'ref': o['number']}).json()
    assert pre['customer_name'] == cust['name'] and pre['serial'] == '000000000000007' and pre['ends_on'] == '2027-01-08'


def test_guarantee_from_missing_order_is_blocked(client):
    assert client.post('/api/guarantees', json={'origin': 'ordem_servico', 'service_order_id': 99999999}).status_code == 404
    assert client.post('/api/guarantees', json={'origin': 'ordem_servico'}).status_code == 404
    assert client.get('/api/guarantees/prefill', params={'origin': 'ordem_servico', 'ref': 'LMF-OS-0000-0000'}).status_code == 404


# ---- origem: venda ----------------------------------------------------------------------------
def test_guarantee_from_sale_does_not_need_an_order_and_autofills(client):
    cust = customer(client); p = product(client)
    s = sale(client, [{'product_id': p['id'], 'quantity': 2, 'unit_price': 50}], cust)
    r = client.post('/api/guarantees', json={'origin': 'venda', 'sale_id': s['id']})
    assert r.status_code == 200, r.text
    g = r.json()
    assert g['origin'] == 'venda' and g['service_order_id'] is None and g['sale_id'] == s['id'] and g['customer_id'] == cust['id']
    assert (g['item'], g['brand'], g['model'], g['quantity']) == (p['name'], 'JBL', 'T110', 2)
    assert g['purchased_on'] == date.today().isoformat() and g['starts_on'] == g['purchased_on'] and g['duration_days'] == 90
    assert g['ends_on'] == (date.today() + timedelta(days=90)).isoformat()


def test_sale_with_several_items_requires_choosing_one_and_pending_sale_is_blocked(client):
    cust = customer(client); a, b = product(client), product(client)
    s = sale(client, [{'product_id': a['id'], 'quantity': 1, 'unit_price': 10}, {'product_id': b['id'], 'quantity': 1, 'unit_price': 10}], cust)
    assert client.post('/api/guarantees', json={'origin': 'venda', 'sale_id': s['id']}).status_code == 400
    pre = client.get('/api/guarantees/prefill', params={'origin': 'venda', 'ref': s['number']}).json()
    assert pre['needs_item_choice'] and len(pre['items']) == 2
    ok = client.post('/api/guarantees', json={'origin': 'venda', 'sale_id': s['id'], 'sale_item_id': pre['items'][1]['sale_item_id']})
    assert ok.status_code == 200 and ok.json()['item'] == b['name']
    pend = client.post('/api/sales', json={'items': [{'product_id': a['id'], 'quantity': 1, 'unit_price': 10}], 'status': 'pendente', 'customer_id': cust['id']}).json()
    assert client.post('/api/guarantees', json={'origin': 'venda', 'sale_id': pend['id']}).status_code == 409
    assert client.post('/api/guarantees', json={'origin': 'venda', 'sale_id': 99999999}).status_code == 404


def test_sale_without_customer_needs_one_and_manual_needs_data(client):
    p = product(client)
    s = sale(client, [{'product_id': p['id'], 'quantity': 1, 'unit_price': 10}])
    assert client.post('/api/guarantees', json={'origin': 'venda', 'sale_id': s['id']}).status_code == 400
    cust = customer(client)
    assert client.post('/api/guarantees', json={'origin': 'venda', 'sale_id': s['id'], 'customer_id': cust['id']}).status_code == 200
    assert client.post('/api/guarantees', json={'origin': 'manual', 'item': 'Cabo'}).status_code == 400
    m = client.post('/api/guarantees', json={'origin': 'manual', 'customer_id': cust['id'], 'item': 'Cabo HDMI', 'starts_on': date.today().isoformat()})
    assert m.status_code == 200 and m.json()['origin'] == 'manual' and m.json()['sale_id'] is None and m.json()['service_order_id'] is None


# ---- unidirecionalidade -------------------------------------------------------------------------
def test_guarantee_changes_never_touch_order_sale_or_stock(client):
    cust = customer(client); o = order(client, cust); p = product(client, 10)
    s = sale(client, [{'product_id': p['id'], 'quantity': 3, 'unit_price': 50}], cust)
    with SessionLocal() as db:
        before_sale = {c.name: str(getattr(db.get(Sale, s['id']), c.name)) for c in Sale.__table__.columns}
        before_stock = db.get(Product, p['id']).quantity; before_moves = db.query(Movement).count()
    before_order = snapshot_order(o['id'])
    g1 = client.post('/api/guarantees', json={'origin': 'ordem_servico', 'service_order_id': o['id']}).json()
    g2 = client.post('/api/guarantees', json={'origin': 'venda', 'sale_id': s['id']}).json()
    for g in (g1, g2):
        client.put(f"/api/guarantees/{g['id']}", json={**g, 'notes': 'editado', 'status': 'cancelada', 'serial': '999', 'customer_id': 999999})
        client.post(f"/api/guarantees/{g['id']}/occurrences", json={'issue': 'defeito x'})
    assert snapshot_order(o['id']) == before_order
    with SessionLocal() as db:
        assert {c.name: str(getattr(db.get(Sale, s['id']), c.name)) for c in Sale.__table__.columns} == before_sale
        assert db.get(Product, p['id']).quantity == before_stock and db.query(Movement).count() == before_moves


def test_origin_and_links_cannot_be_changed_after_creation(client):
    o1, o2 = order(client), order(client)
    g = client.post('/api/guarantees', json={'origin': 'ordem_servico', 'service_order_id': o1['id']}).json()
    r = client.put(f"/api/guarantees/{g['id']}", json={'origin': 'manual', 'service_order_id': o2['id'], 'customer_id': g['customer_id'], 'item': g['item'], 'starts_on': g['starts_on']}).json()
    assert r['origin'] == 'ordem_servico' and r['service_order_id'] == o1['id']


# ---- expiração, listagem e histórico ---------------------------------------------------------------
def test_expired_leave_main_list_but_stay_in_history_with_attachments(client):
    cust = customer(client)
    g = client.post('/api/guarantees', json={'origin': 'manual', 'customer_id': cust['id'], 'item': 'Velha ' + uuid4().hex[:5], 'starts_on': str(date.today() - timedelta(days=200))}).json()
    assert g['status'] == 'expirada'
    active = client.get('/api/guarantees').json()
    assert g['id'] not in [x['id'] for x in active] and all(x['status'] == 'ativa' for x in active)
    assert g['id'] in [x['id'] for x in client.get('/api/guarantees', params={'scope': 'expiradas'}).json()]
    assert g['id'] in [x['id'] for x in client.get('/api/guarantees', params={'scope': 'todas'}).json()]
    up = client.post(f"/api/guarantees/{g['id']}/attachments", files=[('files', ('p1.png', PNG, 'image/png'))], data={'kind': 'contrato'})
    assert up.status_code == 200
    with SessionLocal() as db:
        assert db.get(Guarantee, g['id']) is not None
    assert client.get(up.json()['items'][0]['url']).content == PNG


# ---- contrato -----------------------------------------------------------------------------------------
def test_contract_can_be_viewed_downloaded_and_is_not_modified(client):
    from app.main import CONTRACT_PATH
    before = CONTRACT_PATH.read_bytes()
    view = client.get('/api/guarantees/contract/view')
    assert view.status_code == 200 and 'TERMO DE GARANTIA' in view.text and 'window.print' in view.text
    dl = client.get('/api/guarantees/contract/file')
    assert dl.status_code == 200 and dl.content == before and 'attachment' in dl.headers['content-disposition']
    assert dl.content[:2] == b'PK' and CONTRACT_PATH.read_bytes() == before


# ---- anexos de garantia -------------------------------------------------------------------------------
def test_multiple_images_jpg_png_only_and_stored_in_garantias_folder(client):
    cust = customer(client)
    g = client.post('/api/guarantees', json={'origin': 'manual', 'customer_id': cust['id'], 'item': 'Anexos', 'starts_on': date.today().isoformat()}).json()
    r = client.post(f"/api/guarantees/{g['id']}/attachments", files=[('files', ('pag1.png', PNG, 'image/png')), ('files', ('pag2.jpg', JPG, 'image/jpeg')), ('files', ('assin.JPEG', JPG + b'x', 'image/jpeg'))], data={'kind': 'assinatura'})
    assert r.status_code == 200 and len(r.json()['items']) == 3
    items = client.get(f"/api/guarantees/{g['id']}/attachments").json()['items']
    assert [i['original_name'] for i in items] == ['pag1.png', 'pag2.jpg', 'assin.JPEG'] and all(i['kind'] == 'assinatura' for i in items)
    for i in items:
        assert client.get(i['url']).status_code == 200
        assert 'attachment' in client.get(i['url'], params={'download': True}).headers['content-disposition']
    with SessionLocal() as db:
        from app.models import GuaranteeAttachment
        rows = db.query(GuaranteeAttachment).filter_by(guarantee_id=g['id']).all()
        assert all(r.relative_path.startswith(f"garantias/{g['id']}/") and storage.resolve(r.relative_path).is_file() and len(r.sha256) == 64 for r in rows)
        assert len({r.relative_path for r in rows}) == 3


@pytest.mark.parametrize('name,data', [('x.pdf', PDF), ('x.gif', b'GIF89a' + b'0' * 20), ('x.png', b'nao e imagem'), ('x.webp', b'RIFF0000WEBPVP8 '), ('v.png', b'')])
def test_guarantee_attachments_reject_other_types_and_save_nothing(client, name, data):
    cust = customer(client)
    g = client.post('/api/guarantees', json={'origin': 'manual', 'customer_id': cust['id'], 'item': 'Rej', 'starts_on': date.today().isoformat()}).json()
    r = client.post(f"/api/guarantees/{g['id']}/attachments", files=[('files', ('ok.png', PNG, 'image/png')), ('files', (name, data, 'image/png'))])
    assert r.status_code == 400
    assert client.get(f"/api/guarantees/{g['id']}/attachments").json()['items'] == []


def test_attachment_of_other_guarantee_is_not_served(client):
    cust = customer(client)
    mk = lambda: client.post('/api/guarantees', json={'origin': 'manual', 'customer_id': cust['id'], 'item': 'G', 'starts_on': date.today().isoformat()}).json()
    a, b = mk(), mk()
    att = client.post(f"/api/guarantees/{a['id']}/attachments", files=[('files', ('a.png', PNG, 'image/png'))]).json()['items'][0]
    assert client.get(f"/api/guarantees/{b['id']}/attachments/{att['id']}/file").status_code == 404
    assert client.post('/api/guarantees/99999999/attachments', files=[('files', ('a.png', PNG, 'image/png'))]).status_code == 404


# ---- comprovantes financeiros ----------------------------------------------------------------------------
def entry(c):
    r = c.post('/api/finance', json={'kind': 'despesa', 'category': 'Teste', 'description': 'Comp ' + uuid4().hex[:5], 'amount': 10, 'status': 'pendente'}); assert r.status_code == 200
    return r.json()


def test_receipts_pdf_and_images_multiple_view_and_download(client):
    e = entry(client)
    r = client.post(f"/api/finance/{e['id']}/receipts", files=[('files', ('rec.pdf', PDF, 'application/pdf')), ('files', ('foto.png', PNG, 'image/png')), ('files', ('foto2.jpg', JPG, 'image/jpeg'))])
    assert r.status_code == 200 and len(r.json()['items']) == 3
    items = client.get(f"/api/finance/{e['id']}/receipts").json()['items']
    assert {i['content_type'] for i in items} == {'application/pdf', 'image/png', 'image/jpeg'}
    pdf = next(i for i in items if i['content_type'] == 'application/pdf')
    assert client.get(pdf['url']).content == PDF and 'inline' in client.get(pdf['url']).headers['content-disposition']
    assert 'attachment' in client.get(pdf['url'], params={'download': True}).headers['content-disposition']
    with SessionLocal() as db:
        from app.models import FinanceReceipt
        assert all(r.relative_path.startswith(f"financeiro/comprovantes/{e['id']}/") for r in db.query(FinanceReceipt).filter_by(financial_entry_id=e['id']))


def test_receipt_rejects_invalid_files_and_wrong_entry(client):
    e, e2 = entry(client), entry(client)
    assert client.post(f"/api/finance/{e['id']}/receipts", files=[('files', ('x.exe', b'MZ1234', 'application/pdf'))]).status_code == 400
    assert client.post('/api/finance/99999999/receipts', files=[('files', ('a.png', PNG, 'image/png'))]).status_code == 404
    rec = client.post(f"/api/finance/{e['id']}/receipts", files=[('files', ('a.png', PNG, 'image/png'))]).json()['items'][0]
    assert client.get(f"/api/finance/{e2['id']}/receipts/{rec['id']}/file").status_code == 404


def test_removing_a_receipt_keeps_the_transaction_and_archives_the_file(client):
    e = entry(client)
    rec = client.post(f"/api/finance/{e['id']}/receipts", files=[('files', ('a.png', PNG, 'image/png'))]).json()['items'][0]
    out = client.delete(f"/api/finance/{e['id']}/receipts/{rec['id']}")
    assert out.status_code == 200 and out.json()['archived_as'].startswith('financeiro/comprovantes/_removidos/')
    assert storage.resolve(out.json()['archived_as']).read_bytes() == PNG
    assert client.get(f"/api/finance/{e['id']}/receipts").json()['items'] == []
    assert any(x['id'] == e['id'] for x in client.get('/api/finance').json())


# ---- backup inclui anexos e comprovantes ------------------------------------------------------------------
def test_backup_contains_attachments_and_receipts_and_restore_recovers_them(client):
    cust = customer(client)
    g = client.post('/api/guarantees', json={'origin': 'manual', 'customer_id': cust['id'], 'item': 'Bkp', 'starts_on': date.today().isoformat()}).json()
    att = client.post(f"/api/guarantees/{g['id']}/attachments", files=[('files', ('a.png', PNG, 'image/png'))]).json()['items'][0]
    e = entry(client)
    rec = client.post(f"/api/finance/{e['id']}/receipts", files=[('files', ('r.pdf', PDF, 'application/pdf'))]).json()['items'][0]
    archive = backup_service.create_backup('anexos')
    m = json.loads(zipfile.ZipFile(archive).read('manifest.json'))
    assert m['complete'] is True
    tables = {r['table'] for r in m['references']}
    assert {'guarantee_attachments', 'finance_receipts'} <= tables
    with SessionLocal() as db:
        from app.models import GuaranteeAttachment, FinanceReceipt
        paths = [db.get(GuaranteeAttachment, att['id']).relative_path, db.get(FinanceReceipt, rec['id']).relative_path]
    for p in paths:
        storage.resolve(p).unlink()
    assert client.get(att['url']).status_code == 404
    backup_service.restore_backup(archive)
    assert client.get(att['url']).content == PNG and client.get(rec['url']).content == PDF
