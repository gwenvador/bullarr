from flask import Blueprint

deluge_bp = Blueprint(
    'deluge',
    __name__,
    url_prefix='/api/deluge'
)

from . import routes
