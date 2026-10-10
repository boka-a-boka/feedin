from flask import Blueprint, url_for

estoque_bp = Blueprint(
    'estoque',
    __name__,
    template_folder='templates',
    static_folder='static',
    static_url_path='/static'  # Isolado para não colidir com o Core/outros módulos
)

# ============================================================
# FILTROS E HELPERS LOCAIS DE ESTOQUE (Se necessários)
# ============================================================

@estoque_bp.app_template_filter('formato_moeda')
def formato_moeda(valor):
    """Filtro útil para formatação de preços de custo e venda no estoque."""
    try:
        if valor is None:
            return "R$ 0,00"
        return f"R$ {float(valor):,.2f}".replace(',', 'X').replace('.', ',').replace('X', '.')
    except (ValueError, TypeError):
        return "R$ 0,00"


# ============================================================
# IMPORTAÇÕES (Mantidas ao final para evitar Circular Imports)
# ============================================================

from . import routes, models