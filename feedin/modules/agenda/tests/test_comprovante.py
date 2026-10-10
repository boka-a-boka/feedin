import pytest
from feedin import create_app, database as db

from feedin.modules.agenda.models import AghAgendamento
from feedin.modules.billing.models import LancamentoFinanceiro

@pytest.fixture
def client():
    app = create_app('testing')
    with app.test_client() as client:
        with app.app_context():
            db.create_all()
            yield client
            db.session.remove()
            db.drop_all()

def test_bloqueio_comprovante_status_finalizado(client):
    """Garante que agendamentos com status 'finalizado' retorne erro 400."""
    agendamento = AghAgendamento(status='finalizado', valor_total=100.00)
    db.session.add(agendamento)
    db.session.commit()

    response = client.get(f'/agenda/comprovante/{agendamento.id}')
    assert response.status_code == 400
    assert b"Comprovante indisponivel" in response.data

def test_bloqueio_comprovante_status_agendado(client):
    """Garante que agendamentos pendentes ou em aberto retorne erro 400."""
    agendamento = AghAgendamento(status='agendado', valor_total=100.00)
    db.session.add(agendamento)
    db.session.commit()

    response = client.get(f'/agenda/comprovante/{agendamento.id}')
    assert response.status_code == 400

def test_sucesso_comprovante_status_concluido(client):
    """Garante renderizacao HTTP 200 para agendamentos concluídos."""
    agendamento = AghAgendamento(status='concluido', valor_total=150.00)
    db.session.add(agendamento)
    db.session.commit()

    lancamento = LancamentoFinanceiro(
        agendamento_id=agendamento.id,
        status='LIQUIDADO',
        valor=150.00,
        forma_pagamento='PIX'
    )
    db.session.add(lancamento)
    db.session.commit()

    response = client.get(f'/agenda/comprovante/{agendamento.id}')
    assert response.status_code == 200
    assert b"REC-" in response.data
    assert b"PIX" in response.data