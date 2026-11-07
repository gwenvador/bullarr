"""
Blueprint pour la gestion des bibliothèques de BD
"""
from flask import Blueprint

library_bp = Blueprint('library', __name__)

from . import routes
from . import series_move
from . import book_requests
