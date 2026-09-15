import uuid
import threading
import smtplib
import logging
from zoneinfo import ZoneInfo
from datetime import datetime
from sqlalchemy.exc import IntegrityError
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from flask import current_app

from feedin import database as db
from feedin.modules.auth.models import ModCadastroCliente
from feedin.modules.agenda.models import AghNotificacao, AghAgendamento
from feedin.modules.empresa.models import (
    ClienteBeneficiario,
    ClienteContato,
    EseLogMensagemAutomatica,
    EseNotificacao
)

logger = logging.getLogger(__name__)

# Fuso horário padrão do sistema
TZ_BRASIL = ZoneInfo('America/Sao_Paulo')


# =========================================================================
# 🛠️ HELPER INTERNO DE IDENTIDADE (COMPATIBILIDADE UUID)
# =========================================================================
def _resolver_usuario_uuid(cliente_id_or_obj) -> str:
    """
    Garante a recuperação estrita do UUID (usuario_id) do cliente.
    Elimina o uso de IDs do tipo Integer nos registros de notificação.
    """
    if not cliente_id_or_obj:
        return None

    # Se já for o objeto `ModCadastroCliente`
    if isinstance(cliente_id_or_obj, ModCadastroCliente):
        return str(cliente_id_or_obj.usuario_id).strip() if getattr(cliente_id_or_obj, 'usuario_id', None) else None

    # Se for uma string que já seja um UUID válido
    val_str = str(cliente_id_or_obj).strip()
    try:
        uuid_obj = uuid.UUID(val_str)
        return str(uuid_obj)
    except ValueError:
        # Se for um Integer/ID legado, consulta o banco para obter o usuario_id (UUID)
        cliente = ModCadastroCliente.query.get(cliente_id_or_obj)
        if cliente and getattr(cliente, 'usuario_id', None):
            return str(cliente.usuario_id).strip()

    logger.warning(f"⚠️ [IDENTIDADE] Não foi possível resolver UUID válido para a referência: {cliente_id_or_obj}")
    return val_str


# =========================================================================
# 📅 1. SERVIÇO DE NOTIFICAÇÕES DO MÓDULO AGENDA (AGH)
# =========================================================================
class AghNotificacaoService:

    @staticmethod
    def criar_notificacao(
            empresa_id: int,
            destinatario_id: str,
            papel_destinatario: str,
            titulo: str,
            mensagem: str,
            tipo_evento: str,
            remetente_id: str = None,
            agendamento_id: int = None,
            beneficiario_id: int = None,
            nivel: str = "info",
            payload_extra: dict = None
    ) -> AghNotificacao:
        """
        Registra notificações internas exclusivas do Módulo de Agenda (tabela `agh_notificacoes`).
        Garante que destinatario_id e remetente_id sejam UUIDs em formato string.
        """
        try:
            uuid_destinatario = _resolver_usuario_uuid(destinatario_id)
            uuid_remetente = _resolver_usuario_uuid(remetente_id) if remetente_id else None

            notificacao = AghNotificacao(
                id=str(uuid.uuid4()),
                empresa_id=empresa_id,
                destinatario_id=uuid_destinatario,
                papel_destinatario=papel_destinatario,
                remetente_id=uuid_remetente,
                agendamento_id=agendamento_id,
                beneficiario_id=beneficiario_id,
                titulo=titulo,
                mensagem=mensagem,
                tipo_evento=tipo_evento,
                nivel=nivel,
                payload_extra=payload_extra or {},
                lida=False,
                criado_em=datetime.now(TZ_BRASIL)
            )
            db.session.add(notificacao)
            db.session.commit()
            return notificacao
        except Exception as e:
            db.session.rollback()
            logger.error(f"[ERRO AghNotificacaoService]: Falha ao registrar notificação da Agenda - {str(e)}")
            return None

    @classmethod
    def disparar(cls, *args, **kwargs):
        return cls.criar_notificacao(*args, **kwargs)

    @staticmethod
    def notificar_falta_cliente(agendamento_id: int, colaborador_usuario_id: str = None, colaborador_nome: str = None,
                                motivo: str = "ausencia_cliente"):
        """
        Processa e registra o não-comparecimento do cliente na agenda.
        """
        agendamento = AghAgendamento.query.get(agendamento_id)
        if not agendamento:
            return None

        agora_local = datetime.now(TZ_BRASIL)

        agendamento.status = 'FALTA'
        if hasattr(agendamento, 'atualizado_em'):
            agendamento.atualizado_em = agora_local
        db.session.commit()

        # Identificação de Beneficiários (Pet, Veículo, Dependente)
        beneficiario_info = ""
        beneficiario_id = getattr(agendamento, 'beneficiario_id', None)
        if beneficiario_id:
            beneficiario = ClienteBeneficiario.query.get(beneficiario_id)
            if beneficiario:
                tipo_p = getattr(beneficiario, 'tipo_persona', None)
                if tipo_p == 'pet':
                    beneficiario_info = f" (Pet: {beneficiario.nome})"
                elif tipo_p == 'veiculo':
                    beneficiario_info = f" (Veículo: {beneficiario.nome})"
                else:
                    beneficiario_info = f" ({beneficiario.nome})"

        data_agendamento_fmt = (
            agendamento.data_hora_inicio.strftime("%d/%m/%Y às %H:%M")
            if getattr(agendamento, 'data_hora_inicio', None) else ""
        )
        agora_apontamento_fmt = agora_local.strftime("%d/%m/%Y às %H:%M")
        nome_operador = colaborador_nome or "Colaborador"

        # Resolução estrita do UUID do destinatário
        uuid_cliente = _resolver_usuario_uuid(
            agendamento.cliente if hasattr(agendamento, 'cliente') else agendamento.cliente_id)

        # 1. NOTIFICAÇÃO IN-APP DA AGENDA
        mensagem_notif = (
            f"Foi registrada uma falta referente ao seu agendamento de {data_agendamento_fmt}{beneficiario_info}.\n"
            f"Apontamento realizado por: {nome_operador} em {agora_apontamento_fmt}."
        )

        notificacao_interna = AghNotificacaoService.criar_notificacao(
            empresa_id=agendamento.empresa_id,
            destinatario_id=uuid_cliente,
            papel_destinatario='cliente',
            remetente_id=_resolver_usuario_uuid(colaborador_usuario_id) or (
                str(agendamento.profissional_id) if agendamento.profissional_id else None),
            agendamento_id=agendamento.id,
            beneficiario_id=beneficiario_id,
            titulo="⚠️ Apontamento de Ausência",
            mensagem=mensagem_notif,
            tipo_evento="falta_cliente",
            nivel="warning",
            payload_extra={
                'colaborador_nome': nome_operador,
                'data_apontamento': agora_apontamento_fmt
            }
        )

        # 2. DISPARO EXTERNO (OPCIONAL/BACKGROUND)
        contato_preferencial = ClienteContato.query.filter_by(
            cliente_id=agendamento.cliente_id,
            aceita_notificacao=True
        ).order_by(ClienteContato.is_padrao.desc()).first()

        if not contato_preferencial:
            logger.info(
                f"[AGH NOTIFICAÇÃO] Notificação in-app gerada para Cliente UUID '{uuid_cliente}'. Sem canais externos ativos.")
            return True

        tipo_canal = contato_preferencial.tipo.lower()
        destino = contato_preferencial.valor
        nome_cliente = agendamento.cliente.nome.split()[0] if hasattr(agendamento,
                                                                      'cliente') and agendamento.cliente and agendamento.cliente.nome else 'Cliente'

        assunto = "Aviso de Agendamento - Não Comparecimento"
        mensagem_txt = (
            f"Olá, {nome_cliente}!\n\n"
            f"Identificamos que você não pôde comparecer ao seu agendamento em {data_agendamento_fmt}{beneficiario_info}.\n"
            f"O apontamento foi feito por {nome_operador} em {agora_apontamento_fmt}.\n\n"
            f"Seu horário foi liberado. Para agendar um novo horário, acesse nosso aplicativo."
        )

        app_instance = current_app._get_current_object()
        thread = threading.Thread(
            target=AghNotificacaoService._disparar_canal_externo_bg,
            args=(app_instance, tipo_canal, destino, assunto, mensagem_txt)
        )
        thread.daemon = True
        thread.start()

        return True

    @staticmethod
    def _disparar_canal_externo_bg(app, tipo_canal: str, destino: str, assunto: str, mensagem: str):
        with app.app_context():
            try:
                if tipo_canal == 'email':
                    AghNotificacaoService._enviar_email_smtp(app, destino, assunto, mensagem)
                elif tipo_canal in ['whatsapp', 'sms']:
                    AghNotificacaoService._enviar_mensageria_api(app, tipo_canal, destino, mensagem)
            except Exception as e:
                logger.error(f"[ERRO DISPARO BACKGROUND AGH] Falha ao enviar por {tipo_canal} para {destino}: {str(e)}")

    @staticmethod
    def _enviar_email_smtp(app, email_destino: str, assunto: str, mensagem: str):
        smtp_server = app.config.get('MAIL_SERVER', 'smtp.gmail.com')
        smtp_port = app.config.get('MAIL_PORT', 587)
        smtp_user = app.config.get('MAIL_USERNAME')
        smtp_pass = app.config.get('MAIL_PASSWORD')
        sender_email = app.config.get('MAIL_DEFAULT_SENDER', smtp_user)

        if not smtp_user or not smtp_pass:
            logger.warning("⚠️ [EMAIL OMITIDO] Credenciais de SMTP não configuradas.")
            return

        msg = MIMEMultipart()
        msg['From'] = sender_email
        msg['To'] = email_destino
        msg['Subject'] = assunto
        msg.attach(MIMEText(mensagem, 'plain', 'utf-8'))

        with smtplib.SMTP(smtp_server, smtp_port) as server:
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.sendmail(sender_email, email_destino, msg.as_string())

        logger.info(f"📧 [E-MAIL ENVIADO] Sucesso para {email_destino}")

    @staticmethod
    def _enviar_mensageria_api(app, tipo: str, numero: str, mensagem: str):
        api_url = app.config.get('WHATSAPP_API_URL')
        api_token = app.config.get('WHATSAPP_API_TOKEN')

        if api_url and api_token:
            import requests
            headers = {'Client-Token': api_token, 'Content-Type': 'application/json'}
            payload = {'phone': numero, 'message': mensagem}
            response = requests.post(api_url, json=payload, headers=headers, timeout=10)
            logger.info(f"📱 [{tipo.upper()} API] Status Gateway: {response.status_code}")
        else:
            logger.info(f"📱 [{tipo.upper()} SIMULADO] Destino: {numero} | Conteúdo: {mensagem}")

    @staticmethod
    def contar_nao_lidas(empresa_id: int, usuario_id: str, papel: str) -> int:
        uuid_val = _resolver_usuario_uuid(usuario_id)
        return AghNotificacao.query.filter_by(
            empresa_id=empresa_id,
            destinatario_id=uuid_val,
            papel_destinatario=papel,
            lida=False
        ).count()


# =========================================================================
# 🏢 2. SERVIÇO DE NOTIFICAÇÕES DO MÓDULO EMPRESA (ESE)
# =========================================================================
class EseNotificacaoService:

    @staticmethod
    def criar_notificacao(
            modulo_id: int,
            modulo_slug: str,
            empresa_id: int,
            destinatario_id: str,
            tipo: str,
            categoria: str,
            titulo: str,
            mensagem: str,
            url_acao: str = None,
            data_referencia: datetime = None
    ) -> EseNotificacao:
        """
        Registra notificações institucionais/operacionais do Módulo Empresa (tabela `ese_notificacoes`).
        """
        try:
            uuid_destinatario = _resolver_usuario_uuid(destinatario_id) if destinatario_id else None

            notificacao = EseNotificacao(
                modulo_id=modulo_id,
                modulo_slug=modulo_slug,
                empresa_id=empresa_id,
                destinatario_id=uuid_destinatario,
                tipo=tipo,
                categoria=categoria,
                titulo=titulo,
                mensagem=mensagem,
                url_acao=url_acao,
                data_referencia=data_referencia or datetime.now(TZ_BRASIL).date(),
                lida=False,
                created_at=datetime.now(TZ_BRASIL)
            )
            db.session.add(notificacao)
            db.session.commit()
            return notificacao
        except Exception as e:
            db.session.rollback()
            logger.error(f"[ERRO EseNotificacaoService]: Falha ao registrar notificação da Empresa - {str(e)}")
            return None


# =========================================================================
# 📝 TEMPLATES GLOBAIS DE LEMBRETES (FEEDIN)
# =========================================================================
TEMPLATES_LEMBRETE_PADRAO = {
    "48h": {
        "titulo": "📅 Faltam 2 dias para o seu agendamento!",
        "corpo": (
            "Olá, {nome_cliente}! Tudo bem?\n\n"
            "Passando para lembrar do seu agendamento no FeedIn marcado para {data_hora}"
            "{info_beneficiario}.\n\n"
            "💡 **Precisa alterar o horário?**\n"
            "Você pode reagendar sem custo até 24 horas antes do atendimento clicando no link: {link_reagendamento}\n\n"
            "Tenha um excelente dia!"
        )
    },
    "24h": {
        "titulo": "⏰ Confirmado: Seu agendamento é amanhã!",
        "corpo": (
            "Oi, {nome_cliente}! Amanhã é o dia do seu atendimento às {apenas_hora}"
            "{info_beneficiario}.\n\n"
            "📌 **Aviso Importante:**\n"
            "Para garantir a melhor experiência e organização da agenda, cancelamentos ou reagendamentos "
            "com menos de 24h de antecedência podem estar sujeitos às regras de retenção do estabelecimento.\n\n"
            "Confira os detalhes e a localização aqui: {link_reagendamento}"
        )
    },
    "3h": {
        "titulo": "🚗 É daqui a pouco! Estamos te esperando.",
        "corpo": (
            "Tudo pronto, {nome_cliente}?\n\n"
            "Seu agendamento é hoje às {apenas_hora}{info_beneficiario}.\n\n"
            "Contamos com a sua pontualidade! Se tiver qualquer imprevisto a caminho, acesse os detalhes e contatos de suporte pelo link: {link_reagendamento}"
        )
    }
}


def formatar_texto_beneficiario(beneficiario_id: int = None) -> str:
    if not beneficiario_id:
        return ""

    beneficiario = ClienteBeneficiario.query.get(beneficiario_id)
    if not beneficiario:
        return ""

    if beneficiario.tipo_persona == 'veiculo':
        doc = f" (Placa/Doc: {beneficiario.documento_identificador})" if beneficiario.documento_identificador else ""
        return f" referente ao veículo {beneficiario.nome}{doc}"

    elif beneficiario.tipo_persona == 'pet':
        return f" referente ao Pet {beneficiario.nome}"

    elif beneficiario.tipo_persona == 'humano':
        return f" para o(a) dependente {beneficiario.nome}"

    return f" para {beneficiario.nome}"


def obter_mensagem_formatada(marco: str, dados: dict, empresa_id: int = None) -> tuple:
    template = TEMPLATES_LEMBRETE_PADRAO.get(marco)

    if not template:
        raise ValueError(f"Marco de gatilho '{marco}' não configurado nos templates.")

    info_beneficiario = formatar_texto_beneficiario(dados.get("beneficiario_id"))

    titulo = template["titulo"]
    corpo = template["corpo"].format(
        nome_cliente=dados.get("nome_cliente", "Cliente"),
        data_hora=dados.get("data_hora", ""),
        apenas_hora=dados.get("apenas_hora", ""),
        info_beneficiario=info_beneficiario,
        link_reagendamento=dados.get("link_reagendamento", "https://feedin.com.br/meus-agendamentos")
    )

    return titulo, corpo


def resolver_canal_preferencial(cliente_id_or_uuid) -> tuple:
    """
    Localiza contatos elegíveis com consentimento LGPD.
    Aaceita tanto o ID legado quanto o UUID.
    """
    uuid_val = _resolver_usuario_uuid(cliente_id_or_uuid)

    # Busca cliente por usuario_id (UUID)
    cliente = ModCadastroCliente.query.filter_by(usuario_id=uuid_val).first() if uuid_val else None
    if not cliente and str(cliente_id_or_uuid).isdigit():
        cliente = ModCadastroCliente.query.get(int(cliente_id_or_uuid))

    if not cliente:
        return None, None

    contatos = ClienteContato.query.filter_by(
        cliente_id=cliente.id,
        aceita_notificacao=True
    ).all()

    if not contatos:
        if getattr(cliente, 'whatsapp', None):
            return 'whatsapp', cliente.whatsapp
        elif getattr(cliente, 'email', None):
            return 'email', cliente.email
        return None, None

    contato_escolhido = next((c for c in contatos if getattr(c, 'is_padrao', False)), contatos[0])
    return contato_escolhido.tipo.lower(), contato_escolhido.valor


def disparar_mensagem_cliente(
        agendamento_id: int,
        cliente_id: str,
        marco_gatilho: str,
        dados_interpolacao: dict,
        empresa_id: int,
        beneficiario_id: int = None
) -> bool:
    """
    Executa o disparo idempotente de lembretes automatizados da AGENDA:
    1. Resolve canal e valida LGPD.
    2. Garante idempotência via trava em `EseLogMensagemAutomatica`.
    3. Persiste a notificação In-App usando obrigatoriamente UUID.
    4. Envia via Gateway.
    """
    uuid_cliente = _resolver_usuario_uuid(cliente_id)
    canal, destinatario = resolver_canal_preferencial(cliente_id)

    if not canal or not destinatario:
        logger.warning(f"[Lembretes] Cliente UUID {uuid_cliente} sem canal ativo para opt-in.")
        return False

    # 1. Trava de Idempotência
    log_envio = EseLogMensagemAutomatica(
        referencia_id=agendamento_id,
        cliente_id=uuid_cliente,
        beneficiario_id=beneficiario_id,
        marco_gatilho=marco_gatilho,
        canal_envio=canal,
        destinatario=destinatario,
        status_envio='processando'
    )

    try:
        db.session.add(log_envio)
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        logger.info(f"[Lembretes] Lembrete '{marco_gatilho}' já processado para Agendamento #{agendamento_id}.")
        return False

    # 2. Formatação da mensagem
    dados_interpolacao['beneficiario_id'] = beneficiario_id
    titulo, mensagem_corpo = obter_mensagem_formatada(marco_gatilho, dados_interpolacao, empresa_id=empresa_id)

    # 3. Notificação In-App da Agenda (AGH) usando UUID
    AghNotificacaoService.criar_notificacao(
        empresa_id=empresa_id,
        destinatario_id=uuid_cliente,
        papel_destinatario='cliente',
        agendamento_id=agendamento_id,
        beneficiario_id=beneficiario_id,
        titulo=titulo,
        mensagem=mensagem_corpo,
        tipo_evento=f"lembrete_{marco_gatilho}",
        nivel="info"
    )

    # 4. Gateway Externo
    sucesso_envio = _enviar_para_gateway_externo(canal, destinatario, titulo, mensagem_corpo)

    if sucesso_envio:
        log_envio.status_envio = 'enviado'
    else:
        log_envio.status_envio = 'falha'
        log_envio.detalhes_erro = 'Falha de comunicação com o gateway externo'

    db.session.commit()
    return sucesso_envio


def _enviar_para_gateway_externo(canal: str, destinatario: str, titulo: str, mensagem: str) -> bool:
    logger.info(f"[GATEWAY DISPARADO] [{canal.upper()}] Para: {destinatario} | Título: {titulo}")
    return True


