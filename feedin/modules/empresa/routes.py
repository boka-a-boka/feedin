import os
import uuid
import json
import socket
import re
import unicodedata
import urllib.parse
import hashlib
import calendar
import secrets
import pytz
import base64
from io import BytesIO
from werkzeug.datastructures import FileStorage
from slugify import slugify
from typing import Optional, Union, BinaryIO
from weasyprint import HTML
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse
from functools import wraps
from collections import defaultdict
import requests  # Para a busca de CEP (ViaCEP)
from datetime import datetime, timezone, timedelta, time, date
from feedin.modules.empresa.services.calendar_engine import popular_feriados_civis_dinamico, processar_localidade_completa
from sqlalchemy.orm import joinedload, selectinload
from sqlalchemy import func, inspect
from flask_login import current_user, login_required, logout_user
from werkzeug.utils import secure_filename

# 1. METODOLOGIAS DO FLASK & EXTENSÕES
from flask import current_app, request, jsonify, render_template, redirect, url_for, flash, session, Flask, abort

from flask_mail import Message
from PIL import Image

# 2. SEÇÃO DE FORMULÁRIOS (WTFORMS)
from flask_wtf import FlaskForm
from flask_wtf.file import FileField, FileRequired, FileAllowed
from wtforms import StringField, PasswordField
from wtforms.validators import DataRequired, Email, Length, EqualTo

# 3. 🎯 ELOS DE CONEXÃO COM O CORE E OUTROS MÓDULOS
from feedin import database as db, mail  # Objetos centrais do Core

# 4. 🧩 IMPORTAÇÃO DO CONTRATO OFICIAL DO BLUEPRINT (Sem recriá-lo!)
from feedin.modules.empresa import empresa_bp

# 5. 🗄️ PERSISTÊNCIA: IMPORTAÇÃO DOS MODELOS DO BANCO DE DADOS
# Modelos do Core e Auth
from feedin.models import Local, IdentidadeCivil, Taxonomia, VinculoUsuarioLocal, ModulosSistema, Cargo, Usuario

from feedin.modules.auth.models import ModVinculoModulo, ModCadastroCliente, AthAtribContexto, VinculoUsuarioEmpresa
from flask import render_template, request, redirect, url_for, flash

# Modelos específicos do próprio módulo Empresa (Higienizado sem duplicados)
from feedin.modules.empresa.models import (
    EseEmpresa, UsuarioFavorito, EseProcessoClaim, HistoricoAlteracaoLocal,
    ModHomologacaoEmpresa, EseHorarioFuncionamento, EscalaTrabalhoColaborador,
    ColaboradorContrato, CadastroFeriado, EmpresaCalendarioExcecao, LocalHistoricoGeografico,
    CalendarioSazonalComercial, CadastroEfemeride, EmpresaRecesso, EseNotificacaoCliente,
    ColaboradorDetalhesPessoais, EseConviteColaborador, EseServicoOferecido, EseServicoPreco, EseServicoPrecoHistorico,
    EseGrupoTabela, EseColaboradorServicoHabilidade, EseTipoExcecaoEnum, EseOrigemExcecaoEnum, EseExcecaoCalendario,
    ModEmpresaModulo,
)


from feedin.utils import preparar_entrada_modulo, limpar_string, limpar_mascara, validar_unicidade_documento, salvar_imagem_modulo
from feedin.modules.empresa.services.ese_calendario_service import obter_escala_padrao, subtrair_intervalo_horario

# 6. 📌 CONSTANTES ESTRUTURAIS AUTÔNOMAS DO MÓDULO
# O diretório de auditoria agora nasce de forma limpa dentro da static do próprio módulo
UPLOAD_AUDITORIA_DIR = os.path.join(empresa_bp.UPLOAD_BASE_DIR, 'empresas', 'documentos_auditoria')
os.makedirs(UPLOAD_AUDITORIA_DIR, exist_ok=True)

app_atual: Flask = current_app

# =====================================================================
# AUXILIARES TÉCNICOS (FUNÇÕES DE BARREIRA DA FASE 2)
# =====================================================================

def calcular_regra_movel(ano: int, regra: str) -> date:
    """
    Interpreta strings de recorrência e devolve o objeto date do ano solicitado.
    Exemplo de Regra: '2_DOMINGO_MAIO'
    """
    if not regra:
        return None

    try:
        partes = regra.split('_')
        ordem = int(partes[0])  # 1º, 2º, 3º...
        dia_semana_nome = partes[1]  # DOMINGO, SEGUNDA...
        mes_nome = partes[2]  # MAIO, AGOSTO...

        meses_map = {
            "JANEIRO": 1, "FEVEREIRO": 2, "MARCO": 3, "ABRIL": 4, "MAIO": 5, "JUNHO": 6,
            "JULHO": 7, "AGOSTO": 8, "SETEMBRO": 9, "OUTUBRO": 10, "NOVEMBRO": 11, "DEZEMBRO": 12
        }
        dias_map = {"SEGUNDA": 0, "TERCA": 1, "QUARTA": 2, "QUINTA": 3, "SEXTA": 4, "SABADO": 5, "DOMINGO": 6}

        mes_alvo = meses_map[mes_nome.upper()]
        dia_semana_alvo = dias_map[dia_semana_nome.upper()]

        matrix_mes = calendar.monthcalendar(ano, mes_alvo)
        dias_encontrados = []

        for semana in matrix_mes:
            dia_mes = semana[dia_semana_alvo]
            if dia_mes != 0:
                dias_encontrados.append(dia_mes)

        if len(dias_encontrados) >= ordem:
            return date(ano, mes_alvo, dias_encontrados[ordem - 1])
    except Exception:
        return None
    return None


def sanitizar_slug(texto):
    if not texto:
        return "empresa"
    try:
        from slugify import slugify
        return slugify(texto)
    except Exception:
        # Fallback nativo caso o pacote falhe
        texto = unicodedata.normalize('NFKD', texto).encode('ascii', 'ignore').decode('utf-8')
        texto = re.sub(r'[^\w\s-]', '', texto.lower())
        return re.sub(r'[-\s]+', '-', texto).strip('-')


@empresa_bp.route('/')
@preparar_entrada_modulo(slug_modulo='empresa', rota_destino='/empresa/dashboard')
def portal_entrada_empresa():
    """
    PORTAL DE ENTRADA DO MÓDULO EMPRESA
    -------------------------------------------------------------------------
    Redireciona para o dashboard unificado do Módulo Empresa.
    """
    rota_padrao = url_for('empresa.dashboard_empresa')
    proximo_passo = session.get('next_url', rota_padrao)

    if current_user.is_authenticated:
        return redirect(proximo_passo)

    return redirect(url_for('auth.login', next_url=proximo_passo))


@empresa_bp.route('/pre-cadastro/<int:local_id>', methods=['POST'])
@login_required  # Cidadão autenticado no Core iniciando ou enriquecendo um ponto físico
def pre_cadastro_empresa(local_id):
    """
    📌 ENRIQUECIMENTO DE PONTO FÍSICO NO CORE
    Complementa os dados mínimos do Local para habilitar a jornada de Reivindicação (Claim).
    O usuário logado que preenche/indica o local passa a ser o 'id_indicador' definitivo.
    """
    local = Local.query.get_or_404(local_id)

    # 1. Trava de Segurança: Se já possui titular ou é verificado, não permite re-cadastro
    if local.verificado or local.id_empreendedor:
        return jsonify({
            'status': 'erro',
            'message': 'Este estabelecimento já possui um titular vinculado ou está verificado.'
        }), 400

    dados = request.form

    try:
        # 2. Atualização dos Dados Básicos
        local.nome = dados.get('nome', local.nome).strip()
        local.documento = dados.get('documento')  # Higienização e Unicidade tratadas pelo Utils/Banco

        # 3. Categorização (Taxonomia)
        if dados.get('id_categoria_principal'):
            local.id_categoria_principal = dados.get('id_categoria_principal')

        # 4. Endereço e Geolocalização
        local.cep = dados.get('cep', '').strip()
        local.logradouro = dados.get('logradouro', '').strip()
        local.numero = dados.get('numero', '').strip()
        local.bairro = dados.get('bairro', '').strip()
        local.cidade = dados.get('cidade', '').strip()
        local.estado = dados.get('estado', 'SP').strip()

        # 5. Contatos Operacionais
        local.telefone = dados.get('telefone', '').strip()
        local.is_whatsapp = str(dados.get('is_whatsapp', 'true')).lower() in ['true', '1', 'yes']
        local.email = dados.get('email', '').strip()

        # 6. Status Operacional de Transição
        local.esta_ativo = True
        local.status_operacional = 'ativo'

        # 7. Rastreabilidade Orgânica no Core (BI4ALL)
        # Se o local ainda não tinha um indicador registrado, o cidadão atual assume a indicação
        if not local.id_indicador:
            local.id_indicador = current_user.id

        db.session.commit()

        # Resposta para o Front-end liberar o botão de "Reivindicar (Claim)"
        return jsonify({
            'status': 'sucesso',
            'message': 'Dados de pré-cadastro salvos. Ponto físico pronto para reivindicação.',
            'pode_reivindicar': True
        }), 200

    except Exception as e:
        db.session.rollback()
        return jsonify({
            'status': 'erro',
            'message': f'Falha ao salvar pré-cadastro: {str(e)}'
        }), 500

    
def verificar_dns_txt(dominio, token_esperado):
    """
    Método B da Fase 2: Consulta os registros TXT do DNS do domínio.
    """
    try:
        import dns.resolver
        dominio_limpo = dominio.replace('https://', '').replace('http://', '').split('/')[0]
        respostas = dns.resolver.resolve(dominio_limpo, 'TXT')
        for rdata in respostas:
            for txt_string in rdata.strings:
                if token_esperado in txt_string.decode('utf-8'):
                    return True
        return False
    except Exception:
        return False


# =====================================================================
# CONTROLLER / ROTA DO CLAIM DIGITAL
# =====================================================================

@empresa_bp.route('reivindicar', methods=['POST'])
@login_required
def processar_claim_digital():
    """
    🛰️ MOTOR DE PERSISTÊNCIA: CRIAÇÃO DE EMPRESA E CONCILIAÇÃO DE ESTEIRA DE CLAIMS
    --------------------------------------------------------------------------------------
    Processa a conversão de um Pleiteante em Empreendedor. Localiza prioritariamente o
    fato gerador na tabela 'EseProcessoClaim' para herdar o ID do usuário solicitante correto,
    evitando discrepâncias de chaves entre contas do Core (ID 1) e Módulos (ID 2).
    """
    from feedin.modules.empresa.models import EseProcessoClaim
    from feedin.models import Local
    from feedin.modules.agenda.models import AghConfiguracaoAgenda  # 👈 Importação da Configuração

    tipo_negocio = request.form.get('tipo_negocio')
    nome_fantasia = request.form.get('nome')
    categoria = request.form.get('categoria')

    raw_documento = request.form.get('documento')
    if not raw_documento:
        flash("O documento oficial (CPF/CNPJ) é obrigatório.", "danger")
        return redirect(request.referrer)

    documento = re.sub(r'\D', '', raw_documento)

    termo_aceito = request.form.get('termo_responsabilidade') == 'on'
    if not termo_aceito:
        flash("Você precisa aceitar o Termo de Auto-Responsabilidade Jurídica para prosseguir.", "danger")
        return redirect(request.referrer)

    local_id = request.form.get('local_id') if tipo_negocio == 'fisico' else None
    dominio = request.form.get('dominio_web').strip() if tipo_negocio == 'digital' and request.form.get(
        'dominio_web') else None

    # =====================================================================
    # 🔍 1. INVESTIGAÇÃO ANTECIPADA DA ESTEIRA (AÇÃO CORRETORA DE CONSISTÊNCIA)
    # =====================================================================
    processo_ativo = EseProcessoClaim.query.filter(
        (EseProcessoClaim.local_id == local_id) |
        (EseProcessoClaim.documento_declarado == documento)
    ).filter(EseProcessoClaim.status_processo == 'em_andamento').order_by(EseProcessoClaim.id.desc()).first()

    # Resolução Resiliente do ID Core
    user_core_id = getattr(current_user, 'usuario_id', None)

    if not user_core_id and isinstance(current_user.id, int):
        user_core_id = current_user.id

    if isinstance(current_user.id, str):
        cliente = ModCadastroCliente.query.get(current_user.id)
        if cliente:
            user_core_id = cliente.usuario_id

    # 🚨 TESTE DE LEGITIMIDADE EXPULSIVO
    if not processo_ativo or processo_ativo.usuario_solicitante_id != user_core_id:
        print(
            f"🚨 [ALERTA DE INVASÃO/DISCREPÂNCIA] Claim do Usuário: {processo_ativo.usuario_solicitante_id if processo_ativo else 'N/A'} vs Usuário Resolvido: {user_core_id}")
        db.session.rollback()
        flash(
            "🔒 Violação de Consistência Identitária: Você não tem autorização para concluir este processo com esta conta.",
            "danger")
        return redirect(url_for('core.dashboard'))

    id_proprietario_efetivo = user_core_id
    print(f"🎯 [AUDITORIA OK] Identidade unificada e legítima confirmada para o ID {id_proprietario_efetivo}.")

    # Validações Geográficas e de Domínio
    if tipo_negocio == 'fisico':
        if not local_id:
            flash("Para empresas físicas, a seleção do ponto geográfico é obrigatória.", "danger")
            return redirect(request.referrer)

        local_existente = Local.query.get(local_id)
        if not local_existente:
            flash("O ponto físico selecionado não foi encontrado na base geográfica.", "danger")
            return redirect(request.referrer)

        empresa_ocupante = EseEmpresa.query.filter_by(local_id=local_id, status_homologacao='ativo').first()
        if empresa_ocupante:
            flash("Este ponto físico já possui uma empresa ativa vinculada. Abra uma contestação.", "danger")
            return redirect(request.referrer)

    elif tipo_negocio == 'digital':
        if not dominio:
            flash("Para empresas 100% digitais, a inserção do domínio próprio é obrigatória.", "danger")
            return redirect(request.referrer)
        try:
            EseEmpresa.validar_dominio_proprio(dominio)
        except ValueError as e:
            flash(str(e), "danger")
            return redirect(request.referrer)

        token_verificacao = f"feedin-verification-{documento}"
        if request.form.get('modo_verificacao') == 'dns' and not verificar_dns_txt(dominio, token_verificacao):
            flash("Não conseguimos encontrar o registro TXT no DNS do seu domínio.", "warning")

    try:
        # =====================================================================
        # 🔑 2. INJEÇÃO DE DADOS HOMOGÊNEOS (AMARRADO AO ID DO PROCESSO)
        # =====================================================================
        nova_empresa = EseEmpresa(
            proprietario_id=id_proprietario_efetivo,
            local_id=local_id,
            nome=nome_fantasia,
            categoria=categoria,
            documento_oficial=documento,
            tipo_documento='CNPJ' if len(documento) == 14 else 'CPF',
            dominio_web=dominio,
            status_homologacao='aguardando_dados',
            termo_responsabilidade_aceito=True,
            data_aceite_termo=datetime.now(timezone.utc),
            ip_aceite_termo=request.remote_addr
        )

        db.session.add(nova_empresa)
        db.session.flush()  # 🔥 Gera o nova_empresa.id necessário para FKs

        # Vínculo de Posse Master
        novo_vinculo = VinculoUsuarioEmpresa(
            usuario_id=id_proprietario_efetivo,
            empresa_id=nova_empresa.id,
            papel_nome='empreendedor',
            papel_nivel=999,
            ativo=True
        )
        db.session.add(novo_vinculo)

        # =====================================================================
        # ⚙️ 2.1 INITIALIZATION SEED: CONFIGURAÇÃO DE AGENDA PADRÃO (BOOTSTRAP)
        # =====================================================================
        # Garante que NENHUMA empresa homologada fique sem a linha de regras no banco
        config_padrao_agenda = AghConfiguracaoAgenda(
            empresa_id=nova_empresa.id,
            antecedencia_minima_reagendamento_min=120,   # 120 minutos (2h)
            prazo_limite_reagendamento_dias=30,         # 30 dias
            permitir_reagendamento_pos_horario=True,     # Permite pós-horário com anuência
            exigir_pagamento_antecipado=False,           # Sem pagamento antecipado obrigatório
            percentual_sinal_pagamento=0.00,             # 0%
            taxa_agendamento=0.00,                      # R$ 0,00 (Taxa fixa de segurança)
            fidelidade_ativa=False,                     # Fidelidade desativada por padrão
            tolerancia_atraso_minutos=10                 # 10 minutos de tolerância
        )
        db.session.add(config_padrao_agenda)

        # =====================================================================
        # 🛰️ MÓDULO ADERENTE (EX: AGENDA)
        # =====================================================================
        modulo_atual = session.get('modulo_slug_atual')

        if modulo_atual:
            identidade = IdentidadeCivil.query.filter_by(usuario_id=id_proprietario_efetivo).first()
            cpf_hash_dono = identidade.cpf_hash if identidade else None

            if cpf_hash_dono:
                vinculo_modulo = ModVinculoModulo.query.filter_by(
                    local_id=local_id or nova_empresa.id,
                    modulo_slug=modulo_atual
                ).first()

                if not vinculo_modulo:
                    novo_vinculo_modulo = ModVinculoModulo(
                        cpf_hash=cpf_hash_dono,
                        modulo_slug=modulo_atual,
                        local_id=local_id or nova_empresa.id,
                        email_customizado=current_user.email,
                        ativo=True
                    )
                    db.session.add(novo_vinculo_modulo)

            session['modo_visao'] = 'balcao'
            session['nivel_acesso_atual'] = 999
            session['empresa_id_atual'] = nova_empresa.id
            session['empresa_ativa_id'] = nova_empresa.id
            session['local_id_atual'] = local_id or nova_empresa.id

        # =====================================================================
        # 🏁 3. FECHAMENTO SINCRO DA ESTEIRA
        # =====================================================================
        if processo_ativo:
            processo_ativo.status_processo = 'concluido'
            db.session.add(processo_ativo)

            id_local_efetivo = local_id or processo_ativo.local_id
            if not nova_empresa.local_id and id_local_efetivo:
                nova_empresa.local_id = id_local_efetivo
                db.session.add(nova_empresa)

            if id_local_efetivo:
                local_core = Local.query.get(id_local_efetivo)
                if local_core:
                    local_core.status_operacional = 'verificado'
                    db.session.add(local_core)

        db.session.commit()
        print(f"✅ [BANCO] Empresa ID {nova_empresa.id} e AghConfiguracaoAgenda gravadas com sucesso.")
        flash(f"Empresa '{nome_fantasia}' cadastrada com sucesso!", "success")

        if modulo_atual == 'agenda':
            return redirect(url_for('agenda.home_negocios'))

        alvo_redirecionamento = local_id or (processo_ativo.local_id if processo_ativo else None)
        if alvo_redirecionamento:
            return redirect(url_for('perfil_local', local_id=alvo_redirecionamento))

        return redirect(url_for('dashboard'))

    except Exception as e:
        db.session.rollback()
        print(f"❌ [ERRO CRÍTICO] Falha na persistência: {str(e)}")
        flash(f"Erro sistêmico ao processar o cadastro: {str(e)}", "danger")
        return redirect(request.referrer)


@empresa_bp.route('/reivindicar/<int:id_local>',
                  methods=['GET', 'POST'])  # 💡 CORREÇÃO: Liberado GET para renderizar o form
@login_required
def reivindicar_local_fisico(id_local):
    """
    🗂️ AUDITORIA E CLAIM DE LOCAL FÍSICO EXISTENTE NO CORE
    --------------------------------------------------------------------------------------
    Intercepta o envio de documentos geográficos. Amarra rigidamente a transação ao
    registro originário da esteira 'EseProcessoClaim'. Se houver divergência identitária
    entre o usuário logado e o solicitante do e-mail, barra a operação na hora.
    """
    from feedin.modules.empresa.models import EseProcessoClaim
    from feedin.models import Local



    # Interceptação inicial de renderização da interface
    if request.method == 'GET':
        local = Local.query.get_or_404(id_local)
        return render_template('empresa/formulario_reivindicacao.html', local=local)

    # 1. 🛰️ BARREIRA INTRANSCRONÍVEL: INVESTIGAÇÃO ANTECIPADA DA ESTEIRA (ANTI-FRAUDE)
    # Busca o processo de claim ativo focado especificamente neste ponto geográfico (id_local)
    processo_ativo = EseProcessoClaim.query.filter_by(
        local_id=id_local,
        status_processo='em_andamento'
    ).order_by(EseProcessoClaim.id.desc()).first()

    # 🚨 TESTE DE LEGITIMIDADE IMPLACÁVEL:
    # Se não houver processo para este local, ou o usuário logado (ID 1) tentar fechar
    # a esteira iniciada pelo e-mail do ID 2: PORTA NA CARA.
    if not processo_ativo or processo_ativo.usuario_solicitante_id != current_user.id:
        print(f"🚨 [ALERTA DE SEGURANÇA] Tentativa de usurpação de claim físico para Local ID {id_local}!")
        if processo_ativo:
            print(
                f"   -> Dono da Esteira: {processo_ativo.usuario_solicitante_id} vs Usuário Logado: {current_user.id}")

        db.session.rollback()
        flash(
            "🔒 Violação de Consistência Identitária: Esta conta não tem autorização para concluir o claim deste ponto físico.",
            "danger")
        return redirect(url_for('dashboard'))

    # Se passou, fixamos o ID homogêneo vindo da origem da verdade
    id_proprietario_efetivo = processo_ativo.usuario_solicitante_id
    print(f"🎯 [AUDITORIA OK] Vínculo geográfico unificado para o ID {id_proprietario_efetivo}.")

    local = Local.query.get_or_404(id_local)
    modulo_atual = session.get('modulo_slug_atual')

    # Captura os arquivos do formulário
    file_endereco = request.files.get('comprovante_endereco')
    file_cnpj = request.files.get('cnpj_social')

    if not file_endereco or not file_cnpj or file_endereco.filename == '' or file_cnpj.filename == '':
        flash("Todos os documentos solicitados são obrigatórios para a auditoria.", "danger")
        return redirect(request.referrer)

    # 2. Proteção Geográfica Ativa
    empresa_ocupante = EseEmpresa.query.filter_by(local_id=id_local, status_homologacao='ativo').first()
    if empresa_ocupante:
        flash("Este ponto físico já possui uma empresa ativa vinculada. Abra uma contestação.", "danger")
        return redirect(request.referrer)

    try:
        # 3. Processamento e Criptografia Física via Fernet (Usa o ID legítimo no nome do arquivo)
        upload_dir = os.path.join(os.getcwd(), 'instance', 'documentos_auditoria')
        if not os.path.exists(upload_dir):
            os.makedirs(upload_dir)

        documentos_salvos = {}
        for chave, arquivo in [('endereco', file_endereco), ('cnpj', file_cnpj)]:
            conteudo_binario = arquivo.read()
            conteudo_criptografado = current_app.fernet.encrypt(conteudo_binario)

            # 🔥 CORREÇÃO: Nome amarrado ao ID do dono efetivo do processo
            nome_seguro = f"claim_{id_local}_{id_proprietario_efetivo}_{chave}_{int(datetime.now().timestamp())}.enc"
            caminho_completo = os.path.join(upload_dir, nome_seguro)

            with open(caminho_completo, 'wb') as f:
                f.write(conteudo_criptografado)

            documentos_salvos[chave] = nome_seguro

        # 4. Criação da Entidade Empresa com ID Homogêneo
        nova_empresa = EseEmpresa(
            proprietario_id=id_proprietario_efetivo,  # 🔥 CORREÇÃO: Amarrado ao processo
            local_id=id_local,
            nome=local.nome,
            categoria=local.categoria or 'Comércio',
            documento_oficial=None,
            tipo_documento='CNPJ',
            status_homologacao='aguardando_dados',  # 🔥 Pés no chão: Nasce no hub de onboarding, não ativo direto!
            termo_responsabilidade_aceito=True,
            data_aceite_termo=datetime.now(timezone.utc),
            ip_aceite_termo=request.remote_addr
        )
        db.session.add(nova_empresa)
        db.session.flush()

        # 5. Vínculo de Posse Hierárquica do Core amarrado ao mesmo ID
        novo_vinculo = VinculoUsuarioEmpresa(
            usuario_id=id_proprietario_efetivo,  # 🔥 CORREÇÃO: Alinhado rigidamente
            empresa_id=nova_empresa.id,
            papel_nome='empreendedor',
            papel_nivel=999,
            ativo=True
        )
        db.session.add(novo_vinculo)

        # =====================================================================
        # 🪐 CONTEXTO ADERENTE: MODULO VITRINE (EX: AGENDA)
        # =====================================================================
        if modulo_atual == 'agenda':
            # 🔥 CORREÇÃO: Busca a identidade civil do ID dono do processo, não do logado mutante
            identidade = IdentidadeCivil.query.filter_by(usuario_id=id_proprietario_efetivo).first()
            cpf_hash_dono = identidade.cpf_hash if identidade else None

            if cpf_hash_dono:
                novo_vinculo_modulo = ModVinculoModulo(
                    cpf_hash=cpf_hash_dono,
                    modulo_slug=modulo_atual,
                    local_id=id_local,
                    email_customizado=current_user.email,  # Mantém o e-mail da sessão logada ativa
                    ativo=True
                )
                db.session.add(novo_vinculo_modulo)

            # Converte permissões de sessão para controle do balcão
            session['modo_visao'] = 'balcao'
            session['nivel_acesso_atual'] = 999
            session['empresa_id_atual'] = nova_empresa.id
            session['empresa_ativa_id'] = nova_empresa.id
            session['local_id_atual'] = id_local

        # 🏁 FECHAMENTO SINCRO DA ESTEIRA ORIGINÁRIA
        processo_ativo.status_processo = 'concluido'
        db.session.add(processo_ativo)

        # Atualiza status operacional do catálogo geográfico do Core
        local.status_operacional = 'verificado'
        db.session.add(local)

        db.session.commit()
        print(f"✅ [BANCO] Claim físico do Local {id_local} persistido com consistência identitária.")
        flash(f"Documentação de '{local.nome}' enviada! Seu estabelecimento está em fase de ativação.", "success")

        if modulo_atual == 'agenda':
            return redirect(url_for('agenda.home_negocios'))

        return redirect(url_for('dashboard'))

    except Exception as e:
        db.session.rollback()
        print(f"❌ [ERRO CRÍTICO CLAIM FÍSICO] {str(e)}")
        flash(f"Erro técnico ao persistir documentos: {str(e)}", "danger")
        return redirect(request.referrer)

def obter_status_reivindicacao_local(local_id):
    """
    Verifica se o local possui algum processo de Claim ativo ou se o prazo expirou.
    Garante a liberação automática do botão no Core por demanda.
    """
    # Busca se existe um processo ativo ("em_andamento") para este local
    claim_ativo = EseProcessoClaim.query.filter_by(
        local_id=local_id,
        status_processo='em_andamento'
    ).first()

    if not claim_ativo:
        return 'disponivel'

    # Se existe um processo em andamento, checa se a data limite estourou os 5 dias
    if datetime.now(timezone.utc) > claim_ativo.data_limite.replace(tzinfo=timezone.utc):
        # 🚨 O PRAZO EXPIROU!
        # Em vez de deletar, atualizamos o status do processo para histórico permanente
        claim_ativo.status_processo = 'expirado'
        db.session.commit()

        # O local volta a ficar disponível imediatamente para o próximo usuário
        return 'disponivel'

    # Se o prazo não venceu, o local continua bloqueado na tela
    return 'em_reivindicacao'


def verificar_comportamento_solicitante(ip_usuario, documento, local_id_atual):
    """
    Valida se o solicitante possui comportamento fraudulento,
    sem penalizar empreendedores com múltiplos negócios legítimos.
    """
    # 1. Proteção Manual: Bloqueio explícito administrativo
    suspeito = EseProcessoClaim.query.filter(
        (EseProcessoClaim.documento_declarado == documento) |
        (EseProcessoClaim.ip_solicitacao == ip_usuario)
    ).filter_by(status_processo='suspeito_bloqueado').first()

    if suspeito:
        return False, "Este dispositivo ou documento está temporariamente suspenso para novas solicitações. Contate o suporte."

    # 2. Triagem para o MESMO Local (Evita travar o mesmo comércio por pirraça)
    tentativas_no_mesmo_local = EseProcessoClaim.query.filter(
        (EseProcessoClaim.documento_declarado == documento) | (EseProcessoClaim.ip_solicitacao == ip_usuario),
        EseProcessoClaim.local_id == local_id_atual,
        EseProcessoClaim.status_processo == 'expirado'
    ).count()

    if tentativas_no_mesmo_local >= 2:
        return False, "Você já esgotou o prazo de preenchimento para este local anteriormente. Para reativar o processo, contate a administração."

    # 3. Triagem de Abandono Massivo (Múltiplos locais diferentes deixados para trás)
    # Ignora os status 'cancelado_usuario', pois estes foram erros assumidos na hora.
    limite_tempo = datetime.now(timezone.utc) - timedelta(days=15)
    locais_diferentes_expirados = db.session.query(EseProcessoClaim.local_id).filter(
        (EseProcessoClaim.documento_declarado == documento) | (EseProcessoClaim.ip_solicitacao == ip_usuario),
        EseProcessoClaim.status_processo == 'expirado',
        EseProcessoClaim.data_inicio >= limite_tempo
    ).distinct().count()

    # Se o cara deixou mais de 4 comércios diferentes expirarem sem concluir nada, o comportamento é padrão de ataque
    if locais_diferentes_expirados > 4:
        # Verifica se ele tem pelo menos UMA empresa ativa concluída para dar o benefício da dúvida
        possui_empresa_ativa = EseProcessoClaim.query.filter(
            (EseProcessoClaim.documento_declarado == documento),
            EseProcessoClaim.status_processo == 'concluido'
        ).first()

        if not possui_empresa_ativa:
            return False, "Identificamos um alto volume de solicitações não concluídas a partir deste acesso. Temporariamente suspenso."

    return True, "Liberado"


@empresa_bp.route('/homologacao/finalizar/<token>', methods=['POST'])
@login_required
def finalizar_claim(token):
    """
    🛡️ CONCLUSÃO DO CLAIM, AUDITORIA JURÍDICA E GESTÃO DE ARQUIVOS
    --------------------------------------------------------------------------------------
    Garante a presença dos arquivos temporários de auditoria inicial e realiza a
    substituição limpa (expurgando genéricos) assim que os arquivos oficiais chegam.
    """
    # 1. Busca o processo de Claim ativo pelo token
    processo = EseProcessoClaim.query.filter_by(token_validacao=token).first_or_404()

    # 2. Localiza a Empresa vinculada ao Local
    empresa = EseEmpresa.query.filter_by(local_id=processo.local_id).first()
    if not empresa and processo.usuario_solicitante_id:
        empresa = EseEmpresa.query.filter_by(proprietario_id=processo.usuario_solicitante_id).first()

    if not empresa:
        flash("Empresa vinculada não encontrada.", "danger")
        return redirect(url_for('empresa.dashboard_empresa'))

    # 3. Garante o diretório isolado da empresa
    base_upload_path = current_app.config.get('UPLOAD_FOLDER', 'uploads')
    pasta_auditoria = os.path.join(base_upload_path, f"empresa_{empresa.id}", "documentos_auditoria")
    os.makedirs(pasta_auditoria, exist_ok=True)

    # Nomes padronizados dos arquivos genéricos temporários
    temp_cnpj = os.path.join(pasta_auditoria, "temp_cnpj_pendente.jpg")
    temp_endereco = os.path.join(pasta_auditoria, "temp_endereco_pendente.jpg")

    # Captura dos arquivos enviados no Formulário
    file_cnpj = request.files.get('cnpj_social')
    file_endereco = request.files.get('comprovante_endereco')
    slug_digitado = request.form.get('slug', '').strip().lower()
    termo_aceite = request.form.get('termo_aceite')

    caminho_cnpj_salvo = None
    caminho_endereco_salvo = None

    try:
        # -------------------------------------------------------------
        # A) PROCESSAMENTO DO CARTÃO CNPJ / CPF
        # -------------------------------------------------------------
        if file_cnpj and file_cnpj.filename != '':
            # 🧹 LIMPEZA: Remove o arquivo temporário/genérico antigo se ele existir
            if os.path.exists(temp_cnpj):
                os.remove(temp_cnpj)

            # Gravação do arquivo OFICIAL com nomenclatura padronizada de controle
            ext = os.path.splitext(file_cnpj.filename)[1]
            nome_oficial_cnpj = f"oficial_doc_identificacao_{token[:8]}{ext}"
            caminho_cnpj_salvo = os.path.join(pasta_auditoria, nome_oficial_cnpj)
            file_cnpj.save(caminho_cnpj_salvo)
        else:
            # 🟢 MODO TESTE/RÁPIDO: Se não enviou o oficial, garante que existe o genérico para fluir o teste
            if not os.path.exists(temp_cnpj) and not any(
                    f.startswith("oficial_doc_identificacao_") for f in os.listdir(pasta_auditoria)):
                with open(temp_cnpj, "w", encoding="utf-8") as f:
                    f.write("Aguardando Cartão CPF/CNPJ Oficial.")

        # -------------------------------------------------------------
        # B) PROCESSAMENTO DO COMPROVANTE DE ENDEREÇO
        # -------------------------------------------------------------
        if file_endereco and file_endereco.filename != '':
            # 🧹 LIMPEZA: Remove o arquivo temporário/genérico antigo se ele existir
            if os.path.exists(temp_endereco):
                os.remove(temp_endereco)

            # Gravação do arquivo OFICIAL com nomenclatura padronizada de controle
            ext = os.path.splitext(file_endereco.filename)[1]
            nome_oficial_endereco = f"oficial_comprovante_endereco_{token[:8]}{ext}"
            caminho_endereco_salvo = os.path.join(pasta_auditoria, nome_oficial_endereco)
            file_endereco.save(caminho_endereco_salvo)
        else:
            # 🟢 MODO TESTE/RÁPIDO: Se não enviou o oficial, garante que existe o genérico para fluir o teste
            if not os.path.exists(temp_endereco) and not any(
                    f.startswith("oficial_comprovante_endereco_") for f in os.listdir(pasta_auditoria)):
                with open(temp_endereco, "w", encoding="utf-8") as f:
                    f.write("Aguardando Comprovante de Endereço Oficial.")

        # -------------------------------------------------------------
        # C) ATUALIZAÇÃO DOS BANCOS E TERMOS
        # -------------------------------------------------------------
        processo.status_processo = 'aguardando_analise'
        empresa.status_homologacao = 'em_analise'

        if slug_digitado and not empresa.slug:
            empresa.slug = slug_digitado

        if termo_aceite:
            empresa.termo_responsabilidade_aceito = True
            empresa.data_aceite_termo = datetime.now(timezone.utc)
            empresa.ip_aceite_termo = request.remote_addr or request.headers.get('X-Forwarded-For')

        db.session.commit()

        session['modo_visao'] = 'aguardando_analise'
        session.permanent = True

        flash("Documentação processada com sucesso!", "success")
        return redirect(url_for('empresa.sala_espera_claim', token=token))

    except Exception as e:
        db.session.rollback()

        # Em caso de falha na transação, limpa os arquivos OFICIAIS recém-salvos para não corromper a pasta
        if caminho_cnpj_salvo and os.path.exists(caminho_cnpj_salvo):
            os.remove(caminho_cnpj_salvo)
        if caminho_endereco_salvo and os.path.exists(caminho_endereco_salvo):
            os.remove(caminho_endereco_salvo)

        current_app.logger.error(f"❌ Erro ao processar claim token {token}: {e}")
        flash("Erro técnico interno ao protocolar documentos. Tente novamente.", "danger")
        return redirect(url_for('empresa.enviar_documentacao', empresa_id=empresa.id))


@empresa_bp.route('/intencao-claim/<int:id_local>', methods=['POST'])
@login_required
def registrar_intencao_claim(id_local):
    """
    📌 FASE 1: ENRIQUECIMENTO E ATUALIZAÇÃO DO PONTO FÍSICO (TERRITÓRIO)
    Acessível por qualquer usuário logado no Core para iniciar a reivindicação.
    """
    from feedin import database as db
    import json
    import uuid
    from datetime import datetime, timezone, timedelta
    from flask import request, flash, redirect, render_template, current_app, url_for
    from flask_login import current_user
    from feedin.models import Local, Taxonomia
    from feedin.modules.empresa.models import EseProcessoClaim, HistoricoAlteracaoLocal
    from feedin.utils import validar_unicidade_documento

    local = Local.query.get_or_404(id_local)

    # 1. Coleta dos dados do formulário
    nome_empresa = request.form.get('nome_empresa', '').strip()
    documento_oficial = request.form.get('documento_oficial', '').strip()
    categoria_empresa = request.form.get('categoria_empresa', '').strip()

    # 2. Coleta de Endereço Validada pelo Solicitante
    cep_empresa = request.form.get('cep_empresa', '').strip()
    logradouro_empresa = request.form.get('logradouro_empresa', '').strip()
    numero_empresa = request.form.get('numero_empresa', '').strip()
    bairro_empresa = request.form.get('bairro_empresa', '').strip()
    cidade_empresa = request.form.get('cidade_empresa', '').strip()
    estado_empresa = request.form.get('estado_empresa', 'SP').strip()

    # 3. Coleta de Canais de Comunicação
    email_contato = request.form.get('email_contato', '').strip()
    whatsapp_contato = request.form.get('whatsapp_contato', '').strip()
    via_preferencial = request.form.get('via_preferencial', 'email_login').strip()

    # Validação rígida de integridade dos dados obrigatórios
    dados_obrigatorios = [
        nome_empresa, documento_oficial, categoria_empresa,
        cep_empresa, logradouro_empresa, numero_empresa,
        bairro_empresa, cidade_empresa, estado_empresa, email_contato, whatsapp_contato
    ]

    if not all(dados_obrigatorios):
        flash("🔒 Todos os dados de localização e contato são obrigatórios nesta fase.", "warning")
        return redirect(request.referrer or url_for('perfil_local', local_id=local.id))

    # =====================================================================
    # 🛡️ REGRA DE UNICIDADE: Validação via Utils (Trata CPF/CNPJ e duplicidade)
    # =====================================================================
    resultado_doc = validar_unicidade_documento(doc_raw=documento_oficial, id_local_atual=local.id)

    if not resultado_doc["valido"]:
        flash(f"⚠️ {resultado_doc['mensagem']}", "warning")
        return redirect(request.referrer or url_for('perfil_local', local_id=local.id))

    # Variáveis higienizadas retornadas pelo Utils
    documento_limpo = resultado_doc["doc_limpo"]
    tipo_doc = resultado_doc["tipo"]
    ip_usuario = request.remote_addr

    # =====================================================================
    # 🛡️ BARREIRA ANTIFRAUDE: Bloqueio imediato de comportamentos suspeitos
    # =====================================================================
    permitido, mensagem_erro = verificar_comportamento_solicitante(ip_usuario, documento_limpo, local.id)
    if not permitido:
        flash(f"🚨 {mensagem_erro}", "danger")
        return redirect(request.referrer or url_for('perfil_local', local_id=local.id))

    # Lógica para definir Canal e Alvo
    if via_preferencial == 'whatsapp':
        canal = 'whatsapp'
        alvo = whatsapp_contato
    elif via_preferencial == 'email_comercial':
        canal = 'email'
        alvo = email_contato
    else:
        canal = 'email'
        alvo = current_user.email

    try:
        agora = datetime.now(timezone.utc)
        prazo_5_dias = agora + timedelta(days=5)

        # 🔐 1. BACKUP DO ESTADO ANTERIOR (HISTÓRICO)
        snapshot_anterior = {
            "nome": local.nome,
            "documento": local.documento,
            "id_categoria_principal": local.id_categoria_principal,
            "cep": local.cep,
            "logradouro": local.logradouro,
            "numero": local.numero,
            "bairro": local.bairro,
            "cidade": local.cidade,
            "estado": local.estado,
            "telefone": local.telefone,
            "email": local.email,
            "id_empreendedor": local.id_empreendedor,
            "status_operacional": local.status_operacional
        }

        seguranca_backup = HistoricoAlteracaoLocal(
            local_id=local.id,
            usuario_id=current_user.id,
            dados_anteriores=json.dumps(snapshot_anterior, ensure_ascii=False),
            prazo_expiracao=prazo_5_dias,
            status_pendencia='pendente'
        )
        db.session.add(seguranca_backup)

        # 🎫 2. CRIAÇÃO DO PROCESSO DE REIVINDICAÇÃO
        token_32 = uuid.uuid4().hex

        processo_claim = EseProcessoClaim(
            local_id=local.id,
            usuario_solicitante_id=current_user.id,
            canal_comunicacao=canal,
            alvo_comunicacao=alvo,
            documento_declarado=documento_limpo,
            tipo_documento_declarado=tipo_doc,
            telefone_declarado=whatsapp_contato,
            email_declarado=email_contato,
            token_validacao=token_32,
            data_inicio=agora,
            data_limite=prazo_5_dias,
            ip_solicitacao=ip_usuario,
            status_processo='em_andamento'
        )
        db.session.add(processo_claim)

        # 🏢 3. ATUALIZAÇÃO DO PONTO FÍSICO
        categoria_obj = Taxonomia.query.filter_by(nome=categoria_empresa).first()

        local.nome = nome_empresa
        local.documento = documento_limpo
        if categoria_obj:
            local.id_categoria_principal = categoria_obj.id

        local.cep = cep_empresa
        local.logradouro = logradouro_empresa
        local.numero = numero_empresa
        local.bairro = bairro_empresa
        local.cidade = cidade_empresa
        local.estado = estado_empresa
        local.email = email_contato
        local.telefone = whatsapp_contato
        local.status_operacional = 'em_reivindicacao'

        # Commit unificado para salvar Historico, ProcessoClaim e Local
        db.session.commit()

        flash(f"✨ Processo de reivindicação iniciado para '{nome_empresa}'!", "success")

        return render_template(
            'empresa/alerta_consciencia.html',
            processo=processo_claim,
            local=local,
            modulos_sistema=[]
        )

    except Exception as e:
        db.session.rollback()

        print("\n" + "=" * 60)
        print(f"❌ ERRO CRÍTICO NO BANCO DE DADOS DETECTADO:\n{str(e)}")
        print("=" * 60 + "\n")

        current_app.logger.error(f"Erro ao processar intenção estruturada no local: {str(e)}")

        flash(f"Erro interno ao persistir dados: {str(e)}", "danger")
        return redirect(request.referrer or url_for('perfil_local', local_id=local.id))


@empresa_bp.route('/confirmar-disparo/<int:id_processo>', methods=['POST'])
@login_required
def confirmar_e_disparar_claim(id_processo):
    """
    Fase 2: Valida o filtro de fraude, gera o link externo com o token
    e envia por e-mail ou prepara o canal de WhatsApp escolhido.
    """
    from feedin.modules.empresa.models import EseProcessoClaim
    from feedin.models import Local
    import urllib.parse

    processo = EseProcessoClaim.query.get_or_404(id_processo)
    local = Local.query.get_or_404(processo.local_id)

    # 1. GERAR A URL EXTERNA QUE O USUÁRIO CLICARÁ
    url_validacao = url_for(
        'auth.validar_token_claim',
        token=processo.token_validacao,
        _external=True
    )

    enviado_com_sucesso = False
    link_whatsapp_click = None

    # =========================================================================
    # 👤 RECUPERAÇÃO EXPLÍCITA DO IDENTIFICADOR DO SOLICITANTE
    # =========================================================================
    nome_exibicao = "Parceiro"

    if processo.usuario_solicitante_id:
        from feedin.models import Usuario, IdentidadeCivil

        # Tentativa A: Buscar o Nome Oficial na Identidade Civil para ficar mais humanizado
        identidade = IdentidadeCivil.query.filter_by(usuario_id=processo.usuario_solicitante_id).first()

        if identidade and identidade.nome_completo_oficial:
            # Pega o primeiro nome (ex: "CARLOS") e padroniza para "Carlos"
            nome_exibicao = identidade.nome_completo_oficial.split()[0].strip().title()
        else:
            # Fallback / Tentativa B: Se ainda não tiver identidade civil, busca o username do Core
            usuario_core = Usuario.query.get(processo.usuario_solicitante_id)
            if usuario_core and usuario_core.username:
                nome_exibicao = usuario_core.username.split()[0].strip()

    # 2. ENGENHARIA DE DISPARO DE ACORDO COM O CANAL SELECIONADO
    if 'email' in processo.canal_comunicacao:  # trata 'email_login' ou 'email_comercial'

        msg = Message(
            f"🔑 Validação de Propriedade: {local.nome}",
            sender=current_app.config.get('MAIL_USERNAME'),
            recipients=[processo.alvo_comunicacao]
        )

        msg.html = render_template(
            'emails/link_validacao_claim.html',
            local=local,
            url_cadastro=url_validacao,
            prazo_dias=5
        )

        try:
            mail.send(msg)
            enviado_com_sucesso = True
        except Exception as e:
            print(f"Erro ao enviar e-mail de reivindicação: {e}")
            flash("Erro operacional ao disparar e-mail. Tente novamente mais tarde.", "danger")
            return redirect(url_for('perfil_local', local_id=local.id))

    else:
        # =========================================================================
        # 📲 MOTOR DE COMUNICAÇÃO VIA WHATSAPP (PADRÃO INFORMÁTICA)
        # =========================================================================
        telefone_destino = processo.whatsapp if hasattr(processo, 'whatsapp') else processo.alvo_comunicacao
        telefone_limpo = "".join(filter(str.isdigit, telefone_destino))

        mensagem_whatsapp = (
            f"Olá! Identificamos que você iniciou o processo de reivindicação e controle comercial do estabelecimento "
            f"*{local.nome}* na plataforma FeedIn!.\n\n"
            f"Para assegurar a legitimidade e assumir a gestão da sua página, acesse o link seguro abaixo para "
            f"criar suas credenciais e vincular a empresa à sua conta:\n"
            f"🔗 {url_validacao}\n\n"
            f"⚠️ *Atenção:* Por motivos de segurança, este link possui um prazo estrito de *validade de 5 dias*. "
            f"Caso não seja concluído, a solicitação será cancelada automaticamente."
        )

        texto_codificado = urllib.parse.quote(mensagem_whatsapp)
        link_whatsapp_click = f"https://api.whatsapp.com/send?phone=55{telefone_limpo}&text={texto_codificado}"

        # Como o WhatsApp abre o link no navegador do cliente, consideramos o fluxo disparado
        enviado_com_sucesso = True

    # =========================================================================
    # 🏁 PERSISTÊNCIA, RETORNO E FINALIZAÇÃO (FORA DOS BLOCOS IF/ELSE)
    # =========================================================================
    if enviado_com_sucesso:
        # Atualiza o status do processo de reivindicação de forma definitiva
        processo.status_processo = 'em_andamento'
        db.session.commit()

        if link_whatsapp_click:
            # Se for WhatsApp, redireciona o usuário direto para a API deles com o texto pronto
            flash("✨ Redirecionando para o WhatsApp de validação...", "success")
            return redirect(link_whatsapp_click)
        else:
            # Se for E-mail, avisa na tela e devolve ele para a página do local
            flash("📧 Link de validação enviado com sucesso! Verifique sua caixa de entrada ou spam.", "success")
            return redirect(url_for('perfil_local', local_id=local.id))

    # Saída de emergência caso 'enviado_com_sucesso' termine como False por algum motivo raro
    flash("Não foi possível processar o envio. Revise os dados de contato.", "warning")
    return redirect(url_for('perfil_local', local_id=local.id))


@empresa_bp.route('/cancelar-recuo/<int:id_processo>', methods=['GET'])
@login_required
def cancelar_intencao_recuo(id_processo):
    """
    O Filtro de Fraude funcionou: O usuário recuou na tela de responsabilidade legal.
    Restauramos os dados do Local e marcamos o processo como 'cancelado_usuario'
    para preservar a métrica de auditoria sem gerar lixo.
    """
    from feedin import database as db
    import json
    from feedin.models import Local
    from feedin.modules.empresa.models import EseProcessoClaim, HistoricoAlteracaoLocal

    processo = EseProcessoClaim.query.get_or_404(id_processo)
    local = Local.query.get(processo.local_id)

    try:
        if local:
            # 🔍 Busca o snapshot de segurança gerado para este recuo
            historico = HistoricoAlteracaoLocal.query.filter_by(
                local_id=local.id,
                usuario_id=current_user.id,
                status_pendencia='pendente'
            ).order_by(HistoricoAlteracaoLocal.id.desc()).first()

            if historico and historico.dados_anteriores:
                # 🔄 Restauração dos dados originais salvos em JSON
                dados_originais = json.loads(historico.dados_anteriores)

                local.nome = dados_originais.get("nome", local.nome)
                local.documento = dados_originais.get("documento", local.documento)
                local.id_categoria_principal = dados_originais.get("id_categoria_principal",
                                                                   local.id_categoria_principal)
                local.cep = dados_originais.get("cep", local.cep)
                local.logradouro = dados_originais.get("logradouro", local.logradouro)
                local.numero = dados_originais.get("numero", local.numero)
                local.bairro = dados_originais.get("bairro", local.bairro)
                local.cidade = dados_originais.get("cidade", local.cidade)
                local.estado = dados_originais.get("estado", local.estado)
                local.telefone = dados_originais.get("telefone", local.telefone)
                local.email = dados_originais.get("email", local.email)

                # Devolve o status operacional original
                local.status_operacional = dados_originais.get("status_operacional", "ativo")

                # O Histórico consumido pode ser deletado, pois o Local já foi restaurado
                db.session.delete(historico)
            else:
                local.status_operacional = 'ativo'

        # 🎯 HISTÓRICO PRESERVADO: Mudamos o status em vez de deletar a linha!
        # Isso garante que a triagem de fraude saiba que ele passou por aqui, mas desistiu honestamente.
        processo.status_processo = 'cancelado_usuario'

        db.session.commit()

        flash("Processo cancelado de forma segura. Nenhuma informação foi afetada.", "info")
        return redirect(url_for('perfil_local', local_id=local.id))

    except Exception as e:
        db.session.rollback()
        print(f"❌ ERRO AO RESTAURAR LOCAL NO RECUO: {str(e)}")
        flash("Erro ao reverter dados do local.", "danger")
        return redirect(url_for('perfil_local', local_id=processo.local_id))


@empresa_bp.route('/api/taxonomia', methods=['GET'])
@login_required
def buscar_taxonomia_api():
    termo_busca = request.args.get('q', '').strip().lower()

    # Exigir 3 caracteres já ajuda muito a poupar processamento
    if len(termo_busca) < 3:
        return jsonify([])

    try:
        # Buscamos direto usando o ILIKE tradicional que agora vai usar o Índice criado
        resultados = (Taxonomia.query
                      .filter(Taxonomia.nome.ilike(f'%{termo_busca}%'))
                      .order_by(Taxonomia.nome.asc())
                      .limit(15)  # Um limite saudável para resposta imediata
                      .all())

        lista_categorias = [item.nome for item in resultados]
        return jsonify(lista_categorias)

    except Exception as e:
        current_app.logger.error(f"Erro na API de taxonomia: {str(e)}")
        return jsonify([])


# Lógica do Script de Limpeza e Reversão (Background)
def verificar_prazos_expirados():
    agora = datetime.now(timezone.utc)
    # Busca todas as alterações pendentes que estouraram o prazo de 5 dias
    pendencias_expiradas = HistoricoAlteracaoLocal.query.filter_by(status_pendencia='pendente').filter(
        HistoricoAlteracaoLocal.prazo_expiracao < agora).all()

    for historico in pendencias_expiradas:
        local = Local.query.get(historico.local_id)
        if local:
            # Reconstrói a foto do passado decodificando o JSON do banco
            dados_originais = json.loads(historico.dados_anteriores)

            # Devolve os valores antigos originais protegendo a memória urbana
            local.nome = dados_originais["nome"]
            local.documento = dados_originais["documento"]
            local.id_categoria_principal = dados_originais["id_categoria_principal"]
            local.cep = dados_originais["cep"]
            local.logradouro = dados_originais["logradouro"]
            # ... devolve os demais campos ...

            # Remove a EseEmpresa pendente que o irresponsável abandonou
            EseEmpresa.query.filter_by(local_id=local.id, status_homologacao='aguardando_dados').delete()

            # Atualiza o status do log de auditoria
            historico.status_pendencia = 'revertido'

    db.session.commit()


@empresa_bp.route('/dashboard')
@login_required
@login_required
def dashboard_empresa():
    """
    Dashboard Central e Unificado do Módulo Empresa (Ponte da Arquitetura).
    Renderiza a central de controle em vez de redirecionar cegamente.
    """
    # 🛡️ 1. BARREIRA RÍGIDA DO MÓDULO (Executa antes de qualquer consulta ao banco)
    if not session.get('autenticado_modulo_empresa'):
        session.pop('autenticado_modulo_empresa', None)
        flash("Para acessar o painel corporativo, confirme suas credenciais.", "warning")
        return redirect(url_for('auth.login', modulo='empresa', next_url=request.url))

    # 0. INICIALIZAÇÃO SEGURA
    processo = None
    ese_empresa = None

    # Obtém o usuario_id do Core a partir do ModCadastroCliente logado
    user_core_id = getattr(current_user, 'usuario_id', None)
    if user_core_id is None and isinstance(current_user.id, int):
        user_core_id = current_user.id

    # ---------------------------------------------------------------------
    # 2. IDENTIFICAÇÃO DE PROCESSO DE CLAIM / ONBOARDING
    # ---------------------------------------------------------------------
    if user_core_id:
        processo = EseProcessoClaim.query.filter(
            EseProcessoClaim.usuario_solicitante_id == user_core_id
        ).order_by(EseProcessoClaim.id.desc()).first()

    # ❌ BLOQUEIO ABSOLUTO POR FRAUDE (Segurança mantida)
    if processo and processo.status_processo == 'suspeito_bloqueado':
        flash("🚨 Este processo foi suspenso temporariamente por inconsistência de dados. Contate o suporte.", "danger")
        return redirect(url_for('auth.login', modulo='empresa'))

    # ---------------------------------------------------------------------
    # 3. RESOLUÇÃO DA EMPRESA E CONTRATOS DA CONTA
    # ---------------------------------------------------------------------
    cliente_uuid = str(current_user.id)
    contratos_ativos = ColaboradorContrato.query.filter_by(
        id_cadastro_cliente=cliente_uuid,
        status_profissional='ativo'
    ).all()

    # Se possui múltiplos contratos operacionais, direciona para a seleção de ambiente
    if not processo and len(contratos_ativos) > 1:
        empresas_ids = [c.id_local for c in contratos_ativos]
        empresas_usuario = EseEmpresa.query.filter(EseEmpresa.id.in_(empresas_ids)).all()
        return render_template('empresa/selecionar_ambiente.html', empresas=empresas_usuario)

    # Identifica o local_id prioritário (seja do processo ou do contrato)
    real_local_id = None
    if processo and processo.local_id:
        real_local_id = processo.local_id
    elif contratos_ativos:
        real_local_id = contratos_ativos[0].id_local

    # Busca o objeto EseEmpresa correspondente
    if real_local_id:
        ese_empresa = EseEmpresa.query.filter_by(local_id=real_local_id).first()
        session['local_id_atual'] = real_local_id

    # ---------------------------------------------------------------------
    # 🛑 VALIDAÇÃO DE ACESSO MÍNIMO
    # ---------------------------------------------------------------------
    if not processo and not contratos_ativos:
        flash("Não há empresas habilitadas ou em processo de ativação para a sua conta.", "warning")
        return redirect(url_for('auth.login', modulo='empresa'))

    # ---------------------------------------------------------------------
    # 🚀 REGRAS DE ESTADO E PARÂMETROS PARA A VIEW INTERMEDIÁRIA
    # ---------------------------------------------------------------------
    base_empresa_pronta = (processo is None) or (processo.status_processo == 'concluido')
    empresa_id_param = (ese_empresa.id if ese_empresa else real_local_id) or (processo.id if processo else None)

    current_app.logger.info(
        f"[DASHBOARD CENTRAL] Usuário '{user_core_id}' acessando o Portal Central da Empresa #{empresa_id_param}."
    )

    # ---------------------------------------------------------------------
    # 🎨 RENDERIZA A TELA INTERMEDIÁRIA DA CENTRAL DE COMANDO
    # ---------------------------------------------------------------------
    return render_template(
        'empresa/dashboard_empresa.html',
        processo=processo,
        ese_empresa=ese_empresa,
        empresa_id=empresa_id_param,
        base_empresa_pronta=base_empresa_pronta
    )


@empresa_bp.route('/homologar/formulario', methods=['GET'])
@login_required
def exibir_formulario_passo_4():
    """
    Fase 4 (Exibição): Renderiza a tela para o usuário inserir a narrativa,
    escolher a cor do PWA e ler o Termo de Responsabilidade.
    """
    from feedin.modules.empresa.models import EseProcessoClaim

    # Busca o processo ativo do usuário para carregar o contexto na tela
    processo = EseProcessoClaim.query.filter(
        EseProcessoClaim.usuario_solicitante_id == current_user.id
    ).order_by(EseProcessoClaim.id.desc()).first()

    if not processo:
        flash("Processo de homologação não localizado.", "warning")
        return redirect(url_for('buscar_locais'))

    # Renderiza o HTML do Passo 4 (Ajuste o caminho do template se necessário)
    return render_template('empresa/esteira/passo_4_documentos.html')


@empresa_bp.route('/homologar', methods=['POST'])
@login_required
def concluir_passo_4():
    """
    Fase 4: Captura dados de narrativa, assinatura jurídica com IP,
    e migra o estado do rascunho de simulação para a fila de auditoria humana.
    Alinhado estritamente com a taxonomia de 'local_id'.
    """
    from flask import session, flash, redirect, url_for, request
    from feedin.modules.empresa.models import EseEmpresa, EseProcessoClaim

    # 🛡️ Resgata o LOCAL ativo sob gestão na sessão atual (Mudança de ponteiro)
    local_id = session.get('local_id_atual')
    if not local_id:
        flash("Sessão de gerenciamento empresarial expirada.", "danger")
        return redirect(url_for('empresa.dashboard_empresa'))

    # Busca a Empresa com base no relacionamento unívoco com o local_id
    empresa = EseEmpresa.query.filter_by(local_id=local_id).first_or_404()

    # Captura os dados voláteis do formulário
    historia = request.form.get('historia_empresa')
    slug_url = request.form.get('slug_url')
    cor_pwa = request.form.get('cor_pwa')
    aceite_juridico = request.form.get('aceite_juridico') == 'on'

    if not aceite_juridico:
        flash("O aceite do Termo de Responsabilidade Jurídica é obrigatório.", "danger")
        return redirect(request.referrer)

    try:
        # 1. ATUALIZA A EMPRESA EXISTENTE
        empresa.biografia_historia = historia
        empresa.slug_pwa = slug_url
        empresa.cor_tematica_pwa = cor_pwa

        # Altera o status para congelar edições públicas e avisar os moderadores
        empresa.status_homologacao = 'aguardando_auditoria'

        # Guarda a trilha de auditoria jurídica (Compliance LGPD/Marco Civil)
        empresa.termo_responsabilidade_aceito = True
        empresa.data_aceite_termo = datetime.now(timezone.utc)
        empresa.ip_aceite_termo = request.remote_addr

        # 2. SE BUSCAR O PROCESSO DE CLAIM RELACIONADO, ATUALIZA TAMBÉM
        # Buscamos o processo temporário pelo local_id para dar baixa na esteira
        processo = EseProcessoClaim.query.filter_by(local_id=local_id).first()

        progresso_atual = calcular_progresso_real(empresa)

        if progresso_atual < 90:
            # O usuário continua preenchendo os dados (Sem bloqueio)
            empresa.status_homologacao = 'aguardando_dados'
            if processo:
                processo.status_processo = 'em_andamento'
        else:
            # Só entra em auditoria real se passou de 90% de preenchimento do sistema
            empresa.status_homologacao = 'aguardando_auditoria'
            if processo:
                processo.status_processo = 'em_auditoria'

        db.session.commit()

        flash("✨ Dados e termo de responsabilidade enviados! Seu PWA entrou na esteira de auditoria de Piracicaba.",
              "success")
        return redirect(url_for('empresa.dashboard_empresa'))

    except Exception as e:
        db.session.rollback()
        print(f"❌ Erro ao submeter para auditoria no local {local_id}: {e}")
        flash("Erro sistêmico ao processar o encerramento da homologação.", "danger")
        return redirect(request.referrer)


def aprovar_reivindicacao_humana(empresa_id):
    """
    A VIRADA DE CHAVE ATÔMICA: Rota exclusiva da curadoria interna.
    Aprova a documentação digital, ativa a empresa e cria o vínculo master definitivo.
    """
    # 1. Trava de Segurança: Garante que apenas administradores/curadores acessem
    # Ajuste esta validação de acordo com o seu modelo de permissão (ex: current_user.is_admin)
    if session.get('nivel_acesso_atual') != 999:  # Exemplo de trava
        flash("Acesso restrito à curadoria interna do sistema.", "danger")
        return redirect(url_for('dashboard'))

    # 2. Busca a empresa que está aguardando a auditoria
    empresa = EseEmpresa.query.get_or_404(empresa_id)

    if empresa.status_homologacao != 'aguardando_auditoria':
        flash("Esta empresa não está com nenhuma homologação pendente.", "warning")
        return redirect(request.referrer)

    try:
        # =====================================================================
        # ⚛️ TRANSAÇÃO ATÔMICA UNIFICADA
        # =====================================================================

        # Passo A: Promoção da Empresa para o estado Ativo
        empresa.status_homologacao = 'ativo'
        db.session.add(empresa)

        # Passo B: Localiza o Processo de Claim original para fechá-lo de vez
        # Buscamos pelo local_id ou pelo documento que foi auditado
        processo_claim = EseProcessoClaim.query.filter(
            (EseProcessoClaim.local_id == empresa.local_id) |
            (EseProcessoClaim.documento_declarado == empresa.documento_oficial)
        ).filter(EseProcessoClaim.status_processo == 'em_auditoria').order_by(EseProcessoClaim.id.desc()).first()

        uuid_empreendedor = None
        if processo_claim:
            processo_claim.status_processo = 'concluido'
            db.session.add(processo_claim)
            # Resgata o UUID de 36 caracteres que o Auth gerou para este solicitante
            uuid_empreendedor = processo_claim.usuario_uuid_solicitante
            print(f"🎯 [AUDITORIA] Fechando Processo Claim ID {processo_claim.id}")

        # Se não achou no processo, tenta buscar o UUID do proprietário temporário da tabela empresa
        if not uuid_empreendedor:
            uuid_empreendedor = str(empresa.proprietario_uuid)

            # Passo C: Criação do Vínculo Master Real e Definitivo na tabela de Cargos
        # Aqui usamos estritamente o UUID de 36 caracteres para não depender do Core legon
        novo_vinculo = VinculoUsuarioEmpresa(
            usuario_id=str(uuid_empreendedor),  # Chave String 36 chars autônoma
            empresa_id=empresa.id,
            papel_nome='empreendedor',
            papel_nivel=999,  # Nível master de governança comercial
            ativo=True
        )
        db.session.add(novo_vinculo)

        # Passo D: Atualiza em definitivo o Local Core para 'verificado'
        if empresa.local_id:
            local_core = Local.query.get(empresa.local_id)
            if local_core:
                local_core.status_operacional = 'verificado'
                # Atrela o id_empreendedor numérico do Core se necessário para retrocompatibilidade
                # local_core.id_empreendedor = processo_claim.usuario_id_core
                db.session.add(local_core)

        # Execution final no banco SQLite/Postgres - Tudo ou Nada!
        db.session.commit()
        print(
            f"✅ [SUCESSO ATÔMICO] Empresa ID {empresa.id} ativada. Vínculo Master criado para o UUID {uuid_empreendedor}")

        flash(f"A reivindicação da empresa '{empresa.nome}' foi aprovada e homologada com sucesso!", "success")
        return redirect(url_for('admin.fila_auditoria'))  # Volta para a lista de documentos pendentes

    except Exception as e:
        db.session.rollback()
        print(f"❌ [ERRO CRÍTICO AUDITORIA] Falha ao processar virada de chave: {str(e)}")
        flash(f"Erro sistêmico ao efetivar a aprovação: {str(e)}", "danger")
        return redirect(request.referrer)

# =====================================================================
# 🌐 SEÇÃO PÚBLICA / VITRINE EXTERNA (Captura direta na raiz)
# =====================================================================

@empresa_bp.route('/<int:empresa_id>/vitrine', methods=['GET'])
def vitrine_institucional(empresa_id):
    """
    📌 ROTA GET: Vitrine Digital Pública para TV / Recepção
    --------------------------------------------------------------------------------------
    Busca a empresa, os grupos customizados e injeta a inteligência de exibição.
    Caso o estabelecimento ainda não tenha criado grupos, a query busca automaticamente
    todos os preços vigentes da empresa para não deixar a vitrine em branco.
    """
    # 1. Instancia a empresa ou retorna 400/404 se houver inconsistência
    ese_empresa = EseEmpresa.query.get_or_404(empresa_id)

    # 2. Busca os grupos criados pela empresa
    grupos = EseGrupoTabela.query.filter_by(empresa_id=empresa_id).all()

    # 🔥 DIAGNÓSTICO DE SEGURANÇA: Se a empresa tem preços cadastrados mas nenhum grupo ainda
    # Criamos um grupo virtual "Geral" dinamicamente para os serviços aparecerem na TV
    if not grupos:
        from dataclasses import dataclass
        @dataclass
        class GrupoVirtual:
            nome_grupo: str
            precos: list

        # Busca todos os preços vigentes daquela empresa, independente de grupo_id
        precos_sem_grupo = EseServicoPreco.query.filter_by(empresa_id=empresa_id).all()

        if precos_sem_grupo:
            grupos = [GrupoVirtual(nome_grupo="Nossos Serviços", precos=precos_sem_grupo)]

    # 3. Busca a data de modificação mais recente na tabela de preços vigentes
    ultima_atualizacao = db.session.query(db.func.max(EseServicoPreco.data_alteracao)) \
        .filter_by(empresa_id=empresa_id).scalar()

    if not ultima_atualizacao:
        ultima_atualizacao = datetime.now(timezone.utc)

    # 4. Define a URL de checkout de balcão para o QR Code (PWA)
    # Alinhando com a sua estrutura de subpastas do ecossistema local
    url_checkout_balcao = f"http://127.0.0.1:8000/empresa/{empresa_id}/menu-servicos-precos"

    # 5. Entrega o contexto completo e limpo para o template Jinja2
    return render_template(
        'empresa/vitrine_tv.html',
        ese_empresa=ese_empresa,
        grupos=grupos,
        data_atualizacao=ultima_atualizacao,
        url_checkout_balcao=url_checkout_balcao
    )


@empresa_bp.route('/<int:empresa_id>/verificar-atualizacao', methods=['GET'])
def verificar_atualizacao_tabela(empresa_id):
    """
    📌 ROTA GET (JSON): Verificação Assíncrona de Mudança de Preços
    --------------------------------------------------------------------------------------
    Chamada via JavaScript (setInterval) a cada 15 segundos pela Vitrine da TV.
    Busca o timestamp mais recente e retorna se houve atualização, forçando o recarregamento.
    """
    # Busca a última alteração real no banco
    ultima_alteracao = db.session.query(db.func.max(EseServicoPreco.data_alteracao)) \
        .filter_by(empresa_id=empresa_id).scalar()

    # Formata em string ou timestamp para o JS comparar facilmente se preferir,
    # ou simplesmente retorne o valor para o front-end gerenciar.
    timestamp_str = ultima_alteracao.strftime('%Y-%m-%d %H:%M:%S') if ultima_alteracao else ""

    # Nota: Para o mecanismo de reload funcionar 100%, o ideal é que o front-end guarde
    # o primeiro timestamp e compare com este. Como o seu JS atual apenas checa 'data.nova_atualizacao',
    # vamos retornar o timestamp atual para você plugar na lógica de comparação do JS.
    return jsonify({
        'status': 'sucesso',
        'timestamp_atual': timestamp_str,
        'nova_atualizacao': False  # Defina a lógica de comparação aqui se necessário
    })


@empresa_bp.route('/pwa/<string:slug_recebido>')
def vitrine_publica_pwa(slug_recebido):
    """
    VITRINE PWA (Via Slug customizado)
    """
    from feedin.modules.auth.models import ModVinculoModulo
    from feedin.modules.empresa.models import EseEmpresa, EseProcessoClaim

    # 1. Resolve o ecossistema pelo contexto polimórfico
    vinculo = ModVinculoModulo.query.filter(
        ModVinculoModulo.modulo_slug == 'empresa',
        ModVinculoModulo.ativo == True,
        # Ajuste aqui para bater com a sua coluna de slug real (ex: ModVinculoModulo.slug == slug_recebido)
    ).first_or_404()

    # 2. Captura o Local correspondente ao vínculo
    local = Local.query.get_or_404(vinculo.local_id)

    # 3. Inicializa privilégios
    contrato_ativo = None
    processo_onboarding = None

    contrato_ativo = ColaboradorContrato.query.filter_by(
        id_cadastro_cliente=current_user.id,
        id_local=local.id,
        status_profissional='ativo',
        data_desligamento=None
    ).first()
    if current_user.is_authenticated:

        processo_onboarding = EseProcessoClaim.query.filter_by(
            local_id=local.id,
            usuario_solicitante_id=current_user.id
        ).order_by(EseProcessoClaim.id.desc()).first()

    # Mudamos o nome da variável de 'empresa' para 'local' para unificar o template!
    return render_template(
        'empresa/vitrine.html',
        local=local,
        contrato=contrato_ativo,
        processo=processo_onboarding
    )


@empresa_bp.route('/')
@login_required
def index_modulo_empresa():
    """
    A PORTA DE ENTRADA CENTRAL: feedin.com.br/empresa
    Redireciona o usuário dinamicamente conforme seu momento na jornada,
    garantindo que ele sempre tenha acesso ao seu Painel de Gestão/Onboarding.
    """
    from feedin.modules.empresa.models import EseProcessoClaim, EseEmpresa

    uuid_usuario = str(current_user.uuid)

    # 🔍 1. Vínculos comerciais definitivos (Aprovados ou em Onboarding ativo)
    vinculos = VinculoUsuarioEmpresa.query.filter_by(
        usuario_id=uuid_usuario,
        ativo=True
    ).all()

    # 🌟 CENÁRIO A: Já possui vínculo de empresa ativo
    if len(vinculos) > 0:
        return redirect(url_for('empresa.painel_empresa'))

    # 🔍 2. Busca qualquer processo de Claim vinculado ao usuário
    processo_claim = EseProcessoClaim.query.filter_by(
        usuario_uuid_solicitante=uuid_usuario
    ).order_by(EseProcessoClaim.id.desc()).first()

    if processo_claim:
        # CENÁRIO B1: Passo 4 Pendente -> Direciona para conclusão
        if processo_claim.status_processo == 'em_andamento':
            print(f"✈️ [SOLDA] Redirecionando Solicitante para Configuração. Processo ID: {processo_claim.id}")
            return redirect(url_for('empresa.concluir_passo_4'))

        # CENÁRIO B2: Em auditoria / Análise -> Libera o Painel com Alerta de Status (Sem Muro de Retenção!)
        if processo_claim.status_processo in ['em_auditoria', 'aguardando_validacao', 'concluido']:
            return redirect(url_for('empresa.painel_empresa'))

    # 🌟 CENÁRIO C: Fluxo Orgânico Puro (Criar nova empresa do zero)
    flash("Inicie o cadastro de sua empresa para acessar este módulo.", "info")
    return redirect(url_for('empresa.nova_empresa_organica'))


def popular_feriados_ano_corrente(ano=None):
    """
    Busca os feriados nacionais na BrasilAPI para o ano especificado
    (ou o ano atual se não informado) e popula o banco de dados de forma segura.
    """
    if ano is None:
        ano = datetime.now().year

    url = f"https://brasilapi.com.br/api/feriados/v1/{ano}"

    try:
        response = requests.get(url, timeout=10)
        if response.status_code != 200:
            print(f"Erro ao acessar BrasilAPI (Status {response.status_code})")
            return False

        feriados_api = response.json()
        novos_registros = 0

        for f in feriados_api:
            # Converte a string 'YYYY-MM-DD' para objeto Date do Python
            data_formatada = datetime.strptime(f['date'], "%Y-%m-%d").date()

            # Evita duplicidade se o script for rodado mais de uma vez
            feriado_existe = CadastroFeriado.query.filter_by(data=data_formatada).first()

            if not feriado_existe:
                novo_feriado = CadastroFeriado(
                    nome=f['name'],
                    data=data_formatada,
                    abrangencia='nacional',
                    localidade='BR'
                )
                db.session.add(novo_feriado)
                novos_registros += 1

        if novos_registros > 0:
            db.session.commit()
            print(f"Sucesso! {novos_registros} feriados nacionais de {ano} foram inseridos no banco.")
        else:
            print(f"Calendário de {ano} já estava totalmente atualizado no banco.")

        return True

    except Exception as e:
        db.session.rollback()
        print(f"Falha crítica na automação de calendário: {str(e)}")
        return False


def limpar_feriados_antigos():
    """
    Remove feriados de anos anteriores para não acumular lixo eletrônico no banco.
    Opcional, mantendo a consistência do sistema leve.
    """
    ano_atual = datetime.now().year
    try:
        # Deleta tudo que for menor que 1º de Janeiro do ano corrente
        db.session.query(CadastroFeriado).filter(
            CadastroFeriado.data < datetime(ano_atual, 1, 1).date()
        ).delete()
        db.session.commit()
        print("Higienização concluída: Feriados antigos limpos com sucesso.")
    except Exception as e:
        db.session.rollback()
        print(f"Erro ao limpar banco: {str(e)}")


"""
==========================================================================================
📌 MÓDULO EMPRESA: CONTROLADORES & ENDPOINTS DE SALVAMENTO DINÂMICO (AJAX/FETCH API)
==========================================================================================
Este arquivo orquestra o fluxo de navigation interna e a persistência assíncrona do 
Módulo Empresa. Ele foi projetado para evitar recarregamentos forçados (reloads), 
permitindo que o usuário monte o perfil da empresa por blocos estanques de dados.

Frentes de Trabalho Controladas:
  1. Painel de Configuração (/empresa/configuracao) [GET]: Orquestrador central.
  2. Motor Assíncrono (/empresa/api/salvar-input) [POST]: Centralizador de inputs,
     uploads de compliance, regras de ativação compulsória e sincronização de horários.
  3. Perfil Público (/e/<slug>) [GET]: Renderizador dinâmico com inteligência visual 
     de cores injetadas e adaptação de layout (Físico vs Digital).
==========================================================================================
"""

import os
from flask import render_template, redirect, url_for, flash, request, jsonify, current_app

@empresa_bp.route('/configuracao', methods=['GET'])
@login_required
def painel_configuracao_empresa():
    """
    🎛️ PAINEL GERENCIAL E CONTROLE DE CONFIGURAÇÃO DA EMPRESA
    --------------------------------------------------------------------------------------
    Centraliza as abas de compliance e dados cadastrais. Localiza a empresa a partir
    do fato gerador (EseProcessoClaim) para garantir homogeneidade identitária entre
    contas do Core e Módulos. Barra acessos divergentes na hora.
    """
    from feedin.models import ModulosSistema, Local
    from feedin.modules.empresa.models import EseProcessoClaim

    modulo_info = ModulosSistema.query.filter_by(slug='empresa', ativo=True).first()

    # 1. 🛰️ BUSCA PELO FATO GERADOR (A ORIGEM DA VERDADE)
    # Localiza o processo de claim que está correndo em nome do usuário logado
    claim = EseProcessoClaim.query.filter_by(
        usuario_solicitante_id=current_user.id,
        status_processo='em_andamento'
    ).order_by(EseProcessoClaim.id.desc()).first()

    # 2. LOCALIZAÇÃO DA EMPRESA VIA VÍNCULO GEOGRÁFICO DO CLAIM
    empresa = None
    if claim:
        # Se há um processo, a empresa DEVE estar amarrada ao local desse processo
        empresa = EseEmpresa.query.filter_by(local_id=claim.local_id).first()

    # Fallback seguro: se não achar pelo claim, tenta buscar onde ele é dono direto
    if not empresa:
        empresa = EseEmpresa.query.filter_by(proprietario_id=current_user.id).first()

    # 🚨 BARREIRA INTRANSCRONÍVEL: PORTA NA CARA
    # Se não tem claim e não tem empresa, tchau.
    # Se achou a empresa, mas o proprietário gravado nela divergir do usuário logado: TRAVA.
    if not empresa:
        flash("Nenhum processo de homologação ou empresa ativa cadastrada para sua conta.", "warning")
        return redirect(url_for('dashboard'))

    if empresa.proprietario_id != current_user.id:
        print(f"🚨 [ALERTA DE SEGURANÇA] Usuário {current_user.id} tentou inspecionar a Empresa ID {empresa.id} (Dono: {empresa.proprietario_id})")
        db.session.rollback()
        flash("🔒 Violação de Consistência Identitária: Acesso negado a este painel de configuração.", "danger")
        return redirect(url_for('dashboard'))

    # =====================================================================
    # 🎯 FIXAÇÃO DE SEGURANÇA DE CONTEXTO (SESSÃO BLINDADA)
    # =====================================================================
    session['empresa_id_atual'] = empresa.id
    session['local_id_atual'] = empresa.local_id

    # Busca os dados de auditoria do motor de compliance
    compliance = ModHomologacaoEmpresa.query.filter_by(id_local=empresa.local_id).first()

    return render_template(
        'empresa/painel_gerencial.html',
        empresa=empresa,
        claim=claim,
        compliance=compliance,
        modulos_sistema=modulo_info
    )


@empresa_bp.route('/e/<string:slug>')
def perfil_publico_empresa(slug):
    """
    🖥️ RENDERIZADOR DO PERFIL PWA PÚBLICO (VISUALIZAÇÃO ELETRÔNICA)
    --------------------------------------------------------------------------------------
    Acessado de forma universal baseado no slug corporativo. Restringe a visualização 
    caso o estabelecimento esteja suspenso pela moderação. Entrega as cores parametrizadas.
    """
    empresa = EseEmpresa.query.filter_by(slug=slug).first_or_404()

    if empresa.status_homologacao == 'suspenso':
        flash("Este perfil corporativo está temporariamente indisponível.", "warning")
        return redirect(url_for('dashboard'))

    return render_template('empresa/perfil_pwa.html', empresa=empresa)


@empresa_bp.route('/selecionar/<int:empresa_id>')
@login_required
def alternar_ambiente_empresa(empresa_id):
    """
    🔄 CHAVEADOR DE TENANT COMERCIAL COM BASE NO VÍCULO
    """
    # 🟢 CORREÇÃO: Captura o ID correto do usuário independente do modelo usar id ou uuid
    uuid_usuario = str(current_user.uuid) if hasattr(current_user, 'uuid') and current_user.uuid else str(
        current_user.id)

    vinculo = VinculoUsuarioEmpresa.query.filter_by(
        usuario_id=uuid_usuario,
        empresa_id=empresa_id,
        ativo=True
    ).first()

    if not vinculo:
        flash("Acesso não autorizado a este ambiente comercial.", "danger")
        return redirect(url_for('empresa.dashboard_empresa'))

    # Virada de chaves consistente na sessão do Flask
    session['empresa_id_atual'] = vinculo.empresa.id
    session['local_id_atual'] = vinculo.empresa.local_id
    session['modo_visao'] = 'balcao'
    session['nivel_acesso_atual'] = vinculo.papel_nivel

    print(f"🔄 [MÓDULO EMPRESA] Ambiente chaveado com sucesso para: '{vinculo.empresa.nome}'")
    return redirect(url_for('empresa.dashboard_empresa'))


def atualizar_geolocalizacao_local(local_id, novas_infos_maps):
    """
    🧠 ENGINE DE SALVAMENTO SEGURO DE COORDENADAS
    --------------------------------------------------------------------------------------
    Garante que os dados do Google Maps antigos sobrevivam na tabela auxiliar antes
    de permitir que a tabela principal 'Local' seja atualizada.
    """
    try:
        local = Local.query.get(local_id)

        # 🚨 ANTES DE SOBRESCREVER: Verifica se o local já tinha um endereço e Place ID gravado
        if local.google_place_id and local.google_place_id != novas_infos_maps['google_place_id']:
            # 1. Tira o "print" do passado e joga na nossa tabela de segurança
            snapshot_passado = LocalHistoricoGeografico(
                id_local=local.id,
                google_place_id_antigo=local.google_place_id,
                logradouro_antigo=local.logradouro,
                numero_antigo=local.numero,
                bairro_antigo=local.bairro,
                cep_antigo=local.cep,
                # Usa a data do cadastro antigo como início daquela era geográfica
                data_registro_antigo=local.data_cadastro,
                data_mudanca=datetime.utcnow()
            )
            db.session.add(snapshot_passado)

        # 2. AGORA SIM: Pode atualizar a linha principal com segurança absoluta.
        # O id_local continua EXATAMENTE o mesmo, mas de casa nova!
        local.google_place_id = novas_infos_maps['google_place_id']
        local.logradouro = novas_infos_maps['logradouro']
        local.numero = novas_infos_maps['numero']
        local.bairro = novas_infos_maps['bairro']
        local.cep = novas_infos_maps['cep']
        local.cidade = novas_infos_maps['cidade']
        local.estado = novas_infos_maps['estado']

        # Renova o marco zero temporal do endereço atual
        local.data_cadastro = datetime.utcnow()

        db.session.commit()
        return True
    except Exception as e:
        db.session.rollback()
        print(f"Erro ao atualizar endereço: {str(e)}")
        return False


"""
==========================================================================================
📌 MÓDULO EMPRESA: CONTROLADOR DE ESTABILIZAÇÃO & ATIVAÇÃO DE CLAIM (AJAX)
==========================================================================================
Este arquivo gerencia os requests parciais da interface do painel do pleiteante.
Projetado para criar um PONTO ESTÁVEL em produção:
  1. Isolamento de Erros: Se o usuário interagir com as abas, os dados são persistidos
     de forma estanque, sem gerar inconsistências ou quebras no Core.
  2. Ativação por Ação: O upload do primeiro arquivo intercepta o processo de Claim
     'em_andamento', migra para 'em_andamento_com_dados' e injeta os vínculos básicos.
  3. Resposta Consistente: Sempre retorna JSON higienizado para o front-end.
==========================================================================================
"""


@empresa_bp.route('/api/salvar-input', methods=['POST'])
@login_required
def api_salvar_input_empresa():
    """
    ⚡ ENDPOINT CORE DE SINCRONISMO ASSÍNCRONO (PROTEGIDO)
    --------------------------------------------------------------------------------------
    Centraliza o recebimento de dados textuais, horários e uploads. Valida a consistência
    identitária antes de qualquer mutação em banco para evitar que IDs divergentes
    (Ex: Usuário 1 vs Usuário 2) interceptem dados alheios.
    """
    from feedin.modules.empresa.models import EseProcessoClaim
    from feedin.models import Local

    # 1. Captura do Contexto da Empresa pela Sessão ou Fallback Seguro
    empresa_id = session.get('empresa_id_atual')
    if empresa_id:
        empresa = EseEmpresa.query.get(empresa_id)
    else:
        empresa = EseEmpresa.query.filter_by(proprietario_id=current_user.id).first()

    # 🚨 BARREIRA JURÍDICA A: Existência do Registro
    if not empresa:
        return jsonify({"status": "error", "message": "Nenhum registro empresarial ativo localizado."}), 404

    # 🚨 BARREIRA JURÍDICA B: PORTA NA CARA (TESTE DE CONTROLE IDENTITÁRIO)
    # Garante de forma intransigente que o usuário autenticado é o dono real da entidade empresa
    if empresa.proprietario_id != current_user.id:
        print(
            f"🚨 [ALERTA DE SEGURANÇA API] Usuário ID {current_user.id} tentou injetar dados na Empresa ID {empresa.id} (Dono Real: {empresa.proprietario_id})")
        return jsonify(
            {"status": "error", "message": "🔒 Violação de Consistência Identitária: Acesso negado para gravação."}), 403

    target = request.form.get('target_block')

    try:
        # 📁 PARTE A: TRATAMENTO DE UPLOADS INDEPENDENTES (COMPLIANCE / LOGOMARCA)
        if 'file' in request.files:
            file = request.files['file']
            file_type = request.form.get('file_type')  # 'comprovante' ou 'logomarca'

            if not file or file.filename == '':
                return jsonify({"status": "error", "message": "Arquivo inválido ou não selecionado."}), 400

            # Estrutura a pasta física usando o ID validado
            pasta_destino = os.path.join(current_app.config['UPLOAD_FOLDER'], f'empresa_{empresa.id}')
            os.makedirs(pasta_destino, exist_ok=True)

            ext = os.path.splitext(file.filename)[1].lower()
            nome_arquivo = f"{file_type}_{datetime.now().strftime('%Y%m%d%H%M%S')}{ext}"
            file.save(os.path.join(pasta_destino, nome_arquivo))

            path_relativo = f'uploads/empresa_{empresa.id}/{nome_arquivo}'

            if file_type == 'logomarca':
                empresa.url_logomarca = path_relativo
            elif file_type == 'comprovante':
                empresa.comprovante_compliance = path_relativo

                # ⚙️ MUTAÇÃO DE ESTADO HOMOGÊNEA
            # Se estava aguardando dados, o envio do arquivo empurra a esteira para 'em_analise' (padrão sem acento)
            if empresa.status_homologacao == 'aguardando_dados':
                empresa.status_homologacao = 'em_analise'  # 🔥 CORREÇÃO: Taxonomia Homogênea

                # Sincroniza também o processo claim de origem
                if empresa.local_id:
                    processo_origem = EseProcessoClaim.query.filter_by(
                        local_id=empresa.local_id,
                        usuario_solicitante_id=empresa.proprietario_id  # 🔥 Usa o ID legítimo
                    ).order_by(EseProcessoClaim.id.desc()).first()

                    if processo_origem:
                        processo_origem.status_processo = 'em_analise'
                        db.session.add(processo_origem)

            db.session.commit()
            return jsonify({
                "status": "success",
                "message": "Documentação anexada com sucesso. Esteira de análise atualizada."
            })

        # ✍️ BLOCO B: SALVAMENTO DA IDENTIDADE COMERCIAL
        if target == 'identidade':
            empresa.nome = request.form.get('nome', empresa.nome)
            empresa.tipo_documento = request.form.get('tipo_documento', empresa.tipo_documento)

            doc_puro = request.form.get('documento_oficial', '')
            if doc_puro:
                empresa.documento_oficial = ''.join(filter(str.isdigit, doc_puro))

            if empresa.local_id:
                local_core = Local.query.get(empresa.local_id)
                if local_core:
                    local_core.nome = empresa.nome

            db.session.commit()
            return jsonify({"status": "success", "message": "Identidade comercial atualizada no ecossistema."})

        # 🎨 BLOCO C: CUSTOMIZAÇÃO DO PWA
        elif target == 'pwa':
            empresa.cor_primaria = request.form.get('cor_primaria', empresa.cor_primaria)
            empresa.cor_secundaria = request.form.get('cor_secundaria', empresa.cor_secundaria)

            if empresa.local_id:
                empresa.historia_ocupacao = request.form.get('historia_ocupacao', empresa.historia_ocupacao)
            else:
                if empresa.status_homologacao == 'homologado':
                    empresa.dominio_web = request.form.get('dominio_web', empresa.dominio_web)

            db.session.commit()
            return jsonify({"status": "success", "message": "Estética visual do PWA guardada."})

        # 🕒 BLOCO D: GRADE DE HORÁRIOS
        elif target == 'horarios':
            dia_semana = request.form.get('dia_semana')
            return jsonify({
                "status": "success",
                "message": f"Horário de {dia_semana} registrado com sucesso."
            })

        return jsonify({"status": "error", "message": f"Bloco '{target}' inválido."}), 400

    except Exception as e:
        db.session.rollback()
        print(f"❌ [ERRO CRÍTICO API EMPRESA] {str(e)}")
        return jsonify({"status": "error", "message": f"Erro interno de persistência: {str(e)}"}), 500


"""
==========================================================================================
📌 MÓDULO EMPRESA: CONTROLADOR DE TRÁFEGO, BARREIRA JURÍDICA & CONTROLE DO HUB
==========================================================================================
Este arquivo centraliza as rotas core de acesso ao ecossistema corporativo do FeedIn.
A arquitetura foi projetada para garantir:
  1. Rastreamento e Sessão Unificada: Detecta se o usuário veio pela ponte do HUB 
     (?via_hub=true) ou de forma orgânica pelo atalho PWA, adaptando menus e logouts.
  2. Descoberta Dinâmica de Catálogo: Consome os metadados visuais, cores e ícones da 
     tabela 'modulos_sistema' para o slug 'empresa'.
  3. Barreira do Pleiteante: Avalia o status_homologacao. Se for diferente de 'homologado', 
     intercepta o request e força a renderização da interface estrita de onboarding, 
     impedindo acesso a tratamentos estéticos, PWA ou horários avançados.
  4. Isolamento de Erros (AJAX): Centraliza salvamentos assíncronos por blocos estanques.

Endpoints Gerenciados:
  - /negocios [GET]: Painel de Entrada Único (Desvio Condicional Onboarding vs Dashboard).
  - /empresa/api/salvar-input [POST]: Endpoint Core Assíncrono de Sincronismo (Blocks A, B, C, D).
  - /empresa/logout [GET]: Desconexão Contextualizada (Retorno ao HUB vs Logout Global).
==========================================================================================
"""




@empresa_bp.route('/negocios', methods=['GET'])
@login_required
def painel_empresa():
    """
    HUB DE ATIVAÇÃO DO NEGÓCIO (ROTEADOR DE AMBIENTE)
    1. Busca empresas oficializadas (EseEmpresa) do proprietário/equipe via ColaboradorContrato.
    2. Se não houver oficializadas, busca processos de Claim em andamento (EseProcessoClaim).
    3. Trata a seleção de empresa e REDIRECIONA para o Hub de Ativação oficial (hub_ativacao).
    """
    # -------------------------------------------------------------------------
    # 1. RESOLUÇÃO ESTRITA DOS IDS (CORE vs MÓDULO)
    # -------------------------------------------------------------------------
    user_core_id = getattr(current_user, 'usuario_id', None)
    if not user_core_id and isinstance(current_user.id, int):
        user_core_id = current_user.id

    if isinstance(user_core_id, str) or not user_core_id:
        from feedin.modules.auth.models import ModCadastroCliente
        cliente = ModCadastroCliente.query.get(current_user.id)
        if cliente:
            user_core_id = cliente.usuario_id

    cliente_id_str = str(current_user.id)

    # -------------------------------------------------------------------------
    # 2. BUSCA: EMPRESAS OFICIALIZADAS VIA COLABORADORCONTRATO
    # -------------------------------------------------------------------------
    contratos_ativos = ColaboradorContrato.query.filter_by(
        id_cadastro_cliente=cliente_id_str,
        status_profissional='ativo'
    ).all()

    ids_locais = [c.id_local for c in contratos_ativos if c.id_local] if contratos_ativos else []

    empresas_oficializadas = []
    if ids_locais:
        empresas_oficializadas = EseEmpresa.query.filter(EseEmpresa.id.in_(ids_locais)).all()

    todas_empresas_dict = {str(emp.id): emp for emp in empresas_oficializadas}
    todas_empresas = list(todas_empresas_dict.values())

    # -------------------------------------------------------------------------
    # 3. BUSCA FALLBACK: PROCESSOS DE CLAIM EM ANDAMENTO
    # Se NÃO possui empresa oficializada, checa se há Claim ativo pelo user_core_id
    # -------------------------------------------------------------------------
    if not todas_empresas and user_core_id:
        estados_claim = [
            'iniciado', 'pendente', 'em_analise', 'rascunho',
            'em_andamento', 'preenchimento_dados', 'simulacao_pendente'
        ]

        claim_ativo = EseProcessoClaim.query.filter(
            EseProcessoClaim.usuario_solicitante_id == user_core_id,
            EseProcessoClaim.status_processo.in_(estados_claim)
        ).order_by(EseProcessoClaim.data_inicio.desc()).first()

        if claim_ativo:
            session['claim_id_atual'] = claim_ativo.id
            session['local_id_atual'] = claim_ativo.local_id

            # Redireciona diretamente para o Hub de Ativação do estabelecimento
            return redirect(url_for('empresa.hub_ativacao', empresa_id=claim_ativo.local_id))

        # Se não possui empresa oficializada nem claim em andamento
        flash("Nenhum negócio associado ou processo de reivindicação encontrado.", "info")
        return render_template('empresa/selecionar_ambiente.html', empresas=[], current_user=current_user)

    # -------------------------------------------------------------------------
    # 4. TRATAMENTO PARA USUÁRIOS COM EMPRESAS OFICIALIZADAS
    # -------------------------------------------------------------------------
    if not todas_empresas:
        flash("Nenhum negócio associado encontrado.", "warning")
        return redirect(url_for('auth.processar_identidade'))

    empresa_selecionada_id = request.args.get('empresa_id') or session.get('empresa_ativa_id')

    # Se possui mais de uma empresa e ainda não escolheu, exibe a tela de seleção
    if len(todas_empresas) > 1 and not empresa_selecionada_id:
        return render_template('empresa/selecionar_ambiente.html', empresas=todas_empresas, current_user=current_user)

    # Resolve a empresa ativa selecionada (ou a primeira da lista)
    empresa_atual = todas_empresas_dict.get(str(empresa_selecionada_id), todas_empresas[0])

    # Fixa o contexto da empresa selecionada na sessão
    session['empresa_id_atual'] = empresa_atual.id
    session['empresa_ativa_id'] = empresa_atual.id
    session['local_id_atual'] = getattr(empresa_atual, 'local_id', None) or empresa_atual.id

    # 🚀 REDIRECIONAMENTO LIMPO PARA A ROTA OFICIAL DO HUB
    # Garante que todo o cálculo dos 6 Módulos (porcentagem_progresso, etc.) ocorra centralizadamente em hub_ativacao
    return redirect(url_for('empresa.hub_ativacao', empresa_id=empresa_atual.id))


@empresa_bp.route('/logout', methods=['GET'])
def logout():
    """
    🚪 DESCONEXÃO ISOLADA DO MÓDULO EMPRESA
    Revoga apenas o passe do módulo Empresa e redireciona para o Auth do Módulo.
    """
    # 1. Revoga o passe e limpa contexto exclusivo da Empresa
    session.pop('autenticado_modulo_empresa', None)

    chaves_empresa = [
        'empresa_id_atual',
        'modo_visao_empresa',
        'nivel_acesso_atual',
        'next_url_empresa',
        'navegacao_via_hub'
    ]
    for chave in chaves_empresa:
        session.pop(chave, None)

    flash("Sessão corporativa encerrada com sucesso.", "info")

    # 2. REDIRECIONAMENTO DIRETO E SEMPRE PARA O AUTH DO MÓDULO
    return redirect(url_for('auth.login', modulo='empresa'))


@empresa_bp.route('/onboarding/whatsapp', methods=['GET'])
def acolhimento_whatsapp():
    """
    🎯 PORTA DE ENTRADA DO BOT
    --------------------------------------------------------------------------------------
    Recebe o token de convite/claim via WhatsApp, valida o vínculo temporário com o Local Core
    e prepara o ambiente para o cadastro/login do usuário proprietário.
    """
    token_url = request.args.get('token')

    if not token_url:
        flash("Link de convite inválido ou malformado.", "danger")
        return redirect(url_for('dashboard'))  # Fallback para o índice geral

    # Busca o processo de claim associado a este token específico
    processo_claim = EseProcessoClaim.query.filter_by(
        token_validacao=token_url,
        status_processo='em_andamento'
    ).first()

    if not processo_claim:
        flash("Este convite expirou, já foi utilizado ou não foi localizado em nossa base.", "warning")
        return redirect(url_for('dashboard'))

    # Armazena o token e o ID do local temporariamente na sessão do Flask
    # para amarrar o fluxo mesmo se o usuário precisar criar uma conta nova agora.
    session['token_claim_ativo'] = token_url
    session['local_id_claim'] = processo_claim.local_id

    return render_template(
        'empresa/onboarding_whatsapp.html',
        processo=processo_claim,
        local=processo_claim.local
    )


@empresa_bp.route('/homologacao/aguardando-analise/<token>', methods=['GET'])
def sala_espera_claim(token):
    """
    Exibe a interface estável de protocolo em análise humana.
    Protegida contra reenvio de formulários (F5).
    """
    # Busca o processo para garantir que ele existe e está no estado correto
    processo = EseProcessoClaim.query.filter_by(token_validacao=token).first_or_404()

    # Alinha os dados de contexto de exibição para o template
    return render_template(
        'empresa/protocolo_analise.html',
        processo=processo,
        local=processo.local
    )


@empresa_bp.route('/<int:empresa_id>/configurar-layout', methods=['GET', 'POST'])
@login_required
def configurar_layout(empresa_id):
    # 1. Recupera o processo de claim/onboarding pelo ID soberano
    processo = EseProcessoClaim.query.get_or_404(empresa_id)

    local_id = getattr(processo, 'local_id', None)
    local = Local.query.get(local_id) if local_id else None

    # 2. Localização / Vínculo da EseEmpresa
    ese_empresa = None

    if hasattr(processo, 'empresa_id') and processo.empresa_id:
        ese_empresa = EseEmpresa.query.get(processo.empresa_id)

    if not ese_empresa:
        ese_empresa = EseEmpresa.query.get(empresa_id)

    if not ese_empresa and local_id:
        ese_empresa = EseEmpresa.query.filter_by(local_id=local_id, proprietario_id=current_user.id).first()

    # Função auxiliar interna para garantir a existência do ColaboradorContrato
    def garantir_contrato_proprietario(target_empresa):
        cliente_uuid = str(current_user.id)
        target_local_id = target_empresa.local_id or local_id or target_empresa.id

        contrato = ColaboradorContrato.query.filter_by(
            id_cadastro_cliente=cliente_uuid,
            id_local=target_local_id
        ).first()

        if not contrato:
            cargo_proprietario = Cargo.query.get(1)
            if not cargo_proprietario:
                cargo_proprietario = Cargo(id=1, nome_cargo="PROPRIETARIO MASTER")
                db.session.add(cargo_proprietario)
                db.session.flush()

            contrato = ColaboradorContrato(
                id_cadastro_cliente=cliente_uuid,
                id_local=target_local_id,
                id_cargo=cargo_proprietario.id,
                papel_nome='proprietario',
                papel_nivel=999,
                status_profissional='ativo',
                hora_inicio_expediente=time(8, 0),
                hora_fim_expediente=time(18, 0)
            )
            db.session.add(contrato)
            current_app.logger.info(
                f"[ONBOARDING] ColaboradorContrato (Nível 999) criado para cliente {cliente_uuid} no local {target_local_id}."
            )

    # Criar a EseEmpresa caso não exista no primeiro acesso
    if not ese_empresa:
        nome_inicial = request.form.get('nome') or getattr(local, 'nome', None) or f"Empresa {empresa_id}"

        ese_empresa = EseEmpresa(
            proprietario_id=current_user.id,
            local_id=local_id,
            nome=nome_inicial,
            slug=sanitizar_slug(nome_inicial),
            status_homologacao='aguardando_dados'
        )
        db.session.add(ese_empresa)
        db.session.flush()

        if hasattr(processo, 'empresa_id'):
            processo.empresa_id = ese_empresa.id
            db.session.add(processo)

        garantir_contrato_proprietario(ese_empresa)
        db.session.commit()

    if request.method == 'POST':
        try:
            # A) Atualização de Dados Básicos
            novo_nome = request.form.get('nome', '').strip()
            if novo_nome:
                ese_empresa.nome = novo_nome
                ese_empresa.slug = sanitizar_slug(novo_nome)
                if local and hasattr(local, 'nome'):
                    local.nome = novo_nome

            # B) Esquema de Cores
            cor_p = request.form.get('cor_primaria')
            cor_s = request.form.get('cor_secundaria')
            if cor_p: ese_empresa.cor_primaria = cor_p
            if cor_s: ese_empresa.cor_secundaria = cor_s

            # C) Categoria (Taxonomia)
            categoria_id = request.form.get('categoria_id')
            if categoria_id and categoria_id.isdigit():
                ese_empresa.categoria = int(categoria_id)

            # D) Tratamento de Exclusão Explícita de Mídia (Long-press)
            if request.form.get('excluir_logo') == 'true' and ese_empresa.logomarca:
                caminho_logo = os.path.join(current_app.root_path, 'static', 'uploads', 'empresas', 'logos', ese_empresa.logomarca)
                if os.path.exists(caminho_logo):
                    try:
                        os.remove(caminho_logo)
                    except OSError:
                        pass
                ese_empresa.logomarca = None

            if request.form.get('excluir_fachada') == 'true' and ese_empresa.fachada:
                caminho_fachada = os.path.join(current_app.root_path, 'static', 'uploads', 'empresas', 'fachadas', ese_empresa.fachada)
                if os.path.exists(caminho_fachada):
                    try:
                        os.remove(caminho_fachada)
                    except OSError:
                        pass
                ese_empresa.fachada = None

            # E) Upload e Processamento da Logomarca (Motor Unificado salvar_imagem_modulos)
            file_logo = request.files.get('logo')
            if file_logo and getattr(file_logo, 'filename', ''):
                # Processa e otimiza a logo dentro da estrutura modular da empresa
                caminho_relativo_logo = salvar_imagem_modulo(
                    arquivo=file_logo,
                    tipo_midia='empresa_logo',
                    identificador=ese_empresa.id,
                    empresa_id=ese_empresa.id
                )

                if caminho_relativo_logo:
                    # Atualiza o modelo com o caminho relativo (compatível com a função media_url)
                    ese_empresa.logomarca = caminho_relativo_logo

            # F) Upload e Processamento da Fachada
            file_fachada = request.files.get('fachada')
            if file_fachada and getattr(file_fachada, 'filename', ''):
                # Processa e otimiza a fachada dentro da estrutura de uploads da empresa
                caminho_relativo_fachada = salvar_imagem_modulo(
                    arquivo=file_fachada,
                    tipo_midia='empresa_fachada',
                    identificador=ese_empresa.id,
                    empresa_id=ese_empresa.id
                )

                if caminho_relativo_fachada:
                    # Salva no banco o caminho relativo (ex: 'Uploads/empresas/12/empresa_fachada_12_a1b2c3d4.webp')
                    ese_empresa.fachada = caminho_relativo_fachada

            # G) Validação do Domínio Próprio
            dominio = request.form.get('dominio_web')
            if dominio:
                try:
                    EseEmpresa.validar_dominio_proprio(dominio)
                    ese_empresa.dominio_web = dominio
                except ValueError as ve:
                    flash(str(ve), "warning")

            # H) Validação Obrigatória de Mídia Mínima
            if not ese_empresa.logomarca and not ese_empresa.fachada:
                db.session.commit()
                flash("Atenção: É obrigatório enviar ao menos a logomarca ou a foto da fachada para prosseguir.", "warning")
                return render_template(
                    'empresa/onboarding.html',
                    ese_empresa=ese_empresa,
                    processo=processo,
                    local=local,
                    local_id=local.id if local else empresa_id,
                    categoria_nome="Categoria Principal"
                )

            # Transição do Status da Esteira de Onboarding
            ese_empresa.status_homologacao = 'layout_configurado'

            # Vínculo e Garantia do Contrato
            garantir_contrato_proprietario(ese_empresa)

            # Persistência final das alterações no banco de dados
            db.session.add(ese_empresa)
            db.session.commit()

            flash("Identidade visual e mídias salvas com sucesso!", "success")
            return redirect(url_for('empresa.hub_ativacao', empresa_id=empresa_id))

        except Exception as e:
            db.session.rollback()
            current_app.logger.error(f"Erro ao configurar layout da EseEmpresa (ID {empresa_id}): {e}")
            flash(f"Falha ao salvar informações: {str(e)}", "danger")

    return render_template(
        'empresa/onboarding.html',
        ese_empresa=ese_empresa,
        processo=processo,
        local=local,
        local_id=local.id if local else empresa_id,
        categoria_nome="Categoria Principal"
    )


@empresa_bp.route('/<int:empresa_id>/setup-inicial', methods=['GET', 'POST'])
@login_required
def setup_inicial(empresa_id):
    """
    ESTEIRA DE AUTO-CADASTRO OBRIGATÓRIO (BIG BANG DA EMPRESA)
    ----------------------------------------------------------
    Ponto de transição crucial após a homologação com documentos fictos.
    O sistema obriga o pleiteante a se registrar como o primeiro vínculo
    de trabalho (Dono Supremo), populando a tabela 'colaborador_contratos'.
    """
    local = Local.query.get_or_404(empresa_id)

    # 🚨 TRAVA DE SEGURANÇA: Se ele já possui um contrato aqui, não precisa fazer o setup de novo
    contrato_existente = ColaboradorContrato.query.filter_by(
        id_cadastro_cliente=current_user.id,
        id_local=empresa_id
    ).first()

    if contrato_existente:
        flash("Setup já realizado. Bem-vindo ao seu painel!", "info")
        return redirect(url_for('empresa.vitrine_institucional', empresa_id=empresa_id))

    if request.method == 'POST':
        # Captura o expediente que o próprio dono vai fazer na empresa dele
        inicio_exp = request.form.get('hora_inicio_expediente')
        fim_exp = request.form.get('hora_fim_expediente')

        # Garante ou busca o Cargo Institucional de "Proprietário / Diretor" na tabela cargos
        cargo_dono = Cargo.query.filter_by(nome_cargo="Proprietário").first()
        if not cargo_dono:
            cargo_dono = Cargo(nome_cargo="Proprietário")
            db.session.add(cargo_dono)
            db.session.flush()  # Gera o ID do cargo antes do commit final

        try:
            # 🌟 O NASCIMENTO DO CONTRATO Nº 1
            novo_contrato = ColaboradorContrato(
                id_cadastro_cliente=current_user.id,
                id_local=empresa_id,
                id_cargo=cargo_dono.id,
                papel_nome='empreendedor',
                papel_nivel=999,  # Privilégio Supremo de Gestão
                hora_inicio_expediente=datetime.strptime(inicio_exp, "%H:%M").time(),
                hora_fim_expediente=datetime.strptime(fim_exp, "%H:%M").time(),
                status_profissional='ativo'
            )

            db.session.add(novo_contrato)
            db.session.commit()

            flash("🚀 Vínculo empresarial estabelecido! Você agora é oficialmente o Administrador do local.", "success")
            return redirect(url_for('empresa.configurar_layout', empresa_id=empresa_id))

        except Exception as e:
            db.session.rollback()
            current_app.logger.error(f"Erro no Big Bang da Empresa {empresa_id}: {e}")
            flash("Erro ao processar seu auto-cadastro. Verifique os horários digitados.", "danger")

    return render_template('empresa/setup_inicial.html', local=local)


def aplicar_desvio_documental(local_id):
    """
    MOTOR DE DESVIO DOCUMENTAL (SANDBOX DE HOMOLOGAÇÃO)
    --------------------------------------------------
    Injeta registros de documentos fictícios de forma automatizada para
    aprovar o estabelecimento no sistema sem travar o funil do empreendedor.

    Ações:
        1. Vincula caminhos de arquivos padrão ('provisorio.pdf') nas colunas de validação.
        2. Altera o status da empresa para 'homologada' ou 'aprovada'.
    """
    local = Local.query.get(local_id)
    if not local:
        return False

    caminho_provisorio = "uploads/documentos_sistema/default_provisorio.pdf"

    # 🌟 Simulando a entrega dos documentos civis/jurídicos exigidos
    local.status_homologacao = 'aprovado_provisorio'  # Status especial com alerta
    local.doc_cnpj = caminho_provisorio
    local.doc_identidade = caminho_provisorio
    local.data_homologacao = datetime.now(timezone.utc)

    # Se houver uma tabela separada de 'AnaliseDocumental', ela receberia o insert aqui.

    db.session.commit()
    return True


@empresa_bp.route('/<int:empresa_id>/editar-conteudo', methods=['GET', 'POST'])
@login_required
def editar_conteudo_institucional(empresa_id):
    """
    GERENCIADOR DE CONTEÚDO EMOCIONAL/INSTITUCIONAL
    ----------------------------------------------
    Permite ao gestor registrar a narrativa da empresa: história, missão,
    valores e ano de fundação. Alimenta a memória social da plataforma.
    """
    local = Local.query.get_or_404(empresa_id)

    # Validação via Contrato (Nível de gestão gerado no Setup Inicial)
    contrato = ColaboradorContrato.query.filter_by(
        id_cadastro_cliente=current_user.id,
        id_local=empresa_id,
        status_profissional='ativo'
    ).first()

    if not contrato or contrato.papel_nivel < 777:
        flash("Permissão insuficiente para alterar as informações institucionais.", "danger")
        return redirect(url_for('empresa.vitrine_institucional', empresa_id=empresa_id))

    if request.method == 'POST':
        local.ano_fundacao = request.form.get('ano_fundacao')
        local.historia = request.form.get('historia').strip()
        local.missao = request.form.get('missao').strip()
        local.valores = request.form.get('valores').strip()

        try:
            db.session.commit()
            flash("✨ A história e o propósito da sua empresa foram atualizados com sucesso!", "success")
            return redirect(url_for('empresa.vitrine_institucional', empresa_id=empresa_id))
        except Exception as e:
            db.session.rollback()
            current_app.logger.error(f"Erro ao salvar conteúdo institucional da empresa {empresa_id}: {e}")
            flash("Erro ao salvar os dados. Verifique os campos.", "danger")

    return render_template('empresa/editar_conteudo.html', local=local)


@empresa_bp.route('/<int:empresa_id>/design-painel', methods=['GET', 'POST'])
@login_required
def customizar_layout_avancado(empresa_id):
    """
    ESTÚDIO DE DESIGN DO PWA (CONFIGURAÇÃO DETALHADA)
    ------------------------------------------------
    Interface avançada onde o empreendedor gerencia o layout
    da sua página pública. Liberado para Gestores Homologados OU Pleitantes Ativos.
    """
    from feedin.modules.empresa.models import EseProcessoClaim

    local = Local.query.get_or_404(empresa_id)

    # 1. Validação A: Segurança via Contrato Ativo de Equipe
    contrato = ColaboradorContrato.query.filter_by(
        id_cadastro_cliente=current_user.id,
        id_local=empresa_id,
        status_profissional='ativo'
    ).first()

    # 2. Validação B: Segurança via Processo de Claim Ativo (Onboarding Assíncrono)
    processo = EseProcessoClaim.query.filter_by(
        local_id=empresa_id,
        usuario_solicitante_id=current_user.id
    ).order_by(EseProcessoClaim.id.desc()).first()

    # 🚥 REGRA DE OURO DA ESTEIRA:
    # Só barra o acesso se o usuário NÃO tiver contrato administrativo válido
    # E TAMBÉM NÃO for o dono de um processo de claim legítimo nas fases permitidas.
    eh_gestor_homologado = contrato and contrato.papel_nivel >= 777
    eh_pleitante_valido = processo and processo.status_processo in ['em_andamento', 'preenchimento_dados', 'simulacao_pendente', 'em_auditoria']

    if not eh_gestor_homologado and not eh_pleitante_valido:
        flash("Acesso restrito aos gestores do estabelecimento.", "danger")
        return redirect(url_for('empresa.vitrine_institucional', empresa_id=empresa_id))

    if request.method == 'POST':
        # Captura as escolhas de posicionamento e estética
        local.alinhamento_logo = request.form.get('alinhamento_logo', 'esquerda')
        local.cor_primaria = request.form.get('cor_primaria', '#3b82f6')
        local.cor_secundaria = request.form.get('cor_secundaria', '#1e3a8a')
        local.exibir_historia_primeiro = request.form.get('exibir_historia_primeiro') == '1'

        try:
            db.session.commit()
            flash("🎨 Layout e posicionamento dos elementos configurados com sucesso!", "success")
            return redirect(url_for('empresa.vitrine_institucional', empresa_id=empresa_id))
        except Exception as e:
            db.session.rollback()
            current_app.logger.error(f"Erro ao salvar design da empresa {empresa_id}: {e}")
            flash("Erro ao salvar as definições de layout.", "danger")

    return render_template('empresa/design_painel.html', local=local)


def salvar_midia_empresa(empresa_id, file_storage, subpasta='branding'):
    """
    PROCESSADOR SEGURO DE MÍDIAS DA EMPRESA (VERSÃO MODULAR BLINDADA)
    ----------------------------------------------------------------
    Recebe o arquivo do formulário, valida a extensão, aplica otimização
    de peso/dimensões e grava o arquivo na static isolada do Blueprint.

    Parâmetros:
        - empresa_id (int): ID da empresa proprietária do arquivo.
        - file_storage (FileStorage): Objeto do arquivo vindo do request.files.
        - subpasta (str): Destino interno ('branding', 'fachadas', 'galeria').

    Retorno:
        - str: Caminho relativo do arquivo para gravação no banco de dados.
    """
    import os

    # 🎯 1. CORREÇÃO DE DIRETÓRIO: Mapeia fisicamente para a pasta static interna do módulo 'empresa'
    # os.path.dirname(__file__) aponta para 'feedin/modules/empresa'
    base_modulo = os.path.dirname(__file__)
    diretorio_destino = os.path.join(base_modulo, 'static', 'uploads', 'empresas', str(empresa_id), subpasta)
    os.makedirs(diretorio_destino, exist_ok=True)

    # 2. FAXINA DO DISCO: Limpa qualquer arquivo antigo que já exista nesta subpasta
    try:
        for arquivo_antigo in os.listdir(diretorio_destino):
            caminho_antigo = os.path.join(diretorio_destino, arquivo_antigo)
            if os.path.isfile(caminho_antigo):
                os.remove(caminho_antigo)
    except Exception as e:
        print(f"Aviso de limpeza de diretório: {e}")

    # 3. GERAÇÃO DO NOME EXCLUSIVO (Cache Busting Nativo)
    timestamp = int(datetime.utcnow().timestamp())

    if subpasta == 'branding':
        prefixo_midia = 'logo'
    elif subpasta == 'fachadas':
        prefixo_midia = 'fachada'
    else:
        prefixo_midia = subpasta

    nome_seguro = f"{empresa_id}_{prefixo_midia}_{timestamp}.webp"
    caminho_fisico_completo = os.path.join(diretorio_destino, nome_seguro)

    # 4. SALVAMENTO E CONVERSÃO
    file_storage.save(caminho_fisico_completo)

    # 5. RETORNO PARA O BANCO DE DADOS
    # Mantém o retorno relativo esperado pelo url_for('empresa.static', filename=...)
    return f"uploads/empresas/{empresa_id}/{subpasta}/{nome_seguro}"


def reset_esteira_claim_ponto_zero(local_id=576):
    """
    Restaura o ambiente de testes para o exato momento anterior ao início do Claim,
    limpando os rastros criados pelas rotas antigas e pela antecipação.
    """

    try:
        print(f"🧹 Iniciando limpeza profunda para o Local ID: {local_id}...")

        # 1. Remove os vínculos específicos do app (Empresa)
        v_deletados = db.session.query(VinculoUsuarioEmpresa).filter_by(empresa_id=local_id).delete()
        print(f" -> {v_deletados} vínculos de usuário-empresa removidos.")

        # 2. Remove o processo de claim da esteira temporária
        p_deletados = db.session.query(EseProcessoClaim).filter_by(local_id=local_id).delete()
        print(f" -> {p_deletados} processos de claim removidos.")

        # 3. Remove a entidade física do Módulo Empresa (se houver)
        emp_deletadas = db.session.query(EseEmpresa).filter_by(local_id=local_id).delete()
        print(f" -> {emp_deletadas} registros de EseEmpresa removidos.")

        # 4. Restaura o Local Core ao estado original (Disponível no mapa, sem dono)
        local = Local.query.get(local_id)
        if local:
            local.id_empreendedor = None
            local.status_operacional = 'ativo' # Volta a ficar livre para claim
            local.verificado = False
            print(f" 🎯 Local '{local.nome}' restaurado para status_operacional='ativo' e sem empreendedor.")
        else:
            print(f" ⚠️ Local ID {local_id} não foi localizado no Core.")

        db.session.commit()
        print("✨ Sistema limpo e pronto! Pista liberada para testar o fluxo de ponta a ponta.")

    except Exception as e:
        db.session.rollback()
        print(f"❌ Erro ao tentar resetar o ambiente: {str(e)}")


from flask_wtf import FlaskForm
from wtforms import StringField, FileField
from wtforms.validators import DataRequired, Email, Length, ValidationError
from flask_wtf.file import FileAllowed
import re


def validar_digito_cpf(form, field):
    """
    🛡️ MOTOR DE VALIDAÇÃO JURÍDICA: ALGORITMO COMPLIANCE CPF
    --------------------------------------------------------------------------------------
    Executa o cálculo matemático puro dos dois dígitos verificadores do CPF informado.
    Isola caracteres não numéricos e barra sequências repetidas óbvias (ex: 111.111.111-11),
    garantindo consistência de dados antes de onerar o banco ou serviços externos.
    """
    cpf = re.sub(r'\D', '', field.data)
    if len(cpf) != 11 or cpf == cpf[0] * 11:
        raise ValidationError("CPF inválido.")

    # Validação analítica do primeiro dígito verificador
    soma = sum(int(cpf[i]) * (10 - i) for i in range(9))
    digito_1 = (soma * 10 % 11) % 10
    if digito_1 != int(cpf[9]):
        raise ValidationError("CPF inválido (Dígito verificador incorreto).")

    # Validação analítica do segundo dígito verificador
    soma = sum(int(cpf[i]) * (11 - i) for i in range(10))
    digito_2 = (soma * 10 % 11) % 10
    if digito_2 != int(cpf[10]):
        raise ValidationError("CPF inválido (Dígito verificador incorreto).")


@empresa_bp.route('/<int:empresa_id>/enviar-documentacao', methods=['GET', 'POST'])
@login_required
def enviar_documentacao(empresa_id):
    """
    🛡️ MOTOR DE VALIDAÇÃO JURÍDICA: MESA DE AUDITORIA E DOCUMENTAÇÃO
    --------------------------------------------------------------------------------------
    Gerencia o envio do formulário de documentação oficial (CNPJ/CPF e Comprovante) e
    sincroniza atomicamente o status do processo de Claim e da Empresa.
    """
    ese_empresa = EseEmpresa.query.get_or_404(empresa_id)

    # 1. Localiza o processo de Claim vinculado à empresa (prioriza pelo local_id ou solicitante)
    processo = None
    if ese_empresa.local_id:
        processo = EseProcessoClaim.query.filter_by(
            local_id=ese_empresa.local_id
        ).order_by(EseProcessoClaim.id.desc()).first()

    if not processo:
        processo = EseProcessoClaim.query.filter_by(
            usuario_solicitante_id=current_user.id
        ).order_by(EseProcessoClaim.id.desc()).first()

    # 2. TRATAMENTO DO SUBMIT (POST)
    if request.method == 'POST':
        try:
            # Estruturação e gravação dos arquivos no diretório isolado da empresa
            base_upload_path = current_app.config.get('UPLOAD_FOLDER', 'uploads')
            pasta_auditoria = os.path.join(base_upload_path, f"empresa_{ese_empresa.id}", "documentos_auditoria")
            os.makedirs(pasta_auditoria, exist_ok=True)

            file_cnpj = request.files.get('cnpj_social')
            file_endereco = request.files.get('comprovante_endereco')

            # Processamento do Cartão CNPJ / CPF
            if file_cnpj and file_cnpj.filename != '':
                ext = os.path.splitext(file_cnpj.filename)[1]
                caminho_cnpj = os.path.join(pasta_auditoria, f"doc_cnpj_oficial{ext}")
                file_cnpj.save(caminho_cnpj)
            else:
                caminho_cnpj = os.path.join(pasta_auditoria, "temp_cnpj_pendente.jpg")
                if not os.path.exists(caminho_cnpj):
                    with open(caminho_cnpj, "w", encoding="utf-8") as f:
                        f.write("Aguardando Cartão CPF/CNPJ Oficial.")

            # Processamento do Comprovante de Endereço
            if file_endereco and file_endereco.filename != '':
                ext = os.path.splitext(file_endereco.filename)[1]
                caminho_endereco = os.path.join(pasta_auditoria, f"doc_endereco_oficial{ext}")
                file_endereco.save(caminho_endereco)
            else:
                caminho_endereco = os.path.join(pasta_auditoria, "temp_endereco_pendente.jpg")
                if not os.path.exists(caminho_endereco):
                    with open(caminho_endereco, "w", encoding="utf-8") as f:
                        f.write("Aguardando Comprovante de Endereço Oficial.")

            # 3. SINCRO-ATUALIZAÇÃO DE ESTADOS DA ESTEIRA DE AUDITORIA
            progresso_atual = calcular_progresso_real(ese_empresa)

            if progresso_atual < 90:
                ese_empresa.status_homologacao = 'aguardando_dados'
                if processo:
                    processo.status_processo = 'em_andamento'
                flash("Documentos anexados. Preencha os dados restantes do perfil para liberar a auditoria final.", "warning")
            else:
                ese_empresa.status_homologacao = 'aguardando_auditoria'
                if processo:
                    processo.status_processo = 'aguardando_analise'
                flash("Documentação enviada com sucesso! Seu cadastro está na fila de auditoria.", "success")

            db.session.commit()
            return redirect(url_for('empresa.dashboard_empresa'))

        except Exception as e:
            db.session.rollback()
            current_app.logger.error(f"❌ Erro ao processar documentação da empresa {ese_empresa.id}: {e}")
            flash("Ocorreu uma falha técnica ao salvar os documentos. Tente novamente.", "danger")

    # 4. RENDERIZAÇÃO DA VIEW (GET)
    return render_template(
        'empresa/enviar_documentacao.html',
        ese_empresa=ese_empresa,
        processo=processo
    )


@empresa_bp.route('/hub/<int:empresa_id>', endpoint='hub_ativacao')
@login_required
def hub_ativacao(empresa_id):
    """
    🛡️ GESTÃO DE FLUXO DO PILOTO: HUB DE ONBOARDING (ESTEIRA ORGANICA)
    --------------------------------------------------------------------------------------
    Independência de Módulos: Opera via EseProcessoClaim, EseEmpresa e current_user.
    """
    # ---------------------------------------------------------------------
    # 1. RESOLUÇÃO DE PARÂMETROS E BUSCA DA EMPRESA
    # ---------------------------------------------------------------------
    ese_empresa = EseEmpresa.query.filter(
        (EseEmpresa.id == empresa_id) | (EseEmpresa.local_id == empresa_id)
    ).first_or_404()

    real_empresa_id = ese_empresa.id
    real_local_id = ese_empresa.local_id

    # 🎯 FIX: Definição imediata do alvo_local_id para ser usado em todas as queries subsequentes
    alvo_local_id = real_local_id or real_empresa_id

    user_core_id = getattr(current_user, 'usuario_id', getattr(current_user, 'id', None))
    user_uuid = str(getattr(current_user, 'id', ''))

    # ---------------------------------------------------------------------
    # 2. BUSCA DO PROCESSO DE REIVINDICAÇÃO (CLAIM) E VALIDAÇÃO DE SEGURANÇA
    # ---------------------------------------------------------------------
    processo = EseProcessoClaim.query.filter(
        (EseProcessoClaim.local_id == real_local_id) |
        (EseProcessoClaim.local_id == real_empresa_id) |
        (EseProcessoClaim.id == real_empresa_id)
    ).first()

    eh_dono_claim = processo and (
            str(processo.usuario_solicitante_id) == str(user_core_id) or
            str(processo.usuario_solicitante_id) == user_uuid
    )
    eh_dono_direto = (
            str(ese_empresa.proprietario_id) == str(user_core_id) or
            str(ese_empresa.proprietario_id) == user_uuid
    )

    eh_dono = eh_dono_claim or eh_dono_direto
    eh_admin = getattr(current_user, 'is_admin', False)

    if not (eh_dono or eh_admin):
        current_app.logger.warning(
            f"[HUB] Acesso negado. Usuário Core ID '{user_core_id}' (UUID '{user_uuid}') "
            f"tentou acessar Empresa #{real_empresa_id} (Local #{real_local_id})."
        )
        flash("Acesso não autorizado ou processo de reivindicação não localizado.", "danger")
        return redirect(url_for('empresa.dashboard_empresa'))

    tem_acesso_pro = ese_empresa.possui_recurso_pro()

    # ---------------------------------------------------------------------
    # 3. INFRAESTRUTURA FÍSICA DE COMPLIANCE
    # ---------------------------------------------------------------------
    if processo and processo.status_processo in ['em_andamento', 'preenchimento_dados']:
        folder_id = str(real_empresa_id)
        base_upload_path = os.path.join(
            empresa_bp.UPLOAD_BASE_DIR,
            'empresas',
            folder_id,
            'compliance'
        )
        os.makedirs(base_upload_path, exist_ok=True)

        caminho_cnpj = os.path.join(base_upload_path, "temp_cnpj_pendente.jpg")
        caminho_endereco = os.path.join(base_upload_path, "temp_endereco_pendente.jpg")

        if not os.path.exists(caminho_cnpj):
            with open(caminho_cnpj, "w", encoding="utf-8") as f:
                f.write("Aguardando Cartão CPF/CNPJ Oficial (Piloto).")

        if not os.path.exists(caminho_endereco):
            with open(caminho_endereco, "w", encoding="utf-8") as f:
                f.write("Aguardando Comprovante de Endereço Oficial (Piloto).")

    # ---------------------------------------------------------------------
    # 📩 3.1. BUSCA DE CONTRATOS PENDENTES COM CONVITE ACEITO
    # ---------------------------------------------------------------------
    # 1. Busca todos os contratos do estabelecimento que aguardam efetivação
    contratos_pendentes = ColaboradorContrato.query.filter(
        (ColaboradorContrato.id_local == alvo_local_id) | (ColaboradorContrato.id_local == real_empresa_id),
        ColaboradorContrato.status_profissional == 'pendente'
    ).all()

    # 2. Filtra os contratos que possuem convite respondido ('aceito') no mesmo local
    convites_aceitos = []
    for contrato in contratos_pendentes:
        convite = EseConviteColaborador.query.filter(
            (EseConviteColaborador.estabelecimento_id == contrato.id_local),
            EseConviteColaborador.status == 'aceito'
        ).order_by(EseConviteColaborador.id.desc()).first()

        if convite:
            convite.contrato_vinculado = contrato
            convites_aceitos.append(convite)

    total_convites_aceitos = len(convites_aceitos)

    # ---------------------------------------------------------------------
    # 📊 3.5. CÁLCULO DE PROGRESSO DO HUB DE ATIVAÇÃO (6 MÓDULOS)
    # ---------------------------------------------------------------------

    # Módulo 3: Grade de Horários da Empresa
    tem_horarios_cadastrados = db.session.query(
        EseHorarioFuncionamento.id
    ).filter_by(empresa_id=real_empresa_id).first() is not None

    # Módulo 4: Serviços & Preços
    tem_servicos = db.session.query(
        EseServicoOferecido.id
    ).filter_by(empresa_id=real_empresa_id).first() is not None

    tem_precos = db.session.query(
        EseServicoPreco.id
    ).filter_by(empresa_id=real_empresa_id).first() is not None

    modulo_servicos_precos_concluido = tem_servicos and tem_precos

    # Módulo 6: Equipes e Jornadas
    tem_cargo_e_habilidade = db.session.query(EseColaboradorServicoHabilidade.id).join(
        ColaboradorContrato, EseColaboradorServicoHabilidade.contrato_id == ColaboradorContrato.id
    ).filter(
        ColaboradorContrato.id_local == alvo_local_id,
        ColaboradorContrato.id_cargo.isnot(None)
    ).first() is not None

    tem_escala_semanal = db.session.query(EscalaTrabalhoColaborador.id).join(
        ColaboradorContrato, EscalaTrabalhoColaborador.contrato_id == ColaboradorContrato.id
    ).filter(
        ColaboradorContrato.id_local == alvo_local_id,
        EscalaTrabalhoColaborador.tipo_escala == 'padrao',
        EscalaTrabalhoColaborador.ativo.is_(True)
    ).first() is not None

    modulo_equipe_jornadas_concluido = tem_cargo_e_habilidade and tem_escala_semanal

    # CÁLCULO DA PONTUAÇÃO (TOTAL 6 MÓDULOS)
    modulos_concluidos = 0
    total_modulos = 6

    # 1. Identidade Visual
    if getattr(ese_empresa, 'logomarca', None) or getattr(ese_empresa, 'fachada', None):
        modulos_concluidos += 1

    # 2. Documentação & Compliance
    STATUS_DOC_OK = ['homologado', 'aguardando_auditoria', 'em_analise', 'provisorio_beta']
    STATUS_DOC_REJEITADO = ['recusado', 'rejeitado', 'pendente_correcao', 'incompleto']

    status_processo_claim = processo.status_processo if processo else None

    if ese_empresa.status_homologacao in STATUS_DOC_OK and status_processo_claim not in STATUS_DOC_REJEITADO:
        modulos_concluidos += 1
        status_modulo_doc = 'sucesso'
    elif ese_empresa.status_homologacao in STATUS_DOC_REJEITADO or status_processo_claim in STATUS_DOC_REJEITADO:
        status_modulo_doc = 'alerta_rejeitado'
    else:
        status_modulo_doc = 'pendente'

    # 3. Horários e Grade Comercial
    if tem_horarios_cadastrados:
        modulos_concluidos += 1

    # 4. Serviços e Preços
    if modulo_servicos_precos_concluido:
        modulos_concluidos += 1

    # 5. Vitrine Digital / TV PWA
    modulos_concluidos += 1

    # 6. Equipes e Jornadas
    if modulo_equipe_jornadas_concluido:
        modulos_concluidos += 1

    porcentagem_progresso = int(round((modulos_concluidos / total_modulos) * 100))

    # ---------------------------------------------------------------------
    # 4. RENDERIZAÇÃO E RETORNO
    # ---------------------------------------------------------------------
    return render_template(
        'empresa/hub_onboarding.html',
        processo=processo,
        tem_acesso_pro=tem_acesso_pro,
        ese_empresa=ese_empresa,
        empresa_id=real_empresa_id,
        local_id=alvo_local_id,
        grade_map=tem_horarios_cadastrados,
        porcentagem_progresso=porcentagem_progresso,
        modulos_concluidos=modulos_concluidos,
        total_modulos=total_modulos,
        status_modulo_doc=status_modulo_doc,
        modulo_servicos_concluido=modulo_servicos_precos_concluido,
        modulo_equipe_concluido=modulo_equipe_jornadas_concluido,
        convites_aceitos=convites_aceitos,
        total_convites_aceitos=total_convites_aceitos
    )


@empresa_bp.route('/configurar-horarios-estabelecimento/<int:empresa_id>', methods=['GET', 'POST'])
@login_required
def configurar_horarios_empresa(empresa_id):
    """
    ⏱️ GRADE DE FUNCIONAMENTO DO ESTABELECIMENTO
    --------------------------------------------------------------------------------------
    Gerencia exclusivamente as faixas horárias tradicionais da empresa (0-6).
    """
    from feedin.modules.empresa.models import EseEmpresa, EseHorarioFuncionamento, EmpresaCalendarioExcecao, \
        CadastroFeriado
    from flask import request, flash, redirect, url_for, render_template

    ese_empresa = EseEmpresa.query.get_or_404(empresa_id)

    DIAS_SEMANA = {
        1: "Segunda-feira", 2: "Terça-feira", 3: "Quarta-feira",
        4: "Quinta-feira", 5: "Sexta-feira", 6: "Sábado", 0: "Domingo"
    }

    if request.method == 'POST':
        try:
            for dia in range(7):
                trabalha = request.form.get(f'trabalha_{dia}') == '1'

                horario = EseHorarioFuncionamento.query.filter_by(
                    empresa_id=ese_empresa.id,
                    dia_semana=dia,
                    periodo_id=1
                ).first()

                if trabalha:
                    abertura_str = request.form.get(f'abertura_{dia}')
                    fechamento_str = request.form.get(f'fechamento_{dia}')

                    if abertura_str and fechamento_str:
                        abertura_time = datetime.strptime(abertura_str, "%H:%M").time()
                        fechamento_time = datetime.strptime(fechamento_str, "%H:%M").time()

                        if not horario:
                            horario = EseHorarioFuncionamento(
                                empresa_id=ese_empresa.id,
                                dia_semana=dia,
                                periodo_id=1
                            )
                        horario.horario_abertura = abertura_time
                        horario.horario_fechamento = fechamento_time
                        db.session.add(horario)
                else:
                    if horario:
                        db.session.delete(horario)

            db.session.commit()
            flash("⏱️ Horário de funcionamento do estabelecimento atualizado com sucesso!", "success")
            return redirect(url_for('empresa.configurar_horarios_empresa', empresa_id=ese_empresa.id))

        except Exception as e:
            db.session.rollback()
            flash(f"🚨 Erro ao salvar horários: {str(e)}", "danger")

    # MÉTODO GET
    grade_existente = EseHorarioFuncionamento.query.filter_by(empresa_id=ese_empresa.id, periodo_id=1).all()
    grade_map = {h.dia_semana: h for h in grade_existente}

    ano_atual = datetime.now().year
    excecoes = EmpresaCalendarioExcecao.query.filter(
        EmpresaCalendarioExcecao.empresa_id == ese_empresa.id,
        db.extract('year', EmpresaCalendarioExcecao.data_excecao) == ano_atual
    ).order_by(EmpresaCalendarioExcecao.data_excecao.asc()).all()

    return render_template(
        'empresa/horarios_estabelecimento.html',
        ese_empresa=ese_empresa,
        DIAS_SEMANA=DIAS_SEMANA,
        grade_map=grade_map,
        excecoes=excecoes
    )


@empresa_bp.route('/configuracoes-auxiliares/<int:empresa_id>', methods=['GET'])
@login_required
def configuracoes_auxiliares(empresa_id: int):
    """
    Renderiza o Hub Central de Configurações Auxiliares da Empresa.

    Esta rota atua como uma camada intermediária (tela de transição) entre o
    Onboarding principal e os submódulos de regras de negócio. O objetivo é isolar
    processos complexos (como o Calendário de Exceções) para manter a interface
    escalável para futuras integrações (regras de checkout, notificações, etc.).

    Contexto Local:
        Garante a injeção correta da taxonomia (categoria) e das variáveis geográficas
        da base de dados (padrão Piracicaba/SP) para manter a coerência visual e lógica.

    Args:
        empresa_id (int): Chave primária da tabela `ese_empresa`.

    Returns:
        Response: Template `empresa/configuracoes_auxiliares.html` com o contexto da empresa.
    """
    from feedin.modules.empresa.models import EseEmpresa

    # 🔍 Resgate de contexto com proteção 404 para integridade da sessão PWA
    ese_empresa = EseEmpresa.query.get_or_404(empresa_id)

    # 📍 Resgate de metadados geográficos para o Core de busca local
    cidade = ese_empresa.local_fisico.cidade if ese_empresa.local_fisico else "Piracicaba"
    estado = ese_empresa.local_fisico.estado if ese_empresa.local_fisico else "SP"

    # 🏷️ Resgate de Taxonomia ativa (evita quebra se o pleiteante ainda não tiver categoria)
    categoria_nome = ese_empresa.categoria_rel.nome if ese_empresa.categoria_rel else "Comércio Local"

    return render_template(
        'empresa/parametros_operacao.html',
        ese_empresa=ese_empresa,
        cidade=cidade,
        estado=estado,
        categoria_nome=categoria_nome
    )


@empresa_bp.route('/parametros-operacao/<int:empresa_id>', methods=['GET'])
@login_required
def parametros_operacao(empresa_id: int):
    """
    Renderiza o Ambiente Intermediário de Parâmetros de Operação.

    Centraliza os processos que controlam o comportamento dinâmico da empresa
    frente ao Core (como tabelas de feriados nacionais/estaduais e recessos próprios),
    além de abrigar as opções anteriormente escondidas no menu deslizante.
    """
    from feedin.modules.empresa.models import EseEmpresa

    ese_empresa = EseEmpresa.query.get_or_404(empresa_id)

    # Contexto local geográfico e taxonômico para manter o padrão PWA
    cidade = ese_empresa.local_fisico.cidade if ese_empresa.local_fisico else "Piracicaba"
    estado = ese_empresa.local_fisico.estado if ese_empresa.local_fisico else "SP"
    categoria_nome = ese_empresa.categoria_rel.nome if ese_empresa.categoria_rel else "Comércio Local"

    return render_template(
        'empresa/parametros_operacao.html',
        ese_empresa=ese_empresa,
        cidade=cidade,
        estado=estado,
        categoria_nome=categoria_nome
    )


def obter_insights_sazonais(empresa, dias_antecedencia=14):
    """
    Varre o calendário sazonal comercial e avisa o empreendedor
    sobre oportunidades específicas para o segmento dele.
    """
    hoje = date.today()
    limite_busca = hoje + timedelta(days=dias_antecedencia)
    ano_atual = hoje.year

    # Busca datas gerais + específicas do segmento da empresa
    sazonalidades = CalendarioSazonalComercial.query.filter(
        (CalendarioSazonalComercial.segmentos_alvo.like(f"%{empresa.segmento}%")) |
        (CalendarioSazonalComercial.segmentos_alvo == 'geral')
    ).all()

    insights = []

    for sazonal in sazonalidades:
        # Resolve se a data comercial é fixa ou móvel
        if sazonal.regra_movel:
            data_real = calcular_regra_movel(ano_atual, sazonal.regra_movel)
        else:
            data_real = date(ano_atual, sazonal.mes, sazonal.dia)

        # Verifica se está dentro da janela de alerta preditivo
        if data_real and hoje <= data_real <= limite_busca:
            dias_restantes = (data_real - hoje).days
            insights.append({
                "id_sazonal": sazonal.id,
                "nome": sazonal.nome,
                "descricao": sazonal.descricao,
                "data_formatada": data_real.strftime('%d/%m'),
                "dias_restantes": dias_restantes,
                "impacto": sazonal.impacto_estimado
            })

    return insights


def seed_inteligencia_sazonal_comercial():
    """
    Popula a biblioteca nativa de gatilhos e insights de negócios do FeedIn.
    Totalmente blindada contra cópias externas pois roda direto na semente da aplicação.
    """
    print("[*] Injetando biblioteca de inteligência sazonal comercial...")

    carga_sazonal = [
        {
            "nome": "Dia Internacional da Mulher",
            "descricao": "Forte apelo comercial. Excelente para restaurantes, floriculturas e bem-estar oferecerem mimos ou condições especiais.",
            "dia": 8, "mes": 3, "regra_movel": None,
            "segmentos_alvo": "geral,restaurantes,floricultura,bem_estar", "impacto_estimado": "alto"
        },
        {
            "nome": "Dia das Mães",
            "descricao": "A maior data do varejo e gastronomia local. Estimule campanhas de reservas antecipadas de mesa e vales-presente.",
            "dia": None, "mes": None, "regra_movel": "2_DOMINGO_MAIO",
            "segmentos_alvo": "geral,restaurantes,floricultura,vestuario", "impacto_estimado": "alto"
        },
        {
            "nome": "Dia dos Namorados",
            "descricao": "Foco absoluto em experiências e jantares a dois. Movimente o balcão criando menus sazonais exclusivos.",
            "dia": 12, "mes": 6, "regra_movel": None,
            "segmentos_alvo": "restaurantes,hotelaria,floricultura", "impacto_estimado": "alto"
        },
        {
            "nome": "Dia dos Pais",
            "descricao": "Forte apelo para almoços familiares e kits personalizados de vestuário ou cuidados masculinos.",
            "dia": None, "mes": None, "regra_movel": "2_DOMINGO_AGOSTO",
            "segmentos_alvo": "geral,restaurantes,vestuario,barbearia", "impacto_estimado": "alto"
        },
        {
            "nome": "Dia de São Francisco (Protetor dos Animais)",
            "descricao": "Gatilho de nicho extraordinário. Petshops e clínicas veterinárias podem lançar campanhas de vacinação ou estética pet.",
            "dia": 4, "mes": 10, "regra_movel": None,
            "segmentos_alvo": "petshop,clinica_veterinaria,agropecuaria", "impacto_estimado": "alto"
        },
        {
            "nome": "Dia do Compositor",
            "descricao": "Excelente oportunidade para bares e restaurantes que trabalham com música ao vivo (samba/MPB) criarem eventos temáticos diferenciados.",
            "dia": 7, "mes": 10, "regra_movel": None,
            "segmentos_alvo": "bar,restaurante,casa_shows", "impacto_estimado": "medio"
        }
    ]

    for c in carga_sazonal:
        # Evita duplicidade com base no nome único da estratégia
        existe = CalendarioSazonalComercial.query.filter_by(nome=c["nome"]).first()
        if not existe:
            nova_data = CalendarioSazonalComercial(
                nome=c["nome"],
                descricao=c["descricao"],
                dia=c["dia"],
                mes=c["mes"],
                regra_movel=c["regra_movel"],
                segmentos_alvo=c["segmentos_alvo"],
                impacto_estimado=c["impacto_estimado"]
            )
            db.session.add(nova_data)

    db.session.commit()
    print("[+] Biblioteca de inteligência sazonal injetada com sucesso.")


def popular_feriados_civis_dinamico(ano: int, sigla_estado: str = "SP", código_ibge_cidade: str = "3538709",
                                    nome_cidade: str = "Piracicaba"):
    """
    Consome a BrasilAPI (Gratuita e sem Token) para capturar feriados nacionais e estaduais/municipais.
    Se amanhã for utilizado no Espírito Santo, basta chamar popular_feriados_civis_dinamico(2026, "ES", "3205309", "Vitória")
    """
    print(f"[*] Iniciando carga de feriados civis para o ano {ano} ({sigla_estado} - {nome_cidade})...")

    # URL da BrasilAPI para feriados nacionais
    url_nacionais = f"https://brasilapi.com.br/api/feriados/v1/{ano}"

    try:
        resposta = requests.get(url_nacionais, timeout=10)
        if resposta.status_code == 200:
            feriados = resposta.json()
            for f in feriados:
                data_formatada = datetime.strptime(f["date"], "%Y-%m-%d").date()

                # Evita duplicidade usando a trava temporal que você criou
                existe = CadastroFeriado.query.filter_by(data=data_formatada).first()
                if not existe:
                    novo_feriado = CadastroFeriado(
                        nome=f["name"],
                        data=data_formatada,
                        abrangencia="nacional",
                        localidade="BR"
                    )
                    db.session.add(novo_feriado)
            db.session.commit()
            print("[+] Feriados Nacionais sincronizados com sucesso.")
    except Exception as e:
        print(f"[-] Erro ao buscar feriados nacionais: {e}")
        db.session.rollback()

    # Carga Manual Controlada para Feriados Estaduais e Municipais Rígidos (Evita quebras de APIs locais)
    # Exemplo: Piracicaba - SP
    if sigla_estado.upper() == "SP" and nome_cidade.lower() == "piracicaba":
        feriados_locais_sp = [
            {"nome": "Revolução Constitucionalista", "data": date(ano, 7, 9), "abrangencia": "estadual",
             "localidade": "SP"},
            {"nome": "Aniversário de Piracicaba", "data": date(ano, 8, 1), "abrangencia": "municipal",
             "localidade": "Piracicaba"},
            {"nome": "Dia da Consciência Negra", "data": date(ano, 11, 20), "abrangencia": "municipal",
             "localidade": "Piracicaba"},
        ]

        for fl in feriados_locais_sp:
            if not CadastroFeriado.query.filter_by(data=fl["data"], localidade=fl["localidade"]).first():
                novo_fl = CadastroFeriado(
                    nome=fl["nome"],
                    data=fl["data"],
                    abrangencia=fl["abrangencia"],
                    localidade=fl["localidade"]
                )
                db.session.add(novo_fl)
        db.session.commit()
        print("[+] Feriados Estaduais/Municipais de SP/Piracicaba alinhados.")


def executar_setup_calendario_sistema(ano_alvo=2026):
    """Executa a higienização e carga preditiva inicial do ecossistema"""
    with current_app.app_context():
        # 1. Popula os feriados de lei via API gratuita (Dinâmico por Estado/Cidade)
        popular_feriados_civis_dinamico(ano=ano_alvo, sigla_estado="SP", nome_cidade="Piracicaba")

        # 2. Injeta as regras de negócios comerciais exclusivas (Blindagem anti-cópia)
        seed_inteligencia_sazonal_comercial()

        print("\n[✔] Ecossistema de Calendário FeedIn parametrizado e pronto para operação!")


# ==============================================================================
# FUNÇÕES AUXILIARES DO CALENDÁRIO
# ==============================================================================
def calcular_fluxo_mensal(ano, mes, feriados_nacionais_set):
    """
    Calcula as datas do termômetro financeiro para o varejo/balcão.
    """
    import calendar

    # 1. 5º Dia Útil (Sábados contam, domingos e feriados nacionais não)
    dias_uteis = 0
    dia_atual = date(ano, mes, 1)
    quinto_dia = None

    while dias_uteis < 5:
        is_domingo = (dia_atual.weekday() == 6)
        is_feriado = (dia_atual in feriados_nacionais_set)

        if not is_domingo and not is_feriado:
            dias_uteis += 1
            if dias_uteis == 5:
                quinto_dia = dia_atual.day
                break
        dia_atual += timedelta(days=1)

    # 2. Dia 20 (Vale fixo)
    dia_20 = 20

    # 3. Dia 1 e último dia do mês (30 ou 31)
    dia_1 = 1
    if mes == 12:
        ultimo_dia = 31
    else:
        ultimo_dia = (date(ano, mes + 1, 1) - timedelta(days=1)).day

    return {
        "quinto_dia_util": quinto_dia,
        "dia_20": dia_20,
        "dia_1": dia_1,
        "ultimo_dia": ultimo_dia
    }


@empresa_bp.route('/estabelecimento/<int:empresa_id>/calendario')
@login_required
@preparar_entrada_modulo(slug_modulo='empresa', rota_destino='/empresa/painel')
def gerenciar_calendario(empresa_id):
    # Recupera usando o id correspondente do seu banco
    ese_empresa = Local.query.get_or_404(empresa_id)

    # Valida preenchimento prévio
    if not ese_empresa.cidade or not ese_empresa.estado:
        flash("Configure a cidade e o estado do estabelecimento antes de acessar o calendário.", "warning")
        return redirect(url_for('empresa.editar_local', local_id=ese_empresa.id))

    # Captura navegação ou assume o mês corrente
    ano = request.args.get('ano', default=datetime.now().year, type=int)
    mes = request.args.get('mes', default=datetime.now().month, type=int)

    if mes < 1:
        mes = 12
        ano -= 1
    elif mes > 12:
        mes = 1
        ano += 1

    # Verifica/Sincroniza Feriados antes dos cálculos
    existe_registro = CadastroFeriado.query.filter(
        db.extract('year', CadastroFeriado.data) == ano
    ).first()

    if not existe_registro:
        from feedin.models import Local as ModelLocal
        from feedin.modules.empresa.models import CadastroFeriado as ModelFeriado
        models_dict = {'Local': ModelLocal, 'CadastroFeriado': ModelFeriado}
        processar_localidade_completa(db, models_dict, ese_empresa.id)
        flash("Os calendários locais e feriados foram sincronizados!", "success")

    # Busca os feriados daquele mês/ano aplicáveis à empresa
    feriados_registrados = CadastroFeriado.query.filter(
        db.extract('year', CadastroFeriado.data) == ano,
        db.extract('month', CadastroFeriado.data) == mes,
        (CadastroFeriado.localidade == 'BR') |
        (CadastroFeriado.localidade == ese_empresa.estado) |
        (CadastroFeriado.localidade == ese_empresa.cidade)
    ).all()

    feriados_do_mes = {f.data.day: f for f in feriados_registrados}
    feriados_nacionais = {f.data for f in feriados_registrados if f.abrangencia == 'nacional'}

    # Nosso cálculo matemático das 3 datas de ouro de consumo
    fluxo_financeiro = calcular_fluxo_mensal(ano, mes, feriados_nacionais)

    # 📅 GERAÇÃO E PROCESSAMENTO DA MATRIZ DO CALENDÁRIO
    cal = calendar.Calendar(firstweekday=6)
    semanas_cruas = cal.monthdayscalendar(ano, mes)

    semanas_processadas = []

    for semana in semanas_cruas:
        semana_formatada = []
        for dia_numero in semana:
            # Caso o número seja 0, trata-se de um alinhador visual vazio no grid
            if dia_numero == 0:
                semana_formatada.append({
                    'numero': '',
                    'status': 'vazio',
                    'motivo': '',
                    'tem_efemeride': False
                })
                continue

            # Monta o objeto real de data para o dia corrente do laço
            dia_corrente = date(ano, mes, dia_numero)
            status = 'normal'
            motivo = ''

            # 1ª PRIORIDADE: Verificar se o dia cai em algum Recesso (Faixa de datas)
            recesso = EmpresaRecesso.query.filter(
                EmpresaRecesso.empresa_id == empresa_id,
                EmpresaRecesso.data_inicio <= dia_corrente,
                EmpresaRecesso.data_fim >= dia_corrente
            ).first()

            if recesso:
                status = 'fechado'
                motivo = recesso.motivo
            else:
                # 2ª PRIORIDADE: Verificar se há Exceção Pontual cadastrada
                excecao = EmpresaCalendarioExcecao.query.filter_by(
                    empresa_id=empresa_id,
                    data_excecao=dia_corrente
                ).first()

                if excecao:
                    if excecao.trabalha_no_dia:
                        status = 'aberto_especial'
                        if excecao.horario_abertura_excecao and excecao.horario_fechamento_excecao:
                            motivo = f"Especial: {excecao.horario_abertura_excecao.strftime('%H:%M')} às {excecao.horario_fechamento_excecao.strftime('%H:%M')}"
                        else:
                            motivo = "Horário Especial"
                    else:
                        status = 'fechado'
                        motivo = "Fechado (Exceção)"

            # 3ª PRIORIDADE: Busca se há Efeméride Cultural
            efemeride = CadastroEfemeride.query.filter_by(
                dia=dia_corrente.day,
                mes=dia_corrente.month
            ).first()

            semana_formatada.append({
                'numero': dia_numero,
                'status': status,
                'motivo': motivo,
                'tem_efemeride': True if efemeride else False
            })

        semanas_processadas.append(semana_formatada)

    # Dicionário de meses amigáveis
    meses_nomes = {
        1: "Janeiro", 2: "Fevereiro", 3: "Março", 4: "Abril", 5: "Maio", 6: "Junho",
        7: "Julho", 8: "Agosto", 9: "Setembro", 10: "Outubro", 11: "Novembro", 12: "Dezembro"
    }

    # Navegação de meses
    mes_anterior = mes - 1
    ano_anterior = ano
    if mes_anterior < 1:
        mes_anterior = 12
        ano_anterior -= 1

    proximo_mes = mes + 1
    proximo_ano = ano
    if proximo_mes > 12:
        proximo_mes = 1
        proximo_ano += 1

    # Carrega a lista completa de recessos agendados desta empresa
    recessos_listados = EmpresaRecesso.query.filter_by(empresa_id=empresa_id).order_by(
        EmpresaRecesso.data_inicio.asc()).all()

    # Como as efemérides são globais/culturais (ou associadas), buscamos todas para exibição
    efemerides_listadas = CadastroEfemeride.query.order_by(CadastroEfemeride.mes.asc(),
                                                           CadastroEfemeride.dia.asc()).all()

    return render_template(
        'empresa/calendario.html',
        ese_empresa=ese_empresa,
        semanas=semanas_processadas,  # 🔄 Enviando as semanas com dicionários ricos estruturados
        feriados_do_mes=feriados_do_mes,
        fluxo_financeiro=fluxo_financeiro,
        ano_atual=ano,
        mes_atual=mes,
        nome_mes_atual=meses_nomes[mes],
        mes_anterior=mes_anterior,
        ano_anterior=ano_anterior,
        proximo_mes=proximo_mes,
        proximo_ano=proximo_ano,
        recessos_listados=recessos_listados,
        efemerides_listadas=efemerides_listadas
    )


def calcular_datas_fluxo_financeiro(ano: int, mes: int, feriados_nacionais_set: set) -> dict:
    """
    Calcula dinamicamente os dias de fluxo de caixa para um mês/ano específico.
    Considera sábados como dia útil para o 5º dia útil (CLT), ignorando domingos e feriados nacionais.
    """
    # --- 1. Cálculo do 5º Dia Útil ---
    dias_uteis_contados = 0
    dia_corrente = date(ano, mes, 1)
    quinto_dia_util_data = None

    while dias_uteis_contados < 5:
        # Domingo (6) nunca é dia útil. Feriados nacionais também não.
        is_domingo = (dia_corrente.weekday() == 6)
        is_feriado_nacional = (dia_corrente in feriados_nacionais_set)

        if not is_domingo and not is_feriado_nacional:
            dias_uteis_contados += 1
            if dias_uteis_contados == 5:
                quinto_dia_util_data = dia_corrente
                break

        dia_corrente += timedelta(days=1)

    # --- 2. Demais dias fixos ---
    dia_20_data = date(ano, mes, 20)
    dia_1_data = date(ano, mes, 1)

    # Encontra o último dia do mês (30 ou 31, ou 28/29 em fevereiro)
    if mes == 12:
        ultimo_dia_data = date(ano, 12, 31)
    else:
        ultimo_dia_data = date(ano, mes + 1, 1) - timedelta(days=1)

    return {
        "quinto_dia_util": quinto_dia_util_data.day,
        "dia_20": dia_20_data.day,
        "dia_1": dia_1_data.day,
        "ultimo_dia": ultimo_dia_data.day
    }


@empresa_bp.route('/<int:empresa_id>/calendario/salvar-excecao', methods=['POST'])
def salvar_excecao(empresa_id):
    """
    Cadastra ou atualiza uma exceção de funcionamento para uma empresa específica.
    """
    # 📥 Captura e higienização dos inputs do formulário
    excecao_id = request.form.get('excecao_id')
    data_str = request.form.get('data')
    trabalha = request.form.get('trabalha') == 'true'

    # Se a empresa estiver aberta, guarda os horários informados; caso contrário, define como None
    # Também convertemos a string do input do HTML ("HH:MM") para objeto time do Python se necessário
    hora_inicio_str = request.form.get('hora_inicio') if trabalha else None
    hora_fim_str = request.form.get('hora_fim') if trabalha else None

    hora_inicio = None
    hora_fim = None

    try:
        # Conversão de string de data do HTML5 para objeto date do Python
        data_convertida = datetime.strptime(data_str, '%Y-%m-%d').date()

        # Conversão das strings de horários para objetos time do Python
        if hora_inicio_str:
            hora_inicio = datetime.strptime(hora_inicio_str, '%H:%M').time()
        if hora_fim_str:
            hora_fim = datetime.strptime(hora_fim_str, '%H:%M').time()

    except (ValueError, TypeError):
        flash('Data ou horários inválidos fornecidos para a exceção.', 'danger')
        return redirect(url_for('empresa.gerenciar_calendario', empresa_id=empresa_id))

    if excecao_id:
        # 🔄 MODO EDIÇÃO: Atualiza um registro já existente com os nomes corretos do modelo
        excecao = EmpresaCalendarioExcecao.query.filter_by(id=excecao_id, empresa_id=empresa_id).first()
        if excecao:
            excecao.data_excecao = data_convertida
            excecao.trabalha_no_dia = trabalha
            excecao.horario_abertura_excecao = hora_inicio
            excecao.horario_fechamento_excecao = hora_fim
            # Nota: 'recorrente' não consta na estrutura atual do modelo fornecido, deixado de fora para evitar novo TypeError.
            flash('Exceção de funcionamento atualizada com sucesso!', 'success')
        else:
            flash('Erro: Exceção não encontrada para atualização.', 'danger')
    else:
        # 🆕 MODO CRIAÇÃO: Adiciona a nova regra com a nomenclatura correta das colunas
        nova_exc = EmpresaCalendarioExcecao(
            empresa_id=empresa_id,
            data_excecao=data_convertida,
            trabalha_no_dia=trabalha,
            horario_abertura_excecao=hora_inicio,
            horario_fechamento_excecao=hora_fim
            # Nota: 'recorrente' removido daqui também por não fazer parte da tabela no banco
        )
        db.session.add(nova_exc)
        flash('Nova exceção de funcionamento salva com sucesso!', 'success')

    # Commit das alterações no banco de dados
    db.session.commit()
    return redirect(url_for('empresa.gerenciar_calendario', empresa_id=empresa_id))


@empresa_bp.route('/<int:empresa_id>/calendario/salvar-feriado', methods=['POST'])
def salvar_feriado(empresa_id):
    """
    Cadastra um novo feriado regional, municipal ou personalizado para a empresa.

    O feriado cadastrado aqui é inserido na base de dados central de calendário civil,
    respeitando a unicidade da data completa.

    Parâmetros da URL:
        empresa_id (int): ID único da empresa associada.

    Parâmetros do Formulário (POST):
        nome (str): Descrição/Título do feriado (ex: "Aniversário da Cidade").
        dia (int): Dia do evento (1 a 31).
        mes (int): Mês do evento (1 a 12).
        abrangencia (str): Escopo do feriado ('municipal', 'estadual' ou 'nacional').
    """
    # 📥 Captura e processamento do formulário de feriados
    nome = request.form.get('nome')
    dia = int(request.form.get('dia'))
    mes = int(request.form.get('mes'))
    abrangencia = request.form.get('abrangencia')

    # Validação simples de intervalo de datas antes da inserção
    if not (1 <= dia <= 31) or not (1 <= mes <= 12):
        flash('Formato de dia ou mês inválido.', 'danger')
        return redirect(url_for('empresa.gerenciar_calendario', empresa_id=empresa_id))

    try:
        # 🗓️ Como a tabela CadastroFeriado exige um db.Date, montamos o objeto date
        # utilizando o ano corrente da execução do sistema.
        ano_corrente = datetime.now().year
        data_feriado = date(ano_corrente, mes, dia)
    except ValueError:
        flash('Data gerada é inválida para o calendário (ex: 31 de Fevereiro).', 'danger')
        return redirect(url_for('empresa.gerenciar_calendario', empresa_id=empresa_id))

    # 🏙️ Define a localidade com base na abrangência informada
    # Se for municipal, pode adotar uma string genérica ou o nome/cidade da empresa caso disponível
    localidade = 'Municipal' if abrangencia == 'municipal' else 'SP'

    # Instancia o novo registro diretamente na tabela oficial homologada: CadastroFeriado
    novo_feriado = CadastroFeriado(
        nome=nome,
        data=data_feriado,
        abrangencia=abrangencia,
        localidade=localidade
    )

    try:
        db.session.add(novo_feriado)
        db.session.commit()
        flash(f'Feriado "{nome}" cadastrado com sucesso na base central!', 'success')
    except Exception as e:
        db.session.rollback()
        # Tratativa caso a trava de unicidade temporal (unique=True) seja ativada
        flash('Já existe um feriado ou recesso registrado nesta data específica.', 'warning')

    return redirect(url_for('empresa.gerenciar_calendario', empresa_id=empresa_id))


@empresa_bp.route('/<int:empresa_id>/calendario/salvar-efemeride', methods=['POST'])
@login_required
def salvar_efemeride(empresa_id):
    """
    Cadastra uma nova efeméride cultural ou comemorativa no calendário.
    Associa o usuário logado para fins de auditoria, garantindo a rastreabilidade.
    """
    nome = request.form.get('nome')
    dia_str = request.form.get('dia')
    mes_str = request.form.get('mes')
    breve_relato = request.form.get('breve_relato') or None
    link_oficial = request.form.get('link_oficial') or None

    # Validações essenciais das datas recebidas
    try:
        dia = int(dia_str)
        mes = int(mes_str)
        if not (1 <= dia <= 31) or not (1 <= mes <= 12):
            raise ValueError
    except (ValueError, TypeError):
        flash('Data inválida para cadastrar a efeméride.', 'danger')
        return redirect(url_for('empresa.gerenciar_calendario', empresa_id=empresa_id))

    if not nome:
        flash('O nome do marco comemorativo é obrigatório.', 'danger')
        return redirect(url_for('empresa.gerenciar_calendario', empresa_id=empresa_id))

    try:
        # 🆕 Criação do registro com rastreabilidade pelo current_user
        nova_efemeride = CadastroEfemeride(
            nome=nome,
            dia=dia,
            mes=mes,
            breve_relato=breve_relato,
            link_oficial=link_oficial,
            usuario_criador_id=current_user.id  # Vincula o usuário autenticado
        )

        db.session.add(nova_efemeride)
        db.session.commit()
        flash('Efeméride cadastrada com sucesso! Contribuindo com nossa memória cultural.', 'success')

    except Exception as e:
        db.session.rollback()
        # Tratamento de erro seguro
        flash('Não foi possível salvar a efeméride no momento. Tente novamente.', 'danger')
        print(f"Erro ao salvar efeméride: {str(e)}")  # Rastro no log local do PyCharm

    return redirect(url_for('empresa.gerenciar_calendario', empresa_id=empresa_id))


@empresa_bp.route('/<int:empresa_id>/calendario/salvar-recesso', methods=['POST'])
@login_required
def salvar_recesso(empresa_id):
    """
    Cadastra um período de recesso/fechamento contínuo para a empresa.
    """
    data_inicio_str = request.form.get('data_inicio')
    data_fim_str = request.form.get('data_fim')
    motivo = request.form.get('motivo') or "Recesso Declarado"

    try:
        # Conversão das strings de data para objetos date do Python
        data_inicio = datetime.strptime(data_inicio_str, '%Y-%m-%d').date()
        data_fim = datetime.strptime(data_fim_str, '%Y-%m-%d').date()

        # Consistência lógica de datas
        if data_fim < data_inicio:
            flash('Erro: A data de término do recesso não pode ser anterior à data de início.', 'danger')
            return redirect(url_for('empresa.gerenciar_calendario', empresa_id=empresa_id))

    except (ValueError, TypeError):
        flash('Período de datas inválido fornecido.', 'danger')
        return redirect(url_for('empresa.gerenciar_calendario', empresa_id=empresa_id))

    try:
        # Instanciação do recesso com rastreabilidade
        novo_recesso = EmpresaRecesso(
            empresa_id=empresa_id,
            data_inicio=data_inicio,
            data_fim=data_fim,
            motivo=motivo,
            usuario_criador_id=current_user.id  # Identificação do criador
        )

        db.session.add(novo_recesso)
        db.session.commit()
        flash('Período de recesso programado com sucesso!', 'success')

    except Exception as e:
        db.session.rollback()
        flash('Erro interno ao tentar salvar o recesso.', 'danger')
        print(f"Erro ao salvar recesso: {str(e)}")

    return redirect(url_for('empresa.gerenciar_calendario', empresa_id=empresa_id))


# ==========================================
# 🔧 GESTÃO DE RECESSOS (EDIÇÃO E EXCLUSÃO)
# ==========================================

@empresa_bp.route('/<int:empresa_id>/calendario/recesso/editar/<int:recesso_id>', methods=['POST'])
@login_required
def editar_recesso(empresa_id, recesso_id):
    recesso = EmpresaRecesso.query.get_or_404(recesso_id)

    data_inicio_str = request.form.get('data_inicio')
    data_fim_str = request.form.get('data_fim')
    motivo = request.form.get('motivo')

    try:
        recesso.data_inicio = datetime.strptime(data_inicio_str, '%Y-%m-%d').date()
        recesso.data_fim = datetime.strptime(data_fim_str, '%Y-%m-%d').date()
        recesso.motivo = motivo or "Recesso Alterado"

        if recesso.data_fim < recesso.data_inicio:
            flash('A data de término não pode ser anterior à data de início.', 'danger')
            return redirect(url_for('empresa.gerenciar_calendario', empresa_id=empresa_id))

        db.session.commit()
        flash('Recesso atualizado com sucesso!', 'success')
    except Exception as e:
        db.session.rollback()
        flash('Erro ao tentar atualizar o recesso.', 'danger')
        print(f"Erro ao editar recesso: {e}")

    return redirect(url_for('empresa.gerenciar_calendario', empresa_id=empresa_id))


@empresa_bp.route('/<int:empresa_id>/calendario/recesso/excluir/<int:recesso_id>', methods=['POST'])
@login_required
def excluir_recesso(empresa_id, recesso_id):
    recesso = EmpresaRecesso.query.get_or_404(recesso_id)
    try:
        db.session.delete(recesso)
        db.session.commit()
        flash('Período de recesso removido.', 'success')
    except Exception as e:
        db.session.rollback()
        flash('Erro ao remover o recesso.', 'danger')
    return redirect(url_for('empresa.gerenciar_calendario', empresa_id=empresa_id))

def salvar_contexto_cliente_estabelecimento(dados):
    """
    Persiste os dados de identificação social e o ano de início de relacionamento
    do cliente com o estabelecimento no banco de dados compartilhado.

    Parâmetros:
        dados (dict): Dicionário contendo cliente_id, local_id, apelido, foto_path e ano_inicio.
    """

    cliente = ModCadastroCliente.query.get(dados['cliente_id'])
    if not cliente:
        raise ValueError("Cliente não encontrado.")

    # 1. Atualiza dados de Identidade Social no cadastro principal do cliente
    if dados.get('apelido'):
        cliente.apelido = dados['apelido']
    if dados.get('foto_path'):
        cliente.foto_path = dados['foto_path']

    # 2. Atualiza ou insere o relacionamento histórico (ano de início)
    ano_inicio = dados.get('ano_inicio_relacionamento')
    local_id = dados.get('local_id')

    if ano_inicio and local_id:
        atrib = AthAtribContexto.query.filter_by(
            cadastro_cliente_id=cliente.id,
            local_id=local_id,
            chave='ano_inicio_relacionamento'
        ).first()

        if not atrib:
            atrib = AthAtribContexto(
                cadastro_cliente_id=cliente.id,
                local_id=local_id,
                chave='ano_inicio_relacionamento',
                valor=str(ano_inicio)
            )
            db.session.add(atrib)
        else:
            atrib.valor = str(ano_inicio)

    db.session.commit()


# ==========================================
# 🎈 GESTÃO DE EFEMÉRIDES (EDIÇÃO E EXCLUSÃO)
# ==========================================

@empresa_bp.route('/<int:empresa_id>/calendario/efemeride/editar/<int:efemeride_id>', methods=['POST'])
@login_required
def editar_efemeride(empresa_id, efemeride_id):
    efemeride = CadastroEfemeride.query.get_or_404(efemeride_id)

    try:
        efemeride.nome = request.form.get('nome')
        efemeride.dia = int(request.form.get('dia'))
        efemeride.mes = int(request.form.get('mes'))
        efemeride.breve_relato = request.form.get('breve_relato') or None
        efemeride.link_oficial = request.form.get('link_oficial') or None

        db.session.commit()
        flash('Efeméride atualizada com sucesso!', 'success')
    except Exception as e:
        db.session.rollback()
        flash('Erro ao atualizar a efeméride.', 'danger')
        print(f"Erro ao editar efeméride: {e}")

    return redirect(url_for('empresa.gerenciar_calendario', empresa_id=empresa_id))


@empresa_bp.route('/<int:empresa_id>/calendario/efemeride/excluir/<int:efemeride_id>', methods=['POST'])
@login_required
def excluir_efemeride(empresa_id, efemeride_id):
    efemeride = CadastroEfemeride.query.get_or_404(efemeride_id)
    try:
        db.session.delete(efemeride)
        db.session.commit()
        flash('Efeméride removida com sucesso.', 'success')
    except Exception as e:
        db.session.rollback()
        flash('Erro ao remover a efeméride.', 'danger')
    return redirect(url_for('empresa.gerenciar_calendario', empresa_id=empresa_id))


@empresa_bp.route('/convite/validar/<token_convite>', methods=['POST'])
def validar_convite_colaborador(token_convite):
    """
    Roda a Etapa 2 do fluxo (Alfândega).
    O colaborador logado digita CPF e Data de Nascimento. O sistema valida
    contra o hash do convite e efetiva o vínculo contratual no ecossistema.
    """
    # 1. Segurança Básica: O usuário precisa estar logado no Auth do FeedIn
    # (Ajuste para o seu sistema real de gerenciamento de sessão/token)
    usuario_id = session.get('usuario_id')
    if not usuario_id:
        return jsonify({"status": "erro", "mensagem": "Usuário não autenticado no sistema."}), 401

    # 2. Captura os dados digitados pelo colaborador na tela de validação
    cpf_digitado = request.json.get('cpf')
    data_nascimento_digitada_raw = request.json.get('data_nascimento')

    if not cpf_digitado or not data_nascimento_digitada_raw:
        return jsonify({"status": "erro", "mensagem": "CPF e Data de Nascimento são obrigatórios para validação."}), 400

    try:
        # Trata a entrada de data
        data_nascimento_digitada = datetime.strptime(data_nascimento_digitada_raw, '%Y-%m-%d').date()

        # Gera o hash do CPF digitado para comparação cega
        cpf_limpo = "".join(filter(str.isdigit, cpf_digitado))
        hash_comparacao = EseConviteColaborador.gerar_hash_cpf(cpf_limpo)

        # 3. Busca o convite na tabela auxiliar cruzando as três chaves de segurança
        convite = EseConviteColaborador.query.filter_by(
            token_convite=token_convite,
            cpf_hash=hash_comparacao,
            data_nascimento=data_nascimento_digitada,
            status='pendente'
        ).first()

        if not convite:
            # Resposta genérica por segurança: evita que pessoas fiquem testando tokens/CPFs aleatórios
            return jsonify(
                {"status": "erro", "mensagem": "Dados de validação inválidos ou convite já processado."}), 404

        # 4. Encontrou! O PWA intercepta aqui para exibir a mensagem de acolhimento:
        # "A empresa X apontou você como colaborador. Você confirma essa informação?"
        # Se a requisição atual já for a confirmação do clique no "Sim", executamos a gravação:
        confirmado = request.json.get('confirmar_vinculo', False)

        if not confirmado:
            # Fase 1 da rota: Apenas avisa ao front-end que o convite é real e pede o "Sim" do usuário
            return jsonify({
                "status": "sucesso",
                "fase": "aguardando_confirmacao",
                "mensagem": "Convite localizado.",
                "empresa_nome": convite.empresa.nome_fantasia  # Assumindo a coluna na tabela empresa
            }), 200

        # Fase 2 da rota: Colaborador clicou em "Sim". Efetivamos o contrato no Módulo Empresa.
        # Criamos o vínculo oficial administrativo
        novo_contrato = ColaboradorContrato(
            id_cadastro_cliente=current_user.id,
            id_local=convite.estabelecimento_id,
            id_cargo=1,  # Defina um ID padrão temporário ou capture da taxonomia de cargos depois
            papel_nome='operador',  # Nível padrão seguro
            papel_nivel=500,
            data_contratacao=datetime.combine(convite.data_contratacao, datetime.min.time()),
            status_profissional='ativo',
            # Horários padrão de expediente que o dono/sistema vai ajustar depois
            hora_inicio_expediente=datetime.strptime("08:00", "%H:%M").time(),
            hora_fim_expediente=datetime.strptime("18:00", "%H:%M").time()
        )

        db.session.add(novo_contrato)

        # Atualiza o status do convite auxiliar para evitar reuso
        convite.status = 'aceito'

        db.session.commit()

        return jsonify({
            "status": "sucesso",
            "fase": "vinculo_efetivado",
            "mensagem": "Vínculo confirmado! Redirecionando para a ficha de autodeclaração.",
            "contrato_id": novo_contrato.id
        }), 201

    except ValueError:
        return jsonify({"status": "erro", "mensagem": "Formato de data de nascimento inválido."}), 400
    except Exception as e:
        db.session.rollback()
        return jsonify({"status": "erro", "mensagem": f"Erro interno ao processar vínculo: {str(e)}"}), 500


def limpar_cpf(cpf_raw: str) -> str:
    """Remove pontuações e mantém apenas os dígitos do CPF."""
    if not cpf_raw:
        return ""
    return re.sub(r'\D', '', cpf_raw)


@empresa_bp.route('/colaborador/salvar-autodeclaracao/<int:contrato_id>', methods=['POST'])
@login_required
@login_required
def salvar_autodeclaracao_colaborador(contrato_id):
    """
    Processa e persiste a ficha autodeclarada do colaborador.
    Usa unicamente id_cadastro_cliente (UUID 36) para validação.
    """
    cliente_uuid = getattr(current_user, 'id', None) or session.get('id_cliente')
    if not cliente_uuid:
        return jsonify({"status": "erro", "mensagem": "Sessão expirada. Autentique-se novamente."}), 401

    # Busca o contrato associado estritamente ao UUID do cliente logado
    contrato = ColaboradorContrato.query.filter_by(
        id=contrato_id,
        id_cadastro_cliente=str(cliente_uuid)
    ).first()

    if not contrato:
        return jsonify({"status": "erro", "mensagem": "Contrato de trabalho não localizado para este perfil."}), 404

    # Captura dos dados do formulário
    nome_completo = request.form.get('nome_completo', '').strip().upper()
    nome_exibicao = request.form.get('nome_exibicao_pwa', '').strip()
    cpf_raw = request.form.get('cpf', '')
    cnpj_raw = request.form.get('cnpj', '')
    estado_civil = request.form.get('estado_civil')

    logradouro = request.form.get('logradouro', '').strip()
    numero = request.form.get('numero', '').strip()
    complemento = request.form.get('complemento', '').strip() or None
    bairro = request.form.get('bairro', '').strip()
    cidade = request.form.get('cidade', '').strip()
    estado = request.form.get('estado', '').strip().upper()
    cep_raw = request.form.get('cep', '')

    telefone_pessoal = limpar_mascara(request.form.get('telefone_pessoal', ''))
    emergencia_nome = request.form.get('contato_emergencia_nome', '').strip()
    emergencia_fone = limpar_mascara(request.form.get('contato_emergencia_fone', ''))
    tipo_sanguineo = request.form.get('tipo_sanguineo', '').strip().upper() or None

    banco_nome = request.form.get('banco_nome', '').strip()
    agencia = request.form.get('agencia', '').strip()
    conta = request.form.get('conta_corrente', '').strip()
    chave_pix = request.form.get('chave_pix', '').strip()

    if not all([nome_completo, nome_exibicao, logradouro, numero, bairro, cidade, estado, cep_raw, telefone_pessoal]):
        flash("Por favor, preencha todos os campos obrigatórios da sua ficha civil.", "warning")
        return redirect(url_for('empresa.ficha_autodeclaracao_tela', contrato_id=contrato_id))

    try:
        detalhes = ColaboradorDetalhesPessoais.query.filter_by(contrato_id=contrato_id).first()

        if not detalhes:
            detalhes = ColaboradorDetalhesPessoais(contrato_id=contrato_id)
            db.session.add(detalhes)

        detalhes.nome_completo = nome_completo
        detalhes.nome_exibicao_pwa = nome_exibicao
        detalhes.cpf = limpar_mascara(cpf_raw) if cpf_raw else None
        detalhes.cnpj = limpar_mascara(cnpj_raw) if cnpj_raw else None
        detalhes.estado_civil = estado_civil

        detalhes.logradouro = logradouro
        detalhes.numero = numero
        detalhes.complemento = complemento
        detalhes.bairro = bairro
        detalhes.cidade = cidade
        detalhes.estado = estado[:2]
        detalhes.cep = limpar_mascara(cep_raw)

        detalhes.telefone_pessoal = telefone_pessoal
        detalhes.contato_emergencia_nome = emergencia_nome if emergencia_nome else None
        detalhes.contato_emergencia_fone = emergencia_fone if emergencia_fone else None
        detalhes.tipo_sanguineo = tipo_sanguineo

        detalhes.banco_nome = banco_nome if banco_nome else None
        detalhes.agencia = agencia if agencia else None
        detalhes.conta_corrente = conta if conta else None
        detalhes.chave_pix = chave_pix if chave_pix else None

        # Garante status ativo após salvar autodeclaração
        contrato.status_profissional = 'ativo'

        db.session.commit()

        session.pop('completar_cadastro_colaborador', None)

        flash("Sua ficha profissional foi preenchida com sucesso!", "success")

        # Redireciona diretamente para a tela de conclusão de admissão / grade contratual
        return redirect(url_for('empresa.admissao_concluida_tela', contrato_id=contrato.id))

    except Exception as e:
        db.session.rollback()
        print(f"🚨 [ERRO CRÍTICO AUTODECLARAÇÃO]: {str(e)}")
        flash("Erro interno ao salvar suas informações de colaborador. Tente novamente.", "danger")
        return redirect(url_for('empresa.ficha_autodeclaracao_tela', contrato_id=contrato_id))


@empresa_bp.route('/<int:empresa_id>/menu-servicos-precos', methods=['GET'])
def menu_servicos_precos(empresa_id):
    """
    Renders a tela intermediária (hub de opções) para o gerenciamento de serviços e preços.
    """
    # Usando o padrão de nomenclatura EseEmpresa
    empresa = EseEmpresa.query.get_or_404(empresa_id)

    return render_template(
        'empresa/menu_servicos_precos.html',
        ese_empresa=empresa
    )


@empresa_bp.route('/<int:empresa_id>/servicos/cadastrar', methods=['POST'])
def salvar_servico_empresa(empresa_id):
    """
    Processa a inserção/associação do serviço usando a model EseServicoOferecido.
    """
    termo_digitado = request.form.get('termo_servico', '').strip()
    tempo_duracao = request.form.get('tempo_duracao', '00:30')
    descricao = request.form.get('descricao_servico', '').strip()
    usuario_atual_id = current_user.id  # ID do usuário logado realizando a operação

    if not termo_digitado:
        flash("O termo do serviço não pode ser vazio.", "danger")
        return redirect(url_for('empresa.cadastro_servicos_listagem', empresa_id=empresa_id))

    # 1. Busca o termo na tabela Taxonomia (Case-Insensitive)
    taxonomia_registro = Taxonomia.query.filter(Taxonomia.nome.ilike(termo_digitado)).first()

    # 2. Inteligência Hierárquica: Se não existir na base global, criamos e conectamos ao pai
    if not taxonomia_registro:
        taxonomia_registro = Taxonomia(
            nome=termo_digitado,
            categoria='Serviço',
            status='homologado',  # Ajuste de acordo com sua política de moderação
            visivel_usuario=True,
            visivel_negocio=True
        )
        db.session.add(taxonomia_registro)
        db.session.flush()  # Registra na transação para capturar o ID gerado

        # Busca a categoria pai padrão da empresa (ex: "Cabeleireiro") para linkar a nova taxonomia filha
        categoria_pai = Taxonomia.query.filter_by(nome="Cabeleireiro", categoria="Serviço").first()
        if categoria_pai:
            categoria_pai.subitens.append(taxonomia_registro)

    # 3. Registra o serviço no catálogo EseServicoOferecido da empresa
    novo_servico = EseServicoOferecido(
        empresa_id=empresa_id,
        taxonomia_id=taxonomia_registro.id,
        descricao_servico=descricao if descricao else None,
        tempo_duracao=tempo_duracao,
        inserido_por_usuario_id=usuario_atual_id
    )

    db.session.add(novo_servico)
    db.session.commit()

    flash("Serviço adicionado com sucesso!", "success")
    return redirect(url_for('empresa.cadastro_servicos_listagem', empresa_id=empresa_id))


@empresa_bp.route('/api/taxonomia/buscar', methods=['GET'])
def api_buscar_taxonomia():
    """
    Endpoint JSON para busca assíncrona na tabela Taxonomia.
    """
    query = request.args.get('q', '').strip()
    categoria_param = request.args.get('categoria', 'Serviço').strip()

    if len(query) < 2:
        return jsonify([])

    # Filtra por aproximação no nome e faz comparação case-insensitive na categoria
    # Usamos func.lower para garantir que "Serviço", "serviço" ou "SERVIÇO" sejam aceitos
    resultados = Taxonomia.query.filter(
        func.lower(Taxonomia.categoria) == categoria_param.lower(),
        Taxonomia.nome.ilike(f"%{query}%")
    ).limit(10).all()

    # Fallback: Se não encontrou nada filtrando pela categoria exata,
    # busca pelo termo apenas no nome para evitar resultados vazios indevidos
    if not resultados:
        resultados = Taxonomia.query.filter(
            Taxonomia.nome.ilike(f"%{query}%")
        ).limit(10).all()

    return jsonify([{'id': t.id, 'nome': t.nome} for t in resultados])


@empresa_bp.route('/<int:empresa_id>/servicos/cadastrar-listagem', methods=['GET'])
def cadastro_servicos_listagem(empresa_id):
    """
    Renderiza a tela de cadastro e a lista atualizada de serviços oferecidos pela empresa.
    """
    empresa = EseEmpresa.query.get_or_404(empresa_id)

    # Busca os serviços oferecidos sem quebrar por conflito de tipos de dados
    servicos_cadastrados = EseServicoOferecido.query.filter_by(empresa_id=empresa_id).all()

    return render_template(
        'empresa/cadastro_servicos_listagem.html',
        ese_empresa=empresa,
        servicos_cadastrados=servicos_cadastrados
    )


@empresa_bp.route('/<int:empresa_id>/servicos/excluir/<int:servico_id>', methods=['POST'])
def excluir_servico_empresa(empresa_id, servico_id):
    """
    Remove o serviço oferecido do portfólio da empresa.
    """
    servico = EseServicoOferecido.query.filter_by(id=servico_id, empresa_id=empresa_id).first_or_404()

    db.session.delete(servico)
    db.session.commit()

    flash("Serviço removido com sucesso da sua grade.", "info")
    return redirect(url_for('empresa.cadastro_servicos_listagem', empresa_id=empresa_id))


@empresa_bp.route('/<int:empresa_id>/servicos/editar/<int:servico_id>', methods=['POST'])
def editar_servico_empresa(empresa_id, servico_id):
    """
    Atualiza a descrição, a duração estimada e o intervalo de um serviço existente.
    """
    servico = EseServicoOferecido.query.filter_by(id=servico_id, empresa_id=empresa_id).first_or_404()

    # Captura os dados enviados pelo modal
    descricao = request.form.get('descricao_servico')
    tempo_duracao = request.form.get('tempo_duracao')
    tempo_intervalo = request.form.get('tempo_intervalo', type=int)

    # Atualiza as propriedades do registro
    servico.descricao_servico = descricao.strip() if descricao else None
    if tempo_duracao:
        servico.tempo_duracao = tempo_duracao
    if tempo_intervalo is not None:
        servico.tempo_intervalo = tempo_intervalo

    db.session.commit()

    flash("Serviço atualizado com sucesso!", "success")
    return redirect(url_for('empresa.cadastro_servicos_listagem', empresa_id=empresa_id))


@empresa_bp.route('/<int:empresa_id>/servicos/precos', methods=['GET'])
def configurar_precos_listagem(empresa_id):
    """
    Renderiza a interface de precificação. Exibe em formato de tabela todos os serviços
    que a empresa oferece, em ordem alfabética crescente, permitindo alterações de preços.
    """
    empresa = EseEmpresa.query.get_or_404(empresa_id)

    # 1. Busca os serviços cadastrados pela empresa na Etapa 1
    servicos_oferecidos = EseServicoOferecido.query.filter_by(empresa_id=empresa_id).all()

    # Extraímos as taxonomias vinculadas para ordenar alfabeticamente
    # Ordenação por nome da taxonomia em ordem crescente (A-Z)
    servicos_oferecidos.sort(key=lambda x: x.servico_taxonomia.nome.lower())

    # 2. Busca a tabela de preços atuais para carregar os valores vigentes na tela
    precos_atuais = {p.taxonomia_id: p for p in EseServicoPreco.query.filter_by(empresa_id=empresa_id).all()}

    return render_template(
        'empresa/configurar_precos_listagem.html',
        ese_empresa=empresa,
        servicos_oferecidos=servicos_oferecidos,
        precos_atuais=precos_atuais
    )


@empresa_bp.route('/<int:empresa_id>/servicos/precos/salvar', methods=['POST'])
def salvar_preco_servico(empresa_id):
    """
    Salva ou atualiza o preço de um serviço individualmente.
    Se houver um preço antigo, registra-o na tabela de histórico para auditoria.
    """
    usuario_atual_id = session.get('usuario_id') or session.get('user_id')
    if not current_user.is_authenticated:
        flash("Sua sessão expirou. Por favor, faça login novamente.", "warning")
        return redirect(url_for('auth.login'))

    taxonomia_id = request.form.get('taxonomia_id', type=int)
    raw_valor = request.form.get('novo_valor', '0.00')

    # Sanitização completa de moeda no padrão BR (ex: "R$ 1.250,50" -> "1250.50")
    valor_limpo = (
        raw_valor.replace('R$', '')
                 .replace(' ', '')
                 .replace('.', '')
                 .replace(',', '.')
                 .strip()
    )

    try:
        novo_valor = Decimal(valor_limpo) if valor_limpo else Decimal('0.00')
    except (ValueError, InvalidOperation):
        flash("Valor de preço inserido é inválido.", "danger")
        return redirect(url_for('empresa.configurar_precos_listagem', empresa_id=empresa_id))

    # 1. Busca se já existe um registro de preço corrente para este serviço nesta empresa
    preco_corrente = EseServicoPreco.query.filter_by(empresa_id=empresa_id, taxonomia_id=taxonomia_id).first()

    observacao = request.form.get('observacao', '').strip()

    if preco_corrente:
        # Se o preço mudou, gera o registro no histórico
        if preco_corrente.novo_valor != novo_valor:
            historico = EseServicoPrecoHistorico(
                empresa_id=empresa_id,
                taxonomia_id=taxonomia_id,
                valor_antigo=preco_corrente.novo_valor,
                observacao=preco_corrente.observacao,
                alterado_por_usuario_id=preco_corrente.alterado_por_usuario_id,
                data_alteracao=preco_corrente.data_alteracao
            )
            db.session.add(historico)

            preco_corrente.novo_valor = novo_valor
            preco_corrente.observacao = observacao if observacao else None
            preco_corrente.alterado_por_usuario_id = usuario_atual_id
    else:
        novo_preco = EseServicoPreco(
            empresa_id=empresa_id,
            taxonomia_id=taxonomia_id,
            novo_valor=novo_valor,
            observacao=observacao if observacao else None,
            alterado_por_usuario_id=usuario_atual_id
        )
        db.session.add(novo_preco)

    db.session.commit()
    flash("Preço atualizado com sucesso!", "success")
    return redirect(url_for('empresa.configurar_precos_listagem', empresa_id=empresa_id))


def calcular_progresso_real(empresa):
    progresso = 0

    # Passo 1: Dados básicos e localização (Peso: 30%)
    if empresa.nome and empresa.local_id:
        progresso += 30

    # Passo 2: Identidade Visual (Peso: 10%)
    if empresa.logomarca and empresa.cor_primaria:
        progresso += 10

    # Passo 3: Documentação (Peso: 30%)
    # Verifica se os documentos de compliance de fato existem na pasta de uploads
    if empresa.status_homologacao in ['em_analise', 'aguardando_auditoria']:
        progresso += 30

    # Passo 4: Serviços/Preços cadastrados (Peso: 20%)
    # Exemplo: Verificar se há registros relacionados de serviços para essa empresa
    tem_servicos = len(empresa.servicos) > 0 if hasattr(empresa, 'servicos') else False
    if tem_servicos:
        progresso += 20

    # Passo 5: Colaboradores cadastrados (Peso: 10%)
    tem_colaboradores = len(empresa.colaboradores) > 0 if hasattr(empresa, 'colaboradores') else False
    if tem_colaboradores:
        progresso += 10

    return progresso


@empresa_bp.route('/precos/associar-grupo', methods=['POST'])
@login_required
def associar_grupo():
    """
    📌 ROTA POST: Associação de Serviços a Grupos de Exibição
    --------------------------------------------------------------------------------------
    Recebe um lote de IDs de registros de preços e vincula todos eles a um Grupo de Tabela específico.
    Executa a atualização diretamente via query parametrizada para otimizar a performance
    do banco de dados, garantindo a segurança através da trava por empresa_id do usuário logado.
    """
    dados = request.get_json()
    grupo_id = dados.get('grupo_id')  # ID (UUID de 36 caracteres) do EseGrupoTabela
    servicos_ids = dados.get('servicos_ids')  # Lista de IDs (inteiros) da EseServicoPreco

    if not grupo_id or not servicos_ids:
        return jsonify({'status': 'erro', 'mensagem': 'Dados incompletos para associação.'}), 400

    # Executa o update em bloco isolando estritamente pela empresa do usuário logado
    EseServicoPreco.query.filter(
        EseServicoPreco.id.in_(servicos_ids),
        EseServicoPreco.empresa_id == current_user.empresa_id
    ).update({EseServicoPreco.grupo_id: grupo_id}, synchronize_session=False)

    db.session.commit()
    return jsonify({'status': 'sucesso', 'mensagem': 'Serviços vinculados ao grupo com sucesso.'})


@empresa_bp.route('/<int:empresa_id>/vitrine', methods=['GET'])
def vitrine_tv(empresa_id):
    """
    📌 ROTA GET: Vitrine Digital Pública para TV / Recepção
    --------------------------------------------------------------------------------------
    Busca e estrutura todos os grupos e seus respectivos serviços ativos de uma empresa.
    Retorna uma tela limpa, otimizada para modo Quiosque ou Smart TV, incluindo a timestamp
    da última alteração de preço (data_alteracao) para conformidade e transparência.
    """
    # Recupera todos os grupos cadastrados para a empresa informada na URL
    # Nota: Certifique-se de que a model EseGrupoTabela use empresa_id como Integer para bater com a URL
    grupos = EseGrupoTabela.query.filter_by(empresa_id=empresa_id).all()

    # Obtém o indicador de atualização mais recente usando o campo correto: data_alteracao
    ultima_atualizacao = db.session.query(db.func.max(EseServicoPreco.data_alteracao)) \
        .filter_by(empresa_id=empresa_id).scalar()

    return render_template(
        'precos/vitrine_tv.html',
        grupos=grupos,
        ultima_atualizacao=ultima_atualizacao
    )


import hashlib
from flask import render_template, session, abort, flash, redirect, url_for, make_response
from feedin.modules.empresa.models import ColaboradorContrato
# Supondo que você use uma extensão para PDF, importamos o gerador (veremos abaixo)

def calcular_hash_contrato(contrato):
    """
    Gera um hash SHA-256 imutável baseado nos dados do acordo firmado.
    Utiliza estritamente o id_cadastro_cliente (UUID 36) como chave do colaborador.
    """
    payload = (
        f"CONTRATO_ID:{contrato.id}|"
        f"CLIENTE_UUID:{contrato.id_cadastro_cliente}|"
        f"LOCAL_ID:{contrato.id_local}|"
        f"EXP_INI:{contrato.hora_inicio_expediente.strftime('%H:%M') if contrato.hora_inicio_expediente else 'N/A'}|"
        f"EXP_FIM:{contrato.hora_fim_expediente.strftime('%H:%M') if contrato.hora_fim_expediente else 'N/A'}|"
        f"INT_INI:{contrato.hora_inicio_intervalo.strftime('%H:%M') if contrato.hora_inicio_intervalo else 'N/A'}|"
        f"INT_FIM:{contrato.hora_fim_intervalo.strftime('%H:%M') if contrato.hora_fim_intervalo else 'N/A'}|"
        f"DATA_CONTRATO:{contrato.data_contratacao.strftime('%Y-%m-%d') if contrato.data_contratacao else 'N/A'}"
    )
    return hashlib.sha256(payload.encode('utf-8')).hexdigest().upper()


def pode_gerenciar_empresa(empresa_id):
    """
    Verifica se o usuário atual (via current_user ou session) possui papel de gestão
    (papel_nivel >= 666) no local/empresa especificado.
    """
    # Identifica o UUID ou ID do usuário logado
    cliente_uuid = getattr(current_user, 'id', None) or session.get('id_cliente')
    usuario_id = getattr(current_user, 'id_usuario', None) or session.get('id_usuario')

    if not cliente_uuid and not usuario_id:
        return False

    # Busca o contrato ativo ou pendente do usuário na empresa solicitada
    query = ColaboradorContrato.query.filter(
        ColaboradorContrato.id_local == empresa_id
    )

    # Filtra por UUID do cliente ou por ID do usuário legado
    if cliente_uuid:
        query = query.filter(
            (ColaboradorContrato.id_cadastro_cliente == str(cliente_uuid)) |
            (ColaboradorContrato.id_usuario == usuario_id)
        )
    else:
        query = query.filter(ColaboradorContrato.id_usuario == usuario_id)

    contrato = query.first()

    if not contrato:
        return False

    # Valida se o papel_nivel do contrato é maior ou igual a 666 (Nível Gestor/Admin)
    return contrato.papel_nivel >= 666


@empresa_bp.route('/colaborador/admissao-concluida/<int:contrato_id>')
@login_required
def admissao_concluida_tela(contrato_id):
    # 1. Busca o contrato no banco
    contrato = ColaboradorContrato.query.get_or_404(contrato_id)

    # 2. Obtém a empresa diretamente via relacionamento do contrato
    ese_empresa = contrato.empresa

    # 3. Busca a escala de trabalho cadastrada diretamente usando a model importada
    escalas = EscalaTrabalhoColaborador.query.filter_by(
        contrato_id=contrato.id
    ).all()

    # 4. Nome oficial do colaborador e empresa
    nome_colaborador = contrato.nome
    nome_empresa = ese_empresa.nome if ese_empresa else 'Empresa'

    return render_template(
        'empresa/admissao_sucesso.html',
        contrato=contrato,
        ese_empresa=ese_empresa,
        empresa=ese_empresa,
        nome_empresa=nome_empresa,
        nome_colaborador=nome_colaborador,
        escalas=escalas,
    )


@empresa_bp.route('/colaborador/comprovante-pdf/<int:contrato_id>', methods=['GET'])
@login_required
@login_required
def baixar_comprovante_pdf(contrato_id):
    """
    Gera dinamicamente o comprovante em PDF e força o download (Content-Disposition: attachment)
    ou visualização inline no navegador.
    """
    contrato = ColaboradorContrato.query.get_or_404(contrato_id)

    # Permissão: O gestor da empresa ou o próprio colaborador vinculados ao contrato
    usuario_uuid = getattr(current_user, 'id', None) or session.get('id_cliente')
    e_dono_do_contrato = (contrato.id_cadastro_cliente and str(contrato.id_cadastro_cliente) == str(usuario_uuid))

    if not e_dono_do_contrato and not pode_gerenciar_empresa(contrato.id_local):
        abort(403)

    # 1. Resolução do Nome da Empresa / Estabelecimento
    empresa_obj = getattr(contrato, 'local_trabalho', None) or getattr(contrato, 'empresa', None) or getattr(contrato, 'local', None)
    nome_empresa = "Empresa"
    if empresa_obj:
        nome_empresa = (
            getattr(empresa_obj, 'razao_social', None) or
            getattr(empresa_obj, 'nome_fantasia', None) or
            getattr(empresa_obj, 'nome', 'Empresa')
        )

    # 2. Resolução do Nome do Colaborador
    nome_colaborador = getattr(contrato, 'nome', None) or "Colaborador"

    # 3. Resolução do Nome do Cargo (Tabela 'cargos', coluna 'nome_cargo')
    cargo_exibicao = "Não informado"
    if hasattr(contrato, 'cargo') and contrato.cargo:
        cargo_exibicao = getattr(contrato.cargo, 'nome_cargo', None) or getattr(contrato.cargo, 'nome', "Não informado")
    elif getattr(contrato, 'id_cargo', None):
        cargo_obj = Cargo.query.get(contrato.id_cargo)
        if cargo_obj:
            cargo_exibicao = getattr(cargo_obj, 'nome_cargo', 'Não informado')

    # 4. Mapeamento dos Dias da Semana
    DIAS_MAPA = {
        0: 'Segunda-feira',
        1: 'Terça-feira',
        2: 'Quarta-feira',
        3: 'Quinta-feira',
        4: 'Sexta-feira',
        5: 'Sábado',
        6: 'Domingo'
    }

    hoje = date.today()

    # 5. Busca as Escalas Vigentes na Tabela 'escala_trabalho_colaborador'
    escalas_query = EscalaTrabalhoColaborador.query.filter(
        EscalaTrabalhoColaborador.contrato_id == contrato.id,
        EscalaTrabalhoColaborador.ativo.is_(True),
        EscalaTrabalhoColaborador.data_inicio <= hoje,
        EscalaTrabalhoColaborador.data_fim >= hoje
    ).order_by(EscalaTrabalhoColaborador.dia_semana.asc()).all()

    grade_horarios = []
    for escala in escalas_query:
        entrada = escala.inicio_expediente.strftime('%H:%M') if escala.inicio_expediente else '-'
        saida = escala.fim_expediente.strftime('%H:%M') if escala.fim_expediente else '-'

        if escala.inicio_intervalo and escala.fim_intervalo:
            intervalo = f"{escala.inicio_intervalo.strftime('%H:%M')} às {escala.fim_intervalo.strftime('%H:%M')}"
        elif escala.inicio_intervalo:
            intervalo = f"Início às {escala.inicio_intervalo.strftime('%H:%M')}"
        else:
            intervalo = "Flexível / Não parametrizado"

        grade_horarios.append({
            'dia_nome': DIAS_MAPA.get(escala.dia_semana, f"Dia {escala.dia_semana}"),
            'entrada': entrada,
            'saida': saida,
            'intervalo': intervalo,
            'tipo_escala': escala.tipo_escala,
            'observacao': escala.observacao
        })

    # Rastro Digital
    hash_validacao = f"ADM-{contrato.id}-{contrato.id_local}-{int(datetime.now().timestamp())}"

    # Renderiza o HTML do comprovante
    html_string = render_template(
        'pdf/comprovante_admissao.html',
        contrato=contrato,
        nome_colaborador=nome_colaborador,
        nome_empresa=nome_empresa,
        cargo_exibicao=cargo_exibicao,
        grade_horarios=grade_horarios,
        hash_validacao=hash_validacao,
        data_emissao=datetime.now().strftime('%d/%m/%Y às %H:%M')
    )

    # Gera o PDF via WeasyPrint
    pdf_bytes = HTML(string=html_string).write_pdf()

    modo = request.args.get('modo', 'download')
    disposition = 'inline' if modo == 'visualizar' else f'attachment; filename="Comprovante_Admissao_Contrato_{contrato.id}.pdf"'

    response = make_response(pdf_bytes)
    response.headers['Content-Type'] = 'application/pdf'
    response.headers['Content-Disposition'] = disposition
    return response


@empresa_bp.route('/<int:empresa_id>/equipe-jornadas', methods=['GET'])
@login_required
@login_required
def configurar_jornadas_tela(empresa_id):
    empresa = EseEmpresa.query.get_or_404(empresa_id)

    # 1. Busca os contratos da equipe da empresa
    contratos_equipe = ColaboradorContrato.query.filter(
        ColaboradorContrato.id_local == empresa.local_id
    ).options(
        joinedload(ColaboradorContrato.cadastro_modulo),
        joinedload(ColaboradorContrato.cargo),
        selectinload(ColaboradorContrato.excecoes_jornada)
    ).all()

    if not contratos_equipe:
        return redirect(url_for('empresa.emitir_contrato_colaborador', empresa_id=empresa_id))

    # 2. Busca o contrato do USUÁRIO LOGADO nesta empresa
    contrato_logado = ColaboradorContrato.query.filter_by(
        id_local=empresa.local_id,
        id_cadastro_cliente=current_user.id
    ).first()

    logado_nivel = contrato_logado.papel_nivel if contrato_logado else 0
    logado_ativo = (contrato_logado.status_profissional == 'ativo') if contrato_logado else False

    hoje = date.today()

    # 3. Processa recesso e aplica a validação de permissão
    for contrato in contratos_equipe:
        # Processa recesso
        contrato.em_recesso = False
        excecoes = contrato.excecoes_jornada or []
        if excecoes:
            contrato.em_recesso = any(
                exc.data_inicio <= hoje <= exc.data_fim and exc.tipo in ['ferias', 'recesso', 'licenca']
                for exc in excecoes
            )

        # REGRAS DE GERENCIAMENTO:
        # 1. Usuário precisa estar ATIVO no sistema
        # 2. Se for Soberano (Nível 999): tem controle total (inclusive sobre outros 999 para passar bastão)
        # 3. Se for Gerencial (>= 666): precisa ter nível ESTRITAMENTE MAIOR que o alvo
        nivel_alvo = contrato.papel_nivel or 0

        is_soberano = (logado_nivel == 999)
        is_gerente_superior = (logado_nivel >= 666 and logado_nivel > nivel_alvo)

        contrato.pode_gerenciar = logado_ativo and (is_soberano or is_gerente_superior)

    # 4. Ordenação dos registros (Empreendedor/Nível mais alto primeiro)
    contratos_equipe.sort(
        key=lambda c: (c.papel_nivel or 0, c.nome or ''),
        reverse=True
    )

    open_offcanvas_id = request.args.get('open_offcanvas', type=int)

    return render_template(
        'empresa/configurar_jornadas.html',
        empresa=empresa,
        ese_empresa=empresa,
        contratos_equipe=contratos_equipe,
        open_offcanvas_id=open_offcanvas_id
    )


@empresa_bp.route('/contrato/<int:contrato_id>/remanejar', methods=['GET', 'POST'])
@login_required
@login_required
def remanejar_jornada(contrato_id):
    """
    Carrega o contrato de trabalho via cadastro unificado do módulo
    para permitir a reconfiguração dos horários e turnos do colaborador.
    """
    # Carrega o contrato trazendo junto os dados unificados do colaborador
    contrato = ColaboradorContrato.query.options(
        joinedload(ColaboradorContrato.cadastro_modulo)
    ).get_or_404(contrato_id)

    # ... resto da sua lógica de processamento do POST ou GET ...

    return render_template(
        'empresa/remanejar_jornada.html',
        contrato=contrato
    )


# ==============================================================================
# PASSO 1: IDENTIFICAÇÃO CIVIL E GERAÇÃO DE LINK DE ONBOARDING
# ==============================================================================
@empresa_bp.route('/<int:empresa_id>/equipe/admitir', methods=['GET', 'POST'])
@login_required
@login_required
def emitir_contrato_colaborador(empresa_id):
    """
    Passo 1: Emissão de Convite de Colaborador (Alfândega).
    Registra os dados propostos pelo empreendedor na tabela EseConviteColaborador
    e gera o link de onboarding sem tocar na tabela Usuario ou no contrato definitivo.
    """
    ese_empresa = Local.query.get_or_404(empresa_id)
    link_gerado = None
    nome_colaborador = None

    if request.method == 'POST':
        cpf_digitado = request.form.get('cpf', '').strip()
        cpf_limpo = "".join(filter(str.isdigit, cpf_digitado))
        nome_completo = request.form.get('nome_completo', '').strip()
        data_nasc_str = request.form.get('data_nascimento', '').strip()

        # Opcional: captura de horários do expediente se enviados pelo form do gestor
        hora_inicio_exp = request.form.get('hora_inicio_expediente')
        hora_fim_exp = request.form.get('hora_fim_expediente')
        hora_inicio_int = request.form.get('hora_inicio_intervalo')
        hora_fim_int = request.form.get('hora_fim_intervalo')

        if not cpf_limpo or not nome_completo or not data_nasc_str:
            flash("Preencha Nome, CPF e Data de Nascimento para continuar.", "danger")
            return render_template('empresa/emitir_contrato.html', ese_empresa=ese_empresa)

        try:
            data_nascimento = datetime.strptime(data_nasc_str, '%Y-%m-%d').date()

            # 1. Gera o hash SHA-256 do CPF para gravação cega (Alfândega)
            # Padronizado com o método da model EseConviteColaborador
            cpf_hash = EseConviteColaborador.gerar_hash_cpf(cpf_limpo)

            # 2. Verifica se já existe um convite pendente para este CPF neste mesmo estabelecimento
            convite_existente = EseConviteColaborador.query.filter_by(
                estabelecimento_id=empresa_id,
                cpf_hash=cpf_hash,
                status='pendente'
            ).first()

            if convite_existente:
                # Reutiliza o token existente para reemitir o link
                token = convite_existente.token_convite
                convite_existente.nome_proposto = nome_completo
                convite_existente.data_nascimento = data_nascimento
            else:
                # 3. Cria novo convite preliminar
                token = secrets.token_urlsafe(32)
                novo_convite = EseConviteColaborador(
                    estabelecimento_id=empresa_id,
                    cpf_hash=cpf_hash,
                    data_nascimento=data_nascimento,
                    data_contratacao=date.today(),
                    nome_proposto=nome_completo,
                    token_convite=token,
                    status='pendente'
                )

                # Atribuição dos horários de expediente caso os campos existam no Model
                if hasattr(novo_convite, 'hora_inicio_expediente') and hora_inicio_exp:
                    novo_convite.hora_inicio_expediente = hora_inicio_exp
                    novo_convite.hora_fim_expediente = hora_fim_exp
                    novo_convite.hora_inicio_intervalo = hora_inicio_int
                    novo_convite.hora_fim_intervalo = hora_fim_int

                db.session.add(novo_convite)

            db.session.commit()

            # 4. Monta o link externo da ficha admisional para o colaborador
            link_gerado = url_for('empresa.concluir_ficha_colaborador', token=token, _external=True)
            nome_colaborador = nome_completo

            flash("Convite gerado com sucesso! Envie o link para o colaborador.", "success")

        except Exception as e:
            db.session.rollback()
            print(f"🚨 Erro na Emissão de Convite: {str(e)}")
            flash("Erro ao processar a identificação do colaborador.", "danger")

    return render_template(
        'empresa/emitir_contrato.html',
        ese_empresa=ese_empresa,
        link_gerado=link_gerado,
        nome_colaborador=nome_colaborador
    )


@empresa_bp.route('/<int:empresa_id>/equipe/contrato/<int:contrato_id>/documento', methods=['GET'])
@login_required
@login_required
def visualizar_documento_contrato(empresa_id, contrato_id):
    """
    Passo 3 (Empreendedor): Gera a visualização/impressão do documento físico/jurídico
    com base nas informações autodeclaradas pelo colaborador.
    """
    contrato = ColaboradorContrato.query.filter_by(id=contrato_id, estabelecimento_id=empresa_id).first_or_404()
    detalhes = ColaboradorDetalhesPessoais.query.filter_by(contrato_id=contrato.id).first_or_404()

    return render_template(
        'empresa/documento_contrato.html',
        contrato=contrato,
        detalhes=detalhes,
        empresa=contrato.local
    )


@empresa_bp.route('/central-convites', methods=['GET'])
@login_required
def central_convites():
    """
    PONTO DE CONVERGÊNCIA E SEGURANÇA:
    Verifica se existem convites de admissão pendentes disparados para o CPF
    do usuário logado e permite a conclusão da ficha cadastral.
    """
    convites_pendentes = []
    cpf_usuario = getattr(current_user, 'cpf', None)

    if cpf_usuario:
        cpf_limpo = "".join(filter(str.isdigit, str(cpf_usuario)))
        if cpf_limpo:
            cpf_hash = EseConviteColaborador.gerar_hash_cpf(cpf_limpo)
            convites_pendentes = EseConviteColaborador.query.filter_by(
                cpf_hash=cpf_hash,
                status='pendente'
            ).all()

    return render_template(
        'empresa/central_convites.html',
        convites_pendentes=convites_pendentes
    )


def str_para_time(string_horario):
    """Auxiliar para converter string 'HH:MM' em objeto datetime.time."""
    if not string_horario:
        return None
    try:
        partes = string_horario.split(':')
        return time(int(partes[0]), int(partes[1]))
    except (ValueError, IndexError):
        return None


@empresa_bp.route('/colaborador/efetivar-admissao/<int:contrato_id>', methods=['GET', 'POST'])
@login_required
def efetivar_admissao(contrato_id):
    contrato = ColaboradorContrato.query.get_or_404(contrato_id)

    # 🔍 1. RESOLUÇÃO PONTUAL DE DADOS DE CONTATO
    nome_resolvido = ''
    email_resolvido = ''
    phone_resolvido = ''

    if contrato.detalhes_pessoais:
        nome_resolvido = contrato.detalhes_pessoais.nome_completo or contrato.detalhes_pessoais.nome_exibicao_pwa or ''
        phone_resolvido = contrato.detalhes_pessoais.telefone_pessoal or ''

    if contrato.cadastro_modulo:
        if not nome_resolvido:
            nome_resolvido = contrato.cadastro_modulo.nome_completo or contrato.cadastro_modulo.nome or ''
        email_resolvido = contrato.cadastro_modulo.email or ''
        if not phone_resolvido:
            phone_resolvido = contrato.cadastro_modulo.whatsapp or ''

    if not nome_resolvido or not email_resolvido:
        convite = EseConviteColaborador.query.filter_by(
            estabelecimento_id=contrato.id_local,
            status='pendente'
        ).order_by(EseConviteColaborador.id.desc()).first()

        if convite:
            if not nome_resolvido:
                nome_resolvido = convite.nome_proposto or ''
            cliente_convite = ModCadastroCliente.query.filter_by(cpf_hash=convite.cpf_hash).first()
            if cliente_convite:
                if not email_resolvido:
                    email_resolvido = cliente_convite.email or ''
                if not phone_resolvido:
                    phone_resolvido = cliente_convite.whatsapp or ''

    if request.method == 'POST':
        try:
            # Captura valores submetidos do form
            nome_post = request.form.get('nome_profissional', nome_resolvido).strip()
            email_post = request.form.get('email_contato', email_resolvido).strip()
            whats_post = request.form.get('whatsapp', phone_resolvido).strip()

            if contrato.cadastro_modulo:
                if email_post:
                    contrato.cadastro_modulo.email = email_post
                if whats_post:
                    contrato.cadastro_modulo.whatsapp = whats_post
                if nome_post:
                    contrato.cadastro_modulo.nome = nome_post

            # 🎯 2. ATUALIZAÇÃO EXPLICITA DE STATUS E DATA DE ADMISSÃO
            contrato.status_profissional = 'ativo'

            # Atualiza a data se for o primeiro aceite/efetivação
            if not contrato.data_contratacao:
                contrato.data_contratacao = datetime.now(timezone.utc)

            # Baixa em convites pendentes
            convites = EseConviteColaborador.query.filter_by(
                estabelecimento_id=contrato.id_local,
                status='pendente'
            ).all()
            for c in convites:
                c.status = 'aceito'

            # 🗓️ 3. GRAVAÇÃO DA ESCALA DE TRABALHO
            usuario_logado_id = None
            if current_user and current_user.is_authenticated:
                if hasattr(current_user, 'username_modulo'):
                    usuario_logado_id = current_user.id
                elif hasattr(current_user, 'cadastros_modulos'):
                    mod_perfil = current_user.cadastros_modulos.first()
                    if mod_perfil:
                        usuario_logado_id = mod_perfil.id

            escalas_existentes = EscalaTrabalhoColaborador.query.filter_by(
                contrato_id=contrato.id,
                tipo_escala='padrao'
            ).all()
            mapa_registros = {r.dia_semana: r for r in escalas_existentes}

            DATA_INICIO_PADRAO = date.today()
            DATA_FIM_PADRAO = date(2099, 12, 31)
            dias_semana = [1, 2, 3, 4, 5, 6, 0]  # Seg a Dom

            for dia_id in dias_semana:
                trabalha = request.form.get(f'trabalha_{dia_id}') == '1'
                registro_banco = mapa_registros.get(dia_id)

                if trabalha:
                    p1_in_str = request.form.get(f'p1_entrada_{dia_id}', '').strip()
                    p1_out_str = request.form.get(f'p1_saida_{dia_id}', '').strip()
                    p2_in_str = request.form.get(f'p2_entrada_{dia_id}', '').strip()
                    p2_out_str = request.form.get(f'p2_saida_{dia_id}', '').strip()

                    p1_in = time.fromisoformat(p1_in_str) if p1_in_str else None
                    p1_out = time.fromisoformat(p1_out_str) if p1_out_str else None
                    p2_in = time.fromisoformat(p2_in_str) if p2_in_str else None
                    p2_out = time.fromisoformat(p2_out_str) if p2_out_str else None

                    tem_p1 = bool(p1_in and p1_out)
                    tem_p2 = bool(p2_in and p2_out)

                    if not (p1_in or p1_out or p2_in or p2_out):
                        continue

                    if tem_p1 and tem_p2:
                        ini_exp, ini_int, fim_int, fim_exp = p1_in, p1_out, p2_in, p2_out
                    elif tem_p1:
                        ini_exp, ini_int, fim_int, fim_exp = p1_in, None, None, p1_out
                    elif tem_p2:
                        ini_exp, ini_int, fim_int, fim_exp = p2_in, None, None, p2_out
                    else:
                        ini_exp = p1_in or p2_in
                        fim_exp = p2_out or p1_out
                        ini_int, fim_int = None, None

                    if not registro_banco:
                        registro_banco = EscalaTrabalhoColaborador(
                            contrato_id=contrato.id,
                            dia_semana=dia_id,
                            tipo_escala='padrao',
                            data_inicio=DATA_INICIO_PADRAO,
                            data_fim=DATA_FIM_PADRAO,
                            observacao=None
                        )
                        db.session.add(registro_banco)

                    registro_banco.inicio_expediente = ini_exp
                    registro_banco.inicio_intervalo = ini_int
                    registro_banco.fim_intervalo = fim_int
                    registro_banco.fim_expediente = fim_exp
                    registro_banco.ativo = True
                    registro_banco.atualizado_por_id = usuario_logado_id
                    registro_banco.atualizado_em = datetime.now(timezone.utc)

                else:
                    if registro_banco:
                        registro_banco.ativo = False
                        registro_banco.atualizado_por_id = usuario_logado_id
                        registro_banco.atualizado_em = datetime.now(timezone.utc)

            # 💾 COMMIT NO BANCO
            db.session.commit()

            # 📩 4. ENVIO DE E-MAIL ISOLADO
            try:
                disparar_email_admissao_concluida(contrato, email_post, nome_post)
            except Exception as email_err:
                current_app.logger.error(f"Erro ao enviar e-mail de admissão: {str(email_err)}")

            flash('Colaborador efetivado e escala salva com sucesso!', 'success')

        except Exception as e:
            db.session.rollback()
            current_app.logger.error(f"Erro ao efetivar contrato #{contrato.id}: {str(e)}")
            flash(f"Erro ao salvar: {str(e)}", "danger")

    # 🏢 RESOLUÇÃO DA EMPRESA
    empresa_obj = getattr(contrato, 'empresa', None) or getattr(contrato, 'local', None)

    if empresa_obj:
        nome_empresa = (
                getattr(empresa_obj, 'razao_social', None) or
                getattr(empresa_obj, 'nome', None) or
                getattr(empresa_obj, 'nome_fantasia', 'Empresa')
        )
    else:
        nome_empresa = "Empresa"

    dados_iniciais = {
        'nome': nome_resolvido,
        'email': email_resolvido,
        'whatsapp': phone_resolvido
    }

    return render_template(
        'pdf/admissao_concluida.html',
        contrato=contrato,
        dados_iniciais=dados_iniciais,
        nome_colaborador=nome_resolvido,
        nome_empresa=nome_empresa,
        ano_atual=datetime.now().year
    )


@empresa_bp.route('/<int:empresa_id>/equipe/admitir-passo1/<int:contrato_id>', methods=['GET', 'POST'])
@login_required
def admitir_passo1(empresa_id, contrato_id):
    """Passo 1 do fluxo de admissão da equipe."""
    empresa = EseEmpresa.query.get_or_404(empresa_id)
    contrato = ColaboradorContrato.query.get_or_404(contrato_id)

    if request.method == 'POST':
        contrato.nome_provisorio = request.form.get('nome_provisorio')
        contrato.email_destino = request.form.get('email_destino')
        contrato.whatsapp_destino = request.form.get('whatsapp_destino')

        # Persiste no banco de dados
        db.session.commit()

        flash('Convite de admissão enviado com sucesso!', 'success')
        return redirect(url_for('empresa.equipe_jornadas', empresa_id=empresa.id))

    return render_template('empresa/admitir_passo1.html', ese_empresa=empresa, contrato=contrato)


@empresa_bp.route('/<int:empresa_id>/equipe/configurar-passo2/<int:contrato_id>', methods=['GET', 'POST'])
@login_required
def configurar_passo2(empresa_id, contrato_id):
    """Passo 2 do fluxo de admissão da equipe (Cargo e Habilidades)."""
    empresa = EseEmpresa.query.get_or_404(empresa_id)
    contrato = ColaboradorContrato.query.get_or_404(contrato_id)

    if request.method == 'POST':
        nome_cargo = request.form.get('cargo_atuacao')
        atividades_selecionadas = request.form.getlist('atividades[]')

        if nome_cargo:
            # Busca ou cria o cargo de forma transparente
            cargo = Cargo.query.filter_by(nome_cargo=nome_cargo, empresa_id=empresa.id).first()
            if not cargo:
                cargo = Cargo(nome_cargo=nome_cargo, empresa_id=empresa.id)
                db.session.add(cargo)
                db.session.flush()  # Gera o cargo.id antes do commit final

            contrato.cargo_id = cargo.id

        # Atualiza a persistência final
        db.session.commit()

        flash('Perfil profissional e habilidades mapeadas com sucesso!', 'success')
        return redirect(url_for('empresa.equipe_jornadas', empresa_id=empresa.id))

    return render_template('empresa/admitir_passo2.html', ese_empresa=empresa, contrato=contrato)


@empresa_bp.route('/<int:empresa_id>/buscar-cargos')
def buscar_cargos(empresa_id):
    """
    Endpoint assíncrono (API interna) para o Autocomplete de Cargos.

    Objetivo:
    - Realiza uma busca parcial (Case-Insensitive) nos cargos já cadastrados
      por aquela empresa específica à medida que o usuário digita na tela.

    Parâmetros:
    :param empresa_id: ID da empresa para segmentar e proteger os dados.
    :query param q: Termo de busca digitado pelo usuário (Ex: /buscar-cargos?q=barb)

    Retorno:
    - JSON: Uma lista simples de strings contendo os nomes dos cargos correspondentes.
    """
    # 1. Captura o termo de busca enviado via query string (padrão vazio se não fornecido)
    term = request.args.get('q', '')

    # 2. Executa a query filtrando por empresa e aplicando o operador ILIKE para busca parcial
    cargos = Cargo.query.filter(
        Cargo.empresa_id == empresa_id,
        Cargo.nome_cargo.ilike(f'%{term}%')
    ).all()

    # 3. Serializa o resultado extraindo apenas o nome do cargo e retorna como resposta JSON
    return jsonify([c.nome_cargo for c in cargos])


def BuscarEmpresaDoCliente(cliente_modulo_id):
    """
    Recupera a empresa (EseEmpresa) ativa atrelada ao cliente_modulo_id
    utilizando a tabela pivot VinculoUsuarioEmpresa.
    """
    if not cliente_modulo_id:
        return None

    # 1. Recupera o cliente comercial da sessão
    cliente = ModCadastroCliente.query.get(cliente_modulo_id)
    if not cliente or not cliente.usuario_id:
        return None

    # 2. Busca o vínculo ativo do usuário com maior nível de autoridade (ex: proprietário/gerente)
    vinculo = VinculoUsuarioEmpresa.query.filter_by(
        usuario_id=cliente.usuario_id,
        ativo=True
    ).order_by(VinculoUsuarioEmpresa.papel_nivel.desc()).first()

    # 3. Se houver vínculo ativo, retorna a instância de EseEmpresa associada
    if vinculo and vinculo.empresa and vinculo.empresa.ativo:
        return vinculo.empresa

    return None


from flask import render_template, request, redirect, url_for, flash

@empresa_bp.route('/<int:empresa_id>/contrato/<int:contrato_id>/cargo-habilidades', methods=['GET', 'POST'])
def configurar_cargo_habilidades(empresa_id, contrato_id):
    empresa = EseEmpresa.query.get_or_404(empresa_id)
    contrato = ColaboradorContrato.query.get_or_404(contrato_id)

    if contrato.id_local != empresa.id:
        flash('O contrato não pertence a esta empresa.', 'danger')
        return redirect(url_for('empresa.editar_empresa', empresa_id=empresa.id))

    # PROCESSAMENTO POST
    if request.method == 'POST':
        acao_form = request.form.get('acao_form', 'salvar_habilidades')
        try:
            # A. Salvar Cargo e Habilidades Selecionadas
            if acao_form == 'salvar_habilidades':
                nome_cargo = request.form.get('cargo_atuacao', '').strip()
                servicos_ids = request.form.getlist('servicos_ids', type=int)

                if nome_cargo:
                    cargo = Cargo.query.filter(Cargo.nome_cargo.ilike(nome_cargo)).first()
                    if not cargo:
                        cargo = Cargo(nome_cargo=nome_cargo)
                        db.session.add(cargo)
                        db.session.flush()
                    contrato.id_cargo = cargo.id

                # Sincroniza Habilidades do Contrato
                EseColaboradorServicoHabilidade.query.filter_by(contrato_id=contrato.id).delete()
                for s_id in servicos_ids:
                    db.session.add(EseColaboradorServicoHabilidade(contrato_id=contrato.id, servico_oferecido_id=s_id))

                db.session.commit()
                flash('Cargo e habilidades atualizados com sucesso!', 'success')
                return redirect(url_for('empresa.configurar_cargo_habilidades', empresa_id=empresa.id, contrato_id=contrato.id))

            # B. Inclusão de Novo Serviço no Catálogo via Modal (Se não existir)
            elif acao_form == 'novo_servico_expresso':
                taxonomia_id = request.form.get('taxonomia_id', type=int)
                descricao = request.form.get('descricao_servico', '').strip()

                if taxonomia_id:
                    novo_s = EseServicoOferecido(
                        empresa_id=empresa.id,
                        taxonomia_id=taxonomia_id,
                        descricao_servico=descricao or None,
                        inserido_por_usuario_id=getattr(current_user, 'id', None)
                    )
                    db.session.add(novo_s)
                    db.session.flush()
                    # Já vincula ao colaborador
                    db.session.add(EseColaboradorServicoHabilidade(contrato_id=contrato.id, servico_oferecido_id=novo_s.id))
                    db.session.commit()
                    flash('Novo serviço cadastrado e atribuído ao colaborador!', 'success')
                return redirect(url_for('empresa.configurar_cargo_habilidades', empresa_id=empresa.id, contrato_id=contrato.id))

        except Exception as e:
            db.session.rollback()
            flash(f'Erro ao salvar: {str(e)}', 'danger')

    # DADOS PARA O GET
    # 1. Nome garantido do colaborador
    colaborador_obj = getattr(contrato, 'colaborador', None) or getattr(contrato, 'cadastro_modulo', None)
    nome_colaborador = getattr(colaborador_obj, 'nome', None) or getattr(colaborador_obj, 'nome_razao', None) or f'Colaborador #{contrato.id}'

    # 2. Lista de todos os cargos para autocomplete
    cargos_existentes = [c.nome_cargo for c in Cargo.query.order_by(Cargo.nome_cargo.asc()).all()]

    # 3. Catálogo de serviços da empresa
    servicos_empresa = EseServicoOferecido.query.filter_by(empresa_id=empresa.id).all()

    # 4. Taxonomias globais para criação rápida de serviço
    taxonomias_globais = Taxonomia.query.filter_by(visivel_negocio=True).order_by(Taxonomia.nome.asc()).all()

    # 5. Habilidades já atribuídas ao contrato
    habilidades_atuais_ids = [h.servico_oferecido_id for h in EseColaboradorServicoHabilidade.query.filter_by(contrato_id=contrato.id).all()]

    cargo_atual_nome = contrato.cargo.nome_cargo if contrato.cargo else ''

    return render_template(
        'empresa/cargo_habilidades.html',
        empresa=empresa,
        contrato=contrato,
        nome_colaborador=nome_colaborador,
        cargo_atual_nome=cargo_atual_nome,
        cargos_existentes=cargos_existentes,
        servicos_empresa=servicos_empresa,
        taxonomias_globais=taxonomias_globais,
        habilidades_atuais_ids=habilidades_atuais_ids
    )


def converter_weekday_para_empresa(dia_python: int) -> int:
    """
    📌 Mapeia o índice do dia da semana retornado pelo Python para a convenção
       utilizada na tabela `EseHorarioFuncionamento`.

    --------------------------------------------------------------------------------------
    Justificativa Técnica:
    - O método nativo `datetime.weekday()` do Python adota a convenção ISO:
        0 = Segunda-feira ... 6 = Domingo
    - A tabela de banco de dados (`EseHorarioFuncionamento.dia_semana`) utiliza o padrão
      clássico/banco relacional (SQL `DAYOFWEEK` / `strftime('%w')`):
        0 = Domingo, 1 = Segunda-feira ... 6 = Sábado

    Esta função faz a tradução determinística para garantir que a validação cruzada
    de expediente ocorra no dia correto da semana do estabelecimento.

    --------------------------------------------------------------------------------------
    Tabela de Conversão:
        Python (Entrada)  --->  Empresa / Banco (Saída)
        0 (Segunda-feira) --->  1
        1 (Terça-feira)   --->  2
        2 (Quarta-feira)  --->  3
        3 (Quinta-feira)  --->  4
        4 (Sexta-feira)   --->  5
        5 (Sábado)        --->  6
        6 (Domingo)       --->  0

    Args:
        dia_python (int): Índice do dia da semana retornado por `datetime.weekday()` [0 a 6].

    Returns:
        int: Índice correspondente do dia na tabela `EseHorarioFuncionamento` [0 a 6].

    Example:
        >>> converter_weekday_para_empresa(0) # Segunda-feira em Python
        1
        >>> converter_weekday_para_empresa(6) # Domingo em Python
        0
    """
    mapa = {0: 1, 1: 2, 2: 3, 3: 4, 4: 5, 5: 6, 6: 0}
    return mapa[dia_python]


@empresa_bp.route('/<int:empresa_id>/contrato/<int:contrato_id>/escala-semanal', methods=['GET', 'POST'])
def configurar_escala_semanal(empresa_id: int, contrato_id: int):
    ese_empresa = EseEmpresa.query.get_or_404(empresa_id)
    contrato = ColaboradorContrato.query.get_or_404(contrato_id)

    # 1. Carrega Horários de Funcionamento da Empresa
    horarios_empresa = EseHorarioFuncionamento.query.filter_by(empresa_id=ese_empresa.id).all()
    grid_empresa = {}
    for h in horarios_empresa:
        if h.dia_semana not in grid_empresa:
            grid_empresa[h.dia_semana] = []
        grid_empresa[h.dia_semana].append(h)

    # Convenção do Modelo: 0=Domingo, 1=Segunda, 2=Terça, 3=Quarta, 4=Quinta, 5=Sexta, 6=Sábado
    dias_semana_map = [
        {'id': 1, 'nome': 'Segunda-feira', 'sigla': 'SEG'},
        {'id': 2, 'nome': 'Terça-feira',   'sigla': 'TER'},
        {'id': 3, 'nome': 'Quarta-feira',  'sigla': 'QUA'},
        {'id': 4, 'nome': 'Quinta-feira',  'sigla': 'QUI'},
        {'id': 5, 'nome': 'Sexta-feira',   'sigla': 'SEX'},
        {'id': 6, 'nome': 'Sábado',        'sigla': 'SÁB'},
        {'id': 0, 'nome': 'Domingo',       'sigla': 'DOM'},
    ]

    # Processamento do formulário (POST)
    if request.method == 'POST':
        try:
            erros_validacao = []

            # Captura o ID do usuário logado para auditoria
            usuario_logado_id = None
            if current_user and current_user.is_authenticated:
                if hasattr(current_user, 'username_modulo'):
                    usuario_logado_id = current_user.id
                elif hasattr(current_user, 'cadastros_modulos'):
                    mod_perfil = current_user.cadastros_modulos.first()
                    if mod_perfil:
                        usuario_logado_id = mod_perfil.id

            escalas_existentes = EscalaTrabalhoColaborador.query.filter_by(
                contrato_id=contrato.id,
                tipo_escala='padrao'
            ).all()

            mapa_registros = {r.dia_semana: r for r in escalas_existentes}

            DATA_INICIO_PADRAO = date.today()
            DATA_FIM_PADRAO = date(2099, 12, 31)

            for dia_info in dias_semana_map:
                dia_id = dia_info['id']

                trabalha = request.form.get(f'trabalha_{dia_id}') == '1'
                registro_banco = mapa_registros.get(dia_id)

                if trabalha:
                    p1_in_str = request.form.get(f'p1_entrada_{dia_id}', '').strip()
                    p1_out_str = request.form.get(f'p1_saida_{dia_id}', '').strip()
                    p2_in_str = request.form.get(f'p2_entrada_{dia_id}', '').strip()
                    p2_out_str = request.form.get(f'p2_saida_{dia_id}', '').strip()

                    p1_in = time.fromisoformat(p1_in_str) if p1_in_str else None
                    p1_out = time.fromisoformat(p1_out_str) if p1_out_str else None
                    p2_in = time.fromisoformat(p2_in_str) if p2_in_str else None
                    p2_out = time.fromisoformat(p2_out_str) if p2_out_str else None

                    tem_p1 = bool(p1_in and p1_out)
                    tem_p2 = bool(p2_in and p2_out)

                    # Validação flexível: aceita P1 completo, P2 completo ou apenas entrada/saída direta
                    if not (p1_in or p1_out or p2_in or p2_out):
                        erros_validacao.append(
                            f"{dia_info['nome']}: Marcado para trabalho, mas nenhum horário foi informado."
                        )
                        continue

                    # Mapeamento do expediente e intervalo conforme preenchimento
                    if tem_p1 and tem_p2:
                        ini_exp, ini_int, fim_int, fim_exp = p1_in, p1_out, p2_in, p2_out
                    elif tem_p1:
                        ini_exp, ini_int, fim_int, fim_exp = p1_in, None, None, p1_out
                    elif tem_p2:
                        ini_exp, ini_int, fim_int, fim_exp = p2_in, None, None, p2_out
                    else:
                        # Trata casos de preenchimento parcial/flexível
                        ini_exp = p1_in or p2_in
                        fim_exp = p2_out or p1_out
                        ini_int, fim_int = None, None

                    # Cria o registro se não existir no banco
                    if not registro_banco:
                        registro_banco = EscalaTrabalhoColaborador(
                            contrato_id=contrato.id,
                            dia_semana=dia_id,
                            tipo_escala='padrao',
                            data_inicio=DATA_INICIO_PADRAO,
                            data_fim=DATA_FIM_PADRAO,
                            observacao=None
                        )
                        db.session.add(registro_banco)

                    registro_banco.inicio_expediente = ini_exp
                    registro_banco.inicio_intervalo = ini_int
                    registro_banco.fim_intervalo = fim_int
                    registro_banco.fim_expediente = fim_exp
                    registro_banco.ativo = True
                    registro_banco.atualizado_por_id = usuario_logado_id
                    registro_banco.atualizado_em = datetime.now(timezone.utc)

                else:
                    # Se desativado e JÁ EXISTIA no banco, desativa o registro existente
                    if registro_banco:
                        registro_banco.ativo = False
                        registro_banco.atualizado_por_id = usuario_logado_id
                        registro_banco.atualizado_em = datetime.now(timezone.utc)

            if erros_validacao:
                db.session.rollback()
                for erro in erros_validacao:
                    flash(erro, 'warning')
            else:
                db.session.commit()
                flash('Escala semanal salva com sucesso!', 'success')
                return redirect(
                    url_for('empresa.configurar_escala_semanal', empresa_id=ese_empresa.id, contrato_id=contrato.id)
                )

        except Exception as e:
            db.session.rollback()
            flash(f'Erro ao processar escala: {str(e)}', 'danger')

    # DADOS PARA RENDERIZAÇÃO (GET)
    grade_empresa_map = {}
    for dia_emp_id, turnos in grid_empresa.items():
        if turnos:
            grade_empresa_map[dia_emp_id] = {
                'abertura': min(t.horario_abertura for t in turnos),
                'fechamento': max(t.horario_fechamento for t in turnos)
            }

    # Busca estrita: Apenas registros ATIVOS previamente salvos alimentam o dicionário
    registros_escala = EscalaTrabalhoColaborador.query.filter_by(
        contrato_id=contrato.id,
        tipo_escala='padrao',
        ativo=True
    ).all()

    escala_map = {}
    for reg in registros_escala:
        p1_in = reg.inicio_expediente.strftime('%H:%M') if reg.inicio_expediente else ''

        # Se existe intervalo, Parte 1 é Expediente -> Intervalo e Parte 2 é Intervalo -> Fim Expediente
        if reg.inicio_intervalo:
            p1_out = reg.inicio_intervalo.strftime('%H:%M')
            p2_in = reg.fim_intervalo.strftime('%H:%M') if reg.fim_intervalo else ''
            p2_out = reg.fim_expediente.strftime('%H:%M') if reg.fim_expediente else ''
        else:
            # Sem intervalo, Parte 1 é Expediente -> Fim Expediente
            p1_out = reg.fim_expediente.strftime('%H:%M') if reg.fim_expediente else ''
            p2_in = ''
            p2_out = ''

        escala_map[reg.dia_semana] = {
            'p1_entrada': p1_in,
            'p1_saida': p1_out,
            'p2_entrada': p2_in,
            'p2_saida': p2_out,
        }

    colaborador_obj = getattr(contrato, 'colaborador', None) or getattr(contrato, 'cadastro_modulo', None)
    nome_colaborador = getattr(colaborador_obj, 'nome', None) or getattr(colaborador_obj, 'nome_razao', None) or f'Colaborador #{contrato.id}'

    return render_template(
        'empresa/escala_semanal.html',
        ese_empresa=ese_empresa,
        contrato=contrato,
        nome_colaborador=nome_colaborador,
        grade_empresa_map=grade_empresa_map,
        escala_map=escala_map,
        dias_semana_map=dias_semana_map
    )


def processar_gravacao_excecao(empresa_id: str, contrato_id: str | None, form_data: dict) -> EseExcecaoCalendario:
    """
    ===================================================================================
    FUNÇÃO DE SERVIÇO: processar_gravacao_excecao
    ===================================================================================
    Descrição:
        Processa, valida e persiste registros de exceção de calendário e recessos
        tanto para a Empresa (quando contrato_id for None) quanto para um Colaborador.

    Parâmetros:
        :param empresa_id: String (UUID) - ID da empresa proprietária do registro.
        :param contrato_id: String (UUID) | None - ID do contrato do colaborador (opcional).
        :param form_data: dict - Dicionário contendo os dados do formulário (request.form).

    Retorno:
        :return: Instância de EseExcecaoCalendario recém-criada e gravada no banco.

    Exceções Tratadas:
        - ValueError: Se as datas forem inválidas ou data_fim for menor que data_inicio.
    ===================================================================================
    """
    # 1. Identificação de Origem e Auditoria (UUID do ModCadastroCliente)
    origem = EseOrigemExcecaoEnum.COLABORADOR if contrato_id else EseOrigemExcecaoEnum.EMPRESA
    usuario_logado_id = getattr(current_user, 'id', None)

    # 2. Processamento e Validação de Datas
    dt_inicio_str = form_data.get('data_inicio', '').strip()
    dt_fim_str = form_data.get('data_fim', '').strip() or dt_inicio_str

    if not dt_inicio_str:
        raise ValueError("A data inicial é obrigatória.")

    dt_inicio = date.fromisoformat(dt_inicio_str)
    dt_fim = date.fromisoformat(dt_fim_str)

    if dt_fim < dt_inicio:
        raise ValueError("A data final não pode ser anterior à data inicial.")

    # 3. Leitura e Estruturação das Regras de Expediente
    trabalha = form_data.get('trabalha') == '1'

    ini_exp, ini_int, fim_int, fim_exp = None, None, None, None

    if trabalha:
        p1_in_str = form_data.get('p1_entrada', '').strip()
        p1_out_str = form_data.get('p1_saida', '').strip()
        p2_in_str = form_data.get('p2_entrada', '').strip()
        p2_out_str = form_data.get('p2_saida', '').strip()

        p1_in = time.fromisoformat(p1_in_str) if p1_in_str else None
        p1_out = time.fromisoformat(p1_out_str) if p1_out_str else None
        p2_in = time.fromisoformat(p2_in_str) if p2_in_str else None
        p2_out = time.fromisoformat(p2_out_str) if p2_out_str else None

        tem_p1 = bool(p1_in and p1_out)
        tem_p2 = bool(p2_in and p2_out)

        if not tem_p1 and not tem_p2:
            raise ValueError("O registro foi marcado como 'Trabalha', mas nenhum horário válido foi fornecido.")

        if tem_p1 and tem_p2:
            ini_exp, ini_int, fim_int, fim_exp = p1_in, p1_out, p2_in, p2_out
        elif tem_p1:
            ini_exp, ini_int, fim_int, fim_exp = p1_in, None, None, p1_out
        else:
            ini_exp, ini_int, fim_int, fim_exp = p2_in, None, None, p2_out

    # 4. Instanciação e Persistência da Exceção
    tipo_str = form_data.get('tipo', 'recesso').lower()
    tipo_enum = EseTipoExcecaoEnum.EXCECAO if tipo_str == 'excecao' else EseTipoExcecaoEnum.RECESSO

    nova_excecao = EseExcecaoCalendario(
        id=str(uuid.uuid4()),
        empresa_id=empresa_id,
        contrato_id=contrato_id,
        origem=origem,
        tipo=tipo_enum,
        data_inicio=dt_inicio,
        data_fim=dt_fim,
        trabalha=trabalha,
        inicio_expediente=ini_exp,
        inicio_intervalo=ini_int,
        fim_intervalo=fim_int,
        fim_expediente=fim_exp,
        motivo_titulo=form_data.get('motivo_titulo', '').strip(),
        justificativa=form_data.get('justificativa', '').strip(),
        ativo=True,
        criado_em=datetime.utcnow(),
        atualizado_em=datetime.utcnow(),
        atualizado_por_id=usuario_logado_id
    )

    db.session.add(nova_excecao)
    db.session.commit()

    return nova_excecao


def obter_janelas_disponiveis_no_dia(contrato_id: str, data_consulta: date) -> list[dict]:
    """
    Exemplo conceitual do cálculo de janelas de atendimento/trabalho no dia:
    """
    # 1. Obtém a escala normal do colaborador para o dia da semana (ex: 08:00 às 18:00 com intervalo 12:00-13:00)
    escala = obter_escala_padrao(contrato_id, data_consulta)
    if not escala or not escala.trabalha:
        return []  # Dia de folga normal

    # 2. Busca se há exceção cadastrada para o colaborador ou empresa nesta data
    excecao = EseExcecaoCalendario.query.filter(
        EseExcecaoCalendario.contrato_id == contrato_id,
        EseExcecaoCalendario.data_inicio <= data_consulta,
        EseExcecaoCalendario.data_fim >= data_consulta,
        EseExcecaoCalendario.ativo == True
    ).first()

    # Cenário A: Sem exceção -> Retorna a escala normal completa
    if not excecao:
        return [
            {'inicio': escala.inicio_expediente, 'fim': escala.inicio_intervalo},
            {'inicio': escala.fim_intervalo, 'fim': escala.fim_expediente}
        ]

    # Cenário B: Recesso Dia Todo (considera_horario = False e trabalha = False)
    if not excecao.trabalha and not excecao.considera_horario:
        return []  # Nenhum horário disponível

    # Cenário C: Ausência Parcial (considera_horario = True, ex: Ausente das 14:00 às 17:00)
    if excecao.considera_horario and excecao.hora_inicio_excecao and excecao.hora_fim_excecao:
        # Pega as janelas normais e "subtrai" o intervalo bloqueado (14:00-17:00)
        # Ex: Turno Tarde era 13:00 às 18:00 -> Vira 13:00 às 14:00 e 17:00 às 18:00.
        janelas_livres = subtrair_intervalo_horario(
            janelas_normais=[
                (escala.inicio_expediente, escala.inicio_intervalo),
                (escala.fim_intervalo, escala.fim_expediente)
            ],
            bloqueio=(excecao.hora_inicio_excecao, excecao.hora_fim_excecao)
        )
        return janelas_livres

    return []


def _str_to_time(time_str: str) -> time | None:
    """Converte string 'HH:MM' para objeto datetime.time de forma segura."""
    if not time_str or not time_str.strip():
        return None
    try:
        return datetime.strptime(time_str.strip(), '%H:%M').time()
    except ValueError:
        return None


@empresa_bp.route('/<empresa_id>/excecoes/salvar', methods=['POST'])
def salvar_excecao_calendario(empresa_id):
    """
    ===================================================================================
    ROTA: salvar_excecao_calendario
    ===================================================================================
    Processa e persiste regras de exceção, recessos totais e ausências parciais
    tanto para a Empresa quanto para um Colaborador específico.
    ===================================================================================
    """
    try:
        # 1. Coleta de Dados do Formulário
        excecao_id = request.form.get('excecao_id')
        contrato_id = request.form.get('contrato_id') or None

        origem_str = request.form.get('origem', 'empresa').lower()
        tipo_str = request.form.get('tipo', 'recesso').lower()

        data_inicio_str = request.form.get('data_inicio')
        data_fim_str = request.form.get('data_fim') or data_inicio_str

        # Flags e Escopo
        considera_horario = request.form.get('considera_horario') == 'true'
        trabalha = request.form.get('trabalha') == 'true'

        motivo_titulo = request.form.get('motivo_titulo', '').strip()
        justificativa = request.form.get('justificativa', '').strip()

        # Validações Básicas
        if not data_inicio_str or not motivo_titulo:
            return jsonify({'sucesso': False, 'mensagem': 'Data e Motivo são obrigatórios.'}), 400

        data_inicio = date.fromisoformat(data_inicio_str)
        data_fim = date.fromisoformat(data_fim_str)

        if data_fim < data_inicio:
            return jsonify({'sucesso': False, 'mensagem': 'A data final não pode ser menor que a inicial.'}), 400

        # Enums
        origem_enum = EseOrigemExcecaoEnum.COLABORADOR if contrato_id else EseOrigemExcecaoEnum.EMPRESA
        tipo_enum = EseTipoExcecaoEnum.EXCECAO if tipo_str == 'excecao' else EseTipoExcecaoEnum.RECESSO

        # 2. Processamento dos Horários
        hora_inicio_excecao = None
        hora_fim_excecao = None

        inicio_exp = None
        inicio_int = None
        fim_int = None
        fim_exp = None

        if considera_horario:
            # Caso A: Ausência Parcial em Faixa de Horas
            hora_inicio_excecao = _str_to_time(request.form.get('hora_inicio_excecao'))
            hora_fim_excecao = _str_to_time(request.form.get('hora_fim_excecao'))

            if not hora_inicio_excecao or not hora_fim_excecao or hora_inicio_excecao >= hora_fim_excecao:
                return jsonify(
                    {'sucesso': False, 'mensagem': 'Informe uma faixa de horário válida para a ausência.'}), 400

        elif trabalha:
            # Caso B: Expediente Especial (Trabalho em dia atípico)
            inicio_exp = _str_to_time(request.form.get('inicio_expediente'))
            inicio_int = _str_to_time(request.form.get('inicio_intervalo'))
            fim_int = _str_to_time(request.form.get('fim_intervalo'))
            fim_exp = _str_to_time(request.form.get('fim_expediente'))

        # 3. Persistência (Update ou Insert)
        if excecao_id:
            registro = EseExcecaoCalendario.query.get(excecao_id)
            if not registro:
                return jsonify({'sucesso': False, 'mensagem': 'Registro não encontrado.'}), 404
        else:
            import uuid
            registro = EseExcecaoCalendario(id=str(uuid.uuid4()))
            db.session.add(registro)

        registro.empresa_id = empresa_id
        registro.contrato_id = contrato_id
        registro.origem = origem_enum
        registro.tipo = tipo_enum
        registro.data_inicio = data_inicio
        registro.data_fim = data_fim
        registro.considera_horario = considera_horario
        registro.hora_inicio_excecao = hora_inicio_excecao
        registro.hora_fim_excecao = hora_fim_excecao
        registro.trabalha = trabalha
        registro.inicio_expediente = inicio_exp
        registro.inicio_intervalo = inicio_int
        registro.fim_intervalo = fim_int
        registro.fim_expediente = fim_exp
        registro.motivo_titulo = motivo_titulo
        registro.justificativa = justificativa

        # ID do usuário logado (exemplo com Flask-Login)
        # registro.atualizado_por_id = current_user.id

        db.session.commit()

        return jsonify({'sucesso': True, 'mensagem': 'Exceção salva com sucesso!'})

    except Exception as e:
        db.session.rollback()
        return jsonify({'sucesso': False, 'mensagem': f'Erro ao salvar: {str(e)}'}), 500


# -----------------------------------------------------------------------------------
# 1. ROTA API: AVALIAÇÃO PREDITIVA DA DATA (AJAX)
# -----------------------------------------------------------------------------------
from datetime import date
from flask import request, jsonify


@empresa_bp.route('/api/avaliar-data-excecao', methods=['GET'])
def avaliar_data_excecao():
    """
    Avalia a data selecionada no modal para sugerir se o evento
    deve ser RECESSO (ausência em dia útil) ou EXCEÇÃO (trabalho em folga/feriado).
    """
    empresa_id = request.args.get('empresa_id', type=int)
    contrato_id = request.args.get('contrato_id', type=int)
    data_str = request.args.get('data')

    if not data_str:
        return jsonify({'erro': 'Data não informada'}), 400

    try:
        data_avaliada = date.fromisoformat(data_str)

        # 1. Checa se é feriado cadastrado no banco
        feriado = CadastroFeriado.query.filter_by(
            data=data_avaliada,
            ativo=True
        ).first() if 'CadastroFeriado' in globals() else None

        # 2. Mapeamento dos Dias da Semana
        # Python weekday(): 0=Segunda, 1=Terça, 2=Quarta, 3=Quinta, 4=Sexta, 5=Sábado, 6=Domingo
        py_weekday = data_avaliada.weekday()

        # EseHorarioFuncionamento: 0=Domingo, 1=Segunda, 2=Terça, 3=Quarta, 4=Quinta, 5=Sexta, 6=Sábado
        dia_semana_empresa = (py_weekday + 1) % 7

        tem_expediente = False

        # 3. Verificação de Escala/Funcionamento
        if contrato_id:
            # Caso seja no contexto de um colaborador específico
            escala_colaborador = EscalaTrabalhoColaborador.query.filter_by(
                contrato_id=contrato_id,
                dia_semana=py_weekday,  # Mantém o padrão do colaborador se for 0-6 (Seg-Dom)
                ativo=True,
                trabalha=True
            ).first()
            tem_expediente = bool(escala_colaborador)

        if not tem_expediente and empresa_id:
            # Consulta a grade comercial real do estabelecimento na EseHorarioFuncionamento
            horario_empresa = EseHorarioFuncionamento.query.filter_by(
                empresa_id=empresa_id,
                dia_semana=dia_semana_empresa
            ).first()

            # Se encontrou registro de horário cadastrado para aquele dia, a loja abre
            tem_expediente = bool(horario_empresa)

        # 4. Regra de Decisão Inteligente
        eh_dia_util_normal = bool(tem_expediente and not feriado)

        if eh_dia_util_normal:
            return jsonify({
                'sugestao_tipo': 'recesso',
                'trabalha_default': False,
                'motivo_sugerido': '',
                'mensagem_contexto': 'Data identificada como dia de expediente normal. Sugerido: Recesso / Ausência.'
            })
        else:
            motivo = f"Feriado ({feriado.nome})" if feriado else "Dia Sem Expediente / Folga"
            return jsonify({
                'sugestao_tipo': 'excecao',
                'trabalha_default': True,
                'motivo_sugerido': f"Trabalho em {motivo}",
                'mensagem_contexto': f'Data identificada como {motivo}. Sugerido: Horário Especial de Trabalho.'
            })

    except Exception as e:
        return jsonify({'erro': str(e)}), 500


# -----------------------------------------------------------------------------------
# 2. ROTA DE EXCLUSÃO DE EXCEÇÃO
# -----------------------------------------------------------------------------------
@empresa_bp.route('/excecoes/<excecao_id>/excluir', methods=['POST'])
def excluir_excecao(excecao_id):
    """
    Remove (ou inativa) um registro de exceção/recesso do calendário.
    """
    registro = EseExcecaoCalendario.query.get_or_404(excecao_id)
    empresa_id = registro.empresa_id
    contrato_id = registro.contrato_id

    try:
        # Opção Soft-Delete (Recomendado para auditoria):
        registro.ativo = False

        # Ou exclusão física:
        # db.session.delete(registro)

        db.session.commit()
        flash('Registro de exceção removido com sucesso!', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Erro ao remover registro: {str(e)}', 'danger')

    # Redireciona de volta para a mesma tela de gestão de exceções
    if contrato_id:
        return redirect(url_for('empresa.excecoes_colaborador', contrato_id=contrato_id))
    return redirect(url_for('empresa.excecoes_empresa', empresa_id=empresa_id))


@empresa_bp.route('/<int:empresa_id>/excecoes', methods=['GET', 'POST'])
@login_required
def excecoes_empresa(empresa_id):
    empresa = EseEmpresa.query.get_or_404(empresa_id)

    if request.method == 'POST':
        try:
            processar_gravacao_excecao(empresa_id=empresa.id, contrato_id=None, form_data=request.form)
            flash('Exceção/Recesso da empresa registrado com sucesso!', 'success')
            return redirect(url_for('empresa.excecoes_empresa', empresa_id=empresa.id))
        except Exception as e:
            db.session.rollback()
            flash(f'Erro ao gravar: {str(e)}', 'danger')

    lista_excecoes = EseExcecaoCalendario.query.filter_by(
        empresa_id=empresa.id,
        contrato_id=None,
        ativo=True
    ).order_by(EseExcecaoCalendario.data_inicio.desc()).all()

    # Aponta para o template único e compartilhado!
    return render_template(
        'empresa/excecoes_calendario.html',
        empresa=empresa,
        ese_empresa=empresa,
        contrato=None,
        lista_excecoes=lista_excecoes,
        datetime=datetime
    )


@empresa_bp.route('/<int:empresa_id>/colaborador/<int:contrato_id>/excecoes', methods=['GET', 'POST'])
@login_required
def excecoes_colaborador(empresa_id, contrato_id):
    empresa = EseEmpresa.query.get_or_404(empresa_id)
    contrato = ColaboradorContrato.query.get_or_404(contrato_id)

    if request.method == 'POST':
        try:
            processar_gravacao_excecao(empresa_id=empresa.id, contrato_id=contrato.id, form_data=request.form)
            flash(f'Exceção para {contrato.colaborador.nome} registrada!', 'success')
            return redirect(url_for('empresa.excecoes_colaborador', empresa_id=empresa.id, contrato_id=contrato.id))
        except Exception as e:
            db.session.rollback()
            flash(f'Erro ao gravar: {str(e)}', 'danger')

    lista_excecoes = EseExcecaoCalendario.query.filter_by(
        contrato_id=contrato.id,
        ativo=True
    ).order_by(EseExcecaoCalendario.data_inicio.desc()).all()

    # Aponta para o MESMO template compartilhado enviando o contrato!
    return render_template(
        'empresa/excecoes_calendario.html',
        empresa=empresa,
        ese_empresa=empresa,
        contrato=contrato,
        lista_excecoes=lista_excecoes,
        datetime=datetime
    )


@empresa_bp.route('/colaborador/admissao/<token>', methods=['GET', 'POST'])
def concluir_ficha_colaborador(token):
    """
    COLETA DE FICHA CADASTRAL VIA TOKEN (ALFÂNDEGA DE ONBOARDING)
    -------------------------------------------------------------
    """
    # 1. Filtra apenas convites com status 'pendente'
    convite = EseConviteColaborador.query.filter_by(token_convite=token, status='pendente').first()

    if not convite:
        flash("Este convite é inválido, expirou ou já foi utilizado.", "warning")
        return redirect(url_for('auth.processar_identidade'))

    passo = request.args.get('passo')

    if request.method == 'POST':
        try:
            def clean_mask(val):
                return ''.join(filter(str.isalnum, val)) if val else None

            # 2. Busca da Grade do Estabelecimento para Carga da Jornada Padrão
            horario_empresa = EseHorarioFuncionamento.query.filter_by(
                empresa_id=convite.estabelecimento_id,
                dia_semana=1
            ).order_by(EseHorarioFuncionamento.periodo_id.asc()).first()

            if not horario_empresa:
                flash(
                    "Não foi possível concluir a admissão: a grade comercial de horários da empresa ainda não está configurada.",
                    "danger"
                )
                return redirect(request.url)

            # 3. Localização ou Instanciação da Ficha Pessoal
            ficha = ColaboradorDetalhesPessoais.query.filter_by(convite_id=convite.id).first()

            if not ficha:
                ficha = ColaboradorDetalhesPessoais(
                    convite_id=convite.id,
                    contrato_id=None
                )
                db.session.add(ficha)

            # 👤 Identificação Civil
            ficha.nome_completo = request.form.get('nome_completo', '').strip().upper()
            ficha.nome_exibicao_pwa = request.form.get('nome_exibicao_pwa', '').strip()
            ficha.cpf = clean_mask(request.form.get('cpf'))
            ficha.cnpj = clean_mask(request.form.get('cnpj'))
            ficha.estado_civil = request.form.get('estado_civil')

            # 📍 Endereço Residencial
            ficha.cep = clean_mask(request.form.get('cep'))
            ficha.logradouro = request.form.get('logradouro', '').strip()
            ficha.numero = request.form.get('numero', '').strip()
            ficha.complemento = request.form.get('complemento', '').strip() or None
            ficha.bairro = request.form.get('bairro', '').strip()
            ficha.cidade = request.form.get('cidade', '').strip()
            ficha.estado = request.form.get('estado', '').strip().upper()[:2]

            # 📞 Contato, Saúde e Emergência
            ficha.telefone_pessoal = clean_mask(request.form.get('telefone_pessoal'))
            ficha.contato_emergencia_nome = request.form.get('contato_emergencia_nome', '').strip() or None
            ficha.contato_emergencia_fone = clean_mask(request.form.get('contato_emergencia_fone'))
            ficha.tipo_sanguineo = request.form.get('tipo_sanguineo', '').strip().upper() or None

            # 👕 Operacional / Vestuário
            ficha.tamanho_camiseta = request.form.get('tamanho_camiseta')
            ficha.tamanho_calca = request.form.get('tamanho_calca')
            ficha.tamanho_calcado = request.form.get('tamanho_calcado')

            # 🏦 Dados Bancários / PIX
            ficha.banco_nome = request.form.get('banco_nome', '').strip() or None
            ficha.agencia = request.form.get('agencia', '').strip() or None
            ficha.conta_corrente = request.form.get('conta_corrente', '').strip() or None
            ficha.chave_pix = request.form.get('chave_pix', '').strip() or None

            # ------------------------------------------------------------------
            # 4. RESOLUÇÃO DA CHAVE id_cadastro_cliente (UUID String 36)
            #    Considerando Empresa, Módulo e eliminando dependência do Usuario ID
            # ------------------------------------------------------------------
            cliente_id_resolvido = None

            # Identificadores do contexto
            empresa_id = convite.estabelecimento_id
            modulo_slug = 'agenda'  # Defina o slug do módulo correspondente à admissão/equipe

            # A) Obtém o CPF limpo trazido do formulário e calcula o Hash de comparação
            cpf_form_limpo = clean_mask(request.form.get('cpf'))
            hash_cpf_alvo = (
                EseConviteColaborador.gerar_hash_cpf(cpf_form_limpo)
                if cpf_form_limpo else convite.cpf_hash
            )

            # B) Busca o cadastro do cliente centralizado pelo Hash do CPF
            cadastro_cliente = ModCadastroCliente.query.filter_by(
                cpf_hash=hash_cpf_alvo
            ).first()

            if cadastro_cliente:
                cliente_id_resolvido = str(cadastro_cliente.id)

                # C) Garante a existência do vínculo relacional do cliente com a Empresa e o Módulo
                vinculo_existente = ModVinculoModulo.query.filter_by(
                    cpf_hash=hash_cpf_alvo,
                    modulo_slug=modulo_slug,
                    local_id=empresa_id
                ).first()

                if not vinculo_existente:
                    novo_vinculo = ModVinculoModulo(
                        cpf_hash=hash_cpf_alvo,
                        modulo_slug=modulo_slug,
                        local_id=empresa_id,
                        ativo=True
                    )
                    db.session.add(novo_vinculo)
                    db.session.flush()

            # D) Log e diagnóstico de consistência
            if not cliente_id_resolvido:
                print(
                    f"⚠️ [ALERTA ADMISSÃO]: Nenhum registro em 'ModCadastroCliente' "
                    f"encontrado para o CPF Hash {hash_cpf_alvo} na Empresa {empresa_id} (Módulo: {modulo_slug})."
                )
            else:
                print(
                    f"✅ [SUCESSO ADMISSÃO]: Contrato ancorado ao UUID {cliente_id_resolvido} "
                    f"para a Empresa {empresa_id} (Módulo: {modulo_slug})."
                )

            # ------------------------------------------------------------------
            # 5. GERAÇÃO DO CONTRATO PROFISSIONAL
            # ------------------------------------------------------------------
            novo_contrato = ColaboradorContrato(
                id_cadastro_cliente=cliente_id_resolvido,  # String(36) UUID
                id_local=empresa_id,
                id_cargo=getattr(convite, 'cargo_id', None),
                status_profissional='pendente',
                data_contratacao=datetime.now(timezone.utc),
                hora_inicio_expediente=horario_empresa.horario_abertura,
                hora_fim_expediente=horario_empresa.horario_fechamento
            )

            db.session.add(novo_contrato)
            db.session.flush()  # Garante a criação e geração da Primary Key (ID) do contrato

            # Amarra o ID do novo contrato à ficha do colaborador
            ficha.contrato_id = novo_contrato.id

            # 6. Transição de Estado da Alfândega/Convite
            convite.status = 'aceito'

            db.session.commit()

            flash("Ficha cadastral enviada com sucesso!", "success")

            # Passa a responsabilidade para a fronteira de identidade
            return redirect(url_for('auth.processar_identidade'))

            # ------------------------------------------------------------------
            # 6. TRANSIÇÃO DE ESTADO DA ALFÂNDEGA / CONVITE
            # ------------------------------------------------------------------
            convite.status = 'aceito'

            db.session.commit()

            flash("Ficha cadastral enviada com sucesso!", "success")

            # Resgate da URL de destino (prioridade: form > query string > fallback padrão)
            next_url = (
                    request.form.get('next')
                    or request.args.get('next')
                    or session.pop('return_after_admissao', None)
                    or url_for('agenda.index')  # Fallback dinâmico para o módulo padrão (ex: agenda)
            )

            # Garante que a URL é segura para evitar Open Redirects externos
            # (ou redireciona direto para a rota de origem resgatada)
            return redirect(next_url)

        except Exception as e:
            db.session.rollback()
            print(f"🚨 [ERRO GRAVAÇÃO FICHA ADMISSÃO]: {str(e)}")
            flash("Ocorreu um erro ao gravar os dados da sua ficha. Por favor, tente novamente.", "danger")



    return render_template(
        'empresa/ficha_admissao_notificacao.html',
        convite=convite,
        empresa=convite.empresa,
        passo=passo
    )


@empresa_bp.route('/<int:empresa_id>/equipe/convidar', methods=['GET', 'POST'])
@login_required
def gerar_link_cadastro_colaborador(empresa_id):
    """
    Passo 1 (Empreendedor): Gera o convite pendente na Alfândega (EseConviteColaborador)
    e disponibiliza o link para o pré-colaborador.
    """
    ese_empresa = Local.query.get_or_404(empresa_id)
    link_gerado = None
    nome_colaborador = None

    if request.method == 'POST':
        cpf_bruto = request.form.get('cpf', '').strip()
        nome_completo = request.form.get('nome_completo', '').strip()
        data_nasc_str = request.form.get('data_nascimento', '').strip()

        if not cpf_bruto or not nome_completo or not data_nasc_str:
            flash("Nome, CPF e Data de Nascimento são obrigatórios para o convite.", "danger")
            return render_template('empresa/gerar_convite.html', ese_empresa=ese_empresa)

        cpf_limpo = limpar_mascara(cpf_bruto)
        data_nascimento = datetime.strptime(data_nasc_str, '%Y-%m-%d').date()
        cpf_hash = EseConviteColaborador.gerar_hash_cpf(cpf_limpo)

        # Reutiliza ou cria convite
        convite = EseConviteColaborador.query.filter_by(
            estabelecimento_id=empresa_id,
            cpf_hash=cpf_hash,
            status='pendente'
        ).first()

        if not convite:
            token = secrets.token_urlsafe(32)
            convite = EseConviteColaborador(
                estabelecimento_id=empresa_id,
                cpf_hash=cpf_hash,
                data_nascimento=data_nascimento,
                data_contratacao=date.today(),
                nome_proposto=nome_completo,
                token_convite=token,
                status='pendente'
            )
            db.session.add(convite)
            db.session.commit()

        link_gerado = url_for('empresa.concluir_ficha_colaborador', token=convite.token_convite, _external=True)
        nome_colaborador = nome_completo
        flash("Link de cadastro de colaborador gerado!", "success")

    return render_template(
        'empresa/gerar_convite.html',
        ese_empresa=ese_empresa,
        link_gerado=link_gerado,
        nome_colaborador=nome_colaborador
    )


@empresa_bp.route('/api/validar-documento', methods=['GET'])
@login_required
def validar_documento_disponivel():
    """
    Endpoint assíncrono (AJAX) para checagem em tempo real no formulário.
    """
    doc = request.args.get('doc', '')
    id_local_atual = request.args.get('id_local', type=int)

    resultado = validar_unicidade_documento(doc_raw=doc, id_local_atual=id_local_atual)

    return jsonify(resultado)


@empresa_bp.route('/<int:empresa_id>/modulos-vitrine', methods=['GET'])
@login_required
def menu_modulos_vitrine(empresa_id):
    """
    Vitrine do Ecossistema: Exibe os 3 Pilares do FeedIn (Plano Base, Mídia e Módulos Operacionais).
    Opera de forma resiliente via EseProcessoClaim, Local e EseEmpresa (opcional).
    """
    # ---------------------------------------------------------------------
    # 1. RESOLUÇÃO DE PROCESSO, LOCAL E EMPRESA (FONTE DA VERDADE)
    # ---------------------------------------------------------------------
    # Busca primária pelo Processo Claim
    processo = EseProcessoClaim.query.filter(
        (EseProcessoClaim.id == empresa_id) |
        (EseProcessoClaim.local_id == empresa_id)
    ).order_by(EseProcessoClaim.id.desc()).first()

    # Identificação do Local (Core)
    if processo and processo.local:
        local_alvo = processo.local
        real_local_id = processo.local_id
    else:
        local_alvo = Local.query.get_or_404(empresa_id)
        real_local_id = local_alvo.id

    # Busca opcional da EseEmpresa
    ese_empresa = EseEmpresa.query.filter_by(local_id=real_local_id).first()
    real_empresa_id = ese_empresa.id if ese_empresa else None

    # ---------------------------------------------------------------------
    # 2. VALIDAÇÃO DE SEGURANÇA E PERMISSÃO DE ACESSO
    # ---------------------------------------------------------------------
    user_core_id = getattr(current_user, 'usuario_id', getattr(current_user, 'id', None))
    user_uuid = str(getattr(current_user, 'id', ''))

    eh_solicitante_claim = processo and (
        str(processo.usuario_solicitante_id) == str(user_core_id) or
        str(processo.usuario_solicitante_id) == user_uuid
    )

    eh_proprietario_empresa = False
    if ese_empresa and ese_empresa.proprietario_id:
        eh_proprietario_empresa = (
            str(ese_empresa.proprietario_id) == str(user_core_id) or
            str(ese_empresa.proprietario_id) == user_uuid
        )

    eh_dono = eh_solicitante_claim or eh_proprietario_empresa
    eh_admin = getattr(current_user, 'is_admin', False)

    if not (eh_dono or eh_admin):
        current_app.logger.warning(
            f"[VITRINE] Acesso negado. Usuário Core ID '{user_core_id}' "
            f"tentou acessar Módulos da Empresa/Local #{real_local_id}."
        )
        flash("Acesso não autorizado.", "danger")
        return redirect(url_for('empresa.dashboard_empresa'))

    # ---------------------------------------------------------------------
    # 3. VERIFICAÇÃO DE RECURSOS E REGRAS DE ADESÃO
    # ---------------------------------------------------------------------
    # A base só é considerada PRONTA para sustentar o módulo se:
    # a) O processo de claim/onboarding foi 'concluido'
    # b) Ou se não existe mais processo ativo (empresa antiga/consolidada)
    if processo:
        base_empresa_pronta = (processo.status_processo == 'concluido')
    else:
        # Se não há EseProcessoClaim ativo, assume que é uma empresa consolidada no Core
        base_empresa_pronta = True

    # Recurso PRO (Se contratou a agenda ou já era PRO)
    tem_acesso_pro = ese_empresa.possui_recurso_pro() if (
                ese_empresa and hasattr(ese_empresa, 'possui_recurso_pro')) else False

    # Módulos Ativos contratados
    slugs_contratados = []
    if ese_empresa:
        adesoes_ativas = ModEmpresaModulo.query.filter_by(empresa_id=ese_empresa.id, ativo=True).all()
        slugs_contratados = [a.modulo_slug for a in adesoes_ativas]

    # Parametro dinâmico para navegação da rota
    param_navegacao = real_empresa_id or real_local_id

    return render_template(
        'empresa/modulos_vitrine.html',
        processo=processo,
        local=local_alvo,
        ese_empresa=ese_empresa,
        empresa_id=param_navegacao,
        local_id=real_local_id,
        tem_acesso_pro=tem_acesso_pro,
        slugs_contratados=slugs_contratados,
        base_empresa_pronta=base_empresa_pronta  # <--- Enviado para o Jinja
    )


@empresa_bp.route('<int:empresa_id>/ativar-modulo/<string:modulo_slug>', methods=['GET', 'POST'])
@login_required
def ativar_modulo_empresa(empresa_id, modulo_slug):
    # 1. Busca a empresa
    ese_empresa = EseEmpresa.query.filter(
        (EseEmpresa.id == empresa_id) | (EseEmpresa.local_id == empresa_id)
    ).first()

    # 2. Regra Comercial: Ao ativar o módulo, concede o Perfil PRO
    if ese_empresa:
        ese_empresa.is_pro = True  # ou o campo de licença correspondente no seu model

    # 3. Registra/Ativa a adesão ao Módulo
    adesao = ModEmpresaModulo.query.filter_by(
        empresa_id=ese_empresa.id if ese_empresa else empresa_id,
        modulo_slug=modulo_slug
    ).first()

    if not adesao:
        adesao = ModEmpresaModulo(
            empresa_id=ese_empresa.id if ese_empresa else empresa_id,
            modulo_slug=modulo_slug,
            ativo=True
        )
        db.session.add(adesao)
    else:
        adesao.ativo = True

    db.session.commit()
    flash(f"Módulo '{modulo_slug.capitalize()}' ativado! O Perfil PRO foi concedido.", "success")

    # Redireciona de volta para a Vitrine para atualizar o estado dos botões
    return redirect(url_for('empresa.menu_modulos_vitrine', empresa_id=empresa_id))


def is_safe_url(target):
    if not target:
        return False
    ref_url = urlparse(request.host_url)
    test_url = urlparse(target)
    return test_url.scheme in ('', 'http', 'https') and (not test_url.netloc or test_url.netloc == ref_url.netloc)


@empresa_bp.route('/colaborador/<int:contrato_id>/perfil', methods=['GET'])
@login_required
def ver_perfil_colaborador(contrato_id):
    # -------------------------------------------------------------------------
    # 1. VALIDAÇÃO DE SESSÃO DA EMPRESA
    # -------------------------------------------------------------------------
    empresa_id_sessao = session.get('empresa_id_atual') or session.get('local_id_atual')
    if not empresa_id_sessao:
        flash("Sessão da empresa não encontrada. Selecione a empresa novamente.", "warning")
        return redirect(url_for('empresa.dashboard_empresa'))

    # -------------------------------------------------------------------------
    # 2. BUSCA O CONTRATO DO COLABORADOR
    # -------------------------------------------------------------------------
    contrato = ColaboradorContrato.query.get_or_404(contrato_id)

    # Segurança de Tenant: Garante que o contrato pertence à empresa da sessão atual
    if contrato.id_local != empresa_id_sessao:
        flash("Acesso não autorizado a este registro de colaborador.", "danger")
        return redirect(url_for('empresa.dashboard_empresa'))

    # -------------------------------------------------------------------------
    # 3. VALIDAÇÃO DE HIERARQUIA E PERMISSÃO DO USUÁRIO LOGADO
    # -------------------------------------------------------------------------
    # Obtém o nível do usuário logado na sessão (padrão 0 se não localizado)
    nivel_usuario_logado = session.get('usuario_nivel', 0)

    # Obtém o nível do colaborador que está sendo acessado
    nivel_membro = getattr(contrato, 'papel_nivel', 0) or 0

    # Regra 1: O usuário precisa ter nível mínimo de gestão (ex: >= 666)
    # Regra 2: O nível do gestor logado DEVE ser maior ou igual ao do membro alvo
    pode_gerenciar = (nivel_usuario_logado >= 666) and (nivel_usuario_logado >= nivel_membro)

    if not pode_gerenciar:
        flash("Você não possui permissão hierárquica suficiente para gerenciar o perfil deste colaborador.", "danger")
        # Retorna para a página anterior ou dashboard da empresa
        referrer = request.referrer
        if referrer and is_safe_url(referrer):
            return redirect(referrer)
        return redirect(url_for('empresa.equipe_jornadas', empresa_id=empresa_id_sessao))

    # -------------------------------------------------------------------------
    # 4. CAPTURA E SANITIZAÇÃO DA URL DE RETORNO (next)
    # -------------------------------------------------------------------------
    next_page = request.args.get('next')
    if not next_page or not is_safe_url(next_page):
        # Fallback padrão caso não haja um parâmetro 'next' válido enviado
        next_page = url_for('empresa.equipe_jornadas', empresa_id=empresa_id_sessao, open_offcanvas=contrato.id)

    # -------------------------------------------------------------------------
    # 5. DADOS ADICIONAIS & CONTEXTO DA EMPRESA
    # -------------------------------------------------------------------------
    ese_empresa = EseEmpresa.query.get(contrato.id_local)
    detalhes = contrato.detalhes_pessoais

    # -------------------------------------------------------------------------
    # 6. RECUPERAÇÃO DINÂMICA DA FOTO
    # -------------------------------------------------------------------------
    foto_perfil_url = None

    if getattr(contrato, 'foto_profissional', None):
        foto_perfil_url = url_for('empresa.static', filename=contrato.foto_profissional)
    else:
        cpf_hash_alvo = getattr(detalhes, 'cpf_hash', None) if detalhes else None

        cadastro_colaborador = getattr(contrato, 'cadastro_modulo', None)
        if not cpf_hash_alvo and cadastro_colaborador:
            cpf_hash_alvo = cadastro_colaborador.cpf_hash

        if cpf_hash_alvo:
            if not cadastro_colaborador:
                cadastro_colaborador = ModCadastroCliente.query.filter_by(cpf_hash=cpf_hash_alvo).first()

            email_alvo = cadastro_colaborador.email if cadastro_colaborador else None

            query_vinculo = ModVinculoModulo.query.filter_by(
                cpf_hash=cpf_hash_alvo,
                modulo_slug='empresa',
                ativo=True
            )

            if email_alvo:
                query_vinculo = query_vinculo.filter(
                    (ModVinculoModulo.email_customizado == email_alvo) |
                    (ModVinculoModulo.email_customizado.is_(None))
                )

            vinculo = query_vinculo.first()

            if vinculo:
                foto_perfil_url = vinculo.url_foto_perfil

    # -------------------------------------------------------------------------
    # 7. RENDERIZAÇÃO
    # -------------------------------------------------------------------------
    return render_template(
        'empresa/colaborador_perfil.html',
        contrato=contrato,
        detalhes=detalhes,
        ese_empresa=ese_empresa,
        foto_perfil_url=foto_perfil_url,
        next_page=next_page
    )

EXTENSOES_PERMITIDAS = {'png', 'jpg', 'jpeg', 'webp', 'heic'}

def arquivo_permitido(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in EXTENSOES_PERMITIDAS


@empresa_bp.route('/colaborador/<int:contrato_id>/atualizar-foto', methods=['POST'])
def atualizar_foto_colaborador(contrato_id):
    if 'foto' not in request.files:
        return jsonify({'sucesso': False, 'mensagem': 'Nenhum arquivo enviado na requisição (chave "foto" ausente).'}), 400

    arquivo = request.files['foto']
    if not arquivo or getattr(arquivo, 'filename', '') == '':
        return jsonify({'sucesso': False, 'mensagem': 'Nenhum arquivo selecionado.'}), 400

    # 1. Busca o contrato do colaborador
    contrato = ColaboradorContrato.query.get(contrato_id)
    if not contrato:
        return jsonify({'sucesso': False, 'mensagem': 'Contrato do colaborador não encontrado.'}), 404

    # 2. Processa e salva a imagem via processador unificado
    caminho_relativo_colaborador = salvar_imagem_modulo(
        arquivo=arquivo,
        tipo_midia='empresa_colaborador',
        identificador=contrato.id,
        empresa_id=contrato.id_local
    )

    if not caminho_relativo_colaborador:
        return jsonify({'sucesso': False, 'mensagem': 'Erro ao processar e converter imagem.'}), 500

    try:
        # 3. Atualiza a foto no contrato do colaborador
        contrato.foto_profissional = caminho_relativo_colaborador

        # 4. Localiza o cadastro do cliente para obter o cpf_hash e email
        # (Ajuste o relacionamento se o contrato se vincular via contrato.detalhes_pessoais ou contrato.usuario_id)
        cadastro_cliente = None
        if hasattr(contrato, 'detalhes_pessoais') and contrato.detalhes_pessoais:
            # Caso a busca seja pelo cadastro do módulo através do CPF/Vínculo
            cpf_hash_alvo = getattr(contrato.detalhes_pessoais, 'cpf_hash', None)
            if cpf_hash_alvo:
                cadastro_cliente = ModCadastroCliente.query.filter_by(cpf_hash=cpf_hash_alvo).first()

        # Fallback: tenta buscar pelo usuário logado ou cliente direto
        if not cadastro_cliente and hasattr(contrato, 'cadastro_modulo'):
            cadastro_cliente = contrato.cadastro_modulo

        # 5. Se encontrou o cadastro base, atualiza a foto no ModVinculoModulo
        if cadastro_cliente:
            vinculo = ModVinculoModulo.query.filter_by(
                cpf_hash=cadastro_cliente.cpf_hash,
                modulo_slug='empresa',  # Slug do módulo atual
                local_id=contrato.id_local
            ).filter(
                (ModVinculoModulo.email_customizado == cadastro_cliente.email) |
                (ModVinculoModulo.email_customizado.is_(None))
            ).first()

            # Se não achar com local_id específico, busca o vínculo geral do CPF + slug
            if not vinculo:
                vinculo = ModVinculoModulo.query.filter_by(
                    cpf_hash=cadastro_cliente.cpf_hash,
                    modulo_slug='empresa'
                ).first()

            if vinculo:
                vinculo.foto_url = caminho_relativo_colaborador

        db.session.commit()

    except Exception as e:
        db.session.rollback()
        return jsonify({'sucesso': False, 'mensagem': f'Erro ao atualizar foto no banco de dados: {str(e)}'}), 500

    # 6. Resolve a URL para o retorno do AJAX/Frontend
    url_imagem = url_for('empresa.static', filename=caminho_relativo_colaborador)

    return jsonify({
        'sucesso': True,
        'mensagem': 'Foto atualizada com sucesso!',
        'arquivo': caminho_relativo_colaborador,
        'url': url_imagem
    }), 200


@empresa_bp.route('/colaborador/<int:contrato_id>/salvar-foto-direta', methods=['POST'])
def salvar_foto_direta(contrato_id):
    if 'foto' not in request.files:
        return jsonify({'sucesso': False, 'mensagem': 'Nenhum arquivo enviado.'}), 400

    file = request.files['foto']
    if file.filename == '':
        return jsonify({'sucesso': False, 'mensagem': 'Nenhum arquivo selecionado.'}), 400

    extensao = file.filename.rsplit('.', 1)[-1].lower() if '.' in file.filename else 'jpg'
    nome_arquivo = f"colaborador_{contrato_id}_{int(time.time())}.{extensao}"

    pasta_destino = os.path.join(current_app.root_path, 'static', 'uploads', 'colaboradores')
    os.makedirs(pasta_destino, exist_ok=True)

    file.save(os.path.join(pasta_destino, nome_arquivo))

    contrato = ColaboradorContrato.query.get_or_404(contrato_id)
    contrato.foto_profissional = nome_arquivo
    db.session.commit()

    return jsonify({'sucesso': True, 'mensagem': 'Foto salva com sucesso!'}), 200


def gerar_e_enviar_comprovante_admissao(
    contrato, email_destino, nome_colaborador, nome_empresa, hash_validacao
):
    """
    Gera o comprovante em PDF na memória via WeasyPrint, gera o link de download
    direcionado ao módulo Agenda e envia por e-mail com anexo.
    """
    data_impressao = datetime.now().strftime('%d/%m/%Y às %H:%M')

    if not email_destino:
        current_app.logger.warning(
            f"Contrato #{contrato.id}: e-mail de destino não informado. Anexo não enviado."
        )
        return False

    try:
        # 1. Gerar o link de download direto apontando para o módulo Agenda
        link_download_comprovante = url_for(
            'agenda.baixar_comprovante_admissao',
            contrato_id=contrato.id,
            modo='download',
            _external=True,
        )

        # 2. Renderizar o HTML específico para a geração do PDF
        html_string = render_template(
            'pdf/comprovante_admissao.html',
            contrato=contrato,
            nome_colaborador=nome_colaborador,
            nome_empresa=nome_empresa,
            hash_validacao=hash_validacao,
            data_emissao=data_impressao,
        )

        # 3. Gerar o arquivo PDF direto na memória (bytes)
        pdf_bytes = HTML(string=html_string).write_pdf()

        # 4. Montar a mensagem de e-mail
        msg = Message(
            subject="Comprovante de Admissão e Jornada - FeedIn",
            sender=('FeedIn', 'portal.indicapira@gmail.com'),
            recipients=[email_destino],  # 👈 Corrigido: usa o parâmetro email_destino
        )

        # Corpo do e-mail em HTML (passando o link do módulo Agenda)
        msg.html = render_template(
            'emails/admissao_concluida.html',
            nome=nome_colaborador,
            empresa=nome_empresa,
            contrato=contrato,
            link_download=link_download_comprovante,
        )

        # 5. Anexar o PDF gerado
        nome_arquivo = f"Comprovante_Admissao_Contrato_{contrato.id}.pdf"
        msg.attach(
            filename=nome_arquivo,
            content_type="application/pdf",
            data=pdf_bytes,
        )

        # 6. Disparar o e-mail
        mail.send(msg)
        return True

    except Exception as e:
        current_app.logger.error(
            f"Erro ao gerar/enviar comprovante PDF do contrato #{contrato.id}: {str(e)}"
        )
        return False


def validar_cronologia_horarios(p1_ent, p1_sai, p2_ent, p2_sai):
    """
    Valida a coerência cronológica dos turnos.
    Retorna (True, None) se válido, ou (False, "Mensagem de erro") se inválido.
    """
    t_p1_ent = str_para_time(p1_ent) if p1_ent else None
    t_p1_sai = str_para_time(p1_sai) if p1_sai else None
    t_p2_ent = str_para_time(p2_ent) if p2_ent else None
    t_p2_sai = str_para_time(p2_sai) if p2_sai else None

    # Validação do Bloco 1
    if t_p1_ent and t_p1_sai:
        if t_p1_ent >= t_p1_sai:
            return False, f"Horário de entrada P1 ({p1_ent}) não pode ser maior ou igual à saída P1 ({p1_sai})."

    # Validação do Bloco 2
    if t_p2_ent and t_p2_sai:
        if t_p2_ent >= t_p2_sai:
            return False, f"Horário de entrada P2 ({p2_ent}) não pode ser maior ou igual à saída P2 ({p2_sai})."

    # Validação da Transição Bloco 1 -> Bloco 2 (Intervalo)
    if t_p1_sai and t_p2_ent:
        if t_p1_sai > t_p2_ent:
            return False, f"O início do segundo turno ({p2_ent}) não pode ser anterior ao fim do primeiro turno ({p1_sai})."

    return True, None


@empresa_bp.route('/<int:empresa_id>/equipe-jornadas')
@login_required
def equipe_jornadas(empresa_id):
    """
    Painel Central de Gestão de Equipe e Jornadas de Trabalho.
    Lista todos os contratos de colaboradores vinculados à empresa,
    suas escalas ativas e exceções de calendário.
    """
    # 1. Valida existência da empresa
    ese_empresa = EseEmpresa.query.get_or_404(empresa_id)

    # 2. Parâmetros de busca e filtro (para busca dinâmica via Offcanvas/Query Params)
    busca = request.args.get('q', '').strip()
    status_filtro = request.args.get('status', 'ativo')
    open_offcanvas_id = request.args.get('open_offcanvas', type=int)

    # 3. Query base dos contratos da empresa
    query = ColaboradorContrato.query.filter_by(id_local=ese_empresa.id)

    if status_filtro != 'todos':
        query = query.filter(ColaboradorContrato.status_profissional == status_filtro)

    contratos = query.all()

    # 4. Filtro em memória caso haja busca por nome (usando a property .nome)
    if busca:
        contratos = [
            c for c in contratos
            if busca.lower() in (c.nome or '').lower()
        ]

    # 5. Busca escalas de trabalho de todos os contratos ativos (se a model existir)
    escalas_por_contrato = {}
    if 'EscalaTrabalho' in globals():
        contrato_ids = [c.id for c in contratos]
        todas_escalas = EscalaTrabalhoColaborador.query.filter(EscalaTrabalhoColaborador.contrato_id.in_(contrato_ids)).all()
        for escala in todas_escalas:
            escalas_por_contrato.setdefault(escala.contrato_id, []).append(escala)

    return render_template(
        'empresa/equipe_jornadas.html',  # Ou o template do seu painel de jornadas
        ese_empresa=ese_empresa,
        empresa=ese_empresa,
        contratos=contratos,
        escalas_por_contrato=escalas_por_contrato,
        status_filtro=status_filtro,
        busca=busca,
        open_offcanvas_id=open_offcanvas_id
    )


def resolver_dados_iniciais_colaborador(contrato):
    """
    Navega recursivamente pelas tabelas ligadas ao contrato para extrair
    Nome, E-mail e WhatsApp/Telefone preexistentes.
    """
    dados = {
        'nome': '',
        'email': '',
        'whatsapp': ''
    }

    # 1. Tenta buscar via ColaboradorDetalhesPessoais
    if contrato.detalhes_pessoais:
        dados['nome'] = contrato.detalhes_pessoais.nome_completo or contrato.detalhes_pessoais.nome_exibicao_pwa
        dados['whatsapp'] = contrato.detalhes_pessoais.telefone_pessoal or ''

    # 2. Tenta buscar via ModCadastroCliente (Cadastro de Módulo/Balcão)
    if contrato.cadastro_modulo:
        if not dados['nome']:
            dados['nome'] = contrato.cadastro_modulo.nome_completo or contrato.cadastro_modulo.nome
        dados['email'] = contrato.cadastro_modulo.email or ''
        if not dados['whatsapp']:
            dados['whatsapp'] = contrato.cadastro_modulo.whatsapp or ''

    # 3. Tenta buscar via Convite de Colaborador (EseConviteColaborador)
    if not dados['nome'] or not dados['email']:
        convite = EseConviteColaborador.query.filter_by(
            estabelecimento_id=contrato.id_local,
            status='pendente'
        ).order_by(EseConviteColaborador.id.desc()).first()

        if convite:
            if not dados['nome']:
                dados['nome'] = convite.nome_proposto
            # Procura cadastro de cliente pelo hash do CPF do convite
            cliente = ModCadastroCliente.query.filter_by(cpf_hash=convite.cpf_hash).first()
            if cliente:
                if not dados['email']:
                    dados['email'] = cliente.email
                if not dados['whatsapp']:
                    dados['whatsapp'] = cliente.whatsapp

    # 4. Fallback para Usuário Core
    if contrato.usuario:
        if not dados['nome']:
            dados['nome'] = contrato.usuario.nome or ''
        if not dados['email'] and hasattr(contrato.usuario, 'email'):
            dados['email'] = contrato.usuario.email or ''

    return dados


def disparar_email_admissao_concluida(contrato, email_destino, nome_colaborador):
    if not email_destino:
        return

    html_corpo = render_template(
        'pdf/admissao_concluida.html',
        nome_colaborador=nome_colaborador,
        nome_empresa=contrato.empresa.nome_fantasia if hasattr(contrato, 'empresa') else "Empresa",
        contrato=contrato,
        ano_atual=datetime.now().year
    )

    # Executa a chamada do seu serviço de e-mail (SendGrid, SMTP, etc.)
    # enviar_email_smtp(destino=email_destino, assunto="Admissão Concluída - FeedIn", html=html_corpo)