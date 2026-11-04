"""Tableau « Événements » de Configuration → Notifications : une ligne par type de
notification, une case par canal (Telegram, ntfy), au lieu d'une liste par canal.

Les cases restent stockées dans la configuration de chaque canal (notify_<type>), lues
par notifications.channel_wants."""
from flask import jsonify, request

from notifications import CHAT_CHANNELS, EVENTS, KINDS, channel_config, channel_wants
from . import notification_events_bp


def _save_channel(channel, cfg):
    if channel == 'telegram':
        from blueprints.telegram.routes import save_telegram_config
        return save_telegram_config(cfg)
    from blueprints.ntfy.routes import save_ntfy_config
    return save_ntfy_config(cfg)


@notification_events_bp.route('/events', methods=['GET'])
def get_notification_events():
    channels = {}
    for channel in CHAT_CHANNELS:
        cfg = channel_config(channel)
        configured = bool(cfg.get('chat_id') and cfg.get('bot_token')) if channel == 'telegram' else bool(cfg.get('topic'))
        channels[channel] = {
            'enabled': bool(cfg.get('enabled')),
            'configured': configured,
            'events': {kind: channel_wants(cfg, kind) for kind in KINDS},
        }
    from blueprints.push.routes import load_endpoints
    return jsonify({
        'success': True,
        'events': [{'id': kind, 'label': label, 'description': description} for kind, label, description, _ in EVENTS],
        'channels': channels,
        'push_devices': len(load_endpoints()),
    })


@notification_events_bp.route('/events', methods=['POST'])
def save_notification_events():
    data = request.get_json(silent=True) or {}
    for channel in CHAT_CHANNELS:
        values = data.get(channel)
        if not isinstance(values, dict):
            continue
        cfg = channel_config(channel)
        for kind in KINDS:
            if kind in values:
                cfg[f'notify_{kind}'] = bool(values[kind])
        if not _save_channel(channel, cfg):
            return jsonify({'success': False, 'error': f'Erreur de sauvegarde ({channel})'}), 500
    # « Import disponible » fait tourner la tâche planifiée d'import (voir sync_auto_import_schedule).
    from blueprints.library.scheduler import sync_auto_import_schedule
    sync_auto_import_schedule()
    return jsonify({'success': True})
