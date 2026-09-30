"""
Rotas e Endpoints da Central Universal de Cobranças
"""

import hashlib
from datetime import datetime
from flask import jsonify, render_template, request, session, url_for
from feedin import database as db
from . import billing_bp
from .models import Cobranca, CobrancaPagamento


@billing_bp.route('/api/criar', methods=['POST'])
def criar_cobranca():
    """Cria ou recupera uma comanda para qualquer origem."""
    data = request.get_json(silent=True) or {}

    origem_tipo = data.get('origem_tipo', 'AGENDAMENTO')
    origem_id = data.get('origem_id')
    empresa_id = data.get('empresa_id') or session.get('empresa_id', 1)
    cliente_id = data.get('cliente_id')
    valor_liquido = float(data.get('valor_liquido', 0.0))

    if not origem_id or not empresa_id:
        return jsonify({'sucesso': False, 'mensagem': 'Parâmetros obrigatórios ausentes.'}), 400

    cobranca = Cobranca.query.filter_by(origem_tipo=origem_tipo, origem_id=origem_id).first()

    if not cobranca:
        codigo = f"COB-{datetime.now().strftime('%Y%m%d')}-{origem_id}"
        cobranca = Cobranca(
            codigo_transacao=codigo,
            empresa_id=empresa_id,
            cliente_id=cliente_id,
            origem_tipo=origem_tipo,
            origem_id=origem_id,
            valor_bruto=valor_liquido,
            valor_liquido=valor_liquido,
            status='PENDENTE'
        )
        db.session.add(cobranca)
        db.session.commit()

    return jsonify({
        'sucesso': True,
        'cobranca_id': cobranca.id,
        'uuid': cobranca.uuid_cobranca,
        'codigo': cobranca.codigo_transacao,
        'valor_liquido': float(cobranca.valor_liquido),
        'status': cobranca.status
    }), 200


@billing_bp.route('/api/liquidar-manual', methods=['POST'])
def liquidar_manual():
    """Liquida total ou parcialmente uma comanda."""
    data = request.get_json(silent=True) or {}

    agendamento_id = data.get('agendamento_id')
    cobranca_id = data.get('cobranca_id')
    valor_pago = float(data.get('valor_final') or data.get('valor_pago') or 0.0)
    forma_pagto = data.get('forma_pagamento', 'PIX')
    observacao = str(data.get('observacao', '')).strip()
    cpf_cnpj = data.get('cpf_cnpj')

    cobranca = None
    if cobranca_id:
        cobranca = Cobranca.query.get(cobranca_id)
    elif agendamento_id:
        cobranca = Cobranca.query.filter_by(origem_tipo='AGENDAMENTO', origem_id=agendamento_id).first()

    agora = datetime.now()

    try:
        if not cobranca and agendamento_id:
            codigo = f"COB-{agora.strftime('%Y%m%d')}-{agendamento_id}"
            cobranca = Cobranca(
                codigo_transacao=codigo,
                empresa_id=session.get('empresa_id', 1),
                origem_tipo='AGENDAMENTO',
                origem_id=agendamento_id,
                valor_bruto=valor_pago,
                valor_liquido=valor_pago,
                status='PENDENTE'
            )
            db.session.add(cobranca)
            db.session.flush()

        if not cobranca:
            return jsonify({'sucesso': False, 'mensagem': 'Cobrança não encontrada.'}), 404

        if cobranca.status == 'PAGO':
            return jsonify({'sucesso': False, 'mensagem': 'Esta cobrança já está quitada.'}), 400

        if cpf_cnpj:
            cobranca.cpf_cnpj_fiscal = cpf_cnpj
            cobranca.cpf_hash = hashlib.sha256(cpf_cnpj.encode('utf-8')).hexdigest()
            cobranca.exige_nota_fiscal = True

        novo_pagamento = CobrancaPagamento(
            cobranca_id=cobranca.id,
            forma_pagamento=forma_pagto,
            valor_pago=valor_pago,
            operacao_tipo='MANUAL',
            operador_usuario_id=session.get('usuario_id'),
            observacao=observacao,
            status='CONFIRMADO',
            data_pagamento=agora
        )
        db.session.add(novo_pagamento)

        novo_status = cobranca.recalcular_saldos()

        # Callback de desacoplamento: se for Agendamento e quitou, finaliza a agenda
        if novo_status == 'PAGO' and cobranca.origem_tipo == 'AGENDAMENTO':
            from feedin.modules.agenda.models import Agendamento  # Import pontual para evitar dependência cíclica
            agendamento = Agendamento.query.get(cobranca.origem_id)
            if agendamento:
                agendamento.status = 'finalizado'
                if not getattr(agendamento, 'fim_real', None):
                    agendamento.fim_real = agora

        db.session.commit()

        url_comprovante = url_for('billing.comprovante', uuid_cobranca=cobranca.uuid_cobranca, _external=True)

        return jsonify({
            'sucesso': True,
            'mensagem': 'Pagamento registrado com sucesso.',
            'cobranca_id': cobranca.id,
            'uuid': cobranca.uuid_cobranca,
            'status_cobranca': cobranca.status,
            'url_comprovante': url_comprovante
        }), 200

    except Exception as e:
        db.session.rollback()
        return jsonify({'sucesso': False, 'mensagem': f'Erro interno: {str(e)}'}), 500


@billing_bp.route('/comprovante/<uuid_cobranca>', methods=['GET'])
def comprovante(uuid_cobranca):
    """Renderiza o comprovante dinâmico."""
    cobranca = Cobranca.query.filter_by(uuid_cobranca=uuid_cobranca).first_or_404()

    dados = {
        'codigo': cobranca.codigo_transacao,
        'data': cobranca.data_quitacao.strftime('%d/%m/%Y %H:%M') if cobranca.data_quitacao else cobranca.data_emissao.strftime('%d/%m/%Y %H:%M'),
        'cpf_cnpj': cobranca.cpf_cnpj_fiscal or 'Não informado',
        'pagamentos': [
            {'forma': p.forma_pagamento, 'valor': float(p.valor_pago)} for p in cobranca.pagamentos if p.status == 'CONFIRMADO'
        ],
        'total': float(cobranca.valor_pago),
        'status': cobranca.status
    }

    return render_template('billing/comprovante.html', c=dados)