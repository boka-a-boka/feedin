import uuid
from datetime import datetime
from feedin import database as db


class PdvComandaItem(db.Model):
    """
    Tabela: pdv_comanda_itens
    Registra os itens (produtos/adicionais) consumidos ou adquiridos no PDV.
    Suporta vendas por colaboradores (com cálculo e snapshot de comissão)
    e auto-aquisição (self-checkout via App/Totem).
    """
    __tablename__ = 'pdv_comanda_itens'

    # Chave Primária e Identificação Única
    id = db.Column(db.Integer, primary_key=True, autoincrement=True, comment="Chave primária do item")
    uuid_item = db.Column(
        db.String(36),
        unique=True,
        nullable=False,
        default=lambda: str(uuid.uuid4()),
        comment="UUID universal do lançamento no PDV"
    )

    # Multi-tenant
    empresa_id = db.Column(db.Integer, db.ForeignKey('ese_empresa.id'), nullable=False, index=True, comment="Multi-tenant ID")

    # 🔗 Elo com a Comanda/Cobrança Central Financeira (fin_cobrancas)
    cobranca_id = db.Column(
        db.BigInteger,
        db.ForeignKey('fin_cobrancas.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
        comment="Vínculo com a comanda principal em fin_cobrancas"
    )

    # 📦 Elo com o Produto no Estoque (est_produtos)
    produto_id = db.Column(
        db.Integer,
        db.ForeignKey('est_produtos.id', ondelete='RESTRICT'),
        nullable=False,
        index=True,
        comment="Produto baixado do catálogo de estoque"
    )

    # 🧑‍🎨 Atribuição de Colaborador e Regra de Comissionamento
    colaborador_id = db.Column(
        db.Integer,
        db.ForeignKey('usuario.id', ondelete='SET NULL'),
        nullable=True,
        index=True,
        comment="Profissional/Colaborador que realizou a venda ou aplicação"
    )
    percentual_comissao = db.Column(
        db.Numeric(5, 2),
        nullable=False,
        default=0.00,
        comment="Snapshot do % de comissão contratado no momento da venda"
    )
    valor_comissao = db.Column(
        db.Numeric(10, 2),
        nullable=False,
        default=0.00,
        comment="Valor monetário gerado de comissão para o colaborador"
    )

    # 📊 Snapshot Financeiro e Quantidades
    quantidade = db.Column(db.Integer, nullable=False, default=1, comment="Quantidade consumida/vendida")
    preco_unitario = db.Column(db.Numeric(10, 2), nullable=False, default=0.00, comment="Preço praticado por unidade")
    valor_total = db.Column(db.Numeric(10, 2), nullable=False, default=0.00, comment="Valor total do item (quantidade * preco_unitario)")

    # 🔍 Auditabilidade, Rastreabilidade e Origem do Lançamento
    origem_lancamento = db.Column(
        db.String(20),
        nullable=False,
        default='COLABORADOR',
        index=True,
        comment="Origem: 'COLABORADOR', 'AUTO_SERVICO', 'BALCAO', 'ECOMMERCE'"
    )
    cpf_hash_cliente = db.Column(db.String(64), nullable=True, index=True, comment="CPF do consumidor final (se identificado)")

    criado_em = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, comment="Data/hora do lançamento")

    # 🔗 Relacionamentos ORM
    cobranca = db.relationship('Cobranca', backref=db.backref('itens_pdv', cascade='all, delete-orphan', lazy='dynamic'))
    produto = db.relationship('ProdutoCatalogo', backref=db.backref('lançamentos_pdv', lazy='dynamic'))
    colaborador = db.relationship('Usuario', foreign_keys=[colaborador_id])

    def to_dict(self):
        """Serialização simples para respostas da API em JSON"""
        return {
            'id': self.id,
            'uuid_item': self.uuid_item,
            'cobranca_id': self.cobranca_id,
            'produto_id': self.produto_id,
            'produto_nome': self.produto.nome if self.produto else None,
            'quantidade': self.quantidade,
            'preco_unitario': float(self.preco_unitario),
            'valor_total': float(self.valor_total),
            'colaborador_id': self.colaborador_id,
            'colaborador_nome': self.colaborador.nome if self.colaborador else None,
            'valor_comissao': float(self.valor_comissao),
            'origem_lancamento': self.origem_lancamento,
            'criado_em': self.criado_em.isoformat()
        }