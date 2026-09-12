from functools import wraps
from flask import request, redirect, url_for, flash, jsonify, g
from flask_login import current_user

# Importa o modelo real do FeedIn para checagem de módulos
from feedin.modules.empresa.models import ModEmpresaModulo, ColaboradorContrato


# -------------------------------------------------------------------------
# Helper 1: Resolver Contexto e Nível de Acesso do Usuário na Empresa
# -------------------------------------------------------------------------
def resolver_contexto_usuario(usuario, kwargs):
    """
    Resolve o nível de acesso efetivo do usuário considerando:
    - Se é SuperAdmin/Admin do sistema (Nível 999/900)
    - Se é Proprietário/Colaborador no contrato da empresa (Nível do contrato)
    - Se é Cliente comum (Nível 10)

    Retorna: (nivel_efetivo: int, papel_nome: str, permissoes_lista: list)
    """
    if not usuario or not getattr(usuario, 'is_authenticated', False):
        return 0, 'Anônimo', []

    # SuperAdmin ou Admin global do sistema
    if getattr(usuario, 'is_admin', False) or getattr(usuario, 'is_superadmin', False):
        return 999, 'Administrador', ['*']

    user_uuid = str(usuario.id)
    empresa_id = kwargs.get('empresa_id') or kwargs.get('id')

    if empresa_id:
        # Busca contrato do colaborador no local especificado
        contrato = ColaboradorContrato.query.filter_by(
            id_local=empresa_id,
            id_cadastro_cliente=user_uuid,
            status_profissional='ativo'
        ).first()

        if contrato:
            nivel = getattr(contrato, 'nivel_acesso', 100)
            papel = getattr(contrato, 'papel_nome', 'Colaborador')
            return nivel, papel, []

    # Nível padrão de cliente autenticado no PWA
    return 10, 'Cliente', []


# -------------------------------------------------------------------------
# Helper 2: Padronizar respostas de negação de acesso (Web vs AJAX)
# -------------------------------------------------------------------------
def _responder_negativa_acesso(mensagem, url_redirecionamento=None, status_code=403):
    """Retorna JSON para chamadas AJAX/Fetch e Redirect + Flash para requisições normais."""
    is_ajax = (
            request.headers.get('X-Requested-With') == 'XMLHttpRequest' or
            request.accept_mimetypes.best == 'application/json' or
            request.is_json
    )

    if is_ajax:
        return jsonify({
            'sucesso': False,
            'erro': mensagem,
            'status': status_code
        }), status_code

    flash(mensagem, 'danger')

    if not url_redirecionamento:
        try:
            url_redirecionamento = url_for('dashboard.index')
        except Exception:
            url_redirecionamento = '/'

    return redirect(url_redirecionamento)


# -------------------------------------------------------------------------
# Guard 1: Validação por Nível de Acesso Mínimo (`requer_nivel`)
# -------------------------------------------------------------------------
def requer_nivel(min_nivel=10):
    """
    Garante que o usuário esteja autenticado e possua nível mínimo de acesso
    para o contexto da requisição.
    """

    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if not current_user.is_authenticated:
                return _responder_negativa_acesso("Autenticação necessária.", url_for('auth.login'), 401)

            nivel_efetivo, papel, _ = resolver_contexto_usuario(current_user, kwargs)

            # Injeta no contexto global da requisição
            g.user_nivel = nivel_efetivo
            g.user_papel = papel

            if nivel_efetivo < min_nivel and not getattr(current_user, 'is_admin', False):
                return _responder_negativa_acesso(
                    f"Nível de acesso insuficiente para esta operação ({nivel_efetivo} < {min_nivel}).",
                    url_for('dashboard.index'),
                    403
                )

            return f(*args, **kwargs)

        return decorated_function

    return decorator


# Alias para manter compatibilidade com telas que usam o nome antigo
empresa_acesso_required = requer_nivel


# -------------------------------------------------------------------------
# Guard 2: Validação de Vínculo com Módulo Ativo da Empresa
# -------------------------------------------------------------------------
def requer_acesso_modulo(modulo_slug):
    """
    Verifica se a empresa especificada na rota possui o módulo ativo e homologado
    na tabela real `ModEmpresaModulo`.
    """

    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if not current_user.is_authenticated:
                return _responder_negativa_acesso("Autenticação necessária.", url_for('auth.login'), 401)

            # Administrador do sistema tem passe livre
            if getattr(current_user, 'is_admin', False):
                return f(*args, **kwargs)

            empresa_id = kwargs.get('empresa_id') or kwargs.get('id')

            if empresa_id:
                # Consulta real no banco usando o modelo ModEmpresaModulo
                modulo_ativo = ModEmpresaModulo.query.filter(
                    ModEmpresaModulo.empresa_id == empresa_id,
                    ModEmpresaModulo.modulo_slug == modulo_slug,
                    ModEmpresaModulo.ativo == True
                ).first()

                if not modulo_ativo:
                    return _responder_negativa_acesso(
                        f"A empresa não possui o módulo '{modulo_slug}' ativo.",
                        url_for('dashboard.index'),
                        403
                    )

            return f(*args, **kwargs)

        return decorated_function

    return decorator