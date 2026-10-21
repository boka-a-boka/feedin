from flask import Blueprint

from flask import Blueprint

# O parâmetro static_folder='static' é obrigatório para habilitar o 'pdv.static'
pdv_bp = Blueprint('pdv', __name__, static_folder='static', static_url_path='/pdv/static')

# Imports tardios para evitar importação circular
from feedin.modules.pdv import routes, models