"""
==========================================================================================
📌 MÓDULO AGENDA: CONSULTAS ANALÍTICAS E CONCENTRADOR DE QUERIES (queries.py)
==========================================================================================
Este arquivo isola as rotinas de busca, relatórios e agregações do módulo Agenda.
Garante que o routes.py permaneça enxuto e focado apenas no fluxo da requisição HTTP.
==========================================================================================
"""

from datetime import datetime, time
from sqlalchemy import func, case, extract
from feedin import database as db
from feedin.modules.agenda.models import AghAgendamento


def buscar_metricas_diarias(empresa_id: int, data_consulta: datetime, profissional_id: int = None) -> dict:
    """
    📊 MOTOR DE ANALYTICS: MÉTRICAS RÁPIDAS E INDICADORES DIÁRIOS DE ATENDIMENTO
    --------------------------------------------------------------------------------------
    Calcula em tempo real, via agregações SQL otimizadas (func.count/func.sum), os totais
    de agendamentos por status do dia (Total, Concluídos, Pendentes e Cancelados).
    Elimina a necessidade de tabelas físicas de agregados, garantindo consistência absoluta.
    """
    inicio_dia = data_consulta.replace(hour=0, minute=0, second=0)
    fim_dia = data_consulta.replace(hour=23, minute=59, second=59)

    query = db.session.query(
        func.count(AghAgendamento.id).label('total'),
        func.sum(case((AghAgendamento.status == 'concluido', 1), else_=0)).label('concluidos'),
        func.sum(case((AghAgendamento.status.in_(['confirmado', 'pendente', 'soft_lock']), 1), else_=0)).label('pendentes'),
        func.sum(case((AghAgendamento.status == 'cancelado', 1), else_=0)).label('cancelados')
    ).filter(
        AghAgendamento.empresa_id == empresa_id,
        AghAgendamento.data_hora_inicio >= inicio_dia,
        AghAgendamento.data_hora_inicio <= fim_dia
    )

    if profissional_id:
        query = query.filter(AghAgendamento.profissional_id == profissional_id)

    res = query.one()

    # O int(res.X or 0) garante o casting correto de tipos retornados pelo SUM no Postgres/MySQL
    return {
        "total": int(res.total or 0),
        "concluidos": int(res.concluidos or 0),
        "pendentes": int(res.pendentes or 0),
        "cancelados": int(res.cancelados or 0)
    }


def buscar_agendamentos_dia(empresa_id: int, data_str: str, profissional_id: int = None) -> list:
    """
    📅 MOTOR DA GRADE: CONSULTA E ESTRUTURAÇÃO DE AGENDAMENTOS DIÁRIOS
    --------------------------------------------------------------------------------------
    Busca os agendamentos do dia ordenados por horário e resolve corretamente as pontes
    com ModCadastroCliente, AghAgendamentoItem -> AghServico e ColaboradorContrato.
    """
    try:
        data_consulta = datetime.strptime(data_str, '%Y-%m-%d').date()
    except (ValueError, TypeError):
        data_consulta = datetime.now().date()

    inicio_dia = datetime.combine(data_consulta, time.min)
    fim_dia = datetime.combine(data_consulta, time.max)

    # Consulta base filtrada por empresa e intervalo de datas do dia
    query = db.session.query(AghAgendamento).filter(
        AghAgendamento.empresa_id == empresa_id,
        AghAgendamento.data_hora_inicio >= inicio_dia,
        AghAgendamento.data_hora_inicio <= fim_dia
    )

    # Filtro opcional por profissional
    if profissional_id and str(profissional_id).strip() not in ('', 'None', '0'):
        query = query.filter(AghAgendamento.profissional_id == int(profissional_id))

    # Ordenação cronológica dos atendimentos
    agendamentos = query.order_by(AghAgendamento.data_hora_inicio.asc()).all()

    resultado = []
    for ag in agendamentos:
        # 1. Resolução do Cliente (ModCadastroCliente / UUID)
        cliente_nome = "Cliente Balcão"
        if ag.cliente:
            cliente_nome = getattr(ag.cliente, 'nome_completo', ag.cliente.nome)

        # 2. Resolução do Serviço (AghAgendamentoItem -> AghServico)
        servico_nome = "Atendimento"
        if ag.itens:
            itens_ordenados = sorted(ag.itens, key=lambda x: x.ordem_execucao or 1)
            nomes_servicos = [
                item.servico.nome for item in itens_ordenados
                if item.servico and hasattr(item.servico, 'nome')
            ]
            if nomes_servicos:
                servico_nome = ", ".join(nomes_servicos)

        # 3. Resolução do Profissional (ColaboradorContrato via property @nome)
        profissional_nome = "Equipe"
        if ag.profissional:
            profissional_nome = ag.profissional.nome

        # 4. Extração e Formatação dos Valores
        valor_final = float(ag.valor_total) if ag.valor_total is not None else 0.0

        resultado.append({
            "id": ag.id,
            "hora": ag.data_hora_inicio.strftime('%H:%M') if ag.data_hora_inicio else '--:--',
            "hora_fim": ag.data_hora_fim.strftime('%H:%M') if ag.data_hora_fim else None,
            "cliente_nome": cliente_nome,
            "servico_nome": servico_nome,
            "profissional_nome": profissional_nome,
            "status": ag.status or "soft_lock",
            "valor": valor_final
        })

    return resultado