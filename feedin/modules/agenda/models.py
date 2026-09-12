# feedin/modules/agenda/models.py
import random
import uuid
import logging
from sqlalchemy import func
from feedin import database as db
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

# =====================================================================
# 🔗 TABELAS INTERMEDIÁRIAS / RELACIONAIS
# =====================================================================

"""
==========================================================================================
📌 MÓDULO AGENDA: ARQUITETURA DE RESERVAS, OPERAÇÃO DE BALCÃO E ESCALAS
==========================================================================================
Este arquivo centraliza as entidades de agendamento, slots de horário e itens contratados.
A arquitetura foi projetada para garantir:
  1. Snapshot de Contratação: A tabela 'AghAgendamentoItem' preserva o preço, ordem e 
     duração dos serviços no momento da reserva, garantindo integridade histórica e financeira.
  2. Isolamento Multilocatário (Multi-tenant) e Multilocal: O vínculo direto de 'empresa_id' e 
     'profissional_id' permite que um colaborador atenda em múltiplos estabelecimentos com 
     grades, exceções e comissionamentos totalmente distintos.
  3. Gestão de Soft-Lock e Ciclo de Vida: Controle de retenção temporária de horários 
     (session_token / expira_em) para prevenir Overbooking no PWA do Cliente e no Balcão.

Tabelas Gerenciadas:
  - AghAgendamento: Tabela mestre de reservas, status operacionais e controle financeiro.
  - AghAgendamentoItem: Tabela pivot de snapshot detalhada de serviços por agendamento.
==========================================================================================
"""


class AghAgendamento(db.Model):
    __tablename__ = 'agh_agendamento'
    __table_args__ = {'extend_existing': True}

    id = db.Column(db.Integer, primary_key=True)

    # 🏢 VÍNCULO DIRETO COM A EMPRESA
    empresa_id = db.Column(
        db.Integer, db.ForeignKey('ese_empresa.id'), nullable=False, index=True
    )

    # 👤 VÍNCULO COM O PROFISSIONAL
    profissional_id = db.Column(
        db.Integer,
        db.ForeignKey('colaborador_contratos.id'),
        nullable=False,
        index=True,
    )

    # 👥 VÍNCULO COM O CLIENTE (UUID)
    cliente_id = db.Column(
        db.String(36),
        db.ForeignKey('mod_cadastro_cliente.id'),
        nullable=True,
        index=True,
    )

    # 🐾/👤 VÍNCULO COM O BENEFICIÁRIO/PERSONA (NOVO CAMPO)
    beneficiario_id = db.Column(
        db.Integer,
        db.ForeignKey('cliente_beneficiarios.id'),
        nullable=True,  # Deixe True caso o atendimento possa ser do próprio titular
        index=True,
    )

    # 💰 VALORES E HORÁRIOS
    valor_total = db.Column(db.Numeric(10, 2), nullable=False, default=0.00)
    data_hora_inicio = db.Column(db.DateTime, nullable=False, index=True)
    data_hora_fim = db.Column(db.DateTime, nullable=False)

    # ⚙️ STATUS E ORIGEM
    status = db.Column(
        db.String(30), default='soft_lock', nullable=False, index=True
    )
    # Status atualizados:
    # 'soft_lock', 'pendente', 'agendado', 'confirmado', 'aguardando',
    # 'em_atendimento', 'concluido', 'finalizado', 'cancelado',
    # 'ausente_pendente', 'reagendamento_pendente'

    tipo_origem = db.Column(db.String(20), default='online')

    # 🔒 CONTROLE DE SESSÃO / RESERVA TEMPORÁRIA
    session_token = db.Column(db.String(100), nullable=True, index=True)
    expira_em = db.Column(db.DateTime, nullable=True)

    # 📅 REAGENDAMENTO, PENALIDADES E AUDITORIA
    data_solicitacao_reagendamento = db.Column(db.DateTime, nullable=True)
    marcado_como_ausente_em = db.Column(db.DateTime, nullable=True)
    limite_reagendamento = db.Column(db.DateTime, nullable=True)
    teve_impacto_comportamental = db.Column(
        db.Boolean, default=False
    )  # Marcação permanente no perfil

    criado_em = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    # RELACIONAMENTOS (ORM)
    empresa = db.relationship('EseEmpresa', backref='agendamentos', lazy=True)
    profissional = db.relationship(
        'ColaboradorContrato', backref='agendamentos', lazy=True
    )
    cliente = db.relationship(
        'ModCadastroCliente', backref='agendamentos', lazy=True
    )

    # NOVO RELACIONAMENTO:
    beneficiario = db.relationship(
    'ClienteBeneficiario', backref = 'agendamentos', lazy = True
    )

    itens = db.relationship(
        'AghAgendamentoItem',
        backref='agendamento_pai',
        cascade='all, delete-orphan',
        lazy='joined',
    )
    avaliacao = db.relationship(
        'AghAvaliacaoServico',
        backref='agendamento_avaliado',
        uselist=False,
        lazy=True,
    )

    # ----------------------------------------------------------------------------------
    # MÉTODOS ÚTEIS
    # ----------------------------------------------------------------------------------
    def recalcular_total(self):
        total = sum(
            item.preco_unitario for item in self.itens if item.preco_unitario
        )
        self.valor_total = total
        return total

    @property
    def reagendamento_valido(self):
        """Verifica se o agendamento em ausência ainda está dentro da janela limite."""
        if (
            self.status in ['ausente_pendente', 'reagendamento_pendente']
            and self.limite_reagendamento
        ):
            return datetime.utcnow() <= self.limite_reagendamento
        return False

    def __repr__(self):
        return f'<AghAgendamento #{self.id} | Empresa #{self.empresa_id} | Profissional Contrato #{self.profissional_id} | Status: {self.status}>'

    # -------------------------------------------------------------------------
    # RASTREABILIDADE DE REAGENDAMENTOS MÚLTIPLOS (LINHA DO TEMPO)
    # -------------------------------------------------------------------------
    # Aponta para o PRIMEIRO agendamento da história (Data de Referência Vitalícia)
    agendamento_origem_id = db.Column(db.Integer, db.ForeignKey('agh_agendamento.id'), nullable=True)

    # Aponta para o agendamento imediatamente anterior (passo a passo)
    agendamento_anterior_id = db.Column(db.Integer, db.ForeignKey('agh_agendamento.id'), nullable=True)

    # Status do Ciclo de Vida:
    # 'confirmado', 'em_andamento', 'concluido', 'reagendado', 'cancelado', 'falta'
    status = db.Column(db.String(30), default='confirmado', nullable=False)

    # Contador para auditoria rápida
    qtd_reagendamentos = db.Column(db.Integer, default=0, nullable=False)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Relacionamentos para navegar na árvore de histórico
    historico_posteriores = db.relationship('AghAgendamento',
                                            foreign_keys=[agendamento_origem_id],
                                            backref=db.backref('agendamento_raiz', remote_side=[id]))


class AghAgendamentoItem(db.Model):
    __tablename__ = 'agh_agendamento_item'
    __table_args__ = {'extend_existing': True}

    id = db.Column(db.Integer, primary_key=True)
    agendamento_id = db.Column(
        db.Integer,
        db.ForeignKey('agh_agendamento.id', ondelete='CASCADE'),
        nullable=False,
    )

    # FK apontando para a nova tabela de serviços oferecidos
    servico_id = db.Column(
        db.Integer,
        db.ForeignKey('ese_servico_oferecido.id'),
        nullable=False
    )

    # 📌 CORREÇÃO AQUI: 'colaborador_contratos.id' (no plural)
    profissional_id = db.Column(
        db.Integer,
        db.ForeignKey('colaborador_contratos.id', ondelete='SET NULL'),
        nullable=True
    )

    preco_unitario = db.Column(db.Numeric(10, 2), nullable=False, default=0.00)
    duracao_minutos = db.Column(db.Integer, nullable=False, default=30)
    ordem_execucao = db.Column(db.Integer, default=1)

    # ----------------------------------------------------------------------------------
    # RELACIONAMENTOS (ORM)
    # ----------------------------------------------------------------------------------
    servico = db.relationship(
        'EseServicoOferecido',
        backref='itens_agendados',
        foreign_keys=[servico_id],
        lazy='joined'
    )

    profissional = db.relationship(
        'ColaboradorContrato',
        foreign_keys=[profissional_id],
        backref=db.backref('itens_agendados', lazy='dynamic')
    )


class AghAgendamentoRascunho(db.Model):
    __tablename__ = 'agh_agendamento_rascunho'
    __table_args__ = {'extend_existing': True}

    id = db.Column(db.Integer, primary_key=True)

    # 🏢 VÍNCULO OBRIGATÓRIO COM A EMPRESA
    empresa_id = db.Column(
        db.Integer, db.ForeignKey('ese_empresa.id'), nullable=False, index=True
    )

    # 👥 VÍNCULO COM O CLIENTE (UUID) - Opcional no início do fluxo
    cliente_id = db.Column(
        db.String(36),
        db.ForeignKey('mod_cadastro_cliente.id'),
        nullable=True,
        index=True,
    )

    # 🔑 CONTROLE DE SESSÃO / ANÔNIMO
    session_token = db.Column(
        db.String(100), nullable=True, index=True
    )  # Para identificar visitantes não logados

    # 👤 PROFISSIONAL E HORÁRIO (Definidos apenas no Passo 2)
    profissional_id = db.Column(
        db.Integer,
        db.ForeignKey('colaborador_contratos.id'),
        nullable=True,
        index=True,
    )
    data_hora_inicio = db.Column(db.DateTime, nullable=True, index=True)
    data_hora_fim = db.Column(db.DateTime, nullable=True)

    # 💰 VALOR TOTAL E DURAÇÃO
    valor_total = db.Column(db.Numeric(10, 2), nullable=False, default=0.00)

    # ⚙️ MÁQUINA DE ESTADO DO RASCUNHO
    status = db.Column(
        db.String(30), default='servicos_selecionados', nullable=False
    )
    # Estados possíveis:
    # 'servicos_selecionados' -> Apenas escolheu os serviços no Passo 1
    # 'horario_reservado'     -> Escolheu profissional e horário no Passo 2 (Soft Lock)
    # 'convertido'           -> Já virou um AghAgendamento oficial
    # 'expirado'             -> Tempo limite esgotado

    # ⏳ CONTROLE DE EXPIRAÇÃO
    expira_em = db.Column(db.DateTime, nullable=True, index=True)
    criado_em = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    atualizado_em = db.Column(
        db.DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )

    # RELACIONAMENTOS (ORM)
    empresa = db.relationship('EseEmpresa', lazy=True)
    cliente = db.relationship('ModCadastroCliente', lazy=True)
    profissional = db.relationship('ColaboradorContrato', lazy=True)

    itens = db.relationship(
        'AghAgendamentoRascunhoItem',
        backref='rascunho',
        cascade='all, delete-orphan',
        lazy='joined',
    )

    # -------------------------------------------------------------------------
    # MÉTODOS ÚTEIS
    # -------------------------------------------------------------------------
    def recalcular_total(self):
        """Soma o valor total com base nos itens atrelados."""
        total = sum(
            item.preco_unitario for item in self.itens if item.preco_unitario
        )
        self.valor_total = total
        return total

    @property
    def duracao_total_minutos(self):
        """Calcula o tempo total estimado de todos os serviços selecionados."""
        total_min = 0
        for item in self.itens:
            if hasattr(item, 'servico') and item.servico:
                dur = str(item.servico.tempo_duracao)
                if ':' in dur:
                    h, m = map(int, dur.split(':')[:2])
                    total_min += h * 60 + m
                elif dur.isdigit():
                    total_min += int(dur)
        return total_min

    @property
    def esta_expirado(self):
        """Verifica se o tempo de reserva temporária expirou."""
        if self.expira_em:
            return datetime.utcnow() > self.expira_em
        return False

    def __repr__(self):
        return f'<AghAgendamentoRascunho #{self.id} | Empresa #{self.empresa_id} | Status: {self.status}>'


class AghAgendamentoRascunhoItem(db.Model):
    __tablename__ = 'agh_agendamento_rascunho_item'
    __table_args__ = {'extend_existing': True}

    id = db.Column(db.Integer, primary_key=True)
    rascunho_id = db.Column(
        db.Integer,
        db.ForeignKey('agh_agendamento_rascunho.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
    )
    servico_id = db.Column(
        db.Integer,
        db.ForeignKey('agh_servico.id'),
        nullable=False,
        index=True,
    )

    preco_unitario = db.Column(
        db.Numeric(10, 2), nullable=False, default=0.00
    )
    duracao_minutos = db.Column(db.Integer, nullable=True)

    # RELACIONAMENTOS (ORM)
    servico = db.relationship('AghServico', lazy=True)

    def __repr__(self):
        return f'<AghAgendamentoRascunhoItem Rascunho #{self.rascunho_id} -> Servico #{self.servico_id}>'


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
  """Log Comportamental, Auditoria de Tempo Real e Score de Confiança do Cliente.

  Registra os eventos de pontualidade, atrasos (do cliente ou do profissional)
  e
  serve como base para a concessão de bônus/créditos de fidelização.
  """

  __tablename__ = 'agh_historico_presenca'
  __table_args__ = {'extend_existing': True}

  id = db.Column(db.Integer, primary_key=True)
  estabelecimento_id = db.Column(
      db.Integer, db.ForeignKey('ese_empresa.id'), nullable=False
  )
  cliente_id = db.Column(
      db.String(36), db.ForeignKey('mod_cadastro_cliente.id'), nullable=False
  )  # Nota: Ajustado para String(36) se for UUID
  agendamento_id = db.Column(
      db.Integer, db.ForeignKey('agh_agendamento.id'), nullable=True
  )

  # Eventos: 'checkin_cliente', 'inicio_atendimento', 'conclusao_atendimento',
  #          'atraso_cliente', 'atraso_profissional', 'no_show', 'bonificacao_pontualidade'
  tipo_evento = db.Column(db.String(40), nullable=False)

  # Timestamps estratégicos para análise
  data_hora_agendada = db.Column(
      db.DateTime, nullable=True
  )  # Horário que estava previsto na agenda
  data_hora_evento = db.Column(
      db.DateTime, default=lambda: datetime.now(timezone.utc), nullable=False
  )  # Momento exato em que a ação ocorreu

  # Métricas de desvio (em minutos)
  # Ex: -5 (chegou 5 min antes), +15 (chegou 15 min atrasado)
  desvio_minutos = db.Column(db.Integer, default=0, nullable=False)

  # Gamificação e Fidelização (Pontos/Créditos gerados neste evento)
  creditos_pontualidade = db.Column(
      db.Numeric(10, 2), default=0.00, nullable=False
  )

  motivo = db.Column(db.Text, nullable=True)
  data_registro = db.Column(
      db.DateTime, default=lambda: datetime.now(timezone.utc)
  )

  # Relacionamentos para facilitar consultas no Flask/Jinja
  cliente = db.relationship('ModCadastroCliente', backref='historicos_presenca')
  agendamento = db.relationship('AghAgendamento', backref='historico_eventos')

  def __repr__(self):
    return f'<AghHistoricoPresenca Cliente ID {self.cliente_id} - Evento: {self.tipo_evento} (Desvio: {self.desvio_minutos}m)>'


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


class AghOcorrenciaCliente(db.Model):
    __tablename__ = 'agh_ocorrencia_cliente'

    id = db.Column(db.Integer, primary_key=True)

    # 🛑 AJUSTADO AQUI: De 'agh_cliente.id' para o nome exato da tabela no seu banco
    cliente_id = db.Column(
        db.Integer,
        db.ForeignKey('mod_cadastro_cliente.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
    )

    empresa_id = db.Column(
        db.Integer,
        db.ForeignKey('ese_empresa.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
    )

    agendamento_id = db.Column(
        db.Integer,
        db.ForeignKey('agh_agendamento.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
    )

    # Tipos: 'REAGENDAMENTO_TARDIO', 'CANCELAMENTO_EM_CIMA_DA_HORA', 'NO_SHOW_EXPIRADO'
    tipo_ocorrencia = db.Column(db.String(50), nullable=False)
    antecedencia_minutos = db.Column(db.Integer, nullable=True)
    criado_em = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    def __repr__(self):
        return f'<AghOcorrenciaCliente Cliente={self.cliente_id} Tipo={self.tipo_ocorrencia}>'


class AghConfiguracaoAgenda(db.Model):
    """Modelo de Configuração Parametrizada da Agenda da Empresa.

    Esta tabela centraliza todas as políticas operacionais, regras de
    antecedência, travas de reagendamento, fluxos financeiros e feature flags
    (como fidelização e pagamentos) para cada estabelecimento no ecossistema
    FeedIn.

    Permite que a plataforma se adapte dinamicamente a diferentes segmentos
    (ex: barbearias com atendimento rápido vs. estética automotiva/lava-jato com
    serviços de longa duração).
    """

    __tablename__ = 'agh_configuracao_agenda'

    # -------------------------------------------------------------------------
    # CHAVE PRIMÁRIA E VÍNCULO EMPRESARIAL
    # -------------------------------------------------------------------------
    id = db.Column(
        db.Integer,
        primary_key=True,
        autoincrement=True,
        comment='Identificador único da configuração.',
    )

    empresa_id = db.Column(
        db.Integer,
        db.ForeignKey('ese_empresa.id', ondelete='CASCADE'),
        nullable=False,
        unique=True,
        index=True,
        comment='ID da empresa à qual estas configurações pertencem (Relação 1:1).',
    )

    # -------------------------------------------------------------------------
    # 1. POLÍTICAS DE TOLERÂNCIA, ANTECEDÊNCIA E REAGENDAMENTO
    # -------------------------------------------------------------------------
    antecedencia_minima_reagendamento_min = db.Column(
        db.Integer,
        default=120,
        nullable=False,
        comment=(
            'Tempo mínimo em minutos que o cliente deve respeitar para '
            'reagendar/cancelar sem gerar ocorrência no histórico de hábitos.\n'
            'Exemplos:\n'
            ' - Barbearia: 120 (2 horas)\n'
            ' - Lava-Jato / Estética Automotiva: 240 (4 horas) ou 1440 (24'
            ' horas)'
        ),
    )

    prazo_limite_reagendamento_dias = db.Column(
        db.Integer,
        default=7,
        nullable=False,
        comment=(
            'Janela de carência (em dias) concedida ao cliente para reagendar '
            'um serviço não comparecido ou alterado em cima da hora.\n'
            'Após esse prazo, o direito de reagendamento direto expira e a falta '
            'vira No-Show definitivo.'
        ),
    )

    permitir_reagendamento_pos_horario = db.Column(
        db.Boolean,
        default=True,
        nullable=False,
        comment=(
            'Habilita/desabilita se o cliente pode solicitar o reagendamento '
            'autônomo via PWA mesmo após o horário agendado ter vencido '
            '(respeitando a janela de dias configurada).'
        ),
    )

    # -------------------------------------------------------------------------
    # 2. FINANÇAS E COBRANÇA ANTECIPADA (SINAL / GARANTIA DE RESERVA)
    # -------------------------------------------------------------------------
    exigir_pagamento_antecipado = db.Column(
        db.Boolean,
        default=False,
        nullable=False,
        comment=(
            'Se True, o agendamento só é confirmado na grade após a '
            'confirmação do gateway de pagamento (Pix/Cartão).'
        ),
    )

    percentual_sinal_pagamento = db.Column(
        db.Numeric(5, 2),
        default=100.00,
        nullable=False,
        comment=(
            'Porcentagem cobrada na reserva do horário.\n'
            'Exemplos:\n'
            ' - 100.00: Cobrança integral do serviço.\n'
            ' - 30.00: Cobrança de 30% como sinal de garantia da vaga.'
        ),
    )

    tempo_expiracao_pix_minutos = db.Column(
        db.Integer,
        default=15,
        nullable=False,
        comment=(
            'Tempo em minutos que o horário permanece travado provisoriamente '
            'na agenda aguardando a liquidação do Pix/Webhook.'
        ),
    )

    # -------------------------------------------------------------------------
    # 3. FEATURE FLAGS (MÓDULOS DE FIDELIZAÇÃO E ENCAIXES)
    # -------------------------------------------------------------------------
    fidelidade_ativa = db.Column(
        db.Boolean,
        default=False,
        nullable=False,
        comment=(
            'Chave liga/desliga para o Programa de Fidelidade da empresa.\n'
            'Apenas quando True a interface exibe pontos, selos e resgates no '
            'PWA do cliente.'
        ),
    )

    permitir_encaixe_pwa = db.Column(
        db.Boolean,
        default=True,
        nullable=False,
        comment=(
            'Permite que clientes no PWA solicitem agendamentos de última hora '
            '(encaixes) para horários vagos recentes.'
        ),
    )

    limite_agendamentos_abertos_por_cliente = db.Column(
        db.Integer,
        default=3,
        nullable=False,
        comment=(
            'Quantidade máxima de agendamentos futuros simultâneos que um '
            'mesmo cliente pode manter abertos neste estabelecimento, evitando '
            'bloqueios em massa.'
        ),
    )

    # -------------------------------------------------------------------------
    # POLÍTICAS DE TOLERÂNCIA DE ATRASO DO CLIENTE
    # -------------------------------------------------------------------------
    tolerancia_atraso_minutos = db.Column(
        db.Integer,
        default=0,
        nullable=False,
        comment=(
            'Tolerância de atraso (em minutos) padrão do estabelecimento.\n'
            'Exemplos:\n'
            ' - 0: O sistema usará o tempo_intervalo do(s) serviço(s) do agendamento.\n'
            ' - > 0 (ex: 10 ou 15): Sobrescreve o tempo_intervalo do serviço com um valor fixo da empresa.'
        ),
    )

    # -------------------------------------------------------------------------
    # METADADOS DE AUDITORIA
    # -------------------------------------------------------------------------
    criado_em = db.Column(
        db.DateTime,
        default=datetime.utcnow,
        nullable=False,
        comment='Data e hora de criação do registro de configuração.',
    )

    atualizado_em = db.Column(
        db.DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
        comment='Data e hora da última alteração das políticas da agenda.',
    )

    # -------------------------------------------------------------------------
    # RELACIONAMENTOS (ORMs)
    # -------------------------------------------------------------------------
    empresa = db.relationship(
        'EseEmpresa',
        backref=db.backref('configuracao_agenda', uselist=False),
        lazy=True,
    )

    def __repr__(self):
        return f'<AghConfiguracaoAgenda Empresa_ID={self.empresa_id} AntecedenciaMin={self.antecedencia_minima_reagendamento_min}m>'


def obter_publicidade_agenda(cliente, empresa_agendada_id=None):
    """
    Adapta a inteligência do Core para o PWA da Agenda.
    - Respeita o filtro de concorrência (não mostra concorrente do estabelecimento atual).
    - Prioriza tags dos serviços que o cliente costuma consumir.
    - Serve parceiros locais no mesmo tom de comunidade.
    """
    with db.session.no_autoflush:
        tag_vencedora = None

        # 1. CAPTURA AFINIDADE PELO HISTÓRICO DE SERVIÇOS DO CLIENTE
        # Em vez de posts, olhamos as tags dos serviços que ele consome
        if cliente and hasattr(cliente, 'agendamentos'):
            tags_servicos_cliente = set()
            for ag in cliente.agendamentos.limit(10):
                for item in ag.itens:
                    if item.servico and hasattr(item.servico, 'tags'):
                        tags_servicos_cliente.update(item.servico.tags)

            if tags_servicos_cliente:
                tag_vencedora = random.choice(list(tags_servicos_cliente))

        # Contingência: se o cliente for novo, pega a tag da empresa onde ele está agendando
        if not tag_vencedora and empresa_agendada_id:
            from feedin.models import Local
            empresa_atual = Local.query.get(empresa_agendada_id)
            if empresa_atual and hasattr(empresa_atual, 'tags'):
                tags_empresa = empresa_atual.tags.all() if hasattr(empresa_atual.tags, 'all') else empresa_atual.tags
                if tags_empresa:
                    tag_vencedora = random.choice(tags_empresa)

        # 2. FILTRO DE CONCORRÊNCIA (A mesma regra de ouro do Core!)
        categoria_bloqueada_id = None
        if empresa_agendada_id:
            from feedin.models import Local
            empresa_atual = Local.query.get(empresa_agendada_id)
            if empresa_atual:
                categoria_bloqueada_id = empresa_atual.id_categoria_principal

        # 3. BUSCA SELETIVA NO `LocalAnuncio` (Tabela nativa do Core!)
        from feedin.models import LocalAnuncio, Local

        query = LocalAnuncio.query.filter(LocalAnuncio.status == 'ativo')

        if tag_vencedora:
            query = query.filter(LocalAnuncio.taxonomia_id == tag_vencedora.id)

        # Aplica a trava antirrivalidade
        if empresa_agendada_id and categoria_bloqueada_id:
            query = query.join(Local, LocalAnuncio.local_id == Local.id).filter(
                db.or_(
                    LocalAnuncio.local_id == empresa_agendada_id,  # Pode ser anúncio do próprio local
                    Local.id_categoria_principal != categoria_bloqueada_id
                    # Ou de outro segmento completamente diferente
                )
            )

        anuncios = query.order_by(func.random()).all()

        if not anuncios:
            return None

        # Prioriza patrocinados
        patrocinados = [a for a in anuncios if getattr(a, 'plano_marketing', None) == 'patrocinado']
        anuncio_escolhido = patrocinados[0] if patrocinados else anuncios[0]

        # Texto adaptado para a experiência do PWA/Agenda
        anuncio_escolhido.texto_formatado = f"Parceiro recomendado em Piracicaba no segmento de {tag_vencedora.nome if tag_vencedora else 'serviços'}!"

        # Métrica atômica mantida do Core
        try:
            db.session.query(LocalAnuncio).filter(LocalAnuncio.id == anuncio_escolhido.id).update(
                {"visualizacoes": LocalAnuncio.visualizacoes + 1},
                synchronize_session=False
            )
        except Exception as e:
            print(f"Erro ao registrar view do banner na Agenda: {e}")

        return anuncio_escolhido


class AghNotificacao(db.Model):
    """Entidade responsável pelo gerenciamento de notificações do módulo AGH (Agenda/Prestação de Serviços).

    Isola os eventos do módulo de agendamento, garantindo contexto multi-tenant
    (por empresa), rastreabilidade de remetente/destinatário via UUID (char 36)
    e vínculo opcional com a persona/beneficiário atendido (Humano, Pet ou
    Veículo).

    Atributos:
        id (str): Chave primária em formato UUIDv4 (CHAR 36).
        empresa_id (int): Chave estrangeira da empresa contratante (ESE - Multi-tenant).
        agendamento_id (int, optional): Chave estrangeira do agendamento vinculado.
        remetente_id (str, optional): UUID do usuário gerador do evento/notificação.
        destinatario_id (str): UUID do usuário destino (Cliente, Profissional ou Gestor).
        papel_destinatario (str): Perfil do destinatário no evento ('cliente', 'profissional', 'empresa_gestor').
        beneficiario_id (int, optional): Chave estrangeira da persona/beneficiário atendido (ClienteBeneficiario).
        titulo (str): Título resumido do alerta ou mensagem.
        mensagem (str): Descrição detalhada da notificação.
        tipo_evento (str): Categoria funcional ('atraso_cliente', 'atraso_profissional', 'troca_cadeira', etc.).
        nivel (str): Criticidade do alerta ('info', 'warning', 'danger', 'success').
        payload_extra (dict, optional): Campo JSON para expansões futuras ou metadados da API/App.
        lida (bool): Status de leitura da notificação (Default: False).
        data_leitura (datetime, optional): Timestamp exato em que o destinatário leu o registro.
        criado_em (datetime): Timestamp de criação do registro (UTC).
    """

    __tablename__ = 'agh_notificacoes'

    # --- IDENTIFICAÇÃO ÚNICA (UUID) ---
    id = db.Column(
        db.String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
        comment='Chave primária em formato UUID v4 (CHAR 36)'
    )

    # --- ESCOPO MULTI-TENANT E CONTEXTO DE AGENDAMENTO ---
    empresa_id = db.Column(
        db.Integer,
        db.ForeignKey('ese_empresa.id'),
        nullable=False,
        index=True,
        comment='ID da empresa/unidade proprietária do contexto (ESE)'
    )
    agendamento_id = db.Column(
        db.Integer,
        db.ForeignKey('agh_agendamento.id'),
        nullable=True,
        index=True,
        comment='ID do agendamento relacionado na tabela agh_agendamento'
    )

    # --- PARTICIPANTES (REMETENTE, DESTINATÁRIO E PAPEL) ---
    remetente_id = db.Column(
        db.String(36),
        db.ForeignKey('usuario.id'),
        nullable=True,
        index=True,
        comment='UUID (CHAR 36) do usuário que originou o evento (Nulo se for disparado pelo sistema)'
    )
    destinatario_id = db.Column(
        db.String(36),
        db.ForeignKey('usuario.id'),
        nullable=False,
        index=True,
        comment='UUID (CHAR 36) do usuário final que deve receber o alerta'
    )
    papel_destinatario = db.Column(
        db.String(20),
        nullable=False,
        default='cliente',
        index=True,
        comment='Contexto do destinatário: "cliente", "profissional" ou "empresa_gestor"'
    )

    # --- PERSONA / BENEFICIÁRIO ATENDIDO (PET, VEÍCULO, HUMANO DEPENDENTE) ---
    beneficiario_id = db.Column(
        db.Integer,
        db.ForeignKey('cliente_beneficiarios.id'),
        nullable=True,
        index=True,
        comment='Vínculo com o beneficiário/persona envolvida na prestação do serviço'
    )

    # --- CONTEÚDO E CLASSIFICAÇÃO DA NOTIFICAÇÃO ---
    titulo = db.Column(
        db.String(150),
        nullable=False,
        comment='Título curto exibido nos painéis e push notifications'
    )
    mensagem = db.Column(
        db.Text,
        nullable=False,
        comment='Corpo de texto descritivo da notificação'
    )
    tipo_evento = db.Column(
        db.String(50),
        nullable=False,
        index=True,
        comment='Gatilho/Evento: "atraso_cliente", "reagendamento", "troca_cadeira", "checkin", etc.'
    )
    nivel = db.Column(
        db.String(20),
        default='info',
        nullable=False,
        comment='Impacto visual no Front-end: "info", "warning", "danger", "success"'
    )
    payload_extra = db.Column(
        db.JSON,
        nullable=True,
        comment='Dicionário JSON flexível para parâmetros extras de rotas, deep links de app, etc.'
    )

    # --- AUDITORIA E ESTADO DE LEITURA ---
    lida = db.Column(
        db.Boolean,
        default=False,
        nullable=False,
        index=True,
        comment='Flag indicadora de leitura (False = Não lida, True = Lida)'
    )
    data_leitura = db.Column(
        db.DateTime,
        nullable=True,
        comment='Data e hora do registro de leitura'
    )
    criado_em = db.Column(
        db.DateTime,
        default=datetime.utcnow,
        nullable=False,
        index=True,
        comment='Data e hora do disparo/criação do registro (UTC)'
    )

    # --- RELACIONAMENTOS DO SQLALCHEMY ---
    beneficiario = db.relationship('ClienteBeneficiario', foreign_keys=[beneficiario_id], lazy='joined')
    remetente = db.relationship('Usuario', foreign_keys=[remetente_id], lazy='select')
    destinatario = db.relationship('Usuario', foreign_keys=[destinatario_id], lazy='select')

    def marcar_como_lida(self):
        """Atualiza a notificação como lida e preenche o timestamp de leitura."""
        if not self.lida:
            self.lida = True
            self.data_leitura = datetime.utcnow()

    def __repr__(self):
        return (
            f"<AghNotificacao id={self.id} | Empresa={self.empresa_id} | "
            f"Evento={self.tipo_evento} | Papel={self.papel_destinatario} | Lida={self.lida}>"
        )


class AghAgendamentoEncerramento(db.Model):
    __tablename__ = 'agh_agendamento_encerramentos'

    id = db.Column(db.Integer, primary_key=True)

    # Relacionamentos Principais (Foreign Keys)
    agendamento_id = db.Column(db.Integer, db.ForeignKey('agh_agendamento.id'), nullable=False, unique=True)
    empresa_id = db.Column(db.Integer, db.ForeignKey('ese_empresa.id'), nullable=False)
    colaborador_contrato_id = db.Column(db.Integer, db.ForeignKey('colaborador_contratos.id'), nullable=True)

    # Horários Reais do Atendimento
    data_hora_inicio_real = db.Column(db.DateTime, nullable=True)
    data_hora_fim_real = db.Column(db.DateTime, nullable=True, default=lambda: datetime.now(ZoneInfo('America/Sao_Paulo')))

    # Campos de Encerramento Financeiro e Operacional
    observacoes = db.Column(db.Text, nullable=True)
    valor_final_cobrado = db.Column(db.Numeric(10, 2), nullable=True)
    forma_pagamento = db.Column(db.String(50), nullable=True)

    # Campos de Auditabilidade de Cancelamento / Ausência / Falta
    motivo_cancelamento = db.Column(db.String(255), nullable=True)
    cancelado_por_id = db.Column(db.String(36), db.ForeignKey('mod_cadastro_cliente.id'), nullable=True)
    data_cancelamento = db.Column(db.DateTime, nullable=True)

    # Relacionamentos ORM
    agendamento = db.relationship('AghAgendamento', backref=db.backref('encerramento', uselist=False))
    colaborador_contrato = db.relationship('ColaboradorContrato', backref='atendimentos_encerrados')
    cancelado_por = db.relationship('ModCadastroCliente', foreign_keys=[cancelado_por_id])

    def __repr__(self):
        data_fim_str = self.data_hora_fim_real.strftime('%Y-%m-%d %H:%M') if self.data_hora_fim_real else 'N/A'
        return f"<AghAgendamentoEncerramento AgendamentoID={self.agendamento_id} EmpresaID={self.empresa_id} FimReal={data_fim_str}>"

    @classmethod
    def iniciar_atendimento_servico(cls, agendamento_id: int, empresa_id: int, colaborador_contrato_id: int):
        """Executa a transação de início de atendimento mantendo a rastreabilidade do horário real e auditoria de pontualidade."""
        tz_sp = ZoneInfo('America/Sao_Paulo')
        agora_sp = datetime.now(tz_sp)

        # 1. Busca e valida o agendamento
        agendamento = AghAgendamento.query.filter_by(
            id=agendamento_id, empresa_id=empresa_id
        ).first()

        if not agendamento:
            return {
                'sucesso': False,
                'mensagem': 'Agendamento não encontrado.',
            }, 404

        # Trava de segurança contra reinício de atendimentos já iniciados/finalizados
        status_permitidos = ['agendado', 'confirmado', 'aguardando', 'soft_lock']
        if agendamento.status not in status_permitidos:
            return {
                'sucesso': False,
                'mensagem': f'Não é possível iniciar um atendimento com status "{agendamento.status}".',
            }, 400

        try:
            # 2. Atualiza Status do Agendamento
            agendamento.status = 'em_atendimento'

            # 3. Cria ou Atualiza o Registro de Encerramento (Início Real)
            encerramento = cls.query.filter_by(
                agendamento_id=agendamento.id
            ).first()

            if not encerramento:
                encerramento = cls(
                    agendamento_id=agendamento.id,
                    empresa_id=empresa_id,
                    colaborador_contrato_id=colaborador_contrato_id,
                    data_hora_inicio_real=agora_sp,
                )
                db.session.add(encerramento)
            else:
                encerramento.colaborador_contrato_id = colaborador_contrato_id
                encerramento.data_hora_inicio_real = agora_sp

            # 4. Cálculo do Desvio de Horário para Auditoria (Presença)
            data_agendada = agendamento.data_hora_inicio
            if data_agendada.tzinfo is None:
                data_agendada = data_agendada.replace(tzinfo=tz_sp)

            diferenca_segundos = (agora_sp - data_agendada).total_seconds()
            desvio_minutos = int(diferenca_segundos // 60)

            # 5. Registro na Tabela de Histórico de Presença
            historico_presenca = AghHistoricoPresenca(
                estabelecimento_id=empresa_id,
                cliente_id=agendamento.cliente_id,
                agendamento_id=agendamento.id,
                tipo_evento='inicio_atendimento',
                data_hora_agendada=agendamento.data_hora_inicio,
                data_hora_evento=agora_sp,
                desvio_minutos=desvio_minutos,
                motivo='Início de atendimento acionado na grade',
            )
            db.session.add(historico_presenca)

            # Efetiva a transação no banco
            db.session.commit()

            return {
                'sucesso': True,
                'mensagem': 'Atendimento iniciado com sucesso!',
                'data_hora_inicio': agora_sp.strftime('%H:%M:%S'),
                'desvio_minutos': desvio_minutos,
            }, 200

        except Exception as e:
            db.session.rollback()
            return {
                'sucesso': False,
                'mensagem': f'Erro ao processar início do atendimento: {str(e)}',
            }, 500

    @classmethod
    def finalizar_atendimento_servico(cls, agendamento_id: int, empresa_id: int = None, dados_encerramento: dict = None, **kwargs):
        """Finaliza o atendimento, grava horários reais e persiste a observação de encerramento."""
        tz_sp = ZoneInfo('America/Sao_Paulo')
        agora_sp = datetime.now(tz_sp)
        dados = dados_encerramento or {}

        # Busca observações tanto no dicionário quanto em kwargs soltos
        obs_bruta = (
            dados.get('observacoes') or
            dados.get('observacao') or
            dados.get('inputTexto') or
            kwargs.get('observacoes') or
            kwargs.get('observacao')
        )
        texto_obs = obs_bruta.strip() if isinstance(obs_bruta, str) and obs_bruta.strip() else None

        # 1. Busca o agendamento
        query = AghAgendamento.query.filter_by(id=agendamento_id)
        if empresa_id:
            query = query.filter_by(empresa_id=empresa_id)

        agendamento = query.first()
        if not agendamento:
            return {'sucesso': False, 'mensagem': 'Agendamento não encontrado.'}, 404

        try:
            # 2. Atualiza status do agendamento
            agendamento.status = 'concluido'

            # 3. Busca ou instancia o registro de encerramento
            encerramento = cls.query.filter_by(agendamento_id=agendamento.id).first()
            id_empresa_efetiva = empresa_id or agendamento.empresa_id

            if not encerramento:
                encerramento = cls(
                    agendamento_id=agendamento.id,
                    empresa_id=id_empresa_efetiva,
                    colaborador_contrato_id=agendamento.colaborador_contrato_id,
                    data_hora_inicio_real=agora_sp
                )
                db.session.add(encerramento)

            # 4. Atribui a observação e data de encerramento
            encerramento.data_hora_fim_real = agora_sp
            encerramento.observacoes = texto_obs

            val_final = dados.get('valor_final') or dados.get('valor_final_cobrado') or kwargs.get('valor_final')
            if val_final is not None:
                encerramento.valor_final_cobrado = val_final

            forma_pag = dados.get('forma_pagamento') or kwargs.get('forma_pagamento')
            if forma_pag is not None:
                encerramento.forma_pagamento = forma_pag

            # 5. Registro na Tabela de Histórico de Presença (Fim de Atendimento)
            historico_presenca = AghHistoricoPresenca(
                estabelecimento_id=id_empresa_efetiva,
                cliente_id=agendamento.cliente_id,
                agendamento_id=agendamento.id,
                tipo_evento='fim_atendimento',
                data_hora_agendada=agendamento.data_hora_fim,
                data_hora_evento=agora_sp,
                desvio_minutos=0,
                motivo='Encerramento de atendimento registrado com sucesso',
            )
            db.session.add(historico_presenca)

            db.session.add(encerramento)
            db.session.commit()

            return {
                'sucesso': True,
                'mensagem': 'Atendimento finalizado com sucesso!',
                'data_hora_fim': agora_sp.strftime('%H:%M:%S')
            }, 200

        except Exception as e:
            db.session.rollback()
            return {
                'sucesso': False,
                'mensagem': f'Erro ao finalizar atendimento: {str(e)}'
            }, 500


class AghSolicitacaoReagendamento(db.Model):
  __tablename__ = 'agh_solicitacao_reagendamento'

  id = db.Column(db.Integer, primary_key=True)

  # O agendamento QUE ESTÁ SENDO alterado
  agendamento_id = db.Column(db.Integer, db.ForeignKey('agh_agendamento.id'), nullable=False)

  # O NOVO agendamento gerado (preenchido quando aprovado/efetivado)
  novo_agendamento_id = db.Column(db.Integer, db.ForeignKey('agh_agendamento.id'), nullable=True)

  nova_data = db.Column(db.Date, nullable=False)
  novo_horario = db.Column(db.Time, nullable=False)
  novo_profissional_id = db.Column(db.Integer, nullable=True)

  justificativa = db.Column(db.Text, nullable=True)
  origem = db.Column(db.String(20), default='cliente')  # 'cliente' ou 'colaborador'
  fora_do_prazo = db.Column(db.Boolean, default=False)

  # Estados da solicitação: 'pendente', 'aprovada', 'recusada', 'cancelada'
  status_solicitacao = db.Column(db.String(20), default='pendente')

  resposta_colaborador = db.Column(db.Text, nullable=True)
  analisado_por_id = db.Column(db.Integer, nullable=True)
  analisado_em = db.Column(db.DateTime, nullable=True)

  created_at = db.Column(db.DateTime, default=datetime.utcnow)

class AghCancelamento(db.Model):
  """
  Registra a solicitação e o histórico de cancelamento de agendamentos.
  Preserva dados para auditoria, métricas e posterior conciliação financeira/reembolso.
  """
  __tablename__ = 'agh_cancelamentos'

  id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))

  # Vínculo com o agendamento
  agendamento_id = db.Column(db.Integer, db.ForeignKey('agh_agendamento.id'), nullable=False, index=True)
  empresa_id = db.Column(db.Integer, db.ForeignKey('ese_empresa.id'), nullable=False, index=True)

  # QUEM cancelou
  solicitante_id = db.Column(db.String(36), db.ForeignKey('usuario.id'), nullable=False, index=True)
  origem_solicitacao = db.Column(db.String(20), nullable=False,
                                 default='cliente')  # 'cliente', 'gestor', 'colaborador'

  # MOTIVO E TRATATIVA
  motivo_categoria = db.Column(db.String(50), nullable=False,
                               default='outro')  # 'desistencia', 'imprevisto', 'preco', etc.
  motivo_detalhado = db.Column(db.Text, nullable=True)

  # ESTRUTURA PARA FINANCEIRO (FASE FUTURA - Campos prontos)
  valor_pago_momento = db.Column(db.Numeric(10, 2), default=0.00, nullable=False)
  requer_ressarcimento = db.Column(db.Boolean, default=False, nullable=False)
  valor_ressarcido = db.Column(db.Numeric(10, 2), default=0.00, nullable=True)
  status_ressarcimento = db.Column(db.String(20),
                                   default='nao_aplicavel')  # 'pendente', 'concluido', 'recusado', 'nao_aplicavel'
  data_ressarcimento = db.Column(db.DateTime, nullable=True)

  # AUDITORIA DE DATA/HORA
  solicitado_em = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
  criado_em = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

  # Relacionamento com Agendamento
  agendamento = db.relationship('AghAgendamento', backref=db.backref('cancelamento', uselist=False))

  def __repr__(self):
      return f"<AghCancelamento Agendamento={self.agendamento_id} Solicitante={self.solicitante_id}>"