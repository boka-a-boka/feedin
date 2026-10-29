import logging
from flask import current_app, url_for
from feedin.modules.empresa.models import EseEmpresa, EseProcessoClaim

logger = logging.getLogger(__name__)


def disparar_notificacao_auditoria_empresa(empresa_id: int):
    """
    Dispara notificação multicanal (WhatsApp/E-mail) para a equipe de atendimento
    informando que uma empresa concluiu o envio de documentos e está aguardando auditoria.
    """
    try:
        empresa = EseEmpresa.query.get(empresa_id)
        if not empresa:
            logger.warning(f"[NOTIFICAÇÃO EMPRESA] Empresa #{empresa_id} não encontrada para notificação de auditoria.")
            return False

        processo = EseProcessoClaim.query.filter_by(local_id=empresa.local_id).order_by(
            EseProcessoClaim.id.desc()).first()
        token = processo.token_validacao if processo else "N/A"

        # Link direto para o painel de auditoria interna ou hub da empresa
        try:
            link_auditoria = url_for('empresa.hub_ativacao', empresa_id=empresa.id, _external=True)
        except Exception:
            link_auditoria = f"/empresa/hub/{empresa.id}"

        nome_empresa = empresa.nome or f"Empresa #{empresa.id}"

        logger.info(
            f"🔔 [AUDITORIA EMPRESA] Disparando alerta para atendimento: "
            f"Estabelecimento '{nome_empresa}' enviou a documentação e está aguardando análise humana. Token: {token}"
        )

        # ---------------------------------------------------------------------
        # INTEGRAÇÃO DE DISPARO (Exemplo usando o canal oficial do sistema)
        # ---------------------------------------------------------------------
        # Aqui você pode plugar a chamada para a sua API de WhatsApp de suporte:
        # from feedin.services.whatsapp_service import disparar_whatsapp_suporte
        # mensagem = f"🚨 *Nova Empresa em Auditoria*\n\nEstabelecimento: {nome_empresa}\nAcesse para homologar: {link_auditoria}"
        # disparar_whatsapp_suporte(mensagem)

        return True

    except Exception as e:
        logger.error(f"❌ [ERRO NOTIFICAÇÃO EMPRESA] Falha ao notificar atendimento sobre a empresa #{empresa_id}: {e}",
                     exc_info=True)
        return False