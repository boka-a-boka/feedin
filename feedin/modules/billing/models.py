"""
Modelos de Dados do Módulo Financeiro
Prefixo das Tabelas: fin_
"""

import uuid
from datetime import datetime
from feedin import database as db
from sqlalchemy import func

from feedin.modules.pdv.models import (PdvComandaItem)

from feedin.modules.agenda.models import (AghAgendamento)


class Cobranca(db.Model):
    """
    Tabela Principal: fin_cobrancas
    Representa a comanda/documento de cobrança de qualquer módulo de origem.
    """
    __tablename__ = 'fin_cobrancas'
    __table_args__ = {'extend_existing': True}

    id = db.Column(
        db.Integer,
        primary_key=True,
        autoincrement=False,
        default=lambda: (db.session.query(db.func.max(Cobranca.id)).scalar() or 0) + 1,
        comment="Chave primária"
    )

    uuid_cobranca = db.Column(db.String(36), unique=True, nullable=False, default=lambda: str(uuid.uuid4()), comment="UUID universal")
    codigo_transacao = db.Column(db.String(30), unique=True, nullable=False, comment="Código legível (Ex: COB-20260929-102)")

    empresa_id = db.Column(db.Integer, nullable=False, index=True, comment="Multi-tenant ID")
    cliente_id = db.Column(db.Integer, nullable=True, index=True, comment="ID do cliente")

    origem_tipo = db.Column(db.String(30), nullable=False, default='AGENDAMENTO', index=True, comment="Módulo de origem (AGENDAMENTO, BALCAO, ASSINATURA)")
    origem_id = db.Column(db.BigInteger, nullable=False, index=True, comment="ID no módulo de origem")

    valor_bruto = db.Column(db.Numeric(10, 2), nullable=False, default=0.00)
    valor_desconto = db.Column(db.Numeric(10, 2), nullable=False, default=0.00)
    valor_acrescimo = db.Column(db.Numeric(10, 2), nullable=False, default=0.00)
    valor_liquido = db.Column(db.Numeric(10, 2), nullable=False, default=0.00)
    valor_pago = db.Column(db.Numeric(10, 2), nullable=False, default=0.00)

    status = db.Column(db.String(20), nullable=False, default='PENDENTE', index=True, comment="PENDENTE, PAGO_PARCIAL, PAGO, CANCELADO")

    cpf_cnpj_fiscal = db.Column(db.String(20), nullable=True)
    cpf_hash = db.Column(db.String(64), nullable=True, index=True)
    nome_fiscal = db.Column(db.String(150), nullable=True)
    email_fiscal = db.Column(db.String(120), nullable=True)
    exige_nota_fiscal = db.Column(db.Boolean, default=False)
    status_nf = db.Column(db.String(20), default='NAO_EMITIDA')

    data_emissao = db.Column(db.DateTime, default=datetime.now, nullable=False)
    data_vencimento = db.Column(db.DateTime, nullable=True)
    data_quitacao = db.Column(db.DateTime, nullable=True)

    pagamentos = db.relationship('CobrancaPagamento', backref='cobranca', lazy='joined', cascade='all, delete-orphan')

    def recalcular_saldos(self):
        total_pago = sum(float(p.valor_pago) for p in self.pagamentos if p.status == 'CONFIRMADO')
        self.valor_pago = total_pago

        if self.valor_pago >= float(self.valor_liquido):
            self.status = 'PAGO'
            if not self.data_quitacao:
                self.data_quitacao = datetime.now()
        elif self.valor_pago > 0:
            self.status = 'PAGO_PARCIAL'
        else:
            self.status = 'PENDENTE'

        return self.status

    def atualizar_valor_bruto(self, valor_servicos=None):
        """
        Recalcula e persiste o valor_bruto e valor_liquido com base nos
        itens de PDV e nos serviços associados.
        """
        # Soma produtos do PDV
        total_produtos = db.session.query(
            func.coalesce(func.sum(PdvComandaItem.valor_total), 0.0)
        ).filter(PdvComandaItem.cobranca_id == self.id).scalar()

        # Se valor_servicos não for informado, mantemos a diferença ou buscamos do agendamento
        if valor_servicos is None and self.origem_tipo == 'AGENDAMENTO' and self.origem_id:
            agendamento = AghAgendamento.query.get(self.origem_id)
            if agendamento:
                itens = getattr(agendamento, 'itens', []) or getattr(agendamento, 'servicos', []) or []
                valor_servicos = sum(
                    float(getattr(i, 'preco_unitario', 0) or getattr(i, 'valor', 0) or 0)
                    for i in itens if str(getattr(i, 'status_item', '')).lower() not in ['cancelado', 'cancelada']
                )

        total_servicos = float(valor_servicos or 0.0)
        self.valor_bruto = total_servicos + float(total_produtos)
        self.valor_liquido = float(self.valor_bruto) - float(self.valor_desconto or 0) + float(
            self.valor_acrescimo or 0)

        return self.valor_bruto


class CobrancaPagamento(db.Model):
    """
    Tabela Secundária: fin_cobranca_pagamentos
    Registra cada lançamento de pagamento associado à cobrança.
    """
    __tablename__ = 'fin_cobranca_pagamentos'
    __table_args__ = {'extend_existing': True}

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    cobranca_id = db.Column(db.BigInteger, db.ForeignKey('fin_cobrancas.id'), nullable=False, index=True)

    forma_pagamento = db.Column(db.String(30), nullable=False, comment="DINHEIRO, PIX, DEBITO, CREDITO, VOUCHER")
    valor_pago = db.Column(db.Numeric(10, 2), nullable=False, default=0.00)

    operacao_tipo = db.Column(db.String(20), default='MANUAL')
    operador_usuario_id = db.Column(db.BigInteger, nullable=True)
    observacao = db.Column(db.String(255), nullable=True)

    gateway_provedor = db.Column(db.String(30), nullable=True)
    gateway_tid = db.Column(db.String(100), nullable=True)
    payload_pix_qr = db.Column(db.Text, nullable=True)

    status = db.Column(db.String(20), default='CONFIRMADO', nullable=False)
    data_pagamento = db.Column(db.DateTime, default=datetime.now, nullable=False)


class LancamentoFinanceiro(db.Model):
    """
    Tabela: fin_lancamentos_financeiros
    Registra os lançamentos financeiros de receita ou despesa associados
    a uma cobrança/comanda ou diretamente a uma origem (ex: AghAgendamento).
    """
    __tablename__ = 'fin_lancamentos_financeiros'
    __table_args__ = {'extend_existing': True}

    # db.Integer faz o SQLite criar o id como AUTOINCREMENT automaticamente
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)

    uuid_lancamento = db.Column(
        db.String(36),
        unique=True,
        nullable=False,
        default=lambda: str(uuid.uuid4()),
        comment="UUID do lançamento"
    )

    empresa_id = db.Column(
        db.Integer,
        db.ForeignKey('ese_empresa.id'),
        nullable=False,
        index=True,
        comment="Multi-tenant ID"
    )

    cobranca_id = db.Column(
        db.BigInteger,
        db.ForeignKey('fin_cobrancas.id', ondelete='SET NULL'),
        nullable=True,
        index=True,
        comment="Vínculo com a comanda principal"
    )

    # 🔗 Desacoplamento para consultas diretas por origem
    origem_tipo = db.Column(
        db.String(30),
        nullable=False,
        default='AGENDAMENTO',
        index=True,
        comment="Módulo gerador (AGENDAMENTO, BALCAO, ASSINATURA)"
    )
    agendamento_id = db.Column(
        db.Integer,
        db.ForeignKey('agh_agendamento.id', ondelete='SET NULL'),
        nullable=True,
        index=True,
        comment="Atalho/FK para o agendamento de origem"
    )

    tipo_operacao = db.Column(
        db.String(10),
        nullable=False,
        default='RECEITA',
        comment="RECEITA ou DESPESA"
    )
    descricao = db.Column(db.String(255), nullable=True, comment="Descrição do lançamento")

    valor = db.Column(db.Numeric(10, 2), nullable=False, default=0.00)
    forma_pagamento = db.Column(
        db.String(30),
        nullable=False,
        default='PIX',
        comment="DINHEIRO, PIX, DEBITO, CREDITO, VOUCHER"
    )

    status = db.Column(
        db.String(20),
        nullable=False,
        default='LIQUIDADO',
        index=True,
        comment="PENDENTE, LIQUIDADO, CANCELADO"
    )

    data_pagamento = db.Column(db.DateTime, default=datetime.now, nullable=False)
    data_vencimento = db.Column(db.DateTime, default=datetime.now, nullable=False)

    criado_em = db.Column(db.DateTime, default=datetime.now, nullable=False)

    # Relacionamentos ORM
    cobranca = db.relationship('Cobranca', backref=db.backref('lancamentos', lazy=True))
    agendamento = db.relationship('AghAgendamento', backref=db.backref('lancamentos', lazy=True))