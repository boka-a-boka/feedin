from flask import Blueprint

auth_bp = Blueprint(
    'auth',
    __name__,
    template_folder='templates',
    url_prefix='/auth'
)

from . import routes, models


def auth_user_loader(user_id):
    """
    Provedor de Identidade Exclusivo do Módulo Auth
    Busca o cliente pela chave primária (UUID).
    """
    if not user_id:
        return None

    try:
        from feedin.modules.auth.models import ModCadastroCliente
        return ModCadastroCliente.query.get(str(user_id).strip())
    except Exception:
        return None


@auth_bp.record_once
def register_auth_loader(state):
    """
    Pluga o carregador do Módulo Auth na lista do Core automaticamente durante o boot.
    """
    app = state.app
    if 'MODULE_USER_LOADERS' not in app.config:
        app.config['MODULE_USER_LOADERS'] = []

    if auth_user_loader not in app.config['MODULE_USER_LOADERS']:
        app.config['MODULE_USER_LOADERS'].append(auth_user_loader)