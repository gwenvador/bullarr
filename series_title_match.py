"""Index des séries locales par titre, partagé par Nouveautés (EBDZ, RSS, Telegram) et
l'import automatique : une seule règle d'identification pour toutes les sources.

Bédéthèque rejette le début d'un titre entre parenthèses : « Alix (Les Voyages d') » se lit aussi
« Les Voyages d'Alix ». Réduit à son mot-clé, ce titre vaut « alix » et se confondait avec la série
« Alix » : toutes les deux devenaient ambiguës et aucune release n'était reconnue. Chaque série
est donc indexée sous sa forme naturelle, et la série au titre simple garde le mot-clé court."""
import re


def parenthetical_prefix_title(title):
    """« Alix (Les Voyages d') » -> « Les Voyages d'Alix » ; « Spirou (Le Petit) » -> « Le Petit Spirou ».
    Chaîne vide si le titre n'a pas cette forme."""
    match = re.fullmatch(r'\s*(.+?)\s*\(([^()]+)\)\s*', str(title or ''))
    if not match:
        return ''
    base, prefix = match.group(1).strip(), match.group(2).strip()
    if not base or not prefix:
        return ''
    return f'{prefix}{base}' if prefix.endswith(("'", '’')) else f'{prefix} {base}'


def title_keys(title, key_of):
    """Clés de titre d'une série : la clé habituelle et, si besoin, celle de sa forme naturelle."""
    keys = {key_of(title)}
    alias = parenthetical_prefix_title(title)
    if alias:
        keys.add(key_of(alias))
    keys.discard('')
    return keys


def index_series_by_title(rows, title_of, key_of):
    """{clé de titre: [séries candidates]}. Le titre simple d'une série garde son mot-clé ; une série
    « X (Préfixe) » est indexée sous « Préfixe X » et ne prend le mot-clé « x » que s'il est libre."""
    plain, aliased = [], []
    for row in rows:
        title = title_of(row)
        if not title:
            continue
        alias_key = key_of(parenthetical_prefix_title(title)) if parenthetical_prefix_title(title) else ''
        (aliased if alias_key and alias_key != key_of(title) else plain).append(row)
    index = {}
    plain_keys = set()
    for row in plain:
        key = key_of(title_of(row))
        if key:
            index.setdefault(key, []).append(row)
            plain_keys.add(key)
    for row in aliased:
        title = title_of(row)
        index.setdefault(key_of(parenthetical_prefix_title(title)), []).append(row)
        main = key_of(title)
        if main and main not in plain_keys:
            index.setdefault(main, []).append(row)
    return index
