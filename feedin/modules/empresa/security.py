from functools import wraps
from flask import flash, redirect, url_for
from flask_login import current_user
from datetime import datetime, timezone, timedelta, time, date
from feedin import database as db
from feedin.modules.empresa.models import EseEmpresa, ModEmpresaModulo
from feedin.modules.auth.models import ModVinculoModulo


def verificar_acesso_modulo(modulo_slug):
    """
    Decorador de segurança ecossistêmica:
    Garante que a empresa e o usuário possuem direito legítimo e ativo de uso ao módulo.
    """

    def decorator(f):
        @wraps(f)
        def decorated_function(empresa_id, *args, **kwargs):
            # 1. Valida se a empresa existe
            ese_empresa = EseEmpresa.query.get_or_404(empresa_id)

            # 2. Permissão básica de gestão da empresa (Proprietário ou Admin)
            is_proprietario = getattr(ese_empresa, 'proprietario_id', None) == current_user.id
            is_admin = getattr(current_user, 'is_admin', False)

            if not is_proprietario and not is_admin:
                flash("Você não tem permissão para gerenciar esta empresa.", "danger")
                return redirect(url_for('empresa.central_hub'))

            # 3. Validação do Passaporte Civil (ModVinculoModulo)
            cpf_hash = getattr(current_user, 'cpf_hash', None)
            if not cpf_hash:
                flash("Identificação civil (CPF) não encontrada.", "warning")
                return redirect(url_for('auth.login'))

            vinculo_civil = ModVinculoModulo.query.filter_by(
                cpf_hash=cpf_hash,
                modulo_slug=modulo_slug,
                ativo=True
            ).first()

            # 4. Validação da Adesão/Homologação do Módulo na Empresa (ModEmpresaModulo)
            adesao_comercial = ModEmpresaModulo.query.filter_by(
                empresa_id=ese_empresa.id,
                modulo_slug=modulo_slug,
                ativo=True,
                status_homologacao='homologado'
            ).first()

            # Se não possui nem o vínculo civil e nem a adesão comercial ativa (e não for admin), bloqueia!
            if not vinculo_civil and not adesao_comercial and not is_admin:
                flash(f"O acesso ao módulo '{modulo_slug.capitalize()}' requer contratação ou homologação ativa.",
                      "warning")
                return redirect(url_for('empresa.index_modulo_empresa', empresa_id=ese_empresa.id))

            return f(empresa_id, *args, **kwargs)

        return decorated_function

    return decorator

