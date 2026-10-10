from flask import Blueprint

pdv_bp = Blueprint(
    'pdv',
    __name__,
    url_prefix='/pdv'
)

# Imports tardios para evitar importação circular
from feedin.modules.pdv import routes, models