# feedin/modules/agenda/forms.py
from flask_wtf import FlaskForm
from wtforms import StringField, SubmitField, TextAreaField, DecimalField, IntegerField, HiddenField, BooleanField, IntegerField, SelectField, TimeField
from wtforms.validators import DataRequired, Email, Length, ValidationError, NumberRange, Optional
from feedin.modules.agenda.models import ModCadastroCliente


class FormCadastroBalcao(FlaskForm):
    nome = StringField('Nome Completo', validators=[
        DataRequired(message="O nome é obrigatório."),
        Length(min=3, max=100, message="O nome deve ter entre 3 e 100 caracteres.")
    ])

    cpf = StringField('CPF (Apenas números)', validators=[
        DataRequired(message="O CPF é obrigatório para transações e notas fiscais."),
        Length(min=11, max=11, message="O CPF deve conter exatamente 11 dígitos.")
    ])

    whatsapp = StringField('WhatsApp com DDD', validators=[
        DataRequired(message="O WhatsApp é vital para a comunicação e envio das chaves."),
        Length(min=10, max=20, message="Insira um número de telefone válido.")
    ])

    email = StringField('E-mail', validators=[
        DataRequired(message="O e-mail é obrigatório."),
        Email(message="Insira um e-mail válido.")
    ])

    data_nascimento = StringField('Data de Nascimento', validators=[
        DataRequired(message="A data de nascimento é necessária para segurança e homologação."),
        Length(min=10, max=10, message="Insira a data completa no formato dd/mm/aaaa.")
    ])

    botao_salvar = SubmitField('Cadastrar Cliente')


    # Validação para impedir CPFs duplicados
    def validate_cpf(self, cpf):
        cliente = ModCadastroCliente.query.filter_by(cpf=cpf.data).first()
        if cliente:
            raise ValidationError('Este CPF já está cadastrado no sistema.')

    # Validação para impedir E-mails duplicados
    def validate_email(self, email):
        cliente = ModCadastroCliente.query.filter_by(email=email.data).first()
        if cliente:
            raise ValidationError('Este e-mail já está em uso por outro cliente.')

class FormServico(FlaskForm):
    nome = StringField('Nome do Serviço', validators=[
        DataRequired(message="O nome do serviço é obrigatório."),
        Length(min=3, max=100, message="O nome deve ter entre 3 e 100 caracteres.")
    ])

    preco = DecimalField('Preço (R$)', places=2, validators=[
        DataRequired(message="O preço é obrigatório."),
        NumberRange(min=0.01, message="O preço deve ser maior que zero.")
    ])

    duracao_minutos = IntegerField('Duração Estimada (em minutos)', default=30, validators=[
        DataRequired(message="A duração é obrigatória."),
        NumberRange(min=5, max=480, message="A duração deve ser entre 5 e 480 minutos.")
    ])

    # O campo de texto longo para os diferenciais (estilo LinkedIn)
    descricao = TextAreaField('Descrição Opcional (Diferenciais do seu serviço)', validators=[
        Length(max=500, message="A descrição deve ter no máximo 500 caracteres.")
    ])

    exibir_descricao_pwa = BooleanField('Exibir esta descrição para o cliente no PWA', default=True)

    botao_salvar = SubmitField('Salvar Serviço')

class FormConfigMarca(FlaskForm):
    """Formulário para o lojista customizar sua própria identidade visual no PWA"""
    nome = StringField('Nome Fantasia', validators=[DataRequired(), Length(max=100)])
    categoria = StringField('Categoria Principal', validators=[DataRequired(), Length(max=50)])
    # Usaremos inputs do tipo color no HTML para abrir o palpiteiro de cores nativo do celular/PC
    cor_primaria = StringField('Cor Primária (Hexadecimal)', validators=[DataRequired(), Length(max=7)])
    cor_secundaria = StringField('Cor Secundária (Hexadecimal)', validators=[DataRequired(), Length(max=7)])
    submit = SubmitField('Salvar Identidade de Marca')


class FormColaborador(FlaskForm):
    """Formulário para o administrador incluir membros ativos na equipe com amarração de histórico"""
    email_usuario_feedin = StringField('E-mail do Usuário no FeedIn', validators=[DataRequired(), Email()])

    # Dados profissionais para a tabela de Contratos
    id_cargo = SelectField('Cargo / Função na Empresa', coerce=int, validators=[DataRequired()])

    # Configuração de jornada para cálculo de estouro de horário
    hora_inicio = TimeField('Início do Expediente', validators=[DataRequired()])
    hora_fim = TimeField('Fim do Expediente', validators=[DataRequired()])

    submit = SubmitField('Vincular e Cadastrar Membro da Equipe')


class FormCredenciamentoLocal(FlaskForm):
    """Formulário unificado para assumir memória ou inaugurar local inédito"""
    # Campo oculto para controle técnico do Match
    id_local_existente = HiddenField('ID Local Existente', validators=[Optional()])

    # Identificação e Comercial
    nome = StringField('Nome Fantasia do Estabelecimento', validators=[DataRequired(), Length(max=100)])
    documento = StringField('CNPJ / CPF', validators=[Optional(), Length(max=18)])
    id_categoria_principal = IntegerField('Categoria (Taxonomia)', validators=[Optional()])

    # Geolocalização (Campos populados via Autocomplete ou digitados se inédito)
    cep = StringField('CEP', validators=[DataRequired(), Length(max=10)])
    logradouro = StringField('Logradouro (Rua/Av)', validators=[DataRequired(), Length(max=150)])
    numero = StringField('Número', validators=[DataRequired(), Length(max=20)])
    bairro = StringField('Bairro', validators=[DataRequired(), Length(max=100)])
    cidade = StringField('Cidade', default='Piracicaba', validators=[DataRequired(), Length(max=100)])
    estado = StringField('Estado', default='SP', validators=[DataRequired(), Length(max=2)])

    # Contato
    telefone = StringField('Telefone de Contato', validators=[DataRequired(), Length(max=20)])
    is_whatsapp = BooleanField('Este número é WhatsApp?', default=True)
    email = StringField('E-mail Comercial', validators=[Optional(), Length(max=120)])

    submit = SubmitField('Concluir Credenciamento do Balcão')