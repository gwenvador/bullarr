"""
Blueprint pour Bedetheque - Recherche de séries et source d'infos sur les BD
"""
from flask import Blueprint

bedetheque_bp = Blueprint('bedetheque', __name__)

from . import routes
