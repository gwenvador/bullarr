"""
Scanner pour analyser les bibliothèques de BD
"""
import sqlite3
import os
import re
import hashlib
from pathlib import Path
from zipfile import ZipFile
import rarfile
from pypdf import PdfReader
from PIL import Image
import io
from archive_utils import detect_actual_format
# defusedxml plutôt que xml.etree standard: ComicInfo.xml vient d'archives scannées/
# importées, potentiellement forgées (bombe d'expansion d'entités type "billion laughs")
import defusedxml.ElementTree as ET
from collections import defaultdict
import json
from flask import current_app
import logging
import threading
import fcntl
import time

logger = logging.getLogger(__name__)


class SeriesDirectoryMissingError(Exception):
    """Levée quand le répertoire d'une série n'existe plus sur le disque: la série
    (et ses volumes) viennent d'être supprimés de la base de données"""
    pass


class InterProcessImportLock:
    """Thread + flock lock shared by every Gunicorn/scheduler process."""
    def __init__(self, path=None):
        self.path = path or os.environ.get(
            'BULLARR_LIBRARY_LOCK', '/app/data/library-mutation.lock'
        )
        self._thread_lock = threading.Lock()
        self._local = threading.local()

    def acquire(self, blocking=True, timeout=-1):
        if timeout is None:
            timeout = -1
        if not self._thread_lock.acquire(blocking, timeout):
            return False
        deadline = None if timeout < 0 else time.monotonic() + timeout
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            handle = open(self.path, 'a+')
            while True:
                try:
                    flags = fcntl.LOCK_EX
                    if deadline is not None or not blocking:
                        flags |= fcntl.LOCK_NB
                    fcntl.flock(handle.fileno(), flags)
                    self._local.handle = handle
                    return True
                except BlockingIOError:
                    if not blocking or (deadline is not None and time.monotonic() >= deadline):
                        handle.close()
                        self._thread_lock.release()
                        return False
                    time.sleep(0.05)
        except Exception:
            self._thread_lock.release()
            raise

    def release(self):
        handle = getattr(self._local, 'handle', None)
        if handle is None:
            raise RuntimeError('release unlocked InterProcessImportLock')
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()
        self._local.handle = None
        self._thread_lock.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *_exc):
        self.release()


scan_import_lock = InterProcessImportLock()


# Champs ComicInfo.xml (schéma ComicRack, aussi utilisé par Komga) qu'on extrait
# et conserve, sous forme de clés en minuscules dans le JSON stocké en base
COMICINFO_FIELDS = [
    'Title', 'Series', 'Number', 'Count', 'Summary', 'Writer', 'Penciller',
    'Inker', 'Colorist', 'Letterer', 'CoverArtist', 'Editor', 'Publisher',
    'Genre', 'Web', 'LanguageISO', 'Year', 'Month', 'Day', 'AgeRating',
    # Note Bédéthèque ("parse les notes des volumes bedetheque pour recuperer la note /
    # nb review") - CommunityRating est la balise standard ComicInfo (0-5) ; pas
    # d'équivalent standard pour le nombre d'avis, CommunityRatingCount est un tag
    # perso (les lecteurs qui ne le connaissent pas l'ignorent silencieusement, comme
    # tout élément XML inconnu). Voir build_comicinfo_fields (comicinfo_writer.py).
    'CommunityRating', 'CommunityRatingCount'
]


class LibraryScanner:
    def __init__(self, db_path=None):
        if db_path is None:
            db_path = current_app.config['DATABASE']
        self.db_path = db_path
        self.init_database()

    def init_database(self):
        """Initialise la base de données"""
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        cursor = conn.cursor()
        
        # Activer le mode WAL (Write-Ahead Logging) pour de meilleures performances concurrentes
        cursor.execute('PRAGMA journal_mode=WAL')

        # Table des bibliothèques
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS libraries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                path TEXT NOT NULL,
                description TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_scanned TIMESTAMP
            )
        ''')

        # Table des séries
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS series (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                library_id INTEGER,
                title TEXT NOT NULL,
                path TEXT,
                total_volumes INTEGER,
                missing_volumes TEXT,
                has_parts INTEGER DEFAULT 0,
                last_scanned TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (library_id) REFERENCES libraries(id) ON DELETE CASCADE
            )
        ''')

        # Table des volumes
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS volumes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                series_id INTEGER,
                part_number INTEGER,
                part_name TEXT,
                volume_number INTEGER,
                filename TEXT,
                filepath TEXT,
                author TEXT,
                year INTEGER,
                resolution TEXT,
                file_size INTEGER,
                page_count INTEGER,
                format TEXT,
                FOREIGN KEY (series_id) REFERENCES series(id) ON DELETE CASCADE
            )
        ''')

        # "now loading the bibliotheque is very slow. how come it is just a single db
        # read" - get_library_series (routes.py) exécute 2 sous-requêtes corrélées par
        # série (volumes_without_metadata, oneshot_is_integral), chacune un SCAN complet
        # de `volumes` faute d'index sur series_id (confirmé via EXPLAIN QUERY PLAN:
        # "SCAN v") - 363 séries × 2 × 3213 volumes ≈ 2.3M comparaisons de lignes pour ce
        # qui devrait être un aller-retour instantané. IF NOT EXISTS le rend gratuit une
        # fois créé, comme les index déjà posés ailleurs dans l'app (ed2k_links, etc.).
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_volumes_series_id ON volumes(series_id)')

        conn.commit()

        # Ajouter les colonnes de comparaison EBDZ si elles n'existent pas
        self._add_ebdz_columns(conn)

        # Ajouter les colonnes de métadonnées Komga si elles n'existent pas
        self._add_komga_columns(conn)

        # Ajouter les colonnes de métadonnées ComicInfo.xml / couverture si elles n'existent pas
        self._add_volume_metadata_columns(conn)

        # Ajouter les colonnes de couverture/résumé locaux (issus du ComicInfo.xml des
        # volumes) à la table series si elles n'existent pas
        self._add_series_local_metadata_columns(conn)

        # Ajouter les colonnes de métadonnées éditées manuellement (page de vérification/
        # édition manuelle) à la table series si elles n'existent pas
        self._add_series_manual_metadata_columns(conn)

        conn.close()

    def _add_series_local_metadata_columns(self, conn):
        """Ajoute les colonnes local_cover_path, local_summary, local_genre, local_author
        et local_year à la table series: couverture/résumé/genre/auteur/année dérivés des
        ComicInfo.xml/vignettes des volumes locaux, sans dépendre d'un matching Komga/Bédéthèque"""
        cursor = conn.cursor()
        cursor.execute("PRAGMA table_info(series)")
        existing_columns = {row[1] for row in cursor.fetchall()}

        series_columns = [
            ('local_cover_path', 'TEXT'),
            ('local_summary', 'TEXT'),
            ('local_genre', 'TEXT'),
            ('local_author', 'TEXT'),
            ('local_year', 'TEXT'),
            # "la bibliotheque information in tableau should be in the database. no
            # need of complicated queries. just list the database" - ces deux colonnes
            # remplacent les sous-requêtes corrélées que get_library_series exécutait
            # avant sur `volumes` (une par série, à chaque chargement du tableau) -
            # calculées ici comme le reste des local_*, à chaque update_series_stats.
            ('volumes_without_metadata', 'INTEGER DEFAULT 0'),
            ('oneshot_is_integral', 'INTEGER DEFAULT 0'),
        ]

        for col_name, col_type in series_columns:
            if col_name not in existing_columns:
                try:
                    cursor.execute(f"ALTER TABLE series ADD COLUMN {col_name} {col_type}")
                    logger.info(f"Colonne {col_name} ajoutée à la table series")
                except sqlite3.OperationalError as e:
                    logger.warning(f"Impossible d'ajouter la colonne {col_name}: {e}")

        conn.commit()

    def _add_series_manual_metadata_columns(self, conn):
        """Ajoute les colonnes de métadonnées série éditées manuellement (page fiche
        série, modale "Éditer manuellement"): contrairement à local_* (recalculées à
        chaque scan depuis le ComicInfo.xml des tomes, voir update_series_stats) ou
        bedetheque_* (recalculées à chaque MAJ Bédéthèque), ces colonnes ne sont écrites
        que par l'utilisateur et ne sont jamais touchées ailleurs - sans ça, une
        correction manuelle serait silencieusement effacée au prochain scan/MAJ."""
        cursor = conn.cursor()
        cursor.execute("PRAGMA table_info(series)")
        existing_columns = {row[1] for row in cursor.fetchall()}

        series_columns = [
            ('manual_summary', 'TEXT'),
            ('manual_genre', 'TEXT'),
            ('manual_status', 'TEXT'),
            ('manual_author', 'TEXT'),
            ('manual_year_start', 'INTEGER'),
            ('manual_year_end', 'INTEGER'),
            ('manual_complete_override', 'INTEGER DEFAULT 0'),
        ]

        for col_name, col_type in series_columns:
            if col_name not in existing_columns:
                try:
                    cursor.execute(f"ALTER TABLE series ADD COLUMN {col_name} {col_type}")
                    logger.info(f"Colonne {col_name} ajoutée à la table series")
                except sqlite3.OperationalError as e:
                    logger.warning(f"Impossible d'ajouter la colonne {col_name}: {e}")

        conn.commit()

    def _add_volume_metadata_columns(self, conn):
        """Ajoute les colonnes comicinfo (métadonnées ComicInfo.xml en JSON), cover_path
        (vignette extraite de la première page), komga_book_id/komga_book_url (lien direct
        vers le livre correspondant sur Komga), is_integral/integral_number (tag #INT),
        is_hs/hs_number (tag #HS, hors-série) et is_episode/episode_number (tag "Épisode",
        voir https://www.bedetheque.com/serie-70835-BD-Bete-Frank-Pe-Zidrou.html - une
        publication en épisodes numérotés séparément des tomes qui les compilent ensuite,
        les deux partageant pourtant le même champ `number` côté Bédéthèque: "Tome 1" et
        "Épisode 1" y ont TOUS LES DEUX number=1, un simple volume_number=1 les confondrait
        comme si "Épisode 1" dupliquait "Tome 1" - "il a episode et tome. this is
        different. reflect it") à la table volumes"""
        cursor = conn.cursor()
        cursor.execute("PRAGMA table_info(volumes)")
        existing_columns = {row[1] for row in cursor.fetchall()}

        volume_columns = [
            ('comicinfo', 'TEXT'),
            ('cover_path', 'TEXT'),
            ('komga_book_id', 'TEXT'),
            ('komga_book_url', 'TEXT'),
            ('is_integral', 'INTEGER DEFAULT 0'),
            ('integral_number', 'INTEGER'),
            ('is_hs', 'INTEGER DEFAULT 0'),
            ('hs_number', 'INTEGER'),
            ('is_episode', 'INTEGER DEFAULT 0'),
            ('episode_number', 'INTEGER'),
            # "il y a un volume COF. ce n'est pas un volume, c'est un spécial... same for
            # everything that is not a volume" - catégorie fourre-tout pour tout album
            # Bédéthèque qui n'est ni un tome numéroté classique, ni une intégrale/HS/
            # épisode (déjà leurs propres colonnes ci-dessus): coffrets, tirages spéciaux,
            # rééditions promotionnelles, hors-textes... Bédéthèque n'a AUCUN vocabulaire
            # fermé pour ces codes ("Cof", "TT", "MBD05", "Pub", "Compil1"... 135 codes
            # DISTINCTS recensés sur cette seule bibliothèque, souvent un code promotionnel
            # ponctuel propre à un seul éditeur/partenariat) - contrairement à INT/HS, pas
            # la peine d'un numéro structuré ici, special_label garde tel quel le code brut
            # détecté (voir _parse_special_prefix, scraper.py) pour l'affichage.
            ('is_special', 'INTEGER DEFAULT 0'),
            ('special_label', 'TEXT'),
            ('is_bis', 'INTEGER DEFAULT 0'),
            ('bis_suffix', 'TEXT'),
            ('validated_size', 'INTEGER'),
            ('validation_valid', 'INTEGER'),
            ('validation_error', 'TEXT'),
            ('release_group', 'TEXT'),
        ]

        for col_name, col_type in volume_columns:
            if col_name not in existing_columns:
                try:
                    cursor.execute(f"ALTER TABLE volumes ADD COLUMN {col_name} {col_type}")
                    logger.info(f"Colonne {col_name} ajoutée à la table volumes")
                except sqlite3.OperationalError as e:
                    logger.warning(f"Impossible d'ajouter la colonne {col_name}: {e}")

        conn.commit()

    def _add_ebdz_columns(self, conn):
        """Ajoute les colonnes de comparaison EBDZ à la table series si elles n'existent pas"""
        cursor = conn.cursor()

        ebdz_columns = [
            ('ebdz_volumes_count', 'INTEGER'),
            ('ebdz_missing_volumes', 'TEXT'),
            ('ebdz_checked_at', 'TIMESTAMP'),
            ('ebdz_thread_url', 'TEXT'),
            ('ebdz_thread_id', 'INTEGER'),
            ('ebdz_matched_title', 'TEXT'),
            ('ebdz_match_status', 'TEXT')
        ]

        cursor.execute("PRAGMA table_info(series)")
        existing_columns = {row[1] for row in cursor.fetchall()}

        for col_name, col_type in ebdz_columns:
            if col_name not in existing_columns:
                try:
                    cursor.execute(f"ALTER TABLE series ADD COLUMN {col_name} {col_type}")
                    logger.info(f"Colonne {col_name} ajoutée à la table series")
                except sqlite3.OperationalError as e:
                    logger.warning(f"Impossible d'ajouter la colonne {col_name}: {e}")

        conn.commit()

    def _add_komga_columns(self, conn):
        """Ajoute les colonnes de métadonnées Komga à la table series si elles n'existent pas"""
        cursor = conn.cursor()

        komga_columns = [
            ('komga_series_id', 'TEXT'),
            ('komga_match_status', 'TEXT'),
            ('komga_matched_title', 'TEXT'),
            ('komga_url', 'TEXT'),
            ('komga_status', 'TEXT'),
            ('komga_total_volumes', 'INTEGER'),
            ('komga_summary', 'TEXT'),
            ('komga_authors', 'TEXT'),
            ('komga_cover_path', 'TEXT'),
            ('komga_checked_at', 'TIMESTAMP')
        ]

        cursor.execute("PRAGMA table_info(series)")
        existing_columns = {row[1] for row in cursor.fetchall()}

        for col_name, col_type in komga_columns:
            if col_name not in existing_columns:
                try:
                    cursor.execute(f"ALTER TABLE series ADD COLUMN {col_name} {col_type}")
                    logger.info(f"Colonne {col_name} ajoutée à la table series")
                except sqlite3.OperationalError as e:
                    logger.warning(f"Impossible d'ajouter la colonne {col_name}: {e}")

        conn.commit()

    @staticmethod
    def unscramble_trailing_article(text):
        """Convention de tri bibliothèque: "Titre, Le"/"La"/"Les"/"L'" OU "Titre (Le)"/
        "(La)"/"(Les)"/"(L')" OU "Titre [Le]"/"[La]"/"[Les]"/"[L']" (article déplacé en
        fin pour un tri alphabétique correct côté OS/lecteur - la forme entre
        parenthèses est en réalité la plus courante en pratique, ex: "Adoption (L') -
        T05 - ...", "Sursis (Le) - T01 - ...", la forme crochets se rencontre aussi, ex:
        "Palais Idéal Du Facteur Cheval [Le] - #01 - ..."). Bédéthèque référence
        toujours le titre articles-en-tête ("L'Adoption", "Le Sursis") donc sans ce
        remaniement le matching série (comparaison de titres) échoue - et pire, le
        groupe "(L')"/"[L']" serait sinon retiré comme n'importe quelle autre annotation
        entre parenthèses/crochets par le nettoyage de parse_filename (extraction de
        l'auteur, retrait inconditionnel des groupes crochets restants), perdant
        l'article purement et simplement ("Adoption"/"Palais Idéal Du Facteur Cheval" au
        lieu de "L'Adoption"/"Le Palais Idéal Du Facteur Cheval").

        Extraite de parse_filename pour être réutilisable ailleurs qu'un nom de fichier -
        même transformation nécessaire sur un thread_title EBDZ brut pour la comparaison
        "already_in_library" de /ebdz-nouveautes (blueprints/ebdz/routes.py), qui en
        avait jusqu'ici besoin sans jamais l'appliquer (comparait un thread_title EBDZ
        - article en fin, ex: "Grand vide, Le [Murawiec]" - directement à un titre local
        articles-en-tête, ex: "Le grand vide (Murawiec)", sans jamais matcher)."""
        match = re.match(
            r"^(.+?),\s*(Le|La|Les)\b(.*)$", text, re.IGNORECASE
        ) or re.match(
            r"^(.+?),\s*(L')(.*)$", text, re.IGNORECASE
        ) or re.match(
            r"^(.+?)\s*\((Le|La|Les|L')\)(.*)$", text, re.IGNORECASE
        ) or re.match(
            r"^(.+?)\s*\[(Le|La|Les|L')\](.*)$", text, re.IGNORECASE
        )
        if not match:
            return text
        base_title, article, remainder = match.groups()
        sep = '' if article.endswith("'") else ' '
        return f"{article}{sep}{base_title.strip()}{remainder}"

    @staticmethod
    def parse_filename(filename):
        """Parse le nom de fichier pour extraire les métadonnées.

        Ne devine jamais "one-shot" depuis le TEXTE du nom de fichier (voir historique -
        "why do you need to know that a file is a one shot? ... don't look at the name
        of the volume") - un fichier appartenant à une série déjà connue comme one-shot
        (series.is_oneshot, Bédéthèque) est classé comme tel par l'appelant
        (scan_single_series force volume=None après cet appel), jamais par un mot-clé
        deviné ici, trop ambigu (collision réelle avec le mot français "os")."""
        info = {
            'title': '',
            'part_number': None,
            'part_name': None,
            'volume': None,
            'author': None,
            'year': None,
            'resolution': None,
            # Groupe de scan/release restant entre crochets en fin de nom (ex:
            # "[NEO RIP-Club]", "[TONER]") - voir extraction plus bas, juste avant le
            # retrait générique des groupes entre crochets qui l'effacerait sinon sans le
            # capturer nulle part. "ajouter les tags de qualité et le nom du groupe qui a
            # scanné" (renommage, voir rename_handler.py <group>).
            'group': None,
            'format': filename.split('.')[-1].lower(),
            'is_integral': False,
            'integral_number': None,
            'integral_tome_start': None,
            'integral_tome_end': None,
            'is_hs': False,
            'hs_number': None,
            'is_episode': False,
            'episode_number': None,
            'is_pack': (
                (bool(re.search(r'pack\b', filename, re.IGNORECASE))
                 and not re.search(r'\b(?:re|de|un)pack\b', filename, re.IGNORECASE))
                # « INTEGRALE » sans numéro est aussi utilisé comme nom de pack
                # complet (et parfois comme nom de dossier). Il doit être sélectionnable
                # par la recherche série entière, sans transformer INT01/INT02 en packs.
                or bool(re.search(r'\bint[eé]grale\b(?!\s*[-#.]?\s*\d)', filename, re.IGNORECASE))
            )
        }

        # Retirer l'extension pour faciliter le parsing
        name_without_ext = os.path.splitext(filename)[0]

        name_without_ext = re.sub(
            r'^\[?BD[.\s-]+(?:FR|EN|VF|VO|FRENCH|ENGLISH)\]?[.\s-]+',
            '', name_without_ext, flags=re.IGNORECASE
        )

        # Voir unscramble_trailing_article ci-dessus - fait tôt, avant tout retrait de
        # ponctuation/parenthèses/crochets, sinon le groupe "(L')"/"[L']" serait retiré
        # comme n'importe quelle autre annotation entre parenthèses/crochets par le
        # nettoyage plus bas (extraction de l'auteur, retrait inconditionnel des groupes
        # crochets restants), perdant l'article purement et simplement.
        name_without_ext = LibraryScanner.unscramble_trailing_article(name_without_ext)

        # AVANT la normalisation: Extraire les résolutions depuis les crochets
        # Patterns: [Digital-XXX], [XXX] où XXX >= 300, etc.
        excluded_numbers = set()  # Nombres à exclure de la détection de volume (résolutions)

        # Pattern 1: tag de résolution/format de scan entre crochets OU parenthèses, avec
        # un suffixe "px" explicite (ex: "[Digital-2511px]", "(UpScale 3420px)",
        # "(PRiNTER 2894px)"). "px" est un marqueur assez univoque pour ne pas avoir besoin
        # d'une liste fermée de mots-clés: n'importe quel mot devant "chiffres+px" est
        # accepté (le tag de format de scan varie beaucoup: Digital, ePub, Printer,
        # UpScale, Scan...). On garde le texte du tag tel quel (casse d'origine, tiret ou
        # espace) plutôt que de le reconstruire, pour ne pas perdre ces variantes
        digital_match = re.search(
            r'[\[\(]([A-Za-z][A-Za-z\s.-]*?(\d+)\s*px)[\]\)]',
            name_without_ext, re.IGNORECASE
        )
        if not digital_match:
            digital_match = re.search(
                r'[\[\(][^\[\]\(\)]*?((?:Digital|ePub|Printer|Print|Upscale|Up-Scale|Re-?Scan|Scan|[0-9]p)[\s.-]*(\d+))[^\[\]\(\)]*?[\]\)]',
                name_without_ext, re.IGNORECASE
            )
        if not digital_match:
            # Variante sans crochets/parenthèses: le tag de scan est parfois un simple
            # segment séparé par un tiret en toute fin de nom de fichier plutôt qu'entre
            # crochets (ex: "... - OS - Upscale 3840 px.cbz"). Ancré en fin de chaîne pour
            # ne pas confondre avec un mot-clé qui apparaîtrait ailleurs dans le titre
            digital_match = re.search(
                r'(?:^|-)\s*((?:Digital|ePub|Printer|Print|Upscale|Up-Scale|Re-?Scan|Scan)[\s.-]*(\d+)\s*px)\s*$',
                name_without_ext, re.IGNORECASE
            )
        if digital_match:
            excluded_numbers.add(int(digital_match.group(2)))
            info['resolution'] = digital_match.group(1)
            # Retire le tag entier (crochets/parenthèses inclus) du nom de travail: sinon
            # son contenu (ex: "Digital-1440") se retrouve mélangé au titre une fois les
            # crochets eux-mêmes remplacés par des espaces lors de la normalisation
            name_without_ext = name_without_ext.replace(digital_match.group(0), ' ', 1)

        # Pattern 2: [XXX] où XXX est un nombre >= 300 (typique pour résolutions)
        bracket_match = re.search(r'\[(\d{3,4})\]', name_without_ext)
        if bracket_match:
            bracket_num = int(bracket_match.group(1))
            is_plausible_year = 1900 <= bracket_num <= 2035
            if bracket_num >= 300 and not is_plausible_year:  # Seuil: les résolutions commencent généralement à 300+
                excluded_numbers.add(bracket_num)
                if not info['resolution']:
                    info['resolution'] = str(bracket_num)
                name_without_ext = name_without_ext.replace(bracket_match.group(0), ' ', 1)

        bracket_tag_pattern = (
            r'\[\s*((?:#?\bINT(?:[ée]grale)?\d*\b)'
            r'|(?:\bHS(?:[\s-]*\d+)?\b)'
            r'|(?:\b[EÉeé]p(?:isode)?[\s.-]*\d+\b))\s*\]'
        )
        name_without_ext = re.sub(bracket_tag_pattern, r' \1 ', name_without_ext, flags=re.IGNORECASE)

        # Même "déballage" que pour les crochets ci-dessus, mais pour un tag INT/HS entre
        # PARENTHÈSES. Doit tourner AVANT l'extraction combinée ci-dessous, comme son
        # équivalent crochets un peu plus haut.
        paren_tag_pattern = (
            r'\(\s*((?:#?\bINT(?:[ée]grale)?\d*\b)'
            r'|(?:\bHS(?:[\s-]*\d+)?\b)'
            r'|(?:\b[EÉeé]p(?:isode)?[\s.-]*\d+\b))\s*\)'
        )
        name_without_ext = re.sub(paren_tag_pattern, r' \1 ', name_without_ext, flags=re.IGNORECASE)

        enclosed_tome_range_pattern = (
            r'[\[(]\s*('
            r't(?:omes?)?\.?\s*\d{1,3}\s*(?:à|Ã|a|to)\s*'
            r'(?:t(?:omes?)?\.?\s*)?\d{1,3}'
            r'|t(?:omes?)?\.?\s*\d{1,3}\s*-\s*'
            r't(?:omes?)?\.?\s*\d{1,3}'
            r')\s*[\])]'
        )
        name_without_ext = re.sub(
            enclosed_tome_range_pattern, r' \1 ', name_without_ext,
            flags=re.IGNORECASE
        )

        combined_matches = [
            ((m.group(1) if m.group(1) is not None else m.group(2)).strip(), m.group(1) is not None)
            for m in re.finditer(r'\[([^\]]+?)\]|\(([^)]+?)\)', name_without_ext)
        ]
        group_matches = [
            (text, is_bracket) for text, is_bracket in combined_matches
            if not (re.fullmatch(r'\d{4}', text) and 1900 <= int(text) <= 2035)
        ]
        if len(group_matches) == 1:
            text, is_bracket = group_matches[0]
            if is_bracket:
                info['group'] = text
        elif group_matches:
            info['group'] = group_matches[-1][0]
        for candidate, _ in combined_matches:
            if info['author'] is None and not re.match(r'^\d{4}$', candidate) and candidate != info['group']:
                info['author'] = candidate
                break
        name_without_ext = re.sub(r'\[[^\]]+?\]|\([^)]+?\)', ' ', name_without_ext)

        underscore_volume_match = re.search(r'_(\d{1,3})_', name_without_ext)

        leading_number_match = re.match(r'^(\d{1,3})\s+([A-ZÀÂÄÉÈÊËÎÏÔÖÙÛÜŸÇ])', name_without_ext)

        # AMÉLIORATION: Normaliser le nom en remplaçant les points, underscores et caractères spéciaux par des espaces
        # Sauf pour les points dans les nombres (comme 1.5)
        # On garde aussi les points dans les patterns spéciaux comme "Vol." ou "T.01"
        normalized_name = name_without_ext

        # Remplacer les points par des espaces, sauf si précédés/suivis d'un chiffre
        normalized_name = re.sub(r'\.(?!\d)', ' ', normalized_name)  # Point non suivi d'un chiffre
        normalized_name = re.sub(r'(?<!\d)\.', ' ', normalized_name)  # Point non précédé d'un chiffre
        
        # Remplacer underscores et caractères spéciaux par des espaces
        normalized_name = re.sub(r'[_!,;:?\[\]{}()«»„""]', ' ', normalized_name)
        
        # Nettoyer les espaces multiples
        normalized_name = re.sub(r'\s+', ' ', normalized_name).strip()

        integral_pattern = r'#?\bINT(?:[eé]GRALE)?[\s.-]*(\d+)?\b'
        # INTEGRALE sans numéro désigne un pack global; on ne doit jamais en
        # déduire un tome simple depuis un T01 isolé. Une plage explicite reste
        # analysée juste après (ex: INTEGRALE.T01.T06).
        generic_integrale_pack = bool(re.search(r'\bint[eé]grale\b(?!\s*[-#.]?\s*\d)', normalized_name, re.IGNORECASE))
        integral_match = re.search(integral_pattern, normalized_name, re.IGNORECASE)
        if integral_match:
            info['is_integral'] = True
            if integral_match.group(1) and not (1800 <= int(integral_match.group(1)) <= 2099):
                info['integral_number'] = int(integral_match.group(1))
            normalized_name = re.sub(integral_pattern, ' ', normalized_name, flags=re.IGNORECASE)
            normalized_name = re.sub(r'\s+', ' ', normalized_name).strip()

            if info['integral_number'] is None:
                tag_match = re.search(r'#(\d+)', normalized_name)
                if tag_match:
                    info['integral_number'] = int(tag_match.group(1))
        elif generic_integrale_pack:
            info['is_integral'] = True

        hs_match = re.search(r'\bHS(?:[\s-]*(\d+))?\b', normalized_name, re.IGNORECASE)
        if hs_match:
            info['is_hs'] = True
            if hs_match.group(1):
                info['hs_number'] = int(hs_match.group(1))
            normalized_name = normalized_name.replace(hs_match.group(0), ' ', 1)
            normalized_name = re.sub(r'\s+', ' ', normalized_name).strip()

        # Détecter le tag "Épisode"/"Ep" (publication en épisodes numérotés séparément
        # des tomes qui les compilent ensuite - voir _add_volume_metadata_columns pour le
        # cas Bédéthèque qui a motivé cette catégorie: "il a episode et tome. this is
        # different"). Numéro toujours requis ici (contrairement à HS/INT) - "Ep" seul,
        # sans chiffre, est trop court pour ne pas risquer un faux positif sur un mot
        # ordinaire du titre.
        episode_match = re.search(r'\b[EÉ]p(?:isode)?[\s.-]*(\d+)\b', normalized_name, re.IGNORECASE)
        if episode_match:
            info['is_episode'] = True
            info['episode_number'] = int(episode_match.group(1))
            normalized_name = normalized_name.replace(episode_match.group(0), ' ', 1)
            normalized_name = re.sub(r'\s+', ' ', normalized_name).strip()


        if not (info['is_hs'] or info['is_episode']):
            tome_range = LibraryScanner._parse_integral_tome_range(normalized_name)
            if tome_range and tome_range[1] > tome_range[0]:
                # Même priorité en deux temps que _parse_integral_tome_range (T
                # explicite des deux côtés d'abord, séparateur mot en second) pour
                # retrouver la portée EXACTE du texte à retirer - _parse_integral_tome_range
                # a déjà validé qu'un des deux matche, ici on le refait juste pour obtenir
                # range_match.start()/end().
                range_match = re.search(
                    r'\bint[eé]grale\s+en\s+(\d{1,3})\s+t(?:omes?)?\b',
                    normalized_name, re.IGNORECASE
                )
                if not range_match:
                    range_match = re.search(
                        r'\bt(?:omes?)?\.?\s*(\d{1,3})\b[\s.\-~_/\\]{0,5}\bt(?:omes?)?\.?\s*(\d{1,3})\b',
                        normalized_name, re.IGNORECASE
                    )
                if not range_match:
                    range_match = re.search(
                        r'\bt(?:omes?)?\.?\s*(\d{1,3})[\s-]*(?:à|Ã|a|to)\s*(?:t(?:omes?)?\.?\s*)?(\d{1,3})',
                        normalized_name, re.IGNORECASE
                    )
                if not range_match:
                    range_match = re.search(
                        r'(?:^|-)\s*(\d{1,3})\s*(?:à|Ã|a|to)\s*(\d{1,3})\s*(?=-|$)',
                        normalized_name, re.IGNORECASE
                    )
                if range_match:
                    if not info['is_pack']:
                        info['is_integral'] = True
                    info['integral_tome_start'] = tome_range[0]
                    info['integral_tome_end'] = tome_range[1]
                    normalized_name = normalized_name[:range_match.start()] + ' ' + normalized_name[range_match.end():]
                    normalized_name = re.sub(r'\s*-\s*-\s*', ' - ', normalized_name)
                    normalized_name = re.sub(r'\s+', ' ', normalized_name).strip()

        part_match = re.search(r'(?:Part|Arc|Partie)\s+(\d+)', normalized_name, re.IGNORECASE)
        if part_match and int(part_match.group(1)) > 50:
            part_match = None
        if part_match:
            info['part_number'] = int(part_match.group(1))
            # Essayer d'extraire le nom de la partie
            part_name_match = re.search(r'(?:Part|Arc|Partie)\s+\d+\s*-\s*([^T]+?)(?=\s+T\d+)', normalized_name, re.IGNORECASE)
            if part_name_match:
                info['part_name'] = part_name_match.group(1).strip()

        # Extraire le numéro de tome avec patterns améliorés (sauf pour une intégrale, un
        # hors-série ou un épisode: ils n'ont pas de numéro de tome individuel, voir
        # détection des tags #INT/#HS/Épisode ci-dessus)
        # Si on a une partie, chercher d'abord un volume explicite APRÈS la partie
        used_underscore_volume = False
        used_leading_number_volume = False
        if info['part_number'] and not info['is_integral'] and not info['is_hs'] and not info['is_episode']:
            after_part = re.search(r'(?:Part|Arc|Partie)\s+\d+(?:\s*-\s*[^T-]*?)?\s*-?\s*T[\s\.]?(\d+)', normalized_name, re.IGNORECASE)
            if after_part:
                info['volume'] = int(after_part.group(1))

        # Si pas encore trouvé de volume, utiliser les patterns standard
        if not info['volume'] and not generic_integrale_pack and not info['is_integral'] and not info['is_hs'] and not info['is_episode']:
            volume_patterns = [
                r'#(\d+)',                        # #4 — tag explicite et prioritaire: certains titres
                                                   # contiennent aussi un numéro de tome/partie interne à
                                                   # l'arc (ex: "... - #11 - ... - Tome 1 - ...") qui ne doit
                                                   # pas être confondu avec le numéro de tome de la série
                r'Tome[\s\.](\d+)',               # Tome 09, Tome.09
                r'\bT[\s\.]?(\d+)',                # T04, T.04, T 4 - \b indispensable: sans lui
                                                   # "T" matche aussi le dernier caractère de
                                                   # n'importe quel mot français finissant par "t"
                                                   # suivi d'un nombre ("valai[t] 500", "étai[t] 12",
                                                   # "soi[t] 4", "tou[t] 2020"...), un cas fréquent
                                                   # dans un sous-titre/résumé - trouvé en validant
                                                   # ce parser sur 83719 noms de fichiers EBDZ réels
                                                   # ("Ranger Solitaire - 14 - L'homme qui valait 500 000
                                                   # dollars..." détecté comme tome 500 au lieu de 14)
                r'Vol\.?\s*(\d+)',                # Vol. 4, Vol 4, Vol.4
                r'Volume[\s\.](\d+)',             # Volume 4, Volume.4
                r'\bv[\s\.]?(\d+)',                # v4, v.4 - même raison que \bT ci-dessus
                r'-\s*(\d+)(?:\s|-|$)',           # - 08 (fin/espace) ou -08- encadré d'un second
                                                   # tiret (convention "Titre -N- Sous-titre",
                                                   # constatée sur Mika Tanaka: "Mika Tanaka -1- Le
                                                   # trio de l'étrange.cbz" - le tiret fermant juste
                                                   # après le nombre faisait échouer l'ancienne
                                                   # version de ce pattern, qui n'acceptait qu'un
                                                   # espace ou une fin de chaîne après le chiffre)
                r'\s(\d{1,2})\s+[A-Za-z]+\s*$',   # 08 Noda - nombre suivi d'un nom en toute fin de
                r'\s(\d+)\s*(?:FR|EN|VF|VO)',    # 09 FR (nombre avant langue)
                r'(?:^|\s)(\d{1,3})\s*-\s*\S',    # 04 - Le Gaulois gladiateur, ou Le Gaulois 04 - Le Gaulois
                r'\s(\d{1,3})$'                   # 08 (nombre de 1-3 chiffres à la fin, évite les années)
            ]

            for pattern in volume_patterns:
                match = re.search(pattern, normalized_name, re.IGNORECASE)
                if match:
                    potential_volume = int(match.group(1))
                    # Filtrer les fausses détections :
                    # - Années (entre 1800-2099)
                    # - Nombres trop grands pour être des volumes (> 999)
                    # - Nombres qui sont des résolutions
                    if not (1800 <= potential_volume <= 2099 or potential_volume > 999 or potential_volume in excluded_numbers):
                        info['volume'] = potential_volume
                        break

            used_underscore_volume = False
            if info['volume'] is None and underscore_volume_match:
                potential_volume = int(underscore_volume_match.group(1))
                if not (1800 <= potential_volume <= 2099 or potential_volume > 999 or potential_volume in excluded_numbers):
                    info['volume'] = potential_volume
                    used_underscore_volume = True

            # Repli sur le numéro nu en tête de fichier (voir leading_number_match plus
            # haut, item #30) - même priorité basse que le repli underscore ci-dessus,
            # essayé seulement si rien d'autre n'a rien trouvé.
            if info['volume'] is None and leading_number_match:
                potential_volume = int(leading_number_match.group(1))
                if not (1800 <= potential_volume <= 2099 or potential_volume > 999 or potential_volume in excluded_numbers):
                    info['volume'] = potential_volume
                    used_leading_number_volume = True

        # Extraire le titre (avant Part/Arc ou avant le numéro de tome)
        if info['part_number']:
            title_match = re.match(r'^(.+?)\s+(?:Part|Arc|Partie)\s*\d+', normalized_name, re.IGNORECASE)
        else:
            # Essayer progressivement différents patterns pour extraire le titre
            title_patterns = [
                r'^(?:Tome|T[\s\.]?\d+|Vol\.?|Volume|v[\s\.]?\d+|#\d+)[\s\.]*\d*\s*-\s*(.+)$',
                r'^(.+?)\s+(?:Tome|T[\s\.]?\d+|Vol|Volume|v[\s\.]?\d+|#\d+|-\s*\d+|\d{1,3}\s*-\s)',  # Patterns explicites
                r'^(.+?)\s+(\d{1,2})\s*(?:\(|\[)',  # Titre avant nombre + parenthèse/crochet (ex: "Fer et Neige 01 (Noda)")
                # "NN - Sous-titre" SANS aucun texte avant le numéro (rien à extraire comme
                # titre de série depuis le nom de fichier lui-même, ex: "01 - Le Génie Des
                # Alpages.cbr") - le sous-titre de l'album devient le titre parsé; le
                # matching série retombera sur le nom du DOSSIER si ce sous-titre ne
                # correspond à aucune série connue (voir _match_series_for_auto_import,
                # même repli que pour le nom de dossier ailleurs dans l'app)
                r'^\d{1,3}\s*-\s*(.+)$',
            ]
            if used_underscore_volume:
                # "Titre_NN_Sous-titre" (voir underscore_volume_match plus haut): pas de
                # tiret pour délimiter le titre, mais le numéro déjà validé sert d'ancre -
                # tout ce qui précède est le titre, tout ce qui suit est le sous-titre de
                # l'album (retiré, comme pour la convention à tiret ci-dessus).
                title_patterns.append(rf'^(.+?)\s+0*{info["volume"]}\s')
            if used_leading_number_volume:
                # "05 Les Celtiques..." (voir leading_number_match plus haut, item #30):
                # le numéro déjà validé (garde-fous stricts: espace littéral + majuscule
                # qui suit) ouvre le nom de fichier, tout ce qui suit devient le titre.
                title_patterns.append(rf'^0*{info["volume"]}\s+(.+)$')
            title_match = None
            for pattern in title_patterns:
                title_match = re.match(pattern, normalized_name, re.IGNORECASE)
                if title_match:
                    break

        if title_match:
            info['title'] = title_match.group(1).strip()
        else:
            # Si aucun pattern de tome trouvé, essayer de nettoyer le titre
            # Retirer un marqueur de langue final (ex: "Titre FR"): ancré en fin de chaîne
            # (contrairement à un ".*$" qui couperait tout depuis la PREMIÈRE occurrence
            # trouvée, y compris un marqueur de langue apparaissant en tout début de titre)
            clean_title = re.sub(r'\s+(?:FR|EN|VF|VO|FRENCH|ENGLISH)\s*$', '', normalized_name, flags=re.IGNORECASE)
            clean_title = re.sub(r'\s*-\s*[A-Za-z0-9]+$', '', clean_title)  # Retirer les tags de release
            # Le numéro de tome a pu être détecté via le pattern "nombre nu en fin de chaîne"
            # (ex: "Titre 14", sans mot-clé Tome/T/Vol...) : aucun des title_patterns ci-dessus
            # n'exige de mot-clé absent, donc title_match reste None et le nombre resterait
            # scotché au titre - chaque tome d'une même série deviendrait alors un titre
            # distinct ("Titre 01", "Titre 02"...) au lieu d'être regroupé. On le retire donc
            # ici explicitement, en réutilisant le numéro déjà validé (années/résolutions déjà
            # exclues par la boucle volume_patterns ci-dessus) plutôt que de redupliquer ces
            # exclusions dans un nouveau pattern.
            if info['volume'] is not None:
                clean_title = re.sub(rf'\s+0*{info["volume"]}\s*$', '', clean_title)
            info['title'] = clean_title.strip() if clean_title else normalized_name

        # Nettoyer le titre (retirer les tirets isolés en tête/fin - laissés par le retrait
        # des tags #INT/HS/OS/résolution/auteur ci-dessus -, espaces superflus). Le "+" sur
        # le groupe gère plusieurs tirets consécutifs (ex: "Titre - - -") en un seul passage
        info['title'] = re.sub(r'^(?:\s*-\s*)+', '', info['title'])
        info['title'] = re.sub(r'(?:\s*-\s*)+$', '', info['title'])
        info['title'] = re.sub(r'\s+', ' ', info['title']).strip()

        info['title'] = re.sub(r'\s*@[\w-]+\s*$', '', info['title']).strip()
        # Le retrait tardif du suffixe @canal peut exposer un séparateur qui était juste
        # devant lui (cas « Renard - @9-art-bd »). Rejouer le nettoyage de fin de titre.
        info['title'] = re.sub(r'(?:\s*-\s*)+$', '', info['title']).strip()

        # Chercher aussi l'auteur après un tiret (format: titre - auteur), si l'auteur n'a
        # pas déjà été trouvé entre parenthèses plus haut
        if not info['author']:
            author_dash_match = re.search(r'-\s*([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\s*(?:T\d+|Tome|Vol)', normalized_name)
            if author_dash_match:
                info['author'] = author_dash_match.group(1).strip()

        year_match = re.search(r'\b(19\d{2}|20\d{2})\b', name_without_ext)
        if year_match and int(year_match.group(1)) not in excluded_numbers:
            info['year'] = int(year_match.group(1))
            info['title'] = re.sub(rf'\s+{info["year"]}\s*$', '', info['title']).strip()

        # Extraire la résolution (1920x1080, etc.)
        # Ne pas écraser la résolution déjà extraite depuis les crochets
        if not info['resolution']:
            resolution_match = re.search(r'(\d{3,4}x\d{3,4})', filename)
            if resolution_match:
                info['resolution'] = resolution_match.group(1)

        return info

    @staticmethod
    def resolution_value(resolution_label):
        """Convertit un tag de résolution/scan brut (ex: "Digital-1920", "Upscale 3840 px",
        "2504", "1200x1600") en un entier comparable - pour classer "meilleure qualité"
        (missing_monitor/new_file_handler.py, option "upgrade qualité"), pas seulement
        l'afficher tel quel comme le fait `parse_filename`. Une paire "LxH" garde la plus
        grande dimension, cohérente avec les tags "...px" ci-dessus qui décrivent aussi la
        plus grande dimension du scan. Retourne None si le libellé est vide ou ne contient
        aucun nombre exploitable."""
        if not resolution_label:
            return None
        match = re.search(r'(\d{3,5})\s*x\s*(\d{3,5})', resolution_label, re.IGNORECASE)
        if match:
            return max(int(match.group(1)), int(match.group(2)))
        match = re.search(r'(\d{3,5})', resolution_label)
        if match:
            return int(match.group(1))
        return None

    def get_page_count(self, filepath, format_type):
        """Récupère le nombre de pages d'un fichier"""
        try:
            format_type = detect_actual_format(filepath, format_type)

            if format_type in ['cbz', 'zip']:
                with ZipFile(filepath, 'r') as zip_file:
                    # Compte les images (jpg, jpeg, png, webp)
                    image_files = [f for f in zip_file.namelist()
                                 if f.lower().endswith(('.jpg', '.jpeg', '.png', '.webp'))]
                    return len(image_files)

            elif format_type in ['cbr', 'rar']:
                with rarfile.RarFile(filepath) as rar_file:
                    image_files = [f for f in rar_file.namelist()
                                 if f.lower().endswith(('.jpg', '.jpeg', '.png', '.webp'))]
                    return len(image_files)

            elif format_type == 'pdf':
                with open(filepath, 'rb') as f:
                    pdf = PdfReader(f)
                    return len(pdf.pages)

        except Exception as e:
            print(f"Erreur lecture {filepath}: {e}")
            return 0

        return 0

    def read_comicinfo(self, filepath, format_type):
        """Lit et parse le ComicInfo.xml embarqué dans un cbz/cbr (métadonnées
        ComicRack/Komga: résumé, auteurs, éditeur, genre...).
        Retourne un dict des champs non vides trouvés (clés en minuscules), ou {} si
        absent/illisible."""
        format_type = detect_actual_format(filepath, format_type)

        try:
            xml_bytes = None

            if format_type in ('cbz', 'zip'):
                with ZipFile(filepath, 'r') as zip_file:
                    name = next((n for n in zip_file.namelist()
                                 if n.lower().endswith('comicinfo.xml')), None)
                    if name:
                        xml_bytes = zip_file.read(name)

            elif format_type in ('cbr', 'rar'):
                with rarfile.RarFile(filepath) as rar_file:
                    name = next((n for n in rar_file.namelist()
                                 if n.lower().endswith('comicinfo.xml')), None)
                    if name:
                        xml_bytes = rar_file.read(name)

            if not xml_bytes:
                return {}

            root = ET.fromstring(xml_bytes)
            info = {}
            for field in COMICINFO_FIELDS:
                el = root.find(field)
                if el is not None and el.text and el.text.strip():
                    info[field.lower()] = el.text.strip()
            return info

        except Exception as e:
            logger.debug(f"Impossible de lire ComicInfo.xml de {filepath}: {e}")
            return {}

    def extract_volume_cover(self, filepath, format_type):
        """Extrait la première page d'un cbz/cbr comme vignette du volume, la redimensionne
        et l'enregistre dans data/covers/volumes/. Retourne le chemin relatif (servi via
        /covers/...) ou None si le fichier n'a pas de page image ou n'est pas illustré
        (pdf, pas de couverture générée pour l'instant)."""
        format_type = detect_actual_format(filepath, format_type)
        image_exts = ('.jpg', '.jpeg', '.png', '.webp', '.gif', '.bmp')

        try:
            image_bytes = None

            if format_type in ('cbz', 'zip'):
                with ZipFile(filepath, 'r') as zip_file:
                    image_names = sorted(
                        n for n in zip_file.namelist()
                        if n.lower().endswith(image_exts) and not n.startswith('__MACOSX')
                    )
                    if image_names:
                        image_bytes = zip_file.read(image_names[0])

            elif format_type in ('cbr', 'rar'):
                with rarfile.RarFile(filepath) as rar_file:
                    image_names = sorted(
                        n for n in rar_file.namelist()
                        if n.lower().endswith(image_exts)
                    )
                    if image_names:
                        image_bytes = rar_file.read(image_names[0])

            if not image_bytes:
                return None

            image = Image.open(io.BytesIO(image_bytes)).convert('RGB')
            image.thumbnail((400, 600))

            covers_dir = os.path.join(current_app.config['COVERS_DIR'], 'volumes')
            os.makedirs(covers_dir, exist_ok=True)

            # Nom stable dérivé du chemin du fichier: un re-scan écrase la même vignette
            # au lieu d'en accumuler une nouvelle à chaque fois
            cover_filename = hashlib.md5(filepath.encode('utf-8')).hexdigest() + '.jpg'
            image.save(os.path.join(covers_dir, cover_filename), 'JPEG', quality=85)

            return f"covers/volumes/{cover_filename}"

        except Exception as e:
            logger.debug(f"Impossible d'extraire la couverture de {filepath}: {e}")
            return None

    def _load_existing_volume_state(self, cursor, series_id):
        """Charge l'état des volumes déjà en base pour une série, avant le DELETE+INSERT
        fait par scan_directory/scan_single_series. Factorisé le 2026-07-10 (les deux
        appelants avaient une copie quasi identique de cette requête - un correctif avait
        déjà dû être appliqué aux deux à la main, voir docs/sequences-technique.md).

        Retourne (existing_by_path, placeholder_id_by_volume_number, oneshot_placeholder):
        - existing_by_path: cache d'extraction par chemin (page_count/comicinfo/cover_path
          des fichiers inchangés) ; komga_book_id/komga_book_url y sont reportés
          indépendamment du cache d'extraction (même en scan complet) car le DELETE+INSERT
          qui suit perdrait sinon tout lien Komga déjà établi.
        - placeholder_id_by_volume_number: tomes "placeholder" Bédéthèque (filepath NULL)
          indexés par numéro, pour transformer leur ligne en tome réellement possédé au
          lieu d'en insérer une nouvelle.
        - oneshot_placeholder: pendant pour un one-shot (volume_number NULL des deux
          côtés - un simple dict par numéro ne peut jamais le retrouver, `NULL = NULL`
          étant toujours faux en SQL), au plus un par série."""
        cursor.execute('''
            SELECT filepath, file_size, page_count, comicinfo, cover_path,
                   komga_book_id, komga_book_url, is_special, special_label, resolution, release_group,
                   author, year
            FROM volumes WHERE series_id = ?
        ''', (series_id,))
        existing_by_path = {
            row[0]: {
                'file_size': row[1], 'page_count': row[2], 'comicinfo': row[3],
                'cover_path': row[4], 'komga_book_id': row[5], 'komga_book_url': row[6],
                'is_special': row[7], 'special_label': row[8], 'resolution': row[9],
                'release_group': row[10], 'author': row[11], 'year': row[12]
            }
            for row in cursor.fetchall()
        }

        cursor.execute('''
            SELECT id, volume_number, cover_path, comicinfo FROM volumes
            WHERE series_id = ? AND filepath IS NULL AND volume_number IS NOT NULL
        ''', (series_id,))
        placeholder_id_by_volume_number = {
            row[1]: {'id': row[0], 'cover_path': row[2], 'comicinfo': row[3]}
            for row in cursor.fetchall()
        }

        cursor.execute('''
            SELECT id, cover_path, comicinfo FROM volumes
            WHERE series_id = ? AND filepath IS NULL AND volume_number IS NULL
              AND is_integral = 0 AND is_hs = 0 AND is_episode = 0 AND is_special = 0
        ''', (series_id,))
        oneshot_placeholder_row = cursor.fetchone()
        oneshot_placeholder = {
            'id': oneshot_placeholder_row[0],
            'cover_path': oneshot_placeholder_row[1],
            'comicinfo': oneshot_placeholder_row[2]
        } if oneshot_placeholder_row else None

        return existing_by_path, placeholder_id_by_volume_number, oneshot_placeholder

    def scan_directory(self, library_id, library_path, auto_enrich=False, force_metadata_refresh=False):
        """Scanne un répertoire pour détecter les séries et volumes

        - Les sous-répertoires directs de library_path sont les séries
        - Les fichiers dans chaque sous-répertoire sont les volumes de cette série

        Args:
            library_id: ID de la bibliothèque
            library_path: Chemin du répertoire à scanner
            auto_enrich: Obsolète (toujours False). L'enrichissement se fait via un bouton séparé
            force_metadata_refresh: Si False (scan rapide, par défaut), les fichiers
                inchangés depuis le dernier scan (même chemin + même taille) réutilisent
                leurs page_count/ComicInfo.xml/couverture déjà stockés au lieu de rouvrir
                l'archive. Si True (scan complet), tout est ré-extrait, même l'inchangé
        """
        print(f"\n📂 Scan du répertoire: {library_path}")

        # Séries nouvellement créées ou ayant au moins un volume ajouté/modifié pendant
        # ce scan: seules celles-ci ont besoin d'une re-synchronisation Komga après coup
        # (les séries 100% inchangées gardent déjà leurs komga_book_id/url en cache)
        self.series_created_or_changed = set()

        # Sous-ensemble de series_created_or_changed: uniquement les séries qui viennent
        # d'être créées par ce scan (pas les séries existantes juste modifiées), pour
        # tenter un matching Komga automatique une seule fois, à leur toute première
        # apparition dans la bibliothèque
        self.newly_created_series = set()

        # Vérifier que le chemin existe et est bien un répertoire
        if not os.path.exists(library_path):
            raise Exception(f"Le chemin n'existe pas: '{library_path}'")
        
        if not os.path.isdir(library_path):
            raise Exception(f"Le chemin n'est pas un répertoire: '{library_path}'")

        # Extensions supportées
        supported_extensions = {'.cbz', '.cbr', '.zip', '.rar', '.tar', '.pdf'}

        # Structure pour grouper les fichiers par série
        # Clé = nom du sous-répertoire (= nom de la série)
        series_data = defaultdict(lambda: {
            'volumes': [],
            'path': None
        })

        # Parcourir le répertoire de la bibliothèque
        try:
            # Lister tous les éléments dans le répertoire de la bibliothèque
            items = os.listdir(library_path)
        except PermissionError as e:
            raise Exception(f"Permission refusée pour accéder à: '{library_path}'")
        except (FileNotFoundError, NotADirectoryError, OSError) as e:
            raise Exception(f"Impossible d'accéder au répertoire '{library_path}': {str(e)}")
        
        for item in items:
            item_path = os.path.join(library_path, item)
            
            # Si c'est un répertoire, c'est une série
            if os.path.isdir(item_path):
                # "Neuf: 0 volumes" (tome 3 Neuf Trilogy importé au mauvais endroit,
                # scan complet écrasait ensuite silencieusement la série "Neuf" à 0
                # volume): un dossier <univers>/ (créé par l'éditeur d'univers/
                # render_series_folder_name, voir _rename_series_folder et
                # execute_import) ne contient JAMAIS de fichier directement - seulement
                # des sous-dossiers de séries. Le distinguer d'un dossier de série
                # normal (qui contient toujours ses fichiers directement) évite de le
                # traiter à tort comme une série vide: on descend d'UN niveau
                # supplémentaire pour scanner chaque sous-dossier comme une série à
                # part entière, avec pour path le sous-dossier réel (jamais le dossier
                # d'univers lui-même) - jamais plus d'un niveau, aucune structure de
                # bibliothèque connue de l'app n'imbrique un univers dans un autre.
                try:
                    child_names = os.listdir(item_path)
                except (PermissionError, OSError) as e:
                    print(f"⚠️  Impossible d'accéder au dossier '{item}' ('{item_path}'): {str(e)}")
                    continue

                has_direct_file = any(
                    os.path.splitext(name)[1].lower() in supported_extensions
                    and os.path.isfile(os.path.join(item_path, name))
                    for name in child_names
                )
                has_subdirs = any(os.path.isdir(os.path.join(item_path, name)) for name in child_names)

                if not has_direct_file and has_subdirs:
                    # Dossier d'univers: chaque sous-dossier est une série à part
                    for sub_name in child_names:
                        sub_path = os.path.join(item_path, sub_name)
                        if not os.path.isdir(sub_path):
                            continue
                        series_title = sub_name
                        series_data[series_title]['path'] = sub_path
                        try:
                            for filename in os.listdir(sub_path):
                                filepath = os.path.join(sub_path, filename)
                                if os.path.isdir(filepath):
                                    continue
                                ext = os.path.splitext(filename)[1].lower()
                                if ext in supported_extensions:
                                    parsed = self.parse_filename(filename)
                                    series_data[series_title]['volumes'].append({
                                        'filename': filename,
                                        'filepath': filepath,
                                        'parsed': parsed,
                                        'file_size': os.path.getsize(filepath)
                                    })
                        except (PermissionError, OSError) as e:
                            print(f"⚠️  Impossible d'accéder à la série '{series_title}' ('{sub_path}'): {str(e)}")
                            continue
                    continue

                series_title = item  # Le nom du dossier EST le nom de la série
                series_data[series_title]['path'] = item_path
                
                # Scanner tous les fichiers dans ce répertoire de série
                try:
                    for filename in os.listdir(item_path):
                        filepath = os.path.join(item_path, filename)
                        
                        # Ignorer les sous-répertoires
                        if os.path.isdir(filepath):
                            continue
                        
                        ext = os.path.splitext(filename)[1].lower()
                        
                        if ext in supported_extensions:
                            parsed = self.parse_filename(filename)
                            
                            series_data[series_title]['volumes'].append({
                                'filename': filename,
                                'filepath': filepath,
                                'parsed': parsed,
                                'file_size': os.path.getsize(filepath)
                            })
                except (PermissionError, OSError) as e:
                    print(f"⚠️  Impossible d'accéder à la série '{series_title}' ('{item_path}'): {str(e)}")
                    continue
            
            # Si c'est un fichier directement dans la bibliothèque (pas dans un sous-dossier)
            elif os.path.isfile(item_path):
                ext = os.path.splitext(item)[1].lower()
                
                if ext in supported_extensions:
                    # Parser le nom de fichier pour extraire le titre
                    parsed = self.parse_filename(item)
                    
                    # Utiliser le titre extrait comme nom de série
                    # (fallback si fichiers pas organisés en dossiers)
                    if parsed['title']:
                        series_title = parsed['title']
                    else:
                        # Si pas de titre détecté, utiliser le nom du fichier sans extension
                        series_title = os.path.splitext(item)[0]
                    
                    # Le path de la série sera la bibliothèque elle-même
                    if not series_data[series_title]['path']:
                        series_data[series_title]['path'] = library_path
                    
                    series_data[series_title]['volumes'].append({
                        'filename': item,
                        'filepath': item_path,
                        'parsed': parsed,
                        'file_size': os.path.getsize(item_path)
                    })

        print(f"✓ {len(series_data)} séries détectées")

        # Insérer/mettre à jour dans la base de données
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        cursor = conn.cursor()

        for series_title, data in series_data.items():
            volumes = data['volumes']
            series_path = data['path']

            try:
                # Vérifier si la série existe déjà
                cursor.execute('''
                    SELECT id FROM series
                    WHERE library_id = ? AND title = ?
                ''', (library_id, series_title))

                result = cursor.fetchone()

                if result:
                    # Mettre à jour la série existante
                    series_id = result[0]

                    # Mettre à jour le path de la série
                    if series_path:
                        cursor.execute('UPDATE series SET path = ? WHERE id = ?', (series_path, series_id))

                    existing_by_path, placeholder_id_by_volume_number, oneshot_placeholder = \
                        self._load_existing_volume_state(cursor, series_id)

                    # Supprimer les anciens volumes RÉELLEMENT POSSÉDÉS pour cette série -
                    # les placeholders (filepath NULL) ne sont jamais supprimés par un
                    # scan, seul un tome effectivement retrouvé (voir plus bas) les
                    # transforme en ligne réelle
                    cursor.execute('DELETE FROM volumes WHERE series_id = ? AND filepath IS NOT NULL', (series_id,))
                else:
                    placeholder_id_by_volume_number = {}
                    oneshot_placeholder = None
                    # Créer une nouvelle série
                    if not series_path:
                        series_path = os.path.join(library_path, series_title)

                    cursor.execute('''
                        INSERT INTO series (library_id, title, path, total_volumes, missing_volumes, has_parts)
                        VALUES (?, ?, ?, 0, '[]', 0)
                    ''', (library_id, series_title, series_path))

                    series_id = cursor.lastrowid
                    existing_by_path = {}
                    self.series_created_or_changed.add(series_id)
                    self.newly_created_series.add(series_id)

                # Ajouter tous les volumes
                for volume in volumes:
                    try:
                        parsed = volume['parsed']

                        cached = existing_by_path.get(volume['filepath'])
                        if cached and not force_metadata_refresh and cached['file_size'] == volume['file_size']:
                            page_count = cached['page_count']
                            comicinfo_json = cached['comicinfo']
                            cover_path = cached['cover_path']
                        else:
                            page_count = self.get_page_count(volume['filepath'], parsed['format'])
                            comicinfo = self.read_comicinfo(volume['filepath'], parsed['format'])
                            comicinfo_json = json.dumps(comicinfo) if comicinfo else None
                            cover_path = self.extract_volume_cover(volume['filepath'], parsed['format'])

                        if not cached or cached['file_size'] != volume['file_size']:
                            self.series_created_or_changed.add(series_id)

                        komga_book_id = cached['komga_book_id'] if cached else None
                        komga_book_url = cached['komga_book_url'] if cached else None
                        is_special_value = int(cached['is_special']) if cached else 0
                        special_label_value = cached['special_label'] if cached else None
                        # Le dernier groupe entre crochets peut être un auteur (ex:
                        # ``[Baeken]``) quand le vrai releaseur figurait dans le nom
                        # d'origine sous une parenthèse ``(TONER)``. Après un premier
                        # renommage, cette information n'est plus récupérable depuis le
                        # seul nom canonique. Le release_group déjà enregistré reste donc
                        # la source de vérité lors d'un rescan du même fichier.
                        release_group_value = (
                            cached['release_group'] if cached and cached.get('release_group')
                            else parsed['group']
                        )
                        resolution_value = (
                            cached['resolution'] if cached and cached.get('resolution')
                            else parsed['resolution']
                        )
                        # "all the data from an album is taken from bedetheque not from
                        # the file itself. unless it is releaser and quality" - author/
                        # year ne sont plus JAMAIS déduits du nom de fichier ici (voir
                        # parsed['author']/parsed['year'], toujours calculés par
                        # parse_filename mais désormais ignorés pour ces deux colonnes):
                        # seul apply_volume_comicinfo (bedetheque/comicinfo_writer.py)
                        # les écrit, depuis le writer/penciller/colorist/year RÉELS une
                        # fois Bédéthèque appliqué. Un rescan se contente donc de
                        # reporter ce qui est déjà en base, exactement comme komga_book_id/
                        # is_special juste au-dessus - jamais de "author": None a
                        # l'insertion d'un tome tout juste scanné, jamais recouvert par
                        # une simple relecture de fichier ensuite.
                        author_value = cached['author'] if cached else None
                        year_value = cached['year'] if cached else None

                        # Un placeholder Bédéthèque existe déjà pour ce numéro de tome:
                        # on transforme sa ligne en tome réellement possédé (UPDATE,
                        # même id) plutôt que d'en insérer une nouvelle - conserve sa
                        # couverture/son titre Bédéthèque tant que le fichier scanné n'en
                        # fournit pas lui-même (ComicInfo.xml embarqué prioritaire, comme
                        # partout ailleurs dans l'app, voir VOL-02)
                        placeholder = placeholder_id_by_volume_number.pop(parsed['volume'], None) \
                            if parsed['volume'] is not None else None
                        if placeholder is None and parsed['volume'] is None \
                                and not parsed['is_integral'] and not parsed['is_hs'] and oneshot_placeholder:
                            placeholder = oneshot_placeholder
                            oneshot_placeholder = None

                        if placeholder:
                            cursor.execute('''
                                UPDATE volumes SET
                                    part_number = ?, part_name = ?, filename = ?, filepath = ?,
                                    author = ?, year = ?, resolution = ?, release_group = ?, file_size = ?, page_count = ?,
                                    format = ?, comicinfo = ?, cover_path = ?,
                                    is_integral = ?, integral_number = ?, is_hs = ?, hs_number = ?,
                                    is_episode = ?, episode_number = ?, is_special = ?, special_label = ?,
                                    komga_book_id = ?, komga_book_url = ?
                                WHERE id = ?
                            ''', (
                                parsed['part_number'],
                                parsed['part_name'],
                                volume['filename'],
                                volume['filepath'],
                                author_value,
                                year_value,
                                resolution_value,
                                release_group_value,
                                volume['file_size'],
                                page_count,
                                parsed['format'],
                                comicinfo_json or placeholder['comicinfo'],
                                cover_path or placeholder['cover_path'],
                                int(parsed['is_integral']),
                                parsed['integral_number'],
                                int(parsed['is_hs']),
                                parsed['hs_number'],
                                int(parsed['is_episode']),
                                parsed['episode_number'],
                                is_special_value,
                                special_label_value,
                                komga_book_id,
                                komga_book_url,
                                placeholder['id']
                            ))
                        else:
                            cursor.execute('''
                                INSERT INTO volumes
                                (series_id, part_number, part_name, volume_number, filename, filepath,
                                 author, year, resolution, release_group, file_size, page_count, format, comicinfo, cover_path,
                                 is_integral, integral_number, is_hs, hs_number, is_episode, episode_number,
                                 is_special, special_label, komga_book_id, komga_book_url)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            ''', (
                                series_id,
                                parsed['part_number'],
                                parsed['part_name'],
                                parsed['volume'],
                                volume['filename'],
                                volume['filepath'],
                                author_value,
                                year_value,
                                resolution_value,
                                release_group_value,
                                volume['file_size'],
                                page_count,
                                parsed['format'],
                                comicinfo_json,
                                cover_path,
                                int(parsed['is_integral']),
                                parsed['integral_number'],
                                int(parsed['is_hs']),
                                parsed['hs_number'],
                                int(parsed['is_episode']),
                                parsed['episode_number'],
                                is_special_value,
                                special_label_value,
                                komga_book_id,
                                komga_book_url
                            ))
                    except Exception as vol_error:
                        # Log l'erreur mais continue avec les autres volumes
                        print(f"    ⚠️  Erreur sur volume {volume.get('filename', '?')}: {vol_error}")
                        continue

                # Calculer les statistiques de la série (total, manquants, couverture et
                # résumé locaux issus du ComicInfo.xml des volumes) via la même logique
                # que le rescan d'une série précise
                self.update_series_stats(series_id, conn)

                # Commit après chaque série plutôt qu'une seule fois à la toute fin du
                # scan: sur une grosse bibliothèque, le scan peut prendre plusieurs minutes
                # et une seule transaction géante perdrait tout le travail déjà fait si le
                # scan est interrompu en cours de route (navigateur fermé, requête coupée...)
                conn.commit()

                # Affichage sécurisé avec gestion des caractères spéciaux
                try:
                    print(f"  ✓ {series_title}: {len(volumes)} volumes")
                except UnicodeEncodeError:
                    # Si le print échoue à cause de l'encodage, essayer en ASCII
                    safe_title = series_title.encode('ascii', 'ignore').decode('ascii')
                    print(f"  ✓ {safe_title}: {len(volumes)} volumes")
                
            except Exception as series_error:
                # Log l'erreur mais continue avec les autres séries
                try:
                    print(f"  ⚠️  Erreur sur série '{series_title}': {series_error}")
                except UnicodeEncodeError:
                    print(f"  ⚠️  Erreur sur une série: {series_error}")
                continue

        # Mettre à jour la date de scan de la bibliothèque
        cursor.execute('''
            UPDATE libraries
            SET last_scanned = CURRENT_TIMESTAMP
            WHERE id = ?
        ''', (library_id,))

        conn.commit()
        conn.close()

        return len(series_data)
    
    def scan_single_series(self, series_id, force_metadata_refresh=False):
        """Scanne une seule série (met à jour ses volumes) - voir scan_import_lock
        (module-level, en haut de ce fichier) pour pourquoi cette méthode ne fait
        qu'acquérir ce verrou avant de déléguer à _scan_single_series_locked : un import
        concurrent pour la même série (même verrou côté routes.py) ne doit jamais pouvoir
        s'exécuter pendant ce scan.

        Args:
            series_id: ID de la série à scanner
            force_metadata_refresh: Si False (scan rapide, par défaut), les fichiers
                inchangés depuis le dernier scan (même chemin + même taille) réutilisent
                leurs page_count/ComicInfo.xml/couverture déjà stockés au lieu de rouvrir
                l'archive. Si True (scan complet), tout est ré-extrait, même l'inchangé

        Returns:
            Nombre de volumes détectés
        """
        if not scan_import_lock.acquire(timeout=60):
            raise Exception(
                f"Scan de la série {series_id} impossible : un import est déjà en cours "
                "(nouvelle tentative dans quelques instants)"
            )
        try:
            return self._scan_single_series_locked(series_id, force_metadata_refresh)
        finally:
            scan_import_lock.release()

    def _scan_single_series_locked(self, series_id, force_metadata_refresh=False):
        """Corps réel de scan_single_series - voir cette méthode pour le verrouillage.
        Ne JAMAIS appeler directement en dehors de scan_single_series (le verrou
        scan_import_lock doit déjà être tenu par l'appelant)."""
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        cursor = conn.cursor()
        
        # Récupérer les infos de la série
        cursor.execute('''
            SELECT id, library_id, title, path, is_oneshot FROM series WHERE id = ?
        ''', (series_id,))
        
        result = cursor.fetchone()
        if not result:
            conn.close()
            raise Exception(f"Série {series_id} non trouvée")
        
        series_id, library_id, series_title, series_path, is_oneshot = result

        # Vérifier que le chemin existe. Si le répertoire n'est plus là (série
        # supprimée/déplacée sur le disque), on supprime la série et ses volumes de la
        # base plutôt que de simplement échouer, pour ne pas laisser traîner des entrées
        # obsolètes (FK non appliquées par SQLite ici: suppression explicite des volumes,
        # pas de cascade automatique)
        if not series_path or not os.path.exists(series_path):
            # Une série "voulue" (tomes Bédéthèque ajoutés sans fichier, voir
            # add_series_from_bedetheque - filepath NULL) n'a pas de dossier physique
            # tant qu'aucun téléchargement/import réel n'a eu lieu: ce n'est pas un
            # répertoire disparu à nettoyer, juste un dossier pas encore créé - ne pas
            # supprimer la série dans ce cas, il n'y a simplement rien à scanner
            cursor.execute('SELECT COUNT(*) FROM volumes WHERE series_id = ? AND filepath IS NULL', (series_id,))
            has_placeholders = cursor.fetchone()[0] > 0
            if has_placeholders:
                conn.close()
                return 0

            cursor.execute('DELETE FROM volumes WHERE series_id = ?', (series_id,))
            cursor.execute('DELETE FROM series WHERE id = ?', (series_id,))
            conn.commit()
            conn.close()
            print(f"  🗑️  Série supprimée (répertoire absent): {series_title}")
            raise SeriesDirectoryMissingError(series_title)
        
        print(f"\n📂 Scan de la série: {series_title}")
        
        # Extensions supportées
        supported_extensions = {'.cbz', '.cbr', '.zip', '.rar', '.tar', '.pdf'}
        
        # Lister les fichiers dans le répertoire de la série
        volumes_data = []
        
        try:
            for filename in os.listdir(series_path):
                filepath = os.path.join(series_path, filename)
                
                # Ignorer les sous-répertoires
                if os.path.isdir(filepath):
                    continue
                
                ext = os.path.splitext(filename)[1].lower()
                
                if ext in supported_extensions:
                    parsed = self.parse_filename(filename)
                    # Une série one-shot n'a pas de numérotation de tome: on détecte
                    # quand même le fichier (couverture, ComicInfo.xml, pages...) mais
                    # sans lui assigner de numéro, pour ne pas générer de faux "manquants"
                    # ("why do you need to know that a file is a one shot? ... is
                    # one-shot is triggered once you import the file and at that time
                    # you know the serie" - is_oneshot vient de la série, jamais deviné
                    # depuis le texte du nom de fichier, voir parse_filename).
                    if is_oneshot:
                        parsed['volume'] = None
                    volumes_data.append({
                        'filename': filename,
                        'filepath': filepath,
                        'parsed': parsed,
                        'file_size': os.path.getsize(filepath)
                    })
        except (PermissionError, OSError) as e:
            conn.close()
            raise Exception(f"Impossible d'accéder au répertoire: {e}")
        
        # Récupère les volumes déjà scannés pour réutiliser page_count/comicinfo/
        # cover_path des fichiers inchangés (même chemin + même taille) au lieu de
        # rouvrir/relire chaque archive: c'est cette lecture qui domine le temps de scan.
        # force_metadata_refresh permet de forcer un ré-examen complet (ex: après une
        # amélioration de l'extraction), même pour des fichiers inchangés
        existing_by_path, placeholder_id_by_volume_number, oneshot_placeholder = \
            self._load_existing_volume_state(cursor, series_id)

        # Supprimer les anciens volumes RÉELLEMENT POSSÉDÉS de cette série - les
        # placeholders (filepath NULL) ne sont jamais supprimés par un scan
        cursor.execute('DELETE FROM volumes WHERE series_id = ? AND filepath IS NOT NULL', (series_id,))

        # Insérer/mettre à jour les volumes trouvés
        for volume in volumes_data:
            parsed = volume['parsed']

            cached = existing_by_path.get(volume['filepath'])
            if not force_metadata_refresh and cached and cached['file_size'] == volume['file_size']:
                page_count = cached['page_count']
                comicinfo_json = cached['comicinfo']
                cover_path = cached['cover_path']
            else:
                page_count = self.get_page_count(volume['filepath'], parsed['format'])
                comicinfo = self.read_comicinfo(volume['filepath'], parsed['format'])
                comicinfo_json = json.dumps(comicinfo) if comicinfo else None
                cover_path = self.extract_volume_cover(volume['filepath'], parsed['format'])

            komga_book_id = cached['komga_book_id'] if cached else None
            komga_book_url = cached['komga_book_url'] if cached else None
            is_special_value = int(cached['is_special']) if cached else 0
            special_label_value = cached['special_label'] if cached else None
            release_group_value = (
                cached['release_group'] if cached and cached.get('release_group')
                else parsed['group']
            )
            resolution_value = (
                cached['resolution'] if cached and cached.get('resolution')
                else parsed['resolution']
            )
            # "all the data from an album is taken from bedetheque not from the file
            # itself. unless it is releaser and quality" - voir le même bloc dans
            # scan_directory ci-dessus: author/year ne sont plus jamais déduits du
            # nom de fichier ici, seul apply_volume_comicinfo les écrit.
            author_value = cached['author'] if cached else None
            year_value = cached['year'] if cached else None

            # Un placeholder Bédéthèque existe pour ce numéro: on transforme sa ligne en
            # tome réellement possédé (UPDATE, même id) plutôt que d'en insérer une
            # nouvelle - voir scan_directory pour le même mécanisme
            placeholder = placeholder_id_by_volume_number.pop(parsed['volume'], None) \
                if parsed['volume'] is not None else None
            if placeholder is None and parsed['volume'] is None \
                    and not parsed['is_integral'] and not parsed['is_hs'] and oneshot_placeholder:
                placeholder = oneshot_placeholder
                oneshot_placeholder = None

            try:
                if placeholder:
                    cursor.execute('''
                        UPDATE volumes SET
                            part_number = ?, part_name = ?, filename = ?, filepath = ?,
                            author = ?, year = ?, resolution = ?, release_group = ?, file_size = ?, page_count = ?,
                            format = ?, comicinfo = ?, cover_path = ?,
                            is_integral = ?, integral_number = ?, is_hs = ?, hs_number = ?,
                            is_episode = ?, episode_number = ?, is_special = ?, special_label = ?,
                            komga_book_id = ?, komga_book_url = ?
                        WHERE id = ?
                    ''', (
                        parsed['part_number'],
                        parsed['part_name'],
                        volume['filename'],
                        volume['filepath'],
                        author_value,
                        year_value,
                        resolution_value or None,
                        release_group_value or None,
                        volume['file_size'],
                        page_count,
                        parsed['format'],
                        comicinfo_json or placeholder['comicinfo'],
                        cover_path or placeholder['cover_path'],
                        int(parsed['is_integral']),
                        parsed['integral_number'],
                        int(parsed['is_hs']),
                        parsed['hs_number'],
                        int(parsed['is_episode']),
                        parsed['episode_number'],
                        is_special_value,
                        special_label_value,
                        komga_book_id,
                        komga_book_url,
                        placeholder['id']
                    ))
                else:
                    cursor.execute('''
                        INSERT INTO volumes (
                            series_id, part_number, part_name, volume_number,
                            filename, filepath, author, year, resolution, release_group,
                            file_size, page_count, format, comicinfo, cover_path,
                            is_integral, integral_number, is_hs, hs_number,
                            is_episode, episode_number, is_special, special_label,
                            komga_book_id, komga_book_url
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        series_id,
                        parsed['part_number'],
                        parsed['part_name'],
                        parsed['volume'],
                        volume['filename'],
                        volume['filepath'],
                        author_value,
                        year_value,
                        resolution_value or None,
                        release_group_value or None,
                        volume['file_size'],
                        page_count,
                        parsed['format'],
                        comicinfo_json,
                        cover_path,
                        int(parsed['is_integral']),
                        parsed['integral_number'],
                        int(parsed['is_hs']),
                        parsed['hs_number'],
                        int(parsed['is_episode']),
                        parsed['episode_number'],
                        is_special_value,
                        special_label_value,
                        komga_book_id,
                        komga_book_url
                    ))
            except Exception as e:
                print(f"⚠️  Erreur lors du traitement de {volume['filename']}: {e}")
                continue
        
        conn.commit()
        
        # Mettre à jour les stats de la série
        self.update_series_stats(series_id, conn)
        
        conn.commit()
        conn.close()
        
        print(f"✓ {series_title}: {len(volumes_data)} volumes")
        
        return len(volumes_data)
        
        
    @staticmethod
    def _parse_integral_tome_range(title):
        """Extrait (début, fin) de la plage de tomes classiques couverte par une
        intégrale, depuis son titre ("Intégrale Tomes 1 à 3" -> (1, 3), "Tome 01 à 04"
        -> (1, 4), "(T1 à 3)" -> (1, 3)) - None si le titre ne précise aucune plage
        exploitable ("Intégrale du cycle 1", "Livre 1", "L'intégrale" seule, ou même une
        simple paire "(1-2)" sans le mot tome/T - voir ci-dessous pourquoi), auquel cas
        l'appelant ne doit rien déduire plutôt que de deviner (voir update_series_stats).

        Exige le mot "tome(s)"/l'abréviation "T" immédiatement AVANT la plage plutôt que
        n'importe quelle paire "X-Y"/"X à Y" dans le titre - une intégrale porte souvent
        aussi une plage d'ANNÉES dans son titre ("L'Intégrale 2 - 1988-2002", vu en base
        réelle) qui ressemble syntaxiquement à une plage de tomes ; sans cette exigence,
        1988-2002 serait pris pour la plage de tomes 1988 à 2002. Ça rate en échange
        quelques cas réels sans le mot ("Intégrale (1-2)") - accepté, mieux vaut ne rien
        déduire qu'un mauvais chiffre (même philosophie que le matching Bédéthèque, voir
        CLAUDE.md: "si aucun candidat ne partage un mot, retourne None plutôt que deviner")."""
        if not title:
            return None
        normalized = title.replace('.', ' ').replace('_', ' ')
        # "T1.a.T6" (constaté en réel, "BD.FR.-.Ascension.du.Haut.Mal.-.T1.a.T6...") -
        # le second "T"/"Tome" est répété devant la borne de fin ("T1 à T6", pas
        # seulement "T1 à 6" qui marchait déjà).
        #
        # Forme française explicite « Intégrale en 4 Tomes » : elle indique un
        # pack couvrant les tomes 1 à 4, même si aucune borne T01/T04 n'est écrite.
        match = re.search(r'\bint[eé]grale\s+en\s+(\d{1,3})\s+t(?:omes?)?\b', normalized, re.IGNORECASE)
        if match:
            end = int(match.group(1))
            if end > 1:
                return (1, end)

        match = re.search(
            r'\bt(?:omes?)?\.?\s*(\d{1,3})\b[\s.\-~_/\\]{0,5}\bt(?:omes?)?\.?\s*(\d{1,3})\b',
            normalized, re.IGNORECASE
        )
        if not match:
            match = re.search(
                r'\bt(?:omes?)?\.?\s*(\d{1,3})[\s-]*(?:à|Ã|a|to)\s*(?:t(?:omes?)?\.?\s*)?(\d{1,3})',
                normalized, re.IGNORECASE
            )
        if not match:
            # "01.à.06" (constaté en réel, "BD.FR.-.Magasin.général.-.01.à.06.-.(Loisel-
            # Tripp).-.BDPACK.cbr") - un pack qui regroupe plusieurs tomes SANS même le
            # préfixe "T"/"Tome" devant le premier numéro, contrairement au cas "T1 à T6"
            # ci-dessus. Sans cette détection, "01" était lu comme un tome simple
            # (volume=1) - laissant ce pack entier "confirmer" une recherche automatique
            # de tome 1 et gagner contre un résultat correct d'une autre source (voir
            # _confirms_requested_volume, missing_monitor/searcher.py). Ici, en l'absence
            # du "T" explicite, on exige à la place que la plage forme tout un SEGMENT
            # isolé entre deux tirets (ou début/fin de chaîne) - un sous-titre réel n'est
            # normalement jamais composé UNIQUEMENT de deux nombres séparés par "à"/"a"/
            # "to", donc ce garde-fou reste aussi sûr que celui du cas "T1 à T6" ci-dessus
            # sans nécessiter le préfixe. Séparateur laissé explicite (pas de wildcard
            # générique) ici précisément parce qu'aucun "T" ne borne la plage : un
            # wildcard trop permissif matcherait n'importe quelle paire de nombres du nom.
            match = re.search(
                r'(?:^|-)\s*(\d{1,3})\s*(?:à|Ã|a|to)\s*(\d{1,3})\s*(?=-|$)',
                normalized, re.IGNORECASE
            )
        if not match:
            return None
        start, end = int(match.group(1)), int(match.group(2))
        # Bornes de sécurité: un vrai numéro de tome reste raisonnable, une intégrale ne
        # regroupe jamais des dizaines de tomes.
        # "Lyra (Ed Glénat) [T00 à T09]" (constaté en réel) - une intégrale/pack peut
        # légitimement commencer au Tome 0 (préquelle/prologue numéroté 0, convention BD
        # courante). `0 <= start` (plutôt que `0 < start`) accepte cette borne de départ;
        # `end` reste strictement positif (une plage ne peut jamais se terminer à 0, ce
        # serait un tome unique T0, déjà couvert ailleurs par le parsing simple).
        if not (0 <= start <= 500 and 0 < end <= 500 and start <= end and end - start <= 100):
            return None
        return (start, end)

    def update_series_stats(self, series_id, conn=None):
        """Met à jour les statistiques d'une série (total volumes, volumes manquants)
        
        Args:
            series_id: ID de la série à mettre à jour
            conn: Connexion SQLite existante (optionnel). Si None, une nouvelle connexion sera créée.
        """
        # Si aucune connexion n'est fournie, en créer une nouvelle
        close_conn = False
        if conn is None:
            conn = sqlite3.connect(self.db_path, timeout=30.0)
            close_conn = True
        
        cursor = conn.cursor()

        # Nombre total de fichiers/volumes RÉELLEMENT POSSÉDÉS (filepath NOT NULL) - y
        # compris les non numérotés (one-shot, intégrales #INT, bonus...). Depuis
        # l'ajout des tomes "placeholder" (voir add_series_from_bedetheque: chaque tome
        # listé sur Bédéthèque devient une ligne `volumes`, sans fichier, filepath NULL,
        # "comme s'il existait"), un simple COUNT(*) compterait aussi les tomes non
        # possédés et rendrait total_volumes tautologiquement égal à
        # bedetheque_total_volumes - tout le reste de l'app (badges "collection
        # complète", oneshot, historique...) suppose que total_volumes = possédés.
        cursor.execute('SELECT COUNT(*) FROM volumes WHERE series_id = ? AND filepath IS NOT NULL', (series_id,))
        total_volumes = cursor.fetchone()[0]

        # "il a episode et tome. this is different" (voir parse_filename) - Bédéthèque
        # numérote parfois À LA FOIS des "Épisode N" (pré-publication) ET des "Tome N"
        # distincts pour la même série (ex: "La bête" #786: 2 tomes réels + 9 épisodes
        # jamais destinés à être comptés comme des albums manquants à part). Dans ce
        # cas-là, episode_number ne doit JAMAIS entrer dans le calcul de complétude/trous
        # - seul le cas OPPOSÉ (série de l'univers Kenya, ex: "Namibia" #502, "marquée
        # incomplète 0/5 tomes parus" alors que ses 5 albums possédés sont TOUS numérotés
        # "Épisode N", sans aucun "Tome N" concurrent) doit faire rejoindre episode_number
        # à volume_number dans le même espace de numéros: `has_any_tome_numbering`
        # distingue les deux (Bédéthèque n'utilise jamais "Tome" ET "Épisode" comme SEULE
        # convention à la fois pour cette série - l'un des deux track est toujours vide).
        cursor.execute('''
            SELECT EXISTS(SELECT 1 FROM volumes WHERE series_id = ? AND volume_number IS NOT NULL)
        ''', (series_id,))
        has_any_tome_numbering = bool(cursor.fetchone()[0])

        # Numéros des tomes RÉELLEMENT POSSÉDÉS (pour le calcul de trous filesystem
        # ci-dessous) - les tomes placeholder (filepath NULL) n'y participent pas, ils
        # sont déjà par définition "manquants", pas des trous à déduire.
        # is_bis = 0: une variante "N Bis" (ex: Neuf "13 Bis") partage volume_number
        # avec le vrai tome N - la posséder ne doit jamais faire passer N pour possédé
        # si le vrai tome N, lui, ne l'est pas (et vice-versa: posséder N ne rend pas
        # "N Bis" possédé) - voir _sync_bedetheque_placeholder_volumes, routes.py.
        if has_any_tome_numbering:
            cursor.execute('''
                SELECT DISTINCT volume_number
                FROM volumes
                WHERE series_id = ? AND volume_number IS NOT NULL AND filepath IS NOT NULL AND is_bis = 0
                ORDER BY volume_number
            ''', (series_id,))
        else:
            cursor.execute('''
                SELECT DISTINCT episode_number
                FROM volumes
                WHERE series_id = ? AND is_episode = 1 AND episode_number IS NOT NULL AND filepath IS NOT NULL
                ORDER BY episode_number
            ''', (series_id,))

        volume_numbers = [row[0] for row in cursor.fetchall()]

        # Le nombre d'éléments principaux possédés reste indépendant de la numérotation :
        # certaines fiches Bédéthèque mélangent tome 0, albums sans numéro et numéros
        # absents. On compare donc les albums principaux réellement présents, en
        # excluant les intégrales/HS/spéciaux, qui ne sont pas des éléments de la
        # séquence principale (leur couverture éventuelle est traitée séparément).
        cursor.execute('''
            SELECT COUNT(*)
            FROM volumes
            WHERE series_id = ? AND filepath IS NOT NULL
              AND is_integral = 0
        ''', (series_id,))
        owned_main_item_count = cursor.fetchone()[0]

        # Référence Bédéthèque du nombre total de tomes de la série, si connue (matching
        # déjà fait). Utilisée pour ne pas perdre les tomes manquants au-delà du dernier
        # tome possédé ni avant le premier: sans elle, une série ajoutée "vide" depuis
        # Bédéthèque qui reçoit son premier fichier réel (ex: tome 3 sur 15) retombait
        # sur min=max=3 -> aucun trou détecté, les tomes 1,2 et 4-15 disparaissant
        # silencieusement de missing_volumes et la série sortant du monitoring (voir
        # plan-sonarr-monitoring.md, bug bloquant). Pour une série non matchée
        # (bedetheque_total_volumes NULL), le calcul reste purement filesystem gap-based
        # (min/max possédés uniquement) - pas de référence fiable pour supposer que des
        # tomes avant le premier possédé existent.
        cursor.execute('SELECT bedetheque_total_volumes, bedetheque_status, bedetheque_albums FROM series WHERE id = ?', (series_id,))
        total_row = cursor.fetchone()
        bedetheque_total = total_row[0] if total_row else None
        bedetheque_status = total_row[1] if total_row else None
        # Le nombre total Bédéthèque ne suffit pas à reconstruire les numéros : une
        # série peut commencer au tome 0 (ex. Atalante: 0..14 = 15 albums). Utiliser
        # les numéros réellement parsés dans la fiche quand ils sont disponibles,
        # plutôt que d'inventer systématiquement la séquence 1..N.
        bedetheque_album_numbers = set()
        if total_row and total_row[2]:
            try:
                albums = json.loads(total_row[2])
                bedetheque_album_numbers = {
                    int(album['number']) for album in albums
                    if album.get('number') is not None
                }
            except (TypeError, ValueError, KeyError):
                bedetheque_album_numbers = set()

        # La liste Bédéthèque peut être fiable même si son total n’a pas été extrait.
        if not bedetheque_total and bedetheque_album_numbers:
            bedetheque_total = len(bedetheque_album_numbers)

        if volume_numbers:
            min_vol = min(volume_numbers)
            max_vol = max(volume_numbers)
            actual_volumes = set(volume_numbers)
            if bedetheque_total and owned_main_item_count >= bedetheque_total:
                expected_volumes = actual_volumes
            elif bedetheque_album_numbers and (
                not bedetheque_total or len(bedetheque_album_numbers) == bedetheque_total
            ):
                expected_volumes = bedetheque_album_numbers
            elif bedetheque_album_numbers:
                expected_volumes = set(range(1, max(max_vol, max(bedetheque_album_numbers), bedetheque_total or 0) + 1))
            elif bedetheque_total:
                expected_volumes = set(range(1, max(max_vol, bedetheque_total) + 1))
            else:
                expected_volumes = set(range(min_vol, max_vol + 1))
            gap_missing_volumes = expected_volumes - actual_volumes
        else:
            gap_missing_volumes = set()

        cursor.execute('''
            SELECT filename, comicinfo
            FROM volumes
            WHERE series_id = ? AND is_integral = 1 AND filepath IS NOT NULL
        ''', (series_id,))
        integral_covered_volumes = set()
        for filename, comicinfo_json in cursor.fetchall():
            title = filename
            if comicinfo_json:
                try:
                    ci_title = json.loads(comicinfo_json).get('title')
                    if ci_title:
                        title = ci_title
                except (TypeError, ValueError):
                    pass
            tome_range = self._parse_integral_tome_range(title or '')
            if tome_range:
                integral_covered_volumes.update(range(tome_range[0], tome_range[1] + 1))
        gap_missing_volumes -= integral_covered_volumes

        if has_any_tome_numbering:
            cursor.execute('''
                SELECT DISTINCT volume_number
                FROM volumes
                WHERE series_id = ? AND volume_number IS NOT NULL AND filepath IS NULL AND is_bis = 0
            ''', (series_id,))
        else:
            cursor.execute('''
                SELECT DISTINCT episode_number
                FROM volumes
                WHERE series_id = ? AND is_episode = 1 AND episode_number IS NOT NULL AND filepath IS NULL
            ''', (series_id,))
        placeholder_missing_volumes = {row[0] for row in cursor.fetchall()} - integral_covered_volumes

        missing_volumes = sorted(gap_missing_volumes | placeholder_missing_volumes)

        # Sans numéro exploitable, une série incomplète doit tout de même exposer un manque.
        # Les numéros exacts étant inconnus, on expose la plage attendue.
        if bedetheque_total and owned_main_item_count < bedetheque_total and not missing_volumes:
            missing_volumes = list(range(1, bedetheque_total + 1))

        # Vérifier si la série a des parties
        cursor.execute('''
            SELECT COUNT(DISTINCT part_number)
            FROM volumes
            WHERE series_id = ? AND part_number IS NOT NULL
        ''', (series_id,))

        has_parts = cursor.fetchone()[0] > 1

        # volumes_without_metadata: nombre de volumes sans ComicInfo.xml exploitable
        # (absent, illisible, ou sans aucun champ non-vide - voir LibraryScanner.
        # read_comicinfo), désormais stocké ici plutôt que recalculé à la volée par une
        # sous-requête corrélée sur `volumes` à chaque chargement du tableau bibliothèque.
        cursor.execute('SELECT COUNT(*) FROM volumes WHERE series_id = ? AND comicinfo IS NULL', (series_id,))
        volumes_without_metadata = cursor.fetchone()[0]

        # oneshot_is_integral: le fichier unique d'un one-shot est-il en fait une
        # intégrale (voir _seriesBadgeInfo/library.js: affichage "Intégrale" vs
        # "One-shot") - même raison de stockage que ci-dessus.
        cursor.execute('SELECT is_integral FROM volumes WHERE series_id = ? LIMIT 1', (series_id,))
        oneshot_row_integral = cursor.fetchone()
        oneshot_is_integral = 1 if (oneshot_row_integral and oneshot_row_integral[0]) else 0

        # Couverture/résumé/genre locaux: dérivés du ComicInfo.xml/de la vignette des
        # volumes RÉELLEMENT POSSÉDÉS, sans dépendre d'un matching Komga/Bédéthèque (ces
        # champs "local_*" ont justement vocation à être indépendants de
        # bedetheque_cover_path/bedetheque_description - un tome placeholder a bien un
        # cover_path/comicinfo issus de Bédéthèque, mais ne doit pas être pris comme
        # source "locale"). On prend le volume possédé "représentatif" (le premier tome
        # numéroté, sinon le premier fichier par ordre alphabétique)
        cursor.execute('''
            SELECT cover_path, comicinfo
            FROM volumes
            WHERE series_id = ? AND filepath IS NOT NULL
            ORDER BY (volume_number IS NULL), volume_number, integral_number, filename
        ''', (series_id,))

        local_cover_path = None
        local_summary = None
        local_genre = None
        local_author = None
        local_year = None
        for cover_path, comicinfo_json in cursor.fetchall():
            if local_cover_path is None and cover_path:
                local_cover_path = cover_path
            if comicinfo_json and (local_summary is None or local_genre is None
                                    or local_author is None or local_year is None):
                try:
                    ci = json.loads(comicinfo_json)
                except (TypeError, ValueError):
                    ci = {}
                if local_summary is None and ci.get('summary'):
                    local_summary = ci['summary']
                if local_genre is None and ci.get('genre'):
                    local_genre = ci['genre']
                if local_author is None and ci.get('writer'):
                    local_author = ci['writer']
                if local_year is None and ci.get('year'):
                    local_year = str(ci['year'])
            if local_cover_path and local_summary and local_genre and local_author and local_year:
                break

        # One-shot automatique: une série à un seul volume SANS numéro de tome détecté
        # (volume_numbers vide, calculé plus haut) est présumée one-shot sans intervention
        # de l'utilisateur. Dès qu'elle en a plusieurs, ou que son unique fichier porte
        # déjà un numéro de tome réel (ex: on importe le tome 7 d'une série qu'on ne
        # possède pas encore en entier), ce n'est PAS un one-shot - juste une série dont
        # on n'a pour l'instant qu'un seul tome. Vérifier `volume_numbers` (pas seulement
        # "one-shot is only decided by bedetheque metadata. not because there is only 1
        # file" - is_oneshot ne vient plus QUE de deux faits Bédéthèque (voir plus bas:
        # bedetheque_total_volumes > 1, ou bedetheque_status == "One shot"), jamais d'une
        # heuristique locale (nombre de fichiers possédés, numérotation...). Sans l'un de
        # ces deux faits, la valeur actuelle est simplement conservée telle quelle -
        # qu'elle vienne d'une décision manuelle (bouton one-shot) ou d'un fait
        # Bédéthèque plus ancien, jamais redevinée depuis autre chose.
        cursor.execute('SELECT is_oneshot FROM series WHERE id = ?', (series_id,))
        oneshot_row = cursor.fetchone()
        current_is_oneshot = (oneshot_row[0] or 0) if oneshot_row else 0

        bedetheque_complete = None
        bedetheque_complete_reason = None
        if bedetheque_total:
            # 1. Tomes classiques possédés == parus Bédéthèque - volume_numbers (déjà
            # calculé plus haut) ne compte QUE les tomes réellement possédés avec un
            # numéro, jamais les intégrales/HS/spéciaux qui gonfleraient artificiellement
            # total_volumes au-delà du nombre de tomes classiques annoncé.
            if owned_main_item_count >= bedetheque_total:
                bedetheque_complete = 1
                bedetheque_complete_reason = f"{owned_main_item_count}/{bedetheque_total} éléments principaux"
            else:
                # 2. Une seule intégrale possédée qui couvre TOUTE la série ("INT . ...",
                # sans numéro propre - une série qui n'a qu'une intégrale n'a souvent
                # aucun numéro du tout, voir _parse_int_hs_prefix).
                cursor.execute('''
                    SELECT COUNT(*) FROM volumes
                    WHERE series_id = ? AND is_integral = 1 AND integral_number IS NULL AND filepath IS NOT NULL
                ''', (series_id,))
                has_bare_integral_owned = cursor.fetchone()[0] > 0
                if has_bare_integral_owned:
                    bedetheque_complete = 1
                    bedetheque_complete_reason = f"intégrale possédée, couvrant les {bedetheque_total} tomes parus"
                else:
                    # 3. TOUTES les intégrales numérotées (INT1, INT2...) recensées pour
                    # cette série sont possédées - comparé contre l'ensemble des numéros
                    # d'intégrale connus (placeholders inclus), pas seulement ceux déjà
                    # possédés, pour bien détecter qu'il en manque.
                    cursor.execute('''
                        SELECT DISTINCT integral_number FROM volumes
                        WHERE series_id = ? AND is_integral = 1 AND integral_number IS NOT NULL
                    ''', (series_id,))
                    all_integral_numbers = {row[0] for row in cursor.fetchall()}
                    cursor.execute('''
                        SELECT DISTINCT integral_number FROM volumes
                        WHERE series_id = ? AND is_integral = 1 AND integral_number IS NOT NULL AND filepath IS NOT NULL
                    ''', (series_id,))
                    owned_integral_numbers = {row[0] for row in cursor.fetchall()}
                    if all_integral_numbers and all_integral_numbers <= owned_integral_numbers:
                        bedetheque_complete = 1
                        int_range = ', '.join(f"INT{n}" for n in sorted(all_integral_numbers))
                        bedetheque_complete_reason = f"toutes les intégrales possédées ({int_range}), couvrant les {bedetheque_total} tomes parus"
                    else:
                        bedetheque_complete = 0
                        # missing_volumes (calculé plus haut dans cette fonction) est déjà
                        # conscient des trous filesystem ET des tomes parus après le
                        # dernier possédé (voir son propre commentaire, expected_volumes
                        # étendu à bedetheque_total) - pas la peine de recalculer une 3e
                        # fois la même liste de trous côté client.
                        bedetheque_complete_reason = (
                            f"tomes manquants : {', '.join(str(n) for n in missing_volumes)}"
                            if missing_volumes
                            else f"{owned_main_item_count}/{bedetheque_total} éléments principaux"
                        )

        # Une série Bédéthèque à album unique est complète dès que son album
        # réellement possédé existe, même si Bédéthèque ne la classe pas « One shot »
        # et même si le fichier local n'a aucun numéro de tome.
        if bedetheque_total == 1 and owned_main_item_count > 0:
            bedetheque_complete = 1
            bedetheque_complete_reason = "One-Shot"

        # "one-shot is in bedetheque written as One Shot so there should not be a one
        # shot mistake" - comparaison insensible à la casse par précaution (Bédéthèque
        # n'est pas toujours cohérent sur la casse de ce statut d'une fiche à l'autre),
        # même si la valeur observée en base jusqu'ici est bien 'One shot' (s minuscule).
        bedetheque_status_is_oneshot = (bedetheque_status or '').strip().lower() == 'one shot'

        # Un one-shot n'a souvent aucun numéro exploitable dans sa fiche. Sans ligne
        # réelle possédée, le calcul par intervalles laisse donc missing_volumes vide
        # alors que la série est bien incomplète. Utiliser 1 comme identifiant d'unique
        # album permet à la Surveillance de le filtrer et de l'afficher comme manquant.
        if bedetheque_status_is_oneshot:
            missing_volumes = [] if total_volumes > 0 else [1]

        # Bédéthèque indique explicitement qu'un One shot est une série complète. Cette
        # règle doit primer même quand la fiche fournit `bedetheque_total_volumes = 1`:
        # sinon le calcul précédent produit `0/1 tomes parus` avant d'arriver ici et
        # empêche l'album one-shot réellement possédé d'être marqué complet.
        # Sans fichier local, on conserve toutefois un état non-complet côté collection:
        # une série ajoutée mais vide ne doit pas être affichée comme déjà possédée.
        if bedetheque_status_is_oneshot and total_volumes > 0:
            bedetheque_complete = 1
            bedetheque_complete_reason = "One shot Bédéthèque"
        elif bedetheque_complete is None and bedetheque_status_is_oneshot:
            bedetheque_complete = 0
            bedetheque_complete_reason = "One shot Bédéthèque, album non possédé"

        if bedetheque_total and bedetheque_total > 1:
            new_is_oneshot = 0
        elif bedetheque_status_is_oneshot:
            new_is_oneshot = 1
            if current_is_oneshot != 1:
                missing_volumes = []
                cursor.execute('UPDATE volumes SET volume_number = NULL WHERE series_id = ?', (series_id,))
        else:
            # "renaming the file put the serie in a one-shot? why. one-shot is only
            # decided by bedetheque metadata. not because there is only 1 file" - bug
            # réel: ce repli devinait is_oneshot=1 dès qu'une série n'avait qu'UN SEUL
            # tome possédé et aucun numéro trouvé, même sans le moindre signal
            # Bédéthèque (bedetheque_total inconnu/None, statut pas "One shot") - une
            # série multi-albums dont Bédéthèque ne donne pas de total propre (ex:
            # "Tintin - Divers", un catalogue de one-shots) basculait à tort en
            # one-shot dès qu'un rescan (déclenché par un simple renommage) tombait sur
            # un seul fichier réellement possédé. Aucun signal Bédéthèque exploitable
            # ici: on ne devine plus, on garde la valeur actuelle telle quelle (valeur
            # par défaut si jamais décidée, ou choix déjà fait - manuel ou Bédéthèque -
            # sinon), même logique que "aucun candidat ne partage un mot -> None" pour
            # le matching Bédéthèque (CLAUDE.md).
            new_is_oneshot = current_is_oneshot

        # Mettre à jour
        cursor.execute('''
            UPDATE series
            SET total_volumes = ?,
                missing_volumes = ?,
                has_parts = ?,
                local_cover_path = ?,
                local_summary = ?,
                local_genre = ?,
                local_author = ?,
                local_year = ?,
                is_oneshot = ?,
                volumes_without_metadata = ?,
                oneshot_is_integral = ?,
                bedetheque_complete = ?,
                bedetheque_complete_reason = ?,
                last_scanned = CURRENT_TIMESTAMP
            WHERE id = ?
        ''', (
            total_volumes,
            json.dumps(missing_volumes),
            1 if has_parts else 0,
            local_cover_path,
            local_summary,
            local_genre,
            local_author,
            local_year,
            new_is_oneshot,
            volumes_without_metadata,
            oneshot_is_integral,
            bedetheque_complete,
            bedetheque_complete_reason,
            series_id
        ))

        # Commit et fermeture seulement si on a créé la connexion
        if close_conn:
            conn.commit()
            conn.close()

    def get_library_stats(self, library_id):
        """Récupère les statistiques d'une bibliothèque"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        # Nombre de séries
        cursor.execute('SELECT COUNT(*) FROM series WHERE library_id = ?', (library_id,))
        series_count = cursor.fetchone()[0]

        # Nombre total de volumes
        cursor.execute('''
            SELECT COUNT(*)
            FROM volumes v
            JOIN series s ON v.series_id = s.id
            WHERE s.library_id = ?
        ''', (library_id,))
        volumes_count = cursor.fetchone()[0]

        conn.close()

        return {
            'series_count': series_count,
            'volumes_count': volumes_count
        }
