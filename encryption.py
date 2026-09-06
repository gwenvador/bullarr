"""
Module de chiffrement pour les données sensibles
"""
import os
import json
from cryptography.fernet import Fernet
import base64

# Fichier où stocker la clé de chiffrement
ENCRYPTION_KEY_FILE = './data/.encryption_key'


def ensure_encryption_key():
    """Assure qu'une clé de chiffrement existe, la crée sinon"""
    os.makedirs('./data', exist_ok=True)
    
    if not os.path.exists(ENCRYPTION_KEY_FILE):
        key = Fernet.generate_key()
        with open(ENCRYPTION_KEY_FILE, 'wb') as f:
            f.write(key)
        # Cette clé déchiffre tous les identifiants stockés (Komga/EBDZ/Prowlarr/aMule/
        # qBittorrent) - restreinte au seul propriétaire, pas world-readable par défaut
        os.chmod(ENCRYPTION_KEY_FILE, 0o600)
        print("✓ Clé de chiffrement générée et sauvegardée")
    else:
        os.chmod(ENCRYPTION_KEY_FILE, 0o600)
        print("✓ Clé de chiffrement existante trouvée")


def load_encryption_key():
    """Charge la clé de chiffrement"""
    if not os.path.exists(ENCRYPTION_KEY_FILE):
        raise FileNotFoundError(f"Clé de chiffrement non trouvée. Exécutez ensure_encryption_key()")
    
    with open(ENCRYPTION_KEY_FILE, 'rb') as f:
        return f.read()


def encrypt(plaintext):
    """Chiffre une chaîne de caractères"""
    if not plaintext:
        return None
    
    key = load_encryption_key()
    cipher = Fernet(key)
    encrypted = cipher.encrypt(plaintext.encode())
    return encrypted.decode()


def decrypt(ciphertext):
    """Déchiffre une chaîne de caractères"""
    if not ciphertext:
        return None

    try:
        key = load_encryption_key()
        cipher = Fernet(key)
        decrypted = cipher.decrypt(ciphertext.encode())
        return decrypted.decode()
    except Exception as e:
        print(f"Erreur déchiffrement: {e}")
        return None


def load_encrypted_json_config(config_file, default_config, secret_field='password'):
    """Charge un fichier de config JSON (ou une copie de `default_config` s'il n'existe
    pas encore) et déchiffre un unique champ secret s'il est présent - pattern partagé
    par les 6 intégrations qui stockent un identifiant chiffré (qBittorrent/rTorrent/
    Deluge/aMule: secret_field='password' ; Komga/Prowlarr: secret_field='api_key'),
    auparavant recopié presque à l'identique dans chacune. Ajoute `<secret_field>_decrypted`
    au dict retourné si le déchiffrement réussit, comme le faisait chaque copie."""
    if os.path.exists(config_file):
        with open(config_file, 'r') as f:
            cfg = json.load(f)
    else:
        cfg = default_config.copy()

    secret = cfg.get(secret_field, '')
    if secret:
        decrypted = decrypt(secret)
        if decrypted:
            cfg[f'{secret_field}_decrypted'] = decrypted

    return cfg


def save_encrypted_json_config(config_file, config, secret_field='password', client_label=None):
    """Sauvegarde un fichier de config JSON en re-chiffrant le champ secret et en
    retirant sa version déchiffrée avant écriture - pendant de
    load_encrypted_json_config ci-dessus. client_label (optionnel) reproduit le message
    d'erreur nommé que chacune des 6 copies remplacées avait ("Erreur sauvegarde config
    qBittorrent : ...") ; laissé à None pour un message générique."""
    try:
        config_to_save = config.copy()
        decrypted_field = f'{secret_field}_decrypted'

        if config_to_save.get(secret_field) or config_to_save.get(decrypted_field):
            secret_to_encrypt = config_to_save.get(decrypted_field) or config_to_save.get(secret_field)
            if secret_to_encrypt:
                config_to_save[secret_field] = encrypt(secret_to_encrypt)
            if decrypted_field in config_to_save:
                del config_to_save[decrypted_field]

        with open(config_file, 'w') as f:
            json.dump(config_to_save, f, indent=4)
        os.chmod(config_file, 0o600)
        return True
    except Exception as e:
        label = f" {client_label}" if client_label else ""
        print(f"Erreur sauvegarde config{label} : {e}")
        return False
