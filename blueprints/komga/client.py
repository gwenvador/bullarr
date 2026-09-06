"""
Client pour l'API Komga (recherche et métadonnées de séries)
Documentation API: https://komga.org/docs/openapi/komga-api
"""
import logging
import threading
import requests
from flask import current_app
from encryption import decrypt
from .config_store import load_komga_config, normalize_komga_url

logger = logging.getLogger(__name__)


class KomgaError(Exception):
    """Erreur de configuration ou de communication avec Komga"""
    pass


class KomgaSeriesNotFoundError(KomgaError):
    ""
    pass


def trigger_scan_async():
    """Demande un scan de toutes les bibliothèques Komga en arrière-plan, sans bloquer
    l'appelant (chaque scan peut prendre jusqu'à ~90s côté Komga - voir scan_library) et
    sans lever d'erreur si Komga n'est pas configuré/activé ou injoignable: à appeler en
    best-effort après un rename, un import ou un changement de métadonnées, pour que
    Komga reprenne l'état à jour sans attendre son prochain scan planifié.

    L'app Flask (contexte thread-local) est capturée ici puis repoussée dans le thread:
    sans ça, load_komga_config() (qui lit current_app.config) échoue silencieusement dès
    que l'appelant est lui-même déjà dans un thread en arrière-plan sans contexte (ex:
    _write_series_volumes_metadata_async), qui est justement le cas le plus fréquent."""
    app = current_app._get_current_object()

    def _run():
        try:
            with app.app_context():
                client = KomgaClient()
                for library in client.list_libraries():
                    client.scan_library(library['id'])
                logger.info("Scan Komga déclenché après modification locale")
        except KomgaError as e:
            logger.debug(f"Scan Komga non déclenché: {e}")
        except Exception as e:
            logger.warning(f"Erreur lors du déclenchement du scan Komga: {e}")

    threading.Thread(target=_run, daemon=True).start()


class KomgaClient:
    def __init__(self):
        config = load_komga_config()

        if not config.get('enabled'):
            raise KomgaError('Intégration Komga désactivée')

        url = config.get('url', '').strip()
        api_key = config.get('api_key_decrypted') or decrypt(config.get('api_key', ''))

        if not url or not api_key:
            raise KomgaError('Configuration Komga incomplète (URL ou clé API manquante)')

        self.base_url = normalize_komga_url(url)
        self.headers = {'X-API-Key': api_key}

    def list_libraries(self):
        """Liste les bibliothèques Komga. Retourne une liste de dicts {id, name}."""
        try:
            response = requests.get(
                f"{self.base_url}/api/v1/libraries",
                headers=self.headers,
                timeout=15
            )
        except requests.exceptions.RequestException as e:
            raise KomgaError(f"Impossible de contacter Komga: {e}")

        if response.status_code != 200:
            raise KomgaError(f"Erreur HTTP Komga {response.status_code}: {response.text[:200]}")

        return [{'id': lib.get('id'), 'name': lib.get('name')} for lib in response.json()]

    def scan_library(self, library_id):
        """Déclenche un scan du système de fichiers d'une bibliothèque Komga (nouveaux
        fichiers, fichiers supprimés) via POST /libraries/{id}/scan.

        Contrairement aux GET (quasi instantanés), ce type d'endpoint semble traiter la
        demande de façon synchrone côté Komga et peut prendre plus d'une minute sur
        une grosse bibliothèque (observé jusqu'à ~90s pour /metadata/refresh) - d'où le
        timeout généreux."""
        try:
            response = requests.post(
                f"{self.base_url}/api/v1/libraries/{library_id}/scan",
                headers=self.headers,
                timeout=180
            )
        except requests.exceptions.RequestException as e:
            raise KomgaError(f"Impossible de contacter Komga: {e}")

        if response.status_code not in (200, 202, 204):
            raise KomgaError(f"Erreur HTTP Komga {response.status_code}: {response.text[:200]}")

    def search_series(self, query, size=40):
        """Recherche des séries par titre. Retourne une liste de dicts simplifiés."""
        try:
            response = requests.get(
                f"{self.base_url}/api/v1/series",
                headers=self.headers,
                params={'search': query, 'size': size},
                timeout=15
            )
        except requests.exceptions.RequestException as e:
            raise KomgaError(f"Impossible de contacter Komga: {e}")

        if response.status_code != 200:
            raise KomgaError(f"Erreur HTTP Komga {response.status_code}: {response.text[:200]}")

        page = response.json()
        content = page.get('content', []) if isinstance(page, dict) else page
        return [self._simplify_series(item) for item in content]

    def get_series_books(self, series_id, size=1000):
        """Récupère tous les livres (volumes) d'une série. Retourne une liste de dicts
        simplifiés, utilisée pour associer chaque volume local à son livre Komga."""
        try:
            response = requests.get(
                f"{self.base_url}/api/v1/series/{series_id}/books",
                headers=self.headers,
                params={'size': size},
                timeout=15
            )
        except requests.exceptions.RequestException as e:
            raise KomgaError(f"Impossible de contacter Komga: {e}")

        if response.status_code == 404:
            raise KomgaSeriesNotFoundError('Série introuvable sur Komga')
        if response.status_code != 200:
            raise KomgaError(f"Erreur HTTP Komga {response.status_code}: {response.text[:200]}")

        page = response.json()
        content = page.get('content', []) if isinstance(page, dict) else page
        return [self._simplify_book(item) for item in content]

    def get_series(self, series_id):
        """Récupère la fiche complète d'une série précise"""
        try:
            response = requests.get(
                f"{self.base_url}/api/v1/series/{series_id}",
                headers=self.headers,
                timeout=15
            )
        except requests.exceptions.RequestException as e:
            raise KomgaError(f"Impossible de contacter Komga: {e}")

        if response.status_code == 404:
            raise KomgaSeriesNotFoundError('Série introuvable sur Komga')
        if response.status_code != 200:
            raise KomgaError(f"Erreur HTTP Komga {response.status_code}: {response.text[:200]}")

        return self._simplify_series(response.json())

    def list_book_media_status(self):
        """"regardes si tu peux utiliser l'api komga pour des fichiers incorrects. komga
        a media analysis" - Komga décode réellement les pages de chaque livre pour
        générer ses vignettes (media.status/comment/pagesCount), un signal indépendant du
        simple test d'intégrité CRC local (zipfile.testzip()/rarfile.testrar(), voir
        _check_volume_file_validity dans blueprints/settings/routes.py): une archive peut
        passer ce test CRC tout en ne contenant aucune image décodable, ce que seul Komga
        détecte ici. Récupère TOUS les livres connus de Komga (toutes bibliothèques
        confondues - /api/v1/books n'a pas de filtre par bibliothèque simple et ce n'en
        est de toute façon pas nécessaire, le croisement se fait par komga_book_id côté
        appelant) en paginant par lots de 500. Retourne un dict {komga_book_id: {status,
        comment, pages_count, deleted}}."""
        result = {}
        page = 0
        while True:
            try:
                response = requests.get(
                    f"{self.base_url}/api/v1/books",
                    headers=self.headers,
                    params={'size': 500, 'page': page},
                    timeout=30
                )
            except requests.exceptions.RequestException as e:
                raise KomgaError(f"Impossible de contacter Komga: {e}")

            if response.status_code != 200:
                raise KomgaError(f"Erreur HTTP Komga {response.status_code}: {response.text[:200]}")

            data = response.json()
            content = data.get('content', [])
            for b in content:
                media = b.get('media', {}) or {}
                result[b.get('id')] = {
                    'status': media.get('status'),
                    'comment': media.get('comment') or None,
                    'pages_count': media.get('pagesCount'),
                    'deleted': bool(b.get('deleted')),
                }
            if data.get('last', True) or not content:
                break
            page += 1

        return result

    def download_thumbnail(self, series_id):
        """Télécharge la vignette d'une série. Retourne (bytes, content_type) ou (None, None)."""
        try:
            response = requests.get(
                f"{self.base_url}/api/v1/series/{series_id}/thumbnail",
                headers=self.headers,
                timeout=15
            )
        except requests.exceptions.RequestException:
            return None, None

        if response.status_code != 200:
            return None, None

        return response.content, response.headers.get('Content-Type', 'image/jpeg')

    def _simplify_book(self, item):
        metadata = item.get('metadata', {}) or {}
        book_id = item.get('id')

        try:
            number = float(metadata.get('number')) if metadata.get('number') is not None else None
        except (TypeError, ValueError):
            number = None

        return {
            'komga_book_id': book_id,
            'name': item.get('name'),
            'title': metadata.get('title'),
            'number': number,
            'url': f"{self.base_url}/book/{book_id}"
        }

    def _simplify_series(self, item):
        metadata = item.get('metadata', {}) or {}
        books_metadata = item.get('booksMetadata', {}) or {}

        authors = []
        for author in books_metadata.get('authors', []):
            name = author.get('name')
            if name and name not in authors:
                authors.append(name)

        series_id = item.get('id')
        return {
            'komga_series_id': series_id,
            'title': metadata.get('title') or item.get('name'),
            'status': metadata.get('status'),
            'summary': metadata.get('summary') or books_metadata.get('summary'),
            'total_volumes': metadata.get('totalBookCount') or item.get('booksCount'),
            'authors': authors,
            'library_id': item.get('libraryId'),
            'url': f"{self.base_url}/series/{series_id}"
        }
