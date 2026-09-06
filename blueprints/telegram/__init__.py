"""
Blueprint pour les notifications Telegram
"""
from flask import Blueprint

telegram_bp = Blueprint('telegram', __name__)

from . import routes
