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

        # --- SERVIDOR DE MÍDIAS DA APLICAÇÃO (MULTI-MÓDULO DESENVOLVIMENTO) ---
        @app.route('/media/<path:filename>')
        def serve_media(filename):
            """
            =============================================================================
            ROTA GLOBAL DE ENTREGA DE MÍDIAS E UPLOADS (DESENVOLVIMENTO & FALLBACK)
            =============================================================================
            Busca iterativa do arquivo físico nos diretórios de uploads dos módulos.
            """
            print(f"\n--- [DEBUG MEDIA - ENTROU NA ROTA] ---")
            print(f"Filename recebido: {filename}")

            # 1. Normalização do caminho (limpa barras e prefixos redundantes)
            filename_limpo = filename.replace('\\', '/').strip('/')
            for prefixo in ['media/', 'uploads/']:
                if filename_limpo.startswith(prefixo):
                    filename_limpo = filename_limpo[len(prefixo):]

            # 2. Caminho raiz do pacote 'feedin'
            base_dir = current_app.root_path

            # 3. Mapeamento dos diretórios físicos reais do projeto
            pastas_busca = [
                # Módulo Empresa (Logos e Colaboradores)
                os.path.join(base_dir, 'modules', 'empresa', 'static', 'uploads'),
                # Módulo Agenda (Avatares)
                os.path.join(base_dir, 'modules', 'agenda', 'static', 'uploads'),
                # Módulo Core / Raiz
                os.path.join(base_dir, 'static', 'uploads'),
                # Fallback configurado no app.config
                current_app.config.get('UPLOAD_FOLDER', '')
            ]

            # 4. Verificação e entrega do arquivo físico
            for pasta in pastas_busca:
                if not pasta or not os.path.exists(pasta):
                    print(f"Pasta inexistente: {pasta}")
                    continue

                caminho_completo = os.path.join(pasta, filename_limpo)
                existe = os.path.isfile(caminho_completo)
                print(f"Testando: {caminho_completo} -> Existe? {existe}")

                if existe:
                    subpasta = os.path.dirname(caminho_completo)
                    nome_arquivo = os.path.basename(caminho_completo)
                    print(f"SUCESSO! Servindo: {nome_arquivo} de {subpasta}")
                    return send_from_directory(subpasta, nome_arquivo)

            # 5. Log e encerramento em caso de arquivo inexistente no disco
            print(f"--- [FIM DEBUG MEDIA - 404 REAL] ---\n")
            current_app.logger.error(
                f"[MEDIA 404] Arquivo '{filename}' não localizado em nenhuma das pastas: {pastas_busca}"
            )
            abort(404)

        # --- HELPER GLOBAL JINJA2 PARA MIDIAS CROSS-MODULE ---
        @app.template_global()
        def media_url(modulo: str, caminho_relativo: str) -> str:
            """
            Resolve a URL de mídias de qualquer módulo com suporte a fallback de erros.
            Exemplo no Jinja2: {{ media_url('empresa', empresa.logo_path) }}
            """
            fallback = url_for('static', filename='img/default_avatar.webp')
            if not caminho_relativo:
                return fallback
            try:
                caminho_limpo = str(caminho_relativo).replace('\\', '/')
                return url_for(f'{modulo}.static', filename=caminho_limpo)
            except Exception as err:
                current_app.logger.warning(
                    f"[MEDIA_URL] Falha ao resolver estático para módulo '{modulo}' com caminho '{caminho_relativo}': {err}"
                )
                return fallback

        # Carrega rotas e modelos bases do Core
        from feedin import routes, models

        # 4. 🧩 REGISTRO DOS BLUEPRINTS AUTÔNOMOS
        from feedin.modules.agenda import agenda_bp
        from feedin.modules.empresa import empresa_bp
        from feedin.modules.auth import auth_bp
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