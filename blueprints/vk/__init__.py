from flask import Blueprint

vk_bp = Blueprint(
    'vk',
    __name__,
    url_prefix='/api/vk'
)

from . import routes
