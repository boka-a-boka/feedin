import threading
from flask import current_app
from feedin.modules.agenda.services.whatsapp_service import WhatsappService

def disparar_comprovante_background(app, agendamento_id: int):
    """Executa a task de envio garantindo o contexto da aplicação ativa."""
    with app.app_context():
        WhatsappService.enviar_comprovante_agendamento(agendamento_id)

def dispatch_comprovante_whatsapp(agendamento_id: int):
    """
    Função pública para ser chamada na rota/service logo após dar o db.session.commit().
    Dispara em uma thread separada sem bloquear a resposta HTTP.
    """
    app = current_app._get_current_object()
    thread = threading.Thread(
        target=disparar_comprovante_background,
        args=(app, agendamento_id)
    )
    thread.daemon = True
    thread.start()