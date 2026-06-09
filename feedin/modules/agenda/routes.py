# feedin/modules/agenda/routes.py
# =====================================================================
# 1. IMPORTAÇÕES NATIVAS DO PYTHON E BIBLIOTECAS EXTERNAS
# =====================================================================
import re
import secrets
import feedin.modules.agenda.routes
from datetime import datetime, timedelta, timezone

from flask import current_app, flash, jsonify, redirect, render_template, request, session, url_for
from flask_login import login_required
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from werkzeug.security import generate_password_hash

# =====================================================================
# 2. INSTÂNCIAS E EXTENSÕES DO CORE DO FEEDIN
# =====================================================================
from feedin import database as db
from feedin import bcrypt  # Caso precise usar o bcrypt do Core

# =====================================================================
# 3. COMPONENTES DO MÓDULO INTERNO (AGENDA)
# =====================================================================
from feedin.modules.agenda import agenda_bp
from feedin.modules.agenda.forms import (
    FormCadastroBalcao,
    FormColaborador,
    FormConfigMarca,
    FormCredenciamentoLocal,
    FormServico,
)
from feedin.modules.agenda.models import (
    AghAgendamento,
    AghProfissional,
    AghServico,
    EseEmpresa,
    ModCadastroCliente,
    ModFilaAtivacaoCliente,  # <-- ADICIONADO: Nova tabela de limbo
    UsuarioFavorito,
)

# =====================================================================
# 4. ENTIADES E UTILITÁRIOS DO CORE CENTRAL DO FEEDIN
# =====================================================================
import feedin.utils as utils
from feedin.models import ColaboradorContrato, HistoricoOcupacaoLocal, Local, Usuario, IdentidadeCivil

# =====================================================================


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
def painel_agenda():
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
    usuario_logado_id = 1
    usuario_cidade_atual = "Piracicaba"
    usuario_estado_atual = "SP"

    # [PONTO DE ATENÇÃO]: Verificação de perfil
    # No futuro, buscaremos o cargo/role do usuário no banco.
    # Se ele for um Profissional/Parceiro Beta, jogamos para a Home Gerencial dele.
    usuario_role = "cliente"  # Mocado temporariamente como 'cliente'

    if usuario_role == "profissional":
        # Quando criarmos a Fase 5 (Painel Gerencial), ele cairá aqui
        return render_template('agenda/home_profissional_gerencial.html', cidade=usuario_cidade_atual)

    # --- Tudo o que você já construiu e está funcionando continua exatamente aqui ---
    favoritos = UsuarioFavorito.query.filter_by(usuario_id=usuario_logado_id).all()

    # MATRIZ DE NOTIFICAÇÕES VISUAIS CONTEXTUAIS (Dicionário dinâmico)
    lista_notificacoes = [
        {
            "titulo_tipo": "Atendimento & Agenda",
            "mensagem": "Você possui 1 agendamento pendente no Cabeleireiro",
            "icone": "bi-calendar-event",
            "cor_base": "warning",
            "link_acao": "/agenda/meus-horarios"
        },
        {
            "titulo_tipo": "Delivery & Entrega",
            "mensagem": "Seu pedido no Bar do Baixo saiu para entrega",
            "icone": "bi-truck",
            "cor_base": "primary",
            "link_acao": "/delivery/rastreio"
        },
        {
            "titulo_tipo": "Histórico & Memória",
            "mensagem": "Sua avaliação da Padaria Central foi homologada!",
            "icone": "bi-award",
            "cor_base": "success",
            "link_acao": "/perfil/reputacao"
        }
    ]

    ultimo_local_visitado = "Salão do Zé"
    local_mais_frequentado = "Salão do Zé"

    # Retorna o seu template padrão de cliente, com as suas variáveis intactas
    return render_template(
        'agenda/home_negocios.html',
        cidade=usuario_cidade_atual,
        estado=usuario_estado_atual,
        favoritos=favoritos,
        notificacoes=lista_notificacoes,
        ultimo_local=ultimo_local_visitado,
        mais_visitado=local_mais_frequentado
    )


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
@login_required
def credenciamento_balcao():
    # Pega o ID do usuário logado na sessão do Flask-Login
    empreendedor_id = current_user.id
    form = FormCredenciamentoLocal()

    if form.validate_on_submit():
        id_existente = form.id_local_existente.data

        try:
            if id_existente:
                # 🌟 CENÁRIO A: O usuário selecionou uma Memória da lista do Autocomplete
                local = Local.query.get(id_existente)
                if not local:
                    flash("Ponto físico selecionado inválido.", "danger")
                    return redirect(url_for('agenda.credenciamento_balcao'))

                # Se o local já estiver ativo com OUTRO empreendedor, barramos a duplicidade de operação
                if local.esta_ativo and local.id_empreendedor != empreendedor_id:
                    flash("Este estabelecimento já possui uma administração ativa no FeedIn.", "warning")
                    return redirect(url_for('agenda.credenciamento_balcao'))
            else:
                # 🌟 CENÁRIO B: Busca vazia ou ignorada. O sistema cria um ponto inédito automaticamente
                local = Local()
                local.data_cadastro = datetime.now(timezone.utc)
                db.session.add(local)

            # Injeção direta dos dados do Form na Model 'Local'
            local.nome = form.nome.data.strip()
            local.documento = form.documento.data.strip() if form.documento.data else None
            local.cep = form.cep.data.strip()
            local.logradouro = form.logradouro.data.strip()
            local.numero = form.numero.data.strip()
            local.bairro = form.bairro.data.strip()
            local.cidade = form.cidade.data.strip()
            local.estado = form.estado.data.upper() if form.estado.data else 'SP'
            local.telefone = form.telefone.data.strip()
            local.is_whatsapp = form.is_whatsapp.data

            # Vinculo de posse comercial no Core do sistema
            local.id_empreendedor = empreendedor_id
            local.esta_ativo = True
            local.status_operacional = 'ativo'

            # Gravação do histórico de ocupação para auditoria/mural
            ocupacao = HistoricoOcupacaoLocal(
                local=local,
                id_empreendedor=empreendedor_id,
                plano_contratado=local.plano_marketing
            )
            db.session.add(ocupacao)

            db.session.commit()
            flash("Estabelecimento estruturado com sucesso no FeedIn!", "success")
            return redirect(url_for('agenda.dashboard'))

        except Exception as e:
            db.session.rollback()
            print(f"Erro crítico no credenciamento: {e}")
            flash("Ocorreu um erro interno. Verifique se este documento ou endereço já está cadastrado.", "danger")

    return render_template('agenda/credenciamento.html', form=form)


@agenda_bp.route('/empresa/<int:empresa_id>')
def detalhe_empresa(empresa_id):
    """
    Exibe o perfil público da empresa (Visão do Cliente).
    Se o usuário logado tiver nível e vínculo com o local, habilita o botão de transição.
    """
    # 1. Busca a empresa na base do Core
    empresa = EseEmpresa.query.get_or_404(empresa_id)

    # 2. Resgata o usuário logado através do Core (Exemplo com ID fixo para teste)
    usuario_id_teste = 1
    usuario_logado = Usuario.query.get(usuario_id_teste)

    is_colaborador = False
    nivel_neste_local = 10  # Padrão: Usuário comum

    if usuario_logado:
        # Verifica se ele é o Empreendedor Supremo (999) deste local
        if empresa.proprietario_id == usuario_logado.id and usuario_logado.nivel_acesso == 999:
            is_colaborador = True
            nivel_neste_local = 999
        else:
            # Busca na tabela de colaboradores se ele possui algum cargo ativo (888, 777, 666...)
            vinculo = ColaboradorContrato.query.filter_by(
                empresa_id=empresa.id,
                usuario_id=usuario_logado.id,
                is_ativo=True
            ).first()

            if vinculo:
                is_colaborador = True
                nivel_neste_local = vinculo.nivel_acesso  # Pode ser 888, 777, etc.

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


@agenda_bp.route('/balcao/sair')
def sair_modo_balcao():
    """ Limpa o contexto de gerenciamento e devolve o usuário para a cidade """
    session.pop('modo_visao', None)
    empresa_id = session.pop('empresa_ativa_id', None)
    session.pop('nivel_acesso_atual', None)

    if empresa_id:
        return redirect(url_for('agenda.detalhe_empresa', empresa_id=empresa_id))
    return redirect(url_for('agenda.home_negocios'))


@agenda_bp.route('/identificar', methods=['GET', 'POST'])
def identificar_usuario():
    if request.method == 'GET':
        return render_template('agenda/login_modulo.html')

    email_digitado = request.form.get('email_login', '').strip().lower()

    if not email_digitado:
        flash("Por favor, informe seu E-mail para continuar.", "warning")
        return redirect(url_for('agenda.identificar_usuario'))

    # 🔍 CAMADA 1: O usuário já tem acesso ao módulo?
    cliente_oficial = ModCadastroCliente.query.filter_by(email=email_digitado).first()

    if cliente_oficial:
        # Destino: Desafiar a Senha
        return redirect(url_for('agenda.desafiar_senha', cliente_id=cliente_oficial.id))

    # 🔍 CAMADA 2: O e-mail está na Fila do Limbo?
    cliente_fila = ModFilaAtivacaoCliente.query.filter_by(email=email_digitado).first()

    if cliente_fila:
        # Aqui, como é por e-mail, é muito menos provável que seja um golpista adivinhando,
        # mas mantemos a regra de segurança: a conclusão é pelo WhatsApp.
        flash("Seu cadastro foi iniciado no balcão! Por favor, acesse o link enviado para o seu WhatsApp para criar sua senha de acesso.", "info")
        return redirect(url_for('agenda.identificar_usuario'))

    # 🔍 CAMADA 3: Tem conta no Core da Cidade pelo E-mail?
    usuario_core = Usuario.query.filter_by(email=email_digitado).first()

    if usuario_core:
        # Destino: Exigir senha do Core para vincular ao Módulo
        flash("Você já é um cidadão FeedIn! Digite sua senha da cidade para ativar o módulo de agendamentos.", "success")
        return redirect(url_for('agenda.vincular_conta_core', usuario_id=usuario_core.id))

    # 🔍 CAMADA 4: E-mail não encontrado em lugar nenhum
    # Destino: Cadastro Novo Limpo
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


@agenda_bp.route('/cadastro/ativar/<token>', methods=['GET', 'POST'])
def concluir_via_link(token):
    serializador = URLSafeTimedSerializer(current_app.config["SECRET_KEY"])
    agora = datetime.now(timezone.utc)

    # Desempacota o token para descobrir de qual cliente se trata
    try:
        cpf_cliente = serializador.loads(token, salt="ativacao-modulo-cliente")
    except BadSignature:
        return render_template('erros/erro_geral.html', msg="Link corrompido.")

    # Busca o registro na tabela temporária de Limbo
    registro_fila = ModFilaAtivacaoCliente.query.filter_by(cpf=cpf_cliente).first_or_404()

    # 📊 AUDITORIA: Registra o momento exato que o usuário tentou abrir o link
    if not registro_fila.data_tentativa_abertura:
        registro_fila.data_tentativa_abertura = agora
        db.session.commit()  # Grava o rastro imediatamente para o lojista acompanhar

    # 🛡️ Validação da Regra Interna contra a Expiração
    if agora > registro_fila.data_expiracao:
        return render_template('erros/expirado.html',
                               msg="Este link expirou. O FeedIn preza pela segurança dos seus dados. Solicite um novo envio no estabelecimento.")

    # Se o usuário já pré-existia no FeedIn (usuario_id não é nulo)
    ja_tem_core = registro_fila.usuario_id is not None

    if request.method == 'POST':
        email_obrigatorio = request.form.get('email')
        data_nascimento = request.form.get('data_nascimento')
        senha = request.form.get('password')

        if ja_tem_core:
            # Recupera o usuário do Core e apenas traz as informações que faltavam
            usuario_core = Usuario.query.get(registro_fila.usuario_id)
        else:
            # Cadastra o usuário de forma limpa e oficial no Core do FeedIn
            usuario_core = Usuario(
                nome=registro_fila.nome,
                cpf=registro_fila.cpf,
                email=email_obrigatorio,
                senha_hash=generate_password_hash(senha),
                nivel_acesso=10
            )
            db.session.add(usuario_core)
            db.session.flush()

        # ✨ SUCESSO DO CICLO: Agora sim, os dados limpos entram na tabela de produção do módulo
        novo_cliente_oficial = ModCadastroCliente(
            usuario_id=usuario_core.id,
            nome=registro_fila.nome,
            cpf=registro_fila.cpf,
            whatsapp=registro_fila.whatsapp,
            email=email_obrigatorio,
            data_nascimento=datetime.strptime(data_nascimento, '%Y-%m-%d').date(),
            username_modulo=email_obrigatorio.split('@')[0],  # Limpo e profissional
            senha_modulo_hash=usuario_core.senha_hash
        )

        db.session.add(novo_cliente_oficial)

        # Remove o registro da fila temporária (Opcional, ou mantenha para histórico de relatórios)
        db.session.delete(registro_fila)

        db.session.commit()
        return render_template('cadastro/sucesso.html', msg="Cadastro concluído com sucesso!")

    # Passa para o template se ele já tem cadastro no Core, facilitando o formulário
    return render_template('cadastro/conclusao_modulo.html', fila=registro_fila, ja_tem_core=ja_tem_core)


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
@login_required
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
import os
from datetime import datetime, timezone
from werkzeug.utils import secure_filename
from flask import render_template, redirect, url_for, flash, request
from flask_login import login_required, current_user

@agenda_bp.route('/colaborador/<int:id_colaborador>/desligar', methods=['POST'])
@login_required
def desligar_colaborador(id_colaborador):
    # 1. Busca o contrato do colaborador na tabela que injetamos via SQL Puro
    contrato = ColaboradorContrato.query.get_or_404(id_colaborador)

    # 2. Executa o encerramento do vínculo profissional
    contrato.status_profissional = 'desligado'
    contrato.data_desligamento = datetime.now(timezone.utc)

    # 3. A REGRA DO CARLOS: Busca o usuário correspondente para resetar o nível
    usuario_colaborador = Usuario.query.get(contrato.id_usuario)
    if usuario_colaborador:
        # 🚨 Retorna automaticamente para o nível de cliente comum/inicial
        usuario_colaborador.nivel = 10

    db.session.commit()

    flash(
        "💼 Colaborador desligado com sucesso. Os privilégios de acesso foram revogados e o usuário retornou ao Nível 10.",
        "success")
    return redirect(url_for('agenda.painel_gerencial'))


# feedin/modules/agenda/routes.py

@agenda_bp.route('/criar-conta', methods=['POST'])
def criar_conta():
    email = request.form.get('email').strip().lower()
    senha = request.form.get('senha')

    # 1. Tenta identificar se este e-mail já existe no Core
    usuario_core = Usuario.query.filter_by(email=email).first()

    # 2. Se existe, exigimos login (ou vinculamos)
    if usuario_core:
        flash("Este e-mail já possui conta no FeedIn. Faça login para vincular ao agendamento.", "info")
        return redirect(url_for('agenda.login_modulo'))

    # 3. Se não existe, cria o novo registro na tabela oficial de consumo
    novo_cliente = ModCadastroCliente(
        email=email,
        # A senha deve ser hasheada aqui
        # Vincula o ID caso ele tenha vindo de uma fila pré-ativada via token
        fila_id_origem=session.get('fila_id_ativo')
    )
    db.session.add(novo_cliente)
    db.session.commit()

    return redirect(url_for('agenda.home_negocios'))