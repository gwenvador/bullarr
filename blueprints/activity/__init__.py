from flask import Blueprint

activity_bp = Blueprint(
    'activity',
    __name__
)

from . import routes
