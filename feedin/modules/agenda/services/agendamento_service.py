from zoneinfo import ZoneInfo
from datetime import datetime, timedelta, timezone, date, time
from decimal import Decimal
from sqlalchemy import func, or_, and_

from feedin import database as db

# Modelos do Módulo Empresa
from feedin.modules.empresa.models import (
    ColaboradorContrato, EseExcecaoCalendario, EseServicoOferecido,
    EseColaboradorServicoHabilidade, EseHorarioFuncionamento,
    EscalaTrabalhoColaborador, EseNotificacao, EseRegraPontuacao, ModClientePontos,
)

# Modelos do Módulo Agenda (Duplicidade de AghOcorrenciaCliente removida)
from feedin.modules.agenda.models import (
    AghAgendamento, AghConfiguracaoAgenda, AghOcorrenciaCliente,
    AghSolicitacaoReagendamento, AghAgendamentoItem,
)

# Modelos Core
from feedin.models import ModulosSistema

# Fuso horário padrão do sistema
TZ_BRASIL = ZoneInfo('America/Sao_Paulo')


def _encontrar_colaborador_apto(empresa_id, servico_id, inicio_slot, fim_slot, ignorar_agendamento_id=None):
    """
    Localiza o primeiro colaborador ativo, habilitado para o serviço e com agenda
    livre no intervalo especificado [inicio_slot, fim_slot].
    """
    # 1. Busca colaboradores ativos na empresa com habilidade para este serviço
    contratos_aptos = db.session.query(ColaboradorContrato).join(
        EseColaboradorServicoHabilidade,
        EseColaboradorServicoHabilidade.contrato_id == ColaboradorContrato.id
    ).filter(
        ColaboradorContrato.id_local == empresa_id,
        ColaboradorContrato.status_profissional == 'ativo',
        EseColaboradorServicoHabilidade.servico_oferecido_id == servico_id
    ).all()

    if not contratos_aptos:
        # Fallback: Se não houver amarração de habilidade explícita, avalia todos os ativos
        contratos_aptos = ColaboradorContrato.query.filter_by(
            id_local=empresa_id,
            status_profissional='ativo'
        ).all()

    for colab in contratos_aptos:
        # 2. Verifica se o profissional já possui compromisso nesse intervalo de tempo
        query_conflito = db.session.query(AghAgendamentoItem).join(
            AghAgendamento, AghAgendamentoItem.agendamento_id == AghAgendamento.id
        ).filter(
            AghAgendamentoItem.profissional_id == colab.id,
            AghAgendamento.status != 'cancelado',
            AghAgendamentoItem.data_hora_inicio < fim_slot,
            AghAgendamentoItem.data_hora_fim > inicio_slot
        )

        if ignorar_agendamento_id:
            query_conflito = query_conflito.filter(AghAgendamento.id != ignorar_agendamento_id)

        if query_conflito.first() is None:
            return colab.id  # Retorna o ID do primeiro colaborador disponível

    return None


def obter_colaboradores_e_horarios_disponiveis(empresa_id, data_consulta, servico_ids=None, profissional_id=None):
    """
    Motor de Busca do FeedIn (Otimizado - Batch Querying na Memória):
    Determina os colaboradores aptos e os slots de horários livres para agendamento.

    Retorna uma tupla: (lista_horarios_formatados, dict_colaboradores_por_servico)
    """
    agora_local = datetime.now(TZ_BRASIL).replace(tzinfo=None)
    dia_semana_py = data_consulta.weekday()  # 0 = Segunda, 6 = Domingo

    # --------------------------------------------------------------------------
    # 1. FILTRO DE ELEGIBILIDADE INICIAL
    # --------------------------------------------------------------------------
    query_contratos = ColaboradorContrato.query.filter_by(
        id_local=empresa_id,
        status_profissional='ativo'
    )
    if profissional_id:
        query_contratos = query_contratos.filter_by(id=profissional_id)

    contratos_base = query_contratos.all()
    if not contratos_base:
        return [], {}

    contratos_dict = {c.id: c for c in contratos_base}
    contratos_ids = list(contratos_dict.keys())

    # Estrutura: { servico_id: [ColaboradorContrato, ...] }
    colaboradores_por_servico = {}

    if servico_ids:
        habilidades = EseColaboradorServicoHabilidade.query.filter(
            EseColaboradorServicoHabilidade.contrato_id.in_(contratos_ids),
            EseColaboradorServicoHabilidade.servico_oferecido_id.in_(servico_ids)
        ).all()

        for h in habilidades:
            if h.contrato_id in contratos_dict:
                colaboradores_por_servico.setdefault(h.servico_oferecido_id, []).append(contratos_dict[h.contrato_id])
    else:
        for c in contratos_base:
            colaboradores_por_servico.setdefault(0, []).append(c)

    # --------------------------------------------------------------------------
    # 2. CALCULAR DURAÇÃO TOTAL DOS SERVIÇOS
    # --------------------------------------------------------------------------
    duracao_total_min = 30
    if servico_ids:
        servicos = EseServicoOferecido.query.filter(
            EseServicoOferecido.id.in_(servico_ids),
            EseServicoOferecido.empresa_id == empresa_id
        ).all()
        if servicos:
            def _to_minutes(s):
                d = getattr(s, 'duracao_minutos', None) or getattr(s, 'tempo_duracao', None)
                if isinstance(d, int): return d
                if hasattr(d, 'hour'): return d.hour * 60 + d.minute
                return 30

            duracao_total_min = sum(_to_minutes(s) for s in servicos)

    duracao_delta = timedelta(minutes=duracao_total_min)
    passo_slot = timedelta(minutes=30)

    # --------------------------------------------------------------------------
    # 3. JANELA DE FUNCIONAMENTO DA EMPRESA
    # --------------------------------------------------------------------------
    janelas_empresa = []
    excecao_empresa = EseExcecaoCalendario.query.filter(
        EseExcecaoCalendario.empresa_id == empresa_id,
        EseExcecaoCalendario.contrato_id.is_(None),
        EseExcecaoCalendario.ativo == True,
        EseExcecaoCalendario.data_inicio <= data_consulta,
        or_(EseExcecaoCalendario.data_fim >= data_consulta, EseExcecaoCalendario.data_fim.is_(None))
    ).first()

    if excecao_empresa:
        if not excecao_empresa.trabalha:
            return [], {}
        elif excecao_empresa.inicio_expediente and excecao_empresa.fim_expediente:
            janelas_empresa.append((
                datetime.combine(data_consulta, excecao_empresa.inicio_expediente),
                datetime.combine(data_consulta, excecao_empresa.fim_expediente)
            ))

    if not janelas_empresa:
        horarios_empresa = EseHorarioFuncionamento.query.filter(
            EseHorarioFuncionamento.empresa_id == empresa_id,
            EseHorarioFuncionamento.dia_semana == dia_semana_py
        ).order_by(EseHorarioFuncionamento.horario_abertura).all()

        if not horarios_empresa:
            return [], {}

        for h in horarios_empresa:
            janelas_empresa.append((
                datetime.combine(data_consulta, h.horario_abertura),
                datetime.combine(data_consulta, h.horario_fechamento)
            ))

    # --------------------------------------------------------------------------
    # 4. CARREGAMENTO EM LOTE (BATCH LOADING EM MEMÓRIA)
    # --------------------------------------------------------------------------
    inicio_dia = datetime.combine(data_consulta, time.min)
    fim_dia = datetime.combine(data_consulta, time.max)

    # A) Exceções dos Colaboradores
    excecoes_colab = EseExcecaoCalendario.query.filter(
        EseExcecaoCalendario.empresa_id == empresa_id,
        EseExcecaoCalendario.contrato_id.in_(contratos_ids),
        EseExcecaoCalendario.ativo == True,
        EseExcecaoCalendario.data_inicio <= data_consulta,
        or_(EseExcecaoCalendario.data_fim >= data_consulta, EseExcecaoCalendario.data_fim.is_(None))
    ).all()
    mapa_excecoes = {e.contrato_id: e for e in excecoes_colab}

    # B) Escalas de Trabalho
    escalas = EscalaTrabalhoColaborador.query.filter(
        EscalaTrabalhoColaborador.contrato_id.in_(contratos_ids),
        EscalaTrabalhoColaborador.ativo == True,
        EscalaTrabalhoColaborador.data_inicio <= data_consulta,
        or_(EscalaTrabalhoColaborador.data_fim >= data_consulta, EscalaTrabalhoColaborador.data_fim.is_(None)),
        or_(EscalaTrabalhoColaborador.dia_semana == dia_semana_py, EscalaTrabalhoColaborador.dia_semana.is_(None))
    ).all()

    mapa_escalas = {}
    ordem_prioridade = {'emergencial': 1, 'alternativo': 2, 'padrao': 3}
    for esc in sorted(escalas, key=lambda e: ordem_prioridade.get(getattr(e, 'tipo_escala', 'padrao'), 99)):
        if esc.contrato_id not in mapa_escalas:
            mapa_escalas[esc.contrato_id] = esc

    # C) Agendamentos / Ocupações dos Profissionais
    itens_ocupados = db.session.query(AghAgendamentoItem).join(
        AghAgendamento, AghAgendamentoItem.agendamento_id == AghAgendamento.id
    ).filter(
        AghAgendamentoItem.profissional_id.in_(contratos_ids),
        AghAgendamentoItem.data_hora_inicio >= inicio_dia,
        AghAgendamentoItem.data_hora_inicio <= fim_dia,
        AghAgendamento.status != 'cancelado',
        or_(
            AghAgendamento.status != 'soft_lock',
            and_(AghAgendamento.status == 'soft_lock', AghAgendamento.expira_em > agora_local)
        )
    ).all()

    mapa_ocupacoes = {}
    for item in itens_ocupados:
        c_id = item.profissional_id
        if c_id not in mapa_ocupacoes:
            mapa_ocupacoes[c_id] = []
        if item.data_hora_inicio and item.data_hora_fim:
            mapa_ocupacoes[c_id].append((item.data_hora_inicio, item.data_hora_fim))

    # --------------------------------------------------------------------------
    # 5. DISPONIBILIDADE REAL E INTERSEÇÃO DE HORÁRIOS
    # --------------------------------------------------------------------------
    horarios_consolidados = set()

    for c_id in contratos_ids:
        excecao_colab = mapa_excecoes.get(c_id)
        if excecao_colab and not excecao_colab.trabalha and not excecao_colab.considera_horario:
            continue

        escala_vigente = mapa_escalas.get(c_id)
        if not escala_vigente:
            continue

        inicio_colab = datetime.combine(data_consulta, escala_vigente.inicio_expediente)
        fim_colab = datetime.combine(data_consulta, escala_vigente.fim_expediente)
        intervalo_inicio = datetime.combine(data_consulta, escala_vigente.inicio_intervalo) if escala_vigente.inicio_intervalo else None
        intervalo_fim = datetime.combine(data_consulta, escala_vigente.fim_intervalo) if escala_vigente.fim_intervalo else None

        bloqueios = list(mapa_ocupacoes.get(c_id, []))

        if excecao_colab and excecao_colab.considera_horario and excecao_colab.hora_inicio_excecao:
            bloqueios.append((
                datetime.combine(data_consulta, excecao_colab.hora_inicio_excecao),
                datetime.combine(data_consulta, excecao_colab.hora_fim_excecao)
            ))

        for emp_inicio, emp_fim in janelas_empresa:
            inicio_efetivo = max(emp_inicio, inicio_colab)
            fim_efetivo = min(emp_fim, fim_colab)

            atual = inicio_efetivo
            while atual + duracao_delta <= fim_efetivo:
                # Impede sugestão de horários passados no dia de hoje
                if data_consulta == agora_local.date() and atual <= agora_local:
                    atual += passo_slot
                    continue

                slot_inicio = atual
                slot_fim = atual + duracao_delta
                colisao = False

                if intervalo_inicio and intervalo_fim:
                    if slot_inicio < intervalo_fim and slot_fim > intervalo_inicio:
                        colisao = True

                if not colisao:
                    for b_inicio, b_fim in bloqueios:
                        if slot_inicio < b_fim and slot_fim > b_inicio:
                            colisao = True
                            break

                if not colisao:
                    horarios_consolidados.add(slot_inicio.strftime('%H:%M'))

                atual += passo_slot

    return sorted(list(horarios_consolidados)), colaboradores_por_servico


# --------------------------------------------------------------------------
# 🔔 HELPER DE ALERTA OPERACIONAL (Tabela `ese_notificacoes`)
# --------------------------------------------------------------------------
def registrar_alerta_falta_escala(empresa_id, data_consulta, motivo="escala"):
    """
    Registra notificação operacional na tabela ese_notificacoes quando um cliente
    tenta agendar em um dia sem disponibilidade de agenda/escala.
    """
    modulo_agenda = ModulosSistema.query.filter_by(slug="agenda", ativo=True).first()
    if not modulo_agenda:
        return

    data_fmt = data_consulta.strftime("%d/%m/%Y")
    data_iso = data_consulta.strftime("%Y-%m-%d")

    if motivo == "sem_colaboradores":
        titulo = f"Sem profissionais habilitados em {data_fmt}"
        mensagem = f"Um cliente tentou agendar para {data_fmt}, mas não há colaboradores ativos/habilitados para os serviços selecionados."
    elif motivo == "sem_horario_empresa":
        titulo = f"Sem expediente cadastrado em {data_fmt}"
        mensagem = f"Um cliente tentou agendar para {data_fmt}, mas a empresa não possui horário de funcionamento cadastrado para este dia."
    else:
        titulo = f"Sem escala em {data_fmt}"
        mensagem = f"Um cliente tentou agendar para {data_fmt}, mas nenhum colaborador habilitado possui escala ativa no dia."

    url_acao = f"/agenda/escalas?data={data_iso}"

    alerta_existente = EseNotificacao.query.filter_by(
        empresa_id=empresa_id,
        modulo_id=modulo_agenda.id,
        tipo="alerta_escala_vazia",
        data_referencia=data_consulta,
        lida=False
    ).first()

    if not alerta_existente:
        novo_alerta = EseNotificacao(
            modulo_id=modulo_agenda.id,
            modulo_slug=modulo_agenda.slug,
            empresa_id=empresa_id,
            destinatario_id=None,
            tipo="alerta_escala_vazia",
            categoria="operacional",
            titulo=titulo,
            mensagem=mensagem,
            url_acao=url_acao,
            data_referencia=data_consulta,
            lida=False,
            created_at=datetime.now(TZ_BRASIL)
        )
        db.session.add(novo_alerta)
        db.session.commit()


# --------------------------------------------------------------------------
# 🎯 PROCESSAMENTO DE PONTUALIDADE E CHECK-IN
# --------------------------------------------------------------------------
def processar_pontualidade_checkin(agendamento, data_hora_chegada):
    """
    Verifica se o cliente chegou no horário (ou dentro da tolerância) e credita
    os pontos correspondentes com base na regra ativa do estabelecimento.
    """
    if getattr(agendamento, 'elegivel_pontualidade', True) is False:
        return False

    regra = EseRegraPontuacao.query.filter_by(
        estabelecimento_id=agendamento.estabelecimento_id,
        gatilho_codigo='PONTUALIDADE',
        is_ativo=True
    ).first()

    if not regra:
        return False

    diferenca_minutos = (data_hora_chegada - agendamento.data_hora_inicio).total_seconds() / 60.0

    if diferenca_minutos <= regra.tolerancia_minutos:
        novo_credito = ModClientePontos(
            estabelecimento_id=agendamento.estabelecimento_id,
            cliente_id=agendamento.cliente_id,
            agendamento_id=agendamento.id,
            regra_id=regra.id,
            tipo_operacao='CREDITO',
            pontos=regra.pontos,
            descricao=f'Bônus de pontualidade - Agendamento #{agendamento.id}'
        )
        db.session.add(novo_credito)
        db.session.commit()
        return True

    return False


def processar_solicitacao_reagendamento(
    agendamento_atual: AghAgendamento,
    nova_data_hora: datetime,
    empresa,
    justificativa: str = None,
    is_colaborador: bool = False,
    novo_profissional_id: int = None,
    itens_servico: list = None
):
    """
    Processa a solicitação de reagendamento respeitando as regras de expiração específicas.
    """
    config = AghConfiguracaoAgenda.query.filter_by(empresa_id=empresa.id).first()

    minutos_antecedencia_empresa = config.antecedencia_minima_reagendamento_min if config else 120
    prazo_limite_dias = config.prazo_limite_reagendamento_dias if config else 7
    permitir_pos_horario = config.permitir_reagendamento_pos_horario if config else True

    agora_tz = datetime.now(TZ_BRASIL)
    agora_naive = agora_tz.replace(tzinfo=None)

    orig_inicio_naive = agendamento_atual.data_hora_inicio
    if orig_inicio_naive.tzinfo is not None:
        orig_inicio_naive = orig_inicio_naive.replace(tzinfo=None)

    agendamento_raiz = (
        AghAgendamento.query.get(agendamento_atual.agendamento_origem_id)
        if agendamento_atual.agendamento_origem_id
        else agendamento_atual
    )
    data_original_raiz = agendamento_raiz.data_hora_inicio.date()

    minutos_restantes = (orig_inicio_naive - agora_naive).total_seconds() / 60.0

    # Cálculo da data de expiração
    if minutos_restantes >= minutos_antecedencia_empresa:
        data_expiracao = nova_data_hora + timedelta(days=30)
    else:
        data_expiracao = agendamento_atual.data_hora_inicio + timedelta(days=prazo_limite_dias)

    # Fluxo do cliente
    if not is_colaborador:
        limite_30_dias = data_original_raiz + timedelta(days=30)
        if nova_data_hora.date() > limite_30_dias:
            return False, f'A nova data não pode exceder 30 dias da data do agendamento original ({limite_30_dias.strftime("%d/%m/%Y")}).', 'prazo_excedido'

        if orig_inicio_naive < agora_naive:
            if not permitir_pos_horario:
                return False, 'Esta empresa não permite reagendamentos após o horário agendado.', 'pos_horario_bloqueado'

            limite_carencia = orig_inicio_naive + timedelta(days=prazo_limite_dias)
            if agora_naive > limite_carencia:
                agendamento_atual.status = 'no_show_expirado'
                db.session.commit()
                return False, f'O prazo limite de {prazo_limite_dias} dias para reagendar este atendimento expirou.', 'expirado'

        if minutos_restantes < minutos_antecedencia_empresa:
            if not justificativa:
                return False, 'Reagendamentos com menos de 2h de antecedência exigem justificativa para análise.', 'requer_justificativa'

            solicitacao = AghSolicitacaoReagendamento(
                agendamento_id=agendamento_atual.id,
                nova_data=nova_data_hora.date(),
                novo_horario=nova_data_hora.time(),
                novo_profissional_id=novo_profissional_id or agendamento_atual.profissional_id,
                justificativa=justificativa,
                origem='cliente',
                fora_do_prazo=True,
                status_solicitacao='pendente',
                created_at=agora_tz
            )
            db.session.add(solicitacao)

            tipo_ocorrencia = 'REAGENDAMENTO_TARDIO' if minutos_restantes > 0 else 'NO_SHOW_REAGENDADO'
            ocorrencia = AghOcorrenciaCliente(
                cliente_id=agendamento_atual.cliente_id,
                empresa_id=empresa.id,
                agendamento_id=agendamento_atual.id,
                tipo_ocorrencia=tipo_ocorrencia,
                antecedencia_minutos=max(int(minutos_restantes), 0)
            )
            db.session.add(ocorrencia)

            db.session.commit()
            return True, 'Sua solicitação de reagendamento foi enviada para análise da equipe.', 'pendente_aprovacao'

    # Efetivação do agendamento
    try:
        if itens_servico:
            duracao_minutos_total = sum(i['duracao_minutos'] for i in itens_servico)
            duracao = timedelta(minutes=duracao_minutos_total)
        else:
            duracao = (
                agendamento_atual.data_hora_fim - agendamento_atual.data_hora_inicio
                if (agendamento_atual.data_hora_fim and agendamento_atual.data_hora_inicio)
                else timedelta(minutes=30)
            )

        agendamento_atual.status = 'reagendado'
        agendamento_atual.data_solicitacao_reagendamento = agora_tz

        novo_agendamento = AghAgendamento(
            empresa_id=empresa.id,
            cliente_id=agendamento_atual.cliente_id,
            beneficiario_id=agendamento_atual.beneficiario_id,
            profissional_id=novo_profissional_id or agendamento_atual.profissional_id,
            valor_total=agendamento_atual.valor_total,
            data_hora_inicio=nova_data_hora,
            data_hora_fim=nova_data_hora + duracao,
            agendamento_origem_id=agendamento_raiz.id,
            qtd_reagendamentos=(getattr(agendamento_atual, 'qtd_reagendamentos', 0) or 0) + 1,
            status='confirmado',
            tipo_origem='online',
            expira_em=data_expiracao,
            data_solicitacao_reagendamento=agora_tz,
            criado_em=agora_tz
        )
        db.session.add(novo_agendamento)
        db.session.flush()

        if itens_servico:
            for idx, item in enumerate(itens_servico, start=1):
                db.session.add(AghAgendamentoItem(
                    agendamento_id=novo_agendamento.id,
                    servico_id=item['servico_id'],
                    profissional_id=item.get('colaborador_id') or novo_agendamento.profissional_id,
                    preco_unitario=item['preco_unitario'],
                    duracao_minutos=item['duracao_minutos'],
                    ordem_execucao=idx
                ))
            novo_agendamento.recalcular_total()
        elif agendamento_atual.itens:
            for item_orig in agendamento_atual.itens:
                db.session.add(AghAgendamentoItem(
                    agendamento_id=novo_agendamento.id,
                    servico_id=item_orig.servico_id,
                    profissional_id=item_orig.profissional_id or novo_agendamento.profissional_id,
                    preco_unitario=item_orig.preco_unitario,
                    duracao_minutos=item_orig.duracao_minutos,
                    ordem_execucao=item_orig.ordem_execucao
                ))

        solicitacao_historico = AghSolicitacaoReagendamento(
            agendamento_id=agendamento_atual.id,
            novo_agendamento_id=novo_agendamento.id,
            nova_data=nova_data_hora.date(),
            novo_horario=nova_data_hora.time(),
            novo_profissional_id=novo_agendamento.profissional_id,
            justificativa=justificativa or ('Ajuste Operacional' if is_colaborador else 'Reagendamento no prazo.'),
            origem='colaborador' if is_colaborador else 'cliente',
            fora_do_prazo=(minutos_restantes < minutos_antecedencia_empresa),
            status_solicitacao='aprovada',
            analisado_em=agora_tz,
            created_at=agora_tz
        )
        db.session.add(solicitacao_historico)

        db.session.commit()
        return True, 'Agendamento reagendado com sucesso!', 'reagendado'

    except Exception as e:
        db.session.rollback()
        raise e


def montar_itens_agendamento(empresa_id, servicos_objs, data_hora_inicio, profissional_id_preferencial=None):
    """
    Monta a sequência de itens calculando horários e atribuindo o colaborador correto.
    """
    itens = []
    horario_cursor = data_hora_inicio

    for idx, srv in enumerate(servicos_objs, start=1):
        dur_min = getattr(srv, 'duracao_minutos', None) or getattr(srv, 'tempo_duracao', None) or 30

        colab_id = profissional_id_preferencial
        if not colab_id:
            _, colabs_aptos = obter_colaboradores_e_horarios_disponiveis(
                empresa_id=empresa_id,
                data_consulta=data_hora_inicio.date(),
                servico_ids=[srv.id]
            )
            colabs_lista = colabs_aptos.get(srv.id, [])
            colab_id = colabs_lista[0].id if colabs_lista else None

        fim_slot = horario_cursor + timedelta(minutes=dur_min)

        itens.append({
            'servico_id': srv.id,
            'colaborador_id': colab_id,
            'preco_unitario': float(getattr(srv, 'preco', 0.0) or 0.0),
            'duracao_minutos': dur_min,
            'ordem_execucao': idx,
            'data_hora_inicio': horario_cursor,
            'data_hora_fim': fim_slot
        })

        horario_cursor = fim_slot

    return itens


