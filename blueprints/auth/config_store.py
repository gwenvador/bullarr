"""
Chargement/sauvegarde de la configuration SSO / OIDC (partagé entre routes.py et le client OAuth)
"""
import json
import os
import secrets
import tempfile
from flask import current_app
from encryption import encrypt, decrypt

# none : pas d'authentification ; auth : connexion obligatoire avec un compte local
# (identifiant + mot de passe, voir accounts.py), plus le bouton SSO si oidc_enabled.
AUTH_MODES = {'none', 'auth'}
# Anciens modes, convertis au chargement : (oidc activé, ancien compte local actif).
LEGACY_AUTH_MODES = {'password': (False, True), 'oidc': (True, False), 'both': (True, True)}


def load_oidc_config():
    """Charge la configuration OIDC"""
    config_file = current_app.config['OIDC_CONFIG_FILE']

    if os.path.exists(config_file):
        with open(config_file, 'r') as f:
            cfg = json.load(f)
    else:
        cfg = current_app.config['OIDC_CONFIG'].copy()

    # Anciens modes (password / oidc / both, ou le booléen enabled qui voulait dire OIDC).
    mode = cfg.get('mode')
    if mode in LEGACY_AUTH_MODES:
        oidc_on, password_on = LEGACY_AUTH_MODES[mode]
        cfg['mode'] = 'auth'
        cfg.setdefault('oidc_enabled', oidc_on)
        if not password_on:  # identifiant/mot de passe saisis mais jamais activés
            cfg.pop('username', None)
            cfg.pop('password_hash', None)
    elif mode not in AUTH_MODES:
        cfg['mode'] = 'auth' if cfg.get('enabled', False) else 'none'
        cfg.setdefault('oidc_enabled', cfg['mode'] == 'auth')
    cfg['oidc_enabled'] = bool(cfg.get('oidc_enabled', False))

    # Pas de redirection automatique vers le SSO tant qu'elle n'est pas demandée.
    cfg['auto_redirect'] = bool(cfg.get('auto_redirect', False))

    # Déchiffrer le secret client s'il existe
    client_secret = cfg.get('client_secret', '')
    if client_secret:
        decrypted = decrypt(client_secret)
        if decrypted:
            cfg['client_secret_decrypted'] = decrypted

    api_key = cfg.get('api_key', '')
    if api_key:
        cfg['api_key_decrypted'] = decrypt(api_key) or ''

    # L'ancien compte unique devient un compte administrateur de « Utilisateurs et rôles ».
    if cfg.get('username') and cfg.get('password_hash'):
        try:
            from .accounts import migrate_legacy_account
            if migrate_legacy_account(cfg):
                save_oidc_config(dict(cfg))  # après déchiffrement : les secrets ne sont chiffrés qu'une fois
        except Exception as error:
            print(f'Migration du compte local impossible : {error}')

    return cfg


def generate_api_key():
    """Nouvelle clé API (32 caractères hexadécimaux, comme les applications *arr)."""
    return secrets.token_hex(16)


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

        # La clé API est stockée chiffrée, comme le secret client
        if config_to_save.get('api_key_decrypted'):
            config_to_save['api_key'] = encrypt(config_to_save['api_key_decrypted'])
        config_to_save.pop('api_key_decrypted', None)

        directory = os.path.dirname(config_file)
        fd, temporary = tempfile.mkstemp(prefix='.oidc-config-', dir=directory)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(config_to_save, f, indent=4)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, config_file)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return True
    except Exception as e:
        print(f"Erreur sauvegarde config OIDC : {e}")
        return False


def normalize_issuer_url(url):
    """Normalise l'URL de l'issuer (retire le slash final)"""
    return url.strip().rstrip('/')
