import logging
from sqlalchemy import inspect
from sqlalchemy.event import listens_for
# Ajuste as importações conforme os seus caminhos reais

# 📦 Importações ajustadas exatamente às suas models fornecidas:
from feedin.modules.empresa.models import (ColaboradorContrato,)
from feedin.modules.auth.models import ModCadastroCliente
from feedin.modules.agenda.services.notificacao_service import AghNotificacaoService

logger = logging.getLogger(__name__)

from datetime import datetime, timezone
from feedin import database as db
from decimal import Decimal

class EntradaEstoqueItem(db.Model):
    """
    Histórico de Entradas, Compras e Reposições de Estoque.
    Garante rastreabilidade de custos variáveis e auditoria de compras.
    """
    __tablename__ = 'est_entradas_itens'

    id = db.Column(db.Integer, primary_key=True)
    empresa_id = db.Column(db.Integer, nullable=False, index=True)
    produto_id = db.Column(db.Integer, db.ForeignKey('est_produtos.id'), nullable=False, index=True)

    # Dados da Compra
    quantidade = db.Column(db.Integer, nullable=False)
    preco_custo_unitario = db.Column(db.Numeric(10, 2), nullable=False) # Valor pago nesta compra
    valor_total_compra = db.Column(db.Numeric(10, 2), nullable=False)   # quantidade * preco_custo_unitario

    # Rastreabilidade e Auditoria
    fornecedor_nome = db.Column(db.String(150), nullable=True) # Ex: "Atacadão", "Distribuidora Beauty"
    numero_nota_fiscal = db.Column(db.String(50), nullable=True)
    data_compra = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), index=True)
    observacao = db.Column(db.String(255), nullable=True) # Ex: "Lote promocional de Black Friday"

    # Usuário/Colaborador que deu entrada no sistema
    usuario_registro_id = db.Column(db.Integer, db.ForeignKey('usuario.id'), nullable=True)


class ProdutoCatalogo(db.Model):
    """Domínio de Catálogo de Produtos integrado à Taxonomia Geral"""
    __tablename__ = 'est_produtos'

    id = db.Column(db.Integer, primary_key=True)
    empresa_id = db.Column(db.Integer, nullable=False, index=True)
    codigo_barras = db.Column(db.String(50), nullable=True, index=True)
    nome = db.Column(db.String(150), nullable=False)

    # Preço de Venda Praticado no Balcão/E-commerce
    preco_venda = db.Column(db.Numeric(10, 2, asdecimal=True), nullable=False, default=Decimal('0.00'))

    # Controle Físico de Saldo
    estoque_atual = db.Column(db.Integer, nullable=False, default=0)
    estoque_minimo = db.Column(db.Integer, nullable=False, default=5)

    # Indicadores Financeiros Rápidos
    custo_medio = db.Column(db.Numeric(10, 2, asdecimal=True), nullable=False, default=Decimal('0.00'))
    preco_custo = db.Column(db.Numeric(10, 2, asdecimal=True), nullable=False, default=Decimal('0.00'))

    # 🏷️ ELO DIRETO COM A TAXONOMIA CENTRAL
    categoria_id = db.Column(db.Integer, db.ForeignKey('taxonomia.id'), nullable=True, index=True)
    subcategoria_id = db.Column(db.Integer, db.ForeignKey('taxonomia.id'), nullable=True, index=True)
    tags_busca = db.Column(db.String(255), nullable=True)

    data_criacao = db.Column(db.DateTime, default=datetime.utcnow)

    # Relacionamentos
    categoria = db.relationship('Taxonomia', foreign_keys=[categoria_id], backref='produtos_categoria')
    subcategoria = db.relationship('Taxonomia', foreign_keys=[subcategoria_id], backref='produtos_subcategoria')
    entradas = db.relationship('EntradaEstoqueItem', backref='produto', lazy='dynamic')

    @property
    def ultimo_preco_custo(self):
        """Retorna o valor pago na última movimentação de entrada/compra deste produto."""
        ultima_compra = EntradaEstoqueItem.query.filter_by(
            produto_id=self.id,
            tipo_movimentacao='entrada'
        ).order_by(EntradaEstoqueItem.data_movimentacao.desc()).first()

        if ultima_compra and ultima_compra.preco_custo_unitario:
            return Decimal(str(ultima_compra.preco_custo_unitario))

        return self.preco_custo or Decimal('0.00')


@listens_for(ProdutoCatalogo, 'before_update')
def verificar_estoque_minimo_listener(mapper, connection, target):
    """
    Listener do SQLAlchemy acionado antes de atualizar um ProdutoCatalogo.
    Garante disparo ÚNICO quando o produto atinge ou fica abaixo do estoque mínimo.
    """
    # 1. Inspeciona o estado do objeto antes de salvar no banco
    state = inspect(target)
    history = state.get_history('estoque_atual', passive=True)

    # Se a coluna 'estoque_atual' não foi alterada nesta transação, ignora
    if not history.has_changes():
        return

    # history.deleted traz o valor antes do update; history.added traz o valor novo
    estoque_antigo = history.deleted[0] if history.deleted else target.estoque_atual
    estoque_novo = target.estoque_atual
    estoque_minimo = target.estoque_minimo or 0

    # 🎯 TRAVA DE DISPARO ÚNICO (Idempotência):
    # Só dispara se o estoque ESTAVA acima do mínimo E AGORA ficou menor ou igual ao mínimo.
    condicao_disparo = (estoque_antigo > estoque_minimo) and (estoque_novo <= estoque_minimo)

    if condicao_disparo:
        try:
            # 2. Busca os colaboradores administradores (papel_nivel >= 666) da empresa
            # fazendo o Join pelo ID da empresa e trazendo as credenciais de cadastro.
            contratos_admins = (
                db.session.query(ColaboradorContrato)
                .join(ModCadastroCliente, ColaboradorContrato.id_cadastro_cliente == ModCadastroCliente.id)
                .filter(
                    ColaboradorContrato.id_local == target.empresa_id,
                    ColaboradorContrato.papel_nivel >= 666,
                    ColaboradorContrato.status_profissional == 'ativo',
                    ColaboradorContrato.data_desligamento.is_(None)
                ).all()
            )

            if not contratos_admins:
                logger.warning(
                    f"⚠️ [ESTOQUE MÍNIMO] Produto '{target.nome}' (ID: {target.id}) atingiu o mínimo "
                    f"({estoque_novo}/{estoque_minimo}), mas nenhum colaborador administrador (nível >= 666) "
                    f"ativo foi encontrado para a empresa #{target.empresa_id}."
                )
                return

            # 3. Monta o texto padronizado do alerta
            titulo = f"🚨 Alerta de Estoque Mínimo: {target.nome}"
            mensagem = (
                f"O produto **{target.nome}** (Código: {target.codigo_barras or 'S/N'}) "
                f"atingiu o limite mínimo de segurança.\n\n"
                f"📦 **Estoque Atual:** {estoque_novo} un\n"
                f"⚠️ **Estoque Mínimo Defenido:** {estoque_minimo} un\n\n"
                f"Recomendamos realizar o pedido de compra para reposição do estoque."
            )

            payload_extra = {
                'produto_id': target.id,
                'produto_nome': target.nome,
                'estoque_atual': estoque_novo,
                'estoque_minimo': estoque_minimo,
                'modulo_destino': 'empresa'
            }

            # 4. Dispara a notificação para o UUID (36 chars) de cada administrador
            notificados_count = 0
            for contrato in contratos_admins:
                # Obtém o UUID CHAR(36) via property oficial da model
                uuid_destinatario = contrato.uuid_colaborador

                if not uuid_destinatario:
                    continue

                # Notificação In-App para o Painel/Sininho
                AghNotificacaoService.criar_notificacao(
                    empresa_id=target.empresa_id,
                    destinatario_id=uuid_destinatario,  # UUID v4 String(36)
                    papel_destinatario='gestor',
                    agendamento_id=None,
                    titulo=titulo,
                    mensagem=mensagem,
                    tipo_evento="alerta_estoque_minimo",
                    nivel="warning",
                    payload_extra=payload_extra
                )

                # Disparo externo via canal preferido (WhatsApp se houver número cadastrado)
                _notificar_canal_externo_admin(contrato.cadastro_modulo, mensagem)
                notificados_count += 1

            logger.info(
                f"✅ [ESTOQUE MÍNIMO] Notificação disparada com sucesso para {notificados_count} "
                f"administrador(es) da empresa #{target.empresa_id} (Produto: '{target.nome}')."
            )

        except Exception as err:
            logger.error(
                f"❌ Erro ao processar listener de estoque mínimo para o produto #{target.id}: {str(err)}",
                exc_info=True
            )


def _notificar_canal_externo_admin(cadastro_cliente: ModCadastroCliente, mensagem_texto: str):
    """Encaminha o alerta para o WhatsApp do administrador, caso cadastrado no perfil."""
    if not cadastro_cliente or not cadastro_cliente.whatsapp:
        return

    try:
        from feedin.modules.agenda.services.dispatchers import dispatch_mensagem_whatsapp_generica
        dispatch_mensagem_whatsapp_generica(
            telefone=cadastro_cliente.whatsapp,
            mensagem=mensagem_texto
        )
    except Exception as err_disp:
        logger.error(f"⚠️ Erro ao enviar WhatsApp de estoque mínimo para {cadastro_cliente.nome}: {err_disp}")