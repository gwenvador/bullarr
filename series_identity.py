"""Identification de la série d'un fichier : UNE seule règle pour Nouveautés (EBDZ,
Telegram, RSS) et la surveillance des séries.

« pour nouveautés et surveillance pourquoi avoir 2 codes ? » : chaque source et la
surveillance avaient leur propre rapprochement (clés de titre ici, préfixe parmi les séries
surveillées là), si bien qu'une série vue dans Nouveautés pouvait ne jamais déclencher la
surveillance (Cubitus). Toutes passent maintenant par build_series_identifier.

Règle, sur toutes les séries de la bibliothèque (jamais les seules surveillées, pour ne pas
attribuer à une série surveillée le fichier d'une autre série au titre plus long) :
1. clés de titre du nom brut, du titre lu par parse_filename, puis de ce titre amputé de
   ses derniers mots (auteurs, éditeur…), du plus long au plus court ; les séries
   « X (Préfixe) » sont aussi indexées sous « Préfixe X » (series_title_match) ;
2. sinon, le rapprochement par variantes de la recherche Bédéthèque (titres avec « & »,
   tirets…), qui exige lui aussi une série unique.
Une clé partagée par plusieurs séries arrête l'étape 1 (jamais de choix au hasard) ; l'étape 2
n'accepte elle aussi qu'une série unique.

L'étape 2 est approximative (le titre d'une série de la bibliothèque qui commence le nom suffit :
« Young » pour « Young Hellboy ») : identify.detailed(name) renvoie (série, exact) et seule
l'étape 1 est « exacte ». La surveillance ne télécharge sans validation qu'un résultat exact,
et EBDZ (titres de sujets, déjà des noms de séries) n'utilise que l'étape 1.
"""


_ARTICLES = {'le', 'la', 'les', "l'", 'un', 'une', 'des', 'de', 'du', 'd', 'l', 'the', 'a', 'an', 'et', '&', '-'}


def _significant_words(words):
    return sum(1 for w in words if w.strip('()[],.:;!?').lower() not in _ARTICLES and w.strip('()[],.:;!?-'))


def build_series_identifier(rows):
    """rows : [(series_id, title), ...]. Retourne identify(name) -> series_id ou None."""
    from series_title_match import index_series_by_title
    from blueprints.ebdz.routes import _cached_title_match_key
    from blueprints.library.scanner import LibraryScanner
    from blueprints.library.routes import (
        _normalize_title_for_match, _match_series_for_auto_import, _build_auto_import_series_match_index)

    rows = [(row[0], row[1]) for row in rows if row[1]]
    index = {
        key: (matches[0][0] if len({m[0] for m in matches}) == 1 else None)
        for key, matches in index_series_by_title(rows, lambda r: r[1], _cached_title_match_key).items()
    }
    auto_rows = [(sid, None, None, title, 0, None) for sid, title in rows]
    auto_index = _build_auto_import_series_match_index(auto_rows)

    def detailed(name, exact_only=False):
        raw = str(name or '').strip()
        if not raw:
            return None, False
        parsed = LibraryScanner.parse_filename(raw).get('title') or raw
        words = parsed.split()
        # Mots de fin retirés (auteurs, éditeur…) tant qu'il reste au moins 2 mots significatifs :
        # « Young Hellboy » ne doit pas devenir « Young », ni « Rip Kirby » « RIP ».
        variants = [raw, parsed] + [' '.join(words[:-n]) for n in range(1, len(words))
                                    if _significant_words(words[:-n]) >= 2]
        for value in variants:
            key = _cached_title_match_key(value) if value else ''
            if key and key in index:
                if index[key] is not None:
                    return index[key], True
                break  # clé ambiguë : pas de choix ici, seul le repli (lui aussi unique) peut trancher
        if exact_only:
            return None, False
        auto = _match_series_for_auto_import(_normalize_title_for_match(parsed), auto_rows, auto_index)
        return (auto[0], False) if auto else (None, False)

    def identify(name, exact_only=False):
        return detailed(name, exact_only)[0]

    identify.detailed = detailed
    return identify


# Façons dont Nouveautés a trouvé la série : toutes sont sûres sauf le repli approximatif.
SURE_MATCHES = {'thread', 'override', 'hint', 'exact'}


def _field(item, key):
    try:
        return item[key]
    except (KeyError, IndexError, TypeError):
        return None


class NouveautesMatcher:
    """LA série d'une nouveauté, telle que la page Nouveautés l'affiche. C'est Nouveautés qui
    fait le matching ; la surveillance, appelée quand un nouveau fichier est découvert, reprend
    ce résultat et vérifie seulement que la série est surveillée et que le tome manque.

    series_for(source, item) -> (series_id ou None, comment) ; comment parmi
    'thread' (sujet EBDZ lié à la série), 'override' (série choisie à la main dans Nouveautés),
    'hint' (lien Bédéthèque de la légende Telegram), 'exact' (clé de titre), 'approx' (repli
    approximatif, à valider). La source (EBDZ, Telegram, RSS) n'intervient pas : seules les
    informations portées par le fichier comptent, dans cet ordre : sujet EBDZ lié, correction
    manuelle, lien Bédéthèque, titre.
    """

    def __init__(self, cursor):
        from blueprints.missing_monitor.new_file_handler import _canonical_ebdz_thread_id
        self._canonical_thread = _canonical_ebdz_thread_id
        # Colonne absente (base ancienne ou minimale) : lue comme NULL.
        columns = {row[1] for row in cursor.execute('PRAGMA table_info(series)').fetchall()}
        select = ', '.join(name if name in columns else 'NULL'
                           for name in ('id', 'title', 'ebdz_thread_id', 'ebdz_thread_url', 'bedetheque_url'))
        rows = cursor.execute(f'SELECT {select} FROM series').fetchall()
        self.series_ids = {row[0] for row in rows}
        self.identify = build_series_identifier([(row[0], row[1]) for row in rows])
        self.thread_series = {}
        self.bedetheque_series = {}
        for series_id, _title, thread_id, thread_url, bedetheque_url in rows:
            thread = _canonical_ebdz_thread_id(thread_id, thread_url)
            if thread:
                self.thread_series.setdefault(thread, []).append(series_id)
            url = (bedetheque_url or '').strip().rstrip('/')
            if url:
                self.bedetheque_series.setdefault(url, []).append(series_id)
        try:
            overrides = cursor.execute('SELECT normalized_title, series_id FROM telegram_title_overrides').fetchall()
        except Exception:  # table absente
            overrides = []
        self.telegram_overrides = {row[0]: row[1] for row in overrides if row[1] in self.series_ids}
        self._rss_overrides = None

    def _rss_override(self, item):
        if self._rss_overrides is None:
            from blueprints.ebdz.routes import _load_rss_match_overrides
            self._rss_overrides = _load_rss_match_overrides()
        value = self._rss_overrides.get(_field(item, 'link')) or self._rss_overrides.get(_field(item, 'title'))
        try:
            value = int(value) if value is not None else None
        except (TypeError, ValueError):
            return None
        return value if value in self.series_ids else None

    def _detailed(self, name, exact_only=False):
        series_id, exact = self.identify.detailed(name, exact_only)
        if series_id is None:
            return None, None
        return series_id, ('exact' if exact else 'approx')

    def series_for(self, source, item):
        """``source`` est ignoré : la règle ne dépend que de ce que porte le fichier."""
        from blueprints.library.routes import _normalize_title_for_match
        from blueprints.library.scanner import LibraryScanner
        # 1. Sujet EBDZ lié à une série.
        thread = self._canonical_thread(_field(item, 'thread_id'), _field(item, 'thread_url'))
        linked = self.thread_series.get(thread) if thread else None
        if linked:
            # Plusieurs séries liées au même sujet : la première, à valider.
            return linked[0], ('thread' if len(linked) == 1 else 'approx')
        # 2. Série choisie à la main dans Nouveautés (par lien, titre ou titre normalisé).
        override = self._rss_override(item)
        filename = _field(item, 'filename') or ''
        if override is None and filename:
            parsed = LibraryScanner.parse_filename(filename).get('title', '')
            override = self.telegram_overrides.get(_normalize_title_for_match(parsed))
        if override is not None:
            return override, 'override'
        # 3. Lien Bédéthèque donné avec le fichier.
        hint = (_field(item, 'bedetheque_url_hint') or '').strip().rstrip('/')
        hinted = self.bedetheque_series.get(hint) if hint else None
        if hinted:
            return hinted[0], ('hint' if len(hinted) == 1 else 'approx')
        # 4. Titre. Un titre de sujet (thread_title) est déjà un nom de série : clé exacte
        # seulement ; un nom de release ou de fichier passe aussi par le repli approximatif.
        thread_title = _field(item, 'thread_title')
        if thread_title:
            return self._detailed(thread_title, exact_only=True)
        return self._detailed(_field(item, 'title') or filename)

def match_new_items(source, items, matcher=None):
    """Matching de Nouveautés des fichiers qui viennent d'être découverts : chaque item reçoit
    ``nouveautes_series_id`` et ``nouveautes_match`` (voir NouveautesMatcher.series_for).
    La surveillance (process_new_monitor_files) est appelée ensuite avec ces items."""
    if matcher is None and items:
        from blueprints.library.routes import get_db_connection
        conn = get_db_connection()
        try:
            matcher = NouveautesMatcher(conn.cursor())
        finally:
            conn.close()
    for item in items:
        if 'nouveautes_match' in item:
            continue
        series_id, how = matcher.series_for(source, item)
        item['nouveautes_series_id'] = series_id
        item['nouveautes_match'] = how
    return items
