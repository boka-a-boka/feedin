import uuid
import threading
import smtplib
import logging
from sqlalchemy.exc import IntegrityError
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
from flask import current_app

from feedin import database as db
from feedin.modules.auth.models import ModCadastroCliente
from feedin.modules.agenda.models import AghNotificacao, AghAgendamento
from feedin.modules.empresa.models import (
    ClienteBeneficiario,
    ClienteContato,
    EseNotificacaoCliente,
    EseLogMensagemAutomatica
)

logger = logging.getLogger(__name__)

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
        Método genérico para persistir notificações internas no banco (AGH).
        """
        try:
            notificacao = AghNotificacao(
                id=str(uuid.uuid4()),  # 👈 AJUSTE CRÍTICO: Garante a geração explícita do UUID CHAR(36)
                empresa_id=empresa_id,
                destinatario_id=str(destinatario_id).strip() if destinatario_id else None,
                papel_destinatario=papel_destinatario,
                remetente_id=str(remetente_id).strip() if remetente_id else None,
                agendamento_id=agendamento_id,
                beneficiario_id=beneficiario_id,
                titulo=titulo,
                mensagem=mensagem,
                tipo_evento=tipo_evento,
                nivel=nivel,
                payload_extra=payload_extra or {},
                lida=False,  # Garante valor inicial para a consulta do 'sininho'
                criado_em=datetime.utcnow()
            )
            db.session.add(notificacao)
            db.session.commit()
            return notificacao
        except Exception as e:
            db.session.rollback()
            print(f"[ERRO AGHNotificacaoService]: Falha ao registrar notificação - {str(e)}")
            return None

    # 👈 ALIAS DE COMPATIBILIDADE: Redireciona chamadas .disparar() para .criar_notificacao()
    @classmethod
    def disparar(cls, *args, **kwargs):
        return cls.criar_notificacao(*args, **kwargs)

    @staticmethod
    def notificar_falta_cliente(agendamento_id: int, colaborador_usuario_id: str = None, colaborador_nome: str = None,
                                motivo: str = "ausencia_cliente"):
        """
        1. CRÍTICO & OBRIGATÓRIO: Atualiza o status do agendamento e grava no módulo (AghNotificacao).
        2. OPCIONAL/FALLBACK: Caso o cliente tenha canal preferencial cadastrado, envia via WhatsApp/E-mail/SMS.
        """
        agendamento = AghAgendamento.query.get(agendamento_id)
        if not agendamento:
            return None

        # 👈 AJUSTE 2: Atualiza o status do agendamento em caixa alta
        agendamento.status = 'FALTA'
        if hasattr(agendamento, 'atualizado_em'):
            agendamento.atualizado_em = datetime.utcnow()
        db.session.commit()

        # Identifica beneficiário se houver (Pet, Veículo, Dependente)
        beneficiario_info = ""
        beneficiario_id = getattr(agendamento, 'beneficiario_id', None)
        if beneficiario_id:
            beneficiario = ClienteBeneficiario.query.get(beneficiario_id)
            if beneficiario:
                if getattr(beneficiario, 'tipo_persona', None) == 'pet':
                    beneficiario_info = f" (Pet: {beneficiario.nome})"
                elif getattr(beneficiario, 'tipo_persona', None) == 'veiculo':
                    beneficiario_info = f" (Veículo: {beneficiario.nome})"
                else:
                    beneficiario_info = f" ({beneficiario.nome})"

        # Formatação de datas
        data_agendamento_fmt = agendamento.data_hora_inicio.strftime("%d/%m/%Y às %H:%M") if getattr(agendamento,
                                                                                                     'data_hora_inicio',
                                                                                                     None) else ""
        agora_apontamento_fmt = datetime.now().strftime("%d/%m/%Y às %H:%M")

        nome_operador = colaborador_nome or "Colaborador"

        # 👈 AJUSTE 3: Garante o uso do usuario_id (string/UUID) para o destinatário
        usuario_destinatario_id = getattr(agendamento.cliente, 'usuario_id', None) or agendamento.cliente_id

        # =========================================================================
        # 📌 PASSO 1: NOTIFICAÇÃO DO MÓDULO (SEMPRE GARANTIDA)
        # =========================================================================
        mensagem_notif = (
            f"Foi registrada uma falta referente ao seu agendamento de {data_agendamento_fmt}{beneficiario_info}.\n"
            f"Apontamento realizado por: {nome_operador} em {agora_apontamento_fmt}."
        )

        notificacao_interna = AghNotificacaoService.criar_notificacao(
            empresa_id=agendamento.empresa_id,
            destinatario_id=usuario_destinatario_id,
            papel_destinatario='cliente',
            remetente_id=colaborador_usuario_id or (
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

        # =========================================================================
        # 📌 PASSO 2: COMUNICAÇÃO EXTERNA (WHATSAPP / EMAIL / SMS)
        # =========================================================================
        contato_preferencial = ClienteContato.query.filter_by(
            cliente_id=agendamento.cliente_id,
            aceita_notificacao=True
        ).order_by(ClienteContato.is_padrao.desc()).first()

        if not contato_preferencial:
            print(
                f"ℹ️ [AGH NOTIFICAÇÃO] Notificação interna gerada ({notificacao_interna.id if notificacao_interna else 'OK'}). "
                f"Cliente ID '{agendamento.cliente_id}' não possui canais externos habilitados."
            )
            return True

        # Se houver canal cadastrado, dispara o canal de preferência em segundo plano
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
        """
        Executado em segundo plano (Worker/Thread).
        Acessa as configurações de SMTP/Gateways do Flask.
        """
        with app.app_context():
            try:
                if tipo_canal == 'email':
                    AghNotificacaoService._enviar_email_smtp(app, destino, assunto, mensagem)
                elif tipo_canal in ['whatsapp', 'sms']:
                    AghNotificacaoService._enviar_mensageria_api(app, tipo_canal, destino, mensagem)
            except Exception as e:
                print(f"[ERRO DISPARO SEGUNDO PLANO] Falha ao enviar por {tipo_canal} para {destino}: {str(e)}")

    @staticmethod
    def _enviar_email_smtp(app, email_destino: str, assunto: str, mensagem: str):
        """
        Envio real de E-mail via SMTP configurado no app Flask.
        """
        smtp_server = app.config.get('MAIL_SERVER', 'smtp.gmail.com')
        smtp_port = app.config.get('MAIL_PORT', 587)
        smtp_user = app.config.get('MAIL_USERNAME')
        smtp_pass = app.config.get('MAIL_PASSWORD')
        sender_email = app.config.get('MAIL_DEFAULT_SENDER', smtp_user)

        if not smtp_user or not smtp_pass:
            print(
                f"⚠️ [EMAIL OMITIDO] Credenciais de e-mail (MAIL_USERNAME/MAIL_PASSWORD) não configuradas no app.config.")
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

        print(f"📧 [E-MAIL ENVIADO] Mensagem entregue com sucesso para {email_destino}")

    @staticmethod
    def _enviar_mensageria_api(app, tipo: str, numero: str, mensagem: str):
        """
        Disparo para API de WhatsApp (Ex: Z-API / Evolution API) ou SMS (Twilio).
        """
        api_url = app.config.get('WHATSAPP_API_URL')
        api_token = app.config.get('WHATSAPP_API_TOKEN')

        if api_url and api_token:
            import requests
            headers = {'Client-Token': api_token, 'Content-Type': 'application/json'}
            payload = {'phone': numero, 'message': mensagem}
            response = requests.post(api_url, json=payload, headers=headers, timeout=10)
            print(f"📱 [{tipo.upper()} API] Resposta da integração: {response.status_code}")
        else:
            print(f"📱 [{tipo.upper()} SIMULADO] Destino: {numero} | Conteúdo: {mensagem}")

    @staticmethod
    def contar_nao_lidas(empresa_id: int, usuario_id: str, papel: str) -> int:
        return AghNotificacao.query.filter_by(
            empresa_id=empresa_id,
            destinatario_id=str(usuario_id),
            papel_destinatario=papel,
            lida=False
        ).count()


# =========================================================================
# 📝 TEMPLATES DINÂMICOS DE LEMBRETES
# =========================================================================
TEMPLATES_LEMBRETE = {
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

# -----------------------------------------------------------------------------
# TEMPLATES GLOBAIS PADRÃO (FEEDIN)
# -----------------------------------------------------------------------------
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
    """
    Formata o texto complementar caso o serviço esteja atrelado a um beneficiário
    (Veículo/Placa, Pet ou Dependente Humano).
    """
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
    """
    Interpolador de templates.
    Atualmente busca do dicionário global.
    (Futuramente buscará da tabela `EmpresaTemplateMensagem` filtrando por empresa_id).
    """
    # 📌 PREPARADO PARA O FUTURO: Se empresa_id tiver template customizado no DB, usará ele.
    # Caso contrário (ou agora no MVP), usa o fallback global.
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


def resolver_canal_preferencial(cliente_id: str) -> tuple:
    """
    Mapeia os canais reais da tabela `cliente_contatos`.
    Garante compliance LGPD testando `aceita_notificacao == True`.

    Retorna: (tipo_canal, valor) ex: ('whatsapp', '19998765432')
    """
    # 1. Busca contatos ativos com opt-in LGPD
    contatos = ClienteContato.query.filter_by(
        cliente_id=cliente_id,
        aceita_notificacao=True
    ).all()

    # 2. Fallback: Se não tiver em `cliente_contatos`, tenta ler o WhatsApp/Email direto do cadastro
    if not contatos:
        cliente = ModCadastroCliente.query.get(cliente_id)
        if cliente:
            if cliente.whatsapp:
                return 'whatsapp', cliente.whatsapp
            elif cliente.email:
                return 'email', cliente.email
        return None, None

    # 3. Dá prioridade ao marcado como `is_padrao`
    contato_escolhido = next((c for c in contatos if c.is_padrao), None)

    if not contato_escolhido:
        contato_escolhido = contatos[0]

    return contato_escolhido.tipo.lower(), contato_escolhido.valor


def disparar_mensagem_cliente(
    agendamento_id: int,
    cliente_id: str,
    marco_gatilho: str,
    dados_interpolacao: dict,
    beneficiario_id: int = None
) -> bool:
    """
    Executa o disparo híbrido e idempotente:
    1. Resolve canal e valida LGPD.
    2. Bloqueia duplicidade via trava no banco (`EseLogMensagemAutomatica`).
    3. Cria notificação PWA In-App.
    4. Dispara API externa via Gateway.
    """
    canal, destinatario = resolver_canal_preferencial(cliente_id)

    if not canal or not destinatario:
        logger.warning(f"[Lembretes] Cliente UUID {cliente_id} sem canal de contato com opt-in válido.")
        return False

    # 1. Idempotência: Garante registro de intenção no banco
    log_envio = EseLogMensagemAutomatica(
        referencia_id=agendamento_id,
        cliente_id=cliente_id,
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
        logger.info(f"[Lembretes] Lembrete '{marco_gatilho}' já enviado para Agendamento #{agendamento_id} via {canal}.")
        return False

    # 2. Interpola os dados e insere beneficiario_id no contexto
    dados_interpolacao['beneficiario_id'] = beneficiario_id
    titulo, mensagem_corpo = obter_mensagem_formatada(marco_gatilho, dados_interpolacao)

    # 3. Cria Notificação In-App no PWA (`EseNotificacaoCliente`)
    notificacao_pwa = EseNotificacaoCliente(
        cliente_id=cliente_id,
        referencia_id=agendamento_id,
        titulo=titulo,
        mensagem=mensagem_corpo
    )
    db.session.add(notificacao_pwa)

    # 4. Envio Externo
    sucesso_envio = _enviar_para_gateway_externo(canal, destinatario, titulo, mensagem_corpo)

    if sucesso_envio:
        log_envio.status_envio = 'enviado'
    else:
        log_envio.status_envio = 'falha'
        log_envio.detalhes_erro = 'Falha no gateway externo'

    db.session.commit()
    return sucesso_envio


def _enviar_para_gateway_externo(canal: str, destinatario: str, titulo: str, mensagem: str) -> bool:
    """Mock para gateways (Z-API, Evolution, SendGrid, etc)."""
    logger.info(f"[GATEWAY DISPARADO] [{canal.upper()}] Para: {destinatario} | Titulo: {titulo}")
    return True


