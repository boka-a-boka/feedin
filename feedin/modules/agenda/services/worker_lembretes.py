import logging
from datetime import datetime, timedelta, timezone
import click
from flask import current_app
from flask.cli import with_appcontext

from feedin import database as db
from feedin.modules.agenda.services.notificacao_service import disparar_mensagem_cliente

# Supondo que a model de Agendamento/Pedido no seu ecossistema seja Agendamento
# Ajuste a importação da classe de Agendamento conforme a estrutura do projeto:
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

    # -------------------------------------------------------------------------
    # NOTA DE INTEGRAÇÃO COM MODEL DE AGENDAMENTO:
    # Substitua 'Agendamento' abaixo pela classe correspondente (ex: ModAgendamento / Pedido)
    # -------------------------------------------------------------------------

    query = AghAgendamento.query.filter(
        AghAgendamento.data_agendamento >= inicio_janela,
        AghAgendamento.data_agendamento <= fim_janela,
        AghAgendamento.status.in_(["confirmado", "agendado"])  # Filtra agendamentos válidos
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
                # Prepara dicionário para interpolação
                dt_formatada = agendamento.data_agendamento.strftime("%d/%m/%Y às %H:%H")
                hora_formatada = agendamento.data_agendamento.strftime("%H:%M")

                dados_interpolacao = {
                    "nome_cliente": agendamento.cliente.nome_completo if hasattr(agendamento.cliente, "nome_completo") else agendamento.cliente.nome,
                    "data_hora": dt_formatada,
                    "apenas_hora": hora_formatada,
                    "link_reagendamento": f"https://feedin.com.br/agendamentos/{agendamento.id}/reagendar"
                }

                # Recupera beneficiario_id se existir no registro do agendamento
                beneficiario_id = getattr(agendamento, "beneficiario_id", None)

                # Disparo Híbrido (Notificação PWA + Log de Idempotência + Gateway Externo)
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