"""
Routes API pour la surveillance des volumes manquants
"""
from flask import request, jsonify, current_app
from . import missing_monitor_bp
import sqlite3
import json
from datetime import datetime
from .detector import MissingVolumeDetector
from .searcher import MissingVolumeSearcher
from .downloader import MissingVolumeDownloader


def get_db_connection():
    """Retourne une connexion à la base de données"""
    conn = sqlite3.connect(current_app.config['DATABASE'], timeout=30.0)
    conn.row_factory = sqlite3.Row
    return conn


def get_detector():
    """Crée une instance du détecteur"""
    return MissingVolumeDetector()


def get_searcher():
    """Crée une instance du chercheur"""
    return MissingVolumeSearcher()


def get_downloader():
    """Crée une instance du downloader"""
    return MissingVolumeDownloader()


def load_monitor_config():
    """Charge la configuration de surveillance"""
    config_file = current_app.config.get('MISSING_MONITOR_CONFIG_FILE', 'data/missing_monitor_config.json')

    if not config_file:
        return get_default_monitor_config()

    try:
        with open(config_file, 'r') as f:
            return json.load(f)
    except:
        return get_default_monitor_config()


def get_default_monitor_config():
    """Configuration par défaut."""
    return {
        "enabled": False,
        "monitor_missing_volumes": {
            "enabled": True,
            "action": "download",
        },
        "quality_upgrade": {
            "enabled": False,
            "min_size_increase_percent": 20,
        },
        "search_sources": ["ebdz", "prowlarr"],
        "auto_download_enabled": False,
        "preferred_client": "qbittorrent",
    }


def save_monitor_config(config):
    """Sauvegarde la configuration"""
    config_file = current_app.config.get('MISSING_MONITOR_CONFIG_FILE', 'data/missing_monitor_config.json')

    if not config_file:
        return False

    try:
        import os
        os.makedirs(os.path.dirname(config_file), exist_ok=True)

        with open(config_file, 'w') as f:
            json.dump(config, f, indent=4)
        return True
    except Exception as e:
        print(f"Erreur sauvegarde config: {e}")
        return False


# ========== ROUTES ==========

# ========== ROUTES BIBLIOTHEQUES ==========

@missing_monitor_bp.route('/libraries', methods=['GET'])
def get_libraries():
    """Liste les bibliothèques avec leur statut de surveillance"""

    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        # Récupérer toutes les bibliothèques
        cursor.execute('''
            SELECT
                l.id,
                l.name,
                COUNT(s.id) as total_series,
                COALESCE(mvl.enabled, 0) as monitored
            FROM libraries l
            LEFT JOIN series s ON l.id = s.library_id
            LEFT JOIN missing_volume_library mvl ON l.id = mvl.library_id
            GROUP BY l.id
            ORDER BY l.name
        ''')

        libraries = [dict(row) for row in cursor.fetchall()]
        conn.close()

        return jsonify({
            'success': True,
            'count': len(libraries),
            'libraries': libraries
        })

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@missing_monitor_bp.route('/libraries/<int:library_id>/monitor', methods=['POST'])
def configure_library_monitor(library_id):
    """Configure la surveillance pour une bibliothèque"""

    try:
        data = request.get_json()
        enabled = data.get('enabled', True)

        conn = get_db_connection()
        cursor = conn.cursor()

        # Vérifier que la bibliothèque existe
        cursor.execute('SELECT id FROM libraries WHERE id = ?', (library_id,))
        if not cursor.fetchone():
            conn.close()
            return jsonify({'success': False, 'error': 'Bibliothèque introuvable'}), 404

        # Vérifier si un monitor existe déjà
        cursor.execute('SELECT id FROM missing_volume_library WHERE library_id = ?', (library_id,))
        existing = cursor.fetchone()

        if existing:
            # Mettre à jour
            cursor.execute('''
                UPDATE missing_volume_library
                SET enabled = ?
                WHERE library_id = ?
            ''', (1 if enabled else 0, library_id))
        else:
            # Créer
            cursor.execute('''
                INSERT INTO missing_volume_library (library_id, enabled, created_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
            ''', (library_id, 1 if enabled else 0))

        conn.commit()
        conn.close()

        return jsonify({'success': True})

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@missing_monitor_bp.route('/libraries/<int:library_id>/series', methods=['GET'])
def get_library_series(library_id):
    """Récupère les séries d'une bibliothèque"""

    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        # Vérifier que la bibliothèque existe
        cursor.execute('SELECT id FROM libraries WHERE id = ?', (library_id,))
        if not cursor.fetchone():
            conn.close()
            return jsonify({'success': False, 'error': 'Bibliothèque introuvable'}), 404

        # Récupérer les séries de cette bibliothèque
        cursor.execute('''
            SELECT
                s.id,
                s.title,
                s.total_volumes as total_local,
                s.missing_volumes,
                s.tags,
                COALESCE(s.bedetheque_status, 'Inconnu') as bedetheque_status,
                COALESCE(mm.enabled, 0) as enabled,
                mm.search_sources,
                COALESCE(s.bedetheque_total_volumes, 0) as bedetheque_total_volumes,
                s.bedetheque_complete as bedetheque_complete
            FROM series s
            LEFT JOIN missing_volume_monitor mm ON s.id = mm.series_id
            WHERE s.library_id = ?
            ORDER BY s.title
        ''', (library_id,))

        series = [dict(row) for row in cursor.fetchall()]
        conn.close()

        return jsonify({
            'success': True,
            'count': len(series),
            'series': series
        })

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@missing_monitor_bp.route('/config', methods=['GET', 'POST'])
def monitor_config():
    """Configuration générale de la surveillance"""

    if request.method == 'GET':
        config = load_monitor_config()
        return jsonify(config)

    else:  # POST
        try:
            data = request.get_json()
            config = load_monitor_config()

            # Mettre à jour les paramètres généraux
            config['enabled'] = data.get('enabled', False)
            config['search_sources'] = data.get('search_sources', ['ebdz', 'prowlarr'])
            config['preferred_client'] = data.get('preferred_client', 'qbittorrent')

            # Les nouveautés EBDZ/Telegram sont comparées directement aux tomes
            # manquants; aucune recherche par source n est configurée dans ce flux.
            missing_config = data.get('monitor_missing_volumes', {})
            missing_action = missing_config.get('action', 'download')
            if missing_action not in ('download', 'notify'):
                missing_action = 'download'
            config['monitor_missing_volumes'] = {
                'enabled': missing_config.get('enabled', True),
                'action': missing_action
            }

            # "meilleure qualité" (voir new_file_handler.py): distinct de
            # monitor_missing_volumes, option indépendante - un utilisateur peut vouloir
            # l'un sans l'autre. min_size_increase_percent est la "différence notable de
            # taille" demandée: le nouveau fichier doit dépasser la taille du fichier
            # possédé d'au moins ce pourcentage pour déclencher un remplacement, pas
            # n'importe quel octet de plus.
            quality_config = data.get('quality_upgrade', {})
            try:
                min_increase = max(0, int(quality_config.get('min_size_increase_percent', 20)))
            except (TypeError, ValueError):
                min_increase = 20
            config['quality_upgrade'] = {
                'enabled': bool(quality_config.get('enabled', False)),
                'min_size_increase_percent': min_increase
            }

            if save_monitor_config(config):
                return jsonify({'success': True})
            else:
                return jsonify({'success': False, 'error': 'Erreur sauvegarde'}), 500

        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500


@missing_monitor_bp.route('/series', methods=['GET'])
def get_monitored_series():
    """Liste les séries en surveillance"""

    try:
        status = request.args.get('status', 'all')  # 'all', 'incomplete', 'missing'

        detector = get_detector()
        series = detector.get_series_by_status(status)

        return jsonify({
            'success': True,
            'count': len(series),
            'series': series
        })

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@missing_monitor_bp.route('/series/<int:series_id>/monitor', methods=['POST'])
def configure_series_monitor(series_id):
    """Configure la surveillance pour une série spécifique"""

    try:
        data = request.get_json()

        conn = get_db_connection()
        cursor = conn.cursor()

        # Vérifier que la série existe
        cursor.execute('SELECT id FROM series WHERE id = ?', (series_id,))
        if not cursor.fetchone():
            conn.close()
            return jsonify({'success': False, 'error': 'Série introuvable'}), 404

        # Vérifier si un monitor existe déjà
        cursor.execute('SELECT id FROM missing_volume_monitor WHERE series_id = ?', (series_id,))
        existing = cursor.fetchone()

        enabled = data.get('enabled', True)
        # Colonnes legacy conservées en base pour compatibilité; la surveillance
        # directe ne choisit plus de sources et télécharge toujours les correspondances.
        search_sources = json.dumps([])
        auto_download = True

        if existing:
            # Mettre à jour
            cursor.execute('''
                UPDATE missing_volume_monitor
                SET enabled = ?, search_sources = ?, auto_download_enabled = ?
                WHERE series_id = ?
            ''', (enabled, search_sources, auto_download, series_id))
        else:
            # Créer
            cursor.execute('''
                INSERT INTO missing_volume_monitor
                (series_id, enabled, search_sources, auto_download_enabled, created_at)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            ''', (series_id, enabled, search_sources, auto_download))

        conn.commit()
        conn.close()

        # "ca marche de surveiller a non surveille mais pas dans l'autre sens" -
        # le frontend (toggleSeriesMonitor, static/js/library.js) lit ce champ pour
        # mettre a jour l'icone/label du bouton localement sans recharger la page;
        # sans lui data.monitored valait toujours undefined (falsy), donc l'icone ne
        # basculait jamais vers "Surveille" quel que soit le sens reel de l'appel.
        return jsonify({'success': True, 'monitored': bool(enabled)})

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@missing_monitor_bp.route('/search', methods=['POST'])
def search_volume():
    """Recherche un volume spécifique

    Un tome intégrale/hors-série n'a pas de "volume_num" au sens normal (voir
    series.ebdz_thread_id/is_integral/is_hs) - series_id + is_integral/is_hs indiquent
    ce cas et changent la requête envoyée à EBDZ (filtrée sur le thread déjà matché de
    la série plutôt qu'un simple LIKE) et à Prowlarr (texte "Intégrale N"/"HS N" plutôt
    que juste le numéro), sinon on chercherait par erreur le tome numéroté du même
    numéro (voir docs/sequences-technique.md, Limites connues)."""

    try:
        data = request.get_json()
        title = data.get('title', '').strip()
        raw_volume_num = data.get('volume_num')
        volume_num = int(raw_volume_num) if raw_volume_num not in (None, '') else None
        sources = data.get('sources')  # None = tous
        series_id = data.get('series_id')
        is_integral = bool(data.get('is_integral'))
        is_hs = bool(data.get('is_hs'))
        is_episode = bool(data.get('is_episode'))

        if not title:
            return jsonify({'success': False, 'error': 'Paramètres invalides'}), 400

        label = None
        if is_integral:
            label = f"Intégrale {volume_num}" if volume_num is not None else "Intégrale"
        elif is_hs:
            label = f"HS {volume_num}" if volume_num is not None else "Hors-série"
        elif is_episode:
            label = f"Épisode {volume_num}" if volume_num is not None else "Épisode"

        thread_id = None
        if series_id:
            conn = get_db_connection()
            row = conn.execute('SELECT ebdz_thread_id FROM series WHERE id = ?', (series_id,)).fetchone()
            conn.close()
            thread_id = row['ebdz_thread_id'] if row else None

        searcher = get_searcher()
        results = searcher.search_for_volume(
            title, volume_num, sources, thread_id=thread_id, label=label,
            source_order=sources
        )

        # "don't put a cap or put a way to know there are more results" - tronquer ici
        # perdait silencieusement des résultats déjà dédupliqués/triés par pertinence
        # (voir _deduplicate_and_rank), après le vrai correctif côté EBDZ (LIMIT
        # 10->100 pour une recherche "série entière", voir _search_ebdz) qui aurait
        # sinon buté sur ce second plafond juste en dessous. Chaque source se limite
        # déjà elle-même à une taille raisonnable (30 pour Prowlarr/Telegram, 10/100
        # pour EBDZ selon le cas) - pas besoin d'un plafond combiné en plus.
        query_suffix = label or (f'Vol {volume_num}' if volume_num is not None else '')
        return jsonify({
            'success': True,
            'query': f"{title} {query_suffix}".strip(),
            'results_count': len(results),
            'results': results
        })

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@missing_monitor_bp.route('/download', methods=['POST'])
def trigger_download():
    """Envoie un téléchargement au client"""

    try:
        data = request.get_json()
        link = data.get('link', '').strip()
        title = data.get('title', '').strip()
        volume_num = int(data.get('volume_num', 0))
        client = data.get('client')  # None = auto-détection

        if not link or not title or volume_num <= 0:
            return jsonify({'success': False, 'error': 'Paramètres invalides'}), 400

        downloader = get_downloader()
        success, message = downloader.send_torrent_download(
            link, title, volume_num, client
        )

        return jsonify({
            'success': success,
            'message': message,
            'title': title,
            'volume': volume_num
        })

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500




@missing_monitor_bp.route('/stats', methods=['GET'])
def get_monitor_stats():
    """Récupère les statistiques de surveillance"""

    try:
        detector = get_detector()
        downloader = get_downloader()
        total_series = detector.get_monitored_series_count()
        total_missing = detector.get_total_missing_volumes()
        downloads = downloader.get_download_history(limit=10)

        return jsonify({
            'success': True,
            'monitored_series': total_series,
            'total_missing_volumes': total_missing,
            'recent_downloads': downloads
        })

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@missing_monitor_bp.route('/performance', methods=['GET'])
def get_performance_stats():
    """Récupère les statistiques de performance du monitoring (cache/throttler de
    MissingVolumeSearcher) - aucune page ne l'affiche actuellement (audit qualité
    2026-07-31), pas de panneau de debug câblé dessus. Laissée en place (lecture seule,
    sans effet de bord) plutôt que supprimée au cas où un futur panneau diagnostic voudrait
    s'y brancher."""

    try:
        searcher = get_searcher()

        # Récupérer les stats du cache et du throttler
        cache_stats = searcher._cache.stats() if hasattr(searcher, '_cache') else {}

        # Info sur le throttler
        throttler_info = {
            'requests_per_minute': searcher._throttler.requests_per_minute if hasattr(searcher, '_throttler') else 30,
            'min_interval_seconds': searcher._throttler.min_interval if hasattr(searcher, '_throttler') else 2.0
        }

        return jsonify({
            'success': True,
            'cache': cache_stats,
            'throttler': throttler_info,
            'description': {
                'cache': 'Les résultats de recherche sont mis en cache pour éviter les requêtes répétées',
                'throttler': 'La fréquence des requêtes Prowlarr est limitée pour éviter les surcharges'
            }
        })

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@missing_monitor_bp.route('/history', methods=['GET'])
def get_download_history():
    """Récupère l'historique des téléchargements"""

    try:
        limit = request.args.get('limit', 50, type=int)
        series_id = request.args.get('series_id', type=int)

        downloader = get_downloader()
        history = downloader.get_download_history(limit=limit, series_id=series_id)

        return jsonify({
            'success': True,
            'count': len(history),
            'history': history
        })

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@missing_monitor_bp.route('/attach-pending-series', methods=['POST'])
def attach_pending_series():
    """"ajoute eclair dans nouveautés ca telecharge le fichier automatiquement et charge
    la serie en arriere plan [...] matcher automatiquement la serie dans import sans que
    j'ai a le faire" - voir attach_series_to_pending_download (downloader.py) pour le
    raisonnement complet. `attached: false` (toujours avec success=true, jamais une
    erreur HTTP) n'importe quand rien n'a été trouvé à rattacher - un cas normal (déjà
    importé, ou le téléchargement Telegram sous-jacent n'a pas encore posé sa ligne
    active_downloads le temps que ce rattrapage arrive, voir le retry côté
    ebdz-latest.js), pas un échec à remonter comme tel."""
    data = request.get_json(silent=True) or {}
    client = data.get('client')
    series_id = data.get('series_id')
    if not client or not series_id:
        return jsonify({'success': False, 'error': 'client et series_id requis'}), 400

    from .downloader import attach_series_to_pending_download
    attached = attach_series_to_pending_download(
        client, series_id, link=data.get('link'),
        channel=data.get('channel'), message_id=data.get('message_id')
    )
    return jsonify({'success': True, 'attached': attached})
