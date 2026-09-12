"""
Chargement/sauvegarde de la configuration SSO / OIDC (partagé entre routes.py et le client OAuth)
"""
import json
import os
from flask import current_app
from encryption import encrypt, decrypt


def load_oidc_config():
    """Charge la configuration OIDC"""
    config_file = current_app.config['OIDC_CONFIG_FILE']

    if os.path.exists(config_file):
        with open(config_file, 'r') as f:
            cfg = json.load(f)
    else:
        cfg = current_app.config['OIDC_CONFIG'].copy()

    # Backward compatibility: the old boolean enabled meant OIDC.
    mode = cfg.get('mode')
    if mode not in {'none', 'password', 'oidc'}:
        cfg['mode'] = 'oidc' if cfg.get('enabled', False) else 'none'

    # Déchiffrer le secret client s'il existe
    client_secret = cfg.get('client_secret', '')
    if client_secret:
        decrypted = decrypt(client_secret)
        if decrypted:
            cfg['client_secret_decrypted'] = decrypted

    return cfg


def save_oidc_config(config):
    """Sauvegarde la configuration OIDC"""
    config_file = current_app.config['OIDC_CONFIG_FILE']

    try:
        config_to_save = config.copy()

        # Chiffrer le secret client avant la sauvegarde
        if config_to_save.get('client_secret') or config_to_save.get('client_secret_decrypted'):
            secret_to_encrypt = config_to_save.get('client_secret_decrypted') or config_to_save.get('client_secret')
            if secret_to_encrypt:
                config_to_save['client_secret'] = encrypt(secret_to_encrypt)
            if 'client_secret_decrypted' in config_to_save:
                del config_to_save['client_secret_decrypted']

        with open(config_file, 'w') as f:
            json.dump(config_to_save, f, indent=4)
        os.chmod(config_file, 0o600)
        return True
    except Exception as e:
        print(f"Erreur sauvegarde config OIDC : {e}")
        return False


def normalize_issuer_url(url):
    """Normalise l'URL de l'issuer (retire le slash final)"""
    return url.strip().rstrip('/')
