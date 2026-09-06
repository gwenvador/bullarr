#!/usr/bin/env python3
"""
Script d'entrée pour l'application Bullarr en mode production
Usage: python run.py ou FLASK_ENV=production python app.py
"""
import os
import sys

if __name__ == '__main__':
    os.environ['FLASK_ENV'] = 'production'
    from app import create_app
    
    app = create_app('production')
    
    print("=" * 60)
    print("Gestionnaire Multi-Bibliothèques BD - PRODUCTION")
    print("=" * 60)
    print("Accédez à http://localhost:5000")
    print("Écoute sur IPv4 et IPv6")
    print("=" * 60)
    
    # threaded=True: sans ça (défaut Werkzeug), le serveur ne traite qu'une requête à la
    # fois - un téléchargement Telegram de plusieurs dizaines/centaines de Mo (voir
    # blueprints/telegram_channels/scraper.py) bloquerait alors TOUTE l'app (même charger
    # une autre page) jusqu'à sa fin, et la page Téléchargements ne pourrait jamais
    # interroger sa progression en direct pendant qu'il tourne. Même correctif déjà
    # documenté dans app.py côté create_app(), mais absent ici alors que run.py (pas
    # app.py) est le point d'entrée réel en conteneur (voir docker-entrypoint.sh).
    app.run(debug=False, host='::', port=5000, use_reloader=False, threaded=True)
