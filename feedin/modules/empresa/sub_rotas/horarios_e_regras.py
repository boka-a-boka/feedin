import os, re, secrets
import uuid
import unicodedata
from urllib.parse import urlparse
from weasyprint import HTML
from flask import render_template, redirect, url_for, request, flash, current_app, session, jsonify, abort, make_response
from flask_login import login_required, current_user
from datetime import datetime, timezone, timedelta, time, date
from feedin import database as db
from feedin.modules.empresa import empresa_bp
from sqlalchemy.orm import joinedload, selectinload
from sqlalchemy import func, inspect
from decimal import Decimal, InvalidOperation
from functools import wraps

from feedin.modules.empresa.models import (
    EseEmpresa,
    EseConviteColaborador,
    EseHorarioFuncionamento,
    EmpresaCalendarioExcecao,
    EseExcecaoCalendario,
    EseOrigemExcecaoEnum,
    EseTipoExcecaoEnum,
    EseProcessoClaim,
    EseServicoOferecido,
    EseServicoPreco,
    EseGrupoTabela,
    EseServicoPrecoHistorico,
    ColaboradorContrato,
    ColaboradorDetalhesPessoais,
    EscalaTrabalhoColaborador,
    EseColaboradorServicoHabilidade,
    EmpresaRecesso,
    CadastroFeriado,
    CadastroEfemeride,
    ModEmpresaModulo,
    ModEmpresaModuloLog,
)

from feedin.models import  Local, Cargo, Taxonomia, ModulosSistema, CoreDepartamento, CoreAcaoPendente

from feedin.modules.auth.models import ModCadastroCliente, VinculoUsuarioEmpresa, ModVinculoModulo

from feedin.modules.agenda.models import AghAgendamento

from feedin.modules.estoque.services.estoque_service import (
    validar_acesso_empresa,
    registrar_entrada_estoque,
    registrar_baixa_operacional
)

from feedin.modules.empresa.services.ese_calendario_service import obter_escala_padrao, subtrair_intervalo_horario
from feedin.modules.empresa.security import verificar_acesso_modulo

from feedin.modules.estoque.models import ProdutoCatalogo, EntradaEstoqueItem

from feedin.utils import salvar_imagem_modulo, validar_limite_modulo

# 🌍 CAMINHO RAIZ DO MÓDULO EMPRESA (Independente de ordem de importação)
EMPRESA_MODULO_DIR = os.path.dirname(os.path.abspath(__file__))
EMPRESA_UPLOAD_BASE_DIR = os.path.join(EMPRESA_MODULO_DIR, 'static', 'uploads')

def is_safe_url(target):
    if not target:
        return False
    ref_url = urlparse(request.host_url)
    test_url = urlparse(target)
    return test_url.scheme in ('', 'http', 'https') and (not test_url.netloc or test_url.netloc == ref_url.netloc)

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


def pode_solicitar_degustacao(empresa_id, modulo_slug):
    # Verifica no histórico se já houve ALGUMA degustação anterior para este módulo
    historico_degustacao = ModEmpresaModuloLog.query.filter_by(
        empresa_modulo_id=...,  # ou via join com empresa/módulo
        acao='solicitacao_degustacao'
    ).first()

    if historico_degustacao:
        return False, "Esta empresa já utilizou o período de degustação deste módulo anteriormente. Por favor, selecione o plano Pro."
    return True, "Elegível"


def processar_solicitacao_degustacao(empresa_id, modulo_slug, ip_cliente):
    """
    Processa a adesão à degustação de um módulo.
    Se o módulo for 'agenda', concede/sincroniza automaticamente a Licença PRO do Módulo Empresa
    com vínculo de dependência (Origem: Agenda).
    """
    try:
        agora = datetime.now(timezone.utc)
        vencimento = agora + timedelta(days=30)

        # ---------------------------------------------------------------------
        # 1. ATIVAÇÃO DO MÓDULO SOLICITADO (EX: AGENDA)
        # ---------------------------------------------------------------------
        modulo_empresa = ModEmpresaModulo.query.filter_by(
            empresa_id=empresa_id,
            modulo_slug=modulo_slug
        ).first()

        status_anterior = modulo_empresa.status_homologacao if modulo_empresa else None
        plano_anterior = modulo_empresa.tipo_plano if modulo_empresa else None

        if not modulo_empresa:
            modulo_empresa = ModEmpresaModulo(
                empresa_id=empresa_id,
                modulo_slug=modulo_slug
            )
            db.session.add(modulo_empresa)

        modulo_empresa.ativo = True
        modulo_empresa.tipo_plano = 'degustacao'
        modulo_empresa.status_homologacao = 'homologado'
        modulo_empresa.data_contratacao = agora
        modulo_empresa.data_vencimento = vencimento

        modulo_empresa.termo_aceito = True
        modulo_empresa.data_aceite = agora
        modulo_empresa.ip_aceite = ip_cliente

        # Limites operacionais do plano Degustação da Agenda
        modulo_empresa.limite_profissionais = 3
        modulo_empresa.limite_clientes = 12

        db.session.flush()

        # Log de auditoria da Agenda
        db.session.add(ModEmpresaModuloLog(
            empresa_modulo_id=modulo_empresa.id,
            acao='solicitacao_degustacao_com_aceite',
            tipo_plano_anterior=plano_anterior,
            tipo_plano_novo='degustacao',
            status_anterior=status_anterior,
            status_novo='homologado',
            ip_origem=ip_cliente,
            detalhes=f"Degustação de 30 dias ativada. Vencimento em {vencimento.strftime('%d/%m/%Y')}."
        ))

        # ---------------------------------------------------------------------
        # 2. REGRA DE VÍNCULO: CONCESSÃO DO PERFIL PRO DO MÓDULO EMPRESA
        # ---------------------------------------------------------------------
        if modulo_slug == 'agenda':
            modulo_base_pro = ModEmpresaModulo.query.filter_by(
                empresa_id=empresa_id,
                modulo_slug='empresa_pro'
            ).first()

            if not modulo_base_pro:
                modulo_base_pro = ModEmpresaModulo(
                    empresa_id=empresa_id,
                    modulo_slug='empresa_pro'
                )
                db.session.add(modulo_base_pro)

            # Concede o Perfil PRO com vínculo explícito à Degustação da Agenda
            plano_base_ant = modulo_base_pro.tipo_plano
            status_base_ant = modulo_base_pro.status_homologacao

            modulo_base_pro.ativo = True
            modulo_base_pro.tipo_plano = 'degustacao_vinculada_agenda'
            modulo_base_pro.status_homologacao = 'homologado'
            modulo_base_pro.data_contratacao = agora
            modulo_base_pro.data_vencimento = vencimento  # Vence rigorosamente junto com a Agenda
            modulo_base_pro.termo_aceito = True
            modulo_base_pro.data_aceite = agora
            modulo_base_pro.ip_aceite = ip_cliente

            db.session.flush()

            # Log de auditoria da concessão PRO
            db.session.add(ModEmpresaModuloLog(
                empresa_modulo_id=modulo_base_pro.id,
                acao='concessao_pro_via_agenda',
                tipo_plano_anterior=plano_base_ant,
                tipo_plano_novo='degustacao_vinculada_agenda',
                status_anterior=status_base_ant,
                status_novo='homologado',
                ip_origem=ip_cliente,
                detalhes=f"Licença Empresa PRO concedida temporariamente via Degustação do Módulo Agenda. Vínculo até {vencimento.strftime('%d/%m/%Y')}."
            ))

        db.session.commit()
        return True, "Módulo Agenda ativado em degustação! A licença Empresa PRO foi concedida temporariamente ao seu perfil."

    except Exception as e:
        db.session.rollback()
        return False, f"Erro ao processar solicitação: {str(e)}"


def processar_renovacao_modulo(modulo_empresa, dias_contratados):
    """
    Aplica a regra de renovação inteligente baseada na vigência atual.
    """
    agora = datetime.now(timezone.utc)

    # Define a nova data de vencimento com base na sua regra
    if not modulo_empresa.data_vencimento or agora > modulo_empresa.data_vencimento:
        # Se venceu ou nunca teve vencimento, conta a partir de hoje
        nova_data_vencimento = agora + timedelta(days=dias_contratados)
    else:
        # Se ainda está no prazo, soma em cima da data de vencimento atual
        nova_data_vencimento = modulo_empresa.data_vencimento + timedelta(days=dias_contratados)

    # Atualiza os dados do contrato
    modulo_empresa.data_vencimento = nova_data_vencimento
    modulo_empresa.ativo = True  # <--- Renovou, acende a luz verde de novo!

    db.session.commit()
    return nova_data_vencimento

def verificar_prontidao_modulo(empresa_id, modulo_slug):
    """
    Semáforo Mestre Universal: Valida o acesso da empresa a qualquer módulo,
    independente da modalidade (Degustação, Mensal, Trimestral, Semestral, Anual, etc.).
    """
    modulo = ModEmpresaModulo.query.filter_by(
        empresa_id=empresa_id,
        modulo_slug=modulo_slug,
        ativo=True
    ).first()

    if not modulo:
        return False, "Este estabelecimento não possui registro ativo para este módulo."

    if modulo.status_homologacao != 'homologado':
        return False, "Este módulo está com pendências de homologação ou bloqueado pela administração."

    # Validação Temporal Universal (Validade de qualquer plano: mensal, anual, etc.)
    if modulo.data_vencimento:
        agora = datetime.now(timezone.utc)
        if agora > modulo.data_vencimento:
            # Aqui podemos diferenciar a mensagem pelo plano para orientar o lojista
            if modulo.tipo_plano == 'degustacao':
                return False, "O seu período de degustação expirou. Faça o upgrade para um plano Pro."
            else:
                return False, f"O plano contratado ({modulo.tipo_plano}) venceu em {modulo.data_vencimento.strftime('%d/%m/%Y')}. Regularize sua assinatura para continuar."

    return True, "Acesso autorizado"


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


# =====================================================================
# 🛠️ FUNÇÃO DE SERVIÇO: processar_gravacao_excecao (Isolada)
# =====================================================================
def processar_gravacao_excecao(empresa_id: str, contrato_id: str | None, form_data: dict) -> EseExcecaoCalendario:
    """
    Processa, valida e persiste registros de exceção de calendário e recessos
    tanto para a Empresa (quando contrato_id for None) quanto para um Colaborador.
    """
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
        empresa_id=str(empresa_id),
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


# =====================================================================
# 🗓️ ROTAS DE EXCEÇÕES E CALENDÁRIO (Isoladas)
# =====================================================================

@empresa_bp.route('/<int:empresa_id>/excecoes', methods=['GET', 'POST'], endpoint='excecoes_empresa')
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
        empresa_id=str(empresa_id),
        contrato_id=None,
        ativo=True
    ).order_by(EseExcecaoCalendario.data_inicio.asc()).all()

    return render_template(
        'empresa/excecoes_calendario.html',
        empresa=empresa,
        ese_empresa=empresa,
        contrato=None,
        lista_excecoes=lista_excecoes,
        datetime=datetime
    )


@empresa_bp.route('/<int:empresa_id>/colaborador/<int:contrato_id>/excecoes', methods=['GET', 'POST'], endpoint='excecoes_colaborador')
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

    return render_template(
        'empresa/excecoes_calendario.html',
        empresa=empresa,
        ese_empresa=empresa,
        contrato=contrato,
        lista_excecoes=lista_excecoes,
        datetime=datetime
    )


@empresa_bp.route('/<int:empresa_id>/calendario/salvar-excecao', methods=['POST'], endpoint='salvar_excecao')
def salvar_excecao(empresa_id):
    """
    Cadastra ou atualiza uma exceção de funcionamento para uma empresa específica.
    """
    excecao_id = request.form.get('excecao_id')

    # 📥 Captura o período completo (Início e Fim)
    data_inicio_str = request.form.get('data_inicio') or request.form.get('data')
    data_fim_str = request.form.get('data_fim') or data_inicio_str

    motivo_titulo = request.form.get('motivo_titulo') or request.form.get('motivo') or 'Exceção / Recesso'
    justificativa = request.form.get('justificativa')

    trabalha_raw = request.form.get('trabalha')
    trabalha = trabalha_raw in ['1', 'true', 'True', 'on', True]

    hora_inicio_str = request.form.get('hora_inicio_excecao') or request.form.get(
        'inicio_expediente') or request.form.get('hora_inicio')
    hora_fim_str = request.form.get('hora_fim_excecao') or request.form.get('fim_expediente') or request.form.get(
        'hora_fim')

    try:
        if not data_inicio_str:
            raise ValueError("Data inicial obrigatória ausente.")

        data_inicio_conv = datetime.strptime(data_inicio_str, '%Y-%m-%d').date()
        data_fim_conv = datetime.strptime(data_fim_str, '%Y-%m-%d').date() if data_fim_str else data_inicio_conv

        if data_fim_conv < data_inicio_conv:
            data_fim_conv = data_inicio_conv

        hora_inicio = datetime.strptime(hora_inicio_str.strip(), '%H:%M').time() if hora_inicio_str else None
        hora_fim = datetime.strptime(hora_fim_str.strip(), '%H:%M').time() if hora_fim_str else None

    except (ValueError, TypeError) as e:
        print(f"⚠️ [SALVAR EXCEÇÃO] Erro de conversão: {e}")
        flash('Data ou horários inválidos fornecidos para a exceção.', 'danger')
        return redirect(url_for('empresa.gerenciar_calendario', empresa_id=empresa_id))

    if excecao_id:
        # 🔄 MODO EDIÇÃO (Atualiza o registro pontual)
        excecao = EmpresaCalendarioExcecao.query.filter_by(id=excecao_id, empresa_id=empresa_id).first()
        if excecao:
            excecao.data_excecao = data_inicio_conv
            excecao.trabalha_no_dia = trabalha
            excecao.horario_abertura_excecao = hora_inicio
            excecao.horario_fechamento_excecao = hora_fim
            if hasattr(excecao, 'motivo_titulo'):
                excecao.motivo_titulo = motivo_titulo
            if hasattr(excecao, 'justificativa'):
                excecao.justificativa = justificativa

            flash('Exceção de funcionamento atualizada com sucesso!', 'success')
        else:
            flash('Erro: Exceção não encontrada para atualização.', 'danger')
    else:
        # 🆕 MODO CRIAÇÃO (Percorre e salva cada dia do intervalo no banco)
        delta_dias = (data_fim_conv - data_inicio_conv).days
        total_salvo = 0

        for i in range(delta_dias + 1):
            dia_corrente = data_inicio_conv + timedelta(days=i)

            # Evita duplicatas exatas para o mesmo dia na mesma empresa
            existente = EmpresaCalendarioExcecao.query.filter_by(
                empresa_id=empresa_id,
                data_excecao=dia_corrente
            ).first()

            if existente:
                # Atualiza se já existir para o dia
                existente.trabalha_no_dia = trabalha
                existente.horario_abertura_excecao = hora_inicio
                existente.horario_fechamento_excecao = hora_fim
                if hasattr(existente, 'motivo_titulo'):
                    existente.motivo_titulo = motivo_titulo
                if hasattr(existente, 'justificativa'):
                    existente.justificativa = justificativa
            else:
                # Cria novo registro para o dia
                nova_exc = EmpresaCalendarioExcecao(
                    empresa_id=empresa_id,
                    data_excecao=dia_corrente,
                    trabalha_no_dia=trabalha,
                    horario_abertura_excecao=hora_inicio,
                    horario_fechamento_excecao=hora_fim
                )
                if hasattr(nova_exc, 'motivo_titulo'):
                    nova_exc.motivo_titulo = motivo_titulo
                if hasattr(nova_exc, 'justificativa'):
                    nova_exc.justificativa = justificativa

                db.session.add(nova_exc)

            total_salvo += 1

        flash(f'Exceção de funcionamento salva com sucesso ({total_salvo} dia(s) processado(s))!', 'success')

    # 💾 Confirmação real no banco de dados
    db.session.commit()
    return redirect(url_for('empresa.gerenciar_calendario', empresa_id=empresa_id))


@empresa_bp.route('/<int:empresa_id>/calendario/salvar-recesso', methods=['POST'], endpoint='salvar_recesso')
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


@empresa_bp.route('/<int:empresa_id>/calendario/salvar-feriado', methods=['POST'], endpoint='salvar_feriado')
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


# ==========================================
# 🎈 GESTÃO DE EFEMÉRIDES (EDIÇÃO E EXCLUSÃO)
# ==========================================

@empresa_bp.route('/<int:empresa_id>/calendario/efemeride/editar/<int:efemeride_id>', methods=['POST'], endpoint='editar_efemeride')
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


@empresa_bp.route('/<int:empresa_id>/calendario/salvar-efemeride', methods=['POST'], endpoint='salvar_efemeride')
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


@empresa_bp.route('/<int:empresa_id>/calendario/efemeride/excluir/<int:efemeride_id>', methods=['POST'],  endpoint='excluir_efemeride')
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


@empresa_bp.route('/<int:empresa_id>/modulos-vitrine', methods=['GET'], endpoint='menu_modulos_vitrine')
@login_required
def menu_modulos_vitrine(empresa_id):
    """
    Vitrine do Ecossistema: Exibe os 3 Pilares do FeedIn (Plano Base, Mídia e Módulos Operacionais).
    Integra a checagem de nível de acesso (Proprietário vs. Colaborador vs. Degustação).
    """
    # ---------------------------------------------------------------------
    # 1. RESOLUÇÃO DE PROCESSO, LOCAL E EMPRESA (FONTE DA VERDADE)
    # ---------------------------------------------------------------------
    processo = EseProcessoClaim.query.filter(
        (EseProcessoClaim.id == empresa_id) |
        (EseProcessoClaim.local_id == empresa_id)
    ).order_by(EseProcessoClaim.id.desc()).first()

    if processo and processo.local:
        local_alvo = processo.local
        real_local_id = processo.local_id
    else:
        local_alvo = Local.query.get_or_404(empresa_id)
        real_local_id = local_alvo.id

    ese_empresa = EseEmpresa.query.filter_by(local_id=real_local_id).first()
    real_empresa_id = ese_empresa.id if ese_empresa else None

    # ---------------------------------------------------------------------
    # 2. VALIDAÇÃO DE SEGURANÇA E PERMISSÃO VIA CONTRATO / PROPRIEDADE
    # ---------------------------------------------------------------------
    user_uuid = str(getattr(current_user, 'id', '')).strip()
    user_core_id = getattr(current_user, 'usuario_id', getattr(current_user, 'id', None))

    # A) Checagem via Processo Claim
    eh_solicitante_claim = processo and (
        str(processo.usuario_solicitante_id) == str(user_core_id) or
        str(processo.usuario_solicitante_id) == user_uuid
    )

    # B) Checagem via Proprietário Direto da Empresa
    eh_proprietario_empresa = False
    if ese_empresa and ese_empresa.proprietario_id:
        eh_proprietario_empresa = (
            str(ese_empresa.proprietario_id) == str(user_core_id) or
            str(ese_empresa.proprietario_id) == user_uuid
        )

    # C) Checagem via Tabela de Contratos (ColaboradorContrato - UUID CHAR(36))
    contrato_usuario = ColaboradorContrato.query.filter(
        ColaboradorContrato.id_cadastro_cliente == user_uuid,
        ColaboradorContrato.id_local == (real_empresa_id or real_local_id),
        ColaboradorContrato.status_profissional == 'ativo'
    ).first()

    eh_colaborador = contrato_usuario is not None
    papel_nivel = contrato_usuario.papel_nivel if contrato_usuario else 0

    eh_dono = eh_solicitante_claim or eh_proprietario_empresa or (papel_nivel >= 999)
    eh_admin = getattr(current_user, 'is_admin', False)

    if not (eh_dono or eh_colaborador or eh_admin):
        current_app.logger.warning(
            f"[VITRINE] Acesso negado. Usuário UUID '{user_uuid}' tentou acessar Módulos do Local #{real_local_id}."
        )
        flash("Acesso não autorizado a este estabelecimento.", "danger")
        return redirect(url_for('empresa.dashboard_empresa'))

    # ---------------------------------------------------------------------
    # 3. VERIFICAÇÃO DE RECURSOS E REGRAS DA AGENDA
    # ---------------------------------------------------------------------
    base_empresa_pronta = (processo.status_processo == 'concluido') if processo else True
    tem_acesso_pro = ese_empresa.possui_recurso_pro() if (ese_empresa and hasattr(ese_empresa, 'possui_recurso_pro')) else False

    slugs_contratados = []
    if ese_empresa:
        adesoes_ativas = ModEmpresaModulo.query.filter_by(empresa_id=ese_empresa.id, ativo=True).all()
        slugs_contratados = [a.modulo_slug for a in adesoes_ativas]

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
        base_empresa_pronta=base_empresa_pronta,
        papel_nivel=papel_nivel,
        eh_dono=eh_dono,
        eh_colaborador=eh_colaborador
    )

@empresa_bp.route('/<int:empresa_id>/ativar-modulo/<string:modulo_slug>', methods=['POST', 'GET'], endpoint='ativar_modulo_empresa')
@login_required
def ativar_modulo_empresa(empresa_id, modulo_slug):
    """
    Recebe a solicitação de degustação/ativação feita pelo lojista na vitrine,
    captura o IP de origem e dispara o motor atômico de processamento.
    """
    # 1. Mapeia a empresa real (caso o ID passado seja o do local ou da empresa)
    ese_empresa = EseEmpresa.query.filter(
        (EseEmpresa.id == empresa_id) | (EseEmpresa.local_id == empresa_id)
    ).first_or_404()

    # 2. Captura o IP do cliente para fins de auditoria no log
    ip_cliente = request.remote_addr or '0.0.0.0'

    # 3. Dispara a função atômica de degustação que construímos
    sucesso, mensagem = processar_solicitacao_degustacao(
        empresa_id=ese_empresa.id,
        modulo_slug=modulo_slug,
        ip_cliente=ip_cliente
    )

    if sucesso:
        flash(mensagem, "success")
    else:
        flash(mensagem, "danger")

    # 4. Retorna para a vitrine de módulos atualizada
    return redirect(url_for('empresa.menu_modulos_vitrine', empresa_id=ese_empresa.id))

@empresa_bp.route('/<int:empresa_id>/menu-servicos-precos', methods=['GET'], endpoint='menu_servicos_precos')
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

@empresa_bp.route('/<int:empresa_id>/equipe-jornadas', methods=['GET'], endpoint='configurar_jornadas_tela')
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

# -----------------------------------------------------------------------------
# 📦 HUB INTERMEDIÁRIO DE ESTOQUE
# -----------------------------------------------------------------------------
@empresa_bp.route('/<int:empresa_id>/estoque/hub', methods=['GET'], endpoint='estoque_hub_tela')
@login_required
def estoque_hub_tela(empresa_id):
    ese_empresa = EseEmpresa.query.get_or_404(empresa_id)

    if not validar_acesso_empresa(current_user, ese_empresa):
        flash("Acesso não autorizado ao módulo da empresa.", "danger")
        return redirect(url_for('empresa.dashboard_empresa', empresa_id=empresa_id))

    if not ese_empresa.possui_recurso_pro():
        flash("O controle de estoque é um recurso do plano PRO.", "warning")
        return redirect(url_for('empresa.hub_ativacao', empresa_id=empresa_id))

    total_produtos = ProdutoCatalogo.query.filter_by(empresa_id=ese_empresa.id).count()

    current_app.jinja_env.cache = {}  # Desativa o cache do Jinja em ambiente de desenvolvimento

    return render_template('empresa/estoque_hub.html',
                           ese_empresa=ese_empresa,
                           total_produtos=total_produtos)

@empresa_bp.route('/<int:empresa_id>/configurar-layout', methods=['GET', 'POST'], endpoint='configurar_layout')
@login_required
def configurar_layout(empresa_id):
    """
    Tela de configuração de layout e identidade visual da empresa no Hub.
    Governança: ID Soberano 1:1 (Empresa ID == Local ID == empresa_id).
    """
    # ---------------------------------------------------------------------
    # 1. RESOLUÇÃO SOBERANA DOS REGISTROS (ID 1:1 ESTRITO)
    # ---------------------------------------------------------------------
    local = Local.query.get_or_404(empresa_id)

    # Busca direta e determinística pelo ID soberano
    ese_empresa = EseEmpresa.query.filter_by(id=empresa_id).first()

    # Busca segura do processo de claim vinculado estritamente ao local
    processo = EseProcessoClaim.query.filter_by(local_id=empresa_id).order_by(EseProcessoClaim.id.desc()).first()

    # ---------------------------------------------------------------------
    # FUNÇÃO AUXILIAR: GARANTIR CONTRATO DO PROPRIETÁRIO (1:1 SOBERANO)
    # ---------------------------------------------------------------------
    def garantir_contrato_proprietario():
        cliente_uuid = str(current_user.id)

        contrato = ColaboradorContrato.query.filter_by(
            id_cadastro_cliente=cliente_uuid,
            id_local=empresa_id
        ).first()

        if not contrato:
            cargo_proprietario = Cargo.query.get(1)
            if not cargo_proprietario:
                cargo_proprietario = Cargo(id=1, nome_cargo="PROPRIETARIO MASTER")
                db.session.add(cargo_proprietario)
                db.session.flush()

            contrato = ColaboradorContrato(
                id_cadastro_cliente=cliente_uuid,
                id_local=empresa_id,
                id_cargo=cargo_proprietario.id,
                papel_nome='proprietario',
                papel_nivel=999,
                status_profissional='ativo',
                hora_inicio_expediente=time(8, 0),
                hora_fim_expediente=time(18, 0)
            )
            db.session.add(contrato)
            current_app.logger.info(
                f"[ONBOARDING] ColaboradorContrato (Nível 999) criado para cliente {cliente_uuid} no local {empresa_id}."
            )

    # Criar a EseEmpresa caso não exista no primeiro acesso
    if not ese_empresa:
        nome_inicial = request.form.get('nome') or getattr(local, 'nome', None) or f"Empresa {empresa_id}"
        uuid_pleiteante = str(getattr(current_user, 'id', ''))

        ese_empresa = EseEmpresa(
            id=empresa_id,  # ID Soberano perfeitamente alinhado
            proprietario_id=uuid_pleiteante,
            local_id=local.id,
            nome=nome_inicial,
            slug=sanitizar_slug(nome_inicial),
            status_homologacao='aguardando_dados',
            termo_responsabilidade_aceito=True
        )
        db.session.add(ese_empresa)
        db.session.flush()

        if processo:
            processo.status_processo = 'preenchimento_dados'
            db.session.add(processo)

        garantir_contrato_proprietario()
        db.session.commit()

    # ---------------------------------------------------------------------
    # TRATAMENTO DE POST (SALVAR CONFIGURAÇÕES)
    # ---------------------------------------------------------------------
    if request.method == 'POST':
        try:
            # A) Atualização de Dados Básicos
            novo_nome = request.form.get('nome', '').strip()
            if novo_nome:
                ese_empresa.nome = novo_nome
                slug_base = sanitizar_slug(novo_nome)

                # Garante unicidade do slug (evita conflito se já existir em outro registro)
                slug_candidato = slug_base
                slug_existente = EseEmpresa.query.filter(
                    EseEmpresa.slug == slug_candidato,
                    EseEmpresa.id != ese_empresa.id
                ).first()

                if slug_existente:
                    slug_candidato = f"{slug_base}-{ese_empresa.id}"

                ese_empresa.slug = slug_candidato

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

            # D) Exclusão Explícita de Mídia (Long-press)
            if request.form.get('excluir_logo') == 'true' and ese_empresa.logomarca:
                caminho_logo = os.path.join(current_app.root_path, 'static', 'uploads', 'empresas', 'logos',
                                            ese_empresa.logomarca)
                if os.path.exists(caminho_logo):
                    try:
                        os.remove(caminho_logo)
                    except OSError:
                        pass
                ese_empresa.logomarca = None

            if request.form.get('excluir_fachada') == 'true' and ese_empresa.fachada:
                caminho_fachada = os.path.join(current_app.root_path, 'static', 'uploads', 'empresas', 'fachadas',
                                               ese_empresa.fachada)
                if os.path.exists(caminho_fachada):
                    try:
                        os.remove(caminho_fachada)
                    except OSError:
                        pass
                ese_empresa.fachada = None

            # E) Upload e Processamento da Logomarca
            file_logo = request.files.get('logo')
            if file_logo and getattr(file_logo, 'filename', ''):
                caminho_relativo_logo = salvar_imagem_modulo(
                    arquivo=file_logo,
                    tipo_midia='empresa_logo',
                    identificador=ese_empresa.id,
                    empresa_id=ese_empresa.id
                )
                if caminho_relativo_logo:
                    ese_empresa.logomarca = caminho_relativo_logo

            # F) Upload e Processamento da Fachada
            file_fachada = request.files.get('fachada')
            if file_fachada and getattr(file_fachada, 'filename', ''):
                caminho_relativo_fachada = salvar_imagem_modulo(
                    arquivo=file_fachada,
                    tipo_midia='empresa_fachada',
                    identificador=ese_empresa.id,
                    empresa_id=ese_empresa.id
                )
                if caminho_relativo_fachada:
                    ese_empresa.fachada = caminho_relativo_fachada

            # G) Validação do Domínio Próprio
            dominio = request.form.get('dominio_web')
            if dominio:
                try:
                    EseEmpresa.validar_dominio_proprio(dominio)
                    ese_empresa.dominio_web = dominio
                except ValueError as ve:
                    flash(str(ve), "warning")

            # H) Validação Obrigatória de Mídia Mínima (Removido o commit prematuro!)
            if not ese_empresa.logomarca and not ese_empresa.fachada:
                flash("Atenção: É obrigatório enviar ao menos a logomarca ou a foto da fachada para prosseguir.",
                      "warning")

                # Resolução dinâmica da categoria para retorno com erro
                categoria_nome = "Categoria Principal"
                if ese_empresa.categoria:
                    cat_obj = Taxonomia.query.get(ese_empresa.categoria)
                    if cat_obj:
                        categoria_nome = cat_obj.nome

                return render_template(
                    'empresa/onboarding.html',
                    ese_empresa=ese_empresa,
                    processo=processo,
                    local=local,
                    local_id=empresa_id,
                    categoria_nome=categoria_nome
                )

            # I) Transição de Status da Esteira de Onboarding
            ese_empresa.status_homologacao = 'layout_configurado'
            if processo:
                processo.status_processo = 'concluido'

            # Vínculo e Garantia do Contrato
            garantir_contrato_proprietario()

            # Persistência final unificada e segura
            db.session.add(ese_empresa)
            if processo:
                db.session.add(processo)
            db.session.commit()

            flash("Identidade visual e mídias salvas com sucesso!", "success")
            return redirect(url_for('empresa.hub_ativacao', empresa_id=empresa_id))

        except Exception as e:
            db.session.rollback()
            current_app.logger.error(f"Erro ao configurar layout da EseEmpresa (ID {empresa_id}): {e}")
            flash(f"Falha ao salvar informações: {str(e)}", "danger")

    # ---------------------------------------------------------------------
    # RESOLUÇÃO DINÂMICA DA CATEGORIA PARA RENDERIZAÇÃO NORMAL (GET)
    # ---------------------------------------------------------------------
    categoria_nome = "Categoria Principal"
    if ese_empresa and ese_empresa.categoria:
        cat_obj = Taxonomia.query.get(ese_empresa.categoria)
        if cat_obj:
            categoria_nome = cat_obj.nome

    return render_template(
        'empresa/onboarding.html',
        ese_empresa=ese_empresa,
        processo=processo,
        local=local,
        local_id=empresa_id,
        categoria_nome=categoria_nome
    )

@empresa_bp.route('/<int:empresa_id>/enviar-documentacao', methods=['GET', 'POST'], endpoint='enviar_documentacao')
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
            # 📌 Agora a estrutura contempla a pasta 'empresas' e logo em seguida o ID (ex: /empresas/576/documentos_auditoria)
            pasta_auditoria = os.path.join(EMPRESA_UPLOAD_BASE_DIR, "empresas", str(ese_empresa.id), "documentos_auditoria")
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
            ese_empresa.status_homologacao = 'aguardando_auditoria'
            if processo:
                processo.status_processo = 'aguardando_analise'

            # 🔔 INTEGRAÇÃO COM O BARRAMENTO DE NOTIFICAÇÕES DO CORE
            from feedin.modules.core.services.notificacao_core_service import CoreNotificacaoService

            # Busca o ID mestre do módulo 'empresa' na tabela modulos_sistema
            modulo_empresa = ModulosSistema.query.filter_by(slug='empresa').first()
            modulo_id = modulo_empresa.id if modulo_empresa else 1  # Fallback seguro

            CoreNotificacaoService.registrar_acao_e_notificar(
                modulo_id=modulo_id,
                entidade_id=ese_empresa.id,
                departamento_codigo='homologacao',
                titulo=f"Nova Empresa Aguardando Auditoria: {ese_empresa.nome}",
                descricao=f"O estabelecimento '{ese_empresa.nome}' (ID #{ese_empresa.id}) enviou a documentação e está pendente de análise humana.",
                payload={
                    'token_processo': processo.token_validacao if processo else None,
                    'responsavel_id': str(current_user.id)
                }
            )

            flash("Documentação enviada com sucesso! Etapa registrada e encaminhada para análise.",
                  "success")

            db.session.commit()
            return redirect(url_for('empresa.hub_ativacao', empresa_id=ese_empresa.id))

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

@empresa_bp.route('/<int:empresa_id>/servicos/cadastrar-listagem', methods=['GET'], endpoint='cadastro_servicos_listagem')
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


@empresa_bp.route('/<int:empresa_id>/servicos/excluir/<int:servico_id>', methods=['POST'], endpoint='excluir_servico_empresa')
def excluir_servico_empresa(empresa_id, servico_id):
    """
    Remove o serviço oferecido do portfólio da empresa.
    """
    servico = EseServicoOferecido.query.filter_by(id=servico_id, empresa_id=empresa_id).first_or_404()

    db.session.delete(servico)
    db.session.commit()

    flash("Serviço removido com sucesso da sua grade.", "info")
    return redirect(url_for('empresa.cadastro_servicos_listagem', empresa_id=empresa_id))


@empresa_bp.route('/<int:empresa_id>/servicos/editar/<int:servico_id>', methods=['POST'], endpoint='editar_servico_empresa')
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


@empresa_bp.route('/<int:empresa_id>/servicos/precos', methods=['GET'], endpoint='configurar_precos_listagem')
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

@empresa_bp.route('/<int:empresa_id>/servicos/cadastrar', methods=['POST'], endpoint='salvar_servico_empresa')
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

@empresa_bp.route('/<int:empresa_id>/servicos/precos/salvar', methods=['POST'], endpoint='salvar_preco_servico')
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

@empresa_bp.route('/<int:empresa_id>/vitrine', methods=['GET'], endpoint='vitrine_tv')
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

@empresa_bp.route('/<int:empresa_id>/servicos/tabela-impressao', methods=['GET'], endpoint='tabela_precos_impressao')
@login_required
def tabela_precos_impressao(empresa_id):
    """
    Renderiza a tabela de preços dinâmica (fundo preto, foco na logo,
    fonte proporcional ao maior texto e serviços com valor > 0).
    """
    empresa = EseEmpresa.query.get_or_404(empresa_id)

    try:
        servicos_oferecidos = EseServicoOferecido.query.filter_by(empresa_id=empresa_id).all()
        precos_dict = {p.taxonomia_id: p for p in EseServicoPreco.query.filter_by(empresa_id=empresa_id).all()}

        itens_tabela = []
        for servico in servicos_oferecidos:
            taxonomia_id = servico.taxonomia_id
            preco_obj = precos_dict.get(taxonomia_id)

            # Garante que tem preço válido e maior que zero
            if preco_obj and hasattr(preco_obj, 'preco') and preco_obj.preco > 0:
                nome_servico = None
                if servico.servico_taxonomia and hasattr(servico.servico_taxonomia, 'nome'):
                    nome_servico = servico.servico_taxonomia.nome

                if not nome_servico:
                    nome_servico = f"Serviço #{taxonomia_id}"

                itens_tabela.append({
                    'nome': nome_servico,
                    'valor': preco_obj.preco
                })

        # Ordenação A-Z pelo nome do serviço
        itens_tabela.sort(key=lambda x: x['nome'].lower())

    except Exception as e:
        print(f"Erro ao carregar tabela de preços impressao: {e}")
        itens_tabela = []

    return render_template(
        'empresa/tabela_precos_impressao.html',
        ese_empresa=empresa,
        itens_tabela=itens_tabela
    )

@empresa_bp.route('/precos/associar-grupo', methods=['POST'], endpoint='associar_grupo')
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

@empresa_bp.route('/api/taxonomia/buscar', methods=['GET'], endpoint='api_buscar_taxonomia')
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

# ==============================================================================
# PASSO 1: IDENTIFICAÇÃO CIVIL E GERAÇÃO DE LINK DE ONBOARDING
# ==============================================================================
@empresa_bp.route('/<int:empresa_id>/equipe/admitir', methods=['GET', 'POST'], endpoint='emitir_contrato_colaborador')
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


@empresa_bp.route('/<int:empresa_id>/equipe/contrato/<int:contrato_id>/documento', methods=['GET'], endpoint='visualizar_documento_contrato')
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


@empresa_bp.route('/central-convites', methods=['GET'], endpoint='central_convites')
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


@empresa_bp.route('/colaborador/efetivar-admissao/<int:contrato_id>', methods=['GET', 'POST'],
                  endpoint='efetivar_admissao')
@login_required
def efetivar_admissao(contrato_id):
    empresa_id = session.get('empresa_ativa_id')

    # PASSAMOS O CONTRATO_ID PARA O VALIDADOR SABER SE É APENAS EDIÇÃO DE ALGUÉM JÁ ATIVO
    liberado, aviso = validar_limite_modulo(empresa_id, 'agenda', 'profissionais', contrato_id_atual=contrato_id)
    if not liberado:
        flash(aviso, "warning")
        return redirect(url_for('agenda.listar_colaboradores'))

    contrato = ColaboradorContrato.query.get_or_404(contrato_id)

    # 🔍 RESOLUÇÃO PONTUAL DE DADOS DE CONTATO

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

            # 🗓️ 3. GRAVAÇÃO DA ESCALA DE TRABALHO (ESTRITAMENTE CHAR(36) / UUID)
            usuario_logado_id = None
            if current_user and current_user.is_authenticated:
                # 1. Tenta obter o ID do perfil modular associado (Char(36))
                if hasattr(current_user, 'cadastros_modulos'):
                    mod_perfil = current_user.cadastros_modulos.first()
                    if mod_perfil:
                        usuario_logado_id = str(mod_perfil.id)

                # 2. Se o próprio current_user já for um ID de 36 caracteres (UUID string)
                if not usuario_logado_id and hasattr(current_user, 'id') and isinstance(current_user.id, str) and len(
                        current_user.id) == 36:
                    usuario_logado_id = current_user.id

                # Se por ventura não encontrar um UUID válido de 36 caracteres, rejeitamos explicitamente o uso de inteiros
                if not usuario_logado_id:
                    current_app.logger.warning(
                        "[SEGURANÇA DB] Tentativa de gravar auditoria sem UUID Char(36) válido. Atribuindo None.")

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


def pode_gerenciar_empresa(empresa_id):
    """
    Verifica se o usuário atual (via current_user ou session) possui papel de gestão
    (papel_nivel >= 666) e contrato ativo no local/empresa especificado.
    """
    # Se o usuário for super admin global do sistema, libera direto
    if bool(getattr(current_user, 'is_admin', False)):
        return True

    # Identifica o UUID ou ID do usuário logado
    cliente_uuid = getattr(current_user, 'id', None) or session.get('id_cliente')
    usuario_id = getattr(current_user, 'id_usuario', None) or session.get('id_usuario')

    if not cliente_uuid and not usuario_id:
        return False

    # Busca o contrato na empresa solicitada
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

    # 🔒 ORDENAÇÃO DE SEGURANÇA: Prioriza contratos com maior papel_nivel e que estejam ativos
    contrato = query.order_by(ColaboradorContrato.papel_nivel.desc()).first()

    if not contrato:
        return False

    # Verifica se o status profissional está ativo (evita que ex-gestores mantenham acesso)
    if hasattr(contrato, 'status_profissional') and contrato.status_profissional != 'ativo':
        return False

    # Valida rigorosamente se o papel_nivel do contrato é maior ou igual a 666
    nivel = contrato.papel_nivel if contrato.papel_nivel is not None else 0
    return nivel >= 666

@empresa_bp.route('/<int:empresa_id>/equipe-jornadas', endpoint='equipe_jornadas')
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
        todas_escalas = EscalaTrabalhoColaborador.query.filter(
            EscalaTrabalhoColaborador.contrato_id.in_(contrato_ids)).all()
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


@empresa_bp.route('/colaborador/comprovante-pdf/<int:contrato_id>', methods=['GET'], endpoint='baixar_comprovante_pdf')
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
    empresa_obj = getattr(contrato, 'local_trabalho', None) or getattr(contrato, 'empresa', None) or getattr(contrato,
                                                                                                             'local',
                                                                                                             None)
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

@empresa_bp.route('/<int:empresa_id>/equipe/admitir-passo1/<int:contrato_id>', methods=['GET', 'POST'], endpoint='admitir_passo1')
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


@empresa_bp.route('/<int:empresa_id>/equipe/configurar-passo2/<int:contrato_id>', methods=['GET', 'POST'], endpoint='configurar_passo2')
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


@empresa_bp.route('/<int:empresa_id>/buscar-cargos', endpoint='buscar_cargos')
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


@empresa_bp.route('/<int:empresa_id>/contrato/<int:contrato_id>/cargo-habilidades', methods=['GET', 'POST'], endpoint='configurar_cargo_habilidades')
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
                return redirect(
                    url_for('empresa.configurar_cargo_habilidades', empresa_id=empresa.id, contrato_id=contrato.id))

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
                    db.session.add(
                        EseColaboradorServicoHabilidade(contrato_id=contrato.id, servico_oferecido_id=novo_s.id))
                    db.session.commit()
                    flash('Novo serviço cadastrado e atribuído ao colaborador!', 'success')
                return redirect(
                    url_for('empresa.configurar_cargo_habilidades', empresa_id=empresa.id, contrato_id=contrato.id))

        except Exception as e:
            db.session.rollback()
            flash(f'Erro ao salvar: {str(e)}', 'danger')

    # DADOS PARA O GET
    # 1. Nome garantido do colaborador
    colaborador_obj = getattr(contrato, 'colaborador', None) or getattr(contrato, 'cadastro_modulo', None)
    nome_colaborador = getattr(colaborador_obj, 'nome', None) or getattr(colaborador_obj, 'nome_razao',
                                                                         None) or f'Colaborador #{contrato.id}'

    # 2. Lista de todos os cargos para autocomplete
    cargos_existentes = [c.nome_cargo for c in Cargo.query.order_by(Cargo.nome_cargo.asc()).all()]

    # 3. Catálogo de serviços da empresa
    servicos_empresa = EseServicoOferecido.query.filter_by(empresa_id=empresa.id).all()

    # 4. Taxonomias globais para criação rápida de serviço
    taxonomias_globais = Taxonomia.query.filter_by(visivel_negocio=True).order_by(Taxonomia.nome.asc()).all()

    # 5. Habilidades já atribuídas ao contrato
    habilidades_atuais_ids = [h.servico_oferecido_id for h in
                              EseColaboradorServicoHabilidade.query.filter_by(contrato_id=contrato.id).all()]

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


@empresa_bp.route('/colaborador/<int:contrato_id>/perfil', methods=['GET'], endpoint='ver_perfil_colaborador')
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


@empresa_bp.route('/colaborador/<int:contrato_id>/atualizar-foto', methods=['POST'], endpoint='atualizar_foto_colaborador')
def atualizar_foto_colaborador(contrato_id):
    if 'foto' not in request.files:
        return jsonify(
            {'sucesso': False, 'mensagem': 'Nenhum arquivo enviado na requisição (chave "foto" ausente).'}), 400

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


@empresa_bp.route('/colaborador/<int:contrato_id>/salvar-foto-direta', methods=['POST'], endpoint='salvar_foto_direta')
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

@empresa_bp.route('/<int:empresa_id>/contrato/<int:contrato_id>/escala-semanal', methods=['GET', 'POST'], endpoint='configurar_escala_semanal')
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
        {'id': 2, 'nome': 'Terça-feira', 'sigla': 'TER'},
        {'id': 3, 'nome': 'Quarta-feira', 'sigla': 'QUA'},
        {'id': 4, 'nome': 'Quinta-feira', 'sigla': 'QUI'},
        {'id': 5, 'nome': 'Sexta-feira', 'sigla': 'SEX'},
        {'id': 6, 'nome': 'Sábado', 'sigla': 'SÁB'},
        {'id': 0, 'nome': 'Domingo', 'sigla': 'DOM'},
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
    nome_colaborador = getattr(colaborador_obj, 'nome', None) or getattr(colaborador_obj, 'nome_razao',
                                                                         None) or f'Colaborador #{contrato.id}'

    return render_template(
        'empresa/escala_semanal.html',
        ese_empresa=ese_empresa,
        contrato=contrato,
        nome_colaborador=nome_colaborador,
        grade_empresa_map=grade_empresa_map,
        escala_map=escala_map,
        dias_semana_map=dias_semana_map
    )


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

# -----------------------------------------------------------------------------
# 🏷️ CATÁLOGO DE PRODUTOS (CADASTRO E LISTAGEM)
# -----------------------------------------------------------------------------
@empresa_bp.route('/<int:empresa_id>/estoque/produtos', methods=['GET', 'POST'], endpoint='produto_catalogo_tela')  # 🔥 Removido o '/empresa' duplicado
@login_required
def produto_catalogo_tela(empresa_id):
    ese_empresa = EseEmpresa.query.get_or_404(empresa_id)

    if not validar_acesso_empresa(current_user, ese_empresa):
        flash("Acesso não autorizado ao módulo da empresa.", "danger")
        return redirect(url_for('empresa.dashboard_empresa', empresa_id=empresa_id))

    if not ese_empresa.possui_recurso_pro():
        flash("O controle de estoque é um recurso do plano PRO.", "warning")
        return redirect(url_for('empresa.hub_ativacao', empresa_id=empresa_id))

    if request.method == 'POST':
        nome = request.form.get('nome', '').strip()
        codigo_barras = request.form.get('codigo_barras', '').strip() or None
        preco_venda_raw = request.form.get('preco_venda', '0').strip().replace(',', '.')
        estoque_minimo_raw = request.form.get('estoque_minimo', '5').strip()

        if not nome:
            flash("O nome do produto é obrigatório.", "danger")
        else:
            try:
                preco_venda = Decimal(preco_venda_raw) if preco_venda_raw else Decimal('0.00')
                estoque_minimo = int(estoque_minimo_raw) if estoque_minimo_raw else 5

                novo_produto = ProdutoCatalogo(
                    empresa_id=ese_empresa.id,
                    nome=nome,
                    codigo_barras=codigo_barras,
                    preco_venda=preco_venda,
                    preco_custo=Decimal('0.00'),
                    estoque_atual=0,
                    estoque_minimo=estoque_minimo,
                    custo_medio=Decimal('0.00')
                )
                db.session.add(novo_produto)
                db.session.commit()
                flash(f"Produto '{nome}' cadastrado com sucesso!", "success")
            except (InvalidOperation, ValueError):
                db.session.rollback()
                flash("Preço de venda ou estoque mínimo com formato inválido.", "danger")
            except Exception as e:
                db.session.rollback()
                flash(f"Erro ao cadastrar produto: {str(e)}", "danger")

        return redirect(url_for('empresa.produto_catalogo_tela', empresa_id=empresa_id))

    # ------------------------------------------------------------------
    # RETORNO DO GET (Carregamento da Página com a Lista de Produtos)
    # ------------------------------------------------------------------
    produtos = ProdutoCatalogo.query.filter_by(empresa_id=ese_empresa.id) \
        .order_by(ProdutoCatalogo.nome.asc()) \
        .all()

    return render_template(
        'empresa/produto_catalogo.html',
        ese_empresa=ese_empresa,
        produtos=produtos
    )

@empresa_bp.route('/empresa/<int:empresa_id>/estoque/movimentos', methods=['GET', 'POST'], endpoint='movimento_estoque_tela')
@login_required
def movimento_estoque_tela(empresa_id):
    # 1. Consulta a entidade real no banco
    ese_empresa = EseEmpresa.query.get_or_404(empresa_id)

    # 2. Validação de acesso do usuário à empresa
    if not validar_acesso_empresa(current_user, ese_empresa):
        flash("Acesso não autorizado ao módulo da empresa.", "danger")
        return redirect(url_for('empresa.dashboard_empresa', empresa_id=empresa_id))

    # 3. Trava de Segurança PRO (Utilizando a regra de negócio da EseEmpresa)
    if not ese_empresa.possui_recurso_pro():
        flash("O controle de estoque é um recurso do plano PRO.", "warning")
        return redirect(url_for('empresa.hub_ativacao', empresa_id=empresa_id))

    # 4. Processamento de Formulários via POST
    if request.method == 'POST':
        tipo_acao = request.form.get('tipo_acao')  # 'entrada' ou 'baixa'

        if tipo_acao == 'entrada':
            sucesso, msg = registrar_entrada_estoque(ese_empresa.id, current_user.id, request.form)
        elif tipo_acao == 'baixa':
            sucesso, msg = registrar_baixa_operacional(ese_empresa.id, current_user.id, request.form)
        else:
            sucesso, msg = False, "Operação de estoque inválida."

        flash(msg, "success" if sucesso else "danger")
        return redirect(url_for('empresa.movimento_estoque_tela', empresa_id=empresa_id))

    # 5. Prepara os dados de leitura para a View
    produtos = ProdutoCatalogo.query.filter_by(empresa_id=ese_empresa.id) \
        .order_by(ProdutoCatalogo.nome.asc()) \
        .all()

    ultimas_entradas = EntradaEstoqueItem.query.filter_by(empresa_id=ese_empresa.id) \
        .order_by(EntradaEstoqueItem.data_compra.desc()) \
        .limit(10) \
        .all()

    return render_template('empresa/estoque_movimento.html',
                           ese_empresa=ese_empresa,
                           produtos=produtos,
                           ultimas_entradas=ultimas_entradas)

@empresa_bp.route('/<int:empresa_id>/termos-de-uso', methods=['GET'], endpoint='termos_de_uso')
@login_required
def termos_de_uso(empresa_id):
    ese_empresa = EseEmpresa.query.get_or_404(empresa_id)
    return render_template('empresa/termos_de_uso.html', ese_empresa=ese_empresa)

@empresa_bp.route('/<int:empresa_id>/lgpd', methods=['GET'], endpoint='lgpd_privacidade')
@login_required
def lgpd_privacidade(empresa_id):
    ese_empresa = EseEmpresa.query.get_or_404(empresa_id)
    return render_template('empresa/lgpd_privacidade.html', ese_empresa=ese_empresa)

@empresa_bp.route('/<int:empresa_id>/suporte', methods=['GET', 'POST'], endpoint='suporte_empreendedor')
@login_required
def suporte_empreendedor(empresa_id):
    ese_empresa = EseEmpresa.query.get_or_404(empresa_id)

    # 1. Carrega departamentos ativos para a lista de seleção
    departamentos = CoreDepartamento.query.filter_eq(ativo=True).order_by(CoreDepartamento.nome.asc()).all() if hasattr(
        CoreDepartamento.query, 'filter_eq') else CoreDepartamento.query.filter_by(ativo=True).order_by(
        CoreDepartamento.nome.asc()).all()

    if request.method == 'POST':
        departamento_id = request.form.get('departamento_id', type=int)
        mensagem = request.form.get('mensagem', '').strip()

        if not departamento_id or not mensagem:
            flash("Por favor, selecione um departamento e descreva sua necessidade.", "warning")
            return redirect(url_for('empresa.suporte_empreendedor', empresa_id=empresa_id))

        # Obter o módulo do sistema 'empresa' (ou fallback ID caso necessário)
        modulo_empresa = ModulosSistema.query.filter_by(slug='empresa').first()
        modulo_id = modulo_empresa.id if modulo_empresa else 1

        # 2. Registra a Ação Pendente para a Core Staff
        dept = CoreDepartamento.query.get(departamento_id)
        nova_acao = CoreAcaoPendente(
            modulo_id=modulo_id,
            entidade_id=ese_empresa.id,
            departamento_id=departamento_id,
            status='pendente',
            titulo=f"Solicitação de Suporte: {ese_empresa.nome}",
            descricao=mensagem,
            payload_dados={
                'origem': 'suporte_balcao',
                'empresa_id': ese_empresa.id,
                'empresa_nome': ese_empresa.nome,
                'solicitante_id': getattr(current_user, 'id', None),
                'solicitante_nome': getattr(current_user, 'nome', 'Usuário Balcão'),
                'departamento_codigo': dept.codigo if dept else None
            }
        )

        db.session.add(nova_acao)
        db.session.commit()

        flash("Sua mensagem foi enviada à equipe de suporte do FeedIn! Em breve você receberá o retorno.", "success")
        return redirect(url_for('empresa.suporte_empreendedor', empresa_id=empresa_id))

    return render_template(
        'empresa/suporte_empreendedor.html',
        ese_empresa=ese_empresa,
        departamentos=departamentos
    )