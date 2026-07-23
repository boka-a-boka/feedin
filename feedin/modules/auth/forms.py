from flask_wtf import FlaskForm
from wtforms import StringField, PasswordField, SubmitField, BooleanField
from wtforms.validators import DataRequired, Email, Length, EqualTo, Regexp


class FormLoginUniversal(FlaskForm):
    """
    O formulário canônico de entrada do ecossistema FeedIn.
    """
    email = StringField('E-mail', validators=[
        DataRequired(message="O e-mail é obrigatório para acessar sua conta."),
        Regexp(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$', message="Insira um e-mail válido.")
    ])

    senha = PasswordField('Senha', validators=[
        DataRequired(message="A senha é obrigatória.")
    ])

    lembrar_me = BooleanField('Lembrar-me')

    botao_confirmacao = SubmitField('Acessar plataforma')

