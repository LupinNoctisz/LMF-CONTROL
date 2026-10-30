import sys

import pytest


@pytest.fixture(autouse=True)
def _wait_for_background_audit():
    """O resultado final das operações autorizadas é gravado em segundo plano; espera terminar entre testes.
    (Importação tardia: os módulos de teste definem LMF_DATA_DIR antes de importar o app.)"""
    yield
    module = sys.modules.get('app.authorization')
    if module is not None:
        module.drain_pending(10)
