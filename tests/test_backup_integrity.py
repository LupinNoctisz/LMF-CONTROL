"""12.A - Backup completo: banco + documentos + pastas + vínculos, com somas SHA-256 e restauração verificada."""
import hashlib
import json
import os
import shutil
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault('LMF_DATA_DIR', os.path.abspath('work/json-auth-test-data-6char'))
os.environ.setdefault('LMF_BACKUP_DIR', os.path.abspath('work/json-auth-test-backups-6char'))
os.environ.setdefault('LMF_IMPORT_BACKUP_DIR', os.path.abspath('work/json-auth-import-backups-6char'))

import app.main as main_module  # noqa: E402
from app.main import app  # noqa: E402
from app import authorization  # noqa: E402
from app.database import DATA_LOCK, DATA_DIR, SessionLocal  # noqa: E402
from app.models import ServiceOrderPhoto  # noqa: E402
from app.services import backup_service  # noqa: E402
from tests.test_validation_photos import PNG, make_order, upload, png, authenticate  # noqa: E402


@pytest.fixture
def client():
    with TestClient(app) as c:
        authenticate(c)
        yield c
        authorization.drain_pending(10)
        assert not DATA_LOCK.locked()
        with SessionLocal() as db:  # remove fotos órfãs deixadas de propósito por testes de backup incompleto
            for row in db.query(ServiceOrderPhoto).all():
                if not (main_module._photo_dir(row.service_order_id) / row.stored_name).is_file():
                    db.delete(row)
            db.commit()


def manifest_of(archive):
    with zipfile.ZipFile(archive) as z:
        return json.loads(z.read('manifest.json'))


def rewrite(archive, mutate, name):
    """Cria uma cópia adulterada do ZIP (mutate recebe dict nome->bytes)."""
    with zipfile.ZipFile(archive) as z:
        content = {i.filename: z.read(i.filename) for i in z.infolist()}
    mutate(content)
    out = archive.with_name(name)
    with zipfile.ZipFile(out, 'w') as z:
        for k, v in content.items():
            z.writestr(k, v)
    return out


@pytest.fixture(autouse=True)
def _clean_orphans_before(client):
    with SessionLocal() as db:
        for row in db.query(ServiceOrderPhoto).all():
            if not (main_module._photo_dir(row.service_order_id) / row.stored_name).is_file():
                db.delete(row)
        db.commit()


def photo_with_file(client):
    order = make_order(client)
    item = upload(client, order, [png('a.png')]).json()['items'][0]
    return order, item


def test_manifest_lists_hashes_documents_directories_and_links(client):
    order, item = photo_with_file(client)
    m = manifest_of(backup_service.create_backup('a1'))
    assert m['format_version'] == 2 and m['complete'] is True
    doc = next(d for d in m['documents'] if d['path'].startswith(f'ordens-servico/{order}/'))
    assert doc['sha256'] == hashlib.sha256(PNG).hexdigest() and doc['size'] == len(PNG)
    assert doc['referenced_by'][0]['table'] == 'service_order_photos' and doc['referenced_by'][0]['original_name'] == 'a.png'
    assert f'ordens-servico/{order}' in m['directories']
    assert any(r['record_id'] == item['id'] and r['present'] for r in m['references'])
    assert m['database_sha256'] and m['table_counts']['service_order_photos'] >= 1


def test_empty_directories_are_kept(client):
    empty = DATA_DIR / 'documentos' / 'pasta-vazia-teste'
    empty.mkdir(parents=True, exist_ok=True)
    try:
        archive = backup_service.create_backup('a2')
        with zipfile.ZipFile(archive) as z:
            assert 'documents/pasta-vazia-teste/' in z.namelist()
        shutil.rmtree(empty)
        backup_service.restore_backup(archive)
        assert empty.is_dir()
    finally:
        shutil.rmtree(empty, ignore_errors=True)


def test_missing_referenced_file_makes_backup_incomplete_and_not_silently_restorable(client):
    order, item = photo_with_file(client)
    for p in main_module._photo_dir(order).iterdir():
        p.unlink()
    archive = backup_service.create_backup('a3')
    m = manifest_of(archive)
    assert m['complete'] is False and any(r['record_id'] == item['id'] for r in m['missing_references'])
    assert backup_service.last_status()['status'] == 'incompleto'
    row = next(r for r in backup_service.list_backups() if r['name'] == archive.name)
    assert row['status'] == 'incompleto' and row['missing_references'] >= 1
    with pytest.raises(ValueError, match='INCOMPLETO'):
        backup_service.restore_backup(archive)
    res = client.post(f'/api/backups/{archive.name}/restore?confirm=true', headers=_auth(client, archive.name))
    assert res.status_code == 400 and 'INCOMPLETO' in res.text
    assert backup_service.restore_backup(archive, allow_incomplete=True)['missing_references'] >= 1


def _auth(client, target):
    r = client.post('/api/auth/authorize-operation', json={'operation': 'backup.restaurar', 'target': target, 'password': 'Abc123'})
    assert r.status_code == 200, r.text
    return {authorization.AUTH_HEADER: r.json()['token']}


def test_restore_recovers_records_files_and_links(client):
    order, item = photo_with_file(client)
    archive = backup_service.create_backup('a4')
    for p in main_module._photo_dir(order).iterdir():
        p.unlink()
    assert client.get(item['url']).status_code == 404
    result = backup_service.restore_backup(archive)
    assert result['documents']['created'] >= 1 and result['missing_references'] == 0
    got = client.get(item['url'])
    assert got.status_code == 200 and got.content == PNG
    with SessionLocal() as db:
        row = db.get(ServiceOrderPhoto, item['id'])
        assert row.service_order_id == order and row.original_name == 'a.png'


def test_restore_keeps_a_different_existing_file_instead_of_overwriting(client):
    order, item = photo_with_file(client)
    archive = backup_service.create_backup('a5')
    path = next(main_module._photo_dir(order).iterdir())
    path.write_bytes(b'conteudo alterado depois do backup')
    backup_service.restore_backup(archive)
    assert path.read_bytes() == PNG
    kept = [p for p in path.parent.iterdir() if '.antes-restauracao-' in p.name]
    assert len(kept) == 1 and kept[0].read_bytes() == b'conteudo alterado depois do backup'


def test_tampered_document_is_rejected_at_validation_and_restore_changes_nothing(client):
    order, item = photo_with_file(client)
    archive = backup_service.create_backup('a6')
    key = next(n for n in zipfile.ZipFile(archive).namelist() if n.startswith(f'documents/ordens-servico/{order}/') and not n.endswith('/'))
    bad = rewrite(archive, lambda c: c.__setitem__(key, b'adulterado'), 'adulterado-doc.zip')
    info = backup_service.inspect_archive(bad)
    assert info['integrity'] == 'inválido' and 'soma de verificação' in info['error']
    before = (DATA_DIR / 'documentos').exists()
    with pytest.raises(ValueError):
        backup_service.restore_backup(bad)
    assert client.get(item['url']).content == PNG and before


def test_tampered_database_is_rejected(client):
    archive = backup_service.create_backup('a7')
    def mutate(c):
        doc = json.loads(c['lmf_control.json']); doc['tables']['customers']['records'].append({'id': 999999, 'name': 'x'}); doc['tables']['customers']['row_count'] += 1
        c['lmf_control.json'] = json.dumps(doc).encode()
    bad = rewrite(archive, mutate, 'adulterado-db.zip')
    assert backup_service.inspect_archive(bad)['integrity'] == 'inválido'


def test_extra_or_missing_documents_are_rejected(client):
    order, _ = photo_with_file(client)
    archive = backup_service.create_backup('a8')
    extra = rewrite(archive, lambda c: c.__setitem__('documents/intruso.txt', b'x'), 'extra.zip')
    assert backup_service.inspect_archive(extra)['integrity'] == 'inválido'
    key = next(n for n in zipfile.ZipFile(archive).namelist() if n.startswith(f'documents/ordens-servico/{order}/') and not n.endswith('/'))
    gone = rewrite(archive, lambda c: c.pop(key), 'faltando.zip')
    assert backup_service.inspect_archive(gone)['integrity'] == 'inválido'


def test_failed_restore_rolls_back_database_and_files(client, monkeypatch):
    order, item = photo_with_file(client)
    archive = backup_service.create_backup('a9')
    path = next(main_module._photo_dir(order).iterdir())
    path.write_bytes(b'versao atual')
    client.post('/api/customers', json={'name': 'Criado depois do backup'})
    original_reload = backup_service.reload_json_database
    calls = {'n': 0}
    def flaky(*a, **k):
        calls['n'] += 1
        if calls['n'] == 1:
            raise RuntimeError('falha simulada')
        return original_reload(*a, **k)
    monkeypatch.setattr(backup_service, 'reload_json_database', flaky)
    with pytest.raises(RuntimeError):
        backup_service.restore_backup(archive)
    assert path.read_bytes() == b'versao atual'
    assert not [p for p in path.parent.iterdir() if '.antes-restauracao-' in p.name]
    assert any(c['name'] == 'Criado depois do backup' for c in client.get('/api/customers').json())


def test_legacy_v1_backup_still_restores(client):
    order, item = photo_with_file(client)
    archive = backup_service.create_backup('a10')
    def downgrade(c):
        m = json.loads(c['manifest.json'])
        c['manifest.json'] = json.dumps({k: m[k] for k in ('format', 'created_at', 'schema_revision', 'database')} | {'format_version': 1}).encode()
    old = rewrite(archive, downgrade, 'antigo-v1.zip')
    info = backup_service.inspect_archive(old)
    assert info['integrity'] == 'íntegro' and 'formato antigo' in info['status']
    for p in main_module._photo_dir(order).iterdir():
        p.unlink()
    backup_service.restore_backup(old)
    assert client.get(item['url']).content == PNG


def test_no_staging_leftovers_after_restore(client):
    photo_with_file(client)
    backup_service.restore_backup(backup_service.create_backup('a11'))
    assert not list(DATA_DIR.glob('.restore-staging-*'))
