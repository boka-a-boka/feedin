import re
import uuid
import random
import urllib.parse
from datetime import datetime, timezone, timedelta, date
from feedin.middlewares import modulo_required
from urllib.parse import urlparse

# 1. METODOLOGIAS DO FLASK & EXTENSÕES DE SESSÃO
from flask import current_app, render_template, request, redirect, url_for, flash, session, jsonify, Blueprint
from flask_login import current_user, login_required, login_user, logout_user

# 2. EXTENSÕES DE SEGURANÇA E CORE DO FEEDIN
from flask_bcrypt import check_password_hash, generate_password_hash
from feedin import database as db, bcrypt, csrf  # Objetos centrais e extensões do Core

# 3. 🧩 IMPORTAÇÃO DO CONTRATO OFICIAL DO BLUEPRINT (Sem recriá-lo!)
from feedin.modules.auth import auth_bp

# 4. FORMULÁRIOS (WTFORMS) - INTERNOS E INTER-MÓDULOS
from feedin.modules.auth.forms import FormLoginUniversal

# 5. 🗄️ PERSISTÊNCIA: MODELOS DO BANCO DE DADOS
# Modelos da Estrutura Base (Core)
from feedin.models import Usuario, IdentidadeCivil, Perfil, Generos, LocalMidia, Local, ModulosSistema

# Modelos do próprio módulo Auth
from feedin.modules.auth.models import (ModCadastroCliente, ModFilaAtivacaoCliente, ModVinculoModulo, VinculoUsuarioEmpresa,
                                        AthAtribContexto)

# Modelos do módulo Empresa necessários para validações no fluxo de autenticação
from feedin.modules.empresa.models import (EseProcessoClaim, EseEmpresa,EscalaTrabalhoColaborador, EseConviteColaborador,
                                           ColaboradorContrato)

# =========================================================================
# ⚙️ FUNÇÕES AUXILIARES DO MÓDULO AUTH
# =========================================================================
def gerar_username_unico(nome):
    """Gera um username amigável e único com base no primeiro nome."""
    base = "".join(filter(str.isalnum, nome.lower().split()[0]))
    return f"{base}{random.randint(100, 999)}"


# =========================================================================
# 🔑 PORTAL DE LOGIN UNIVERSAL (INTEGRADO & DINÂMICO)
# =========================================================================
@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    """
    ROTA DE AUTENTICAÇÃO UNIFICADA (MOTOR AGNÓSTICO DE MÓDULOS)

    Responsável por validar credenciais, injetar o módulo ativo na sessão
    e direcionar o usuário para o endpoint correto do catálogo 'ModulosSistema'.
    """
    # 🎯 CORREÇÃO 1: Adicionado ModCadastroCliente que faltava no import
    from feedin.models import ModulosSistema, Usuario
    from werkzeug.security import check_password_hash
    from flask_login import login_user, current_user
    from feedin.modules.auth.models import ModCadastroCliente

    # 1. Se já está autenticado, redireciona diretamente para o módulo
    if current_user.is_authenticated:
        modulo_slug_atual = session.get('modulo_slug_atual', 'core')
        modulo_info = ModulosSistema.query.filter_by(slug=modulo_slug_atual, ativo=True).first()
        if modulo_info and modulo_info.endpoint:
            try:
                url_destino = url_for(modulo_info.endpoint)
                if '/hub' not in url_destino:
                    return redirect(url_destino)
            except Exception as err:
                print(f"⚠️ [LOGIN] Falha ao resolver endpoint do módulo logado: {err}")
        return redirect(url_for('core.dashboard'))

    form = FormLoginUniversal()

    # 2. CAPTURA DE CONTEXTO DO MÓDULO E NEXT_URL
    modulo_slug = request.args.get('modulo') or request.form.get('modulo_slug') or session.get('modulo_slug_atual',
                                                                                               'core')
    next_url = request.args.get('next_url') or request.form.get('next_url')

    if next_url and ('/hub' in next_url or '/login' in next_url or 'auth.hub' in next_url):
        next_url = None

    session['modulo_slug_atual'] = modulo_slug
    modulo_info = ModulosSistema.query.filter_by(slug=modulo_slug, ativo=True).first()

    def validar_hash_senha(entidade, senha_raw):
        if not entidade:
            return False

        # 1. Se a classe possuir método nativo (check_password / verificar_senha)
        if hasattr(entidade, 'check_password'):
            try:
                if entidade.check_password(senha_raw):
                    return True
            except Exception:
                pass

        if hasattr(entidade, 'verificar_senha'):
            try:
                if entidade.verificar_senha(senha_raw):
                    return True
            except Exception:
                pass

        # 2. Captura o hash bruto no banco
        hash_bruto = getattr(entidade, 'senha_hash', None) or getattr(entidade, 'senha', None)

        if not hash_bruto:
            return False

        # Garante versão em bytes e versão em string
        if isinstance(hash_bruto, bytes):
            hash_bytes = hash_bruto
            try:
                hash_str = hash_bruto.decode('utf-8')
            except UnicodeDecodeError:
                hash_str = str(hash_bruto)
        else:
            hash_str = str(hash_bruto)
            hash_bytes = hash_str.encode('utf-8')

        # 🎯 3. SUPORTE A BCRYPT ($2a$, $2b$, $2y$)
        if hash_str.startswith(('$2a$', '$2b$', '$2y$')):
            try:
                import bcrypt
                return bcrypt.checkpw(senha_raw.encode('utf-8'), hash_bytes)
            except Exception as e:
                print(f"⚠️ [LOGIN] Erro ao validar Bcrypt: {e}")
                pass

        # 4. SUPORTE A WERKZEUG
        if hash_str.startswith(('pbkdf2:', 'scrypt:', 'argon2:', 'sha256$')):
            return check_password_hash(hash_str, senha_raw)

        # 5. Fallback para senha em texto plano
        return hash_str == senha_raw

    # 3. PROCESSAMENTO DO FORMULÁRIO (POST)
    if form.validate_on_submit():
        email_digitado = form.email.data.strip().lower()
        senha_digitada = form.senha.data

        usuario_autenticado = None

        # ---------------------------------------------------------------------
        # 🚨 DEPURADOR TEMPORÁRIO (VEJA O OUTPUT NO TERMINAL DO PYCHARM)
        # ---------------------------------------------------------------------
        cliente_modulo = ModCadastroCliente.query.filter(
            ModCadastroCliente.email.ilike(email_digitado)
        ).first()

        print("\n" + "=" * 50)
        print(f"🔍 [DEBUG LOGIN] E-mail digitado: '{email_digitado}'")
        print(f"🔍 [DEBUG LOGIN] Cliente encontrado no banco? {cliente_modulo is not None}")
        if cliente_modulo:
            print(f"🔍 [DEBUG LOGIN] ID do Cliente: {getattr(cliente_modulo, 'id', None)}")
            print(f"🔍 [DEBUG LOGIN] E-mail no Banco: '{getattr(cliente_modulo, 'email', None)}'")
            hash_no_banco = getattr(cliente_modulo, 'senha_hash', None) or getattr(cliente_modulo, 'senha', None)
            print(f"🔍 [DEBUG LOGIN] Hash no banco (raw): {repr(hash_no_banco)}")
            print(f"🔍 [DEBUG LOGIN] Tipo do hash: {type(hash_no_banco)}")

            # Testa a validação e imprime o resultado
            resultado_valida = validar_hash_senha(cliente_modulo, senha_digitada)
            print(f"🔍 [DEBUG LOGIN] Resultado do validar_hash_senha(): {resultado_valida}")
        print("=" * 50 + "\n")

        if cliente_modulo:
            # 1. Valida a senha no registro do módulo
            if validar_hash_senha(cliente_modulo, senha_digitada):
                if getattr(cliente_modulo, 'usuario_id', None):
                    usuario_autenticado = Usuario.query.get(cliente_modulo.usuario_id)

                # Se não tem usuário vinculado no Core, usa o próprio cliente do módulo para a sessão
                if not usuario_autenticado:
                    usuario_autenticado = cliente_modulo

            # 2. Se a senha não estava no módulo, valida no Usuario atrelado (caso exista vínculo)
            elif getattr(cliente_modulo, 'usuario_id', None):
                usr_vinculado = Usuario.query.get(cliente_modulo.usuario_id)
                if validar_hash_senha(usr_vinculado, senha_digitada):
                    usuario_autenticado = usr_vinculado

        # ---------------------------------------------------------------------
        # PASSO C: Efetivação da Sessão e Redirecionamento
        # ---------------------------------------------------------------------
        if usuario_autenticado:
            login_user(usuario_autenticado, remember=getattr(form, 'lembrar_me', False) and form.lembrar_me.data)

            session['usuario_id'] = getattr(usuario_autenticado, 'id', None)
            session['modulo_slug_atual'] = modulo_slug
            if cliente_modulo:
                session['cliente_modulo_id'] = cliente_modulo.id

            flash("Bem-vindo de volta!", "success")

            # Redirecionamento
            if next_url and next_url.startswith('/'):
                return redirect(next_url)

            if modulo_info and modulo_info.endpoint:
                try:
                    url_destino = url_for(modulo_info.endpoint)
                    if '/hub' not in url_destino:
                        return redirect(url_destino)
                except Exception as err:
                    print(f"⚠️ [LOGIN] Erro ao resolver endpoint do banco ({modulo_info.endpoint}): {err}")

            return redirect(url_for('core.dashboard'))

        else:
            flash("E-mail ou senha incorretos. Verifique suas credenciais.", "danger")

    return render_template(
        'auth/login_modulo.html',
        form=form,
        next_url=next_url,
        modulo_slug=modulo_slug,
        modulo_info=modulo_info
    )


# =========================================================================
# 🔄 2. CADASTRO ORGÂNICO ETAPA 1: Credenciais Básicas (E-mail e Senha)
# =========================================================================
@auth_bp.route('/cadastro/inicio', methods=['GET', 'POST'])
def cadastro_inicio():

    from feedin.models import ModulosSistema
    # 1. Captura as intenções de destino originais da requisição
    proximo_passo = request.args.get('next_url', '')

    # 🪐 2. ARQUITETURA POLIMÓRFICA: Resolução dinâmica de Layout baseada no Banco de Dados
    modulo_slug = session.get('modulo_slug_atual')
    modulo_info = None

    if modulo_slug:
        # Consulta o catálogo mestre de extensões de forma orientada a dados
        modulo_info = ModulosSistema.query.filter_by(slug=modulo_slug, ativo=True).first()

    # Define dinamicamente o arquivo pai por convenção se o módulo existir; caso contrário, usa o Core

    # 🪐 2. RESOLUÇÃO DINÂMICA DO LAYOUT PAI COM PREFIXO DE BLUEPRINT
    if modulo_info:
        # Como você já padronizou os arquivos, isso gerará:
        # - "empresa/base_empresa.html" para o módulo de empresas
        # - "agenda/base_agenda.html" para o módulo de agenda
        base_layout = f"{modulo_info.slug}/base_{modulo_info.slug}.html"
    else:
        # Fallback seguro para o Core / Portal Unificado
        base_layout = "auth/base_auth.html"

    # 🏙️ FLUXO DE ENTRADA (GET): Renderização da Interface com herança polimórfica
    if request.method == 'GET':
        email_inicial = request.args.get('email_inicial', '').strip()

        return render_template(
            'auth/cadastro_inicio.html',
            email_inicial=email_inicial,
            next_url=proximo_passo,
            base_layout=base_layout,
            modulo_info=modulo_info,  # Mantém para o cadastro_inicio.html
            modulos_sistema=modulo_info  # 🟢 CORREÇÃO: Alinha com o nome esperado pelo base_empresa.html
        )

    # 🏙️ FLUXO DE PROCESSAMENTO (POST): Coleta de Credenciais e Validação
    email = request.form.get('email', '').strip().lower()
    senha = request.form.get('senha', '')
    confirma_senha = request.form.get('confirma_senha', '')
    proximo_passo = request.form.get('next_url', '')

    # Validação 1: Campos nulos ou em branco
    if not email or not senha or not confirma_senha:
        flash("Todos os campos são obrigatórios.", "warning")
        return redirect(url_for('auth.cadastro_inicio', next_url=proximo_passo))

    # Validação 2: Confirmação matemática de integridade da senha
    if senha != confirma_senha:
        flash("As senhas informadas não conferem.", "danger")
        return render_template(
            'auth/cadastro_inicio.html',
            email_inicial=email,
            next_url=proximo_passo,
            base_layout=base_layout,
            modulo_info=modulo_info,  # Mantém para o cadastro_inicio.html
            modulos_sistema=modulo_info  # 🟢 CORREÇÃO: Alinha com o nome esperado pelo base_empresa.html
        )

    # =========================================================================
    # 🟢 INTEGRIDADE MULTI-MÓDULO: Validação baseada na tabela pivô/vínculo
    # =========================================================================
    # Busca a existência do e-mail na base mestre de clientes de negócios
    cliente_existente = ModCadastroCliente.query.filter_by(email=email).first()

    if cliente_existente and modulo_slug:
        # Com o cliente localizado, varre se o CPF_HASH dele já possui vínculo com o módulo atual
        possui_vinculo_modulo = ModVinculoModulo.query.filter(
            ModVinculoModulo.cpf_hash == cliente_existente.cpf_hash,
            ModVinculoModulo.modulo_slug == modulo_slug
        ).first()

        # O bloqueio de duplicidade só ocorre se ele de fato já tiver uma permissão ativa NESTE módulo
        if possui_vinculo_modulo:
            flash("Este e-mail já possui um cadastro ativo neste módulo de negócios. Faça login diretamente.", "info")
            return redirect(url_for('auth.login', next_url=proximo_passo))

        # Nota de engenharia: Se 'cliente_existente' for verdadeiro, mas 'possui_vinculo_modulo' for None,
        # significa que ele já é cliente da plataforma em outro escopo (ex: agenda). O sistema passará
        # reto de forma segura, permitindo que o onboarding construa o novo elo em concluir_vinculo_claim.

    # 🪐 Força a sessão a ser permanente para persistir os estados no localhost:8000
    session.permanent = True

    # Prepara e empacota as credenciais na sessão temporária para a esteira civil subsequente
    session['temp_cadastro_email'] = email
    session['temp_cadastro_senha'] = senha

    next_url_atual = request.args.get('next_url') or request.form.get('next_url')

    return redirect(url_for('auth.validar_identidade_tela', next_url=next_url_atual))


# =========================================================================
# 🛡️ ESTEIRA AUTÔNOMA DE IDENTIFICAÇÃO CIVIL (ALFÂNDEGA SSO)
# =========================================================================
@auth_bp.route('/validar-identidade', methods=['GET'])
def validar_identidade_tela():
    next_url = request.args.get('next_url')

    # Limpa se o next_url for a tela do Hub ou Login
    if next_url and ('/hub' in next_url or '/login' in next_url):
        next_url = None

    # Garante que a sessão saiba qual é o módulo atual
    modulo_slug = session.get('modulo_slug_atual', 'core')

    return render_template('auth/validar_identidade.html', next_url=next_url, modulo_slug=modulo_slug)


@auth_bp.route('/processar-identidade', methods=['POST'])
def processar_identidade():
    """
    ALFÂNDEGA DE IDENTIDADE CIVIL (MOTOR AGNÓSTICO E DINÂMICO)

    Responsável por processar a identidade civil e assegurar os vínculos de
    acesso nos módulos. Todo o direcionamento operacional é guiado pela
    tabela 'ModulosSistema' e parâmetros de rota ('next_url').
    """
    from feedin.utils import validar_cpf_estrutura
    from feedin.models import ModulosSistema  # Modelo do catálogo mestre

    # =========================================================================
    # 📑 1. CAPTURA DE CONTEXTO E FLUXO VOLÁTIL
    # =========================================================================
    email_cadastro = session.get('temp_cadastro_email')

    if not email_cadastro and current_user.is_authenticated:
        email_cadastro = current_user.email

    if not email_cadastro:
        flash("Sessão de identificação expirada. Por favor, reinicie o processo.", "warning")
        return redirect(url_for('auth.cadastro_inicio'))

    nome_real = request.form.get('nome_real', '').strip().upper()
    cpf_digitado = re.sub(r'\D', '', request.form.get('cpf', ''))
    data_nasc_str = request.form.get('data_nascimento')
    genero_id = request.form.get("genero")

    modulo_atual_slug = session.get('modulo_slug_atual', 'core')
    local_atual_id = session.get('local_id_atual') or session.get('empresa_ativa_id')

    # Prioridade de redirecionamento: Parâmetro da requisição > Sessão
    proximo_passo = request.args.get('next_url') or session.pop('next_url', None)

    whatsapp_input = request.form.get('whatsapp', '').strip()
    whatsapp_final = whatsapp_input if whatsapp_input else None

    # =========================================================================
    # 🛡️ 2. VALIDAÇÕES CRÍTICAS E ESTRUTURAIS
    # =========================================================================
    if not nome_real or not cpf_digitado or not data_nasc_str or not genero_id:
        flash("Todos os campos de identidade são obrigatórios.", "warning")
        return redirect(url_for('auth.validar_identidade_tela', next_url=proximo_passo))

    if not validar_cpf_estrutura(cpf_digitado):
        flash("O CPF informado é inválido.", "danger")
        return redirect(url_for('auth.validar_identidade_tela', next_url=proximo_passo))

    hash_digitado = IdentidadeCivil.gerar_hash(cpf_digitado)
    identidade_existente = IdentidadeCivil.query.filter_by(cpf_hash=hash_digitado).first()

    # =========================================================================
    # 🔍 3. BUSCA E VERIFICAÇÃO DO ID CORE
    # =========================================================================
    id_usuario_core = None

    if identidade_existente:
        id_usuario_core = identidade_existente.usuario_id
    elif current_user.is_authenticated:
        id_usuario_core = current_user.id
    else:
        usuario_core = Usuario.query.filter_by(email=email_cadastro).first()
        if usuario_core:
            id_usuario_core = usuario_core.id

    if identidade_existente and id_usuario_core and identidade_existente.usuario_id != id_usuario_core:
        flash("Este CPF já está vinculado a outra conta ativa no sistema.", "danger")
        return redirect(url_for('auth.validar_identidade_tela', next_url=proximo_passo))

    # Variable para guardar redirecionamento de alta prioridade (ex: onboarding de colaborador)
    destino_prioritario = None

    # =========================================================================
    # 💾 4. GRAVAÇÃO ATÔMICA DA IDENTIDADE E VÍNCULO MESTRE
    # =========================================================================
    try:
        data_nasc_obj = datetime.strptime(data_nasc_str, '%Y-%m-%d').date()

        # 🪐 BLOCO A: COFRE CIVIL GLOBAL (Core)
        if id_usuario_core:
            if not identidade_existente:
                cpf_protegido = current_app.fernet.encrypt(cpf_digitado.encode())
                nova_identidade = IdentidadeCivil(
                    usuario_id=id_usuario_core,
                    nome_completo_oficial=nome_real,
                    cpf_criptografado=cpf_protegido,
                    cpf_hash=hash_digitado,
                    data_nascimento=data_nasc_obj,
                    ip_origem=request.remote_addr,
                    versao_termos_aceita="1.0-BETA"
                )
                db.session.add(nova_identidade)

                perfil = Perfil.query.filter_by(id_usuario=id_usuario_core).first()
                if not perfil:
                    perfil = Perfil(
                        id_usuario=id_usuario_core,
                        nome_completo=nome_real,
                        data_nascimento=data_nasc_obj,
                        genero=int(genero_id)
                    )
                    db.session.add(perfil)

        # 🟢 BLOCO B: ESTEIRA DE CADASTRO COMERCIAL UNIFICADA
        cliente_local = ModCadastroCliente.query.filter_by(email=email_cadastro).first()

        if cliente_local:
            cliente_local.usuario_id = id_usuario_core
            cliente_local.nome = nome_real
            cliente_local.cpf = cpf_digitado
            cliente_local.data_nascimento = data_nasc_obj
            cliente_local.status_conta = 'ativo'

            ModFilaAtivacaoCliente.query.filter_by(email=email_cadastro).delete()
        else:
            senha_temporaria = session.get('temp_cadastro_senha', '')
            senha_hash_local = generate_password_hash(senha_temporaria) if senha_temporaria else ""

            username_padronizado = generar_username_corporativo(nome_real)

            cliente_local = ModCadastroCliente(
                usuario_id=id_usuario_core,
                nome=nome_real,
                email=email_cadastro,
                whatsapp=whatsapp_final or "",
                data_nascimento=data_nasc_obj,
                username_modulo=username_padronizado,
                senha_hash=senha_hash_local,
                status_conta='ativo'
            )
            cliente_local.cpf = cpf_digitado
            db.session.add(cliente_local)

        db.session.flush()

        # 🤝 BLOCO C: VERIFICAÇÃO DE CONVITES PENDENTES (Colaborador/Equipe)
        convite_trabalho = EseConviteColaborador.query.filter_by(
            cpf_hash=hash_digitado,
            data_nascimento=data_nasc_obj,
            status='pendente'
        ).first()

        if convite_trabalho:
            print(f"👔 [ALFÂNDEGA] Convite de trabalho efetivado para Usuário ID: {id_usuario_core}")
            novo_contrato = ColaboradorContrato(
                usuario_id=id_usuario_core,
                estabelecimento_id=convite_trabalho.estabelecimento_id,
                data_admissao=convite_trabalho.data_contratacao or date.today(),
                status='ativo'
            )
            db.session.add(novo_contrato)
            db.session.flush()

            convite_trabalho.status = 'aceito'

            session['completar_cadastro_colaborador'] = True
            session['empresa_ativa_id'] = convite_trabalho.estabelecimento_id
            session['modo_visao'] = 'colaborador'

            # Define destino prioritário de onboarding
            destino_prioritario = url_for('empresa.ficha_autodeclaracao_tela', contrato_id=novo_contrato.id)

        # 🚀 BLOCO D: REGISTRO DE VÍNCULO NO CATÁLOGO DE MÓDULOS (GENÉRICO)
        vinculo_ativo = ModVinculoModulo.query.filter_by(
            cpf_hash=hash_digitado,
            modulo_slug=modulo_atual_slug
        ).first()

        if not vinculo_ativo:
            print(f"🔗 [MOTOR DINÂMICO] Habilitando acesso ao módulo: {modulo_atual_slug}")
            vinculo_ativo = ModVinculoModulo(
                cpf_hash=hash_digitado,
                modulo_slug=modulo_atual_slug,
                local_id=local_atual_id,
                email_customizado=email_cadastro,
                ativo=True
            )
            db.session.add(vinculo_ativo)
        else:
            vinculo_ativo.ativo = True

        db.session.commit()

        # =========================================================================
        # 🔒 AUTENTICAÇÃO AUTOMÁTICA E FIXAÇÃO DA SESSÃO
        # =========================================================================
        from flask_login import login_user
        if not current_user.is_authenticated and id_usuario_core:
            usuario_master = Usuario.query.get(id_usuario_core)
            if usuario_master:
                login_user(usuario_master, remember=True)

        if 'cliente_local' in locals() and cliente_local:
            session['cliente_modulo_id'] = cliente_local.id

        session['usuario_id'] = id_usuario_core
        session['modo_visao'] = session.get('modo_visao', 'cliente')
        session['nivel_acesso_atual'] = session.get('nivel_acesso_atual', 10)

        # 🔑 FIXAÇÃO CRÍTICA DE CONTEXTO DO MÓDULO
        session['modulo_slug_atual'] = modulo_atual_slug

        # Limpeza cirúrgica da sessão temporária de cadastro
        session.pop('temp_cadastro_email', None)
        session.pop('temp_cadastro_senha', None)

        flash("Validação realizada com sucesso. Acesso liberado!", "success")

    except Exception as err:
        db.session.rollback()
        print(f"❌ [ALFÂNDEGA] Erro crítico ao processar identidade civil: {err}")
        flash("Ocorreu um erro interno ao processar seus dados. Tente novamente.", "danger")
        return redirect(url_for('auth.validar_identidade_tela', next_url=proximo_passo))

    # =========================================================================
    # 🧭 5. RESOLUÇÃO DE REDIRECIONAMENTO DINÂMICO (SEM HUB)
    # =========================================================================

    # Prioridade 1: Destino de onboarding acionado durante a gravação (ex: convite)
    if destino_prioritario:
        return redirect(destino_prioritario)

    # Sanitização do proximo_passo (descarta URLs que levem ao Hub ou Login)
    if proximo_passo and ('/hub' in proximo_passo or 'auth.hub' in proximo_passo or '/login' in proximo_passo):
        print(f"⚠️ [ALFÂNDEGA] 'next_url' descartado por conter rota de Hub/Login: {proximo_passo}")
        proximo_passo = None

    # Prioridade 2: Destino explícito válido informado via requisição
    if proximo_passo and proximo_passo.startswith('/'):
        return redirect(proximo_passo)

    # Prioridade 3: Consulta o catálogo ModulosSistema pelo slug do módulo ativo
    modulo_info = ModulosSistema.query.filter_by(slug=modulo_atual_slug, ativo=True).first()

    if modulo_info and modulo_info.endpoint:
        try:
            url_destino = url_for(modulo_info.endpoint)
            if '/hub' not in url_destino:
                print(f"🚀 [ALFÂNDEGA] Direcionando para o endpoint do módulo '{modulo_atual_slug}': {url_destino}")
                return redirect(url_destino)
            print(f"⚠️ [ALFÂNDEGA] Endpoint no banco para '{modulo_atual_slug}' aponta para o Hub. Aplicando fallback.")
        except Exception as err:
            print(f"⚠️ [ROTEADOR] Falha ao resolver endpoint '{modulo_info.endpoint}' do banco: {err}")

    # Prioridade 4: Fallback absoluto do Core (Dashboard/Painel principal, nunca Hub)
    return redirect(url_for('core.dashboard'))

    
@auth_bp.route('/admin/esteira-manutencao', methods=['POST'])
# 🔒 Coloque seu decorador de segurança aqui (ex: @admin_required)
@modulo_required
def rodar_esteira_manutencao():
    if getattr(current_user, 'nivel_acesso', 0) < 100:
        return jsonify({"status": "error", "message": "Acesso restrito."}), 403

    from feedin.tools.manutencao import CaixaFerramentasManutencao
    relatorio_geral = {}

    try:
        # 🔧 Executa a Ferramenta 1: Saneamento de Épocas
        status_epocas = CaixaFerramentasManutencao.sanear_termos_atualmente(db)
        relatorio_geral["saneamento_epocas"] = status_epocas

        # 🔧 [Futuro] Executa a Ferramenta 2...
        # relatorio_geral["tarefa_2"] = CaixaFerramentasManutencao.tarefa_2(database)

        # Se todas as tarefas acopladas rodarem com sucesso, consolidamos o banco de uma vez
        db.session.commit()

        return jsonify({
            "status": "success",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "relatorio": relatorio_geral
        })

    except Exception as e:
        db.session.rollback()
        print(f"Falha na esteira de manutenção: {e}")
        return jsonify({
            "status": "error",
            "message": f"Erro crítico durante a execução da esteira: {str(e)}"
        }), 500


# =========================================================================
# 🎯 Correção: Removido o '/auth' daqui de dentro, pois o Blueprint já insere o prefixo automaticamente!
# =========================================================================
# 🔗 PASSO 3: VALIDAÇÃO DO TOKEN DE REIVINDICAÇÃO (CLAIM)
# =========================================================================
@auth_bp.route('/reivindicar-cadastro/<string:token>', methods=['GET'])
def validar_token_claim(token):
    """
    Passo 3: Identifica o token, valida a regra dos 5 dias e intercepta o fluxo,
    direcionando o usuário para a esteira orgânica de cadastro padrão.
    """
    from datetime import datetime, timedelta

    # 🛡️ SEGURANÇA MESTRE: Se o usuário estiver logado no Core ou em outra conta na sessão local,
    # nós deslogamos ele imediatamente para não misturar os IDs no momento de criar o vínculo do negócio.
    if current_user.is_authenticated:
        logout_user()

    # 1. BUSCA O PROCESSO PELO TOKEN
    processo = EseProcessoClaim.query.filter_by(token_validacao=token).first()

    if not processo:
        # 🎯 RETENÇÃO: Token inexistente ou já limpo da base. Prende no Login.
        flash("🔒 Link de validação inválido, já utilizado ou expirado. Caso já tenha concluído, faça seu login.", "danger")
        return redirect(url_for('auth.login', next_url=url_for('empresa.dashboard_empresa')))

    # 2. VALIDAÇÃO DO PRAZO EXPIRADO (5 DIAS)
    # =====================================================================
    # ⏳ VALIDAÇÃO DO PRAZO DE 5 DIAS (BLINDAGEM DE FUSO HORÁRIO - NAIVE)
    # =====================================================================
    data_atual_naive = datetime.utcnow()
    data_inicio_processo = processo.data_inicio.replace(tzinfo=None) if processo.data_inicio.tzinfo else processo.data_inicio

    if data_atual_naive > (data_inicio_processo + timedelta(days=5)):
        local = Local.query.get(processo.local_id)
        if local:
            local.status_operacional = 'ativo'
        db.session.delete(processo)
        db.session.commit()

        flash("⏳ O prazo de 5 dias expirou durante o processo. O local voltou a ficar disponível.", "warning")
        return redirect(url_for('auth.login'))

    # 3. FIXAÇÃO DE CONTEXTO DO MÓDULO COMERCIAL
    session['modulo_slug_atual'] = 'empresa'
    session['next_url'] = url_for('empresa.dashboard_empresa')
    session.permanent = True

    # 4. INTERCEPTAÇÃO E DIRECIONAMENTO PARA A ESTEIRA ORGÂNICA
    url_final_match = url_for('auth.concluir_vinculo_claim', token=token)

    print(f"🔀 Redirecionando Claim do local ID {processo.local_id} para a esteira orgânica do Módulo Empresa.")

    return redirect(url_for(
        'auth.cadastro_inicio',
        email_inicial=processo.alvo_comunicacao,
        next_url=url_final_match
    ))


@auth_bp.route('/cadastro/concluir-claim/<string:token>')
@modulo_required
def concluir_vinculo_claim(token):
    from datetime import datetime, timezone, timedelta
    from feedin.modules.empresa.models import EseProcessoClaim
    from feedin.models import Local

    processo = EseProcessoClaim.query.filter_by(token_validacao=token).first()

    if not processo:  # [Manter validação de existência do processo]
        flash("🔒 Processo de reivindicação concluído ou expirado. Acesse sua conta para gerenciar seu local.", "info")
        return redirect(url_for('auth.login', next_url=url_for('empresa.dashboard_empresa')))

    # 1. VALIDAÇÃO DE PRAZO (5 DIAS)
    data_atual_naive = datetime.utcnow()
    data_inicio_processo = processo.data_inicio.replace(tzinfo=None) if processo.data_inicio.tzinfo else processo.data_inicio

    if data_atual_naive > (data_inicio_processo + timedelta(days=5)):
        local = Local.query.get(processo.local_id)
        if local:
            local.status_operacional = 'ativo'  # Devolve o local para o mapa geral
        db.session.delete(processo)
        db.session.commit()
        flash("⏳ O prazo de 5 dias expirou durante o processo. O local voltou a ficar disponível.", "warning")
        return redirect(url_for('auth.login'))

    local = Local.query.get_or_404(processo.local_id)

    try:
        # 2. ATUALIZAÇÃO DOS STATUS OPERACIONAIS DA ESTEIRA
        # O local agora foi reivindicado por um usuário real validado
        local.id_empreendedor = current_user.id
        local.status_operacional = 'verificado'

        # Avança o status do processo para a fase de preenchimento de conteúdo/design
        processo.status_processo = 'preenchimento_dados'
        if processo.usuario_solicitante_id is None:
            processo.usuario_solicitante_id = current_user.id

        # 3. CONFIGURAÇÃO DE SESSÃO DO TENANT ATIVO
        session['local_id_atual'] = local.id
        session['modo_configuracao_ativo'] = True
        session.permanent = True

        # Limpeza de resíduos voláteis
        session.pop('temp_cadastro_email', None)
        session.pop('temp_cadastro_senha', None)

        # 4. DIRECIONAMENTO INTELIGENTE
        # Como o usuário já está logado (@modulo_required), mandamos direto para a dashboard
        destino_final = url_for('empresa.dashboard_empresa')
        session['next_url'] = destino_final

        db.session.commit()
        print(f"🏁 [CLAIM AVANÇADO] Processo {processo.id} atualizado para 'preenchimento_dados'. Redirecionando.")

        flash("✨ Identidade confirmada e estabelecimento vinculado! Vamos configurar sua empresa.", "success")
        return redirect(destino_final)

    except Exception as e:
        db.session.rollback()
        print(f"❌ ERRO REAL NO FECHAMENTO DO CLAIM: {str(e)}")
        flash("Erro sistêmico ao processar o vínculo final. Por favor, tente novamente ou contate o suporte.", "danger")
        return redirect(url_for('empresa.dashboard_empresa'))


def generar_username_corporativo(nome_real):
    """
    Gera um username imutável no padrão informática: primeiro.ultimo[+numero]
    Elimina preposições (de, da, do, das, dos).
    Exemplo: Carlos Cesar de Oliveira -> carlos.oliveira
    Exemplo 2: Carlos Silva de Oliveira (Havendo colisão) -> carlos.oliveira+2
    """
    import re

    # 1. Limpeza inicial: minúsculo e remove acentos/caracteres especiais
    nome_fatiado = nome_real.lower().split()

    # Lista de conectores/preposições para eliminar do mapa
    preposicoes = {'de', 'da', 'do', 'das', 'dos', 'e'}
    nomes_limpos = [n for n in nome_fatiado if n not in preposicoes and n.isalpha()]

    if not nomes_limpos:
        return f"user.{random.randint(100, 999)}"

    # 2. Monta a base: primeiro.ultimo
    primeiro = nomes_limpos[0]
    ultimo = nomes_limpos[-1] if len(nomes_limpos) > 1 else ""

    if ultimo:
        base_username = f"{primeiro}.{ultimo}"
    else:
        base_username = primeiro  # Fallback caso o usuário digite apenas um nome

    # 3. Busca colisões no banco que comecem com essa base exata
    usuarios_com_mesma_base = ModCadastroCliente.query.filter(
        ModCadastroCliente.username_modulo.like(f"{base_username}%")
    ).all()

    if not usuarios_com_mesma_base:
        # 🎯 PIONEIRO: Padrão limpo sem número
        return base_username

    # 4. Tratamento do sufixo com "+" para controle de colisões
    maior_sufixo = 1

    for u in usuarios_com_mesma_base:
        # Verifica se existe o caractere '+' separando o número
        if '+' in u.username_modulo:
            try:
                sufixo_str = u.username_modulo.split('+')[-1]
                if sufixo_str.isdigit():
                    num = int(sufixo_str)
                    if num > maior_sufixo:
                        maior_sufixo = num
            except ValueError:
                continue
        else:
            # É o pioneiro que não tem o '+' ainda
            continue

    # 🚀 O próximo entrante herda o padrão formal de TI
    proximo_numero = maior_sufixo + 1
    return f"{base_username}+{proximo_numero}"


@auth_bp.context_processor
def injetar_contexto_plataforma():
    """
    🪐 ENGINE POLIMÓRFICA GLOBAL (FEEDIN)
    Resolve layouts dinamicamente e distribui tabelas mestre do Core
    para todas as telas de autenticação e homologação civil.
    """
    # 1. Resolução do Layout Dinâmico
    modulo_solicitante = session.get('modulo_slug_atual')
    modulo_info = None

    if modulo_solicitante:
        modulo_info = ModulosSistema.query.filter_by(slug=modulo_solicitante, ativo=True).first()

    if modulo_info:
        base_layout = f"{modulo_info.slug}/base_{modulo_info.slug}.html"
    else:
        base_layout = "auth/base_auth.html"

    # 2. Resolução da Tabela de Gêneros Padrão Global
    # Alimenta o {% for g in generos %} automaticamente sem mexer nas rotas
    lista_generos = Generos.query.order_by(Generos.id).all()

    # Retorna o dicionário que o Jinja2 distribui para os templates
    return dict(
        base_layout=base_layout,
        modulo_info=modulo_info,
        modulos_sistema=modulo_info,
        generos=lista_generos
    )


@auth_bp.route('/cadastro-organico', methods=['GET', 'POST'])
def cadastro_organico_fluxo():
    """
    FUNIL ORGÂNICO REAL: Controla a entrada de usuários via link/QR Code.
    Faz a varredura cruzada usando o Hash do CPF para garantir integridade absoluta.
    """
    if request.method == 'GET':
        # Renderiza a tela inicial que pede APENAS o CPF para iniciar o funil
        return render_template('agenda/cadastro_organico_cpf.html')

    # PROCESSAMENTO DO POST (Usuário digitou o CPF e avançou)
    cpf_digitado = request.form.get('cpf', '').strip()

    # 1. TRATAMENTO NA ENTRADA: Limpa caracteres e gera o Hash idêntico ao do Core
    cpf_limpo = "".join(filter(str.isdigit, cpf_digitado))

    if len(cpf_limpo) != 11:
        flash("Por favor, informe um CPF válido com 11 dígitos.", "warning")
        return redirect(url_for('agenda.cadastro_organico_fluxo'))

    # Aciona o método estático do Core para gerar o hash de busca
    cpf_hash_procurado = IdentidadeCivil.gerar_hash(cpf_limpo)

    # 2. VARREDURA CRUZADA EM SEGUNDO PLANO
    existe_no_core = IdentidadeCivil.query.filter_by(cpf_hash=cpf_hash_procurado).first()
    existe_no_modulo = ModCadastroCliente.query.filter_by(cpf_hash=cpf_hash_procurado).first()

    # =====================================================================
    # TOMADA DE DECISÃO: AS 4 LINHAS DE AÇÃO
    # =====================================================================

    # 🔴 LINHA 1: CPF Inédito em Ambos (O Verdadeiro Cadastro Novo)
    if not existe_no_core and not existe_no_modulo:
        # Libera o restante do formulário passando o CPF limpo para o próximo passo
        # Armazenamos temporariamente na sessão ou passamos via parâmetro para o form completo
        session['cadastro_cpf_limpo'] = cpf_limpo
        return redirect(url_for('agenda.cadastro_organico_novo_formulario'))

    # 🔵 LINHA 2: O CPF já existe na Cidade (Core), mas NÃO nos Módulos
    if existe_no_core and not existe_no_modulo:
        flash(
            "Identificamos que você já possui cadastro no FeedIn! Digite sua senha da cidade para ativar seu acesso a este módulo.",
            "success")
        # Redireciona para a rota invisível de vinculação que vai exigir a senha do Core
        return redirect(
            url_for('agenda.vincular_conta_core', usuario_id=existe_no_core.usuario_id, cpf_limpo=cpf_limpo))

    # 🟡 LINHA 3: O CPF já existe nos Módulos, mas NÃO na Cidade (Core)
    if existe_no_modulo and not existe_no_core:
        flash("Você já utiliza nossos serviços de conveniência! Digite sua senha de acesso para continuar.", "info")
        # Desafia a senha local do módulo que já existe
        return redirect(url_for('agenda.desafiar_senha', cliente_id=existe_no_modulo.id))

    # 🟢 LINHA 4: O CPF já existe em Ambos e estão Atrelados
    if existe_no_modulo and existe_no_core:
        # Usuário totalmente regularizado. Vai direto para o fluxo padrão de login (senha do módulo)
        return redirect(url_for('agenda.desafiar_senha', cliente_id=existe_no_modulo.id))

    # Fallback de segurança
    return redirect(url_for('agenda.cadastro_organico_fluxo'))


from werkzeug.security import check_password_hash


@auth_bp.route('/desafiar-senha-equipe/<int:usuario_id>', methods=['GET', 'POST'])
def desafiar_senha_equipe(usuario_id):
    # Garante que o usuário realmente existe no Core
    usuario = Usuario.query.get_or_404(usuario_id)

    if request.method == 'GET':
        # Renderiza uma tela elegante de senha (podemos usar um template focado)
        return render_template('agenda/senha_equipe.html', usuario=usuario)

    # Processamento do POST
    senha_digitada = request.form.get('senha_equipe', '').strip()

    if not senha_digitada:
        flash("Por favor, digite sua senha de acesso.", "warning")
        return redirect(url_for('agenda.desafiar_senha_equipe', usuario_id=usuario.id))

    # Valida a senha usando o hash do modelo Usuario do Core
    # (Ajuste 'senha_hash' para o nome exato do campo de senha no seu model Usuario)
    if not check_password_hash(usuario.senha_hash, senha_digitada):
        flash("Senha incorreta. Por favor, tente novamente.", "danger")
        return redirect(url_for('agenda.desafiar_senha_equipe', usuario_id=usuario.id))

    # Sucesso! O usuário foi autenticado com sucesso.
    flash(f"Bem-vindo de volta, {usuario.nome}! Painel operacional liberado.", "success")

    # Redireciona direto para a nossa Home de Negócios Dinâmica
    return redirect(url_for('agenda.home_negocios'))


@auth_bp.route('/contexto/<string:cliente_id>/<int:local_id>', methods=['GET'])
def obter_contexto_cliente(cliente_id, local_id):
    """
    Retorna os dados de identificação e o contexto histórico do cliente para um local específico.
    A Agenda chama essa rota para saber se o cliente já passou pelo onboarding.
    """
    # Certifique-se de que ModCadastroCliente e AthAtribContexto estão importados no topo deste routes.py
    cliente = ModCadastroCliente.query.get(cliente_id)
    if not cliente:
        return jsonify({"erro": "Cliente não encontrado"}), 404

    # Busca se já existe o registro de "ano de início de relacionamento" para este estabelecimento
    atrib = AthAtribContexto.query.filter_by(
        cadastro_cliente_id=cliente.id,
        local_id=local_id,
        chave='ano_inicio_relacionamento'
    ).first()

    return jsonify({
        "cliente_id": cliente.id,
        "nome_preferencia": cliente.nome.split()[0],  # Fallback para o primeiro nome
        "apelido": cliente.apelido,
        "foto_path": cliente.foto_path,
        "whatsapp": cliente.whatsapp,
        "cadastro_completo": bool(cliente.foto_path and atrib)  # Flag que indica se já fez o onboarding nesse local
    }), 200


@auth_bp.route('/logout', methods=['GET'])
def logout():
    """
    🚪 LOGOUT INTEGRADO E ADAPTATIVO
    Limpa o estado de login do Flask-Login (Core) e destrói
    completamente as credenciais comerciais de 36 caracteres da sessão.
    """
    # 1. Limpa a autenticação padrão do Flask-Login (se houver)
    if current_user.is_authenticated:
        logout_user()

    # 2. Destrói as chaves de controle comercial que criamos para os módulos
    # para garantir que ninguém herde IDs de sessões passadas
    chaves_modulo = [
        'cliente_modulo_id',
        'modulo_slug_atual',
        'empresa_id_atual',
        'local_id_atual',
        'next_url'
    ]
    for chave in chaves_modulo:
        session.pop(chave, None)

    # Nota: Não usamos session.clear() direto porque você pode ter variáveis temporárias
    # úteis do processo de Claim (como 'temp_cadastro_email') que não quer perder imediatamente.

    flash("Você saiu do sistema com segurança.", "info")
    return redirect(url_for('auth.login'))