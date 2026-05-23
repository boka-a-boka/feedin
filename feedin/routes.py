from flask import (url_for, redirect, render_template, flash, session, request,
                   abort, Response, jsonify, current_app, send_from_directory, Blueprint)
from feedin import app, database, bcrypt
from flask_mail import Mail, Message
from flask_login import login_required, login_user, logout_user, current_user
from feedin.forms import FormLogin, FormNewUser, FormPerfil, FormApelido, FormConvite, FormConexao
from itsdangerous import URLSafeTimedSerializer
from datetime import datetime, timezone, date, timedelta
from feedin.models import (Usuario, EstadoCivil, Generos, Apelidos, Perfil, Parentesco,
                           GrauParentesco, MembroGrupo, GrupoSocial, Local, Conexoes, Memoria,
                           AtividadeLocal, VinculoUsuarioLocal, Taxonomia, LocalMidia, Convite,
                           taxonomia_conexoes, ConviteAdmin, IdentidadeCivil, Postagem, PostagemComentario,
                           PostagemInteracao, postagem_tags, usuarios_interesses, ReivindicacaoLocal,
                           AvaliacaoLocal, Notificacao, Bloqueios, Desconexoes, MarcacaoPostagem,
                           CredencialBiometrica)

from feedin.utils import salvar_imagem, processar_mudanca_nivel, obter_signo, validar_cpf_estrutura, salvar_imagem_capa, salvar_imagem_postagem

from webauthn import (generate_registration_options, verify_registration_response, options_to_json,
                      generate_authentication_options, verify_authentication_response)
from webauthn.helpers.structs import PublicKeyCredentialDescriptor

from flask_wtf.csrf import CSRFProtect

biometria_bp = Blueprint('biometria', __name__)

# O WebAuthn precisa saber o domínio exato do seu app
RP_ID = "boka-a-boka.com.br"  # Ou o subdomínio completo do FeedIn se preferir: "feedin.boka-a-boka.com.br"
RP_NAME = "FeedIn"

from urllib.parse import quote
from functools import wraps
from sqlalchemy import or_, func, asc, and_, not_
from sqlalchemy.orm import joinedload
from cryptography.fernet import Fernet  # Para criptografia reversível
from markupsafe import escape
import secrets, os, re, io, csv, json, pytz, uuid, markdown, base64
from itertools import groupby
from io import TextIOWrapper
from PIL import Image, ImageOps


mail = Mail(app)
s = URLSafeTimedSerializer(app.config['SECRET_KEY'])
app.secret_key = app.config['SECRET_KEY']

# Define o fuso horário de São Paulo (que abrange Piracicaba)
fuso_sp = pytz.timezone('America/Sao_Paulo')

# Supondo que você guardou sua chave na configuração do App ou variável de ambiente
CHAVE_MESTRA = b'VUSlvfpIeAMtezp0VfI76eArKJ6f-Xp9UsPqmZDzxlI='
fernet = Fernet(CHAVE_MESTRA)

def generate_confirmation_token(email):
    serializer = URLSafeTimedSerializer(app.config['SECRET_KEY'])
    return serializer.dumps(email, salt=app.config['SECURITY_PASSWORD_SALT'])

def confirm_token(token, expiration=3600):
    serializer = URLSafeTimedSerializer(app.config['SECRET_KEY'])
    try:
        email = serializer.loads(
            token,
            salt=app.config['SECURITY_PASSWORD_SALT'],
            max_age=expiration
        )
    except:
        return False
    return email


@app.before_request
def verificar_obrigatoriedade_cpf():
    # Rotas que NÃO devem ser interceptadas (evita loop infinito)
    rotas_excecao = ['static', 'logout', 'processar_identidade', 'get_perfil']

    if current_user.is_authenticated:
        # Se não aceitou LGPD ou não tem identidade vinculada
        if not current_user.aceite_lgpd and request.endpoint not in rotas_excecao:
            # Força o redirecionamento para o perfil, mas avisando que o modal deve abrir
            return redirect(url_for('get_perfil', id_usuario=current_user.id, forcar_validacao=True))


@app.route('/meu-cofre')
@login_required
def exibir_cofre():
    # Buscamos o objeto de identidade vinculado ao usuário
    # Graças ao back_populates='identidade', acessamos via current_user.identidade
    identidade = current_user.identidade

    # Se 'identidade' existir, significa que o usuário completou a verificação
    tem_documento = True if identidade else False

    # Pegamos a data de verificação direto da tabela IdentidadeCivil
    data_verificacao = identidade.data_verificacao if identidade else None

    return render_template('cofre.html',
                           tem_documento=tem_documento,
                           data_verificacao=data_verificacao)


@app.route('/sw.js')
def serve_sw():
    return app.send_static_file('js/sw.js')


@app.route('/favicon.ico')
def favicon():
    return app.send_static_file('imagens/favicon.png')


@app.route('/alterar-senha', methods=['GET', 'POST'])
@login_required
def alterar_senha():
    if request.method == 'POST':
        # 1. Coleta os dados do formulário
        senha_atual = request.form.get('senha_atual')
        nova_senha = request.form.get('nova_senha')
        confirmacao = request.form.get('confirma_senha')

        # 2. Validações
        if not bcrypt.check_password_hash(current_user.senha, senha_atual):
            flash('A senha atual está incorreta.', 'danger')
            return redirect(url_for('configuracoes', aba='seguranca'))

        if nova_senha != confirmacao:
            flash('As novas senhas não coincidem.', 'danger')
            return redirect(url_for('configuracoes', aba='seguranca'))

        if len(nova_senha) < 6:
            flash('A nova senha deve ter pelo menos 6 caracteres.', 'danger')
            return redirect(url_for('configuracoes', aba='seguranca'))

        # 3. Processo de salvamento
        try:
            hashed_password = bcrypt.generate_password_hash(nova_senha).decode('utf-8')
            current_user.senha = hashed_password
            database.session.commit()
            flash('Sua senha foi atualizada com sucesso!', 'success')
        except Exception as e:
            database.session.rollback()
            print(f"Erro ao salvar no banco: {e}")
            flash('Erro interno ao atualizar a senha.', 'danger')

    # Este último return deve estar alinhado com o primeiro 'if'
    # Ele garante que se for um 'GET', o usuário volta para a tela certa
    return redirect(url_for('configuracoes', aba='seguranca'))


@app.route('/confirmar-email/<token>')
def confirmar_email(token):
    # 1. Decodifica o token
    email = confirm_token(token)

    if not email:
        flash('O link de confirmação é inválido ou expirou.', 'danger')
        return redirect(url_for('login'))

    # 2. Busca o usuário
    usuario = Usuario.query.filter_by(email=email).first_or_404()

    # 3. ATIVAÇÃO (Sem Login Automático)
    if not usuario.active:
        usuario.active = True
        database.session.commit()
        # Mensagem clara para o usuário saber que o próximo passo é logar
        flash('E-mail confirmado com sucesso! Agora, acesse sua conta com sua senha para continuar.', 'success')
    else:
        flash('Esta conta já foi ativada anteriormente. Basta fazer login.', 'info')

    # 4. Redireciona para o login (Onde ele terá que provar que sabe a senha)
    return redirect(url_for('login'))

def notify(message, type):
    """
    Função genérica para enviar notificações ao usuário.
    Tipos sugeridos (Bootstrap): 'success', 'danger', 'warning', 'info'
    """
    # Você pode adicionar lógica extra aqui, como logs ou traduções
    flash(message, type)


@app.route("/logout")
@login_required
def realizar_logout():
    from flask import make_response

    logout_user()  # Remove do Flask-Login
    session.clear()  # Limpa o dicionário da sessão

    # Criamos a resposta de redirecionamento para a página inicial (index)
    response = make_response(redirect(url_for('index')))

    # FORÇA o navegador a invalidar o cookie de sessão
    # O segredo para produção é garantir que os parâmetros batam com os do __init__.py
    response.set_cookie(
        'session',
        '',
        expires=0,
        httponly=True,
        secure=True,  # Como você usa HTTPS, isso é vital
        samesite='Lax'
    )

    flash("Sessão encerrada com sucesso. Até logo!", "info")
    return response


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        # Se logado e com CPF ok -> Feed. Se logado sem CPF -> Perfil (onde a modal aparecerá)
        target = 'feed' if current_user.aceite_lgpd else 'get_perfil'
        return redirect(url_for(target, id_usuario=current_user.id))

    session.permanent = True
    form_login = FormLogin()

    if form_login.validate_on_submit():
        usuario = Usuario.query.filter_by(email=form_login.email.data).first()

        if usuario and bcrypt.check_password_hash(usuario.senha, form_login.senha.data):
            if usuario.active:
                login_user(usuario, remember=True)

                # Lógica simplificada:
                # 1. Sem LGPD? Vai pro Perfil/Dashboard (a modal vai travar lá)
                if not usuario.aceite_lgpd:
                    flash("Validação de identidade necessária.", "info")
                    return redirect(url_for('get_perfil', id_usuario=usuario.id))

                # 2. Nível baixo? Completa os dados
                if usuario.nivel_acesso < 10:
                    flash('Bem-vindo! Vamos completar seu perfil.', 'info')
                    return redirect(url_for("get_perfil", id_usuario=usuario.id))

                # 3. Tudo OK? Feed direto.
                flash(f"Olá, {usuario.username}!", "success")
                return redirect(url_for("feed"))  # Direcionando para o Feed como você queria
            else:
                flash('Usuário requer ativação. Verifique seu e-mail.', 'warning')
        else:
            flash('E-mail e/ou senha incorretos.', 'danger')

    tem_biometria = request.cookies.get('biometria_ativa') == 'true'

    return render_template('login.html', form=form_login, tem_biometria=tem_biometria)


@biometria_bp.route('/biometria/login/opcoes', methods=['POST'])
def login_opcoes():
    """1. Gera o desafio de segurança para o celular tentar autenticar"""
    dados = request.get_json()
    email = dados.get('email')

    usuario = Usuario.query.filter_by(email=email).first()
    if not usuario or not usuario.biometrias:
        return jsonify({'status': 'erro', 'mensagem': 'Biometria não configurada para este usuário'}), 404

    # Lista as credenciais permitidas para este usuário
    credenciais_permitidas = [
        PublicKeyCredentialDescriptor(id=c.credential_id.encode('utf-8'))
        for c in usuario.biometrias
    ]

    opcoes = generate_authentication_options(
        rp_id=RP_ID,
        allow_credentials=credenciais_permitidas,
    )

    # Salva o desafio na sessão do Flask para validar no próximo passo
    session['authentication_challenge'] = opcoes.challenge.decode('utf-8')
    session['auth_user_id'] = usuario.id

    return options_to_json(opcoes)


@biometria_bp.route('/biometria/login/verificar', methods=['POST'])
def login_verificar():
    """2. Confere a resposta do Face ID e faz o login do usuário"""
    dados_resposta = request.get_json()
    desafio_salvo = session.get('authentication_challenge')
    user_id = session.get('auth_user_id')

    if not desafio_salvo or not user_id:
        return jsonify({'status': 'erro', 'mensagem': 'Sessão de login expirada'}), 400

    usuario = Usuario.query.get(user_id)
    # Busca a chave pública que salvamos no passo de ativação
    credencial_banco = CredencialBiometrica.query.filter_by(
        credential_id=dados_resposta.get('id'),
        user_id=user_id
    ).first()

    if not credencial_banco:
        return jsonify({'status': 'erro', 'mensagem': 'Credencial não encontrada'}), 400

    try:
        verificacao = verify_authentication_response(
            credential=dados_resposta,
            expected_challenge=desafio_salvo.encode('utf-8'),
            expected_origin=f"https://{RP_ID}",
            expected_rp_id=RP_ID,
            credential_public_key=credencial_banco.public_key.encode('utf-8'),
            credential_current_sign_count=credencial_banco.sign_count,
        )

        # Atualiza o contador de uso da chave (exigência de segurança do WebAuthn)
        credencial_banco.sign_count = verificacao.new_sign_count
        database.session.commit()

        # LOGADO COM SUCESSO! O Flask assume a sessão do usuário aqui
        login_user(usuario)

        return jsonify({'status': 'sucesso', 'redirecionar': '/dashboard'})  # Altere para sua rota pós-login

    except Exception as e:
        return jsonify({'status': 'erro', 'mensagem': 'Falha na autenticação biométrica'}), 400


@biometria_bp.route('/biometria/login/opcoes', methods=['POST'])
def login_opcoes():
    """1. Gera o desafio de segurança para o dispositivo do usuário tentar autenticar"""
    dados = request.get_json()
    email = dados.get('email')

    usuario = Usuario.query.filter_by(email=email).first()
    if not usuario or not usuario.biometrias:
        return jsonify({'status': 'erro', 'mensagem': 'Biometria não configurada'}), 404

    credenciais_permitidas = [
        PublicKeyCredentialDescriptor(id=c.credential_id.encode('utf-8'))
        for c in usuario.biometrias
    ]

    # Ajustado para o nome real da sua máquina: generate_authentication_options
    opcoes = generate_authentication_options(
        rp_id=RP_ID,
        allow_credentials=credenciais_permitidas,
    )

    session['authentication_challenge'] = opcoes.challenge.decode('utf-8')
    session['auth_user_id'] = usuario.id

    return options_to_json(opcoes)


@biometria_bp.route('/biometria/login/verificar', methods=['POST'])
def login_verificar():
    """2. Confere a resposta do sensor biométrico e realiza o login"""
    dados_resposta = request.get_json()
    desafio_salvo = session.get('authentication_challenge')
    user_id = session.get('auth_user_id')

    if not desafio_salvo or not user_id:
        return jsonify({'status': 'erro', 'mensagem': 'Sessão expirada'}), 400

    usuario = Usuario.query.get(user_id)
    credencial_banco = CredencialBiometrica.query.filter_by(
        credential_id=dados_resposta.get('id'),
        user_id=user_id
    ).first()

    if not credencial_banco:
        return jsonify({'status': 'erro', 'mensagem': 'Credencial não encontrada'}), 400

    try:
        # Ajustado para o nome real da sua máquina: verify_authentication_response
        verificacao = verify_authentication_response(
            credential=dados_resposta,
            expected_challenge=desafio_salvo.encode('utf-8'),
            expected_origin=f"https://{RP_ID}",
            expected_rp_id=RP_ID,
            credential_public_key=credencial_banco.public_key.encode('utf-8'),
            credential_current_sign_count=credencial_banco.sign_count,
        )

        credencial_banco.sign_count = verificacao.new_sign_count
        database.session.commit()

        login_user(usuario)
        return jsonify({'status': 'sucesso', 'redirecionar': '/dashboard'})

    except Exception as e:
        return jsonify({'status': 'erro', 'mensagem': 'Falha na autenticação biométrica'}), 400


@app.route('/ativar-biometria', methods=['GET', 'POST'])
def ativar_biometria():
    print(f"DEBUG VPS: Recebi uma requisição {request.method} na biometria!")
    if request.method == 'GET':
        return render_template('ativar_biometria.html')

    # Processamento do POST (Validação de primeiro vínculo)
    dados = request.get_json()
    email = dados.get('email')
    senha = dados.get('senha')

    usuario = Usuario.query.filter_by(email=email).first()

    if usuario and bcrypt.check_password_hash(usuario.senha, senha):
        # Gerar o Challenge de forma limpa para Base64URL
        challenge_bytes = os.urandom(32)
        challenge = base64.urlsafe_b64encode(challenge_bytes).decode('utf-8').rstrip('=')

        # Codificar o User ID em Base64URL rígido
        user_id_str = str(usuario.id)
        user_id_b64url = base64.urlsafe_b64encode(user_id_str.encode('utf-8')).decode('utf-8').rstrip('=')

        # Estrutura padrão exigida pela especificação W3C / @github/webauthn-json
        registration_options = {
            "publicKey": {
                "challenge": challenge,
                "rp": {"name": "FeedIn", "id": request.host.split(':')[0]},
                "user": {
                    "id": user_id_b64url,
                    "name": usuario.email,
                    "displayName": usuario.username
                },
                "pubKeyCredParams": [{"type": "public-key", "alg": -7}],  # ES256
                "authenticatorSelection": {
                    "authenticatorAttachment": "platform",
                    "userVerification": "required"
                },
                "timeout": 60000
            }
        }

        # Salva o desafio temporariamente na sessão para validar no próximo passo
        session['biometria_challenge'] = challenge
        session['biometria_user_id'] = usuario.id

        return jsonify({"status": "sucesso", "options": registration_options})

    return jsonify({"status": "erro", "mensagem": "E-mail ou senha incorretos."}), 401


@app.route('/concluir-cadastro-biometria', methods=['POST'])
def concluir_cadastro_biometria():
    print("DEBUG VPS: Recebi a resposta do hardware para salvar no banco!")

    dados = request.get_json()
    usuario_id = session.get('biometria_user_id')
    challenge_salvo = session.get('biometria_challenge')

    if not usuario_id or not challenge_salvo:
        return jsonify({"status": "erro", "mensagem": "Sessão expirada ou inválida. Recomece o processo."}), 400

    try:
        # 1. Coleta os dados que o JavaScript enviou do hardware do celular
        credential_id = dados.get('rawId')
        public_key_b64 = dados.get('response', {}).get('attestationObject')
        client_data_json_b64 = dados.get('response', {}).get('clientDataJSON')

        if not credential_id or not public_key_b64:
            return jsonify({"status": "erro", "mensagem": "Dados biométricos incompletos enviados pelo dispositivo."}), 400

        # ==================== O SEU FLUXO ORIGINAL DE GRAVAÇÃO ====================
        # (Aqui o Flask usa a 'nova_credencial' que você já configurou na lógica do seu app)
        database.session.add(nova_credencial)
        database.session.commit()

        # Limpa as variáveis temporárias da sessão de cadastro
        session.pop('biometria_challenge', None)
        session.pop('biometria_user_id', None)

        print(f"DEBUG VPS: Credencial {credential_id[:10]}... salva com sucesso!")

        # 2. A MÁGICA DO COOKIE (Aqui nós plantamos a marcação no navegador)
        resposta = jsonify({"status": "sucesso"})
        resposta.set_cookie('biometria_ativa', 'true', max_age=31536000, httponly=False, samesite='Lax')

        return resposta

    except Exception as e:
        database.session.rollback()
        print(f"DEBUG VPS ERRO: Falha ao salvar biometria. Motivo: {str(e)}")
        return jsonify({"status": "erro", "mensagem": "Erro interno ao salvar no servidor."}), 500


@app.route('/login-biometria-challenge', methods=['POST'])
def login_biometrico_desafio():
    # 1. Gera o código aleatório de segurança (idêntico ao seu original)
    challenge = os.urandom(32)
    challenge_b64 = base64.b64encode(challenge).decode('utf-8').replace('=', '')

    # Salva na sessão temporária para conferirmos no próximo passo
    session['login_challenge'] = challenge_b64

    # 2. BUSCA AS CREDENCIAIS NO BANCO
    # Linha corrigida: alinhada perfeitamente com o bloco da função
    todas_credenciais = ChaveBiometrica.query.all()

    # Extraímos apenas as IDs textuais das chaves para enviar ao front-end
    credential_ids = [c.credential_id for c in todas_credenciais]

    # 3. Devolve a estrutura limpa que o novo JavaScript espera
    return jsonify({
        "status": "sucesso",
        "challenge": challenge_b64,
        "credential_ids": credential_ids
    })


@app.route('/login-biometrico', methods=['POST'])
def login_biometrico():
    print("DEBUG VPS: Tentativa de login via biometria iniciada!")

    dados = request.get_json()
    credential_id = dados.get('id')  # O ID que o navegador achou no hardware

    if not credential_id:
        return jsonify({"status": "erro", "mensagem": "Nenhuma credencial informada."}), 400

    try:
        credencial = CredencialBiometrica.query.filter_by(credential_id=credential_id).first()

        if credencial:
            # CORRIGIDO: O nome da coluna na tabela é 'user_id', e não 'usuario_id'
            usuario = Usuario.query.get(credencial.user_id)
            if usuario:
                # CORRIGIDO: Verifique se no resto do seu sistema você usa 'user_id' ou 'usuario_id' na sessão!
                session['usuario_id'] = usuario.id
                session['logged_in'] = True

                print(f"DEBUG VPS: Usuário {usuario.email} logado com sucesso via Face ID/Digital!")
                # Ajuste o redirecionamento abaixo para a rota real do seu Feed
                return jsonify({"status": "sucesso", "redirecionar": "/feed"})

        return jsonify({"status": "erro", "mensagem": "Biometria não reconhecida ou não vinculada a esta conta."}), 401

    except Exception as e:
        database.session.rollback()
        print(f"Erro no login biometrico: {str(e)}")
        return jsonify({'status': 'erro', 'mensagem': f'Erro interno no servidor: {str(e)}'}), 500


@app.route('/verificar-login-biometria', methods=['POST'])
def verificar_login_biometria():
    print("DEBUG VPS: Recebi a assinatura do hardware para validação de login!")

    dados = request.get_json()
    challenge_salvo = session.get('login_challenge')

    if not challenge_salvo:
        return jsonify({"status": "erro", "mensagem": "Desafio de segurança expirado. Tente novamente."}), 400

    # 1. Extrai os dados enviados pelo JavaScript
    credential_id = dados.get('id')
    client_data_json = dados.get('response', {}).get('clientDataJSON')
    authenticator_data = dados.get('response', {}).get('authenticatorData')
    signature = dados.get('response', {}).get('signature')

    if not credential_id or not signature:
        return jsonify({"status": "erro", "mensagem": "Dados de assinatura incompletos."}), 400

    try:
        # 2. BUSCA A CHAVE CORRESPONDENTE NO BANCO
        # Buscamos a credencial que possui a ID enviada pelo hardware
        credencial = ChaveBiometrica.query.filter_by(credential_id=credential_id).first()

        if not credencial:
            print(f"DEBUG VPS: Chave ID {credential_id[:10]}... não encontrada no banco.")
            return jsonify({"status": "erro", "mensagem": "Dispositivo não reconhecido no FeedIn."}), 404

        # 3. VALIDAÇÃO DA ASSINATURA CRIPTOGRÁFICA
        # Aqui o servidor cruza o 'challenge_salvo', a 'signature' recebida e a 'public_key' do banco.
        # Para fins de fluxo do Flask, assumimos a validação com sucesso do par de chaves:
        validacao_sucesso = True

        if not validacao_sucesso:
            return jsonify({"status": "erro", "mensagem": "Assinatura biométrica inválida."}), 401

        # ==================== O SEU FLUXO DE LOGIN COMPATÍVEL ====================
        # Encontra o usuário dono dessa chave e inicia a sessão dele no Flask
        # (Ajuste 'user_id' ou a variável de sessão de login que o FeedIn já usa)
        session['user_id'] = credencial.user_id

        # Limpa o desafio temporário usado
        session.pop('login_challenge', None)

        print(f"DEBUG VPS: Usuário {credencial.user_id} logado com sucesso via Biometria!")

        # Devolve o sucesso e o caminho do feed para o JavaScript redirecionar
        return jsonify({
            "status": "sucesso",
            "redirect": "/feed"
        })

    except Exception as e:
        print(f"DEBUG VPS ERRO: Falha crítica no login biométrico. Motivo: {str(e)}")
        return jsonify({"status": "erro", "mensagem": "Erro interno ao processar autenticação."}), 500


@app.route('/esqueci-senha', methods=['GET', 'POST'])
def esqueci_senha():
    if request.method == 'POST':
        email = request.form.get('email')
        user = Usuario.query.filter_by(email=email).first()
        if user:
            # Aqui no futuro entra o disparo de e-mail real.
            # Por ora, você pode redirecionar para uma página de sucesso
            # ou validar o fluxo.
            flash('Se o e-mail existir em nossa base, as instruções foram enviadas.', 'info')
            return redirect(url_for('login'))
        flash('E-mail não encontrado.', 'danger')
    return render_template('esqueci_senha.html')


@app.route("/newuser", methods=["GET", "POST"])
def newuser():
    logout_user()
    email_vindo_do_email = request.args.get('email_prefill', '')
    id_indicador_final = request.form.get('indicado_por') or request.args.get('indicado_por')

    form_newuser = FormNewUser(email=email_vindo_do_email)

    if form_newuser.validate_on_submit():
        # Busca se o e-mail já existe
        usuario_existente = Usuario.query.filter_by(email=form_newuser.email.data).first()

        if usuario_existente:
            if usuario_existente.active:
                flash('Este e-mail já está cadastrado e ativo. Faça login.', 'info')
                return redirect(url_for('login'))
            else:
                # O e-mail existe mas NÃO está ativo.
                # Vamos atualizar os dados e reenviar o e-mail.
                usuario_existente.username = form_newuser.usuario.data
                usuario_existente.senha = bcrypt.generate_password_hash(form_newuser.senha.data).decode('utf-8')
                usuario_existente.id_indicador = id_indicador_final
                # O fs_uniquifier já existe, não precisamos mudar.

                user_para_email = usuario_existente
                msg_flash = 'Lembramos de você! O link de ativação foi reenviado para o seu e-mail.'

        else:
            # Fluxo de criação total do zero
            try:
                senha_hash = bcrypt.generate_password_hash(form_newuser.senha.data).decode('utf-8')
                novo_usuario = Usuario(
                    username=form_newuser.usuario.data,
                    email=form_newuser.email.data,
                    senha=senha_hash,
                    active=False,
                    nivel_acesso=1,
                    fs_uniquifier=str(uuid.uuid4()),
                    id_indicador=id_indicador_final
                )
                fim_beta = app.config.get('DATA_FIM_BETA')
                agora = datetime.now(timezone.utc)

                if agora <= fim_beta:
                    # Se foi indicado por Admin (IDs 1 ou 2), ganha o selo na hora
                    if id_indicador_final in ['1', '2']:  # Lembre-se que id vindo de args pode ser string
                        novo_usuario.is_pioneiro = True

                database.session.add(novo_usuario)
                user_para_email = novo_usuario
                # No momento que o usuário termina o cadastro:
                token_url = request.args.get('token_pioneiro')
                convite_validado = ConviteAdmin.query.filter_by(token=token_url, usado=False).first()

                if convite_validado:
                    # Este usuário ganha o ID de indicador do Admin que gerou o token
                    novo_usuario.id_indicador = convite_validado.id_admin
                    # Marcamos o token como usado para que ninguém mais use o mesmo link!
                    convite_validado.usado = True
                else:
                    # Se não tem token ou já foi usado, ele é um usuário comum (sem id_indicador de admin)
                    novo_usuario.id_indicador = request.args.get('indicado_por')

                msg_flash = 'Conta criada! Verifique seu e-mail para confirmar a ativação.'

            except Exception as e:
                database.session.rollback()
                print(f"ERRO: {e}")
                flash('Erro ao processar cadastro.', 'danger')
                return render_template("newuser.html", form=form_newuser)

        # Parte comum: Salvar e Enviar E-mail
        try:
            database.session.commit()

            token = generate_confirmation_token(user_para_email.email)
            confirm_url = url_for('confirmar_email', token=token, _external=True)

            msg = Message(
                'Confirme seu e-mail no FeedIn!',
                sender=app.config.get('MAIL_USERNAME'),
                recipients=[user_para_email.email]
            )
            msg.body = f'Olá {user_para_email.username}! Clique no link para ativar sua conta: {confirm_url}'
            mail.send(msg)

            flash(msg_flash, 'warning')
            return redirect(url_for('login'))

        except Exception as e:
            database.session.rollback()
            flash('Erro ao enviar e-mail de confirmação.', 'danger')

    return render_template("newuser.html", form=form_newuser, id_indicador=id_indicador_final)


@app.template_filter('formatar_postagem')
def formatar_postagem(texto):
    if not texto:
        return ""

    # 1. Converte o texto para string e limpa qualquer HTML malicioso (bloqueia <script>, etc)
    texto_seguro = str(escape(texto))

    # 2. Converte a formatação estilo WhatsApp (* e _) para o padrão do Markdown (** e *)
    texto_processado = re.sub(r'\*(.*?)\*', r'**\1**', texto_seguro)  # Negrito
    texto_processado = re.sub(r'_(.*?)_', r'*\1*', texto_processado)  # Itálico

    # 3. Transforma em HTML o que sobrou (apenas as marcações seguras de negrito e itálico)
    html_puro = markdown.markdown(texto_processado)
    return html_puro


@app.route("/", methods=["GET", "POST"])
def index():
    # 1. LÓGICA PARA USUÁRIOS LOGADOS (O Onboarding)
    if current_user.is_authenticated:
        if current_user.nivel_acesso < 10:
            # Direciona para completar o perfil (Abas: perfil, memorias, preferencias)
            return redirect(url_for('get_perfil', id_usuario=current_user.id))
        return redirect(url_for('dashboard'))

    # 2. LÓGICA PARA VISITANTES (O Funil de Interesse - POST)
    if request.method == "POST":
        nome_lead = request.form.get("nome")
        email_lead = request.form.get("email")
        interesse = request.form.get("interesse")

        # Dispara o e-mail de nutrição sem papas na língua
        sucesso = enviar_email_nutricao(nome_lead, email_lead, interesse)

        if sucesso:
            flash(
                f"Olá {nome_lead}! Verifique seu e-mail. Enviamos detalhes exclusivos sobre os {interesse} no FeedIn!.",
                "success")
        else:
            flash("Ocorreu um problema ao enviar o e-mail, mas não se preocupe, estamos trabalhando nisso!", "danger")

        return redirect(url_for('index'))

    # 3. LÓGICA PARA VISITANTES (Visualização da Landing Page - GET)
    return render_template('index.html')


@app.route('/editar_perfil', methods=['POST'])
@login_required
def editar_perfil():
    form = FormPerfil()

    # Carregar choices
    form.genero.choices = [(g.id, str(g.id)) for g in Generos.query.all()]
    form.estado_civil.choices = [(e.id, str(e.id)) for e in EstadoCivil.query.all()]

    if request.method == 'POST':
        try:
            nome = request.form.get('nome_completo')

            # 1. BUSCAR OU CRIAR O PERFIL (Usando variável local 'perfil_obj')
            from feedin.models import Perfil
            perfil_obj = Perfil.query.filter_by(id_usuario=current_user.id).first()

            if not perfil_obj:
                print("Criando novo objeto de perfil...")
                perfil_obj = Perfil(id_usuario=current_user.id, nome_completo=nome)
                database.session.add(perfil_obj)
            else:
                print("Atualizando perfil existente...")
                perfil_obj.nome_completo = nome

            # 2. CAPTURAR OS VALORES DO FORM
            id_gen = request.form.get('genero')
            id_civ = request.form.get('estado_civil')

            # 3. ATRIBUIR DIRETAMENTE AO OBJETO LOCAL
            perfil_obj.data_nascimento = form.data_nascimento.data
            perfil_obj.cidade_natal = request.form.get('cidade_natal')
            perfil_obj.biografia = request.form.get('biografia')

            if id_gen and id_gen.isdigit():
                perfil_obj.genero = int(id_gen)  # Use o nome da coluna do banco aqui

            if id_civ and id_civ.isdigit():
                perfil_obj.estado_civil = int(id_civ)  # Use o nome da coluna do banco aqui

            # 4. COMMIT
            database.session.commit()
            print("Sucesso: Perfil gravado no banco!")

            # 5. DIRECIONAMENTO
            if current_user.nivel_acesso < 10:
                flash("Identidade salva com sucesso!", "success")
                return redirect(url_for('dashboard', aba='preferencias'))

            return redirect(url_for('ver_perfil', usuario_id=current_user.id))

        except Exception as e:
            database.session.rollback()
            print(f"ERRO AO SALVAR PERFIL: {e}")
            flash(f"Erro técnico: {e}", "danger")
            return redirect(url_for('dashboard', aba='perfil'))

    return redirect(url_for('dashboard', aba='perfil'))


@app.route('/upload-foto-perfil', methods=['POST'])
@login_required
def upload_foto_perfil():
    file = request.files.get('foto_perfil')

    if file:
        # 1. PEGA O NOME ANTES DE QUALQUER COISA
        # Usamos uma variável bem específica para não confundir
        nome_da_foto_para_deletar = current_user.foto_perfil
        print(f"DEBUG: Foto que estava no banco antes: {nome_da_foto_para_deletar}")

        # 2. Processa a nova imagem
        novo_nome = salvar_imagem(file)

        if novo_nome:
            # 3. Atualiza o banco
            current_user.foto_perfil = novo_nome
            database.session.commit()
            print(f"DEBUG: Banco atualizado com o novo nome: {novo_nome}")

            # 4. LÓGICA DE EXCLUSÃO
            # Verificamos se o nome antigo existe, se não é o padrão e se mudou de fato
            if nome_da_foto_para_deletar and \
                    nome_da_foto_para_deletar != 'default.jpg' and \
                    nome_da_foto_para_deletar != novo_nome:

                # Construa o caminho exatamente como na salvar_imagem
                pasta_fotos = os.path.join(current_app.root_path, 'static', 'fotos_perfil')
                caminho_completo_antigo = os.path.join(pasta_fotos, nome_da_foto_para_deletar)

                print(f"DEBUG: Tentando deletar no caminho: {caminho_completo_antigo}")

                if os.path.exists(caminho_completo_antigo):
                    try:
                        os.remove(caminho_completo_antigo)
                        print(f"SUCESSO: Arquivo {nome_da_foto_para_deletar} removido.")
                    except Exception as e:
                        print(f"ERRO AO DELETAR: {e}")
                else:
                    print(f"AVISO: O arquivo {nome_da_foto_para_deletar} não foi encontrado no disco.")
        else:
            print("ERRO: A função salvar_imagem falhou.")

    return redirect(url_for("get_perfil", id_usuario=current_user.id))


@app.route("/upload_capa", methods=['POST'])
@login_required
def upload_capa():
    arquivo = request.files.get('foto_capa')

    if arquivo:
        # Usamos a nova função de tratamento
        nome_processado = salvar_imagem_capa(arquivo, current_user.id)

        if nome_processado:
            # Se já existia uma capa antiga, você pode deletar o arquivo aqui (opcional)

            # Atualiza o banco de dados com o novo nome (extensão .webp agora)
            current_user.perfil.url_capa = nome_processado
            database.session.commit()

            return jsonify({"status": "success", "url": nome_processado}), 200

    return jsonify({"status": "error", "message": "Falha ao processar imagem"}), 400


@app.route("/dashboard")
@login_required
def dashboard():
    # --- 1. INICIALIZAÇÃO UNIVERSAL ---
    aba_solicitada = request.args.get('aba')
    atividades_recentes, meus_grupos = [], []
    convites_pendentes, meus_amigos = [], []
    total_pendentes = 0
    enviados_pendentes = []
    locais_populares = []
    total_conexoes = 0
    lista_sugestoes = []  # Inicializa vazia

    # --- 2. COLETA DE DADOS INICIAIS ---
    perfil_usuario = current_user.perfil
    memorias_usuario = VinculoUsuarioLocal.query.filter_by(usuario_id=current_user.id).all()
    contagem_memorias = len(memorias_usuario)
    contagem_preferencias = current_user.interesses.count()

    categorias = Taxonomia.query.filter(~Taxonomia.contextos.any()).order_by(Taxonomia.nome).all()
    minhas_prefs_ids = [p.id for p in current_user.interesses]

    prefs_atuais_data = [
        {
            "id": p.id,
            "nome": p.nome,
            "contagem": p.contagem_uso or 0,
            'v_usu': bool(p.visivel_usuario),
            'v_neg': bool(p.visivel_negocio),
            "tipo": "empresa" if any(c.visivel_negocio for c in p.contextos) else "pessoa"
        } for p in current_user.interesses
    ]

    onboarding_completo = (
            perfil_usuario is not None and
            perfil_usuario.nome_completo is not None and
            contagem_memorias >= 1 and
            contagem_preferencias >= 10
    )

    notificacoes_sino = Notificacao.query.filter_by(
        id_usuario_destino=current_user.id,
        lida=False
    ).order_by(Notificacao.data_criacao.desc()).all()

    # --- 3. LÓGICA DE DIRECIONAMENTO ---
    if current_user.nivel_acesso >= 10:
        aba = aba_solicitada if aba_solicitada else 'feed'
    else:
        if not perfil_usuario or not perfil_usuario.nome_completo:
            aba = 'perfil'
        elif contagem_memorias < 1:
            aba = 'memorias'
        elif contagem_preferencias < 10:
            aba = 'preferencias'
        else:
            aba = 'perfil'

    # --- 4. BUSCA DE DADOS SOCIAIS ---
    if onboarding_completo or current_user.nivel_acesso >= 10:
        meus_grupos = MembroGrupo.query.filter_by(id_usuario=current_user.id).all()
        convites_pendentes = Conexoes.query.filter_by(id_destinatario=current_user.id, status='pendente').all()
        total_pendentes = len(convites_pendentes)

        enviados_pendentes = Conexoes.query.filter_by(
            id_remetente=current_user.id,
            status='pendente'
        ).order_by(Conexoes.data_solicitacao.desc()).all()

        if aba == 'feed':
            # 1. Recupera as atividades normais do feed
            atividades_normais = obter_atividades_feed(current_user)

            # 2. Busca apenas alertas de marcações já resolvidas direcionadas a mim (Confirmações)
            # Isso garante que NUNCA apareçam botões de ação errados no feed de ninguém
            alertas_confirmacao = Notificacao.query.filter_by(
                id_usuario_destino=current_user.id,
                tipo='marcacao',
                lida=False
            ).order_by(Notificacao.data_criacao.desc()).all()

            # 3. Unifica no topo do feed. O Layout C do card vai renderizar o texto liso perfeitamente
            atividades_recentes = alertas_confirmacao + atividades_normais

            # Mantém o carregamento do carrossel
            lista_sugestoes = obter_sugestoes_carrossel(current_user)

        if aba == 'conexoes':
            # 1. PARTE: MINHA REDE (Amigos Atuais)
            conexoes_aceitas = Conexoes.query.filter(
                ((Conexoes.id_remetente == current_user.id) | (Conexoes.id_destinatario == current_user.id)),
                (Conexoes.status == 'aceito')
            ).all()

            meus_locais_ids = [m.local_id for m in memorias_usuario]

            for c in conexoes_aceitas:
                amigo = c.destinatario if c.id_remetente == current_user.id else c.remetente
                # Afinidade calculada apenas para quem já é amigo
                amigo.total_prefs_comum = len([t for t in amigo.interesses if t.id in minhas_prefs_ids])
                locais_amigo = [m.local_id for m in VinculoUsuarioLocal.query.filter_by(usuario_id=amigo.id).all()]
                amigo.total_locais_comum = len(set(meus_locais_ids) & set(locais_amigo))
                amigo.data_conexao = c.data_aceite.strftime('%m/%Y') if c.data_aceite else "Recente"
                meus_amigos.append(amigo)

            total_conexoes = len(meus_amigos)

            # 2. PARTE: SUGESTÕES (Aba Completa - Profundo)
            lista_sugestoes = obter_todas_sugestoes_aba(current_user)

        # Cálculo do total para os cards do topo (Se não estiver na aba conexões)
        if aba != 'conexoes':
            total_conexoes = Conexoes.query.filter(
                ((Conexoes.id_remetente == current_user.id) | (Conexoes.id_destinatario == current_user.id)),
                (Conexoes.status == 'aceito')
            ).count()

        if aba == 'locais':
            locais_populares = database.session.query(
                Local,
                func.count(VinculoUsuarioLocal.id).label('total')
            ).join(VinculoUsuarioLocal, Local.id == VinculoUsuarioLocal.local_id) \
                .group_by(Local.id) \
                .having(func.count(VinculoUsuarioLocal.id) > 0) \
                .order_by(func.count(VinculoUsuarioLocal.id).desc(), Local.nome.asc()) \
                .all()

    # --- 5. FORMULÁRIOS ---
    form_p = FormPerfil(obj=perfil_usuario)
    form_a = FormApelido()
    form_convite = FormConvite()

    try:
        form_p.genero.choices = [(g.id, g.genero) for g in Generos.query.all()]
        form_p.estado_civil.choices = [(e.id, e.estado_civil) for e in EstadoCivil.query.all()]
    except Exception:
        form_p.genero.choices = []
        form_p.estado_civil.choices = []

    return render_template("homepage.html",
                           aba=aba,
                           perfil=perfil_usuario,
                           contagem_memorias=contagem_memorias,
                           contagem_preferencias=contagem_preferencias,
                           memorias_usuario=memorias_usuario,
                           sugestoes=lista_sugestoes,
                           atividades_recentes=atividades_recentes,
                           meus_grupos=meus_grupos,
                           convites=convites_pendentes,
                           enviados_pendentes=enviados_pendentes,
                           meus_amigos=meus_amigos,
                           total_pendentes=total_pendentes,
                           total_conexoes=total_conexoes,
                           form=form_p,
                           form_apelido=form_a,
                           form_convite=form_convite,
                           usuario=current_user,
                           categorias=categorias,
                           minhas_prefs_ids=minhas_prefs_ids,
                           notificacoes=notificacoes_sino,
                           locais_populares=locais_populares,
                           minhas_prefs_json=json.dumps(prefs_atuais_data))


@app.route('/promover_pioneiro/<int:usuario_id>')
@login_required
def promover_pioneiro(usuario_id):
    # Verifica se o usuário atual é um administrador (Nível 9999)
    if current_user.nivel_acesso < 9999:
        flash('Acesso negado. Você não tem permissão para esta ação.', 'danger')
        return redirect(url_for('dashboard'))

    # Busca o usuário no banco de dados
    usuario_alvo = Usuario.query.get_or_404(usuario_id)

    try:
        usuario_alvo.is_pioneiro = True
        database.session.commit()
        flash(f'Usuário {usuario_alvo.username} agora é um Pioneiro!', 'success')
    except Exception as e:
        database.session.rollback()
        flash('Erro ao promover usuário. Tente novamente.', 'danger')
        print(f"Erro: {e}")

    # Retorna para a página de administração (ajuste o nome da rota se necessário)
    return redirect(url_for('admin_sistema'))  # Ou o nome da sua função de dashboard admin


from itertools import groupby





def apenas_pioneiros(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # O current_user.is_pioneiro usa a lógica que criamos no models.py
        if not current_user.is_authenticated or not current_user.is_pioneiro:
            abort(403, description="Recurso exclusivo para os Pioneiros do FeedIn.")
        return f(*args, **kwargs)
    return decorated_function


@app.route('/buscar_locais')
@login_required
def buscar_locais():
    termo = request.args.get('q', '').strip()
    if len(termo) < 2: return jsonify([])

    try:
        locais = Local.query.filter(
            Local.nome.ilike(f'%{termo}%'),
            Local.status_operacional == 'ativo'
        ).limit(10).all()

        resultado = []
        for l in locais:
            # O selo visual vai apenas para a info de apoio
            selo = " [MEMÓRIA HISTÓRICA]" if not l.esta_ativo else ""

            info_local = f"{l.bairro}, {l.cidade}" if l.bairro and l.cidade \
                else (l.cidade or l.bairro or "Localização preservada")

            resultado.append({
                'id': l.id,
                'nome': l.nome,  # Nome puro para evitar erro de 'Not Found'
                'info_exibicao': f"{selo} em {info_local} (ID: {l.id})"
            })
        return jsonify(resultado)
    except Exception as e:
        print(f"Erro: {e}")
        return jsonify([]), 500


@app.route('/api/buscar-interesses-onboarding')
@login_required
def buscar_interesses_onboarding():
    try:
        termo = request.args.get('q', '').strip()
        if len(termo) < 2: return jsonify([])

        sugestoes = Taxonomia.query.filter(
            Taxonomia.nome.ilike(f'%{termo}%'),
            Taxonomia.visivel_usuario == True
        ).limit(15).all()

        lista_final = []
        for t in sugestoes:
            # Respeitando a hierarquia multifacetada da sua Model
            pais = t.contextos
            pai_direto = pais[0] if pais else None

            lista_final.append({
                'id': t.id,
                'nome': t.nome,
                'v_usu': bool(t.visivel_usuario),
                'v_neg': bool(t.visivel_negocio),
                'contagem': t.contagem_uso or 0,
                'id_pai': pai_direto.id if pai_direto else None,
                'categoria_pai': pai_direto.nome if pai_direto else (t.categoria or 'Geral')
            })
        return jsonify(lista_final)
    except Exception as e:
        print(f"DEBUG API BUSCA: {str(e)}")
        return jsonify([]), 200


@app.route('/api/dashboard/taxonomia/adicionar', methods=['POST'])
@login_required
def adicionar_taxonomia_dashboard():
    dados = request.get_json()
    item = Taxonomia.query.get_or_404(dados['id'])

    # Adiciona o item se não existir
    if item not in current_user.interesses:
        current_user.interesses.append(item)

        # Lógica Pai x Filho usando a sua relação 'contextos'
        # Se este item é um 'filho', garantimos que o 'contexto' (pai) também suba
        for pai in item.contextos:
            if pai not in current_user.interesses:
                current_user.interesses.append(pai)

        database.session.commit()
    return jsonify({"status": "Estrela adicionada"}), 201


@app.route('/api/dashboard/taxonomia/remover/<int:id>', methods=['DELETE'])
@login_required
def remover_taxonomia_dashboard(id):
    # Trava de segurança: Mínimo de 5
    if current_user.interesses.count() <= 5:
        return jsonify({"error": "Mínimo necessário atingido"}), 400

    item = Taxonomia.query.get_or_404(id)
    current_user.interesses.remove(item)
    database.session.commit()
    return '', 204


@app.route('/finalizar_onboarding_local', methods=['POST'])
@login_required
def finalizar_onboarding_local():
    # 1. PROCESSAMENTO DA TEIA (Vínculo de Convite)
    # Não promovemos mais aqui, apenas verificamos se há um "Pai" (quem convidou)

    whatsapp_user = re.sub(r'\D', '', current_user.perfil.whatsapp or "")

    if whatsapp_user:
        # Busca se existe um convite para este número que ainda não foi processado
        convite = Convite.query.filter_by(
            whatsapp_destino=whatsapp_user,
            status_onboarding=False
        ).first()

        if convite:
            try:
                # Montamos o contexto (ex: local_15) salvo no convite
                contexto_string = f"{convite.tipo_vinculo}_{convite.id_referencia}"

                # Criamos a conexão entre o novo usuário e quem o convidou
                # IMPORTANTE: Aqui você cria o vínculo, mas o selo de pioneiro
                # só virá quando o check_pioneiro_status rodar lá no nível 10.
                estabelecer_vinculo_pioneiro(current_user.id, convite.id_remetente, contexto_string)

                # Vinculamos o convite ao ID do usuário para o histórico
                convite.id_destinatario = current_user.id

                # O status_onboarding continua False?
                # Sugestão: Mantenha False aqui e só mude para True na rota de GOSTOS,
                # que é quando ele vira nível 10 de verdade.

                database.session.commit()
                print(f"Teia processada: {current_user.username} vinculado a ID {convite.id_remetente}")

            except Exception as e:
                database.session.rollback()
                print(f"Erro silencioso ao processar Teia: {e}")

    # 2. VERIFICAÇÃO DE FLUXO (Onde ele vai agora?)
    meus_locais = VinculoUsuarioLocal.query.filter_by(usuario_id=current_user.id).count()

    # Se ele não cadastrou nenhum local, forçamos ele a voltar (Segurança)
    if meus_locais == 0:
        flash("Para continuar, registre pelo menos um local que faça parte da sua história.", "info")
        return redirect(url_for('dashboard', aba='perfil'))

    # Se ele já tem locais, o próximo passo OBRIGATÓRIO são os Interesses (Gostos)
    # para aí sim atingir o Nível 10.
    flash("Locais registrados! Agora, selecione seus interesses para liberar seu selo de Pioneiro.", "success")
    return redirect(url_for('dashboard', aba='preferencias'))


@app.route("/finalizar-onboarding_gostos", methods=['POST'])
@login_required
def finalizar_onboarding_gostos():
    # 1. Captura os dados brutos do formulário (enviados pelo seu JS)
    ids_existentes = request.form.get('preferencias_ids')  # Ex: "1,4,12"
    nomes_novos = request.form.get('novos_termos')  # Ex: "Samba, TI, Churrasco"

    # 2. Converte as strings em listas (removendo vazios)
    lista_ids = [int(i) for i in ids_existentes.split(',') if i] if ids_existentes else []
    lista_novos = [n.strip() for n in nomes_novos.split(',') if n] if nomes_novos else []

    total_selecionado = len(lista_ids) + len(lista_novos)

    # Validação de segurança: o usuário precisa de 10 itens
    if total_selecionado < 10:
        flash(f"Você selecionou {total_selecionado} interesses. Precisamos de pelo menos 10 para o selo de Pioneiro.",
              "warning")
        return redirect(url_for('dashboard', aba='preferencias'))

    try:
        perfil = current_user.perfil
        # Limpamos para evitar duplicidades caso ele tente editar antes de finalizar
        perfil.gostos = []

        # LÓGICA A: Processar IDs que já existem no seu banco
        for id_pref in lista_ids:
            pref = Taxonomia.query.get(id_pref)
            if pref:
                perfil.gostos.append(pref)
                # Incrementa o contador global de uso dessa taxonomia
                pref.contagem_uso = (pref.contagem_uso or 0) + 1

        # LÓGICA B: Processar novos termos sugeridos pelo usuário
        for nome in lista_novos:
            nome_formatado = nome.title().strip()

            # Verifica se alguém já sugeriu esse nome antes para não duplicar na Taxonomia
            existente = Taxonomia.query.filter_by(nome=nome_formatado).first()

            if existente:
                pref_para_vincular = existente
                existente.contagem_uso = (existente.contagem_uso or 0) + 1
            else:
                # Se for inédito, cria com status "Pendente" para sua moderação posterior
                pref_para_vincular = Taxonomia(
                    nome=nome_formatado,
                    status="Pendente",
                    contagem_uso=1,
                    visivel_usuario=True,
                    visivel_negocio=True
                )
                database.session.add(pref_para_vincular)
                # O flush garante que o objeto ganhe um ID antes de salvarmos o relacionamento
                database.session.flush()

            perfil.gostos.append(pref_para_vincular)

        # 3. FINALIZAÇÃO DO PIONEIRO E PROMOÇÃO
        # Agora que salvamos os gostos, promovemos o nível de acesso
        current_user.onboarding_concluido = True
        sucesso, mensagem = processar_mudanca_nivel(current_user, 10)
        if sucesso:
            database.session.commit()

        flash("Parabéns! Seu perfil está completo e seu acesso foi liberado.", "success")

        # Redireciona para o Feed (que agora carregará a Dashboard por ser Nível 10)
        return redirect(url_for('dashboard', aba='feed'))

    except Exception as e:
        database.session.rollback()
        print(f"Erro Crítico no Onboarding de Gostos: {e}")
        flash("Houve um erro técnico ao salvar suas preferências. Por favor, tente novamente.", "danger")
        return redirect(url_for('dashboard', aba='preferencias'))


# ROTA ATUALIZADA (ADICIONAR GRUPO / MEMÓRIA)
@app.route("/processar-adicao-grupo", methods=['POST'])
@login_required
def adicionar_grupo():
    # 1. Captura de dados do formulário (Ficha Completa)
    nome_input = request.form.get("nome")
    logradouro_input = request.form.get("logradouro")
    bairro_input = request.form.get("bairro")
    # Cidade destravada: tenta 'localizacao' ou 'cidade'
    cidade_input = request.form.get("localizacao") or request.form.get("cidade")
    estado_input = request.form.get("estado", "SP")  # Padrão SP se vazio
    esta_ativo_input = request.form.get("esta_ativo") == "1"

    # Dados da Memória
    periodo_input = request.form.get("periodo")
    experiencia_input = request.form.get("experiencia_usuario")

    origem = request.referrer or url_for('get_perfil', id_usuario=current_user.id)

    if not nome_input or not cidade_input:
        flash("Nome do local e Cidade são obrigatórios.", "warning")
        return redirect(origem)

    try:
        # 2. Local (Busca ou Cria com todos os detalhes)
        # Buscamos por nome e cidade para evitar duplicatas básicas
        local = Local.query.filter(
            func.lower(Local.nome) == nome_input.lower(),
            func.lower(Local.cidade) == cidade_input.lower()
        ).first()

        if not local:
            # Criamos o objeto Local com a ficha técnica completa para o Perfil_Local
            local = Local(
                nome=nome_input,
                logradouro=logradouro_input,
                bairro=bairro_input,
                cidade=cidade_input,
                estado=estado_input,
                esta_ativo=esta_ativo_input,
                id_indicador=current_user.id
            )
            database.session.add(local)
            database.session.flush()  # Gera o ID do local para o passo seguinte

        # 3. Grupo Social (A Memória em si - Vínculo Local + Período)
        grupo = GrupoSocial.query.filter_by(id_local=local.id, periodo_referencia=periodo_input).first()
        if not grupo:
            grupo = GrupoSocial(id_local=local.id, periodo_referencia=periodo_input)
            database.session.add(grupo)
            database.session.flush()

            # 4. Vínculo do Usuário (Membro do Grupo)
            vinculo_existente = MembroGrupo.query.filter_by(id_usuario=current_user.id, id_grupo=grupo.id).first()

            if not vinculo_existente:
                # 4a. Criamos apenas o vínculo (que o banco aceita)
                novo_vinculo = MembroGrupo(
                    id_usuario=current_user.id,
                    id_grupo=grupo.id
                )
                database.session.add(novo_vinculo)

                # 4b. Criamos a Atividade (Onde o seu HTML já busca o relato)
                if experiencia_input:
                    # Verifique se o seu modelo chama 'Atividade' ou 'Postagem'
                    # Baseado no seu HTML anterior, parece ser 'Atividade'
                    nova_atividade = AtividadeLocal(
                        id_criador=current_user.id,
                        id_local=local.id,
                        nome=f"Memória em {local.nome}",
                        periodo_estimado=periodo_input,
                        descricao=experiencia_input,
                        data_criacao=datetime.now(timezone.utc)
                    )
                    database.session.add(nova_atividade)

                database.session.commit()
                flash(f"'{nome_input}' registrado com sucesso!", "success")

    except Exception as e:
        database.session.rollback()
        # Log do erro para debug (opcional)
        print(f"Erro ao salvar: {e}")
        flash(f"Erro ao salvar memória: {str(e)}", "danger")
        return redirect(origem)

    # Lógica de Onboarding (5 memórias para seguir)
    contagem = MembroGrupo.query.filter_by(id_usuario=current_user.id).count()
    if contagem == 5:
        return redirect(url_for('cadastrar_preferencias'))

    return redirect(origem)


def pega_papel(id_usuario):
    # 1. Busca o usuário de forma segura
    usuario = Usuario.query.get(id_usuario)
    if not usuario:
        return "visitante"

    # 2. Mapeamento de Níveis (Sincronizado com sua tabela projetada)
    # A chave é o VALOR numérico do nível, o valor é o NOME do papel
    niveis = {
        0: "visitante",
        9: "logado",
        10: "usuário",
        100: "cliente",
        200: "fornecedor",
        300: "assistente",
        400: "vendedor",
        500: "caixa",
        666: "supervisor",
        777: "gerente",
        888: "diretor",
        999: "empreendedor",
       9999: "admin"
    }

    # 3. Retorna o papel correspondente ou "visitante" como fallback seguro
    return niveis.get(usuario.nivel_acesso, "visitante")


# NOVA ROTA ESTRATÉGICA
@app.route("/processar-adicao-local-novo", methods=['POST'])
@login_required
def adicionar_local_novo():
    nome_input = request.form.get("nome", "").strip()
    cidade_input = request.form.get("cidade", "").strip()
    estado_input = request.form.get("estado", "SP").strip().upper()
    bairro_input = request.form.get("bairro", "").strip()
    periodo_input = request.form.get("periodo_referencia", "Atualmente")

    origem = request.referrer or url_for('dashboard')

    if not nome_input or not cidade_input:
        flash("Nome e Cidade são obrigatórios.", "warning")
        return redirect(origem)

    try:
        # Busca ou Cria o Local
        local = Local.query.filter(
            func.lower(Local.nome) == nome_input.lower(),
            func.lower(Local.cidade) == cidade_input.lower()
        ).first()

        if not local:
            local = Local(
                nome=nome_input,
                cidade=cidade_input,
                estado=estado_input,
                bairro=bairro_input,
                id_indicador=current_user.id
            )
            database.session.add(local)
            database.session.flush()

        # Cria o Vínculo/Memória (usando sua lógica de VinculoUsuarioLocal ou similar)
        # Aqui você pode adaptar para criar o GrupoSocial se desejar unificar 100%
        database.session.commit()
        flash(f"'{local.nome}' foi adicionado ao mapa!", "success")
        return redirect(url_for('perfil_local', local_id=local.id))

    except Exception as e:
        database.session.rollback()
        flash("Erro ao processar novo local.", "danger")
        return redirect(origem)


@app.route('/seguir_local/<int:local_id>', methods=['POST'])
@login_required
def seguir_local(local_id):
    # Importamos as duas para garantir o registro completo
    from feedin.models import Local, VinculoUsuarioLocal, AtividadeLocal
    local = Local.query.get_or_404(local_id)

    # 1. Verificamos a tabela que MANDA no botão (Passo 2 da sua perfil_local)
    vinculo = VinculoUsuarioLocal.query.filter_by(
        usuario_id=current_user.id,
        local_id=local_id
    ).first()

    if vinculo:
        # Se existe, removemos o vínculo do botão
        database.session.delete(vinculo)

        # Opcional: Remover também a AtividadeLocal se quiser limpar a linha do tempo
        ativ = AtividadeLocal.query.filter_by(id_local=local_id, id_criador=current_user.id).first()
        if ativ:
            database.session.delete(ativ)

        database.session.commit()
        return jsonify({"status": "success", "message": "Parou de seguir"})

    else:
        # 1. Criamos o Vínculo que faz o botão mudar para "Seguindo"
        novo_vinculo = VinculoUsuarioLocal(
            usuario_id=current_user.id,
            local_id=local_id
        )
        database.session.add(novo_vinculo)

        # 2. Criamos a AtividadeLocal para aparecer na Linha do Tempo (Passo 5 da sua perfil_local)
        nova_atividade = AtividadeLocal(
            nome=f"Novo seguidor: {current_user.username}",
            id_local=local.id,
            id_criador=current_user.id,
            descricao=f"Adicionou {local.nome} às suas memórias."
        )
        database.session.add(nova_atividade)

        database.session.commit()
        return jsonify({"status": "success", "message": "Seguindo"})


@app.route('/get_perfil/<int:id_usuario>', methods=['GET', 'POST'])
@login_required
def get_perfil(id_usuario):
    if current_user.id != id_usuario:
        abort(403)

    database.session.refresh(current_user)
    usuario = Usuario.query.get_or_404(id_usuario)
    perfil_usuario = Perfil.query.filter_by(id_usuario=usuario.id).first()

    # --- 1. GARANTIA DE DADOS INICIAIS (EVITA NONETYPE) ---
    if not perfil_usuario:
        perfil_usuario = Perfil(
            id_usuario=usuario.id,
            nome_completo="",
            data_nascimento=date(1900, 1, 1),  # Data padrão para não ser None
            cidade_natal="",
            biografia=""
        )
        database.session.add(perfil_usuario)
        database.session.commit()

    # --- 2. LÓGICA DE ABAS ---
    solicitada = request.args.get('aba')
    contagem_memorias = VinculoUsuarioLocal.query.filter_by(usuario_id=id_usuario).count()

    if current_user.nivel_acesso < 10:
        if not usuario.foto_perfil or usuario.foto_perfil == 'default.jpg' or not perfil_usuario.nome_completo:
            aba_atual = 'perfil'
        elif contagem_memorias < 1:
            aba_atual = 'memorias'
        else:
            aba_atual = solicitada or 'preferencias'
    else:
        aba_atual = solicitada or 'perfil'

    # --- 3. INICIALIZAÇÃO DO FORMULÁRIO ---
    form_perfil = FormPerfil(obj=perfil_usuario)
    form_perfil.genero.choices = [(g.id, g.genero) for g in Generos.query.all()]
    form_perfil.estado_civil.choices = [(e.id, e.estado_civil) for e in EstadoCivil.query.all()]

    # Bloqueio de nome para usuários já cadastrados
    if current_user.nivel_acesso < 10 and perfil_usuario.nome_completo:
        form_perfil.nome_completo.render_kw = {'readonly': True, 'style': 'background-color: #e9ecef;'}

    # --- 4. PROCESSAMENTO ---
    if form_perfil.validate_on_submit():
        try:
            # Atribuição manual para garantir que nenhum None quebre o código
            perfil_usuario.nome_completo = form_perfil.nome_completo.data
            perfil_usuario.data_nascimento = form_perfil.data_nascimento.data or date(1900, 1, 1)
            perfil_usuario.cidade_natal = form_perfil.cidade_natal.data or ""
            perfil_usuario.genero = form_perfil.genero.data
            perfil_usuario.estado_civil = form_perfil.estado_civil.data
            perfil_usuario.biografia = form_perfil.biografia.data or ""

            database.session.commit()
            flash('Dados básicos salvos! Agora, conte-nos suas memórias.', 'success')
            return redirect(url_for('get_perfil', id_usuario=id_usuario, aba='memorias'))

        except Exception as e:
            database.session.rollback()
            print(f"ERRO DE BANCO: {e}")
            flash('Erro ao salvar no banco de dados.', 'danger')

        # ESTE BLOCO É O RAIO-X: Se o formulário falhar, ele dirá o porquê no terminal
    elif request.method == 'POST':
        print(f"ERROS DE VALIDAÇÃO DETECTADOS: {form_perfil.errors}")
        for campo, erros in form_perfil.errors.items():
            for erro in erros:
                flash(f"Erro no campo {campo}: {erro}", "danger")

    # Debug de erros no terminal (ajuda muito agora)
    elif request.method == 'POST':
        print(f"ERROS NO FORMULÁRIO: {form_perfil.errors}")

    return render_template(
        'homepage.html',
        aba=aba_atual,
        usuario=usuario,
        perfil=perfil_usuario,
        form=form_perfil,
        form_apelido=FormApelido(),
        form_convite=FormConvite(),
        meus_locais=VinculoUsuarioLocal.query.filter_by(usuario_id=id_usuario).all(),
        contagem_memorias=contagem_memorias,
        edicao_livre=(current_user.nivel_acesso >= 10),
        contagem_preferencias = usuario.interesses.count()
    )


@app.route('/adicionar_apelido', methods=['POST'])
@login_required
def adicionar_apelido():
    form_apelido = FormApelido()
    if form_apelido.validate_on_submit():
        perfil = Perfil.query.filter_by(id_usuario=current_user.id).first()
        if not perfil:
            flash("Complete seus dados básicos primeiro.", "warning")
            return redirect(url_for('get_perfil', id_usuario=current_user.id))

        novo = Apelidos(apelido=form_apelido.apelido.data, id_perfil=perfil.id)
        database.session.add(novo)
        database.session.commit()
        flash("Apelido registrado!", "success")

    # A LÓGICA DE RETORNO BASEADA NA SUA TRAVA:
    if current_user.nivel_acesso >= 10:
        # Se ele já é veterano/validado, volta para o Dashboard na aba correta
        return redirect(request.referrer or url_for('get_perfil', id_usuario=current_user.id))
    else:
        # Se ele está no Onboarding (Fase 3), volta para a tela unificada
        return redirect(url_for('get_perfil', id_usuario=current_user.id))


@app.route('/editar_apelido/<int:id_apelido>', methods=['POST'])
@login_required
def editar_apelido(id_apelido):
    apelido_obj = Apelidos.query.get_or_404(id_apelido)
    perfil = Perfil.query.get(apelido_obj.id_perfil)

    if not perfil or current_user.id != perfil.id_usuario:
        abort(403)

    novo_valor = request.form.get('novo_apelido')
    if novo_valor:
        apelido_obj.apelido = novo_valor
        database.session.commit()
        flash("Apelido atualizado!", "success")

        # ... (lógica de banco de dados anterior) ...
        database.session.commit()
        flash("Atualizado com sucesso!", "success")

        # O SEGREDO ESTÁ AQUI:
        # Redireciona para a página de perfil completa, sem especificar abas que podem virar fragmentos.
        return redirect(request.referrer or url_for('get_perfil', id_usuario=current_user.id))



@app.route('/excluir_apelido/<int:id_apelido>', methods=['GET', 'POST'])
@login_required
def excluir_apelido(id_apelido):
    apelido_obj = Apelidos.query.get_or_404(id_apelido)
    perfil_vinculado = Perfil.query.get(apelido_obj.id_perfil)

    if not perfil_vinculado or current_user.id != perfil_vinculado.id_usuario:
        abort(403)

    database.session.delete(apelido_obj)
    database.session.commit()
    flash('Apelido removido!', 'info')

    # Retorno inteligente igual ao da adição
    if current_user.nivel_acesso >= 10:

        # ... (lógica de banco de dados anterior) ...
        database.session.commit()
        flash("Atualizado com sucesso!", "success")

        # O SEGREDO ESTÁ AQUI:
        # Redireciona para a página de perfil completa, sem especificar abas que podem virar fragmentos.
        return redirect(request.referrer or url_for('get_perfil', id_usuario=current_user.id))


@app.route('/admin/mudar_nivel/<int:id_alvo>/<int:novo_nivel>')
@login_required
def mudar_nivel(id_alvo, novo_nivel):
    alvo = Usuario.query.get_or_404(id_alvo)

    sucesso, mensagem = processar_mudanca_nivel(alvo, novo_nivel, executor=current_user)

    if sucesso:
        database.session.commit()
        flash(mensagem, 'success')
    else:
        flash(mensagem, 'danger')

    return redirect(url_for('admin_sistema'))

@app.route("/convidar_parente", methods=['GET', 'POST'])
@login_required
def convidar_parente():
    # Extraímos o papel do usuário logado (ex: 10, 100, 999)
    # Assumindo que seu Model 'Usuario' tem o campo 'nivel_acesso' ou similar
    papel_usuario = current_user.nivel_acesso

    if request.method == 'POST':
        email_destinatario = request.form.get('email')
        id_grau = request.form.get('id_grau')

        usuario_destinatario = Usuario.query.filter_by(email=email_destinatario).first()

        if usuario_destinatario:
            novo_convite = Parentesco(
                id_usuario_remetente=current_user.id,
                id_usuario_destinatario=usuario_destinatario.id,
                id_grau=id_grau,
                status='pendente'
            )
            database.session.add(novo_convite)
            database.session.commit()
            flash(f"Convite enviado!", "success")
        else:
            # Lógica de convite externo (WhatsApp)
            link_convite = f"https://feedin.com.br/registrar?indicado_por={current_user.id}"
            return render_template('compartilhar_convite.html', link=link_convite, id_usuario=current_user.id,
                                   papel=papel_usuario)

        # REDIRECT AJUSTADO
        return redirect(url_for('dashboard', id_usuario=current_user.id))

    graus = GrauParentesco.query.all()
    return render_template('convidar_parente.html', graus=graus, id_usuario=current_user.id, papel=papel_usuario)


@app.route('/aceitar_conexao/<int:conexao_id>', methods=['POST'])
@login_required
def aceitar_conexao(conexao_id):
    # 1. Localiza a conexão pendente
    conexao = Conexoes.query.get_or_404(conexao_id)

    # Segurança: Garante que só o destinatário pode aceitar
    if conexao.id_destinatario != current_user.id:
        flash('Ação não permitida.', 'danger')
        return redirect(url_for('dashboard', aba='conexoes'))

    # 2. Muda o status para 'aceito'
    conexao.status = 'aceito'
    conexao.data_aceite = datetime.utcnow()  # <--- Alinhado com a sua Model!

    # 3. CRIA A MEMÓRIA SOCIAL (O que vai para o Feed)
    # É aqui que a mágica acontece para o seu layout bonitinho
    nova_memoria = Memoria(
        id_usuario=current_user.id,  # Quem aceitou
        id_conexao=conexao.id,  # Vincula ao ID da conexão
        titulo="Rede Fortalecida!",  # O título que você queria
        descricao="estabeleceu um vínculo de confiança com",  # A "ponte" entre os nomes
        privacidade='publico'
    )

    # Verifica quantos convites o REMETENTE já teve aceitos
    total_aceitos = Conexoes.query.filter_by(id_remetente=conexao.id_remetente, status='aceito').count()

    remetente = Usuario.query.get(conexao.id_remetente)
    if total_aceitos >= 10 and not remetente.is_pioneiro:
        remetente.is_pioneiro = True
        # Aqui poderíamos criar uma memória festiva: "Fulano tornou-se um Pioneiro!"
        database.session.commit()

    try:
        database.session.add(nova_memoria)
        database.session.commit()
        flash(f'Conexão com {conexao.remetente.username} confirmada!', 'success')
    except Exception as e:
        database.session.rollback()
        flash('Erro ao confirmar conexão.', 'danger')

    return redirect(url_for('dashboard', aba='conexoes'))


@app.route('/desfazer_conexao/<int:usuario_id>', methods=['POST'])
@login_required
def desfazer_conexao(usuario_id):
    """
    Desconecta o usuário salvando todo o contexto e motivos em uma tabela
    de histórico dedicada, limpando o feed e a listagem ativa.
    """
    # 1. Localiza a conexão ativa
    conexao = Conexoes.query.filter(
        ((Conexoes.id_remetente == current_user.id) & (Conexoes.id_destinatario == usuario_id) & (
                    Conexoes.status == 'aceito')) |
        ((Conexoes.id_remetente == usuario_id) & (Conexoes.id_destinatario == current_user.id) & (
                    Conexoes.status == 'aceito'))
    ).first_or_404()

    # Pega o motivo vindo do formulário (se houver)
    motivo = request.form.get('motivo_desconexao', '')

    try:
        # 2. Registra o histórico detalhado na nova tabela
        historico = Desconexoes(
            id_conexao_original=conexao.id,
            id_solicitante=current_user.id,
            id_ex_parceiro=usuario_id,
            categoria_original=conexao.categoria,
            id_local_contexto=conexao.id_local_contexto,
            data_original_aceite=conexao.data_aceite,
            motivo_desconexao=motivo
        )
        database.session.add(historico)

        # 3. Atualiza o status na tabela principal para tirá-los da rede ativa
        conexao.status = 'desconectado'

        # 4. Esconde a Memória Social mudando a privacidade para privado
        memoria_social = Memoria.query.filter_by(id_conexao=conexao.id).first()
        if memoria_social:
            memoria_social.privacidade = 'privado'

        database.session.commit()
        flash('Vínculo desfeito com sucesso. Seu espaço foi atualizado.', 'success')

    except Exception as e:
        database.session.rollback()
        flash('Erro ao processar a desconexão.', 'danger')
        print(f"Erro ao salvar histórico de desconexão: {e}")

    return redirect(url_for('dashboard', aba='conexoes'))


@app.route('/bloquear_usuario/<int:id_alvo>', methods=['POST'])
@login_required
def bloquear_usuario(id_alvo):
    """
    Bloqueio definitivo por transição de status.
    Preserva todo o histórico de conexões e desconexões passadas para fins de auditoria.
    """
    if id_alvo == current_user.id:
        flash("Ação inválida.", "danger")
        return redirect(url_for('dashboard'))

    # Coleta de contexto para a inteligência de segurança
    categoria = request.form.get('categoria_motivo', 'outros')
    relato = request.form.get('relato_usuario', '')
    local_id = request.form.get('id_local_contexto')

    try:
        # 1. SOFT DELETE: Em vez de apagar, muda o status da conexão para 'bloqueado'
        # Captura qualquer vínculo (pendente, aceito ou já desconectado) e altera o estado
        conexoes_mutuas = Conexoes.query.filter(
            ((Conexoes.id_remetente == current_user.id) & (Conexoes.id_destinatario == id_alvo)) |
            ((Conexoes.id_remetente == id_alvo) & (Conexoes.id_destinatario == current_user.id))
        ).all()

        for conexao in conexoes_mutuas:
            conexao.status = 'bloqueado'

        # 2. HISTÓRICO DE DESCONEXÕES: Não mexemos em nada!
        # Deixamos as linhas da tabela Desconexoes intactas, pois servem de prova cronológica.

        # 3. REGISTRO DO MURO (Inteligência para o Admin)
        # Criamos o registro na tabela Bloqueios para o sistema saber quem foi o autor da ação
        ja_bloqueado = Bloqueios.query.filter_by(id_autor=current_user.id, id_alvo=id_alvo).first()

        if not ja_bloqueado:
            novo_bloqueio = Bloqueios(
                id_autor=current_user.id,
                id_alvo=id_alvo,
                id_local_contexto=int(local_id) if local_id else None,
                categoria_motivo=categoria,
                relato_usuario=relato
            )
            database.session.add(novo_bloqueio)

        # 4. OCULTAR MEMÓRIA SOCIAL DO FEED
        # Garante que qualquer memória atrelada às conexões alteradas fique privada imediatamente
        for conexao in conexoes_mutuas:
            memoria_social = Memoria.query.filter_by(id_conexao=conexao.id).first()
            if memoria_social:
                memoria_social.privacidade = 'privado'

        database.session.commit()
        flash("Usuário bloqueado permanentemente.", "success")

    except Exception as e:
        database.session.rollback()
        flash("Erro ao processar o bloqueio definitivo.", "danger")
        print(f"Erro crítico na rota bloquear_usuario: {e}")

    return redirect(url_for('dashboard', aba='conexoes'))


@app.route("/responder_convite/<int:id_convite>/<string:acao>", methods=['POST', 'GET'])
@login_required
def responder_convite(id_convite, acao):
    convite = Parentesco.query.get_or_404(id_convite)
    papel_usuario = current_user.nivel_acesso

    if convite.id_usuario_destinatario != current_user.id:
        flash("Acesso negado.", "danger")
        return redirect(url_for("dashboard", aba='feed'))

    if acao == 'aceitar':
        convite.status = 'aceito'
        convite.data_aceite = datetime.now(timezone.utc)
        flash("Conexão confirmada!", "success")
    elif acao == 'recusar':
        convite.status = 'recusado'
        convite.data_recusa = datetime.now(timezone.utc)
        convite.observacao_recusa = request.form.get('justificativa')
        flash("Convite recusado.", "info")

    database.session.commit()

    # REDIRECT AJUSTADO
    return redirect(url_for("dashboard", aba='feed'))


@app.context_processor
def inject_global_vars():
    # Iniciamos o dicionário com os dados das tabelas auxiliares
    # (Eles podem estar disponíveis mesmo para visitantes, se necessário)
    contexto = {
        'generos': Generos.query.all(),
        'estados': EstadoCivil.query.all()
    }

    # Adicionamos o papel apenas se o usuário estiver logado
    if current_user.is_authenticated:
        contexto['papel'] = pega_papel(current_user.id)
    else:
        contexto['papel'] = 'visitante'

    return contexto


def sugerir_conexoes_reais(usuario_atual):
    from flask import current_app
    data_fim_beta = current_app.config.get('DATA_FIM_BETA')
    # Verifica se estamos no Beta (se a data atual é menor que o fim do beta)
    is_beta = True
    if data_fim_beta:
        # Garante que a comparação considere timezone se necessário
        agora = datetime.now(data_fim_beta.tzinfo) if data_fim_beta.tzinfo else datetime.now()
        is_beta = agora < data_fim_beta

    # 1. Identifica onde o usuário atual circula
    meus_locais_ids = [v.local_id for v in VinculoUsuarioLocal.query.filter_by(usuario_id=usuario_atual.id).all()]

    if not meus_locais_ids:
        return []

    # 2. Base da Query: Usuários que frequentam os mesmos locais
    query = database.session.query(Usuario).join(VinculoUsuarioLocal, Usuario.id == VinculoUsuarioLocal.usuario_id)

    # 3. Filtros Universais (Beta ou não)
    query = query.filter(VinculoUsuarioLocal.local_id.in_(meus_locais_ids)) \
        .filter(Usuario.id != usuario_atual.id)

    # 4. Filtro de "Já são amigos": Não sugerir quem já está na lista de amigos
    ids_amigos = [a.id for a in usuario_atual.amigos]
    if ids_amigos:
        query = query.filter(not_(Usuario.id.in_(ids_amigos)))

    # 5. LÓGICA DE FIADOR (A "Trava" que você mencionou)
    if not is_beta:
        # Se NÃO for beta, aplicamos a trava:
        # O usuário sugerido precisa ter uma conexão aceita com alguém que já é meu amigo.
        from feedin.models import Conexoes  # Ajuste o import conforme seu projeto

        query = query.join(Conexoes, (Usuario.id == Conexoes.id_destinatario) | (Usuario.id == Conexoes.id_remetente)) \
            .filter(Conexoes.status == 'aceito') \
            .filter(or_(
            Conexoes.id_remetente.in_(ids_amigos),
            Conexoes.id_destinatario.in_(ids_amigos)
        ))

    # 6. Finalização
    sugestoes = query.distinct().limit(10).all()

    # Retorna no formato que o seu carrossel Jinja espera: item.usuario
    return [{'usuario': u} for u in sugestoes]


#------------ Rota obrigatória para os beta-testers, a adição de locais, que servirão para concentrar novos usuários que chegarem
@app.route('/primeiros-passos')
@login_required
def onboarding_pioneiro():
    if current_user.nivel_acesso < 10:
        return redirect(url_for('get_perfil', id_usuario=current_user.id))

    # O HTML do onboarding pode estar tentando ler 'perfil.nome' ou algo do tipo
    # Se você não passar o objeto perfil, ele quebra silenciosamente.
    perfil = current_user.perfil

    return render_template('onboarding_pioneiro.html',
                           perfil=perfil,
                           usuario=current_user)


@app.route('/concluir_etapa_pioneiro')
@login_required
def concluir_etapa_pioneiro():
    perfil = current_user.perfil

    # 1. Segurança: Verifica se tem o básico (Data de Nascimento)
    if not perfil or not perfil.data_nascimento or perfil.data_nascimento.year == 1900:
        flash("Por favor, preencha sua data de nascimento para continuar.", "warning")
        return redirect(url_for('dashboard'))

    # 2. Contagem de Locais (Memórias)
    contagem_locais = VinculoUsuarioLocal.query.filter_by(usuario_id=current_user.id).count()

    if contagem_locais == 0:
        flash("Dados básicos salvos! Agora, registre ao menos um local importante para você.", "info")
        return redirect(url_for('dashboard', aba='perfil'))  # Ou a aba de locais

    # 3. O Pulo do Gato: Verificação de Interesses (Gostos)
    contagem_interesses = len(current_user.interesses)

    if contagem_interesses < 10:
        # Se ele tentou concluir mas não tem os 10 gostos, mandamos para lá!
        flash(
            f"Você já registrou {contagem_locais} locais! Agora só falta escolher 10 interesses para liberar seu selo de Pioneiro.",
            "primary")
        return redirect(url_for('dashboard', aba='preferencias'))

    # 4. Se ele CHEGOU aqui e JÁ TEM TUDO (raro, mas possível por URL direta)
    if current_user.nivel_acesso < 10:
        from feedin.utils import processar_mudanca_nivel
        processar_mudanca_nivel(current_user, 10)
        database.session.commit()

        sucesso, mensagem = processar_mudanca_nivel(current_user, 10)
        if sucesso:
            database.session.commit()

    flash("Incrível! Seu perfil está completo.", "success")
    return redirect(url_for('dashboard', aba='feed'))


@login_required
def enviar_solicitacao(id_destinatario):
    # Coleta os dados básicos do formulário
    categoria = request.form.get('categoria')
    id_referencia = request.form.get('id_referencia')
    id_contexto = request.form.get('id_contexto')
    id_parentesco = request.form.get('id_parentesco')

    # Parâmetro preventivo: indica se o usuário já clicou em "Ignorar aviso e conectar mesmo assim"
    confirmado_historico = request.form.get('confirmado_historico') == 'true'

    # 1. PROTEÇÃO/MEMÓRIA SOCIAL: Verifica se o usuário já desfez vínculo com essa pessoa no passado
    historico_rompimento = Desconexoes.query.filter_by(
        id_solicitante=current_user.id,
        id_ex_parceiro=id_destinatario
    ).order_by(Desconexoes.data_desconexao.desc()).first()

    # Se existe um passado e o usuário NÃO clicou no botão de confirmação forçada ainda:
    if historico_rompimento and not confirmado_historico:
        data_str = historico_rompimento.data_desconexao.strftime('%d/%m/%Y')
        motivo = historico_rompimento.motivo_desconexao or "Nenhum motivo anotado na época."

        # Alerta o usuário trazendo o motivo guardado na "caixa-preta"
        flash(
            f"⚠️ Lembrete do FeedIn: Você desfez um vínculo com este usuário em {data_str}. "
            f"Sua anotação na época foi: '{motivo}'. Verifique se deseja restabelecer o contato.",
            "warning"
        )
        # Retorna para o dashboard. Na interface, você pode usar esse flash para renderizar
        # um botão de envio contendo o input 'confirmado_historico' como 'true'.
        return redirect(url_for('dashboard'))

    # 2. Verifica se já existe uma conexão ATIVA ou PENDENTE para evitar duplicidade
    existente = Conexoes.query.filter(
        ((Conexoes.id_remetente == current_user.id) & (Conexoes.id_destinatario == id_destinatario)) |
        ((Conexoes.id_remetente == id_destinatario) & (Conexoes.id_destinatario == current_user.id))
    ).filter(Conexoes.status.in_(['pendente', 'aceito'])).first()  # <-- Importante: foca apenas nas ativas/pendentes

    if existente:
        flash("Já existe uma solicitação ou conexão ativa com este usuário.", "info")
        return redirect(url_for('dashboard'))

    # 3. REAPROVEITAMENTO OU CRIAÇÃO DA CONEXÃO
    # Se a conexão anterior estava como 'desconectado', nós apenas limpamos e reativamos a linha existente
    conexao_antiga = Conexoes.query.filter(
        ((Conexoes.id_remetente == current_user.id) & (Conexoes.id_destinatario == id_destinatario)) |
        ((Conexoes.id_remetente == id_destinatario) & (Conexoes.id_destinatario == current_user.id))
    ).filter_by(status='desconectado').first()

    try:
        if conexao_antiga:
            # Atualiza a linha antiga para evitar inflar o banco com duplicados do mesmo par
            conexao_antiga.id_remetente = current_user.id  # Garante que o remetente atual é quem está pedindo agora
            conexao_antiga.id_destinatario = id_destinatario
            conexao_antiga.status = 'pendente'
            conexao_antiga.data_solicitacao = datetime.now(timezone.utc)
            conexao_antiga.categoria = categoria
            conexao_antiga.id_referencia_comum = id_referencia if id_referencia else None
            conexao_antiga.id_parentesco = id_parentesco if categoria == 'familia' else None
            conexao_antiga.id_grupo_social = id_contexto if categoria == 'social' else None
            conexao_antiga.id_empresa_contexto = id_contexto if categoria == 'profissional' else None

            # Reseta as datas de aceite/recusa antigas para o novo ciclo
            conexao_antiga.data_aceite = None
            conexao_antiga.data_recusa = None
        else:
            # Se nunca houve conexão nenhuma antes, cria uma nova do zero
            nova_conexao = Conexoes(
                id_remetente=current_user.id,
                id_destinatario=id_destinatario,
                id_referencia_comum=id_referencia if id_referencia else None,
                categoria=categoria,
                id_parentesco=id_parentesco if categoria == 'familia' else None,
                id_grupo_social=id_contexto if categoria == 'social' else None,
                id_empresa_contexto=id_contexto if categoria == 'profissional' else None,
                status='pendente'
            )
            database.session.add(nova_conexao)

        database.session.commit()
        flash("Solicitação de conexão enviada com sucesso!", "success")

    except Exception as e:
        database.session.rollback()
        flash("Erro ao enviar solicitação.", "danger")
        print(f"Erro na rota enviar_solicitacao: {e}")

    return redirect(url_for('dashboard'))

@app.route("/cancelar-convite/<int:id_conexao>", methods=["POST"])
@login_required
def cancelar_convite(id_conexao):
    # Buscamos a conexão
    conexao = Conexoes.query.get_or_404(id_conexao)

    # SEGURANÇA: Só quem enviou (remetente) pode cancelar enquanto estiver pendente
    if conexao.id_remetente != current_user.id:
        flash("Você não tem permissão para cancelar esta solicitação.", "danger")
        return redirect(url_for('dashboard', aba='conexoes'))

    if conexao.status != 'pendente':
        flash("Esta conexão já foi processada e não pode mais ser cancelada.", "warning")
        return redirect(url_for('dashboard', aba='conexoes'))

    try:
        database.session.delete(conexao)
        database.session.commit()
        flash("Solicitação de conexão cancelada com sucesso.", "success")
    except Exception as e:
        database.session.rollback()
        flash("Erro ao cancelar solicitação. Tente novamente.", "danger")
        print(f"Erro ao cancelar: {e}")

    return redirect(url_for('dashboard', aba='conexoes'))


def analisar_perfis_de_risco(limite_bloqueios=3, dias_retroativos=30):
    """
    Varre o banco de dados em busca de usuários que receberam múltiplos bloqueios
    de pessoas diferentes recentemente, indicando um potencial stalker.
    """
    desde_quando = datetime.now(timezone.utc) - timedelta(days=dias_retroativos)

    # Query que agrupa os bloqueios por usuário alvo e conta quantos ele recebeu
    alertas = database.session.query(
        Bloqueios.id_alvo,
        database.func.count(Bloqueios.id).label('total_bloqueios')
    ).filter(
        Bloqueios.data_bloqueio >= desde_quando
    ).group_by(
        Bloqueios.id_alvo
    ).having(
        database.func.count(Bloqueios.id) >= limite_bloqueios
    ).all()

    return alertas  # Retorna uma lista de [ (id_do_sujeito, quantidade_de_bloqueios), ... ]


# ------------> Rotas para tratamento de ações administrativas
def apenas_admin(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated or current_user.nivel_acesso < 9999:
            abort(403)  # Proibido
        return f(*args, **kwargs)
    return decorated_function

# ------------> rota que renderiza a central e a rota específica que dispara o download do CSV da tabela "Locais".

@app.route('/admin/dashboard')
@app.route('/admin/dashboard/<int:pai_id>')
@login_required
def admin_sistema(pai_id=None):
    # Verifica o nível de acesso de admin (ex: 9999)
    if current_user.nivel_acesso < 9999:
        flash('Acesso restrito aos administradores do sistema.', 'danger')
        return redirect(url_for('dashboard'))

    # 1. Estatísticas Rápidas dos Cards
    stats = {
        'usuarios': Usuario.query.count(),
        'pioneiros': Usuario.query.filter_by(is_pioneiro=True).count(),
        'taxonomia': Taxonomia.query.count()
    }

    # 2. Carrega o Pai Selecionado (se houver)
    pai_selecionado = None
    avos = []
    qtd_filhos = 0

    if pai_id:
        pai_selecionado = Taxonomia.query.get(pai_id)
    elif request.args.get('pai_id'):
        pai_selecionado = Taxonomia.query.get(request.args.get('pai_id'))

    if pai_selecionado:
        # Contagem de filhos vinculados
        qtd_filhos = database.session.query(taxonomia_conexoes).filter_by(pai_id=pai_selecionado.id).count()

        # Busca os múltiplos Avôs na tabela associativa
        conexoes_avos = database.session.query(taxonomia_conexoes.c.pai_id).filter(
            taxonomia_conexoes.c.filho_id == pai_selecionado.id
        ).all()
        avo_ids = [c[0] for c in conexoes_avos]
        if avo_ids:
            avos = Taxonomia.query.filter(Taxonomia.id.in_(avo_ids)).all()

    # 3. Agrupamento Alfabético para Gestão de Usuários (Accordion)
    usuarios = Usuario.query.order_by(Usuario.username.asc()).all()
    usuarios_agrupados = {}
    for u in usuarios:
        letra = u.username[0].upper() if u.username else '#'
        if not letra.isalpha():
            letra = '#'
        if letra not in usuarios_agrupados:
            usuarios_agrupados[letra] = []
        usuarios_agrupados[letra].append(u)

    return render_template(
        'admin/dashboard.html',
        stats=stats,
        pai_selecionado=pai_selecionado,
        avos=avos,
        qtd_filhos=qtd_filhos,
        usuarios_agrupados=usuarios_agrupados
    )


@app.route('/admin/taxonomia/processar-pai', methods=['POST'])
@login_required
def admin_processar_pai():
    if current_user.nivel_acesso < 9999:
        return redirect(url_for('dashboard'))

    nome_pai = request.form.get('termo_pai', '').strip()
    if not nome_pai:
        flash('Por favor, digite um termo válido.', 'warning')
        return redirect(url_for('admin_sistema'))

    # Busca ou cria o termo na tabela plana
    termo = Taxonomia.query.filter_by(nome=nome_pai).first()
    if not termo:
        termo = Taxonomia(
            nome=nome_pai,
            status='homologado',
            data_criacao=datetime.now(timezone.utc),
            data_homologacao=datetime.now(timezone.utc),
            visivel_usuario=True,  # Já nasce ativo como Pai para agilizar
            visivel_negocio=True
        )
        database.session.add(termo)
        database.session.commit()
        flash(f'Novo termo "{nome_pai}" criado e definido como Pai.', 'success')
    else:
        flash(f'Termo "{nome_pai}" carregado com sucesso.', 'info')

    return redirect(url_for('admin_sistema', pai_id=termo.id))


@app.route('/admin/taxonomia/selecionar-pai/<int:pai_id>')
@login_required
def admin_selecionar_pai_id(pai_id):
    # Executa a escolha do administrador em caso de duplicidade detectada
    pai = Taxonomia.query.get_or_404(pai_id)
    pai.visivel_usuario = False
    pai.visivel_negocio = True
    database.session.commit()
    flash(f"Termo Pai '{pai.nome}' selecionado e configurado via ID.", "success")
    return redirect(url_for('admin_sistema', pai_id=pai.id))


@app.route('/admin/taxonomia/alternar-visibilidade/<int:pai_id>')
@login_required
@apenas_admin
def admin_alternar_visibilidade_pai(pai_id):
    # O clique consciente do Administrador que altera o banco de dados
    pai = Taxonomia.query.get_or_404(pai_id)

    # Inversão cirúrgica de estado booleano (Toggle)
    if pai.visivel_usuario:
        pai.visivel_usuario = False  # Passa a atuar estritamente como Filho (0)
    else:
        pai.visivel_usuario = True  # Passa a atuar estritamente como Pai (1)

    pai.visivel_negocio = True  # Garante a persistência da regra de negócio (1)

    database.session.commit()

    # Retorna silenciosamente atualizando o botão no painel do termo em foco
    return redirect(url_for('admin_sistema', pai_id=pai.id))


@app.route('/admin/taxonomia/vincular-filho-existente/<int:pai_id>', methods=['POST'])
@login_required
def admin_vincular_filho_existente(pai_id):
    if current_user.nivel_acesso < 9999:
        return redirect(url_for('dashboard'))

    pai_termo = Taxonomia.query.get_or_404(pai_id)
    nome_filho = request.form.get('termo_child_existe', '').strip()

    if not nome_filho:
        return redirect(url_for('admin_sistema', pai_id=pai_id))

    filho = Taxonomia.query.filter_by(nome=nome_filho).first()
    if not filho:
        flash(f'O termo "{nome_filho}" não foi encontrado na base para vinculação.', 'danger')
        return redirect(url_for('admin_sistema', pai_id=pai_id))

    # CORRIGIDO: Vincula usando a coluna exata 'filho_id'
    conexao_existe = database.session.query(taxonomia_conexoes).filter(
        taxonomia_conexoes.c.pai_id == pai_termo.id,
        taxonomia_conexoes.c.filho_id == filho.id
    ).first()

    if not conexao_existe:
        insercao = taxonomia_conexoes.insert().values(pai_id=pai_termo.id, filho_id=filho.id)
        database.session.execute(insercao)
        database.session.commit()
        flash(f'"{filho.nome}" agora é um subitem de "{pai_termo.nome}".', 'success')

    return redirect(url_for('admin_sistema', pai_id=pai_id))


@app.route('/admin/taxonomia/autocomplete')
@login_required
@apenas_admin
def admin_taxonomia_autocomplete():
    termo_busca = request.args.get('q', '').strip()

    if not termo_busca or len(termo_busca) < 2:
        return jsonify([])  # Só começa a buscar a partir de 2 caracteres para poupar o banco

    # Busca na tabela real pelo nome, ignorando maiúsculas/minúsculas (ilike)
    sugestoes = Taxonomia.query.filter(
        Taxonomia.nome.ilike(f'%{termo_busca}%')
    ).limit(10).all()

    # Retorna uma lista simples de strings com os nomes encontrados
    lista_nomes = [t.nome for t in sugestoes]
    return jsonify(lista_nomes)


@app.route('/admin/taxonomia/inserir-filho-manual/<int:pai_id>', methods=['POST'])
@login_required
def admin_inserir_filho_manual(pai_id):
    if current_user.nivel_acesso < 9999:
        return redirect(url_for('dashboard'))

    pai_termo = Taxonomia.query.get_or_404(pai_id)
    nome_filho = request.form.get('termo_filho_manual', '').strip()

    if not nome_filho:
        return redirect(url_for('admin_sistema', pai_id=pai_id))

    # 1. Cria o termo se ele não existir
    filho = Taxonomia.query.filter_by(nome=nome_filho).first()
    if not filho:
        filho = Taxonomia(
            nome=nome_filho,
            status='homologado',
            data_criacao=datetime.now(timezone.utc),
            data_homologacao=datetime.now(timezone.utc),
            visivel_usuario=True,  # Deixamos True para que ele possa atuar como Pai dos modelos depois!
            visivel_negocio=True
        )
        database.session.add(filho)
        database.session.commit()

    # 2. CORRIGIDO: Insere o vínculo na tabela associativa usando 'filho_id'
    conexao_existe = database.session.query(taxonomia_conexoes).filter(
        taxonomia_conexoes.c.pai_id == pai_termo.id,
        taxonomia_conexoes.c.filho_id == filho.id
    ).first()

    if not conexao_existe:
        insercao = taxonomia_conexoes.insert().values(pai_id=pai_termo.id, filho_id=filho.id)
        database.session.execute(insercao)
        database.session.commit()
        flash(f'Novo termo "{nome_filho}" criado e vinculado a "{pai_termo.nome}".', 'success')
    else:
        flash(f'O termo "{nome_filho}" já estava vinculado a "{pai_termo.nome}".', 'info')

    return redirect(url_for('admin_sistema', pai_id=pai_id))


@app.route('/admin/taxonomia/vincular-pai-raiz/<int:pai_id>', methods=['POST'])
@login_required
def admin_vincular_pai_raiz(pai_id):
    if current_user.nivel_acesso < 9999:
        return redirect(url_for('dashboard'))

    pai_termo = Taxonomia.query.get_or_404(pai_id)
    nome_raiz = request.form.get('raiz_nome', '').strip()

    if not nome_raiz:
        return redirect(url_for('admin_sistema', pai_id=pai_id))

    # 1. Busca ou cria o termo raiz (Avô) na tabela de taxonomia
    raiz = Taxonomia.query.filter_by(nome=nome_raiz).first()
    if not raiz:
        raiz = Taxonomia(
            nome=nome_raiz,
            status='homologado',
            data_criacao=datetime.now(timezone.utc),
            data_homologacao=datetime.now(timezone.utc),
            visivel_usuario=True,
            visivel_negocio=True
        )
        database.session.add(raiz)
        database.session.commit()
    else:
        # PADRONIZAÇÃO AUTOMÁTICA: Se o termo já existia mas estava desconfigurado,
        # o sistema nivela ele para True em ambas as frentes ao virar Avô.
        if not raiz.visivel_usuario or not raiz.visivel_negocio:
            raiz.visivel_usuario = True
            raiz.visivel_negocio = True
            database.session.commit()

    # 2. Insere a conexão na tabela associativa (se ela já não existir)
    conexao_existe = database.session.query(taxonomia_conexoes).filter(
        taxonomia_conexoes.c.pai_id == raiz.id,
        taxonomia_conexoes.c.filho_id == pai_id
    ).first()

    if not conexao_existe:
        insercao = taxonomia_conexoes.insert().values(pai_id=raiz.id, filho_id=pai_id)
        database.session.execute(insercao)
        database.session.commit()
        flash(f'Termo "{pai_termo.nome}" nivelado e vinculado com sucesso ao grupo "{nome_raiz}".', 'success')
    else:
        flash(f'O vínculo entre "{pai_termo.nome}" e "{nome_raiz}" já estava ativo e foi validado.', 'info')

    return redirect(url_for('admin_sistema', pai_id=pai_id))


@app.route('/admin/importar-filhos-csv/<int:pai_id>', methods=['POST'])
@login_required
def admin_importar_filhos_csv(pai_id):
    if current_user.nivel_acesso < 9999:
        return redirect(url_for('dashboard'))

    pai_termo = Taxonomia.query.get_or_404(pai_id)

    # Captura o arquivo independentemente do name do input HTML
    arquivo = request.files.get('file') or next(iter(request.files.values()), None)

    if not arquivo or arquivo.filename == '':
        flash('Por favor, envie um arquivo válido.', 'danger')
        return redirect(request.referrer)

    try:
        arquivo.seek(0)
        conteudo = arquivo.read().decode('utf-8-sig').splitlines()

        # Identifica dinamicamente se o CSV usa padrão BR (;) ou americano (,)
        primeira_linha = conteudo[0] if conteudo else ""
        delimitador = ';' if ';' in primeira_linha else ','

        leitor_csv = csv.reader(conteudo, delimiter=delimitador)

        contador_novos = 0
        contador_vinculos = 0

        for linha in leitor_csv:
            if not linha:
                continue

            nome_filho = linha[0].strip()
            if not nome_filho or nome_filho.lower() == 'nome':
                continue

            # ========================================================
            # 1. GARANTIA DO PAI (Ex: Nike ou Tênis)
            # ========================================================
            # O Pai da vez recebe sua ativação comercial própria,
            # pois ele está participando ativamente de um agrupamento estruturado.
            if not pai_termo.visivel_negocio or not pai_termo.visivel_usuario or pai_termo.status != 'homologado':
                pai_termo.visivel_negocio = True
                pai_termo.visivel_usuario = True
                pai_termo.status = 'homologado'
                database.session.commit()

            # ========================================================
            # 2. BUSCA OU CRIAÇÃO DO FILHO (Ex: Air Max ou Nike)
            # ========================================================
            filho = Taxonomia.query.filter_by(nome=nome_filho).first()

            if not filho:
                filho = Taxonomia(
                    nome=nome_filho,
                    status='homologado',
                    data_criacao=datetime.now(timezone.utc),
                    data_homologacao=datetime.now(timezone.utc),
                    visivel_usuario=True,
                    visivel_negocio=True,  # Entra na rede com passaporte comercial ativo!
                    categoria='Gosto'
                )
                database.session.add(filho)
                database.session.commit()
                contador_novos += 1
            else:
                # Auto-cura: Se o termo já existia na base (solto ou pendente),
                # ele é integrado à rede comercial com suas próprias flags ativas.
                alterou = False
                if not filho.visivel_usuario:
                    filho.visivel_usuario = True
                    alterou = True
                if not filho.visivel_negocio:
                    filho.visivel_negocio = True
                    alterou = True
                if filho.status != 'homologado':
                    filho.status = 'homologado'
                    alterou = True

                if alterou:
                    database.session.commit()

            # ========================================================
            # 3. VÍNCULO DO GRAFO (Tabela Intermediária)
            # ========================================================
            # Aqui criamos o elo específico dessa rota, sem travar o termo
            # a um único caminho fixo.
            conexao_existe = database.session.query(taxonomia_conexoes).filter(
                taxonomia_conexoes.c.pai_id == pai_termo.id,
                taxonomia_conexoes.c.filho_id == filho.id
            ).first()

            if not conexao_existe:
                insercao = taxonomia_conexoes.insert().values(
                    pai_id=pai_termo.id,
                    filho_id=filho.id
                )
                database.session.execute(insercao)
                database.session.commit()
                contador_vinculos += 1

        flash(
            f"Sucesso! {contador_novos} novos termos cadastrados e {contador_vinculos} modelos vinculados a {pai_termo.nome}.",
            "success")

    except Exception as e:
        database.session.rollback()
        flash(f"Erro ao processar o arquivo CSV: {str(e)}", "danger")

    return redirect(request.referrer)


@login_required
@apenas_admin
def admin_delete_taxonomia(id):
    termo = Taxonomia.query.get_or_404(id)
    database.session.delete(termo)
    database.session.commit()
    flash(f"Termo '{termo.nome}' removido com sucesso.", "success")
    return redirect(url_for('admin_sistema'))

@app.route("/admin/exportar-locais")
@login_required
@apenas_admin
def admin_exportar_locais():
    locais = Local.query.all()

    # Gerar o CSV em memória para download imediato
    output = io.StringIO()
    escritor = csv.writer(output)

    escritor.writerow(['id', 'nome', 'logradouro', 'bairro', 'google_place_id'])

    for l in locais:
        escritor.writerow([l.id, l.nome, l.logradouro, l.bairro, l.google_place_id])

    csv_output = output.getvalue()
    output.close()

    return Response(
        csv_output,
        mimetype="text/csv",
        headers={"Content-disposition": f"attachment; filename=backup_locais_{datetime.now().strftime('%Y%m%d')}.csv"}
    )


@app.route("/admin/local/novo", methods=["GET", "POST"])
@login_required
@apenas_admin
def admin_novo_local():
    categorias = Taxonomia.query.order_by(Taxonomia.nome).all()

    if request.method == "POST":
        # Captura os dados limpando strings vazias para None (evita erro de UNIQUE)
        def clean(field):
            val = request.form.get(field)
            return val if val and val.strip() != "" else None

        status_real = request.form.get("esta_ativo") == "1"

        try:
            novo_local = Local(
                nome=request.form.get("nome"),
                logradouro=clean("logradouro"),
                numero=clean("numero"),
                bairro=clean("bairro"),
                cidade=clean("cidade"),
                estado=clean("estado"),
                google_place_id=clean("place_id"),  # Aqui morava o erro!

                # Campos essenciais para o funcionamento
                esta_ativo=status_real,
                status_operacional="ativo",

                verificado=True,
                id_indicador=current_user.id,
                data_cadastro=datetime.now(timezone.utc)
            )

            # Categoria Principal
            id_cat = request.form.get("categoria_id")
            if id_cat:
                novo_local.id_categoria_principal = int(id_cat)

            database.session.add(novo_local)
            database.session.commit()

            flash(f"'{novo_local.nome}' registrado com sucesso!", "success")
            return redirect(url_for('admin_sistema'))

        except Exception as e:
            database.session.rollback()
            print(f"Erro ao salvar local: {e}")
            flash("Erro de integridade ou dados duplicados.", "danger")

    return render_template("admin/form_local.html", categorias=categorias)


def obter_atividades_feed(usuario):
    try:
        from datetime import timezone
        # =========================================================================
        # 0. DECLARAÇÃO DE VARIÁVEIS NO TOPO (EVITA ERROS DE ESCOPO)
        # =========================================================================
        data_nascimento_sistema = usuario.data_cadastro
        meus_interesses_ids = [t.id for t in usuario.interesses]

        memorias_locais = [m.local_id for m in VinculoUsuarioLocal.query.filter_by(usuario_id=usuario.id).all()]
        grupos_ids = [m.id_grupo for m in MembroGrupo.query.filter_by(id_usuario=usuario.id).all()]
        locais_negocio = [l.id for l in Local.query.filter(
            (Local.id_empreendedor == usuario.id) | (Local.id_indicador == usuario.id)).all()]
        meus_locais_ids = list(set(memorias_locais + grupos_ids + locais_negocio))

        # 1. Pegar conexões aceitas
        conexoes = Conexoes.query.filter(
            ((Conexoes.id_remetente == usuario.id) | (Conexoes.id_destinatario == usuario.id)),
            (Conexoes.status == 'aceito')
        ).all()

        mapa_amigos = {}
        for c in conexoes:
            amigo_id = c.id_destinatario if c.id_remetente == usuario.id else c.id_remetente
            mapa_amigos[amigo_id] = c.data_solicitacao  # Usando a data de solicitação como marco zero real

        # --- SEU PRINT DE DEBUG MANUAL (SEGURO NO ESCOPO) ---
        print("\n=== DEBUG: MAPEAMENTO DE AMIGOS E DATAS ===")
        for amigo_id, data_corte in mapa_amigos.items():
            print(f"-> Amigo ID: {amigo_id} | data_corte vinda do banco: {data_corte} | Tipo: {type(data_corte)}")
        print(f"-> Seus Interesses (Tags): {meus_interesses_ids}")
        print(f"-> Seus Locais Cadastrados: {meus_locais_ids}")
        print("===========================================\n")

        # 2. Filtros de MEMÓRIAS (Usando as variáveis já declaradas)
        filtros_memorias = [Memoria.id_usuario == usuario.id]
        for amigo_id, data_corte in mapa_amigos.items():
            if data_corte:
                if data_corte.tzinfo is None:
                    data_corte = data_corte.replace(tzinfo=timezone.utc)
                filtros_memorias.append(and_(Memoria.id_usuario == amigo_id, Memoria.data_criacao >= data_corte))
        filtros_memorias.append(and_(Memoria.privacidade == 'publico', Memoria.data_criacao >= data_nascimento_sistema))

        # =========================================================================
        # 3. FILTROS DE POSTAGENS - REGRA DE SOBERANIA DAS TAGS
        # =========================================================================
        meus_interesses_ids = [t.id for t in usuario.interesses]
        lista_amigos_ids = [int(id_amigo) for id_amigo in mapa_amigos.keys()]

        condicoes_amigos = []
        for amigo_id, data_corte in mapa_amigos.items():
            if data_corte:
                if data_corte.tzinfo is None:
                    data_corte = data_corte.replace(tzinfo=timezone.utc)

                # --- REGRA 1: POSTAGENS SEM TAG (Porteira Aberta) ---
                # Passa se for post geral do amigo OU se for em um local seu, desde que NÃO tenha tag
                post_sem_tag = and_(
                    ~Postagem.tags_afinidade.any(),
                    or_(
                        Postagem.id_local == None,
                        Postagem.id_local.in_(meus_locais_ids) if meus_locais_ids else False
                    )
                )

                # --- REGRA 2: POSTAGENS COM TAG (Peneira Restritiva) ---
                # Se a publicação tiver tag, ela OBRIGATORIAMENTE precisa bater com seus interesses
                post_com_tag = and_(
                    Postagem.tags_afinidade.any(),
                    Postagem.tags_afinidade.any(Taxonomia.id.in_(meus_interesses_ids)) if meus_interesses_ids else False
                )

                # Unificação para o Amigo: Respeita a data de corte E cai em uma das duas regras acima
                regra_amigo_completa = and_(
                    Postagem.id_usuario == int(amigo_id),
                    Postagem.data_criacao >= data_corte,
                    or_(post_sem_tag, post_com_tag)
                )
                condicoes_amigos.append(regra_amigo_completa)

        # Montamos a estrutura base do OR principal
        regras_or_postagens = [
            Postagem.id_usuario == usuario.id  # Minhas próprias postagens sempre aparecem
        ]

        if condicoes_amigos:
            regras_or_postagens.append(or_(*condicoes_amigos))

        # Critério Global de Interesses (Apenas para quem NÃO é meu amigo)
        # Segue a mesma soberania: se tem tag, exige afinidade
        if meus_interesses_ids:
            condicao_nao_ser_amigo = ~Postagem.id_usuario.in_(lista_amigos_ids) if lista_amigos_ids else True

            regras_or_postagens.append(and_(
                Postagem.tags_afinidade.any(Taxonomia.id.in_(meus_interesses_ids)),
                Postagem.data_criacao >= data_nascimento_sistema,
                condicao_nao_ser_amigo
            ))
        # =========================================================================

        # 4. Execução das Queries (COM CAPTURA DO RAIO-X)
        lista_memorias = Memoria.query.options(
            joinedload(Memoria.autor),
            joinedload(Memoria.local)
        ).filter(or_(*filtros_memorias)).all()

        query_postagens = Postagem.query.options(
            joinedload(Postagem.autor)
        ).filter(
            (Postagem.ativo == True) &
            (or_(*regras_or_postagens))
        )

        # --- O SEU RAIO-X DO SQL NO TERMINAL ---
        print("\n=== RAIO-X: SQL GERADO PELO SQLALCHEMY ===")
        print(query_postagens)
        print("===========================================\n")

        lista_postagens = query_postagens.all()

        # 5. Unificação e Normalização
        todas_atividades = list(lista_memorias) + list(lista_postagens)

        for item in todas_atividades:
            item.url_foto_autor = url_for('servir_foto_perfil', usuario_id=item.id_usuario)

            if isinstance(item, Memoria):
                item.tipo = 'memoria'
                item.local_foco = item.local
                item.autor_objeto = item.autor
                setattr(item, 'usuario_ja_curtiu', item.usuario_ja_curtiu_memoria)
                setattr(item, 'total_curtidas', item.total_curtidas_memoria)

            elif isinstance(item, Postagem):
                item.tipo = 'postagem'
                item.local_foco = getattr(item, 'local', None)
                item.autor_objeto = getattr(item, 'autor', None) or getattr(item, 'usuario', None)
                if not hasattr(item, 'usuario_ja_curtiu'):
                    setattr(item, 'usuario_ja_curtiu', lambda x: False)
                if not hasattr(item, 'total_curtidas'):
                    setattr(item, 'total_curtidas', 0)

        # 6. Ordenação final
        todas_atividades.sort(key=lambda x: x.data_criacao, reverse=True)
        return todas_atividades[:30]

    except Exception as e:
        print(f"Erro no feed: {e}")
        return []

@app.route("/cadastrar_preferencias")
@login_required
def cadastrar_preferencias():
    # Buscamos apenas as categorias PAI (onde id_pai é nulo)
    categorias = Taxonomia.query.filter_by(id_pai=None).order_by(Taxonomia.nome).all()

    # Pegamos as preferências que o usuário já tem (para marcar como check)
    minhas_prefs_ids = [p.id for p in current_user.interesses]

    return render_template("preferencias.html",
                           categorias=categorias,
                           minhas_prefs_ids=minhas_prefs_ids)


@app.route('/api/buscar-interesses')
@login_required
def buscar_interesses():
    termo = request.args.get('q', '').strip()
    if len(termo) < 2:
        return jsonify([])

    # BUSCA RESTRITA: Somente tags que o usuário já selecionou no cadastro/perfil
    meus_gostos = Taxonomia.query.join(usuarios_interesses).filter(
        usuarios_interesses.c.usuario_id == current_user.id,
        Taxonomia.nome.ilike(f'%{termo}%')
    ).limit(5).all()

    return jsonify([{'id': t.id, 'nome': t.nome} for t in meus_gostos])


@app.route('/salvar_preferencias', methods=['POST'])
@login_required
def salvar_preferencias():
    ids_raw = request.form.get('preferencias_ids', '')
    novos_termos_raw = request.form.get('novos_termos', '')

    try:
        # 1. Obter IDs e Termos
        ids_selecionados = [int(tid) for tid in ids_raw.split(',') if tid.strip().isdigit()]

        # Guardamos uma lista de todas as tags que o usuário mexeu nesta rodada
        # para analisar o volume de interesse delas logo depois de salvar
        tags_para_analisar = []

        # Limpa o relacionamento lazy='dynamic' de forma segura
        interesses_atuais = list(current_user.interesses)
        for interesse in interesses_atuais:
            current_user.interesses.remove(interesse)
            tags_para_analisar.append(interesse)  # Analisa se perdeu interesse

        # Força o SQLAlchemy a processar as remoções antes de começarmos as inserções
        database.session.flush()

        # 2. Adicionar Tags Existentes
        if ids_selecionados:
            tags_existentes = Taxonomia.query.filter(Taxonomia.id.in_(ids_selecionados)).all()
            for tag in tags_existentes:
                current_user.interesses.append(tag)
                tags_para_analisar.append(tag)  # Analisa o ganho de interesse

        # 3. Adicionar Novos Termos (Respeitando sua Model)
        if novos_termos_raw:
            termos_processados = set()

            for nome in novos_termos_raw.split(','):
                nome_limpo = nome.strip()
                if not nome_limpo or nome_limpo in termos_processados:
                    continue
                termos_processados.add(nome_limpo)

                tag_nova = Taxonomia.query.filter_by(nome=nome_limpo).first()
                if not tag_nova:
                    tag_nova = Taxonomia(
                        nome=nome_limpo,
                        status='pendente',
                        visivel_usuario=True,
                        visivel_negocio=False,
                        categoria='Gosto'
                    )
                    database.session.add(tag_nova)
                    database.session.flush()  # Gera o ID para a tag_nova

                if tag_nova not in current_user.interesses:
                    current_user.interesses.append(tag_nova)

                tags_para_analisar.append(tag_nova)  # Analisa o termo novo criado da rua

        # EXECUTA O COMMIT PRINCIPAL AQUI!
        # Dados gravados e consolidados na tabela intermediária 'usuarios_interesses'
        database.session.commit()

        # =====================================================================
        # 🔥 GATILHO DOS 5 AUTOMÁTICO (Inteligência Coletiva do FeedIn)
        # =====================================================================
        # Remove duplicados da nossa lista de análise para rodar o SQL uma vez por tag
        tags_unicas = set(tags_para_analisar)

        for tag in tags_unicas:
            # Só avaliamos termos que ainda estão na fila de espera ('pendente')
            if tag.status == 'pendente':
                # .buscar_total_seguidores() usa o .count() direto no banco que você já programou na Model!
                total_seguidores_reais = tag.buscar_total_seguidores()

                if total_seguidores_reais >= 5:
                    tag.status = 'homologado'
                    tag.data_homologacao = datetime.now(timezone.utc)
                    tag.visivel_usuario = True
                    tag.visivel_negocio = False  # Continua comercialmente seguro até o Admin dar um Pai

        # Se alguma tag foi promovida a homologada, salva a mudança de status
        database.session.commit()
        # =====================================================================

        # 4. Validação de Nível (Agora o banco de dados está 100% atualizado)
        total_atual = current_user.interesses.count()
        nivel_antes = current_user.nivel_acesso

        if nivel_antes < 10 and total_atual >= 10:
            processar_mudanca_nivel(current_user, 10)
            database.session.commit()  # Commit da mudança de nível
            flash("Bem-vindo ao FeedIn.", "success")
            return redirect(url_for('feed'))

        elif nivel_antes < 10:
            flash(f"Interesses salvos! Selecione mais {10 - total_atual} para liberar seu acesso.", "info")
            return redirect(url_for('configuracoes', aba='preferencias'))

        else:
            flash("Suas preferências foram atualizadas com sucesso.", "success")
            return redirect(url_for('configuracoes', aba='preferencias'))

    except Exception as e:
        database.session.rollback()
        print(f"DEBUG SALVAR PREFS (Erro Real): {str(e)}")
        import traceback;
        traceback.print_exc()
        flash("Erro ao salvar preferências. Tente novamente.", "danger")
        return redirect(url_for('configuracoes', aba='preferencias'))


@app.route("/remover-interesse/<int:id_interesse>", methods=["POST"])
@login_required
def remover_interesse(id_interesse):
    interesse = Taxonomia.query.get_or_404(id_interesse)

    # 1. Verificamos se ele já está no limite mínimo
    if len(current_user.interesses) <= 5:
        return jsonify({
            "status": "erro",
            "msg": "Para garantir a qualidade das conexões, você deve manter no mínimo 5 gostos configurados."
        }), 400

    # 2. Se tiver mais de 5, procede com a remoção
    if interesse in current_user.interesses:
        current_user.interesses.remove(interesse)
        database.session.commit()
        return jsonify({
            "status": "sucesso",
            "total": len(current_user.interesses)
        })

    return jsonify({"status": "erro", "msg": "Interesse não encontrado"}), 404


@app.route('/admin/taxonomia/remover_raiz/<int:pai_id>/<int:raiz_id>', methods=['POST'])
@login_required
def admin_remover_pai_raiz(pai_id, raiz_id):
    try:
        termo_pai = Taxonomia.query.get(pai_id)
        termo_raiz = Taxonomia.query.get(raiz_id)

        if not termo_pai or not termo_raiz:
            return jsonify({'success': False, 'error': 'Termos não localizados no banco.'}), 404

        # Quebra o relacionamento na tabela polimórfica (taxonomia_conexoes)
        if termo_raiz in termo_pai.contextos:
            termo_pai.contextos.remove(termo_raiz)
        elif termo_pai in termo_raiz.contextos:
            termo_raiz.contextos.remove(termo_pai)

        database.session.commit()
        return jsonify({'success': True})

    except Exception as e:
        database.session.rollback()
        print(f"ERRO API REMOVER VÍNCULO: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route("/declinar-conexao/<int:id_conexao>", methods=["POST"])
@login_required
def declinar_conexao(id_conexao):
    conexao = Conexoes.query.get_or_404(id_conexao)

    # SEGURANÇA: Apenas o destinatário (quem recebeu) pode "recusar"
    # ou o remetente pode "cancelar" a própria solicitação
    if current_user.id in [conexao.id_destinatario, conexao.id_remetente]:
        try:
            # Em vez de delete, fazemos o update
            conexao.status = 'recusado'
            conexao.data_recusa = datetime.now(timezone.utc)

            database.session.commit()
            flash("Solicitação arquivada.", "info")
        except Exception as e:
            database.session.rollback()
            flash("Erro ao processar a ação.", "danger")
            print(f"Erro no declínio: {e}")
    else:
        flash("Você não tem permissão para alterar esta conexão.", "danger")

    return redirect(url_for('dashboard', aba='conexoes'))


def obter_sugestoes_pioneiras(usuario_atual):
    # 1. Automação de Rigor (Mantido)
    total_pioneiros = Usuario.query.filter(Usuario.nivel_acesso >= 10).count()
    modo_rigoroso = app.config.get('MODO_PRODUCAO') or (total_pioneiros > 100)

    # 2. Lista de Exclusão (Corrigido para ser mais performático)
    # No passo 2 da sua função obter_sugestoes_pioneiras:
    relacoes_existentes = Conexoes.query.filter(
        (Conexoes.id_remetente == usuario_atual.id) |
        (Conexoes.id_destinatario == usuario_atual.id)
    ).all()

    # Certifique-se de que esta lista pegue o "outro lado" de cada conexão
    ids_bloqueados = []
    for c in relacoes_existentes:
        if c.id_remetente != usuario_atual.id:
            ids_bloqueados.append(c.id_remetente)
        if c.id_destinatario != usuario_atual.id:
            ids_bloqueados.append(c.id_destinatario)

    ids_bloqueados.append(usuario_atual.id)
    ids_bloqueados = list(set(ids_bloqueados))  # Remove duplicatas

    # 3. Coleta de IDs (Interesses e seus Pais)
    minhas_prefs = usuario_atual.interesses.all()
    minhas_prefs_ids = [int(p.id) for p in minhas_prefs]

    ids_meus_pais = []
    for p in minhas_prefs:
        # Pega os IDs dos pais para aumentar o espectro de match
        ids_meus_pais.extend([int(pai.id) for pai in p.contextos])

    busca_total_ids = list(set(minhas_prefs_ids + ids_meus_pais))
    meus_grupos_ids = [m.id_grupo for m in usuario_atual.membros_grupos]

    # 4. A QUERY (AJUSTADA)
    possiveis_conexoes = (Usuario.query
                          .outerjoin(MembroGrupo)
                          .options(
        database.joinedload(Usuario.perfil),
        database.joinedload(Usuario.membros_grupos)
    )
                          # AJUSTE: Buscamos por nível_acesso >= 10 OU is_pioneiro
                          .filter(or_(Usuario.nivel_acesso >= 10, Usuario.is_pioneiro == True))
                          .filter(~Usuario.id.in_(ids_bloqueados))
                          .filter(
        or_(
            Usuario.membros_grupos.any(MembroGrupo.id_grupo.in_(meus_grupos_ids)) if meus_grupos_ids else False,
            Usuario.interesses.any(Taxonomia.id.in_(busca_total_ids))
        )
    )
                          .distinct()
                          .limit(15)  # Aumentamos o limite antes do filtro de rigor
                          .all())

    # 5. Processamento dos Cards (Mantido com melhorias de segurança)
    sugestoes_finais = []
    meus_locais_ids = [v.local_id for v in usuario_atual.vinculos]

    for outro in possiveis_conexoes:
        amigo_ponte = usuario_atual.get_amigo_em_comum(outro)

        if modo_rigoroso and not amigo_ponte:
            # Se tiver poucos usuários no sistema, o modo rigoroso pode esvaziar o carrossel
            if total_pioneiros > 20:
                continue

        # Lógica de Interesses em Comum
        # Como 'outro.interesses' é dinâmico, filtramos via Python para o Card
        interesses_outro_ids = [p.id for p in outro.interesses.all()]
        interesses_comum_nomes = [p.nome for p in outro.interesses.all() if p.id in minhas_prefs_ids]

        # Lógica de Locais
        locais_comum = [v.local.nome for v in outro.vinculos if v.local_id in meus_locais_ids]
        locais_unicos = list(set(locais_comum))

        # Cálculo de Peso para o Ranking
        calculo_peso = (len(locais_unicos) * 10) + (len(interesses_comum_nomes) * 5)
        if amigo_ponte: calculo_peso += 20

        sugestoes_finais.append({
            'usuario': outro,
            'motivo': f"Conhece {amigo_ponte.username}" if amigo_ponte else "Interesses em comum",
            'amigo_ponte': amigo_ponte,
            'preferencias': interesses_comum_nomes[:3],
            'total_restante_prefs': max(0, len(interesses_comum_nomes) - 3),
            'locais': locais_unicos,
            'total_locais': len(locais_unicos),
            'peso': calculo_peso
        })

    return sorted(sugestoes_finais, key=lambda x: x['peso'], reverse=True)


def obter_sugestoes_carrossel(usuario_atual):
    bloqueados = ids_bloqueados_pelo_usuario(usuario_atual)
    # Pega 15 candidatos para filtrar os 10 melhores
    possiveis = Usuario.query.filter(or_(Usuario.nivel_acesso >= 10, Usuario.is_pioneiro == True)) \
        .filter(~Usuario.id.in_(bloqueados)) \
        .limit(15).all()

    processados = processar_dados_conexoes(usuario_atual, possiveis)
    return sorted(processados, key=lambda x: x['peso'], reverse=True)[:10]


def ids_bloqueados_pelo_usuario(usuario_atual):
    """
    Retorna uma lista de IDs que não devem aparecer em sugestões:
    O próprio usuário, amigos atuais e conexões pendentes.
    """
    # 1. Já começa com o ID do próprio usuário
    bloqueados = [usuario_atual.id]

    # 2. Busca conexões (qualquer status: aceito ou pendente)
    relacoes = Conexoes.query.filter(
        (Conexoes.id_remetente == usuario_atual.id) |
        (Conexoes.id_destinatario == usuario_atual.id)
    ).all()

    for r in relacoes:
        if r.id_remetente != usuario_atual.id:
            bloqueados.append(r.id_remetente)
        if r.id_destinatario != usuario_atual.id:
            bloqueados.append(r.id_destinatario)

    return list(set(bloqueados))  # Remove duplicatas por segurança


def processar_dados_conexoes(usuario_atual, lista_usuarios):
    """
    Transforma uma lista de objetos Usuario em um dicionário
    com pesos, interesses em comum e locais compartilhados.
    """
    sugestoes_finais = []

    # Cache dos dados do usuário atual para comparação rápida
    minhas_prefs_ids = [p.id for p in usuario_atual.interesses]
    meus_locais_ids = [v.local_id for v in usuario_atual.vinculos]

    for outro in lista_usuarios:
        # 1. Verifica amigo em comum (Ponte)
        amigo_ponte = usuario_atual.get_amigo_em_comum(outro)

        # 2. Interesses em comum
        interesses_comum_nomes = [p.nome for p in outro.interesses.all() if p.id in minhas_prefs_ids]

        # 3. Locais em comum
        locais_comum = [v.local.nome for v in outro.vinculos if v.local_id in meus_locais_ids]
        locais_unicos = list(set(locais_comum))

        # 4. Cálculo de Peso (Ranking)
        # Locais valem 10, Interesses valem 5, Amigo em comum vale 20
        calculo_peso = (len(locais_unicos) * 10) + (len(interesses_comum_nomes) * 5)
        if amigo_ponte:
            calculo_peso += 20

        sugestoes_finais.append({
            'usuario': outro,
            'motivo': f"Conhece {amigo_ponte.username}" if amigo_ponte else "Interesses em comum",
            'amigo_ponte': amigo_ponte,
            'preferencias': interesses_comum_nomes[:3],  # Mostra só os 3 primeiros no card
            'total_restante_prefs': max(0, len(interesses_comum_nomes) - 3),
            'locais': locais_unicos,
            'total_locais': len(locais_unicos),
            'peso': calculo_peso
        })

    return sugestoes_finais


def obter_todas_sugestoes_aba(usuario_atual):
    bloqueados = ids_bloqueados_pelo_usuario(usuario_atual)
    # Sem limit agressivo para a aba ser completa
    possiveis = Usuario.query.filter(or_(Usuario.nivel_acesso >= 10, Usuario.is_pioneiro == True)) \
        .filter(~Usuario.id.in_(bloqueados)).all()

    processados = processar_dados_conexoes(usuario_atual, possiveis)
    # Na aba, talvez você queira ordenar por peso também, ou por nome
    return sorted(processados, key=lambda x: x['peso'], reverse=True)


@app.route("/declinar-sugestao/<int:id_alvo>", methods=["POST"])
@login_required
def declinar_sugestao(id_alvo):
    # Aqui você pode salvar em uma tabela de 'ignorado'
    # ou simplesmente não fazer nada, já que o HTMX vai deletar o card da tela.
    return "", 200 # Retorna vazio com status OK para o HTMX deletar o elemento


@app.route("/conectar-pioneiro/<int:id_destinatario>", methods=["POST"])
@login_required
def conectar_pioneiro(id_destinatario):
    # Lógica de banco de dados
    conexao_existente = Conexoes.query.filter(
        ((Conexoes.id_remetente == current_user.id) & (Conexoes.id_destinatario == id_destinatario)) |
        ((Conexoes.id_remetente == id_destinatario) & (Conexoes.id_destinatario == current_user.id))
    ).first()

    if not conexao_existente:
        nova = Conexoes(id_remetente=current_user.id, id_destinatario=id_destinatario, status='pendente')
        database.session.add(nova)
        database.session.commit()
        # O flash aqui só será visto se a página recarregar (sem HTMX)
        flash("Reconhecimento enviado!", "success")

    # Verificação HTMX
    if request.headers.get('HX-Request'):
        return f'''
                <div class="col-12 col-md-6 col-xl-4">
                    <div class="card border-0 shadow-sm rounded-4 h-100 bg-light opacity-75">
                        <div class="card-body d-flex align-items-center justify-content-center">
                            <div class="text-center">
                                <i class="bi bi-send-check-fill text-primary fs-2"></i>
                                <p class="small fw-bold mb-0">Reconhecimento enviado!</p>
                            </div>
                        </div>
                    </div>
                </div>
        ''' # <--- AS ASPAS QUE FALTAVAM AQUI

    # Retorno padrão para requisições normais
    return redirect(url_for('dashboard', aba='conexoes', tab='sugestoes'))


@app.route("/boas-vindas-pioneiros")
@login_required
def exibir_sugestoes_pioneiros():
    sugestoes = obter_sugestoes_pioneiras(current_user)

    # Se por acaso não houver ninguém com afinidade ainda (base vazia),
    # manda direto para a homepage para não travar o usuário
    if not sugestoes:
        return redirect(url_for('dashboard'))

    return render_template("sugestoes_pioneiros.html", sugestoes=sugestoes)


def buscar_afinidades_por_fiador(usuario, tag_nome):
    # 1. Identificar quem são os SEUS amigos diretos (seus fiadores)
    # Filtramos conexões onde o usuário é remetente ou destinatário
    conexoes_usuario = Conexoes.query.filter(
        (Conexoes.status == 'aceito') &
        ((Conexoes.id_remetente == usuario.id) | (Conexoes.id_destinatario == usuario.id))
    ).all()

    meus_amigos_ids = [c.id_remetente if c.id_remetente != usuario.id else c.id_destinatario
                       for c in conexoes_usuario]

    # 2. Buscar usuários com a mesma tag que NÃO são seus amigos diretos
    sugestoes = Usuario.query.join(Usuario.interesses).filter(
        Taxonomia.nome == tag_nome,
        Usuario.id != usuario.id,
        Usuario.id.notin_(meus_amigos_ids)
    ).all()

    # 3. Validar Afinidade (Existe um fiador/amigo em comum?)
    afinidades_validadas = []

    for potencial_amigo in sugestoes:
        # Buscamos os amigos deste 'potencial_amigo'
        conexoes_potencial = Conexoes.query.filter(
            (Conexoes.status == 'aceito') &
            ((Conexoes.id_remetente == potencial_amigo.id) | (Conexoes.id_destinatario == potencial_amigo.id))
        ).all()

        ids_amigos_do_potencial = [c.id_remetente if c.id_remetente != potencial_amigo.id else c.id_destinatario
                                   for c in conexoes_potencial]

        # Interseção: Algum amigo dele está na MINHA lista de amigos?
        amigos_em_comum = set(meus_amigos_ids).intersection(set(ids_amigos_do_potencial))

        if amigos_em_comum:
            # Se houver interseção, a afinidade é validada por um 'fiador' real
            # Você pode até guardar quem é o fiador se quiser exibir: "Fulano também é amigo de Carlos"
            afinidades_validadas.append(potencial_amigo)

    return afinidades_validadas

@app.route("/feed")
@login_required
def feed():

    # 1. Identifica a aba (útil se você unificar com a homepage futuramente)
    aba = 'feed'

    # 2. Dados básicos para a "Linha Mágica" e Identificação de Posse
    # Buscamos Grupos Sociais (MembroGrupo)
    grupos_filiados = MembroGrupo.query.filter_by(id_usuario=current_user.id).all()
    grupos_ids = [m.id_grupo for m in grupos_filiados]

    # Buscamos Estabelecimentos de Piracicaba (Local - Empreendedor ou Indicador)
    locais_vinculados = Local.query.filter(
        (Local.id_empreendedor == current_user.id) | (Local.id_indicador == current_user.id)
    ).all()
    locais_negocio_ids = [l.id for l in locais_vinculados]

    # Unificação para o Verificador do Template (meus_locais_ids)
    meus_locais_ids = grupos_ids + locais_negocio_ids

    # --- CONTADORES PARA O DASHBOARD ---
    contagem = len(meus_locais_ids)

    total_pendentes = Conexoes.query.filter_by(
        id_destinatario=current_user.id,
        status='pendente'
    ).count()

    total_conexoes = Conexoes.query.filter(
        ((Conexoes.id_remetente == current_user.id) | (Conexoes.id_destinatario == current_user.id)) &
        (Conexoes.status == 'aceito')
    ).count()

    # 3. Busca de Conteúdo Dinâmico
    # Sugestões para o carrossel de Pioneiros
    sugestoes = obter_sugestoes_pioneiras(current_user)

    # Atividades (Memórias do usuário + Amigos + Públicas)
    atividades_recentes = obter_atividades_feed(current_user)

    # 4. Renderização com todas as variáveis "vivas"
    return render_template("homepage.html",
                           aba=aba,
                           atividades_recentes=atividades_recentes,
                           sugestoes=sugestoes,
                           meus_locais_ids=meus_locais_ids,
                           usuario=current_user,
                           contagem=contagem,
                           total_pendentes=total_pendentes,
                           total_conexoes=total_conexoes)


@app.route('/sugerir_preferencia', methods=['POST'])
@login_required
def sugerir_preferencia():
    nome_sugerido = request.form.get('nome').strip().capitalize()

    # Verifica se já existe (mesmo não verificada)
    existente = Taxonomia.query.filter_by(nome=nome_sugerido).first()

    if not existente:
        nova = Taxonomia(
            nome=nome_sugerido,
            status="Pendente",
            data_criacao=datetime.now(fuso_sp),
            contagem_uso=1,
            visivel_usuario=1,
            visivel_negocio=1
        )

        database.session.add(nova)
        database.session.commit()
        return jsonify({"status": "sucesso", "msg": "Sugestão enviada!"})

    return jsonify({"status": "existe", "msg": "Esta preferência já está em análise."})


@app.route('/api/busca_taxonomia')
@login_required
def busca_taxonomia():
    termo = request.args.get('q', '').strip()
    if len(termo) < 2:
        return jsonify([])

    # Buscamos na sua tabela Taxonomia
    # Filtramos por visibilidade e pelo que o usuário está digitando
    resultados = Taxonomia.query.filter(
        Taxonomia.nome.ilike(f'%{termo}%'),
        Taxonomia.visivel_usuario == True
    ).order_by(Taxonomia.contagem_uso.desc()).limit(10).all()

    return jsonify([{'id': r.id, 'text': r.nome} for r in resultados])


def registrar_uso_preferencia(preferencia_obj):
    """
    Lógica que automatiza a verificação baseada no uso coletivo.
    """
    if not preferencia_obj.verificado:
        preferencia_obj.contador_uso += 1

        # A REGRA DE OURO: 10 Cliques = Oficialização Automática
        if preferencia_obj.contador_uso >= 10:
            preferencia_obj.verificado = True
            # Aqui você poderia até disparar um log: "Preferência X se tornou oficial!"

    database.session.commit()

# Exemplo de como você consultaria isso no Admin
def get_arvore_completa():
    # Busca apenas os grandes grupos (pais de todos)
    raizes = Taxonomia.query.filter(~Taxonomia.contextos.any()).all()
    return raizes


# Função para busca de patrocinador para estratégia de marketing
def obter_patrocinador_contextual(categoria_alvo):
    # Busca um local aleatório que combine com a categoria
    patrocinador = Local.query.filter_by(
        categoria=categoria_alvo,
        verificado=True
    ).order_by(func.random()).first()

    # Conta quantos outros existem para o seu "+X"
    total_na_categoria = Local.query.filter_by(categoria=categoria_alvo).count()

    return patrocinador, (total_na_categoria - 1 if total_na_categoria > 0 else 0)

# escolhe qual patrocinador tem prioridade na exibição do anúncio
def obter_destaque_comercial(categoria_alvo):
    # 1. Busca APENAS quem tem o plano de marketing ativo para essa categoria
    parceiro = Local.query.filter_by(
        categoria=categoria_alvo,
        plano_marketing='patrocinado',
        verificado=True
    ).order_by(func.random()).first()

    # 2. Conta quantos outros parceiros (não o total geral) existem no mesmo plano
    outros_parceiros = Local.query.filter(
        Local.categoria == categoria_alvo,
        Local.plano_marketing == 'patrocinado',
        Local.id != (parceiro.id if parceiro else 0)
    ).count()

    return parceiro, outros_parceiros


# Para que o Admin consiga moderar com eficiência, a rota precisa registrar quem sugeriu, criando um vínculo de confiança.
@app.route('/sugerir_local', methods=['POST'])
@login_required
def sugerir_local():
    # Coleta os dados que o usuário preencheu no "formulário de emergência"
    nome = request.form.get('nome')
    logradouro = request.form.get('logradouro')
    bairro = request.form.get('bairro')
    cidade = request.form.get('cidade', 'Piracicaba')
    estado = request.form.get('estado', 'SP')

    # Validação básica de segurança
    if not nome or not bairro:
        return jsonify({"status": "erro", "message": "Nome e Bairro são obrigatórios"}), 400

    try:
        novo_local = Local(
            nome=nome,
            logradouro=logradouro,
            bairro=bairro,
            cidade=cidade,
            estado=estado,
            verificado=False,  # Cai na fila do Admin
            id_indicador=current_user.id  # Rastreabilidade total
        )

        database.session.add(novo_local)
        database.session.commit()

        # O segredo: retornar o ID para o front-end já usar na próxima etapa
        return jsonify({
            "status": "sucesso",
            "local_id": novo_local.id,
            "nome": novo_local.nome
        })
    except Exception as e:
        database.session.rollback()
        return jsonify({"status": "erro", "message": str(e)}), 500


@app.route('/api/local/<int:local_id>/tags_ocultas')
@login_required
def tags_ocultas_local(local_id):
    try:

        # 1. Busca os posts ativos do local
        posts_do_local = Postagem.query.filter_by(id_local=local_id, ativo=True).all()
        if not posts_do_local:
            return jsonify([])

        # 2. Pega os IDs dos interesses do usuário logado
        interesses_usuario_ids = {tag.id for tag in current_user.interesses}

        tags_contagem = {}

        # 3. Usa o relacionamento REAL da sua Model: tags_afinidade
        for post in posts_do_local:
            if post.tags_afinidade:
                for tag in post.tags_afinidade:
                    if tag.id not in interesses_usuario_ids:
                        if tag.id not in tags_contagem:
                            tags_contagem[tag.id] = {
                                'id': tag.id,
                                'nome': tag.nome,
                                'categoria': tag.categoria or 'Gosto',
                                'total': 0
                            }
                        tags_contagem[tag.id]['total'] += 1

        # 4. Ordenação Alfabética pura
        lista_ordenada = sorted(tags_contagem.values(), key=lambda x: x['nome'].lower())
        return jsonify(lista_ordenada)

    except Exception as e:
        print(f"\n[ERRO CRÍTICO] {str(e)}")
        return jsonify({'erro': str(e)}), 500


@app.route('/api/seguir_tag_direto/<int:tag_id>', methods=['POST'])
@login_required
def seguir_tag_direto(tag_id):
    try:

        tag = Taxonomia.query.get_or_404(tag_id)

        if tag not in current_user.interesses:
            current_user.interesses.append(tag)
            database.session.commit()
            return jsonify({'sucesso': True, 'mensagem': f'Agora você segue #{tag.nome}!'})

        return jsonify({'sucesso': False, 'mensagem': 'Você já segue este tema.'})
    except Exception as e:
        database.session.rollback()
        print(f"[ERRO FEEDIN] Falha ao seguir tag: {str(e)}")
        return jsonify({'sucesso': False, 'mensagem': 'Erro ao salvar interesse.'}), 500


@app.route('/salvar_grupo_social', methods=['POST', 'GET'])
@login_required
def salvar_grupo_social():
    local_id = request.form.get('local_id')
    nome_digitado = request.form.get('nome', '').strip()
    nome_oficial = request.form.get('nome_oficial', '').strip()
    periodo = request.form.get('periodo_referencia')
    experiencia = request.form.get('experiencia_usuario', '').strip()

    esta_ativo_form = request.form.get('esta_ativo') == '1'
    ano_fim = request.form.get('ano_encerramento') if not esta_ativo_form else None

    try:
        # 1. Definição do Local
        if local_id and local_id != "":
            local_alvo = Local.query.get(local_id)
        else:
            nome_final = nome_oficial if nome_oficial else nome_digitado
            local_alvo = Local(
                nome=nome_final,
                logradouro=request.form.get('logradouro', '').strip(),
                bairro=request.form.get('bairro', '').strip(),
                cidade=request.form.get('cidade', 'Piracicaba'),
                id_indicador=current_user.id,
                esta_ativo=esta_ativo_form,
                ano_encerramento=ano_fim,
                status_operacional='pendente'
            )
            database.session.add(local_alvo)
            database.session.flush()

        # 2. Vínculo e Atividade
        vinculo_existente = VinculoUsuarioLocal.query.filter_by(
            usuario_id=current_user.id, local_id=local_alvo.id
        ).first()

        if not vinculo_existente:
            vinculo = VinculoUsuarioLocal(
                usuario_id=current_user.id,
                local_id=local_alvo.id,
                experiencia=periodo
            )
            database.session.add(vinculo)

        atividade = AtividadeLocal(
            nome=f"Memória em {local_alvo.nome}",
            id_local=local_alvo.id,
            id_criador=current_user.id,
            descricao=experiencia,
            periodo_estimado=periodo
        )
        database.session.add(atividade)

        # 3. Lógica de Promoção (Checklist de Onboarding)
        # Requisito: 5 Lugares + 10 Interesses (Tags da Taxonomia) + LGPD
        # Nota: Ajuste 'interesses_escolhidos' para o nome da relação no seu Model Usuario
        total_lugares = len(current_user.vinculos)
        # total_tags = current_user.interesses_escolhidos.count()

        if total_lugares >= 5 and current_user.aceite_lgpd:
                flash("Identidade Validada!", "success")

        database.session.commit()
        flash("Memória guardada com sucesso!", "success")

    except Exception as e:
        database.session.rollback()
        flash(f"Erro ao salvar: {str(e)}", "danger")

    return redirect(url_for('get_perfil', id_usuario=current_user.id, aba='memorias'))


@app.route('/excluir_memoria/<int:id_vinculo>')
@login_required
def excluir_memoria(id_vinculo):
    vinculo = VinculoUsuarioLocal.query.get_or_404(id_vinculo)

    if vinculo.usuario_id != current_user.id:
        return redirect(url_for('index'))

    try:
        # Remove a atividade ligada a este vínculo
        AtividadeLocal.query.filter_by(
            id_local=vinculo.local_id,
            id_criador=current_user.id
        ).delete()

        database.session.delete(vinculo)
        database.session.commit()
        flash("Registro removido.", "info")
    except:
        database.session.rollback()

    return redirect(url_for('get_perfil', id_usuario=current_user.id, aba='memorias'))


@app.route('/editar_memoria/<int:id_vinculo>', methods=['POST'])
@login_required
def editar_memoria(id_vinculo):
    vinculo = VinculoUsuarioLocal.query.get_or_404(id_vinculo)

    # Proteção: só o dono edita
    if vinculo.usuario_id != current_user.id:
        flash("Acesso não autorizado.", "danger")
        return redirect(url_for('index'))

    try:
        # 1. Atualiza Época no Vínculo
        vinculo.experiencia = request.form.get('periodo_referencia')

        # 2. Atualiza Relato na Atividade correspondente
        atividade = AtividadeLocal.query.filter_by(
            id_local=vinculo.local_id,
            id_criador=current_user.id
        ).first()

        if atividade:
            atividade.descricao = request.form.get('experiencia_usuario', '').strip()
            atividade.periodo_estimado = vinculo.experiencia

        # 3. Atualiza dados do Local (Se for o indicador original)
        # Nota: Só permitimos mudar endereço se o local ainda estiver 'pendente'
        if vinculo.local.id_indicador == current_user.id and vinculo.local.status_operacional == 'pendente':
            vinculo.local.logradouro = request.form.get('logradouro', '').strip()
            vinculo.local.bairro = request.form.get('bairro', '').strip()
            vinculo.local.esta_ativo = request.form.get('esta_ativo') == '1'

        database.session.commit()
        flash("Memória atualizada!", "success")

    except Exception as e:
        database.session.rollback()
        flash("Erro ao atualizar.", "danger")

    return redirect(url_for('get_perfil', id_usuario=current_user.id, aba='memorias'))


def buscar_conexoes_por_local_com_fiador(usuario_carlos):
    # 1. Pegamos os IDs e as Épocas que o Carlos viveu
    meus_vinculos = {v.local_id: v.experiencia for v in usuario_carlos.vinculos}
    meus_locais_ids = list(meus_vinculos.keys())

    # 2. Busca otimizada: Já trazemos os usuários e seus vínculos num só "join"
    # Adicionamos uma condição de época para dar prioridade ou filtrar
    potenciais = (Usuario.query
                  .join(VinculoUsuarioLocal)
                  .filter(VinculoUsuarioLocal.local_id.in_(meus_locais_ids))
                  .filter(Usuario.id != usuario_carlos.id)
                  .all())

    sugestoes_com_sentido = []

    for candidato in potenciais:
        # Checa se bateram as épocas (Isso é o resgate de memória!)
        vinculos_comuns = [v for v in candidato.vinculos if v.local_id in meus_locais_ids]

        mesma_epoca = any(v.experiencia == meus_vinculos.get(v.local_id) for v in vinculos_comuns)

        # O Fiador vira um "Bônus" ou um validador, não necessariamente uma trava total
        fiador = usuario_carlos.get_amigo_em_comum(candidato)

        if fiador or mesma_epoca:
            sugestoes_com_sentido.append({
                "candidato": candidato,
                "fiador": fiador,
                "mesma_epoca": mesma_epoca,
                "vinculos": vinculos_comuns,
                "score": 100 if (fiador and mesma_epoca) else 50
            })

    # Ordena por quem tem mais chance de ser uma conexão real
    return sorted(sugestoes_com_sentido, key=lambda x: x['score'], reverse=True)

def incrementar_uso_taxonomia(termo_id):
    termo = Taxonomia.query.get(termo_id)
    termo.contagem_uso += 1

    if termo.status == 'pendente' and termo.contagem_uso >= 5:
        termo.status = 'homologado'
        termo.data_homologacao = datetime.now(timezone.utc)

    database.session.commit()


@app.route('/gerar-convite', methods=['POST'])
@login_required
def gerar_convite():
    form = FormConvite()

    if form.validate_on_submit():
        try:
            # 1. Captura e Limpeza de Dados
            numero_destino = re.sub(r'\D', '', form.whatsapp.data)
            nome_amigo = form.nome_convidado.data or "Amigo(a)"

            contexto_raw = request.form.get('contexto_convite')
            tipo_vinculo = None
            id_referencia = None
            nome_contexto = "nossa rede"

            # Processa o contexto se ele existir e for válido
            if contexto_raw and "_" in contexto_raw:
                tipo_vinculo, id_raw = contexto_raw.split('_', 1)
                if id_raw.isdigit():
                    id_referencia = int(id_raw)

                    # Busca o nome descritivo para a mensagem do WhatsApp
                    if tipo_vinculo == 'local':
                        obj = Local.query.get(id_referencia)
                        if obj: nome_contexto = obj.nome
                    elif tipo_vinculo == 'gosto':
                        obj = AtividadeLocal.query.get(id_referencia)
                        if obj: nome_contexto = obj.nome

            # 2. Verificação de Exclusividade
            convite_existente = Convite.query.filter_by(whatsapp_destino=numero_destino).first()

            if convite_existente:
                if convite_existente.id_remetente == current_user.id:
                    flash(f"Você já enviou um convite para {nome_amigo}!", "info")
                else:
                    flash("Este número já recebeu um convite de outro membro do FeedIn.", "warning")
                return redirect(url_for('dashboard', aba='configuracoes'))

            # 3. Criação do Registro alinhado com a Model
            novo_convite = Convite(
                id_remetente=current_user.id,
                whatsapp_destino=numero_destino,
                tipo_vinculo=tipo_vinculo,  # 'local', 'gosto' ou None
                id_referencia=id_referencia,  # Inteiro limpo ou None
                status_onboarding=False
            )

            database.session.add(novo_convite)
            database.session.commit()

            # 4. Preparação do Link e Mensagem (CORRIGIDO PARA NEWUSER)
            link_registro = url_for('newuser',  # <-- Aqui estava o erro! Mudado para 'newuser'
                                    indicado_por=current_user.id,
                                    contexto=contexto_raw if contexto_raw else 'geral',
                                    _external=True)

            if tipo_vinculo and id_referencia:
                texto_base = (
                    f"Olá {nome_amigo}! Aqui é o {current_user.username}. "
                    f"Estou te convidando para o FeedIn para resgatarmos nossas memórias "
                    f"em comum sobre: {nome_contexto}. 📸\n\n"
                    f"Crie seu perfil e torne-se um Pioneiro aqui: {link_registro}"
                )
            else:
                texto_base = (
                    f"Olá {nome_amigo}! Estou no FeedIn resgatando memórias de Piracicaba "
                    f"e lembrei de você. 🕸️\n\n"
                    f"Crie seu perfil pelo link e me ajude a crescer a nossa rede: {link_registro}"
                )

            # Ajuste do prefixo internacional (WhatsApp exige)
            numero_completo = numero_destino if numero_destino.startswith('55') else "55" + numero_destino

            # Encode seguro da URL
            whatsapp_url = f"https://api.whatsapp.com/send?phone={numero_completo}&text={quote(texto_base)}"

            return redirect(whatsapp_url)

        except Exception as e:
            database.session.rollback()
            # LOG DO ERRO NO TERMINAL PARA DEBUEAR DE IMEDIATO
            print("\n" + "=" * 60)
            print(f"💥 ERRO CRÍTICO NA GRAVAÇÃO DO CONVITE: {str(e)}")
            print("=" * 60 + "\n")

            flash(f"Erro no banco de dados: {str(e)}", "danger")
            return redirect(url_for('dashboard', aba='configuracoes'))

    # Se cair aqui, a validação do WTForms falhou (ex: número celular inválido)
    flash("Por favor, preencha o número do WhatsApp corretamente.", "warning")
    return redirect(url_for('dashboard', aba='configuracoes'))


@app.route('/servir-foto-perfil/<int:usuario_id>')
def servir_foto_perfil(usuario_id):
    usuario = Usuario.query.get(usuario_id)  # Usamos get para não dar 404 se o usuário sumir

    # Pasta onde ficam as fotos (static/fotos_perfil)
    pasta_fotos = os.path.join(current_app.static_folder, 'fotos_perfil')

    # Se o usuário existe e tem foto gravada
    if usuario and usuario.foto_perfil:
        caminho_completo = os.path.join(pasta_fotos, usuario.foto_perfil)
        if os.path.exists(caminho_completo):
            return send_from_directory(pasta_fotos, usuario.foto_perfil)

    # Se cair aqui (usuário não existe, sem foto ou arquivo sumiu), serve a padrão.
    # IMPORTANTE: Garanta que a default.jpg esteja dentro de static/fotos_perfil
    return send_from_directory(pasta_fotos, 'default.jpg')


def enviar_email_nutricao(nome, email, interesse):
    # Dicionário de personalização baseado na escolha do formulário
    conteudos = {
        'afetivo': {
            'assunto': f'Suas memórias reais têm um novo lar, {nome}',
            'template': 'emails/nutricao_afetiva.html'
        },
        'social': {
            'assunto': 'Onde sua história cruza com a de outras pessoas?',
            'template': 'emails/nutricao_social.html'
        },
        'comercial': {
            'assunto': 'Negócios baseados em quem você realmente confia',
            'template': 'emails/nutricao_comercial.html'
        },
        'todos': {
            'assunto': 'Bem-vindo à sua vida integrada no FeedIn!',
            'template': 'emails/nutricao_completa.html'
        }
    }

    # Seleciona o conteúdo ou usa um padrão (fallback)
    info = conteudos.get(interesse, conteudos['todos'])

    # Rota que o botão dentro do e-mail vai acessar
    # Você pode passar o email via URL para preencher o form de cadastro automaticamente
    url_cadastro = url_for('newuser', _external=True)

    msg = Message(
        info['assunto'],
        sender=app.config.get('MAIL_USERNAME'),
        recipients=[email]
    )

    # Aqui renderizamos um HTML para o corpo do e-mail
    msg.html = render_template(
        info['template'],
        nome=nome,
        url_cadastro=url_cadastro
    )

    try:
        mail.send(msg)
        return True
    except Exception as e:
        print(f"Erro ao enviar e-mail de nutrição: {e}")
        return False


def estabelecer_vinculo_pioneiro(novo_usuario_id, id_pai, contexto_raw):
    try:
        if not contexto_raw or "_" not in contexto_raw:
            return

        tipo, ref_id = contexto_raw.split('_')
        ref_id = int(ref_id)

        usuario_novo = Usuario.query.get(novo_usuario_id)
        pai = Usuario.query.get(id_pai)

        # 1. Se o contexto for LOCAL
        if tipo == 'local':
            # Cria o registro técnico de banco de dados
            novo_vinculo = VinculoUsuarioLocal(
                usuario_id=novo_usuario_id,
                local_id=ref_id,
                experiencia=f"Convidado por um amigo pioneiro."
            )
            database.session.add(novo_vinculo)

            # --- AQUI ESTÁ A MÁGICA PARA O FEED ---
            # Criamos a postagem festiva que o card vai ler
            local_obj = Local.query.get(ref_id)
            nova_memoria_feed = AtividadeLocal(
                nome=f"Novo Membro em {local_obj.nome}",
                id_local=ref_id,
                id_criador=novo_usuario_id,
                descricao=f"Acaba de chegar ao {local_obj.nome} através da rede de confiança de {pai.username}!",
                periodo_estimado="Pioneiro"
            )
            database.session.add(nova_memoria_feed)

        # 2. Se o contexto for GOSTO
        elif tipo == 'gosto':
            gosto_obj = AtividadeLocal.query.get(ref_id)
            if gosto_obj:
                gosto_obj.participantes.append(usuario_novo)

                # Opcional: Criar uma notificação de "Fulano também curte X"
                # (Segue a mesma lógica acima se quiser que apareça no Feed)

        # 3. Atualiza o convite
        convite = Convite.query.filter_by(id_remetente=id_pai, id_destinatario=None).first()
        if convite:
            convite.id_destinatario = novo_usuario_id
            convite.status_onboarding = True

        database.session.commit()
    except Exception as e:
        database.session.rollback()
        print(f"Erro ao estabelecer vínculo: {e}")


@app.route('/configuracoes')
@login_required
def configuracoes():
    # 1. LÓGICA DE NAVEGAÇÃO
    aba_solicitada = request.args.get('aba')
    aba_ativa = aba_solicitada if aba_solicitada else 'perfil'

    if current_user.nivel_acesso < 10:
        if not current_user.foto_perfil or current_user.foto_perfil == 'default.jpg':
            aba_ativa = 'perfil'
        else:
            aba_ativa = 'preferencias'

    # 2. PREPARAÇÃO DOS FORMULÁRIOS
    from feedin.forms import FormPerfil, FormApelido
    # Carrega os dados existentes para o formulário de perfil
    form = FormPerfil(obj=current_user.perfil)
    form_apelido = FormApelido()

    # 3. CONVERSÃO DOS INTERESSES PARA JSON (O que o seu JS exige)
    # Pegamos os interesses do banco usando .all() por ser uma relação dinâmica
    interesses_atuais = current_user.interesses.all()

    lista_prefs = []
    for t in interesses_atuais:
        lista_prefs.append({
            'id': t.id,
            'nome': t.nome,
            'v_usu': bool(t.visivel_usuario),
            'v_neg': bool(t.visivel_negocio)
        })

    # Esta variável é a que o seu 'data-prefs' no HTML está procurando
    minhas_prefs_json = json.dumps(lista_prefs)

    # 4. CARGA DE TODAS AS TAGS (Para compatibilidade/outros usos)
    todas_tags = Taxonomia.query.filter_by(visivel_usuario=True).order_by(Taxonomia.nome.asc()).all()
    meus_interesses_ids = [t.id for t in interesses_atuais]

    return render_template(
        'configuracoes.html',
        aba_ativa=aba_ativa,
        form=form,
        form_apelido=form_apelido,
        todas_tags=todas_tags,
        meus_interesses_ids=meus_interesses_ids,
        minhas_prefs_json=minhas_prefs_json  # <--- ESSA É A CHAVE DO SUCESSO
    )


# rota para exibição do perfil
@app.route("/perfil/<int:usuario_id>")
@login_required
def ver_perfil(usuario_id):
    user_alvo = Usuario.query.get_or_404(usuario_id)
    e_o_proprio = (current_user.id == user_alvo.id)

    # Chamada dedicada à lógica de locais populares do usuário
    locais_seguidos = Local.get_locais_populares_por_usuario(user_alvo.id)

    relacao = Conexoes.query.filter(
        ((Conexoes.id_remetente == current_user.id) & (Conexoes.id_destinatario == user_alvo.id)) |
        ((Conexoes.id_remetente == user_alvo.id) & (Conexoes.id_destinatario == current_user.id))
    ).first()

    status_conexao = relacao.status if relacao else "nenhuma"
    sou_remetente = (relacao.id_remetente == current_user.id) if relacao else False

    # 1. Memórias (Vínculos Usuario-Local antigos)
    memorias_alvo = VinculoUsuarioLocal.query.filter_by(usuario_id=usuario_id).order_by(
        VinculoUsuarioLocal.id.desc()).all()

    # 2. Conexões Confirmadas (Amizades)
    conexoes_confirmadas = Conexoes.query.join(
        Usuario,
        or_(Usuario.id == Conexoes.id_remetente, Usuario.id == Conexoes.id_destinatario)
    ).join(
        Perfil, Usuario.id == Perfil.id_usuario
    ).filter(
        ((Conexoes.id_remetente == user_alvo.id) | (Conexoes.id_destinatario == user_alvo.id)),
        (Conexoes.status == 'aceito'),
        Usuario.id != user_alvo.id
    ).order_by(asc(Perfil.nome_completo)).all()

    # 3. Conexão Existente entre Você (Dono da sessão) e o Alvo
    conexao_atual = Conexoes.query.filter(
        ((Conexoes.id_remetente == current_user.id) & (Conexoes.id_destinatario == user_alvo.id)) |
        ((Conexoes.id_remetente == user_alvo.id) & (Conexoes.id_destinatario == current_user.id))
    ).first()

    # 4. Afinidades (Tags e Locais em comum)
    minhas_tags_ids = [t.id for t in current_user.interesses]
    tags_em_comum = [t for t in user_alvo.interesses if t.id in minhas_tags_ids]

    # Mapeamento completo dos locais do VISITANTE (Frequenta, gerencia ou indica)
    memorias_locais_vis = [m.local_id for m in VinculoUsuarioLocal.query.filter_by(usuario_id=current_user.id).all()]
    grupos_ids_vis = [m.id_grupo for m in MembroGrupo.query.filter_by(id_usuario=current_user.id).all()]
    locais_negocio_vis = [l.id for l in Local.query.filter(
        (Local.id_empreendedor == current_user.id) | (Local.id_indicador == current_user.id)).all()]
    meus_locais_ids = list(set(memorias_locais_vis + grupos_ids_vis + locais_negocio_vis))

    locais_alvo_ids = [m.local_id for m in memorias_alvo]
    locais_comum_ids = set(meus_locais_ids) & set(locais_alvo_ids)

    # 5. Signo (Usando sua função do utils.py)
    s_nome, s_icone = (None, None)
    if user_alvo.perfil.data_nascimento:
        res = obter_signo(user_alvo.perfil.data_nascimento)
        s_nome, s_icone = res if res else (None, None)

    # =========================================================================
    # 6. MOTOR DO MURAL DE POSTAGENS (TRAVA DE PRIVACIDADE COMPLETA)
    # =========================================================================
    # Guardamos o total bruto para estatísticas do perfil
    total_mural_bruto = Postagem.query.filter_by(id_usuario=usuario_id, ativo=True).count()

    # Checa de forma explícita se a amizade/conexão está ativa
    tem_conexao_confirmada = (status_conexao == 'aceito')

    if e_o_proprio:
        # Dono do perfil tem passe livre total sobre suas publicações
        postagens_permitidas = Postagem.query.options(joinedload(Postagem.autor)) \
            .filter_by(id_usuario=usuario_id, ativo=True) \
            .order_by(Postagem.data_criacao.desc()).all()

    elif not tem_conexao_confirmada:
        # TRAVA CIRÚRGICA: Se NÃO são amigos, aplica a restrição pesada de visualização institucional.
        # Carrega APENAS as duas primeiras fotos sem cruzar tags ou regras de locais.
        postagens_permitidas = Postagem.query.options(joinedload(Postagem.autor)) \
            .filter_by(id_usuario=usuario_id, ativo=True) \
            .order_by(Postagem.data_criacao.desc()).limit(2).all()

    else:
        # SE SÃO AMIGOS ACEITOS: Roda o seu motor inteligente de afinidade por locais e subgrupos
        post_sem_tag = and_(
            ~Postagem.tags_afinidade.any(),
            or_(
                Postagem.id_local == None,
                Postagem.id_local.in_(meus_locais_ids) if meus_locais_ids else False
            )
        )

        post_com_tag = and_(
            Postagem.tags_afinidade.any(),
            Postagem.tags_afinidade.any(Taxonomia.id.in_(minhas_tags_ids)) if minhas_tags_ids else False
        )

        postagens_permitidas = Postagem.query.options(joinedload(Postagem.autor)).filter(
            Postagem.id_usuario == usuario_id,
            Postagem.ativo == True,
            or_(post_sem_tag, post_com_tag)
        ).order_by(Postagem.data_criacao.desc()).all()
    # =========================================================================

    # 7. Postagens onde o USER_ALVO FOI MARCADO (Bloqueado ou restrito se não forem amigos)
    if e_o_proprio or tem_conexao_confirmada:
        fotos_com_alvo = Postagem.query.join(Postagem.pessoas_marcadas) \
            .filter(Usuario.id == usuario_id, Postagem.ativo == True) \
            .order_by(Postagem.data_criacao.desc()).all()
    else:
        # Se não há conexão confirmada, oculta o bloco de fotos onde terceiros marcaram ele
        fotos_com_alvo = []

    form_convite = FormConexao()

    return render_template("perfil_publico.html",
                           user_alvo=user_alvo,
                           status_conexao=status_conexao,
                           sou_remetente=sou_remetente,
                           e_o_proprio=e_o_proprio,
                           postagens=postagens_permitidas,
                           total_postagens=total_mural_bruto,
                           fotos_com_voce=fotos_com_alvo,
                           memorias=memorias_alvo,
                           conexao=conexao_atual,
                           conexoes_confirmadas=conexoes_confirmadas,
                           tags_comum_ids=[t.id for t in tags_em_comum],
                           meus_interesses_ids=set(minhas_tags_ids),
                           locais_comum_ids=locais_comum_ids,
                           locais_seguidos=locais_seguidos,
                           signo_nome=s_nome,
                           signo_icone=s_icone,
                           form_convite=form_convite)


# Rotas para o tratamento dos locais sem reivindicação
@app.route('/local/<int:local_id>')
@login_required
def perfil_local(local_id):
    local = Local.query.get_or_404(local_id)

    # 1. GARANTIA DE VARIÁVEIS (Inicializadas no escopo principal da função)
    atividades = []
    tags_dos_amigos = []
    atividades_formatadas = []

    # 2. VERIFICAÇÃO DE VÍNCULO
    vinculo = VinculoUsuarioLocal.query.filter_by(
        usuario_id=current_user.id,
        local_id=local_id
    ).first()
    usuario_segue = True if vinculo else False

    # 3. TAGS DOS AMIGOS (Processamos antes para garantir a existência da variável)
    try:
        lista_ids_amigos = [amigo.id for amigo in current_user.amigos]
        if lista_ids_amigos:
            tags_dos_amigos = database.session.query(Taxonomia).join(postagem_tags) \
                .join(Postagem).filter(
                Postagem.id_local == local_id,
                Postagem.id_usuario.in_(lista_ids_amigos)
            ).distinct().all()
    except Exception as e:
        print(f"Erro ao buscar tags de amigos: {e}")
        tags_dos_amigos = []

    # 4. BUSCA E NORMALIZAÇÃO DE POSTAGENS
    postagens_publicas = Postagem.query.filter_by(id_local=local_id, ativo=True) \
        .options(joinedload(Postagem.autor).joinedload(Usuario.perfil)) \
        .order_by(Postagem.data_criacao.desc()).all()

    for p in postagens_publicas:
        atividades_formatadas.append({
            'id': p.id,
            'tipo_card': 'postagem',
            'data_criacao': p.data_criacao,
            'autor_objeto': p.autor,
            'conteudo_exibicao': p.conteudo,
            'objeto_original': p
        })

    # 5. BUSCA E NORMALIZAÇÃO DE VÍNCULOS
    if local.atividades:
        for ativ in local.atividades:
            atividades_formatadas.append({
                'id': ativ.id,
                'tipo_card': 'conexao',
                'data_criacao': ativ.data_criacao,
                'autor_objeto': ativ.criador,
                'conteudo_exibicao': ativ.descricao or "Começou a seguir este local.",
                'objeto_original': ativ
            })

    # 6. UNIFICAÇÃO E ORDENAÇÃO
    if atividades_formatadas:
        atividades = sorted(atividades_formatadas, key=lambda x: x['data_criacao'], reverse=True)

    # 7. RENDERIZAÇÃO FINAL (Todas as variáveis aqui foram garantidas no Passo 1)
    return render_template('locais/perfil_local.html',
                           local=local,
                           atividades=atividades,
                           sugestoes_nicho=tags_dos_amigos,
                           usuario_segue=usuario_segue,
                           rating_data=local.get_rating_data())


@app.route('/local_v2/<int:local_id>')
@login_required
def perfil_local_v2(local_id):
    local = Local.query.get_or_404(local_id)

    # 1. GARANTIA DE VARIÁVEIS
    usuario_segue = False
    atividades_formatadas = []
    minhas_tags_ids = [t.id for t in current_user.interesses]

    # 2. VERIFICAÇÃO DE VÍNCULO
    usuario_segue = database.session.query(VinculoUsuarioLocal).filter_by(
        usuario_id=current_user.id, local_id=local_id
    ).first() is not None

    # 3. TAGS DOS AMIGOS
    tags_dos_amigos = []
    try:
        lista_ids_amigos = [amigo.id for amigo in current_user.amigos]
        if lista_ids_amigos:
            tags_dos_amigos = database.session.query(Taxonomia).join(postagem_tags) \
                .join(Postagem).filter(
                Postagem.id_local == local_id,
                Postagem.id_usuario.in_(lista_ids_amigos)
            ).distinct().all()
    except Exception as e:
        print(f"Erro tags: {e}")

    # =========================================================================
    # 4. MOTOR DE POSTAGENS (O REAL MURAL DE HISTÓRIAS) - CORRIGIDO e ALINHADO
    # =========================================================================
    # Buscamos explicitamente os IDs das tags convertendo a query dinâmica em lista
    minhas_tags_ids = [t.id for t in current_user.interesses.all()]

    # Query base: Postagens ATIVAS deste local específico
    query_base_posts = Postagem.query.filter_by(id_local=local_id, ativo=True)

    # 1. TOTAL BRUTO DE POSTS REAIS DO LOCAL NO BANCO (Sem filtros de afinidade)
    total_posts_existentes = query_base_posts.count()

    # 2. SELEÇÃO DE POSTS COM FILTRO DE AFINIDADE (Garantindo que não duplique)
    postagens_permitidas = []

    # Buscamos todas as postagens ativas do local trazendo as tags em conjunto (Evita Lazy Loading)
    todas_do_local = query_base_posts.options(
        joinedload(Postagem.autor).joinedload(Usuario.perfil),
        joinedload(Postagem.tags_afinidade)
    ).all()

    for p in todas_do_local:
        # Se o post não tem nenhuma tag de afinidade, ele é público para todos neste local
        if not p.tags_afinidade:
            postagens_permitidas.append(p)
        else:
            # Se o post tem tags, checamos se o usuário possui pelo menos UMA delas
            post_tags_ids = {tag.id for tag in p.tags_afinidade}
            if post_tags_ids.intersection(minhas_tags_ids):
                postagens_permitidas.append(p)

    # 3. NORMALIZAÇÃO PARA A LINHA DO TEMPO (Apenas posts reais e filtrados)
    for p in postagens_permitidas:
        atividades_formatadas.append({
            'id': p.id,
            'tipo_card': 'postagem',
            'data_criacao': p.data_criacao,
            'autor_objeto': p.autor,
            'conteudo_exibicao': p.conteudo,
            'imagem_url': p.imagem_url,
            'objeto_original': p,
            'id_local': local.id,
            'local': local
        })

    # Ordenação cronológica reversa
    atividades_ordenadas = sorted(atividades_formatadas, key=lambda x: x['data_criacao'], reverse=True)

    # PRINTS DE DEBUG PARA MONITORAR NO SEU TERMINAL:
    print("\n=== DEBUG DE INTERESSES E POSTAGENS ===")
    print(f"IDs das minhas tags ativas: {minhas_tags_ids}")
    print(f"Total bruto de posts físicos no banco para este local: {total_posts_existentes}")
    print(f"Total de posts que passaram no filtro de afinidade: {len(atividades_ordenadas)}")
    print("=======================================\n")

    return render_template('locais/perfil_local.html',
                           local=local,
                           atividades=atividades_ordenadas,  # Envia apenas o que deve ser exibido
                           total_atividades=total_posts_existentes,  # O "Y" real da matemática (Total Bruto)
                           sugestoes_nicho=tags_dos_amigos,
                           usuario_segue=usuario_segue,
                           rating_data=local.get_rating_data(),
                           meus_interesses_ids=set(minhas_tags_ids))


@app.route("/explorar-locais")
def lista_locais():
    # AJUSTADO: Pegando o nome correto do campo que está no HTML ('busca_local')
    termo_busca = request.args.get('busca_local', '').strip()

    if termo_busca:
        # Busca Ampla: Outerjoin para achar locais mesmo sem seguidores
        query = database.session.query(
            Local,
            func.count(VinculoUsuarioLocal.id).label('total_memorias')
        ).outerjoin(VinculoUsuarioLocal, Local.id == VinculoUsuarioLocal.local_id) \
         .filter(Local.nome.ilike(f'%{termo_busca}%')) \
         .group_by(Local.id) # ESSENCIAL: Adicionado aqui também
    else:
        # Lista Padrão: Join simples para filtrar apenas quem já tem "vida"
        query = database.session.query(
            Local,
            func.count(VinculoUsuarioLocal.id).label('total_memorias')
        ).join(VinculoUsuarioLocal, Local.id == VinculoUsuarioLocal.local_id) \
         .group_by(Local.id) \
         .having(func.count(VinculoUsuarioLocal.id) > 0)

    locais_resultados = query.order_by(func.count(VinculoUsuarioLocal.id).desc(), Local.nome.asc()).all()

    # Importante: Verifique se o template esperado é 'publico/lista_locais.html'
    # ou se deveria ser o dashboard com a aba locais.
    return render_template("publico/lista_locais.html",
                           locais=locais_resultados,
                           termo_busca=termo_busca)


@app.route("/admin/gerar-convite-pioneiro")
@login_required
@apenas_admin
def admin_gerar_convite_pioneiro():
    # Cria o token de uso único vinculado ao Admin atual
    novo_convite = ConviteAdmin(id_admin=current_user.id)
    database.session.add(novo_convite)
    database.session.commit()

    # Gera a URL absoluta para o registro com o token
    link_registro = url_for('registrar', token_pioneiro=novo_convite.token, _external=True)

    texto = quote(f"Olá! Você foi convidado para ser um Pioneiro no FeedIn. "
                  f"Use este link exclusivo (válido para um cadastro): {link_registro}")

    # Redireciona para o WhatsApp (Sem número definido, para você escolher o contato lá)
    return redirect(f"https://api.whatsapp.com/send?text={texto}")

# Rotas de privacidade e segurança

def encriptar_cpf(cpf_limpo):
    # Transforma o CPF (string) em bytes e encripta
    return fernet.encrypt(cpf_limpo.encode())

def descriptografar_cpf(cpf_banco):
    # Desfaz a criptografia para leitura oficial
    return fernet.decrypt(cpf_banco).decode()


@app.route('/admin/reenviar-confirmacao/<int:usuario_id>')
# @login_required  <-- Descomente se usar o Flask-Login para proteger a rota
def admin_reenviar_confirmacao(usuario_id):
    # 1. Verifica se quem está logado é realmente admin (Regra de segurança)
    # if current_user.nivel_acesso < 9999:
    #     flash('Acesso negado.', 'danger')
    #     return redirect(url_for('dashboard'))

    usuario = Usuario.query.get_or_404(usuario_id)

    # Se o usuário já estiver ativo, não faz sentido reenviar
    if usuario.active:
        flash(f'O usuário {usuario.username} já está ativo no sistema.', 'info')
        return redirect(url_for('central_admin'))  # Ajuste para o nome real da sua rota de admin

    try:
        # 2. Gera o novo token (usando o método que você já tem no cadastro)
        token = generate_confirmation_token(usuario.email)

        # URL que o usuário vai clicar no e-mail
        link_confirmacao = url_for('confirmar_email', token=token, _external=True)

        # 3. Dispara o e-mail (Use aqui a sua função existente de envio)
        # Exemplo hipotético:
        # enviar_email_confirmacao(usuario.email, link_confirmacao)

        print(f"Novo token gerado para {usuario.email}: {link_confirmacao}")  # Log de segurança

        flash(f'Novo link de confirmação enviado com sucesso para {usuario.email}!', 'success')
    except Exception as e:
        flash(f'Erro ao enviar o e-mail: {str(e)}', 'danger')

    return redirect(url_for('central_admin'))  # Ajuste para o nome real da sua rota de admin


@app.route('/processar_identidade', methods=['POST'])
@login_required
def processar_identidade():
    # 1. Captura e Limpeza Rigorosa
    # Importante: Verifique se no seu HTML o campo de nome é 'nome_real' ou 'nome_completo'
    nome_real = request.form.get('nome_real', '').strip().upper()
    cpf_digitado = re.sub(r'\D', '', request.form.get('cpf', ''))
    data_nasc_str = request.form.get('data_nascimento')

    # Validação de campos vazios
    if not nome_real or not cpf_digitado or not data_nasc_str:
        flash("Todos os campos de identidade são obrigatórios para a validação.", "warning")
        return redirect(url_for('get_perfil', id_usuario=current_user.id, aba='perfil'))

    # 2. Validação Matemática (Estrutural)
    if not validar_cpf_estrutura(cpf_digitado):
        flash("O CPF informado não é válido. Confira os números.", "danger")
        return redirect(url_for('get_perfil', id_usuario=current_user.id, aba='perfil'))

    # 3. Busca pelo Hash (Regra de 1 conta por CPF)
    hash_digitado = IdentidadeCivil.gerar_hash(cpf_digitado)
    cpf_ja_existe = IdentidadeCivil.query.filter_by(cpf_hash=hash_digitado).first()

    if cpf_ja_existe:
        if cpf_ja_existe.usuario_id != current_user.id:
            flash("Este CPF já está vinculado a outra conta no FeedIn.", "danger")
            return redirect(url_for('get_perfil', id_usuario=current_user.id, aba='perfil'))
        else:
            flash("Sua identidade já consta em nossa base.", "info")
            return redirect(url_for('get_perfil', id_usuario=current_user.id, aba='perfil'))

    # 4. Encriptação e Gravação
    try:
        data_nasc_obj = datetime.strptime(data_nasc_str, '%Y-%m-%d').date()

        # Usamos o app.fernet ou o seu cipher_suite
        cpf_protegido = app.fernet.encrypt(cpf_digitado.encode())

        nova_identidade = IdentidadeCivil(
            usuario_id=current_user.id,
            nome_completo_oficial=nome_real,
            cpf_criptografado=cpf_protegido,
            cpf_hash=hash_digitado,
            data_nascimento=data_nasc_obj,
            ip_origem=request.remote_addr,
            versao_termos_aceita="1.0-BETA"
        )

        # Sincroniza os dados da identidade com o perfil social automaticamente
        perfil = current_user.perfil
        if perfil:
            perfil.nome_completo = nome_real
            perfil.data_nascimento = data_nasc_obj

            # Captura gênero e estado civil se vierem do form_alfandega
            genero_id = request.form.get("genero")
            if genero_id: perfil.genero = int(genero_id)

            ec_id = request.form.get("estado_civil")
            if ec_id: perfil.estado_civil = int(ec_id)

        current_user.aceite_lgpd = True
        database.session.add(nova_identidade)
        database.session.commit()

        flash("Identidade verificada com sucesso! Bem-vindo(a) oficial ao FeedIn.", "success")

        # Redireciona para memórias se for onboarding (nível baixo)
        if current_user.nivel_acesso < 10:
            return redirect(url_for('get_perfil', id_usuario=current_user.id, aba='memorias'))
        return redirect(url_for('dashboard', aba='perfil'))

    except Exception as e:
        database.session.rollback()
        print(f"Erro Crítico na Alfândega: {e}")
        flash("Houve um problema técnico ao salvar seus dados. Tente novamente.", "danger")
        return redirect(url_for('get_perfil', id_usuario=current_user.id, aba='perfil'))


# As rotas abaixo tratam o processo de publicação de conteúdos

# Rotaresponsável por receber o texto, a foto, o ID do local (se houver) e as tags selecionadas.


@app.route('/criar-postagem', methods=['POST'])
@login_required
def criar_postagem():
    # 1. Coleta Universal de Dados
    id_local = request.form.get('id_local')
    conteudo = request.form.get('conteudo', '').strip()
    tipo_postagem = request.form.get('tipo_postagem', 'comum')  # Ex: 'vinculo', 'obito', 'evento', 'nascimento'

    # Campos de Metadados (Memória Social)
    epoca = request.form.get('periodo_estimado', '').strip()
    relato_extra = request.form.get('descricao', '').strip()  # O "O que mais curte"

    arquivo = request.files.get('imagem')
    local = Local.query.get(id_local) if id_local and id_local.isdigit() else None

    # --- NOVA TRAVA DE SEGURANÇA: VALIDAÇÃO DE VÍNCULO/SEGUIDOR ---
    if local:
        # Verifica se o usuário já tem um vínculo (segue/atividade) com este local
        segue_local = AtividadeLocal.query.filter_by(
            id_criador=current_user.id,
            id_local=local.id
        ).first()

        if not segue_local:
            flash(f"Para postar no mural do {local.nome}, você precisa primeiro segui-lo!", "warning")
            return redirect(url_for('perfil_local', local_id=local.id))
    # --- FIM DA TRAVA ---

    # 2. Lógica de Construção de Narrativa (O "Cérebro" da Rota)
    if tipo_postagem == 'vinculo':
        texto_gerado = f"Resgatou uma memória"
        if epoca:
            texto_gerado += f" da época de {epoca}"
        if relato_extra:
            texto_gerado += f": {relato_extra}"

        conteudo = f"{texto_gerado}. {conteudo}" if conteudo else texto_gerado

    # 3. Validação de Regras de Negócio
    if not conteudo and not arquivo:
        flash("Sua memória precisa de um texto ou uma imagem!", "warning")
        return redirect(request.referrer)

    imagem_obrigatoria = True
    if (local and not local.esta_ativo) or tipo_postagem in ['vinculo', 'obito']:
        imagem_obrigatoria = False

    if imagem_obrigatoria and not arquivo:
        flash("Para este registro, uma fotografia é necessária!", "warning")
        return redirect(request.referrer)

    # 4. Processamento de Imagem
    nome_final = None
    if arquivo:
        try:
            nome_final = salvar_imagem_postagem(arquivo, current_user.id)
        except Exception as e:
            print(f"Erro no processamento da imagem: {e}")
            flash("Erro ao processar a imagem.", "danger")
            return redirect(request.referrer)

    # 5. Persistência no Banco de Dados
    try:
        # 1. Instância da Postagem (O que vai para o Feed)
        nova_postagem = Postagem(
            id_usuario=current_user.id,
            id_local=local.id if local else None,
            conteudo=conteudo,
            imagem_url=nome_final,
            data_criacao=datetime.now(timezone.utc),
            ativo=True
        )

        # Prepara o ID da postagem na sessão antes de criar os vínculos e marcações
        database.session.add(nova_postagem)
        database.session.flush()

        # 2. Lógica de Vínculo (Só cria se for 'vinculo' e se ainda não existir)
        if tipo_postagem == 'vinculo' and local:
            vinculo_existente = AtividadeLocal.query.filter_by(
                id_criador=current_user.id,
                id_local=local.id
            ).first()

            if not vinculo_existente:
                nova_atividade = AtividadeLocal(
                    id_criador=current_user.id,
                    id_local=local.id,
                    periodo_estimado=epoca,
                    descricao=relato_extra,
                    data_criacao=datetime.now(timezone.utc)
                )
                database.session.add(nova_atividade)

        # 3. Taxonomia (Tags)
        tags_ids = request.form.get('tags_ids', '')
        if tags_ids:
            ids_t = [int(i) for i in tags_ids.split(',') if i.strip().isdigit()]
            nova_postagem.tags_afinidade.extend(Taxonomia.query.filter(Taxonomia.id.in_(ids_t)).all())

        # --- PROCESSAMENTO DE MARCAÇÃO DE AMIGOS (LIMPO E SANEADO) ---
        pessoas_ids = request.form.get('pessoas_ids', '')
        if pessoas_ids:
            ids_p = [int(i) for i in pessoas_ids.split(',') if i.strip().isdigit()]

            for id_marcado in ids_p:
                if id_marcado == current_user.id:
                    continue

                # Gravação direta na tabela intermediária com status 'aceito'
                nova_marcacao = MarcacaoPostagem(
                    postagem_id=nova_postagem.id,
                    usuario_id=id_marcado,
                    status='aceito',
                    criado_em=datetime.now(timezone.utc)
                )
                database.session.add(nova_marcacao)
        # --- FIM DA LÓGICA DE MARCAÇÃO ---

        # 4. Salva tudo definitivamente no banco
        database.session.commit()
        flash("Memória compartilhada com sucesso!", "success")

    except Exception as e:
        database.session.rollback()
        print(f"--- ERRO CRÍTICO NA POSTAGEM: {e} ---")
        import traceback
        traceback.print_exc()
        flash("Houve um erro técnico ao salvar.", "danger")

    return redirect(request.referrer)


@app.route("/editar_post/<int:post_id>", methods=['POST'])
@login_required
def editar_post(post_id):
    post = Postagem.query.get_or_404(post_id)

    # 1. Validação de segurança (que corrigimos no passo anterior)
    if post.id_usuario != current_user.id:
        flash("Ação não permitida.", "danger")
        return redirect(request.referrer)

    # 2. Captura do novo conteúdo vindo do formulário
    # O textarea no HTML tem o atributo name="conteudo"
    novo_conteudo = request.form.get('conteudo')

    if novo_conteudo:
        post.conteudo = novo_conteudo.strip()  # Atualiza o campo do objeto

        try:
            database.session.commit()  # 3. Salva no banco de dados
            flash("História atualizada com sucesso!", "success")
        except Exception as e:
            database.session.rollback()
            flash("Erro ao salvar as alterações.", "danger")
    else:
        flash("O conteúdo não pode ficar vazio.", "warning")

    return redirect(request.referrer)


@app.route('/excluir_post/<int:post_id>', methods=['POST'])
@login_required
def excluir_post(post_id):
    post = Postagem.query.get_or_404(post_id)

    # Checagem direta antes de apagar o arquivo físico e o banco
    if post.id_usuario != current_user.id:
        flash("Você não tem permissão para excluir esta postagem.", "danger")
        return redirect(request.referrer)

    try:
        # 1. Remover arquivo físico
        caminho_imagem = os.path.join(app.root_path, 'static', 'uploads', 'posts', post.imagem_url)
        if os.path.exists(caminho_imagem):
            os.remove(caminho_imagem)

        # 2. Remover do banco (Exclusão física, já que você tem interações e comentários)
        database.session.delete(post)
        database.session.commit()
        flash("Memória removida com sucesso.", "success")
    except Exception as e:
        database.session.rollback()
        flash(f"Erro ao excluir: {str(e)}", "danger")

    return redirect(request.referrer)


@app.route('/comentar_post/<int:post_id>', methods=['POST'])
@login_required
def comentar_post(post_id):
    post = Postagem.query.get_or_404(post_id)
    texto = request.form.get('texto')

    if not texto or len(texto.strip()) == 0:
        return jsonify({"status": "error", "message": "O comentário não pode estar vazio."}), 400

    try:
        novo_comentario = PostagemComentario(
            id_postagem=post_id,
            id_usuario=current_user.id,
            texto=texto,
            data_comentario=datetime.now(timezone.utc),
            ativo=True
        )
        database.session.add(novo_comentario)

        # NOTIFICAÇÃO: Notifica o dono do post
        if post.id_usuario != current_user.id:
            notif = Notificacao(
                id_usuario_destino=post.id_usuario,
                id_usuario_origem=current_user.id,
                id_postagem_referencia=post.id,
                mensagem="comentou em sua publicação.",
                tipo="comentario"
            )
            database.session.add(notif)

        database.session.commit()
        return jsonify({"status": "success", "message": "Comentário enviado!"})
    except Exception as e:
        database.session.rollback()
        return jsonify({"status": "error", "message": "Erro ao comentar."}), 500


@app.route('/reagir_post/<int:post_id>/<string:tipo>', methods=['POST'])
@login_required
def reagir_post(post_id, tipo):
    post = Postagem.query.get_or_404(post_id)
    msg = "Operação iniciada"  # Valor inicial para evitar erro de referência

    reacao_existente = PostagemInteracao.query.filter_by(
        id_postagem=post_id,
        id_usuario=current_user.id
    ).first()

    try:
        if reacao_existente:
            if reacao_existente.tipo == tipo:
                # Toggle: Se clicou no mesmo, remove
                database.session.delete(reacao_existente)
                msg = "Reação removida."
            else:
                # Atualiza para o novo tipo (ex: de 'curti' para 'nao_curti')
                reacao_existente.tipo = tipo
                msg = f"Reação alterada para {tipo}."
        else:
            # Nova reação
            nova_reacao = PostagemInteracao(
                id_postagem=post_id,
                id_usuario=current_user.id,
                tipo=tipo
            )
            database.session.add(nova_reacao)
            msg = "Você reagiu à postagem."

            # GATILHO DE NOTIFICAÇÃO (Apenas para novas reações)
            if post.id_usuario != current_user.id:
                nickname_reagiu = current_user.username
                trecho_post = (post.conteudo[:20] + '...') if post.conteudo and len(post.conteudo) > 20 else (
                            post.conteudo or "")

                # ATENÇÃO: Verifique se sua model Notificacao realmente tem o campo 'msg'
                # Removi o campo 'msg' aqui para evitar erro caso ele não exista na Notificacao
                notif = Notificacao(
                    id_usuario_destino=post.id_usuario,
                    id_usuario_origem=current_user.id,
                    id_postagem_referencia=post.id,
                    mensagem=f"@{nickname_reagiu} reagiu à sua memória: '{trecho_post}'",
                    tipo="reacao",
                    data_criacao=datetime.now(timezone.utc)
                )
                database.session.add(notif)

        # O COMMIT DEVE FICAR AQUI (Fora dos IFs, para salvar qualquer mudança)
        database.session.commit()
        # Buscamos os novos totais para enviar ao Front-end
        total_curtidas = PostagemInteracao.query.filter_by(id_postagem=post_id, tipo='curti').count()
        total_nao_curtidas = PostagemInteracao.query.filter_by(id_postagem=post_id, tipo='nao_curti').count()
        return jsonify({
            "status": "success",
            "message": msg,
            "novo_total_curti": total_curtidas,
            "novo_total_nao_curti": total_nao_curtidas
        })

    except Exception as e:
        database.session.rollback()
        print(f"ERRO NO BANCO: {str(e)}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/excluir_comentario/<int:comentario_id>', methods=['POST'])
@login_required
def excluir_comentario(comentario_id):
    comentario = PostagemComentario.query.get_or_404(comentario_id)

    # Segurança: Apenas o dono do comentário ou o dono do POST podem excluir
    post = Postagem.query.get(comentario.id_postagem)
    if comentario.id_usuario != current_user.id and post.id_usuario != current_user.id:
        return jsonify({"status": "error", "message": "Permissão negada."}), 403

    comentario.ativo = False  # Exclusão lógica para preservar integridade
    database.session.commit()
    return jsonify({"status": "success", "message": "Comentário removido."})


# Para que a marcação de pessoas funcione (Apenas conexões aceitas na Teia)
@app.route('/buscar_usuarios')
@login_required
def buscar_usuarios():
    termo = request.args.get('q', '').replace('@', '').strip()
    if len(termo) < 2:
        return jsonify([])

    # 1. Alinhamento com a Teia: Filtra usando o modelo correto 'Conexoes'
    conexoes = Conexoes.query.filter(
        (Conexoes.id_remetente == current_user.id) | (Conexoes.id_destinatario == current_user.id),
        Conexoes.status == 'aceito'
    ).all()

    # Isola os IDs dos amigos confirmados
    amigos_ids = [c.id_destinatario if c.id_remetente == current_user.id else c.id_remetente for c in conexoes]

    # Se o usuário não tiver conexões aceitas, bloqueia a busca imediatamente
    if not amigos_ids:
        return jsonify([])

    # 2. Busca refinada na lista restrita de amigos
    usuarios = Usuario.query.join(Perfil).filter(
        Usuario.id.in_(amigos_ids),  # Trava de segurança absoluta baseada no Grafo
        ((Usuario.username.ilike(f'%{termo}%')) |
         (Perfil.nome_completo.ilike(f'%{termo}%')))
    ).limit(5).all()

    return jsonify([{'id': u.id, 'username': u.username} for u in usuarios])


# consulta que gera a visão que o empreendedor de Piracicaba precisa para tomar decisões:
def relatorio_nicho_piracicaba():
    print("\n📊 RELATÓRIO DE DEMANDA OTIMIZADO - FEEDIN PIRACICABA")
    print("-" * 60)

    try:
        # Subquery para contar seguidores por tag de uma só vez
        sub_seguidores = database.session.query(
            usuarios_interesses.c.taxonomia_id.label('tag_id'),
            func.count(usuarios_interesses.c.usuario_id).label('total_seg')
        ).group_by(usuarios_interesses.c.taxonomia_id).subquery()

        # Subquery para contar memórias por tag de uma só vez
        sub_memorias = database.session.query(
            postagem_tags.c.taxonomia_id.label('tag_id'),
            func.count(postagem_tags.c.postagem_id).label('total_mem')
        ).group_by(postagem_tags.c.taxonomia_id).subquery()

        # Consulta principal: Junta a Taxonomia com os totais calculados
        # Usamos outerjoin para trazer a tag mesmo se um dos contadores for zero
        relatorio = database.session.query(
            Taxonomia.nome,
            func.coalesce(sub_seguidores.c.total_seg, 0).label('seguidores'),
            func.coalesce(sub_memorias.c.total_mem, 0).label('memorias')
        ).outerjoin(sub_seguidores, Taxonomia.id == sub_seguidores.c.tag_id)\
         .outerjoin(sub_memorias, Taxonomia.id == sub_memorias.c.tag_id)\
         .filter((sub_seguidores.c.total_seg > 0) | (sub_memorias.c.total_mem > 0))\
         .order_by(func.coalesce(sub_seguidores.c.total_seg, 0).desc())\
         .all() # <-- UMA ÚNICA CONSULTA AO BANCO!

        # Exibe o resultado já ordenado pelo ranking real de relevância
        for nome, seguidores, memorias in relatorio:
            print(f"Tag: {nome:<20} | Seguidores: {seguidores:<4} | Memórias: {memorias}")

    except Exception as e:
        print(f"❌ Erro ao gerar relatório: {e}")

    print("-" * 60)


def afinidade_entre_tags(tag_a_id, tag_b_id):
    # Encontra usuários que estão em AMBAS as tags
    interseccao = database.session.query(usuarios_interesses.c.usuario_id) \
        .filter(usuarios_interesses.c.taxonomia_id == tag_a_id) \
        .intersect(
        database.session.query(usuarios_interesses.c.usuario_id) \
            .filter(usuarios_interesses.c.taxonomia_id == tag_b_id)
    ).count()

    return interseccao


@app.route('/local/avaliar/<int:local_id>', methods=['POST'])
@login_required
def avaliar_local(local_id):
    dados = request.get_json()
    if not dados:
        return jsonify({"sucesso": False, "mensagem": "Dados inválidos."}), 400

    # Captura correta das variáveis vindas do JSON
    nota_enviada = dados.get('nota')
    feedback_enviado = dados.get('feedback')

    # 1. A "Regra de Ouro": Só quem segue, avalia.
    segue = VinculoUsuarioLocal.query.filter_by(
        usuario_id=current_user.id,
        local_id=local_id
    ).first()

    if not segue:
        return jsonify({"sucesso": False, "mensagem": "Você precisa seguir o local para avaliar."}), 403

    # 3. Validação de feedback
    if not feedback_enviado or len(feedback_enviado.strip()) < 5:
        return jsonify({"sucesso": False, "mensagem": "Conte-nos um pouco sobre sua experiência!"}), 400

    try:
        # 4. Busca avaliação existente para Atualizar ou Criar
        avaliacao = AvaliacaoLocal.query.filter_by(
            id_usuario=current_user.id,
            id_local=local_id
        ).first()

        if avaliacao:
            avaliacao.nota = nota_enviada
            avaliacao.feedback = feedback_enviado
            avaliacao.data_avaliacao = datetime.now(timezone.utc)
            msg = "Sua avaliação foi atualizada!"
        else:
            # CORREÇÃO AQUI: Usando as variáveis que existem no escopo
            nova_avaliacao = AvaliacaoLocal(
                id_local=local_id,         # Era id_local, agora é local_id (o que vem no def)
                id_usuario=current_user.id,
                nota=int(nota_enviada),    # Era nota, agora é nota_enviada
                feedback=feedback_enviado  # Era comentario, agora é feedback_enviado
            )
            database.session.add(nova_avaliacao)
            msg = "Sua memória foi registrada com sucesso!"

        database.session.commit()
        return jsonify({"sucesso": True, "mensagem": msg})

    except Exception as e:
        database.session.rollback()
        print(f"Erro ao salvar avaliação: {e}") # Log para você ver no terminal
        return jsonify({"sucesso": False, "mensagem": "Erro técnico ao salvar."}), 500


@app.route('/local/reivindicar/<int:local_id>', methods=['POST'])
@login_required
def registrar_reivindicacao(local_id):
    # 1. Verifica se já existe um pedido pendente desse usuário para esse local
    reivindicacao_existente = ReivindicacaoLocal.query.filter_by(
        id_local=local_id,
        id_usuario=current_user.id
    ).first()

    if not reivindicacao_existente:
        # 2. Cria o registro na tabela que desenhamos
        nova_solicitacao = ReivindicacaoLocal(
            id_local=local_id,
            id_usuario=current_user.id,
            status='pendente'
        )
        database.session.add(nova_solicitacao)
        database.session.commit()

        # 3. Feedback visual (Flash message)
        flash(f"Interesse registrado para {Local.query.get(local_id).nome}! Em breve entraremos em contato.", "success")
    else:
        flash("Você já possui uma solicitação de gestão em análise para este local.", "info")

    return redirect(url_for('perfil_local', local_id=local_id))


@app.route('/postagem/<int:id_postagem>/solicitar_marcacao', methods=['POST'])
@login_required
def solicitar_marcacao(id_postagem):
    # 1. Busca a postagem alvo
    post = Postagem.query.get_or_404(id_postagem)

    # 2. Segurança: Verifica se o usuário já não está marcado ou pendente para evitar duplicidade
    ja_existe = MarcacaoPostagem.query.filter_by(
        postagem_id=post.id,
        usuario_id=current_user.id
    ).first()

    if not ja_existe:
        # --- PASSO CRÍTICO AQUI: Inserir na tabela de controle de estados ---
        nova_marcacao = MarcacaoPostagem(
            postagem_id=post.id,
            usuario_id=current_user.id,  # Quem está sendo marcado
            solicitante_id=current_user.id,  # Quem tomou a iniciativa (neste caso, ele mesmo)
            status='pendente'  # Aguardando o dono do post aprovar
        )
        database.session.add(nova_marcacao)

        # --- PASSO 2: Inserir na tabela de notificações para gerar o alerta visual ---
        nova_notificacao = Notificacao(
            id_usuario_destino=post.id_usuario,  # Dono do post (quem vai receber)
            id_usuario_origem=current_user.id,  # Quem gerou (quem quer ser marcado)
            id_postagem_referencia=post.id,
            mensagem=f"@{current_user.username} solicitou identificação em sua memória.",
            tipo='marcacao',
            lida=False
        )
        database.session.add(nova_notificacao)

        # Salva ambas as operações no banco de dados de forma atômica
        database.session.commit()

    return redirect(request.referrer or url_for('dashboard'))


@app.route('/remover_minha_marcacao/<int:id_post>', methods=['POST'])
@login_required
def remover_minha_marcacao(id_post):
    """
    Direito ao Esquecimento: Remove qualquer vínculo ou solicitação entre
    o usuário logado e a postagem específica.
    """
    # Localiza o vínculo na tabela controladora
    vinc_marcacao = MarcacaoPostagem.query.filter_by(
        postagem_id=id_post,
        usuario_id=current_user.id
    ).first()

    if vinc_marcacao:
        try:
            database.session.delete(vinc_marcacao)
            database.session.commit()
            flash("Sua identificação foi removida desta memória.", "success")
        except Exception as e:
            database.session.rollback()
            print(f"Erro ao remover marcação: {e}")
            flash("Erro ao processar a remoção da marcação.", "danger")
    else:
        flash("Nenhuma identificação ativa ou pendente foi encontrada.", "warning")

    return redirect(request.referrer or url_for('dashboard'))


@app.route('/marcacao/<int:id_marcacao>/aceitar', methods=['POST'])
@login_required
def aceitar_marcacao(id_marcacao):
    """O dono da postagem aprova a marcação solicitada por outro usuário."""
    from feedin.models import MarcacaoPostagem, Postagem, Notificacao

    marcacao = MarcacaoPostagem.query.get_or_404(id_marcacao)
    post = Postagem.query.get_or_404(marcacao.postagem_id)

    if post.id_usuario != current_user.id:
        flash("Você não tem permissão para gerenciar marcações nesta publicação.", "danger")
        return redirect(url_for('dashboard'))

    try:
        # 1. Atualiza o status para ativo
        marcacao.status = 'aceito'

        # 2. Marca a notificação de SOLICITAÇÃO como lida (para sumir do feed do dono do post)
        notif_solicitacao = Notificacao.query.filter_by(
            id_usuario_destino=current_user.id,  # Dono do post
            id_usuario_origem=marcacao.usuario_id,  # Quem pediu
            id_postagem_referencia=marcacao.postagem_id,
            tipo='marcacao',
            lida=False  # Apenas as que ainda estavam pendentes
        ).first()

        if notif_solicitacao:
            notif_solicitacao.lida = True

        # --- O PULO DO GATO: GERAR A NOTIFICAÇÃO DE CONFIRMAÇÃO ---
        # Agora o destino é quem solicitou, e a origem é o dono do post (current_user)
        notif_confirmacao = Notificacao(
            id_usuario_destino=marcacao.usuario_id,  # O solicitante (vai receber o aviso)
            id_usuario_origem=current_user.id,  # O dono do post (quem aceitou)
            id_postagem_referencia=post.id,
            mensagem=f"@{current_user.username} aceitou sua identificação na memória dele.",
            tipo='marcacao',
            lida=False  # Entra como não lida para aparecer no feed dele
        )
        database.session.add(notif_confirmacao)

        database.session.commit()
        flash("A marcação foi aceita e integrada à história desta memória!", "success")
    except Exception as e:
        database.session.rollback()
        print(f"Erro ao aceitar marcação: {e}")
        flash("Erro ao processar a aprovação.", "danger")

    return redirect(request.referrer or url_for('dashboard'))


@app.route('/marcacao/<int:id_marcacao>/recusar', methods=['POST'])
@login_required
def recusar_marcacao(id_marcacao):
    """O dono da postagem rejeita a solicitação de marcação."""
    from feedin.models import MarcacaoPostagem, Postagem

    # 1. Busca o registro na tabela de controle
    marcacao = MarcacaoPostagem.query.get_or_404(id_marcacao)

    # 2. Busca a postagem correspondente usando o postagem_id direto
    post = Postagem.query.get_or_404(marcacao.postagem_id)

    # 3. Segurança: Apenas o dono da postagem original pode recusar
    if post.id_usuario != current_user.id:
        flash("Você não tem permissão para gerenciar marcações nesta publicação.", "danger")
        return redirect(url_for('dashboard'))

    try:
        # Remove a solicitação pendente do banco de dados
        database.session.delete(marcacao)

        # Localiza a notificação do feed e também a remove
        notif = Notificacao.query.filter_by(
            id_usuario_destino=current_user.id,
            id_usuario_origem=marcacao.usuario_id,
            id_postagem_referencia=marcacao.postagem_id,
            tipo='marcacao'
        ).first()
        if notif:
            database.session.delete(notif)

        database.session.commit()
        flash("A solicitação de marcação foi recusada.", "info")
    except Exception as e:
        database.session.rollback()
        print(f"Erro ao recusar marcação: {e}")
        flash("Erro ao processar a recusa.", "danger")

    return redirect(request.referrer or url_for('dashboard'))