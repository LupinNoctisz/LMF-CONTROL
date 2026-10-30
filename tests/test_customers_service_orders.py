"""Seções 3 e 5 do prompt mestre: CPF, número de série, tipo de equipamento, cliente dentro da OS e nº sequencial."""
from concurrent.futures import ThreadPoolExecutor
import os

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault('LMF_DATA_DIR', os.path.abspath('work/json-auth-test-data-6char'))
os.environ.setdefault('LMF_BACKUP_DIR', os.path.abspath('work/json-auth-test-backups-6char'))
os.environ.setdefault('LMF_IMPORT_BACKUP_DIR', os.path.abspath('work/json-auth-import-backups-6char'))

from app.main import app  # noqa: E402
from app import authorization, customers as customer_rules  # noqa: E402
from app.database import DATA_LOCK, SessionLocal  # noqa: E402
from app.models import Customer  # noqa: E402
from app.validation import normalize_cpf, normalize_serial, SERIAL_MESSAGE  # noqa: E402
from tests.test_validation_photos import authenticate, unique_cpf  # noqa: E402

MSG = 'Número de série incorreto. O número de série deve conter exatamente 15 dígitos.'


@pytest.fixture
def client():
    with TestClient(app) as c:
        authenticate(c)
        yield c
        authorization.drain_pending(10)
        assert not DATA_LOCK.locked()


def os_body(**extra):
    body = {'equipment': 'Notebook Dell', 'equipment_type': 'Notebook', 'serial': '012345678901234', 'reported_fault': 'Não liga'}
    body.update(extra)
    return body


def new_customer(client, name='Cliente teste'):
    r = client.post('/api/customers', json={'name': name, 'document': unique_cpf()})
    assert r.status_code == 200, r.text
    return r.json()


# ---- CPF ------------------------------------------------------------------------------------
@pytest.mark.parametrize('cpf', ['529.982.247-24', '52998224726', '111.111.111-11', '00000000000', '123.456.789-00'])
def test_cpf_with_wrong_check_digits_is_rejected(cpf):
    with pytest.raises(ValueError):
        normalize_cpf(cpf)


def test_cpf_with_valid_check_digits_is_accepted():
    assert normalize_cpf('52998224725') == '529.982.247-25'


def test_api_rejects_invalid_cpf(client):
    assert client.post('/api/customers', json={'name': 'X', 'document': '111.111.111-11'}).status_code == 422


def test_duplicate_cpf_is_blocked_on_create_and_update(client):
    first = new_customer(client)
    dup = client.post('/api/customers', json={'name': 'Outro', 'document': first['document']})
    assert dup.status_code == 409 and 'CPF' in dup.text
    second = new_customer(client, 'Segundo')
    upd = client.put(f"/api/customers/{second['id']}", json={'name': 'Segundo', 'document': first['document']})
    assert upd.status_code == 409
    same = client.put(f"/api/customers/{first['id']}", json={'name': 'Primeiro editado', 'document': first['document']})
    assert same.status_code == 200 and same.json()['public_seq'] == first['public_seq']


# ---- número de série --------------------------------------------------------------------------
@pytest.mark.parametrize('serial', ['01234567890123', '0123456789012345', '', '12345678901234A', '1234 5678 90123 4', '١٢٣٤٥٦٧٨٩٠١٢٣٤٥', None])
def test_serial_must_have_exactly_15_digits(serial):
    with pytest.raises(ValueError, match='Número de série incorreto'):
        normalize_serial(serial)


def test_serial_15_digits_preserves_leading_zeros():
    assert normalize_serial('000000000000123') == '000000000000123'


def test_api_rejects_14_and_16_digits_with_exact_message(client):
    cust = new_customer(client)
    for bad in ('01234567890123', '0123456789012345'):
        r = client.post('/api/service-orders', json=os_body(customer_id=cust['id'], serial=bad))
        assert r.status_code == 422 and MSG in r.text, r.text
    missing = os_body(customer_id=cust['id']); missing.pop('serial')
    assert client.post('/api/service-orders', json=missing).status_code == 422


def test_api_15_digits_saved_as_text_with_leading_zeros(client):
    cust = new_customer(client)
    r = client.post('/api/service-orders', json=os_body(customer_id=cust['id'], serial='000000000000042'))
    assert r.status_code == 200 and r.json()['serial'] == '000000000000042'
    listed = next(x for x in client.get('/api/service-orders').json() if x['id'] == r.json()['id'])
    assert listed['serial'] == '000000000000042'


def test_update_also_validates_serial(client):
    cust = new_customer(client)
    created = client.post('/api/service-orders', json=os_body(customer_id=cust['id'])).json()
    bad = client.put(f"/api/service-orders/{created['id']}", json=os_body(customer_id=cust['id'], serial='123'))
    assert bad.status_code == 422 and MSG in bad.text
    ok = client.put(f"/api/service-orders/{created['id']}", json=os_body(customer_id=cust['id'], serial='999999999999999'))
    assert ok.status_code == 200 and ok.json()['serial'] == '999999999999999'


# ---- tipo de equipamento ---------------------------------------------------------------------
@pytest.mark.parametrize('kind', ['Celular', 'Computador', 'Notebook', 'Videogame'])
def test_each_equipment_type_is_saved_and_filterable(client, kind):
    cust = new_customer(client)
    r = client.post('/api/service-orders', json=os_body(customer_id=cust['id'], equipment_type=kind))
    assert r.status_code == 200 and r.json()['equipment_type'] == kind
    filtered = client.get('/api/service-orders', params={'equipment_type': kind}).json()
    assert r.json()['id'] in [x['id'] for x in filtered] and all(x['equipment_type'] == kind for x in filtered)
    assert r.json()['id'] in [x['id'] for x in client.get('/api/service-orders', params={'q': kind}).json()]


@pytest.mark.parametrize('kind', ['Tablet', '', None])
def test_equipment_type_is_required_and_restricted(client, kind):
    cust = new_customer(client)
    body = os_body(customer_id=cust['id'])
    if kind is None: body.pop('equipment_type')
    else: body['equipment_type'] = kind
    assert client.post('/api/service-orders', json=body).status_code == 422


@pytest.mark.parametrize('field', ['estimate', 'cost', 'charged'])
def test_negative_values_are_rejected_and_zero_and_large_are_accepted(client, field):
    cust = new_customer(client)
    assert client.post('/api/service-orders', json=os_body(customer_id=cust['id'], **{field: -1})).status_code == 422
    assert client.post('/api/service-orders', json=os_body(customer_id=cust['id'], **{field: 0})).status_code == 200
    assert client.post('/api/service-orders', json=os_body(customer_id=cust['id'], **{field: 99999999999.5})).status_code == 200


# ---- cliente dentro da OS ---------------------------------------------------------------------
def test_new_customer_is_created_inside_the_order(client):
    cpf = unique_cpf()
    r = client.post('/api/service-orders', json=os_body(customer={'name': 'Joana Nova', 'document': cpf, 'phone': '92998158767'}))
    assert r.status_code == 200, r.text
    found = client.get('/api/customers/lookup', params={'document': cpf}).json()
    assert found['name'] == 'Joana Nova' and found['id'] == r.json()['customer_id'] and found['phone'] == '(92) 9 9815-8767'
    assert found['public_code'] == f"{found['public_seq']:02d}"


def test_existing_customer_is_reused_by_cpf_without_duplicates(client):
    cpf = unique_cpf()
    a = client.post('/api/service-orders', json=os_body(customer={'name': 'Reuso', 'document': cpf})).json()
    b = client.post('/api/service-orders', json=os_body(customer={'name': 'Nome diferente', 'document': cpf})).json()
    assert a['customer_id'] == b['customer_id']
    with SessionLocal() as db:
        assert db.query(Customer).filter(Customer.document == normalize_cpf(cpf)).count() == 1


def test_order_with_invalid_cpf_or_without_customer_is_blocked(client):
    assert client.post('/api/service-orders', json=os_body(customer={'name': 'X', 'document': '111.111.111-11'})).status_code == 422
    assert client.post('/api/service-orders', json=os_body()).status_code == 400
    assert client.post('/api/service-orders', json=os_body(customer={'name': 'Sem CPF'})).status_code == 400
    assert client.post('/api/service-orders', json=os_body(customer={'document': unique_cpf()})).status_code == 400  # novo sem nome
    assert client.post('/api/service-orders', json=os_body(customer_id=99999999)).status_code == 400


def test_lookup_endpoint(client):
    assert client.get('/api/customers/lookup', params={'document': unique_cpf()}).status_code == 404
    assert client.get('/api/customers/lookup', params={'document': '123'}).status_code == 400


# ---- identificador sequencial ------------------------------------------------------------------
def test_sequence_increments_by_one_and_is_never_reused(client):
    a = new_customer(client); b = new_customer(client)
    assert b['public_seq'] == a['public_seq'] + 1
    assert client.delete(f"/api/customers/{b['id']}?confirm=true").status_code == 200
    c = new_customer(client)
    assert c['public_seq'] == b['public_seq'] + 1  # o número do cliente excluído não volta


def test_public_code_has_at_least_two_digits_and_grows_past_99():
    assert customer_rules.public_code(1) == '01' and customer_rules.public_code(99) == '99'
    assert customer_rules.public_code(100) == '100' and customer_rules.public_code(1234) == '1234'


def test_client_cannot_choose_or_change_the_public_number(client):
    r = client.post('/api/customers', json={'name': 'Forçado', 'document': unique_cpf(), 'public_seq': 7})
    assert r.status_code == 200 and r.json()['public_seq'] != 7
    n = r.json()['public_seq']
    upd = client.put(f"/api/customers/{r.json()['id']}", json={'name': 'Forçado 2', 'public_seq': 1})
    assert upd.json()['public_seq'] == n


def test_legacy_customers_are_numbered_in_registration_order_preserving_existing(client):
    with SessionLocal() as db:
        old = [Customer(name=f'Legado {i}') for i in range(3)]
        db.add_all(old); db.commit()
        ids = [c.id for c in old]
        highest_before = max(c.public_seq or 0 for c in db.query(Customer).all())
        assert customer_rules.assign_missing_public_seq(db) >= 3
        seqs = [db.get(Customer, i).public_seq for i in ids]
        assert seqs == sorted(seqs) and len(set(seqs)) == 3 and min(seqs) > highest_before
        assert customer_rules.assign_missing_public_seq(db) == 0


def test_concurrent_registrations_never_duplicate_the_number(client):
    def create(i):
        r = client.post('/api/customers', json={'name': f'Concorrente {i}', 'document': unique_cpf()})
        return r.status_code, r.json().get('public_seq')
    with ThreadPoolExecutor(6) as pool:
        results = list(pool.map(create, range(12)))
    assert all(code == 200 for code, _ in results), results
    seqs = [s for _, s in results]
    assert len(set(seqs)) == 12
    with SessionLocal() as db:
        allseq = [c.public_seq for c in db.query(Customer).all() if c.public_seq]
        assert len(allseq) == len(set(allseq))
