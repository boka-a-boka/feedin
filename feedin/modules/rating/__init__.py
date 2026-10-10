from flask import Blueprint

rating_bp = Blueprint(
    'rating',
    __name__,
    url_prefix='/rating'
)

# Imports tardios para evitar importação circular
from feedin.modules.rating import routes, models