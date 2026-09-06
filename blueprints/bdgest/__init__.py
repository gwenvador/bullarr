"""
Blueprint pour BDGest - Top 100 annuel des ventes BD (bdgest.com)

Site distinct de Bédéthèque (autre nom de domaine, autre société éditrice), mais les
BD listées y renvoient directement vers leur fiche Bédéthèque - même identifiant
(series.bedetheque_url) pour détecter "déjà possédé" que Panthéon/Thèmes
(voir blueprints/bedetheque/routes.py).
"""
from flask import Blueprint

bdgest_bp = Blueprint('bdgest', __name__)

from . import routes
