# Cria os formulários do site
from flask_wtf import FlaskForm
from flask_wtf.file import FileField, FileRequired, FileAllowed
from wtforms import StringField, PasswordField, SubmitField, FileField, DateField, SelectField, TextAreaField
from wtforms.validators import DataRequired, Email, Length, EqualTo, Regexp, InputRequired, Optional
from wtforms_sqlalchemy.fields import QuerySelectField
from flask_wtf.file import FileField, FileRequired, FileAllowed

class FormPasso3CadastroEHomologacao(FlaskForm):
    # Credenciais de Acesso
    username = StringField('Nome de Usuário (Apelido)', validators=[DataRequired(), Length(min=3, max=30)])
    email = StringField('E-mail de Acesso', validators=[DataRequired(), Email()])
    password = PasswordField('Defina sua Senha', validators=[
        DataRequired(),
        Length(min=8, message="A senha deve ter no mínimo 8 caracteres.")
    ])
    confirm_password = PasswordField('Confirme sua Senha', validators=[
        DataRequired(),
        EqualTo('password', message="As senhas devem ser iguais.")
    ])

    # 📁 Arquivos de Compliance Jurídico (ModHomologacaoEmpresa)
    comprovante_endereco = FileField('Comprovante de Endereço do Local (PDF, PNG, JPG)', validators=[
        FileRequired(),
        FileAllowed(['pdf', 'png', 'jpg', 'jpeg'], 'Apenas PDF ou Imagens são aceitos.')
    ])
    documento_comercial = FileField('Cartão CNPJ ou Contrato Social (PDF, PNG, JPG)', validators=[
        FileRequired(),
        FileAllowed(['pdf', 'png', 'jpg', 'jpeg'], 'Apenas PDF ou Imagens são aceitos.')
    ])