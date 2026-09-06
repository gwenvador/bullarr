"""
Blueprint pour l'intégration Komga
"""
from flask import Blueprint

komga_bp = Blueprint('komga', __name__)

from . import routes
