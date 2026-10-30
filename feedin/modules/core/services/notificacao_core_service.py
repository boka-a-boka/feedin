import logging
import threading
from flask import current_app
from feedin import database as db
from feedin.models import CoreDepartamento, CoreStaff, CoreAcaoPendente

logger = logging.getLogger(__name__)


class CoreNotificacaoService:

    @staticmethod
    def registrar_acao_e_notificar(
        modulo_id: int,
        entidade_id: int,
        departamento_codigo: str,
        titulo: str,
        descricao: str,
        payload: dict = None
    ) -> CoreAcaoPendente:
        """
        Registra uma nova pendência na fila unificada do Core e despacha os alertas
        em background para todos os colaboradores lotados no departamento responsável.
        """
        try:
            # 1. Localiza o departamento pelo código (ex: 'homologacao', 'comercial')
            departamento = CoreDepartamento.query.filter_by(codigo=departamento_codigo, ativo=True).first()
            if not departamento:
                logger.error(f"[CORE NOTIFICAÇÃO] Departamento com código '{departamento_codigo}' não encontrado ou inativo.")
                return None

            # 2. Cria a pendência na fila do Core
            acao = CoreAcaoPendente(
                modulo_id=modulo_id,
                entidade_id=entidade_id,
                departamento_id=departamento.id,
                titulo=titulo,
                descricao=descricao,
                payload_dados=payload or {},
                status='pendente',
                notificado=False
            )
            db.session.add(acao)
            db.session.commit()

            # 3. Dispara as notificações em background para o staff do departamento
            app_instance = current_app._get_current_object()
            thread = threading.Thread(
                target=CoreNotificacaoService._processar_disparos_staff_bg,
                args=(app_instance, acao.id)
            )
            thread.daemon = True
            thread.start()

            return acao
        except Exception as e:
            db.session.rollback()
            logger.error(f"[CORE NOTIFICAÇÃO] Erro ao registrar ação pendente: {e}", exc_info=True)
            return None

    @staticmethod
    def _processar_disparos_staff_bg(app, acao_id: int):
        with app.app_context():
            acao = db.session.get(CoreAcaoPendente, acao_id)
            if not acao or acao.notificado:
                return

            # Busca todos os membros do staff ativos lotados no departamento da ação
            membros_staff = CoreStaff.query.join(CoreStaff.departamentos).filter(
                CoreStaff.ativo.is_(True),
                CoreDepartamento.id == acao.departamento_id
            ).all()

            if not membros_staff:
                logger.warning(
                    f"[CORE NOTIFICAÇÃO] Nenhum membro do staff ativo encontrado no departamento ID #{acao.departamento_id}.")
                return

            # Monta a mensagem base de alerta
            mensagem_base = (
                f"🚨 *NOVA PENDÊNCIA NO SISTEMA*\n\n"
                f"🏷️ *Ação:* {acao.titulo}\n"
                f"📝 *Detalhes:* {acao.descricao}\n"
            )

            # Se houver payload extra (ex: espaço de anúncio, token de documentos), podemos anexar de forma elegante
            if acao.payload_dados:
                mensagem_base += f"\n📊 *Dados Adicionais:* {acao.payload_dados}\n"

            mensagem_base += f"\nAcesse o painel administrativo do Core para realizar a tratativa."

            sucesso_geral = True
            for membro in membros_staff:
                try:
                    # Recupera o nome de exibição do usuário vinculado ao staff
                    nome_membro = membro.usuario.username if membro.usuario else "Staff"

                    # Disparo via WhatsApp (se cadastrado)
                    if membro.whatsapp:
                        logger.info(f"[CORE WHATSAPP] Enviando para {nome_membro} ({membro.whatsapp})")
                        # _enviar_whatsapp_gateway(membro.whatsapp, mensagem_base)

                    # Disparo via E-mail (utilizando o e-mail profissional do staff ou do usuário)
                    email_destino = membro.email_profissional or (membro.usuario.email if membro.usuario else None)
                    if email_destino:
                        logger.info(f"[CORE E-MAIL] Enviando para {nome_membro} ({email_destino})")
                        # _enviar_email_smtp(email_destino, acao.titulo, mensagem_base)

                except Exception as ex:
                    sucesso_geral = False
                    nome_membro = membro.usuario.username if membro.usuario else "Staff"
                    logger.error(f"❌ Erro ao notificar membro do staff {nome_membro}: {ex}")

            if sucesso_geral:
                acao.notificado = True
                db.session.commit()