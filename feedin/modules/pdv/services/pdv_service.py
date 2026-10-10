from flask import Blueprint, request, jsonify, current_app
from flask_login import login_required, current_user
from decimal import Decimal
from datetime import datetime

from feedin import database as db
from feedin.modules.agenda.models import AghAgendamento
from feedin.modules.pdv.models import PdvComandaItem
from feedin.modules.billing.models import Cobranca  # fin_cobrancas
from feedin.modules.estoque.models import ProdutoCatalogo  # est_produtos

import hashlib
from decimal import Decimal

def registrar_item_comanda_logic(data):
    """
    Função centralizada para processar o lançamento de item no PDV.
    """
    cobranca_id = data.get('cobranca_id')
    produto_id = data.get('produto_id')
    quantidade = int(data.get('quantidade', 1))
    colaborador_id = data.get('colaborador_id') or getattr(current_user, 'id', None)
    origem = data.get('origem_lancamento', 'COLABORADOR')

    if not cobranca_id or not produto_id:
        raise ValueError("Parâmetros obrigatórios ausentes: cobranca_id e produto_id.")

    if quantidade <= 0:
        raise ValueError("A quantidade lançada deve ser maior que zero.")

    produto = ProdutoCatalogo.query.get(produto_id)
    if not produto:
        raise ValueError("Produto não encontrado no catálogo de estoque.")

    cobranca = Cobranca.query.get(cobranca_id)
    if not cobranca:
        raise ValueError("Comanda/Cobrança central não encontrada.")

    # Snapshot Financeiro
    preco_base = getattr(produto, 'preco_venda', None) or getattr(produto, 'preco', 0.0)
    preco_unitario = Decimal(str(preco_base or 0.00))
    valor_total = preco_unitario * quantidade

    # Comissão
    percentual_comissao = Decimal('0.00')
    if hasattr(produto, 'percentual_comissao') and produto.percentual_comissao:
        percentual_comissao = Decimal(str(produto.percentual_comissao))

    valor_comissao = (valor_total * percentual_comissao) / Decimal('100.00')

    # 🟢 Resgate Garantido do CPF Hash do Cliente Titular
    cpf_hash = getattr(cobranca, 'cpf_hash_cliente', None)

    if not cpf_hash:
        # Tenta buscar pelo agendamento vinculado à cobrança
        if getattr(cobranca, 'origem_tipo', '') == 'AGENDAMENTO' and getattr(cobranca, 'origem_id', None):
            agendamento_obj = AghAgendamento.query.get(cobranca.origem_id)
            if agendamento_obj:
                cliente_obj = getattr(agendamento_obj, 'cliente', None) or getattr(agendamento_obj, 'usuario', None)
                if cliente_obj:
                    cpf_hash = getattr(cliente_obj, 'cpf_hash', None)
                    if not cpf_hash:
                        cpf_val = getattr(cliente_obj, 'cpf', None) or getattr(cliente_obj, 'documento', None)
                        if cpf_val:
                            cpf_hash = hashlib.sha256(str(cpf_val).strip().encode('utf-8')).hexdigest()

    # Se a cobrança ainda não tinha o hash, atualiza a cobrança pai também
    if cpf_hash and hasattr(cobranca, 'cpf_hash_cliente') and not cobranca.cpf_hash_cliente:
        cobranca.cpf_hash_cliente = cpf_hash

    # Instanciação do Item da Comanda
    item_kwargs = {
        'empresa_id': cobranca.empresa_id,
        'cobranca_id': cobranca.id,
        'produto_id': produto.id,
        'colaborador_id': colaborador_id,
        'quantidade': quantidade,
        'preco_unitario': preco_unitario,
        'valor_total': valor_total,
        'percentual_comissao': percentual_comissao,
        'valor_comissao': valor_comissao
    }

    if hasattr(PdvComandaItem, 'origem_lancamento'):
        item_kwargs['origem_lancamento'] = origem

    if hasattr(PdvComandaItem, 'nome_produto'):
        item_kwargs['nome_produto'] = produto.nome
    elif hasattr(PdvComandaItem, 'descricao_item'):
        item_kwargs['descricao_item'] = produto.nome
    elif hasattr(PdvComandaItem, 'descricao'):
        item_kwargs['descricao'] = produto.nome

    if hasattr(PdvComandaItem, 'cpf_hash_cliente') and cpf_hash:
        item_kwargs['cpf_hash_cliente'] = cpf_hash

    novo_item = PdvComandaItem(**item_kwargs)
    db.session.add(novo_item)

    # Recálculo do Saldo da Cobrança
    if hasattr(cobranca, 'atualizar_valor_bruto'):
        cobranca.atualizar_valor_bruto()
    else:
        valor_bruto_atual = Decimal(str(cobranca.valor_bruto or 0.00))
        cobranca.valor_bruto = valor_bruto_atual + valor_total
        desconto = Decimal(str(cobranca.valor_desconto or 0.00))
        acrescimo = Decimal(str(cobranca.valor_acrescimo or 0.00))
        cobranca.valor_liquido = cobranca.valor_bruto - desconto + acrescimo

    db.session.commit()
    return novo_item