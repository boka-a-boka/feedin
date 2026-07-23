import uuid
from feedin import database as db
from datetime import datetime, timezone
from flask_wtf import FlaskForm
from wtforms import StringField, PasswordField, SubmitField
from wtforms.validators import DataRequired, Email, Length, EqualTo

import uuid
from datetime import datetime, timezone
from feedin import database as db


class ModCadastroCliente(db.Model):
    """
    PERFIL DE CREDENCIAIS UNIFICADO: Tabela paralela de clientes comum a todos os módulos.
    Centraliza o e-mail, metadados de balcão e hash de senha para acesso seguro ao PWA.
    """
    __tablename__ = 'mod_cadastro_cliente'

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))

    # Elo 1X1 Opcional com o FeedIn Core (Se for NULL, o cliente é só do balcão)
    usuario_id = db.Column(db.Integer, db.ForeignKey('usuario.id'), nullable=True)

    # Dados obrigatórios coletados no balcão/módulo
    nome = db.Column(db.String(150), nullable=False)
    email = db.Column(db.String(150), unique=True, nullable=False)

    # 🛡️ Mantido nullable=False: Casos sem telefone persistem como string vazia ('') tratada na rota
    whatsapp = db.Column(db.String(20), nullable=False)
    data_nascimento = db.Column(db.Date, nullable=True)

    # Motores de Segurança e Conformidade do CPF
    # 🎯 unique=False garante que o mesmo CPF ancore múltiplos perfis/e-mails de acesso
    cpf_hash = db.Column(db.String(64), index=True, unique=False)
    cpf_encrypted = db.Column(db.LargeBinary, nullable=False)

    # Credenciais de Acesso ao Módulo
    # 🎯 unique=True e tamanho 50: Sob medida para o padrão imutável "primeiro.ultimo+num"
    username_modulo = db.Column(db.String(50), unique=True, nullable=False)
    senha_hash = db.Column(db.String(255), nullable=False)

    # Controle de Estado e Auditoria
    status_conta = db.Column(db.String(20), default='ativo')  # ativo, suspenso, bloqueado_fraude
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    # Relacionamento direto com o Usuário para consultas no ecossistema
    usuario_core = db.relationship('Usuario', backref=db.backref('cadastros_modulos', lazy='dynamic'))

    @property
    def cpf(self):
        """Descriptografa o CPF sob demanda utilizando o Fernet do Core."""
        from flask import current_app
        return current_app.fernet.decrypt(self.cpf_encrypted).decode()

    @cpf.setter
    def cpf(self, plain_cpf):
        """MÁGICA INTERNA: Hashea e encripta o CPF usando os motores do Core."""
        from flask import current_app
        from feedin.models import IdentidadeCivil
        self.cpf_hash = IdentidadeCivil.gerar_hash(plain_cpf)
        self.cpf_encrypted = current_app.fernet.encrypt(plain_cpf.encode())

    def __repr__(self):
        return f"<ModCadastroCliente {self.nome} ({self.username_modulo})>"


    # Dentro da classe ModCadastroCliente

    @property
    def nome_completo(self):
        """
        Resolve o nome do usuário para os módulos conexos.
        Tenta buscar o nome oficial na Identidade Civil através do Core,
        se não houver vínculo, retorna o nome de balcão registrado no módulo.
        """
        if self.usuario_core and self.usuario_core.identidade:
            return self.usuario_core.identidade.nome_completo_oficial

        # Fallback para usuários puramente de balcão/módulo
        return self.nome

class ModFilaAtivacaoCliente(db.Model):
    """
    Tabela de trânsito (O Limbo). Guarda os pré-cadastros realizados no balcão
    aguardando ativação/definição de senha pelo cliente.
    """
    __tablename__ = 'mod_fila_ativacao_cliente'

    id = db.Column(db.Integer, primary_key=True)
    usuario_id = db.Column(db.Integer, db.ForeignKey('usuario.id'), nullable=True)

    nome = db.Column(db.String(100), nullable=False)
    whatsapp = db.Column(db.String(20), nullable=False)
    email = db.Column(db.String(255), nullable=True)

    data_disparo = db.Column(db.DateTime, nullable=True)
    data_expiracao = db.Column(db.DateTime, nullable=False)
    data_tentativa_abertura = db.Column(db.DateTime, nullable=True)

    def __repr__(self):
        return f"<ModFilaAtivacaoCliente {self.nome} - WhatsApp: {self.whatsapp}>"


class FormLoginModulo(FlaskForm):
    email = StringField('E-mail de Acesso', validators=[DataRequired(), Email()])
    senha = PasswordField('Senha', validators=[DataRequired(), Length(min=6)])
    botao_confirmacao = SubmitField('Acessar Plataforma')

class FormHabilitarModulo(FlaskForm):
    nome = StringField('Nome Completo Oficial', validators=[DataRequired()])
    email = StringField('E-mail de Acesso', validators=[DataRequired(), Email()])
    whatsapp = StringField('WhatsApp / Celular', validators=[DataRequired()])
    cpf = StringField('CPF (Apenas números)', validators=[DataRequired(), Length(min=11, max=14)])
    senha = PasswordField('Crie uma Senha', validators=[DataRequired(), Length(min=6)])
    confirmar_senha = PasswordField('Confirme a Senha', validators=[DataRequired(), EqualTo('senha', message='As senhas devem ser idênticas.')])
    botao_confirmacao = SubmitField('Habilitar meu Acesso')


class VinculoUsuarioEmpresa(db.Model):
    __tablename__ = 'vinculos_usuario_empresa'

    id = db.Column(db.Integer, primary_key=True)

    # Elos lógicos
    usuario_id = db.Column(db.Integer, db.ForeignKey('usuario.id'), nullable=False)
    empresa_id = db.Column(db.Integer, db.ForeignKey('ese_empresa.id'), nullable=False)

    # Mapeamento do userpaper
    papel_nome = db.Column(db.String(30), nullable=False)  # Ex: 'gerente', 'supervisor', 'caixa'
    papel_nivel = db.Column(db.Integer, nullable=False)  # Ex: 777, 666, 500 (O peso numérico)

    # Rastreabilidade do vínculo
    data_vinculo = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    ativo = db.Column(db.Boolean, default=True)

    # Relacionamentos para facilitar buscas
    usuario = db.relationship('Usuario', backref=db.backref('vinculos_empresas', lazy=True))
    empresa = db.relationship('EseEmpresa', backref=db.backref('equipe_vinculada', lazy=True))

    def __repr__(self):
        return f"<Vinculo: Usuário ID {self.usuario_id} é {self.papel_nome} ({self.papel_nivel}) na Empresa ID {self.empresa_id}>"

class ModVinculoModulo(db.Model):
    """
    TABELA RELACIONAL: Mapeia de forma universal o acesso do cliente a múltiplos
    módulos externos (agenda, convites, cardapio, etc.) unificados pelo CPF.
    """
    __tablename__ = 'mod_vinculo_modulo'
    __table_args__ = {'extend_existing': True}

    id = db.Column(db.Integer, primary_key=True)

    # O elo universal de ligação (Hash do CPF vindo da varredura)
    cpf_hash = db.Column(db.String(64), index=True, nullable=False)

    # Identificador do módulo (ex: 'agenda', 'venda_convites', 'cardapio_online')
    modulo_slug = db.Column(db.String(50), nullable=False)

    # Contexto local (Vincula o usuário à barbearia, restaurante ou estabelecimento atual)
    local_id = db.Column(db.Integer, db.ForeignKey('locais.id'),
                         nullable=True)  # Ajuste se o nome da tabela de locais for diferente

    # Flexibilidade: Caso o usuário queira usar um e-mail de notificação diferente para ESTE módulo
    email_customizado = db.Column(db.String(255), nullable=True)

    # Metadados operacionais
    criado_em = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    ativo = db.Column(db.Boolean, default=True)

    def __repr__(self):
        return f"<ModVinculoModulo CPF_Hash: {self.cpf_hash[:8]} -> Módulo: {self.modulo_slug}>"

class AthAtribContexto(db.Model):
    """
    A TABELA DE MEMÓRIA URBANA E SOCIAL (NOVA).

    UTILIDADE TÉCNICA E NEGÓCIO:
    Armazena de forma elástica as respostas de enquetes e os metadados de
    relacionamento que os módulos (como a Agenda) capturam, mas quem gerencia
    é o Auth.

    Permite saber, por exemplo, desde que ano o usuário frequenta um local,
    como conheceu o estabelecimento, etc., personalizando o atendimento de balcão.
    """
    __tablename__ = 'ath_atrib_contexto'

    id = db.Column(db.Integer, primary_key=True)

    # Elo com a tabela principal herdada do seu legado
    cadastro_cliente_id = db.Column(db.String(36), db.ForeignKey('mod_cadastro_cliente.id'), nullable=False)

    # ID do estabelecimento (Local) onde a informação faz sentido
    local_id = db.Column(db.Integer, db.ForeignKey('locais.id'), nullable=True)

    # A chave e o valor do atributo coletado
    chave = db.Column(db.String(50), nullable=False, index=True)  # Ex: 'ano_inicio_relacionamento'
    valor = db.Column(db.TEXT, nullable=False)  # Ex: '2021'

    criado_em = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    # Relacionamento direto para consultas fáceis
    cliente = db.relationship('ModCadastroCliente', backref=db.backref('atributos_contexto', lazy='dynamic'))

    # Restrição para garantir consistência (uma resposta por pergunta, por cliente, por local)
    __table_args__ = (
        db.UniqueConstraint('cadastro_cliente_id', 'local_id', 'chave', name='uq_cliente_local_chave'),
    )

    def __repr__(self):
        return f"<AthAtribContexto Cliente: {self.cadastro_cliente_id[:8]} - {self.chave}: {self.valor}>"