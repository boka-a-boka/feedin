import os
import logging
import re
from datetime import timedelta, timezone, datetime
from logging.handlers import RotatingFileHandler
from flask import Flask, url_for, current_app, send_from_directory, abort
from flask_bcrypt import Bcrypt
from flask_login import LoginManager
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import event
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

    app.config['SESSION_COOKIE_DOMAIN'] = None
    app.config['SESSION_COOKIE_HTTPONLY'] = True
    app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

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

    # --- INICIALIZAÇÃO DE LISTA DE LOADERS PLUGGABLE ---
    app.config['MODULE_USER_LOADERS'] = []

    # 2. ⚡ VINCULAÇÃO DAS EXTENSÕES AO APP CONSTRUÍDO
    database.init_app(app)
    migrate.init_app(app, database)
    bcrypt.init_app(app)
    bootstrap.init_app(app)
    csrf.init_app(app)
    mail.init_app(app)

    login_manager.init_app(app)
    login_manager.login_view = "auth.login"
    login_manager.login_message = "Sua sessão expirou, por favor faça login novamente."
    login_message_category = "info"

    from feedin.modules.agenda.services.worker_lembretes import init_app as init_worker_lembretes
    init_worker_lembretes(app)

    # 3. 🛰️ PROTOCOLO DE CONEXÃO DOS ACESSÓRIOS DO CORE
    with app.app_context():
        # Filtros de template customizados
        from .utils import tempo_atras_filter
        app.template_filter('tempo_atras')(tempo_atras_filter)

        @event.listens_for(database.engine, 'connect')
        def set_sqlite_pragma(dbapi_connection, connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")  # Habilita gravações sem bloquear leituras
            cursor.execute("PRAGMA busy_timeout=30000")  # Espera até 30 segundos antes de dar erro
            cursor.execute("PRAGMA synchronous=NORMAL")  # Melhora a performance de gravação
            cursor.close()

        @app.route('/media/<path:filename>')
        def serve_media(filename):
            """
            ROTA GLOBAL DE ENTREGA DE MÍDIAS E UPLOADS (DESENVOLVIMENTO & FALLBACK)
            """
            filename_limpo = filename.replace('\\', '/').strip('/')

            # Limpa prefixos redundantes
            for prefixo in ['media/', 'uploads/', 'empresa/uploads/', 'agenda/uploads/']:
                if filename_limpo.lower().startswith(prefixo):
                    filename_limpo = filename_limpo[len(prefixo):]

            base_dir = current_app.root_path  # Aponta para .../ProjetoFeedIn/feedin

            # 1. Pastas exatas do seu projeto
            pasta_agenda_uploads = os.path.join(base_dir, 'modules', 'agenda', 'static', 'uploads')
            pasta_empresa_uploads = os.path.join(base_dir, 'modules', 'empresa', 'static', 'uploads')

            # Teste 1: Busca direta a partir de modules/empresa/static/uploads
            caminho_empresa = os.path.join(pasta_empresa_uploads, filename_limpo)
            if os.path.isfile(caminho_empresa):
                return send_from_directory(os.path.dirname(caminho_empresa), os.path.basename(caminho_empresa))

            # Teste 2: Busca direta a partir de modules/agenda/static/uploads
            caminho_agenda = os.path.join(pasta_agenda_uploads, filename_limpo)
            if os.path.isfile(caminho_agenda):
                return send_from_directory(os.path.dirname(caminho_agenda), os.path.basename(caminho_agenda))

            # Teste 3: Busca recursiva dentro da estrutura /empresas/<ID>/colaboradores
            nome_arquivo = os.path.basename(filename_limpo)
            if nome_arquivo and os.path.exists(pasta_empresa_uploads):
                for root, _, files in os.walk(pasta_empresa_uploads):
                    if nome_arquivo in files:
                        return send_from_directory(root, nome_arquivo)

            abort(404)

        # --- HELPER GLOBAL JINJA2 PARA MÍDIAS CROSS-MODULE ---
        @app.template_global()
        def media_url(modulo: str, caminho_relativo: str) -> str:
            """
            Gera a URL pública aparente direcionada para a rota `/media/`.
            Exemplo no Jinja2: {{ media_url('empresa', colaborador.foto_profissional) }}
            """
            fallback = url_for('static', filename='img/default_avatar.webp')
            if not caminho_relativo:
                return fallback

            caminho_limpo = str(caminho_relativo).replace('\\', '/').strip('/')

            # Se já for uma URL completa ou já começar com /media/
            if caminho_limpo.startswith('http://') or caminho_limpo.startswith('https://'):
                return caminho_limpo
            if caminho_limpo.startswith('media/'):
                return f"/{caminho_limpo}"

            # Retorna o apontamento direto para o endpoint serve_media
            return url_for('serve_media', filename=f"{modulo}/{caminho_limpo}")

        # Carrega rotas e modelos bases do Core
        from feedin import routes, models

        # 4. 🧩 REGISTRO DOS BLUEPRINTS AUTÔNOMOS
        from feedin.modules.agenda import agenda_bp
        from feedin.modules.empresa import empresa_bp
        from feedin.modules.auth import auth_bp
        from feedin.modules.billing import billing_bp
        from feedin.utils import media_bp

        # Registrar definindo o prefixo de cada URL:
        app.register_blueprint(agenda_bp, url_prefix='/agenda')
        app.register_blueprint(empresa_bp, url_prefix='/empresa')
        app.register_blueprint(auth_bp, url_prefix='/auth')


        # 🚀 REGISTRO DO BLUEPRINT DE MÍDIA GLOBAL
        from feedin.utils import media_bp
        app.register_blueprint(media_bp)

    # 5. 🔐 PROVEDOR UNIFICADO DE CARREGAMENTO DE USUÁRIO (DESACOPLADO)
    @login_manager.user_loader
    def load_user(user_id):
        if not user_id:
            return None

        user_id_str = str(user_id).strip()

        # 1. 🌐 ROTA DO CORE (ID Numérico / Inteiro)
        if user_id_str.isdigit():
            try:
                from feedin.models import Usuario
                return Usuario.query.get(int(user_id_str))
            except Exception as e:
                app.logger.error(f"[LOADER CORE] Erro ao carregar Usuario ({user_id_str}): {e}")
                return None

        # 2. 🧩 ROTA DOS MÓDULOS (UUID / String de 36 caracteres)
        for loader_func in app.config.get('MODULE_USER_LOADERS', []):
            try:
                usuario_modulo = loader_func(user_id_str)
                if usuario_modulo:
                    return usuario_modulo
            except Exception as e:
                app.logger.error(f"[LOADER MODULE] Erro ao executar loader do módulo: {e}")

        return None

    return app