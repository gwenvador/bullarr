"""
Chargement/sauvegarde de la configuration Komga (partagé entre routes.py et client.py)
"""
from flask import current_app
from encryption import load_encrypted_json_config, save_encrypted_json_config


def load_komga_config():
    """Charge la configuration Komga"""
    return load_encrypted_json_config(
        current_app.config['KOMGA_CONFIG_FILE'], current_app.config['KOMGA_CONFIG'], secret_field='api_key'
    )


def is_komga_configured():
    """"make sure that if komga or ebdz is not configured they dont show up in the
    table or the settings with matching" - point d'entrée unique pour cette question,
    réutilisé par tous les endroits qui affichent un statut/une action de matching
    Komga (page /bedetheque-enrich, colonne "Matching Komga" du tableau bibliothèque,
    boutons de la fiche série)."""
    return bool(load_komga_config().get('enabled'))


def save_komga_config(config):
    """Sauvegarde la configuration Komga"""
    return save_encrypted_json_config(
        current_app.config['KOMGA_CONFIG_FILE'], config, secret_field='api_key', client_label='Komga'
    )


def normalize_komga_url(url):
    url = url.strip().rstrip('/')
    if not url.startswith('http://') and not url.startswith('https://'):
        url = 'https://' + url
    return url
