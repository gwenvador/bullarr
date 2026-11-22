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
