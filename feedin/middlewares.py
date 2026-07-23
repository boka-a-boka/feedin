import functools
from flask import request, session, flash, redirect, url_for
from flask_login import current_user

from feedin.modules.empresa.models import ColaboradorContrato


def modulo_required(f):
    """
    Middleware de Módulo:
    Sincroniza o usuário autenticado com o Módulo e valida acessos sem gerar loops.
    """

    @functools.wraps(f)
    def decorated_function(*args, **kwargs):
        # 1. Padroniza o módulo na sessão
        bp_name = request.blueprint or ''
        modulo_solicitante = bp_name.replace('_bp', '') if bp_name else session.get('modulo_slug_atual', 'empresa')
        session['modulo_slug_atual'] = modulo_solicitante

        # 2. VALIDAÇÃO DE AUTENTICAÇÃO (Sincroniza Core <-> Módulo)
        cliente_id = session.get('cliente_modulo_id')
        if not cliente_id and current_user.is_authenticated:
            cliente_id = current_user.id
            session['cliente_modulo_id'] = cliente_id

        # Se não há usuário logado de forma alguma, envia para a autenticação
        if not cliente_id:
            return redirect(url_for('auth.login'))

        # 3. ROTAS ISENTAS DE TRAVA DE NEGÓCIO ATIVO
        # Rotas centrais que resolvem a seleção de empresa não podem exigir que o 'local_id' já esteja na sessão!
        rotas_isentas = ['empresa.painel_empresa', 'empresa.meus_negocios_tela', 'empresa.dashboard_empresa']

        local_id = session.get('local_id_atual') or session.get('empresa_ativa_id')

        # Se não tiver empresa selecionada na sessão e estiver tentando acessar outra sub-rota restrita:
        if not local_id and request.endpoint not in rotas_isentas:
            flash("Selecione um negócio para continuar.", "info")
            return redirect(url_for('empresa.painel_empresa'))

        # 4. VALIDAÇÃO CONTRATUAL / PERMISSÕES
        if current_user.is_authenticated and local_id:
            filtros_contrato = [
                ColaboradorContrato.id_local == local_id,
                ColaboradorContrato.status_profissional == 'ativo'
            ]

            if cliente_id:
                filtros_contrato.append(ColaboradorContrato.id_cadastro_cliente == str(cliente_id))
            elif getattr(current_user, 'id', None):
                filtros_contrato.append(ColaboradorContrato.id_usuario == current_user.id)

            contrato = ColaboradorContrato.query.filter(*filtros_contrato).first()

            if contrato:
                nivel_usuario = getattr(contrato, 'papel_nivel', 500)
                if modulo_solicitante == 'financeiro' and nivel_usuario < 777:
                    flash("Seu nível de permissão não permite acesso a este módulo.", "warning")
                    return redirect(url_for('empresa.painel_empresa'))

        return f(*args, **kwargs)

    return decorated_function