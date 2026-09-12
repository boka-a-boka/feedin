# app/services/worker_lembretes.py

from datetime import datetime, timedelta
import logging

from app import create_app  # Seu factory do Flask
from app.models import db, Agendamento, EseLogMensagemAutomatica
from app.services.notificacao_service import obter_mensagem_formatada, disparar_mensagem_cliente

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def processar_regua_lembretes():
    """
    Varre os agendamentos próximos e dispara lembretes (48h, 24h, 3h).
    Devidamente protegido contra envios duplicados via EseLogMensagemAutomatica.
    """
    agora = datetime.utcnow()

    # Tolerância de janela de busca (ex: 15 min para mais ou para menos)
    janelas = {
        'lembrete_48h': (agora + timedelta(hours=47, minutes=45), agora + timedelta(hours=48, minutes=15)),
        'lembrete_24h': (agora + timedelta(hours=23, minutes=45), agora + timedelta(hours=24, minutes=15)),
        'lembrete_3h': (agora + timedelta(hours=2, minutes=45), agora + timedelta(hours=3, minutes=15))
    }

    for marco, (inicio_janela, fim_janela) in janelas.items():
        # Busca agendamentos ativos/confirmados dentro do intervalo de tempo
        agendamentos = Agendamento.query.filter(
            Agendamento.data_hora >= inicio_janela,
            Agendamento.data_hora <= fim_janela,
            Agendamento.status == 'confirmado'
        ).all()

        for ag in agendamentos:
            # Garante a idempotência (evita disparar 2x o mesmo marco para o mesmo agendamento)
            ja_enviado = EseLogMensagemAutomatica.query.filter_by(
                referencia_id=ag.id,
                marco_gatilho=marco
            ).first()

            if not ja_enviado:
                try:
                    disparar_mensagem_cliente(ag, marco)
                    logger.info(f"[{marco}] Lembrete enviado com sucesso | Agendamento ID: {ag.id}")
                except Exception as e:
                    logger.error(f"Erro ao enviar {marco} para Agendamento ID {ag.id}: {str(e)}")


if __name__ == "__main__":
    # Inicializa o contexto do Flask para ter acesso ao Banco de Dados (SQLAlchemy)
    app = create_app()
    with app.app_context():
        logger.info("Iniciando varredura de lembretes automáticos...")
        processar_regua_lembretes()
        logger.info("Varredura concluída.")