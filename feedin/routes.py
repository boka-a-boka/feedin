from flask import (url_for, redirect, render_template, flash, session, request,
                   abort, Response, jsonify, current_app, send_from_directory)
from feedin import app, database, bcrypt
from flask_mail import Mail, Message
from flask_login import login_required, login_user, logout_user, current_user
from feedin.forms import FormLogin, FormNewUser, FormPerfil, FormApelido, FormConvite, FormConexao
from itsdangerous import URLSafeTimedSerializer
from datetime import datetime, timezone, date
from feedin.models import (Usuario, EstadoCivil, Generos, Apelidos, Perfil, Parentesco,
                           GrauParentesco, MembroGrupo, GrupoSocial, Local, Conexoes, Memoria,
                           AtividadeLocal, VinculoUsuarioLocal, Taxonomia, LocalMidia, Convite,
                           taxonomia_conexoes, ConviteAdmin, IdentidadeCivil, Postagem, PostagemComentario,
                           PostagemInteracao, postagem_tags, usuarios_interesses, ReivindicacaoLocal,
                           AvaliacaoLocal, Notificacao)

from feedin.utils import salvar_imagem, processar_mudanca_nivel, obter_signo, validar_cpf_estrutura
from werkzeug.utils import secure_filename
from urllib.parse import quote
from functools import wraps
from sqlalchemy import or_, func, asc, and_
from sqlalchemy.orm import joinedload
from cryptography.fernet import Fernet  # Para criptografia reversível
from PIL import Image

import secrets, os, re, io, csv, json, pytz, uuid

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

    return render_template("login.html", form=form_login)


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


@app.route("/editar-perfil", methods=["POST"])
@login_required
def editar_perfil():
    # Chamamos o formulário para validar os dados que vieram do HTML
    form = FormPerfil()

    # Precisamos carregar as escolhas dos Selects para a validação não falhar
    form.genero.choices = [(g.id, g.genero) for g in Generos.query.all()]
    form.estado_civil.choices = [(e.id, e.estado_civil) for e in EstadoCivil.query.all()]

    if form.validate_on_submit():
        try:
            perfil = current_user.perfil

            # 1. ATUALIZAÇÃO DOS DADOS SOCIAIS (Usando os dados validados do Form)
            perfil.nome_completo = form.nome_completo.data
            perfil.data_nascimento = form.data_nascimento.data
            perfil.cidade_natal = form.cidade_natal.data
            perfil.genero = form.genero.data
            perfil.estado_civil = form.estado_civil.data
            perfil.biografia = form.biografia.data

            # 2. TRATAMENTO DA IDENTIDADE CIVIL (SÓ SE AINDA NÃO ACEITOU LGPD)
            if not current_user.aceite_lgpd:
                # O CPF não está no FormPerfil, então pegamos direto do formulário HTML
                cpf_bruto = request.form.get("cpf")
                if cpf_bruto:
                    cpf_digitado = re.sub(r'\D', '', cpf_bruto)
                    cpf_protegido = app.fernet.encrypt(cpf_digitado.encode())

                    nova_id = IdentidadeCivil(
                        usuario_id=current_user.id,
                        nome_completo_oficial=perfil.nome_completo.upper(),
                        cpf_criptografado=cpf_protegido,
                        data_nascimento=perfil.data_nascimento,
                        ip_origem=request.remote_addr,
                        versao_termos_aceita="1.0-BETA"
                    )
                    database.session.add(nova_id)
                    current_user.aceite_lgpd = True

            database.session.commit()
            flash("Perfil atualizado com sucesso!", "success")

            # Se for Onboarding, mantém o fluxo original
            if current_user.nivel_acesso < 10:
                return redirect(url_for('get_perfil', id_usuario=current_user.id, aba='memorias'))

            # SE FOR GESTÃO (Usuário já ativo), volta para a aba de perfil nas configurações
            return redirect(url_for('configuracoes', aba='perfil'))


        except Exception as e:
            database.session.rollback()
            print(f"ERRO CRÍTICO NO COMMIT: {e}")
            flash(f"Erro técnico ao salvar. Tente novamente.", "danger")
            return redirect(url_for('configuracoes', aba='perfil'))

    # Se o formulário NÃO validar (algum campo obrigatório vazio ou formato errado)
    print(f"ERROS WTFORMS: {form.errors}")
    flash("Por favor, verifique os campos destacados e tente novamente.", "warning")
    return redirect(request.referrer or url_for('get_perfil', id_usuario=current_user.id))
    return redirect(url_for('configuracoes', aba='perfil'))


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
        # 1. Gera nome seguro
        nome_arquivo = f"capa_{current_user.id}_{int(datetime.now().timestamp())}.jpg"
        caminho = os.path.join(app.config['UPLOAD_FOLDER_CAPAS'], nome_arquivo)

        # 2. Salva e atualiza banco
        arquivo.save(caminho)
        current_user.perfil.url_capa = nome_arquivo
        database.session.commit()

        flash("Capa atualizada!", "success")
    return redirect(url_for('configuracoes'))


@app.route("/dashboard")
@login_required
def dashboard():
    # --- 1. INICIALIZAÇÃO UNIVERSAL ---
    aba_solicitada = request.args.get('aba')
    atividades_recentes, meus_grupos = [], []
    convites_pendentes, meus_amigos = [], []
    total_pendentes = 0
    enviados_pendentes = []
    locais_populares = []  # Garante que 'locais' sempre exista para o template
    total_conexoes = 0  # Garante um valor inicial seguro

    # --- 2. COLETA DE DADOS INICIAIS ---
    perfil_usuario = current_user.perfil
    memorias_usuario = VinculoUsuarioLocal.query.filter_by(usuario_id=current_user.id).all()
    contagem_memorias = len(memorias_usuario)
    contagem_preferencias = current_user.interesses.count()

    # Buscamos as sugestões uma única vez
    lista_sugestoes = obter_sugestoes_pioneiras(current_user)

    categorias = Taxonomia.query.filter(~Taxonomia.contextos.any()).order_by(Taxonomia.nome).all()
    minhas_prefs_ids = [p.id for p in current_user.interesses]

    prefs_atuais_data = [
        {
            "id": p.id,
            "nome": p.nome,
            "contagem": p.contagem_uso or 0,
            'v_usu': bool(p.visivel_usuario),  # Importante!
            'v_neg': bool(p.visivel_negocio),  # Importante!
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

    # --- 4. LÓGICA DE DIRECIONAMENTO ---
    if current_user.nivel_acesso >= 10:
        aba = aba_solicitada if aba_solicitada else 'feed'
    else:
        if aba_solicitada:
            aba = aba_solicitada
        else:
            if not perfil_usuario or not perfil_usuario.nome_completo:
                aba = 'perfil'
            elif contagem_memorias < 1:
                aba = 'memorias'
            else:
                aba = 'preferencias'

    # --- 5. BUSCA DE DADOS SOCIAIS (Se qualificado) ---
    if onboarding_completo or current_user.nivel_acesso >= 10:
        meus_grupos = MembroGrupo.query.filter_by(id_usuario=current_user.id).all()
        convites_pendentes = Conexoes.query.filter_by(id_destinatario=current_user.id, status='pendente').all()
        total_pendentes = len(convites_pendentes)

        # Convites que EU enviei e estão aguardando
        enviados_pendentes = Conexoes.query.filter_by(
            id_remetente=current_user.id,
            status='pendente'
        ).order_by(Conexoes.data_solicitacao.desc()).all()

        if aba == 'feed':
            atividades_recentes = obter_atividades_feed(current_user)

        # AGORA SIM: Lógica de Minha Rede
        if aba == 'conexoes':


            # 1. Busca as conexões aceitas
            conexoes_aceitas = Conexoes.query.filter(
                ((Conexoes.id_remetente == current_user.id) | (Conexoes.id_destinatario == current_user.id)),
                (Conexoes.status == 'aceito')
            ).all()

            # 2. Prepara os IDs para comparação
            minhas_tags_ids = [t.id for t in current_user.interesses]
            meus_locais_ids = [m.local_id for m in memorias_usuario]

            for c in conexoes_aceitas:
                amigo = c.destinatario if c.id_remetente == current_user.id else c.remetente

                # Afinidade: Interesses
                amigo.total_prefs_comum = len([t for t in amigo.interesses if t.id in minhas_tags_ids])

                # Afinidade: Locais (Memórias)
                locais_amigo = [m.local_id for m in VinculoUsuarioLocal.query.filter_by(usuario_id=amigo.id).all()]
                amigo.total_locais_comum = len(set(meus_locais_ids) & set(locais_amigo))

                # Data de conexão
                # No loop: for c in conexoes_aceitas:
                amigo.data_conexao = c.data_aceite.strftime('%m/%Y') if c.data_aceite else "Recente"
                meus_amigos.append(amigo)

        # Cálculo do total para os cards do topo (Independente da aba)
        if aba == 'conexoes':
            total_conexoes = len(meus_amigos)
        else:
            total_conexoes = Conexoes.query.filter(
                ((Conexoes.id_remetente == current_user.id) | (Conexoes.id_destinatario == current_user.id)),
                (Conexoes.status == 'aceito')
            ).count()

        # Dentro da função dashboard...

        # --- AJUSTE NA ABA LOCAIS (COM BUSCA E AUTOCOMPLETE) ---
        if aba == 'locais':
            # Esqueça o termo_busca aqui. A lista da tela é fixa e elitista (só com seguidores).
            locais_populares = database.session.query(
                Local,
                func.count(VinculoUsuarioLocal.id).label('total')
            ).join(VinculoUsuarioLocal, Local.id == VinculoUsuarioLocal.local_id) \
                .group_by(Local.id) \
                .having(func.count(VinculoUsuarioLocal.id) > 0) \
                .order_by(func.count(VinculoUsuarioLocal.id).desc(), Local.nome.asc()) \
                .all()


    # --- 6. FORMULÁRIOS ---
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
                           sugestoes=lista_sugestoes,  # Passa a lista completa
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
    if len(termo) < 2:
        return jsonify([])

    try:
        # Buscamos o termo no nome (Independente de estar ativo ou não)
        # Mantemos apenas o filtro operacional para evitar exibir locais deletados/bloqueados
        locais = Local.query.filter(
            Local.nome.ilike(f'%{termo}%'),
            Local.status_operacional == 'ativo'
        ).limit(10).all()

        resultado = []
        for l in locais:
            # Se não está ativo, recebe o selo de Memória
            rotulo_memoria = " [MEMÓRIA HISTÓRICA]" if not l.esta_ativo else ""

            # Localização dinâmica: Prioriza Bairro + Cidade, mas aceita o que houver
            info_local = f"{l.bairro}, {l.cidade}" if l.bairro and l.cidade \
                else (l.cidade or l.bairro or "Localização preservada")

            resultado.append({
                'id': l.id,
                'nome': f"{l.nome}{rotulo_memoria}",
                'info_exibicao': f"em {info_local} (ID: {l.id})"  # Criamos uma string facilitadora
            })

        return jsonify(resultado)

    except Exception as e:
        # Se houver rabo para puxar, o log vai nos contar!
        print(f"DEBUG AUTOCOMPLETE: Erro inesperado -> {e}")
        return jsonify([]), 500


@app.route('/api/buscar-interesses-onboarding')
@login_required
def buscar_interesses_onboarding():
    try:
        termo = request.args.get('q', '').strip()
        if len(termo) < 2:
            return jsonify([])

        sugestoes = Taxonomia.query.filter(
            Taxonomia.nome.ilike(f'%{termo}%')
        ).limit(15).all()

        lista_final = []
        for t in sugestoes:
            # Buscamos o primeiro "Pai" (Contexto) se existir
            # Na sua model, 'contextos' é a lista de pais
            pai_direto = t.contextos[0] if t.contextos else None

            item = {
                'id': t.id,
                'nome': t.nome,
                'v_usu': bool(t.visivel_usuario),
                'v_neg': bool(t.visivel_negocio),
                'contagem': t.contagem_uso or 0,
                # Dados do Pai para o gatilho automático no JS
                'id_pai': pai_direto.id if pai_direto else None,
                'categoria_pai': pai_direto.nome if pai_direto else ''
            }
            lista_final.append(item)

        return jsonify(lista_final)

    except Exception as e:
        print(f"ERRO NA HIERARQUIA: {e}")
        return jsonify({"erro": str(e)}), 500


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

        # 4. Vínculo do Usuário (Membro do Grupo + Relato)
        vinculo_existente = MembroGrupo.query.filter_by(id_usuario=current_user.id, id_grupo=grupo.id).first()

        if not vinculo_existente:
            # Se for novo vínculo, salvamos também o relato/experiência se houver
            novo_vinculo = MembroGrupo(
                id_usuario=current_user.id,
                id_grupo=grupo.id,
                experiencia_usuario=experiencia_input  # Certifique-se que este campo existe no seu modelo MembroGrupo
            )
            database.session.add(novo_vinculo)
            database.session.commit()
            flash(f"'{nome_input}' registrado em {cidade_input} com sucesso!", "success")
        else:
            flash("Você já registrou esta memória neste período.", "info")

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
    conexao.data_confirmacao = datetime.utcnow()

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
    # 1. Pegar todos os Grupos que o Carlos (usuario_atual) frequenta
    meus_grupos_ids = [m.id_grupo for m in MembroGrupo.query.filter_by(id_usuario=usuario_atual.id).all()]

    # 2. Buscar amigos do Gilmar que estão nesses mesmos grupos
    # mas que ainda não são amigos do Carlos
    sugestoes = database.session.query(Usuario, GrupoSocial.nome) \
        .join(MembroGrupo, Usuario.id == MembroGrupo.id_usuario) \
        .join(GrupoSocial, MembroGrupo.id_grupo == GrupoSocial.id) \
        .join(Conexoes, (Usuario.id == Conexoes.id_destinatario) | (Usuario.id == Conexoes.id_remetente)) \
        .filter(MembroGrupo.id_grupo.in_(meus_grupos_ids)) \
        .filter(Usuario.id != usuario_atual.id) \
        .filter(Conexoes.status == 'aceito') \
        .filter(~Usuario.id.in_([a.id for a in usuario_atual.amigos])) \
        .distinct().all()
    return sugestoes

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
    # 1. Coleta os dados do formulário (que virão do card de sugestão ou perfil)
    categoria = request.form.get('categoria') # 'familia', 'social', 'profissional'
    id_referencia = request.form.get('id_referencia') # O "Gilmar" (fiador)
    id_contexto = request.form.get('id_contexto') # ID do Grupo ou Empresa
    id_parentesco = request.form.get('id_parentesco') # ID do Grau (se for família)

    # 2. Verifica se já existe uma conexão pendente ou aceita para evitar duplicidade
    existente = Conexoes.query.filter(
        ((Conexoes.id_remetente == current_user.id) & (Conexoes.id_destinatario == id_destinatario)) |
        ((Conexoes.id_remetente == id_destinatario) & (Conexoes.id_destinatario == current_user.id))
    ).first()

    if existente:
        flash("Já existe uma solicitação ou conexão com este usuário.", "info")
        return redirect(url_for('dashboard'))

    # 3. Cria a nova Conexão centralizada
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

    try:
        database.session.add(nova_conexao)
        database.session.commit()
        flash("Solicitação de conexão enviada com sucesso!", "success")
    except Exception as e:
        database.session.rollback()
        flash("Erro ao enviar solicitação.", "danger")

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

# ------------> Rotas para tratamento de ações administrativas
def apenas_admin(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated or current_user.nivel_acesso < 9999:
            abort(403)  # Proibido
        return f(*args, **kwargs)
    return decorated_function

# ------------> rota que renderiza a central e a rota específica que dispara o download do CSV da tabela "Locais".

@app.route("/admin/dashboard")
@login_required
@apenas_admin
def admin_sistema():
    # 1. Definimos os dados (stats) logo no início
    stats = {
        'usuarios': Usuario.query.count(),
        'pioneiros': Usuario.query.filter_by(is_pioneiro=True).count(),
        'taxonomia': Taxonomia.query.count(),
        'empreendedores': Usuario.query.filter_by(nivel_acesso=999).count()
    }

    # 2. Buscamos a lista de usuários
    usuarios_lista = Usuario.query.order_by(Usuario.id.desc()).limit(10).all()

    # 3. O return agora enxerga tanto o 'stats' quanto o 'usuarios_lista'
    return render_template("admin/dashboard.html", stats=stats, usuarios=usuarios_lista)


# Rota para deletar ou editar taxonomia (Exemplo de manutenção)
@app.route("/admin/taxonomia/delete/<int:id>")
@login_required
@apenas_admin
def admin_delete_taxonomia(id):
    termo = Taxonomia.query.get_or_404(id)
    database.session.delete(termo)
    database.session.commit()
    flash(f"Termo '{termo.nome}' removido com sucesso.", "success")
    return redirect(url_for('admin_dashboard'))

@app.route("/admin/exportar-locais")
@login_required
@apenas_admin
def admin_exportar_locais():
    locais = Local.query.all()

    # Gerar o CSV em memória para download imediato
    output = io.StringIO()
    escritor = csv.writer(output)

    escritor.writerow(['id', 'nome', 'categoria', 'logradouro', 'bairro', 'google_place_id'])

    for l in locais:
        escritor.writerow([l.id, l.nome, l.categoria, l.logradouro, l.bairro, l.google_place_id])

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
                status_operacional="ativo" if status_real else "encerrado",

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
        # O "Marco Zero" do usuário no sistema
        data_nascimento_sistema = usuario.data_cadastro

        # 1. Pegar conexões aceitas
        conexoes = Conexoes.query.filter(
            ((Conexoes.id_remetente == usuario.id) | (Conexoes.id_destinatario == usuario.id)),
            (Conexoes.status == 'aceito')
        ).all()

        mapa_amigos = {}
        for c in conexoes:
            amigo_id = c.id_destinatario if c.id_remetente == usuario.id else c.id_remetente
            mapa_amigos[amigo_id] = c.data_aceite

        # 2. Filtros de MEMÓRIAS
        filtros_memorias = [Memoria.id_usuario == usuario.id]
        for amigo_id, data_corte in mapa_amigos.items():
            if data_corte:
                filtros_memorias.append(and_(Memoria.id_usuario == amigo_id, Memoria.data_criacao >= data_corte))
        filtros_memorias.append(and_(Memoria.privacidade == 'publico', Memoria.data_criacao >= data_nascimento_sistema))

        # 3. Filtros de POSTAGENS
        filtros_postagens = [Postagem.id_usuario == usuario.id]
        for amigo_id, data_corte in mapa_amigos.items():
            if data_corte:
                filtros_postagens.append(and_(Postagem.id_usuario == amigo_id, Postagem.data_criacao >= data_corte))

        # 4. Execução das Queries
        lista_memorias = Memoria.query.options(
            joinedload(Memoria.autor),
            joinedload(Memoria.local)
        ).filter(or_(*filtros_memorias)).all()

        lista_postagens = Postagem.query.options(
            joinedload(Postagem.autor)
        ).filter(
            (Postagem.ativo == True) &
            (or_(*filtros_postagens))
        ).all()

        # 5. Unificação e Normalização
        todas_atividades = list(lista_memorias) + list(lista_postagens)

        for item in todas_atividades:
            # Foto básica
            item.url_foto_autor = url_for('servir_foto_perfil', usuario_id=item.id_usuario)

            if isinstance(item, Memoria):
                item.tipo = 'memoria'
                item.local_foco = item.local
                item.autor_objeto = item.autor
                # Blindagem de métodos para o footer
                setattr(item, 'usuario_ja_curtiu', item.usuario_ja_curtiu_memoria)
                setattr(item, 'total_curtidas', item.total_curtidas_memoria)

            elif isinstance(item, Postagem):
                item.tipo = 'postagem'
                item.local_foco = getattr(item, 'local', None)
                item.autor_objeto = getattr(item, 'autor', None) or getattr(item, 'usuario', None)
                # Funções de fallback seguras
                if not hasattr(item, 'usuario_ja_curtiu'):
                    setattr(item, 'usuario_ja_curtiu', lambda x: False)
                if not hasattr(item, 'total_curtidas'):
                    setattr(item, 'total_curtidas', 0)

        # 6. Ordenação final
        todas_atividades.sort(key=lambda x: x.data_criacao, reverse=True)
        return todas_atividades[:30]

    except Exception as e:
        print(f"Erro no feed: {e}")
        return [] # Se der qualquer erro, retorna lista vazia para o HTML não explodir


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


@app.route('/salvar_preferencias', methods=['POST','GET'])  # Só POST já resolve aqui
@login_required
def salvar_preferencias():
    # O HTML moderno envia uma string como "1,2,55,104"
    ids_raw = request.form.get('preferencias_ids', '')
    novos_termos_raw = request.form.get('novos_termos', '')  # Para sugestões novas

    # Converte a string de IDs em uma lista de inteiros
    ids_selecionados = [int(id) for id in ids_raw.split(',') if id.strip()]

    try:
        # 1. Limpa interesses antigos
        current_user.interesses_escolhidos = []

        # 2. Adiciona as tags existentes
        for tid in ids_selecionados:
            tag = Taxonomia.query.get(tid)
            if tag:
                current_user.interesses_escolhidos.append(tag)
                tag.contagem_uso = (tag.contagem_uso or 0) + 1

        # 3. Lógica do nível de acesso (Pioneiro)
        # Verificamos o total ATUAL de interesses salvos
        total_interesses = len(current_user.interesses_escolhidos)

        # Sua regra: Se tem 10 ou mais e ainda é nível baixo, sobe para 10
        if total_interesses >= 10 and current_user.nivel_acesso < 10:
            sucesso, msg = processar_mudanca_nivel(current_user, 10)
            if sucesso:
                flash("Parabéns! Você agora é um usuário oficial do FeedIn!", "success")

        database.session.commit()
        flash("Interesses atualizados com sucesso!", "success")

    except Exception as e:
        database.session.rollback()
        print(f"Erro ao salvar: {e}")  # Bom para debug
        flash("Erro ao salvar preferências.", "danger")

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
    """
    Motor de Afinidade: Ajustado para lidar com relações dinâmicas (lazy='dynamic')
    e evitar o erro de object population.
    """
    from sqlalchemy import or_

    # 1. Automação de Rigor
    total_pioneiros = Usuario.query.filter_by(is_pioneiro=True).count()
    modo_rigoroso = app.config.get('MODO_PRODUCAO') or (total_pioneiros > 100)

    # 2. Lista de Exclusão (Eu + conexões existentes)
    relacoes_existentes = Conexoes.query.filter(
        (Conexoes.id_remetente == usuario_atual.id) |
        (Conexoes.id_destinatario == usuario_atual.id)
    ).all()

    ids_bloqueados = [c.id_remetente if c.id_remetente != usuario_atual.id else c.id_destinatario
                      for c in relacoes_existentes]
    ids_bloqueados.append(usuario_atual.id)

    # 3. Coleta de IDs (Ajustado para relação dinâmica)
    # Como é dinâmico, tratamos como query para pegar os IDs
    minhas_prefs_ids = [int(p.id) for p in usuario_atual.interesses.all()]

    ids_meus_pais = []
    for p in usuario_atual.interesses.all():
        for pai in p.contextos:
            ids_meus_pais.append(int(pai.id))

    busca_total_ids = list(set(minhas_prefs_ids + ids_meus_pais))
    meus_grupos_ids = [m.id_grupo for m in usuario_atual.membros_grupos]

    # 4. A QUERY (Limpa de Eager Loading nos interesses)
    possiveis_conexoes = (Usuario.query
                          .outerjoin(MembroGrupo)
                          .options(
        # Carregamos apenas o que NÃO é dinâmico
        database.joinedload(Usuario.perfil),
        database.joinedload(Usuario.membros_grupos)
        .joinedload(MembroGrupo.grupo)
        .joinedload(GrupoSocial.local)
    )
                          .filter(Usuario.is_pioneiro == True)
                          .filter(~Usuario.id.in_(ids_bloqueados))
                          .filter(
        or_(
            MembroGrupo.id_grupo.in_(meus_grupos_ids) if meus_grupos_ids else False,
            # O any() é seguro, pois gera um sub-select no banco e não popula o objeto agora
            Usuario.interesses.any(Taxonomia.id.in_(busca_total_ids))
        )
    )
                          .distinct()
                          .limit(10)
                          .all())

    # 5. Processamento dos Cards
    sugestoes_finais = []
    meus_locais_ids = [v.local_id for v in usuario_atual.vinculos]

    for outro in possiveis_conexoes:
        amigo_ponte = usuario_atual.get_amigo_em_comum(outro)

        if modo_rigoroso and not amigo_ponte:
            continue

        # --- Lógica de Locais ---
        locais_comum = []
        for v_outro in outro.vinculos:
            if v_outro.local_id in meus_locais_ids:
                nome_local = v_outro.local.nome
                epoca = v_outro.experiencia or ""
                locais_comum.append(f"{nome_local} {epoca}")

        locais_unicos = list(set(locais_comum))

        # --- Lógica de Preferências (Ajustada para query dinâmica) ---
        # Como 'outro.interesses' é uma query, usamos .all() para filtrar no Python
        # ou filtramos na query. Para o card, o .all() resolve:
        interesses_outro = outro.interesses.all()
        todos_interesses_comum = [p.nome for p in interesses_outro if int(p.id) in minhas_prefs_ids]

        # Limitamos a exibição para os 3 primeiros
        preferencias_exibicao = todos_interesses_comum[:3]
        total_restante_prefs = max(0, len(todos_interesses_comum) - 3)

        # --- Definição do Motivo e Peso ---
        txt_motivo = f"Conhece {amigo_ponte.username}" if amigo_ponte else "Pioneiro FeedIn"
        calculo_peso = (len(locais_unicos) * 5) + (len(todos_interesses_comum) * 2)

        sugestoes_finais.append({
            'usuario': outro,
            'motivo': txt_motivo,
            'amigo_ponte': amigo_ponte,
            'preferencias': preferencias_exibicao,
            'total_restante_prefs': total_restante_prefs,
            'locais': locais_unicos,
            'total_locais': len(locais_unicos),
            'peso': calculo_peso
        })

    # Ordena pelo peso (afinidade)
    return sorted(sugestoes_finais, key=lambda x: x['peso'], reverse=True)


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
    return redirect(request.referrer or url_for('dashboard'))


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
        total_tags = current_user.interesses_escolhidos.count()

        if total_lugares >= 5 and total_tags >= 10 and current_user.aceite_lgpd:
            if current_user.nivel_acesso < 10:
                current_user.nivel_acesso = 10
                flash("Identidade Validada! Você agora é um usuário do FeedIn.", "success")

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
    # --- NOVO BLOCO DE SEGURANÇA ---
    if current_user.nivel_acesso < 10:
        flash("Para convidar amigos e garantir o selo de Pioneiro, você precisa completar seu perfil (Nível 10).", "warning")
        return redirect(url_for('get_perfil', id_usuario=current_user.id))
    # ------------------------------

    form = FormConvite()
    if form.validate_on_submit():
        # 1. Captura e Limpeza de Dados
        numero_destino = re.sub(r'\D', '', form.whatsapp.data)
        nome_amigo = form.nome_convidado.data or "Amigo(a)"

        # Captura o contexto selecionado (Ex: "local_12" ou "gosto_5")
        contexto_raw = request.form.get('contexto_convite')
        tipo_vinculo = None
        id_referencia = None
        nome_contexto = "nossa rede"  # Fallback caso algo falhe

        if contexto_raw and "_" in contexto_raw:
            tipo_vinculo, id_referencia = contexto_raw.split('_')

            # Busca o nome amigável para a mensagem do WhatsApp
            if tipo_vinculo == 'local':
                obj = Local.query.get(id_referencia)
                if obj: nome_contexto = obj.nome
            elif tipo_vinculo == 'gosto':
                # Aqui você pode buscar na sua tabela de Taxonomia ou AtividadeLocal
                obj = AtividadeLocal.query.get(id_referencia)
                if obj: nome_contexto = obj.nome

        # Ajuste do prefixo 55 para o Deep Link
        numero_completo = numero_destino if numero_destino.startswith('55') else "55" + numero_destino

        # 2. Verificação de Exclusividade
        convite_existente = Convite.query.filter_by(whatsapp_destino=numero_destino).first()

        if convite_existente:
            if convite_existente.id_remetente == current_user.id:
                flash("Você já enviou um convite para este número!", "info")
            else:
                flash("Este número já recebeu um convite de outro usuário.", "warning")
            return redirect(url_for('get_perfil', id_usuario=current_user.id))

        # 3. Criação do Registro com Contexto
        novo_convite = Convite(
            id_remetente=current_user.id,
            whatsapp_destino=numero_destino,
            tipo_vinculo=tipo_vinculo,
            id_referencia=id_referencia,
            status_onboarding=False
        )

        try:
            database.session.add(novo_convite)
            database.session.commit()

            # 4. Preparação do Link e Mensagem Contextualizada
            # Passamos o contexto na URL para a rota de registro capturar
            link_registro = url_for('registrar',
                                    indicado_por=current_user.id,
                                    contexto=contexto_raw,
                                    _external=True)

            # A "mágica" da mensagem:
            if tipo_vinculo:
                texto_base = (
                    f"Olá {nome_amigo}! Estou te convidando para o FeedIn! para resgatarmos "
                    f"nossa conexão através de **{nome_contexto}**. "
                    f"Crie seu perfil e torne-se um Pioneiro: {link_registro}"
                )
            else:
                texto_base = (
                    f"Olá {nome_amigo}! Estou te convidando para conhecer o FeedIn, "
                    f"nosso resgate de memória social de Piracicaba. "
                    f"Crie seu perfil pelo link: {link_registro}"
                )

            whatsapp_url = f"https://api.whatsapp.com/send?phone={numero_completo}&text={quote(texto_base)}"

            return redirect(whatsapp_url)

        except Exception as e:
            database.session.rollback()
            flash("Erro ao gerar convite. Tente novamente.", "danger")

    return redirect(url_for('get_perfil', id_usuario=current_user.id))


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


@app.route("/configuracoes")
@app.route("/configuracoes/<aba>")
@login_required
def configuracoes(aba='perfil'):
    import json
    from feedin.forms import FormApelido

    # Buscas básicas
    generos = Generos.query.all()
    estados_civis = EstadoCivil.query.all()
    form_apelido = FormApelido()

    # Prioridade total para o que vier na URL
    aba_final = request.args.get('aba') or aba

    # Interesses (simplificado para não travar)
    try:
        interesses_obj = current_user.interesses_escolhidos
    except:
        interesses_obj = []

    minhas_prefs_json = json.dumps([{'id': p.id, 'nome': p.nome} for p in interesses_obj])
    perfil = Perfil.query.filter_by(id_usuario=current_user.id).first()

    return render_template("configuracoes.html",
                           aba_ativa=aba_final,
                           generos=generos,
                           estados=estados_civis,
                           perfil=perfil,
                           form_apelido=form_apelido,
                           minhas_prefs_json=minhas_prefs_json,
                           identidade=current_user.identidade)


# rota para exibição do perfil
@app.route("/perfil/<int:usuario_id>")
@login_required
def ver_perfil(usuario_id):
    user_alvo = Usuario.query.get_or_404(usuario_id)
    e_o_proprio = (current_user.id == user_alvo.id)
    # Chamada dedicada à lógica de locais populares do usuário
    locais_seguidos = Local.get_locais_populares_por_usuario(user_alvo.id)

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
    meus_locais_ids = [m.local_id for m in VinculoUsuarioLocal.query.filter_by(usuario_id=current_user.id).all()]
    locais_alvo_ids = [m.local_id for m in memorias_alvo]
    locais_comum_ids = set(meus_locais_ids) & set(locais_alvo_ids)

    # 5. Signo (Usando sua função do utils.py)
    s_nome, s_icone = (None, None)
    if user_alvo.perfil.data_nascimento:
        res = obter_signo(user_alvo.perfil.data_nascimento)
        s_nome, s_icone = res if res else (None, None)

    # --- NOVO BLOCO: O MURAL DE POSTAGENS ---

    # A. Postagens que o USER_ALVO CRIOU (Suas próprias memórias)
    mural_postagens = Postagem.query.filter_by(id_usuario=usuario_id, ativo=True) \
        .order_by(Postagem.data_criacao.desc()).all()

    # B. Postagens onde o USER_ALVO FOI MARCADO (Fotos com ele)
    # Usamos o relacionamento 'pessoas_marcadas' que você adicionou ao Model
    fotos_com_alvo = Postagem.query.join(Postagem.pessoas_marcadas) \
        .filter(Usuario.id == usuario_id, Postagem.ativo == True) \
        .order_by(Postagem.data_criacao.desc()).all()

    form_convite = FormConexao()

    return render_template("perfil_publico.html",
                           user_alvo=user_alvo,
                           e_o_proprio=e_o_proprio,
                           postagens=mural_postagens,  # Postagens autorais
                           fotos_com_voce=fotos_com_alvo,  # Postagens de terceiros onde ele aparece
                           memorias=memorias_alvo,
                           conexao=conexao_atual,
                           conexoes_confirmadas=conexoes_confirmadas,
                           tags_comum_ids=[t.id for t in tags_em_comum],
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


@app.route('/local_v2/<int:local_id>') # Use este endpoint para validar
@login_required
def perfil_local_v2(local_id):
    local = Local.query.get_or_404(local_id)

    # 1. GARANTIA DE VARIÁVEIS
    usuario_segue = False
    atividades_formatadas = []

    # 2. VERIFICAÇÃO DE VÍNCULO (Simplificado para performance)
    usuario_segue = database.session.query(VinculoUsuarioLocal).filter_by(
        usuario_id=current_user.id, local_id=local_id
    ).first() is not None

    # 3. TAGS DOS AMIGOS (Mantendo sua lógica original de Piracicaba)
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

    # 4. BUSCA E NORMALIZAÇÃO DE POSTAGENS (Onde o card universal bebe)
    postagens_publicas = Postagem.query.filter_by(id_local=local_id, ativo=True) \
        .options(joinedload(Postagem.autor).joinedload(Usuario.perfil)) \
        .order_by(Postagem.data_criacao.desc()).all()

    for p in postagens_publicas:
        atividades_formatadas.append({
            'id': p.id,
            'tipo_card': 'postagem', # Para o card saber o estilo
            'data_criacao': p.data_criacao,
            'autor_objeto': p.autor, # Casando com o card
            'conteudo_exibicao': p.conteudo,
            'imagem_url': p.imagem_url, # Necessário para exibir a foto
            'objeto_original': p, # Para os métodos de curtir/comentar
            'id_local': local.id,
            'local': local # Objeto completo para o card pegar o nome
        })

    # 5. BUSCA E NORMALIZAÇÃO DE VÍNCULOS (O Lado Festivo/Resgate)
    # Aqui usamos as 'atividades' que o seu HTML já exibe na lateral
    if local.atividades:
        for ativ in local.atividades:
            atividades_formatadas.append({
                'id': ativ.id,
                'tipo_card': 'conexao', # Aciona o "Banner de Celebração" no card
                'data_criacao': ativ.data_criacao,
                'autor_objeto': ativ.criador, # Casando com o card
                'descricao': ativ.descricao, # Casando com o card
                'periodo_estimado': ativ.periodo_estimado, # Casando com o card
                'conteudo_exibicao': ativ.descricao or "Resgatou uma memória.",
                'id_local_vinc': local.id, # Para a lógica de borda do card
                'objeto_original': ativ
            })

    # 6. UNIFICAÇÃO E ORDENAÇÃO
    atividades_ordenadas = sorted(atividades_formatadas, key=lambda x: x['data_criacao'], reverse=True)

    return render_template('locais/perfil_local.html', # CONSUMINDO SEU HTML ORIGINAL
                           local=local,
                           atividades=atividades_ordenadas,
                           sugestoes_nicho=tags_dos_amigos,
                           usuario_segue=usuario_segue,
                           rating_data=local.get_rating_data())


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

    # 2. Lógica de Construção de Narrativa (O "Cérebro" da Rota)
    # Se for um vínculo inicial, montamos o texto base caso o usuário não tenha escrito nada
    if tipo_postagem == 'vinculo':
        texto_gerado = f"Resgatou uma memória"
        if epoca: texto_gerado += f" da época de {epoca}"
        if relato_extra: texto_gerado += f": {relato_extra}"

        # Se o usuário escreveu algo a mais no campo conteúdo, concatenamos
        conteudo = f"{texto_gerado}. {conteudo}" if conteudo else texto_gerado

    # 3. Validação de Regras de Negócio
    if not conteudo and not arquivo:
        flash("Sua memória precisa de um texto ou uma imagem!", "warning")
        return redirect(request.referrer)

    # Definição de quando a imagem é obrigatória
    # Não é obrigatória em: locais históricos, postagens de vínculo ou tipos específicos sem foto
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

        # 4. Salva tudo
        database.session.add(nova_postagem)
        database.session.commit()

        flash("Memória compartilhada com sucesso!", "success")
    except Exception as e:
        database.session.rollback()
        print(f"--- ERRO CRÍTICO NA POSTAGEM: {e} ---")
        flash("Houve um erro técnico ao salvar.", "danger")

    return redirect(request.referrer)

def salvar_imagem_postagem(foto, usuario_id):
    """
    Processa a foto mantendo a proporção original.
    Converte para WEBP e garante largura máxima de 1200px para qualidade.
    """
    if not foto or not hasattr(foto, 'filename') or foto.filename == '':
        return None

    nome_arquivo = f"post_{usuario_id}_{int(datetime.now().timestamp())}.webp"
    pasta_destino = os.path.join(current_app.root_path, 'static', 'uploads', 'posts')

    if not os.path.exists(pasta_destino):
        os.makedirs(pasta_destino)

    try:
        img = Image.open(foto)

        # Manter proporção original (Aspect Ratio)
        # Redimensionamos apenas se for muito grande para economizar banda
        max_size = (1200, 1200)
        img.thumbnail(max_size, Image.Resampling.LANCZOS)

        img.save(os.path.join(pasta_destino, nome_arquivo), "WEBP", quality=85)
        return nome_arquivo
    except Exception as e:
        print(f"Erro ao processar imagem: {e}")
        return None


@app.route('/editar_post/<int:post_id>', methods=['POST'])
@login_required
def editar_post(post_id):
    post = Postagem.query.get_or_404(post_id)

    # Uso do método de conveniência que você já tem no Model!
    if not post.pode_gerenciar(current_user.id):
        flash("Ação não permitida.", "danger")
        return redirect(request.referrer)

    post.conteudo = request.form.get('conteudo')
    database.session.commit()
    flash("Postagem atualizada.", "success")
    return redirect(request.referrer)


@app.route('/excluir_post/<int:post_id>', methods=['POST'])
@login_required
def excluir_post(post_id):
    post = Postagem.query.get_or_404(post_id)

    if not post.pode_gerenciar(current_user.id):
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

# Para que a marcação de pessoas funcione
@app.route('/buscar_usuarios')
@login_required
def buscar_usuarios():
    termo = request.args.get('q', '').replace('@', '').strip()
    if len(termo) < 2:
        return jsonify([])

    # Busca por username ou nome no perfil
    usuarios = Usuario.query.join(Perfil).filter(
        (Usuario.username.ilike(f'%{termo}%')) |
        (Perfil.nome_completo.ilike(f'%{termo}%'))
    ).limit(5).all()

    return jsonify([{'id': u.id, 'username': u.username} for u in usuarios])


# consulta que gera a visão que o empreendedor de Piracicaba precisa para tomar decisões:
def relatorio_nicho_piracicaba():
    print("\n📊 RELATÓRIO DE DEMANDA - FEEDIN PIRACICABA")
    print("-" * 60)

    try:
        # 1. Pegamos todas as tags (Taxonomia) para analisar uma a uma
        tags = Taxonomia.query.all()

        for tag in tags:
            # 2. Contamos seguidores (Interesses dos usuários)
            # Acessamos a tabela 'usuarios_interesses' através do atributo 'c' (columns)
            total_seguidores = database.session.query(func.count()) \
                                   .select_from(usuarios_interesses) \
                                   .filter(usuarios_interesses.c.taxonomia_id == tag.id) \
                                   .scalar() or 0

            # 3. Contamos memórias (Postagens marcadas com essa tag)
            # Acessamos a tabela 'postagem_tags' através do atributo 'c'
            vol_memorias = database.session.query(func.count()) \
                               .select_from(postagem_tags) \
                               .filter(postagem_tags.c.taxonomia_id == tag.id) \
                               .scalar() or 0

            # Só exibe se houver algum movimento (seguidores ou fotos)
            if total_seguidores > 0 or vol_memorias > 0:
                print(f"Tag: {tag.nome:<20} | Seguidores: {total_seguidores:<4} | Memórias: {vol_memorias}")

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


# Rotas ligadas ao Perfil Local

@app.route('/local/reivindicar/<int:local_id>', methods=['POST'])
@login_required
def reivindicar_local(local_id):
    # Verifica se já existe uma solicitação para este par Usuário/Local
    existente = ReivindicacaoLocal.query.filter_by(id_local=local_id, id_usuario=current_user.id).first()

    if not existente:
        nova_solicitacao = ReivindicacaoLocal(id_local=local_id, id_usuario=current_user.id)
        database.session.add(nova_solicitacao)
        database.session.commit()

        # Aqui entra o disparo de e-mail automático
        # enviar_email_confirmacao_reivindicacao(current_user.email, local_id)

        flash('Interesse registrado! Avisaremos você assim que a gestão oficial for liberada.', 'success')
    else:
        flash('Você já registrou interesse neste local.', 'info')

    return redirect(url_for('perfil_local', id=local_id))


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