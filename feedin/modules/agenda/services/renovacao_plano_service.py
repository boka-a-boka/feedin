from datetime import datetime, timedelta, timezone
from flask import current_app
from feedin import database as db
from feedin.modules.empresa.models import EseEmpresa, ModEmpresaModulo
from feedin.models import CoreAcaoPendente
from feedin.modules.core.services.notificacao_core_service import CoreNotificacaoService


def _ja_notificado_recentemente(empresa_id, modulo_slug: str, motivo: str, horas_janela: int = 24) -> bool:
    """Evita reenvio repetido do mesmo alerta para a mesma empresa e motivo dentro da janela."""
    limite = datetime.now(timezone.utc) - timedelta(hours=horas_janela)
    query = CoreAcaoPendente.query.filter(
        CoreAcaoPendente.entidade_id == empresa_id
    )
    if hasattr(CoreAcaoPendente, 'criado_em'):
        query = query.filter(CoreAcaoPendente.criado_em >= limite)

    pendencias = query.all()
    for p in pendencias:
        payload = p.payload_dados or {}
        if payload.get('tipo_alerta') == 'convite_renovacao_plano':
            if payload.get('modulo_slug') == modulo_slug and payload.get('motivo') == motivo:
                return True
    return False


def disparar_alerta_renovacao_empreendedor(
    empresa_id,
    modulo_slug: str = 'agenda',
    motivo: str = 'degustacao_expirada',
    horas_janela: int = 24
) -> bool:
    """
    Registra e dispara o alerta de encerramento/limite do plano ao empreendedor
    com convite para contratação ou renovação, utilizando o padrão oficial do Core.
    """
    empresa = EseEmpresa.query.get(empresa_id)
    if not empresa:
        return False

    mod_modulo = ModEmpresaModulo.query.filter_by(
        empresa_id=empresa.id,
        modulo_slug=modulo_slug
    ).first()

    # Se o módulo já está ativo e vigente (e fora de bloqueio por limite), não alerta
    agora = datetime.now(timezone.utc)
    if mod_modulo and mod_modulo.ativo:
        venc = mod_modulo.data_vencimento
        if venc is None:
            return False
        venc_aware = venc if venc.tzinfo else venc.replace(tzinfo=timezone.utc)
        if venc_aware >= agora and motivo != 'limite_12_clientes_teste':
            return False

    if _ja_notificado_recentemente(empresa.id, modulo_slug, motivo, horas_janela=horas_janela):
        return False

    if motivo == 'limite_12_clientes_teste':
        titulo = f"Limite de Degustação Atingido ({modulo_slug.upper()}) - {empresa.nome}"
        descricao = (
            f"O estabelecimento '{empresa.nome}' atingiu o limite de 12 clientes em teste "
            f"no plano de degustação do módulo '{modulo_slug}'. "
            f"Contrate ou renove o plano para ampliar o atendimento sem interrupções."
        )
    else:
        titulo = f"Período de Degustação Encerrado ({modulo_slug.upper()}) - {empresa.nome}"
        descricao = (
            f"O prazo de degustação/vigência do módulo '{modulo_slug}' para '{empresa.nome}' "
            f"chegou ao fim e o estabelecimento saiu da seleção ativa. "
            f"Realize a contratação ou renovação para reativar o módulo."
        )

    modulo_id = getattr(mod_modulo, 'modulo_id', None)
    payload = {
        'tipo_alerta': 'convite_renovacao_plano',
        'modulo_slug': modulo_slug,
        'motivo': motivo,
        'empresa_id': empresa.id,
        'empresa_slug': empresa.slug,
        'acao_recomendada': 'contratar_ou_renovar_plano',
    }

    try:
        CoreNotificacaoService.registrar_acao_e_notificar(
            modulo_id=modulo_id,
            entidade_id=empresa.id,
            departamento_codigo='usuario',
            titulo=titulo,
            descricao=descricao,
            payload=payload
        )
        db.session.commit()
        return True
    except Exception as exc:
        db.session.rollback()
        current_app.logger.error(
            f"❌ Falha ao disparar alerta de renovação (empresa={empresa.id}, motivo={motivo}): {exc}"
        )
        return False