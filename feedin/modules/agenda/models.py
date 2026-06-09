# feedin/modules/agenda/models.py
from feedin import database as db
from datetime import datetime, timezone
from feedin.models import IdentidadeCivil
from cryptography.fernet import Fernet
import os
from cryptography.fernet import Fernet
from feedin import app  # Importa o app que já está inicializado no escopo global

# Em vez de criar um novo Fernet com uma chave solta, usa a instância central do app.
# Adicionamos um fallback seguro para o servidor nunca quebrar caso a chave não esteja no .env.
if hasattr(app, 'fernet'):
    cipher = app.fernet
else:
    # Se por algum motivo o .env não tiver a chave de criptografia, gera uma temporária para não derrubar o PyCharm
    app.logger.warning("Instância app.fernet não encontrada. Criando cipher de fallback temporário.")
    fallback_raw = os.environ.get('CHAVE_CRIPTOGRAFIA_CPF')
    if fallback_raw:
        cipher = Fernet(fallback_raw.encode())
    else:
        # Fallback de segurança absoluto (gera chave randômica temporária)
        cipher = Fernet(Fernet.generate_key())

class AghProfissional(db.Model):
    __tablename__ = 'agh_profissional'
    id = db.Column(db.Integer, primary_key=True)
    estabelecimento_id = db.Column(db.Integer, db.ForeignKey('ese_empresa.id'), nullable=False)
    nome = db.Column(db.String(100), nullable=False)
    cargo_especialidade = db.Column(db.String(100))  # Ex: "Cabeleireiro Master", "Barbeiro"
    is_ativo = db.Column(db.Boolean, default=True)

    # Relacionamento reverso para facilitar buscas
    empresa = db.relationship('EseEmpresa', backref=db.backref('profissionais', lazy=True))


class AghServico(db.Model):
    """Catálogo de Serviços com foco em diferenciais e clareza para o cliente."""
    __tablename__ = 'agh_servico'
    __table_args__ = {'extend_existing': True}

    id = db.Column(db.Integer, primary_key=True)
    estabelecimento_id = db.Column(db.Integer, db.ForeignKey('usuario.id'), nullable=False)

    nome = db.Column(db.String(100), nullable=False)
    preco = db.Column(db.Numeric(10, 2), nullable=False)
    duracao_minutos = db.Column(db.Integer, nullable=False, default=30)

    # O diferencial competitivo e descritivo
    descricao = db.Column(db.Text, nullable=True)  # Detalhes da tratativa, o que inclui, mimos, etc.
    exibir_descricao_pwa = db.Column(db.Boolean, default=True)  # Controle gerencial de exibição

    is_ativo = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Relacionamentos
    agendamentos = db.relationship('AghAgendamento', backref='servico_relacionado', lazy=True)
    avaliacoes = db.relationship('AghAvaliacaoServico', backref='servico', lazy=True)

    def __repr__(self):
        return f"<AghServico {self.nome} (R$ {self.preco})>"


class AghAgendamento(db.Model):
    """Gerenciamento de horários, prazos de 30/37 dias e histórico."""
    __tablename__ = 'agh_agendamento'
    __table_args__ = {'extend_existing': True}

    id = db.Column(db.Integer, primary_key=True)
    profissional_id = db.Column(db.Integer, db.ForeignKey('agh_profissional.id'), nullable=False)
    cliente_id = db.Column(db.Integer, db.ForeignKey('mod_cadastro_cliente.id'), nullable=False)

    # ADAPTAÇÃO SOLICITADA: Chave estrangeira real ligada à tabela de serviços
    servico_id = db.Column(db.Integer, db.ForeignKey('agh_servico.id'), nullable=False)

    preco_cobrado = db.Column(db.Numeric(10, 2), nullable=False)  # Registra o valor real cobrado no dia
    data_hora_inicio = db.Column(db.DateTime, nullable=False)
    data_hora_fim = db.Column(db.DateTime, nullable=False)

    # Status: 'confirmado', 'reagendamento_pendente', 'cancelado', 'perdido', 'finalizado'
    status = db.Column(db.String(30), default='confirmado')
    tipo_origem = db.Column(db.String(20), default='online')  # 'online' ou 'manual'

    # Controle do relógio de 7 dias para reagendamento inativo
    data_solicitacao_reagendamento = db.Column(db.DateTime, nullable=True)

    # Relacionamentos de apoio
    cliente = db.relationship('ModCadastroCliente', backref='agendamentos')

    # A avaliação deste agendamento específico (uselist=False garante o relacionamento 1X1)
    avaliacao = db.relationship('AghAvaliacaoServico', backref='agendamento', uselist=False, lazy=True)

    def __repr__(self):
        return f"<AghAgendamento ID {self.id} - Status {self.status}>"


class AghAvaliacaoServico(db.Model):
    """Módulo de Reputação e Avaliação Justa (Métrica + Depoimento)."""
    __tablename__ = 'agh_avaliacao_servico'
    __table_args__ = {'extend_existing': True}

    id = db.Column(db.Integer, primary_key=True)
    agendamento_id = db.Column(db.Integer, db.ForeignKey('agh_agendamento.id'), nullable=False, unique=True)
    cliente_id = db.Column(db.Integer, db.ForeignKey('mod_cadastro_cliente.id'), nullable=False)
    servico_id = db.Column(db.Integer, db.ForeignKey('agh_servico.id'), nullable=False)
    profissional_id = db.Column(db.Integer, db.ForeignKey('agh_profissional.id'), nullable=False)

    # Notas quantitativas (1 a 5 estrelas)
    nota_servico = db.Column(db.Integer, nullable=False)
    nota_profissional = db.Column(db.Integer, nullable=False)

    # O Depoimento (O "Ouro" do profissional para sua vitrine estilo LinkedIn)
    comentario = db.Column(db.Text, nullable=True)

    # Sistema de moderação gerencial para proteção contra avaliações falsas
    status_moderacao = db.Column(db.String(20), default='aprovado')  # 'pendente', 'aprovado', 'oculto'

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    cliente = db.relationship('ModCadastroCliente', backref='minhas_avaliacoes_feitas')

    def __repr__(self):
        return f"<AghAvaliacao ID {self.id} - Notas S:{self.nota_servico}/P:{self.nota_profissional}>"


class EseEmpresa(db.Model):
    __tablename__ = 'ese_empresa'
    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(100), nullable=False)
    categoria = db.Column(db.String(50))
    slug = db.Column(db.String(100), unique=True)
    logomarca = db.Column(db.String(255))  # Caminho do arquivo

    # 🎨 Campos vitais para a identidade visual que você mencionou:
    cor_primaria = db.Column(db.String(7), default="#111827")  # Padrão: Grafite Escuro
    cor_secundaria = db.Column(db.String(7), default="#6B7280")  # Padrão: Cinza


class UsuarioFavorito(db.Model):
    """Registra os favoritamentos do usuário para personalizar o FeedIn Negócios."""
    __tablename__ = 'usuario_favorito'
    __table_args__ = (
        db.UniqueConstraint('usuario_id', 'empresa_id', name='unique_usuario_empresa_fav'),
        db.UniqueConstraint('usuario_id', 'segmento_id', name='unique_usuario_segmento_fav'),
        {'extend_existing': True}
    )

    id = db.Column(db.Integer, primary_key=True)

    # Quem está favoritando (ID do Usuário vindo do FeedIn Core)
    usuario_id = db.Column(db.Integer, db.ForeignKey('usuario.id'), nullable=False)

    # O que está sendo favoritado (Apontando para os nomes corretos das suas tabelas)
    empresa_id = db.Column(db.Integer, db.ForeignKey('ese_empresa.id'), nullable=True)
    segmento_id = db.Column(db.Integer, db.ForeignKey('taxonomia.id'), nullable=True)

    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    def __repr__(self):
        return f"<UsuarioFavorito User:{self.usuario_id} Empresa:{self.empresa_id} Segmento:{self.segmento_id}>"


class ModHomologacaoEmpresa(db.Model):
    """
    Tabela de segurança jurídica e compliance do FeedIn.
    Faz parte do ecossistema do módulo de Agenda.
    """
    __tablename__ = 'mod_homologacao_empresa'

    id = db.Column(db.Integer, primary_key=True)
    id_local = db.Column(db.Integer, db.ForeignKey('locais.id'), nullable=False)

    # Caminhos dos arquivos físicos salvos de forma protegida na VPS
    path_comprovante_endereco = db.Column(db.String(255), nullable=False)
    path_cartao_cnpj_ou_social = db.Column(db.String(255), nullable=False)

    # Metadados para auditoria jurídica
    data_envio = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    data_analise = db.Column(db.DateTime, nullable=True)

    # Quem analisou (Chave estrangeira apontando para a tabela 'usuario' do Core)
    id_auditor_feedin = db.Column(db.Integer, db.ForeignKey('usuario.id'), nullable=True)

    # Status do trâmite legal: 'pendente', 'aprovado', 'rejeitado'
    status_auditoria = db.Column(db.String(20), default='pendente', nullable=False)
    motivo_rejeicao = db.Column(db.Text, nullable=True)

    # Relacionamento com o Local (A tabela 'locais' está no Core)
    local = db.relationship('Local', backref='historico_homologacao')

class ModCadastroCliente(db.Model):
    """Tabela paralela e unificada de clientes (Comum a todos os módulos)."""
    __tablename__ = 'mod_cadastro_cliente'
    __table_args__ = {'extend_existing': True}

    id = db.Column(db.Integer, primary_key=True)

    # Elo 1X1 Opcional com o FeedIn Core (Se for NULL, o cliente é só do balcão)
    usuario_id = db.Column(db.Integer, db.ForeignKey('usuario.id'), nullable=True, unique=True)

    # Dados obrigatórios coletados no balcão/módulo
    nome = db.Column(db.String(100), nullable=False)
    # Novo campo Hash (substitui o campo cpf limpo)
    cpf_hash = db.Column(db.String(64), unique=True, index=True, nullable=False)
    whatsapp = db.Column(db.String(20), nullable=False)
    email = db.Column(db.String(255), nullable=True)
    data_nascimento = db.Column(db.Date, nullable=False)
    cpf_encrypted = db.Column(db.LargeBinary, nullable=False)

    # Credenciais do módulo (Garantindo que ele acesse o PWA de forma simples)
    username_modulo = db.Column(db.String(50), unique=True, nullable=False)
    senha_modulo_hash = db.Column(db.String(255), nullable=False)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    @property
    def cpf(self):
        # Descriptografa sob demanda
        return cipher.decrypt(self.cpf_encrypted).decode()

    @cpf.setter
    def cpf(self, plain_cpf):
        # A MÁGICA: Gera o hash usando o padrão do Core e criptografa
        self.cpf_hash = IdentidadeCivil.gerar_hash(plain_cpf)
        self.cpf_encrypted = cipher.encrypt(plain_cpf.encode())

    def __repr__(self):
        return f"<ModCadastroCliente {self.nome} ({self.username_modulo})>"

class ModFilaAtivacaoCliente(db.Model):
    """Tabela de Linbo/Trânsito para controle de disparos e auditoria de cliques."""
    __tablename__ = 'mod_fila_ativacao_cliente'

    id = db.Column(db.Integer, primary_key=True)

    # Se o CPF já existir no Core, amarramos o ID logo na origem para facilitar o retorno
    usuario_id = db.Column(db.Integer, db.ForeignKey('usuario.id'), nullable=True)

    # Dados mínimos do Balcão Rápido para o Agendamento e Disparo
    nome = db.Column(db.String(100), nullable=False)
    whatsapp = db.Column(db.String(20), nullable=False)
    email = db.Column(db.String(255), nullable=True)  # Aceita nulo real (Sem e-mails fictícios!)

    # Métricas de Auditoria e Controle (O Coração da sua Regra)
    data_disparo = db.Column(db.DateTime, default=datetime.utcnow)
    data_expiracao = db.Column(db.DateTime, nullable=False)
    data_tentativa_abertura = db.Column(db.DateTime, nullable=True)  # Mantido NULL se o usuário ignorar o link

    def __repr__(self):
        return f"<ModFilaAtivacao {self.nome} - Status: {'Acessou' if self.data_tentativa_abertura else 'Ignorou'}>"