import os
import re
import secrets
from functools import wraps
import requests  # Para fazer a chamada HTTP interna para o Auth
from datetime import datetime, timedelta, timezone

# 1. METODOLOGIAS DO FLASK & EXTENSÕES DE SESSÃO
from flask import current_app, flash, jsonify, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from werkzeug.utils import secure_filename

# 2. EXTENSÕES DE SEGURANÇA E CORE DO FEEDIN
from flask_bcrypt import check_password_hash, generate_password_hash  # Mantido apenas o do Bcrypt para consistência
from feedin import database as db, bcrypt  # Objetos centrais e extensões do Core
from feedin.middlewares import modulo_required

import feedin.utils as utils

# 3. 🧩 IMPORTAÇÃO DO CONTRATO OFICIAL DO BLUEPRINT (Sem recriá-lo!)
from feedin.modules.agenda import agenda_bp

# 4. FORMULÁRIOS (WTFORMS) - INTERNOS E INTER-MÓDULOS
from feedin.modules.agenda.forms import (
    FormHabilitarModulo,
    FormCadastroBalcao,
    FormColaborador,
    FormConfigMarca,
    FormCredenciamentoLocal,
    FormServico
)

# 5. 🗄️ PERSISTÊNCIA: MODELOS DO BANCO DE DADOS
# Modelos da Estrutura Base (Core)
from feedin.models import (
    HistoricoOcupacaoLocal, Local, Usuario, IdentidadeCivil,
    Epoca, VinculoUsuarioLocal, UsuarioLocalEpoca
)

# Modelos do módulo parceiro Auth
from feedin.modules.auth.models import (ModCadastroCliente, ModFilaAtivacaoCliente, AthAtribContexto)

# Modelos específicos do próprio módulo Agenda
from feedin.modules.agenda.models import AghAgendamento, AghProfissional, AghServico

# Modelos do módulo parceiro Empresa necessários para regras de negócio da agenda
from feedin.modules.empresa.models import EseEmpresa, UsuarioFavorito, ColaboradorContrato
from utils import preparar_entrada_modulo

@agenda_bp.route('/')
@preparar_entrada_modulo(slug_modulo='agenda', rota_destino='/agenda/negocios')
def portal_entrada_agenda():
    """Ponto de entrada único do módulo. Focado apenas na checagem de privilégio."""
    from flask import request, session

    # =========================================================================
    # 🎯 INJEÇÃO CIRÚRGICA: ALTERNÂNCIA DE VISIBILIDADE DO HUB NA AGENDA
    # =========================================================================
    # Captura o rastro se o usuário veio clicando no card do HUB
    if request.args.get('origem') == 'hub':
        session['navegacao_via_hub'] = True
    elif request.endpoint == 'agenda.portal_entrada_agenda' and not request.args.get('origem'):
        # Se ele entrou na URL seca da agenda direto por fora, limpa o rastro
        session.pop('navegacao_via_hub', None)
    # =========================================================================

    proximo_passo = session.get('next_url', '/agenda/negocios')

    # 🚀 Se já estiver logado com nível operacional, entra direto
    if current_user.is_authenticated and current_user.nivel_acesso >= 10:
        return redirect(proximo_passo)

    # 🎯 Despacha o usuário para a esteira centralizada do Auth
    return redirect(url_for('auth.login', next_url=proximo_passo))


@agenda_bp.route('/desafiar-senha/<int:cliente_id>', methods=['GET', 'POST'])
def desafiar_senha(cliente_id):
    """
    DESAFIO DE SENHA: Segunda etapa do login para quem já é cliente do módulo.
    Valida a senha e, se correta, inicializa a sessão e abre o Dashboard.
    """
    # 1. BUSCA O CLIENTE NO BANCO DA AGENDA
    cliente = ModCadastroCliente.query.get_or_404(cliente_id)

    # Se o cliente já estiver logado com essa mesma conta, manda direto pro painel
    if session.get('cliente_modulo_id') == cliente.id:
        return redirect(url_for('agenda.dashboard_cliente'))

    if request.method == 'POST':
        senha_digitada = request.form.get('senha_login', '')

        if not senha_digitada:
            flash("Por favor, digite sua senha.", "warning")
            return render_template('agenda/desafiar_senha.html', cliente=cliente)

        # 2. VALIDAÇÃO DA SENHA (HASH SEGURO)
        # Substitua por 'cliente.senha == senha_digitada' APENAS se ainda estiver em texto puro para testes
        if cliente and check_password_hash(cliente.senha, senha_digitada):

            # 3. ESTABELECIMENTO DA SESSÃO ISOLADA
            session['cliente_modulo_id'] = cliente.id
            session['usuario_nome'] = cliente.nome  # Mantém o nome acessível se necessário

            flash(f"Olá, {cliente.nome}! Acesso autorizado.", "success")

            # 4. DIRECIONAMENTO PARA O DASHBOARD QUE REVISAMOS
            return redirect(url_for('agenda.dashboard_cliente'))

        else:
            flash("Senha incorreta. Por favor, tente novamente.", "danger")

    # Se for GET, renderiza a tela passando o objeto cliente (para exibir o nome/foto dele se quiser)
    return render_template('agenda/desafiar_senha.html', cliente=cliente)

def gerar_username_unico(nome_completo):
    """
    Gera um username amigável (nome.sobrenome) baseado no nome completo.
    Se houver duplicidade na tabela mod_cadastro_cliente, adiciona um sufixo numérico.
    """
    nome_limpo = utils.limpar_string(nome_completo)
    partes = nome_limpo.split()

    # Define a base do username
    if len(partes) >= 2:
        username_base = f"{partes[0]}.{partes[1]}"
    elif len(partes) == 1:
        username_base = partes[0]
    else:
        username_base = "cliente"

    username_proposto = username_base
    contador = 1

    # Loop infinito seguro: só para quando encontra um username vago no banco
    while True:
        usuario_existente = ModCadastroCliente.query.filter_by(username_modulo=username_proposto).first()
        if not usuario_existente:
            return username_proposto

        # Se o username já existir, adiciona o número atual e incrementa para a próxima tentativa
        username_proposto = f"{username_base}{contador}"
        contador += 1


def gerar_token_cadastro(cpf):
    """Gera o token assinado usando a SECRET_KEY do app."""
    serializador = URLSafeTimedSerializer(current_app.config["SECRET_KEY"])
    return serializador.dumps(cpf, salt="ativacao-modulo-cliente")


@agenda_bp.route('/balcao/cadastrar', methods=['GET', 'POST'])
def cadastro_balcao():
    form = FormCadastroBalcao()

    if form.validate_on_submit():
        username_automatico = gerar_username_unico(form.nome.data)
        senha_temporaria = str(secrets.randbelow(900000) + 100000)
        senha_hash = bcrypt.generate_password_hash(senha_temporaria).decode('utf-8')

        # Converte a string "dd/mm/aaaa" vinda do formulário para um objeto Date do Python
        try:
            data_convertida = datetime.strptime(form.data_nascimento.data, '%d/%m/%Y').date()
        except ValueError:
            flash("Formato de data inválido. Use dd/mm/aaaa.", "danger")
            return render_template('agenda/cadastro_balcao.html', form=form)

        novo_cliente = ModCadastroCliente(
            nome=form.nome.data,
            cpf=form.cpf.data,
            whatsapp=form.whatsapp.data,
            email=form.email.data,
            data_nascimento=data_convertida,  # Grava o objeto correto no banco
            username_modulo=username_automatico,
            senha_modulo_hash=senha_hash
        )

        try:
            db.session.add(novo_cliente)
            db.session.commit()

            # --- PREPARAÇÃO PARA O DISPARO (WHATSAPP / EMAIL) ---
            # Aqui deixamos printado no log do Flask as credenciais que seriam enviadas.
            # Em poucos dias, conectaremos a API de disparo aqui usando esses dados.
            print("\n" + "=" * 50)
            print(f"CLIENTE CADASTRADO NO BALCÃO COM SUCESSO!")
            print(f"Nome: {novo_cliente.nome}")
            print(f"Username Gerado: {username_automatico}")
            print(f"Senha Temporária: {senha_temporaria}")
            print(f"Disparar para WhatsApp: {novo_cliente.whatsapp}")
            print(f"Disparar para E-mail: {novo_cliente.email}")
            print("=" * 50 + "\n")

            flash(f"Cliente {novo_cliente.nome} cadastrado! Usuário: {username_automatico} | Senha: {senha_temporaria}",
                  "success")
            return redirect(url_for('agenda.cadastro_balcao'))

        except Exception as e:
            db.session.rollback()
            flash("Erro crítico ao salvar o cliente no banco de dados paralelo.", "danger")
            # Loga o erro real no seu arquivo feedin.log automaticamente
            from flask import current_app
            current_app.logger.error(f"Erro no cadastro de balcão: {str(e)}")

    return render_template('agenda/cadastro_balcao.html', form=form)


@agenda_bp.route('/painel')
@modulo_required # 🔒 1. Ninguém entra sem login ativo no Core
def painel_agenda():
    # 🛡️ 2. INTERCEPTAÇÃO DA ALFÂNDEGA UNIVERSAL
    # Se o usuário está logado, mas não passou pela validação civil (LGPD),
    # ele é empurrado para a tela autônoma e o painel não é processado.
    if not current_user.aceite_lgpd:
        # Passamos request.url para que, após se validar, ele volte exatamente para cá!
        return redirect(url_for('auth.validar_identidade_tela', next_url=request.url))

    # -------------------------------------------------------------------------
    # O restante do seu código original permanece intocado e seguro abaixo:
    # -------------------------------------------------------------------------
    agora = datetime.now()
    limite_trinta_dias = agora + timedelta(days=30)
    limite_trinta_sete_dias = agora + timedelta(days=37)

    # 1. Busca agendamentos normais dentro da faixa de 30 dias
    agendamentos_ativos = AghAgendamento.query.filter(
        AghAgendamento.data_hora_inicio >= agora,
        AghAgendamento.data_hora_inicio <= limite_trinta_dias,
        AghAgendamento.status == 'confirmado'
    ).order_by(AghAgendamento.data_hora_inicio.asc()).all()

    # 2. Busca reagendamentos pendentes dentro da janela de segurança (37 dias)
    reagendamentos_pendentes = AghAgendamento.query.filter(
        AghAgendamento.status == 'reagendamento_pendente',
        AghAgendamento.data_hora_inicio <= limite_trinta_sete_dias
    ).order_by(AghAgendamento.data_solicitacao_reagendamento.asc()).all()

    # 3. Busca quem "perdeu" o prazo de 7 dias, mas permite reversão manual
    reagendamentos_perdidos = AghAgendamento.query.filter(
        AghAgendamento.status == 'perdido'
    ).all()

    return render_template(
        'agenda/painel_agenda.html',
        agendamentos=agendamentos_ativos,
        pendentes=reagendamentos_pendentes,
        perdidos=reagendamentos_perdidos,
        agora=agora
    )


# Rota para o Empreendedor reverter a perda de prazo do cliente (Exceção à regra)
@agenda_bp.route('/reagendamento/reverter/<int:id>')
def reverter_prazo(id):
    agendamento = AghAgendamento.query.get_or_404(id)
    # Reverte o status para pendente, dando nova chance após contato pessoal
    agendamento.status = 'reagendamento_pendente'
    agendamento.data_solicitacao_reagendamento = datetime.now()  # Renova o relógio por mais 7 dias
    db.session.commit()
    flash(f"Prazo do cliente {agendamento.cliente.nome} revertido com sucesso!", "success")
    return redirect(url_for('agenda.painel_agenda'))


@agenda_bp.route('/servicos', methods=['GET', 'POST'])
def gerenciar_servicos():
    form = FormServico()

    # Substitua pelo ID do usuário logado quando o login manager estiver integrado ao módulo
    estabelecimento_id_atual = 1

    if form.validate_on_submit():
        novo_servico = AghServico(
            estabelecimento_id=estabelecimento_id_atual,
            nome=form.nome.data,
            preco=form.preco.data,
            duracao_minutos=form.duracao_minutos.data,
            descricao=form.descricao.data,
            exibir_descricao_pwa=form.exibir_descricao_pwa.data
        )

        db.session.add(novo_servico)
        db.session.commit()
        flash(f"Serviço '{novo_servico.nome}' cadastrado com sucesso!", "success")
        return redirect(url_for('agenda.gerenciar_servicos'))

    # Busca todos os serviços já cadastrados por esse estabelecimento para listar na tela
    servicos_cadastrados = AghServico.query.filter_by(
        estabelecimento_id=estabelecimento_id_atual,
        is_ativo=True
    ).order_by(AghServico.nome.asc()).all()

    return render_template(
        'agenda/gerenciar_servicos.html',
        form=form,
        servicos=servicos_cadastrados
    )


@agenda_bp.route('/empresa/favoritar/<int:empresa_id>', methods=['POST'])
def toggle_favorito_empresa(empresa_id):
    # Temporariamente fixando o ID do usuário como 1 até plugar o login_required do Flask-Login
    usuario_logado_id = 1

    # Verifica se esse favorito já existe no banco
    favorito_existente = UsuarioFavorito.query.filter_by(
        usuario_id=usuario_logado_id,
        empresa_id=empresa_id
    ).first()

    if favorito_existente:
        # Se já existia e ele clicou de novo, significa que quer desfavoritar
        db.session.delete(favorito_existente)
        db.session.commit()
        return jsonify({"status": "removido", "mensagem": "Removido dos favoritos"})
    else:
        # Se não existia, adiciona o novo favorito
        novo_favorito = UsuarioFavorito(
            usuario_id=usuario_logado_id,
            empresa_id=empresa_id
        )
        db.session.add(novo_favorito)
        db.session.commit()
        return jsonify({"status": "adicionado", "mensagem": "Adicionado aos favoritos com sucesso!"})


@agenda_bp.route('/negocios')
def home_negocios():
    """Renderiza a central de atendimento urbana (Listagem de Empresas)"""

    # 🪐 1. VALIDAÇÃO DE CONTEXTO EXCLUSIVA DA AGENDA
    if 'cliente_modulo_id' not in session:
        # Não há identidade da Agenda ativa. Força o login direto no módulo.
        session['modulo_slug_atual'] = 'agenda'
        session['next_url'] = '/agenda/negocios'

        flash("Por favor, identifique-se para acessar a Agenda.", "warning")
        return redirect(url_for('auth.login', next_url='/agenda/negocios'))

    # =========================================================================
    # 🏙️ 2. CONTEXTO OPERACIONAL ATIVO (Usuário autenticado na Agenda)
    # =========================================================================
    # Garante os estados padrão de navegação na sessão da Agenda
    if 'modo_visao' not in session:
        session['modo_visao'] = 'cliente'
    if 'nivel_acesso_atual' not in session:
        session['nivel_acesso_atual'] = 10

    cliente_id = session.get('cliente_modulo_id')
    cliente_operacional = ModCadastroCliente.query.get(cliente_id)

    # Prevenção caso o registro tenha sido deletado do banco manualmente
    if not cliente_operacional:
        session.pop('cliente_modulo_id', None)
        return redirect(url_for('auth.login', next_url='/agenda/negocios'))

    # 🏢 LÓGICA DE NEGÓCIOS: Busca as empresas locais
    empresas_locais = []  # Sua lógica de busca de locais ativos vai aqui

    # =========================================================================
    # 🎨 3. ENTREGA DA RENDERIZAÇÃO FINAL
    # =========================================================================
    return render_template(
        'agenda/home_negocios.html',
        cliente=cliente_operacional,
        empresas=empresas_locais,
        cidade="Piracicaba",
        estado="SP",
        favoritos=[],
        notificacoes=[]
    )


@agenda_bp.route('/logout')
def logout():
    """
    Limpa o contexto da Agenda e decide o destino:
    Se veio pelo HUB, volta para o HUB. Se usa atalho direto, vai para o login.
    """
    # 1. Identifica se o usuário tem a bandeira de que está navegando através do HUB
    # Essa bandeira 'veio_do_hub' será injetada na session assim que ele clica no card
    veio_do_hub = session.get('navegacao_via_hub', False)

    # 2. Limpa todas as chaves operacionais e de identidade da Agenda (Seu código original)
    session.pop('cliente_modulo_id', None)
    session.pop('modo_visao', None)
    session.pop('nivel_acesso_atual', None)
    session.pop('local_contexto_id', None)
    session.pop('empresa_ativa_id', None)
    session.pop('next_url', None)

    # Removemos a própria bandeira para não poluir os próximos logins
    session.pop('navegacao_via_hub', None)

    # 3. Garante que o slug atual aponta para a agenda para guiar o ecossistema
    session['modulo_slug_atual'] = 'agenda'

    flash("Você saiu do painel da agenda.", "success")

    # 4. 🔀 A CONDICIONAL INTELIGENTE:
    if veio_do_hub:
        # Se ele estava usando o ecossistema integrado, o "Sair" apenas fecha o módulo e volta ao concentrador
        return redirect(url_for('central_hub'))
    else:
        # Se ele acessou pelo atalho direto "por fora", mantém o comportamento padrão de ir para o login
        return redirect(url_for('auth.login', next_url='/agenda/negocios'))


@agenda_bp.route('/balcao/sair')
@modulo_required
def sair_modo_balcao():
    """
    Desativa o modo interno do lojista (funcionário/gerente)
    e o devolve pacificamente para a visão urbana como cliente.
    """
    # 1. Limpa o contexto operacional de balcão da sessão
    session.pop('modo_visao', None)
    session.pop('nivel_acesso_atual', None)
    session.pop('papel_nome', None)
    session.pop('cargo_institucional', None)

    # 2. Resgata e limpa o ID do local onde ele estava operando
    # (Ajustado para ler 'local_contexto_id' que usamos na rota /negocios)
    id_local_atual = session.pop('local_contexto_id', None)

    flash("Você saiu do modo interno e retornou para a visão da cidade.", "info")

    # 3. Decisão inteligente de destino pós-saída
    if id_local_atual:
        # Se ele estava em um local, devolve ele para a página pública desse comércio
        # (Ajuste o nome da rota se no seu sistema for 'detalhe_local' ou similar)
        return redirect(url_for('agenda.detalhe_empresa', empresa_id=id_local_atual))

    # Caso contrário, joga ele na listagem geral de Piracicaba
    return redirect(url_for('agenda.home_negocios'))


@agenda_bp.route('/api/busca-autocomplete')
def busca_autocomplete():
    """Retorna empresas para o input inteligente do JS baseado no modelo EseEmpresa"""
    termo = request.args.get('q', '').strip()

    if not termo or len(termo) < 2:
        return jsonify([])  # Protege o SQLite de buscas pesadas com 1 letra

    # Buscando na tabela EseEmpresa criada hoje cedo na Fase 1
    # Filtra por nome ou categoria trazendo apenas os 8 primeiros para manter o PWA ágil
    resultados = EseEmpresa.query.filter(
        (EseEmpresa.nome.ilike(f'%{termo}%')) |
        (EseEmpresa.categoria.ilike(f'%{termo}%'))
    ).limit(8).all()

    # Formatação do JSON respeitando os campos que você mapeou na Fase 1
    sugestoes = []
    for empresa in resultados:
        sugestoes.append({
            'id': empresa.id,
            'nome': empresa.nome,
            'categoria': empresa.categoria,
            'slug': empresa.slug,  # ex: 'salao-do-ze' para navegação direta
            'logomarca': empresa.logomarca or '/static/img/default-logo.png'
        })

    return jsonify(sugestoes)


@agenda_bp.route('/gerencial/marca', methods=['GET', 'POST'])
def gerenciar_marca():
    """Painel do Empreendedor: Customização de Identidade Visual e Cores"""
    estabelecimento_id_atual = 1
    empresa = EseEmpresa.query.get_or_404(estabelecimento_id_atual)

    form = FormConfigMarca(obj=empresa)  # Já pré-carrega os dados existentes no banco

    if form.validate_on_submit():
        empresa.nome = form.nome.data
        empresa.categoria = form.categoria.data
        empresa.cor_primaria = form.cor_primaria.data
        empresa.cor_secundaria = form.cor_secundaria.data

        db.session.commit()
        flash("Identidade de marca atualizada com sucesso!", "success")
        return redirect(url_for('agenda.gerenciar_marca'))

    return render_template('agenda/gerenciar_marca.html', form=form, empresa=empresa)


@agenda_bp.route('/gerencial/equipe', methods=['GET', 'POST'])
def gerenciar_equipe():
    """Painel do Empreendedor: Cadastro e Listagem de Colaboradores"""
    estabelecimento_id_atual = 1
    form = FormColaborador()

    if form.validate_on_submit():
        novo_profissional = AghProfissional(
            estabelecimento_id=estabelecimento_id_atual,
            nome=form.nome.data,
            cargo_especialidade=form.cargo_especialidade.data,
            is_ativo=True
        )
        db.session.add(novo_profissional)
        db.session.commit()
        flash(f"Profissional '{novo_profissional.nome}' adicionado à equipe!", "success")
        return redirect(url_for('agenda.gerenciar_equipe'))

    # Busca a equipe atual para listar na tabela da tela
    equipe = AghProfissional.query.filter_by(
        estabelecimento_id=estabelecimento_id_atual,
        is_ativo=True
    ).order_by(AghProfissional.nome.asc()).all()

    return render_template('agenda/gerenciar_equipe.html', form=form, equipe=equipe)


@agenda_bp.route('/balcao/credenciamento', methods=['GET', 'POST'])
@modulo_required
def credenciamento_balcao():
    empreendedor_id = current_user.id
    form = FormCredenciamentoLocal()

    if form.validate_on_submit():
        id_existente = form.id_local_existente.data

        try:
            # 1. TRATATIVA DO PONTO FÍSICO (LOCAL)
            if id_existente:
                local = Local.query.get(id_existente)
                if not local:
                    flash("Ponto físico selecionado inválido.", "danger")
                    return redirect(url_for('agenda.credenciamento_balcao'))
            else:
                local = Local()
                local.data_cadastro = datetime.now(timezone.utc)
                db.session.add(local)

            # População dos dados físicos do Local
            local.nome = form.nome_ponto_fisico.data.strip() if hasattr(form,
                                                                        'nome_ponto_fisico') else form.nome.data.strip()
            local.documento = form.documento.data.strip() if form.documento.data else None
            local.cep = form.cep.data.strip()
            local.logradouro = form.logradouro.data.strip()
            local.numero = form.numero.data.strip()
            local.bairro = form.bairro.data.strip()
            local.cidade = form.cidade.data.strip()
            local.estado = form.estado.data.upper() if form.estado.data else 'SP'
            local.telefone = form.telefone.data.strip()
            local.is_whatsapp = form.is_whatsapp.data

            local.id_empreendedor = empreendedor_id  # Dono do "Prédio"
            local.esta_ativo = True
            local.status_operacional = 'ativo'

            # Gravação do histórico de ocupação
            ocupacao = HistoricoOcupacaoLocal(
                local=local,
                id_empreendedor=empreendedor_id,
                plano_contratado=local.plano_marketing
            )
            db.session.add(ocupacao)

            # Força o flush para o banco gerar o ID do local se ele for novo,
            # sem fechar a transação com commit ainda
            db.session.flush()

            # 2. TRATATIVA DA EMPRESA (O INQUILINO DA AGENDA)
            # Criamos a empresa comercial vinculada ao usuário e ao local gerado/escolhido
            nova_empresa = EseEmpresa(
                proprietario_id=empreendedor_id,
                local_id=local.id,
                nome=form.nome.data.strip(),  # Nome comercial da Marca
                categoria=form.categoria.data.strip() if hasattr(form, 'categoria') else "Geral",
                slug=form.slug.data.strip() if hasattr(form, 'slug') else f"empresa-{local.id}"
            )
            db.session.add(nova_empresa)

            # Salva tudo de forma atômica (Se um falhar, nenhum entra)
            db.session.commit()

            flash("Estabelecimento e Empresa estruturados com sucesso no FeedIn!", "success")
            return redirect(url_for('agenda.dashboard'))

        except Exception as e:
            db.session.rollback()
            print(f"Erro crítico no credenciamento: {e}")
            flash("Ocorreu um erro interno ao processar o credenciamento.", "danger")

    return render_template('agenda/credenciamento.html', form=form)


@agenda_bp.route('/empresa/<int:empresa_id>')
def detalhe_empresa(empresa_id):
    """
    Exibe o perfil público da empresa (Visão do Cliente).
    Valida as permissões do usuário logado cruzando o Core com a Agenda de forma segura.
    """
    # 1. Busca a empresa na base do módulo (que agora conhece o seu proprietario_id e local_id)
    empresa = EseEmpresa.query.get_or_404(empresa_id)

    # 2. Resgata o usuário logado através do Core (Substituir pelo seu sistema real de login ex: current_user)
    usuario_id_teste = 1
    usuario_logado = Usuario.query.get(usuario_id_teste)

    is_colaborador = False
    nivel_neste_local = 10  # Padrão: Usuário comum / Cliente

    if usuario_logado:
        # 👑 NÍVEL SOBERANO: Verifica se ele é o Dono Supremo desta empresa específica
        if empresa.proprietario_id == usuario_logado.id:
            is_colaborador = True
            nivel_neste_local = 999
        else:
            # 👔 NÍVEL OPERACIONAL: Busca o contrato de trabalho ativo dele no ponto físico correspondente
            # e valida se ele está cadastrado como profissional desta Empresa específica na Agenda
            vinculo_profissional = AghProfissional.query.join(ColaboradorContrato).filter(
                AghProfissional.estabelecimento_id == empresa.id,
                ColaboradorContrato.id_usuario == usuario_logado.id,
                ColaboradorContrato.status_profissional == 'ativo',
                ColaboradorContrato.data_desligamento.is_(None),
                AghProfissional.is_ativo == True
            ).first()

            if vinculo_profissional:
                is_colaborador = True
                # Aqui você herda o nível ou o cargo mapeado no contrato dele do Core
                # Como a rota espera uma métrica, podemos capturar o ID do cargo ou um padrão operacional
                nivel_neste_local = 888  # Ex: Nível de Colaborador Habilitado

    return render_template(
        'agenda/detalhe_empresa.html',
        empresa=empresa,
        is_colaborador=is_colaborador,
        nivel_neste_local=nivel_neste_local
    )


@agenda_bp.route('/empresa/<int:empresa_id>/entrar-balcao')
def entrar_modo_balcao(empresa_id):
    """
    Gatilho acionado pelo botão físico. Altera o estado da sessão
    e ativa o ecossistema administrativo.
    """
    usuario_id_teste = 1
    usuario_logado = Usuario.query.get(usuario_id_teste)
    empresa = EseEmpresa.query.get_or_404(empresa_id)

    # Validação de segurança baseada na nossa matriz de níveis
    # Para entrar no balcão, precisa ter nível de funcionário ou dono (>= 300, conforme sua tabela)
    # Aqui verificamos o nível contextual dele nesta empresa
    is_autorizado = False
    nivel_atribuido = 10

    if empresa.proprietario_id == usuario_logado.id and usuario_logado.nivel_acesso == 999:
        is_autorizado = True
        nivel_atribuido = 999
    else:
        vinculo = ColaboradorContrato.query.filter_by(
            empresa_id=empresa.id, usuario_id=usuario_logado.id, is_ativo=True
        ).first()
        if vinculo and vinculo.nivel_acesso >= 300:  # Ex: De assistente para cima
            is_autorizado = True
            nivel_atribuido = vinculo.nivel_acesso

    if not is_autorizado:
        flash("Seu nível de acesso atual não permite gerenciar este estabelecimento.", "danger")
        return redirect(url_for('agenda.detalhe_empresa', empresa_id=empresa_id))

    # Grava o contexto na sessão do Flask
    session['modo_visao'] = 'balcao'
    session['empresa_ativa_id'] = empresa.id
    session['nivel_acesso_atual'] = nivel_atribuido
    session['usuario_is_pioneiro'] = usuario_logado.is_pioneiro  # Carrega a flag global de Pioneiro

    # Redireciona para o painel gerencial que você já tem funcional
    return redirect(url_for('agenda.painel_agenda'))


@agenda_bp.route('/identificar', methods=['GET', 'POST'])
def identificar_usuario():
    if request.method == 'GET':
        return redirect(url_for('auth.login', next_url=request.url))

    email_digitado = request.form.get('email_login', '').strip().lower()

    if not email_digitado:
        flash("Por favor, informe seu E-mail para continuar.", "warning")
        return redirect(url_for('agenda.identificar_usuario'))

    # Limpamos resíduos antigos da agenda na sessão para garantir uma entrada limpa
    session.pop('cliente_modulo_id', None)
    session.pop('modo_visao', None)

    # 🔍 CAMADA 1: O usuário já é um cliente cadastrado no universo da Agenda?
    cliente_oficial = ModCadastroCliente.query.filter_by(email=email_digitado).first()
    if cliente_oficial:
        return redirect(url_for('agenda.desafiar_senha', cliente_id=cliente_oficial.id))

    # 🔍 CAMADA 2: O e-mail está na Fila de Ativação do Balcão da Agenda (Limbo)?
    cliente_fila = ModFilaAtivacaoCliente.query.filter_by(email=email_digitado).first()
    if cliente_fila:
        flash("Seu cadastro foi iniciado no balcão! Por favor, acesse o link enviado para criar sua senha de acesso.",
              "info")
        return redirect(url_for('agenda.identificar_usuario'))

    # 🔍 CAMADA 3: Não está na Agenda, mas possui conta no Core? (Atrelo Silencioso)
    # Buscamos APENAS a entidade básica para capturar o ID e fazer o vínculo
    usuario_core = Usuario.query.filter_by(email=email_digitado).first()

    if usuario_core:
        # Criamos o registro na tabela de Clientes da Agenda isolando as informações necessárias
        # Se o campo de nome no seu Core for diferente, o próprio script captura dinamicamente aqui
        nome_usuario = getattr(usuario_core, 'nome', getattr(usuario_core, 'nome_completo', 'Usuário Central'))

        # Geramos um username exclusivo para a operação deste módulo
        username_novo = gerar_username_unico(nome_usuario)

        novo_cliente = ModCadastroCliente(
            usuario_id=usuario_core.id,  # O único elo real com o Core guardado aqui
            nome=nome_usuario,
            email=usuario_core.email,
            username_modulo=username_novo,
            # Se existirem dados de CPF no core, replica de forma estática sem herança viva
            cpf_hash=usuario_core.identidade_civil.cpf_hash if getattr(usuario_core, 'identidade_civil',
                                                                       None) else None,
            cpf_criptografado=usuario_core.identidade_civil.cpf_criptografado if getattr(usuario_core,
                                                                                         'identidade_civil',
                                                                                         None) else None
        )

        db.session.add(novo_cliente)
        db.session.commit()

        flash("Identificamos seu perfil urbano! Defina uma senha para acessar o painel de serviços.", "success")
        return redirect(url_for('agenda.definir_senha_nova', cliente_id=novo_cliente.id))

    # 🔍 CAMADA 4: E-mail totalmente novo (Cadastro Orgânico na Agenda)
    return redirect(url_for('agenda.cadastro_organico_novo', email_inicial=email_digitado))


@agenda_bp.route('/agenda/autenticar/sucesso')
def redireciona_por_perfil():
    """
    Controlador central de tráfego pós-login.
    Garante que cada perfil caia exatamente no seu ambiente de direito.
    """
    # Resgata o nível contextualizado na sessão
    nivel = session.get('nivel_acesso_atual', 10)  # 10 = Cidadão Padrão
    modo_visao = session.get('modo_visao', 'cliente')

    # Perfil 1: O Dono da Empresa/Empreendedor Supremo (999)
    if nivel == 999:
        # Se ele escolheu o Modo Balcão, vai para o gerenciamento interno
        if modo_visao == 'balcao':
            return redirect(url_for('agenda.painel_agenda'))
        # Se ele quer apenas navegar, vai para a home de negócios tradicional
        return redirect(url_for('agenda.home_negocios'))

    # Perfil 2: Corpo Técnico/Colaboradores Operacionais (De 666 a 888)
    elif nivel >= 666:
        # Funcionários caem direto no cockpit de atendimento para trabalhar
        session['modo_visao'] = 'balcao'
        return redirect(url_for('agenda.painel_agenda'))

    # Perfil 3: O Cidadão/Consumidor Comum (Nível 10)
    else:
        session['modo_visao'] = 'cliente'
        # Cai direto na tela de agendamentos dele (Meus Horários) ou na vitrine de Piracicaba
        return redirect(url_for('agenda.home_negocios'))


@agenda_bp.route('/habilitar-cliente', methods=['GET', 'POST'])
def habilitar_cliente_no_modulo():
    proximo_passo = request.args.get('next_url', '') or request.form.get('next_url', '')
    form = FormHabilitarModulo()

    if form.validate_on_submit():
        cpf_digitado = re.sub(r'\D', '', form.cpf.data)
        email_modulo = form.email.data.strip().lower()
        senha_modulo = form.senha.data
        nome_modulo = form.nome.data.strip()
        whatsapp_modulo = form.whatsapp.data.strip()

        try:
            hash_civil = IdentidadeCivil.gerar_hash(cpf_digitado)
            identidade_core = IdentidadeCivil.query.filter_by(cpf_hash=hash_civil).first()
            usuario_id_capturado = identidade_core.usuario_id if identidade_core else None

            cliente_existente = ModCadastroCliente.query.filter_by(email=email_modulo).first()
            if cliente_existente:
                flash("Este e-mail já está ativo. Prossiga com seu login.", "info")
                return redirect(url_for('auth.login', next_url=proximo_passo))

            novo_cliente = ModCadastroCliente(
                usuario_id=usuario_id_capturado,
                nome=nome_modulo,
                email=email_modulo,
                whatsapp=whatsapp_modulo,
                username_modulo=email_modulo,
                senha_hash=generate_password_hash(senha_modulo),
                status_conta='ativo'
            )
            novo_cliente.cpf = cpf_digitado

            db.session.add(novo_cliente)
            ModFilaAtivacaoCliente.query.filter_by(email=email_modulo).delete()
            db.session.commit()

            # Alimenta o contexto da sessão local
            session['cliente_modulo_id'] = novo_cliente.id
            session['usuario_id'] = novo_cliente.usuario_id
            session['modo_visao'] = 'cliente'
            session['nivel_acesso_atual'] = 10

            flash("Sua conta foi habilitada com sucesso!", "success")

            # =========================================================================
            # 🛡️ INTEGRAÇÃO COM A ESTEIRA AUTÔNOMA DE IDENTIDADE
            # =========================================================================
            # Se o usuário criado NÃO veio atrelado a um ID do Core (ou seja, usuário_id é None)
            # ou se ele não passou pela validação da LGPD ainda, nós barramos aqui e empurramos
            # para a tela autônoma de validação que criamos no Auth.

            if not usuario_id_capturado or (current_user.is_authenticated and not current_user.aceite_lgpd):
                # Guarda o destino final para onde ele queria ir após se validar
                destino_apos_validacao = proximo_passo or url_for('agenda.home_negocios')

                # Desvia o fluxo para a tela centralizada do Auth
                return redirect(url_for('auth.validar_identidade_tela', next_url=destino_apos_validacao))
            # =========================================================================

            if proximo_passo:
                return redirect(proximo_passo)
            return redirect(url_for('agenda.home_negocios'))

        except Exception as e:
            db.session.rollback()
            print(f"🚨 Erro Crítico no Auth Universal: {e}")
            flash("Houve um erro técnico ao ativar seu acesso.", "danger")

    return render_template('auth/validar_identidade.html', form=form, next_url=proximo_passo)


@agenda_bp.route('/agenda/balcao/gerar-fila', methods=['POST'])
def gerar_fila_ativacao():
    nome = request.form.get('nome')
    cpf_limpo = re.sub(r'\D', '', request.form.get('cpf'))
    whatsapp = request.form.get('whatsapp')
    email = request.form.get('email')  # Pode vir em branco do balcão

    # 1. Checagem imediata no Core para ver se ele já é da base do FeedIn
    usuario_existente = Usuario.query.filter_by(cpf=cpf_limpo).first()

    # 2. Define os tempos de controle do processo
    agora = datetime.now(timezone.utc)
    tempo_limite = agora + timedelta(hours=24)  # Regra interna inflexível de 24h

    # 3. Alimenta a Mesa de Limbo
    novo_limbo = ModFilaAtivacaoCliente(
        usuario_id=usuario_existente.id if usuario_existente else None,
        nome=nome,
        cpf=cpf_limpo,
        whatsapp=whatsapp,
        email=email if email else None,
        data_disparo=agora,
        data_expiracao=tempo_limite,
        data_tentativa_abertura=None  # Começa sem nenhuma tentativa
    )

    db.session.add(novo_limbo)
    db.session.commit()

    # Gera o token de transporte seguro contendo o CPF
    token = gerar_token_cadastro(cpf_limpo)
    link_final = url_for('agenda.concluir_via_link', token=token, _external=True)

    # Dispara o WhatsApp acessório com o link_final...
    flash("Agendamento fixado. Link temporário de ativação gerado na fila.", "success")
    return redirect(url_for('agenda.painel_agenda'))


@agenda_bp.route('/concluir-ativacao/<token>', methods=['GET', 'POST'])
def concluir_via_link(token):
    # -------------------------------------------------------------------------
    # CONDUTOR DE ENTRADA: Recupera o registro temporário do limbo do balcão
    # -------------------------------------------------------------------------
    # Ajustado dinamicamente para o nome real da sua classe de trânsito
    registro_fila = ModFilaAtivacaoCliente.query.filter_by(token=token).first_or_404()

    # Resgata o contexto geográfico do local trazido pelo escaneamento do QR Code
    id_local_atual = session.get('local_contexto_id')

    if request.method == 'POST':
        email_obrigatorio = request.form.get('email', '').strip().lower()
        data_nascimento_raw = request.form.get('data_nascimento')
        senha_digitada = request.form.get('password')

        # Validação básica de barreira
        if not senha_digitada:
            flash("A definição de uma senha é obrigatória para acessar seu painel.", "danger")
            return render_template('cadastro/conclusao_modulo.html', fila=registro_fila)

        # -------------------------------------------------------------------------
        # 1. TRATAMENTO CRITERIOSO E CONSISTÊNCIA PELO CPF
        # -------------------------------------------------------------------------
        # Consome a utilidade do Core para limpar a string vinda da fila
        cpf_hash_procurado, cpf_limpo = utils.preparar_e_hashear_cpf(registro_fila.cpf)

        # -------------------------------------------------------------------------
        # 2. VARREDURA SILENCIOSA NA ALFÂNDEGA
        # -------------------------------------------------------------------------
        # Verifica se este CPF já cruzou a fronteira e se tornou Cidadão no Core
        identidade_core = IdentidadeCivil.query.filter_by(cpf_hash=cpf_hash_procurado).first()
        usuario_id_core = identidade_core.usuario_id if identidade_core else None

        # -------------------------------------------------------------------------
        # 3. POPULAÇÃO OCULTA DE MEMÓRIA URBANA (Se já for Cidadão do Core)
        # -------------------------------------------------------------------------
        if usuario_id_core and id_local_atual:
            try:
                # Localiza o período cronológico vigente do ecossistema
                epoca_atual = Epoca.query.filter_by(eh_vigente=True).first()
                if not epoca_atual:
                    epoca_atual = Epoca.query.filter(Epoca.nome_exibicao.like('%(Atual)%')).first()

                # A) Cria o vínculo de pertencimento com o estabelecimento (se inédito)
                vinculo_existente = VinculoUsuarioLocal.query.filter_by(
                    usuario_id=usuario_id_core,
                    local_id=id_local_atual
                ).first()

                if not vinculo_existente:
                    novo_vinculo = VinculoUsuarioLocal(
                        usuario_id=usuario_id_core,
                        local_id=id_local_atual,
                        experiencia=f"Ativado via Módulo Agenda na época {epoca_atual.nome_exibicao if epoca_atual else 'Vigente'}"
                    )
                    db.session.add(novo_vinculo)

                # B) Registra a presença na linha do tempo histórica da época
                if epoca_atual:
                    historico_existente = UsuarioLocalEpoca.query.filter_by(
                        id_usuario=usuario_id_core,
                        id_local=id_local_atual,
                        id_epoca=epoca_atual.id
                    ).first()

                    if not historico_existente:
                        novo_historico = UsuarioLocalEpoca(
                            id_usuario=usuario_id_core,
                            id_local=id_local_atual,
                            id_epoca=epoca_atual.id
                        )
                        db.session.add(novo_historico)

            except Exception as e:
                # Falha silenciosa: Regra de ouro (O Core nunca trava o negócio da empresa parceira)
                current_app.logger.error(f"⚠️ Erro silencioso ao processar memória social no onboarding: {str(e)}")

        # -------------------------------------------------------------------------
        # 4. INSTANCIAÇÃO DO CADASTRO AUTÔNOMO (Fora do except, no fluxo principal do POST)
        # -------------------------------------------------------------------------
        try:
            # Importa o motor de hash do __init__.py global do Core
            from feedin import bcrypt

            # Gera o hash seguro padrão utilizando o Bcrypt real
            senha_hasheada_modulo = bcrypt.generate_password_hash(senha_digitada).decode('utf-8')

            # Gera o username automático a partir da primeira parte do e-mail
            username_gerado = email_obrigatorio.split('@')[0] if email_obrigatorio else f"user_{int(datetime.now().timestamp())}"

            novo_cliente = ModCadastroCliente(
                usuario_id=usuario_id_core,  # Vincula o ID encontrado ou salva como None (Avulso)
                nome=registro_fila.nome,
                whatsapp=registro_fila.whatsapp,
                email=email_obrigatorio if email_obrigatorio else None,
                data_nascimento=datetime.strptime(data_nascimento_raw, '%Y-%m-%d').date() if data_nascimento_raw else None,
                username_modulo=username_gerado,
                senha_modulo_hash=senha_hasheada_modulo
            )

            # DISPARA A MÁGICA DO SEU SETTER (@cpf.setter)
            # Consome o current_app.fernet definido no seu __init__.py
            novo_cliente.cpf = cpf_limpo

            db.session.add(novo_cliente)

            # -------------------------------------------------------------------------
            # 5. LIMPEZA DOS RASTROS DA FILA TEMPORÁRIA
            # -------------------------------------------------------------------------
            db.session.delete(registro_fila)

            # Comita toda a operação de forma atômica e segura
            db.session.commit()

            flash("Seu acesso de conveniência foi ativado com sucesso!", "success")
            return redirect(url_for('agenda.dashboard_cliente'))

        except Exception as e:
            db.session.rollback()
            current_app.logger.error(f"💥 ERRO CRÍTICO NA GRAVAÇÃO DO MÓDULO AGENDA: {str(e)}")
            flash("Houve um problema ao finalizar seu cadastro. Tente novamente.", "danger")
            return render_template('cadastro/conclusao_modulo.html', fila=registro_fila)

    return render_template('cadastro/conclusao_modulo.html', fila=registro_fila)


# ROTA AUXILIAR API: Quando o usuário clica no autocomplete, trazemos os dados brutos do local para preenchimento automático
@agenda_bp.route('/api/local/<int:id_local>')
def api_obter_local(id_local):
    local = Local.query.get_or_404(id_local)
    return jsonify({
        'id': local.id,
        'nome': local.nome,
        'documento': local.documento or '',
        'cep': local.cep or '',
        'logradouro': local.logradouro or '',
        'numero': local.numero or '',
        'bairro': local.bairro or '',
        'cidade': local.cidade or 'Piracicaba',
        'estado': local.estado or 'SP',
        'telefone': local.telefone or '',
        'email': local.email or '',
        'id_categoria_principal': local.id_categoria_principal or ''
    })


def verificar_disponibilidade_agenda(id_local, id_profissional, data_inicio_proposta, duracao_minutos):
    """
    Analisa a grade horária e possíveis conflitos de um profissional.
    Retorna um dicionário com o diagnóstico: (status_sugerido, mensagem, id_substituto_alternativo)
    """
    # 1. Calcula o horário de término previsto para o serviço
    data_fim_proposta = data_inicio_proposta + timedelta(minutes=duracao_minutos)

    # Extrai apenas as frações de tempo (Time) para checar o expediente diário
    hora_inicio_proposta = data_inicio_proposta.time()
    hora_fim_proposta = data_fim_proposta.time()

    # 2. Busca o contrato de trabalho e expediente do profissional neste local
    contrato = ColaboradorContrato.query.filter_by(
        id_usuario=id_profissional,
        id_local=id_local,
        status_profissional='ativo'
    ).first()

    if not contrato:
        return {"valido": False, "status_sugerido": "erro",
                "msg": "Profissional não possui contrato ativo neste local."}

    # Converte os horários salvos como String/Text do banco para objetos de tempo (Time) do Python
    exp_inicio = datetime.strptime(contrato.hora_inicio_expediente, "%H:%M").time()
    exp_fim = datetime.strptime(contrato.hora_fim_expediente, "%H:%M").time()

    # 🚨 TESTE 1: Checa se a solicitação ESTOURA o limite do expediente (Sua regra de contingência!)
    if hora_inicio_proposta < exp_inicio or hora_fim_proposta > exp_fim:
        return {
            "valido": True,  # É válido para registro, mas sob condições especiais
            "status_sugerido": "sob_avaliacao_expediente",
            "msg": "O horário solicitado ultrapassa as barreiras do expediente normal do profissional.",
            "substituto_id": buscar_cadeira_substituta_livre(id_local, id_profissional, data_inicio_proposta,
                                                             data_fim_proposta, contrato.id_cargo)
        }

    # 🚨 TESTE 2: Checa se há choque direto com outro agendamento já confirmado na mesma janela (Double-Booking)
    conflito_direto = AghAgendamento.query.filter(
        AghAgendamento.id_profissional == id_profissional,
        AghAgendamento.id_local == id_local,
        AghAgendamento.status.in_(['confirmado', 'pendente', 'proposta_remanejamento']),
        AghAgendamento.data_hora_inicio < data_fim_proposta,
        AghAgendamento.data_hora_fim > data_inicio_proposta
    ).first()

    if conflito_direto:
        return {
            "valido": False,
            "status_sugerido": "conflito_agenda",
            "msg": "Este profissional já possui um atendimento agendado neste intervalo de tempo.",
            "substituto_id": buscar_cadeira_substituta_livre(id_local, id_profissional, data_inicio_proposta,
                                                             data_fim_proposta, contrato.id_cargo)
        }

    # Cenário Perfeito: Livre e dentro do horário
    return {"valido": True, "status_sugerido": "confirmado", "msg": "Horário disponível!", "substituto_id": None}


def buscar_cadeira_substituta_livre(id_local, id_profissional_atual, data_inicio, data_fim, id_cargo):
    """
    Procura em tempo real por outra cadeira (outro profissional com o mesmo cargo)
    que esteja livre e ativa no mesmo intervalo.
    """
    # Busca todos os outros colegas da mesma função/cargo no estabelecimento
    colegas = ColaboradorContrato.query.filter(
        ColaboradorContrato.id_local == id_local,
        ColaboradorContrato.id_cargo == id_cargo,
        ColaboradorContrato.id_usuario != id_profissional_atual,
        ColaboradorContrato.status_profissional == 'ativo'
    ).all()

    for colega in colegas:
        # Verifica se o expediente do colega cobre essa janela
        exp_inicio = datetime.strptime(colega.hora_inicio_expediente, "%H:%M").time()
        exp_fim = datetime.strptime(colega.hora_fim_expediente, "%H:%M").time()

        if data_inicio.time() >= exp_inicio and data_fim.time() <= exp_fim:
            # Checa se o colega está sem nenhum agendamento conflitante
            ocupado = AghAgendamento.query.filter(
                AghAgendamento.id_profissional == colega.id_usuario,
                AghAgendamento.id_local == id_local,
                AghAgendamento.status.in_(['confirmado', 'pendente', 'proposta_remanejamento']),
                AghAgendamento.data_hora_inicio < data_fim,
                AghAgendamento.data_hora_fim > data_inicio
            ).first()

            if not ocupado:
                return colega.id_usuario  # Retorna o ID do substituto ideal encontrado!

    return None  # Ninguém disponível na mesma função


@agenda_bp.route('/balcao/agendar', methods=['POST'])
@modulo_required
def criar_agendamento_balcao():
    id_local = request.form.get('id_local', type=int)
    profissional_id = request.form.get('id_profissional', type=int)
    cliente_id = request.form.get('id_cliente', type=int)
    servico_id = request.form.get('id_servico', type=int)
    preco_real = request.form.get('preco_cobrado', type=float)  # Pegando o preço cobrado real da sua model

    data_hora_str = request.form.get('data_hora_atendimento')
    data_inicio = datetime.strptime(data_hora_str, "%Y-%m-%d %H:%M")
    duracao_minutos = request.form.get('duracao_servico', default=60, type=int)

    # O motor de cálculo processa os dados com a inteligência que desenhamos
    diagnostico = verificar_disponibilidade_agenda(
        id_local=id_local,
        id_profissional=profissional_id,
        data_inicio_proposta=data_inicio,
        duracao_minutos=duracao_minutos
    )

    # APLICANDO AS REGRAS NO SEU PADRÃO:
    if diagnostico["status_sugerido"] == "confirmado":
        novo_agendamento = AghAgendamento(
            profissional_id=profissional_id,
            cliente_id=cliente_id,
            servico_id=servico_id,
            preco_cobrado=preco_real,
            data_hora_inicio=data_inicio,
            data_hora_fim=data_inicio + timedelta(minutes=duracao_minutos),
            status='confirmado',
            tipo_origem='manual'  # Balcão é inserção manual
        )
        db.session.add(novo_agendamento)
        db.session.commit()

        flash("✅ Agendamento realizado com sucesso!", "success")
        return redirect(url_for('agenda.painel_gerencial'))

    elif diagnostico["status_sugerido"] in ["sob_avaliacao_expediente", "conflito_agenda"]:
        id_substituto = diagnostico["substituto_id"]

        if id_substituto:
            # Pegamos o nome do profissional substituto
            substituto = Usuario.query.get(id_substituto)

            # Cria a proposta de remanejamento na outra cadeira (outra FK de profissional)
            agendamento_resiliente = AghAgendamento(
                profissional_id=id_substituto,  # Transfere para a cadeira livre
                cliente_id=cliente_id,
                servico_id=servico_id,
                preco_cobrado=preco_real,
                data_hora_inicio=data_inicio,
                data_hora_fim=data_inicio + timedelta(minutes=duracao_minutos),
                status='reagendamento_pendente',  # Alinhado com os status da sua model!
                tipo_origem='manual'
            )
            db.session.add(agendamento_resiliente)
            db.session.commit()

            flash(
                f"⚠️ Horário indisponível com o original! "
                f"Movido automaticamente para a cadeira de {substituto.username}. "
                f"Aguardando validação do cliente no app.", "warning"
            )
            return redirect(url_for('agenda.painel_gerencial'))
        else:
            flash("❌ Horário indisponível e nenhuma outra cadeira da mesma especialidade está livre.", "danger")
            return redirect(url_for('agenda.painel_gerencial'))


@agenda_bp.route('/api/agenda/notificacoes-pendentes', methods=['GET'])
def verificar_notificacoes_app():
    """
    1. ENDPOINT GET: O aplicativo chama esta rota para saber se o cliente logado
    possui algum agendamento aguardando aprovação de troca de cadeira.
    """
    # Exemplo simples capturando o ID do cliente enviado pelo app via query string
    # (Adapte para o seu sistema de @jwt_required ou token se já estiver usando)
    cliente_id = request.args.get('cliente_id', type=int)

    if not cliente_id:
        return jsonify({"erro": "ID do cliente é obrigatório."}), 400

    # Busca na tabela oficial agh_agendamento qualquer registro pendente do cliente
    pendencia = AghAgendamento.query.filter_by(
        cliente_id=cliente_id,
        status='reagendamento_pendente'
    ).first()

    if not pendencia:
        # Retorna um objeto vazio ou sinaliza que está tudo limpo (sem pop-ups no app)
        return jsonify({"possui_pendencia": False}), 200

    # Se achou, precisamos buscar os dados do profissional substituto para mostrar no App
    # Como profissional_id na sua model é uma FK, buscamos o objeto dele para pegar o nome
    profissional_substituto = Usuario.query.get(pendencia.profissional_id)
    nome_profissional = profissional_substituto.username if profissional_substituto else "Profissional Técnico"

    # Monta a resposta estruturada para o Front-end do aplicativo
    return jsonify({
        "possui_pendencia": True,
        "dados_remanejamento": {
            "id_agendamento": pendencia.id,
            "data_hora": pendencia.data_hora_inicio.strftime('%Y-%m-%d %H:%M'),
            "data_formatada": pendencia.data_hora_inicio.strftime('%d/%m às %H:%M'),
            "preco_cobrado": float(pendencia.preco_cobrado),
            "profissional_sugerido": nome_profissional,
            "alerta_mensagem": f"O seu horário original estava indisponível, mas garantimos sua vaga com o profissional {nome_profissional}. Deseja aceitar a substituição?"
        }
    }), 200


@agenda_bp.route('/api/agenda/responder-remanejamento', methods=['POST'])
def responder_remanejamento_cliente():
    """
    ENDPOINT POST: Recebe a ação do clique do botão no aplicativo do cliente.
    Suporta as decisões 'aceitar', 'recusar' ou 'rejeitar'.
    """
    data = request.get_json() or {}

    id_agendamento = data.get('id_agendamento')
    decisao_cliente = data.get('decisao')

    if not id_agendamento or not decisao_cliente:
        return jsonify({"erro": "Parâmetros inválidos. Informe o id_agendamento e a decisao."}), 400

    # Busca a linha correta no banco (Alinhado com AghAgendamento)
    agendamento = AghAgendamento.query.get(id_agendamento)

    if not agendamento:
        return jsonify({"erro": "Agendamento não localizado no sistema."}), 404

    # Trava de segurança: impede reprocessamento se o status mudou enquanto a tela estava aberta
    if agendamento.status != 'reagendamento_pendente':
        return jsonify({"erro": "Este agendamento já foi processado ou expirou."}), 400

    if decisao_cliente == 'aceitar':
        # Clique no botão: "Aceitar Substituição"
        agendamento.status = 'confirmado'
        db.session.commit()

        return jsonify({
            "sucesso": True,
            "status_final": "confirmado",
            "acao_app": "fechar_modal_sucesso",
            "mensagem": "Perfeito! Seu atendimento foi confirmado com o novo profissional."
        }), 200

    elif decisao_cliente in ['recusar', 'rejeitar']:
        # Clique no botão: "Mudar Horário / Recusar"
        # O sistema cancela a pré-reserva na cadeira para liberar o espaço imediatamente
        agendamento.status = 'cancelado'
        db.session.commit()

        # O JSON avisa o App que a vaga foi liberada e instrui o app a abrir a tela de calendário
        return jsonify({
            "sucesso": True,
            "status_final": "cancelado",
            "acao_app": "abrir_tela_calendario",
            "mensagem": "Entendido. A reserva provisória foi liberada. Escolha um novo horário de sua preferência."
        }), 200

    else:
        return jsonify({"erro": "Decisão inválida. Utilize 'aceitar', 'recusar' ou 'rejeitar'."}), 400


# feedin/routes.py (ou correspondente do Core)


@agenda_bp.route('/painel/<int:id_local_alvo>')
@modulo_required
def acessar_estabelecimento(id_local_alvo):
    """
    Rota de triagem contextual movida para o módulo correto (Agenda).
    """
    contrato = ColaboradorContrato.query.filter_by(
        id_usuario=current_user.id,
        id_local=id_local_alvo,
        status_profissional='ativo',
        data_desligamento=None
    ).first()

    if contrato:
        session['modo_visao'] = 'balcao'
        session['nivel_acesso_atual'] = contrato.papel_nivel
        session['papel_nome'] = contrato.papel_nome
        session['local_contexto_id'] = id_local_alvo
        flash(f"Modo interno ativado: {contrato.cargo.nome_cargo}", "success")
        return redirect(url_for('agenda.painel_interno_loja', id_local=id_local_alvo))
    else:
        session['modo_visao'] = 'cliente'
        session['nivel_acesso_atual'] = 10
        session['local_contexto_id'] = id_local_alvo
        return redirect(url_for('agenda.detalhe_empresa', id_local=id_local_alvo))


# =====================================================================
# 🗓️ BLUEPRINT: AGENDA_BP (OPERAÇÕES E COCKPIT DE ATENDIMENTO)
# =====================================================================

@agenda_bp.route('/dashboard', methods=['GET'])
@modulo_required
def dashboard_cliente():
    """
    PAINEL DE CONVENIÊNCIA (PWA CLIENTE)
    ----------------------------------
    Garante o acesso à área logada do cliente para gerenciamento de sua
    agenda pessoal no ecossistema de Piracicaba.

    Contexto:
        - Consome a sessão ativa do usuário para listar agendamentos futuros,
          histórico de atendimentos e status de remanejamentos pendentes.

    Retorno:
        - Renderiza 'agenda/dashboard_cliente.html' com dados operacionais locais.
    """
    cliente_id = session.get('cliente_modulo_id')
    if not cliente_id:
        flash("Por favor, faça login para acessar seu painel de agendamentos.", "warning")
        return redirect(url_for('auth.login'))

    cliente = ModCadastroCliente.query.get_or_404(cliente_id)
    id_local_atual = session.get('local_contexto_id')
    agendamentos_ativos = []  # Query resiliente de busca

    return render_template(
        'agenda/dashboard_cliente.html',
        cliente=cliente,
        agendamentos=agendamentos_ativos,
        id_local=id_local_atual
    )


@agenda_bp.route('/negocio/<int:empresa_id>/agendar', methods=['GET', 'POST'])
def agendar_servico(empresa_id):
    """
    FUNIL DE FECHAMENTO DE RESERVA
    ------------------------------
    Interface final onde o cliente seleciona o profissional, horário e
    confirma a prestação do serviço.

    Segurança:
        - Fiscal de Portaria: Se o ID do cliente não estiver fixado na sessão,
          desvia o fluxo para o login centralizado preservando a URL de retorno (next_url).
    """
    cliente_id = session.get('cliente_modulo_id')
    if not cliente_id:
        flash("Para realizar um agendamento, por favor, conecte-se à sua conta.", "info")
        return redirect(url_for('auth.login', next_url=request.url))

    cliente = ModCadastroCliente.query.get(cliente_id)
    return render_template('agenda/fechar_agendamento.html', cliente=cliente, empresa_id=empresa_id)


@agenda_bp.route('/colaborador/<int:id_colaborador>/desligar', methods=['POST'])
@modulo_required
def desligar_colaborador(id_colaborador):
    """
    ENCERRAMENTO DE VÍNCULO TRABALHISTA
    -----------------------------------
    Executa a revogação atômica dos privilégios de um profissional dentro
    de um estabelecimento específico.

    Regras de Negócio:
        1. Altera o status do contrato para 'desligado' e grava o timestamp.
        2. Comunica ao Core a necessidade de resetar o nível de acesso do
           usuário afetado de volta ao patamar de Cidadão Inicial (Nível 10).
    """
    contrato = ColaboradorContrato.query.get_or_404(id_colaborador)

    # 1. Encerramento do vínculo
    contrato.status_profissional = 'desligado'
    contrato.data_desligamento = datetime.now(timezone.utc)

    # 2. Rebaixamento de privilégios via encapsulamento do Core
    usuario_colaborador = Usuario.query.get(contrato.id_usuario)
    if usuario_colaborador:
        usuario_colaborador.resetar_para_nivel_padrao()  # Método encapsulado na model Usuario

    db.session.commit()
    flash("💼 Colaborador desligado. Privilégios revogados com sucesso.", "success")
    return redirect(url_for('agenda.painel_gerencial'))


def verificar_onboarding_local(f):
    """
    Decorator para rotas da Agenda.
    Verifica se o usuário logado já passou pelo acolhimento de primeiro acesso neste estabelecimento.
    """

    @wraps(f)
    def decorated_function(*args, **kwargs):
        # 1. Recupera o usuário logado na sessão (injetado pelo Auth) e o estabelecimento atual
        cliente_id = session.get('user_id')  # ID do ModCadastroCliente
        local_id = kwargs.get('local_id') or request.args.get('local_id')

        if cliente_id and local_id:
            # 2. Bate na API do Auth para checar o contexto
            try:
                # Nota: Em produção, substitua pelo domínio correto ou comunicação interna entre apps
                resposta = requests.get(f'http://localhost:5000/api/auth/contexto/{cliente_id}/{local_id}', timeout=3)
                if resposta.status_code == 200:
                    dados_contexto = resposta.json()

                    # 3. Se NÃO completou o onboarding, redireciona para a rota que exibe o modal simpático
                    if not dados_contexto.get('cadastro_completo'):
                        return redirect(url_for('agenda.boas_vindas', local_id=local_id))
            except requests.exceptions.RequestException:
                # Se a API interna falhar por algum motivo, não travamos o cliente! O agendamento segue.
                pass

        return f(*args, **kwargs)

    return decorated_function


@agenda_bp.route('/estabelecimento/<int:local_id>/boas-vindas', methods=['GET', 'POST'])
def boas_vindas(local_id):

    cliente_id = session.get('user_id')
    destino_original = request.args.get('next')  # Pega a URL de onde o cliente veio

    if request.method == 'POST':
        # Captura os dados inseridos simpaticamente pelo cliente
        apelido = request.form.get('apelido')
        ano_inicio = request.form.get('ano_inicio')

        # Aqui capturaríamos a foto (selfie) temporariamente e salvaríamos no storage, gerando o path
        foto_path = None
        if 'foto' in request.files:
            foto_file = request.files['foto']
            # Lógica para salvar a imagem no servidor e obter a URL...
            foto_path = "/uploads/perfis/" + foto_file.filename

            # Prepara o pacote de dados para enviar ao Auth
        payload = {
            "cliente_id": cliente_id,
            "local_id": local_id,
            "apelido": apelido if apelido else None,
            "foto_path": foto_path,
            "ano_inicio_relacionamento": ano_inicio if ano_inicio else None
        }

        if destino_original:
            return redirect(destino_original)
        return redirect(url_for('agenda.detalhe_empresa', empresa_id=local_id))

        # Despacha para o cofre do Auth gerenciar e salvar
        try:
            requests.post('http://localhost:5000/api/auth/contexto/salvar', json=payload, timeout=5)
        except requests.exceptions.RequestException:
            pass  # Tratar erro de comunicação sem quebrar a tela

        # Tudo pronto! Devolvemos o cliente para a rota onde ele estava agendando
        return redirect(url_for('agenda.escolher_horario', local_id=local_id))

    # Se for GET, renderiza a interface do modal de primeiro acesso
    # Buscamos o nome do local no Core para personalizar a mensagem ("Seja bem-vindo ao Cortes do João!")
    nome_estabelecimento = "Nossa Empresa"  # Buscar do banco do Core usando o local_id

    return render_template('agenda/boas_vindas_onboarding.html',
                           nome_estabelecimento=nome_estabelecimento,
                           local_id=local_id)