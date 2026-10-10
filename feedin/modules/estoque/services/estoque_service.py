from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone
from feedin import database as db
from feedin.modules.estoque.models import ProdutoCatalogo, EntradaEstoqueItem

def validar_acesso_empresa(usuario, empresa):
    """
    Verifica se o usuário possui permissão de gestão sobre a EseEmpresa:
    1. Se é o proprietário direto (proprietario_id)
    2. Se é administrador do sistema (is_admin / papel 999)
    """
    if not usuario or not usuario.is_authenticated:
        return False

    if getattr(usuario, 'e_admin', False) or getattr(usuario, 'papel', None) == 999:
        return True

    return empresa.proprietario_id == usuario.id


def registrar_entrada_estoque(empresa_id, usuario_id, form_data):
    """
    Registra uma nova compra/lote de estoque:
    - Busca o produto por ID ou Código de Barras
    - Grava/Atualiza o codigo_barras no ProdutoCatalogo caso informado
    - Grava o item em est_entradas_itens
    - Atualiza o estoque_atual do ProdutoCatalogo
    - Atualiza o preco_custo e recalcula o custo_medio ponderado
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

        # 1. Registra o Lote no Histórico
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

        # 2. Recalculo do Custo Médio Ponderado e Atualização de Saldo
        saldo_anterior = produto.estoque_atual or 0
        custo_medio_anterior = produto.custo_medio or Decimal('0.00')

        novo_saldo = saldo_anterior + quantidade

        if novo_saldo > 0:
            # Fórmula do Custo Médio Ponderado: (Saldo Antigo * Custo Médio Antigo + Valor da Nova Compra) / Novo Saldo
            valor_patrimonial_anterior = Decimal(saldo_anterior) * custo_medio_anterior
            novo_custo_medio = (valor_patrimonial_anterior + valor_total_compra) / Decimal(novo_saldo)
            produto.custo_medio = round(novo_custo_medio, 2)

        produto.estoque_atual = novo_saldo

        # ✅ AJUSTE PONTUAL: Atualiza a coluna da tabela 'preco_custo' em vez de atribuir na @property
        produto.preco_custo = preco_custo_unitario

        db.session.commit()
        return True, f"Entrada de {quantidade} un. de '{produto.nome}' registrada com sucesso!"

    except (ValueError, InvalidOperation):
        db.session.rollback()
        return False, "Valor numérico de preço ou quantidade inválido."
    except Exception as e:
        db.session.rollback()
        return False, f"Erro ao registrar entrada: {str(e)}"


def registrar_baixa_operacional(empresa_id, usuario_id, form_data):
    """
    Registra o consumo interno (bancada) ou perda/avaria de produtos:
    - Abate o saldo físico em estoque_atual
    """
    try:
        produto_id = form_data.get('produto_id', type=int)
        quantidade = form_data.get('quantidade', type=int)
        motivo = form_data.get('motivo', 'uso_interno')  # 'uso_interno' ou 'avaria'

        if not produto_id or not quantidade or quantidade <= 0:
            return False, "Quantidade ou produto inválido para baixa."

        produto = ProdutoCatalogo.query.filter_by(id=produto_id, empresa_id=empresa_id).first()
        if not produto:
            return False, "Produto não encontrado para este estabelecimento."

        if produto.estoque_atual < quantidade:
            return False, f"Saldo insuficiente em estoque. Saldo atual: {produto.estoque_atual} un."

        # Abate do saldo físico sem alterar o Custo Médio
        produto.estoque_atual -= quantidade

        db.session.commit()

        rotulo_motivo = "Uso em bancada" if motivo == 'uso_interno' else "Avaria/Perda"
        return True, f"Baixa de {quantidade} un. ('{produto.nome}') efetuada. Motivo: {rotulo_motivo}."

    except Exception as e:
        db.session.rollback()
        return False, f"Erro ao registrar baixa no estoque: {str(e)}"