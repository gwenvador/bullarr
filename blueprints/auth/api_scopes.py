"""
Droits (scopes) d'une clé API : une clé peut être limitée à quelques actions, par exemple
lire la bibliothèque et écrire les tags, sans rien pouvoir faire d'autre.

Une clé sans liste de droits (clé créée avant les scopes) garde l'accès complet. Une clé
personnelle reste en plus limitée aux droits de son compte (lecteur, bibliothèques permises).
"""

# (code, libellé, explication) dans l'ordre affiché.
API_SCOPES = [
    ('full', 'Accès complet', 'Tout ce que permet le compte, réglages compris.'),
    ('read', 'Lecture', 'Consulter bibliothèques, séries, tomes, nouveautés (requêtes GET). '
                        'Jamais les réglages, les comptes ni les clés.'),
    ('tags', 'Tags', 'Modifier les tags des séries.'),
    ('reading', 'Suivi de lecture', 'Lu / non lu, avancement de lecture, listes de lecture.'),
    ('metadata', 'Métadonnées', 'Corriger les métadonnées, les relire sur Bédéthèque.'),
    ('monitor', 'Surveillance', 'Surveiller ou non une série ou une bibliothèque.'),
    ('requests', 'Demandes', 'Créer, chercher ou annuler une demande de série.'),
]
SCOPE_CODES = [code for code, _, _ in API_SCOPES]
SCOPE_LABELS = {code: label for code, label, _ in API_SCOPES}

# Routes d'écriture permises par chaque droit (noms d'endpoint Flask).
WRITE_ENDPOINTS = {
    'tags': {'library.manage_series_tags'},
    'reading': {'library.post_series_read_status', 'library.post_volume_read_status',
                'library.post_volume_reader_progress', 'library.set_series_reading_lists'},
    'metadata': {'library.update_series_manual_metadata', 'library.update_volume_manual_metadata',
                 'bedetheque.update_metadata_series', 'bedetheque.update_metadata_volume'},
    'monitor': {'missing_monitor.configure_series_monitor', 'missing_monitor.configure_library_monitor'},
    'requests': {'library.create_request', 'library.cancel_request', 'library.lookup_requests'},
}
# Toujours permis à une clé valide : savoir à qui elle appartient et lister les routes.
ALWAYS_ALLOWED = {'auth.current_user', 'auth.api_docs'}
# Lectures réservées à l'accès complet : réglages (souvent avec des secrets), comptes, sauvegardes.
SENSITIVE_PREFIXES = ('/api/auth/', '/api/settings/')
SENSITIVE_ENDPOINTS = {'library.export_reading_history', 'library.get_reading_history_users'}
READ_METHODS = {'GET', 'HEAD', 'OPTIONS'}


def normalize_scopes(value):
    """Liste de droits connus, dans l'ordre d'affichage ; None = accès complet (ancienne clé)."""
    if value is None:
        return None
    if isinstance(value, str):
        value = [part.strip() for part in value.split(',')]
    if not isinstance(value, (list, tuple, set)):
        return None
    wanted = {str(code).strip() for code in value}
    if 'full' in wanted:
        return ['full']
    return [code for code in SCOPE_CODES if code in wanted]


def is_full(scopes):
    return scopes is None or 'full' in scopes


def is_sensitive_read(rule, endpoint):
    rule = rule or ''
    return (rule.startswith(SENSITIVE_PREFIXES) or '/config' in rule or rule.endswith('/schedule')
            or endpoint in SENSITIVE_ENDPOINTS)


def scope_allows(scopes, endpoint, method, rule):
    """True si une clé avec ces droits peut appeler cette route."""
    if is_full(scopes):
        return True
    if endpoint in ALWAYS_ALLOWED:
        return True
    if (method or 'GET').upper() in READ_METHODS:
        return 'read' in scopes and not is_sensitive_read(rule, endpoint)
    return any(endpoint in WRITE_ENDPOINTS.get(code, ()) for code in scopes)


def denied_message(scopes):
    names = ', '.join(SCOPE_LABELS[code] for code in scopes) if scopes else 'aucun droit'
    return f'Cette clé API ne permet pas cette action (droits : {names})'


def scopes_payload(scopes):
    """Pour l'interface : droits de la clé et liste de tous les droits possibles."""
    return {
        'scopes': ['full'] if is_full(scopes) else scopes,
        'available': [{'code': code, 'label': label, 'help': text} for code, label, text in API_SCOPES],
    }
