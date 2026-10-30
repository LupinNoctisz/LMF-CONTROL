"""Telefone/WhatsApp, CPF e fotos das ordens de serviço."""
import base64
import os
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault('LMF_DATA_DIR', os.path.abspath('work/json-auth-test-data-6char'))
os.environ.setdefault('LMF_BACKUP_DIR', os.path.abspath('work/json-auth-test-backups-6char'))
os.environ.setdefault('LMF_IMPORT_BACKUP_DIR', os.path.abspath('work/json-auth-import-backups-6char'))

import app.main as main_module  # noqa: E402
from app.main import app  # noqa: E402
from app.database import DATA_LOCK, SessionLocal  # noqa: E402
from app.models import Customer, ServiceOrderPhoto  # noqa: E402
from app.services import backup_service  # noqa: E402
from app.validation import normalize_cpf, normalize_phone  # noqa: E402

USER = {'username': 'testadmin', 'password': 'Abc123'}
PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==')
JPEG = b'\xff\xd8\xff\xe0' + b'\x00' * 32
WEBP = b'RIFF\x24\x00\x00\x00WEBPVP8 ' + b'\x00' * 24


def authenticate(client):
    status = client.get('/api/auth/status').json()
    response = client.post('/api/auth/setup' if status['setup_required'] else '/api/auth/login', json=USER)
    assert response.status_code == 200, response.text
    client.headers.update({'X-CSRF-Token': response.json()['csrf_token']})


@pytest.fixture
def client():
    with TestClient(app) as c:
        authenticate(c)
        yield c
        assert not DATA_LOCK.locked()


def make_order(client):
    customer = client.post('/api/customers', json={'name': 'Cliente das fotos'}).json()
    order = client.post('/api/service-orders', json={'customer_id': customer['id'], 'equipment': 'Notebook', 'reported_fault': 'Não liga'})
    assert order.status_code == 200, order.text
    return order.json()['id']


def upload(client, order_id, files):
    return client.post(f'/api/service-orders/{order_id}/photos', files=[('files', f) for f in files])


def png(name='foto.png'):
    return (name, PNG, 'image/png')


# ---- funções de validação -------------------------------------------------------------------

@pytest.mark.parametrize('raw,expected', [
    ('92998158767', '(92) 9 9815-8767'),
    ('(92) 9 9815-8767', '(92) 9 9815-8767'),
    ('92 99815-8767', '(92) 9 9815-8767'),
    ('9232345678', '(92) 3234-5678'),
    ('9299815876', '(92) 9981-5876'),  # 10 dígitos = DDD + 8 (fixo)
    ('', None), (None, None), ('   ', None),
])
def test_phone_is_normalized(raw, expected):
    assert normalize_phone(raw) == expected


@pytest.mark.parametrize('raw', ['929981587678', '92 9981-58A7', 'abc', '(92) 8 9815-8767', '0299815-8767', '+55 92 99815-8767'])
def test_phone_rejects_invalid(raw):
    with pytest.raises(ValueError):
        normalize_phone(raw)


@pytest.mark.parametrize('raw,expected', [('99988877754', '999.888.777-54'), ('999.888.777-54', '999.888.777-54'), ('', None), (None, None)])
def test_cpf_is_normalized(raw, expected):
    assert normalize_cpf(raw) == expected


@pytest.mark.parametrize('raw', ['9998887775', '999888777544', '999.888.777-5X', 'abcdefghijk', '12345678000195'])
def test_cpf_rejects_invalid(raw):
    with pytest.raises(ValueError):
        normalize_cpf(raw)


# ---- API: telefone e CPF ---------------------------------------------------------------------

def test_customer_phone_whatsapp_and_cpf_are_saved_formatted(client):
    response = client.post('/api/customers', json={'name': 'Maria', 'phone': '92998158767', 'whatsapp': '(92) 9 9815-8767', 'document': '99988877754'})
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body['phone'], body['whatsapp'], body['document']) == ('(92) 9 9815-8767', '(92) 9 9815-8767', '999.888.777-54')


@pytest.mark.parametrize('payload', [
    {'phone': '92 9981-58A7'}, {'whatsapp': '929981587678'}, {'document': '9998887775'}, {'document': '999.888.777-5X'},
])
def test_customer_invalid_values_are_blocked_by_the_server(client, payload):
    response = client.post('/api/customers', json={'name': 'Inválido', **payload})
    assert response.status_code == 422, response.text
    assert 'inválido' in response.text


def test_supplier_phone_is_validated_and_saved_formatted(client):
    assert client.post('/api/suppliers', json={'name': 'F', 'whatsapp': '92x99815876'}).status_code == 422
    ok = client.post('/api/suppliers', json={'name': 'Fornecedor ok', 'phone': '9232345678', 'whatsapp': '92998158767'})
    assert ok.status_code == 200 and ok.json()['phone'] == '(92) 3234-5678' and ok.json()['whatsapp'] == '(92) 9 9815-8767'


def test_update_is_validated_too(client):
    created = client.post('/api/customers', json={'name': 'Edita'}).json()
    bad = client.put(f"/api/customers/{created['id']}", json={'name': 'Edita', 'phone': 'telefone'})
    assert bad.status_code == 422
    good = client.put(f"/api/customers/{created['id']}", json={'name': 'Edita', 'phone': '92998158767'})
    assert good.status_code == 200 and good.json()['phone'] == '(92) 9 9815-8767'


def test_legacy_values_outside_the_pattern_do_not_break_listing(client):
    """Registros antigos com telefone/CPF fora do padrão precisam continuar aparecendo."""
    with SessionLocal() as db:
        db.add(Customer(name='Antigo', phone='tel: 3234-5678 ramal 2', whatsapp='+55 92 99815-8767', document='12.345.678/0001-95'))
        db.commit()
    response = client.get('/api/customers')
    assert response.status_code == 200
    assert any(row['name'] == 'Antigo' and row['document'] == '12.345.678/0001-95' for row in response.json())
    assert client.get('/api/suppliers').status_code == 200


# ---- API: fotos ------------------------------------------------------------------------------

def test_upload_list_download_and_delete_photo(client):
    order_id = make_order(client)
    response = upload(client, order_id, [png('frente.png')])
    assert response.status_code == 200, response.text
    item = response.json()['items'][0]
    assert item['original_name'] == 'frente.png' and item['content_type'] == 'image/png'
    listing = client.get(f'/api/service-orders/{order_id}/photos').json()
    assert listing['max'] == 6 and len(listing['items']) == 1
    served = client.get(item['url'])
    assert served.status_code == 200 and served.content == PNG
    assert served.headers['content-type'] == 'image/png' and served.headers['x-content-type-options'] == 'nosniff'
    stored = next((main_module._photo_dir(order_id)).iterdir())
    assert stored.is_file()
    assert client.delete(f"/api/service-orders/{order_id}/photos/{item['id']}").status_code == 200
    assert not stored.exists()
    assert client.get(item['url']).status_code == 404


def test_photo_limit_is_six_per_order(client):
    order_id = make_order(client)
    assert upload(client, order_id, [png(f'f{i}.png') for i in range(6)]).status_code == 200
    seventh = upload(client, order_id, [png('extra.png')])
    assert seventh.status_code == 400 and 'Limite de 6' in seventh.json()['detail']
    assert len(client.get(f'/api/service-orders/{order_id}/photos').json()['items']) == 6
    assert len(list(main_module._photo_dir(order_id).iterdir())) == 6


def test_batch_over_the_limit_saves_nothing(client):
    order_id = make_order(client)
    assert upload(client, order_id, [png('a.png'), png('b.png')]).status_code == 200
    response = upload(client, order_id, [png(f'x{i}.png') for i in range(5)])  # 2 + 5 = 7
    assert response.status_code == 400
    assert len(client.get(f'/api/service-orders/{order_id}/photos').json()['items']) == 2
    assert len(list(main_module._photo_dir(order_id).iterdir())) == 2


def test_accepts_jpeg_png_and_webp_by_content(client):
    order_id = make_order(client)
    response = upload(client, order_id, [('a.jpg', JPEG, 'image/jpeg'), ('b.png', PNG, 'image/png'), ('c.webp', WEBP, 'image/webp')])
    assert response.status_code == 200, response.text
    assert [i['content_type'] for i in response.json()['items']] == ['image/jpeg', 'image/png', 'image/webp']


@pytest.mark.parametrize('name,data,mime', [
    ('script.jpg', b'<script>alert(1)</script>', 'image/jpeg'),
    ('doc.png', b'%PDF-1.4 conteudo', 'image/png'),
    ('anim.gif', b'GIF89a' + b'\x00' * 20, 'image/gif'),
    ('vazio.png', b'', 'image/png'),
    ('x.svg', b'<svg xmlns="http://www.w3.org/2000/svg"/>', 'image/svg+xml'),
])
def test_non_images_and_empty_files_are_rejected(client, name, data, mime):
    order_id = make_order(client)
    response = upload(client, order_id, [png('boa.png'), (name, data, mime)])
    assert response.status_code == 400, response.text
    assert client.get(f'/api/service-orders/{order_id}/photos').json()['items'] == []  # nem a boa foi gravada


def test_oversized_photo_is_rejected(client, monkeypatch):
    monkeypatch.setattr(main_module, 'MAX_PHOTO_BYTES', 64)
    order_id = make_order(client)
    response = upload(client, order_id, [('grande.png', PNG + b'\x00' * 100, 'image/png')])
    assert response.status_code == 400 and 'passa de' in response.json()['detail']


def test_unknown_order_and_mismatched_photo_ids(client):
    assert client.get('/api/service-orders/999999/photos').status_code == 404
    assert upload(client, 999999, [png()]).status_code == 404
    first, second = make_order(client), make_order(client)
    item = upload(client, first, [png()]).json()['items'][0]
    assert client.get(f"/api/service-orders/{second}/photos/{item['id']}/file").status_code == 404
    assert client.delete(f"/api/service-orders/{second}/photos/{item['id']}").status_code == 404
    assert client.get(item['url']).status_code == 200


def test_photo_requires_login():
    with TestClient(app) as anonymous:
        assert anonymous.get('/api/service-orders/1/photos/1/file').status_code == 401


def test_filename_is_sanitized(client):
    order_id = make_order(client)
    item = upload(client, order_id, [('../../etc/passwd.png', PNG, 'image/png')]).json()['items'][0]
    assert item['original_name'] == 'passwd.png'
    stored = [p.name for p in main_module._photo_dir(order_id).iterdir()]
    assert len(stored) == 1 and '/' not in stored[0] and '..' not in stored[0]


def test_deleting_the_order_removes_its_photos(client):
    order_id = make_order(client)
    upload(client, order_id, [png('a.png'), png('b.png')])
    folder = main_module._photo_dir(order_id)
    assert len(list(folder.iterdir())) == 2
    assert client.delete(f'/api/service-orders/{order_id}?confirm=true').status_code == 200
    assert not folder.exists()
    with SessionLocal() as db:
        assert db.query(ServiceOrderPhoto).filter_by(service_order_id=order_id).count() == 0


def test_photos_are_included_in_backups_and_restored(client):
    order_id = make_order(client)
    item = upload(client, order_id, [png('backup.png')]).json()['items'][0]
    archive = backup_service.create_backup('teste-fotos')
    with zipfile.ZipFile(archive) as z:
        names = [n for n in z.namelist() if n.startswith(f'documents/ordens-servico/{order_id}/')]
    assert len(names) == 1
    # simula perda do arquivo; a restauração devolve o arquivo e as linhas do banco
    for path in main_module._photo_dir(order_id).iterdir():
        path.unlink()
    assert client.get(item['url']).status_code == 404
    backup_service.restore_backup(archive)
    restored = client.get(item['url'])
    assert restored.status_code == 200 and restored.content == PNG
