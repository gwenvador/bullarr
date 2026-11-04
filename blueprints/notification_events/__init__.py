from flask import Blueprint

notification_events_bp = Blueprint('notification_events', __name__)

from . import routes  # noqa: E402,F401
