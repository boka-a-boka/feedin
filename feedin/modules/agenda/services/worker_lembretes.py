import logging
from datetime import datetime, timedelta, timezone
import click
from flask import current_app, url_for
from flask.cli import with_appcontext

from feedin import database as db
from feedin.modules.agenda.services.notificacao_service import disparar_mensagem_cliente
from feedin.modules.agenda.models import AghAgendamento

logger = logging.getLogger(__name__)

# Configuração dos marcos de disparo (em horas) e tolerância (em minutos)
MARCOS_GATILHO = {
    "48h": 48,
    "24h": 24,
    "3h": 3,
}
TOLERANCIA_MINUTOS = 15


def buscar_agendamentos_na_janela(horas_antecedencia: int, agora: datetime):
    """
    Calcula a janela de tempo esperada [alvo - tolerancia, alvo + tolerancia]
    e busca os agendamentos ativos que recaem nesse intervalo.
    """
    alvo = agora + timedelta(hours=horas_antecedencia)
    inicio_janela = alvo - timedelta(minutes=TOLERANCIA_MINUTOS)
    fim_janela = alvo + timedelta(minutes=TOLERANCIA_MINUTOS)

    # AJUSTADO: Utiliza data_hora_inicio (nome oficial no model AghAgendamento)
    query = AghAgendamento.query.filter(
        AghAgendamento.data_hora_inicio >= inicio_janela,
        AghAgendamento.data_hora_inicio <= fim_janela,
        AghAgendamento.status.in_(["confirmado", "agendado"])
    )

    return query.all()


def processar_varredura_lembretes():
    """
    Executa o ciclo completo de varredura para cada marco configurado (48h, 24h, 3h).
    Retorna um dicionário resumido com as estatísticas do processamento.
    """
    agora = datetime.now(timezone.utc)
    estatisticas = {"processados": 0, "enviados": 0, "ignorados": 0, "erros": 0}

    logger.info(f"[WORKER LEMBRETES] Iniciando varredura às {agora.strftime('%Y-%m-%d %H:%M:%S UTC')}")

    for marco, horas in MARCOS_GATILHO.items():
        agendamentos = buscar_agendamentos_na_janela(horas, agora)
        logger.info(f"[WORKER LEMBRETES] Marco '{marco}': {len(agendamentos)} agendamento(s) encontrado(s).")

        for agendamento in agendamentos:
            estatisticas["processados"] += 1

            try:
                # AJUSTADO: data_hora_inicio e sintaxe correta de minutos (%H:%M)
                dt_formatada = agendamento.data_hora_inicio.strftime("%d/%m/%Y às %H:%M")
                hora_formatada = agendamento.data_hora_inicio.strftime("%H:%M")

                # Resolve slug da empresa de forma segura
                slug_empresa = (
                    agendamento.empresa.slug
                    if (hasattr(agendamento, 'empresa') and agendamento.empresa and getattr(agendamento.empresa, 'slug', None))
                    else "geral"
                )

                # AJUSTADO: URL relativa/absoluta dinâmica usando a rota de reagendamento do Flask
                try:
                    link_reagendamento = url_for(
                        'agenda.agendar',
                        slug_empresa=slug_empresa,
                        reagendar_id=agendamento.id,
                        _external=True
                    )
                except Exception:
                    link_reagendamento = f"/agenda/{slug_empresa}/agendar?reagendar_id={agendamento.id}"

                # Trata a informação do beneficiário (Pet/Veículo/Dependente) caso exista
                info_beneficiario = ""
                if getattr(agendamento, "beneficiario", None) and getattr(agendamento.beneficiario, "nome", None):
                    info_beneficiario = f" (para {agendamento.beneficiario.nome})"

                # Trata nome do cliente
                nome_cliente = "Cliente"
                if hasattr(agendamento, "cliente") and agendamento.cliente:
                    nome_cliente = (
                        getattr(agendamento.cliente, "nome_completo", None) or
                        getattr(agendamento.cliente, "nome", "Cliente")
                    ).split()[0]

                dados_interpolacao = {
                    "nome_cliente": nome_cliente,
                    "data_hora": dt_formatada,
                    "apenas_hora": hora_formatada,
                    "info_beneficiario": info_beneficiario,
                    "link_reagendamento": link_reagendamento
                }

                # Recupera beneficiario_id se existir no registro
                beneficiario_id = getattr(agendamento, "beneficiario_id", None)

                # Disparo Híbrido
                sucesso = disparar_mensagem_cliente(
                    agendamento_id=agendamento.id,
                    cliente_id=agendamento.cliente_id,  # UUID String(36)
                    marco_gatilho=marco,
                    dados_interpolacao=dados_interpolacao,
                    beneficiario_id=beneficiario_id
                )

                if sucesso:
                    estatisticas["enviados"] += 1
                else:
                    estatisticas["ignorados"] += 1

            except Exception as e:
                estatisticas["erros"] += 1
                logger.error(
                    f"[WORKER LEMBRETES] Erro ao processar Agendamento #{getattr(agendamento, 'id', 'N/A')} "
                    f"no marco '{marco}': {str(e)}",
                    exc_info=True
                )

    logger.info(
        f"[WORKER LEMBRETES] Varredura concluída. "
        f"Enviados: {estatisticas['enviados']} | "
        f"Ignorados/Duplicados: {estatisticas['ignorados']} | "
        f"Erros: {estatisticas['erros']}"
    )

    return estatisticas


# -----------------------------------------------------------------------------
# COMANDOS CLI DO FLASK (Para agendamento via Cron/Systemd no Linux)
# -----------------------------------------------------------------------------
@click.command("executar-lembretes")
@with_appcontext
def executar_lembretes_command():
    """Comando da CLI para varredura e envio de lembretes automáticos."""
    click.echo("⏰ Iniciando o robô de varredura de lembretes...")
    resumo = processar_varredura_lembretes()
    click.echo(
        f"✅ Concluído! Enviados: {resumo['enviados']} | "
        f"Ignorados/Já processados: {resumo['ignorados']} | "
        f"Erros: {resumo['erros']}"
    )


def init_app(app):
    """Registra o comando CLI na aplicação Flask."""
    app.cli.add_command(executar_lembretes_command)