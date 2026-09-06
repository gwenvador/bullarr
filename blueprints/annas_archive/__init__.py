from flask import Blueprint

annas_archive_bp = Blueprint('annas_archive', __name__, url_prefix='/api/annas-archive')

from . import routes
