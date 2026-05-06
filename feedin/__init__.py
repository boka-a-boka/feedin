from flask import Flask
from flask_bcrypt import Bcrypt
from flask_login import LoginManager
from flask_sqlalchemy import SQLAlchemy
from flask_bootstrap import Bootstrap5
from flask_mail import Mail
from datetime import timedelta, timezone, datetime
from flask_migrate import Migrate
from flask_wtf.csrf import CSRFProtect
from cryptography.fernet import Fernet
from dotenv import load_dotenv
import os
import logging
from logging.handlers import RotatingFileHandler

# 1. Carrega as variáveis do arquivo .env
load_dotenv()

# 2. Criamos o app
app = Flask(__name__)

# --- CONFIGURAÇÃO DE LOGS ---
if not os.path.exists('logs'):
    os.mkdir('logs')

file_handler = RotatingFileHandler('logs/feedin.log', maxBytes=10240, backupCount=10)
file_handler.setFormatter(logging.Formatter(
    '%(asctime)s %(levelname)s: %(message)s [in %(pathname)s:%(lineno)d]'
))
file_handler.setLevel(logging.INFO)
app.logger.addHandler(file_handler)
app.logger.setLevel(logging.INFO)

# --- CONFIGURAÇÕES DE CRIPTOGRAFIA DO CPF ---
CHAVE_CPF = os.environ.get('CHAVE_CRIPTOGRAFIA_CPF')
if CHAVE_CPF:
    app.fernet = Fernet(CHAVE_CPF)
else:
    app.logger.warning("CHAVE_CRIPTOGRAFIA_CPF não encontrada no arquivo .env")

# 3. Importamos o que depende do utils (Filtros de Template)
from .utils import tempo_atras_filter
app.template_filter('tempo_atras')(tempo_atras_filter)

# --- CONFIGURAÇÕES DE BANCO DE DADOS (FOCO SQLITE) ---

# Ajuste de Hierarquia: Subimos um nível para encontrar a 'instance' na raiz do projeto
# Isso evita que o PyCharm/Flask crie uma pasta duplicada dentro de /feedin
basedir = os.path.abspath(os.path.dirname(__file__))
project_root = os.path.dirname(basedir)
instance_path = os.path.join(project_root, 'instance')

# Garante que a pasta 'instance' exista na raiz
if not os.path.exists(instance_path):
    os.makedirs(instance_path)

db_path = os.path.join(instance_path, 'feedin-db.db')

# Configuração Única para SQLite (Local e Produção)
app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{db_path}'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# --- SEGURANÇA E CHAVES ---
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', '$2a$20$DefaultFallbackKeySeOEnvFalhar')
app.config['SECURITY_PASSWORD_SALT'] = os.environ.get('SECURITY_PASSWORD_SALT', '$2a$12$SaltFallback')

# --- CONFIGURAÇÕES DE SESSÃO E COOKIES ---
app.config['REMEMBER_COOKIE_DURATION'] = timedelta(days=30)
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=30)
app.config['SESSION_PROTECTION'] = 'strong'
app.config["PASTA_FOTOS"] = "fotos_perfil"

# Lógica de Segurança de Cookies (SSL)
if os.name != 'nt' or os.environ.get('FLASK_ENV') == 'production':
    app.config['SESSION_COOKIE_SECURE'] = True
    app.config['REMEMBER_COOKIE_SECURE'] = True
    app.config['SESSION_COOKIE_HTTPONLY'] = True
else:
    app.config['SESSION_COOKIE_SECURE'] = False
    app.config['REMEMBER_COOKIE_SECURE'] = False

# --- CONFIGURAÇÃO DE COMPORTAMENTO DO FEED ---
app.config['MODO_PRODUCAO'] = (os.name != 'nt')
app.config['DATA_FIM_BETA'] = datetime(2026, 8, 5, tzinfo=timezone.utc)

# --- CONFIGURAÇÕES DE E-MAIL ---
app.config['MAIL_SERVER'] = 'smtp.gmail.com'
app.config['MAIL_PORT'] = 587
app.config['MAIL_USE_TLS'] = True
app.config['MAIL_USERNAME'] = 'portal.indicapira@gmail.com'
app.config['MAIL_PASSWORD'] = 'osqo ohef suzl nree'
mail = Mail(app)

# --- INICIALIZAÇÃO DAS EXTENSÕES ---
database = SQLAlchemy(app)
migrate = Migrate(app, database, render_as_batch=True)
bcrypt = Bcrypt(app)
bootstrap = Bootstrap5(app)
csrf = CSRFProtect(app)

login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.login_message = "Sua sessão expirou, por favor faça login novamente."
login_manager.login_message_category = "info"

# Importações de rotas e modelos
from feedin import routes, models