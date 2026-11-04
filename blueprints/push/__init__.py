from flask import Blueprint

push_bp = Blueprint('push', __name__)

from . import routes  # noqa: E402,F401
