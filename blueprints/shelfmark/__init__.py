from flask import Blueprint

shelfmark_bp = Blueprint('shelfmark', __name__, url_prefix='/api/shelfmark')

from . import routes  # noqa: E402,F401
