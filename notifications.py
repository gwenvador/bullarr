"""Envoi des notifications de Bullarr sur tous les canaux configurés.

Chaque point d'envoi de l'app indique le TYPE de notification (voir EVENTS). Telegram et
ntfy ont chacun une case par type, réglées ensemble dans le tableau « Événements » de
Configuration → Notifications (cases notify_<type> de leur configuration). Les téléphones
abonnés aux notifications instantanées de l'application Android (blueprints/push)
reçoivent tout et filtrent eux-mêmes selon les alertes cochées sur le téléphone.

Nécessite un contexte applicatif Flask (current_app), comme send_telegram_notification.
Ne lève jamais d'exception : une notification ratée ne fait pas échouer l'opération.
"""

# (type, libellé, description, coché par défaut) - l'ordre est celui du tableau.
EVENTS = (
    ('download_added', 'Téléchargement ajouté',
     "Un téléchargement est envoyé à un client (recherche, surveillance ou validation)", True),
    ('import_completed', 'Import effectué',
     "Des fichiers entrent dans la bibliothèque, avec la couverture quand l'import concerne une seule série", True),
    ('import_available', 'Import disponible',
     "De nouveaux fichiers attendent dans les répertoires surveillés", True),
    ('monitor', 'Surveillance',
     "La surveillance repère un album manquant et elle est réglée sur « Notifier »", True),
    ('review', 'Validation en attente',
     "Un résultat douteux attend votre décision dans la page Validation", False),
    ('account_request', 'Demande de compte',
     "Quelqu'un demande un compte depuis la page de connexion", True),
    ('book_request', 'Demande de BD',
     "Un lecteur demande une série ; elle attend votre décision dans la page Validation", True),
    ('update_available', 'Mise à jour disponible',
     "Une nouvelle version de Bullarr ou de l'application Android est publiée", True),
)
KINDS = tuple(event[0] for event in EVENTS)
DEFAULTS = {event[0]: event[3] for event in EVENTS}
CHAT_CHANNELS = ('telegram', 'ntfy')
# Noms attendus par l'application Android (voir Push.onMessage côté app). Un téléchargement
# ajouté y passe pour un évènement d'import (alertes « Imports »), une mise à jour pour une
# nouveauté (alertes « Nouveautés ») : l'app ignore les types qu'elle ne connaît pas.
_PUSH_KINDS = {'monitor': 'monitor_found', 'download_added': 'import_available',
               'update_available': 'monitor_found', 'account_request': 'monitor_found',
               'book_request': 'monitor_found'}


_PUBLIC_URL_FILE = 'public_url.txt'
_public_url_cache = {}


def remember_public_url(url_root):
    """Mémorise l'adresse publique de Bullarr vue par le navigateur (request.url_root,
    correct derrière le proxy grâce à ProxyFix) pour mettre un lien Bullarr dans les
    notifications, sans adresse codée en dur. Ignore localhost et les adresses IP locales
    (appels internes, tests), n'écrit le fichier que si l'adresse change."""
    try:
        import os
        from urllib.parse import urlparse
        from flask import current_app
        base = (url_root or '').rstrip('/')
        host = (urlparse(base).hostname or '').lower()
        if not host or host in ('localhost', '127.0.0.1', '::1') or host.endswith('.localhost'):
            return
        if all(part.isdigit() for part in host.split('.')):
            return
        path = os.path.join(current_app.config['DATA_DIR'], _PUBLIC_URL_FILE)
        if _public_url_cache.get(path) == base:
            return
        current = learned_public_url()
        _public_url_cache[path] = base
        if current != base:
            with open(path, 'w') as f:
                f.write(base)
    except Exception:
        pass


def public_url():
    """Adresse de Bullarr pour les liens des notifications (sans / final) : celle saisie
    dans Configuration → Notifications → ntfy, sinon celle apprise du navigateur, sinon ''."""
    try:
        from blueprints.ntfy.routes import load_ntfy_config
        configured = (load_ntfy_config().get('site_url') or '').strip().rstrip('/')
        if configured:
            return configured
    except Exception:
        pass
    return learned_public_url()


def learned_public_url():
    """Adresse publique apprise des pages ouvertes dans un navigateur, ou ''."""
    try:
        import os
        from flask import current_app
        path = os.path.join(current_app.config['DATA_DIR'], _PUBLIC_URL_FILE)
        if path in _public_url_cache:
            return _public_url_cache[path]
        with open(path) as f:
            value = f.read().strip().rstrip('/')
        _public_url_cache[path] = value
        return value
    except Exception:
        return ''


def bullarr_link(path):
    base = public_url()
    return f"{base}{path}" if base else ''


def channel_config(channel):
    if channel == 'telegram':
        from blueprints.telegram.routes import load_telegram_config
        return load_telegram_config()
    from blueprints.ntfy.routes import load_ntfy_config
    return load_ntfy_config()


def channel_wants(cfg, kind):
    """Case notify_<type> d'une configuration de canal (Telegram ou ntfy)."""
    return bool(cfg.get(f'notify_{kind}', DEFAULTS.get(kind, False)))


def _chat_wants(channel):
    def wants(kind):
        cfg = channel_config(channel)
        return kind in KINDS and bool(cfg.get('enabled')) and channel_wants(cfg, kind)
    return wants


def _push_wants(kind):
    from blueprints.push.routes import has_endpoints
    return has_endpoints()


def _channels(kind):
    channels = []
    for name, wants in (('telegram', _chat_wants('telegram')), ('ntfy', _chat_wants('ntfy')), ('push', _push_wants)):
        try:
            if wants(kind):
                channels.append(name)
        except Exception as exc:
            print(f"Erreur lecture configuration {name} : {exc}")
    return channels


def notification_wanted(kind):
    """True si au moins un canal activé veut ce type de notification."""
    return bool(_channels(kind))


def send_notification(message, *, kind, title=None, icon=None, tags=None, click=None, photo_path=None):
    """Envoie la notification sur chaque canal qui la veut.

    Retourne (au moins un envoi réussi, [erreurs]). Telegram reçoit « icon title » en
    première ligne du message (et le lien `click` en dernière ligne) ; ntfy affiche `title`
    comme titre de la notification, `tags` en icônes et ouvre `click` au toucher."""
    channels = _channels(kind)
    if not channels:
        return False, ['Aucun canal de notification activé']
    sent = False
    errors = []
    for channel in channels:
        try:
            if channel == 'telegram':
                from blueprints.telegram.routes import send_telegram_notification
                heading = ' '.join(part for part in (icon, title) if part)
                text = f"{heading}\n{message}" if heading else message
                if click and click not in text:
                    text += f"\n{click}"
                ok, error = send_telegram_notification(text, photo_path=photo_path)
            elif channel == 'ntfy':
                from blueprints.ntfy.routes import send_ntfy_notification
                ok, error = send_ntfy_notification(message, title=title, tags=tags, click=click,
                                                   photo_path=photo_path)
            else:
                from blueprints.push.routes import send_push
                body = f"{message.strip()}\n{click}" if click and click not in message else message
                sent_count, push_errors = send_push(_PUSH_KINDS.get(kind, kind), (title or '').rstrip(' :'), body)
                ok, error = bool(sent_count), '; '.join(push_errors) or 'aucun téléphone joignable'
        except Exception as exc:
            ok, error = False, str(exc)
        if ok:
            sent = True
        else:
            errors.append(f"{channel} : {error}")
    return sent, errors


def series_cover_path(series_id):
    """Couverture locale d'une série (même priorité que la bibliothèque : vignette extraite
    du fichier, puis Bédéthèque, puis Komga), ou None."""
    if not series_id:
        return None
    import os
    import sqlite3
    from flask import current_app
    conn = sqlite3.connect(current_app.config['DATABASE'], timeout=30.0)
    try:
        row = conn.execute(
            'SELECT local_cover_path, bedetheque_cover_path, komga_cover_path FROM series WHERE id = ?',
            (series_id,)).fetchone()
    except sqlite3.Error:
        row = None
    finally:
        conn.close()
    cover = next((value for value in (row or ()) if value), None)
    if not cover:
        return None
    path = os.path.join(current_app.config['DATA_DIR'], cover)
    return path if os.path.isfile(path) else None


_CLIENT_LABELS = {
    'amule': 'aMule', 'qbittorrent': 'qBittorrent', 'rtorrent': 'rTorrent', 'deluge': 'Deluge',
    'telegram': 'Telegram', 'fourtoutici': 'fourtoutici', 'vk': 'VK', 'annas_archive': "Anna's Archive",
    'shelfmark': 'Shelfmark',
}


def notify_download_added(title, client, series_id=None, volume_number=None):
    """« Téléchargement ajouté », envoyée en arrière-plan : l'ajout au client (souvent un clic
    dans l'interface) n'attend jamais Telegram, ntfy ou les téléphones."""
    try:
        if not notification_wanted('download_added'):
            return
        import threading
        from flask import current_app
        app = current_app._get_current_object()
    except Exception as exc:
        print(f"Erreur notification (téléchargement ajouté) : {exc}")
        return

    def run():
        with app.app_context():
            try:
                lines = []
                if series_id:
                    import sqlite3
                    conn = sqlite3.connect(app.config['DATABASE'], timeout=30.0)
                    try:
                        row = conn.execute('SELECT title FROM series WHERE id = ?', (series_id,)).fetchone()
                    finally:
                        conn.close()
                    if row and row[0]:
                        lines.append(f"{row[0]} - Tome {volume_number}" if volume_number is not None else row[0])
                lines.append(f"Fichier : {title}")
                lines.append(f"Client : {_CLIENT_LABELS.get(client, client)}")
                send_notification('\n'.join(lines), kind='download_added', icon='⬇️',
                                  title='Téléchargement ajouté', tags=['arrow_down'],
                                  photo_path=series_cover_path(series_id))
            except Exception as exc:
                print(f"Erreur notification (téléchargement ajouté) : {exc}")

    threading.Thread(target=run, name='notify-download-added', daemon=True).start()
