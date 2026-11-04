"""Envoi des notifications de Bullarr sur tous les canaux configurés (Telegram, ntfy).

Chaque point d'envoi de l'app indique le TYPE de notification ; chaque canal a sa propre
case par type (Configuration → Notifications) :

- import_completed : un import a ajouté des fichiers à la bibliothèque ;
- import_available : de nouveaux fichiers sont arrivés dans les dossiers surveillés ;
- monitor : la surveillance a repéré un album manquant (action « notifier »).

Nécessite un contexte applicatif Flask (current_app), comme send_telegram_notification.
Ne lève jamais d'exception : une notification ratée ne fait pas échouer l'opération.
"""

KINDS = ('import_completed', 'import_available', 'monitor')


def _telegram_wants(kind):
    from blueprints.telegram.routes import load_telegram_config
    cfg = load_telegram_config()
    if not cfg.get('enabled'):
        return False
    # Telegram n'a pas de case pour la surveillance : elle notifie dès que le canal est actif.
    return kind == 'monitor' or bool(cfg.get(f'notify_{kind}', True))


def _ntfy_wants(kind):
    from blueprints.ntfy.routes import load_ntfy_config
    cfg = load_ntfy_config()
    return bool(cfg.get('enabled')) and bool(cfg.get(f'notify_{kind}', True))


def _channels(kind):
    channels = []
    for name, wants in (('telegram', _telegram_wants), ('ntfy', _ntfy_wants)):
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
            else:
                from blueprints.ntfy.routes import send_ntfy_notification
                ok, error = send_ntfy_notification(message, title=title, tags=tags, click=click,
                                                   photo_path=photo_path)
        except Exception as exc:
            ok, error = False, str(exc)
        if ok:
            sent = True
        else:
            errors.append(f"{channel} : {error}")
    return sent, errors
