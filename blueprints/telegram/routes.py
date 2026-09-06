"""
Routes pour la configuration Telegram + fonction d'envoi réutilisable.

Infrastructure seule pour l'instant ("ajouté une notification telegram" -> "just build
the settings/infrastructure") : ce module ne déclenche aucun envoi automatique tout seul,
il fournit juste la config (jeton de bot + chat_id) et send_telegram_notification() à
appeler depuis un futur point d'accroche (fin de scrape EBDZ, volume manquant trouvé,
import terminé...) quand ce sera demandé.
"""
from flask import request, jsonify, current_app
from . import telegram_bp
import json
import os
import requests
from encryption import encrypt, decrypt


def load_telegram_config():
    """Charge la configuration Telegram"""
    config_file = current_app.config['TELEGRAM_CONFIG_FILE']

    if os.path.exists(config_file):
        with open(config_file, 'r') as f:
            cfg = json.load(f)
    else:
        cfg = current_app.config['TELEGRAM_CONFIG'].copy()

    # Déchiffrer le jeton de bot s'il existe - même traitement que la clé API Prowlarr/
    # Komga (voir encryption.py), un jeton Telegram donne un contrôle total du bot
    bot_token = cfg.get('bot_token', '')
    if bot_token:
        decrypted = decrypt(bot_token)
        if decrypted:
            cfg['bot_token_decrypted'] = decrypted

    return cfg


def save_telegram_config(config):
    """Sauvegarde la configuration Telegram"""
    config_file = current_app.config['TELEGRAM_CONFIG_FILE']

    try:
        config_to_save = config.copy()

        if config_to_save.get('bot_token') or config_to_save.get('bot_token_decrypted'):
            token_to_encrypt = config_to_save.get('bot_token_decrypted') or config_to_save.get('bot_token')
            if token_to_encrypt:
                config_to_save['bot_token'] = encrypt(token_to_encrypt)
            if 'bot_token_decrypted' in config_to_save:
                del config_to_save['bot_token_decrypted']

        with open(config_file, 'w') as f:
            json.dump(config_to_save, f, indent=4)
        os.chmod(config_file, 0o600)
        return True
    except Exception as e:
        print(f"Erreur sauvegarde config Telegram : {e}")
        return False


def send_telegram_notification(message, bot_token=None, chat_id=None, photo_path=None):
    """Envoie un message via le bot Telegram configuré (API sendMessage, ou sendPhoto si
    photo_path est fourni). Nécessite un contexte applicatif Flask actif (current_app) -
    depuis un thread d'arrière-plan (scheduler, écriture ComicInfo async...), pousser
    explicitement app.app_context() avant d'appeler cette fonction, comme pour
    KomgaClient.trigger_scan_async (voir CLAUDE.md, section Komga) - sans ça current_app
    lève un RuntimeError silencieux en dehors d'un contexte de requête.

    bot_token/chat_id optionnels: pour tester des valeurs pas encore enregistrées
    (voir test_telegram_notification) sans devoir d'abord passer par save_telegram_config
    - par défaut, retombe sur la configuration sauvegardée (et sur enabled=True requis).

    photo_path (optionnel): chemin local d'une image de couverture ("add the cover to
    telegram notification") - envoyée via sendPhoto avec `message` comme légende au lieu
    d'un sendMessage texte seul. Un chemin manquant/illisible retombe silencieusement sur
    sendMessage plutôt que de faire échouer toute la notification pour une couverture
    secondaire.

    Retourne (success: bool, error: str|None) plutôt que de lever une exception : un
    envoi Telegram raté ne doit jamais faire échouer l'opération qui l'a déclenché
    (import, scan, etc.), seulement être journalisé.
    """
    config = load_telegram_config()

    if bot_token is None and chat_id is None and not config.get('enabled'):
        return False, 'Notifications Telegram désactivées'

    if not bot_token:
        bot_token = config.get('bot_token_decrypted') or decrypt(config.get('bot_token', ''))
    if not chat_id:
        chat_id = config.get('chat_id', '').strip()

    if not bot_token or not chat_id:
        return False, 'Jeton de bot ou chat_id manquant'

    try:
        if photo_path and os.path.isfile(photo_path):
            with open(photo_path, 'rb') as photo_file:
                response = requests.post(
                    f'https://api.telegram.org/bot{bot_token}/sendPhoto',
                    data={'chat_id': chat_id, 'caption': message},
                    files={'photo': photo_file},
                    timeout=15
                )
        else:
            response = requests.post(
                f'https://api.telegram.org/bot{bot_token}/sendMessage',
                json={'chat_id': chat_id, 'text': message},
                timeout=10
            )
        if response.status_code == 200:
            return True, None
        return False, f"Erreur Telegram ({response.status_code}): {response.text[:200]}"
    except requests.exceptions.Timeout:
        return False, 'Timeout: impossible de contacter Telegram'
    except requests.exceptions.ConnectionError:
        return False, 'Impossible de contacter api.telegram.org'
    except Exception as e:
        return False, f'Erreur: {str(e)}'


@telegram_bp.route('/config', methods=['GET', 'POST'])
def telegram_config():
    """Configuration Telegram"""

    if request.method == 'GET':
        config = load_telegram_config()

        # Masquer le jeton de bot, même traitement que la clé API Prowlarr/Komga
        return jsonify({
            'enabled': config.get('enabled', False),
            'chat_id': config.get('chat_id', ''),
            'bot_token': '****' if config.get('bot_token') else '',
            'notify_import_completed': config.get('notify_import_completed', True),
            'notify_import_available': config.get('notify_import_available', True)
        })

    else:  # POST
        try:
            new_config = request.get_json()
            config = load_telegram_config()

            config['enabled'] = new_config.get('enabled', False)
            config['chat_id'] = new_config.get('chat_id', '').strip()
            config['notify_import_completed'] = new_config.get('notify_import_completed', True)
            config['notify_import_available'] = new_config.get('notify_import_available', True)

            # Ne change le jeton que si ce n'est pas la valeur masquée
            new_token = new_config.get('bot_token', '')
            if new_token and new_token != '****':
                config['bot_token_decrypted'] = new_token

            if save_telegram_config(config):
                # La tâche planifiée d'import automatique tourne aussi pour la
                # notification "import disponible" même quand l'import automatique
                # lui-même est désactivé (voir sync_auto_import_schedule) - un
                # changement ici peut donc devoir démarrer/arrêter cette tâche.
                from blueprints.library.scheduler import sync_auto_import_schedule
                sync_auto_import_schedule()
                return jsonify({'success': True})
            else:
                return jsonify({'success': False, 'error': 'Erreur de sauvegarde'}), 500

        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500


@telegram_bp.route('/test', methods=['POST'])
def test_telegram_notification():
    """Envoie un message de test, pour valider jeton + chat_id sans attendre qu'un futur
    événement de l'app déclenche un envoi réel.

    Accepte optionnellement un body JSON {bot_token, chat_id} pour tester les valeurs
    actuellement saisies dans le formulaire, sans exiger qu'elles aient été enregistrées
    ni que l'intégration soit déjà activée (même pattern que /api/komga/test)."""
    payload = request.get_json(silent=True) or {}

    payload_token = payload.get('bot_token', '')
    bot_token = payload_token if payload_token and payload_token != '****' else None

    payload_chat_id = (payload.get('chat_id') or '').strip()
    chat_id = payload_chat_id or None

    success, error = send_telegram_notification(
        '✅ Bullarr : notification de test - la configuration Telegram fonctionne.',
        bot_token=bot_token,
        chat_id=chat_id
    )
    if success:
        return jsonify({'success': True, 'message': 'Message envoyé'})
    return jsonify({'success': False, 'error': error}), 400
