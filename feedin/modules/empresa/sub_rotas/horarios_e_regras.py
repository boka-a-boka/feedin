from flask import render_template, redirect, url_for, request, flash
from flask_login import login_required, current_user
from datetime import datetime
from feedin import database as db
from feedin.modules.empresa import empresa_bp
from feedin.modules.empresa.models import (
    EseEmpresa,
    EseHorarioFuncionamento,
    EmpresaCalendarioExcecao
)

@empresa_bp.route('/configurar-horarios-estabelecimento/<int:empresa_id>', methods=['GET', 'POST'], endpoint='configurar_horarios_empresa')
@login_required
def configurar_horarios_empresa(empresa_id):
    """⏱️ Grade de funcionamento tradicional do estabelecimento (Isolado)."""
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
            flash("⏱️ Horário de funcionamento atualizado com sucesso!", "success")
            return redirect(url_for('empresa.configurar_horarios_empresa', empresa_id=ese_empresa.id))

        except Exception as e:
            db.session.rollback()
            flash(f"🚨 Erro ao salvar horários: {str(e)}", "danger")

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


@empresa_bp.route('/parametros-operacao/<int:empresa_id>', methods=['GET'], endpoint='parametros_operacao')
@login_required
def parametros_operacao(empresa_id: int):
    """🎛️ Ambiente intermediário de parâmetros de operação (Isolado)."""
    ese_empresa = EseEmpresa.query.get_or_404(empresa_id)

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