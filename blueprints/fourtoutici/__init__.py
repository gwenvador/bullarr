from flask import Blueprint

fourtoutici_bp = Blueprint(
    'fourtoutici',
    __name__,
    url_prefix='/api/fourtoutici'
)

from . import routes
