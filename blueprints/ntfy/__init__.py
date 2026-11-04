from flask import Blueprint

ntfy_bp = Blueprint('ntfy', __name__)

from . import routes  # noqa: E402,F401
