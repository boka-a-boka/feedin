import os
import re
import secrets
import hashlib
import random

import traceback
import uuid
from typing import Dict, Any, List, Tuple, Optional
from functools import wraps
from decimal import Decimal
from werkzeug.exceptions import HTTPException
from copy import deepcopy
import pytz
from zoneinfo import ZoneInfo
from datetime import datetime, timedelta, date, time, timezone

from PIL import Image, ImageOps
from sqlalchemy import or_, distinct, exists, and_, func, case, Date

from sqlalchemy.orm import joinedload, selectinload, aliased

# 1. METODOLOGIAS DO FLASK & EXTENSÕES DE SESSÃO
from flask import (Blueprint, current_app, flash, jsonify, redirect, render_template, request, session,
                   url_for, send_from_directory, abort, make_response)
from flask_login import current_user, login_required, logout_user
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from werkzeug.utils import secure_filename

# 2. EXTENSÕES DE SEGURANÇA E CORE DO FEEDIN
from flask_bcrypt import check_password_hash, generate_password_hash
from feedin import database as db
from feedin import bcrypt  # Objetos centrais e extensões do Core
from feedin.middlewares import empresa_acesso_required

# 3. UTILS DO SISTEMA
import feedin.utils as utils
from utils import (_parse_profissional_id, converter_duracao_para_minutos, salvar_imagem_modulo,)

# 4. 🧩 CONTRATO OFICIAL DO BLUEPRINT E QUERIES DO MÓDULO AGENDA
# Mantido em ÚNICO local para evitar duplicidade e desconexão de rotas
from feedin.modules.agenda import agenda_bp
from feedin.modules.agenda.queries import buscar_agendamentos_dia, buscar_metricas_diarias

# 5. FORMULÁRIOS (WTFORMS) - INTERNOS E INTER-MÓDULOS
from feedin.modules.agenda.forms import (
    FormHabilitarModulo,
    FormCadastroBalcao,
    FormColaborador,
    FormConfigMarca,
    FormCredenciamentoLocal,
    FormServico
)

# 6. 🗄️ PERSISTÊNCIA: MODELOS DO BANCO DE DADOS
# Modelos da Estrutura Base (Core)
from feedin.models import (
    HistoricoOcupacaoLocal, Local, Usuario, IdentidadeCivil,
    Epoca, VinculoUsuarioLocal, UsuarioLocalEpoca, Taxonomia, ModulosSistema,
)

# Modelos do módulo parceiro Auth
from feedin.modules.auth.models import (
    ModCadastroCliente, ModFilaAtivacaoCliente, AthAtribContexto, ModVinculoModulo
)

# Modelos específicos do próprio módulo Agenda
from feedin.modules.agenda.models import (
    AghAgendamento, AghAgendamentoItem, AghServico, AghAgendamentoRascunho, AghAgendamentoRascunhoItem,
    AghNotificacao,  AghConfiguracaoAgenda, AghAgendamentoEncerramento, AghHistoricoPresenca,
    AghSolicitacaoReagendamento, AghCancelamento, AghReordenacaoSolicitada,
)

# Modelos do módulo parceiro Empresa necessários para regras de negócio da agenda
from feedin.modules.empresa.models import (
    EseEmpresa, UsuarioFavorito, ColaboradorContrato, EseExcecaoCalendario, CadastroFeriado,
    EseNotificacaoCliente, EseServicoOferecido, EseServicoPreco, EseColaboradorServicoHabilidade,
    EseHorarioFuncionamento, EmpresaCalendarioExcecao, EscalaTrabalhoColaborador, EseProcessoClaim,
    EseConviteColaborador, ClienteBeneficiario, ClienteContato, ClienteEndereco, EseNotificacao,
    EseRegraPontuacao, ModClientePontos, EseLogMensagemAutomatica, ModEmpresaModulo
)

from feedin.modules.agenda.services.agendamento_service import (processar_solicitacao_reagendamento)
from weasyprint import HTML
from feedin.modules.agenda.services.agendamento_service import (_encontrar_colaborador_apto,)

@agenda_bp.route(
    "/api/empresa/<int:empresa_id>/dias-desabilitados",
    methods=["GET"],
    strict_slashes=False,
)
def api_dias_desabilitados(empresa_id):
  """Retorna os dias da semana desabilitados e as datas bloqueadas em uma janela de 180 dias,

  combinando o calendário civil (nacional/estadual/municipal) com as regras
  da empresa.
  """

  from feedin.utils import _parse_profissional_id

  profissional_id = _parse_profissional_id(
      request.args.get("profissional_id")
  )
  hoje = date.today()
  limite_futuro = hoje + timedelta(days=180)

  # 1. Mapeamento geográfico para validação de feriados civis
  local = Local.query.get(empresa_id)
  uf_empresa = local.estado.strip().upper() if local and local.estado else None
  cidade_empresa = local.cidade.strip() if local and local.cidade else None

  locais_civis_validos = ["BR"]
  if uf_empresa:
    locais_civis_validos.append(uf_empresa)
  if cidade_empresa:
    locais_civis_validos.append(cidade_empresa)

  # 2. Definição da grade semanal (Empresa vs Profissional)
  if profissional_id:
    dias_com_escala = (
        db.session.query(EscalaTrabalhoColaborador.dia_semana)
        .filter(
            EscalaTrabalhoColaborador.contrato_id == profissional_id,
            EscalaTrabalhoColaborador.ativo == True,
        )
        .distinct()
        .all()
    )
    dias_abertos_set = {d[0] for d in dias_com_escala}
  else:
    dias_com_expediente = (
        db.session.query(EseHorarioFuncionamento.dia_semana)
        .filter_by(empresa_id=empresa_id)
        .distinct()
        .all()
    )
    dias_abertos_set = {d[0] for d in dias_com_expediente}

  dias_semana_fechados = [
      dia for dia in range(7) if dia not in dias_abertos_set
  ]

  # 3. Camada 1: Calendário Civil (Feriados Nacionais, Estaduais e Municipais)
  feriados_oficiais = CadastroFeriado.query.filter(
      CadastroFeriado.data >= hoje,
      CadastroFeriado.data <= limite_futuro,
      CadastroFeriado.abrangencia != "sazonal",
      or_(
          CadastroFeriado.abrangencia == "nacional",
          CadastroFeriado.localidade.in_(locais_civis_validos),
      ),
  ).all()

  datas_bloqueadas_set = {f.data for f in feriados_oficiais}

  # 4. Camada 2: Exceções e Sobreposições da Empresa
  excecoes_empresa = EmpresaCalendarioExcecao.query.filter(
      EmpresaCalendarioExcecao.empresa_id == empresa_id,
      EmpresaCalendarioExcecao.data_excecao >= hoje,
      EmpresaCalendarioExcecao.data_excecao <= limite_futuro,
  ).all()

  for exc in excecoes_empresa:
    if exc.trabalha_no_dia:
      datas_bloqueadas_set.discard(exc.data_excecao)
    else:
      datas_bloqueadas_set.add(exc.data_excecao)

  datas_bloqueadas = sorted(
      [d.strftime("%Y-%m-%d") for d in datas_bloqueadas_set]
  )

  return jsonify({
      "dias_semana_fechados": dias_semana_fechados,
      "datas_bloqueadas": datas_bloqueadas,
  })


def obter_hora_local():
    # Retorna o datetime exato no fuso horário de São Paulo / Brasília
    return datetime.now(ZoneInfo("America/Sao_Paulo"))

# Uso:
agora = obter_hora_local()
print(f"Data e Hora Atual: {agora.strftime('%Y-%m-%d %H:%M:%S')}")


# --------------------------------------------------------------------------
# 🗓️ ROTA: /api/horarios-disponiveis (COM CAST DE DURAÇÃO PARA INT)
# --------------------------------------------------------------------------
@agenda_bp.route("/api/horarios-disponiveis", methods=["GET"], strict_slashes=False)
def api_horarios_disponiveis():
    from datetime import datetime, timedelta, time
    import pytz
    from sqlalchemy import or_, and_, func

    empresa_id = request.args.get('empresa_id', type=int)
    data_str = request.args.get('data') or request.args.get('data_agendamento')
    servicos_ids_param = request.args.get('servicos') or request.args.get('servico_ids') or request.args.get(
        'servico_id')
    profissional_id_param = request.args.get('profissional_id') or request.args.get('colaborador_id')

    if not empresa_id or not data_str:
        return jsonify({'erro': 'Parâmetros obrigatórios ausentes'}), 400

    try:
        data_solicitada = datetime.strptime(data_str, '%Y-%m-%d').date()
    except ValueError:
        return jsonify({'erro': 'Formato de data inválido. Use YYYY-MM-DD'}), 400

    profissional_id = _parse_profissional_id(profissional_id_param) if profissional_id_param else None

    # Parse higienizado dos IDs dos serviços
    servico_ids = []
    if servicos_ids_param:
        string_limpa = str(servicos_ids_param).replace('[', '').replace(']', '').replace("'", "").replace('"', '')
        servico_ids = [int(s.strip()) for s in string_limpa.split(',') if s.strip().isdigit()]

    tz_br = pytz.timezone('America/Sao_Paulo')
    agora_local = datetime.now(pytz.utc).astimezone(tz_br).replace(tzinfo=None)
    hoje_local = agora_local.date()
    data_consulta = data_solicitada

    def responder_slots(slots_lista, status='ok', modo='individual'):
        return jsonify({
            'data_utilizada': data_consulta.strftime('%Y-%m-%d'),
            'data_alterada': False,
            'status': status,
            'modo_agendamento': modo,
            'horarios': sorted(list(slots_lista))
        })

    if data_consulta < hoje_local:
        return responder_slots([], status='passado')

    # Mapeamento do dia da semana (0 = Segunda, 6 = Domingo)
    dia_semana_num = data_consulta.weekday()

    # 1. Checa trava de exceção (Feriados/Folgas da Empresa)
    excecao_bloqueio = EmpresaCalendarioExcecao.query.filter(
        EmpresaCalendarioExcecao.empresa_id == empresa_id,
        EmpresaCalendarioExcecao.data_excecao == data_consulta,
        EmpresaCalendarioExcecao.trabalha_no_dia == False
    ).first()

    if excecao_bloqueio:
        return responder_slots([], status='fechado')

    # 2. Carrega horários de funcionamento da empresa para o dia
    horarios_empresa = EseHorarioFuncionamento.query.filter(
        EseHorarioFuncionamento.empresa_id == empresa_id,
        EseHorarioFuncionamento.dia_semana == dia_semana_num
    ).order_by(EseHorarioFuncionamento.horario_abertura).all()

    if not horarios_empresa:
        return responder_slots([], status='fechado')

    # Valida encerramento do expediente no dia atual
    if data_consulta == hoje_local:
        maior_fechamento = max(h.horario_fechamento for h in horarios_empresa)
        if agora_local.time() >= maior_fechamento:
            return responder_slots([], status='expediente_encerrado')

    janelas_empresa = [
        (datetime.combine(data_consulta, h.horario_abertura), datetime.combine(data_consulta, h.horario_fechamento))
        for h in horarios_empresa
    ]

    # 3. Carrega e valida os serviços solicitados
    servicos_objetos = []
    if servico_ids:
        servicos_objetos = EseServicoOferecido.query.filter(
            EseServicoOferecido.id.in_(servico_ids),
            EseServicoOferecido.empresa_id == empresa_id
        ).all()

    if not servicos_objetos:
        return responder_slots([], status='servico_nao_encontrado')

    # 4. Pré-carregamento em lote
    inicio_dia = datetime.combine(data_consulta, time.min)
    fim_dia = datetime.combine(data_consulta, time.max)

    # Agendamentos do dia (Ocupações)
    agendamentos_dia = AghAgendamento.query.filter(
        AghAgendamento.empresa_id == empresa_id,
        AghAgendamento.data_hora_inicio >= inicio_dia,
        AghAgendamento.data_hora_inicio <= fim_dia,
        AghAgendamento.status.notin_(["cancelado", "recusado"]),
        or_(
            AghAgendamento.status != "soft_lock",
            and_(AghAgendamento.status == "soft_lock", AghAgendamento.expira_em > agora_local)
        )
    ).all()

    # Mapeia ocupações por colaborador: { contrato_id: [(inicio, fim), ...] }
    mapa_ocupacoes = {}
    for ag in agendamentos_dia:
        c_id = ag.profissional_id or ag.contrato_id
        if c_id not in mapa_ocupacoes:
            mapa_ocupacoes[c_id] = []
        mapa_ocupacoes[c_id].append((ag.data_hora_inicio, ag.data_hora_fim))

    # Escalas de trabalho ativas para a data (JOIN com ColaboradorContrato)
    escalas_dia = EscalaTrabalhoColaborador.query.join(
        ColaboradorContrato, EscalaTrabalhoColaborador.contrato_id == ColaboradorContrato.id
    ).filter(
        ColaboradorContrato.id_local == empresa_id,
        EscalaTrabalhoColaborador.ativo == True,
        EscalaTrabalhoColaborador.data_inicio <= data_consulta,
        or_(EscalaTrabalhoColaborador.data_fim >= data_consulta, EscalaTrabalhoColaborador.data_fim.is_(None)),
        or_(EscalaTrabalhoColaborador.dia_semana == dia_semana_num, EscalaTrabalhoColaborador.dia_semana.is_(None))
    ).all()

    # Mapeia blocos de trabalho por colaborador
    mapa_escalas = {}
    for esc in escalas_dia:
        c_id = esc.contrato_id
        if c_id not in mapa_escalas:
            mapa_escalas[c_id] = []

        ini_c = datetime.combine(data_consulta, esc.inicio_expediente) if getattr(esc, 'inicio_expediente',
                                                                                  None) else None
        fim_c = datetime.combine(data_consulta, esc.fim_expediente) if getattr(esc, 'fim_expediente', None) else None
        int_ini = datetime.combine(data_consulta, esc.inicio_intervalo) if getattr(esc, 'inicio_intervalo',
                                                                                   None) else None
        int_fim = datetime.combine(data_consulta, esc.fim_intervalo) if getattr(esc, 'fim_intervalo', None) else None

        if ini_c and fim_c:
            mapa_escalas[c_id].append((ini_c, fim_c, int_ini, int_fim))

    # Helper de validação em memória
    def checar_disponibilidade_memoria(c_id, slot_inicio, duracao_min):
        slot_fim = slot_inicio + timedelta(minutes=int(duracao_min))
        blocos = mapa_escalas.get(c_id, [])

        if not blocos:
            menor_abertura = min(h.horario_abertura for h in horarios_empresa)
            maior_fechamento = max(h.horario_fechamento for h in horarios_empresa)
            blocos = [(
                datetime.combine(data_consulta, menor_abertura),
                datetime.combine(data_consulta, maior_fechamento),
                None, None
            )]

        ocupacoes = mapa_ocupacoes.get(c_id, [])

        for ini_c, fim_c, int_ini, int_fim in blocos:
            for emp_ini, emp_fim in janelas_empresa:
                ini_efetivo = max(emp_ini, ini_c)
                fim_efetivo = min(emp_fim, fim_c)

                if slot_inicio >= ini_efetivo and slot_fim <= fim_efetivo:
                    if int_ini and int_fim:
                        if slot_inicio < int_fim and slot_fim > int_ini:
                            return False
                    for oc_ini, oc_fim in ocupacoes:
                        if slot_inicio < oc_fim and slot_fim > oc_ini:
                            return False
                    return True
        return False

    # Helper para extrair a duração garantindo tipo inteiro (int)
    def obter_duracao(s_obj):
        raw_val = getattr(s_obj, 'duracao_minutos', None) or getattr(s_obj, 'tempo_duracao', None) or 30
        try:
            return int(raw_val)
        except (ValueError, TypeError):
            return 30

    duracao_total_combo = sum(obter_duracao(s) for s in servicos_objetos)
    passo_slot = timedelta(minutes=30)

    # =========================================================================
    # TENTATIVA 1: PROFISSIONAL ÚNICO
    # =========================================================================
    if profissional_id and profissional_id > 0:
        contratos_unicos = [profissional_id]
    else:
        habilidades_validas = (
            db.session.query(EseColaboradorServicoHabilidade.contrato_id)
            .filter(EseColaboradorServicoHabilidade.servico_oferecido_id.in_(servico_ids))
            .group_by(EseColaboradorServicoHabilidade.contrato_id)
            .having(func.count(EseColaboradorServicoHabilidade.servico_oferecido_id) == len(servico_ids))
            .all()
        )
        contratos_unicos = [h.contrato_id for h in habilidades_validas]

    slots_unificados = set()

    if contratos_unicos:
        menor_inicio_empresa = min(h.horario_abertura for h in horarios_empresa)
        maior_fim_empresa = max(h.horario_fechamento for h in horarios_empresa)
        cursor = datetime.combine(data_consulta, menor_inicio_empresa)
        horario_limite = datetime.combine(data_consulta, maior_fim_empresa)

        while cursor + timedelta(minutes=duracao_total_combo) <= horario_limite:
            if not (data_consulta == hoje_local and cursor <= agora_local):
                for c_id in contratos_unicos:
                    if checar_disponibilidade_memoria(c_id, cursor, duracao_total_combo):
                        slots_unificados.add(cursor.strftime("%H:%M"))
                        break
            cursor += passo_slot

    if slots_unificados:
        return responder_slots(slots_unificados, status='ok', modo='individual')

    # =========================================================================
    # TENTATIVA 2 (FALLBACK): MULTI-COLABORADOR SEQUENCIAL
    # =========================================================================
    mapa_profissionais_por_servico = {}
    for s_obj in servicos_objetos:
        habs = EseColaboradorServicoHabilidade.query.filter_by(servico_oferecido_id=s_obj.id).all()
        mapa_profissionais_por_servico[s_obj.id] = [h.contrato_id for h in habs]

    slots_multi = set()
    menor_inicio_empresa = min(h.horario_abertura for h in horarios_empresa)
    maior_fim_empresa = max(h.horario_fechamento for h in horarios_empresa)
    cursor = datetime.combine(data_consulta, menor_inicio_empresa)
    horario_limite = datetime.combine(data_consulta, maior_fim_empresa)

    while cursor + timedelta(minutes=duracao_total_combo) <= horario_limite:
        if data_consulta == hoje_local and cursor <= agora_local:
            cursor += passo_slot
            continue

        posivel_inicio_cadeia = cursor
        tempo_acumulado = cursor
        cadeia_valida = True

        for s_obj in servicos_objetos:
            duracao_s = obter_duracao(s_obj)
            candidatos = mapa_profissionais_por_servico.get(s_obj.id, [])

            alguem_disponivel = any(
                checar_disponibilidade_memoria(c_id, tempo_acumulado, duracao_s)
                for c_id in candidatos
            )

            if alguem_disponivel:
                tempo_acumulado += timedelta(minutes=duracao_s)
            else:
                cadeia_valida = False
                break

        if cadeia_valida:
            slots_multi.add(posivel_inicio_cadeia.strftime("%H:%M"))

        cursor += passo_slot

    if slots_multi:
        return responder_slots(slots_multi, status='ok', modo='multi_colaborador')

    return responder_slots([], status='sem_horarios_disponiveis')


TEMPO_SOFT_LOCK_MINUTOS = 10


@agenda_bp.route('/empresa/<int:empresa_id>/dashboard', methods=['GET'])
@login_required
def dashboard_empresa(empresa_id):
    """
    📌 DASHBOARD OPERACIONAL DA EMPRESA
    """
    empresa = EseEmpresa.query.get_or_404(empresa_id)
    agora_tz = datetime.now(ZoneInfo('America/Sao_Paulo')).replace(tzinfo=None)
    hoje = agora_tz.date()

    # =========================================================================
    # 🎯 0. CONTROLE DE ACESSO E PERMISSÕES (ESTRITAMENTE VIA COLABORADORCONTRATO)
    # =========================================================================
    usuario_uuid = str(getattr(current_user, 'uuid', getattr(current_user, 'id', ''))).strip().lower()

    # Busca o contrato ativo do usuário logado para esta empresa específica
    contrato_colaborador = ColaboradorContrato.query.filter(
        ColaboradorContrato.id_local == empresa_id,
        ColaboradorContrato.id_cadastro_cliente == usuario_uuid,
        ColaboradorContrato.status_profissional == 'ativo'
    ).first()

    e_colaborador = contrato_colaborador is not None
    is_super_admin = bool(getattr(current_user, 'is_admin', False))

    # Nível do papel vem DIRETAMENTE do campo Integer 'papel_nivel' da ColaboradorContrato
    papel_nivel = contrato_colaborador.papel_nivel if e_colaborador else 0

    # 🔒 REGRA DE GESTÃO REAL:
    # Nível 500 = Operacional Padrão.
    # Nível >= 600 ou Super Admin = Gestão da Empresa.
    e_gestor = (papel_nivel >= 600) or is_super_admin

    if not e_gestor and not e_colaborador:
        flash("Você não tem permissão para acessar a área operacional desta empresa.", "warning")
        return redirect(url_for('cliente.dashboard'))

    pode_gerenciar = e_gestor

    # =========================================================================
    # 🎯 1. LEITURA DOS FILTROS E BLINDAGEM POR PERFIL
    # =========================================================================
    data_str = request.args.get('data')
    if data_str:
        try:
            data_filtro = datetime.strptime(data_str, '%Y-%m-%d').date()
        except ValueError:
            data_filtro = hoje
    else:
        data_filtro = hoje

    prof_id_param = request.args.get('profissional_id', type=int)

    if not pode_gerenciar:
        # 👤 COLABORADOR OPERACIONAL (Nível < 600, ex: 500)
        # Força filtragem estritamente pelo ID do contrato (Integer)
        ids_filtro_sql = [contrato_colaborador.id]
        prof_id_param = contrato_colaborador.id
    else:
        # 👔 GESTOR DA EMPRESA (Nível >= 600 ou Super Admin)
        # Se selecionou um profissional no combo, filtra por ele.
        # Se NÃO selecionou (None), exibe a visão GERAL de todos os funcionários.
        if prof_id_param is not None:
            ids_filtro_sql = [prof_id_param]
        else:
            ids_filtro_sql = None  # None = Visão geral de todos

    # Conjunto de Strings para filtragem rápida em memória dos itens de agendamento
    prof_ids_permitidos_str = {str(x).strip().lower() for x in ids_filtro_sql} if ids_filtro_sql else None

    inicio_dia = datetime.combine(data_filtro, time.min)
    fim_dia = datetime.combine(data_filtro, time.max)

    # =========================================================================
    # 🎯 2. CÁLCULO DO TEMPO MÍNIMO (T_min) DA EMPRESA
    # =========================================================================
    servicos_oferecidos = EseServicoOferecido.query.filter_by(empresa_id=empresa_id).all()
    duracoes_validas = [s.duracao_em_minutos for s in servicos_oferecidos if getattr(s, 'duracao_em_minutos', 0) > 0]
    tempo_minimo_empresa = min(duracoes_validas) if duracoes_validas else 30

    # =========================================================================
    # 🎯 3. FILA OPERACIONAL EM TEMPO REAL (BASEADA 100% NOS ITENS)
    # =========================================================================
    query_em_andamento = AghAgendamento.query.filter(
        AghAgendamento.empresa_id == empresa_id,
        AghAgendamento.status == 'em_atendimento',
        AghAgendamento.data_hora_inicio >= inicio_dia,
        AghAgendamento.data_hora_inicio <= fim_dia
    )

    query_proximo_espera = AghAgendamento.query.filter(
        AghAgendamento.empresa_id == empresa_id,
        AghAgendamento.status.in_(['aguardando', 'confirmado', 'agendado']),
        AghAgendamento.data_hora_inicio >= inicio_dia,
        AghAgendamento.data_hora_inicio <= fim_dia
    )

    if ids_filtro_sql:
        # 🎯 OPERACIONAL: Consulta estritamente os itens vinculados ao colaborador
        query_em_andamento = query_em_andamento.filter(
            AghAgendamento.itens.any(AghAgendamentoItem.profissional_id.in_(ids_filtro_sql))
        )
        query_proximo_espera = query_proximo_espera.filter(
            AghAgendamento.itens.any(AghAgendamentoItem.profissional_id.in_(ids_filtro_sql))
        )

    fila_operacional = {
        'em_andamento': query_em_andamento.order_by(AghAgendamento.data_hora_inicio.asc()).first(),
        'proximo': query_proximo_espera.order_by(AghAgendamento.data_hora_inicio.asc()).first()
    }

    # =========================================================================
    # 🎯 4. CONSULTA DE AGENDAMENTOS DO DIA (FILTRAGEM POR ITENS EXECUTADOS)
    # =========================================================================
    query_agendamentos = AghAgendamento.query.options(
        joinedload(AghAgendamento.cliente),
        joinedload(AghAgendamento.itens).joinedload(AghAgendamentoItem.servico),
        joinedload(AghAgendamento.itens).joinedload(AghAgendamentoItem.profissional)
    ).filter(
        AghAgendamento.empresa_id == empresa_id,
        AghAgendamento.data_hora_inicio >= inicio_dia,
        AghAgendamento.data_hora_inicio <= fim_dia
    )

    if ids_filtro_sql:
        # Traz apenas os agendamentos que contêm itens sob responsabilidade deste colaborador
        query_agendamentos = query_agendamentos.filter(
            AghAgendamento.itens.any(AghAgendamentoItem.profissional_id.in_(ids_filtro_sql))
        )

    todos_agendamentos = query_agendamentos.all()

    # =========================================================================
    # 🎯 5. APURAÇÃO CONSISTENTE DAS MÉTRICAS E MAPEAMENTO
    # =========================================================================
    agendamentos_ocupados = []

    metricas_hoje = {
        'total': 0,
        'concluidos': 0,
        'cancelados_faltas': 0,
        'abandonos': 0,
        'em_atendimento': 0,
        'pendentes': 0,
        'atrasos_abertura': 0
    }

    status_map = {
        'agendado': {'label': 'Agendado', 'badge': 'bg-primary'},
        'confirmado': {'label': 'Confirmado', 'badge': 'bg-info text-dark'},
        'aguardando': {'label': 'Aguardando', 'badge': 'bg-warning text-dark'},
        'em_atendimento': {'label': 'Em Atendimento', 'badge': 'bg-primary'},
        'concluido': {'label': 'Concluído', 'badge': 'bg-success'},
        'finalizado': {'label': 'Finalizado', 'badge': 'bg-success'},
        'cancelado': {'label': 'Cancelado', 'badge': 'bg-danger'},
        'falta': {'label': 'Falta / Ausente', 'badge': 'bg-secondary'},
        'ausente_pendente': {'label': 'Ausente', 'badge': 'bg-secondary'},
        'pendente': {'label': 'Pendente', 'badge': 'bg-warning text-dark'},
    }

    TOLERANCIA_MINUTOS = 5

    for item in todos_agendamentos:
        raw_status = getattr(item, 'status', None)
        status_key_orig = raw_status.value if hasattr(raw_status, 'value') else raw_status
        st = str(status_key_orig).lower().strip() if status_key_orig is not None else ''

        metricas_hoje['total'] += 1

        if st in ['concluido', 'finalizado']:
            metricas_hoje['concluidos'] += 1
        elif st in ['cancelado', 'falta', 'ausente_pendente'] or getattr(item, 'marcado_como_ausente_em', None):
            metricas_hoje['cancelados_faltas'] += 1
            if st in ['falta', 'ausente_pendente'] or getattr(item, 'marcado_como_ausente_em', None):
                metricas_hoje['abandonos'] += 1
        elif st == 'em_atendimento':
            metricas_hoje['em_atendimento'] += 1
            metricas_hoje['pendentes'] += 1
        else:
            metricas_hoje['pendentes'] += 1

        nome_cliente = "Cliente Avulso"
        foto_cliente = ""  # 🎯 INICIALIZAÇÃO DEFENSIVA DA FOTO

        if getattr(item, 'cliente', None):
            nome_cliente = getattr(item.cliente, 'nome', None) or getattr(item.cliente, 'razao_social',
                                                                          None) or "Cliente Avulso"

            # 🎯 RESGATE DEFENSIVO DO CAMPO DE FOTO/AVATAR DO CLIENTE
            foto_cliente = getattr(item.cliente, 'foto_url', None) or getattr(item.cliente, 'foto', None) or getattr(
                item.cliente, 'avatar', None) or ""

            # 🎯 DESMEMBRAMENTO POR ITEM COM SUPORTE A ORDEM E EXECUÇÃO SIMULTÂNEA
            if getattr(item, 'itens', None) and len(item.itens) > 0:

                # Ordena os itens da comanda pela ordem definida na reunião técnica/equipe
                itens_ordenados = sorted(item.itens, key=lambda x: getattr(x, 'ordem_execucao', 1))

                cursor_horario = item.data_hora_inicio
                inicio_item_anterior = item.data_hora_inicio

                for sub_index, it in enumerate(itens_ordenados):
                    prof_item_id_raw = getattr(it, 'profissional_id', None)
                    if not prof_item_id_raw:
                        continue

                    prof_item_id_str = str(prof_item_id_raw).strip().lower()

                    # Se houver filtro ativo na tela (ex: Colaborador nível 500 vendo apenas a sua agenda)
                    if prof_ids_permitidos_str and prof_item_id_str not in prof_ids_permitidos_str:
                        duracao_servico = getattr(it.servico, 'duracao_em_minutos', 30) if getattr(it, 'servico',
                                                                                                   None) else 30
                        modo = getattr(it, 'modo_execucao', 'sequencial')

                        if modo == 'sequencial':
                            inicio_item_anterior = cursor_horario
                            cursor_horario = cursor_horario + timedelta(minutes=duracao_servico)
                        continue

                    prof_obj = getattr(it, 'profissional', None)
                    prof_item_nome = resolver_nome_profissional(
                        prof_obj) if prof_obj else f"Profissional #{prof_item_id_raw}"
                    duracao_servico = getattr(it.servico, 'duracao_em_minutos', 30) if getattr(it, 'servico',
                                                                                               None) else 30

                    modo = getattr(it, 'modo_execucao', 'sequencial')

                    if modo == 'simultaneo' and sub_index > 0:
                        inicio_item_dt = inicio_item_anterior
                        fim_item_dt = inicio_item_dt + timedelta(minutes=duracao_servico)
                    else:
                        inicio_item_dt = cursor_horario
                        fim_item_dt = inicio_item_dt + timedelta(minutes=duracao_servico)
                        inicio_item_anterior = inicio_item_dt
                        cursor_horario = fim_item_dt

                    # Status e Atrasos do Item
                    item_status = getattr(it, 'status_item', getattr(it, 'status', st))
                    item_st = str(item_status.value if hasattr(item_status, 'value') else item_status).lower().strip()

                    limite_inicio_item = inicio_item_dt + timedelta(minutes=TOLERANCIA_MINUTOS)
                    esta_atrasado_item = (
                            data_filtro == hoje and
                            item_st in ['agendado', 'confirmado', 'aguardando', 'pendente'] and
                            agora_tz > limite_inicio_item
                    )

                    if esta_atrasado_item:
                        metricas_hoje['atrasos_abertura'] += 1

                    info_status = deepcopy(
                        status_map.get(item_st, {'label': item_st.capitalize(), 'badge': 'bg-secondary'}))
                    if esta_atrasado_item:
                        info_status['label'] = 'Atrasado'
                        info_status['badge'] = 'bg-danger text-white'

                    nome_servico = getattr(it.servico, 'nome', 'Serviço Geral') if getattr(it, 'servico',
                                                                                           None) else 'Serviço Geral'
                    val_un = getattr(it, 'preco_unitario', None)
                    valor_fmt = f"R$ {val_un:,.2f}".replace('.', ',') if val_un is not None else "R$ 0,00"

                    agendamentos_ocupados.append({
                        'tipo': 'ocupado',
                        'id': f"{item.id}_{it.id}",
                        'agendamento_id': item.id,
                        'item_id': it.id,
                        'profissional_id': prof_item_id_str,
                        'profissional_nome': prof_item_nome,
                        'inicio_dt': inicio_item_dt,
                        'fim_dt': fim_item_dt,
                        'fim_bloqueio_dt': fim_item_dt,
                        'hora_inicio': inicio_item_dt.strftime('%H:%M'),
                        'hora_fim': fim_item_dt.strftime('%H:%M'),
                        'hora_fim_bloqueio': fim_item_dt.strftime('%H:%M'),
                        'cliente_nome': nome_cliente,
                        'cliente_foto': foto_cliente,  # 🎯 CHAVE ADICIONADA AQUI!
                        'cliente': item.cliente,
                        # 🎯 PASSAGEM DO OBJETO CLIENTE PARA GARANTIR RETROCOMPATIBILIDADE NO JINJA
                        'servico_nome': nome_servico,
                        'valor_total': valor_fmt,
                        'status_slug': 'atrasado' if esta_atrasado_item else item_st,
                        'status_label': info_status['label'],
                        'status_badge_class': info_status['badge'],
                        'esta_atrasado': esta_atrasado_item,
                        'modo_execucao': modo,
                        'ordem_execucao': getattr(it, 'ordem_execucao', sub_index + 1)
                    })

    # =========================================================================
    # 🎯 6. CONSTRUÇÃO DINÂMICA DA GRADE OPERACIONAL
    # =========================================================================
    grade_completa = []
    hora_atual_dt = datetime.combine(data_filtro, time(8, 0))
    limite_padrao_dt = datetime.combine(data_filtro, time(21, 0))

    if agendamentos_ocupados:
        maior_fim_agendamento = max(ag['fim_bloqueio_dt'] for ag in agendamentos_ocupados)
        hora_limite_dt = max(limite_padrao_dt, maior_fim_agendamento)
    else:
        hora_limite_dt = limite_padrao_dt

    agendamentos_ocupados.sort(key=lambda x: x['inicio_dt'])

    while hora_atual_dt < hora_limite_dt:
        ags_no_horario = [ag for ag in agendamentos_ocupados if
                          ag['inicio_dt'] <= hora_atual_dt < ag['fim_bloqueio_dt']]

        if ags_no_horario:
            for ag in ags_no_horario:
                grade_completa.append(ag)
            hora_atual_dt = max(ag['fim_bloqueio_dt'] for ag in ags_no_horario)
        else:
            proximos = [ag for ag in agendamentos_ocupados if ag['inicio_dt'] > hora_atual_dt]
            fim_slot_livre = hora_atual_dt + timedelta(minutes=tempo_minimo_empresa)

            if proximos and proximos[0]['inicio_dt'] < fim_slot_livre:
                fim_slot_livre = proximos[0]['inicio_dt']

            if fim_slot_livre > hora_limite_dt:
                fim_slot_livre = hora_limite_dt

            if hora_atual_dt < fim_slot_livre:
                grade_completa.append({
                    'tipo': 'livre',
                    'hora_inicio': hora_atual_dt.strftime('%H:%M'),
                    'hora_fim': fim_slot_livre.strftime('%H:%M'),
                    'profissional_id': str(prof_id_param) if prof_id_param else ''
                })

            hora_atual_dt = fim_slot_livre

    # =========================================================================
    # 🎯 7. PROFISSIONAIS ATIVOS PARA EXIBIÇÃO NO DROPDOWN
    # =========================================================================
        # =========================================================================
        # 🎯 7. PROFISSIONAIS ATIVOS PARA EXIBIÇÃO NO DROPDOWN
        # =========================================================================
        if pode_gerenciar:
            # Gestor recebe a lista de todos os colaboradores ativos para montar o combo de filtro
            profissionais_ativos = ColaboradorContrato.query.filter_by(
                id_local=empresa_id, status_profissional='ativo'
            ).all()
        else:
            # Operacional (nível 500) NÃO recebe lista -> o combo não é renderizado
            profissionais_ativos = []

    usuario_uuid_lower = usuario_uuid.strip().lower()
    notificacoes_empresa = []
    total_notificacoes_nao_lidas = 0

    try:
        base_query_notif = AghNotificacao.query.filter(
            func.lower(AghNotificacao.destinatario_id) == usuario_uuid_lower,
            AghNotificacao.lida == False,
            or_(
                AghNotificacao.empresa_id == empresa_id,
                AghNotificacao.empresa_id.is_(None)
            )
        )

        total_notificacoes_nao_lidas = base_query_notif.count()
        notificacoes_empresa = (
            base_query_notif
            .order_by(AghNotificacao.criado_em.desc())
            .limit(10)
            .all()
        )
    except Exception as e:
        print(f"[DEBUG DASHBOARD EMPRESA] Erro ao buscar notificações: {e}")

    # =========================================================================
    # 🎯 8. RETORNO OBRIGATÓRIO
    # =========================================================================
    return render_template(
        'agenda/dashboard_empresa.html',
        empresa=empresa,
        data_filtro=data_filtro,
        prof_id_param=prof_id_param,  # Será None para Gestores (visão de todos) ou o ID específico
        tempo_minimo_empresa=tempo_minimo_empresa,
        fila_operacional=fila_operacional,
        metricas_hoje=metricas_hoje,
        grade_completa=grade_completa,
        profissionais_ativos=profissionais_ativos,
        total_notificacoes_nao_lidas=total_notificacoes_nao_lidas,
        notificacoes=notificacoes_empresa,
        pode_gerenciar=pode_gerenciar,
        e_colaborador=e_colaborador,
        colaboradorcontrato=contrato_colaborador,
        hoje=hoje
    )


# =========================================================================
# 2. ROTA DO BALCÃO (OPERAÇÃO / FRENTE DE LOJA)
# =========================================================================
@agenda_bp.route('/empresa/<int:empresa_id>/balcao', methods=['GET'])
@login_required
def agenda_balcao(empresa_id):
    empresa = EseEmpresa.query.get_or_404(empresa_id)
    hoje = datetime.now()

    profissionais_ativos = getattr(empresa, 'profissionais', [])
    eh_proprietario = True
    cargo_atribuicao = "Gerente" if eh_proprietario else "Profissional"
    contrato_ativo = None

    # Contagem de notificações pendentes para o balcão
    usuario_uuid_lower = str(current_user.id).strip().lower()
    total_notificacoes_nao_lidas = 0
    try:
        total_notificacoes_nao_lidas = AghNotificacao.query.filter(
            func.lower(AghNotificacao.destinatario_id) == usuario_uuid_lower,
            AghNotificacao.lida == False,
            or_(
                AghNotificacao.empresa_id == empresa_id,
                AghNotificacao.empresa_id.is_(None)
            )
        ).count()
    except Exception as e:
        print(f"DEBUG BALCAO EMPRESA: Erro ao contar notificações -> {e}")

    return render_template(
        'agenda/agenda_balcao.html',
        empresa=empresa,
        hoje=hoje,
        agora=hoje,
        cargo_atribuicao=cargo_atribuicao,
        eh_proprietario=eh_proprietario,
        contrato_ativo=contrato_ativo,
        profissionais_ativos=profissionais_ativos,
        total_notificacoes_nao_lidas=total_notificacoes_nao_lidas,
        metricas_hoje={'total': 0, 'pendentes': 0, 'concluidos': 0, 'cancelados': 0}
    )


def _calcular_duracao_total(rascunho=None, agendamento_original=None, dados=None, args=None):
    # INICIALIZAÇÃO OBRIGATÓRIA NA LINHA 1 DA FUNÇÃO
    servico_ids = []

    if dados:
        raw_servicos = dados.get('servico_ids') or dados.get('servicos') or dados.get('servico_id')
        if raw_servicos:
            if isinstance(raw_servicos, str):
                servico_ids = [int(x.strip()) for x in raw_servicos.split(',') if x.strip().isdigit()]
            elif isinstance(raw_servicos, (list, set, tuple)):
                servico_ids = [int(x) for x in raw_servicos if str(x).isdigit()]
            elif isinstance(raw_servicos, int):
                servico_ids = [raw_servicos]

    if not servico_ids and rascunho and getattr(rascunho, 'itens', None):
        servico_ids = [item.servico_id for item in rascunho.itens if getattr(item, 'servico_id', None)]

    if not servico_ids and agendamento_original and getattr(agendamento_original, 'itens', None):
        servico_ids = [item.servico_id for item in agendamento_original.itens if getattr(item, 'servico_id', None)]

    if not servico_ids and args:
        raw_args = args.get('servico_ids') or args.get('servicos') or args.get('servico_id')
        if raw_args:
            servico_ids = [int(x.strip()) for x in str(raw_args).split(',') if x.strip().isdigit()]

    if not servico_ids:
        return 0

    servicos_db = AghServico.query.filter(AghServico.id.in_(servico_ids)).all()

    duracao_total = sum(
        getattr(s, 'duracao_minutos_int', 0) or getattr(s, 'duracao_minutos', 0) or 0
        for s in servicos_db
    )

    return duracao_total if duracao_total > 0 else 30


@agenda_bp.route('/', methods=['GET'])
@agenda_bp.route('', methods=['GET'])
def index_agenda():
    """
    📌 PORTA DE ENTRADA DO MÓDULO AGENDA
    Qualquer chamada para /agenda ou /agenda/ cai aqui.
    """
    # 1. Se NÃO estiver autenticado no módulo Agenda, manda DIRETO para o login/identificação do módulo
    if not session.get('autenticado_modulo_agenda'):
        # Limpa resíduos por segurança
        session.pop('autenticado_modulo_agenda', None)

        # Redireciona direto para a tela inicial de Auth da Agenda
        return redirect(url_for('auth.login', modulo='agenda'))

    # 2. Se JÁ ESTIVER autenticado no módulo, vai direto para o Painel/Dashboard
    return redirect(url_for('agenda.dashboard_cliente'))


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


from flask import render_template, request, g, redirect, url_for, flash
from feedin.middlewares import requer_acesso_modulo, resolver_contexto_usuario, requer_nivel
from feedin.modules.agenda import agenda_bp
from feedin.modules.empresa.models import ColaboradorContrato, EseEmpresa


@agenda_bp.route('/painel', methods=['GET'])
@requer_acesso_modulo('agenda')  # Já garante o contrato ativo no local e o passe de módulo
def painel_agenda():
    # 1. Recupera o contexto já resolvido pelo middleware ou pega da query string/sessão
    empresa_id = request.args.get('empresa_id', type=int) or getattr(g, 'empresa_id', None)

    if not empresa_id:
        flash("Empresa/Local não informado.", "warning")
        return redirect(url_for('dashboard.index'))

    user_uuid = str(g.user.id) if hasattr(g, 'user') else str(current_user.id)
    empresa = EseEmpresa.query.get_or_404(empresa_id)

    # 2. Identifica se é o proprietário do estabelecimento
    proprietario_id = str(getattr(empresa, 'proprietario_id', None) or getattr(empresa, 'usuario_proprietario_id', ''))
    eh_proprietario = (proprietario_id == user_uuid)

    # 3. Recupera o contrato ativo obtido pelo middleware ou faz a busca direta por UUID
    contrato = getattr(g, 'contrato_ativo', None)
    if not contrato:
        contrato = ColaboradorContrato.query.filter_by(
            id_local=empresa_id,
            id_cadastro_cliente=user_uuid,
            status_profissional='ativo'
        ).first()

    # 4. Obtém o nível de acesso no contexto da empresa
    nivel_acesso = 999 if eh_proprietario else getattr(contrato, 'nivel_acesso', 300)

    # CORTE DE PERMISSÃO: >= 666 Gestão | < 600 Operacional
    pode_gerenciar_todos = (nivel_acesso >= 666)

    if pode_gerenciar_todos:
        # Visão ampla: Carrega todos os colaboradores ativos do local
        equipe_agenda = ColaboradorContrato.query.filter_by(
            id_local=empresa_id,
            status_profissional='ativo'
        ).all()
    else:
        # Visão restrita (< 600): Apenas a própria linha de agenda
        equipe_agenda = [contrato] if contrato else []

    return render_template(
        'agenda/painel_agenda.html',
        empresa=empresa,
        equipe=equipe_agenda,
        pode_gerenciar_todos=pode_gerenciar_todos,
        contrato_logado=contrato
    )


# Rota para o Empreendedor reverter a perda de prazo do cliente (Exceção à regra)
@agenda_bp.route('/reagendamento/reverter/<int:id>')
def reverter_prazo(id):
    """Permite ao empreendedor reverter a perda de prazo de um cliente (Exceção)."""
    agendamento = AghAgendamento.query.get_or_404(id)
    agendamento.status = 'reagendamento_pendente'
    agendamento.data_solicitacao_reagendamento = datetime.now()
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


@agenda_bp.route('/empresa/<int:empresa_id>/favoritar', methods=['POST'])
@login_required
def toggle_favorito_empresa(empresa_id):
    usuario_logado_id = current_user.id

    # 1. Valida existência da empresa sem sobrescrever o ID
    empresa = EseEmpresa.query.get_or_404(empresa_id)

    # 2. Busca favorito existente usando a PK (empresa.id)
    favorito_existente = UsuarioFavorito.query.filter_by(
        usuario_id=usuario_logado_id,
        empresa_id=empresa.id
    ).first()

    if favorito_existente:
        db.session.delete(favorito_existente)
        db.session.commit()
        return jsonify({
            "success": True,
            "is_favorito": False,
            "mensagem": "Removido dos favoritos"
        }), 200

    novo_favorito = UsuarioFavorito(
        usuario_id=usuario_logado_id,
        empresa_id=empresa.id
    )
    db.session.add(novo_favorito)
    db.session.commit()

    return jsonify({
        "success": True,
        "is_favorito": True,
        "mensagem": "Adicionado aos favoritos com sucesso!"
    }), 200


@agenda_bp.route('/negocios')
def home_negocios():
    """Renderiza a central de atendimento urbana (Listagem de Empresas)"""

    # 🪐 1. VALIDAÇÃO DE CONTEXTO EXCLUSIVA DA AGENDA
    if 'cliente_modulo_id' not in session:
        # Se ele não tem o cliente_modulo_id na sessão, mas já está logado no app,
        # ele não deveria ir pro Auth de novo, e sim para a seleção de perfil/cliente da Agenda!
        # Se a única forma de pegar essa ID é no login, mandamos para o Auth com aviso claro:
        flash("Por favor, selecione seu perfil para acessar a Agenda.", "warning")
        return redirect(url_for('auth.login', next_url=url_for('agenda.home_negocios')))

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
    📌 DESCONEXÃO ISOLADA DO MÓDULO AGENDA
    Revoga apenas o passe do módulo Agenda e redireciona para o Auth da Agenda.
    """
    # 1. Revoga o passe e limpa contexto exclusivo da Agenda
    session.pop('autenticado_modulo_agenda', None)

    chaves_agenda = [
        'cliente_modulo_id',
        'modo_visao_agenda',
        'local_contexto_id',
        'empresa_ativa_id',
        'next_url_agenda',
        'navegacao_via_hub'
    ]
    for chave in chaves_agenda:
        session.pop(chave, None)

    flash("Sessão da Agenda encerrada.", "info")

    # 2. REDIRECIONAMENTO DIRETO E SEMPRE PARA O AUTH DO MÓDULO
    return redirect(url_for('auth.login', modulo='agenda'))


@agenda_bp.route('/balcao/sair')
@login_required
def sair_modo_balcao():
    """
    📌 DESATIVA MODO OPERACIONAL (BALCÃO)
    Desativa o modo interno do lojista (funcionário/gerente)
    e o devolve pacificamente para a visão urbana como cliente.
    """
    # 1. Limpa o contexto operacional de balcão da sessão
    session.pop('modo_visao', None)
    session.pop('nivel_acesso_atual', None)
    session.pop('papel_nome', None)
    session.pop('cargo_institucional', None)

    # 2. Resgata e limpa o ID do local onde ele estava operando
    id_local_atual = session.pop('local_contexto_id', None)

    flash("Você saiu do modo interno e retornou para a visão da cidade.", "info")

    # 3. Decisão inteligente de destino pós-saída
    if id_local_atual:
        return redirect(url_for('agenda.detalhe_empresa', empresa_id=id_local_atual))

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
    # 1. Recupera o id_cargo do form ou usa um ID padrão (ex: 1 para Operador/Geral)
    id_cargo = getattr(form, 'id_cargo', None)
    id_cargo_val = (
        id_cargo.data if id_cargo and id_cargo.data else 1
    )  # Fallback para ID do cargo

    # 2. Instancia o contrato usando os campos reais do model ColaboradorContrato
    novo_contrato = ColaboradorContrato(
        id_local=estabelecimento_id_atual,
        id_cadastro_cliente=form.id_cadastro_cliente.data,  # UUID (36 chars) do ModCadastroCliente
        id_cargo=id_cargo_val,
        # Expediente padrão inicial (8h às 18h) caso não venha no form
        hora_inicio_expediente=getattr(
            form, 'hora_inicio_expediente', None
        ).data
        if hasattr(form, 'hora_inicio_expediente')
        and form.hora_inicio_expediente.data
        else time(8, 0),
        hora_fim_expediente=getattr(form, 'hora_fim_expediente', None).data
        if hasattr(form, 'hora_fim_expediente') and form.hora_fim_expediente.data
        else time(18, 0),
        status_profissional='ativo',
    )

    db.session.add(novo_contrato)
    db.session.commit()

    # Utiliza a property @property def nome do model (que acessa cadastro_modulo.nome)
    flash(
        f"Colaborador '{novo_contrato.nome}' adicionado à equipe com sucesso!",
        'success',
    )
    return redirect(url_for('agenda.gerenciar_equipe'))

  # 3. Busca a equipe ativa utilizando o relacionamento 'cadastro_modulo' (ModCadastroCliente)
  equipe = (
      ColaboradorContrato.query.filter_by(
          id_local=estabelecimento_id_atual, status_profissional='ativo'
      )
      .join(ColaboradorContrato.cadastro_modulo)
      .order_by(ModCadastroCliente.nome.asc())
      .all()
  )

  return render_template(
      'agenda/gerenciar_equipe.html', form=form, equipe=equipe
  )


@agenda_bp.route('/balcao/credenciamento', methods=['GET', 'POST'])
@login_required
def credenciamento_balcao():
    """
    📌 CREDENCIAMENTO E ESTRUTURAÇÃO DE NOVO ESTABELECIMENTO/EMPRESA
    Permite ao empreendedor registrar seu ponto físico (Local) e a empresa comercial.
    """
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
            local.nome = form.nome_ponto_fisico.data.strip() if hasattr(form, 'nome_ponto_fisico') else form.nome.data.strip()
            local.documento = form.documento.data.strip() if form.documento.data else None
            local.cep = form.cep.data.strip()
            local.logradouro = form.logradouro.data.strip()
            local.numero = form.numero.data.strip()
            local.bairro = form.bairro.data.strip()
            local.cidade = form.cidade.data.strip()
            local.estado = form.estado.data.upper() if form.estado.data else 'SP'
            local.telefone = form.telefone.data.strip() if hasattr(form, 'telefone') and form.telefone.data else None
            local.is_whatsapp = form.is_whatsapp.data if hasattr(form, 'is_whatsapp') else False

            local.id_empreendedor = empreendedor_id  # Dono do "Prédio"
            local.esta_ativo = True
            local.status_operacional = 'ativo'

            # Gravação do histórico de ocupação
            ocupacao = HistoricoOcupacaoLocal(
                local=local,
                id_empreendedor=empreendedor_id,
                plano_contratado=getattr(local, 'plano_marketing', 'degustacao')
            )
            db.session.add(ocupacao)

            # Flush para gerar o ID do local
            db.session.flush()

            # 2. TRATATIVA DA EMPRESA (O INQUILINO DA AGENDA)
            nova_empresa = EseEmpresa(
                proprietario_id=empreendedor_id,
                local_id=local.id,
                nome=form.nome.data.strip(),
                categoria=form.categoria.data.strip() if hasattr(form, 'categoria') and form.categoria.data else "Geral",
                slug=form.slug.data.strip() if hasattr(form, 'slug') and form.slug.data else f"empresa-{local.id}"
            )
            db.session.add(nova_empresa)
            db.session.flush()

            # 3. VÍNCULO AUTOMÁTICO COM O MÓDULO 'AGENDA'
            modulo_agenda = ModEmpresaModulo(
                empresa_id=nova_empresa.id,
                modulo_slug='agenda',
                ativo=True,
                status_homologacao='homologado',
                tipo_plano='degustacao',
                termo_aceito=True,
                data_aceite=datetime.now(timezone.utc)
            )
            db.session.add(modulo_agenda)

            # Gravação atômica de todas as entidades
            db.session.commit()

            flash("Estabelecimento e Empresa estruturados com sucesso no FeedIn!", "success")
            return redirect(url_for('agenda.dashboard_cliente'))

        except Exception as e:
            db.session.rollback()
            print(f"Erro crítico no credenciamento: {e}")
            flash("Ocorreu um erro interno ao processar o credenciamento.", "danger")

    return render_template('agenda/credenciamento.html', form=form)

@agenda_bp.route('/empresa/<int:empresa_id>')
def detalhe_empresa(empresa_id):
    """
    📌 PERFIL PÚBLICO E VITRINE DO ESTABELECIMENTO (HUB PWA)
    """
    from datetime import datetime, date, timedelta
    from sqlalchemy import or_
    from sqlalchemy.orm import joinedload
    from feedin.utils import calcular_status_funcionamento

    # 1. Busca empresa
    empresa = EseEmpresa.query.options(
        joinedload(EseEmpresa.categoria_rel),
        joinedload(EseEmpresa.local_fisico)
    ).get_or_404(empresa_id)

    # -------------------------------------------------------------------------
    # 2. PERMISSÕES E STATUS DO USUÁRIO LOGADO (CLIENTE / OPERADOR)
    # -------------------------------------------------------------------------
    exibir_switch_operacional = False
    is_colaborador = False
    nivel_neste_local = "Cliente"
    eh_favorita = False
    beneficiarios = []

    if current_user.is_authenticated:
        user_uuid = str(current_user.id)

        # 1. Checa favorito
        eh_favorita = UsuarioFavorito.query.filter_by(
            usuario_id=user_uuid,
            empresa_id=empresa_id
        ).first() is not None

        # 2. Checa se é proprietário
        proprietario_id = str(
            getattr(empresa, 'proprietario_id', None) or getattr(empresa, 'usuario_proprietario_id', ''))
        eh_proprietario = (proprietario_id == user_uuid)

        # 3. Busca contrato ativo no local
        contrato_ativo = ColaboradorContrato.query.filter_by(
            id_local=empresa.id,
            id_cadastro_cliente=user_uuid,
            status_profissional='ativo'
        ).first()

        # Define se é colaborador/gestor ativo
        is_colaborador = (contrato_ativo is not None) or eh_proprietario

        # 4. Libera switch operacional se for proprietário ou colaborador
        if eh_proprietario or contrato_ativo:
            exibir_switch_operacional = True
            nivel_neste_local = "Gestor" if (eh_proprietario or (
                    contrato_ativo and getattr(contrato_ativo, 'nivel_acesso', 0) >= 666)) else "Operacional"

        # 5. Busca dependentes/beneficiários vinculados ao titular (pets, veículos, familiares)
        beneficiarios = ClienteBeneficiario.query.filter_by(
            cliente_id=user_uuid,
            ativo=True
        ).order_by(ClienteBeneficiario.nome.asc()).all()

    # -------------------------------------------------------------------------
    # REGRA DE TRIAGEM DE DEPENDENTES (PETS, VEÍCULOS, DEPENDENTES)
    # -------------------------------------------------------------------------
    # Avalia se a empresa/categoria exige a especificação do beneficiário no agendamento.
    # Ex: Checa o campo na tabela EseEmpresa ou na Categoria associada
    requer_triagem_dependente = (
            getattr(empresa, 'requer_triagem_dependente', False) or
            getattr(getattr(empresa, 'categoria_rel', None), 'exige_beneficiario', False)
    )

    # -------------------------------------------------------------------------
    # 3. ABA 1: SERVIÇOS E PREÇOS VIGENTES
    # -------------------------------------------------------------------------
    precos_query = EseServicoPreco.query.filter(
        EseServicoPreco.empresa_id == empresa.id,
        EseServicoPreco.novo_valor > 0
    ).all()
    mapa_precos = {p.taxonomia_id: float(p.novo_valor) for p in precos_query}

    servicos_brutos = EseServicoOferecido.query.options(
        joinedload(EseServicoOferecido.servico_taxonomia)
    ).filter_by(empresa_id=empresa.id).all()

    servicos = []
    for s in servicos_brutos:
        if s.taxonomia_id in mapa_precos:
            s.preco_vigente = mapa_precos[s.taxonomia_id]
            servicos.append(s)

    # -------------------------------------------------------------------------
    # 4. ABA 2: HORÁRIOS E GRADE COMERCIAL
    # -------------------------------------------------------------------------
    status_func = calcular_status_funcionamento(empresa.id)
    status_hoje = status_func  # Consolida para reuso limpo na view

    horarios_bd = EseHorarioFuncionamento.query.filter_by(
        empresa_id=empresa.id
    ).order_by(EseHorarioFuncionamento.dia_semana.asc(), EseHorarioFuncionamento.periodo_id.asc()).all()

    dias_semana_map = [
        (0, 'Domingo'), (1, 'Segunda-feira'), (2, 'Terça-feira'),
        (3, 'Quarta-feira'), (4, 'Quinta-feira'), (5, 'Sexta-feira'), (6, 'Sábado')
    ]

    hoje_python = datetime.now().weekday()
    hoje_bd = (hoje_python + 1) % 7

    grade_semanal = []
    for dia_idx, nome_dia in dias_semana_map:
        turnos_dia = [h for h in horarios_bd if h.dia_semana == dia_idx]
        grade_semanal.append({
            'dia_num': dia_idx,
            'nome_dia': nome_dia,
            'eh_hoje': (dia_idx == hoje_bd),
            'turnos': turnos_dia
        })

    data_hoje = date.today()
    excecoes_proximas = EmpresaCalendarioExcecao.query.options(
        joinedload(EmpresaCalendarioExcecao.feriado_oficial)
    ).filter(
        EmpresaCalendarioExcecao.empresa_id == empresa.id,
        EmpresaCalendarioExcecao.data_excecao >= data_hoje,
        EmpresaCalendarioExcecao.data_excecao <= (data_hoje + timedelta(days=180)),
        EmpresaCalendarioExcecao.trabalha_no_dia == False
    ).order_by(EmpresaCalendarioExcecao.data_excecao.asc()).all()

    # -------------------------------------------------------------------------
    # 5. ABA 3: EQUIPE DE PROFISSIONAIS
    # -------------------------------------------------------------------------
    colaboradores_contratos = ColaboradorContrato.query.options(
        joinedload(ColaboradorContrato.cargo),
        joinedload(ColaboradorContrato.cadastro_modulo)
    ).filter(
        ColaboradorContrato.id_local == empresa.id,
        ColaboradorContrato.status_profissional == 'ativo',
        ColaboradorContrato.data_desligamento.is_(None)
    ).all()

    colaboradores = []
    for contrato in colaboradores_contratos:
        cad = contrato.cadastro_modulo
        nome_exibicao = contrato.nome
        username_exibicao = getattr(cad, 'username_modulo', None) or getattr(cad, 'username', None) if cad else None
        cargo_exibicao = contrato.cargo.nome_cargo if getattr(contrato, 'cargo', None) and hasattr(contrato.cargo, 'nome_cargo') else getattr(getattr(contrato, 'cargo', None), 'nome', getattr(contrato, 'papel_nome', 'Especialista'))

        foto_raw = getattr(contrato, 'foto_profissional', None) or \
                   (getattr(cad, 'foto_url', None) if cad else None) or \
                   (getattr(cad, 'foto', None) if cad else None)

        url_foto = None
        if foto_raw:
            foto_raw = str(foto_raw).strip()
            if foto_raw.lower() not in ['none', 'null', '']:
                if foto_raw.startswith(('http://', 'https://')):
                    url_foto = foto_raw
                else:
                    caminho_limpo = foto_raw.replace('\\', '/')
                    if 'uploads/' in caminho_limpo.lower():
                        idx = caminho_limpo.lower().find('uploads/')
                        caminho_relativo = caminho_limpo[idx + 8:]
                    else:
                        caminho_relativo = caminho_limpo.lstrip('/')
                    url_foto = url_for('empresa.static', filename=f'uploads/{caminho_relativo}')

        colaboradores.append({
            'id': contrato.id,
            'id_cadastro_cliente': contrato.id_cadastro_cliente,
            'nome': nome_exibicao,
            'username': username_exibicao,
            'cargo_nome': cargo_exibicao,
            'foto_raw': foto_raw,
            'url_foto': url_foto,
            'data_contratacao': contrato.data_contratacao,
            'escala_vigente': contrato.escala_vigente,
            'excecoes_ativas': contrato.excecoes_ativas
        })

    # -------------------------------------------------------------------------
    # 6. RENDERIZAÇÃO DA VIEW
    # -------------------------------------------------------------------------
    return render_template(
        'agenda/detalhe_empresa.html',
        empresa=empresa,
        servicos=servicos,
        grade_semanal=grade_semanal,
        excecoes_proximas=excecoes_proximas,
        colaboradores=colaboradores,
        is_colaborador=is_colaborador,
        nivel_neste_local=nivel_neste_local,
        status_func=status_func,
        eh_favorita=eh_favorita,
        exibir_switch_operacional=exibir_switch_operacional,
        beneficiarios=beneficiarios,
        requer_triagem_dependente=requer_triagem_dependente,
        status_hoje=status_hoje
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
            id_local=empresa.id, id_usuario=usuario_logado.id, status_profissional='ativo'
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
            cpf_hash=usuario_core.identidade_civil.cpf_hash if getattr(usuario_core, 'identidade_civil', None) else None,
            cpf_criptografado=usuario_core.identidade_civil.cpf_criptografado if getattr(usuario_core, 'identidade_civil', None) else None
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


@agenda_bp.route('/balcao/agendar', methods=['GET', 'POST'])
@login_required
@requer_acesso_modulo('agenda')
def criar_agendamento_balcao():
    """
    📌 CRIAR AGENDAMENTO DIRETO VIA BALCÃO OPERACIONAL
    Permite ao operador/gestor agendar manualmente na grade com verificação
    de disponibilidade e sistema inteligente de remanejamento/cadeira livre.
    """
    # 1. Se o usuário estiver apenas ACESSANDO a tela (GET), renderiza a interface
    if request.method == 'GET':
        return render_template('agenda/dashboard_cliente.html')

    # 2. Se for uma submissão de dados (POST), executa o processamento do formulário
    id_local = request.form.get('id_local', type=int)
    profissional_id = request.form.get('id_profissional', type=int)
    cliente_id = request.form.get('id_cliente', type=int)
    servico_id = request.form.get('id_servico', type=int)
    preco_real = request.form.get('preco_cobrado', type=float)

    data_hora_str = request.form.get('data_hora_atendimento')

    # Validação preventiva de segurança
    if not data_hora_str:
        flash("❌ A data e hora do atendimento são obrigatórias.", "danger")
        return redirect(url_for('agenda.criar_agendamento_balcao'))

    try:
        data_inicio = datetime.strptime(data_hora_str, "%Y-%m-%d %H:%M")
    except ValueError:
        flash("❌ Formato de data e hora inválido. Use AAAA-MM-DD HH:MM.", "danger")
        return redirect(url_for('agenda.criar_agendamento_balcao'))

    duracao_minutos = request.form.get('duracao_servico', default=60, type=int)

    # Motor de verificação de disponibilidade da grade
    diagnostico = verificar_disponibilidade_agenda(
        id_local=id_local,
        id_profissional=profissional_id,
        data_inicio_proposta=data_inicio,
        duracao_minutos=duracao_minutos
    )

    # -------------------------------------------------------------------------
    # DECISÃO DO AGENDAMENTO BASEADO NO DIAGNÓSTICO DO MOTOR
    # -------------------------------------------------------------------------
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
        id_substituto = diagnostico.get("substituto_id")

        if id_substituto:
            # Resgata o profissional substituto
            substituto = Usuario.query.get(id_substituto)
            nome_substituto = getattr(substituto, 'username', 'outro especialista')

            # Cria a proposta de remanejamento na cadeira disponível
            agendamento_resiliente = AghAgendamento(
                profissional_id=id_substituto,
                cliente_id=cliente_id,
                servico_id=servico_id,
                preco_cobrado=preco_real,
                data_hora_inicio=data_inicio,
                data_hora_fim=data_inicio + timedelta(minutes=duracao_minutos),
                status='reagendamento_pendente',
                tipo_origem='manual'
            )
            db.session.add(agendamento_resiliente)
            db.session.commit()

            flash(
                f"⚠️ Horário indisponível com o profissional original! "
                f"Movido automaticamente para a cadeira de {nome_substituto}. "
                f"Aguardando validação do cliente no app.", "warning"
            )
            return redirect(url_for('agenda.painel_gerencial'))
        else:
            flash("❌ Horário indisponível e nenhuma outra cadeira da mesma especialidade está livre.", "danger")
            return redirect(url_for('agenda.painel_gerencial'))

    flash("❌ Não foi possível realizar o agendamento para este horário.", "danger")
    return redirect(url_for('agenda.painel_gerencial'))


@agenda_bp.route('/api/agenda/notificacoes-pendentes', methods=['GET'])
@login_required
def verificar_notificacoes_app():
    """
    Consulta se o cliente logado possui agendamentos com aceite de reagendamento/substituição pendente.
    """
    # Garante a busca pelo usuário autenticado na sessão
    cliente_id = str(current_user.id)

    pendencia = AghAgendamento.query.filter_by(
        cliente_id=cliente_id,
        status='reagendamento_pendente'
    ).order_by(AghAgendamento.id.desc()).first()

    if not pendencia:
        return jsonify({"possui_pendencia": False}), 200

    profissional_substituto = ColaboradorContrato.query.get(pendencia.profissional_id) if hasattr(AghAgendamento, 'profissional_id') else None
    nome_profissional = getattr(profissional_substituto, 'nome', 'Profissional Técnico')

    valor_cobrado = float(getattr(pendencia, 'valor_total', 0.0) or getattr(pendencia, 'preco_cobrado', 0.0) or 0.0)

    return jsonify({
        "possui_pendencia": True,
        "dados_remanejamento": {
            "id_agendamento": pendencia.id,
            "data_hora": pendencia.data_hora_inicio.strftime('%Y-%m-%d %H:%M'),
            "data_formatada": pendencia.data_hora_inicio.strftime('%d/%m às %H:%M'),
            "preco_cobrado": valor_cobrado,
            "profissional_sugerido": nome_profissional,
            "alerta_mensagem": f"O seu horário original estava indisponível, mas garantimos sua vaga com o profissional {nome_profissional}. Deseja aceitar a substituição?"
        }
    }), 200


@agenda_bp.route('/api/agenda/responder-remanejamento', methods=['POST'])
def responder_remanejamento_cliente():
    """Processa a decisão do cliente ('aceitar', 'recusar', 'rejeitar')."""
    data = request.get_json() or {}
    id_agendamento = data.get('id_agendamento')
    decisao_cliente = data.get('decisao')

    if not id_agendamento or not decisao_cliente:
        return jsonify({"erro": "Parâmetros inválidos. Informe id_agendamento e decisao."}), 400

    agendamento = AghAgendamento.query.get(id_agendamento)
    if not agendamento:
        return jsonify({"erro": "Agendamento não localizado."}), 404

    if agendamento.status != 'reagendamento_pendente':
        return jsonify({"erro": "Este agendamento já foi processado ou expirou."}), 400

    if decisao_cliente == 'aceitar':
        agendamento.status = 'confirmado'
        db.session.commit()
        return jsonify({
            "sucesso": True,
            "status_final": "confirmado",
            "acao_app": "fechar_modal_sucesso",
            "mensagem": "Perfeito! Seu atendimento foi confirmado com o novo profissional."
        }), 200

    elif decisao_cliente in ['recusar', 'rejeitar']:
        agendamento.status = 'cancelado'
        db.session.commit()
        return jsonify({
            "sucesso": True,
            "status_final": "cancelado",
            "acao_app": "abrir_tela_calendario",
            "mensagem": "Reserva liberada. Escolha um novo horário de sua preferência."
        }), 200

    return jsonify({"erro": "Decisão inválida. Utilize 'aceitar', 'recusar' ou 'rejeitar'."}), 400


@agenda_bp.route('/painel/<int:id_local_alvo>')
@login_required
def acessar_estabelecimento(id_local_alvo):
    """
    📌 TRIAGEM CONTEXTUAL DO MÓDULO AGENDA
    Alterna o contexto do usuário na sessão entre Painel Operacional (Balcão) e Perfil Cliente (Vitrine).
    """
    # -------------------------------------------------------------------------
    # 1. IDENTIFICAÇÃO DOS IDENTIFICADORES DO USUÁRIO LOGADO
    # -------------------------------------------------------------------------
    user_uuid_str = str(current_user.id)
    cad_cliente_uuid = str(getattr(current_user, 'id_cadastro_cliente', ''))

    # -------------------------------------------------------------------------
    # 2. VERIFICAÇÃO DE PROPRIETÁRIO / GESTOR DA EMPRESA
    # -------------------------------------------------------------------------
    empresa = EseEmpresa.query.get_or_404(id_local_alvo)
    proprietario_id_empresa = getattr(empresa, 'proprietario_id', None) or getattr(empresa, 'usuario_proprietario_id',
                                                                                   None)

    # Compara convertendo para string por segurança de tipo (UUID vs INT)
    eh_proprietario = (str(proprietario_id_empresa) == user_uuid_str) if proprietario_id_empresa else False

    # -------------------------------------------------------------------------
    # 3. BUSCA DO CONTRATO ATIVO DO COLABORADOR
    # -------------------------------------------------------------------------
    contrato = ColaboradorContrato.query.filter(
        ColaboradorContrato.id_local == id_local_alvo,
        ColaboradorContrato.status_profissional == 'ativo',
        ColaboradorContrato.data_desligamento.is_(None),
        or_(
            ColaboradorContrato.id_cadastro_cliente == cad_cliente_uuid,
            ColaboradorContrato.id_cadastro_cliente == user_uuid_str
        )
    ).first()

    # -------------------------------------------------------------------------
    # 4. CHAVEAMENTO PARA MODO INTERNO (OPERACIONAL / BALCÃO)
    # -------------------------------------------------------------------------
    if contrato or eh_proprietario:
        session['autenticado_modulo_agenda'] = True
        session['modo_visao'] = 'balcao'
        session['local_contexto_id'] = id_local_alvo

        if eh_proprietario:
            session['nivel_acesso_atual'] = 999
            session['papel_nome'] = 'Proprietário / Gestor'
            nome_cargo = 'Proprietário'
        else:
            session['nivel_acesso_atual'] = getattr(contrato, 'nivel_acesso', getattr(contrato, 'papel_nivel', 300))
            session['papel_nome'] = getattr(contrato, 'papel_nome', 'Colaborador')
            nome_cargo = contrato.cargo.nome_cargo if getattr(contrato, 'cargo', None) else 'Colaborador'

        flash(f"Modo interno ativado: {nome_cargo}", "success")

        # Redireciona para o painel operacional do módulo agenda
        return redirect(url_for('agenda.painel_interno_loja', id_local=id_local_alvo))

    # -------------------------------------------------------------------------
    # 5. FALLBACK PARA CLIENTE COMUM (VISÃO PÚBLICA / VITRINE)
    # -------------------------------------------------------------------------
    session['modo_visao'] = 'cliente'
    session['nivel_acesso_atual'] = 10
    session['local_contexto_id'] = id_local_alvo

    return redirect(url_for('agenda.detalhe_empresa', empresa_id=id_local_alvo))




@agenda_bp.route('/dashboard', methods=['GET'])
@login_required
def dashboard_cliente():
    """📌 DASHBOARD PRINCIPAL DO MÓDULO AGENDA (PWA CLIENTE)"""
    cliente = current_user

    # 1. RESOLUÇÃO DO CADASTRO DE CLIENTE
    if isinstance(cliente, ModCadastroCliente):
        cadastro_cliente = cliente
    else:
        cadastro_cliente = ModCadastroCliente.query.filter_by(
            usuario_id=cliente.id
        ).first()

    # 🛡️ GARANTIA DE SESSÃO E PERFIL DO MÓDULO AGENDA
    if not cadastro_cliente:
        flash('Para acessar a Agenda, complete a identificação do seu perfil.', 'warning')
        return redirect(url_for('auth.login', modulo='agenda', next=request.url))

    session['autenticado_modulo_agenda'] = True
    cliente_id_relacional = cadastro_cliente.id
    agora = obter_hora_local()
    agora_naive = agora.replace(tzinfo=None) if hasattr(agora, 'tzinfo') and agora.tzinfo else agora

    # ==================================================================================
    # 🎯 RESOLUÇÃO UNIFICADA DE IDENTIDADE, CPF E FOTO DO CLIENTE
    # ==================================================================================
    cpf_bruto = getattr(cadastro_cliente, 'cpf', '') or getattr(current_user, 'cpf', '')
    cpf_limpo = re.sub(r'\D', '', str(cpf_bruto)) if cpf_bruto else None

    cpf_hash_calculado = getattr(cadastro_cliente, 'cpf_hash', None)
    if not cpf_hash_calculado and cpf_limpo and hasattr(EseConviteColaborador, 'gerar_hash_cpf'):
        cpf_hash_calculado = EseConviteColaborador.gerar_hash_cpf(cpf_limpo)

    # Hierarquia da Foto: CadastroCliente -> User -> Vínculo Módulo Agenda
    foto_definida = getattr(cadastro_cliente, 'foto_url', None) or getattr(cadastro_cliente, 'foto', None)

    if not foto_definida:
        foto_definida = getattr(current_user, 'foto_url', None) or getattr(current_user, 'foto', None)

    if not foto_definida and cpf_hash_calculado:
        email_alvo = (
            session.get('email_modulo_agenda')
            or getattr(cadastro_cliente, 'email', None)
            or getattr(current_user, 'email', None)
            or session.get('email')
        )
        if email_alvo:
            email_alvo = str(email_alvo).strip().lower()
            vinculo_agenda = ModVinculoModulo.query.filter(
                ModVinculoModulo.modulo_slug == 'agenda',
                ModVinculoModulo.cpf_hash == cpf_hash_calculado,
                func.lower(ModVinculoModulo.email_customizado) == email_alvo
            ).first()

            if vinculo_agenda and vinculo_agenda.foto_url:
                foto_definida = vinculo_agenda.foto_url

    # Sanitização da foto
    if foto_definida:
        foto_str = str(foto_definida).strip()
        if foto_str.lower() in ['none', 'null', ''] or foto_str.lower().endswith('/none') or foto_str.lower().endswith('/null'):
            foto_definida = None
        else:
            foto_definida = foto_str

    cadastro_cliente.foto_url = foto_definida

    # 📋 VERIFICAÇÃO DE FICHA / CONVITE DE COLABORADOR PENDENTE
    ficha_pendente = None
    if cpf_hash_calculado:
        ficha_pendente = EseConviteColaborador.query.filter_by(
            cpf_hash=cpf_hash_calculado,
            status='pendente'
        ).first()

    # ----------------------------------------------------------------------------------
    # 2. BRANDING DA EMPRESA (CONTEXTO ATIVO) E BUSCA DE COLABORADORES
    # ----------------------------------------------------------------------------------
    local_id_ativo = (
        session.get('local_contexto_id')
        or session.get('local_id_atual')
        or session.get('empresa_operavel_id')
    )
    colaboradores_elegiveis = []
    empresa_ativa = None

    if local_id_ativo:
        try:
            empresa_ativa = EseEmpresa.query.filter(
                (EseEmpresa.local_id == local_id_ativo)
                | (EseEmpresa.id == local_id_ativo)
            ).first()

            if empresa_ativa:
                session['empresa_id'] = empresa_ativa.id
                session['empresa_nome'] = empresa_ativa.nome
                session['empresa_cor_primaria'] = empresa_ativa.cor_primaria or '#111827'
                session['empresa_cor_secundaria'] = empresa_ativa.cor_secundaria or '#6B7280'

                colaboradores_elegiveis = buscar_colaboradores_elegiveis(id_local=empresa_ativa.id)
        except Exception as e:
            print(f'DEBUG DASHBOARD: Erro ao carregar empresa -> {e}')

    # ----------------------------------------------------------------------------------
    # 3. NOTIFICAÇÕES (ADEQUADO PARA AghNotificacao E CONTRATO PWA)
    # ----------------------------------------------------------------------------------
    notificacoes = []
    total_nao_lidas = 0

    try:
        user_id_str = str(current_user.id).strip().lower()
        notificacoes = AghNotificacao.query.filter(
            func.lower(AghNotificacao.destinatario_id) == user_id_str,
            AghNotificacao.lida == False
        ).order_by(AghNotificacao.criado_em.desc()).all()

        total_nao_lidas = len(notificacoes)
    except Exception as e:
        print(f'DEBUG DASHBOARD: Erro ao carregar notificações -> {e}')

    # ----------------------------------------------------------------------------------
    # 4. HISTÓRICO, AGENDAMENTOS ATIVOS E PENDÊNCIAS DE REAGENDAMENTO
    # ----------------------------------------------------------------------------------
    ultimos_agendamentos = []
    agendamentos_ativos = []
    agendamentos_ausente_pendentes = []

    try:
        # 4.1. HISTÓRICO CONCLUÍDO / CANCELADO
        ultimos_agendamentos = (
            AghAgendamento.query.options(
                joinedload(AghAgendamento.empresa),
                joinedload(AghAgendamento.itens).joinedload(AghAgendamentoItem.servico),
                joinedload(AghAgendamento.profissional),
            )
            .filter_by(cliente_id=cliente_id_relacional)
            .filter(AghAgendamento.status.in_(['concluido', 'finalizado', 'cancelado']))
            .order_by(AghAgendamento.data_hora_inicio.desc())
            .limit(5)
            .all()
        )

        # 4.2. REAGENDAMENTOS PENDENTES / AUSÊNCIAS EXPLICITAS
        agendamentos_ausente_pendentes = (
            AghAgendamento.query.options(
                joinedload(AghAgendamento.empresa),
                joinedload(AghAgendamento.itens).joinedload(AghAgendamentoItem.servico),
                joinedload(AghAgendamento.profissional),
            )
            .filter_by(cliente_id=cliente_id_relacional)
            .filter(AghAgendamento.status.in_(['ausente_pendente', 'reagendamento_pendente']))
            .order_by(AghAgendamento.data_hora_inicio.desc())
            .all()
        )

        # 4.3. BUSCA TODOS OS AGENDAMENTOS EM ABERTO/PENDENTES
        agendamentos_brutos = (
            AghAgendamento.query.options(
                joinedload(AghAgendamento.empresa),
                joinedload(AghAgendamento.itens).joinedload(AghAgendamentoItem.servico),
                joinedload(AghAgendamento.profissional),
            )
            .filter_by(cliente_id=cliente_id_relacional)
            .filter(AghAgendamento.status.in_(['agendado', 'confirmado', 'aguardando', 'em_atendimento', 'soft_lock']))
            .order_by(AghAgendamento.data_hora_inicio.asc())
            .all()
        )

        # 4.4. CLASSIFICAÇÃO E TRATAMENTO EM TEMPO REAL
        houve_alteracao_banco = False

        for ag in agendamentos_brutos:
            ag.atrasado = False
            ag.mensagem_atraso = None

            # Caso A: O agendamento JÁ PASSOU da hora e NUNCA foi concluído
            if ag.data_hora_fim < agora_naive or (ag.status == 'soft_lock' and ag.data_hora_inicio < agora_naive):
                ag.is_nao_executado = True
                ag.status = 'cancelado' if ag.status == 'soft_lock' else 'ausente_pendente'
                houve_alteracao_banco = True

                if ag not in agendamentos_ausente_pendentes:
                    agendamentos_ausente_pendentes.append(ag)

            # Caso B: Agendamentos FUTUROS ou EM ATENDIMENTO
            else:
                if ag.status in ['agendado', 'confirmado', 'aguardando']:
                    # Checagem leve sem disparar queries desnecessárias caso já tenha começado
                    if agora_naive > ag.data_hora_inicio:
                        ag.atrasado = True
                        ag.mensagem_atraso = (
                            'O profissional está finalizando o atendimento anterior. '
                            'Acompanhe a chamada no painel.'
                        )

                agendamentos_ativos.append(ag)

        if houve_alteracao_banco:
            db.session.commit()

    except Exception as e:
        db.session.rollback()
        print(f'DEBUG DASHBOARD: Erro ao processar agendamentos -> {e}')

    # ----------------------------------------------------------------------------------
    # 5. EMPRESAS FAVORITADAS
    # ----------------------------------------------------------------------------------
    empresas_favoritas = []
    try:
        empresas_favoritas = (
            EseEmpresa.query.join(UsuarioFavorito, EseEmpresa.id == UsuarioFavorito.empresa_id)
            .filter(
                UsuarioFavorito.usuario_id == cliente_id_relacional,
                UsuarioFavorito.empresa_id.isnot(None),
            )
            .order_by(EseEmpresa.nome.asc())
            .limit(10)
            .all()
        )
    except Exception as e:
        print(f'DEBUG DASHBOARD: Erro ao buscar empresas favoritas -> {e}')

    # ----------------------------------------------------------------------------------
    # 6. VÍNCULO OPERACIONAL (SWITCH DE PERFIL)
    # ----------------------------------------------------------------------------------
    exibir_switch_operacional = False
    if empresa_ativa:
        try:
            user_uuid_str = str(current_user.id)
            vinculo_existente = ColaboradorContrato.query.filter(
                ColaboradorContrato.id_local == empresa_ativa.id,
                ColaboradorContrato.status_profissional == 'ativo',
                ColaboradorContrato.data_desligamento.is_(None),
                (ColaboradorContrato.id_cadastro_cliente == user_uuid_str) | (ColaboradorContrato.id_usuario == cliente_id_relacional)
            ).first()

            exibir_switch_operacional = vinculo_existente is not None
        except Exception as e:
            print(f'DEBUG DASHBOARD: Erro ao checar vínculo operacional -> {e}')

    # ----------------------------------------------------------------------------------
    # 7. PUBLICIDADE E BANNER INSTITUCIONAL
    # ----------------------------------------------------------------------------------
    HOUSE_ADS_FEEDIN = [
        {
            'id': 'house_core',
            'titulo': 'Descubra a História Viva de Piracicaba',
            'texto_formatado': 'Relembre lugares marcantes, fotos antigas e histórias da nossa cidade no FeedIn Social.',
            'tag_referencia_nome': 'Memória Local',
            'imagem_url': None,
            'link_destino': url_for('index'),
            'botao_texto': 'Explorar Core',
            'is_house_ad': True,
        },
        {
            'id': 'house_empresa',
            'titulo': 'Sua Empresa Aqui no FeedIn',
            'texto_formatado': 'Conecte seu negócio a milhares de clientes em Piracicaba. Divulgue seus serviços na nossa agenda!',
            'tag_referencia_nome': 'FeedIn Soluções',
            'imagem_url': None,
            'link_destino': 'https://wa.me/5519982671750?text=Quero%20divulgar%20minha%20empresa%20no%20FeedIn',
            'botao_texto': 'Saiba Mais',
            'is_house_ad': True,
        },
        {
            'id': 'house_comunidade',
            'titulo': 'Valorize o Comércio de Piracicaba',
            'texto_formatado': 'Agendando pelos nossos parceiros locais, você fortalece os estabelecimentos da nossa região.',
            'tag_referencia_nome': 'Comunidade',
            'imagem_url': None,
            'link_destino': None,
            'botao_texto': 'FeedIn',
            'is_house_ad': True,
        },
    ]

    publicidade_ativa = obter_publicidade_agenda(
        cliente=cliente, empresa_agendada_id=session.get('empresa_id')
    ) or random.choice(HOUSE_ADS_FEEDIN)

    return render_template(
        'agenda/dashboard_cliente.html',
        cliente=cadastro_cliente,
        agora=agora_naive,
        timedelta=timedelta,
        notificacoes=notificacoes,
        total_notificacoes_nao_lidas=total_nao_lidas,
        ultimos_agendamentos=ultimos_agendamentos,
        agendamentos=agendamentos_ativos,
        agendamentos_ausente_pendentes=agendamentos_ausente_pendentes,
        ficha_pendente=ficha_pendente,
        empresas_favoritas=empresas_favoritas,
        colaboradores=colaboradores_elegiveis,
        empresa_ativa=empresa_ativa,
        exibir_switch_operacional=exibir_switch_operacional,
        publicidade_ativa=publicidade_ativa,
    )


# Mapeamento de prioridade da EscalaTrabalhoColaborador
PRIORIDADE_ESCALA = {
    'emergencial': 1,  # Maior prioridade (sobrepõe todas)
    'alternativo': 2,  # Média prioridade (férias, trocas pontuais)
    'padrao': 3        # Menor prioridade (base contratual)
}


def obter_escala_vigente_colaborador(contrato_id, data_alvo):
    """
    📌 MOTOR DE RESOLUÇÃO DE CONFLITOS DE ESCALA
    --------------------------------------------------------------------------------------
    Busca os registros ativos para o 'contrato_id' onde data_inicio <= data_alvo <= data_fim.
    Aplica a regra de precedência: emergencial > alternativo > padrao.
    Retorna o único objeto EscalaTrabalhoColaborador vencedor para o dia.
    """
    # Dia da semana no padrão Python (0=Segunda, 1=Terça ... 6=Domingo)
    dia_semana_py = data_alvo.weekday()

    escalas_candidatas = EscalaTrabalhoColaborador.query.filter(
        EscalaTrabalhoColaborador.contrato_id == contrato_id,
        EscalaTrabalhoColaborador.ativo == True,
        EscalaTrabalhoColaborador.data_inicio <= data_alvo,
        EscalaTrabalhoColaborador.data_fim >= data_alvo
    ).all()

    escalas_validas = []
    for esc in escalas_candidatas:
        # Se 'dia_semana' estiver definido na escala, deve casar com o dia calculado
        if esc.dia_semana is not None:
            if esc.dia_semana == dia_semana_py:
                escalas_validas.append(esc)
        else:
            # Escalas sem dia_semana (ex: emergencial/alternativo cobrindo intervalo de datas continuo)
            escalas_validas.append(esc)

    if not escalas_validas:
        return None  # Colaborador não possui escala ativa neste dia

    # Ordena pela menor pontuação de prioridade (1=emergencial, 2=alternativo, 3=padrao)
    escala_vencedora = min(
        escalas_validas,
        key=lambda e: PRIORIDADE_ESCALA.get(e.tipo_escala, 99)
    )

    return escala_vencedora


def calcular_slots_disponiveis_colaborador(
        empresa_id: int,
        contrato_id: int,
        data_alvo,
        taxonomia_id: int = None,
        duracao_minutos_override: int = None
):
    """
    REAPROVEITADA & REFINADA:
    Calcula as janelas livres para um colaborador específico (contrato_id).
    Pode receber 'taxonomia_id' (para serviço único) OU 'duracao_minutos_override' (soma do rascunho).
    """
    if isinstance(data_alvo, str):
        try:
            data_alvo = datetime.strptime(data_alvo, "%Y-%m-%d").date()
        except ValueError:
            data_alvo = obter_hora_local().date()

    # 1. Checagem de Feriado/Exceção Global da Empresa
    excecao_empresa = EmpresaCalendarioExcecao.query.filter_by(
        empresa_id=empresa_id,
        data_excecao=data_alvo
    ).first()

    if excecao_empresa and not excecao_empresa.trabalha_no_dia:
        return []  # Empresa fechada no feriado

    # 2. Resolução da Escala Individual do Colaborador
    escala = obter_escala_vigente_colaborador(contrato_id, data_alvo)
    if not escala:
        return []  # Sem expediente para este colaborador no dia

    # 3. Duração do Serviço (Prioriza a soma customizada se enviada)
    if duracao_minutos_override and duracao_minutos_override > 0:
        duracao_delta = timedelta(minutes=duracao_minutos_override)
    else:
        servico = EseServicoOferecido.query.filter_by(
            empresa_id=empresa_id,
            taxonomia_id=taxonomia_id
        ).first()

        duracao_str = (servico.tempo_duracao if servico and servico.tempo_duracao else "00:30").strip()
        try:
            h_dur, m_dur = map(int, duracao_str.split(':'))
        except (ValueError, AttributeError):
            h_dur, m_dur = 0, 30

        duracao_delta = timedelta(hours=h_dur, minutes=m_dur)
        if duracao_delta.total_seconds() == 0:
            duracao_delta = timedelta(minutes=30)

    # 4. Agendamentos Existentes do Colaborador no Dia
    inicio_dia = datetime.combine(data_alvo, time.min)
    fim_dia = datetime.combine(data_alvo, time.max)

    agendamentos_existentes = AghAgendamento.query.filter(
        AghAgendamento.profissional_id == contrato_id,
        AghAgendamento.data_hora_inicio >= inicio_dia,
        AghAgendamento.data_hora_inicio <= fim_dia,
        AghAgendamento.status.in_(['confirmado', 'pendente', 'em_andamento'])
    ).all()

    # 5. Geração e Filtragem dos Slots
    slots_livres = []
    passo = timedelta(minutes=30)
    agora = obter_hora_local()  # Uso da utilidade de fuso do projeto

    dt_expediente_inicio = datetime.combine(data_alvo, escala.inicio_expediente)
    dt_expediente_fim = datetime.combine(data_alvo, escala.fim_expediente)

    dt_intervalo_inicio = datetime.combine(data_alvo, escala.inicio_intervalo) if escala.inicio_intervalo else None
    dt_intervalo_fim = datetime.combine(data_alvo, escala.fim_intervalo) if escala.fim_intervalo else None

    dt_atual = dt_expediente_inicio
    while dt_atual + duracao_delta <= dt_expediente_fim:
        dt_fim_slot = dt_atual + duracao_delta

        # A) Checa se o slot intercepta o intervalo de almoço/descanso
        em_intervalo = False
        if dt_intervalo_inicio and dt_intervalo_fim:
            if (dt_atual < dt_intervalo_fim) and (dt_fim_slot > dt_intervalo_inicio):
                em_intervalo = True

        # B) Checa choque com agendamento do colaborador
        tem_conflito = False
        if not em_intervalo:
            for ag in agendamentos_existentes:
                # Remove timezone se necessário para comparação homogênea
                ag_ini = ag.data_hora_inicio.replace(tzinfo=None) if ag.data_hora_inicio else None
                ag_fim = ag.data_hora_fim.replace(tzinfo=None) if ag.data_hora_fim else None

                if ag_ini and ag_fim:
                    if (dt_atual < ag_fim) and (dt_fim_slot > ag_ini):
                        tem_conflito = True
                        break

        # C) Checa se é horário passado (se a consulta for para HOJE)
        eh_passado = (data_alvo == agora.date()) and (dt_atual < agora.replace(tzinfo=None))

        if not em_intervalo and not tem_conflito and not eh_passado:
            slots_livres.append(dt_atual.strftime("%H:%M"))

        dt_atual += passo

    return slots_livres


@agenda_bp.route('/notificacoes/<string:notificacao_id>/marcar-lida', methods=['POST'])
def marcar_notificacao_lida(notificacao_id):
    """
    Endpoint AJAX para marcar uma única notificação como lida/arquivada.
    """
    notificacao = AghNotificacao.query.get(notificacao_id)

    if not notificacao:
        return jsonify({'sucesso': False, 'mensagem': 'Notificação não encontrada.'}), 404

    try:
        # Usa o método nativo que definimos na tabela (atualiza lida=True e data_leitura)
        notificacao.marcar_como_lida()
        db.session.commit()

        return jsonify({
            'sucesso': True,
            'mensagem': 'Notificação arquivada com sucesso.',
            'id': notificacao_id
        }), 200

    except Exception as e:
        db.session.rollback()
        return jsonify({'sucesso': False, 'mensagem': f"Erro ao arquivar: {str(e)}"}), 500


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


def salvar_imagem_perfil_modulo(foto):
    """
    Processa a imagem recebida e SEMPRE converte o resultado para WEBP.

    Processamento:
        - Abre a imagem com Pillow
        - Corrige orientação EXIF
        - Trata transparência
        - Converte para RGB
        - Crop central 1:1
        - Redimensiona para 800x800
        - Salva exclusivamente em WEBP

    Retorna:
        uploads/avatares/perfil_<usuario>_<token>.webp

    Em caso de erro:
        None
    """

    # ==========================================================
    # 1. Validação básica
    # ==========================================================

    if not foto or not hasattr(foto, 'filename') or not foto.filename:
        return None

    # ==========================================================
    # 2. Define SEMPRE o nome/extensão como WEBP
    # ==========================================================

    codigo = secrets.token_hex(8)

    nome_arquivo = (
        f"perfil_{current_user.id}_{codigo}.webp"
    )

    # ==========================================================
    # 3. Diretório físico do módulo Agenda
    # ==========================================================

    pasta_destino = os.path.join(
        current_app.root_path,
        'modules',
        'agenda',
        'static',
        'uploads',
        'avatares'
    )

    os.makedirs(
        pasta_destino,
        exist_ok=True
    )

    caminho_completo = os.path.join(
        pasta_destino,
        nome_arquivo
    )

    try:

        # ======================================================
        # 4. Abre a imagem
        # ======================================================

        img = Image.open(foto)

        # Carrega efetivamente os dados da imagem
        img.load()

        # ======================================================
        # 5. Corrige orientação EXIF
        # ======================================================

        img = ImageOps.exif_transpose(img)

        # ======================================================
        # 6. Converte para RGB
        #
        # Transparência será colocada sobre fundo branco.
        # Isso é importante porque WEBP pode trabalhar com
        # transparência, mas seu avatar está sendo tratado
        # como imagem RGB.
        # ======================================================

        if img.mode in ('RGBA', 'LA'):

            fundo = Image.new(
                'RGB',
                img.size,
                (255, 255, 255)
            )

            fundo.paste(
                img,
                mask=img.getchannel('A')
            )

            img = fundo

        elif img.mode == 'P':

            img = img.convert('RGBA')

            fundo = Image.new(
                'RGB',
                img.size,
                (255, 255, 255)
            )

            fundo.paste(
                img,
                mask=img.getchannel('A')
            )

            img = fundo

        elif img.mode != 'RGB':

            img = img.convert('RGB')

        # ======================================================
        # 7. Crop central 1:1
        # ======================================================

        largura, altura = img.size

        if largura > altura:

            esquerda = (largura - altura) // 2

            img = img.crop((
                esquerda,
                0,
                esquerda + altura,
                altura
            ))

        elif altura > largura:

            topo = (altura - largura) // 2

            img = img.crop((
                0,
                topo,
                largura,
                topo + largura
            ))

        # ======================================================
        # 8. Redimensiona para 800x800
        # ======================================================

        img = img.resize(
            (800, 800),
            Image.Resampling.LANCZOS
        )

        # ======================================================
        # 9. SALVA SEMPRE COMO WEBP
        # ======================================================

        img.save(
            caminho_completo,
            format='WEBP',
            quality=85,
            method=6
        )

        # ======================================================
        # 10. Retorna somente o caminho relativo
        # ======================================================

        return (
            f"uploads/avatares/{nome_arquivo}"
        )

    except Exception as e:

        current_app.logger.exception(
            f"Erro ao processar imagem de perfil: {e}"
        )

        # Remove arquivo incompleto, caso tenha sido criado
        try:
            if os.path.exists(caminho_completo):
                os.remove(caminho_completo)
        except Exception:
            pass

        return None


@agenda_bp.route('/perfil/upload-foto', methods=['POST'])
@login_required
def upload_foto_perfil():
    current_app.logger.info("=== [DEBUG UPLOAD FOTO] INÍCIO DO PROCESSAMENTO ===")

    # ==========================================================
    # 1. Validação do arquivo enviado
    # ==========================================================
    if 'foto' not in request.files:
        current_app.logger.warning("[DEBUG UPLOAD FOTO] Nenhum arquivo recebido no request.files")
        return jsonify({'sucesso': False, 'mensagem': 'Nenhum arquivo enviado.'}), 400

    foto = request.files['foto']

    if not foto or not foto.filename:
        current_app.logger.warning("[DEBUG UPLOAD FOTO] Arquivo recebido está vazio ou sem nome")
        return jsonify({'sucesso': False, 'mensagem': 'Nenhum arquivo selecionado.'}), 400

    current_app.logger.info(f"[DEBUG UPLOAD FOTO] Arquivo recebido: {foto.filename} ({foto.content_type})")

    # ==========================================================
    # 2. Processa e salva a imagem fisicamente
    # ==========================================================
    try:
        caminho_relativo = salvar_imagem_perfil_modulo(foto)
    except Exception as e:
        current_app.logger.exception(f'[DEBUG UPLOAD FOTO] Erro ao salvar imagem no disco: {e}')
        return jsonify({'sucesso': False, 'mensagem': 'Erro ao processar imagem.'}), 500

    if not caminho_relativo:
        current_app.logger.error("[DEBUG UPLOAD FOTO] salvar_imagem_perfil_modulo retornou None/Vazio")
        return jsonify({'sucesso': False, 'mensagem': 'Erro ao processar imagem.'}), 500

    # Normalização do caminho estático
    caminho_relativo = caminho_relativo.replace('\\', '/').lstrip('/')
    if caminho_relativo.startswith('agenda/'):
        caminho_relativo = caminho_relativo[len('agenda/'):]
    if caminho_relativo.startswith('static/'):
        caminho_relativo = caminho_relativo[len('static/'):]

    current_app.logger.info(f"[DEBUG UPLOAD FOTO] Caminho relativo normalizado: '{caminho_relativo}'")

    # ==========================================================
    # 3. Inspeção e Resolução da Identidade (Session, Current User e ModCadastroCliente)
    # ==========================================================

    # Audit das variáveis da Sessão Flask
    session_dump = {
        'cpf_hash': session.get('cpf_hash'),
        'email': session.get('email'),
        'email_modulo_agenda': session.get('email_modulo_agenda'),
        'cliente_modulo_id': session.get('cliente_modulo_id'),
        'usuario_id': session.get('usuario_id')
    }
    current_app.logger.info(f"[DEBUG UPLOAD FOTO] Estado da Session Flask: {session_dump}")

    # Audit do Current User
    current_app.logger.info(
        f"[DEBUG UPLOAD FOTO] current_user Tipo: {type(current_user).__name__} | "
        f"ID: {getattr(current_user, 'id', None)} | "
        f"E-mail: '{getattr(current_user, 'email', None)}' | "
        f"CPF Hash: '{getattr(current_user, 'cpf_hash', None)}'"
    )

    # Identifica o cadastro de cliente correto
    if isinstance(current_user, ModCadastroCliente):
        cadastro_cliente = current_user
    else:
        # Busca prioritária considerando e-mail da sessão para evitar pegar o cadastro errado
        email_sessao = session.get('email_modulo_agenda') or session.get('email')
        query_cadastro = ModCadastroCliente.query.filter_by(usuario_id=getattr(current_user, 'id', None))

        if email_sessao:
            cadastro_cliente = query_cadastro.filter(
                func.lower(ModCadastroCliente.email) == email_sessao.strip().lower()
            ).first()
        else:
            cadastro_cliente = query_cadastro.first()

    # Resolução dos alvos com sanitização e trimming
    cpf_alvo = (
            getattr(cadastro_cliente, 'cpf_hash', None)
            or getattr(current_user, 'cpf_hash', None)
            or session.get('cpf_hash')
    )

    email_alvo = (
            session.get('email_modulo_agenda')
            or getattr(cadastro_cliente, 'email', None)
            or getattr(current_user, 'email', None)
            or session.get('email')
    )

    if cpf_alvo:
        cpf_alvo = str(cpf_alvo).strip()
    if email_alvo:
        email_alvo = str(email_alvo).strip().lower()

    current_app.logger.info(
        f"[DEBUG UPLOAD FOTO] Credenciais Resolvidas -> CPF_ALVO: '{cpf_alvo}' | EMAIL_ALVO: '{email_alvo}'"
    )

    if not cpf_alvo or not email_alvo:
        current_app.logger.error("[DEBUG UPLOAD FOTO] Falha: CPF ou E-mail estão nulos.")
        return jsonify({
            'sucesso': False,
            'mensagem': 'Não foi possível identificar as credenciais (CPF/E-mail) do cliente.'
        }), 400

    # ==========================================================
    # 4. DIAGNÓSTICO: Buscar TODOS os vínculos com o mesmo CPF
    # ==========================================================
    todos_vinculos_cpf = ModVinculoModulo.query.filter_by(
        modulo_slug='agenda',
        cpf_hash=cpf_alvo
    ).all()

    current_app.logger.info(
        f"[DEBUG UPLOAD FOTO] Total de vínculos na Agenda com o CPF '{cpf_alvo}': {len(todos_vinculos_cpf)}")
    for idx, v in enumerate(todos_vinculos_cpf, start=1):
        # Alterado de "└─" para "[+]" para previne UnicodeEncodeError no Windows CP1252
        current_app.logger.info(
            f"   [+] Vínculo #{idx} [ID: {v.id}] | "
            f"email_customizado: '{v.email_customizado}' | "
            f"local_id: {getattr(v, 'local_id', None)} | "
            f"foto_url atual: '{v.foto_url}'"
        )

    # ==========================================================
    # 5. Busca Exata com Case-Insensitive (func.lower)
    # ==========================================================
    vinculo = ModVinculoModulo.query.filter(
        ModVinculoModulo.modulo_slug == 'agenda',
        ModVinculoModulo.cpf_hash == cpf_alvo,
        func.lower(ModVinculoModulo.email_customizado) == email_alvo
    ).first()

    if not vinculo:
        current_app.logger.error(
            f"[DEBUG UPLOAD FOTO] NENHUM VÍNCULO ENCONTRADO para modulo_slug='agenda', "
            f"cpf_hash='{cpf_alvo}' e email_customizado='{email_alvo}'."
        )
        return jsonify({
            'sucesso': False,
            'mensagem': f'Vínculo não encontrado para o e-mail ({email_alvo}) e CPF informados.'
        }), 404

    current_app.logger.info(
        f"[DEBUG UPLOAD FOTO] Vínculo EXACT ENCONTRADO! ID: {vinculo.id} | "
        f"E-mail no banco: '{vinculo.email_customizado}' | "
        f"Foto antiga: '{vinculo.foto_url}'"
    )

    # ==========================================================
    # 6. Atualização e Persistência
    # ==========================================================
    vinculo.foto_url = caminho_relativo

    try:
        db.session.commit()
        current_app.logger.info(f"[DEBUG UPLOAD FOTO] COMMIT REALIZADO COM SUCESSO no Vínculo ID {vinculo.id}!")
    except Exception as e:
        db.session.rollback()
        current_app.logger.exception(f'[DEBUG UPLOAD FOTO] Erro ao dar commit no banco: {e}')
        return jsonify({'sucesso': False, 'mensagem': 'Erro ao salvar a foto no banco de dados.'}), 500

    # ==========================================================
    # 7. Geração da URL Global e Retorno
    # ==========================================================
    # Garante que o caminho passe limpo para a helper get_avatar_url
    # CÓDIGO CORRIGIDO:
    # Se caminho_relativo for 'uploads/avatares/perfil_...webp', remove o 'uploads/' inicial
    clean_path = caminho_relativo.replace('\\', '/').lstrip('/')
    if clean_path.startswith('uploads/'):
        clean_path = clean_path[len('uploads/'):]

    url_publica = f"/media/{clean_path}"

    current_app.logger.info(f"[DEBUG UPLOAD FOTO] URL gerada: '{url_publica}'. Finalizando com sucesso.")

    return jsonify({
        'sucesso': True,
        'mensagem': 'Foto atualizada com sucesso!',
        'foto_url': url_publica
    })

    current_app.logger.info(f"[DEBUG UPLOAD FOTO] URL gerada: '{url_publica}'. Finalizando com sucesso.")

    return jsonify({
        'sucesso': True,
        'mensagem': 'Foto atualizada com sucesso!',
        'foto_url': url_publica
    })


@agenda_bp.route('/catalogo', methods=['GET'])
@login_required
def catalogo_servicos():
    """
    📌 BUSCA INTELIGENTE DE ESTABELECIMENTOS E CATEGORIAS (HUB COMERCIAL)
    --------------------------------------------------------------------------------------
    Objetivo:
        Exibir o diretório de empresas/estabelecimentos ativos no FeedIn.
        A busca localiza a empresa por:
          1. Nome da Empresa (EseEmpresa.nome)
          2. Categoria Principal da Empresa (EseEmpresa.categoria -> Taxonomia)
          3. Serviços oferecidos no catálogo (EseServicoOferecido -> Taxonomia)
    """
    termo_busca = request.args.get('q', '').strip()
    categoria_id = request.args.get('categoria', type=int)

    # ----------------------------------------------------------------------------------
    # 1. CATEGORIAS DE EMPRESAS PARA FILTRAGEM (PÍLULAS)
    #    Traz apenas categorias que possuem pelo menos 1 empresa ativa/verificada (A-Z)
    # ----------------------------------------------------------------------------------
    categorias = []
    try:
        categorias = (
            Taxonomia.query.join(EseEmpresa, EseEmpresa.categoria == Taxonomia.id)
            .join(Local, EseEmpresa.local_id == Local.id)
            .filter(
                or_(
                    Local.status_operacional == "ativo",
                    Local.status_operacional == "verificado",
                )
            )
            .group_by(Taxonomia.id)
            .order_by(Taxonomia.nome.asc())
            .all()
        )
    except Exception as e:
        print(f"DEBUG CATALOGO: Erro ao carregar categorias -> {e}")

    # ----------------------------------------------------------------------------------
    # 2. QUERY BASE CENTRALIZADA NA ESE_EMPRESA
    # ----------------------------------------------------------------------------------
    aliased_servico = aliased(Taxonomia)

    query = (
        db.session.query(EseEmpresa)
        .join(Local, EseEmpresa.local_id == Local.id)
        .outerjoin(Taxonomia, EseEmpresa.categoria == Taxonomia.id)
        .outerjoin(EseServicoOferecido, EseServicoOferecido.empresa_id == EseEmpresa.id)
        .outerjoin(aliased_servico, EseServicoOferecido.taxonomia_id == aliased_servico.id)
        .filter(
            or_(
                Local.status_operacional == "ativo",
                Local.status_operacional == "verificado",
            )
        )
    )

    # ----------------------------------------------------------------------------------
    # 3. FILTRAGEM MULTICAMADA
    # ----------------------------------------------------------------------------------
    if termo_busca:
        subq_servicos = (
            db.session.query(EseServicoOferecido.empresa_id)
            .join(Taxonomia, EseServicoOferecido.taxonomia_id == Taxonomia.id)
            .filter(
                or_(
                    Taxonomia.nome.ilike(f"%{termo_busca}%"),
                    EseServicoOferecido.descricao_servico.ilike(f"%{termo_busca}%")
                )
            )
        )

        query = query.filter(
            or_(
                EseEmpresa.nome.ilike(f"%{termo_busca}%"),
                Taxonomia.nome.ilike(f"%{termo_busca}%"),
                EseEmpresa.id.in_(subq_servicos)
            )
        )

    if categoria_id:
        query = query.filter(EseEmpresa.categoria == categoria_id)

    # ----------------------------------------------------------------------------------
    # 4. EXECUÇÃO DA BUSCA E MONTAGEM DO RETORNO
    # ----------------------------------------------------------------------------------
    empresas_resultado = (
        query.options(
            joinedload(EseEmpresa.categoria_rel),
            joinedload(EseEmpresa.local_fisico)
        )
        .distinct()
        .order_by(EseEmpresa.nome.asc())
        .all()
    )

    lista_empresas = []
    for emp in empresas_resultado:
        lista_empresas.append({
            'id': emp.id,
            'nome': emp.nome,
            'slug': emp.slug,
            'logomarca': emp.logomarca,
            'fachada': emp.fachada,
            'categoria_nome': emp.categoria_rel.nome if emp.categoria_rel else 'Estabelecimento',
            'endereco': f"{emp.local_fisico.logradouro}, {emp.local_fisico.numero}" if emp.local_fisico else "Atendimento Digital/Local",
            'bairro': emp.local_fisico.bairro if emp.local_fisico else "",
            'cor_primaria': emp.cor_primaria or "#111827"
        })

    return render_template(
        'agenda/catalogo.html',
        categorias=categorias,
        empresas=lista_empresas,
        termo_busca=termo_busca,
        categoria_selecionada=categoria_id
    )


@agenda_bp.route('/meus-agendamentos', methods=['GET'])
@login_required
def meus_agendamentos():
    """
    📌 TELA DE GERENCIAMENTO DE AGENDAMENTOS DO CLIENTE (MULTI-SERVIÇO & MULTI-COLABORADOR)
    """
    if isinstance(current_user, ModCadastroCliente):
        cadastro_cliente = current_user
    else:
        cadastro_cliente = ModCadastroCliente.query.filter_by(usuario_id=current_user.id).first()

    agora = obter_hora_local()
    agora_comparacao = agora.replace(tzinfo=None) if hasattr(agora, 'tzinfo') and agora.tzinfo else agora

    proximos_agendamentos = []
    historico_agendamentos = []

    if cadastro_cliente:
        # Opções de carregamento otimizado para a hierarquia Agendamento -> Itens -> Serviços/Colaboradores
        opcoes_carregamento = [
            joinedload(AghAgendamento.empresa),
            joinedload(AghAgendamento.profissional),
            selectinload(AghAgendamento.itens).joinedload(AghAgendamentoItem.servico).joinedload(EseServicoOferecido.servico_taxonomia),
            selectinload(AghAgendamento.itens).joinedload(AghAgendamentoItem.profissional) # Ou o relacionamento do colaborador no Item
        ]

        # Compromissos futuros/ativos
        proximos_agendamentos = (
            AghAgendamento.query.options(*opcoes_carregamento)
            .filter_by(cliente_id=cadastro_cliente.id)
            .filter(
                AghAgendamento.data_hora_inicio >= agora_comparacao,
                AghAgendamento.status.in_(['agendado', 'confirmado', 'aguardando', 'reagendamento_pendente'])
            )
            .order_by(AghAgendamento.data_hora_inicio.asc())
            .all()
        )

        # Compromissos passados ou encerrados/cancelados
        historico_agendamentos = (
            AghAgendamento.query.options(*opcoes_carregamento)
            .filter_by(cliente_id=cadastro_cliente.id)
            .filter(
                or_(
                    AghAgendamento.data_hora_inicio < agora_comparacao,
                    AghAgendamento.status.in_(['cancelado', 'concluido', 'finalizado', 'ausente_pendente'])
                )
            )
            .order_by(AghAgendamento.data_hora_inicio.desc())
            .limit(20)
            .all()
        )

    return render_template(
        'agenda/meus_agendamentos.html',
        proximos=proximos_agendamentos,
        historico=historico_agendamentos,
        cliente=cadastro_cliente
    )


@agenda_bp.route(
    '/agendamento/<int:agendamento_id>/cancelar', methods=['POST']
)
def cancelar_agendamento(agendamento_id):
  # Eager loading para carregar o agendamento junto com seus itens em consulta única
  agendamento = (
      AghAgendamento.query.options(selectinload(AghAgendamento.itens))
      .filter_by(id=agendamento_id)
      .first()
  )

  if not agendamento:
    return (
        jsonify({'sucesso': False, 'mensagem': 'Agendamento não encontrado.'}),
        404,
    )

  # 1. Identificação do Cliente Logado
  id_cliente_logado = None
  if hasattr(current_user, 'cliente') and current_user.cliente:
    id_cliente_logado = current_user.cliente.id
  elif isinstance(current_user, ModCadastroCliente):
    id_cliente_logado = current_user.id
  else:
    cliente_obj = ModCadastroCliente.query.filter_by(
        usuario_id=current_user.id
    ).first()
    if cliente_obj:
      id_cliente_logado = cliente_obj.id

  is_cliente = (id_cliente_logado is not None) and (
      agendamento.cliente_id == id_cliente_logado
  )

  # Validação de regras/funções administrativas
  has_role_func = getattr(current_user, 'has_role', lambda r: False)
  is_admin_ou_staff = any(
      has_role_func(role) for role in ['admin', 'gestor', 'colaborador']
  )

  if not (is_cliente or is_admin_ou_staff):
    return (
        jsonify({
            'sucesso': False,
            'mensagem': 'Você não tem permissão para cancelar este agendamento.',
        }),
        403,
    )

  if agendamento.status == 'cancelado':
    return (
        jsonify({
            'sucesso': False,
            'mensagem': 'Este agendamento já se encontra cancelado.',
        }),
        400,
    )

  # Horário local padronizado
  agora_local = (
      obter_hora_local().replace(tzinfo=None)
      if 'obter_hora_local' in globals()
      else datetime.now()
  )

  # 2. Validação Temporal para Clientes
  if is_cliente:
    if agora_local >= agendamento.data_hora_inicio:
      return (
          jsonify({
              'sucesso': False,
              'mensagem': (
                  'Não é possível cancelar um agendamento cujo horário já'
                  ' passou.'
              ),
          }),
          400,
      )

    horas_limite = (
        getattr(agendamento.empresa, 'horas_limite_cancelamento', 2) or 2
    )
    limite_cancelamento = agendamento.data_hora_inicio - timedelta(
        hours=horas_limite
    )

    if agora_local > limite_cancelamento:
      return (
          jsonify({
              'sucesso': False,
              'mensagem': (
                  'Cancelamento direto permitido apenas com até'
                  f' {horas_limite}h de antecedência. Entre em contato com o'
                  ' estabelecimento.'
              ),
          }),
          400,
      )

  data = request.get_json() or {}
  motivo_cat = data.get('motivo_categoria', 'desistencia')
  motivo_det = data.get('motivo_detalhado', '').strip()

  try:
    data_cancelamento = (
        obter_hora_local().replace(tzinfo=None)
        if 'obter_hora_local' in globals()
        else datetime.now()
    )

    # 3. Atualiza Agendamento Principal
    agendamento.status = 'cancelado'
    agendamento.atualizado_em = data_cancelamento

    # 4. Tratativa dos Itens
    for item in agendamento.itens:
      item.status = 'cancelado'
      if hasattr(item, 'atualizado_em'):
        item.atualizado_em = data_cancelamento

    # 5. Define Origem
    if is_cliente:
      origem = 'cliente'
    elif has_role_func('gestor') or has_role_func('admin'):
      origem = 'gestor'
    else:
      origem = 'colaborador'

    valor_pago = getattr(agendamento, 'valor_pago', 0.00) or 0.00
    requer_reembolso = valor_pago > 0

    # 6. Grava Registro em AghCancelamento
    novo_cancelamento = AghCancelamento(
        agendamento_id=agendamento.id,
        empresa_id=agendamento.empresa_id,
        solicitante_id=str(current_user.id),
        origem_solicitacao=origem,
        motivo_categoria=motivo_cat,
        motivo_detalhado=motivo_det,
        valor_pago_momento=valor_pago,
        requer_ressarcimento=requer_reembolso,
        status_ressarcimento=(
            'pendente' if requer_reembolso else 'nao_aplicavel'
        ),
        solicitado_em=data_cancelamento,
        criado_em=data_cancelamento,
    )
    db.session.add(novo_cancelamento)

    # 7. Grava Evento no Histórico de Presença (Prontuário/Score do Cliente)
    historico_cancelamento = AghHistoricoPresenca(
        estabelecimento_id=agendamento.empresa_id,
        cliente_id=agendamento.cliente_id,
        agendamento_id=agendamento.id,
        tipo_evento=(
            'cancelamento_cliente'
            if origem == 'cliente'
            else 'cancelamento_estabelecimento'
        ),
        data_hora_agendada=agendamento.data_hora_inicio,
        data_hora_evento=data_cancelamento,
        desvio_minutos=0,
        motivo=f'Categoria: {motivo_cat} | Detalhes: {motivo_det or "Não informado"}',
    )
    db.session.add(historico_cancelamento)

    # Commit Atômico Único
    db.session.commit()

    return (
        jsonify({
            'sucesso': True,
            'mensagem': (
                'Agendamento e seus serviços associados foram cancelados com'
                ' sucesso.'
            ),
        }),
        200,
    )

  except Exception as e:
    db.session.rollback()
    return (
        jsonify({
            'sucesso': False,
            'mensagem': f'Erro ao processar cancelamento: {str(e)}',
        }),
        500,
    )


# =============================================================================
# 🛠️ FUNÇÕES AUXILIARES (HELPERS)
# =============================================================================

def _extrair_ids_servicos(dados, request_args) -> list[int]:
    """
    Extrai e sanitiza os IDs de serviços a partir dos campos do form, JSON ou querystring.
    """
    servico_ids_raw = (
            dados.get('servico_ids')
            or request_args.get('servicos')
            or request_args.get('servico_id')
            or ''
    )
    if isinstance(servico_ids_raw, list):
        return [int(x) for x in servico_ids_raw if str(x).isdigit()]

    return [
        int(x.strip())
        for x in str(servico_ids_raw).split(',')
        if x.strip().isdigit()
    ]


def _formatar_duracao(minutos: int) -> str:
    """
    Retorna a duração em minutos formatada de forma amigável (ex: '1h 15 min' ou '45 min').
    """
    if minutos >= 60:
        horas = minutos // 60
        mins = minutos % 60
        return f"{horas}h {mins} min" if mins > 0 else f"{horas}h"
    if minutos > 0:
        return f"{minutos} min"
    return "30 min"


# =============================================================================
# 📌 ROTA PRINCIPAL DE AGENDAMENTO E REAGENDAMENTO
# =============================================================================

TZ_SP = ZoneInfo('America/Sao_Paulo')


# =============================================================================
# 🛠️ HELPERS AUXILIARES DE RESPOSTA E SESSÃO
# =============================================================================

def _obter_ou_criar_sessao_cliente() -> tuple[str | None, str]:
    """Retorna a dupla (cliente_id, session_token)."""
    cliente_id = str(current_user.id) if current_user.is_authenticated else None
    session_token = session.get('agendamento_session_token')

    if not cliente_id and not session_token:
        session_token = secrets.token_hex(16)
        session['agendamento_session_token'] = session_token

    return cliente_id, session_token


def _resposta_erro(mensagem: str, status_code: int, slug_empresa: str, reagendar_id: int | None = None,
                   categoria_flash: str = 'danger'):
    """Padroniza respostas de erro adaptando-se para solicitações JSON ou HTML."""
    if request.is_json:
        return jsonify({'status': 'erro', 'mensagem': mensagem}), status_code
    flash(mensagem, categoria_flash)
    return redirect(url_for('agenda.agendar', slug_empresa=slug_empresa, reagendar_id=reagendar_id))


def _obter_rascunho_ativo(empresa_id: int, cliente_id: str | None, session_token: str):
    """Localiza o rascunho de agendamento ativo."""
    q_rascunho = AghAgendamentoRascunho.query.filter_by(
        empresa_id=empresa_id,
        status='servicos_selecionados'
    )
    if cliente_id:
        q_rascunho = q_rascunho.filter_by(cliente_id=cliente_id)
    else:
        q_rascunho = q_rascunho.filter_by(session_token=session_token)

    return q_rascunho.order_by(AghAgendamentoRascunho.atualizado_em.desc()).first()


# =============================================================================
# 🧮 HELPERS DE REGRAS DE NEGÓCIO (PRAZOS E CONFLITOS)
# =============================================================================

# =============================================================================
# 🧮 HELPERS DE REGRAS DE NEGÓCIO (VERSÃO ENXUTA)
# =============================================================================

def _avaliar_prazo_reagendamento(agendamento_original):
    """Calcula horas de antecedência e limite de expiração."""
    if not agendamento_original or not agendamento_original.data_hora_inicio:
        return None, None

    orig_dt = agendamento_original.data_hora_inicio
    orig_dt = orig_dt.replace(tzinfo=TZ_SP) if orig_dt.tzinfo is None else orig_dt
    horas_antecedencia = (orig_dt - datetime.now(TZ_SP)).total_seconds() / 3600.0

    # Antecedência < 2h: trava expiração em 7 dias a partir da data original
    data_expiracao = (orig_dt + timedelta(days=7)) if horas_antecedencia < 2.0 else None
    return horas_antecedencia, data_expiracao


def _calcular_duracao_total(rascunho=None, agendamento_original=None, dados=None, args=None):
    # INICIALIZAÇÃO OBRIGATÓRIA NA LINHA 1 DA FUNÇÃO
    servico_ids = []

    if dados:
        raw_servicos = dados.get('servico_ids') or dados.get('servicos') or dados.get('servico_id')
        if raw_servicos:
            if isinstance(raw_servicos, str):
                servico_ids = [int(x.strip()) for x in raw_servicos.split(',') if x.strip().isdigit()]
            elif isinstance(raw_servicos, (list, set, tuple)):
                servico_ids = [int(x) for x in raw_servicos if str(x).isdigit()]
            elif isinstance(raw_servicos, int):
                servico_ids = [raw_servicos]

    if not servico_ids and rascunho and getattr(rascunho, 'itens', None):
        servico_ids = [item.servico_id for item in rascunho.itens if getattr(item, 'servico_id', None)]

    if not servico_ids and agendamento_original and getattr(agendamento_original, 'itens', None):
        servico_ids = [item.servico_id for item in agendamento_original.itens if getattr(item, 'servico_id', None)]

    if not servico_ids and args:
        raw_args = args.get('servico_ids') or args.get('servicos') or args.get('servico_id')
        if raw_args:
            servico_ids = [int(x.strip()) for x in str(raw_args).split(',') if x.strip().isdigit()]

    if not servico_ids:
        return 0

    servicos_db = AghServico.query.filter(AghServico.id.in_(servico_ids)).all()

    duracao_total = sum(
        getattr(s, 'duracao_minutos_int', 0) or getattr(s, 'duracao_minutos', 0) or 0
        for s in servicos_db
    )

    return duracao_total if duracao_total > 0 else 30


def _existe_conflito_horario(empresa_id: int, profissional_id: int, inicio_dt: datetime, fim_dt: datetime, ignorar_id: int | None = None) -> bool:
    """Verifica sobreposição de agenda para o profissional."""
    q = AghAgendamento.query.filter(
        AghAgendamento.empresa_id == empresa_id,
        AghAgendamento.profissional_id == profissional_id,
        AghAgendamento.status.in_(['confirmado', 'pendente']),
        AghAgendamento.data_hora_inicio < fim_dt,
        AghAgendamento.data_hora_fim > inicio_dt
    )
    if ignorar_id:
        q = q.filter(AghAgendamento.id != ignorar_id)

    return q.first() is not None


def _resolver_profissional_id(empresa, profissional_id: int | None, inicio_dt: datetime, fim_dt: datetime, agendamento_original) -> int | None:
    """Retorna o profissional selecionado ou o primeiro disponível na empresa."""
    if profissional_id or not getattr(empresa, 'local_id', None):
        return profissional_id

    colaboradores = ColaboradorContrato.query.filter_by(
        id_local=empresa.local_id,
        status_profissional='ativo'
    ).filter(ColaboradorContrato.data_desligamento.is_(None)).all()

    ignorar_id = agendamento_original.id if agendamento_original else None

    for colab in colaboradores:
        if not _existe_conflito_horario(empresa.id, colab.id, inicio_dt, fim_dt, ignorar_id):
            return colab.id

    return None


def _preparar_itens_para_gravar(rascunho, agendamento_original, empresa_id: int, dados, req_args) -> list[dict]:
    """Normaliza a lista de itens de serviço a serem vinculados ao novo agendamento."""
    itens = []

    if rascunho and rascunho.itens:
        for item in rascunho.itens:
            raw_dur = getattr(item, 'duracao_minutos', None)
            if not raw_dur and item.servico_id:
                srv = EseServicoOferecido.query.get(item.servico_id)
                if srv:
                    raw_dur = getattr(srv, 'duracao_minutos', None) or getattr(srv, 'tempo_duracao', None)

            itens.append({
                'servico_id': item.servico_id,
                'preco_unitario': item.preco_unitario or 0.00,
                'duracao_minutos': converter_duracao_para_minutos(raw_dur)
            })

    elif agendamento_original and agendamento_original.itens:
        for item in agendamento_original.itens:
            itens.append({
                'servico_id': item.servico_id,
                'preco_unitario': item.preco_unitario or 0.00,
                'duracao_minutos': item.duracao_minutos
            })

    else:
        s_ids = _extrair_ids_servicos(dados, req_args)
        if s_ids:
            servicos = EseServicoOferecido.query.filter(EseServicoOferecido.id.in_(s_ids)).all()
            for srv in servicos:
                raw_dur = getattr(srv, 'duracao_minutos', None) or getattr(srv, 'tempo_duracao', None)
                preco_obj = EseServicoPreco.query.filter_by(empresa_id=empresa_id,
                                                            taxonomia_id=srv.taxonomia_id).first()
                preco_val = float(preco_obj.novo_valor) if (preco_obj and preco_obj.novo_valor) else 0.00

                itens.append({
                    'servico_id': srv.id,
                    'preco_unitario': preco_val,
                    'duracao_minutos': converter_duracao_para_minutos(raw_dur)
                })

    return itens


# 💡 Certifique-se de importar a função da sua camada de serviço de agendamentos:
from feedin.modules.agenda.services.agendamento_service import obter_colaboradores_e_horarios_disponiveis

# =============================================================================
# 🎯 FUNÇÃO AUXILIAR DE ALOCAÇÃO MULTI-PROFISSIONAL
# (Coloque esta função logo acima do controller do agendar)
# =============================================================================
from datetime import datetime, timedelta, time


from datetime import datetime, timedelta
from sqlalchemy import or_, and_, func

def _atribuir_profissionais_e_horarios(empresa_id, objs_servicos, data_inicio_base, ignorar_agendamento_id=None):
    """
    Calcula a sequência de horários e aloca especialistas habilitados para cada item do agendamento.
    """

    def _extrair_minutos(servico_obj):
        val = getattr(servico_obj, 'duracao_minutos_int', None) or \
              getattr(servico_obj, 'duracao_minutos', None) or \
              getattr(servico_obj, 'tempo_duracao', None)
        if not val:
            return 30
        if isinstance(val, int) or (isinstance(val, str) and val.isdigit()):
            return int(val)
        if isinstance(val, str) and ":" in val:
            try:
                partes = val.split(":")
                return (int(partes[0]) * 60) + int(partes[1])
            except (ValueError, IndexError):
                return 30
        return 30

    itens_planejados = []
    tempo_acumulado = data_inicio_base
    duracao_total_acumulada = 0

    for idx, s_obj in enumerate(objs_servicos, start=1):
        duracao_min = _extrair_minutos(s_obj)

        horario_inicio_item = tempo_acumulado
        horario_fim_item = horario_inicio_item + timedelta(minutes=duracao_min)

        # Encontra o colaborador apto usando a função auxiliar declarada acima
        colaborador_alocado_id = _encontrar_colaborador_apto(
            empresa_id, s_obj.id, horario_inicio_item, horario_fim_item, ignorar_agendamento_id
        )

        if not colaborador_alocado_id:
            nome_servico = getattr(s_obj, 'nome', None) or f"ID {s_obj.id}"
            return None, 0, f"Não há profissional disponível para o serviço '{nome_servico}' às {horario_inicio_item.strftime('%H:%M')}."

        itens_planejados.append({
            'servico_id': s_obj.id,
            'servico_obj': s_obj,
            'colaborador_id': colaborador_alocado_id,
            'duracao_minutos': duracao_min,
            'ordem_execucao': idx,
            'hora_inicio': horario_inicio_item,
            'hora_fim': horario_fim_item
        })

        tempo_acumulado = horario_fim_item
        duracao_total_acumulada += duracao_min

    return itens_planejados, duracao_total_acumulada, None


def obter_colaboradores_e_horarios_disponiveis(empresa_id: int, data_consulta, servico_ids=None, profissional_id=None, ignorar_agendamento_id: int = None):
    """
    Motor de Busca do FeedIn:
    Determina os colaboradores aptos e os slots de horários livres para agendamento.
    Retorna a tupla: (lista_horarios_formatados, dict_colaboradores_por_servico, lista_colaboradores_aptos)
    """
    agora = datetime.utcnow()
    dia_semana_py = data_consulta.weekday()  # 0 = Segunda, 6 = Domingo

    print(f"\n🔎 [DEBUG MOTOR] Data Consulta: {data_consulta} (Dia da semana Python: {dia_semana_py})")

    # 1. Colaboradores Ativos
    query_contratos = ColaboradorContrato.query.filter(
        ColaboradorContrato.id_local == empresa_id,
        ColaboradorContrato.status_profissional == 'ativo',
        ColaboradorContrato.data_desligamento.is_(None)
    )

    if profissional_id:
        query_contratos = query_contratos.filter(ColaboradorContrato.id == profissional_id)

    contratos_base = query_contratos.all()

    print(f"  ├─ 1. Colaboradores Ativos Encontrados: {len(contratos_base)}")

    if not contratos_base:
        print("  └─ ❌ [BLOQUEIO MOTOR] Nenhum colaborador ativo encontrado na empresa!")
        return [], {}, []

    contratos_dict = {c.id: c for c in contratos_base}
    contratos_ids = list(contratos_dict.keys())

    # Mapa de colaboradores por serviço: { servico_id: [ColaboradorContrato, ...] }
    colaboradores_por_servico = {}

    if servico_ids:
        for s_id in servico_ids:
            habilidades = EseColaboradorServicoHabilidade.query.filter(
                EseColaboradorServicoHabilidade.contrato_id.in_(contratos_ids),
                EseColaboradorServicoHabilidade.servico_oferecido_id == s_id
            ).all()

            c_ids_servico = [h.contrato_id for h in habilidades]
            colaboradores_por_servico[s_id] = [contratos_dict[cid] for cid in c_ids_servico if cid in contratos_dict]
            print(f"  ├─ Servico ID {s_id} -> Colaboradores Habilitados: {[c.id for c in colaboradores_por_servico[s_id]]}")
    else:
        for c in contratos_base:
            colaboradores_por_servico.setdefault(0, []).append(c)

    # 2. Duração Total dos Serviços Solicitados
    duracao_total_min = 30
    if servico_ids:
        servicos = EseServicoOferecido.query.filter(
            EseServicoOferecido.id.in_(servico_ids),
            EseServicoOferecido.empresa_id == empresa_id
        ).all()
        if servicos:
            def _to_minutes(d):
                if isinstance(d, int):
                    return d
                if str(d).isdigit():
                    return int(d)
                if hasattr(d, 'hour'):
                    return d.hour * 60 + d.minute
                return 30

            duracao_total_min = sum(_to_minutes(getattr(s, 'duracao_minutos', None) or getattr(s, 'tempo_duracao', 30)) for s in servicos)

    duracao_delta = timedelta(minutes=duracao_total_min)
    passo_slot = timedelta(minutes=30)

    # 3. Janelas da Empresa
    janelas_empresa = []
    excecao_empresa = EseExcecaoCalendario.query.filter(
        EseExcecaoCalendario.empresa_id == empresa_id,
        EseExcecaoCalendario.contrato_id.is_(None),
        EseExcecaoCalendario.ativo == True,
        EseExcecaoCalendario.data_inicio <= data_consulta,
        EseExcecaoCalendario.data_fim >= data_consulta
    ).first()

    if excecao_empresa:
        if not excecao_empresa.trabalha:
            print("  └─ ❌ [BLOQUEIO MOTOR] Empresa em exceção de fechamento total no dia.")
            return [], colaboradores_por_servico, contratos_base
        elif excecao_empresa.inicio_expediente and excecao_empresa.fim_expediente:
            janelas_empresa.append((
                datetime.combine(data_consulta, excecao_empresa.inicio_expediente),
                datetime.combine(data_consulta, excecao_empresa.fim_expediente)
            ))

    if not janelas_empresa:
        horarios_empresa = EseHorarioFuncionamento.query.filter(
            EseHorarioFuncionamento.empresa_id == empresa_id,
            or_(
                EseHorarioFuncionamento.dia_semana == dia_semana_py,
                EseHorarioFuncionamento.dia_semana == (dia_semana_py + 1)
            )
        ).order_by(EseHorarioFuncionamento.horario_abertura).all()

        print(f"  ├─ 3. Horarios de Funcionamento da Empresa no dia: {len(horarios_empresa)} registros")

        if not horarios_empresa:
            print(f"  └─ ❌ [BLOQUEIO MOTOR] A empresa nao tem horario de funcionamento cadastrado para o dia {dia_semana_py}!")
            return [], colaboradores_por_servico, contratos_base

        for h in horarios_empresa:
            janelas_empresa.append((
                datetime.combine(data_consulta, h.horario_abertura),
                datetime.combine(data_consulta, h.horario_fechamento)
            ))

    # 4. Escalas e Ocupações dos Colaboradores
    horarios_consolidados = set()

    for c_id in contratos_ids:
        excecao_colab = EseExcecaoCalendario.query.filter(
            EseExcecaoCalendario.empresa_id == empresa_id,
            EseExcecaoCalendario.contrato_id == c_id,
            EseExcecaoCalendario.ativo == True,
            EseExcecaoCalendario.data_inicio <= data_consulta,
            EseExcecaoCalendario.data_fim >= data_consulta
        ).first()

        if excecao_colab and not excecao_colab.trabalha and not excecao_colab.considera_horario:
            print(f"  ├─ Colaborador ID {c_id} em folga/excecao no dia.")
            continue

        escalas_candidatas = EscalaTrabalhoColaborador.query.filter(
            EscalaTrabalhoColaborador.contrato_id == c_id,
            EscalaTrabalhoColaborador.ativo == True,
            EscalaTrabalhoColaborador.data_inicio <= data_consulta,
            EscalaTrabalhoColaborador.data_fim >= data_consulta
        ).all()

        if not escalas_candidatas:
            continue

        escalas_validas = [
            e for e in escalas_candidatas
            if e.dia_semana is None or e.dia_semana == dia_semana_py or e.dia_semana == (dia_semana_py + 1)
        ]

        if not escalas_validas:
            continue

        ordem_prioridade = {'emergencial': 1, 'alternativo': 2, 'padrao': 3}
        escala_vigente = sorted(
            escalas_validas,
            key=lambda e: ordem_prioridade.get(getattr(e, 'tipo_escala', 'padrao'), 99)
        )[0]

        inicio_colab = datetime.combine(data_consulta, escala_vigente.inicio_expediente)
        fim_colab = datetime.combine(data_consulta, escala_vigente.fim_expediente)
        intervalo_inicio = datetime.combine(data_consulta, escala_vigente.inicio_intervalo) if escala_vigente.inicio_intervalo else None
        intervalo_fim = datetime.combine(data_consulta, escala_vigente.fim_intervalo) if escala_vigente.fim_intervalo else None

        # Ocupações na tabela AghAgendamentoItem
        inicio_dia = datetime.combine(data_consulta, datetime.min.time())
        fim_dia = datetime.combine(data_consulta, datetime.max.time())

        query_ocupacoes = db.session.query(AghAgendamentoItem).join(
            AghAgendamento, AghAgendamentoItem.agendamento_id == AghAgendamento.id
        ).filter(
            AghAgendamentoItem.profissional_id == c_id,
            AghAgendamentoItem.data_hora_inicio >= inicio_dia,
            AghAgendamentoItem.data_hora_inicio <= fim_dia,
            AghAgendamento.status != 'cancelado',
            or_(
                AghAgendamento.status != 'soft_lock',
                and_(AghAgendamento.status == 'soft_lock', AghAgendamento.expira_em > agora)
            )
        )

        if ignorar_agendamento_id:
            query_ocupacoes = query_ocupacoes.filter(AghAgendamento.id != ignorar_agendamento_id)

        itens_ocupados = query_ocupacoes.all()

        bloqueios = [(it.data_hora_inicio, it.data_hora_fim) for it in itens_ocupados if it.data_hora_inicio and it.data_hora_fim]

        if excecao_colab and excecao_colab.considera_horario and excecao_colab.hora_inicio_excecao:
            bloqueios.append((
                datetime.combine(data_consulta, excecao_colab.hora_inicio_excecao),
                datetime.combine(data_consulta, excecao_colab.hora_fim_excecao)
            ))

        # Geração e validação dos slots de horário
        for emp_inicio, emp_fim in janelas_empresa:
            inicio_efetivo = max(emp_inicio, inicio_colab)
            fim_efetivo = min(emp_fim, fim_colab)

            atual = inicio_efetivo
            while atual + duracao_delta <= fim_efetivo:
                slot_inicio = atual
                slot_fim = atual + duracao_delta

                colisao = False

                if intervalo_inicio and intervalo_fim:
                    if slot_inicio < intervalo_fim and slot_fim > intervalo_inicio:
                        colisao = True

                if not colisao:
                    for b_inicio, b_fim in bloqueios:
                        # Garante que ambas as variáveis estejam em formato naive para comparação
                        b_in_naive = b_inicio.replace(tzinfo=None) if hasattr(b_inicio, 'tzinfo') and b_inicio.tzinfo else b_inicio
                        b_fm_naive = b_fim.replace(tzinfo=None) if hasattr(b_fim, 'tzinfo') and b_fim.tzinfo else b_fim

                        if slot_inicio < b_fm_naive and slot_fim > b_in_naive:
                            colisao = True
                            break

                if not colisao:
                    horarios_consolidados.add(slot_inicio.strftime('%H:%M'))

                atual += passo_slot

    return sorted(list(horarios_consolidados)), colaboradores_por_servico, contratos_base


# =============================================================================
# 🚀 CONTROLLER PRINCIPAL (AJUSTADO PARA A NOVA ESTRUTURA AGH_AGENDAMENTO)
# =============================================================================
@agenda_bp.route('/<string:slug_empresa>/agendar', methods=['GET', 'POST'])
def agendar(slug_empresa: str):
    """📌 FLUXO DE AGENDAMENTO E REAGENDAMENTO MULTI-ESPECIALISTA"""

    empresa = EseEmpresa.query.filter_by(slug=slug_empresa).first_or_404()
    cliente_id, session_token = _obter_ou_criar_sessao_cliente()

    dados_post = (request.get_json(silent=True) if request.is_json else request.form) or {}

    def _obter_int_dict(dicionario, chave):
        val = dicionario.get(chave)
        if val is not None and str(val).isdigit():
            return int(val)
        return None

    reagendar_id = (
        request.args.get('reagendar_id', type=int) or
        request.args.get('agendamento_id', type=int) or
        _obter_int_dict(dados_post, 'reagendar_id') or
        _obter_int_dict(dados_post, 'agendamento_id')
    )

    agendamento_original = None
    horas_antecedencia = None
    data_expiracao_calculada = None

    if reagendar_id:
        agendamento_original = AghAgendamento.query.filter_by(id=reagendar_id, empresa_id=empresa.id).first()
        if agendamento_original:
            horas_antecedencia, data_expiracao_calculada = _avaliar_prazo_reagendamento(agendamento_original)

    # =========================================================================
    # 🚀 1. EFETIVAÇÃO FINAL DO AGENDAMENTO / REAGENDAMENTO (POST)
    # =========================================================================
    if request.method == 'POST':

        rascunho = _obter_rascunho_ativo(empresa.id, cliente_id, session_token)

        beneficiario_id = _obter_int_dict(dados_post, 'beneficiario_id') or (
            getattr(rascunho, 'beneficiario_id', None) if rascunho else None
        )

        str_data = dados_post.get('data_agendamento') or dados_post.get('data') or (
            rascunho.data_agendamento if rascunho else None)
        str_horario = dados_post.get('horario_agendamento') or dados_post.get('horario') or (
            rascunho.horario_agendamento if rascunho else None)

        if not str_data or not str_horario:
            return _resposta_erro('Por favor, selecione uma data e horário válidos.', 400, empresa.slug, reagendar_id, 'warning')

        try:
            if '/' in str_data:
                inicio_naive = datetime.strptime(f"{str_data} {str_horario}", "%d/%m/%Y %H:%M")
            else:
                inicio_naive = datetime.strptime(f"{str_data} {str_horario}", "%Y-%m-%d %H:%M")
        except ValueError:
            try:
                inicio_naive = datetime.strptime(f"{str_data} {str_horario}:00", "%Y-%m-%d %H:%M:%S")
            except ValueError:
                return _resposta_erro('Formato de data ou horário inválido.', 400, empresa.slug, reagendar_id)

        inicio_dt = inicio_naive.replace(tzinfo=TZ_SP)
        agora_sp = datetime.now(TZ_SP)

        if agendamento_original:
            orig_dt = agendamento_original.data_hora_inicio
            orig_dt = orig_dt.replace(tzinfo=TZ_SP) if orig_dt.tzinfo is None else orig_dt
            horas_antecedencia = (orig_dt - agora_sp).total_seconds() / 3600.0

            if horas_antecedencia >= 2.0:
                expira_em_dt = inicio_dt + timedelta(days=30)
            else:
                expira_em_dt = agora_sp + timedelta(days=7)
        else:
            expira_em_dt = inicio_dt + timedelta(days=30)

        # ---------------------------------------------------------------------
        # Extração dos Serviços Solicitados
        # ---------------------------------------------------------------------
        servicos_post_ids = []

        def _extrair_ids(val):
            ids = []
            if isinstance(val, list):
                for item in val:
                    ids.extend(_extrair_ids(item))
            elif isinstance(val, str):
                for x in val.split(','):
                    x_clean = x.strip()
                    if x_clean.isdigit():
                        ids.append(int(x_clean))
            elif isinstance(val, int):
                ids.append(val)
            return ids

        raw_servs = (
            dados_post.get('servico_ids[]') or
            dados_post.get('servico_ids') or
            dados_post.get('servico_id') or
            dados_post.get('servicos')
        )
        if raw_servs:
            servicos_post_ids = _extrair_ids(raw_servs)

        rascunho_ids = [item.servico_id for item in rascunho.itens] if (
            rascunho and getattr(rascunho, 'itens', None)) else []

        orig_ids = []
        if agendamento_original:
            itens_orig = getattr(agendamento_original, 'itens', None)
            if itens_orig:
                lista_orig = itens_orig.all() if hasattr(itens_orig, 'all') else itens_orig
                orig_ids = [
                    getattr(item, 'servico_id', None)
                    for item in lista_orig if getattr(item, 'servico_id', None)
                ]

        ids_finais_servicos = list(dict.fromkeys(servicos_post_ids + rascunho_ids + orig_ids))

        objs_servicos = []
        if ids_finais_servicos:
            objs_servicos = EseServicoOferecido.query.filter(
                EseServicoOferecido.empresa_id == empresa.id,
                EseServicoOferecido.id.in_(ids_finais_servicos)
            ).all()

        if not objs_servicos:
            return _resposta_erro('Nenhum serviço válido foi selecionado.', 400, empresa.slug, reagendar_id)

        # ---------------------------------------------------------------------
        # 🎯 ALOCAÇÃO INTELIGENTE DE PROFISSIONAIS POR ESPECIALIDADE E HORÁRIO
        # ---------------------------------------------------------------------
        ignorar_id = agendamento_original.id if agendamento_original else None
        itens_planejados, duracao_total, erro_alocacao = _atribuir_profissionais_e_horarios(
            empresa_id=empresa.id,
            objs_servicos=objs_servicos,
            data_inicio_base=inicio_dt,
            ignorar_agendamento_id=ignorar_id
        )

        if erro_alocacao:
            return _resposta_erro(erro_alocacao, 409, empresa.slug, reagendar_id, 'warning')

        fim_dt = inicio_dt + timedelta(minutes=duracao_total)

        # ---------------------------------------------------------------------
        # Cálculo Financeiro
        # ---------------------------------------------------------------------
        precos_db = EseServicoPreco.query.filter(
            EseServicoPreco.empresa_id == empresa.id,
            EseServicoPreco.novo_valor > 0
        ).all()
        mapa_precos_post = {p.taxonomia_id: float(p.novo_valor) for p in precos_db}

        valor_total_calculado = 0.0
        for item in itens_planejados:
            tax_id = getattr(item['servico_obj'], 'taxonomia_id', None)
            preco_item = mapa_precos_post.get(tax_id, 0.0) if tax_id else 0.0
            if preco_item == 0.0:
                preco_item = float(
                    getattr(item['servico_obj'], 'preco', 0.0) or getattr(item['servico_obj'], 'valor', 0.0) or 0.0
                )

            item['preco_unitario'] = preco_item
            valor_total_calculado += preco_item

        valor_final_agendamento = valor_total_calculado if valor_total_calculado > 0 else (
            float(rascunho.valor_total) if (rascunho and getattr(rascunho, 'valor_total', None)) else (
                float(getattr(agendamento_original, 'valor_total', 0.0) or 0.0) if agendamento_original else 0.0
            )
        )

        # ---------------------------------------------------------------------
        # Persistência no Banco de Dados
        # ---------------------------------------------------------------------
        try:
            # Profissional titular/referência do pai (primeiro serviço alocado)
            profissional_titular_id = itens_planejados[0]['colaborador_id'] if itens_planejados else None

            if agendamento_original:
                agendamento_original.status = 'reagendado'
                agendamento_original.data_solicitacao_reagendamento = agora_sp
                raiz_id = agendamento_original.agendamento_origem_id or agendamento_original.id
                anterior_id = agendamento_original.id

                agendamento = AghAgendamento(
                    empresa_id=empresa.id,
                    profissional_id=profissional_titular_id, # Referência opcional do 1º serviço
                    cliente_id=cliente_id or agendamento_original.cliente_id,
                    beneficiario_id=beneficiario_id or agendamento_original.beneficiario_id,
                    valor_total=valor_final_agendamento,
                    data_hora_inicio=inicio_dt,
                    data_hora_fim=fim_dt,
                    status='confirmado',
                    tipo_origem='online',
                    session_token=session_token,
                    agendamento_anterior_id=anterior_id,
                    agendamento_origem_id=raiz_id,
                    expira_em=expira_em_dt,
                    data_solicitacao_reagendamento=agora_sp,
                    criado_em=agora_sp
                )
                db.session.add(agendamento)
                db.session.flush()

                # Ajuste de solicitações pendentes
                solic_pendente = AghSolicitacaoReagendamento.query.filter_by(
                    agendamento_id=agendamento_original.id, status_solicitacao='pendente'
                ).first()
                if solic_pendente:
                    solic_pendente.status_solicitacao = 'atendida'
                    solic_pendente.analisado_em = agora_sp

                db.session.add(AghSolicitacaoReagendamento(
                    agendamento_id=agendamento_original.id,
                    novo_agendamento_id=agendamento.id,
                    nova_data=inicio_dt.date(),
                    novo_horario=inicio_dt.time(),
                    novo_profissional_id=agendamento.profissional_id,
                    justificativa="Reagendamento efetuado via plataforma.",
                    origem='cliente',
                    fora_do_prazo=(horas_antecedencia < 2.0) if horas_antecedencia is not None else False,
                    status_solicitacao='aprovada',
                    analisado_em=agora_sp,
                    created_at=agora_sp
                ))
            else:
                agendamento = AghAgendamento(
                    empresa_id=empresa.id,
                    profissional_id=profissional_titular_id, # Referência opcional do 1º serviço
                    cliente_id=cliente_id,
                    beneficiario_id=beneficiario_id,
                    valor_total=valor_final_agendamento,
                    data_hora_inicio=inicio_dt,
                    data_hora_fim=fim_dt,
                    status='confirmado',
                    tipo_origem='online',
                    session_token=session_token,
                    expira_em=expira_em_dt,
                    criado_em=agora_sp
                )
                db.session.add(agendamento)
                db.session.flush()

            # Grava cada item com seu respectivo especialista responsável e horários específicos
            profissionais_notificar = set()

            for item in itens_planejados:
                novo_item = AghAgendamentoItem(
                    agendamento_id=agendamento.id,
                    servico_id=item['servico_id'],
                    profissional_id=item['colaborador_id'], # 👈 Profissional mandatório do item
                    preco_unitario=item['preco_unitario'],
                    duracao_minutos=item['duracao_minutos'],
                    ordem_execucao=item['ordem_execucao'],
                    data_hora_inicio=item['hora_inicio'],
                    data_hora_fim=item['hora_fim'],
                    status_item='pendente'
                )
                db.session.add(novo_item)
                profissionais_notificar.add(item['colaborador_id'])

            db.session.commit()

        except Exception as e:
            db.session.rollback()
            return _resposta_erro(f'Ocorreu um erro ao processar o agendamento: {str(e)}', 500, empresa.slug, reagendar_id)

        # ---------------------------------------------------------------------
        # 📬 DISPARO DE NOTIFICAÇÕES (CLIENTE + COLABORADORES DA EQUIPE)
        # ---------------------------------------------------------------------
        def _enviar_notificacao_segura(**kwargs):
            try:
                if hasattr(AghNotificacao, 'criar_notificacao'):
                    AghNotificacao.criar_notificacao(**kwargs)
                elif hasattr(AghNotificacao, 'notificar'):
                    AghNotificacao.notificar(**kwargs)
                else:
                    db.session.add(AghNotificacao(**kwargs))
                    db.session.commit()
            except Exception as e_notif:
                print(f"⚠️ [AVISO] Falha ao disparar notificação: {str(e_notif)}")

        # Notificação Cliente
        target_cliente_id = cliente_id or (agendamento_original.cliente_id if agendamento_original else None)
        if target_cliente_id:
            usuario_dest_id = str(target_cliente_id).strip()
            data_fmt = agendamento.data_hora_inicio.strftime('%d/%m/%Y')
            hora_fmt = agendamento.data_hora_inicio.strftime('%H:%M')
            nome_empresa = getattr(empresa, 'nome_fantasia', None) or getattr(empresa, 'razao_social', 'Empresa')

            _enviar_notificacao_segura(
                empresa_id=empresa.id,
                destinatario_id=usuario_dest_id,
                papel_destinatario='cliente',
                agendamento_id=agendamento.id,
                beneficiario_id=beneficiario_id,
                titulo="Agendamento Confirmado!",
                mensagem=f"Seu atendimento na empresa {nome_empresa} foi agendado para {data_fmt} às {hora_fmt}.",
                tipo_evento="agendamento_confirmado",
                nivel="info"
            )

        # Notificações Técnicas para a Equipe de Colaboradores alocados nos itens
        for prof_id in profissionais_notificar:
            _enviar_notificacao_segura(
                empresa_id=empresa.id,
                destinatario_id=str(prof_id),
                papel_destinatario='colaborador',
                agendamento_id=agendamento.id,
                titulo="Novo Agendamento Multi-Especialista",
                mensagem=(
                    f"Você foi alocado no agendamento #{agendamento.id}. "
                    f"Acesse o painel operacional para validar a sequência técnica."
                ),
                tipo_evento="ajuste_sequencia_pendente",
                nivel="warning"
            )

        msg_sucesso = 'Agendamento realizado com sucesso!'
        if request.is_json:
            return jsonify(
                {'status': 'sucesso', 'mensagem': msg_sucesso, 'redirect_url': url_for('agenda.meus_agendamentos')}
            )

        flash(msg_sucesso, 'success')
        return redirect(url_for('agenda.dashboard_cliente'))

    # =========================================================================
    # 📖 2. CARREGAMENTO E EXIBIÇÃO DA TELA (GET)
    # =========================================================================

    rascunho = _obter_rascunho_ativo(empresa.id, cliente_id, session_token)

    servicos_oferecidos = EseServicoOferecido.query.options(
        joinedload(EseServicoOferecido.servico_taxonomia)
    ).filter_by(empresa_id=empresa.id).all()

    precos = EseServicoPreco.query.filter(
        EseServicoPreco.empresa_id == empresa.id,
        EseServicoPreco.novo_valor > 0
    ).all()
    mapa_precos = {p.taxonomia_id: float(p.novo_valor) for p in precos}

    duracoes_validas = []
    for s in servicos_oferecidos:
        s.preco_vigente = mapa_precos.get(s.taxonomia_id, 0.0)
        s.valor = s.preco_vigente
        s.descricao_servico = s.servico_taxonomia.nome if getattr(s, 'servico_taxonomia', None) else 'Serviço'

        dur_raw = getattr(s, 'duracao_minutos', None) or getattr(s, 'tempo_duracao', None)
        dur_srv = converter_duracao_para_minutos(dur_raw) or 0
        s.duracao_minutos_int = dur_srv

        if s.preco_vigente > 0 and dur_srv > 0:
            duracoes_validas.append(dur_srv)

    tempo_minimo_empresa = max(min(duracoes_validas), 30) if duracoes_validas else 30

    servicos_url_ids = []
    for chave in ['servico_id', 'servico_ids', 'servicos']:
        valores = request.args.getlist(f'{chave}[]') or request.args.getlist(chave)
        for val in valores:
            if isinstance(val, str) and ',' in val:
                servicos_url_ids.extend([int(i.strip()) for i in val.split(',') if i.strip().isdigit()])
            elif str(val).isdigit():
                servicos_url_ids.append(int(val))

    servicos_rascunho_ids = [item.servico_id for item in rascunho.itens] if (
            rascunho and hasattr(rascunho, 'itens') and rascunho.itens) else []

    servicos_original_ids = []
    if agendamento_original:
        itens_rel = getattr(agendamento_original, 'itens', None)
        if itens_rel:
            lista_itens = itens_rel.all() if hasattr(itens_rel, 'all') else itens_rel
            for item in lista_itens:
                s_id = getattr(item, 'servico_id', None)
                if s_id:
                    servicos_original_ids.append(int(s_id))

        if not servicos_original_ids:
            try:
                itens_db = AghAgendamentoItem.query.filter_by(agendamento_id=agendamento_original.id).all()
                for item in itens_db:
                    s_id = getattr(item, 'servico_id', None)
                    if s_id:
                        servicos_original_ids.append(int(s_id))
            except Exception as err:
                print(f"⚠️ Erro consulta fallback AghAgendamentoItem: {err}")

    todos_servicos_ids = list(dict.fromkeys(servicos_rascunho_ids + servicos_url_ids + servicos_original_ids))

    if not todos_servicos_ids and servicos_oferecidos:
        fallback_id = request.args.get('servico_id', type=int) or servicos_oferecidos[0].id
        todos_servicos_ids = [fallback_id]

    servicos_selecionados = [s for s in servicos_oferecidos if s.id in todos_servicos_ids]
    tempo_total_minutos = sum(
        getattr(s, 'duracao_minutos_int', 0) or getattr(s, 'duracao_minutos', 0) for s in servicos_selecionados)

    soma_selecionados = sum(
        float(getattr(s, 'preco_vigente', 0) or getattr(s, 'preco', 0) or 0) for s in servicos_selecionados)

    if soma_selecionados > 0:
        valor_total_cobrado = soma_selecionados
    elif agendamento_original and getattr(agendamento_original, 'valor_total', None):
        valor_total_cobrado = float(agendamento_original.valor_total)
    else:
        valor_total_cobrado = 0.0

    profissional_id_selecionado = request.args.get('profissional_id', type=int) or (
        rascunho.colaborador_id if rascunho else (
            agendamento_original.profissional_id if agendamento_original else None
        )
    )

    colaboradores = ColaboradorContrato.query.options(
        joinedload(ColaboradorContrato.cargo),
        joinedload(ColaboradorContrato.cadastro_modulo)
    ).filter(
        ColaboradorContrato.id_local == empresa.id,
        ColaboradorContrato.status_profissional == 'ativo',
        ColaboradorContrato.data_desligamento.is_(None)
    ).all()

    def _obter_nome_cargo(c):
        cargo_obj = getattr(c, 'cargo', None)
        if cargo_obj and not isinstance(cargo_obj, str):
            return (
                getattr(cargo_obj, 'descricao', None) or
                getattr(cargo_obj, 'nome', None) or
                getattr(cargo_obj, 'titulo', None) or
                getattr(cargo_obj, 'nome_cargo', None) or
                'Especialista'
            )
        if isinstance(cargo_obj, str) and cargo_obj:
            return cargo_obj
        return 'Especialista'

    colaboradores_payload = [
        {
            'id': c.id,
            'nome': c.nome,
            'cargo': _obter_nome_cargo(c),
            'foto_url': getattr(c, 'url_foto_profissional', None),
            'escala': getattr(c, 'escala_vigente', None)
        }
        for c in colaboradores
    ]

    categorias_ids = list({s.taxonomia_id for s in servicos_oferecidos if getattr(s, 'taxonomia_id', None)})
    categorias = Taxonomia.query.filter(Taxonomia.id.in_(categorias_ids)).all() if categorias_ids else []

    beneficiarios = []
    if current_user.is_authenticated:
        beneficiarios = ClienteBeneficiario.query.filter_by(
            cliente_id=str(current_user.id),
            ativo=True
        ).order_by(ClienteBeneficiario.nome.asc()).all()

    categoria_slug = getattr(getattr(empresa, 'categoria_rel', None), 'slug', '')
    requer_triagem_beneficiario = categoria_slug in {'saude', 'odontologia', 'veterinaria', 'estetica', 'oficina', 'lava-jato'}

    atendimentos_nao_realizados = []
    if cliente_id:
        atendimentos_nao_realizados = AghAgendamento.query.filter(
            AghAgendamento.cliente_id == cliente_id,
            AghAgendamento.status.in_(['CANCELADO', 'NAO_COMPARECEU', 'PENDENTE'])
        ).all()

    data_selecionada = request.args.get('data') or (
        rascunho.data_agendamento if rascunho else (
            agendamento_original.data_hora_inicio.strftime('%Y-%m-%d') if agendamento_original else datetime.now(TZ_SP).strftime('%Y-%m-%d')
        )
    )

    horario_selecionado = request.args.get('horario') or (
        rascunho.horario_agendamento if rascunho else (
            agendamento_original.data_hora_inicio.strftime('%H:%M') if agendamento_original else None
        )
    )

    servico_id_principal = request.args.get('servico_id', type=int) or (
        todos_servicos_ids[0] if todos_servicos_ids else None
    )

    return render_template(
        'agenda/agendamento_passo_unico.html',
        empresa=empresa,
        categorias=categorias,
        servicos=servicos_oferecidos,
        servicos_selecionados=servicos_selecionados,
        servicos_ids_string=','.join(map(str, todos_servicos_ids)),
        servicos_rascunho_ids=todos_servicos_ids,
        valor_total_cobrado=valor_total_cobrado,
        tempo_total=tempo_total_minutos,
        duracao_total_formatada=_formatar_duracao(tempo_total_minutos),
        colaboradores=colaboradores,
        profissionais=colaboradores,
        colaboradores_payload=colaboradores_payload,
        profissional_id_selecionado=profissional_id_selecionado,
        data_atual=datetime.now(TZ_SP).strftime('%Y-%m-%d'),
        data_selecionada=data_selecionada,
        horario_selecionado=horario_selecionado,
        tempo_minimo_empresa=tempo_minimo_empresa,
        beneficiarios=beneficiarios,
        requer_triagem_beneficiario=requer_triagem_beneficiario,
        servico_id_selecionado=servico_id_principal,
        rascunho=rascunho,
        data_limite_maxima=data_expiracao_calculada.strftime('%Y-%m-%d') if data_expiracao_calculada else None,
        modo_edicao=agendamento_original is not None,
        atendimentos_nao_realizados=atendimentos_nao_realizados,
        agendamento_original=agendamento_original
    )


def obter_profissionais_empresa(empresa_id):
    """
    Retorna todos os colaboradores ativos da empresa (incluindo o proprietário)
    que possuem pelo menos uma escala de trabalho ativa configurada.
    """
    tem_escala_ativa = exists().where(
        EscalaTrabalhoColaborador.contrato_id == ColaboradorContrato.id,
        EscalaTrabalhoColaborador.ativo == True
    )

    return (
        ColaboradorContrato.query
        .filter(
            ColaboradorContrato.empresa_id == empresa_id,
            ColaboradorContrato.status_profissional == 'ativo',
            ColaboradorContrato.data_desligamento.is_(None),
            tem_escala_ativa
        )
        .all()
    )


def buscar_colaboradores_elegiveis(id_local: int, id_contrato: int = None) -> list[ColaboradorContrato]:
    """
    Recupera colaboradores com contrato ativo E escala de trabalho ativa para um determinado estabelecimento.
    """
    if not id_local:
        return []

    # Subquery: garante que só entram profissionais com escala de trabalho ativa
    tem_escala_ativa = exists().where(
        EscalaTrabalhoColaborador.contrato_id == ColaboradorContrato.id,
        EscalaTrabalhoColaborador.ativo == True
    )

    query = ColaboradorContrato.query.options(
        joinedload(ColaboradorContrato.cadastro_modulo),  # Traz os dados do perfil (nome, foto)
        joinedload(ColaboradorContrato.cargo)             # Traz o cargo cadastrado
    ).filter(
        ColaboradorContrato.id_local == id_local,
        ColaboradorContrato.status_profissional == 'ativo',
        ColaboradorContrato.data_desligamento.is_(None),
        tem_escala_ativa
    )

    if id_contrato:
        query = query.filter(ColaboradorContrato.id == id_contrato)

    return query.all()


MESES_PTBR = {
    1: 'Janeiro', 2: 'Fevereiro', 3: 'Março', 4: 'Abril',
    5: 'Maio', 6: 'Junho', 7: 'Julho', 8: 'Agosto',
    9: 'Setembro', 10: 'Outubro', 11: 'Novembro', 12: 'Dezembro'
}


@agenda_bp.route('/api/agendamentos-grid', methods=['GET'], strict_slashes=False)
def api_agendamentos_grid():
    """
    MOTOR DE CONSULTA: MATRIZ DE AGENDAMENTOS E GRADE OPERACIONAL DO BALCÃO
    """
    from feedin.utils import _parse_profissional_id

    empresa_id = request.args.get("empresa_id", type=int)
    data_inicio_str = request.args.get("data_inicio")  # YYYY-MM-DD
    data_fim_str = request.args.get("data_fim")  # YYYY-MM-DD

    # Captura como string bruta para tratar 'todos', 'null', 'undefined'
    profissional_id_raw = request.args.get('profissional_id')

    if not empresa_id or not data_inicio_str or not data_fim_str:
        return jsonify({"erro": "Parâmetros 'empresa_id', 'data_inicio' e 'data_fim' são obrigatórios"}), 400

    try:
        dt_inicio = datetime.strptime(data_inicio_str, '%Y-%m-%d')
        # Limite do dia final até 23:59:59.999999
        dt_fim = datetime.combine(datetime.strptime(data_fim_str, '%Y-%m-%d').date(), time.max)
    except ValueError:
        return jsonify({'erro': 'Formato de data inválido. Use YYYY-MM-DD.'}), 400

    # Sanitização robusta do profissional_id
    prof_id = None
    if profissional_id_raw and str(profissional_id_raw).strip().lower() not in ['todos', 'null', 'undefined', '0', '']:
        try:
            parsed = _parse_profissional_id(profissional_id_raw)
            if parsed and int(parsed) > 0:
                prof_id = int(parsed)
        except (ValueError, TypeError):
            prof_id = None

    # 1. Query Principal com Eager Loading
    # Nota: Não filtramos status != 'cancelado' para permitir a renderização dos badges de cancelamento e métricas precisas no Balcão
    query = AghAgendamento.query.options(
        joinedload(AghAgendamento.profissional).joinedload(ColaboradorContrato.cadastro_modulo),
        joinedload(AghAgendamento.cliente),
        selectinload(AghAgendamento.itens).joinedload(AghAgendamentoItem.servico)
    ).filter(
        AghAgendamento.empresa_id == empresa_id,
        AghAgendamento.data_hora_inicio >= dt_inicio,
        AghAgendamento.data_hora_inicio <= dt_fim
    )

    if prof_id is not None:
        query = query.filter(AghAgendamento.profissional_id == prof_id)

    agendamentos = query.order_by(AghAgendamento.data_hora_inicio.asc()).all()

    # 2. Mapeamento eficiente de Contatos
    cliente_ids = list({ag.cliente_id for ag in agendamentos if ag.cliente_id})
    contatos_map = {}
    if cliente_ids:
        contatos_db = ClienteContato.query.filter(
            ClienteContato.cliente_id.in_(cliente_ids)
        ).all()

        for c in contatos_db:
            cid = c.cliente_id
            if cid not in contatos_map or c.is_padrao or c.tipo == 'whatsapp':
                contatos_map[cid] = c.valor

    # 3. Mapeamento de Beneficiários
    beneficiario_ids = list({
        getattr(ag, 'beneficiario_id', None) or getattr(ag, 'cliente_beneficiario_id', None)
        for ag in agendamentos
        if getattr(ag, 'beneficiario_id', None) or getattr(ag, 'cliente_beneficiario_id', None)
    })

    beneficiarios_map = {}
    if beneficiario_ids:
        beneficiarios_db = ClienteBeneficiario.query.filter(
            ClienteBeneficiario.id.in_(beneficiario_ids)
        ).all()
        beneficiarios_map = {b.id: b for b in beneficiarios_db}

    # 4. Rótulo do mês em PT-BR para sincronização
    nome_mes = MESES_PTBR.get(dt_inicio.month, '')
    mes_extenso = f"{nome_mes} de {dt_inicio.year}" if nome_mes else dt_inicio.strftime('%B/%Y')

    resultado = []
    for ag in agendamentos:
        servicos_nomes = [item.servico.nome for item in ag.itens if item.servico] if ag.itens else []

        # Formatação do telefone do cliente
        cliente_telefone = contatos_map.get(ag.cliente_id, "") if ag.cliente_id else ""
        if not cliente_telefone and ag.cliente:
            cliente_telefone = getattr(ag.cliente, 'whatsapp', '') or getattr(ag.cliente, 'telefone', '') or ""

        # Mapeamento do beneficiário
        ben_id = getattr(ag, 'beneficiario_id', None) or getattr(ag, 'cliente_beneficiario_id', None)
        ben_obj = beneficiarios_map.get(ben_id) if ben_id else None

        dt_fim_ag = ag.data_hora_fim or ag.data_hora_inicio

        # Resolução Desacoplada do Nome do Profissional (Priorizando contrato / módulo de cadastro local)
        prof_nome = "Equipe / Não atribuído"
        if ag.profissional:
            if hasattr(ag.profissional, 'nome_exibicao') and ag.profissional.nome_exibicao:
                prof_nome = ag.profissional.nome_exibicao
            elif ag.profissional.cadastro_modulo and getattr(ag.profissional.cadastro_modulo, 'nome', None):
                prof_nome = ag.profissional.cadastro_modulo.nome
            elif getattr(ag.profissional, 'nome', None):
                prof_nome = ag.profissional.nome
            elif getattr(ag.profissional, 'usuario', None) and getattr(ag.profissional.usuario, 'nome', None):
                prof_nome = ag.profissional.usuario.nome

        # Resolução do Nome do Cliente
        cliente_nome = "Cliente Balcão"
        if ag.cliente and getattr(ag.cliente, 'nome', None):
            cliente_nome = ag.cliente.nome

        resultado.append({
            "id": ag.id,
            "empresa_id": ag.empresa_id,
            "profissional_id": ag.profissional_id,
            "profissional_nome": prof_nome,
            "cliente_id": ag.cliente_id,
            "cliente_nome": cliente_nome,
            "cliente_telefone": cliente_telefone,

            "beneficiario_id": ben_id,
            "beneficiario_nome": ben_obj.nome if ben_obj else None,
            "beneficiario_tipo": getattr(ben_obj, 'tipo_persona', None) if ben_obj else None,

            # Propriedades de data/hora consumidas no Front-end
            "data_dia": ag.data_hora_inicio.strftime('%Y-%m-%d'),
            "data_inicio": ag.data_hora_inicio.strftime('%Y-%m-%d %H:%M'),
            "data_fim": dt_fim_ag.strftime('%Y-%m-%d %H:%M'),
            "horario_inicio": ag.data_hora_inicio.strftime('%H:%M'),
            "horario_fim": dt_fim_ag.strftime('%H:%M'),
            "mes_rotulo": mes_extenso,

            "valor_total": float(ag.valor_total or 0.0),
            "status": ag.status,
            "tipo_origem": getattr(ag, 'tipo_origem', 'online'),
            "servicos": servicos_nomes
        })

    return jsonify(resultado)


@agenda_bp.route('/api/metricas-dia', methods=['GET'], strict_slashes=False)
def api_metricas_dia():
    """
    🌐 ENDPOINT: MÉTRICAS DIÁRIAS DO BALCÃO
    --------------------------------------------------------------------------------------
    Recebe os filtros via Query Params (empresa_id, data, profissional_id), trata
    a sanitização das entradas e orquestra a resposta JSON para o Frontend.
    """

    from feedin.utils import _parse_profissional_id

    empresa_id = request.args.get("empresa_id", type=int)
    data_str = request.args.get("data") # Formato esperado: 'YYYY-MM-DD'
    profissional_id = _parse_profissional_id(request.args.get("profissional_id"))

    if not empresa_id or not data_str:
        return jsonify({"erro": "Parâmetros 'empresa_id' e 'data' são obrigatórios"}), 400

    try:
        data_consulta = datetime.strptime(data_str, '%Y-%m-%d')
    except ValueError:
        return jsonify({'erro': 'Formato de data inválido. Use YYYY-MM-DD.'}), 400

    # Chama a função pura e isolada do queries.py
    metricas = buscar_metricas_diarias(
        empresa_id=empresa_id,
        data_consulta=data_consulta,
        profissional_id=profissional_id
    )

    return jsonify(metricas), 200


@agenda_bp.route('/api/agendamentos-dia', methods=['GET'], strict_slashes=False)
@login_required
def api_agendamentos_dia():
    """
    🌐 ENDPOINT: GRADE DE AGENDAMENTOS DIÁRIOS
    --------------------------------------------------------------------------------------
    Retorna a lista estruturada de atendimentos para popular os cards da agenda.
    """

    from feedin.utils import _parse_profissional_id

    empresa_id = request.args.get('empresa_id', type=int)
    data_str = request.args.get('data')

    # Usa a mesma higienização da rota de métricas
    raw_prof = request.args.get('profissional_id')
    profissional_id = _parse_profissional_id(raw_prof) if '_parse_profissional_id' in globals() else (
        int(raw_prof) if raw_prof and raw_prof.isdigit() else None)

    print(
        f"\n🔍 [DEBUG AGENDA] Params recebidos -> empresa_id: {empresa_id}, data: {data_str}, prof_id: {profissional_id}")

    if not empresa_id or not data_str:
        return jsonify({"erro": "Parâmetros 'empresa_id' e 'data' são obrigatórios"}), 400

    try:
        agendamentos = buscar_agendamentos_dia(
            empresa_id=empresa_id,
            data_str=data_str,
            profissional_id=profissional_id
        )
        print(f"✅ [DEBUG AGENDA] Sucesso! Encontrados {len(agendamentos)} registros.")
        return jsonify(agendamentos), 200

    except Exception as e:
        print(f"❌ [DEBUG AGENDA] ERRO CRÍTICO NA CONSULTA: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({'erro': str(e)}), 500


def verificar_empresa_funciona_no_dia(empresa_id: int, data_alvo: date) -> bool:
    dia_semana_py = data_alvo.weekday()

    # 1. Verifica exceção de calendário cadastrada na empresa
    excecao = EseExcecaoCalendario.query.filter(
        EseExcecaoCalendario.empresa_id == empresa_id,
        EseExcecaoCalendario.contrato_id.is_(None),
        EseExcecaoCalendario.ativo == True,
        EseExcecaoCalendario.data_inicio <= data_alvo,
        EseExcecaoCalendario.data_fim >= data_alvo
    ).first()

    if excecao:
        return bool(excecao.trabalha)

    # 2. Verifica horário de funcionamento regular da empresa
    horario = EseHorarioFuncionamento.query.filter_by(
        empresa_id=empresa_id,
        dia_semana=dia_semana_py
    ).first()

    return horario is not None


def verificar_colaborador_trabalha_no_dia(colaborador_id: int, data_alvo: date) -> bool:
    # Verifica se há exceção de folga/afastamento cadastrada para o ColaboradorContrato.id
    excecao = EseExcecaoCalendario.query.filter(
        EseExcecaoCalendario.contrato_id == colaborador_id,
        EseExcecaoCalendario.ativo == True,
        EseExcecaoCalendario.data_inicio <= data_alvo,
        EseExcecaoCalendario.data_fim >= data_alvo
    ).first()

    if excecao and not excecao.trabalha:
        return False

    return True


def obter_horario_fechamento_empresa(empresa_id: int, data_alvo: date):
    """
    Retorna o horário de fechamento da empresa para o dia informado (time object ou None).
    """
    dia_semana_py = data_alvo.weekday()

    # 1. Exceção com horário específico
    excecao = EseExcecaoCalendario.query.filter(
        EseExcecaoCalendario.empresa_id == empresa_id,
        EseExcecaoCalendario.contrato_id.is_(None),
        EseExcecaoCalendario.ativo == True,
        EseExcecaoCalendario.data_inicio <= data_alvo,
        EseExcecaoCalendario.data_fim >= data_alvo
    ).first()

    if excecao:
        if not excecao.trabalha:
            return None
        if excecao.fim_expediente:
            return excecao.fim_expediente

    # 2. Horário normal de funcionamento
    ultimo_horario = EseHorarioFuncionamento.query.filter_by(
        empresa_id=empresa_id,
        dia_semana=dia_semana_py
    ).order_by(EseHorarioFuncionamento.horario_fechamento.desc()).first()

    return ultimo_horario.horario_fechamento if ultimo_horario else None


# --------------------------------------------------------------------------
# ⚙️ FUNÇÕES AUXILIARES E MOTOR UNIFICADO
# --------------------------------------------------------------------------
def obter_proximo_dia_disponivel(empresa_id: int, colaborador_id: int = None, data_referencia: date = None):
    """
    Retorna a data válida mais próxima considerando expediente, escalas e exceções do calendário.
    """
    agora = datetime.now()
    data_alvo = data_referencia or agora.date()
    max_tentativas = 30
    tentativas = 0

    while tentativas < max_tentativas:
        slots, _, colabs = obter_colaboradores_e_horarios_disponiveis(
            empresa_id=empresa_id,
            data_consulta=data_alvo,
            profissional_id=colaborador_id
        )

        # Se for hoje, valida se ainda restam slots futuros
        if data_alvo == agora.date():
            hora_atual_str = agora.strftime('%H:%M')
            slots = [s for s in slots if s > hora_atual_str]

        if slots and colabs:
            return data_alvo

        data_alvo += timedelta(days=1)
        tentativas += 1

    return data_alvo


@agenda_bp.route('/api/colaboradores-disponiveis', methods=['GET'], strict_slashes=False)
@login_required
def api_colaboradores_disponiveis():
    empresa_id = request.args.get("empresa_id", type=int)
    data_hora_str = request.args.get("data_hora")  # 'YYYY-MM-DD HH:MM'
    servicos_raw = request.args.get('servicos', '')

    if not empresa_id or not data_hora_str:
        return jsonify({"erro": "Parâmetros 'empresa_id' e 'data_hora' são obrigatórios"}), 400

    try:
        data_hora_inicio = datetime.strptime(data_hora_str, '%Y-%m-%d %H:%M')
    except ValueError:
        return jsonify({'erro': 'Formato de data_hora inválido. Use YYYY-MM-DD HH:MM'}), 400

    hora_desejada = data_hora_inicio.strftime('%H:%M')
    servico_ids = [int(s) for s in servicos_raw.split(',') if s.isdigit()]

    # Chama o motor com a estrutura exata do seu projeto
    colabs_aptos, mapa_horarios = obter_colaboradores_e_horarios_disponiveis(
        empresa_id=empresa_id,
        data_consulta=data_hora_inicio.date(),
        servico_ids=servico_ids
    )

    colabs_livres = [
        colab for colab in colabs_aptos
        if hora_desejada in mapa_horarios.get(colab.id, [])
    ]

    resposta = [
        {
            "id": c.id,
            "nome": c.nome  # Usa a property @property def nome do ColaboradorContrato
        }
        for c in colabs_livres
    ]

    return jsonify(resposta), 200


def obter_colaboradores_e_horarios_disponiveis(empresa_id: int, data_consulta: date, servico_ids: list = None,
                                               profissional_id: int = None, ignorar_agendamento_id: int = None):
    """
    Motor Central de Disponibilidade do FeedIn:
    Mapeia escalas, exceções e ocupações em AghAgendamentoItem.

    Retorna:
        - slots_formatados: list de strings ex: ['08:00', '08:30', ...]
        - colaboradores_por_servico: dict { servico_id: [ColaboradorContrato, ...] }
        - contratos_base: list de ColaboradorContrato aptos
    """
    agora = datetime.utcnow()
    dia_semana_py = data_consulta.weekday()

    # 1. Colaboradores Ativos
    query_contratos = ColaboradorContrato.query.filter(
        ColaboradorContrato.id_local == empresa_id,
        ColaboradorContrato.status_profissional == 'ativo',
        ColaboradorContrato.data_desligamento.is_(None)
    )

    if profissional_id:
        query_contratos = query_contratos.filter(ColaboradorContrato.id == profissional_id)

    contratos_base = query_contratos.all()
    if not contratos_base:
        return [], {}, []

    contratos_dict = {c.id: c for c in contratos_base}
    contratos_ids = list(contratos_dict.keys())

    # Mapeia colaboradores por serviço conforme habilidades registradas
    colaboradores_por_servico = {}
    if servico_ids:
        for s_id in servico_ids:
            habilidades = EseColaboradorServicoHabilidade.query.filter(
                EseColaboradorServicoHabilidade.contrato_id.in_(contratos_ids),
                EseColaboradorServicoHabilidade.servico_oferecido_id == s_id
            ).all()

            c_ids_servico = [h.contrato_id for h in habilidades]
            colaboradores_por_servico[s_id] = [contratos_dict[cid] for cid in c_ids_servico if cid in contratos_dict]
    else:
        for c in contratos_base:
            colaboradores_por_servico.setdefault(0, []).append(c)

    # 2. Duração Total dos Serviços
    duracao_total_min = 30
    if servico_ids:
        servicos = EseServicoOferecido.query.filter(
            EseServicoOferecido.id.in_(servico_ids),
            EseServicoOferecido.empresa_id == empresa_id
        ).all()
        if servicos:
            duracao_total_min = sum(
                converter_duracao_para_minutos(getattr(s, 'duracao_minutos', None) or getattr(s, 'tempo_duracao', 30))
                for s in servicos)

    duracao_delta = timedelta(minutes=duracao_total_min)
    passo_slot = timedelta(minutes=30)

    # 3. Janelas de Funcionamento da Empresa
    janelas_empresa = []
    excecao_empresa = EseExcecaoCalendario.query.filter(
        EseExcecaoCalendario.empresa_id == empresa_id,
        EseExcecaoCalendario.contrato_id.is_(None),
        EseExcecaoCalendario.ativo == True,
        EseExcecaoCalendario.data_inicio <= data_consulta,
        EseExcecaoCalendario.data_fim >= data_consulta
    ).first()

    if excecao_empresa:
        if not excecao_empresa.trabalha:
            return [], colaboradores_por_servico, contratos_base
        elif excecao_empresa.inicio_expediente and excecao_empresa.fim_expediente:
            janelas_empresa.append((
                datetime.combine(data_consulta, excecao_empresa.inicio_expediente),
                datetime.combine(data_consulta, excecao_empresa.fim_expediente)
            ))

    if not janelas_empresa:
        horarios_empresa = EseHorarioFuncionamento.query.filter(
            EseHorarioFuncionamento.empresa_id == empresa_id,
            or_(
                EseHorarioFuncionamento.dia_semana == dia_semana_py,
                EseHorarioFuncionamento.dia_semana == (dia_semana_py + 1)
            )
        ).order_by(EseHorarioFuncionamento.horario_abertura).all()

        if not horarios_empresa:
            return [], colaboradores_por_servico, contratos_base

        for h in horarios_empresa:
            janelas_empresa.append((
                datetime.combine(data_consulta, h.horario_abertura),
                datetime.combine(data_consulta, h.horario_fechamento)
            ))

    # 4. Avalia Escalas e Conflitos de Agendamentos por Colaborador
    horarios_consolidados = set()

    for c_id in contratos_ids:
        excecao_colab = EseExcecaoCalendario.query.filter(
            EseExcecaoCalendario.empresa_id == empresa_id,
            EseExcecaoCalendario.contrato_id == c_id,
            EseExcecaoCalendario.ativo == True,
            EseExcecaoCalendario.data_inicio <= data_consulta,
            EseExcecaoCalendario.data_fim >= data_consulta
        ).first()

        if excecao_colab and not excecao_colab.trabalha and not excecao_colab.considera_horario:
            continue

        escalas_candidatas = EscalaTrabalhoColaborador.query.filter(
            EscalaTrabalhoColaborador.contrato_id == c_id,
            EscalaTrabalhoColaborador.ativo == True,
            EscalaTrabalhoColaborador.data_inicio <= data_consulta,
            or_(EscalaTrabalhoColaborador.data_fim >= data_consulta, EscalaTrabalhoColaborador.data_fim.is_(None))
        ).all()

        escalas_validas = [
            e for e in escalas_candidatas
            if e.dia_semana is None or e.dia_semana == dia_semana_py or e.dia_semana == (dia_semana_py + 1)
        ]

        if not escalas_validas:
            continue

        ordem_prioridade = {'emergencial': 1, 'alternativo': 2, 'padrao': 3}
        escala_vigente = sorted(
            escalas_validas,
            key=lambda e: ordem_prioridade.get(getattr(e, 'tipo_escala', 'padrao'), 99)
        )[0]

        inicio_colab = datetime.combine(data_consulta, escala_vigente.inicio_expediente)
        fim_colab = datetime.combine(data_consulta, escala_vigente.fim_expediente)
        intervalo_inicio = datetime.combine(data_consulta,
                                            escala_vigente.inicio_intervalo) if escala_vigente.inicio_intervalo else None
        intervalo_fim = datetime.combine(data_consulta,
                                         escala_vigente.fim_intervalo) if escala_vigente.fim_intervalo else None

        # Ocupações na tabela relacional AghAgendamentoItem
        inicio_dia = datetime.combine(data_consulta, time.min)
        fim_dia = datetime.combine(data_consulta, time.max)

        query_ocupacoes = db.session.query(AghAgendamentoItem).join(
            AghAgendamento, AghAgendamentoItem.agendamento_id == AghAgendamento.id
        ).filter(
            AghAgendamentoItem.profissional_id == c_id,
            AghAgendamentoItem.data_hora_inicio >= inicio_dia,
            AghAgendamentoItem.data_hora_inicio <= fim_dia,
            AghAgendamento.status != 'cancelado',
            or_(
                AghAgendamento.status != 'soft_lock',
                and_(AghAgendamento.status == 'soft_lock', AghAgendamento.expira_em > agora)
            )
        )

        if ignorar_agendamento_id:
            query_ocupacoes = query_ocupacoes.filter(AghAgendamento.id != ignorar_agendamento_id)

        itens_ocupados = query_ocupacoes.all()
        bloqueios = [(it.data_hora_inicio, it.data_hora_fim) for it in itens_ocupados if
                     it.data_hora_inicio and it.data_hora_fim]

        if excecao_colab and excecao_colab.considera_horario and excecao_colab.hora_inicio_excecao:
            bloqueios.append((
                datetime.combine(data_consulta, excecao_colab.hora_inicio_excecao),
                datetime.combine(data_consulta, excecao_colab.hora_fim_excecao)
            ))

        # Geração dos slots dentro dos limites efetivos
        for emp_inicio, emp_fim in janelas_empresa:
            inicio_efetivo = max(emp_inicio, inicio_colab)
            fim_efetivo = min(emp_fim, fim_colab)

            atual = inicio_efetivo
            while atual + duracao_delta <= fim_efetivo:
                slot_inicio = atual
                slot_fim = atual + duracao_delta
                colisao = False

                if intervalo_inicio and intervalo_fim:
                    if slot_inicio < intervalo_fim and slot_fim > intervalo_inicio:
                        colisao = True

                if not colisao:
                    for b_inicio, b_fim in bloqueios:
                        b_in_naive = b_inicio.replace(tzinfo=None) if hasattr(b_inicio,
                                                                              'tzinfo') and b_inicio.tzinfo else b_inicio
                        b_fm_naive = b_fim.replace(tzinfo=None) if hasattr(b_fim, 'tzinfo') and b_fim.tzinfo else b_fim

                        if slot_inicio < b_fm_naive and slot_fim > b_in_naive:
                            colisao = True
                            break

                if not colisao:
                    horarios_consolidados.add(slot_inicio.strftime('%H:%M'))

                atual += passo_slot

    return sorted(list(horarios_consolidados)), colaboradores_por_servico, contratos_base


def obter_publicidade_agenda(cliente, empresa_agendada_id=None):
    """Adapta a inteligência do Core para o PWA da Agenda.

    - Respeita o filtro de concorrência (não mostra concorrente do
    estabelecimento atual). - Prioriza tags dos serviços que o cliente costuma
    consumir. - Serve parceiros locais no mesmo tom de comunidade.
    """
    with db.session.no_autoflush:
        tag_vencedora = None

        # 1. CAPTURA AFINIDADE PELO HISTÓRICO DE SERVIÇOS DO CLIENTE
        if cliente and hasattr(cliente, 'agendamentos') and cliente.agendamentos:
            try:
                tags_servicos_cliente = set()

                # Trata de forma híbrida caso seja Query ou Lista em memória (InstrumentedList)
                if hasattr(cliente.agendamentos, 'limit'):
                    agendamentos_recentes = cliente.agendamentos.limit(
                        10
                    ).all()
                else:
                    agendamentos_recentes = cliente.agendamentos[:10]

                for ag in agendamentos_recentes:
                    # Verifica se o agendamento tem itens vinculados
                    itens_ag = getattr(ag, 'itens', [])
                    for item in itens_ag:
                        servico = getattr(item, 'servico', None)
                        if servico and hasattr(servico, 'tags') and servico.tags:
                            tags_lista = (
                                servico.tags.all()
                                if hasattr(servico.tags, 'all')
                                else servico.tags
                            )
                            tags_servicos_cliente.update(
                                [t for t in tags_lista if t]
                            )

                if tags_servicos_cliente:
                    tag_vencedora = random.choice(list(tags_servicos_cliente))
            except Exception as e:
                print(
                    f'DEBUG PUBLICIDADE: Erro ao mapear tags do cliente -> {e}'
                )

        # Contingência: se o cliente não tiver histórico, pega a tag da empresa atual
        if not tag_vencedora and empresa_agendada_id:
            try:
                from feedin.models import Local

                empresa_atual = Local.query.get(empresa_agendada_id)
                if empresa_atual and hasattr(empresa_atual, 'tags'):
                    tags_empresa = (
                        empresa_atual.tags.all()
                        if hasattr(empresa_atual.tags, 'all')
                        else empresa_atual.tags
                    )
                    if tags_empresa:
                        tag_vencedora = random.choice(
                            [t for t in tags_empresa if t]
                        )
            except Exception:
                pass

        # 2. FILTRO DE CONCORRÊNCIA (Regra de Ouro do Core)
        categoria_bloqueada_id = None
        if empresa_agendada_id:
            try:
                from feedin.models import Local

                empresa_atual = Local.query.get(empresa_agendada_id)
                if empresa_atual:
                    categoria_bloqueada_id = getattr(
                        empresa_atual, 'id_categoria_principal', None
                    )
            except Exception:
                pass

        # 3. BUSCA SELETIVA NO `LocalAnuncio` (Tabela nativa do Core)
        try:
            from feedin.models import Local, LocalAnuncio

            query = LocalAnuncio.query.filter(LocalAnuncio.status == 'ativo')

            if tag_vencedora:
                query = query.filter(
                    LocalAnuncio.taxonomia_id == tag_vencedora.id
                )

            # Aplica a trava antirrivalidade
            if empresa_agendada_id and categoria_bloqueada_id:
                query = query.join(
                    Local, LocalAnuncio.local_id == Local.id
                ).filter(
                    db.or_(
                        LocalAnuncio.local_id
                        == empresa_agendada_id,  # Anúncio do próprio local parceiro
                        Local.id_categoria_principal
                        != categoria_bloqueada_id,  # Ou segmento diferente
                    )
                )

            anuncios = query.order_by(func.random()).all()

            if not anuncios:
                return None

            # Prioriza anúncios com plano patrocinado
            patrocinados = [
                a
                for a in anuncios
                if getattr(a, 'plano_marketing', None) == 'patrocinado'
            ]
            anuncio_escolhido = patrocinados[0] if patrocinados else anuncios[0]

            # Atribui o texto contextual formatado
            anuncio_escolhido.tag_referencia_nome = (
                tag_vencedora.nome if tag_vencedora else 'Destaque'
            )
            anuncio_escolhido.texto_formatado = f"Parceiro recomendado em Piracicaba no segmento de {anuncio_escolhido.tag_referencia_nome}!"
            anuncio_escolhido.is_house_ad = False

            # Telemetria de visualização atômica (Sem autoflush friction)
            try:
                db.session.query(LocalAnuncio).filter(
                    LocalAnuncio.id == anuncio_escolhido.id
                ).update(
                    {'visualizacoes': LocalAnuncio.visualizacoes + 1},
                    synchronize_session=False,
                )
            except Exception as e:
                print(f'Erro ao registrar view do banner na Agenda: {e}')

            return anuncio_escolhido

        except Exception as e:
            print(f'DEBUG PUBLICIDADE: Erro ao buscar LocalAnuncio -> {e}')
            return None


@agenda_bp.route("/reserva-temporaria", methods=["POST"], strict_slashes=False)
@login_required
def reserva_temporaria():
    from datetime import datetime, timedelta
    import pytz
    from feedin.utils import _parse_profissional_id

    data = request.get_json() or {}

    empresa_id = data.get("empresa_id")
    profissional_id_param = data.get("profissional_id") or data.get("colaborador_id")
    servico_ids = data.get("servico_ids", [])
    data_hora_str = data.get("data_hora_inicio")

    if not empresa_id or not servico_ids or not data_hora_str:
        return jsonify({"sucesso": False, "mensagem": "Parâmetros obrigatórios ausentes."}), 400

    profissional_id = _parse_profissional_id(profissional_id_param) if profissional_id_param else None

    # 1. Identificação do Cliente autenticado
    cliente = getattr(current_user, "cliente", None)
    if not cliente:
        cliente = ModCadastroCliente.query.filter_by(usuario_id=current_user.id).first()

    if not cliente:
        return jsonify({"sucesso": False, "mensagem": "Perfil de cliente não localizado para o usuário logado."}), 403

    # 2. Parse da Data/Hora e Fuso Horário Local
    tz_local = pytz.timezone("America/Sao_Paulo")
    agora_local = datetime.now(tz_local).replace(tzinfo=None)

    try:
        if "T" in data_hora_str:
            data_hora_inicio = datetime.fromisoformat(data_hora_str).replace(tzinfo=None)
        else:
            data_hora_inicio = datetime.strptime(data_hora_str, "%Y-%m-%d %H:%M")
    except ValueError:
        return jsonify({"sucesso": False, "mensagem": "Formato de data/hora inválido. Use YYYY-MM-DD HH:MM."}), 400

    if data_hora_inicio <= agora_local:
        return jsonify({"sucesso": False, "mensagem": "Não é possível reservar um horário no passado."}), 400

    # 3. Carregamento dos Serviços e Conversão Blindada da Duração
    servicos = EseServicoOferecido.query.filter(
        EseServicoOferecido.id.in_(servico_ids),
        EseServicoOferecido.empresa_id == empresa_id
    ).all()

    if not servicos:
        return jsonify({"sucesso": False, "mensagem": "Serviços não encontrados."}), 404

    def extrair_minutos_servico(s):
        # Tenta pegar duracao_minutos ou tempo_duracao
        val = getattr(s, 'duracao_minutos', None) or getattr(s, 'tempo_duracao', None)
        if not val:
            return 30

        # Se for um inteiro ou string numérica (ex: 45 ou "45")
        if isinstance(val, int) or (isinstance(val, str) and val.isdigit()):
            return int(val)

        # Se for string no formato HH:MM (ex: "02:00" ou "00:45")
        if isinstance(val, str) and ":" in val:
            try:
                partes = val.split(":")
                return (int(partes[0]) * 60) + int(partes[1])
            except (ValueError, IndexError):
                return 30
        return 30

    duracao_total_min = sum(extrair_minutos_servico(s) for s in servicos)
    if duracao_total_min <= 0:
        duracao_total_min = 30

    data_hora_fim = data_hora_inicio + timedelta(minutes=duracao_total_min)

    # 4. Checagem de Conflito de Horário no Banco
    query_conflito = AghAgendamento.query.filter(
        AghAgendamento.empresa_id == empresa_id,
        AghAgendamento.status != "cancelado",
        AghAgendamento.data_hora_inicio < data_hora_fim,
        AghAgendamento.data_hora_fim > data_hora_inicio,
        db.or_(
            AghAgendamento.status != "soft_lock",
            db.and_(
                AghAgendamento.status == "soft_lock",
                AghAgendamento.expira_em > agora_local,
            )
        )
    )

    if profissional_id:
        query_conflito = query_conflito.filter(AghAgendamento.profissional_id == profissional_id)

    if query_conflito.first():
        return jsonify({
            "sucesso": False,
            "mensagem": "O horário selecionado acabou de ser reservado ou não está mais disponível."
        }), 409

    # 5. Persistência da Reserva Temporária (Soft Lock de 10 minutos)
    expira_em = agora_local + timedelta(minutes=10)

    novo_agendamento = AghAgendamento(
        empresa_id=empresa_id,
        profissional_id=profissional_id,
        cliente_id=cliente.id,
        data_hora_inicio=data_hora_inicio,
        data_hora_fim=data_hora_fim,
        status="soft_lock",
        expira_em=expira_em,
        origem="cliente"
    )

    db.session.add(novo_agendamento)
    db.session.commit()

    return jsonify({
        "sucesso": True,
        "agendamento_id": novo_agendamento.id,
        "expira_em": expira_em.strftime("%Y-%m-%d %H:%M:%S"),
        "mensagem": "Horário reservado com sucesso!"
    }), 201


# snippet de contexto preparado na rota Flask
@agenda_bp.route('/local/<int:local_id>/detalhes')
def ver_detalhes_local(local_id):
    local = Local.query.get_or_404(local_id)

    # 1. Agrupamento de Serviços
    servicos_agrupados = {}
    servicos = EseServicoOferecido.query.filter_by(empresa_id=local.id) \
        .join(Taxonomia) \
        .order_by(EseServicoOferecido.grupo, Taxonomia.nome).all()

    for s in servicos:
        grupo_nome = f"Grupo {s.grupo}" if s.grupo else "Serviços Gerais"
        if grupo_nome not in servicos_agrupados:
            servicos_agrupados[grupo_nome] = []
        servicos_agrupados[grupo_nome].append(s)

    # 2. Resumo de Rating
    rating_data = local.get_rating_data()

    # 3. Busca de Colaboradores Ativos do Local
    colaboradores = ColaboradorContrato.query.options(
        joinedload(ColaboradorContrato.cargo),
        joinedload(ColaboradorContrato.cadastro_modulo)
    ).filter(
        ColaboradorContrato.id_local == local_id,
        ColaboradorContrato.status_profissional == 'ativo',
        ColaboradorContrato.data_desligamento.is_(None)
    ).all()

    servicos_ordenados = sorted(
        servicos or [],
        key=lambda s: (
            str(getattr(s, 'grupo', '') or ''),
            str(getattr(s, 'descricao_servico', '') or '')
        )
    )

    return render_template(
        'empresa/perfil_institucional.html',
        local=local,
        rating=rating_data,
        servicos_agrupados=servicos_agrupados,
        servicos=servicos_ordenados,
        colaboradores=colaboradores
    )


def obter_passo_grade_empresa(empresa_id: int) -> int:
    """
    Busca o menor tempo total de bloqueio (duração + intervalo) entre os serviços
    oferecidos pela empresa para definir o incremento da grade de horários.
    Retorna o valor em minutos (mínimo de 15 min para evitar loops excessivos).
    """
    servicos = EseServicoOferecido.query.filter_by(empresa_id=empresa_id).all()
    if not servicos:
        return 30  # Fallback padrão caso a empresa não tenha serviços cadastrados

    menor_tempo = min(s.tempo_total_bloqueio_minutos for s in servicos)

    # Garante que o incremento não seja menor que 15 min para performance
    return max(menor_tempo, 15)


@agenda_bp.route('/api/rascunho/salvar', methods=['POST'])
def api_salvar_rascunho():
    """
    📌 API SILENCIOSA (AJAX) - AUTOSAVE DO RASCUNHO EM TEMPO REAL
    Sincroniza serviços, datas e profissionais com o rascunho ativo.
    """
    dados = request.get_json() or {}
    empresa_id = dados.get('empresa_id', type=int)

    if not empresa_id:
        return jsonify({'status': 'erro', 'mensagem': 'Empresa não informada.'}), 400

    cliente_id = current_user.id if current_user.is_authenticated else None
    session_token = session.get('agendamento_session_token')

    if not cliente_id and not session_token:
        session_token = secrets.token_hex(16)
        session['agendamento_session_token'] = session_token

    # 1. Busca ou cria o rascunho ativo
    q_rascunho = AghAgendamentoRascunho.query.filter_by(
        empresa_id=empresa_id,
        status='servicos_selecionados'
    )
    q_rascunho = q_rascunho.filter_by(cliente_id=cliente_id) if cliente_id else q_rascunho.filter_by(
        session_token=session_token)
    rascunho = q_rascunho.first()

    if not rascunho:
        rascunho = AghAgendamentoRascunho(
            empresa_id=empresa_id,
            cliente_id=cliente_id,
            session_token=session_token if not cliente_id else None,
            status='servicos_selecionados'
        )
        db.session.add(rascunho)
        db.session.flush()

    # 2. Atualiza cabeçalho do rascunho
    if 'profissional_id' in dados:
        rascunho.colaborador_id = dados.get('profissional_id', type=int)
    if 'data_agendamento' in dados:
        rascunho.data_agendamento = dados.get('data_agendamento')
    if 'horario_agendamento' in dados:
        rascunho.horario_agendamento = dados.get('horario_agendamento')
    if 'beneficiario_id' in dados:
        rascunho.beneficiario_id = dados.get('beneficiario_id', type=int)
    if 'observacoes' in dados:
        rascunho.observacoes = dados.get('observacoes', '')

    rascunho.atualizado_em = datetime.now(ZoneInfo('America/Sao_Paulo'))

    # 3. ATUALIZAÇÃO DOS ITENS (RESOLVE O ACÚMULO E DESMARCAÇÃO)
    # Se 'servico_ids' for enviado na requisição (mesmo que seja lista vazia [])
    if 'servico_ids' in dados:
        servico_ids = dados.get('servico_ids', [])

        # 🗑️ Sincronização: Remove SEMPRE todos os itens anteriores do rascunho
        AghAgendamentoRascunhoItem.query.filter_by(rascunho_id=rascunho.id).delete(synchronize_session=False)
        db.session.expire(rascunho, ['itens'])  # Força o SQLAlchemy a recarregar a lista de itens limpa

        valor_total = 0.0

        if servico_ids:
            # Busca os serviços e preços vigentes na empresa
            servicos_com_precos = db.session.query(
                EseServicoOferecido.id,
                EseServicoOferecido.tempo_duracao,
                EseServicoPreco.novo_valor
            ).join(
                EseServicoPreco,
                (EseServicoPreco.empresa_id == empresa_id) &
                (EseServicoPreco.taxonomia_id == EseServicoOferecido.taxonomia_id),
                isouter=True
            ).filter(
                EseServicoOferecido.empresa_id == empresa_id,
                EseServicoOferecido.id.in_(servico_ids)
            ).all()

            for s_id, tempo_duracao, preco_valor in servicos_com_precos:
                preco_unitario = float(preco_valor) if preco_valor is not None else 0.0
                duracao = int(tempo_duracao) if tempo_duracao and int(tempo_duracao) > 0 else 30

                valor_total += preco_unitario

                novo_item = AghAgendamentoRascunhoItem(
                    rascunho_id=rascunho.id,
                    servico_id=s_id,
                    preco_unitario=preco_unitario,
                    duracao_minutos=duracao
                )
                db.session.add(novo_item)

        rascunho.valor_total = valor_total

    db.session.commit()

    return jsonify({
        'status': 'sucesso',
        'rascunho_id': rascunho.id,
        'valor_total': float(rascunho.valor_total)
    })


def obter_grade_agendamento_empresa(
        empresa_id: int,
        data_alvo,
        duracao_total_minutos: int,
        contrato_id: int = None
) -> dict:
    """
    Orquestra o cálculo de disponibilidade:
    - Se contrato_id for informado (>0): Roda a função original para o profissional.
    - Se contrato_id for None/0 ("Qualquer um"): Roda a função original para todos os profissionais ativos
      e consolida quem está livre em cada slot.

    Retorna:
      {
        '08:00': [id_contrato1, id_contrato2],
        '08:30': [id_contrato2],
        '09:00': []
      }
    """
    empresa = EseEmpresa.query.get(empresa_id)
    if not empresa or not empresa.local_id:
        return {}

    if contrato_id and int(contrato_id) > 0:
        colaboradores = ColaboradorContrato.query.filter_by(
            id=contrato_id,
            status_profissional='ativo'
        ).all()
    else:
        colaboradores = ColaboradorContrato.query.filter_by(
            id_local=empresa.local_id,
            status_profissional='ativo'
        ).filter(ColaboradorContrato.data_desligamento.is_(None)).all()

    mapa_slots = {}

    for c in colaboradores:
        # Chama a SUA FUNÇÃO ORIGINAL existente!
        slots_colab = calcular_slots_disponiveis_colaborador(
            empresa_id=empresa_id,
            contrato_id=c.id,
            data_alvo=data_alvo,
            duracao_minutos_override=duracao_total_minutos
        )

        for hr in slots_colab:
            if hr not in mapa_slots:
                mapa_slots[hr] = []
            mapa_slots[hr].append(c.id)

    # Retorna dicionário ordenado pelo horário
    return dict(sorted(mapa_slots.items()))


def calcular_tolerancia_agendamento(agendamento, config_agenda=None) -> int:
    """Calcula o tempo de tolerância de atraso (em minutos) para um agendamento.

    Regra de Negócio:
    1. Se houver configuração ativa com 'tolerancia_atraso_minutos > 0', usa esse valor fixo.
    2. Caso contrário (0 ou None), calcula a tolerância como a soma dos 'tempo_intervalo'
       de cada serviço pertencente ao agendamento.
    3. Se o resultado final ainda for 0, retorna 0 (ou a tolerância mínima desejada).
    """
    # 1. Regra Prioritária: Tolerância parametrizada na empresa
    if config_agenda and getattr(config_agenda, 'tolerancia_atraso_minutos', 0) > 0:
        return int(config_agenda.tolerancia_atraso_minutos)

    # 2. Regra Secundaria (Fallback): Soma dos intervalos dos serviços
    intervalo_total = 0

    if agendamento and hasattr(agendamento, 'itens') and agendamento.itens:
        for item in agendamento.itens:
            # Tenta obter o serviço associado ao item do agendamento
            servico = getattr(item, 'servico', None)

            # Caso a propriedade no item se chame 'servico_oferecido' ou similar:
            if not servico and hasattr(item, 'servico_oferecido'):
                servico = item.servico_oferecido

            if servico:
                intervalo_total += getattr(servico, 'tempo_intervalo', 0) or 0

    # 3. Retorna a soma dos intervalos acumulados (ou 0 se nenhum serviço tiver intervalo)
    return intervalo_total


@agenda_bp.route('/api/agendamentos/<int:agendamento_id>/marcar-falta', methods=['POST'])
@login_required
def marcar_falta_agendamento(agendamento_id):
    """
    📌 Registra ausência do cliente (falta) no balcão/dashboard.
    Atualiza status para 'ausente_pendente', audita o encerramento e notifica o cliente.
    """
    agora = datetime.now(ZoneInfo('America/Sao_Paulo'))

    agendamento = AghAgendamento.query.get(agendamento_id)
    if not agendamento:
        return jsonify({
            'sucesso': False,
            'mensagem': f'O agendamento #{agendamento_id} não existe no banco de dados.'
        }), 404

    try:
        # 1. Atualiza status principal
        agendamento.status = 'ausente_pendente'
        agendamento.marcado_como_ausente_em = agora
        agendamento.marcado_como_ausente_por_id = current_user.id
        agendamento.updated_at = agora

        # 2. Registra ou atualiza o encerramento do agendamento
        encerramento = AghAgendamentoEncerramento.query.filter_by(agendamento_id=agendamento.id).first()
        prof_id = getattr(agendamento, 'profissional_id', None) or getattr(agendamento, 'colaborador_contrato_id', None)

        if not encerramento:
            encerramento = AghAgendamentoEncerramento(
                agendamento_id=agendamento.id,
                empresa_id=agendamento.empresa_id,
                colaborador_contrato_id=prof_id
            )
            db.session.add(encerramento)

        encerramento.motivo_cancelamento = 'Cliente ausente / Não compareceu'
        encerramento.cancelado_por_id = current_user.id
        encerramento.data_cancelamento = agora

        db.session.commit()

        # 3. Disparo de Notificação (Bloco Isolado)
        try:
            AghNotificacao.criar_notificacao(
                empresa_id=agendamento.empresa_id,
                usuario_id=str(agendamento.cliente_id),
                titulo="Ausência Registrada",
                mensagem=f"Seu agendamento das {agendamento.data_hora_inicio.strftime('%H:%M')} foi marcado como ausente.",
                tipo="ausencia",
                papel="cliente"
            )
        except Exception as e_notif:
            current_app.logger.warning(f"Falha ao enviar notificação de falta para agendamento {agendamento.id}: {e_notif}")

        return jsonify({
            'sucesso': True,
            'mensagem': 'Falta registrada com sucesso.',
            'agendamento_id': agendamento.id,
            'status': agendamento.status
        }), 200

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Erro ao registrar falta no agendamento {agendamento_id}: {e}")
        return jsonify({
            'sucesso': False,
            'mensagem': f'Erro ao gravar no banco de dados: {str(e)}'
        }), 500


@agenda_bp.route('/api/agendamentos/<int:agendamento_id>/marcar-ausente-pendente', methods=['POST'])
@login_required
def marcar_ausente_pendente(agendamento_id):
    """
    📌 SINALIZAÇÃO TEMPORÁRIA: Cliente atrasou, mas a falta ainda não foi efetivada.
    """
    agora = datetime.now(ZoneInfo('America/Sao_Paulo'))
    agendamento = AghAgendamento.query.get_or_404(agendamento_id)

    agendamento.status = 'ausente_pendente'
    agendamento.marcado_como_ausente_em = agora
    agendamento.updated_at = agora

    db.session.commit()
    return jsonify({'sucesso': True, 'status': 'ausente_pendente'})


@agenda_bp.route('/api/agendamentos/<int:agendamento_id>/marcar-falta', methods=['POST'])
@login_required
def marcar_falta_efetiva(agendamento_id):
    """
    📌 EFETIVAÇÃO DEFINITIVA DA FALTA (Balcão / API)
    Muda status para 'falta', registra encerramento auditado,
    grava no histórico de presença do cliente e envia notificação.
    """
    agora = datetime.now(ZoneInfo('America/Sao_Paulo'))

    agendamento = AghAgendamento.query.get(agendamento_id)
    if not agendamento:
        return jsonify({
            'sucesso': False,
            'mensagem': f'O agendamento #{agendamento_id} não existe no banco de dados.'
        }), 404

    try:
        # 1. Status definitivo
        agendamento.status = 'falta'
        agendamento.marcado_como_ausente_em = agora
        agendamento.marcado_como_ausente_por_id = current_user.id
        agendamento.updated_at = agora

        # 2. Encerramento Auditado
        encerramento = AghAgendamentoEncerramento.query.filter_by(agendamento_id=agendamento.id).first()
        prof_id = getattr(agendamento, 'profissional_id', None) or getattr(agendamento, 'colaborador_contrato_id', None)

        if not encerramento:
            encerramento = AghAgendamentoEncerramento(
                agendamento_id=agendamento.id,
                empresa_id=agendamento.empresa_id,
                colaborador_contrato_id=prof_id
            )
            db.session.add(encerramento)

        encerramento.motivo_cancelamento = 'Cliente ausente / Não compareceu'
        encerramento.cancelado_por_id = current_user.id
        encerramento.data_cancelamento = agora

        # 3. Histórico de Presença (reincidência do cliente)
        if getattr(agendamento, 'cliente_id', None):
            historico_kwargs = {
                'cliente_id': agendamento.cliente_id,
                'agendamento_id': agendamento.id,
                'tipo_evento': 'falta_cliente',
                'data_hora_agendada': agendamento.data_hora_inicio,
                'data_hora_evento': agora,
                'desvio_minutos': 0,
                'motivo': 'Cliente não compareceu ao horário agendado.'
            }
            if hasattr(AghHistoricoPresenca, 'empresa_id'):
                historico_kwargs['empresa_id'] = agendamento.empresa_id
            elif hasattr(AghHistoricoPresenca, 'estabelecimento_id'):
                historico_kwargs['estabelecimento_id'] = agendamento.empresa_id

            db.session.add(AghHistoricoPresenca(**historico_kwargs))

        db.session.commit()

        # 4. Notificação
        try:
            AghNotificacao.criar_notificacao(
                empresa_id=agendamento.empresa_id,
                usuario_id=str(agendamento.cliente_id),
                titulo="Ausência Registrada",
                mensagem=f"Seu agendamento das {agendamento.data_hora_inicio.strftime('%H:%M')} foi assinalado como não comparecimento.",
                tipo="ausencia",
                papel="cliente"
            )
        except Exception as e_notif:
            current_app.logger.warning(f"Falha ao enviar notificação de falta para agendamento {agendamento.id}: {e_notif}")

        return jsonify({
            'sucesso': True,
            'mensagem': 'Falta efetivada com sucesso.',
            'agendamento_id': agendamento.id,
            'status': agendamento.status
        }), 200

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Erro ao registrar falta no agendamento {agendamento_id}: {e}")
        return jsonify({
            'sucesso': False,
            'mensagem': f'Erro ao gravar no banco de dados: {str(e)}'
        }), 500


def obter_memoria_cliente(cliente_id, empresa_id):
    """Retorna as notas de encerramento passadas do cliente com suas respetivas datas."""
    return (
        db.session.query(
            AghAgendamentoEncerramento.observacoes,
            AghAgendamentoEncerramento.data_hora_fim_real,
        )
        .join(
            AghAgendamento,
            AghAgendamentoEncerramento.agendamento_id == AghAgendamento.id,
        )
        .filter(
            AghAgendamento.cliente_id == cliente_id,
            AghAgendamentoEncerramento.empresa_id == empresa_id,
            AghAgendamentoEncerramento.observacoes.isnot(None),
            AghAgendamentoEncerramento.observacoes != '',
        )
        .order_by(AghAgendamentoEncerramento.data_hora_fim_real.desc())
        .all()
    )


@agenda_bp.route('/empresa/<int:empresa_id>/agendamentos/<int:agendamento_id>/iniciar', methods=['POST'])
@login_required
@requer_nivel(min_nivel=300)
def iniciar_atendimento(empresa_id, agendamento_id):
    """
    📌 INÍCIO DE ATENDIMENTO E REGISTRO DE AUDITORIA DE PRESENÇA
    --------------------------------------------------------------------------------------
    Objetivo:
        Registra o marco real de início do serviço, armazena a auditoria de desvio de horário
        (pontualidade/atraso) e notifica o cliente final via push/notificação interna.
    """
    cliente_uuid = str(current_user.id)

    # Busca o contrato de trabalho ativo do colaborador que está realizando a operação
    contrato = ColaboradorContrato.query.filter_by(
        id_cadastro_cliente=cliente_uuid,
        id_local=empresa_id,
        status_profissional='ativo'
    ).first()

    colaborador_contrato_id = contrato.id if contrato else None

    tz_sp = ZoneInfo('America/Sao_Paulo')
    agora_sp = datetime.now(tz_sp)

    agendamento = AghAgendamento.query.filter_by(
        id=agendamento_id,
        empresa_id=empresa_id
    ).first()

    if not agendamento:
        return jsonify({
            'sucesso': False,
            'mensagem': 'Agendamento não encontrado.'
        }), 404

    status_permitidos = [
        'agendado',
        'confirmado',
        'aguardando',
        'soft_lock',
        'pendente'
    ]

    if agendamento.status not in status_permitidos:
        return jsonify({
            'sucesso': False,
            'mensagem': f'Não é possível iniciar um atendimento com status "{agendamento.status}".'
        }), 400

    try:
        # 1. Atualização do status do agendamento principal
        agendamento.status = 'em_atendimento'

        # 2. Criação ou atualização da ficha de controle/encerramento
        encerramento = AghAgendamentoEncerramento.query.filter_by(
            agendamento_id=agendamento.id
        ).first()

        if not encerramento:
            encerramento = AghAgendamentoEncerramento(
                agendamento_id=agendamento.id,
                empresa_id=empresa_id,
                colaborador_contrato_id=colaborador_contrato_id,
                data_hora_inicio_real=agora_sp
            )
            db.session.add(encerramento)
        else:
            encerramento.colaborador_contrato_id = colaborador_contrato_id
            encerramento.data_hora_inicio_real = agora_sp

        # 3. Auditoria e cálculo de pontualidade/desvio (em minutos)
        data_agendada = agendamento.data_hora_inicio
        if data_agendada.tzinfo is None:
            data_agendada = data_agendada.replace(tzinfo=tz_sp)

        diferenca_segundos = (agora_sp - data_agendada).total_seconds()
        desvio_minutos = int(diferenca_segundos // 60)

        if agendamento.cliente_id:
            historico_presenca = AghHistoricoPresenca(
                estabelecimento_id=empresa_id,
                cliente_id=agendamento.cliente_id,
                agendamento_id=agendamento.id,
                tipo_evento='inicio_atendimento',
                data_hora_agendada=agendamento.data_hora_inicio,
                data_hora_evento=agora_sp,
                desvio_minutos=desvio_minutos,
                motivo='Início de atendimento acionado na grade'
            )
            db.session.add(historico_presenca)

        # 4. Disparo de Notificação para o Cliente
        destinatario_id = getattr(agendamento.cliente, 'usuario_id', None) if agendamento.cliente else None

        # Identifica o nome do serviço para personalizar a mensagem
        nome_servico = "o serviço agendado"
        if hasattr(agendamento, 'servico') and agendamento.servico:
            nome_servico = getattr(agendamento.servico, 'nome', nome_servico)
        elif hasattr(agendamento, 'servico_oferecido') and agendamento.servico_oferecido:
            nome_servico = getattr(agendamento.servico_oferecido, 'nome', nome_servico)

        if destinatario_id:
            AghNotificacao.disparar(
                empresa_id=empresa_id,
                destinatario_id=destinatario_id,
                papel_destinatario='cliente',
                agendamento_id=agendamento.id,
                titulo="Atendimento Iniciado",
                mensagem=f"Seu atendimento para {nome_servico} foi iniciado.",
                tipo_evento="inicio_atendimento",
                nivel="info"
            )

        db.session.commit()

        return jsonify({
            'sucesso': True,
            'mensagem': 'Atendimento iniciado com sucesso!',
            'data_hora_inicio': agora_sp.strftime('%H:%M:%S'),
            'desvio_minutos': desvio_minutos
        }), 200

    except Exception as e:
        db.session.rollback()
        print(f"DEBUG INICIAR_ATENDIMENTO: Erro ao iniciar agendamento {agendamento_id} -> {e}")
        return jsonify({
            'sucesso': False,
            'mensagem': f'Erro interno ao processar início do atendimento: {str(e)}'
        }), 500


@agenda_bp.route('/empresa/<int:empresa_id>/agendamentos/<int:agendamento_id>/finalizar', methods=['POST'])
def finalizar_atendimento(empresa_id, agendamento_id):
    dados = request.get_json(silent=True) or {}

    resposta, status_code = AghAgendamentoEncerramento.finalizar_atendimento_servico(
        agendamento_id=agendamento_id,
        empresa_id=empresa_id,
        dados_encerramento=dados
    )

    return jsonify(resposta), status_code


@staticmethod
def finalizar_atendimento_servico(
        agendamento_id: int,
        empresa_id: int,
        observacoes: str = None,
        valor_final: float = None,
        forma_pagamento: str = None,
        dados_encerramento: dict = None,
        **kwargs
):
    """
    Finaliza o atendimento gravando a data/hora real de fim, atualizando o status do agendamento,
    registrando as observações do colaborador e disparando notificação ao cliente.
    """
    tz_sp = ZoneInfo('America/Sao_Paulo')
    agora_sp = datetime.now(tz_sp)

    dados = dados_encerramento or {}

    obs_raw = (
        observacoes if observacoes is not None
        else dados.get('observacoes') or dados.get('observacao') or dados.get('inputTexto') or kwargs.get('observacoes')
    )
    texto_obs = obs_raw.strip() if isinstance(obs_raw, str) and obs_raw.strip() else (obs_raw if obs_raw else None)

    val_final = (
        valor_final if valor_final is not None
        else dados.get('valor_final') or dados.get('valor_final_cobrado') or kwargs.get('valor_final')
    )

    forma_pag = (
        forma_pagamento if forma_pagamento is not None
        else dados.get('forma_pagamento') or kwargs.get('forma_pagamento')
    )

    agendamento = AghAgendamento.query.filter_by(
        id=agendamento_id, empresa_id=empresa_id
    ).first()

    if not agendamento:
        return {'sucesso': False, 'mensagem': 'Agendamento não encontrado.'}, 404

    try:
        # 1. Atualiza status
        agendamento.status = 'concluido'

        # 2. Registra encerramento
        encerramento = AghAgendamentoEncerramento.query.filter_by(
            agendamento_id=agendamento.id
        ).first()

        if not encerramento:
            encerramento = AghAgendamentoEncerramento(
                agendamento_id=agendamento.id,
                empresa_id=empresa_id,
                colaborador_contrato_id=getattr(agendamento, 'colaborador_contrato_id', None),
                data_hora_inicio_real=agora_sp,
                data_hora_fim_real=agora_sp,
                observacoes=texto_obs,
                valor_final_cobrado=val_final,
                forma_pagamento=forma_pag
            )
            db.session.add(encerramento)
        else:
            encerramento.data_hora_fim_real = agora_sp
            encerramento.observacoes = texto_obs
            if val_final is not None:
                encerramento.valor_final_cobrado = val_final
            if forma_pag is not None:
                encerramento.forma_pagamento = forma_pag
            db.session.add(encerramento)

        # 3. Histórico de presença
        historico_presenca = AghHistoricoPresenca(
            estabelecimento_id=empresa_id,
            cliente_id=agendamento.cliente_id,
            agendamento_id=agendamento.id,
            tipo_evento='fim_atendimento',
            data_hora_agendada=agendamento.data_hora_fim,
            data_hora_evento=agora_sp,
            desvio_minutos=0,
            motivo='Encerramento de atendimento registrado no modal com observação',
        )
        db.session.add(historico_presenca)

        # 4. 🔔 DISPARO DE NOTIFICAÇÃO (FIM DE ATENDIMENTO)
        destinatario_id = getattr(agendamento.cliente, 'usuario_id', None) if agendamento.cliente else None
        if destinatario_id:
            msg_valor = f" Valor final: R$ {val_final:.2f}." if val_final else ""
            AghNotificacao.disparar(
                empresa_id=empresa_id,
                destinatario_id=destinatario_id,
                papel_destinatario='cliente',
                agendamento_id=agendamento.id,
                titulo="Atendimento Concluído",
                mensagem=f"Seu atendimento foi concluído com sucesso.{msg_valor} Agradecemos a preferência!",
                tipo_evento="fim_atendimento",
                nivel="success"
            )

        db.session.commit()

        return {
            'sucesso': True,
            'mensagem': 'Atendimento finalizado com sucesso!',
            'data_hora_fim': agora_sp.strftime('%H:%M:%S')
        }, 200

    except Exception as e:
        db.session.rollback()
        return {
            'sucesso': False,
            'mensagem': f'Erro ao processar finalização: {str(e)}'
        }, 500


@agenda_bp.route('/empresa/<int:empresa_id>/agendamentos/<int:agendamento_id>/marcar-falta', methods=['POST'])
@agenda_bp.route('/atendimento/<int:agendamento_id>/marcar-falta', methods=['POST'])
@login_required
def marcar_falta_cliente(agendamento_id, empresa_id=None):
    """
    📌 ROTA OPERACIONAL DO PROFISSIONAL PARA REGISTRO DE NÃO COMPARECIMENTO (FALTA)
    --------------------------------------------------------------------------------------
    Objetivo:
        Registra a ausência do cliente na grade, atualiza o status para 'ausente_pendente',
        audita o encerramento, registra no histórico de presença e dispara notificação.
    """
    try:
        agendamento = AghAgendamento.query.get(agendamento_id)
        if not agendamento:
            return jsonify({
                'sucesso': False,
                'mensagem': 'Agendamento não encontrado.'
            }), 404

        emp_id = empresa_id or getattr(agendamento, 'empresa_id', None)
        tz_sp = ZoneInfo('America/Sao_Paulo')
        agora_sp = datetime.now(tz_sp)

        # 1. Altera o status para a chave padrão do dashboard e registra auditoria de ausência
        agendamento.status = 'ausente_pendente'
        agendamento.marcado_como_ausente_em = agora_sp
        agendamento.marcado_como_ausente_por_id = current_user.id
        agendamento.updated_at = agora_sp

        # 2. Registra ou atualiza o Encerramento
        encerramento = AghAgendamentoEncerramento.query.filter_by(agendamento_id=agendamento.id).first()
        prof_id = getattr(agendamento, 'profissional_id', None) or getattr(agendamento, 'colaborador_contrato_id', None)

        if not encerramento:
            encerramento = AghAgendamentoEncerramento(
                agendamento_id=agendamento.id,
                empresa_id=emp_id,
                colaborador_contrato_id=prof_id
            )
            db.session.add(encerramento)

        encerramento.motivo_cancelamento = 'Cliente ausente / Não compareceu'
        encerramento.cancelado_por_id = current_user.id
        encerramento.data_cancelamento = agora_sp

        # 3. Registra no Histórico de Presença
        if getattr(agendamento, 'cliente_id', None):
            historico_kwargs = {
                'cliente_id': agendamento.cliente_id,
                'agendamento_id': agendamento.id,
                'tipo_evento': 'falta_cliente',
                'data_hora_agendada': agendamento.data_hora_inicio,
                'data_hora_evento': agora_sp,
                'desvio_minutos': 0,
                'motivo': 'Cliente não compareceu ao horário agendado.'
            }

            if hasattr(AghHistoricoPresenca, 'estabelecimento_id'):
                historico_kwargs['estabelecimento_id'] = emp_id
            elif hasattr(AghHistoricoPresenca, 'empresa_id'):
                historico_kwargs['empresa_id'] = emp_id

            historico_presenca = AghHistoricoPresenca(**historico_kwargs)
            db.session.add(historico_presenca)

        # 4. Resolução segura do ID do destinatário da notificação
        destinatario_id = None
        if hasattr(agendamento, 'cliente') and agendamento.cliente:
            destinatario_id = getattr(agendamento.cliente, 'usuario_id', None) or getattr(agendamento.cliente, 'id_cadastro_cliente', None)

        if not destinatario_id:
            destinatario_id = getattr(agendamento, 'cliente_id', None)

        # 5. Disparo de Notificação Informativa (Bloco Isolado)
        if destinatario_id:
            try:
                data_formatada = agendamento.data_hora_inicio.strftime('%d/%m às %H:%M') if agendamento.data_hora_inicio else "horário agendado"

                AghNotificacao.disparar(
                    empresa_id=emp_id,
                    destinatario_id=destinatario_id,
                    papel_destinatario='cliente',
                    agendamento_id=agendamento.id,
                    titulo="Ausência Registrada",
                    mensagem=(
                        f"O seu atendimento marcado para {data_formatada} foi assinalado como não comparecimento. "
                        "Acesse a agenda caso deseje agendar um novo horário."
                    ),
                    tipo_evento="atraso_falta_cliente",
                    nivel="warning"
                )
            except Exception as err_notif:
                current_app.logger.warning(f"[AVISO NOTIFICAÇÃO] Falha ao enviar notificação de falta: {err_notif}")

        db.session.commit()

        return jsonify({
            'sucesso': True,
            'mensagem': 'Falta registrada com sucesso.',
            'agendamento_id': agendamento.id,
            'status': agendamento.status
        }), 200

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"[ERRO MARCAR FALTA]: {str(e)}")
        return jsonify({
            'sucesso': False,
            'mensagem': f'Erro interno no servidor: {str(e)}'
        }), 500


def definir_data_inicial_atendimento(empresa_id):
    """
    Define a data padrão de abertura do agendamento baseando-se unicamente
    no horário de encerramento das atividades da empresa no dia de HOJE.
    """
    tz_br = pytz.timezone('America/Sao_Paulo')
    agora_local = datetime.now(tz_br).replace(tzinfo=None)
    hoje_local = agora_local.date()

    # Mapeamento do dia da semana (1=Dom, 2=Seg, ..., 7=Sáb)
    py_weekday = hoje_local.weekday()
    dia_semana_padrao = 1 if py_weekday == 6 else py_weekday + 2

    # Busca o horário de funcionamento da empresa HOJE
    horarios_hoje = EseHorarioFuncionamento.query.filter_by(
        empresa_id=empresa_id,
        dia_semana=dia_semana_padrao
    ).all()

    # Se a empresa abre hoje, encontra o horário final de encerramento
    if horarios_hoje:
        ultimo_fechamento = max(h.horario_fechamento for h in horarios_hoje)
        limite_expediente_hoje = datetime.combine(hoje_local, ultimo_fechamento)

        # Se o horário atual ainda NÃO ultrapassou o fim do expediente da empresa:
        if agora_local < limite_expediente_hoje:
            return hoje_local

    # Se a empresa não abre hoje OU o expediente de hoje já encerrou,
    # busca o próximo dia em que a empresa possui expediente.
    return buscar_proximo_dia_expediente_empresa(empresa_id, a_partir_de=hoje_local + timedelta(days=1))


def buscar_proximo_dia_expediente_empresa(empresa_id, a_partir_de):
    """
    Varre os dias a partir da data informada até encontrar o próximo dia
    em que a empresa possui expediente cadastrado (e sem exceção de fechamento).
    """
    data_teste = a_partir_de

    # Limite de segurança de 30 dias para evitar loop infinito
    for _ in range(30):
        py_weekday = data_teste.weekday()
        dia_semana_padrao = 1 if py_weekday == 6 else py_weekday + 2

        # Verifica se há exceção de bloqueio cadastrada para este dia
        excecao = EmpresaCalendarioExcecao.query.filter_by(
            empresa_id=empresa_id,
            data_excecao=data_teste,
            trabalha_no_dia=False
        ).first()

        if not excecao:
            # Verifica se a empresa tem horário de funcionamento cadastrado para o dia da semana
            tem_funcionamento = EseHorarioFuncionamento.query.filter_by(
                empresa_id=empresa_id,
                dia_semana=dia_semana_padrao
            ).first()

            if tem_funcionamento:
                return data_teste

        data_teste += timedelta(days=1)

    return a_partir_de


from feedin.modules.agenda.services.agendamento_service import processar_solicitacao_reagendamento

@agenda_bp.route("/api/solicitacao-reagendamento/<int:solicitacao_id>/avaliar", methods=["POST"])
def avaliar_solicitacao_reagendamento(solicitacao_id):
    """Avaliação dos colaboradores para aprovar/recusar pedidos pendentes."""
    data = request.get_json() or {}
    acao = (data.get('acao') or '').lower().strip()
    resposta_colaborador = (data.get('resposta_colaborador') or '').strip()
    colaborador_id = data.get('colaborador_id')
    TZ_BRASIL = ZoneInfo('America/Sao_Paulo')

    if acao not in ['aprovar', 'recusar']:
        return jsonify({'erro': 'Ação inválida. Utilize "aprovar" ou "recusar".'}), 400

    solicitacao = AghSolicitacaoReagendamento.query.get_or_404(solicitacao_id)
    if solicitacao.status_solicitacao != 'pendente':
        return jsonify({'erro': f'Solicitação já encerrada ({solicitacao.status_solicitacao}).'}), 400

    agendamento_atual = AghAgendamento.query.get(solicitacao.agendamento_id)
    if not agendamento_atual:
        return jsonify({'erro': 'Agendamento associado não foi encontrado.'}), 404

    agora_br = datetime.now(TZ_BRASIL).replace(tzinfo=None)
    solicitacao.analisado_por_id = colaborador_id
    solicitacao.analisado_em = agora_br
    solicitacao.resposta_colaborador = resposta_colaborador

    if acao == 'aprovar':
        nova_data_hora = datetime.combine(solicitacao.nova_data, solicitacao.novo_horario)

        sucesso, mensagem, status_retorno = processar_solicitacao_reagendamento(
            agendamento_atual=agendamento_atual,
            nova_data_hora=nova_data_hora,
            empresa=agendamento_atual.empresa,
            justificativa=f"Aprovado por colaborador: {resposta_colaborador}" if resposta_colaborador else solicitacao.justificativa,
            is_colaborador=True,
            novo_profissional_id=solicitacao.novo_profissional_id
        )

        if not sucesso:
            db.session.rollback()
            return jsonify({'erro': mensagem}), 400

        solicitacao.status_solicitacao = 'aprovada'
        novo_agendamento = AghAgendamento.query.filter_by(agendamento_anterior_id=agendamento_atual.id).first()
        if novo_agendamento:
            solicitacao.novo_agendamento_id = novo_agendamento.id

        db.session.commit()
        return jsonify({
            'sucesso': True,
            'status': 'aprovada',
            'mensagem': 'Solicitação aprovada e nova data reagendada com sucesso!'
        })

    else:
        solicitacao.status_solicitacao = 'recusada'
        # Ajustado para usar a nomenclatura padronizada
        if agendamento_atual.status == 'reagendamento_pendente':
            agendamento_atual.status = 'confirmado'

        db.session.commit()
        return jsonify({
            'sucesso': True,
            'status': 'recusada',
            'mensagem': 'Solicitação recusada. O agendamento original permanece mantido.'
        })


@agenda_bp.route('/agendamento/<int:agendamento_id>/modal-reagendar', methods=['GET'])
@login_required  # Ou a verificação de sessão/cliente adequada
def obter_modal_reagendamento(agendamento_id):
    # Busca o agendamento pertencente ao usuário ou empresa
    agendamento = AghAgendamento.query.get_or_404(agendamento_id)

    # Renderiza apenas a partial isolada enviando o objeto agendamento
    return render_template('partials/_modal_reagendamento.html', agendamento=agendamento)


@agenda_bp.route("/api/agendamento/<int:agendamento_id>/reagendar", methods=["POST"])
def api_reagendar_agendamento(agendamento_id):
    data = request.get_json() or {}

    nova_data_str = data.get('data')       # Ex: "2026-08-28"
    novo_horario_str = data.get('horario') # Ex: "14:30"
    novo_profissional_id = data.get('profissional_id')
    justificativa = data.get('justificativa', '').strip()

    is_colaborador = session.get('user_tipo') in ['colaborador', 'admin', 'gestor'] or data.get('is_colaborador', False)

    agendamento = AghAgendamento.query.get_or_404(agendamento_id)

    if not nova_data_str or not novo_horario_str:
        return jsonify({'erro': 'Data e horário são obrigatórios para realizar o reagendamento.'}), 400

    try:
        nova_data_hora = datetime.strptime(f"{nova_data_str} {novo_horario_str}", '%Y-%m-%d %H:%M')
    except ValueError:
        return jsonify({'erro': 'Formato de data ou horário inválido.'}), 400

    # Delega a inteligência de negócios e genealogia para a função unificada
    sucesso, mensagem, status_retorno = processar_solicitacao_reagendamento(
        agendamento_atual=agendamento,
        nova_data_hora=nova_data_hora,
        empresa=agendamento.empresa,
        justificativa=justificativa,
        is_colaborador=is_colaborador,
        novo_profissional_id=novo_profissional_id
    )

    if not sucesso:
        code = 422 if status_retorno == 'requer_justificativa' else 400
        return jsonify({'erro': mensagem, 'status': status_retorno}), code

    return jsonify({
        'sucesso': True,
        'status': status_retorno,
        'mensagem': mensagem
    })


def calcular_regras_reagendamento(data_hora_original: datetime, data_hora_solicitacao: datetime = None):
    """
    Calcula as janelas e limites de reagendamento com base na antecedência de 2 horas.
    """
    tz_sp = ZoneInfo('America/Sao_Paulo')

    if not data_hora_solicitacao:
        data_hora_solicitacao = datetime.now(tz_sp)

    if data_hora_original.tzinfo is None:
        data_hora_original = data_hora_original.replace(tzinfo=tz_sp)

    # ⏱️ 1. Verifica a antecedência da solicitação em relação ao horário agendado
    horas_antecedencia = (data_hora_original - data_hora_solicitacao).total_seconds() / 3600.0

    # 🎯 CENÁRIO A: Reagendamento Preventivo (≥ 2 horas antes)
    if horas_antecedencia >= 2.0:
        limite_dias = 30
        data_limite_maxima = data_hora_original + timedelta(dias=30)
        requer_justificativa = False
        tipo_janela = 'preventivo_30_dias'
        mensagem = 'Você tem até 30 dias a partir da data original para reagendar.'

    # 🎯 CENÁRIO B: Em cima da hora (< 2 horas antes ou até a data limite de 7 dias)
    else:
        limite_dias = 7
        data_limite_maxima = data_hora_original + timedelta(dias=7)
        tipo_janela = 'emergencia_7_dias'

        # Se a tentativa de reagendar for realizada DENTRO do prazo de 7 dias
        if data_hora_solicitacao <= data_limite_maxima:
            requer_justificativa = False
            mensagem = 'Reagendamento de emergência: selecione uma data nos próximos 7 dias para não perder sua reserva.'
        else:
            # Passou dos 7 dias sem remarcar: Exige justificativa para análise de exceção
            requer_justificativa = True
            mensagem = 'O prazo de 7 dias para reagendamento expirou. É necessário enviar uma justificativa para análise.'

    return {
        'horas_antecedencia': horas_antecedencia,
        'limite_dias': limite_dias,
        'data_limite_maxima': data_limite_maxima,
        'requer_justificativa': requer_justificativa,
        'tipo_janela': tipo_janela,
        'mensagem': mensagem
    }


@agenda_bp.route('/comprovante-admissao/<int:contrato_id>', methods=['GET'])
@login_required
def baixar_comprovante_admissao(contrato_id):
    """
    Permite que o colaborador logado no módulo Agenda baixe ou
    visualize seu próprio comprovante de admissão em PDF.
    """
    contrato = ColaboradorContrato.query.get_or_404(contrato_id)

    # Validação de Segurança: O contrato precisa pertencer ao colaborador logado
    usuario_uuid = str(getattr(current_user, 'id', None))
    e_dono_do_contrato = (contrato.id_cadastro_cliente and str(contrato.id_cadastro_cliente) == usuario_uuid)

    if not e_dono_do_contrato:
        abort(403)

    # Identificação do nome da empresa/local
    nome_empresa = "Empresa"
    if hasattr(contrato, 'local_trabalho') and contrato.local_trabalho:
        nome_empresa = getattr(contrato.local_trabalho, 'nome_fantasia', None) or getattr(contrato.local_trabalho, 'razao_social', 'Empresa')
    elif hasattr(contrato, 'empresa') and contrato.empresa:
        nome_empresa = getattr(contrato.empresa, 'nome_fantasia', None) or getattr(contrato.empresa, 'razao_social', 'Empresa')

    nome_colaborador = getattr(contrato, 'nome', getattr(current_user, 'nome', 'Colaborador'))
    hash_validacao = f"ADM-{contrato.id}-{contrato.id_local}-{int(datetime.now().timestamp())}"

    # Renderiza o template do comprovante PDF
    html_string = render_template(
        'pdf/comprovante_admissao.html',
        contrato=contrato,
        nome_colaborador=nome_colaborador,
        nome_empresa=nome_empresa,
        hash_validacao=hash_validacao,
        data_emissao=datetime.now().strftime('%d/%m/%Y às %H:%M')
    )

    # Gera o PDF direto na memória via WeasyPrint
    pdf_bytes = HTML(string=html_string).write_pdf()

    # Define se faz o download imediato ou abre no navegador
    modo = request.args.get('modo', 'download')
    disposition = 'inline' if modo == 'visualizar' else f'attachment; filename="Comprovante_Admissao_{contrato.id}.pdf"'

    response = make_response(pdf_bytes)
    response.headers['Content-Type'] = 'application/pdf'
    response.headers['Content-Disposition'] = disposition
    return response


def expirar_rascunhos_obsoletos():
    """Libera horários e marca como expirados os rascunhos cujo tempo estourou."""
    agora = datetime.utcnow()
    rascunhos_expirados = AghAgendamentoRascunho.query.filter(
        AghAgendamentoRascunho.status.in_(['servicos_selecionados', 'horario_reservado']),
        AghAgendamentoRascunho.expira_em <= agora
    ).all()

    for rascunho in rascunhos_expirados:
        rascunho.status = 'expirado'

    db.session.commit()


def expirar_rascunhos_obsoletos():
    """Libera horários e marca como expirados os rascunhos cujo tempo estourou."""
    agora = datetime.utcnow()
    rascunhos_expirados = AghAgendamentoRascunho.query.filter(
        AghAgendamentoRascunho.status.in_(['servicos_selecionados', 'horario_reservado']),
        AghAgendamentoRascunho.expira_em <= agora
    ).all()

    for rascunho in rascunhos_expirados:
        rascunho.status = 'expirado'

    db.session.commit()


from datetime import date, datetime
from flask import render_template, request
from sqlalchemy import asc


@agenda_bp.route('/agenda', methods=['GET'])
# @login_required # Ative se usar Flask-Login
def visualizar_agenda():
    # -------------------------------------------------------------------------
    # 1. AUTENTICAÇÃO REAL (Substituindo get_usuario_logado)
    # -------------------------------------------------------------------------
    # Se estiver usando Flask-Login:
    usuario_atual = current_user

    # SE ESTIVER USANDO SESSÃO MANUAL (descomente as linhas abaixo se for o caso):
    # usuario_id = session.get('usuario_id')
    # usuario_atual = Usuario.query.get(usuario_id)

    # Identifica a empresa do usuário logado
    empresa_id_atual = getattr(usuario_atual, 'empresa_id', 1)

    # Verificação de Administrador (Nível 999 ou flag e_admin)
    nivel = getattr(usuario_atual, 'nivel_acesso', 0)
    e_admin = (nivel == 999) or getattr(usuario_atual, 'e_admin', False)

    # -------------------------------------------------------------------------
    # 2. TRATAMENTO DA DATA
    # -------------------------------------------------------------------------
    data_str = request.args.get('data')
    if data_str:
        try:
            data_filtro = datetime.strptime(data_str, '%Y-%m-%d').date()
        except ValueError:
            data_filtro = date.today()
    else:
        data_filtro = date.today()

    # -------------------------------------------------------------------------
    # 3. CONSULTA DOS AGENDAMENTOS NO BANCO
    # -------------------------------------------------------------------------
    query = AghAgendamento.query.filter(
        AghAgendamento.empresa_id == empresa_id_atual,
        AghAgendamento.data == data_filtro,
    )

    # Se NÃO for Admin (Nível 999), restringe os agendamentos ao profissional logado
    if not e_admin:
        # Pega o ID de profissional vinculado ao usuário logado
        prof_id_logado = getattr(usuario_atual, 'profissional_id', None)
        query = query.filter(AghAgendamento.profissional_id == prof_id_logado)

    # Ordenação Cronológica pura por horário de início
    agendamentos_raw = query.order_by(asc(AghAgendamento.hora_inicio)).all()

    # -------------------------------------------------------------------------
    # 4. MONTAGEM DA GRADE PARA O JINJA
    # -------------------------------------------------------------------------
    grade_slots = []
    for agend in agendamentos_raw:
        # Busca segura do nome do profissional
        prof_nome = 'Não especificado'
        if agend.profissional:
            if hasattr(agend.profissional, 'nome') and agend.profissional.nome:
                prof_nome = agend.profissional.nome
            elif (
                hasattr(agend.profissional, 'usuario')
                and agend.profissional.usuario
            ):
                prof_nome = agend.profissional.usuario.nome
            else:
                prof_nome = f'Profissional #{agend.profissional_id}'

        servico_nome = (
            agend.servico.nome
            if getattr(agend, 'servico', None)
            else 'Serviço não especificado'
        )

        grade_slots.append({
            'id': str(agend.id),
            'hora_inicio': (
                agend.hora_inicio.strftime('%H:%M')
                if agend.hora_inicio
                else '--:--'
            ),
            'cliente_nome': agend.cliente_nome or 'Cliente não informado',
            'servico_nome': servico_nome,
            'status_slug': agend.status_slug,
            'profissional_id': (
                str(agend.profissional_id).lower()
                if agend.profissional_id
                else ''
            ),
            'profissional_nome': prof_nome,
        })

    # -------------------------------------------------------------------------
    # 5. LISTA REAL DE PROFISSIONAIS (Substituindo obter_lista_profissionais)
    # -------------------------------------------------------------------------
    profissionais_filtro = []
    if e_admin:
        # Busca direto na tabela/Model de Profissionais da empresa atual
        # (Ajuste 'AghProfissional' para o nome exato da sua model de profissionais)
        profissionais_db = ColaboradorContrato.query.filter_by(
            empresa_id=empresa_id_atual, ativo=True
        ).all()

        for prof in profissionais_db:
            # Obtém o nome vindo do próprio cadastro ou da relação com o Usuário
            nome = getattr(prof, 'nome', None)
            if not nome and hasattr(prof, 'usuario') and prof.usuario:
                nome = prof.usuario.nome

            profissionais_filtro.append({
                'id': str(prof.id).lower(),  # UUID/ID garantido como String minúscula
                'nome': nome or f'Profissional #{prof.id}',
            })

    # -------------------------------------------------------------------------
    # 6. RENDERIZAÇÃO
    # -------------------------------------------------------------------------
    return render_template(
        'agenda/grade.html',
        grade=grade_slots,
        e_admin=e_admin,
        profissionais=profissionais_filtro,
        data_selecionada=data_filtro.strftime('%Y-%m-%d'),
        cor_empresa='#0d6efd',
    )


@agenda_bp.route('/api/grade-agendamentos', methods=['GET'])
@login_required
def carregar_grade_agendamentos():
    data_str = request.args.get('data')
    data_selecionada = datetime.strptime(data_str, '%Y-%m-%d').date() if data_str else date.today()

    # 1. Verifica permissões de administração/gestão
    eh_admin = current_user.tem_permissao('admin_agenda') or current_user.is_admin

    query = db.session.query(AghAgendamentoItem).join(AghAgendamento).filter(
        db.func.date(AghAgendamentoItem.data_hora_inicio) == data_selecionada
    )

    if eh_admin:
        # Se for admin e houver um filtro específico selecionado na tela
        colaborador_filtro_id = request.args.get('profissional_id', type=int)
        if colaborador_filtro_id:
            query = query.filter(AghAgendamentoItem.profissional_id == colaborador_filtro_id)
    else:
        # Colaborador comum: força a filtragem exclusiva pelo seu próprio contrato_id
        colaborador_id_usuario = current_user.colaborador_contrato_id
        query = query.filter(AghAgendamentoItem.profissional_id == colaborador_id_usuario)

    # Ordenação pela ordem de execução e horário inicial
    itens = query.order_by(
        AghAgendamentoItem.ordem_execucao.asc(),
        AghAgendamentoItem.data_hora_inicio.asc()
    ).all()

    return jsonify({
        "exibir_filtro_admin": eh_admin,
        "itens": [item.to_dict() for item in itens]
    })


def resolver_nome_profissional(prof_obj) -> str:
    """
    Extrai o nome do profissional navegando pelos relacionamentos de ColaboradorContrato.
    Tolera objetos nulos e falhas de carregamento de relacionamentos.
    """
    if not prof_obj:
        return "Profissional não informado"

    # 1. Tenta obter diretamente via cadastro_modulo (relacionamento padrão)
    if hasattr(prof_obj, 'cadastro_modulo') and prof_obj.cadastro_modulo:
        # Se houver campo 'nome_social' ou 'nome' no módulo de cadastro
        nome = getattr(prof_obj.cadastro_modulo, 'nome_social', None) or getattr(prof_obj.cadastro_modulo, 'nome', None)
        if nome:
            return nome.strip()

    # 2. Tenta campos de nome direto no próprio objeto prof_obj
    for campo in ['nome_social', 'nome', 'nome_completo', 'razao_social']:
        if hasattr(prof_obj, campo):
            val = getattr(prof_obj, campo)
            if val:
                return str(val).strip()

    # 3. Fallback caso possua apenas ID
    if hasattr(prof_obj, 'id'):
        return f"Profissional #{prof_obj.id}"

    return "Profissional Indefinido"


@agenda_bp.route('/agendamento/<int:agendamento_id>/reorganizar_itens', methods=['POST'])
@login_required
def reorganizar_itens_agendamento(agendamento_id):
    """
    Recebe uma lista com a nova ordem e modo de execução dos itens da comanda.
    Exemplo JSON esperado:
    {
        "itens": [
            {"item_id": 105, "ordem_execucao": 1, "modo_execucao": "sequencial"},
            {"item_id": 106, "ordem_execucao": 2, "modo_execucao": "simultaneo"}
        ]
    }
    """
    data = request.get_json() or {}
    itens_payload = data.get('itens', [])

    if not itens_payload:
        return jsonify({'sucesso': False, 'mensagem': 'Nenhum item informado.'}), 400

    try:
        for payload in itens_payload:
            item_db = AghAgendamentoItem.query.filter_by(
                id=payload['item_id'],
                agendamento_id=agendamento_id
            ).first()

            if item_db:
                item_db.ordem_execucao = int(payload.get('ordem_execucao', 1))
                item_db.modo_execucao = payload.get('modo_execucao', 'sequencial')

                # Se mudou o profissional responsável na reunião técnica
                if 'profissional_id' in payload:
                    item_db.profissional_id = payload['profissional_id']

        db.session.commit()
        return jsonify({'sucesso': True, 'mensagem': 'Ordem e slots atualizados com sucesso!'})

    except Exception as e:
        db.session.rollback()
        return jsonify({'sucesso': False, 'mensagem': f'Erro ao atualizar: {str(e)}'}), 500


@agenda_bp.route('/agendamento/<int:agendamento_id>/solicitar_reordenacao', methods=['POST'])
@login_required
def solicitar_reordenacao(agendamento_id):
    """
    Registra uma proposta de reordenamento.
    Se o usuario for Gestor (nível >= 600), aplica imediatamente.
    Se for Colaborador, notifica os envolvidos e aguarda aceite.
    """
    data = request.get_json() or {}
    itens_propostos = data.get('itens', [])  # [{"item_id": 1, "ordem_execucao": 2, "modo_execucao": "simultaneo"}]

    if not itens_propostos:
        return jsonify({'sucesso': False, 'mensagem': 'Nenhum item informado para alteração.'}), 400

    agendamento = AghAgendamento.query.get_or_404(agendamento_id)

    # Identifica o contrato do usuário atual nesta empresa
    contrato_atual = ColaboradorContrato.query.filter_by(
        id_local=agendamento.empresa_id,
        id_cadastro_cliente=str(getattr(current_user, 'uuid', getattr(current_user, 'id', ''))).strip().lower(),
        status_profissional='ativo'
    ).first()

    e_gestor = (contrato_atual and contrato_atual.papel_nivel >= 600) or getattr(current_user, 'is_admin', False)

    # 👔 CASO 1: SE FOR GESTOR, APLICA DIRETO
    if e_gestor:
        aplicar_reordenacao_bd(agendamento_id, itens_propostos)
        return jsonify({
            'sucesso': True,
            'requer_aceite': False,
            'mensagem': 'Reorganização aprovada e aplicada pelo Gestor!'
        })

    # 👤 CASO 2: COLABORADOR -> IDENTIFICA PROFISSIONAIS AFETADOS
    # Coleta todos os colaboradores que possuem itens nesta comanda (exceto o próprio solicitante)
    profissionais_afetados = set()
    for item in agendamento.itens:
        if item.profissional_id and item.profissional_id != contrato_atual.id:
            profissionais_afetados.add(item.profissional_id)

    # Se não houver outros colaboradores envolvidos (apenas o próprio solicitante em mais de 1 item)
    if not profissionais_afetados:
        aplicar_reordenacao_bd(agendamento_id, itens_propostos)
        return jsonify({
            'sucesso': True,
            'requer_aceite': False,
            'mensagem': 'Ajuste em seus próprios itens aplicado com sucesso!'
        })

    # Cria a solicitação pendente de aprovação dos colegas
    solicitacao = AghReordenacaoSolicitada(
        agendamento_id=agendamento_id,
        solicitante_id=contrato_atual.id,
        proposta_json=itens_propostos,
        profissionais_pendentes_ids=list(profissionais_afetados),
        status='pendente'
    )

    db.session.add(solicitacao)
    db.session.commit()

    return jsonify({
        'sucesso': True,
        'requer_aceite': True,
        'solicitacao_id': solicitacao.id,
        'mensagem': 'Solicitação de reprogramação enviada aos colaboradores envolvidos.'
    })


def aplicar_reordenacao_bd(agendamento_id, itens_propostos):
    """Função utilitária que grava as alterações finais no banco."""
    for prop in itens_propostos:
        item_db = AghAgendamentoItem.query.filter_by(id=prop['item_id'], agendamento_id=agendamento_id).first()
        if item_db:
            item_db.ordem_execucao = int(prop.get('ordem_execucao', 1))
            item_db.modo_execucao = prop.get('modo_execucao', 'sequencial')
    db.session.commit()


@agenda_bp.route('/reordenacao/<int:solicitacao_id>/responder', methods=['POST'])
@login_required
def responder_solicitacao_reordenacao(solicitacao_id):
    """
    Permite que o colaborador afetado aceite ou recuse a mudança de horários/sequência.
    """
    data = request.get_json() or {}
    resposta = data.get('resposta')  # 'aceitar' ou 'rejeitar'

    solicitacao = AghReordenacaoSolicitada.query.get_or_404(solicitacao_id)
    if solicitacao.status != 'pendente':
        return jsonify({'sucesso': False, 'mensagem': 'Esta solicitação não está mais pendente.'}), 400

    # Localiza o contrato do usuário que está respondendo
    contrato_atual = ColaboradorContrato.query.filter_by(
        id_local=solicitacao.agendamento.empresa_id,
        id_cadastro_cliente=str(getattr(current_user, 'uuid', getattr(current_user, 'id', ''))).strip().lower(),
        status_profissional='ativo'
    ).first()

    if not contrato_atual or contrato_atual.id not in solicitacao.profissionais_pendentes_ids:
        return jsonify({'sucesso': False, 'mensagem': 'Você não tem pendência de autorização nesta proposta.'}), 403

    if resposta == 'rejeitar':
        solicitacao.status = 'rejeitado'
        db.session.commit()
        return jsonify({'sucesso': True, 'mensagem': 'Proposta de alteração de horário foi recusada.'})

    elif resposta == 'aceitar':
        # Remove este profissional da lista de pendências
        pendentes = [p_id for p_id in solicitacao.profissionais_pendentes_ids if p_id != contrato_atual.id]
        solicitacao.profissionais_pendentes_ids = pendentes

        # Se todos os envolvidos já deram o 'de acordo'
        if len(pendentes) == 0:
            solicitacao.status = 'aprovado'
            # Aplica a reorganização na tabela oficial AghAgendamentoItem
            aplicar_reordenacao_bd(solicitacao.agendamento_id, solicitacao.proposta_json)
            mensagem_retorno = 'Todos os envolvidos aceitaram. Horários e sequência atualizados!'
        else:
            mensagem_retorno = 'Seu aceite foi registrado. Aguardando demais colaboradores.'

        db.session.commit()
        return jsonify({'sucesso': True, 'mensagem': mensagem_retorno})


agenda_bp = Blueprint('agenda_bp', __name__)

def obter_data_hora_atual():
    """Helper interno para obter fuso local ou UTC desacoplado."""
    if 'obter_hora_local' in globals():
        return obter_hora_local().replace(tzinfo=None)
    return datetime.now(timezone.utc).replace(tzinfo=None)


# --------------------------------------------------------------------------
# 1. ROTA DE CANCELAMENTO (Auditada + AghHistoricoPresenca)
# --------------------------------------------------------------------------
@agenda_bp.route('/agendamento/<int:agendamento_id>/cancelar', methods=['POST'])
def cancelar_agendamento(agendamento_id):
    agendamento = (
        AghAgendamento.query.options(selectinload(AghAgendamento.itens))
        .filter_by(id=agendamento_id)
        .first()
    )

    if not agendamento:
        return jsonify({'sucesso': False, 'mensagem': 'Agendamento não encontrado.'}), 404

    # Identificação do Cliente Logado
    id_cliente_logado = None
    if hasattr(current_user, 'cliente') and current_user.cliente:
        id_cliente_logado = current_user.cliente.id
    elif isinstance(current_user, ModCadastroCliente):
        id_cliente_logado = current_user.id
    else:
        cliente_obj = ModCadastroCliente.query.filter_by(usuario_id=current_user.id).first()
        if cliente_obj:
            id_cliente_logado = cliente_obj.id

    is_cliente = (id_cliente_logado is not None) and (agendamento.cliente_id == id_cliente_logado)

    has_role_func = getattr(current_user, 'has_role', lambda r: False)
    is_admin_ou_staff = any(has_role_func(role) for role in ['admin', 'gestor', 'colaborador'])

    if not (is_cliente or is_admin_ou_staff):
        return jsonify({'sucesso': False, 'mensagem': 'Você não tem permissão para cancelar este agendamento.'}), 403

    if agendamento.status == 'cancelado':
        return jsonify({'sucesso': False, 'mensagem': 'Este agendamento já se encontra cancelado.'}), 400

    agora_local = obter_data_hora_atual()

    # Validação Temporal para Clientes
    if is_cliente:
        if agora_local >= agendamento.data_hora_inicio:
            return jsonify({'sucesso': False, 'mensagem': 'Não é possível cancelar um agendamento cujo horário já passou.'}), 400

        horas_limite = getattr(agendamento.empresa, 'horas_limite_cancelamento', 2) or 2
        limite_cancelamento = agendamento.data_hora_inicio - timedelta(hours=horas_limite)

        if agora_local > limite_cancelamento:
            return jsonify({
                'sucesso': False,
                'mensagem': f'Cancelamento direto permitido apenas com até {horas_limite}h de antecedência. Entre em contato com o estabelecimento.'
            }), 400

    data = request.get_json() or {}
    motivo_cat = data.get('motivo_categoria', 'desistencia')
    motivo_det = data.get('motivo_detalhado', '').strip()

    try:
        data_cancelamento = agora_local

        # 1. Atualiza Agendamento Principal e Itens
        agendamento.status = 'cancelado'
        agendamento.atualizado_em = data_cancelamento

        for item in agendamento.itens:
            item.status = 'cancelado'
            if hasattr(item, 'atualizado_em'):
                item.atualizado_em = data_cancelamento

        # 2. Define Origem
        if is_cliente:
            origem = 'cliente'
        elif has_role_func('gestor') or has_role_func('admin'):
            origem = 'gestor'
        else:
            origem = 'colaborador'

        valor_pago = getattr(agendamento, 'valor_pago', 0.00) or 0.00
        requer_reembolso = valor_pago > 0

        # 3. Registro Auditado em AghCancelamento
        novo_cancelamento = AghCancelamento(
            agendamento_id=agendamento.id,
            empresa_id=agendamento.empresa_id,
            solicitante_id=str(current_user.id),
            origem_solicitacao=origem,
            motivo_categoria=motivo_cat,
            motivo_detalhado=motivo_det,
            valor_pago_momento=valor_pago,
            requer_ressarcimento=requer_reembolso,
            status_ressarcimento='pendente' if requer_reembolso else 'nao_aplicavel',
            solicitado_em=data_cancelamento,
            criado_em=data_cancelamento
        )
        db.session.add(novo_cancelamento)

        # 4. Registro no Histórico de Presença (Prontuário/Score do Cliente)
        historico_cancelamento = AghHistoricoPresenca(
            estabelecimento_id=agendamento.empresa_id,
            cliente_id=agendamento.cliente_id,
            agendamento_id=agendamento.id,
            tipo_evento='cancelamento_cliente' if origem == 'cliente' else 'cancelamento_estabelecimento',
            data_hora_agendada=agendamento.data_hora_inicio,
            data_hora_evento=data_cancelamento,
            desvio_minutos=0,
            motivo=f"Categoria: {motivo_cat} | Detalhes: {motivo_det or 'Não informado'}"
        )
        db.session.add(historico_cancelamento)

        db.session.commit()
        return jsonify({'sucesso': True, 'mensagem': 'Agendamento e seus serviços associados foram cancelados com sucesso.'}), 200

    except Exception as e:
        db.session.rollback()
        return jsonify({'sucesso': False, 'mensagem': f'Erro ao processar cancelamento: {str(e)}'}), 500


# --------------------------------------------------------------------------
# 2. ROTA DE ENCERRAMENTO (Prontuário + Matriz de Fidelidade)
# --------------------------------------------------------------------------
@agenda_bp.route('/agendamento/<int:agendamento_id>/concluir', methods=['POST'])
def concluir_agendamento(agendamento_id):
    agendamento = AghAgendamento.query.get_or_404(agendamento_id)

    if agendamento.status == 'concluido':
        return jsonify({'sucesso': False, 'mensagem': 'Atendimento já se encontra concluído.'}), 400

    data = request.get_json() or {}
    observacao_atendimento = data.get('observacao_cliente', '').strip()

    agora_local = obter_data_hora_atual()

    try:
        # 1. Conclui Agendamento e Itens
        agendamento.status = 'concluido'
        agendamento.data_hora_fim_real = agora_local

        for item in agendamento.itens:
            item.status = 'concluido'

        # 2. Grava Log no Prontuário Comportamental
        log_presenca = AghHistoricoPresenca(
            estabelecimento_id=agendamento.empresa_id,
            cliente_id=agendamento.cliente_id,
            agendamento_id=agendamento.id,
            tipo_evento='conclusao_atendimento',
            data_hora_agendada=agendamento.data_hora_inicio,
            data_hora_evento=agora_local,
            desvio_minutos=0,
            motivo=observacao_atendimento or 'Atendimento concluído com sucesso.'
        )
        db.session.add(log_presenca)

        db.session.commit()
        return jsonify({'sucesso': True, 'mensagem': 'Atendimento concluído e registrado no histórico do cliente.'}), 200

    except Exception as e:
        db.session.rollback()
        return jsonify({'sucesso': False, 'mensagem': f'Erro ao concluir atendimento: {str(e)}'}), 500


# --------------------------------------------------------------------------
# 3. MODAL DO COLABORADOR / SERVIÇO (Duplo Clique)
# --------------------------------------------------------------------------
@agenda_bp.route('/colaborador/<int:usuario_id>/perfil_modal', methods=['GET'])
def perfil_colaborador_modal(usuario_id):
    usuario = Usuario.query.get_or_404(usuario_id)

    # A. Média Geral do Profissional (CoreAvaliacaoProfissional)
    avaliacoes = CoreAvaliacaoProfissional.query.filter_by(profissional_usuario_id=usuario_id).all()
    total_avaliacoes = len(avaliacoes)

    if total_avaliacoes > 0:
        media_atend = sum(a.nota_atendimento for a in avaliacoes) / total_avaliacoes
        media_tec = sum(a.nota_tecnica for a in avaliacoes) / total_avaliacoes
        media_geral = round((media_atend + media_tec) / 2, 1)
    else:
        media_geral = 5.0  # Padrão inicial para novos perfis

    # B. Média por Item de Serviço Específico (se fornecido via query param ?item_id=123)
    item_id = request.args.get('item_id', type=int)
    media_servico_item = None

    if item_id:
        item = AghAgendamentoItem.query.get(item_id)
        if item and hasattr(item, 'servico_id'):
            avaliacoes_item = db.session.query(AghAvaliacaoServico).join(AghAgendamentoItem).filter(
                AghAgendamentoItem.colaborador_id == usuario_id,
                AghAgendamentoItem.servico_id == item.servico_id
            ).all()
            if avaliacoes_item:
                media_servico_item = round(sum(a.nota for a in avaliacoes_item) / len(avaliacoes_item), 1)

    # C. Total de Atendimentos Concluídos
    total_atendimentos = AghAgendamentoItem.query.filter_by(
        colaborador_id=usuario_id, status='concluido'
    ).count()

    return jsonify({
        'sucesso': True,
        'nome': getattr(usuario, 'nome', 'Colaborador'),
        'nao_empresa_desde': usuario.criado_em.strftime('%d/%m/%Y') if hasattr(usuario, 'criado_em') and usuario.criado_em else 'N/A',
        'media_geral': media_geral,
        'media_servico_especifico': media_servico_item,
        'total_avaliacoes': total_avaliacoes,
        'total_atendimentos': total_atendimentos
    }), 200


# --------------------------------------------------------------------------
# 4. MODAL DO PRONTUÁRIO DO CLIENTE (Clique Longo / Manter Pressionado)
# --------------------------------------------------------------------------
@agenda_bp.route('/cliente/<string:cliente_id>/prontuario_modal', methods=['GET'])
def prontuario_cliente_modal(cliente_id):
    cliente = ModCadastroCliente.query.get_or_404(cliente_id)

    # Busca até 10 últimos registros comportamentais do cliente
    historico = AghHistoricoPresenca.query.filter_by(
        cliente_id=cliente_id
    ).order_by(AghHistoricoPresenca.data_hora_evento.desc()).limit(10).all()

    logs = [
        {
            'data': h.data_hora_evento.strftime('%d/%m/%Y %H:%M'),
            'evento': h.tipo_evento,
            'observacao': h.motivo
        }
        for h in historico
    ]

    total_concluidos = AghHistoricoPresenca.query.filter_by(
        cliente_id=cliente_id, tipo_evento='conclusao_atendimento'
    ).count()

    total_cancelamentos = AghHistoricoPresenca.query.filter_by(
        cliente_id=cliente_id, tipo_evento='cancelamento_cliente'
    ).count()

    return jsonify({
        'sucesso': True,
        'nome_cliente': getattr(cliente, 'nome', 'Cliente'),
        'total_concluidos': total_concluidos,
        'total_cancelamentos': total_cancelamentos,
        'historico_observacoes': logs
    }), 200