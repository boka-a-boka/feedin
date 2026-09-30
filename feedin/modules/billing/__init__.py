"""
Módulo Central de Cobranças e Faturamento (Billing Engine)
Prefixo das Tabelas: fin_
"""

from flask import Blueprint

billing_bp = Blueprint(
    'billing',
    __name__,
    url_prefix='/billing',
    template_folder='templates',
    static_folder='static'
)

# Importa as rotas para registrá-las na Blueprint sem circular import
from . import routes  # noqa: E402, F401