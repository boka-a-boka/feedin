import logging
import requests
from flask import current_app, url_for
from feedin import current_app, database as db

logger = logging.getLogger(__name__)


class WhatsappService:
    @staticmethod
    def enviar_comprovante_agendamento(agendamento_id: int):
        """
        Recupera os dados do agendamento concluído, gera o link público
        e envia a mensagem de comprovante via API do WhatsApp.
        """
        from feedin.modules.agenda.models import AghAgendamento  # import local para evitar circular import

        agendamento = AghAgendamento.query.get(agendamento_id)
        if not agendamento or str(agendamento.status).lower() != 'concluido':
            logger.warning(
                f"[WhatsApp] Cancelado disparo: Agendamento {agendamento_id} não encontrado ou não concluído.")
            return False

        # 1. Recupera o telefone do cliente
        cliente = agendamento.cliente
        telefone = getattr(cliente, 'telefone', None) or getattr(cliente, 'celular', None) if cliente else None

        if not telefone:
            logger.info(f"[WhatsApp] Agendamento {agendamento_id} não possui telefone de cliente vinculado.")
            return False

        # Sanitize do telefone (apenas números com DDD + DDI)
        phone_clean = ''.join(filter(str.isdigit, str(telefone)))
        if not phone_clean.startswith('55'):
            phone_clean = f"55{phone_clean}"

        # 2. Gera URL absoluta para o comprovante
        url_comprovante = url_for(
            'agenda_bp.visualizar_comprovante',
            agendamento_id=agendamento.id,
            _external=True
        )

        # 3. Monta o texto legível e amigável
        nome_cliente = getattr(cliente, 'nome', 'Cliente').split()[0]
        empresa_nome = agendamento.empresa.razao_social if agendamento.empresa else "Nosso Estabelecimento"
        valor_total = float(agendamento.valor_total or 0.0)

        mensagem = (
            f"Olá, *{nome_cliente}*! 👋\n\n"
            f"Seu pagamento na *{empresa_nome}* no valor de *R$ {valor_total:.2f}* foi confirmado com sucesso!\n\n"
            f"📄 *Acesse seu comprovante de atendimento:* \n"
            f"{url_comprovante}\n\n"
            f"Agradecemos a preferência e até breve!"
        )

        # 4. Envio HTTP para o Provider (Exemplo: Evolution API / Z-API)
        api_url = current_app.config.get(
            'WHATSAPP_API_URL')  # ex: https://api.z-api.io/instances/YOUR_INSTANCE/token/YOUR_TOKEN/send-text
        api_key = current_app.config.get('WHATSAPP_API_KEY')

        if not api_url:
            logger.error("[WhatsApp] WHATSAPP_API_URL não configurada no ambiente.")
            return False

        headers = {
            'Content-Type': 'application/json',
            'apikey': api_key
        }

        payload = {
            'number': phone_clean,
            'text': mensagem,
            'options': {
                'delay': 1200,
                'presence': 'composing',
                'linkPreview': True
            }
        }

        try:
            response = requests.post(api_url, json=payload, headers=headers, timeout=10)
            if response.status_code in [200, 201]:
                logger.info(
                    f"[WhatsApp] Comprovante enviado com sucesso para {phone_clean} (Agendamento: {agendamento_id}).")
                return True
            else:
                logger.error(f"[WhatsApp] Falha no envio para {phone_clean}: {response.status_code} - {response.text}")
                return False
        except Exception as e:
            logger.exception(f"[WhatsApp] Exceção ao enviar comprovante do agendamento {agendamento_id}: {str(e)}")
            return False