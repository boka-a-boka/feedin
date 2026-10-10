from flask import Blueprint, request, jsonify, current_app
from flask_login import login_required, current_user
from decimal import Decimal

from datetime import datetime
from feedin import database as db
from feedin.modules.pdv import pdv_bp
from feedin.modules.pdv.models import PdvComandaItem

# Importe os modelos dos outros domínios conforme a estrutura do seu projeto
from feedin.modules.billing.models import Cobranca  # fin_cobrancas
from feedin.modules.estoque.models import ProdutoCatalogo  # est_produtos

from feedin.modules.pdv.services.pdv_service import registrar_item_comanda_logic # ou de onde estiver a lógica do pdv]


@pdv_bp.route('/api/item/adicionar', methods=['POST'])
@login_required
def adicionar_item():
    """Endpoint unificado de adição de produto/serviço à comanda.
    - Suporta PRODUTO (com baixa de estoque) e SERVICO/AVULSO (sem baixa de estoque).
    - Localiza ou abre a comanda (fin_cobrancas) ativa do cliente.
    - Calcula o snapshot de comissão se houver colaborador_id.
    - Grava o item em pdv_comanda_itens e atualiza os totais da cobrança.
    """
    import sys
    import traceback

    try:
        data = request.get_json() or {}

        # 1. Extração de parâmetros
        tipo_item = data.get('tipo_item', 'PRODUTO').upper()
        produto_id = (
                data.get('hdnEstoqueProdutoId')
                or data.get('produto_id')
                or data.get('item_id')
        )
        agendamento_id = data.get('agendamento_id')
        cpf_hash = data.get('cpf_hash')

        # Extração e resolução do colaborador_id com fallback para o usuário logado
        colaborador_id = data.get('colaborador_id') or data.get('profissional_id')
        origem_lancamento = data.get(
            'origem_lancamento', 'COLABORADOR' if colaborador_id else 'AUTO_SERVICO'
        )

        if not colaborador_id and origem_lancamento == 'COLABORADOR':
            colaborador_id = getattr(current_user, 'colaborador_id', None) or getattr(
                current_user, 'id', None
            )

        empresa_id = data.get('empresa_id')
        quantidade = int(data.get('quantidade', 1))

        if quantidade <= 0:
            return jsonify({'error': 'A quantidade deve ser maior que zero.'}), 400

        produto = None
        preco_unitario = 0.0
        pct_comissao = 0.0

        # 2. Tratamento conforme o tipo de item (PRODUTO vs SERVICO/AVULSO)
        if tipo_item == 'PRODUTO':
            if not produto_id:
                return jsonify({'error': 'Produto não selecionado.'}), 400

            produto = ProdutoCatalogo.query.get(produto_id)
            if not produto:
                return jsonify({'error': 'Produto não localizado no estoque.'}), 404

            if produto.estoque_atual < quantidade:
                return (
                    jsonify({
                        'error': (
                            f'Estoque insuficiente para {produto.nome}. Saldo atual:'
                            f' {produto.estoque_atual}'
                        )
                    }),
                    400,
                )

            target_empresa_id = empresa_id or produto.empresa_id
            preco_unitario = float(produto.preco_venda or 0.0)
            pct_comissao = float(getattr(produto, 'percentual_comissao', 0.0) or 0.0)

            # Baixa física no estoque
            produto.estoque_atual -= quantidade

        else:
            # Lançamento de Serviço ou Item Avulso
            descricao = data.get('descricao', '').strip() or 'Serviço Avulso'
            valor_req = data.get('valor_unitario') or data.get('valor') or 0.0
            preco_unitario = float(valor_req)

            if preco_unitario <= 0:
                return (
                    jsonify(
                        {'error': 'Informe um valor válido para o serviço/item avulso.'}
                    ),
                    400,
                )

            target_empresa_id = empresa_id
            if not target_empresa_id and agendamento_id:
                cob_tmp = Cobranca.query.filter_by(
                    origem_tipo='AGENDAMENTO', origem_id=agendamento_id
                ).first()
                if cob_tmp:
                    target_empresa_id = cob_tmp.empresa_id

        # 3. Resolução da Comanda Ativa (fin_cobrancas)
        cobranca = None

        if agendamento_id:
            cobranca = Cobranca.query.filter_by(
                empresa_id=target_empresa_id,
                origem_tipo='AGENDAMENTO',
                origem_id=agendamento_id,
            ).first()

        if not cobranca and cpf_hash:
            hoje_inicio = datetime.now().replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            cobranca = Cobranca.query.filter(
                Cobranca.empresa_id == target_empresa_id,
                Cobranca.cpf_hash == cpf_hash,
                Cobranca.status == 'PENDENTE',
                Cobranca.data_emissao >= hoje_inicio,
            ).first()

        if not cobranca:
            codigo_transacao = f"CMD-{datetime.now().strftime('%Y%m%d%H%M%S')}"
            cobranca = Cobranca(
                empresa_id=target_empresa_id,
                cpf_hash=cpf_hash,
                origem_tipo='AGENDAMENTO' if agendamento_id else 'BALCAO',
                origem_id=agendamento_id or 0,
                codigo_transacao=codigo_transacao,
                valor_bruto=0.00,
                valor_desconto=0.00,
                valor_acrescimo=0.00,
                valor_liquido=0.00,
                valor_pago=0.00,
                status='PENDENTE',
            )
            db.session.add(cobranca)
            db.session.flush()

        # 4. Cálculo de Comissão e Totais
        valor_total_item = preco_unitario * quantidade
        val_comissao = (
            (valor_total_item * pct_comissao / 100.0)
            if (colaborador_id and pct_comissao > 0)
            else 0.0
        )

        # 5. Instanciação do Item da Comanda
        novo_item = PdvComandaItem(
            empresa_id=target_empresa_id,
            cobranca_id=cobranca.id,
            produto_id=produto.id if produto else None,
            colaborador_id=colaborador_id,
            percentual_comissao=pct_comissao,
            valor_comissao=val_comissao,
            quantidade=quantidade,
            preco_unitario=preco_unitario,
            valor_total=valor_total_item,
            origem_lancamento=origem_lancamento,
            cpf_hash_cliente=cpf_hash,
        )

        # Atualiza totais da comanda de forma segura
        cobranca.valor_bruto = float(cobranca.valor_bruto or 0.0) + valor_total_item
        cobranca.valor_liquido = (
                float(cobranca.valor_bruto)
                + float(cobranca.valor_acrescimo or 0.0)
                - float(cobranca.valor_desconto or 0.0)
        )

        db.session.add(novo_item)
        db.session.commit()

        # Serialização segura do item
        item_dict = novo_item.to_dict() if hasattr(novo_item, 'to_dict') else {
            'id': novo_item.id,
            'quantidade': novo_item.quantidade,
            'valor_total': novo_item.valor_total
        }

        return (
            jsonify({
                'status': 'sucesso',
                'sucesso': True,
                'message': 'Item adicionado à comanda!',
                'item': item_dict,
                'comanda': {
                    'cobranca_id': cobranca.id,
                    'codigo_transacao': cobranca.codigo_transacao,
                    'valor_bruto': float(cobranca.valor_bruto),
                    'valor_liquido': float(cobranca.valor_liquido),
                },
            }),
            200,
        )

    except Exception as e:
        db.session.rollback()
        import traceback
        tb_str = traceback.format_exc()
        sys.stderr.write("\n" + "!" * 70 + "\n")
        sys.stderr.write(f"[ERRO 500 NO PDV ADICIONAR ITEM]: {str(e)}\n")
        sys.stderr.write(tb_str)
        sys.stderr.write("!" * 70 + "\n")
        sys.stderr.flush()

        return jsonify({
            'success': False,
            'sucesso': False,
            'error': f'Erro interno no servidor: {str(e)}',
            'message': f'Erro interno no servidor: {str(e)}'
        }, 500)


@pdv_bp.route('/api/comanda/<int:cobranca_id>/itens', methods=['GET'])
def listar_itens_comanda(cobranca_id):
    """
    Retorna todos os itens vinculados a uma comanda/cobrança específica.
    """
    cobranca = Cobranca.query.get(cobranca_id)
    if not cobranca:
        return jsonify({'error': 'Comanda não localizada.'}), 404

    itens = PdvComandaItem.query.filter_by(cobranca_id=cobranca_id).all()
    itens_json = [item.to_dict() for item in itens]

    return jsonify({
        'status': 'sucesso',
        'cobranca_id': cobranca_id,
        'codigo_transacao': cobranca.codigo_transacao,
        'valor_bruto': float(cobranca.valor_bruto or 0.0),
        'valor_desconto': float(cobranca.valor_desconto or 0.0),
        'valor_acrescimo': float(cobranca.valor_acrescimo or 0.0),
        'valor_liquido': float(cobranca.valor_liquido or 0.0),
        'valor_pago': float(cobranca.valor_pago or 0.0),
        'status_cobranca': cobranca.status,
        'total_itens': len(itens_json),
        'itens': itens_json
    }), 200


@pdv_bp.route('/api/item/remover', methods=['POST'])
def remover_item():
    """
    Remove/Estorna um item da comanda.
    - Se for um PRODUTO, devolve a quantidade de volta ao estoque.
    - Recalcula o valor_bruto e valor_liquido da cobrança.
    """
    data = request.get_json() or {}
    item_id = data.get('item_id')

    if not item_id:
        return jsonify({'error': 'ID do item não informado.'}), 400

    item = PdvComandaItem.query.get(item_id)
    if not item:
        return jsonify({'error': 'Item da comanda não localizado.'}), 404

    cobranca = Cobranca.query.get(item.cobranca_id)

    # 1. Se for produto físico, estorna a quantidade no estoque
    if item.produto_id:
        produto = ProdutoCatalogo.query.get(item.produto_id)
        if produto:
            produto.estoque_atual += item.quantidade

    # 2. Subtrai o valor do item dos totais da comanda
    if cobranca:
        cobranca.valor_bruto = max(0.0, float(cobranca.valor_bruto or 0.0) - float(item.valor_total or 0.0))
        cobranca.valor_liquido = max(
            0.0,
            float(cobranca.valor_bruto)
            + float(cobranca.valor_acrescimo or 0.0)
            - float(cobranca.valor_desconto or 0.0)
        )

    # 3. Exclui o registro do item
    db.session.delete(item)
    db.session.commit()

    return jsonify({
        'status': 'sucesso',
        'sucesso': True,
        'message': 'Item removido da comanda e estoque estornado.',
        'comanda': {
            'cobranca_id': cobranca.id if cobranca else None,
            'valor_bruto': float(cobranca.valor_bruto) if cobranca else 0.0,
            'valor_liquido': float(cobranca.valor_liquido) if cobranca else 0.0
        }
    }), 200


@pdv_bp.route('/api/comanda/fechar', methods=['POST'])
def fechar_comanda():
    """
    Finaliza e quita a comanda/cobrança.
    - Recebe cobranca_id (ou agendamento_id).
    - Aplica desconto e acréscimo informados na tela de liquidação.
    - Registra a forma de pagamento (PIX, CARTAO_CREDITO, CARTAO_DEBITO, DINHEIRO, etc.).
    - Atualiza valor_pago, data_pagamento e status='PAGO'.
    """
    data = request.get_json() or {}

    cobranca_id = data.get('cobranca_id')
    agendamento_id = data.get('agendamento_id')

    cobranca = None
    if cobranca_id:
        cobranca = Cobranca.query.get(cobranca_id)
    elif agendamento_id:
        cobranca = Cobranca.query.filter_by(origem_tipo='AGENDAMENTO', origem_id=agendamento_id).first()

    if not cobranca:
        return jsonify({'error': 'Comanda/Cobrança não localizada para encerramento.'}), 404

    if cobranca.status == 'PAGO':
        return jsonify({'error': 'Esta comanda já foi finalizada e paga anteriormente.'}), 400

    # Extração de valores e ajustes
    desconto = float(data.get('valor_desconto') or 0.0)
    acrescimo = float(data.get('valor_acrescimo') or 0.0)
    forma_pagamento = data.get('forma_pagamento', 'PIX').upper()
    observacao = data.get('observacao', '').strip()

    valor_bruto = float(cobranca.valor_bruto or 0.0)
    valor_liquido = max(0.0, valor_bruto + acrescimo - desconto)
    valor_pago = float(data.get('valor_pago') or valor_liquido)

    if valor_pago < valor_liquido:
        return jsonify({
            'error': f'Valor pago (R$ {valor_pago:.2f}) é inferior ao líquido a pagar (R$ {valor_liquido:.2f}).'
        }), 400

    # Atualização da cobrança
    cobranca.valor_desconto = desconto
    cobranca.valor_acrescimo = acrescimo
    cobranca.valor_liquido = valor_liquido
    cobranca.valor_pago = valor_pago
    cobranca.forma_pagamento = forma_pagamento
    cobranca.status = 'PAGO'
    cobranca.data_pagamento = datetime.now()

    if hasattr(cobranca, 'observacao') and observacao:
        cobranca.observacao = observacao

    db.session.commit()

    return jsonify({
        'status': 'sucesso',
        'sucesso': True,
        'message': 'Comanda encerrada e quitada com sucesso!',
        'comanda': {
            'cobranca_id': cobranca.id,
            'codigo_transacao': cobranca.codigo_transacao,
            'valor_bruto': float(cobranca.valor_bruto),
            'valor_desconto': float(cobranca.valor_desconto),
            'valor_acrescimo': float(cobranca.valor_acrescimo),
            'valor_liquido': float(cobranca.valor_liquido),
            'valor_pago': float(cobranca.valor_pago),
            'troco': float(valor_pago - valor_liquido),
            'forma_pagamento': cobranca.forma_pagamento,
            'status': cobranca.status,
            'data_pagamento': cobranca.data_pagamento.strftime('%Y-%m-%d %H:%M:%S')
        }
    }), 200


@pdv_bp.route('/agenda/api/comanda/adicionar-item', methods=['POST'])
@login_required
def api_adicionar_item_comanda():
    try:
        dados = request.get_json()
        if not dados:
            return jsonify({'erro': 'Dados inválidos ou ausentes.'}), 400

        item_criado = registrar_item_comanda_logic(dados)

        return jsonify({
            'sucesso': True,
            'mensagem': 'Item lançado com sucesso no PDV!',
            'item': item_criado.to_dict()
        }), 200

    except ValueError as ve:
        db.session.rollback()
        return jsonify({'sucesso': False, 'erro': str(ve)}), 400
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Erro ao adicionar item na comanda: {str(e)}")
        return jsonify({'sucesso': False, 'erro': 'Erro interno ao processar requisição no servidor.'}), 500
