# feedin/modules/agenda/models.py
import random
import uuid
import logging
from sqlalchemy import func
from sqlalchemy.orm import synonym
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

from datetime import datetime, timezone

# models.py

class AghAgendamento(db.Model):
    __tablename__ = 'agh_agendamento'
    __table_args__ = {'extend_existing': True}

    id = db.Column(db.Integer, primary_key=True)
    empresa_id = db.Column(db.Integer, db.ForeignKey('ese_empresa.id'), nullable=False, index=True)
    cliente_id = db.Column(db.String(36), db.ForeignKey('mod_cadastro_cliente.id'), nullable=True, index=True)
    beneficiario_id = db.Column(db.Integer, db.ForeignKey('cliente_beneficiarios.id'), nullable=True, index=True)

    valor_total = db.Column(db.Numeric(10, 2), nullable=False, default=0.00)
    data_hora_inicio = db.Column(db.DateTime, nullable=False, index=True)  # Horário do 1º serviço
    data_hora_fim = db.Column(db.DateTime, nullable=False)                 # Horário final do último serviço

    status = db.Column(db.String(30), default='agendado', nullable=False, index=True)
    tipo_origem = db.Column(db.String(20), default='online')
    session_token = db.Column(db.String(100), nullable=True, index=True)
    expira_em = db.Column(db.DateTime, nullable=True)

    criado_em = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    # RELACIONAMENTOS (ORM)
    empresa = db.relationship('EseEmpresa', backref='agendamentos', lazy=True)
    cliente = db.relationship('ModCadastroCliente', backref='agendamentos', lazy=True)
    beneficiario = db.relationship('ClienteBeneficiario', foreign_keys=[beneficiario_id], lazy=True)

    # VINCULAÇÃO COM ITENS (Múltiplos serviços / profissionais)
    itens = db.relationship(
        'AghAgendamentoItem',
        back_populates='agendamento',
        cascade='all, delete-orphan',
        lazy='joined',
        order_by='AghAgendamentoItem.ordem_execucao'
    )

    def recalcular_total(self):
        self.valor_total = sum(
            item.preco_unitario or 0
            for item in self.itens
            if getattr(item, 'status_item', None) != 'cancelado'
        )
        return self.valor_total

    @property
    def total_reagendamentos(self):
        """Retorna o número de vezes que este agendamento foi reagendado com sucesso."""
        return AghSolicitacaoReagendamento.query.filter_by(
            agendamento_id=self.id,
            status_solicitacao='aprovada'
        ).count()


class AghAgendamentoItem(db.Model):
    __tablename__ = 'agh_agendamento_item'
    __table_args__ = {'extend_existing': True}

    id = db.Column(db.Integer, primary_key=True)
    agendamento_id = db.Column(
        db.Integer,
        db.ForeignKey('agh_agendamento.id', ondelete='CASCADE'),
        nullable=False,
        index=True
    )

    servico_id = db.Column(
        db.Integer,
        db.ForeignKey('ese_servico_oferecido.id'),
        nullable=False,
        index=True
    )

    # 👤 PROFISSIONAL / COLABORADOR MANDATÓRIO DO ITEM (UUID em CHAR(36))
    colaborador_id_contrato = db.Column(
        db.String(36),
        db.ForeignKey('colaborador_contratos.id', ondelete='RESTRICT'),
        nullable=False,
        index=True
    )

    preco_unitario = db.Column(db.Numeric(10, 2, asdecimal=False), nullable=False, default=0.00)
    duracao_minutos = db.Column(db.Integer, nullable=False, default=30)

    # 🎯 CONTROLE OPERACIONAL DE SEQUÊNCIA E SLOT DE HORÁRIO
    ordem_execucao = db.Column(db.Integer, default=1, nullable=False, index=True)

    # 'sequencial' = executa após a conclusão do item anterior
    # 'simultaneo' = executa no mesmo slot/horário do item anterior/agendamento
    modo_execucao = db.Column(db.String(20), nullable=False, default='sequencial')

    # HORÁRIOS ESPECÍFICOS DO ITEM NA GRADE
    data_hora_inicio = db.Column(db.DateTime, nullable=False)
    data_hora_fim = db.Column(db.DateTime, nullable=False)

    # EXECUÇÃO REAL E STATUS INDIVIDUAL
    status_item = db.Column(db.String(20), nullable=False, default='pendente')
    data_hora_inicio_real = db.Column(db.DateTime, nullable=True)
    data_hora_fim_real = db.Column(db.DateTime, nullable=True)
    observacao_item = db.Column(db.String(255), nullable=True)

    # RELACIONAMENTOS (ORM) SANEADOS E UNIFICADOS
    agendamento = db.relationship(
        'AghAgendamento',
        back_populates='itens',
        foreign_keys=[agendamento_id],
        overlaps="agendamento_pai"
    )
    servico = db.relationship('EseServicoOferecido', lazy='joined')
    profissional = db.relationship(
        'ColaboradorContrato',
        foreign_keys=[colaborador_id_contrato],
        lazy='joined'
    )

    @property
    def profissional_id(self) -> str:
        """Alias para compatibilidade de leitura com scripts legados."""
        return str(self.colaborador_id_contrato) if self.colaborador_id_contrato else None

    @property
    def agendamento_pai(self):
        """
        Alias de retrocompatibilidade para códigos legados que ainda leiam item.agendamento_pai.
        """
        return self.agendamento

    @property
    def nome_servico(self) -> str:
        if self.servico and getattr(self.servico, 'nome', None):
            return self.servico.nome
        return f"Serviço #{self.servico_id}"

    @property
    def nome_profissional(self) -> str:
        if not self.profissional:
            return f"Profissional {self.colaborador_id_contrato or 'N/A'}"

        # 1. Tenta atributos diretos do contrato
        for attr in ['nome_exibicao', 'nome', 'nome_completo']:
            val = getattr(self.profissional, attr, None)
            if val:
                return val

        # 2. Tenta recuperar através da relação cadastro_modulo
        if hasattr(self.profissional, 'cadastro_modulo') and self.profissional.cadastro_modulo:
            nome_mod = getattr(self.profissional.cadastro_modulo, 'nome', None) or getattr(
                self.profissional.cadastro_modulo, 'razao_social', None)
            if nome_mod:
                return nome_mod

        # 3. Tenta recuperar através do relacionamento com o usuário base
        if hasattr(self.profissional, 'usuario') and self.profissional.usuario:
            nome_usr = getattr(self.profissional.usuario, 'nome', None)
            if nome_usr:
                return nome_usr

        return f"Profissional {self.colaborador_id_contrato}"

    def to_dict(self) -> dict:
        cliente_nome = "Cliente"
        if self.agendamento and getattr(self.agendamento, 'cliente', None):
            cliente_nome = getattr(self.agendamento.cliente, 'nome', None) or getattr(
                self.agendamento.cliente, 'razao_social', None
            ) or "Cliente"

        return {
            "id": self.id,
            "agendamento_id": self.agendamento_id,
            "ordem_execucao": self.ordem_execucao,
            "modo_execucao": self.modo_execucao,
            "servico_id": self.servico_id,
            "servico_nome": self.nome_servico,
            "colaborador_id_contrato": str(self.colaborador_id_contrato) if self.colaborador_id_contrato else None,
            "profissional_id": str(self.colaborador_id_contrato) if self.colaborador_id_contrato else None,
            "profissional_nome": self.nome_profissional,
            "cliente_nome": cliente_nome,
            "preco_unitario": float(self.preco_unitario) if self.preco_unitario is not None else 0.0,
            "duracao_minutos": self.duracao_minutos,
            "data_hora_inicio": self.data_hora_inicio.isoformat() if self.data_hora_inicio else None,
            "data_hora_fim": self.data_hora_fim.isoformat() if self.data_hora_fim else None,
            "status_item": self.status_item,
            "observacao_item": self.observacao_item
        }

    @staticmethod
    def iniciar_atendimento_servico(agendamento_id, agendamento_item_id, empresa_id, usuario_id, colaborador_id_contrato=None):
        try:
            # 1. Carrega o item específico
            item = AghAgendamentoItem.query.get(agendamento_item_id)
            if not item:
                return {"status": "error", "mensagem": "Item de agendamento não encontrado."}, 404

            tz_sp = ZoneInfo('America/Sao_Paulo')
            agora = datetime.now(tz_sp)

            # Atualiza o colaborador caso tenha sido explicitamente redefinido na abertura
            if colaborador_id_contrato:
                item.colaborador_id_contrato = str(colaborador_id_contrato).strip()

            # 2. Atualiza APENAS o estado do item do serviço
            item.status_item = 'em_atendimento'
            item.data_hora_inicio_real = agora

            # 3. Atualiza o status global do Agendamento PAI (se ainda for 'agendado'/'pendente')
            agendamento_pai = item.agendamento
            if agendamento_pai and getattr(agendamento_pai, 'status_slug', None) not in ['em_atendimento', 'concluido', 'finalizado']:
                agendamento_pai.status_slug = 'em_atendimento'

            # 4. Resgate do ID (CHAR 36) do colaborador associado
            colaborador_uuid = str(item.colaborador_id_contrato).strip() if item.colaborador_id_contrato else None

            # 5. Registra/Recupera a sessão de encerramento SOMENTE para este item
            encerramento_item = AghAgendamentoEncerramento.query.filter_by(
                agendamento_item_id=item.id
            ).first()

            if not encerramento_item:
                encerramento_item = AghAgendamentoEncerramento(
                    agendamento_id=agendamento_id,
                    agendamento_item_id=item.id,
                    empresa_id=empresa_id,
                    colaborador_id_contrato=colaborador_uuid,  # 🟢 Gravação em String (CHAR 36)
                    data_hora_inicio=agora,
                    status='em_atendimento',
                    criado_por=str(usuario_id)
                )
                db.session.add(encerramento_item)

            db.session.commit()
            return {"status": "success", "mensagem": f"Serviço #{item.id} iniciado com sucesso."}, 200

        except Exception as e:
            db.session.rollback()
            return {"status": "error", "mensagem": f"Erro interno ao processar início: {str(e)}"}, 500

    profissional_id = synonym('colaborador_id_contrato')


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
    )

    # 👤 PROFISSIONAL E HORÁRIO (Definidos no Passo 2)
    profissional_id = db.Column(
        db.Integer,
        db.ForeignKey('colaborador_contratos.id'),
        nullable=True,
        index=True,
    )
    data_hora_inicio = db.Column(db.DateTime, nullable=True, index=True)
    data_hora_fim = db.Column(db.DateTime, nullable=True)

    # 💰 VALOR TOTAL
    valor_total = db.Column(db.Numeric(10, 2), nullable=False, default=0.00)

    # ⚙️ MÁQUINA DE ESTADO DO RASCUNHO
    status = db.Column(
        db.String(30), default='servicos_selecionados', nullable=False
    )

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

    # MÉTODOS ÚTEIS
    def recalcular_total(self):
        """Soma o valor total com base nos itens atrelados."""
        total = sum(
            item.preco_unitario for item in self.itens if item.preco_unitario
        )
        self.valor_total = total
        return total

    @property
    def duracao_total_minutos(self):
        """Calcula o tempo total estimado somando as durações dos itens do rascunho."""
        return sum(item.duracao_minutos or 0 for item in self.itens)

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

    # 📌 CORRIGIDO: Aponta corretamente para a tabela ese_servico_oferecido
    servico_id = db.Column(
        db.Integer,
        db.ForeignKey('ese_servico_oferecido.id'),
        nullable=False,
        index=True,
    )

    preco_unitario = db.Column(
        db.Numeric(10, 2), nullable=False, default=0.00
    )
    duracao_minutos = db.Column(db.Integer, nullable=False, default=30)

    # RELACIONAMENTOS (ORM)
    servico = db.relationship('EseServicoOferecido', lazy=True)

    def __repr__(self):
        return f'<AghAgendamentoRascunhoItem Rascunho #{self.rascunho_id} -> Servico #{self.servico_id}>'


class AghAvaliacaoServico(db.Model):
  __tablename__ = 'agh_avaliacao_servico'
  __table_args__ = {'extend_existing': True}

  id = db.Column(db.Integer, primary_key=True)
  agendamento_id = db.Column(
      db.Integer, db.ForeignKey('agh_agendamento.id'), nullable=False
  )

  # 1. Adicione a ForeignKey apontando para a tabela do item de agendamento:
  agendamento_item_id = db.Column(
      db.Integer, db.ForeignKey('agh_agendamento_item.id'), nullable=True
  )

  nota = db.Column(db.Integer, nullable=False)
  comentario = db.Column(db.Text, nullable=True)

  # 2. Mantém o relacionamento com a ForeignKey mapeada
  item = db.relationship('AghAgendamentoItem', backref='avaliacoes')


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

  # Adicione este método dentro da classe AghHistoricoPresenca

  @classmethod
  def registrar_evento(cls, empresa_id, cliente_id, agendamento_id, tipo_evento, data_agendada, desvio=0, creditos=0.0,
                       motivo=None):
      from datetime import datetime, timezone
      log = cls(
          estabelecimento_id=empresa_id,
          cliente_id=cliente_id,
          agendamento_id=agendamento_id,
          tipo_evento=tipo_evento,
          data_hora_agendada=data_agendada,
          data_hora_evento=datetime.now(timezone.utc),
          desvio_minutos=desvio,
          creditos_pontualidade=creditos,
          motivo=motivo
      )
      db.session.add(log)
      return log

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

    taxa_agendamento = db.Column(db.Numeric(10, 2), default=0.00,
                                 nullable=False)  # 🆕 Taxa fixa de segurança (monetário)

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

    @classmethod
    def disparar(cls, **kwargs):
        # Lógica para criar/enviar a notificação
        nova_notificacao = cls(**kwargs)
        db.session.add(nova_notificacao)
        db.session.commit()
        return nova_notificacao

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


from zoneinfo import ZoneInfo
from datetime import datetime


class AghAgendamentoEncerramento(db.Model):
    __tablename__ = 'agh_agendamento_encerramentos'

    __table_args__ = (
        db.Index('idx_agendamento_item_enc', 'agendamento_id', 'agendamento_item_id'),
    )

    # Chave Primária própria (UUID)
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))

    # Estruturas relacionais numéricas (Integer)
    agendamento_id = db.Column(db.Integer, db.ForeignKey('agh_agendamento.id'), nullable=False)
    agendamento_item_id = db.Column(db.Integer, db.ForeignKey('agh_agendamento_item.id'), nullable=True)
    empresa_id = db.Column(db.Integer, db.ForeignKey('ese_empresa.id'), nullable=False)

    # Entidades baseadas em UUID / String(36)
    colaborador_contrato_id = db.Column(db.String(36), db.ForeignKey('colaborador_contratos.id'), nullable=True)
    usuario_id = db.Column(db.String(36), db.ForeignKey('mod_cadastro_cliente.id'), nullable=True)

    # Horários e Encerramento Operacional
    data_hora_inicio_real = db.Column(db.DateTime, nullable=True)
    data_hora_fim_real = db.Column(db.DateTime, nullable=True)
    data_hora_encerramento = db.Column(db.DateTime, nullable=True)
    observacoes = db.Column(db.Text, nullable=True)
    nota_avaliacao_cliente = db.Column(db.SmallInteger, nullable=True)

    # Fechamento Financeiro / Caixa
    valor_final_cobrado = db.Column(db.Numeric(10, 2), nullable=True)
    valor_total = db.Column(db.Numeric(10, 2), nullable=True)
    forma_pagamento = db.Column(db.String(50), nullable=True)
    observacao = db.Column(db.Text, nullable=True)

    # Auditabilidade e Cancelamento (UUID)
    motivo_cancelamento = db.Column(db.String(255), nullable=True)
    cancelado_por_id = db.Column(db.String(36), db.ForeignKey('mod_cadastro_cliente.id'), nullable=True)
    data_cancelamento = db.Column(db.DateTime, nullable=True)

    # Relacionamentos ORM
    agendamento = db.relationship('AghAgendamento', backref=db.backref('encerramentos', lazy='dynamic'))
    agendamento_item = db.relationship('AghAgendamentoItem', backref=db.backref('encerramento', uselist=False))
    colaborador_contrato = db.relationship('ColaboradorContrato', backref='atendimentos_encerrados')

    # Relacionamentos mapeados para ModCadastroCliente
    usuario = db.relationship('ModCadastroCliente', foreign_keys=[usuario_id], backref='encerramentos_realizados')
    cancelado_por = db.relationship('ModCadastroCliente', foreign_keys=[cancelado_por_id], backref='encerramentos_cancelados')

    @classmethod
    def iniciar_atendimento_servico(cls, agendamento_id: int, agendamento_item_id: int, empresa_id: int,
                                    colaborador_contrato_id: str = None, usuario_id: str = None):
        """
        Inicia o atendimento do serviço:
        1. Altera o status do agendamento pai para 'em_atendimento'.
        2. Altera o status_item em AghAgendamentoItem para 'em_andamento'.
        3. Registra/Atualiza o inicio real em AghAgendamentoEncerramento (sem marcar encerramento).
        """
        tz_sp = ZoneInfo('America/Sao_Paulo')
        agora_sp = datetime.now(tz_sp)

        agendamento = AghAgendamento.query.filter_by(id=agendamento_id, empresa_id=empresa_id).first()
        if not agendamento:
            return {'sucesso': False, 'mensagem': 'Agendamento não encontrado.'}, 404

        status_permitidos = ['agendado', 'confirmado', 'aguardando', 'soft_lock', 'pendente', 'ausente_pendente']
        st_agendamento = str(getattr(agendamento, 'status', '')).lower().strip()

        if st_agendamento not in status_permitidos and st_agendamento != 'em_atendimento':
            return {'sucesso': False, 'mensagem': f'Não é possível iniciar com status "{agendamento.status}".'}, 400

        try:
            # 1. Transição de status do Agendamento Pai
            agendamento.status = 'em_atendimento'

            # 2. Transição do Item Específico na AghAgendamentoItem
            item = AghAgendamentoItem.query.get(agendamento_item_id)
            if item:
                item.status_item = 'em_andamento'
                if not item.data_hora_inicio_real:
                    item.data_hora_inicio_real = agora_sp

            # 3. Registro / Upsert do início operacional na AghAgendamentoEncerramento
            encerramento = cls.query.filter_by(
                agendamento_id=agendamento_id,
                agendamento_item_id=agendamento_item_id
            ).first()

            if not encerramento:
                encerramento = cls(
                    agendamento_id=agendamento_id,
                    agendamento_item_id=agendamento_item_id,
                    empresa_id=empresa_id,
                    colaborador_contrato_id=colaborador_contrato_id,
                    usuario_id=usuario_id,
                    data_hora_inicio_real=agora_sp,
                    # Mantém data_hora_encerramento e data_hora_fim_real estritamente como None
                )
                db.session.add(encerramento)
            else:
                encerramento.colaborador_contrato_id = colaborador_contrato_id
                if usuario_id:
                    encerramento.usuario_id = usuario_id
                encerramento.data_hora_inicio_real = agora_sp

            # 4. Histórico de Presença / Auditoria
            data_agendada = agendamento.data_hora_inicio
            if data_agendada:
                if data_agendada.tzinfo is None:
                    data_agendada = data_agendada.replace(tzinfo=tz_sp)
                desvio_minutos = int((agora_sp - data_agendada).total_seconds() // 60)
            else:
                desvio_minutos = 0

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

            db.session.commit()

            return {
                'sucesso': True,
                'mensagem': 'Atendimento iniciado com sucesso!',
                'data_hora_inicio': agora_sp.strftime('%H:%M:%S'),
                'desvio_minutos': desvio_minutos,
            }, 200

        except Exception as e:
            db.session.rollback()
            return {'sucesso': False, 'mensagem': f'Erro ao processar início: {str(e)}'}, 500

    @classmethod
    def encerrar_atendimento_servico(cls, agendamento_id: int, agendamento_item_id: int, empresa_id: int):
        """
        Conclui o serviço individual:
        1. Atualiza AghAgendamentoItem para 'concluido'.
        2. Atualiza AghAgendamentoEncerramento marcando data_hora_encerramento e valores.
        3. Verifica se restam itens em andamento/pendentes para liberar a comanda global.
        """
        item = AghAgendamentoItem.query.get(agendamento_item_id)
        if not item:
            return {'sucesso': False, 'mensagem': 'Item de agendamento não encontrado.'}, 404

        tz_sp = ZoneInfo('America/Sao_Paulo')
        agora_sp = datetime.now(tz_sp)

        try:
            # 1. Atualização da fonte da verdade do Item
            item.status_item = 'concluido'
            item.data_hora_fim_real = agora_sp

            valor_item = float(getattr(item, 'preco_unitario', 0.0) or getattr(item, 'valor', 0.0) or 0.0)

            # 2. Atualização / Preenchimento dos dados de Fechamento no Encerramento
            encerramento = cls.query.filter_by(
                agendamento_id=agendamento_id,
                agendamento_item_id=agendamento_item_id
            ).first()

            if encerramento:
                encerramento.data_hora_fim_real = agora_sp
                encerramento.data_hora_encerramento = agora_sp  # 👈 Atributo fundamental para validar conclusão
                if not encerramento.valor_total or encerramento.valor_total == 0:
                    encerramento.valor_total = valor_item
                    encerramento.valor_final_cobrado = valor_item
            else:
                encerramento = cls(
                    agendamento_id=agendamento_id,
                    agendamento_item_id=agendamento_item_id,
                    empresa_id=empresa_id,
                    colaborador_contrato_id=getattr(item, 'profissional_id', None),
                    data_hora_inicio_real=item.data_hora_inicio_real or agora_sp,
                    data_hora_fim_real=agora_sp,
                    data_hora_encerramento=agora_sp,
                    valor_total=valor_item,
                    valor_final_cobrado=valor_item
                )
                db.session.add(encerramento)

            # 3. Checagem de itens pendentes no Agendamento Pai
            itens_pendentes_totais = AghAgendamentoItem.query.filter(
                AghAgendamentoItem.agendamento_id == agendamento_id,
                db.func.lower(AghAgendamentoItem.status_item) != 'concluido'
            ).count()

            agendamento_totalmente_concluido = (itens_pendentes_totais == 0)

            # Se TODOS os itens do agendamento foram concluídos, atualiza o status pai
            if agendamento_totalmente_concluido:
                agendamento = AghAgendamento.query.get(agendamento_id)
                if agendamento:
                    agendamento.status = 'concluido'  # Pronto para 'Liquidar Comanda' / Financeiro

            db.session.commit()

            return {
                'sucesso': True,
                'mensagem': 'Serviço concluído com sucesso!',
                'comanda_pronta': agendamento_totalmente_concluido,
                'agendamento_id': agendamento_id
            }, 200

        except Exception as e:
            db.session.rollback()
            return {'sucesso': False, 'mensagem': f'Erro ao encerrar serviço: {str(e)}'}, 500


class AghSolicitacaoReagendamento(db.Model):
    __tablename__ = 'agh_solicitacao_reagendamento'

    id = db.Column(db.Integer, primary_key=True)
    agendamento_id = db.Column(db.Integer, db.ForeignKey('agh_agendamento.id'), nullable=False)
    novo_agendamento_id = db.Column(db.Integer, db.ForeignKey('agh_agendamento.id'), nullable=True)

    nova_data = db.Column(db.Date, nullable=False)
    novo_horario = db.Column(db.Time, nullable=False)
    novo_profissional_id = db.Column(db.Integer, nullable=True)

    justificativa = db.Column(db.Text, nullable=True)
    origem = db.Column(db.String(20), default='cliente')
    fora_do_prazo = db.Column(db.Boolean, default=False)
    status_solicitacao = db.Column(db.String(20), default='pendente')

    resposta_colaborador = db.Column(db.Text, nullable=True)
    analisado_por_id = db.Column(db.Integer, nullable=True)
    analisado_em = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    @property
    def total_reagendamentos(self):
        """Retorna quantos reagendamentos aprovados foram gerados a partir do agendamento pai desta solicitação."""
        return AghSolicitacaoReagendamento.query.filter_by(
            agendamento_id=self.agendamento_id,
            status_solicitacao='aprovada'
        ).count()


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


class AghReordenacaoSolicitada(db.Model):
    __tablename__ = 'agh_reordenacao_solicitada'

    id = db.Column(db.Integer, primary_key=True)
    agendamento_id = db.Column(db.Integer, db.ForeignKey('agh_agendamento.id', ondelete='CASCADE'), nullable=False)
    solicitante_id = db.Column(db.Integer, db.ForeignKey('colaborador_contratos.id'), nullable=False)

    # Payload JSON guardando a nova proposta (ordem_execucao, modo_execucao, etc)
    proposta_json = db.Column(db.JSON, nullable=False)

    # Lista de IDs de contratos que precisam dar o 'de acordo'
    profissionais_pendentes_ids = db.Column(db.JSON, nullable=False)

    status = db.Column(db.String(20), default='pendente', nullable=False)  # pendente, aprovado, rejeitado, cancelado
    data_criacao = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    # Relacionamentos
    agendamento = db.relationship('AghAgendamento')
    solicitante = db.relationship('ColaboradorContrato')