import os
import logging
import re
from datetime import timedelta, timezone, datetime
from logging.handlers import RotatingFileHandler
from flask import Flask
from flask_bcrypt import Bcrypt
from flask_login import LoginManager
from flask_sqlalchemy import SQLAlchemy
from flask_bootstrap import Bootstrap5
from flask_mail import Mail
from flask_migrate import Migrate
from flask_wtf.csrf import CSRFProtect
from cryptography.fernet import Fernet
from dotenv import load_dotenv
from werkzeug.middleware.proxy_fix import ProxyFix

# 1. INSTANCIAÇÃO NEUTRA DAS EXTENSÕES
database = SQLAlchemy()
migrate = Migrate(render_as_batch=True)
bcrypt = Bcrypt()
bootstrap = Bootstrap5()
csrf = CSRFProtect()
login_manager = LoginManager()
mail = Mail()


def create_app():
    """
    🏭 APPLICATION FACTORY: A linha de montagem blindada do FeedIn.
    Centraliza a segurança e distribui a carga lógica de forma organizada.
    """
    load_dotenv()
    app = Flask(__name__)

    # --- CONFIGURAÇÃO PARA PROXY REVERSO (NGINX / VPS) ---
    # Garante que o Flask entenda os cabeçalhos de HTTPS e Host enviados pelo Nginx
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

    # --- CONFIGURAÇÃO DE LOGS ---
    if not os.path.exists('logs'):
        os.mkdir('logs')
    file_handler = RotatingFileHandler('logs/feedin.log', maxBytes=10240, backupCount=10)
    file_handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s: %(message)s [in %(pathname)s:%(lineno)d]'))
    file_handler.setLevel(logging.INFO)
    app.logger.addHandler(file_handler)
    app.logger.setLevel(logging.INFO)

    # --- CONFIGURAÇÕES DE CRIPTOGRAFIA DO CPF ---
    CHAVE_CPF = os.environ.get('CHAVE_CRIPTOGRAFIA_CPF')
    if CHAVE_CPF:
        app.fernet = Fernet(CHAVE_CPF)
    else:
        app.logger.warning("CHAVE_CRIPTOGRAFIA_CPF não encontrada no arquivo .env")

    # --- CONFIGURAÇÕES DE BANCO DE DADOS (SQLITE INSTANCE) ---
    basedir = os.path.abspath(os.path.dirname(__file__))
    project_root = os.path.dirname(basedir)
    instance_path = os.path.join(project_root, 'instance')
    os.makedirs(instance_path, exist_ok=True)
    db_path = os.path.join(instance_path, 'feedin-db.db')

    app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{db_path}'
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

    # --- SEGURANÇA E CHAVES ---
    app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', '$2a$20$DefaultFallbackKeySeOEnvFalhar')
    app.config['SECURITY_PASSWORD_SALT'] = os.environ.get('SECURITY_PASSWORD_SALT', '$2a$12$SaltFallback')
    app.config['REMEMBER_COOKIE_DURATION'] = timedelta(days=30)
    app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=30)
    app.config['SESSION_PROTECTION'] = 'strong'
    app.config["PASTA_FOTOS"] = "fotos_perfil"
    app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024

    # --- CHAVEAMENTO DE AMBIENTE: LOCALHOST vs VPS / HOMOLOGAÇÃO ---
    is_production = os.environ.get('FLASK_ENV') == 'production' or os.name != 'nt'

    # Deixamos o DOMAIN como None para aceitar dinamicamente subdomínios (ex: homolog, IP ou dominio principal)
    app.config['SESSION_COOKIE_DOMAIN'] = None
    app.config['SESSION_COOKIE_HTTPONLY'] = True
    app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

    # Habilita HTTPS Secure nos cookies apenas se explicitamente em produção/SSL configurado
    # Se estiver testando homologação por HTTP, desativa temporariamente para evitar a perda do CSRF
    usar_https = os.environ.get('USE_HTTPS', 'false').lower() == 'true'
    app.config['SESSION_COOKIE_SECURE'] = usar_https
    app.config['REMEMBER_COOKIE_SECURE'] = usar_https

    app.config['MODO_PRODUCAO'] = is_production
    app.config['DATA_FIM_BETA'] = datetime(2026, 8, 5, tzinfo=timezone.utc)

    # --- CONFIGURAÇÕES DE E-MAIL ---
    app.config['MAIL_SERVER'] = 'smtp.gmail.com'
    app.config['MAIL_PORT'] = 587
    app.config['MAIL_USE_TLS'] = True
    app.config['MAIL_USERNAME'] = 'portal.indicapira@gmail.com'
    app.config['MAIL_PASSWORD'] = 'osqo ohef suzl nree'

    # 2. ⚡ VINCULAÇÃO DAS EXTENSÕES AO APP CONSTRUÍDO
    database.init_app(app)
    migrate.init_app(app, database)
    bcrypt.init_app(app)
    bootstrap.init_app(app)
    csrf.init_app(app)
    mail.init_app(app)

    login_manager.init_app(app)
    # Aponta diretamente para o endpoint do blueprint de autenticação, se aplicável, ou 'login'
    login_manager.login_view = "auth.login" if "auth_bp" in locals() else "login"
    login_manager.login_message = "Sua sessão expirou, por favor faça login novamente."
    login_manager.login_message_category = "info"

    # 3. 🛰️ PROTOCOLO DE CONEXÃO DOS ACESSÓRIOS DO CORE
    with app.app_context():
        # Filtros de template customizados
        from .utils import tempo_atras_filter
        app.template_filter('tempo_atras')(tempo_atras_filter)

        # Carrega rotas e modelos bases do Core
        from feedin import routes, models

        # 4. 🧩 REGISTRO DOS BLUEPRINTS AUTÔNOMOS
        from feedin.modules.agenda import agenda_bp
        from feedin.modules.empresa import empresa_bp
        from feedin.modules.auth import auth_bp

        app.register_blueprint(agenda_bp)
        app.register_blueprint(empresa_bp)
        app.register_blueprint(auth_bp)

    @login_manager.user_loader
    def load_user(user_id):
        from feedin.models import Usuario
        return Usuario.query.get(int(user_id))

    return app