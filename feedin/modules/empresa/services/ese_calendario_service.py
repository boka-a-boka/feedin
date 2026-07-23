from datetime import time, datetime, timedelta, date
from typing import Optional

from feedin.modules.empresa.models import EseHorarioFuncionamento, EscalaTrabalhoColaborador

def obter_escala_padrao(empresa_id: str, contrato_id: Optional[str], data_consulta: date):
    """
    Retorna o registro de escala padrão (empresa ou colaborador) referente
    ao dia da semana da data consultada.
    """
    # 0 = Segunda-feira, 6 = Domingo
    dia_semana = data_consulta.weekday()

    # 1. Se informou colaborador, busca a escala individual
    if contrato_id:
        escala_colaborador = EscalaTrabalhoColaborador.query.filter_by(
            contrato_id=contrato_id,
            dia_semana=dia_semana,
            ativo=True
        ).first()

        if escala_colaborador:
            return escala_colaborador

    # 2. Fallback: busca a escala padrão da empresa
    escala_empresa = EseHorarioFuncionamento.query.filter_by(
        empresa_id=empresa_id,
        dia_semana=dia_semana,
        ativo=True
    ).first()

    return escala_empresa


def subtrair_intervalo_horario(
        janelas_normais: list[tuple[time, time]],
        bloqueio: tuple[time, time]
) -> list[tuple[time, time]]:
    """
    Subtrai uma faixa de bloqueio (exceção parcial) das janelas de horários disponíveis.

    Exemplo:
        janelas_normais = [(08:00, 12:00), (13:00, 18:00)]
        bloqueio = (14:00, 17:00)
        Retorno = [(08:00, 12:00), (13:00, 14:00), (17:00, 18:00)]
    """
    bloq_inicio, bloq_fim = bloqueio
    if not bloq_inicio or not bloq_fim or bloq_inicio >= bloq_fim:
        return janelas_normais

    novas_janelas = []

    for jan_inicio, jan_fim in janelas_normais:
        if not jan_inicio or not jan_fim:
            continue

        # Caso 1: O bloqueio está totalmente fora da janela (antes ou depois) -> Mantém janela
        if bloq_fim <= jan_inicio or bloq_inicio >= jan_fim:
            novas_janelas.append((jan_inicio, jan_fim))

        # Caso 2: O bloqueio cobre/envelopa totalmente a janela -> Janela é removida
        elif bloq_inicio <= jan_inicio and bloq_fim >= jan_fim:
            continue

        # Caso 3: Interseção parcial ou divisão no meio da janela
        else:
            # Sobra um pedaço ANTES do bloqueio?
            if bloq_inicio > jan_inicio:
                novas_janelas.append((jan_inicio, bloq_inicio))

            # Sobra um pedaço DEPOIS do bloqueio?
            if bloq_fim < jan_fim:
                novas_janelas.append((bloq_fim, jan_fim))

    return novas_janelas