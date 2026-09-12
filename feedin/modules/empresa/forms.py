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


class FormHomologacaoPleiteante(FlaskForm):
    """
    📝 DATA SCHEMA: AUDITORIA MANUAL DE JURIDICIDADE (FASE PLEITEANTE)
    --------------------------------------------------------------------------------------
    Ajustado para flexibilidade fiscal. Permite CPF para Pessoa Física na primeira empresa
    ou exige CNPJ caso o modelo de negócio avance.
    """
    # Aumentado a flexibilidade de tamanho para o input expandido
    razao_social = StringField('Razão Social / Nome Completo', validators=[DataRequired(), Length(min=3, max=150)])

    # 🔓 Remove-se o DataRequired fixo para permitir a alternância de campos
    cnpj = StringField('CNPJ do Estabelecimento', validators=[Length(max=18)])
    cpf_responsavel = StringField('CPF do Proprietário', validators=[validar_digito_cpf])
    email_contato = StringField('E-mail de Contato Comercial', validators=[DataRequired(), Email()])

    comprovante_endereco = FileField('Comprovante de Endereço', validators=[
        FileAllowed(['pdf', 'png', 'jpg', 'jpeg'], 'Apenas PDF ou Imagens são aceitos.')
    ])
    documento_comercial = FileField('Cartão CNPJ ou Contrato Social', validators=[
        FileAllowed(['pdf', 'png', 'jpg', 'jpeg'], 'Apenas PDF ou Imagens são aceitos.')
    ])

    def validate(self, extra_validators=None):
        """Validação cruzada: Exige que pelo menos um dos dois documentos fiscais seja preenchido"""
        initial_validation = super(FormHomologacaoPleiteante, self).validate(extra_validators)
        if not initial_validation:
            return False

        # Se ambos estiverem vazios, dispara o erro na tela
        if not self.cnpj.data and not self.cpf_responsavel.data:
            self.cnpj.errors.append("Você deve informar o CNPJ ou o CPF do responsável.")
            self.cpf_responsavel.errors.append("Você deve informar o CPF caso não possua CNPJ.")
            return False

        return True