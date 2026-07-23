# feedin/modules/agenda/models.py
from feedin import database as db
from datetime import datetime, timezone

# =====================================================================
# 🔗 TABELAS INTERMEDIÁRIAS / RELACIONAIS
# =====================================================================

# Tabela Pivot: Cruza quais profissionais realizam quais serviços específicos.
agh_profissional_servico = db.Table(
    'agh_profissional_servico',
    db.Column('profissional_id', db.Integer, db.ForeignKey('agh_profissional.id', ondelete='CASCADE'),
              primary_key=True),
    db.Column('servico_id', db.Integer, db.ForeignKey('agh_servico.id', ondelete='CASCADE'), primary_key=True),
    extend_existing=True
)


# =====================================================================
# 🏛️ ENTIDADES CORE E PERIFÉRICAS DO MÓDULO
# =====================================================================

class AghProfissional(db.Model):
    __tablename__ = 'agh_profissional'
    __table_args__ = {'extend_existing': True}

    id = db.Column(db.Integer, primary_key=True)
    estabelecimento_id = db.Column(db.Integer, db.ForeignKey('ese_empresa.id'), nullable=False)

    # Vincula o perfil de agendamento ao Contrato de Trabalho ativo dele no Core
    contrato_id = db.Column(db.Integer, db.ForeignKey('colaborador_contratos.id'), nullable=True)

    nome = db.Column(db.String(100), nullable=False)
    cargo_especialidade = db.Column(db.String(100))
    is_ativo = db.Column(db.Boolean, default=True)

    # Relacionamentos
    empresa = db.relationship('EseEmpresa', backref=db.backref('profissionais', lazy=True))
    contrato_core = db.relationship('ColaboradorContrato', backref=db.backref('perfil_agenda', uselist=False))

    def __repr__(self):
        return f"<AghProfissional {self.nome} - {self.cargo_especialidade}>"


class AghAgendamento(db.Model):
    """Gerenciamento de horários, prazos de 30/37 dias e histórico."""

    __tablename__ = 'agh_agendamento'
    __table_args__ = {'extend_existing': True}

    id = db.Column(db.Integer, primary_key=True)
    profissional_id = db.Column(db.Integer, db.ForeignKey('agh_profissional.id'), nullable=False)
    cliente_id = db.Column(db.Integer, db.ForeignKey('mod_cadastro_cliente.id'), nullable=False)
    servico_id = db.Column(db.Integer, db.ForeignKey('agh_servico.id'), nullable=False)
    preco_cobrado = db.Column(db.Numeric(10, 2), nullable=False)
    data_hora_inicio = db.Column(db.DateTime, nullable=False)
    data_hora_fim = db.Column(db.DateTime, nullable=False)
    status = db.Column(db.String(30), default='confirmado')
    tipo_origem = db.Column(db.String(20), default='online')
    data_solicitacao_reagendamento = db.Column(db.DateTime, nullable=True)

    cliente = db.relationship('ModCadastroCliente', backref='agendamentos')

    # Relacionamento com a tabela de avaliação do serviço (definida abaixo)
    avaliacao = db.relationship('AghAvaliacaoServico', backref='agendamento_avaliado', uselist=False, lazy=True)

    def __repr__(self):
        return f"<AghAgendamento ID {self.id} - Status {self.status}>"


class AghAvaliacaoServico(db.Model):
    """
    📌 AGH_AVALIACAO_SERVICO: Avaliação específica do serviço realizado
    --------------------------------------------------------------------------------------
    Guarda o feedback do cliente sobre o agendamento específico após a sua conclusão.
    """
    __tablename__ = 'agh_avaliacao_servico'
    __table_args__ = {'extend_existing': True}

    id = db.Column(db.Integer, primary_key=True)
    agendamento_id = db.Column(db.Integer, db.ForeignKey('agh_agendamento.id'), nullable=False)
    nota = db.Column(db.Integer, nullable=False)
    comentario = db.Column(db.Text, nullable=True)
    criado_em = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    def __repr__(self):
        return f"<AghAvaliacaoServico Agendamento {self.agendamento_id} - Nota {self.nota}>"


class AghServico(db.Model):
    """Cardápio de Serviços Disponíveis para Agendamento"""
    __tablename__ = 'agh_servico'
    __table_args__ = {'extend_existing': True}

    id = db.Column(db.Integer, primary_key=True)
    estabelecimento_id = db.Column(db.Integer, db.ForeignKey('ese_empresa.id'), nullable=False)

    nome = db.Column(db.String(100), nullable=False)
    descricao = db.Column(db.Text, nullable=True)
    preco_padrao = db.Column(db.Numeric(10, 2), nullable=False)

    # Tempo em minutos para cálculo dinâmico
    duracao_minutos = db.Column(db.Integer, nullable=False, default=30)

    is_ativo = db.Column(db.Boolean, default=True)
    criado_em = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    # Relacionamentos
    empresa = db.relationship('EseEmpresa', backref=db.backref('servicos_agenda', lazy=True))

    def __repr__(self):
        return f"<AghServico {self.nome} ({self.duracao_minutos}min) - R$ {self.preco_padrao}>"


class AghControlePagamento(db.Model):
    """Garantia de Presença e Regra do Relógio (PIX)"""
    __tablename__ = 'agh_controle_pagamento'
    __table_args__ = {'extend_existing': True}

    id = db.Column(db.Integer, primary_key=True)
    agendamento_id = db.Column(db.Integer, db.ForeignKey('agh_agendamento.id'), nullable=False)

    status_pagamento = db.Column(db.String(20), default='pendente')
    pix_copia_cola = db.Column(db.Text, nullable=True)
    data_limite_pagamento = db.Column(db.DateTime, nullable=False)
    pago_em = db.Column(db.DateTime, nullable=True)
    transacao_id = db.Column(db.String(100), nullable=True)

    agendamento = db.relationship('AghAgendamento',
                                  backref=db.backref('controle_pagamento', uselist=False, cascade='all, delete-orphan'))

    def __repr__(self):
        return f"<AghControlePagamento Agendamento {self.agendamento_id} - {self.status_pagamento}>"


class AghHistoricoPresenca(db.Model):
    """Log Comportamental e Score de Confiança do Cliente"""
    __tablename__ = 'agh_historico_presenca'
    __table_args__ = {'extend_existing': True}

    id = db.Column(db.Integer, primary_key=True)
    estabelecimento_id = db.Column(db.Integer, db.ForeignKey('ese_empresa.id'), nullable=False)
    cliente_id = db.Column(db.Integer, db.ForeignKey('mod_cadastro_cliente.id'), nullable=False)
    agendamento_id = db.Column(db.Integer, db.ForeignKey('agh_agendamento.id'), nullable=True)

    tipo_evento = db.Column(db.String(30), nullable=False)
    motivo = db.Column(db.Text, nullable=True)
    data_registro = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    def __repr__(self):
        return f"<AghHistoricoPresenca Cliente ID {self.cliente_id} - Evento: {self.tipo_evento}>"


class CoreAvaliacaoProfissional(db.Model):
    """Reputação Unificada do Profissional"""
    __tablename__ = 'core_avaliacao_profissional'
    __table_args__ = {'extend_existing': True}

    id = db.Column(db.Integer, primary_key=True)
    estabelecimento_id = db.Column(db.Integer, db.ForeignKey('ese_empresa.id'), nullable=False)

    avaliador_cliente_id = db.Column(db.Integer, db.ForeignKey('mod_cadastro_cliente.id'), nullable=False)
    profissional_usuario_id = db.Column(db.Integer, db.ForeignKey('usuario.id'), nullable=False)

    # Referência à tabela 'taxonomia'
    funcao_taxonomia_id = db.Column(db.Integer, db.ForeignKey('taxonomia.id'), nullable=False)

    agendamento_id = db.Column(db.Integer, db.ForeignKey('agh_agendamento.id'), nullable=True)
    nota_atendimento = db.Column(db.Integer, nullable=False)
    nota_tecnica = db.Column(db.Integer, nullable=False)
    comentario = db.Column(db.Text, nullable=True)
    criado_em = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    def __repr__(self):
        return f"<AvaliacaoProfissional Destinatario ID {self.profissional_usuario_id} - Funcao Termo {self.funcao_taxonomia_id}>"


class AghProntuarioCliente(db.Model):
    """Prontuário Técnico e Avaliação do Cliente"""
    __tablename__ = 'agh_prontuario_cliente'
    __table_args__ = {'extend_existing': True}

    id = db.Column(db.Integer, primary_key=True)
    estabelecimento_id = db.Column(db.Integer, db.ForeignKey('ese_empresa.id'), nullable=False)
    cliente_id = db.Column(db.Integer, db.ForeignKey('mod_cadastro_cliente.id'), nullable=False)
    profissional_id = db.Column(db.Integer, db.ForeignKey('agh_profissional.id'), nullable=False)

    # Referência à tabela 'taxonomia'
    curvatura_cabelo_termo_id = db.Column(db.Integer, db.ForeignKey('taxonomia.id'), nullable=True)

    nota_cliente = db.Column(db.Integer, nullable=False, default=5)
    observacoes_tecnicas = db.Column(db.Text, nullable=True)
    data_atualizacao = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc),
                                 onupdate=lambda: datetime.now(timezone.utc))

    def __repr__(self):
        return f"<Prontuario Cliente {self.cliente_id} - Cabelo Termo {self.curvatura_cabelo_termo_id}>"