# feedin/modules/agenda/__init__.py
from flask import Blueprint

# Criamos o blueprint apontando para a sua própria pasta de templates interna
agenda_bp = Blueprint(
    'agenda',
    __name__,
    template_folder='templates',
    static_folder='static' # Caso decida colocar arquivos estáticos exclusivos do PWA aqui
)

# Importamos as rotas para que o Flask as conheça ao registrar o Blueprint
from . import routes
from feedin.modules.agenda import routes