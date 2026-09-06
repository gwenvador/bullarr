from flask import Blueprint

telegram_channels_bp = Blueprint(
    'telegram_channels',
    __name__,
    url_prefix='/api/telegram-channels'
)

from . import routes
