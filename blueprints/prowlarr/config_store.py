"""
Chargement/sauvegarde de la configuration Prowlarr (partagé entre prowlarr/routes.py,
search/routes.py et missing_monitor/searcher.py - même pattern que
blueprints/komga/config_store.py). Avant ce module, load_prowlarr_config existait en 2
copies identiques (prowlarr/routes.py et search/routes.py) et une 3e réimplémentation
inline dans missing_monitor/searcher.py - voir search.py de ce même dossier pour le
coeur de recherche partagé, qui utilise ce module.
"""
from flask import current_app
from encryption import load_encrypted_json_config, save_encrypted_json_config


def load_prowlarr_config():
    """Charge la configuration Prowlarr"""
    return load_encrypted_json_config(
        current_app.config['PROWLARR_CONFIG_FILE'], current_app.config['PROWLARR_CONFIG'], secret_field='api_key'
    )


def save_prowlarr_config(config):
    """Sauvegarde la configuration Prowlarr"""
    return save_encrypted_json_config(
        current_app.config['PROWLARR_CONFIG_FILE'], config, secret_field='api_key', client_label='Prowlarr'
    )
