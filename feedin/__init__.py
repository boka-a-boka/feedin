from flask import Flask
from flask_bcrypt import Bcrypt
from flask_login import LoginManager
from flask_sqlalchemy import SQLAlchemy
from flask_bootstrap import Bootstrap5
from flask_mail import Mail
from datetime import timedelta, timezone, datetime
from flask_migrate import Migrate
from flask_wtf.csrf import CSRFProtect
from cryptography.fernet import Fernet # IMPORTANTE
from dotenv import load_dotenv # IMPORTANTE
import os
import logging
from logging.handlers import RotatingFileHandler

# 1. Carrega as variáveis do arquivo .env (SECRET_KEY e CHAVE_CPF)
load_dotenv()

# 2. Criamos o app
app = Flask(__name__)

# Configuração de Logs
if not os.path.exists('logs'):
    os.mkdir('logs')

file_handler = RotatingFileHandler('logs/feedin.log', maxBytes=10240, backupCount=10)
file_handler.setFormatter(logging.Formatter(
    '%(asctime)s %(levelname)s: %(message)s [in %(pathname)s:%(lineno)d]'
))
file_handler.setLevel(logging.INFO)
app.logger.addHandler(file_handler)

app.logger.setLevel(logging.INFO)
app.logger.info("FeedIn Startup - Iniciando Beta Oficial")

# --- CONFIGURAÇÕES DE CRIPTOGRAFIA DO CPF ---
# Buscamos a chave do .env. Se não existir, o sistema avisa.
CHAVE_CPF = os.environ.get('CHAVE_CRIPTOGRAFIA_CPF')
if CHAVE_CPF:
    app.fernet = Fernet(CHAVE_CPF)
else:
    # Aviso de segurança caso você esqueça de configurar o .env
    print("AVISO: CHAVE_CRIPTOGRAFIA_CPF não encontrada no arquivo .env")

# 3. Importamos o que depende do utils (evita importação circular)
from .utils import tempo_atras_filter
app.template_filter('tempo_atras')(tempo_atras_filter)

# --- CONFIGURAÇÕES DE BANCO E SEGURANÇA ---
database_uri = os.environ.get('DATABASE_URL')

if database_uri:
    if database_uri.startswith("postgres://"):
        database_uri = database_uri.replace("postgres://", "postgresql://", 1)

    # Adicione os argumentos de SSL para o motor do SQLAlchemy
    app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DATABASE_URL', 'sqlite:///instance/feedin-db.db')
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
        "connect_args": {
            "sslmode": "require"
        }
    }

# Agora buscamos as chaves reais do seu .env
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', '$2a$20$DefaultFallbackKeySeOEnvFalhar')
app.config['SECURITY_PASSWORD_SALT'] = os.environ.get('SECURITY_PASSWORD_SALT', '$2a$12$SaltFallback')

# --- CONFIGURAÇÃO DE COMPORTAMENTO DO FEED ---
app.config['MODO_PRODUCAO'] = False
app.config['DATA_FIM_BETA'] = datetime(2026, 8, 5, tzinfo=timezone.utc)

# --- CONFIGURAÇÕES DE E-MAIL ---
app.config['MAIL_SERVER'] = 'smtp.gmail.com'
app.config['MAIL_PORT'] = 587
app.config['MAIL_USE_TLS'] = True
app.config['MAIL_USERNAME'] = 'portal.indicapira@gmail.com'
app.config['MAIL_PASSWORD'] = 'osqo ohef suzl nree'
mail = Mail(app)

# --- CONFIGURAÇÕES DE SESSÃO E COOKIES ---
app.config['REMEMBER_COOKIE_DURATION'] = timedelta(days=30)
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=30)
app.config['SESSION_PROTECTION'] = 'strong'
app.config["PASTA_FOTOS"] = "fotos_perfil"

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

# Importação ao final para evitar importação circular
from feedin import routes, models