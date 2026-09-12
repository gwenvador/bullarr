"""
Routes pour l'authentification SSO / OIDC
Utilise Authlib pour la découverte OIDC (issuer/.well-known/openid-configuration),
le PKCE et la validation du token d'identité — pas de JWT fait maison.
"""
from flask import request, jsonify, redirect, url_for, session, current_app, render_template
from authlib.integrations.flask_client import OAuth
from werkzeug.security import check_password_hash, generate_password_hash
import requests

from . import auth_bp
from encryption import decrypt
from .config_store import load_oidc_config, save_oidc_config, normalize_issuer_url

# Endpoints toujours accessibles, même quand le SSO/OIDC est activé (sans quoi
# l'utilisateur ne pourrait jamais atteindre le flux de connexion ou les assets statiques)
_PUBLIC_ENDPOINTS = {
    'auth.login', 'auth.login_password', 'auth.login_redirect', 'auth.callback',
    'auth.logout', 'auth.logout_provider',
    'static', 'serve_cover',
}


def _get_oidc_client(config):
    """Construit un client OAuth Authlib à partir de la configuration OIDC sauvegardée"""
    issuer = normalize_issuer_url(config.get('issuer', ''))
    client_id = config.get('client_id', '')
    client_secret = config.get('client_secret_decrypted') or decrypt(config.get('client_secret', '')) or ''
    scopes = config.get('scopes') or 'openid profile email'

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
    mode = config.get('mode') or ('oidc' if config.get('enabled', False) else 'none')
    if mode == 'none':
        return None

    if session.get('user'):
        return None

    # Keep the post-login destination same-origin. Storing request.url trusted the
    # client-controlled Host/X-Forwarded-Host header and could turn the callback into
    # an open redirect after a successful login.
    session['next_url'] = request.full_path if request.query_string else request.path
    return redirect(url_for('auth.login'))


@auth_bp.route('/login')
def login():
    """Affiche un écran de chargement, puis laisse /login/redirect démarrer le flux
    OIDC (la découverte du fournisseur SSO passe par le réseau et peut prendre un
    instant : sans cet écran intermédiaire, le navigateur reste blanc pendant ce temps)"""
    config = load_oidc_config()

    mode = config.get('mode') or ('oidc' if config.get('enabled', False) else 'none')
    if mode == 'none':
        return redirect('/')
    if mode == 'password':
        return render_template('login.html', mode='password')
    if not config.get('issuer') or not config.get('client_id'):
        return render_template(
            'login.html',
            error="Configuration OIDC incomplète : vérifiez l'issuer et le client ID dans les paramètres.",
            mode='oidc'
        ), 500
    return render_template('login.html', loading=True, mode='oidc')


@auth_bp.route('/login/password', methods=['POST'])
def login_password():
    config = load_oidc_config()
    mode = config.get('mode') or ('oidc' if config.get('enabled', False) else 'none')
    username = (request.form.get('username') or '').strip()
    password = request.form.get('password') or ''
    if mode != 'password' or not username or username != config.get('username') or not config.get('password_hash') or not check_password_hash(config['password_hash'], password):
        return render_template('login.html', mode='password', error='Identifiant ou mot de passe incorrect.'), 401
    next_url = session.get('next_url') or '/'
    session.clear()
    session['user'] = {'name': username, 'auth_method': 'password'}
    return redirect(next_url)


@auth_bp.route('/login/redirect')
def login_redirect():
    """Effectue la découverte OIDC et la redirection vers le fournisseur SSO"""
    config = load_oidc_config()

    if (config.get('mode') or ('oidc' if config.get('enabled', False) else 'none')) != 'oidc':
        return redirect('/')

    try:
        oidc_client = _get_oidc_client(config)
        redirect_uri = url_for('auth.callback', _external=True)
        return oidc_client.authorize_redirect(redirect_uri)
    except Exception as e:
        return render_template(
            'login.html',
            error=f"Erreur lors de la connexion au fournisseur SSO : {e}"
        ), 500


@auth_bp.route('/callback')
def callback():
    """Termine l'échange de tokens OIDC et ouvre la session utilisateur"""
    config = load_oidc_config()

    if (config.get('mode') or ('oidc' if config.get('enabled', False) else 'none')) != 'oidc':
        return redirect('/')

    try:
        oidc_client = _get_oidc_client(config)
        token = oidc_client.authorize_access_token()
        userinfo = token.get('userinfo') or oidc_client.userinfo(token=token)

        session['user'] = {
            'sub': userinfo.get('sub'),
            'email': userinfo.get('email'),
            'name': userinfo.get('name') or userinfo.get('preferred_username') or userinfo.get('email'),
        }

        next_url = session.pop('next_url', None) or '/'
        return redirect(next_url)
    except Exception as e:
        return render_template(
            'login.html',
            error=f"Erreur lors de l'authentification SSO : {e}"
        ), 500


@auth_bp.route('/logout')
def logout():
    """Termine la session locale et affiche une confirmation de déconnexion.

    La déconnexion côté fournisseur OIDC (round-trip réseau) n'est pas déclenchée
    automatiquement : elle est proposée en lien optionnel sur la page de confirmation,
    pour que /logout reste instantané plutôt que de laisser le navigateur en attente
    d'une requête réseau sans rien afficher."""
    config = load_oidc_config()
    session.pop('user', None)
    session.pop('next_url', None)

    show_provider_link = bool((config.get('mode') or ('oidc' if config.get('enabled', False) else 'none')) == 'oidc' and config.get('issuer'))
    return render_template('logout.html', show_provider_link=show_provider_link)


@auth_bp.route('/logout/provider')
def logout_provider():
    """Termine aussi la session côté fournisseur OIDC (lien optionnel depuis /logout)"""
    config = load_oidc_config()

    if (config.get('mode') or ('oidc' if config.get('enabled', False) else 'none')) != 'oidc' or not config.get('issuer'):
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
    return jsonify({'user': session.get('user')})


@auth_bp.route('/api/auth/config', methods=['GET', 'POST'])
def oidc_config():
    """Configuration SSO / OIDC"""

    if request.method == 'GET':
        config = load_oidc_config()

        return jsonify({
            'mode': config.get('mode') or ('oidc' if config.get('enabled', False) else 'none'),
            'username': config.get('username', ''),
            'enabled': config.get('enabled', False),
            'issuer': config.get('issuer', ''),
            'client_id': config.get('client_id', ''),
            'client_secret': '****' if config.get('client_secret') else '',
            'scopes': config.get('scopes', 'openid profile email')
        })

    else:  # POST
        try:
            new_config = request.get_json()
            config = load_oidc_config()

            mode = new_config.get('mode', 'none')
            if mode not in {'none', 'password', 'oidc'}:
                return jsonify({'success': False, 'error': 'Mode d’authentification invalide'}), 400
            config['mode'] = mode
            config['enabled'] = mode != 'none'
            config['username'] = (new_config.get('username') or '').strip()
            new_password = new_config.get('password') or ''
            if new_password:
                config['password_hash'] = generate_password_hash(new_password)
            if mode == 'password' and (not config.get('username') or not config.get('password_hash')):
                return jsonify({'success': False, 'error': 'Un identifiant et un mot de passe sont requis'}), 400
            config['issuer'] = normalize_issuer_url(new_config.get('issuer', ''))
            config['client_id'] = new_config.get('client_id', '').strip()
            config['scopes'] = (new_config.get('scopes') or 'openid profile email').strip()

            # Ne change le secret client que s'il n'est pas masqué
            new_secret = new_config.get('client_secret', '')
            if new_secret and new_secret != '****':
                config['client_secret_decrypted'] = new_secret

            if save_oidc_config(config):
                return jsonify({'success': True})
            else:
                return jsonify({'success': False, 'error': 'Erreur de sauvegarde'}), 500

        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500


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
        return jsonify({'success': False, 'error': f"Erreur: {str(e)}"}), 500
