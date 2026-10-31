import os
from uuid import uuid4
from io import BytesIO
from openpyxl import Workbook
from fastapi.testclient import TestClient
import shutil
import sqlite3
import zipfile
from datetime import date
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

os.environ['LMF_DATA_DIR']=os.path.abspath('work/json-auth-test-data-6char')
os.environ['LMF_BACKUP_DIR']=os.path.abspath('work/json-auth-test-backups-6char')
os.environ['LMF_IMPORT_BACKUP_DIR']=os.path.abspath('work/json-auth-import-backups-6char')
from app.main import app
from app.database import IMPORT_BACKUP_DIR, DB_PATH, BACKUP_DIR, SessionLocal, engine, reload_json_database, LEGACY_SQLITE_PATH, get_db, DATA_LOCK
from app.models import BankTransaction, AccountBalanceAdjustment
from app.services.backup_service import inspect_archive

TEST_USER={'username':'testadmin','password':'Abc123'}

def test_database_dependency_can_release_lock_from_another_worker_thread():
    dependency=get_db()
    next(dependency)
    with ThreadPoolExecutor(max_workers=1) as executor:
        executor.submit(dependency.close).result(timeout=2)
    assert not DATA_LOCK.locked()

def authenticate(client):
    status=client.get('/api/auth/status').json()
    route='/api/auth/setup' if status['setup_required'] else '/api/auth/login'
    response=client.post(route,json=TEST_USER)
    assert response.status_code==200,response.text
    client.headers.update({'X-CSRF-Token':response.json()['csrf_token']})

def authorize(client,operation,target):
    """Passo de senha do administrador: devolve o cabeçalho de autorização de uso único."""
    response=client.post('/api/auth/authorize-operation',json={'operation':operation,'target':str(target),'password':TEST_USER['password']})
    assert response.status_code==200,response.text
    return {'X-Operation-Authorization':response.json()['token']}

def test_authentication_blocks_api_until_first_admin_login():
    with TestClient(app) as c:
        for route in ['/api/accounts','/api/finance','/api/backups']:
            assert c.get(route).status_code==401
        assert c.post('/api/backups').status_code==401
        assert c.post('/api/backups/example.zip/restore?confirm=true').status_code==401
        assert c.delete('/api/backups/example.zip?confirm=true').status_code==401
        assert c.post('/api/accounts/1/remove-opening-balance').status_code==401
        status=c.get('/api/auth/status').json()
        response=c.post('/api/auth/setup' if status['setup_required'] else '/api/auth/login',json=TEST_USER)
        assert response.status_code==200
        assert c.get('/api/accounts').status_code==200
        assert c.post('/api/backups').status_code==403
        csrf=response.json()['csrf_token']
        assert c.post('/api/backups',headers={'X-CSRF-Token':csrf}).status_code==200

def test_password_must_be_between_six_and_twelve_characters():
    with TestClient(app) as c:
        assert c.post('/api/auth/login',json={'username':'testadmin','password':'12345'}).status_code==422
        assert c.post('/api/auth/login',json={'username':'testadmin','password':'1234567890123'}).status_code==422

def test_remember_me_extends_login_session_to_thirty_days():
    with TestClient(app) as c:
        authenticate(c)
        remember=c.post('/api/auth/login',json={**TEST_USER,'remember_me':True})
        assert remember.status_code==200 and remember.json()['remember_me'] is True
        assert 'Max-Age=2592000' in remember.headers['set-cookie']
        regular=c.post('/api/auth/login',json={**TEST_USER,'remember_me':False})
        assert regular.status_code==200 and regular.json()['remember_me'] is False
        assert 'Max-Age=43200' in regular.headers['set-cookie']

def test_login_name_uses_lowercase_letter_and_digits_only():
    with TestClient(app) as c:
        for username in ('123pedro','pedro_123','pedro..123'):
            response=c.post('/api/auth/login',json={'username':username,'password':'Abc123'})
            assert response.status_code==422
            assert 'O login deve começar com uma letra' in response.json()['detail'][0]['msg']
        # These names pass format validation; authentication fails only because they are not registered.
        for username in ('pedro123','Lmf.informatica',' Lmf.informatica '):
            assert c.post('/api/auth/login',json={'username':username,'password':'Abc123'}).status_code==401

def test_forgot_password_requires_code_and_rotates_recovery_code():
    from app.security import RECOVERY_CODE_PATH
    with TestClient(app) as c:
        old_code=RECOVERY_CODE_PATH.read_text(encoding='utf-8').strip()
        payload={'username':TEST_USER['username'],'recovery_code':'wrong-recovery-code','new_password':'New!42'}
        assert c.post('/api/auth/forgot-password',json=payload).status_code==400
        payload['recovery_code']=old_code
        reset=c.post('/api/auth/forgot-password',json=payload)
        assert reset.status_code==200,reset.text
        new_code=RECOVERY_CODE_PATH.read_text(encoding='utf-8').strip()
        assert new_code and new_code!=old_code
        assert c.post('/api/auth/login',json={'username':TEST_USER['username'],'password':'New!42'}).status_code==200
        payload.update(recovery_code=new_code,new_password=TEST_USER['password'])
        assert c.post('/api/auth/forgot-password',json=payload).status_code==200

def test_json_persistence_falls_back_when_windows_denies_replace(monkeypatch,tmp_path):
    import app.database as database_module
    import json
    target=tmp_path/'lmf_control.json'
    monkeypatch.setattr(database_module,'JSON_DB_PATH',target)
    original_replace=os.replace
    def deny_replacing_target(source,destination):
        if Path(destination)==target:
            raise PermissionError('simulated Windows ACL denies replace')
        return original_replace(source,destination)
    monkeypatch.setattr(database_module.os,'replace',deny_replacing_target)
    with TestClient(app):
        database_module._write_json_snapshot()
    saved=json.loads(target.read_text(encoding='utf-8'))
    assert saved['format']=='lmf-control-json-database'
    assert saved['tables']['bank_accounts']['row_count']>=1

def test_dashboard_and_products():
    with TestClient(app) as c:
        authenticate(c)
        sku='T-'+uuid4().hex
        r=c.post('/api/products',json={'name':'Fonte teste','sku':sku,'quantity':3,'min_quantity':1,'cost':10,'price':20})
        assert r.status_code==200
        pid=r.json()['id']
        key='manual-'+uuid4().hex
        first=c.post(f'/api/products/{pid}/movement?kind=saida&quantity=1&reason=Venda',headers={'Idempotency-Key':key})
        assert first.status_code==200
        repeat=c.post(f'/api/products/{pid}/movement?kind=saida&quantity=1&reason=Venda',headers={'Idempotency-Key':key})
        assert repeat.status_code==200 and repeat.json()['duplicate_request'] is True and repeat.json()['quantity']==2
        assert c.post(f'/api/products/{pid}/movement?kind=saida&quantity=9&reason=Erro').status_code==409
        assert c.get('/api/dashboard').json()['products']>=1

def test_customer_service_and_backup():
    with TestClient(app) as c:
        authenticate(c)
        assert BACKUP_DIR.is_dir()
        customer=c.post('/api/customers',json={'name':'Cliente teste'}).json()
        so=c.post('/api/service-orders',json={'customer_id':customer['id'],'equipment':'Notebook','equipment_type':'Notebook','serial':'012345678901234','reported_fault':'Não liga'})
        assert so.status_code==200
        manual=c.post('/api/backups')
        assert manual.status_code==200
        from app.services.backup_service import create_backup
        automatic=create_backup('automatico')
        assert (BACKUP_DIR/manual.json()['file']).parent.resolve()==BACKUP_DIR.resolve()
        assert automatic.parent.resolve()==BACKUP_DIR.resolve()
        assert manual.json()['file'] != automatic.name
        listed={item['name'] for item in c.get('/api/backups').json()['items']}
        assert manual.json()['file'] in listed and automatic.name in listed
        assert c.delete(f"/api/backups/{manual.json()['file']}").status_code==400
        assert c.delete(f"/api/backups/{manual.json()['file']}?confirm=true").status_code==200
        assert (BACKUP_DIR/automatic.name).is_file() and not (BACKUP_DIR/manual.json()['file']).exists()

def test_supplier_and_service_order_search_cover_operational_fields():
    marker=uuid4().hex[:10]
    with TestClient(app) as c:
        authenticate(c)
        supplier=c.post('/api/suppliers',json={'name':f'Fornecedor {marker}','email':f'contato-{marker}@example.test','phone':'92999990000'}).json()
        assert supplier.get('id')
        assert any(row['id']==supplier['id'] for row in c.get(f'/api/suppliers?q=contato-{marker}').json())

        customer=c.post('/api/customers',json={'name':f'Cliente {marker}'}).json()
        service=c.post('/api/service-orders',json={'customer_id':customer['id'],'equipment':'Notebook','equipment_type':'Notebook','serial':'012345678901234','reported_fault':f'Falha placa {marker}'}).json()
        assert service.get('id')
        assert any(row['id']==service['id'] for row in c.get(f'/api/service-orders?q=Falha%20placa%20{marker}').json())
        assert any(row['id']==service['id'] for row in c.get(f'/api/service-orders?q=Cliente%20{marker}').json())

def test_navigation_and_independent_search_controls_are_present():
    script=(Path(__file__).resolve().parents[1]/'app'/'static'/'app.js').read_text(encoding='utf-8')
    assert "const modules={Início:null" in script
    assert "Clientes:'customers'" not in script
    assert 'function normalizeSearch(value)' in script
    assert 'function clearModuleSearch()' in script
    assert 'oninput="filterRows()"' in script
    assert 'oninput="filterReportSections()"' in script

def test_excel_import_guarantee_finance_and_restore():
    with TestClient(app) as c:
        authenticate(c)
        sku='X-'+uuid4().hex
        wb=Workbook(); ws=wb.active; ws.append(['Código/SKU','Produto']); ws.append([sku,'Memória teste']); ws.append([sku,'Memória repetida'])
        buf=BytesIO(); wb.save(buf); buf.seek(0)
        preview=c.post('/api/import/preview',files={'file':('estoque.xlsx',buf.getvalue())})
        assert preview.status_code==200 and preview.json()['suggested_mapping']=={'name':'Produto','sku':'Código/SKU'}
        buf.seek(0); imported=c.post('/api/import/products',params={'mapping':'{"sku":"Código/SKU","name":"Produto"}','key':'sku'},files={'file':('estoque.xlsx',buf.getvalue())})
        assert imported.status_code==200 and imported.json()['created']==1 and len(imported.json()['duplicates'])==1
        customer=c.post('/api/customers',json={'name':'Garantia teste'}).json()
        g=c.post('/api/guarantees',json={'customer_id':customer['id'],'item':'Memória','starts_on':'2026-01-01','ends_on':'2027-01-01'})
        assert g.status_code==200
        fin=c.post('/api/finance',json={'kind':'receita','category':'serviço','description':'OS teste','amount':50,'reference':'OS-'+uuid4().hex})
        assert fin.status_code==200
        backup=c.post('/api/backups').json()['file']
        assert c.post(f'/api/backups/{backup}/restore?confirm=true',headers=authorize(c,'backup.restaurar',backup)).status_code==200

def test_real_stock_workbook_layout():
    filename='LMF_Controle_Produtos_Caixa (1).xlsx'
    source=os.path.abspath(os.path.join('data','planilhas',filename))
    if not os.path.exists(source): source=os.path.join(os.environ.get('LOCALAPPDATA',''), 'LMF-Control','data','planilhas',filename)
    with open(source,'rb') as f, TestClient(app) as c:
        authenticate(c)
        response=c.post('/api/import/preview',files={'file':('estoque.xlsx',f.read())})
    body=response.json()
    assert response.status_code==200
    assert body['sheet']=='Estoque e Caixa' and body['header_row']==6
    assert body['suggested_mapping']=={'name':'Produto','sku':'Código/SKU','category':'Categoria','supplier':'Fornecedor','quantity':'Qtd. estoque','cost':'Custo unit. (R$)','price':'Preço venda unit. (R$)'}
    assert body['rows']==0

def test_sale_stock_finance_report_and_imported_backup():
    with TestClient(app) as c:
        authenticate(c)
        product=c.post('/api/products',json={'name':'Produto venda teste','sku':'SALE-'+uuid4().hex,'quantity':4,'cost':10,'price':20}).json()
        account=c.post('/api/accounts',json={'name':'Conta teste','business_unit':'Assistência','provider':'InfinitePay','opening_balance':100}).json()
        sale_payload={'items':[{'product_id':product['id'],'quantity':2,'unit_price':20}],'received_amount':25,'bank_account_id':account['id'],'payment_method':'PIX'}
        sale=c.post('/api/sales',headers={'Idempotency-Key':'sale-test-'+uuid4().hex},json=sale_payload)
        assert sale.status_code==200
        assert sale.json()['total']==40
        repeated=c.post('/api/sales',headers={'Idempotency-Key':sale.request.headers['Idempotency-Key']},json=sale_payload)
        assert repeated.status_code==200 and repeated.json()['duplicate_request'] is True
        assert c.get('/api/products').json()[0]['quantity']==2
        finance=c.get('/api/finance').json()
        assert {x['amount'] for x in finance[:2]}=={25,15}
        report=c.get('/api/reports/summary').json()
        assert report['sales_count']>=1 and report['received']>=25 and report['pending']>=15
        failed=c.post('/api/sales',json={'items':[{'product_id':product['id'],'quantity':3,'unit_price':20}],'received_amount':0})
        assert failed.status_code==409
        assert c.get('/api/products').json()[0]['quantity']==2
        canceled=c.post(f"/api/sales/{sale.json()['id']}/cancel",headers=authorize(c,'financeiro.venda.cancelar',sale.json()['id']))
        assert canceled.status_code==200 and canceled.json()['refunded']==25
        assert c.get('/api/products').json()[0]['quantity']==2  # cancelar NÃO devolve estoque sem retorno físico
        assert c.post(f"/api/sales/{sale.json()['id']}/cancel",headers=authorize(c,'financeiro.venda.cancelar',sale.json()['id'])).json()['duplicate_request'] is True
        backup=c.post('/api/backups').json()['file']
        IMPORT_BACKUP_DIR.mkdir(parents=True,exist_ok=True)
        imported_name='imported-'+backup
        shutil.copy2(BACKUP_DIR/backup,IMPORT_BACKUP_DIR/imported_name)
        assert c.post(f'/api/backups/import/{imported_name}').status_code==200
        assert c.post(f'/api/backups/{imported_name}/restore?confirm=true',headers=authorize(c,'backup.restaurar',imported_name)).status_code==200

def test_purchase_receipt_is_idempotent_and_updates_stock_only_on_receipt():
    with TestClient(app) as c:
        authenticate(c)
        supplier=c.post('/api/suppliers',json={'name':'Fornecedor teste'}).json()
        product=c.post('/api/products',json={'name':'Produto compra teste','sku':'PUR-'+uuid4().hex,'quantity':1,'cost':5}).json()
        order=c.post('/api/orders',json={'kind':'compra','supplier_id':supplier['id'],'description':'Compra de produto','quantity':5,'total':25}).json()
        assert order['number'].startswith('LMF-PED-')
        key='receipt-'+uuid4().hex
        payload={'product_id':product['id'],'quantity':3,'unit_cost':5}
        first=c.post(f"/api/orders/{order['id']}/receive",headers={'Idempotency-Key':key},json=payload)
        assert first.status_code==200 and first.json()['product_quantity']==4 and first.json()['order_complete'] is False
        repeated=c.post(f"/api/orders/{order['id']}/receive",headers={'Idempotency-Key':key},json=payload)
        assert repeated.status_code==200 and repeated.json()['duplicate_request'] is True
        final=c.post(f"/api/orders/{order['id']}/receive",headers={'Idempotency-Key':'receipt-final-'+uuid4().hex},json={'product_id':product['id'],'quantity':2,'unit_cost':5})
        assert final.status_code==200 and final.json()['product_quantity']==6 and final.json()['order_complete'] is True

def test_legacy_backup_is_migrated_before_restore():
    with TestClient(app) as c:
        authenticate(c)
        legacy_db=os.path.abspath('work/legacy-test.db')
        source=engine.raw_connection(); target=sqlite3.connect(legacy_db)
        try:
            source.backup(target)
            target.execute('CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)')
            target.execute("INSERT INTO alembic_version VALUES ('20261028_0005')")
            target.commit()
        finally: target.close(); source.close()
        connection=sqlite3.connect(legacy_db)
        connection.execute('DROP TABLE alembic_version')
        connection.commit(); connection.close()
        legacy_zip=os.path.abspath('work/json-auth-test-backups-6char/legacy-test.zip')
        with zipfile.ZipFile(legacy_zip,'w',zipfile.ZIP_DEFLATED) as z: z.write(legacy_db,'lmf.db')
        assert inspect_archive(__import__('pathlib').Path(legacy_zip))['integrity']=='íntegro'
        restored=c.post('/api/backups/legacy-test.zip/restore?confirm=true',headers=authorize(c,'backup.restaurar','legacy-test.zip'))
        assert restored.status_code==200,restored.text

def test_corrupt_backup_cannot_replace_live_database():
    with TestClient(app) as c:
        authenticate(c)
        before=c.get('/api/products').json()
        bad=BACKUP_DIR/'corrupt-test.zip'
        bad.write_bytes(b'not a valid zip')
        response=c.post('/api/backups/corrupt-test.zip/restore?confirm=true',headers=authorize(c,'backup.restaurar','corrupt-test.zip'))
        assert response.status_code==400
        assert c.get('/api/products').json()==before
        import json
        current=json.loads(DB_PATH.read_text(encoding='utf-8'))
        assert current['format']=='lmf-control-json-database'
        assert isinstance(current['tables']['products']['records'],list)

def test_remove_opening_balance_preserves_financial_and_bank_movements():
    with TestClient(app) as c:
        authenticate(c)
        account=c.post('/api/accounts',json={'name':'Conta saldo inicial '+uuid4().hex[:6],'opening_balance':500,'opening_balance_date':'2026-01-15'}).json()
        entry=c.post('/api/finance',json={'kind':'receita','category':'venda','description':'Recebimento real','amount':100,'status':'recebido','paid_on':'2026-02-01','bank_account_id':account['id']})
        assert entry.status_code==200
        with SessionLocal() as db:
            db.add(BankTransaction(bank_account_id=account['id'],external_id='bank-'+uuid4().hex,occurred_on=date(2026,2,1),description='Extrato real',kind='receita',amount=100,reconciled=True))
            db.commit()
        csrf=c.get('/api/auth/status').json()['csrf_token']
        url=f"/api/accounts/{account['id']}/remove-opening-balance"
        payload={'account_name_confirmation':account['name'],'confirm_linked_transactions':False}
        blocked=c.post(url,headers={'X-CSRF-Token':csrf},json=payload)
        assert blocked.status_code==409
        payload['confirm_linked_transactions']=True
        removed=c.post(url,headers={'X-CSRF-Token':csrf,**authorize(c,'financeiro.conta.saldo_inicial.remover',account['id'])},json=payload)
        assert removed.status_code==200 and removed.json()['balance']==100
        updated=next(a for a in c.get('/api/accounts').json() if a['id']==account['id'])
        assert updated['opening_balance']==0 and updated['opening_balance_date'] is None and updated['balance']==100
        assert c.get('/api/finance').json()[0]['amount']==100
        with SessionLocal() as db:
            assert db.query(BankTransaction).filter_by(bank_account_id=account['id']).count()==1
            audit=db.query(AccountBalanceAdjustment).filter_by(bank_account_id=account['id']).one()
            assert audit.old_balance==500 and audit.new_balance==0 and audit.old_reference_date==date(2026,1,15)

def test_remove_initial_balance_without_movements_and_no_balance():
    with TestClient(app) as c:
        authenticate(c)
        account=c.post('/api/accounts',json={'name':'Conta sem movimentos '+uuid4().hex[:6],'opening_balance':75}).json()
        csrf=c.get('/api/auth/status').json()['csrf_token']
        response=c.post(f"/api/accounts/{account['id']}/remove-opening-balance",headers={'X-CSRF-Token':csrf,**authorize(c,'financeiro.conta.saldo_inicial.remover',account['id'])},json={'account_name_confirmation':account['name']})
        assert response.status_code==200 and response.json()['balance']==0
        assert c.post(f"/api/accounts/{account['id']}/remove-opening-balance",headers={'X-CSRF-Token':csrf},json={'account_name_confirmation':account['name']}).json()['status']=='sem saldo inicial'

def test_open_backup_folder_recreates_central_directory(monkeypatch,tmp_path):
    import os
    import app.main as main_module
    target=tmp_path/'central-backups'
    monkeypatch.setattr(main_module,'BACKUP_DIR',target)
    opened=[]
    monkeypatch.setattr(os,'startfile',lambda path:opened.append(path),raising=False)
    with TestClient(app) as c:
        authenticate(c)
        csrf=c.get('/api/auth/status').json()['csrf_token']
        assert not target.exists()
        result=c.post('/api/backups/open-folder/backups',headers={'X-CSRF-Token':csrf})
        assert result.status_code==200 and target.is_dir()
        assert Path(opened[0])==target.resolve() and Path(result.json()['path'])==target.resolve()

def test_backup_directory_falls_back_when_documents_is_unavailable(monkeypatch,tmp_path):
    import app.database as database_module
    primary=tmp_path/'documents'/'LMF-Control'/'Backups'
    fallback=tmp_path/'local'/'Backups'
    original=Path.mkdir
    def mkdir_with_unavailable_documents(path,*args,**kwargs):
        if path==primary: raise OSError('simulated unavailable Documents folder')
        return original(path,*args,**kwargs)
    monkeypatch.setattr(Path,'mkdir',mkdir_with_unavailable_documents)
    selected,notice=database_module.resolve_backup_directory(primary,fallback)
    assert selected==fallback and fallback.is_dir()
    assert str(fallback) in notice and 'Documents' in notice

def test_json_database_is_authoritative_after_reload():
    with TestClient(app) as c:
        authenticate(c)
        sku='JSON-'+uuid4().hex
        created=c.post('/api/products',json={'name':'Persistência JSON','sku':sku,'quantity':2,'price':10})
        assert created.status_code==200
        assert DB_PATH.suffix=='.json' and DB_PATH.is_file()
        reload_json_database()
        rows=c.get('/api/products').json()
        assert any(row['sku']==sku for row in rows)
        assert not LEGACY_SQLITE_PATH.exists()


# --- Regressão: travamento ao trocar de aba / "Failed to fetch" -------------------------
import pytest
from fastapi import HTTPException
from app import database as database_module

TAB_ENDPOINTS=['/api/dashboard','/api/products','/api/sales','/api/service-orders','/api/guarantees','/api/orders','/api/suppliers','/api/finance','/api/accounts','/api/reports/summary','/api/backups']

def test_database_dependency_releases_lock_when_request_raises():
    dependency=get_db()
    next(dependency)
    with pytest.raises(RuntimeError):
        dependency.throw(RuntimeError('falha simulada'))
    assert not DATA_LOCK.locked()

def test_database_dependency_gives_up_with_503_when_lock_is_stuck(monkeypatch):
    monkeypatch.setattr(database_module,'DB_LOCK_TIMEOUT',0.2)
    assert DATA_LOCK.acquire(timeout=1)
    try:
        dependency=get_db()
        with pytest.raises(HTTPException) as caught:
            next(dependency)
        assert caught.value.status_code==503
    finally:
        DATA_LOCK.release()
    dependency=get_db()
    next(dependency)
    dependency.close()
    assert not DATA_LOCK.locked()

def test_api_answers_503_instead_of_hanging_when_database_lock_is_stuck(monkeypatch):
    with TestClient(app) as c:
        authenticate(c)
        monkeypatch.setattr(database_module,'DB_LOCK_TIMEOUT',0.2)
        assert DATA_LOCK.acquire(timeout=1)
        try:
            response=c.get('/api/suppliers')
        finally:
            DATA_LOCK.release()
        assert response.status_code==503
        assert 'ocupado' in response.json()['detail']
        assert c.get('/api/suppliers').status_code==200
        assert not DATA_LOCK.locked()

def test_switching_between_tabs_repeatedly_never_leaves_the_lock_held():
    with TestClient(app) as c:
        authenticate(c)
        for _ in range(5):
            for path in TAB_ENDPOINTS:
                response=c.get(path)
                assert response.status_code==200,(path,response.text)
                assert not DATA_LOCK.locked(),path

def test_concurrent_tab_requests_complete_without_deadlock():
    with TestClient(app) as c:
        authenticate(c)
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures=[executor.submit(lambda p=path:c.get(p).status_code) for path in TAB_ENDPOINTS*4]
            codes=[future.result(timeout=60) for future in futures]
        assert set(codes)=={200}
        assert not DATA_LOCK.locked()
