"""
Recherche de volumes manquants sur les sources configurées
"""
import re
import os
from typing import List, Dict, Optional
from flask import current_app
from datetime import datetime
from urllib.parse import unquote
from .request_throttler import RequestThrottler, SearchResultCache, SmartSearchOptimizer


# "je ne veux pas avoir epub etre download. ajoute une section pour desactiver les
# extensions qui peuvent etre affiche et download" (blocked_search_extensions, voir
# config.py) - un résultat EBDZ/Telegram/fourtoutici/Anna's Archive a un vrai nom de
# fichier (l'extension apparaît en SUFFIXE), mais un résultat Prowlarr n'en a presque
# jamais un exploitable (voir _detect_pack_format, bedetheque/auto_acquire.py, même
# raisonnement) - le vrai format y apparaît en mot-clé n'importe où dans le titre de
# release plutôt qu'en dernière position. Les deux formes sont donc vérifiées, jamais
# une simple sous-chaîne (\bepub\b, pas "epub" seul, pour ne pas confondre avec un titre
# qui contiendrait la séquence par hasard).
def _blocked_extension_pattern(ext):
    ext = re.escape(ext.lstrip('.').lower())
    return re.compile(r'(?:\.' + ext + r'$|\b' + ext + r'\b)', re.IGNORECASE)


class MissingVolumeSearcher:
    """Recherche les volumes manquants sur les sources disponibles"""
    
    # Instance partagée du throttler et du cache (global)
    _throttler = RequestThrottler(requests_per_minute=30)
    _cache = SearchResultCache(cache_duration_minutes=60)
    _optimizer = SmartSearchOptimizer()
    
    def __init__(self):
        self.sources = {
            'ebdz': self._search_ebdz,
            'prowlarr': self._search_prowlarr,
            'telegram': self._search_telegram,
            'fourtoutici': self._search_fourtoutici,
            'annas_archive': self._search_annas_archive,
        }
    
    def search_for_volume(self, title: str, volume_num: int, sources: List[str] = None,
                           thread_id: int = None, label: str = None,
                           source_order: List[str] = None) -> List[Dict]:
        """Recherche un volume spécifique sur les sources

        Args:
            title: Titre de la série
            volume_num: Numéro du volume (ou numéro d'intégrale/hors-série)
            sources: Liste des sources à utiliser (par défaut toutes)
            thread_id: Thread EBDZ déjà matché pour cette série (series.ebdz_thread_id) -
                restreint la recherche EBDZ à ce thread précis plutôt qu'à un simple
                LIKE sur le titre. Indispensable pour une intégrale/hors-série: sans lui,
                un numéro d'intégrale pourrait piocher un fichier du thread des tomes
                normaux portant le même numéro.
            label: Texte de recherche à utiliser à la place de "{title} {volume_num}" pour
                Prowlarr (recherche plein texte) - ex. "Intégrale 6"/"HS 2" pour un tome
                qui n'est pas un tome numéroté classique.
            source_order: ordre de priorité à utiliser pour le classement final (voir
                _deduplicate_and_rank) - None (tout appelant existant) garde le dict
                `source_priority` codé en dur. Utilisé par l'acquisition automatique à
                l'ajout d'une série (blueprints/bedetheque/auto_acquire.py) pour que
                l'ordre des sources choisi dans Configuration détermine réellement quel
                résultat est téléchargé, plutôt qu'un ordre fixe non configurable.

        Returns:
            Liste des résultats trouvés
        """
        if sources is None:
            sources = list(self.sources.keys())

        all_results = []

        for source in sources:
            if source not in self.sources:
                continue

            try:
                # Vérifier le cache d'abord (le label/thread_id fait partie de la clé:
                # une intégrale et un tome normal de même numéro ne doivent pas partager
                # un résultat en cache)
                cache_key = self._cache.generate_key(source, title, volume_num, thread_id, label)
                cached_results = self._cache.get(cache_key)

                if cached_results is not None:
                    print(f"📦 Cache hit: {title} vol {volume_num} from {source}")
                    all_results.extend(cached_results)
                    continue

                # Throttle Prowlarr pour éviter les surcharges
                if source == 'prowlarr':
                    self._throttler.wait_if_needed('prowlarr')

                results = self.sources[source](title, volume_num, thread_id=thread_id, label=label)

                if results:
                    # Mettre en cache les résultats
                    self._cache.set(cache_key, results)
                    all_results.extend(results)
            except Exception as e:
                print(f"⚠️  Erreur recherche {source}: {e}")

        # Dédupliquer et trier par score de pertinence
        return self._deduplicate_and_rank(all_results, title, volume_num, source_order=source_order)
    
    def _search_ebdz(self, title: str, volume_num: int, thread_id: int = None, label: str = None) -> List[Dict]:
        """Recherche dans la base de données EBDZ (ed2k_links)

        thread_id (optionnel): restreint la recherche à ce thread précis
        (series.ebdz_thread_id, déjà résolu par le matching automatique/manuel EBDZ de la
        série) au lieu d'un simple LIKE sur le titre - indispensable pour une intégrale/
        hors-série, où un même numéro de tome existe aussi bien dans le thread des tomes
        normaux que dans celui de l'intégrale qui les regroupe (voir
        MyBBScraper.parse_volume_info côté scraper EBDZ - un seul parser pour tout,
        `ed2k_links.volume` reste NULL pour un fichier d'intégrale/HS/épisode, son propre
        numéro vit dans integral_number/hs_number/episode_number)."""
        try:
            import sqlite3

            # Utiliser la même base de données que la page search
            db_path = current_app.config.get('DB_FILE', 'data/ebdz.db')

            if not db_path or not os.path.exists(db_path):
                return []

            conn = sqlite3.connect(db_path, timeout=30.0)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            # Vérifier si la table ed2k_links existe
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='ed2k_links'")
            if not cursor.fetchone():
                conn.close()
                return []

            from blueprints.search.routes import ebdz_title_variants, normalize_search_text
            conn.create_function('search_normalize', 1, normalize_search_text)
            title_variants = [normalize_search_text(v) for v in ebdz_title_variants(title)]
            normalized_title = normalize_search_text(title)

            def _run(title_filter):
                """title_filter: chaîne à exiger (en plus du thread_id, quand il est
                connu) via LIKE, ou None pour thread_id seul.

                Un titre est requis en priorité pour un thread EBDZ qui regroupe
                PLUSIEURS séries locales sous un seul fil (constaté: le thread "Kenya"
                mélange les tomes de "Kenya"/"Namibia"/"Amazonie"/"Scotland", 4 séries
                locales distinctes toutes matchées au même thread_id - sans filtre de
                titre en plus, chercher un tome de "Namibia" renvoyait aussi des tomes de
                "Kenya"/"Amazonie"). thread_id seul (title_filter=None) reste un dernier
                repli pour le cas inverse (thread correctement dédié à une seule série,
                mais dont AUCUNE variante de titre - voir ebdz_title_variants, qui gère
                déjà l'article en tête/fin de titre - ne matche le thread_title/nom de
                fichier réel, ex: sous-titre complètement différent)."""
                sql = '''
                    SELECT DISTINCT thread_id, thread_title, thread_url, forum_category,
                           link, filename, filesize, volume
                    FROM ed2k_links
                    WHERE 1=1
                '''
                params = []
                if thread_id:
                    sql += ' AND thread_id = ?'
                    params.append(thread_id)
                if not thread_id or title_filter is not None:
                    t = title_filter if title_filter is not None else title_variants[0]
                    if thread_id:
                        # thread_id déjà connu -> thread_title est IDENTIQUE pour toutes
                        # les lignes du thread et ne distingue donc rien entre elles
                        # (constaté: le thread "Kenya" a pour thread_title unique "Kenya
                        # (Quatre cycles : Kenya + Namibia + Amazonie + Scotland)" -
                        # matcher dessus revient à matcher TOUTES les lignes, quel que
                        # soit le titre recherché parmi les 4). Seul le nom de FICHIER
                        # (propre à chaque ligne) peut distinguer un cycle/une série de
                        # l'autre dans un thread partagé.
                        sql += ' AND (search_normalize(filename) LIKE ? OR search_normalize(filename) LIKE ?)'
                        params.extend([f'%{t}%', f'%{normalized_title}%'])
                    else:
                        sql += ' AND (search_normalize(thread_title) LIKE ? OR search_normalize(filename) LIKE ? OR search_normalize(thread_title) LIKE ? OR search_normalize(filename) LIKE ?)'
                        params.extend([f'%{t}%', f'%{t}%', f'%{normalized_title}%', f'%{normalized_title}%'])
                # volume_num absent (None, aucun numéro connu, ex: intégrale listée sans
                # numéro sur Bédéthèque, ou recherche voulue sur toute la série) -> pas de
                # filtre AND volume = ?, recherche par titre/thread seul plutôt que de
                # refuser. "check why it has not parse volume 0 in bedetheque for
                # valerian" - `if volume_num` (vérité) traitait à tort un VRAI tome 0
                # (existe sur Bédéthèque) comme "absent", ratant le filtre AND volume = 0.
                if volume_num is not None:
                    sql += ' AND volume = ?'
                    params.append(volume_num)
                if label:
                    # Un thread EBDZ peut mélanger tomes normaux et intégrale/HS sous le même
                    # numéro de fichier (constaté: thread unique contenant à la fois "01 -
                    # Jukurpa" et "Intégrale 01" pour une même série) - le filtre thread_id
                    # seul ne suffit alors pas à écarter le tome normal. On exige en plus un
                    # mot-clé du label ("intégrale"/"hs") dans le nom de fichier ou du thread.
                    keyword = 'int' if label.lower().startswith('int') else 'hs'
                    sql += ' AND (LOWER(filename) LIKE ? OR LOWER(thread_title) LIKE ?)'
                    params.extend([f'%{keyword}%', f'%{keyword}%'])
                sql += f' ORDER BY thread_id DESC LIMIT {10 if volume_num is not None else 100}'

                cursor.execute(sql, params)
                return cursor.fetchall()

            if thread_id:
                # "return everything from ebdz seems the right way" (série 1009,
                # "Fanfoué des Pnottas": le thread a 7 fichiers/4 tomes, la recherche
                # n'en remontait qu'1 - tome 1) - le filtre par nom de fichier ci-dessous
                # existe pour désambiguïser un thread PARTAGÉ par plusieurs séries locales
                # (ex: thread "Kenya" = Kenya + Namibia + Amazonie + Scotland), mais il
                # rejetait à tort des tomes de la MÊME série dès que leur nom de fichier
                # n'incluait pas le titre local en entier (tome 1 "Fanfoué des Pnottas -
                # ..." matchait, tomes 2/3 "Fanfoué - 02/03 - ..." non, alors qu'ils
                # appartiennent tous les 4 au même thread_id déjà résolu à cette série
                # précise). Si ce thread n'est mappé qu'à CETTE série (cas normal), aucune
                # désambiguïsation n'est nécessaire: thread_id seul suffit, retourne tout.
                # Le filtre par variante de titre ne s'applique plus qu'au cas réellement
                # partagé (plusieurs series.ebdz_thread_id pointant vers le même thread).
                main_conn = sqlite3.connect(current_app.config['DATABASE'], timeout=30.0)
                try:
                    sharing_series_count = main_conn.execute(
                        'SELECT COUNT(*) FROM series WHERE ebdz_thread_id = ?', (thread_id,)
                    ).fetchone()[0]
                finally:
                    main_conn.close()

                if sharing_series_count <= 1:
                    rows = _run(None)
                else:
                    rows = []
                    seen_links = set()
                    for variant in title_variants:
                        for row in _run(variant):
                            link = row[4]
                            if link not in seen_links:
                                seen_links.add(link)
                                rows.append(row)
                    if not rows:
                        rows = _run(None)
            else:
                rows = []
                for variant in title_variants:
                    rows = _run(variant)
                    if rows:
                        break
            
            results = []
            for row in rows:
                from urllib.parse import unquote
                try:
                    decoded_filename = unquote(row[5])
                except Exception:
                    decoded_filename = row[5]
                results.append({
                    'source': 'ebdz',
                    'title': row[1],  # thread_title
                    'link': row[4],   # ed2k_link
                    'filename': row[5],
                    'size': row[6],
                    'volume': row[7],
                    'forum': row[3],
                    'thread_url': row[2],  # "voir comment est le fichier source" - lien vers le fil du forum
                    # "ajoute la colonne volume si tu arrives à le parser" - reparsé depuis
                    # le nom de fichier plutôt que d'utiliser `volume` (colonne DB) tel
                    # quel: ce dernier ne distingue pas un tome normal d'une intégrale/HS
                    # numérotée pareil, alors que ce label doit être cohérent avec les
                    # deux autres sources (Prowlarr/Telegram, sans colonne `volume` en DB).
                    'parsed_volume': self._parsed_volume_label(decoded_filename),
                    'resolution': self._resolution_label(decoded_filename),
                })
            
            conn.close()
            return results
            
        except Exception as e:
            print(f"Erreur EBDZ search: {e}")
            return []
    
    def _clean_series_name(self, name: str) -> str:
        """Nettoie le nom d'une série pour la recherche"""
        if not name:
            return ""
        
        import re
        
        # Convertir en minuscules
        cleaned = name.lower().strip()
        
        # Enlever les ponctuations
        chars_to_remove = ',;:\'"' + '`'
        for char in chars_to_remove:
            cleaned = cleaned.replace(char, '')
        
        cleaned = cleaned.replace('.', '')
        
        # Normaliser les espaces
        cleaned = re.sub(r'\s+', ' ', cleaned)
        cleaned = cleaned.strip()
        
        return cleaned
    
    @staticmethod
    def _parsed_volume_label(item_title: str) -> Optional[str]:
        """Tome/intégrale/hors-série/one-shot détecté dans le titre d'un résultat, pour
        affichage seul (colonne Volume du tableau de résultats) - "dans la recherche d'une
        série ajoute la colonne volume si tu arrives à le parser": best-effort, None si
        rien d'extrait plutôt que de deviner. Réutilise LibraryScanner.parse_filename,
        même logique que _confirms_requested_volume ci-dessous. `@staticmethod` (ne
        dépend d'aucun état d'instance) pour être réutilisable tel quel depuis les autres
        routes de recherche (Prowlarr/Telegram, `blueprints/prowlarr/routes.py` et
        `blueprints/telegram_channels/routes.py`) qui n'ont pas leur propre parsing de
        volume - "dans le resultats des sources il ne parse pas les volumes"."""
        from blueprints.library.scanner import LibraryScanner
        # Les noms EBDZ sont parfois renvoyés URL-encodés (ex. `%20`).
        # L'interface les décode avant affichage, mais le matcher automatique doit
        # aussi les décoder avant parse_filename pour retrouver T01, T02, etc.
        parsed = LibraryScanner.parse_filename(unquote(item_title or ''))
        tome_range = LibraryScanner._parse_integral_tome_range(item_title)
        # "il faudrait que tu parses PACK et met le dans volume dans les recherches" -
        # vérifié en premier: "PACK est mieux que T1-TXX" (voir _best_pack_result,
        # auto_acquire.py) - un fichier qui s'annonce PACK prime sur ce que la détection
        # tome/intégrale générique ci-dessous aurait pu en déduire par ailleurs.
        if parsed.get('is_pack'):
            if tome_range:
                return f"PACK (T{tome_range[0]:02d} à T{tome_range[1]:02d})"
            return "PACK"
        if parsed.get('is_integral'):
            num = parsed.get('integral_number')
            if num:
                return f"Intégrale {num}"
            # "pourquoi... est Intégrale. devrait être t01 a t06" - une intégrale détectée
            # via une plage de tomes ("T1 à T6") plutôt qu'un vrai numéro d'intégrale
            # ("Intégrale 2") n'a pas de num à afficher - la plage elle-même est
            # l'information utile ici, pas juste "Intégrale" tout court.
            start, end = parsed.get('integral_tome_start'), parsed.get('integral_tome_end')
            if start and end:
                return f"T{start:02d} à T{end:02d}"
            return "Intégrale"
        if parsed.get('is_hs'):
            num = parsed.get('hs_number')
            return f"HS {num}" if num else "Hors-série"
        if parsed.get('is_episode'):
            num = parsed.get('episode_number')
            return f"Épisode {num}" if num else "Épisode"
        if parsed.get('volume') is not None:
            return f"Tome {parsed['volume']}"
        # "Bouncer (T01- a T12) FR CBZ & PDF => tu devrais parser T01 à T12" - une plage
        # détectée sans le mot PACK ni aucun marqueur INT/HS/OS/épisode: toujours un lot
        # de plusieurs tomes en un seul fichier, affiché tel quel plutôt que rien du tout.
        if tome_range:
            return f"T{tome_range[0]:02d} à T{tome_range[1]:02d}"
        return None

    @staticmethod
    def _resolution_label(item_title: str) -> Optional[str]:
        """Tag de résolution/scan détecté dans le nom (ex: "Digital-2504",
        "UpScale 3840px") - même parser que _parsed_volume_label ci-dessus
        (LibraryScanner.parse_filename), réutilisable par les 3 sources de recherche.
        "why pour l'epervier tu ne matches pas correctement la resolution:
        [...] [Digital-2504] [...] - Digital-2504 is quality 2504?" - oui, mais
        detectResultResolution (search-results-table.js) ne le voyait pas: sa propre
        regex ("\\d+px") exige le suffixe "px", absent ici. Ce champ, calculé une fois
        côté serveur avec le même parser que le reste de l'app, laisse le frontend
        arrêter de deviner (voir le fallback conservé côté JS pour les cas où ce champ
        est absent malgré tout, ex: anciens caches)."""
        from blueprints.library.scanner import LibraryScanner
        parsed = LibraryScanner.parse_filename(item_title)
        return parsed.get('resolution')

    def _confirms_requested_volume(self, item_title: str, volume_num: Optional[int], label: Optional[str]) -> tuple:
        """Le titre d'un résultat Prowlarr contient-il vraiment le tome/l'intégrale/le
        hors-série demandé, ou seulement le nom de la série (voire un mot générique) ?
        "vérifie si les résultats des recherches parsent bien aux volumes et séries
        recherchés" - le score de _search_prowlarr ne pénalise qu'un manque de mots-clés,
        jamais un titre qui contient bien le nom de la série mais pour un AUTRE tome que
        celui demandé (un indexeur renvoie souvent toute la série en vrac). Réutilise
        LibraryScanner.parse_filename - même logique déjà éprouvée pour extraire un
        numéro de tome/intégrale/hors-série d'un nom de fichier de release, ici appliquée
        au titre du résultat plutôt qu'à un fichier déjà sur disque.

        Retourne (confirmed: bool, reason: str|None) - reason est None quand confirmed
        est True (rien à expliquer), sinon un message concret affiché en tooltip côté
        frontend ("le warning faudrait savoir pourquoi": un simple ⚠️ sans explication
        obligeait à deviner - vérifier soi-même le titre à chaque fois).

        confirmed=True si aucun tome précis n'était demandé (recherche de série entière
        sans volume_num/label). Si un tome précis EST demandé, en revanche, un titre trop
        générique pour en extraire quoi que ce soit ("Les Naufrages Du Temps.FRENCH.BD.CBR"
        - ni numéro de tome, ni INT/HS/OS) est traité comme un mismatch, pas comme
        "incertain donc accepté": "il faut que tu puisses trouver le bon album et volume"
        - impossible de prouver que ce fichier est bien LE tome demandé, donc pas de
        confiance à lui accorder par défaut."""
        from blueprints.library.scanner import LibraryScanner
        # Les résultats EBDZ peuvent contenir des noms URL-encodés (ex. `%20`).
        # Les décoder avant parse_filename permet de détecter correctement T01, T02, etc.
        parsed = LibraryScanner.parse_filename(unquote(item_title or ''))

        if label:
            label_lower = label.lower()
            match = re.search(r'(\d+)', label)
            label_num = int(match.group(1)) if match else None
            if label_lower.startswith('int'):
                if not parsed.get('is_integral'):
                    return False, f"{label} demandée, mais aucune intégrale détectée dans ce titre"
                if label_num is not None and parsed.get('integral_number') != label_num:
                    return False, f"{label} demandée, mais ce titre correspond à l'intégrale {parsed.get('integral_number')}"
                return True, None
            if label_lower.startswith('hs') or 'hors' in label_lower:
                if not parsed.get('is_hs'):
                    return False, f"{label} demandé, mais aucun hors-série détecté dans ce titre"
                if label_num is not None and parsed.get('hs_number') != label_num:
                    return False, f"{label} demandé, mais ce titre correspond au hors-série {parsed.get('hs_number')}"
                return True, None
            if label_lower.startswith('ep') or 'épisode' in label_lower or 'episode' in label_lower:
                if not parsed.get('is_episode'):
                    return False, f"{label} demandé, mais aucun épisode détecté dans ce titre"
                if label_num is not None and parsed.get('episode_number') != label_num:
                    return False, f"{label} demandé, mais ce titre correspond à l'épisode {parsed.get('episode_number')}"
                return True, None
            return True, None  # forme de label non reconnue, ne pas pénaliser à l'aveugle

        # None est LA seule sentinelle "aucun numéro connu" dans tout ce module (recherche
        # "série entière"/one-shot, voir search_volume côté routes.py) - PAS 0, qui est un
        # vrai numéro de tome existant sur Bédéthèque pour certaines séries (constaté:
        # Valérian a un vrai "Tome 0"). "check why it has not parse volume 0 in bedetheque
        # for valerian" - `if not volume_num` (vérité) traitait ce 0 réel exactement comme
        # None, empêchant toute confirmation fiable pour ce tome précis (tout candidat
        # était accepté sans vérifier qu'il s'agit bien du tome 0, comme pour une recherche
        # série entière). is None distingue enfin les deux.
        if volume_num is None:
            return True, None

        # Un tome normal (numéroté) est demandé: un résultat qui parse comme une
        # intégrale/hors-série/one-shot n'est PAS ce tome-là, même sans numéro de tome
        # "classique" à comparer - "je cherche le numéro 10 mais c'est l'intégrale qui
        # sort. ça devrait indiquer problème". Avant ce contrôle, un titre "[INTEGRALE]"
        # sans numéro de tome tombait dans le cas "rien d'extrait" ci-dessous et n'était
        # jamais signalé comme mismatch.
        if parsed.get('is_integral'):
            num = parsed.get('integral_number')
            return False, f"Tome {volume_num} demandé, mais ce titre est une intégrale" + (f" (n°{num})" if num else "")
        if parsed.get('is_hs'):
            num = parsed.get('hs_number')
            return False, f"Tome {volume_num} demandé, mais ce titre est un hors-série" + (f" (n°{num})" if num else "")
        if parsed.get('is_episode'):
            num = parsed.get('episode_number')
            return False, f"Tome {volume_num} demandé, mais ce titre est un épisode" + (f" (n°{num})" if num else "")

        parsed_volume = parsed.get('volume')
        if parsed_volume is None:
            return False, f"Tome {volume_num} demandé, mais aucun numéro de tome détecté dans ce titre (titre trop générique)"
        if parsed_volume != volume_num:
            return False, f"Tome {volume_num} demandé, mais ce titre correspond au tome {parsed_volume}"
        return True, None

    def _search_prowlarr(self, title: str, volume_num: int, thread_id: int = None, label: str = None) -> List[Dict]:
        """Recherche via Prowlarr - recherche plein texte sur les indexeurs, sans notion
        de "numéro de tome" structurée. label (optionnel, ex. "Intégrale 6"/"HS 2")
        remplace le simple numéro dans le texte de la requête quand fourni - permet de
        chercher une intégrale/hors-série correctement plutôt que de chercher le tome
        numéroté du même numéro.

        Relais mince vers search_prowlarr_raw (blueprints/prowlarr/search.py) - avant ce
        partage, cette méthode avait sa propre copie quasi-identique de la construction
        de requête/scoring que prowlarr/routes.py et search/routes.py, déjà divergée en
        pratique (ex: reconstruction de l'URL depuis `port` séparément, jamais utilisée
        par les 2 autres copies). confirm=True: seul appelant qui a besoin du warning
        "tome non confirmé" (voir _confirms_requested_volume), limit=30 pour préserver le
        comportement historique de cette méthode (repli, voir commentaire ci-dessus dans
        l'ancien code : la recherche "série entière" élargie à 30 résultats bruts)."""
        from blueprints.prowlarr.search import search_prowlarr_raw

        try:
            results = search_prowlarr_raw(title, volume_num=volume_num, label=label, confirm=True, limit=30)
            return results or []
        except Exception as e:
            print(f"Erreur Prowlarr search: {e}")
            return []

    
    def _search_telegram(self, title: str, volume_num: int, thread_id: int = None, label: str = None) -> List[Dict]:
        """Recherche parmi les fichiers Telegram scrapés (voir
        blueprints/telegram_channels) - "je ne vois pas telegram dans... rechercher dans
        le volume": la modale de recherche par tome (searchMissingVolume, library.js)
        n'interrogeait jusqu'ici que EBDZ/Prowlarr, cette source manquait entièrement
        alors que la page /search l'a depuis longtemps.

        "why telegram search so slow in decouvrir. this should be instant as we updated
        the database" - interrogeait auparavant l'API Telegram EN DIRECT (search_channels,
        Telethon, plusieurs secondes) plutôt que la base locale déjà scrapée/indexée
        (search_telegram_files_local, blueprints/telegram_channels/routes.py).

        "1014 toujours pas de telegram result" - self._clean_series_name(title)
        SUPPRIME l'apostrophe (`'Lincroyable Histoire'`) au lieu de la remplacer par un
        espace, contrairement à normalize_search_text (utilisée par
        search_telegram_files_local elle-même pour construire ses mots de recherche ET
        pour normaliser les noms de fichiers à l'écriture) qui la remplace par un espace
        ("l incroyable histoire") - vérifié concrètement: "lincroyable" (un seul mot
        fusionné) ne matche jamais aucun nom de fichier réel, qui normalise toujours vers
        deux mots séparés "l"/"incroyable". search_telegram_files_local fait déjà sa
        propre normalisation complète (voir sa docstring) - lui passer le titre BRUT
        plutôt qu'un nettoyage local redondant et incohérent avec elle."""
        try:
            from blueprints.telegram_channels.routes import search_telegram_files_local

            files = search_telegram_files_local(title, limit=30)

            results = []
            for f in files:
                # "alors que clairement c'est HS1... valide les recherche telegram
                # comme pour prowlarr" - même repli sans notion de tome structurée que
                # Prowlarr (voir _search_prowlarr), exposé au même risque de faux
                # résultat ("HS1" remonté pour une recherche de tome 1).
                confirmed, reason = self._confirms_requested_volume(f['filename'], volume_num, label)
                results.append({
                    'source': 'telegram',
                    'filename': f['filename'],
                    'size': f.get('file_size'),
                    'channel': f['channel'],
                    'channel_title': f.get('channel_title') or f['channel'],
                    'message_id': f['message_id'],
                    'parsed_volume': f.get('parsed_volume') or self._parsed_volume_label(f['filename']),
                    'resolution': f.get('resolution') or self._resolution_label(f['filename']),
                    # Uniquement pour la déduplication par lien dans
                    # _deduplicate_and_rank ci-dessous (jamais affiché ni cliqué -
                    # le téléchargement passe par channel/message_id, voir
                    # downloadTelegramFile côté frontend)
                    'link': f"telegram://{f['channel']}/{f['message_id']}",
                    'unconfirmed_volume': not confirmed,
                    'unconfirmed_reason': reason,
                })
            return results
        except Exception as e:
            print(f"Erreur Telegram search: {e}")
            return []

    def _search_fourtoutici(self, title: str, volume_num: int, thread_id: int = None, label: str = None) -> List[Dict]:
        """Recherche sur fourtoutici.cc (item #24 improvement.txt) - source à
        téléchargement HTTP direct, comme Telegram (voir _search_telegram ci-dessus,
        même découpage recherche/scoring). search_fourtoutici_raw fait juste l'appel API
        + le filtre d'extension ; le parsing volume/titre et la confirmation restent ici,
        même répartition que pour Telegram (search_telegram_files_local)."""
        try:
            from blueprints.fourtoutici.scraper import search_fourtoutici_raw
            from blueprints.library.scanner import LibraryScanner

            search_title = self._clean_series_name(title)
            files = search_fourtoutici_raw(search_title, limit=30)

            results = []
            for f in files:
                filename = f.get('original_name') or f.get('file_name') or ''
                confirmed, reason = self._confirms_requested_volume(filename, volume_num, label)
                parsed = LibraryScanner.parse_filename(filename)
                results.append({
                    'source': 'fourtoutici',
                    'filename': filename,
                    'size': f.get('size'),
                    'file_id': f.get('file_id'),
                    'download_url': f.get('download_url'),
                    'link': f.get('download_url'),
                    'parsed_volume': self._parsed_volume_label(filename),
                    'resolution': self._resolution_label(filename),
                    'volume': parsed['volume'],
                    'is_integral': parsed['is_integral'],
                    'integral_number': parsed['integral_number'],
                    'is_hs': parsed['is_hs'],
                    'hs_number': parsed['hs_number'],
                    'unconfirmed_volume': not confirmed,
                    'unconfirmed_reason': reason,
                })
            return results
        except Exception as e:
            print(f"Erreur fourtoutici search: {e}")
            return []

    def _search_annas_archive(self, title: str, volume_num: int, thread_id: int = None, label: str = None) -> List[Dict]:
        """Recherche des BD publiques dans Anna's Archive (résultats HTML uniquement)."""
        try:
            from blueprints.annas_archive.scraper import search_annas_archive_raw
            from blueprints.library.scanner import LibraryScanner
            files = search_annas_archive_raw(self._clean_series_name(title), limit=30)
            results = []
            for item in files:
                filename = item.get('title') or ''
                confirmed, reason = self._confirms_requested_volume(filename, volume_num, label)
                parsed = LibraryScanner.parse_filename(filename)
                results.append({
                    'source': 'annas_archive', 'title': filename, 'filename': filename,
                    'size': item.get('size'), 'info_url': item.get('info_url'),
                    'md5': item.get('md5'),
                    'download_url': item.get('partner_url'), 'link': item.get('info_url'),
                    'parsed_volume': self._parsed_volume_label(filename),
                    'resolution': self._resolution_label(filename),
                    'volume': parsed['volume'], 'is_integral': parsed['is_integral'],
                    'integral_number': parsed['integral_number'], 'is_hs': parsed['is_hs'],
                    'hs_number': parsed['hs_number'], 'unconfirmed_volume': not confirmed,
                    'unconfirmed_reason': reason,
                })
            return results
        except Exception as e:
            print(f"Erreur Anna's Archive search: {e}")
            return []

    def _deduplicate_and_rank(self, results: List[Dict], title: str, volume_num: int,
                               source_order: List[str] = None) -> List[Dict]:
        """Déduplique et trie les résultats par pertinence

        Args:
            results: Liste des résultats bruts
            title: Titre de la série (pour calcul de score)
            volume_num: Numéro du volume
            source_order: voir search_for_volume - remplace le dict source_priority
                codé en dur ci-dessous quand fourni (position dans la liste = priorité,
                score le plus haut en tête ; une source absente de la liste retombe sur
                le score par défaut ci-dessous plutôt que d'être exclue, ce filtrage ayant
                déjà eu lieu plus haut dans search_for_volume).

        Returns:
            Résultats dédupliqués et triés
        """
        # blocked_search_extensions (config.py/settings "Formats de recherche") - exclu
        # ICI plutôt que côté frontend ou juste dans l'éligibilité d'acquisition
        # automatique: cette méthode est le POINT D'ENTRÉE UNIQUE partagé par la
        # recherche manuelle (search_missing_volume, library.js) ET la recherche
        # automatique (run_auto_acquire_for_series, bedetheque/auto_acquire.py) - voir
        # search_for_volume, qui appelle toujours _deduplicate_and_rank en dernier. Un
        # format bloqué n'apparaît donc ni dans le tableau de résultats manuel, ni comme
        # candidat pour un téléchargement automatique, en un seul endroit.
        from blueprints.library.routes import load_library_import_config
        blocked_extensions = load_library_import_config().get('blocked_search_extensions', [])
        if blocked_extensions:
            blocked_patterns = [_blocked_extension_pattern(ext) for ext in blocked_extensions]
            results = [
                r for r in results
                if not any(p.search(r.get('filename') or r.get('title') or '') for p in blocked_patterns)
            ]

        # "aussi on dirait que la recherche de prowlarr ne s'affiche plus" - certains
        # indexeurs Prowlarr ne renseignent que 'downloadUrl' (voir search_prowlarr_raw),
        # jamais 'link', laissant 'link' vide pour CHAQUE résultat de cet indexeur. Le
        # `if link and ...` ci-dessous ne se contentait pas de rater la dédup dans ce cas:
        # un lien vide (faux) faisait carrément tout perdre le résultat, silencieusement -
        # 29 résultats Prowlarr bien réels réduits à 0 après ce passage. download_url en
        # repli (même logique déjà utilisée côté frontend, voir _markSearchResultAdded,
        # search-results-table.js: "r.download_url || r.link") comme clé de dédup, et un
        # résultat sans AUCUN des deux est quand même conservé (juste jamais déduplicable)
        # plutôt que perdu.
        seen_links = set()
        unique_results = []

        for result in results:
            link = (result.get('link') or result.get('download_url') or '').lower()
            if not link:
                unique_results.append(result)
            elif link not in seen_links:
                seen_links.add(link)
                unique_results.append(result)

        # Trier par source (priorité) - "la priorité c'est telegram... pourquoi ca a
        # envoyé amule": il existait un poids FIXE par source (EBDZ toujours 100,
        # Telegram toujours 60, fourtoutici toujours 45 - jamais une vraie pertinence
        # calculée sur le contenu) trié avant la priorité, qui rendait tout ordre de
        # sources choisi par l'utilisateur totalement inopérant. Supprimé entièrement
        # ("remove all this calculation") : l'ordre des sources (settings, ou le dict par
        # défaut ci-dessous si aucun n'est fourni) est désormais le SEUL classement
        # cross-source, sans second critère caché. `seeders` ne sert plus qu'à départager
        # deux résultats de LA MÊME source (torrents Prowlarr). unconfirmed_volume reste
        # le tout premier critère (jamais un résultat non confirmé devant un confirmé,
        # quelle que soit la source).
        if source_order:
            # Position 0 = priorité la plus haute -> score le plus haut. Longueur de la
            # liste comme base pour que le score reste toujours positif et strictement
            # décroissant, quel que soit le nombre de sources fournies.
            source_priority = {name: (len(source_order) - i) * 10 for i, name in enumerate(source_order)}
        else:
            source_priority = {'prowlarr': 50, 'ebdz': 40, 'telegram': 30, 'fourtoutici': 20}

        def sort_key(item):
            source_score = source_priority.get(item.get('source', ''), 10)
            seeders = item.get('seeders', 0)
            return (bool(item.get('unconfirmed_volume')), -source_score, -seeders)

        return sorted(unique_results, key=sort_key)
