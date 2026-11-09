import datetime
from flask_login import current_user
from flask import render_template, abort, redirect, url_for, current_app, session
from feedin.models import database as db, Local, Epoca, Postagem, VinculoUsuarioLocal, Usuario, Conexoes
from feedin.modules.empresa.models import EseEmpresa, ModEmpresaModulo, EseServicoOferecido, ColaboradorContrato, \
    EseAvaliacaoEmpresa, EseRelacionamentoClienteEmpresa, EseHorarioFuncionamento

# Importamos o blueprint do módulo empresa a partir do pacote pai ou passamos a rota decorada nele
from feedin.modules.empresa import empresa_bp
from sqlalchemy.orm import joinedload

from feedin.modules.registry_acoes import MODULOS_ACOES_REGISTRY


@empresa_bp.route('/perfil/<string:empresa_uuid>', methods=['GET'], endpoint='perfil_comercial_empresa')
def perfil_comercial(empresa_uuid):
    empresa = EseEmpresa.query.filter_by(id=empresa_uuid).first() or EseEmpresa.query.filter_by(
        uuid=empresa_uuid).first_or_404()

    # 🌟 CHAMA O MOTOR DE RATING UNIFICADO
    rating_data = empresa.get_rating_data()

    # 🌟 DEFINE SE O USUÁRIO PODE AVALIAR
    pode_avaliar = current_user.is_authenticated

    # 🌟 CONTEXTO ATUAL DO MÓDULO
    contexto_modulo_atual = 'empresa'

    # 1. Identifica o local_id vinculado à empresa
    local_id_alvo = empresa.local_id if hasattr(empresa, 'local_id') and empresa.local_id else None

    # 🎯 1. Identifica o módulo principal vigente da empresa (com fallback seguro para 'agenda')
    modulo_ativo_slug = getattr(empresa, 'modulo_principal_slug', None) or session.get('modulo_slug_atual') or 'agenda'

    # 🎯 2. Resgata a configuração padrão atrelada ao blueprint do módulo
    config_acao = MODULOS_ACOES_REGISTRY.get(modulo_ativo_slug, MODULOS_ACOES_REGISTRY['agenda'])

    # 🎯 3. Constrói o dicionário de CTA (Call to Action) dinâmico
    acao_principal = {}
    try:
        # Tenta gerar a URL dinamicamente baseada no endpoint do módulo
        # Passando o ID da empresa para garantir o escopo correto
        url_gerada = url_for(config_acao['endpoint'], empresa_id=empresa.id)
    except Exception:
        # Fallback de segurança caso o endpoint exija outros parâmetros ou não esteja mapeado
        url_gerada = f"https://feedin.boka-a-boka.com.br/{modulo_ativo_slug}"

    acao_principal = {
        'texto': config_acao['texto'],
        'icone': config_acao['icone'],
        'url': url_gerada,
        'cor': config_acao['cor'],
        'target': '_blank'
    }

    # 1. Busca os vínculos tradicionais do Core (seguidores do local físico)
    vinculos_core = []
    if empresa.local_fisico:
        if hasattr(empresa.local_fisico, 'seguidores') and empresa.local_fisico.seguidores:
            vinculos_core = list(empresa.local_fisico.seguidores)
        elif hasattr(empresa.local_fisico, 'usuarios_vinculados') and empresa.local_fisico.usuarios_vinculados:
            vinculos_core = list(empresa.local_fisico.usuarios_vinculados)
        elif hasattr(empresa.local_fisico, 'vinculos') and empresa.local_fisico.vinculos:
            vinculos_core = list(empresa.local_fisico.vinculos)
        else:
            vinculos_core = VinculoUsuarioLocal.query.filter_by(local_id=empresa.local_fisico.id).all()

    ids_vinculados = {v.usuario_id for v in vinculos_core if hasattr(v, 'usuario_id') and v.usuario_id}

    # 2. Identifica autores de avaliações do módulo que ainda não estão na listagem
    for av in empresa.avaliacoes_empresas:
        if av.usuario_core_id and av.usuario_core_id not in ids_vinculados:
            usuario_avaliador = Usuario.query.get(av.usuario_core_id)
            if usuario_avaliador:
                class VinculoVirtual:
                    def __init__(self, usuario):
                        self.usuario = usuario
                        self.usuario_id = usuario.id

                vinculos_core.append(VinculoVirtual(usuario_avaliador))
                ids_vinculados.add(av.usuario_core_id)

    # 3. GARANTIA: Injeta o proprietário/empreendedor na lista se necessário
    ids_usuarios_ja_na_lista = {v.usuario_id for v in vinculos_core if hasattr(v, 'usuario_id') and v.usuario_id}

    if hasattr(empresa, 'id_empreendedor') and empresa.id_empreendedor:
        if empresa.id_empreendedor not in ids_usuarios_ja_na_lista:
            proprietario = Usuario.query.get(empresa.id_empreendedor)
            if proprietario:
                class VinculoTemporario:
                    def __init__(self, user):
                        self.usuario = user
                        self.usuario_id = user.id

                vinculos_core.insert(0, VinculoTemporario(proprietario))

    # 🌟 ORDENAÇÃO ALFABÉTICA DA COMUNIDADE PELO USERNAME
    if vinculos_core:
        vinculos_core = sorted(
            vinculos_core,
            key=lambda v: v.usuario.username.lower() if (
                    hasattr(v, 'usuario') and v.usuario and v.usuario.username) else ''
        )

    # 🌟 MAPEAMENTO DOS STATUS DE CONEXÃO DO USUÁRIO LOGADO
    status_conexoes_usuario = {}
    if current_user.is_authenticated:
        conexoes_usuario = Conexoes.query.filter(
            (Conexoes.id_remetente == current_user.id) | (Conexoes.id_destinatario == current_user.id)
        ).all()
        for c in conexoes_usuario:
            outro_id = c.id_destinatario if c.id_remetente == current_user.id else c.id_remetente
            status_conexoes_usuario[outro_id] = c.status

    # 🌟 PROCESSAMENTO DA GRADE SEMANAL E STATUS EM TEMPO REAL DE HORÁRIOS (GLOBAL/ANÔNIMO)
    dias_semana_nomes = [
        ("Segunda", 0),
        ("Terça", 1),
        ("Quarta", 2),
        ("Quinta", 3),
        ("Sexta", 4),
        ("Sábado", 5),
        ("Domingo", 6)
    ]

    dia_atual_index = datetime.datetime.now().weekday()
    grade_semanal = []

    horarios_cadastrados = EseHorarioFuncionamento.query.filter_by(empresa_id=empresa.id).all()

    horarios_por_dia = {}
    for h in horarios_cadastrados:
        dia_num = getattr(h, 'dia_semana', 0)
        if dia_num not in horarios_por_dia:
            horarios_por_dia[dia_num] = []
        horarios_por_dia[dia_num].append(h)

    for nome_dia, index in dias_semana_nomes:
        turnos_do_dia = horarios_por_dia.get(index, [])
        grade_semanal.append({
            'nome_dia': nome_dia,
            'eh_hoje': (index == dia_atual_index),
            'turnos': turnos_do_dia
        })

    def verificar_status_aberto_agora(turnos_hoje):
        if not turnos_hoje:
            return {"aberto": False, "texto": "Fechado hoje"}

        agora = datetime.datetime.now()
        hora_atual_minutos = agora.hour * 60 + agora.minute

        for turno in turnos_hoje:
            if turno.horario_abertura and turno.horario_fechamento:
                abertura_minutos = turno.horario_abertura.hour * 60 + turno.horario_abertura.minute
                fechamento_minutos = turno.horario_fechamento.hour * 60 + turno.horario_fechamento.minute

                if abertura_minutos <= hora_atual_minutos <= fechamento_minutos:
                    return {
                        "aberto": True,
                        "texto": f"Aberto agora • Fecha às {turno.horario_fechamento.strftime('%H:%M')}"
                    }

        return {"aberto": False, "texto": "Fechado no momento"}

    status_funcionamento = verificar_status_aberto_agora(horarios_por_dia.get(dia_atual_index, []))

    # 4. Busca os CPFs que possuem relacionamento comercial
    cpfs_clientes_empresa = {
        rel.cpf_hash for rel in EseRelacionamentoClienteEmpresa.query.filter_by(empresa_id=str(empresa.id)).all()
        if rel.cpf_hash
    }

    # 🌟 5. MAPEAMENTO UNIFICADO VIA get_rating_data()
    avaliacoes_por_cpf = {}
    avaliacoes_por_usuario_id = {}

    for item in rating_data.get('lista', []):
        comentario = item.get('comentario')
        autor = item.get('autor')
        if comentario and autor:
            if hasattr(autor, 'id'):
                avaliacoes_por_usuario_id[autor.id] = comentario
                avaliacoes_por_usuario_id[str(autor.id)] = comentario
            if hasattr(autor, 'cpf_hash') and autor.cpf_hash:
                avaliacoes_por_cpf[autor.cpf_hash] = comentario

    # 🛡️ TRAVA CONTRA LOOP INFINITO: Não faz redirect de volta para perfil_local!
    status_atual = getattr(empresa, 'status_homologacao', None)
    if status_atual not in ['concluido', 'provisorio_beta', 'homologado', 'verificado']:
        # Se a empresa não estiver liberada e não houver local, aborta 404 de forma limpa
        if not empresa.local_id:
            abort(404)

    tem_pro = empresa.possui_recurso_pro()
    plano_ativo = 'pro' if tem_pro else 'free'

    servicos = []
    colaboradores = []
    atividades = []
    epocas_todas = Epoca.query.order_by(Epoca.ordem_cronologica.asc()).all()

    if plano_ativo == 'pro':
        servicos = EseServicoOferecido.query.filter_by(empresa_id=empresa.id).all()
        colaboradores = ColaboradorContrato.query.filter_by(
            id_local=empresa.id,
            status_profissional='ativo'
        ).all()

    # 🏛️ RECUPERAÇÃO DAS ATIVIDADES DA LINHA DO TEMPO
    local = empresa.local_fisico
    if local:
        postagens_totais = Postagem.query.filter(
            Postagem.id_local == local.id,
            Postagem.ativo == True
        ).options(
            joinedload(Postagem.autor),
            joinedload(Postagem.tags_afinidade)
        ).all()

        for p in postagens_totais:
            atividades.append({
                'id': p.id,
                'tipo_card': 'postagem',
                'tipo': 'postagem',
                'data_criacao': p.data_criacao,
                'autor_objeto': p.autor,
                'autor': p.autor,
                'conteudo': p.conteudo,
                'objeto_original': p,
                'pessoas_marcadas': p.pessoas_marcadas_confirmadas,
                'id_usuario': p.id_usuario
            })
        atividades = sorted(atividades, key=lambda x: x['data_criacao'], reverse=True)

    return render_template(
        'empresa/perfil_empresa.html',
        empresa=empresa,
        rating_data=rating_data,
        pode_avaliar=pode_avaliar,
        contexto_modulo_atual=contexto_modulo_atual,
        local=local,
        plano_ativo=plano_ativo,
        acao_principal=acao_principal,
        servicos=servicos,
        colaboradores=colaboradores,
        atividades=atividades,
        epocas_todas=epocas_todas,
        vinculos_core=vinculos_core,
        cpfs_clientes_empresa=cpfs_clientes_empresa,
        avaliacoes_por_cpf=avaliacoes_por_cpf,
        avaliacoes_por_usuario_id=avaliacoes_por_usuario_id,
        status_conexoes_usuario=status_conexoes_usuario,
        grade_semanal=grade_semanal,
        status_funcionamento=status_funcionamento
    )


import datetime
def verificar_status_aberto_agora(empresa_id):
    agora = datetime.datetime.now()
    dia_atual_index = agora.weekday()  # 0 = Segunda, 6 = Domingo
    hora_atual_minutos = agora.hour * 60 + agora.minute

    turnos_hoje = EseHorarioFuncionamento.query.filter(
        ((EseHorarioFuncionamento.empresa_id == empresa_id) |
         (EseHorarioFuncionamento.empresa_id == str(empresa_id))) &
        (EseHorarioFuncionamento.dia_semana == dia_atual_index)
    ).all()

    if not turnos_hoje:
        return {"aberto": False, "texto": "Fechado hoje"}

    for turno in turnos_hoje:
        if turno.horario_abertura and turno.horario_fechamento:
            abertura_minutos = turno.horario_abertura.hour * 60 + turno.horario_abertura.minute
            fechamento_minutos = turno.horario_fechamento.hour * 60 + turno.horario_fechamento.minute

            if abertura_minutos <= hora_atual_minutos <= fechamento_minutos:
                return {
                    "aberto": True,
                    "texto": f"Aberto agora • Fecha às {turno.horario_fechamento.strftime('%H:%M')}"
                }

    return {"aberto": False, "texto": "Fechado no momento"}