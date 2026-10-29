import re
import uuid
import random
import urllib.parse
from datetime import datetime, timezone, timedelta, date
from urllib.parse import unquote, urlparse

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
                                           ColaboradorContrato, ColaboradorDetalhesPessoais, ClienteBeneficiario, )

from feedin.utils import validar_cpf_estrutura, validar_hash_senha

# =========================================================================
# ⚙️ FUNÇÕES AUXILIARES DO MÓDULO AUTH
# =========================================================================
def gerar_username_unico(nome):
    """Gera um username amigável e único com base no primeiro nome."""
    base = "".join(filter(str.isalnum, nome.lower().split()[0]))
    return f"{base}{random.randint(100, 999)}"


# =========================================================================
# 🔑 PORTAL DE LOGIN UNIVERSAL & ISOLAMENTO DE MÓDULOS (SSO ISOLADO)
# =========================================================================
@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    from feedin.models import ModulosSistema
    from feedin.modules.auth.models import ModCadastroCliente, ModVinculoModulo
    from flask_login import login_user, current_user
    from urllib.parse import unquote, urlparse

    # -------------------------------------------------------------------------
    # 1. CAPTURA UNIFICADA E CONSISTENTE DE PARÂMETROS
    # -------------------------------------------------------------------------
    next_url = (
            request.args.get('next')
            or request.args.get('next_url')
            or request.form.get('next')
            or request.form.get('next_url')
    )

    # Prioriza o módulo passado explicitamente; fallback para a sessão do módulo atual ou 'agenda'
    modulo_slug = (
            request.args.get('modulo')
            or request.form.get('modulo_slug')
            or request.form.get('modulo')
            or session.get('modulo_slug_atual', 'agenda')
    )

    origem_hub = request.args.get('origem') == 'hub' or 'hub' in (next_url or '')
    if origem_hub:
        session['navegacao_via_hub'] = True

    form = FormLoginUniversal()

    # Sanitização estrita do next_url para evitar loops de redirecionamento para o login
    if next_url:
        next_url_decodificado = unquote(next_url)
        if any(auth_path in next_url_decodificado for auth_path in ['/login', '/auth', 'auth.login']):
            next_url = None
        else:
            parsed_url = urlparse(next_url_decodificado)
            if parsed_url.path:
                query_string = f"?{parsed_url.query}" if parsed_url.query else ""
                next_url = f"{parsed_url.path}{query_string}"

    chave_autenticacao_modulo = f"autenticado_modulo_{modulo_slug}"

    # -------------------------------------------------------------------------
    # 🔑 VALIDAÇÃO SE O USUÁRIO JÁ POSSUI SESSÃO ATIVA PARA ESTE MÓDULO
    # -------------------------------------------------------------------------
    if current_user.is_authenticated:
        if session.get(chave_autenticacao_modulo) is True:
            if next_url and next_url.startswith('/') and next_url != request.path and '/login' not in next_url:
                return redirect(next_url)

            modulo_info = ModulosSistema.query.filter_by(slug=modulo_slug, ativo=True).first()

            if modulo_info and modulo_info.endpoint and modulo_info.endpoint != 'auth.login':
                try:
                    url_destino = url_for(modulo_info.endpoint)
                    if url_destino != request.path:
                        return redirect(url_destino)
                except Exception as err:
                    current_app.logger.error(f"[LOGIN] Falha ao resolver endpoint do módulo '{modulo_slug}': {err}")

            try:
                return redirect(url_for(f"{modulo_slug}.dashboard_cliente"))
            except Exception:
                return redirect(url_for(f"{modulo_slug}.index"))

    # Atualiza o contexto da sessão para o módulo em foco
    session['modulo_slug_atual'] = modulo_slug
    modulo_info = ModulosSistema.query.filter_by(slug=modulo_slug, ativo=True).first()

    # -------------------------------------------------------------------------
    # 2. PROCESSAMENTO DE CREDENCIAIS / AUTENTICAÇÃO DO MÓDULO (POST)
    # -------------------------------------------------------------------------
    if form.validate_on_submit():
        email_digitado = form.email.data.strip().lower()
        senha_digitada = form.senha.data

        cliente_modulo = ModCadastroCliente.query.filter(
            ModCadastroCliente.email.ilike(email_digitado)
        ).first()

        if cliente_modulo and validar_hash_senha(cliente_modulo, senha_digitada):

            cpf_hash_alvo = getattr(cliente_modulo, 'cpf_hash', None)

            # Mesmo se o cpf_hash for nulo em cadastros antigos/novos, garantimos o vínculo por ID/E-mail para evitar travamentos
            try:
                query_vinculo = ModVinculoModulo.query.filter(
                    ModVinculoModulo.modulo_slug == modulo_slug,
                    db.or_(
                        ModVinculoModulo.email_customizado == cliente_modulo.email,
                        ModVinculoModulo.email_customizado.is_(None)
                    )
                )

                # Chave da verdade estrita para o ModVinculoModulo
                cpf_hash_alvo = getattr(cliente_modulo, 'cpf_hash', None) or f"temp_{cliente_modulo.id}"

                vinculo_ativo = ModVinculoModulo.query.filter(
                    ModVinculoModulo.modulo_slug == modulo_slug,
                    ModVinculoModulo.cpf_hash == cpf_hash_alvo,
                    ModVinculoModulo.email_customizado == cliente_modulo.email,
                    ModVinculoModulo.ativo == True
                ).first()

                if not vinculo_ativo:
                    vinculo_ativo = ModVinculoModulo(
                        cpf_hash=cpf_hash_alvo,
                        modulo_slug=modulo_slug,
                        local_id=session.get('local_id_atual'),
                        email_customizado=cliente_modulo.email,
                        ativo=True
                    )
                    db.session.add(vinculo_ativo)
                    db.session.commit()
                elif not vinculo_ativo.ativo:
                    vinculo_ativo.ativo = True
                    db.session.commit()
            except Exception as e:
                db.session.rollback()
                current_app.logger.error(f"⚠️ [LOGIN] Erro ao gravar ModVinculoModulo para novo usuário: {e}")

            # Efetiva o login no Flask-Login
            login_user(cliente_modulo, remember=getattr(form, 'lembrar_me', False) and form.lembrar_me.data)
            session.permanent = True

            # 🛡️ CHAVE MASTER DO ISOLAMENTO
            session[chave_autenticacao_modulo] = True
            session['cliente_modulo_id'] = str(cliente_modulo.id)
            session['usuario_id'] = str(cliente_modulo.id)
            session['modulo_slug_atual'] = modulo_slug

            flash(f"Acesso concedido ao módulo '{modulo_slug.upper()}'. Bem-vindo de volta!", "success")

            # Redirecionamento seguro pós-login
            if next_url and next_url.startswith('/') and '/login' not in next_url:
                return redirect(next_url)

            if modulo_info and modulo_info.endpoint:
                try:
                    url_destino = url_for(modulo_info.endpoint)
                    if url_destino != request.path and '/hub' not in url_destino:
                        return redirect(url_destino)
                except Exception as err:
                    current_app.logger.error(f"⚠️ [LOGIN] Erro no endpoint ({modulo_info.endpoint}): {err}")

            try:
                return redirect(url_for(f"{modulo_slug}.dashboard_cliente"))
            except Exception:
                return redirect(url_for(f"{modulo_slug}.index"))

        else:
            flash("E-mail ou senha incorretos. Verifique suas credenciais.", "danger")

    # Fallback seguro
    FALLBACK_ENDPOINTS = {
        'agenda': 'agenda.dashboard_cliente',
        'empresa': 'empresa.dashboard_empresa',
    }

    endpoint_padrao = FALLBACK_ENDPOINTS.get(modulo_slug, f"{modulo_slug}.dashboard")

    try:
        fallback_next = url_for(endpoint_padrao)
    except Exception:
        fallback_next = url_for('central_hub')

    return render_template(
        'auth/login_modulo.html',
        form=form,
        next_url=next_url or fallback_next,
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
    proximo_passo = (
        request.args.get('next_url')
        or request.args.get('next')
        or request.form.get('next_url', '')
    )

    # 🪐 2. RESOLUÇÃO DINÂMICA DE MÓDULO (Precedência: Query Params > Form > Sessão)
    modulo_slug = (
        request.args.get('modulo')
        or request.form.get('modulo_slug')
        or request.form.get('modulo')
        or session.get('modulo_slug_atual')
    )

    # Sincroniza a sessão imediatamente com o módulo da requisição atual
    if modulo_slug:
        session['modulo_slug_atual'] = modulo_slug

    modulo_info = None
    if modulo_slug:
        # Consulta o catálogo mestre de extensões de forma orientada a dados
        modulo_info = ModulosSistema.query.filter_by(slug=modulo_slug, ativo=True).first()

    # 🪐 3. RESOLUÇÃO DINÂMICA DO LAYOUT PAI COM PREFIXO DE BLUEPRINT
    if modulo_info:
        # Padronização: "empresa/base_empresa.html", "agenda/base_agenda.html", etc.
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
            modulos_sistema=modulo_info  # Alinha com o nome esperado pelo base_empresa.html
        )

    # 🏙️ FLUXO DE PROCESSAMENTO (POST): Coleta de Credenciais e Validação
    email = request.form.get('email', '').strip().lower()
    senha = request.form.get('senha', '')
    confirma_senha = request.form.get('confirma_senha', '')
    proximo_passo = request.form.get('next_url', '')

    # Validação 1: Campos nulos ou em branco
    if not email or not senha or not confirma_senha:
        flash("Todos os campos são obrigatórios.", "warning")
        return redirect(url_for('auth.cadastro_inicio', modulo=modulo_slug, next_url=proximo_passo))

    # Validação 2: Confirmação matemática de integridade da senha
    if senha != confirma_senha:
        flash("As senhas informadas não conferem.", "danger")
        return render_template(
            'auth/cadastro_inicio.html',
            email_inicial=email,
            next_url=proximo_passo,
            base_layout=base_layout,
            modulo_info=modulo_info,
            modulos_sistema=modulo_info
        )

    # =========================================================================
    # 🟢 INTEGRIDADE MULTI-MÓDULO: Validação pela Chave Tripla (CPF + Módulo + E-mail)
    # =========================================================================
    cliente_existente = ModCadastroCliente.query.filter_by(email=email).first()

    if cliente_existente and modulo_slug:
        # 🎯 Varre se este E-MAIL / CPF específico já possui vínculo ATIVO com o módulo ALVO
        possui_vinculo_modulo = ModVinculoModulo.query.filter(
            ModVinculoModulo.modulo_slug == modulo_slug,
            ModVinculoModulo.ativo == True,
            db.or_(
                ModVinculoModulo.email_customizado == email,
                db.and_(
                    ModVinculoModulo.cpf_hash == cliente_existente.cpf_hash,
                    ModVinculoModulo.email_customizado.is_(None)
                )
            )
        ).first()

        # O bloqueio de duplicidade SÓ ocorre se ESTE E-MAIL ESPECÍFICO já estiver vinculado a este módulo
        if possui_vinculo_modulo:
            flash(
                f"O e-mail '{email}' já possui um cadastro ativo no módulo '{modulo_slug.upper()}'. Faça login diretamente.",
                "info")
            return redirect(url_for('auth.login', modulo=modulo_slug, next_url=proximo_passo))

        # Nota de engenharia: Se 'cliente_existente' for verdadeiro, mas 'possui_vinculo_modulo' for None,
        # ele já é cliente no Core/outro módulo, mas não na 'agenda'. O fluxo prossegue normalmente
        # para a esteira de validação/claim, onde o novo vínculo será registrado.

    # 🪐 Força a sessão a ser permanente para persistir os estados no localhost:8000
    session.permanent = True

    # Prepara e empacota as credenciais na sessão temporária para a esteira civil subsequente
    session['temp_cadastro_email'] = email
    session['temp_cadastro_senha'] = senha

    next_url_atual = request.args.get('next_url') or request.form.get('next_url')

    return redirect(url_for('auth.validar_identidade_tela', modulo=modulo_slug, next_url=next_url_atual))


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


@auth_bp.route('/processar-identidade', methods=['GET', 'POST'])
def processar_identidade():
    """
    ALFÂNDEGA DE IDENTIDADE CIVIL (MOTOR AGNÓSTICO E DINÂMICO)

    Responsável por processar a identidade civil e assegurar os vínculos de
    acesso nos módulos. Todo o direcionamento operacional é guiado pela
    tabela 'ModulosSistema' e parâmetros de rota ('next_url').
    """
    print("\n----------------------------------------------------")
    print("🔍 [DEBUG] Entrou em processar-identidade via método:", request.method)

    # Recupera o e-mail da sessão ou do campo de transporte oculto do formulário
    email_cadastro = session.get('temp_cadastro_email') or request.form.get('email_transporte')
    print(f"🔍 [DEBUG] 1. email_cadastro obtido: {email_cadastro}")

    if not email_cadastro and current_user.is_authenticated:
        email_cadastro = current_user.email
        print(f"🔍 [DEBUG] 1.1. Recuperado via current_user: {email_cadastro}")

    if not email_cadastro:
        print("❌ [DEBUG] REDIRECIONANDO: email_cadastro ausente (Sessão expirada)!")
        flash("Sessão de identificação expirada. Por favor, reinicie o processo.", "warning")
        return redirect(url_for('auth.cadastro_inicio'))

    nome_real = request.form.get('nome_real', '').strip().upper()
    cpf_digitado = re.sub(r'\D', '', request.form.get('cpf', ''))
    data_nasc_str = request.form.get('data_nascimento')

    # 🎯 CAPTURA SEGURA DOS NOVOS CAMPOS NO TOPO
    genero_id = request.form.get("genero")
    estado_civil_id = request.form.get("estado_civil")

    print(
        f"🔍 [DEBUG] 2. Dados do form -> Nome preenchido: {bool(nome_real)}, CPF: {bool(cpf_digitado)}, Data: {bool(data_nasc_str)}, Gênero: {genero_id}, Estado Civil: {estado_civil_id}")

    modulo_atual_slug = session.get('modulo_slug_atual', 'core')
    local_atual_id = session.get('local_id_atual') or session.get('empresa_ativa_id')
    proximo_passo = request.args.get('next_url') or session.pop('next_url', None)
    whatsapp_input = request.form.get('whatsapp', '').strip()
    whatsapp_final = whatsapp_input if whatsapp_input else None

    # =========================================================================
    # 🛡️ 2. VALIDAÇÕES CRÍTICAS E ESTRUTURAIS
    # =========================================================================
    if not nome_real or not cpf_digitado or not data_nasc_str or not genero_id:
        print(f"❌ [DEBUG] REDIRECIONANDO: Campos obrigatórios vazios!")
        flash("Todos os campos de identidade são obrigatórios.", "warning")
        return redirect(url_for('auth.validar_identidade_tela', next_url=proximo_passo))

    if not validar_cpf_estrutura(cpf_digitado):
        print("❌ [DEBUG] REDIRECIONANDO: CPF inválido pela estrutura!")
        flash("O CPF informado é inválido.", "danger")
        return redirect(url_for('auth.validar_identidade_tela', next_url=proximo_passo))

    # 🔑 REGRA SOBERANA: O CPF HASH É O ÚNICO E VERDADEIRO ELO DE LIGAÇÃO
    hash_digitado = IdentidadeCivil.gerar_hash(cpf_digitado)
    identidade_existente = IdentidadeCivil.query.filter_by(cpf_hash=hash_digitado).first()

    # =========================================================================
    # 🔍 3. BUSCA DO ID CORE (Exclusivamente via IdentidadeCivil / CPF Hash)
    # =========================================================================
    id_usuario_core = None

    if identidade_existente and identidade_existente.usuario_id:
        id_usuario_core = identidade_existente.usuario_id
    elif current_user.is_authenticated and isinstance(current_user.id, int):
        id_usuario_core = current_user.id

    # Conflito de integridade se o CPF já estiver associado a outro Core diferente
    if identidade_existente and identidade_existente.usuario_id and id_usuario_core:
        if identidade_existente.usuario_id != id_usuario_core:
            print("❌ [DEBUG] REDIRECIONANDO: Conflito de CPF com outro usuário core!")
            flash("Este CPF já está vinculado a outra conta ativa no sistema.", "danger")
            return redirect(url_for('auth.validar_identidade_tela', next_url=proximo_passo))

    destino_prioritario = None

    # =========================================================================
    # 💾 4. GRAVAÇÃO ATÔMICA DA IDENTIDADE E VÍNCULO MESTRE
    # =========================================================================
    try:
        data_nasc_obj = datetime.strptime(data_nasc_str, '%Y-%m-%d').date()

        # Conversões seguras dos IDs numéricos
        genero_val = int(genero_id) if (genero_id and str(genero_id).isdigit()) else None
        estado_civil_val = int(estado_civil_id) if (estado_civil_id and str(estado_civil_id).isdigit()) else None

        # ---------------------------------------------------------------------
        # 🪐 PASSO 1: COFRE CIVIL GLOBAL (IdentidadeCivil)
        # ---------------------------------------------------------------------
        if not identidade_existente:
            cpf_protegido = current_app.fernet.encrypt(cpf_digitado.encode())

            identidade_existente = IdentidadeCivil(
                usuario_id=id_usuario_core,  # Pode ser None se for totalmente novo no Core
                nome_completo_oficial=nome_real,
                cpf_criptografado=cpf_protegido,
                cpf_hash=hash_digitado,
                data_nascimento=data_nasc_obj,
                ip_origem=request.remote_addr,
                versao_termos_aceita="1.0-BETA"
            )
            db.session.add(identidade_existente)
            db.session.flush()
        elif id_usuario_core and not identidade_existente.usuario_id:
            identidade_existente.usuario_id = id_usuario_core
            db.session.flush()

        # Atualiza o Perfil no Core APENAS se houver conta Core associada
        if id_usuario_core:
            perfil = Perfil.query.filter_by(id_usuario=id_usuario_core).first()
            if not perfil:
                perfil = Perfil(
                    id_usuario=id_usuario_core,
                    nome_completo=nome_real,
                    data_nascimento=data_nasc_obj,
                    genero=genero_val
                )
                db.session.add(perfil)
            else:
                perfil.nome_completo = nome_real
                perfil.data_nascimento = data_nasc_obj
                if genero_val is not None:
                    perfil.genero = genero_val

        # ---------------------------------------------------------------------
        # 🟢 PASSO 2: ESTEIRA DE CADASTRO DO MÓDULO (ModCadastroCliente)
        # ---------------------------------------------------------------------
        cliente_local = ModCadastroCliente.query.filter_by(email=email_cadastro).first()

        if cliente_local:
            cliente_local.usuario_id = id_usuario_core
            cliente_local.nome = nome_real
            cliente_local.cpf = cpf_digitado
            cliente_local.data_nascimento = data_nasc_obj
            cliente_local.genero = genero_val
            cliente_local.estado_civil = estado_civil_val
            if whatsapp_final:
                cliente_local.whatsapp = whatsapp_final
            cliente_local.status_conta = 'ativo'
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
                genero=genero_val,
                estado_civil=estado_civil_val,
                username_modulo=username_padronizado,
                senha_hash=senha_hash_local,
                status_conta='ativo'
            )
            cliente_local.cpf = cpf_digitado
            db.session.add(cliente_local)

        db.session.flush()

        # Desativa beneficiário antigo com o mesmo CPF hash
        ClienteBeneficiario.query.filter_by(
            documento_identificador=hash_digitado,
            ativo=True
        ).update({ClienteBeneficiario.ativo: False})

        db.session.commit()

        # ---------------------------------------------------------------------
        # 🚀 PASSO 3: VÍNCULO NO CATÁLOGO DE MÓDULOS (ModVinculoModulo)
        # ---------------------------------------------------------------------
        vinculo_ativo = ModVinculoModulo.query.filter_by(
            cpf_hash=hash_digitado,
            modulo_slug=modulo_atual_slug,
            local_id=local_atual_id
        ).first()

        if not vinculo_ativo:
            vinculo_ativo = ModVinculoModulo(
                cpf_hash=hash_digitado,
                modulo_slug=modulo_atual_slug,
                local_id=local_atual_id,
                email_customizado=email_cadastro,
                criado_em=datetime.now(timezone.utc),
                ativo=True
            )
            db.session.add(vinculo_ativo)
        else:
            vinculo_ativo.ativo = True
            vinculo_ativo.email_customizado = email_cadastro

        db.session.commit()

        # ---------------------------------------------------------------------
        # 🔒 AUTENTICAÇÃO E SESSÃO
        # ---------------------------------------------------------------------
        if not current_user.is_authenticated and cliente_local:
            login_user(cliente_local, remember=True)

        if cliente_local:
            session['cliente_modulo_id'] = cliente_local.id

        session['usuario_id'] = id_usuario_core
        session['modo_visao'] = session.get('modo_visao', 'cliente')
        session['nivel_acesso_atual'] = session.get('nivel_acesso_atual', 10)
        session['modulo_slug_atual'] = modulo_atual_slug

        session.pop('temp_cadastro_email', None)
        session.pop('temp_cadastro_senha', None)

        flash("Validação realizada com sucesso. Acesso liberado!", "success")
        print("✅ [DEBUG] Processo concluído com sucesso! Redirecionando para o destino...")

    except Exception as err:
        db.session.rollback()
        import traceback
        traceback.print_exc()
        print(f"❌ [ALFÂNDEGA] Erro crítico ao processar identidade civil: {err}")
        flash("Ocorreu um erro interno ao processar seus dados. Tente novamente.", "danger")
        return redirect(url_for('auth.validar_identidade_tela', next_url=proximo_passo))

    # =========================================================================
    # 🧭 5. RESOLUÇÃO DE REDIRECIONAMENTO DINÂMICO
    # =========================================================================
    if destino_prioritario:
        return redirect(destino_prioritario)

    if proximo_passo and '/login' in proximo_passo:
        proximo_passo = None

    if proximo_passo and proximo_passo.startswith('/'):
        return redirect(proximo_passo)

    if local_atual_id:
        return redirect(url_for('empresa.hub_empresa', empresa_id=local_atual_id))

    modulo_info = ModulosSistema.query.filter_by(slug=modulo_atual_slug, ativo=True).first()
    if modulo_info and modulo_info.endpoint:
        try:
            url_destino = url_for(modulo_info.endpoint)
            return redirect(url_destino)
        except Exception:
            pass

    return redirect(url_for('core.dashboard'))


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
@login_required  # 🛑 ALTERADO: Usa @login_required. O @login_required_geral não deve rodar aqui.
def concluir_vinculo_claim(token):
    from datetime import datetime, timezone, timedelta
    from feedin.modules.empresa.models import EseProcessoClaim, EseEmpresa
    from feedin.models import Local
    from feedin.modules.auth.models import ModCadastroCliente

    print(f"\n🔍 [DEBUG CLAIM] Iniciando conclusão para o token: {token}")

    processo = EseProcessoClaim.query.filter_by(token_validacao=token).first()

    if not processo:
        print("❌ [DEBUG CLAIM] Processo NÃO encontrado para este token.")
        flash("🔒 Processo de reivindicação concluído ou expirado. Acesse sua conta para gerenciar seu local.", "info")
        return redirect(url_for('auth.login', next_url=url_for('empresa.dashboard_empresa')))

    print(f"✅ [DEBUG CLAIM] Processo encontrado. ID do Processo: {processo.id}, Local ID: {processo.local_id}")

    # 1. VALIDAÇÃO DE PRAZO (5 DIAS)
    data_atual_naive = datetime.utcnow()
    data_inicio_processo = processo.data_inicio.replace(
        tzinfo=None) if processo.data_inicio.tzinfo else processo.data_inicio

    if data_atual_naive > (data_inicio_processo + timedelta(days=5)):
        print("⏳ [DEBUG CLAIM] Prazo de 5 dias expirado!")
        local = Local.query.get(processo.local_id)
        if local:
            local.status_operacional = 'ativo'
        db.session.delete(processo)
        db.session.commit()
        flash("⏳ O prazo de 5 dias expirou durante o processo. O local voltou a ficar disponível.", "warning")
        return redirect(url_for('auth.login'))

    local = Local.query.get_or_404(processo.local_id)
    print(f"🏠 [DEBUG CLAIM] Local obtido: {local.nome} (ID: {local.id})")

    try:
        # Resolve o ID do Core (Integer) para garantir integridade da FK do local se necessário
        user_core_id = getattr(current_user, 'usuario_id', None)
        if not user_core_id and isinstance(current_user.id, int):
            user_core_id = current_user.id
        if isinstance(user_core_id, str):
            cliente = ModCadastroCliente.query.get(current_user.id)
            if cliente:
                user_core_id = cliente.usuario_id

        # 🎯 CAPTURA CORRETA DO UUID DO PLEITEANTE PARA O PROPRIETARIO_ID
        uuid_pleiteante = str(getattr(current_user, 'id', ''))
        print(f"👤 [DEBUG CLAIM] Usuário logado ID: {uuid_pleiteante} | User Core ID: {user_core_id}")

        # 2. ATUALIZAÇÃO DOS STATUS OPERACIONAIS DA ESTEIRA
        local.id_empreendedor = user_core_id or current_user.id
        local.status_operacional = 'verificado'

        # =========================================================================
        # 🏢 GARANTE O NASCIMENTO DA EMPRESA NA TABELA ESE_EMPRESA
        # =========================================================================
        empresa_existente = EseEmpresa.query.filter(
            (EseEmpresa.id == local.id) | (EseEmpresa.local_id == local.id)
        ).first()

        if not empresa_existente:
            print(f"🏢 [DEBUG CLAIM] Empresa não encontrada para o ID {local.id}. Criando nova...")
            nova_ese_empresa = EseEmpresa(
                id=local.id,  # Mantém o ID alinhado
                proprietario_id=uuid_pleiteante,  # UUID do pleiteante
                local_id=local.id,
                nome=local.nome,
                status_homologacao='provisorio_beta',
                termo_responsabilidade_aceito=True
            )
            db.session.add(nova_ese_empresa)
            print(f"✅ [DEBUG CLAIM] Instância da EseEmpresa adicionada à sessão com ID: {local.id}")
        else:
            print(f"🏢 [DEBUG CLAIM] Empresa já existe (ID: {empresa_existente.id}). Atualizando proprietário...")
            empresa_existente.proprietario_id = uuid_pleiteante

        # Avança o status do processo
        processo.status_processo = 'preenchimento_dados'
        if user_core_id:
            processo.usuario_solicitante_id = user_core_id

        # 3. CONFIGURAÇÃO DE SESSÃO DO TENANT ATIVO
        session['local_id_atual'] = local.id
        session['modo_configuracao_ativo'] = True
        session.permanent = True

        session['cliente_modulo_id'] = str(current_user.id)
        session['cliente_id_empresa'] = str(current_user.id)

        # Limpeza de resíduos voláteis
        session.pop('temp_cadastro_email', None)
        session.pop('temp_cadastro_senha', None)

        # 4. DIRECIONAMENTO
        destino_final = url_for('empresa.dashboard_empresa')
        session['next_url'] = destino_final

        db.session.commit()
        print(f"🏁 [DEBUG CLAIM] Transação commitada com sucesso! Redirecionando para {destino_final}")

        flash("✨ Identidade confirmada e estabelecimento vinculado! Vamos configurar sua empresa.", "success")
        return redirect(destino_final)

    except Exception as e:
        db.session.rollback()
        # 🛑 CAPTURA DETALHADA DO ERRO NO CONSOLE
        print(f"❌ [ERRO CRÍTICO NO CLAIM] Falha ao fechar o processo: {repr(e)}")
        import traceback
        traceback.print_exc()

        flash(f"Erro sistêmico ao processar o vínculo final: {str(e)}", "danger")
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


# Certifique-se de ter importado EseConviteColaborador no topo do arquivo:
# from feedin.modules.empresa.models import EseConviteColaborador

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

    # 1. TRATAMENTO NA ENTRADA: Limpa caracteres não numéricos
    cpf_limpo = "".join(filter(str.isdigit, cpf_digitado))

    if len(cpf_limpo) != 11:
        flash("Por favor, informe um CPF válido com 11 dígitos.", "warning")
        return redirect(url_for('agenda.cadastro_organico_fluxo'))

    # 2. GERAÇÃO DOS HASHES DE BUSCA
    # Hash padrão do Core / Identidade Civil
    cpf_hash_procurado = IdentidadeCivil.gerar_hash(cpf_limpo)

    # Hash do Módulo Empresa (para consulta de convites de colaboradores)
    hash_digitado = EseConviteColaborador.gerar_hash_cpf(cpf_limpo)

    # 3. VARREDURA CRUZADA EM SEGUNDO PLANO
    existe_no_core = IdentidadeCivil.query.filter_by(cpf_hash=cpf_hash_procurado).first()
    existe_no_modulo = ModCadastroCliente.query.filter_by(cpf_hash=cpf_hash_procurado).first()
    convite_colaborador = EseConviteColaborador.query.filter_by(cpf_hash=hash_digitado, status='PENDENTE').first()

    # =====================================================================
    # TOMADA DE DECISÃO: LINHAS DE AÇÃO
    # =====================================================================

    # 🔴 LINHA 1: CPF Inédito em Ambos (O Verdadeiro Cadastro Novo)
    if not existe_no_core and not existe_no_modulo:
        # Armazena temporariamente na sessão para o próximo passo
        session['cadastro_cpf_limpo'] = cpf_limpo
        return redirect(url_for('agenda.cadastro_organico_novo_formulario'))

    # 🔵 LINHA 2: O CPF já existe na Cidade (Core), mas NÃO nos Módulos
    if existe_no_core and not existe_no_modulo:
        flash(
            "Identificamos que você já possui cadastro no FeedIn! Digite sua senha da cidade para ativar seu acesso a este módulo.",
            "success"
        )
        return redirect(
            url_for('agenda.vincular_conta_core', usuario_id=existe_no_core.usuario_id, cpf_limpo=cpf_limpo)
        )

    # 🟡 LINHA 3: O CPF já existe nos Módulos, mas NÃO na Cidade (Core)
    if existe_no_modulo and not existe_no_core:
        flash("Você já utiliza nossos serviços de conveniência! Digite sua senha de acesso para continuar.", "info")
        return redirect(url_for('agenda.desafiar_senha', cliente_id=existe_no_modulo.id))

    # 🟢 LINHA 4: O CPF já existe em Ambos e estão Atrelados
    if existe_no_modulo and existe_no_core:
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
    completamente as credenciais e contextos de módulos da sessão.
    """
    if current_user.is_authenticated:
        logout_user()

    # Destrói chaves comerciais, IDs de módulos e limpa vestígios de sessões passadas
    chaves_para_remover = [
        'cliente_modulo_id',
        'usuario_id',
        'modulo_slug_atual',
        'empresa_id_atual',
        'local_id_atual',
        'empresa_ativa_id',
        'next_url',
        'temp_cadastro_email',
        'temp_cadastro_senha',
        'navegacao_via_hub'
    ]
    for chave in chaves_para_remover:
        session.pop(chave, None)

    flash("Você saiu do sistema com segurança.", "info")
    return redirect(url_for('auth.login'))


@auth_bp.route('/trocar-modulo/<string:modulo_destino>/<int:empresa_id>')
@login_required
def trocar_modulo_gateway(modulo_destino, empresa_id):
    """
    Ponte de Transição entre Módulos do Ecossistema
    """
    modulo_origem = request.args.get('origem', session.get('modulo_slug_atual', 'empresa'))

    current_app.logger.info(
        f"🔄 [GATEWAY] Transição solicitada: '{modulo_origem}' -> '{modulo_destino}' (Empresa #{empresa_id})")

    # 1. Atualiza a agulha da bússola na sessão
    session['modulo_slug_atual'] = modulo_destino

    # 2. Redireciona para o destino final daquele módulo
    if modulo_destino == 'agenda':
        return redirect(url_for('agenda.painel_agenda', empresa_id=empresa_id))
    elif modulo_destino == 'empresa':
        return redirect(url_for('empresa.dashboard_empresa', empresa_id=empresa_id))

    return redirect(url_for('empresa.dashboard_empresa'))


@auth_bp.route('/colaborador/ficha/<int:convite_id>', methods=['GET', 'POST'])
@login_required
def preencher_ficha_colaborador(convite_id):
    """
    📋 PREENCHIMENTO DA FICHA DE AUTODECLARAÇÃO DO COLABORADOR
    Recebe convite_id diretamente da URL para isolamento e rápida validação.
    """
    # 1. Busca o convite correspondente ao ID informado na URL
    convite = EseConviteColaborador.query.get_or_404(convite_id)

    # 2. Defesa de Perímetro: Valida se o CPF do usuário logado bate com o convite
    cpf_bruto = getattr(current_user, 'cpf', '')
    cpf_limpo = re.sub(r'\D', '', str(cpf_bruto)) if cpf_bruto else None

    cpf_hash_user = None
    if cpf_limpo and hasattr(EseConviteColaborador, 'gerar_hash_cpf'):
        cpf_hash_user = EseConviteColaborador.gerar_hash_cpf(cpf_limpo)

    is_dono_convite = (cpf_hash_user and convite.cpf_hash == cpf_hash_user)
    is_admin = getattr(current_user, 'nivel_acesso', 0) >= 9999

    if not is_dono_convite and not is_admin:
        flash("Acesso não autorizado a este convite.", "danger")
        return redirect(url_for('agenda.dashboard_cliente'))

    # 3. Busca se os detalhes já existem usando o convite_id da rota
    detalhes = ColaboradorDetalhesPessoais.query.filter_by(convite_id=convite_id).first()

    if request.method == 'POST':
        try:
            # Se ainda não existir registro, instancia usando diretamente o convite_id da URL
            if not detalhes:
                detalhes = ColaboradorDetalhesPessoais(convite_id=convite_id)
                db.session.add(detalhes)

            # 👤 Dados Civis e Pessoais
            detalhes.nome_completo = request.form.get('nome_completo', '').strip()
            detalhes.nome_exibicao_pwa = request.form.get('nome_exibicao_pwa', '').strip()
            detalhes.cpf = request.form.get('cpf', '').strip()
            detalhes.cnpj = request.form.get('cnpj', '').strip() or None
            detalhes.estado_civil = request.form.get('estado_civil', '').strip()

            # 📍 Endereço Completo
            detalhes.logradouro = request.form.get('logradouro', '').strip()
            detalhes.numero = request.form.get('numero', '').strip()
            detalhes.complemento = request.form.get('complemento', '').strip() or None
            detalhes.bairro = request.form.get('bairro', '').strip()
            detalhes.cidade = request.form.get('cidade', '').strip()
            detalhes.estado = request.form.get('estado', '').strip().upper()
            detalhes.cep = request.form.get('cep', '').strip()

            # 📞 Contatos de Segurança e Saúde
            detalhes.telefone_pessoal = request.form.get('telefone_pessoal', '').strip()
            detalhes.contato_emergencia_nome = request.form.get('contato_emergencia_nome', '').strip() or None
            detalhes.contato_emergencia_fone = request.form.get('contato_emergencia_fone', '').strip() or None
            detalhes.tipo_sanguineo = request.form.get('tipo_sanguineo', '').strip() or None

            # 👕 Vestuário e Uniformes
            detalhes.tamanho_camiseta = request.form.get('tamanho_camiseta', '').strip() or None
            detalhes.tamanho_calca = request.form.get('tamanho_calca', '').strip() or None
            detalhes.tamanho_calcado = request.form.get('tamanho_calcado', '').strip() or None

            # 🏦 Dados Bancários
            detalhes.banco_nome = request.form.get('banco_nome', '').strip() or None
            detalhes.agencia = request.form.get('agencia', '').strip() or None
            detalhes.conta_corrente = request.form.get('conta_corrente', '').strip() or None
            detalhes.chave_pix = request.form.get('chave_pix', '').strip() or None

            db.session.commit()
            flash("Ficha cadastral salva com sucesso!", "success")
            return redirect(url_for('agenda.dashboard_cliente'))

        except Exception as e:
            db.session.rollback()
            print(f"❌ Erro ao salvar detalhes do colaborador: {e}")
            flash("Erro técnico ao salvar a ficha. Verifique os campos.", "danger")

    return render_template(
        'empresa/ficha_admissao_notificacao.html',
        convite=convite,
        detalhes=detalhes
    )


from flask import render_template, redirect, url_for, request, flash, current_app
from flask_login import login_required, current_user
from feedin import database, bcrypt
from . import auth_bp
from .models import ModCadastroCliente


@auth_bp.route('/esqueci-senha', methods=['GET', 'POST'])  # <-- Ajustado para a rota correta do link do template
def esqueci_senha():  # <-- O nome da função precisa ser 'esqueci_senha' para bater com o url_for('auth.esqueci_senha')
    """
    =============================================================================
    ORQUESTRADOR AUTH: RECUPERAÇÃO / ALTERAÇÃO DE SENHA (Módulos Conexos)
    =============================================================================
    Centraliza a rotina de alteração/recuperação de senha para os clientes cadastrados
    nos módulos conexos. Valida a senha atual via bcrypt no modelo unificado e
    persiste o novo hash com segurança.
    =============================================================================
    """
    # Se for uma requisição POST (veio do formulário de alteração)
    if request.method == 'POST':
        senha_atual = request.form.get('senha_atual')
        nova_senha = request.form.get('nova_senha')
        confirmacao = request.form.get('confirma_senha')

        # Valida se o usuário está autenticado para alterar a senha logado
        if not current_user.is_authenticated:
            flash('Você precisa estar conectado para alterar sua senha.', 'warning')
            return redirect(url_for('auth.login'))

        # 1. Valida se a senha atual confere com o registro do cliente logado
        if not bcrypt.check_password_hash(current_user.senha, senha_atual):
            flash('A senha atual está incorreta.', 'danger')
            return redirect(request.referrer or url_for('central_hub'))

        # 2. Valida se as novas senhas coincidem
        if nova_senha != confirmacao:
            flash('As novas senhas não coincidem.', 'danger')
            return redirect(request.referrer or url_for('central_hub'))

        # 3. Validação do tamanho mínimo de segurança
        if len(nova_senha) < 6:
            flash('A nova senha deve ter pelo menos 6 caracteres.', 'danger')
            return redirect(request.referrer or url_for('central_hub'))

        # 4. Processo de salvamento do novo hash no banco de dados
        try:
            hashed_password = bcrypt.generate_password_hash(nova_senha).decode('utf-8')
            current_user.senha = hashed_password
            database.session.commit()
            flash('Sua senha foi atualizada com sucesso!', 'success')
        except Exception as e:
            database.session.rollback()
            current_app.logger.error(f"Erro ao atualizar senha no orquestrador auth: {e}")
            flash('Erro interno ao atualizar a senha.', 'danger')

        return redirect(request.referrer or url_for('central_hub'))

    # Se for GET, renderiza a tela de solicitação/recuperação de senha
    return render_template('auth/esqueci_senha.html')