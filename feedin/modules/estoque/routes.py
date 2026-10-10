from datetime import datetime, timezone
from flask import Blueprint, request, jsonify, session, current_app
from flask_login import login_required
from decimal import Decimal, InvalidOperation
from feedin import database as db
from feedin.modules.estoque.models import ProdutoCatalogo, EntradaEstoqueItem

# Importa a referência do blueprint criada no __init__.py local
from . import estoque_bp


@estoque_bp.route('/api/produto/cadastrar', methods=['POST'])
@login_required
def cadastrar_produto_rapido():
    """
    Cadastro rápido de produtos no estoque (com suporte a código de barras e estoque mínimo).
    """
    try:
        data = request.get_json(force=True, silent=True) or {}
        empresa_id = session.get('empresa_id') or data.get('empresa_id') or 2

        nome = str(data.get('nome', '')).strip()
        codigo_barras = str(data.get('codigo_barras', '')).strip() or None
        preco_venda = data.get('preco_venda') or data.get('preco')
        preco_custo = data.get('preco_custo', 0.00)
        estoque_atual = int(data.get('estoque_atual', 0))
        estoque_minimo = int(data.get('estoque_minimo', 5))

        if not nome or preco_venda is None:
            return jsonify({'sucesso': False, 'mensagem': 'Nome do produto e preço de venda são obrigatórios.'}), 400

        # Valida duplicidade de código de barras para a mesma empresa
        if codigo_barras:
            existente = ProdutoCatalogo.query.filter_by(codigo_barras=codigo_barras, empresa_id=empresa_id).first()
            if existente:
                return jsonify({
                    'sucesso': False,
                    'mensagem': f'O código de barras "{codigo_barras}" já pertence ao produto "{existente.nome}".'
                }), 400

        novo_produto = ProdutoCatalogo(
            empresa_id=empresa_id,
            nome=nome,
            codigo_barras=codigo_barras,
            preco_custo=float(preco_custo),
            preco_venda=float(preco_venda),
            estoque_atual=estoque_atual,
            estoque_minimo=estoque_minimo
        )

        db.session.add(novo_produto)
        db.session.commit()

        return jsonify({
            'sucesso': True,
            'mensagem': 'Produto cadastrado com sucesso no catálogo!',
            'produto_id': novo_produto.id,
            'nome': novo_produto.nome,
            'codigo_barras': novo_produto.codigo_barras
        }), 201

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"[ERRO CADASTRAR PRODUTO] {str(e)}")
        return jsonify({'sucesso': False, 'mensagem': f'Erro interno ao cadastrar produto: {str(e)}'}), 500


@estoque_bp.route('/api/produto/buscar-barras/<string:codigo_barras>', methods=['GET'])
@login_required
def buscar_por_codigo_barras(codigo_barras):
    """
    Busca produto no catálogo pelo código de barras para inclusão rápida na comanda.
    """
    try:
        empresa_id = session.get('empresa_id') or request.args.get('empresa_id', 2)
        codigo_limpo = str(codigo_barras).strip()

        produto = ProdutoCatalogo.query.filter_by(codigo_barras=codigo_limpo, empresa_id=empresa_id).first()

        if not produto:
            return jsonify({'sucesso': False, 'mensagem': 'Produto não encontrado para o código de barras informado.'}), 404

        return jsonify({
            'sucesso': True,
            'produto': {
                'id': produto.id,
                'nome': produto.nome,
                'codigo_barras': produto.codigo_barras,
                'preco_venda': float(produto.preco_venda),
                'estoque_atual': produto.estoque_atual
            }
        }), 200

    except Exception as e:
        current_app.logger.error(f"[ERRO BUSCA BARRAS] {str(e)}")
        return jsonify({'sucesso': False, 'mensagem': f'Erro ao pesquisar produto: {str(e)}'}), 500


@estoque_bp.route('/api/relatorios/estoque-critico', methods=['GET'])
@login_required
def relatorio_estoque_critico():
    """
    Gera a listagem de produtos com saldo igual ou abaixo do estoque mínimo.
    """
    try:
        empresa_id = session.get('empresa_id') or request.args.get('empresa_id', 2)

        produtos_criticos = ProdutoCatalogo.query.filter(
            ProdutoCatalogo.empresa_id == empresa_id,
            ProdutoCatalogo.estoque_atual <= ProdutoCatalogo.estoque_minimo
        ).order_by(ProdutoCatalogo.estoque_atual.asc()).all()

        resultado = [{
            'id': p.id,
            'nome': p.nome,
            'codigo_barras': p.codigo_barras,
            'estoque_atual': p.estoque_atual,
            'estoque_minimo': p.estoque_minimo,
            'deficit': p.estoque_minimo - p.estoque_atual,
            'preco_venda': float(p.preco_venda)
        } for p in produtos_criticos]

        return jsonify({
            'sucesso': True,
            'total_criticos': len(resultado),
            'itens': resultado
        }), 200

    except Exception as e:
        current_app.logger.error(f"[ERRO RELATORIO CRITICO] {str(e)}")
        return jsonify({'sucesso': False, 'mensagem': f'Erro ao gerar relatório: {str(e)}'}), 500


def registrar_entrada_estoque(empresa_id, usuario_id, form_data):
    """
    Registra uma nova compra/lote de estoque:
    - Busca o produto por ID ou Código de Barras
    - Grava/Atualiza o codigo_barras no ProdutoCatalogo caso informado
    - Grava o item em est_entradas_itens (Histórico)
    - Recalcula o custo_medio e atualiza o estoque_atual do ProdutoCatalogo
    """
    try:
        produto_id = form_data.get('produto_id', type=int)
        codigo_barras = form_data.get('codigo_barras', '').strip() or None
        quantidade = form_data.get('quantidade', type=int)
        preco_custo_str = form_data.get('preco_custo_unitario', '').replace(',', '.')
        fornecedor_nome = form_data.get('fornecedor_nome', '').strip() or None

        if not quantidade or quantidade <= 0:
            return False, "Quantidade inválida para entrada de estoque."

        preco_custo_unitario = Decimal(preco_custo_str)
        if preco_custo_unitario < 0:
            return False, "O preço de custo não pode ser negativo."

        # Busca o produto por ID ou por Código de Barras dentro da empresa
        produto = None
        if produto_id:
            produto = ProdutoCatalogo.query.filter_by(id=produto_id, empresa_id=empresa_id).first()
        elif codigo_barras:
            produto = ProdutoCatalogo.query.filter_by(codigo_barras=codigo_barras, empresa_id=empresa_id).first()

        if not produto:
            return False, "Produto não encontrado para este estabelecimento."

        # Se o código de barras veio na entrada e o produto ainda não tinha (ou foi atualizado), salva no catálogo
        if codigo_barras and produto.codigo_barras != codigo_barras:
            produto.codigo_barras = codigo_barras

        valor_total_compra = quantidade * preco_custo_unitario

        # 1. Registra o Lote no Histórico (Aqui é onde o preço fica registrado para consultas)
        nova_entrada = EntradaEstoqueItem(
            empresa_id=empresa_id,
            produto_id=produto.id,
            quantidade=quantidade,
            preco_custo_unitario=preco_custo_unitario,
            valor_total_compra=valor_total_compra,
            fornecedor_nome=fornecedor_nome,
            usuario_registro_id=usuario_id,
            data_compra=datetime.now(timezone.utc)
        )
        db.session.add(nova_entrada)

        # 2. Recálculo do Custo Médio Ponderado e Atualização de Saldo
        saldo_anterior = produto.estoque_atual or 0
        custo_medio_anterior = produto.custo_medio or Decimal('0.00')

        novo_saldo = saldo_anterior + quantidade

        if novo_saldo > 0:
            # Fórmula do Custo Médio Ponderado: (Saldo Antigo * Custo Médio Antigo + Valor da Nova Compra) / Novo Saldo
            valor_patrimonial_anterior = Decimal(saldo_anterior) * custo_medio_anterior
            novo_custo_medio = (valor_patrimonial_anterior + valor_total_compra) / Decimal(novo_saldo)
            produto.custo_medio = round(novo_custo_medio, 2)

        produto.estoque_atual = novo_saldo
        # NOTA: produto.ultimo_preco_custo NÃO é atribuído diretamente aqui
        # pois é calculado dinamica e dinamicamente via @property através da relação de 'entradas'.

        db.session.commit()
        return True, f"Entrada de {quantidade} un. de '{produto.nome}' registrada com sucesso!"

    except (ValueError, InvalidOperation):
        db.session.rollback()
        return False, "Valor numérico de preço ou quantidade inválido."
    except Exception as e:
        db.session.rollback()
        return False, f"Erro ao registrar entrada: {str(e)}"