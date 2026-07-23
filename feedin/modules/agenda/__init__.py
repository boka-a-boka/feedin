# feedin/modules/agenda/__init__.py
from flask import Blueprint

agenda_bp = Blueprint(
    'agenda',
    __name__,
    template_folder='templates',
    static_folder='static',          # 🟢 Avisa o Flask que este módulo tem arquivos estáticos
    static_url_path='/agenda/static' # 🟢 Define a rota única para não chocar com o Core
)

# Importações relativas limpas e sem duplicidade
from . import routes, models, forms