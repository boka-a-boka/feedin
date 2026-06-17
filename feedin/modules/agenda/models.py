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
    db.Column('servico_id', db.Integer, db.ForeignKey('agh_servico.id', ondelete='CASCADE'), primary_key=True)
)


# =====================================================================
# 🏛️ ENTIDADES CORE E PERIFÉRICAS DO MÓDULO
# =====================================================================

class EseEmpresa(db.Model):
    __tablename__ = 'ese_empresa'
    __table_args__ = {'extend_existing': True}

    id = db.Column(db.Integer, primary_key=True)

    # 🔗 OS DOIS ELOS VITAIS:
    # 1. Quem é o dono desta empresa (independente de onde ela esteja fisicamente)
    proprietario_id = db.Column(db.Integer, db.ForeignKey('usuario.id'), nullable=False)
    # 2. Em qual ponto físico essa empresa está operando HOJE (pode mudar no futuro)
    local_id = db.Column(db.Integer, db.ForeignKey('locais.id'), nullable=False)

    nome = db.Column(db.String(100), nullable=False)
    categoria = db.Column(db.String(50))  # Ex: "Barbearia", "Estética"
    slug = db.Column(db.String(100), unique=True)
    logomarca = db.Column(db.String(255))

    # Identidade visual dinâmica do estabelecimento aplicada no PWA
    cor_primaria = db.Column(db.String(7), default="#111827")
    cor_secundaria = db.Column(db.String(7), default="#6B7280")

    # Relacionamentos explícitos para facilitar buscas nas rotas
    proprietario = db.relationship('Usuario', backref=db.backref('minhas_empresas', lazy=True))
    local_fisico = db.relationship('Local', backref=db.backref('empresas_instaladas', lazy=True))

    def __repr__(self):
        return f"<EseEmpresa {self.nome} - Instalada no Local ID: {self.local_id}>"


class AghProfissional(db.Model):
    __tablename__ = 'agh_profissional'
    __table_args__ = {'extend_existing': True}

    id = db.Column(db.Integer, primary_key=True)
    estabelecimento_id = db.Column(db.Integer, db.ForeignKey('ese_empresa.id'), nullable=False)

    # 🔗 O ELO DE SEGURANÇA JURÍDICA E OPERACIONAL:
    # Vincula o perfil de agendamento do cara ao Contrato de Trabalho ativo dele no Core
    contrato_id = db.Column(db.Integer, db.ForeignKey('colaborador_contratos.id'), nullable=True)

    nome = db.Column(db.String(100), nullable=False)
    cargo_especialidade = db.Column(db.String(100))
    is_ativo = db.Column(db.Boolean, default=True)

    # Relacionamentos
    empresa = db.relationship('EseEmpresa', backref=db.backref('profissionais', lazy=True))
    contrato_core = db.relationship('ColaboradorContrato', backref=db.backref('perfil_agenda', uselist=False))

    def __repr__(self):
        return f"<AghProfissional {self.nome} - {self.cargo_especialidade}>"


class AghServico(db.Model):
    """Catálogo de Serviços com foco em diferenciais e clareza para o cliente."""
    __tablename__ = 'agh_servico'
    __table_args__ = {'extend_existing': True}

    id = db.Column(db.Integer, primary_key=True)
    estabelecimento_id = db.Column(db.Integer, db.ForeignKey('ese_empresa.id'), nullable=False)

    nome = db.Column(db.String(100), nullable=False)
    preco = db.Column(db.Numeric(10, 2), nullable=False)
    duracao_minutos = db.Column(db.Integer, nullable=False, default=30)

    descricao = db.Column(db.Text, nullable=True)
    exibir_descricao_pwa = db.Column(db.Boolean, default=True)

    is_ativo = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    agendamentos = db.relationship('AghAgendamento', backref='servico_relacionado', lazy=True)
    avaliacoes = db.relationship('AghAvaliacaoServico', backref='servico_associado', lazy=True)

    def __repr__(self):
        return f"<AghServico {self.nome} (R$ {self.preco})>"


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
    avaliacao = db.relationship('AghAvaliacaoServico', backref='agendamento_avaliado', uselist=False, lazy=True)

    def __repr__(self):
        return f"<AghAgendamento ID {self.id} - Status {self.status}>"


class AghAvaliacaoServico(db.Model):
    """Módulo de Reputação e Evaluation Justa (Métrica + Depoimento)."""
    __tablename__ = 'agh_avaliacao_servico'
    __table_args__ = {'extend_existing': True}

    id = db.Column(db.Integer, primary_key=True)
    agendamento_id = db.Column(db.Integer, db.ForeignKey('agh_agendamento.id'), nullable=False, unique=True)
    cliente_id = db.Column(db.Integer, db.ForeignKey('mod_cadastro_cliente.id'), nullable=False)
    servico_id = db.Column(db.Integer, db.ForeignKey('agh_servico.id'), nullable=False)
    profissional_id = db.Column(db.Integer, db.ForeignKey('agh_profissional.id'), nullable=False)

    nota_servico = db.Column(db.Integer, nullable=False)
    nota_profissional = db.Column(db.Integer, nullable=False)
    comentario = db.Column(db.Text, nullable=True)

    status_moderacao = db.Column(db.String(20), default='aprovado')
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    cliente = db.relationship('ModCadastroCliente', backref='minhas_avaliacoes_feitas')

    def __repr__(self):
        return f"<AghAvaliacao ID {self.id} - Notas S:{self.nota_servico}/P:{self.nota_profissional}>"


class UsuarioFavorito(db.Model):
    """Registra os favoritamentos do usuário para personalizar o FeedIn Negócios."""
    __tablename__ = 'usuario_favorito'
    __table_args__ = (
        db.UniqueConstraint('usuario_id', 'empresa_id', name='unique_usuario_empresa_fav'),
        db.UniqueConstraint('usuario_id', 'segmento_id', name='unique_usuario_segmento_fav'),
        {'extend_existing': True}
    )

    id = db.Column(db.Integer, primary_key=True)
    usuario_id = db.Column(db.Integer, db.ForeignKey('usuario.id'), nullable=False)
    empresa_id = db.Column(db.Integer, db.ForeignKey('ese_empresa.id'), nullable=True)
    segmento_id = db.Column(db.Integer, db.ForeignKey('taxonomia.id'), nullable=True)

    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    def __repr__(self):
        return f"<UsuarioFavorito User:{self.usuario_id} Empresa:{self.empresa_id}>"


class ModHomologacaoEmpresa(db.Model):
    """Tabela de segurança jurídica e compliance do FeedIn."""
    __tablename__ = 'mod_homologacao_empresa'
    __table_args__ = {'extend_existing': True}

    id = db.Column(db.Integer, primary_key=True)
    id_local = db.Column(db.Integer, db.ForeignKey('locais.id'), nullable=False)

    path_comprovante_endereco = db.Column(db.String(255), nullable=False)
    path_cartao_cnpj_ou_social = db.Column(db.String(255), nullable=False)

    data_envio = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    data_analise = db.Column(db.DateTime, nullable=True)
    id_auditor_feedin = db.Column(db.Integer, db.ForeignKey('usuario.id'), nullable=True)

    status_auditoria = db.Column(db.String(20), default='pendente', nullable=False)
    motivo_rejeicao = db.Column(db.Text, nullable=True)

    local = db.relationship('Local', backref='historico_homologacao')

    def __repr__(self):
        return f"<ModHomologacaoEmpresa Local ID: {self.id_local} - Status: {self.status_auditoria}>"