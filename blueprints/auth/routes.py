"""
Routes pour l'authentification SSO / OIDC
Utilise Authlib pour la découverte OIDC (issuer/.well-known/openid-configuration),
le PKCE et la validation du token d'identité — pas de JWT fait maison.
"""
from flask import request, jsonify, redirect, url_for, session, current_app, render_template, g
from authlib.integrations.flask_client import OAuth
from werkzeug.security import check_password_hash, generate_password_hash
import hmac
import secrets
import threading
import time
import zlib
import requests

from . import auth_bp
from encryption import decrypt
from error_utils import error_message
from .config_store import (AUTH_MODES, LEGACY_AUTH_MODES, load_oidc_config, save_oidc_config, normalize_issuer_url,
                           generate_api_key)
from . import accounts
from .activity import log_activity, list_activity
from .roles import (ROLES, ROLE_READER, PARENTAL_PRESET, all_genres, computed_role, current_role, normalize_groups,
                    reader_allowed, reader_scope_allowed, record_login, user_key)

# Endpoints toujours accessibles, même quand le SSO/OIDC est activé (sans quoi
# l'utilisateur ne pourrait jamais atteindre le flux de connexion ou les assets statiques)
_PUBLIC_ENDPOINTS = {
    'auth.login', 'auth.login_password', 'auth.login_redirect', 'auth.callback', 'auth.app_login',
    'auth.signup',
    'auth.logout', 'auth.logout_provider',
    'static', 'serve_cover',
    # Serveur MCP : authentifié par son propre jeton (BULLARR_MCP_TOKEN), pas par la session
    'mcp.mcp_endpoint',
}


# Connexion SSO de l'application Android. Le fournisseur (passkeys) ne fonctionne pas dans une
# WebView : l'app ouvre /login/redirect?app=1 dans le navigateur du téléphone ; à la fin du SSO,
# Bullarr redirige vers l'adresse FIXE bullarr://auth avec un code à usage unique (jamais une
# adresse fournie par le client), que l'app échange contre une session via POST /api/auth/app-login.
# Un seul worker gunicorn (voir gunicorn.conf.py) : un dictionnaire en mémoire suffit ; au
# redémarrage les codes en attente (60 s de validité) sont perdus, l'app recommence.
APP_LOGIN_REDIRECT = 'bullarr://auth'
APP_LOGIN_CODE_TTL_SECONDS = 60
APP_LOGIN_MAX_PENDING_CODES = 100
APP_LOGIN_MAX_FAILURES_PER_MINUTE = 10
_app_login_codes = {}
_app_login_failures = {}
_app_login_lock = threading.Lock()


# Connexion par mot de passe : même limite que l'échange de code de l'app, par adresse.
PASSWORD_LOGIN_MAX_FAILURES_PER_MINUTE = 10
_password_login_failures = {}


def _password_login_blocked(address):
    """True si cette adresse a déjà raté PASSWORD_LOGIN_MAX_FAILURES_PER_MINUTE connexions
    dans la dernière minute (sans limite, un mot de passe pouvait être deviné en boucle)."""
    now = time.monotonic()
    with _app_login_lock:
        failures = [t for t in _password_login_failures.get(address, []) if now - t < 60]
        if failures:
            _password_login_failures[address] = failures
        else:
            _password_login_failures.pop(address, None)
        return len(failures) >= PASSWORD_LOGIN_MAX_FAILURES_PER_MINUTE


def _record_password_login_failure(address):
    now = time.monotonic()
    with _app_login_lock:
        failures = [t for t in _password_login_failures.get(address, []) if now - t < 60]
        failures.append(now)
        _password_login_failures[address] = failures


def _purge_app_login_state(now):
    for code in [code for code, (expires, _user) in _app_login_codes.items() if expires <= now]:
        del _app_login_codes[code]
    for address in [a for a, times in _app_login_failures.items() if not [t for t in times if now - t < 60]]:
        del _app_login_failures[address]


def _issue_app_login_code(user):
    now = time.monotonic()
    code = secrets.token_urlsafe(32)
    with _app_login_lock:
        _purge_app_login_state(now)
        if len(_app_login_codes) >= APP_LOGIN_MAX_PENDING_CODES:
            oldest = min(_app_login_codes, key=lambda key: _app_login_codes[key][0])
            del _app_login_codes[oldest]
        _app_login_codes[code] = (now + APP_LOGIN_CODE_TTL_SECONDS, dict(user))
    return code


def _consume_app_login_code(code, address):
    """Utilisateur lié au code (le code est détruit), None si inconnu/expiré, False si trop d'échecs."""
    now = time.monotonic()
    with _app_login_lock:
        _purge_app_login_state(now)
        failures = [t for t in _app_login_failures.get(address, []) if now - t < 60]
        if len(failures) >= APP_LOGIN_MAX_FAILURES_PER_MINUTE:
            return False
        entry = _app_login_codes.pop(code, None) if isinstance(code, str) and code else None
        if entry is None or entry[0] <= now:
            failures.append(now)
            _app_login_failures[address] = failures
            return None
        return entry[1]


def _auth_mode(config):
    # Un ancien mode non normalisé (password / oidc / both) reste une authentification.
    mode = config.get('mode')
    return 'auth' if mode == 'auth' or mode in LEGACY_AUTH_MODES else 'none'


def _password_enabled(config):
    """Avec l'authentification, le formulaire des comptes locaux est toujours proposé."""
    return _auth_mode(config) == 'auth'


def _oidc_enabled(config):
    return _auth_mode(config) == 'auth' and bool(config.get('oidc_enabled'))


def _oidc_ready(config):
    """SSO activé ET suffisamment configuré pour démarrer le flux."""
    return _oidc_enabled(config) and bool(config.get('issuer')) and bool(config.get('client_id'))


def _supplied_api_key():
    """Clé API envoyée par un client externe : en-tête X-Api-Key (ou Authorization: Bearer),
    ou paramètre ?apikey= pour les clients qui ne savent pas ajouter d'en-tête."""
    key = request.headers.get('X-Api-Key') or request.args.get('apikey') or ''
    if not key:
        auth = request.headers.get('Authorization', '')
        if auth.lower().startswith('bearer '):
            key = auth[7:]
    return key.strip()


def _api_key_status(config):
    """None si aucune clé n'est fournie, True si elle est valide, False sinon.

    Une clé n'est acceptée que sur les routes /api/ : elle ne donne pas accès aux pages."""
    supplied = _supplied_api_key()
    if not supplied or not request.path.startswith('/api/'):
        return None
    expected = config.get('api_key_decrypted') or ''
    return bool(expected) and hmac.compare_digest(expected, supplied)


def _signup_enabled(config):
    return _auth_mode(config) == 'auth' and bool(config.get('allow_signup'))


def _render_login(config, error=None, status=200, view='login', notice=None, form=None):
    """Page de connexion (formulaire et/ou bouton SSO) ou d'inscription (view='signup')."""
    oidc_ready = _oidc_ready(config)
    if view == 'login' and _oidc_enabled(config) and not oidc_ready and not error:
        error = "Configuration OIDC incomplète : vérifiez l'issuer et le client ID dans les paramètres."
    return render_template(
        'login.html', password_enabled=_password_enabled(config), oidc_enabled=oidc_ready, error=error,
        signup_enabled=_signup_enabled(config), view=view, notice=notice, form=form or {},
    ), status


def _get_oidc_client(config):
    """Construit un client OAuth Authlib à partir de la configuration OIDC sauvegardée"""
    issuer = normalize_issuer_url(config.get('issuer', ''))
    client_id = config.get('client_id', '')
    client_secret = config.get('client_secret_decrypted') or decrypt(config.get('client_secret', '')) or ''
    scopes = config.get('scopes') or 'openid profile email'
    if (config.get('admin_group') or '').strip() and 'groups' not in scopes.split():
        scopes = f'{scopes} groups'

    oauth = OAuth(current_app)
    oauth.register(
        name='oidc',
        client_id=client_id,
        client_secret=client_secret,
        server_metadata_url=f"{issuer}/.well-known/openid-configuration",
        client_kwargs={
            'scope': scopes,
            # PKCE : Authlib génère et vérifie automatiquement le code_verifier/code_challenge
            'code_challenge_method': 'S256'
        },
    )
    return oauth.oidc


@auth_bp.before_app_request
def enforce_login():
    """Bloque toute requête non authentifiée quand le SSO/OIDC est activé.

    Quand l'intégration est désactivée (valeur par défaut), cette fonction ne fait
    strictement rien : l'application se comporte exactement comme avant.
    """
    if request.endpoint is None or request.endpoint in _PUBLIC_ENDPOINTS:
        return None

    # Explicit emergency recovery switch, supplied only through .env/container env.
    if current_app.config.get('AUTH_BYPASS_LOGIN', False):
        return None

    config = load_oidc_config()

    # Accès API : une clé valide remplace la session et le jeton CSRF ; une clé fausse
    # est refusée même sans authentification, pour que le client voie son erreur.
    api_key_ok = _api_key_status(config)
    if api_key_ok is True:
        g.api_key_auth = True
        return None
    if api_key_ok is False:
        return jsonify({'success': False, 'error': 'Clé API invalide'}), 401

    mode = _auth_mode(config)
    if mode == 'none':
        return None

    if session.get('user') and accounts.session_revoked(user_key(session['user']), session.get('login_at')):
        session.clear()
        if request.path.startswith('/api/'):
            return jsonify({'success': False, 'error': 'Session expirée, reconnectez-vous'}), 401
        return redirect(url_for('auth.login'))

    if session.get('user'):
        if request.method not in {'GET', 'HEAD', 'OPTIONS'}:
            expected = session.get('csrf_token')
            supplied = request.headers.get('X-CSRF-Token', '')
            if not expected or not hmac.compare_digest(expected, supplied):
                return jsonify({'success': False, 'error': 'Jeton de sécurité manquant ou invalide'}), 403
        # Compte lecteur : seules les routes de READER_ENDPOINTS (roles.py) sont permises.
        is_reader_account = current_role(config) == ROLE_READER
        if is_reader_account and not reader_allowed(request.endpoint, request.method):
            if request.path.startswith('/api/') or request.method != 'GET':
                return jsonify({'success': False, 'error': 'Réservé aux administrateurs'}), 403
            return redirect('/')
        # Bibliothèques permises et contrôle parental (genres masqués) d'un lecteur.
        if is_reader_account and not reader_scope_allowed(
                request.view_args, request.get_json(silent=True) if request.method == 'POST' else None):
            if request.path.startswith('/api/'):
                return jsonify({'success': False, 'error': 'Contenu non disponible pour ce compte'}), 404
            return redirect('/')
        return None

    # Keep the post-login destination same-origin. Storing request.url trusted the
    # client-controlled Host/X-Forwarded-Host header and could turn the callback into
    # an open redirect after a successful login.
    if (request.path.startswith('/api/') and request.accept_mimetypes.accept_json
            and not request.accept_mimetypes.accept_html):
        return jsonify({'success': False, 'error': 'Authentification requise : envoyez la clé API dans l\'en-tête X-Api-Key'}), 401
    # Seule une page ouverte par l'utilisateur sert de retour après connexion : un appel
    # d'arrière-plan (badges de la barre, /api/import/pending-count…) arrivé après
    # l'expiration de la session renvoyait sinon sur du JSON une fois connecté.
    if request.method == 'GET' and not request.path.startswith('/api/'):
        session['next_url'] = request.full_path if request.query_string else request.path
    return redirect(url_for('auth.login'))


@auth_bp.route('/login')
def login():
    """Page de connexion: formulaire identifiant/mot de passe et/ou bouton SSO selon le mode.

    Aucune redirection automatique vers le SSO par défaut: elle n'a lieu que si
    « auto_redirect » est activé, et jamais avec ?noauto=1 (accès au formulaire, retour
    après une déconnexion)."""
    config = load_oidc_config()
    if _auth_mode(config) == 'none':
        return redirect('/')
    if _oidc_ready(config) and config.get('auto_redirect') and not request.args.get('noauto'):
        return redirect(url_for('auth.login_redirect'))
    return _render_login(config)


@auth_bp.route('/signup', methods=['GET', 'POST'])
def signup():
    """Demande de compte : créé en attente, un administrateur le valide ou le refuse."""
    config = load_oidc_config()
    if not _signup_enabled(config):
        return redirect(url_for('auth.login'))
    if request.method == 'GET':
        return _render_login(config, view='signup')
    address = request.remote_addr or ''
    form = {key: (request.form.get(key) or '').strip() for key in ('username', 'name', 'email')}
    if _password_login_blocked(address):
        return _render_login(config, view='signup', form=form, status=429,
                             error='Trop de tentatives. Réessayez dans une minute.')
    _record_password_login_failure(address)  # chaque demande compte dans la limite par adresse
    password = request.form.get('password') or ''
    if password != (request.form.get('password_confirm') or ''):
        return _render_login(config, view='signup', form=form, status=400,
                             error='Les deux mots de passe ne correspondent pas.')
    try:
        key = accounts.request_account(form['username'], password, name=form['name'], email=form['email'])
    except accounts.AccountError as error:
        return _render_login(config, view='signup', form=form, status=400, error=str(error))
    who = form['name'] or form['username']
    log_activity('signup_request', actor_key=key, actor_name=who, target_key=key, target_name=who,
                 detail=' · '.join(filter(None, [form['username'], form['email'], address])))
    try:
        from notifications import send_notification
        send_notification(f"{who} ({form['username']}) demande un compte.\n"
                          "À valider dans la page Validation.",
                          kind='account_request', title='Demande de compte', icon='👤', tags=['bust_in_silhouette'])
    except Exception:
        current_app.logger.exception('Notification de demande de compte')
    return _render_login(config, notice=(
        'Demande envoyée. Vous pourrez vous connecter dès qu’un administrateur aura validé votre compte.'))


@auth_bp.route('/login/password', methods=['POST'])
def login_password():
    config = load_oidc_config()
    address = request.remote_addr or ''
    if _password_login_blocked(address):
        return _render_login(config, error='Trop de tentatives de connexion. Réessayez dans une minute.', status=429)
    username = (request.form.get('username') or '').strip()
    password = request.form.get('password') or ''
    row = accounts.authenticate(username, password) if _password_enabled(config) else None
    if row is None:
        _record_password_login_failure(address)
        log_activity('login_failed', actor_key=None, actor_name=username[:80] or '?',
                     target_name=username[:80] or None, success=False,
                     detail=f'Par mot de passe · IP {address}')
        return _render_login(config, error='Identifiant ou mot de passe incorrect.', status=401)
    if row['status'] == accounts.STATUS_PENDING:
        log_activity('login_failed', actor_key=row['user_key'], actor_name=row['name'] or username,
                     success=False, detail=f'Compte en attente de validation · IP {address}')
        return _render_login(config, error='Votre demande de compte attend la validation d’un administrateur.', status=403)
    with _app_login_lock:
        _password_login_failures.pop(address, None)
    next_url = session.get('next_url') or '/'
    session.clear()
    session['user'] = {'name': username, 'auth_method': 'password'}
    session['login_at'] = time.time()
    try:
        record_login(session['user'])
    except Exception:
        current_app.logger.exception('Compte non enregistré')
    log_activity('login', detail=f'Par mot de passe · IP {address}')
    return redirect(next_url)


@auth_bp.route('/login/redirect')
def login_redirect():
    """Effectue la découverte OIDC et la redirection vers le fournisseur SSO"""
    config = load_oidc_config()
    if not _oidc_enabled(config):
        return redirect('/')
    if not _oidc_ready(config):
        return _render_login(config, status=500)

    # Connexion depuis l'application Android : à la fin du SSO, retour vers l'app (voir callback).
    if request.args.get('app') == '1':
        session['app_login'] = True
    else:
        session.pop('app_login', None)

    try:
        oidc_client = _get_oidc_client(config)
        redirect_uri = url_for('auth.callback', _external=True)
        return oidc_client.authorize_redirect(redirect_uri)
    except Exception as e:
        return _render_login(config, error=f"Erreur lors de la connexion au fournisseur SSO : {e}", status=500)


@auth_bp.route('/callback')
def callback():
    """Termine l'échange de tokens OIDC et ouvre la session utilisateur"""
    config = load_oidc_config()
    if not _oidc_enabled(config):
        return redirect('/')

    try:
        oidc_client = _get_oidc_client(config)
        token = oidc_client.authorize_access_token()
        userinfo = token.get('userinfo') or oidc_client.userinfo(token=token)

        user = {
            'sub': userinfo.get('sub'),
            'email': userinfo.get('email'),
            'name': userinfo.get('name') or userinfo.get('preferred_username') or userinfo.get('email'),
            'groups': normalize_groups(userinfo.get('groups')),
            'auth_method': 'oidc',
        }
        try:
            record_login(user)
        except Exception:
            current_app.logger.exception('Compte non enregistré')

        log_activity('login', actor_key=user_key(user), actor_name=user.get('name') or user.get('email'),
                     detail=('Par SSO · application Android' if session.get('app_login') else 'Par SSO')
                     + f" · IP {request.remote_addr or '-'}")
        if session.pop('app_login', False):
            # Application Android : pas de session dans le navigateur, un code à usage unique
            # vers l'adresse fixe de l'app (jamais une adresse venant de la requête).
            return redirect(f'{APP_LOGIN_REDIRECT}?code={_issue_app_login_code(user)}')

        session['user'] = user
        session['login_at'] = time.time()
        next_url = session.pop('next_url', None) or '/'
        return redirect(next_url)
    except Exception as e:
        log_activity('login_failed', actor_name='SSO', success=False, detail=f'SSO · {e}'[:300])
        return _render_login(config, error=f"Erreur lors de l'authentification SSO : {e}", status=500)


@auth_bp.route('/api/auth/app-login', methods=['POST'])
def app_login():
    """Échange le code reçu par l'application Android (bullarr://auth?code=...) contre une session.

    Publique, sans jeton CSRF : le code est la preuve, valable 60 s et utilisable une seule fois.
    La réponse porte le cookie de session ; /api/auth/csrf donne ensuite le jeton des écritures."""
    config = load_oidc_config()
    if not _oidc_enabled(config):
        return jsonify({'success': False, 'error': 'Connexion SSO non activée'}), 400
    data = request.get_json(silent=True) or {}
    user = _consume_app_login_code(data.get('code'), request.remote_addr or '-')
    if user is False:
        return jsonify({'success': False, 'error': 'Trop de tentatives, réessayez dans une minute'}), 429
    if user is None:
        return jsonify({'success': False, 'error': 'Code invalide ou expiré'}), 400
    session.clear()
    session['user'] = {**user, 'auth_method': 'oidc-app'}
    session['login_at'] = time.time()
    session['csrf_token'] = secrets.token_urlsafe(32)
    return jsonify({'success': True, 'user': session['user']})


@auth_bp.route('/logout')
def logout():
    """Termine la session locale et affiche une confirmation de déconnexion.

    La déconnexion côté fournisseur OIDC (round-trip réseau) n'est pas déclenchée
    automatiquement : elle est proposée en lien optionnel sur la page de confirmation,
    pour que /logout reste instantané plutôt que de laisser le navigateur en attente
    d'une requête réseau sans rien afficher."""
    config = load_oidc_config()
    if session.get('user'):
        log_activity('logout')
    session.pop('user', None)
    session.pop('next_url', None)

    show_provider_link = bool(_oidc_enabled(config) and config.get('issuer'))
    return render_template('logout.html', show_provider_link=show_provider_link)


@auth_bp.route('/logout/provider')
def logout_provider():
    """Termine aussi la session côté fournisseur OIDC (lien optionnel depuis /logout)"""
    config = load_oidc_config()

    if not _oidc_enabled(config) or not config.get('issuer'):
        return redirect(url_for('auth.login'))

    try:
        issuer = normalize_issuer_url(config['issuer'])
        metadata = requests.get(f"{issuer}/.well-known/openid-configuration", timeout=5).json()
        end_session_endpoint = metadata.get('end_session_endpoint')

        if end_session_endpoint:
            base_url = request.url_root.rstrip('/')
            return redirect(f"{end_session_endpoint}?post_logout_redirect_uri={base_url}")
    except Exception as e:
        print(f"Erreur déconnexion côté fournisseur SSO : {e}")

    return redirect(url_for('auth.login'))


@auth_bp.route('/api/auth/me')
def current_user():
    """Retourne l'utilisateur de la session courante (ou null si non connecté)"""
    user = session.get('user')
    profile = None
    if user:
        try:
            row = accounts.get_user(user_key(user))
            profile = {'name': (row['name'] if row else None) or user.get('name') or user.get('email'),
                       'avatar_url': avatar_url(row) if row else None}
        except Exception:
            profile = None
    return jsonify({'user': user, 'role': current_role(), 'profile': profile})


@auth_bp.route('/api/auth/csrf')
def csrf_token():
    """Provide a per-session token for same-origin state-changing requests."""
    if 'csrf_token' not in session:
        session['csrf_token'] = secrets.token_urlsafe(32)
    return jsonify({'token': session['csrf_token']})


@auth_bp.route('/api/auth/config', methods=['GET', 'POST'])
def oidc_config():
    """Configuration SSO / OIDC"""

    if request.method == 'GET':
        config = load_oidc_config()

        return jsonify({
            'mode': _auth_mode(config),
            'oidc_enabled': bool(config.get('oidc_enabled')),
            'enabled': _auth_mode(config) != 'none',
            'auto_redirect': bool(config.get('auto_redirect', False)),
            'issuer': config.get('issuer', ''),
            'client_id': config.get('client_id', ''),
            'client_secret': '****' if config.get('client_secret') else '',
            'scopes': config.get('scopes', 'openid profile email'),
            'admin_group': config.get('admin_group', ''),
        })

    else:  # POST
        try:
            new_config = request.get_json()
            config = load_oidc_config()

            mode = new_config.get('mode', 'none')
            if mode in LEGACY_AUTH_MODES:  # anciens clients (application, scripts)
                oidc_enabled = LEGACY_AUTH_MODES[mode][0]
                mode = 'auth'
            elif mode in AUTH_MODES:
                oidc_enabled = bool(new_config.get('oidc_enabled'))
            else:
                return jsonify({'success': False, 'error': 'Mode d’authentification invalide'}), 400
            config['mode'] = mode
            config['enabled'] = mode != 'none'
            config['oidc_enabled'] = oidc_enabled
            config.pop('username', None)
            config.pop('password_hash', None)
            config['issuer'] = normalize_issuer_url(new_config.get('issuer', ''))
            config['client_id'] = new_config.get('client_id', '').strip()
            config['scopes'] = (new_config.get('scopes') or 'openid profile email').strip()
            if oidc_enabled and (not config['issuer'] or not config['client_id']):
                return jsonify({'success': False, 'error': "L'URL de l'issuer et le Client ID sont requis"}), 400
            # Ne pas se fermer la porte : sans OIDC, il faut un compte administrateur local.
            if mode == 'auth' and not oidc_enabled and accounts.local_admin_count() == 0:
                return jsonify({'success': False, 'error': (
                    "Créez d'abord un compte administrateur (Utilisateurs et rôles, ci-dessous) "
                    "ou activez la connexion OIDC")}), 400
            config['auto_redirect'] = bool(new_config.get('auto_redirect', False))

            # Ne change le secret client que s'il n'est pas masqué
            new_secret = new_config.get('client_secret', '')
            if new_secret and new_secret != '****':
                config['client_secret_decrypted'] = new_secret

            if save_oidc_config(config):
                return jsonify({'success': True})
            else:
                return jsonify({'success': False, 'error': 'Erreur de sauvegarde'}), 500

        except Exception as e:
            return jsonify({'success': False, 'error': 'Erreur interne'}), 500


@auth_bp.route('/api/auth/users', methods=['GET'])
def list_users():
    """Comptes locaux et comptes OIDC déjà connectés, avec leur rôle et leurs accès."""
    import json as _json
    from .roles import _connect
    config = load_oidc_config()
    admin_group = (config.get('admin_group') or '').strip()
    conn = _connect()
    try:
        rows = conn.execute("SELECT * FROM users ORDER BY auth_method != 'password', name COLLATE NOCASE").fetchall()
    finally:
        conn.close()
    me = user_key(session.get('user'))
    users = []
    for row in rows:
        groups = _json.loads(row['groups_json'] or '[]')
        local = row['auth_method'] == 'password'
        computed = 'admin' if local else computed_role(groups, admin_group)
        users.append({
            'key': row['user_key'], 'name': row['name'], 'email': row['email'],
            'username': row['user_key'].split(':', 1)[1] if local else None,
            'type': 'local' if local else 'oidc', 'auth_method': row['auth_method'], 'groups': groups,
            'computed_role': computed, 'role_override': row['role_override'],
            'role': row['role_override'] if row['role_override'] in ROLES else computed,
            'is_me': row['user_key'] == me, 'last_seen': row['last_seen'], 'first_seen': row['first_seen'],
            'status': row['status'] or 'active', 'avatar_url': avatar_url(row), 'avatar_kind': avatar_kind(row),
            'allowed_libraries': _json.loads(row['allowed_libraries_json']) if row['allowed_libraries_json'] else None,
            'blocked_genres': _json.loads(row['blocked_genres_json']) if row['blocked_genres_json'] else [],
        })
    return jsonify({'success': True, 'admin_group': admin_group, 'users': users,
                    'oidc_enabled': _oidc_enabled(config), 'allow_signup': bool(config.get('allow_signup')),
                    'auto_approve_requests': bool(config.get('auto_approve_requests')),
                    'pending_count': sum(1 for user in users if user['status'] == 'pending')})


@auth_bp.route('/api/auth/users/approve', methods=['POST'])
def approve_user():
    """Valide une demande d'inscription (rôle Lecteur par défaut). Refuser = DELETE."""
    data = request.get_json(silent=True) or {}
    try:
        approved = accounts.approve_account(data.get('key') or '', data.get('role') or 'reader')
    except accounts.AccountError as error:
        return jsonify({'success': False, 'error': error_message(error)}), 400
    if not approved:
        return jsonify({'success': False, 'error': 'Demande introuvable'}), 404
    row = accounts.get_user(data.get('key') or '')
    log_activity('account_approved', target_key=data.get('key'), target_name=row['name'] if row else None,
                 detail='Rôle : ' + ('Admin' if (data.get('role') or 'reader') == 'admin' else 'Lecteur'))
    return jsonify({'success': True})


@auth_bp.route('/api/auth/signup', methods=['PUT'])
def set_signup():
    """Active ou non le lien « Créer un compte » de la page de connexion."""
    data = request.get_json(silent=True) or {}
    config = load_oidc_config()
    config['allow_signup'] = bool(data.get('enabled'))
    if save_oidc_config(config):
        log_activity('settings_changed', detail='Inscription ouverte : ' + ('activée' if config['allow_signup'] else 'désactivée'))
        return jsonify({'success': True})
    return jsonify({'success': False, 'error': 'Erreur de sauvegarde'}), 500


def _last_local_admin(key):
    """Vrai si retirer ce compte administrateur local fermerait la porte : authentification
    active sans OIDC et aucun autre administrateur local."""
    config = load_oidc_config()
    if _auth_mode(config) != 'auth' or _oidc_enabled(config):
        return False
    row = accounts.get_user(key)
    if not row or row['auth_method'] != 'password' or (row['role_override'] or 'admin') != 'admin':
        return False
    return accounts.local_admin_count(exclude_key=key) == 0


def _set_role(key, role):
    """Rôle forcé (admin / reader) ou rôle calculé (None ; un compte local redevient admin)."""
    from .roles import _connect
    if role not in (*ROLES, None):
        return jsonify({'success': False, 'error': 'Rôle invalide'}), 400
    if key == user_key(session.get('user')) and role == ROLE_READER:
        return jsonify({'success': False, 'error': 'Vous ne pouvez pas retirer vos propres droits d\'administrateur'}), 400
    if role == ROLE_READER and _last_local_admin(key):
        return jsonify({'success': False, 'error': 'C\'est le dernier administrateur local : créez-en un autre ou activez OIDC'}), 400
    conn = _connect()
    try:
        changed = conn.execute('UPDATE users SET role_override = ? WHERE user_key = ?', (role, key)).rowcount
        conn.commit()
    finally:
        conn.close()
    if not changed:
        return jsonify({'success': False, 'error': 'Compte introuvable'}), 404
    return None


@auth_bp.route('/api/auth/users/role', methods=['PUT'])
def set_user_role():
    """Force le rôle d'un compte (admin / reader), ou revient au rôle calculé (null)."""
    data = request.get_json(silent=True) or {}
    failure = _set_role(data.get('key') or '', data.get('role'))
    if failure:
        return failure
    _log_role(data.get('key') or '', data.get('role'))
    return jsonify({'success': True})


def _role_label(role):
    return {'admin': 'Admin', 'reader': 'Lecteur'}.get(role, 'automatique')


def _log_role(key, role):
    row = accounts.get_user(key)
    log_activity('role_changed', target_key=key, target_name=row['name'] if row else key,
                 detail=f'Rôle : {_role_label(role)}')


@auth_bp.route('/api/auth/users', methods=['POST'])
def create_user():
    """Crée un compte local : identifiant, mot de passe, nom, e-mail, rôle."""
    data = request.get_json(silent=True) or {}
    try:
        key = accounts.create_local_account(data.get('username'), data.get('password') or '',
                                            name=data.get('name'), email=data.get('email'),
                                            role=data.get('role') or 'reader')
    except accounts.AccountError as error:
        return jsonify({'success': False, 'error': error_message(error)}), 400
    log_activity('account_created', target_key=key, target_name=data.get('name') or data.get('username'),
                 detail=f"Compte local {data.get('username')} · {_role_label(data.get('role') or 'reader')}")
    return jsonify({'success': True, 'key': key})


@auth_bp.route('/api/auth/users', methods=['PUT'])
def edit_user():
    """Modifie un compte : nom, e-mail et mot de passe (comptes locaux), rôle (tous)."""
    data = request.get_json(silent=True) or {}
    key = data.get('key') or ''
    row = accounts.get_user(key)
    if not row:
        return jsonify({'success': False, 'error': 'Compte introuvable'}), 404
    if 'role' in data:
        failure = _set_role(key, data.get('role'))
        if failure:
            return failure
    if row['auth_method'] == 'password':
        try:
            accounts.update_account(key, name=data.get('name'), email=data.get('email'),
                                    password=data.get('password') or None)
        except accounts.AccountError as error:
            return jsonify({'success': False, 'error': error_message(error)}), 400
        if data.get('password') and key == user_key(session.get('user')):
            session['login_at'] = time.time()  # garder sa propre session après le changement
    changes = []
    if row['auth_method'] == 'password':
        if data.get('name') is not None and (data.get('name') or '').strip() != (row['name'] or ''):
            changes.append('nom')
        if data.get('email') is not None and (data.get('email') or '').strip() != (row['email'] or ''):
            changes.append('e-mail')
        if data.get('password'):
            changes.append('mot de passe')
    if 'role' in data and data.get('role') != row['role_override']:
        changes.append(f"rôle : {_role_label(data.get('role'))}")
    if changes:
        log_activity('account_edited', target_key=key, target_name=data.get('name') or row['name'],
                     detail='Modifié : ' + ', '.join(changes))
    return jsonify({'success': True})


@auth_bp.route('/api/auth/users', methods=['DELETE'])
def delete_user():
    """Supprime un compte et coupe ses sessions ouvertes."""
    data = request.get_json(silent=True) or {}
    key = data.get('key') or ''
    if key == user_key(session.get('user')):
        return jsonify({'success': False, 'error': 'Vous ne pouvez pas supprimer votre propre compte'}), 400
    if _last_local_admin(key):
        return jsonify({'success': False, 'error': 'C\'est le dernier administrateur local : créez-en un autre ou activez OIDC'}), 400
    row = accounts.get_user(key)
    if not accounts.delete_account(key):
        return jsonify({'success': False, 'error': 'Compte introuvable'}), 404
    _remove_avatar_file(row['avatar'] if row else None)
    pending = bool(row and row['status'] == accounts.STATUS_PENDING)
    log_activity('account_rejected' if pending else 'account_deleted', target_key=key,
                 target_name=(row['name'] if row else None) or key,
                 detail='Demande d’inscription refusée' if pending else
                 ('Compte local' if row and row['auth_method'] == 'password' else 'Compte OIDC'))
    return jsonify({'success': True})


@auth_bp.route('/api/auth/users/access', methods=['PUT'])
def set_user_access():
    """Ce qu'un lecteur peut voir : bibliothèques permises (null = toutes) et genres
    Bédéthèque masqués (contrôle parental). Sans effet sur un administrateur."""
    import json as _json
    from .roles import _connect
    data = request.get_json(silent=True) or {}
    key = data.get('key') or ''
    allowed = data.get('allowed_libraries')
    if allowed is not None:
        if not isinstance(allowed, list) or not all(str(i).isdigit() for i in allowed):
            return jsonify({'success': False, 'error': 'Bibliothèques invalides'}), 400
        allowed = sorted({int(i) for i in allowed})
    genres = data.get('blocked_genres') or []
    if not isinstance(genres, list):
        return jsonify({'success': False, 'error': 'Genres invalides'}), 400
    genres = sorted({str(gn).strip()[:80] for gn in genres if str(gn).strip()}, key=str.casefold)
    conn = _connect()
    try:
        cursor = conn.execute('UPDATE users SET allowed_libraries_json = ?, blocked_genres_json = ? WHERE user_key = ?',
                              (_json.dumps(allowed) if allowed is not None else None,
                               _json.dumps(genres, ensure_ascii=False) if genres else None, key))
        conn.commit()
        changed = cursor.rowcount
    finally:
        conn.close()
    if not changed:
        return jsonify({'success': False, 'error': 'Compte introuvable'}), 404
    row = accounts.get_user(key)
    log_activity('access_changed', target_key=key, target_name=row['name'] if row else key,
                 detail=('Bibliothèques : ' + ('toutes' if allowed is None else str(len(allowed))))
                 + (f" · {len(genres)} genre(s) masqué(s)" if genres else ''))
    return jsonify({'success': True})


@auth_bp.route('/api/auth/activity', methods=['GET'])
def user_activity():
    """Journal des comptes et des demandes (page Historique, administrateurs)."""
    try:
        limit = max(1, min(int(request.args.get('limit', 500)), 2000))
    except ValueError:
        limit = 500
    return jsonify({'success': True, 'entries': list_activity(limit)})


# ===== Profil : chaque compte modifie son nom, son e-mail, son mot de passe (comptes
# locaux) et sa photo (tous). =====
AVATAR_SIZE = 256
AVATAR_MAX_BYTES = 8 * 1024 * 1024


def _avatar_dir():
    import os
    path = os.path.join(current_app.config['DATA_DIR'], 'avatars')
    os.makedirs(path, exist_ok=True)
    return path


# Avatars proposés (dessins SVG de static/img/avatars). Un compte sans photo ni avatar choisi
# en reçoit un d'office, toujours le même pour un compte donné (tiré de sa clé).
AVATAR_PRESETS = ('chat', 'renard', 'hibou', 'ours', 'panda', 'manchot', 'grenouille', 'lapin',
                  'lion', 'koala', 'pieuvre', 'robot', 'astronaute', 'fantome', 'alien', 'bulle')
PRESET_PREFIX = 'preset:'


def _preset_url(name):
    return f'/static/img/avatars/{name}.svg'


def _default_preset(key):
    return AVATAR_PRESETS[zlib.crc32((key or '?').encode()) % len(AVATAR_PRESETS)]


def avatar_url(row):
    try:
        name = row['avatar']
    except (KeyError, IndexError, TypeError):
        return None
    if name and name.startswith(PRESET_PREFIX):
        preset = name[len(PRESET_PREFIX):]
        return _preset_url(preset if preset in AVATAR_PRESETS else _default_preset(row['user_key']))
    if name:
        return f'/avatars/{name}'
    try:
        return _preset_url(_default_preset(row['user_key']))
    except (KeyError, IndexError, TypeError):
        return None


def avatar_kind(row):
    """'photo' (image envoyée), 'preset' (avatar choisi) ou 'default' (attribué d'office)."""
    try:
        name = row['avatar']
    except (KeyError, IndexError, TypeError):
        name = None
    if not name:
        return 'default'
    return 'preset' if name.startswith(PRESET_PREFIX) else 'photo'


def _remove_avatar_file(name):
    import os
    if not name or '/' in name or '..' in name or name.startswith(PRESET_PREFIX):
        return
    try:
        os.remove(os.path.join(_avatar_dir(), name))
    except OSError:
        pass


def _my_row():
    key = user_key(session.get('user'))
    return key, (accounts.get_user(key) if key else None)


def _profile_payload(key, row):
    user = session.get('user') or {}
    local = bool(row) and row['auth_method'] == 'password'
    return {
        'key': key, 'type': 'local' if local else 'oidc',
        'username': key.split(':', 1)[1] if local and key else None,
        'name': (row['name'] if row else None) or user.get('name'),
        'email': (row['email'] if row else None) or user.get('email'),
        'role': current_role(), 'avatar_url': avatar_url(row) if row else None,
        'avatar_kind': avatar_kind(row) if row else 'default',
        'first_seen': row['first_seen'] if row else None, 'last_seen': row['last_seen'] if row else None,
        'can_edit_identity': local,
    }


@auth_bp.route('/profil')
def profile_page():
    if not session.get('user'):
        return redirect('/')
    return render_template('profile.html')


@auth_bp.route('/api/auth/profile', methods=['GET'])
def get_profile():
    key, row = _my_row()
    if not key:
        return jsonify({'success': False, 'error': 'Aucun compte connecté'}), 400
    return jsonify({'success': True, 'profile': _profile_payload(key, row)})


@auth_bp.route('/api/auth/profile', methods=['PUT'])
def update_profile():
    """Nom, e-mail et mot de passe de son propre compte local (mot de passe actuel requis
    pour en choisir un nouveau). Un compte OIDC se modifie dans Pocket ID."""
    key, row = _my_row()
    if not row:
        return jsonify({'success': False, 'error': 'Compte introuvable'}), 404
    if row['auth_method'] != 'password':
        return jsonify({'success': False, 'error': 'Votre nom, e-mail et mot de passe se changent chez votre fournisseur SSO'}), 400
    data = request.get_json(silent=True) or {}
    new_password = data.get('new_password') or ''
    if new_password:
        if new_password != (data.get('new_password_confirm') or ''):
            return jsonify({'success': False, 'error': 'Les deux nouveaux mots de passe ne correspondent pas'}), 400
        if not accounts.authenticate(key.split(':', 1)[1], data.get('current_password') or ''):
            log_activity('password_change_failed', target_key=key, target_name=row['name'], success=False,
                         detail='Mot de passe actuel incorrect')
            return jsonify({'success': False, 'error': 'Mot de passe actuel incorrect'}), 403
    try:
        accounts.update_account(key, name=data.get('name'), email=data.get('email'), password=new_password or None)
    except accounts.AccountError as error:
        return jsonify({'success': False, 'error': error_message(error)}), 400
    changes = []
    if data.get('name') is not None and (data.get('name') or '').strip() != (row['name'] or ''):
        changes.append('nom')
    if data.get('email') is not None and (data.get('email') or '').strip() != (row['email'] or ''):
        changes.append('e-mail')
    if new_password:
        changes.append('mot de passe')
        session['login_at'] = time.time()  # garder cette session, couper les autres
    if changes:
        log_activity('profile_edited', target_key=key, target_name=data.get('name') or row['name'],
                     detail='Modifié : ' + ', '.join(changes))
    return jsonify({'success': True, 'profile': _profile_payload(key, accounts.get_user(key))})


def _set_avatar(key, name):
    from .roles import _connect
    conn = _connect()
    try:
        conn.execute('UPDATE users SET avatar = ? WHERE user_key = ?', (name, key))
        conn.commit()
    finally:
        conn.close()


def _avatar_upload(key, row, by_admin=False):
    """Photo de profil : recadrée au carré, 256 px, enregistrée en WebP (jamais le fichier envoyé)."""
    import io
    import os
    from PIL import Image, ImageOps
    file = request.files.get('avatar')
    if not file:
        return jsonify({'success': False, 'error': 'Aucune image'}), 400
    raw = file.read(AVATAR_MAX_BYTES + 1)
    if len(raw) > AVATAR_MAX_BYTES:
        return jsonify({'success': False, 'error': 'Image trop lourde (8 Mo maximum)'}), 400
    try:
        Image.MAX_IMAGE_PIXELS = 40_000_000
        image = Image.open(io.BytesIO(raw))
        image = ImageOps.exif_transpose(image).convert('RGB')
        image = ImageOps.fit(image, (AVATAR_SIZE, AVATAR_SIZE), Image.LANCZOS)
    except Exception:
        return jsonify({'success': False, 'error': 'Image illisible (JPEG, PNG, WebP ou GIF)'}), 400
    name = f'{secrets.token_hex(8)}-{int(time.time())}.webp'
    image.save(os.path.join(_avatar_dir(), name), 'WEBP', quality=86)
    _remove_avatar_file(row['avatar'])
    _set_avatar(key, name)
    log_activity('avatar_changed', target_key=key, target_name=row['name'],
                 detail='Nouvelle photo de profil' + (' (par un administrateur)' if by_admin else ''))
    return jsonify({'success': True, 'avatar_url': f'/avatars/{name}', 'avatar_kind': 'photo'})


def _avatar_preset(key, row, preset, by_admin=False):
    if preset not in AVATAR_PRESETS:
        return jsonify({'success': False, 'error': 'Avatar inconnu'}), 400
    _remove_avatar_file(row['avatar'])
    _set_avatar(key, PRESET_PREFIX + preset)
    log_activity('avatar_changed', target_key=key, target_name=row['name'],
                 detail=f'Avatar « {preset} »' + (' (par un administrateur)' if by_admin else ''))
    return jsonify({'success': True, 'avatar_url': _preset_url(preset), 'avatar_kind': 'preset'})


def _avatar_reset(key, row, by_admin=False):
    _remove_avatar_file(row['avatar'])
    _set_avatar(key, None)
    log_activity('avatar_changed', target_key=key, target_name=row['name'],
                 detail='Avatar par défaut' + (' (par un administrateur)' if by_admin else ''))
    return jsonify({'success': True, 'avatar_url': _preset_url(_default_preset(key)), 'avatar_kind': 'default'})


@auth_bp.route('/api/auth/avatar-presets', methods=['GET'])
def avatar_presets():
    return jsonify({'success': True, 'presets': [{'name': n, 'url': _preset_url(n)} for n in AVATAR_PRESETS]})


@auth_bp.route('/api/auth/profile/avatar', methods=['POST'])
def upload_avatar():
    key, row = _my_row()
    if not row:
        return jsonify({'success': False, 'error': 'Compte introuvable'}), 404
    return _avatar_upload(key, row)


@auth_bp.route('/api/auth/profile/avatar', methods=['PUT'])
def choose_avatar():
    key, row = _my_row()
    if not row:
        return jsonify({'success': False, 'error': 'Compte introuvable'}), 404
    return _avatar_preset(key, row, (request.get_json(silent=True) or {}).get('preset'))


@auth_bp.route('/api/auth/profile/avatar', methods=['DELETE'])
def delete_avatar():
    key, row = _my_row()
    if not row:
        return jsonify({'success': False, 'error': 'Compte introuvable'}), 404
    return _avatar_reset(key, row)


# Administrateur : l'avatar de n'importe quel compte (clé dans le formulaire ou le JSON).
def _target_account():
    key = request.form.get('key') or (request.get_json(silent=True) or {}).get('key') or ''
    row = accounts.get_user(key) if key else None
    return key, row


@auth_bp.route('/api/auth/users/avatar', methods=['POST', 'PUT', 'DELETE'])
def admin_user_avatar():
    key, row = _target_account()
    if not row:
        return jsonify({'success': False, 'error': 'Compte introuvable'}), 404
    if request.method == 'POST':
        return _avatar_upload(key, row, by_admin=True)
    if request.method == 'PUT':
        return _avatar_preset(key, row, (request.get_json(silent=True) or {}).get('preset'), by_admin=True)
    return _avatar_reset(key, row, by_admin=True)


@auth_bp.route('/avatars/<name>')
def serve_avatar(name):
    from flask import abort, send_from_directory
    if not name.endswith('.webp') or '/' in name or '..' in name:
        abort(404)
    return send_from_directory(_avatar_dir(), name, max_age=86400 * 30)


@auth_bp.route('/api/auth/genres', methods=['GET'])
def list_genres():
    """Genres présents dans la bibliothèque + préréglage « Enfant » du contrôle parental."""
    return jsonify({'success': True, 'preset': PARENTAL_PRESET,
                    'genres': [{'name': name, 'count': count} for name, count in all_genres()]})


@auth_bp.route('/api/auth/roles', methods=['POST'])
def save_roles_config():
    """Groupe OIDC des administrateurs (vide : tout le monde est administrateur)."""
    data = request.get_json(silent=True) or {}
    admin_group = (data.get('admin_group') or '').strip()
    if admin_group and current_role() != 'admin':
        return jsonify({'success': False, 'error': 'Réservé aux administrateurs'}), 403
    config = load_oidc_config()
    me = session.get('user')
    # Ne pas se couper l'accès : le compte qui enregistre doit rester administrateur.
    if admin_group and me and me.get('auth_method') != 'password':
        from .roles import _connect
        conn = _connect()
        try:
            row = conn.execute('SELECT role_override FROM users WHERE user_key = ?', (user_key(me),)).fetchone()
        finally:
            conn.close()
        override = row['role_override'] if row else None
        if override != 'admin' and computed_role(me.get('groups'), admin_group) != 'admin':
            return jsonify({'success': False, 'error': (
                f"Vous n'êtes pas dans le groupe « {admin_group} » : vous perdriez l'accès administrateur. "
                "Ajoutez-vous au groupe dans Pocket ID puis reconnectez-vous, ou forcez d'abord votre rôle à Admin.")}), 400
    config['admin_group'] = admin_group
    if save_oidc_config(config):
        return jsonify({'success': True})
    return jsonify({'success': False, 'error': 'Erreur de sauvegarde'}), 500


@auth_bp.route('/api/auth/test', methods=['POST'])
def test_oidc_discovery():
    """Teste la découverte OIDC (.well-known/openid-configuration) pour l'issuer fourni"""
    try:
        payload = request.get_json(silent=True) or {}
        issuer = normalize_issuer_url(payload.get('issuer', ''))

        if not issuer:
            return jsonify({'success': False, 'error': "URL de l'issuer manquante"}), 400

        response = requests.get(f"{issuer}/.well-known/openid-configuration", timeout=10)

        if response.status_code == 200:
            metadata = response.json()
            return jsonify({
                'success': True,
                'message': f"Découverte réussie (issuer: {metadata.get('issuer', issuer)})"
            })
        else:
            return jsonify({
                'success': False,
                'error': f"Erreur HTTP {response.status_code} lors de la découverte OIDC"
            }), response.status_code

    except requests.exceptions.Timeout:
        return jsonify({'success': False, 'error': "Timeout: impossible de joindre l'issuer"}), 500
    except requests.exceptions.ConnectionError:
        return jsonify({'success': False, 'error': "Impossible de se connecter à l'issuer. Vérifiez l'URL."}), 500
    except Exception as e:
        return jsonify({'success': False, 'error': f"Erreur: {'Erreur interne'}"}), 500


@auth_bp.route('/api/auth/api-key', methods=['GET'])
def get_api_key():
    """Clé API de Bullarr (en-tête X-Api-Key pour les applications externes)."""
    if getattr(g, 'api_key_auth', False):
        return jsonify({'success': False, 'error': 'La clé API ne peut pas être lue avec la clé API'}), 403
    config = load_oidc_config()
    return jsonify({'success': True, 'api_key': config.get('api_key_decrypted') or ''})


@auth_bp.route('/api/auth/api-key', methods=['POST'])
def regenerate_api_key():
    """Crée (ou remplace) la clé API ; l'ancienne cesse aussitôt de fonctionner."""
    if getattr(g, 'api_key_auth', False):
        return jsonify({'success': False, 'error': 'La clé API ne peut pas être changée avec la clé API'}), 403
    config = load_oidc_config()
    config['api_key_decrypted'] = generate_api_key()
    if not save_oidc_config(config):
        return jsonify({'success': False, 'error': 'Erreur de sauvegarde de la clé API'}), 500
    return jsonify({'success': True, 'api_key': config['api_key_decrypted']})


@auth_bp.route('/api/auth/api-key', methods=['DELETE'])
def delete_api_key():
    """Supprime la clé API : plus aucun accès par clé."""
    if getattr(g, 'api_key_auth', False):
        return jsonify({'success': False, 'error': 'La clé API ne peut pas être supprimée avec la clé API'}), 403
    config = load_oidc_config()
    config.pop('api_key', None)
    config.pop('api_key_decrypted', None)
    if not save_oidc_config(config):
        return jsonify({'success': False, 'error': 'Erreur de sauvegarde'}), 500
    return jsonify({'success': True})


def api_routes():
    """Toutes les routes /api/ de l'application, lues dans la table de routage Flask."""
    routes = []
    for rule in current_app.url_map.iter_rules():
        if not rule.rule.startswith('/api/'):
            continue
        view = current_app.view_functions.get(rule.endpoint)
        doc = (view.__doc__ or '').strip() if view else ''
        methods = sorted(m for m in rule.methods if m not in {'HEAD', 'OPTIONS'})
        parts = rule.rule.split('/')
        group = parts[2] if len(parts) > 2 else ''
        routes.append({
            'path': rule.rule,
            'methods': methods,
            'group': group,
            'summary': doc.split('\n\n')[0].replace('\n', ' ').strip(),
            'endpoint': rule.endpoint,
        })
    routes.sort(key=lambda r: (r['group'], r['path'], r['methods']))
    return routes


@auth_bp.route('/api/docs')
def api_docs():
    """Documentation de l'API : HTML dans un navigateur, JSON sinon (ou avec ?format=json)."""
    routes = api_routes()
    wants_json = request.args.get('format') == 'json' or (
        request.accept_mimetypes.best_match(['application/json', 'text/html']) == 'application/json')
    if wants_json:
        return jsonify({'success': True, 'auth': {'header': 'X-Api-Key', 'query': 'apikey'},
                        'count': len(routes), 'routes': routes})
    groups = {}
    for route in routes:
        groups.setdefault(route['group'], []).append(route)
    return render_template('api-docs.html', groups=groups, count=len(routes))
