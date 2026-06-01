from feedin import login_manager
from datetime import datetime, timezone, timedelta
from flask_login import UserMixin
import hashlib, uuid
from feedin import database, app
from sqlalchemy import func

@login_manager.user_loader
def load_user(user_id):
    return Usuario.query.get(user_id)

# --- TABELA DE CONEXÕES (O Grafo de Confiança) ---
class Conexoes(database.Model):
    __tablename__ = 'conexoes'
    __table_args__ = {'extend_existing': True}
    id = database.Column(database.Integer, primary_key=True)

    id_remetente = database.Column(database.Integer, database.ForeignKey('usuario.id'), nullable=False)
    id_destinatario = database.Column(database.Integer, database.ForeignKey('usuario.id'), nullable=True)



    # O "Fiador" ou Amigo em Comum (O diferencial do FeedIn)
    id_referencia_comum = database.Column(database.Integer, database.ForeignKey('usuario.id'), nullable=True)

    # Contexto do Vínculo
    categoria = database.Column(database.String(20))  # 'familia', 'social', 'profissional'
    id_parentesco = database.Column(database.Integer, database.ForeignKey('grauparentesco.id'), nullable=True)
    id_grupo_social = database.Column(database.Integer, database.ForeignKey('grupos_sociais.id'), nullable=True)
    id_local_contexto = database.Column(database.Integer, database.ForeignKey('locais.id'), nullable=True)

    # Status e Datas
    status = database.Column(database.String(20), default='pendente')
    data_solicitacao = database.Column(database.DateTime, default=lambda: datetime.now(timezone.utc))
    data_aceite = database.Column(database.DateTime, nullable=True)
    data_recusa = database.Column(database.DateTime, nullable=True)
    justificativa_recusa = database.Column(database.Text, nullable=True)

    # Relacionamentos (Lógico)
    remetente = database.relationship('Usuario', foreign_keys=[id_remetente], backref='solicitacoes_feitas')
    destinatario = database.relationship('Usuario', foreign_keys=[id_destinatario], backref='solicitacoes_recebidas')
    referencia = database.relationship('Usuario', foreign_keys=[id_referencia_comum])


taxonomia_conexoes = database.Table('taxonomia_conexoes',
    database.Column('pai_id', database.Integer, database.ForeignKey('taxonomia.id'), primary_key=True),
    database.Column('filho_id', database.Integer, database.ForeignKey('taxonomia.id'), primary_key=True),
    extend_existing=True)
# Tabela de ligação para a Hierarquia Multifacetada


class Desconexoes(database.Model):
    __tablename__ = 'desconexoes'
    __table_args__ = {'extend_existing': True}
    id = database.Column(database.Integer, primary_key=True)

    # Referência à conexão original que foi quebrada (para manter o histórico do contexto)
    id_conexao_original = database.Column(database.Integer, nullable=False)

    # Quem tomou a iniciativa de se afastar (fundamental para sabermos quem "esqueceu")
    id_solicitante = database.Column(database.Integer, database.ForeignKey('usuario.id'), nullable=False)
    id_ex_parceiro = database.Column(database.Integer, database.ForeignKey('usuario.id'), nullable=False)

    # O contexto original da conexão que está sendo desfeita (dados preservados da tabela Conexoes)
    categoria_original = database.Column(database.String(20))
    id_local_contexto = database.Column(database.Integer, nullable=True)
    data_original_aceite = database.Column(database.DateTime, nullable=True)

    # O registro do arrependimento ou quebra
    data_desconexao = database.Column(database.DateTime, default=lambda: datetime.now(timezone.utc))
    motivo_desconexao = database.Column(database.Text, nullable=True)  # Anotação de uso pessoal do solicitante

    # Relacionamentos lógicos
    solicitante = database.relationship('Usuario', foreign_keys=[id_solicitante], backref='desconexoes_iniciadas')


class Bloqueios(database.Model):
    __tablename__ = 'bloqueios'
    id = database.Column(database.Integer, primary_key=True)

    # O Muro entre as duas pessoas
    id_autor = database.Column(database.Integer, database.ForeignKey('usuario.id'), nullable=False)
    id_alvo = database.Column(database.Integer, database.ForeignKey('usuario.id'), nullable=False)
    data_bloqueio = database.Column(database.DateTime, default=lambda: datetime.now(timezone.utc))

    # O contexto de inteligência da plataforma
    id_local_contexto = database.Column(database.Integer, database.ForeignKey('locais.id'), nullable=True)
    categoria_motivo = database.Column(database.String(30), nullable=False,
                                       default='outros')  # 'importunacao', 'perfil_falso', 'comportamento_abusivo', 'outros'
    relato_usuario = database.Column(database.Text, nullable=True)

    # Relacionamento para o painel de administração (backref sutil)
    alvo = database.relationship('Usuario', foreign_keys=[id_alvo], backref='bloqueios_recebidos')


class Taxonomia(database.Model):
    __tablename__ = 'taxonomia'
    __table_args__ = {'extend_existing': True}
    id = database.Column(database.Integer, primary_key=True)
    nome = database.Column(database.String(100), nullable=False, unique=True)
    status = database.Column(database.String(20), default='pendente')
    data_criacao = database.Column(database.DateTime, default=lambda: datetime.now(timezone.utc))
    data_homologacao = database.Column(database.DateTime, nullable=True)
    contagem_uso = database.Column(database.Integer, default=1)
    visivel_usuario = database.Column(database.Boolean, default=True)
    visivel_negocio = database.Column(database.Boolean, default=True)
    categoria = database.Column(database.String(50))  # 'Gosto', 'Serviço', 'Ambiente'

    # RELACIONAMENTO CORRIGIDO COM IDENTIFICAÇÃO EXPLÍCITA
    contextos = database.relationship(
        'Taxonomia',
        secondary=taxonomia_conexoes,
        primaryjoin="Taxonomia.id == taxonomia_conexoes.c.filho_id",
        secondaryjoin="Taxonomia.id == taxonomia_conexoes.c.pai_id",
        backref=database.backref('subitens', lazy='dynamic')
    )

    def stats_empreendedor(self):
        """
        Retorna o 'Peso' da tag em Piracicaba.
        Ideal para o dashboard do parceiro.
        """
        return {
            'seguidores_reais': self.usuarios_interessados.count(),
            'volume_memorias': self.postagens_relacionadas.count(),
            'concorrencia_locais': database.session.query(local_tags).filter_by(taxonomia_id=self.id).count()
        }

    def buscar_total_seguidores(self):
        """
        Retorna a contagem real de usuários que seguem esta tag.
        O .count() é processado diretamente no SQL, sem carregar a lista de usuários.
        """
        return self.usuarios_interessados.count()

    def __repr__(self):
        return f"<Tag {self.nome}>"

usuarios_interesses = database.Table('usuarios_interesses',
    database.Column('usuario_id', database.Integer, database.ForeignKey('usuario.id'), primary_key=True),
    database.Column('taxonomia_id', database.Integer, database.ForeignKey('taxonomia.id'), primary_key=True),
    extend_existing=True
)


class Usuario(database.Model, UserMixin):
    __tablename__ = 'usuario'
    __table_args__ = {'extend_existing': True}
    id = database.Column(database.Integer, primary_key=True)
    username = database.Column(database.String, nullable=False)
    email = database.Column(database.String(255), nullable=False, unique=True)
    senha = database.Column(database.String(255))
    foto_perfil = database.Column(database.String, default='default.jpg')
    nivel_acesso = database.Column(database.Integer, nullable=False, default=1) # Unificado!
    active = database.Column(database.Boolean())
    created_at = database.Column(database.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))
    fs_uniquifier = database.Column(database.String(255), unique=True, nullable=False, default=lambda: str(uuid.uuid4()))

    is_pioneiro = database.Column(database.Boolean, default=False)
    grupos_sociais = database.Column(database.String(255))
    data_cadastro = database.Column(database.DateTime, default=lambda: datetime.now(timezone.utc))
    id_indicador = database.Column(database.Integer)
    aceite_lgpd = database.Column(database.Boolean, default=False, nullable=False)

    # Relacionamentos
    perfil = database.relationship('Perfil', backref='usuario_dono', uselist=False, lazy=True)

    identidade = database.relationship('IdentidadeCivil', back_populates='usuario', uselist=False)

    convites_enviados_whats = database.relationship(
        'Convite',
        backref='remetente',
        lazy=True,
        foreign_keys='Convite.id_remetente'  # Aponta explicitamente para quem envia
    )

    @property
    def convites_aceitos(self):
        # Conta quantos convites enviados via whats resultaram em cadastro
        return sum(1 for c in self.convites_enviados_whats if c.status_onboarding)

    # Atualize no Model Usuario
    interesses = database.relationship('Taxonomia',
                                       secondary=usuarios_interesses,
                                       backref=database.backref('usuarios_interessados', lazy='dynamic'),
                                       lazy='dynamic')  # Adicione o lazy='dynamic' aqui também!

    vinculos = database.relationship('VinculoUsuarioLocal', backref='usuario', lazy=True)

    # O motor de busca de amigos (Self-referential)
    amigos = database.relationship(
        "Usuario",
        secondary="conexoes",
        primaryjoin="and_(Usuario.id == Conexoes.id_remetente, Conexoes.status == 'aceito')",
        secondaryjoin="and_(Usuario.id == Conexoes.id_destinatario, Conexoes.status == 'aceito')",
        overlaps="solicitacoes_feitas,solicitacoes_recebidas,remetente,destinatario"
    )

    seguidores = database.relationship(
        "Usuario",
        secondary="conexoes",
        primaryjoin="Usuario.id == Conexoes.id_destinatario",
        secondaryjoin="Usuario.id == Conexoes.id_remetente",
        overlaps="amigos,solicitacoes_feitas,solicitacoes_recebidas,remetente,destinatario"
    )


    # MÉTODO PARA RESOLVER O ERRO DA LINHA 512 (A Identação correta é aqui!)
    # Dentro da classe Usuario no models.py
    def get_amigo_em_comum(self, outro_usuario):
        # Pegamos os IDs dos amigos do usuário atual
        # Ajuste 'amigos' para o nome correto do seu relationship
        meus_amigos_ids = {amigo.id for amigo in self.amigos}

        # Verificamos se algum amigo do outro usuário está na minha lista
        for amigo_do_outro in outro_usuario.amigos:
            if amigo_do_outro.id in meus_amigos_ids:
                return amigo_do_outro  # Retorna o primeiro amigo em comum encontrado

        return None

    def check_pioneiro_status(self):
        # Se já é pioneiro ou não atingiu o nível de maturidade, para aqui
        if self.is_pioneiro or self.nivel_acesso < 10:
            return False

        # Atingiu Nível 10? Agora verificamos a origem ou o esforço

        # 1. Veio por convite de alguém legítimo? (Neste caso, IDs 1 e 2 são automáticos)
        if self.id_indicador in [1, 2]:
            self.is_pioneiro = True
            return True

        # 2. Esforço Próprio: Se ele chegou ao Nível 10 e tem as 10 conexões
        if len(self.amigos) >= 10:
            self.is_pioneiro = True
            return True

        return False


# Cria tabela de complemento do Apelidos
class Apelidos(database.Model):
    __tablename__ = 'apelidos'
    __table_args__ = {'extend_existing': True}

    id = database.Column(database.Integer, primary_key=True)
    apelido = database.Column(database.String(50), nullable=False)
    data_criacao = database.Column(database.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))

    # A chave estrangeira deve apontar para perfil.id (que é a PK da tabela perfil)
    # ou para usuario.id, dependendo da sua estratégia de normalização.
    # Se na classe Perfil você usou backref='perfil_dono', aqui deve ser:
    id_perfil = database.Column(database.Integer, database.ForeignKey('perfil.id'), nullable=False)

# Cria tabela de Gêneros
class Generos(database.Model):
    __table_args__ = {'extend_existing': True}
    id = database.Column(database.Integer, primary_key=True)
    genero = database.Column(database.String(50), nullable=False)

# Cria tabela de Estado Civil
class EstadoCivil(database.Model):
    __tablename__ = 'estadocivil' #esse comando evita alteração do nome da tabela para 'estado-civil' no banco de dados
    __table_args__ = {'extend_existing': True}
    id = database.Column(database.Integer, primary_key=True)
    estado_civil = database.Column(database.String(50), nullable=False)

# Cria tabela de Grau de Parentesco
class GrauParentesco(database.Model):
    __tablename__ = 'grauparentesco' #esse comando evita alteração do nome da tabela para 'grau-parenteco' no banco de dados
    __table_args__ = {'extend_existing': True}
    id = database.Column(database.Integer, primary_key=True)
    parentesco = database.Column(database.String(50), nullable=False)
    grau_parentesco = database.Column(database.Integer, nullable=True)

# Cria tabela de Parentescos
class Parentesco(database.Model):
    __tablename__ = 'parentescos'
    __table_args__ = {'extend_existing': True}
    id = database.Column(database.Integer, primary_key=True)

    # Quem enviou o convite
    id_usuario_remetente = database.Column(database.Integer, database.ForeignKey('usuario.id'), nullable=False)
    # Quem vai receber/aceitar
    id_usuario_destinatario = database.Column(database.Integer, database.ForeignKey('usuario.id'), nullable=False)

    # O grau que o remetente declarou (ex: Tio, Primo)
    id_grau = database.Column(database.Integer, database.ForeignKey('grauparentesco.id'), nullable=False)

    # Logs Temporais (O que os usuários adoram acompanhar)
    data_solicitacao = database.Column(database.DateTime, default=lambda: datetime.now(timezone.utc))
    data_aceite = database.Column(database.DateTime, nullable=True)  # Fica nulo até a pessoa aceitar
    data_recusa = database.Column(database.DateTime, nullable=True)

    # Status: 'pendente', 'aceito', 'recusado'
    status = database.Column(database.String(20), default='pendente')

    # Justificativa em caso de recusa (conforme sua ideia de ontem)
    observacao_recusa = database.Column(database.Text, nullable=True)

    # Relacionamentos para facilitar o acesso via Python (backrefs)
    remetente = database.relationship('Usuario', foreign_keys=[id_usuario_remetente], backref='convites_enviados')
    destinatario = database.relationship('Usuario', foreign_keys=[id_usuario_destinatario],
                                        backref='convites_recebidos')

# Cria tabela de Nível de Acesso, que ajudarão a limitar acessos àos módulos
class UserPaper(database.Model):
    __tablename__ = 'userpaper' #esse comando evita alteração do nome da tabela para 'user-paper' no banco de dados
    __table_args__ = {'extend_existing': True}
    id = database.Column(database.Integer, primary_key=True)
    papel = database.Column(database.String(20), nullable=False)
    direitos  = database.Column(database.String(255), nullable=False)
    nivel = database.Column(database.Integer, nullable=False)

# Cria tabela de complemento do Perfil
class Perfil(database.Model):
    __tablename__ = 'perfil'
    __table_args__ = {'extend_existing': True}

    id = database.Column(database.Integer, primary_key=True)
    nome_completo = database.Column(database.String(100), nullable=False)
    data_nascimento = database.Column(database.Date, nullable=False)
    cidade_natal = database.Column(database.String(80), nullable=False)
    data_criacao = database.Column(database.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))
    url_capa = database.Column(database.String(255), default='default_capa.jpg')

    # CHAVES ESTRANGEIRAS (Físicas no Banco)
    id_usuario = database.Column(database.Integer, database.ForeignKey('usuario.id'), nullable=False)
    genero = database.Column(database.Integer, database.ForeignKey('generos.id'), nullable=True)
    estado_civil = database.Column(database.Integer, database.ForeignKey('estadocivil.id'), nullable=True)

    # RELACIONAMENTO (Lógico no Python)
    # Permite acessar perfil.apelidos e retorna uma lista de objetos Apelidos
    apelidos = database.relationship('Apelidos', backref='perfil', lazy=True, cascade="all, delete-orphan")
    biografia = database.Column(database.String(500), nullable=True)  # 500 caracteres é um bom limite

# --- TABELA DE GRUPOS SOCIAIS (A Memória que une o Local ao Tempo) ---
class GrupoSocial(database.Model):
    __tablename__ = 'grupos_sociais'
    __table_args__ = {'extend_existing': True}
    id = database.Column(database.Integer, primary_key=True)
    id_local = database.Column(database.Integer, database.ForeignKey('locais.id'))
    periodo_referencia = database.Column(database.String(50), nullable=False)

    # Isso permite fazer: grupo.local.nome
    local = database.relationship('Local', backref='grupos')

# --- HISTÓRICO PROFISSIONAL (A Linha do Tempo) ---
class HistoricoProfissional(database.Model):
    __tablename__ = 'historico_profissional'
    __table_args__ = {'extend_existing': True}
    id = database.Column(database.Integer, primary_key=True)

    id_usuario = database.Column(database.Integer, database.ForeignKey('usuario.id'), nullable=False)
    # AJUSTE AQUI: Mudamos de 'empresas.id' para 'locais.id'
    id_local = database.Column(database.Integer, database.ForeignKey('locais.id'), nullable=False)

    # Detalhes da Época/Local
    departamento = database.Column(database.String(100))  # Ex: TI, Financeiro, Operacional
    cargo = database.Column(database.String(100))

    data_admissao = database.Column(database.Date)
    data_demissao = database.Column(database.Date, nullable=True)  # Null significa que ainda trabalha lá

    # Avaliação (NPS e Estrelas do Colaborador)
    rating_colaborador = database.Column(database.Integer)  # 1 a 5 estrelas
    nps_colaborador = database.Column(database.Integer)  # 0 a 10

    # Relacionamentos
    usuario = database.relationship('Usuario', backref='historico_carreira')
    local = database.relationship('Local', backref='historico_trabalhadores')

class MembroGrupo(database.Model):
    __tablename__ = 'membros_grupos'
    __table_args__ = {'extend_existing': True}
    id = database.Column(database.Integer, primary_key=True)
    id_usuario = database.Column(database.Integer, database.ForeignKey('usuario.id'), nullable=False)
    id_grupo = database.Column(database.Integer, database.ForeignKey('grupos_sociais.id'), nullable=False)
    data_entrada = database.Column(database.DateTime, default=datetime.utcnow)
    id_local = database.Column(database.Integer, database.ForeignKey('locais.id'))

    # ADICIONE ESTA LINHA ABAIXO:
    # Isso permite que o Jinja entenda o que é "vinculo.grupo"
    grupo = database.relationship('GrupoSocial', backref='membros')
    usuario = database.relationship('Usuario', backref='membros_grupos')
    local = database.relationship('Local', backref='membros_vinculados')


# 1. Tabela de ligação: Locais <-> Categorias Adicionais (Tags)
local_tags = database.Table('local_tags',
    database.Column('local_id', database.Integer, database.ForeignKey('locais.id'), primary_key=True),
    database.Column('taxonomia_id', database.Integer, database.ForeignKey('taxonomia.id'), primary_key=True),
    extend_existing=True
)

# 2. Tabela de ligação: Locais <-> Preferências do Usuário (Interesses)
local_preferencias = database.Table('local_preferencias',
    database.Column('local_id', database.Integer, database.ForeignKey('locais.id'), primary_key=True),
    database.Column('taxonomia_id', database.Integer, database.ForeignKey('taxonomia.id'), primary_key=True),
    extend_existing=True
)


class Local(database.Model):
    __tablename__ = 'locais'

    id = database.Column(database.Integer, primary_key=True)
    nome = database.Column(database.String(100), nullable=False)
    documento = database.Column(database.String(18), unique=True, nullable=True)

    # 1. CATEGORIA PRINCIPAL: Agora aponta para Taxonomia
    id_categoria_principal = database.Column(database.Integer, database.ForeignKey('taxonomia.id'))

    # GEOLOCALIZAÇÃO
    cep = database.Column(database.String(10))
    logradouro = database.Column(database.String(150))
    numero = database.Column(database.String(20))
    bairro = database.Column(database.String(100))
    cidade = database.Column(database.String(100), nullable=True)
    estado = database.Column(database.String(2), nullable=True)

    # CONTATO
    telefone = database.Column(database.String(20))
    is_whatsapp = database.Column(database.Boolean, default=True)
    email = database.Column(database.String(120))

    # MARKETING E STATUS
    status_operacional = database.Column(database.String(20), default='ativo')
    plano_marketing = database.Column(database.String(20), default='gratuito')
    data_expiracao_teste = database.Column(database.Date, nullable=True)
    verificado = database.Column(database.Boolean, default=False)
    google_place_id = database.Column(database.String(255), unique=True, nullable=True)
    url_flyer = database.Column(database.String(255), nullable=True)

    # RASTREABILIDADE
    id_indicador = database.Column(database.Integer, database.ForeignKey('usuario.id'), nullable=True)
    id_empreendedor = database.Column(database.Integer, database.ForeignKey('usuario.id'), nullable=True)
    data_cadastro = database.Column(database.DateTime, default=lambda: datetime.now(timezone.utc))

    # GATILHOS DE MEMÓRIA E HISTÓRIA
    ano_encerramento = database.Column(database.Integer, nullable=True)
    data_encerramento_exata = database.Column(database.Date, nullable=True)
    esta_ativo = database.Column(database.Boolean, default=True, index=True)

    # RELACIONAMENTOS (DNA DO FEEDIN)
    indicador = database.relationship('Usuario', foreign_keys=[id_indicador], backref='locais_indicados')
    empreendedor = database.relationship('Usuario', foreign_keys=[id_empreendedor], backref='meus_estabelecimentos')

    # AJUSTE TÉCNICO: back_populates aponta para a variável 'local' na classe AtividadeLocal
    atividades = database.relationship('AtividadeLocal', back_populates='local', lazy=True)

    # TAGS/ESPECIALIDADES: Taxonomia Unificada
    tags_especialidade = database.relationship('Taxonomia',
                                               secondary='local_tags',
                                               backref='locais_com_esta_tag')

    # O MATCH: Lógica de preferências
    preferencias_atreladas = database.relationship(
        'Taxonomia',
        secondary='local_preferencias',
        primaryjoin="Local.id == local_preferencias.c.local_id",
        secondaryjoin="Taxonomia.id == local_preferencias.c.taxonomia_id",
        backref=database.backref('locais_relacionados', lazy='dynamic'))

    # VÍNCULO DO MURAL DE HISTÓRIAS (Bloqueio definitivo contra resets de ID)
    mural_historias = database.relationship('Postagem', back_populates='local_foco', lazy='dynamic')

    def __repr__(self):
        return f'<Local {self.nome}>'

    @staticmethod
    def get_locais_populares_por_usuario(usuario_id):
        from sqlalchemy import func
        from feedin.models import VinculoUsuarioLocal  # Import local para evitar circularidade

        locais_ids_usuario = database.session.query(VinculoUsuarioLocal.local_id) \
            .filter(VinculoUsuarioLocal.usuario_id == usuario_id).all()

        ids = [id_tuple[0] for id_tuple in locais_ids_usuario]
        if not ids:
            return []

        return database.session.query(
            Local,
            func.count(VinculoUsuarioLocal.id).label('total')
        ).join(VinculoUsuarioLocal, Local.id == VinculoUsuarioLocal.local_id) \
            .filter(Local.id.in_(ids)) \
            .group_by(Local.id) \
            .order_by(func.count(VinculoUsuarioLocal.id).desc()) \
            .all()

    def get_rating_data(self):
        from feedin.models import AvaliacaoLocal
        avaliacoes = AvaliacaoLocal.query.filter_by(id_local=self.id).all()

        if not avaliacoes:
            return {'media': 0, 'total': 0}

        soma = sum(a.nota for a in avaliacoes)
        media = round(soma / len(avaliacoes), 1)
        return {'media': media, 'total': len(avaliacoes)}

class LocalMidia(database.Model):
    __tablename__ = 'local_midias'
    id = database.Column(database.Integer, primary_key=True)
    id_local = database.Column(database.Integer, database.ForeignKey('locais.id'), nullable=False)
    url_arquivo = database.Column(database.String(255), nullable=False)
    legenda = database.Column(database.String(200))
    tipo = database.Column(database.String(20)) # 'foto', 'video', 'documento_historico'
    id_usuario_autor = database.Column(database.Integer, database.ForeignKey('usuario.id'))
    e_oficial = database.Column(database.Boolean, default=False) # True se for do dono
    data_upload = database.Column(database.DateTime, default=lambda: datetime.now(timezone.utc))


# --- TABELA DE ASSOCIAÇÃO (Deve vir antes das classes que a utilizam) ---
usuario_atividades = database.Table('usuario_atividades',
                                    database.Column('usuario_id', database.Integer, database.ForeignKey('usuario.id'),
                                                    primary_key=True),
                                    database.Column('atividade_id', database.Integer,
                                                    database.ForeignKey('atividades_local.id'), primary_key=True),
                                    extend_existing=True
                                    )


# --- CLASSE MEMÓRIA (O Coração do FeedIn) ---
class Memoria(database.Model):
    __tablename__ = 'memorias'
    __table_args__ = {'extend_existing': True}

    id = database.Column(database.Integer, primary_key=True)
    id_usuario = database.Column(database.Integer, database.ForeignKey('usuario.id'), nullable=False)
    id_local = database.Column(database.Integer, database.ForeignKey('locais.id'), nullable=True)
    id_grupo_social = database.Column(database.Integer, database.ForeignKey('grupos_sociais.id'), nullable=True)
    id_atividade = database.Column(database.Integer, database.ForeignKey('atividades_local.id'), nullable=True)

    titulo = database.Column(database.String(100), nullable=True)
    descricao = database.Column(database.Text, nullable=True)
    foto_memoria = database.Column(database.String, nullable=True, default='post_default.jpg')
    data_criacao = database.Column(database.DateTime, default=lambda: datetime.now(timezone.utc))
    data_evento = database.Column(database.Date, nullable=True)
    privacidade = database.Column(database.String(20), default='conexoes')

    # RELACIONAMENTO COM CONEXÕES (Apenas aqui dentro!)
    # Isso permite que uma memória seja gerada automaticamente quando dois usuários se conectam
    id_conexao = database.Column(database.Integer, database.ForeignKey('conexoes.id'), nullable=True)
    dados_conexao = database.relationship('Conexoes', backref='memoria_gerada')

    autor = database.relationship('Usuario', backref='minhas_memorias')
    local = database.relationship('Local', backref='memorias_no_local')
    grupo = database.relationship('GrupoSocial', backref='memorias_no_grupo')

    @property
    def total_curtidas_memoria(self):
        # Base para o contador que os ícones vão ler
        return 0

    def usuario_ja_curtiu_memoria(self, user_id):
        # Base para o coração ficar preenchido ou vazio
        return False

    def __repr__(self):
        return f"Memoria('{self.titulo}', '{self.data_criacao}')"


# --- CLASSE ATIVIDADE LOCAL ---
class AtividadeLocal(database.Model):
    __tablename__ = 'atividades_local'

    id = database.Column(database.Integer, primary_key=True)
    nome = database.Column(database.String(100), nullable=False)
    id_local = database.Column(database.Integer, database.ForeignKey('locais.id'), nullable=False)
    id_criador = database.Column(database.Integer, database.ForeignKey('usuario.id'))
    data_criacao = database.Column(database.DateTime, default=lambda: datetime.now(timezone.utc))
    periodo_estimado = database.Column(database.String(50))
    descricao = database.Column(database.Text)

    # --- RELACIONAMENTOS ---
    # backref 'minhas_atividades' permite ver o que o usuário frequentou
    participantes = database.relationship('Usuario', secondary=usuario_atividades, backref='minhas_atividades')

    # backref 'atividades_criadas' para o histórico do autor
    criador = database.relationship('Usuario', foreign_keys=[id_criador], backref='atividades_criadas')

    # AJUSTE TÉCNICO: back_populates aponta para a variável 'atividades' na classe Local
    local = database.relationship('Local', back_populates='atividades')

    # --- ATRIBUTOS DE COMPATIBILIDADE (LAYOUT UNIVERSAL) ---
    @property
    def tipo(self):
        return 'vinculo'

    @property
    def conteudo_exibicao(self):
        return self.descricao

    @property
    def autor_objeto(self):
        return self.criador

    # Coloque isso DENTRO da classe AtividadeLocal:
    @property
    def pessoas_marcadas_confirmadas(self):
        """Nas atividades de local, os participantes fixados na tabela intermediária já são confirmados"""
        return self.participantes

    @property
    def solicitantes_pendentes_ids(self):
        """Atividades locais de histórico geralmente não têm fluxo de pendência individual por post"""
        return []

    # --- MÉTODOS DE LÓGICA ---
    def usuario_ja_reagiu_tipo(self, user_id, tipo_procurado):
        # Nota: Certifique-se que PostagemInteracao está acessível ou importado
        from feedin.models import PostagemInteracao
        return PostagemInteracao.query.filter_by(
            id_postagem=self.id,
            id_usuario=user_id,
            tipo=tipo_procurado
        ).first() is not None

    def __repr__(self):
        return f'<AtividadeLocal {self.nome} em Local {self.id_local}>'


class VinculoUsuarioLocal(database.Model):
    __tablename__ = 'vinculousuariolocal'
    id = database.Column(database.Integer, primary_key=True)
    usuario_id = database.Column(database.Integer, database.ForeignKey('usuario.id'), nullable=False)
    local_id = database.Column(database.Integer, database.ForeignKey('locais.id'), nullable=False)
    experiencia = database.Column(database.Text)

    local = database.relationship('Local', backref='vinculos_recebidos')


# Convites via WhatsApp
class Convite(database.Model):
    __tablename__ = 'convites'
    __table_args__ = {'extend_existing': True}
    id = database.Column(database.Integer, primary_key=True)

    # Chaves Estrangeiras
    id_remetente = database.Column(database.Integer, database.ForeignKey('usuario.id'), nullable=True)
    id_destinatario = database.Column(database.Integer, database.ForeignKey('usuario.id'), nullable=True)

    # Relacionamentos Explícitos
    # Isso ajuda o Flask-SQLAlchemy a não se perder ao acessar convite.usuario_remetente
    usuario_remetente = database.relationship(
        'Usuario',
        foreign_keys=[id_remetente],
        overlaps="convites_enviados_whats,remetente"  # <--- A CHAVE DA SOLUÇÃO AQUI
    )
    usuario_confirmado = database.relationship('Usuario', foreign_keys=[id_destinatario])
    tipo_vinculo = database.Column(database.String(20))  # 'gosto', 'local' ou 'grupo'
    id_referencia = database.Column(database.Integer)  # ID da Taxonomia ou do Local

    mensagem_personalizada = database.Column(database.Text)  # O "motivo" que o Pai escreveu
    # O "RG" do convite para consistência
    whatsapp_destino = database.Column(database.String(20), nullable=False)

    data_disparo = database.Column(database.DateTime, default=lambda: datetime.now(timezone.utc))
    status_onboarding = database.Column(database.Boolean, default=False)


class ConviteAdmin(database.Model):
    __tablename__ = 'convite_admin'
    __table_args__ = {'extend_existing': True}
    id = database.Column(database.Integer, primary_key=True)
    token = database.Column(database.String(64), unique=True, nullable=False, default=lambda: str(uuid.uuid4()))
    id_admin = database.Column(database.Integer, nullable=False) # ID 1 ou 2
    usado = database.Column(database.Boolean, default=False)
    data_criacao = database.Column(database.DateTime, default=lambda: datetime.now(timezone.utc))

# Modelo da Tabela de Identidade Civil



    # Nota: A chave deve ser guardada em uma variável de ambiente por segurança
    # CHAVE_MESTRA = os.environ.get('CHAVE_SEGURANCA_FEEDIN')

class IdentidadeCivil(database.Model):
    __tablename__ = 'identidade_civil'
    __table_args__ = {'extend_existing': True}
    id = database.Column(database.Integer, primary_key=True)
    usuario_id = database.Column(database.Integer, database.ForeignKey('usuario.id'), unique=True)

    nome_completo_oficial = database.Column(database.String(255), nullable=False)
    cpf_criptografado = database.Column(database.LargeBinary, nullable=False)  # BLOB no SQLite
    cpf_hash = database.Column(database.String(64), unique=True, nullable=False, index=True)

    data_nascimento = database.Column(database.Date, nullable=False)

    # Metadados essenciais para o Marco Civil/Justiça
    ip_origem = database.Column(database.String(45))
    versao_termos_aceita = database.Column(database.String(20))
    data_verificacao = database.Column(database.DateTime, default=datetime.utcnow)

    usuario = database.relationship('Usuario', back_populates='identidade')

    @staticmethod
    def gerar_hash(cpf):
        return hashlib.sha256(cpf.encode()).hexdigest()


# Tabela de ligação: Postagens <-> Taxonomia (Interesses/Tags)
postagem_tags = database.Table('postagem_tags',
                               database.Column('postagem_id', database.Integer, database.ForeignKey('postagens.id'),
                                               primary_key=True),
                               database.Column('taxonomia_id', database.Integer, database.ForeignKey('taxonomia.id'),
                                               primary_key=True),
                               extend_existing=True
                               )



class Postagem(database.Model):
    __tablename__ = 'postagens'

    id = database.Column(database.Integer, primary_key=True)
    id_usuario = database.Column(database.Integer, database.ForeignKey('usuario.id'), nullable=False)
    id_local = database.Column(database.Integer, database.ForeignKey('locais.id'), nullable=True)

    conteudo = database.Column(database.Text, nullable=False)
    imagem_url = database.Column(database.String(255), nullable=True)
    data_criacao = database.Column(database.DateTime, default=lambda: datetime.now(timezone.utc))
    ativo = database.Column(database.Boolean, default=True)

    # RELACIONAMENTOS EXISTENTES
    autor = database.relationship('Usuario', backref=database.backref('postagens', lazy='dynamic'),
                                  foreign_keys=[id_usuario])

    # Ajustado de backref para back_populates apontando para a nova variável da classe Local
    local_foco = database.relationship(
        'Local',
        back_populates='mural_historias',
        foreign_keys=[id_local],
        passive_deletes=True  # <-- Impede que o SQLAlchemy altere essa coluna de forma automatizada
    )

    tags_afinidade = database.relationship('Taxonomia',
                                           secondary=postagem_tags,
                                           backref=database.backref('postagens_relacionadas', lazy='dynamic'))

    anuncio = None

    # 2. Nova relação (Mantida exatamente como você estruturou)
    pessoas_marcadas = database.relationship(
        'Usuario',
        secondary='marcacoes_postagens',
        primaryjoin="Postagem.id == marcacoes_postagens.c.postagem_id",
        secondaryjoin="and_(Usuario.id == marcacoes_postagens.c.usuario_id, marcacoes_postagens.c.status == 'aceito')",
        viewonly=True
    )

    @property
    def lista_comentarios_ativos(self):
        """Retorna os comentários ativos ordenados para o template"""
        with database.session.no_autoflush:
            return PostagemComentario.query.filter_by(id_postagem=self.id, ativo=True).order_by(
                PostagemComentario.data_comentario.asc()).all()

    # Métodos de conveniência
    @property
    def total_curtidas(self):
        from feedin.models import PostagemInteracao
        with database.session.no_autoflush:
            return PostagemInteracao.query.filter_by(id_postagem=self.id, tipo='curti').count()

    @property
    def total_comentarios(self):
        with database.session.no_autoflush:
            return PostagemComentario.query.filter_by(id_postagem=self.id, ativo=True).count()

    def usuario_ja_curtiu(self, user_id):
        from feedin.models import PostagemInteracao
        with database.session.no_autoflush:
            return PostagemInteracao.query.filter_by(id_postagem=self.id, id_usuario=user_id, tipo='curti').first() is not None

    # Métodos de compatibilidade para o template (Espelhamento)
    @property
    def total_curtidas_memoria(self):
        return self.total_curtidas

    @property
    def total_nao_curtidas(self):
        from feedin.models import PostagemInteracao
        with database.session.no_autoflush:
            return PostagemInteracao.query.filter_by(id_postagem=self.id, tipo='nao_curti').count()

    @property
    def tags(self):
        return self.tags_afinidade

    @property
    def pessoas_marcadas_confirmadas(self):
        """Retorna a lista de usuários cuja marcação já foi aceita pelo autor"""
        from feedin.models import MarcacaoPostagem
        with database.session.no_autoflush:
            vinculos = MarcacaoPostagem.query.filter_by(postagem_id=self.id, status='aceito').all()
            return [v.usuario for v in vinculos if v.usuario]

    @property
    def solicitantes_pendentes_ids(self):
        """Retorna uma lista de IDs de usuários que pediram para ser marcados e estão aguardando"""
        from feedin.models import MarcacaoPostagem
        with database.session.no_autoflush:
            vinculos = MarcacaoPostagem.query.filter_by(postagem_id=self.id, status='pendente').all()
            return [v.usuario_id for v in vinculos]

    def usuario_ja_deu_nao_curti(self, user_id):
        from feedin.models import PostagemInteracao
        with database.session.no_autoflush:
            return PostagemInteracao.query.filter_by(id_postagem=self.id, id_usuario=user_id, tipo='nao_curti').first() is not None

    def usuario_ja_reagiu_tipo(self, user_id, tipo_procurado):
        """Verifica se o usuário reagiu com um tipo específico"""
        from feedin.models import PostagemInteracao
        with database.session.no_autoflush:
            return PostagemInteracao.query.filter_by(
                id_postagem=self.id,
                id_usuario=user_id,
                tipo=tipo_procurado
            ).first() is not None


class PostagemInteracao(database.Model):
    __tablename__ = 'postagem_interacoes'
    # Combine os argumentos em uma única tupla:
    __table_args__ = (
        database.UniqueConstraint('id_postagem', 'id_usuario', name='unique_reacao_user'),
        {'extend_existing': True}
    )
    id = database.Column(database.Integer, primary_key=True)
    id_postagem = database.Column(database.Integer, database.ForeignKey('postagens.id'), nullable=False)
    id_usuario = database.Column(database.Integer, database.ForeignKey('usuario.id'), nullable=False)
    tipo = database.Column(database.String(10))  # 'curtir', 'amei', etc.

    # Garante que um usuário só tenha uma reação por postagem
    __table_args__ = (database.UniqueConstraint('id_postagem', 'id_usuario', name='unique_reacao_user'),)


class PostagemComentario(database.Model):
    __tablename__ = 'postagem_comentarios'
    __table_args__ = {'extend_existing': True}
    id = database.Column(database.Integer, primary_key=True)
    id_postagem = database.Column(database.Integer, database.ForeignKey('postagens.id'), nullable=False)
    id_usuario = database.Column(database.Integer, database.ForeignKey('usuario.id'), nullable=False)
    texto = database.Column(database.String(500), nullable=False)
    data_comentario = database.Column(database.DateTime, default=lambda: datetime.now(timezone.utc))
    ativo = database.Column(database.Boolean, default=True)  # Para "exclusão" lógica

    @property
    def usuario(self):
        """Busca o objeto do usuário dono do comentário direto na sessão ativa do banco"""
        from feedin.models import Usuario  # Mantém a importação isolada contra loops cíclicos
        try:
            # Buscamos direto pela sessão global do banco, garantindo o mapeamento no Jinja
            return database.session.query(Usuario).get(self.id_usuario)
        except Exception:
            return None


# Tabela para tratamento da reinvindicação na versão Beta.
class ReivindicacaoLocal(database.Model):
    __tablename__ = 'reivindicacoes_locais'
    id = database.Column(database.Integer, primary_key=True)
    id_local = database.Column(database.Integer, database.ForeignKey('locais.id'), nullable=False)
    id_usuario = database.Column(database.Integer, database.ForeignKey('usuario.id'), nullable=False)
    data_solicitacao = database.Column(database.DateTime, default=lambda: datetime.now(timezone.utc))
    status = database.Column(database.String(20), default='pendente')  # 'pendente', 'em_analise', 'finalizado'
    aceitou_termos_legais = database.Column(database.Boolean, default=False)
    ip_solicitacao = database.Column(database.String(45))  # Para segurança extra

    # Relacionamentos para facilitar a vida do FeedIn Team
    usuario = database.relationship('Usuario', backref='minhas_reivindicacoes')
    local = database.relationship('Local', backref='reivindicacoes_pendentes')


class AvaliacaoLocal(database.Model):
    __tablename__ = 'avaliacoes_locais'
    id = database.Column(database.Integer, primary_key=True)
    id_local = database.Column(database.Integer, database.ForeignKey('locais.id'), nullable=False)
    id_usuario = database.Column(database.Integer, database.ForeignKey('usuario.id'), nullable=False)
    nota = database.Column(database.Integer, nullable=False)
    feedback = database.Column(database.Text, nullable=True)
    data_avaliacao = database.Column(database.DateTime, default=lambda: datetime.now(timezone.utc))

    # ESTA LINHA É A CHAVE: Ela cria o atributo .avaliacoes dentro de Local
    local = database.relationship('Local', backref=database.backref('avaliacoes', lazy=True))


class Notificacao(database.Model):
    __tablename__ = 'notificacoes'
    id = database.Column(database.Integer, primary_key=True)
    id_usuario_destino = database.Column(database.Integer, database.ForeignKey('usuario.id'), nullable=False)
    id_usuario_origem = database.Column(database.Integer, database.ForeignKey('usuario.id'), nullable=True)
    id_postagem_referencia = database.Column(database.Integer, database.ForeignKey('postagens.id'), nullable=True)

    mensagem = database.Column(database.String(255), nullable=False)
    tipo = database.Column(database.String(50))  # 'marcacao', 'curtida', 'comentario'
    lida = database.Column(database.Boolean, default=False)
    data_criacao = database.Column(database.DateTime, default=lambda: datetime.now(timezone.utc))

    # Relacionamentos para facilitar a exibição
    origem = database.relationship('Usuario', foreign_keys=[id_usuario_origem])


class MarcacaoPostagem(database.Model):
    __tablename__ = 'marcacoes_postagens'

    id = database.Column(database.Integer, primary_key=True)

    # 1. Ajustado para 'postagens.id' (Confirme se o __tablename__ da sua classe Postagem é plural ou singular)
    postagem_id = database.Column(database.Integer, database.ForeignKey('postagens.id', ondelete='CASCADE'),
                                  nullable=False)

    # 2. CORREÇÃO CRÍTICA: Mudado de 'usuarios.id' para 'usuario.id' para casar com o seu __tablename__
    usuario_id = database.Column(database.Integer, database.ForeignKey('usuario.id', ondelete='CASCADE'),
                                 nullable=False)
    solicitante_id = database.Column(database.Integer, database.ForeignKey('usuario.id', ondelete='CASCADE'))

    status = database.Column(database.String(20), default='pendente', nullable=False)
    criado_em = database.Column(database.DateTime, default=lambda: datetime.now(timezone.utc))
    respondido_em = database.Column(database.DateTime)

    # Relacionamentos explícitos apontando para a classe 'Usuario'
    postagem = database.relationship('Postagem', backref=database.backref('marcacoes', lazy='dynamic'))

    usuario = database.relationship('Usuario', foreign_keys=[usuario_id],
                                    backref=database.backref('memorias_marcado', lazy='dynamic'))
    solicitante = database.relationship('Usuario', foreign_keys=[solicitante_id],
                                        backref=database.backref('solicitacoes_marcacao_feitas', lazy='dynamic'))


class CredencialBiometrica(database.Model):
    __tablename__ = 'credenciais_biometricas'

    id = database.Column(database.Integer, primary_key=True)
    user_id = database.Column(database.Integer, database.ForeignKey('usuario.id'), nullable=False)
    credential_id = database.Column(database.String(255), unique=True, nullable=False)
    public_key = database.Column(database.Text, nullable=False)
    sign_count = database.Column(database.Integer, default=0)

    # Passando a classe direto (sem aspas!) e deixando o backref explícito
    usuario = database.relationship(Usuario, backref=database.backref('biometrias', lazy=True))


# Tabela Intermediária (Many-to-Many) para ligar Publicações às suas Tags
publicacao_taxonomia = database.Table('publicacao_taxonomia',
    database.Column('publicacao_id', database.Integer, database.ForeignKey('publicacao.id', ondelete='CASCADE'), primary_key=True),
    database.Column('taxonomia_id', database.Integer, database.ForeignKey('taxonomia.id', ondelete='CASCADE'), primary_key=True),
    extend_existing=True  # 🌟 Adicionado para não dar erro no PyCharm ao recriar o banco
)

# Tabela Intermediária: Publicações <-> Tags (Se você usar Many-to-Many aqui)
publicacao_tags = database.Table('publicacao_tags',
                                 database.Column('publicacao_id', database.Integer,
                                                 database.ForeignKey('publicacao.id', ondelete='CASCADE'),
                                                 primary_key=True),
                                 database.Column('taxonomia_id', database.Integer,
                                                 database.ForeignKey('taxonomia.id', ondelete='CASCADE'),
                                                 primary_key=True),
                                 extend_existing=True
                                 )


class Publicacao(database.Model):
    __tablename__ = 'publicacao'
    __table_args__ = {'extend_existing': True}

    id = database.Column(database.Integer, primary_key=True)
    id_usuario = database.Column(database.Integer, database.ForeignKey('usuario.id'), nullable=False)
    descricao = database.Column(database.Text, nullable=False)
    imagem_url = database.Column(database.String(255), nullable=True)

    # Aponta para 'locais.id' (no plural, como está na sua tabela real)
    local_id = database.Column(database.Integer, database.ForeignKey('locais.id'), nullable=True)

    # 🌟 A LINHA QUE FALTAVA: Registro cronológico da memória
    data_cadastro = database.Column(database.DateTime, default=lambda: datetime.now(timezone.utc))

    # RELACIONAMENTOS
    local_objeto = database.relationship('Local', backref='publicacoes_vinculadas', foreign_keys=[local_id])

    tags = database.relationship('Taxonomia',
                                 secondary=publicacao_taxonomia,
                                 backref='publicacoes_com_esta_tag')


class LocalAnuncio(database.Model):
    __tablename__ = 'local_anuncio'

    id = database.Column(database.Integer, primary_key=True)
    local_id = database.Column(database.Integer, database.ForeignKey('locais.id'), nullable=False)
    taxonomia_id = database.Column(database.Integer, database.ForeignKey('taxonomia.id'), nullable=False)
    url_flyer = database.Column(database.String(255), nullable=True)  # Guarda o arquivo se hovar
    local = database.relationship('Local', backref='anuncios_locais')
    
    # === AQUI ESTÁ O QUE FALTAVA: O link de destino do botão/flyer ===
    url_destino = database.Column(database.String(500), nullable=False)

    plano_marketing = database.Column(database.String(50), default='gratuito')
    status = database.Column(database.String(20), default='ativo')
    data_expiracao = database.Column(database.DateTime, nullable=True)
    visualizacoes = database.Column(database.Integer, default=0)
    cliques = database.Column(database.Integer, default=0)


class AnuncioClique(database.Model):
    __tablename__ = 'anuncio_clique'

    id = database.Column(database.Integer, primary_key=True)
    anuncio_id = database.Column(database.Integer, database.ForeignKey('local_anuncio.id'), nullable=False)

    # AJUSTE AQUI: Verifique se sua tabela de usuários se chama 'usuario' ou 'usuarios'
    usuario_id = database.Column(database.Integer, database.ForeignKey('usuario.id'), nullable=False)

    taxonomia_id = database.Column(database.Integer, database.ForeignKey('taxonomia.id'), nullable=False)
    origem_clique = database.Column(database.String(20), nullable=False)
    data_hora = database.Column(database.DateTime, default=datetime.utcnow)

    anuncio = database.relationship('LocalAnuncio', backref='cliques_detalhados')
    usuario = database.relationship('Usuario', backref='cliques_anuncios')

    from sqlalchemy import event

    # Ouvinte de evento: Toda vez que o id_local de QUALQUER postagem mudar...
    @event.listens_for(Postagem.id_local, 'set')
    def interceptar_reset_id(target, value, oldvalue, initiator):
        # Se o valor antigo era um ID válido (como 671) e o novo é None (NULL)
        if oldvalue is not None and value is None:
            import traceback
            print("\n❌❌❌ [ALERTA CRÍTICO] PEGAMOS O CULPADO NO PULO! ❌❌❌")
            print(f"A Postagem ID {target.id} estava amarrada ao local {oldvalue} e foi resetada para NULL.")
            print("Rastro de execução (Quem chamou essa limpeza):")
            # Imprime a linha exata do código que disparou o comando
            traceback.print_stack()


class MensagemCampanha(database.Model):
    id = database.Column(database.Integer, primary_key=True)
    texto = database.Column(database.String(255), nullable=False) # Ex: "Clima de Copa! Esse parceiro apoia nossas memórias de futebol..."
    taxonomia_id = database.Column(database.Integer, database.ForeignKey('taxonomia.id')) # Atrelado a #Futebol, #Samba, etc.
    ativo = database.Column(database.Boolean, default=True)