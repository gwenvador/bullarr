from flask import Blueprint

rtorrent_bp = Blueprint(
    'rtorrent',
    __name__,
    url_prefix='/api/rtorrent'
)

from . import routes
