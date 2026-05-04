# Cria os formulários do site
from flask_wtf import FlaskForm
from flask_wtf.file import FileField, FileRequired, FileAllowed
from wtforms import StringField, PasswordField, SubmitField, FileField, DateField, SelectField, TextAreaField
from wtforms.validators import DataRequired, Email, Length, EqualTo, Regexp, InputRequired, Optional
from wtforms_sqlalchemy.fields import QuerySelectField
from flask_wtf.file import FileField, FileRequired, FileAllowed
#from feedin.models import CategoriaNegocio

class FormNewUser(FlaskForm):
    usuario = StringField("Usuário", validators=[
        DataRequired(message="Este campo é obrigatório"),
        Regexp(r'^[\w-]+$', message="Use apenas letras, números, _ ou -")
    ])

    email = StringField("E-mail", validators=[DataRequired(), Email()])
    senha = PasswordField("Senha", validators=[DataRequired(), Length(6, 20)])
    confirmar_senha = PasswordField(
        'Confirme a Senha',
        validators=[
            DataRequired(),
            EqualTo('senha', message='As senhas devem ser iguais.')
        ]
    )
    botao_confirmacao = SubmitField("Enviar")


class FormLogin(FlaskForm):
    email = StringField("E-mail", validators=[DataRequired(), Email()])
    senha = PasswordField("Senha", validators=[DataRequired(), Length(6, 20)])
    botao_confirmacao = SubmitField("Entrar")


class FormPerfil(FlaskForm):
    # O único campo estritamente obrigatório para o Flask não barrar o envio
    nome_completo = StringField("Nome completo", validators=[DataRequired(message="Nome é obrigatório")])

    # Adicionamos Optional() para que, se a data vier vazia ou estranha, o form NÃO invalide
    data_nascimento = DateField('Data de Nascimento', validators=[Optional()])

    cidade_natal = StringField("Cidade natal", validators=[Optional()])

    # SelectFields: Se o banco estiver vazio ou o ID não bater, o Optional() salva o processo
    estado_civil = SelectField('Estado civil', coerce=int, validators=[Optional()])
    genero = SelectField('Gênero', coerce=int, validators=[Optional()])

    biografia = TextAreaField('Sua História (Bio)', validators=[Optional(), Length(max=500)])

    botao_confirmacao = SubmitField("Salvar Informações do Perfil")

class FormApelido(FlaskForm):
    apelido = StringField("Como seus amigos te chamam?", validators=[
        InputRequired(message="Digite um apelido"),
        Length(max=30)
    ])
    # O ID do usuário/perfil a gente pega via Python (Back-end) por segurança.

def busca_categorias():
    from feedin.models import Taxonomia  # <--- Importação movida para cá
    return Taxonomia.query.all()


class FormEmpresa(FlaskForm):
    nome_fantasia = StringField('Nome do Empreendimento', validators=[DataRequired()])

    # O CAMPO DINÂMICO:
    categoria = QuerySelectField(
        'Categoria do Negócio',
        query_factory=busca_categorias,  # Aqui chamamos a função acima
        get_label='nome',  # Qual coluna do banco mostrar no select
        allow_blank=True,
        blank_text='Selecione uma categoria...'
    )

    documento = StringField('CPF ou CNPJ (Apenas números)', validators=[DataRequired(), Length(min=11, max=14)])

    # Endereço
    cep = StringField('CEP', validators=[DataRequired(), Length(min=8, max=8)])
    logradouro = StringField('Endereço', validators=[DataRequired()])
    numero = StringField('Número', validators=[DataRequired()])
    bairro = StringField('Bairro', validators=[DataRequired()])
    cidade = StringField('Cidade', validators=[DataRequired()])
    estado = StringField('UF', validators=[DataRequired(), Length(min=2, max=2)])
    descricao = TextAreaField('Fale um pouco sobre o seu negócio')

    botao_confirmar = SubmitField('Cadastrar Empreendimento')

class FormConvite(FlaskForm):
    # Campo para o WhatsApp: Obrigatório e com limite de caracteres
    whatsapp = StringField('WhatsApp do Convidado', validators=[
        DataRequired(message="O número do WhatsApp é obrigatório."),
        Length(min=10, max=15, message="Insira um número válido com DDD.")
    ])

    # Campo para o nome: Opcional
    nome_convidado = StringField('Nome do Convidado (Opcional)')

    submit = SubmitField('Gerar Convite')


class FormConexao(FlaskForm):
    # Não precisa de campos, o CSRF já vem embutido por herança
    pass