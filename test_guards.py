import pytest
from unittest.mock import MagicMock, patch
from flask import Flask, g, session


class DummyUser:
    """Simula o current_user do Flask-Login"""

    def __init__(self, id="31a2c4e5-9f87-41a2-b3c4-d5e6f7a8b9c0", is_auth=True, is_admin=False,
                 email="usuario@teste.com", cpf_hash="hash123"):
        self.id = id
        self.is_authenticated = is_auth
        self.is_admin = is_admin
        self.email = email
        self.cpf_hash = cpf_hash
        self.nivel_acesso = 10
        self.papel_slug = 'cliente'


def setup_app():
    """Cria uma instância enxuta do Flask para os testes de rota"""
    app = Flask(__name__)
    app.config['SECRET_KEY'] = 'test-key-feedin'

    from feedin.middlewares import (
        resolver_contexto_usuario,
        requer_acesso_modulo,
        empresa_acesso_required,
        requer_nivel
    )

    @app.route('/modulo-agenda/<int:empresa_id>')
    @requer_acesso_modulo('agenda')
    def rota_agenda(empresa_id):
        return f"Acesso liberado para o Módulo Agenda no Local {empresa_id}", 200

    @app.route('/empresa-gestao/<int:empresa_id>')
    @empresa_acesso_required(min_nivel=500)
    def rota_gestao(empresa_id):
        return f"Painel de Operador (Nível {g.user_nivel}) Liberado", 200

    return app


def test_cenarios():
    app = setup_app()

    print("\n--- 🧪 INICIANDO TESTES DE COMPORTAMENTO DO MIDDLEWARE ---")

    # Mock que simula o resultado positivo do banco (.filter_by().filter().first())
    mock_vinculo_query = MagicMock()
    mock_vinculo_query.filter_by.return_value.filter.return_value.first.return_value = True

    # 1. Teste de Acesso a Módulo sem exigência de re-login na session
    with app.test_request_context('/modulo-agenda/101'):
        with patch('flask_login.utils._get_user', return_value=DummyUser()):
            with patch('feedin.middlewares.resolver_contexto_usuario', return_value=(500, 'operador', [])):
                # Intercepta o acesso à propriedade ModVinculoModulo.query para não tocar no engine do SQLAlchemy
                with patch('feedin.modules.auth.models.ModVinculoModulo.query', mock_vinculo_query):
                    session.clear()

                    res = app.dispatch_request()
                    print("✅ Cenário 1 [Módulo via Middleware]:",
                          "PASSOU (Status 200)" if "Acesso liberado" in str(res) else f"FALHOU ({res})")

    # 2. Teste de Bloqueio por Nível Insuficiente (< min_nivel)
    with app.test_request_context('/empresa-gestao/101'):
        with patch('flask_login.utils._get_user', return_value=DummyUser()):
            with patch('feedin.middlewares.resolver_contexto_usuario', return_value=(300, 'assistente', [])):
                res = app.dispatch_request()
                print("✅ Cenário 2 [Bloqueio por Nível Insuficiente]:",
                      "PASSOU (Redirecionamento/Bloqueio correto)" if res.status_code in (302, 403) else f"FALHOU ({res})")

    # 3. Resposta AJAX/JSON para não autenticado
    with app.test_request_context('/empresa-gestao/101', headers={'X-Requested-With': 'XMLHttpRequest'}):
        with patch('flask_login.utils._get_user', return_value=DummyUser(is_auth=False)):
            res = app.dispatch_request()
            print("✅ Cenário 3 [Resposta AJAX para não autenticado]:",
                  "PASSOU (Retornou JSON 401)" if res.status_code == 401 and res.is_json else f"FALHOU ({res})")


if __name__ == '__main__':
    test_cenarios()