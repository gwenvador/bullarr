"""Parsers for the current (``bdt-*`` / ``bdg-*``) Bédéthèque and BDGest page layouts.

Both sites were redesigned in October 2026: every list the app scrapes (search results,
Indispensables, Panthéon, Thèmes, reader reviews, BDGest Top) moved to new markup. The
functions here are pure (they take a parsed BeautifulSoup tree and return plain data) so
they are tested against saved copies of the real pages (tests/fixtures/bedetheque/).
Callers must treat an empty result as "layout changed" and not cache it.
"""
import re

from bs4 import NavigableString

THEME_HREF_RE = re.compile(r'theme-BD-([A-Za-z0-9_-]+)\.html')


def clean_text(node, sep=' '):
    return ' '.join(node.get_text(sep).split()) if node else ''


def own_text(node):
    """Texte direct du nœud, sans ses enfants (<em>, <small>, <span>...)."""
    if node is None:
        return ''
    return ' '.join(''.join(c for c in node.children if isinstance(c, NavigableString)).split())


def _first_int(text):
    match = re.search(r'\d+', text or '')
    return int(match.group(0)) if match else None


def parse_search_links(soup, kind):
    """Résultats de /search/tout pour `kind` = 'serie' ou 'auteur'.

    Chaque résultat est un <a> de ul.bdt-liste: .bdt-liste-libelle (nom, avec le terme
    cherché entouré d'un <span class="highlight"> - d'où la jointure sans séparateur) et
    .bdt-liste-meta (genre pour une série, pays pour un auteur).
    """
    results, seen = [], set()
    for link in soup.select(f'ul.bdt-liste a[href*="/{kind}-"]'):
        href = link.get('href', '').strip()
        label = link.select_one('.bdt-liste-libelle')
        name = clean_text(label, '') if label else clean_text(link, '')
        if not href or not name or href in seen:
            continue
        seen.add(href)
        results.append({'name': name, 'href': href, 'meta': clean_text(link.select_one('.bdt-liste-meta'))})
    return results


def parse_album_reviews(soup):
    reviews = []
    for article in soup.select('article.bdt-review'):
        body = article.select_one('[itemprop="reviewBody"]')
        if not body:
            continue
        # <br> -> vrai retour à la ligne (get_text seul les avalerait)
        for br in body.find_all('br'):
            br.replace_with('\n')
        lines = [line.strip() for line in body.get_text().splitlines()]
        text = re.sub(r'\n{3,}', '\n\n', '\n'.join(lines)).strip()
        if not text:
            continue
        author = article.select_one('[itemprop="author"] [itemprop="name"]')
        date = article.select_one('time[itemprop="datePublished"]')
        rating = article.select_one('[itemprop="ratingValue"]')
        reviews.append({
            'author': clean_text(author) or None,
            'date': date.get('datetime') if date and date.get('datetime') else None,
            'rating': _first_int(rating.get('content')) if rating else None,
            'text': text,
        })
    return reviews


def parse_indispensables(soup):
    """Podium (5) puis classement (95), dans l'ordre. Le podium n'indique pas le genre."""
    items, seen = [], set()
    for li in soup.select('ol.bdt-indisp-podium > li, ol.bdt-indisp-classement > li'):
        link = li.select_one('a.bdt-indisp-titre') or li.select_one('a.bdt-ttl')
        if not link:
            continue
        url = link.get('href', '').strip()
        title = clean_text(link) or link.get('title', '').strip()
        if not url or not title or url in seen:
            continue
        seen.add(url)
        genres = [clean_text(a) for a in li.select('.bdt-indisp-genres a')]
        items.append({'url': url, 'title': title, 'genre': ', '.join(g for g in genres if g) or '-'})
    return items


def parse_pantheon(soup):
    items = []
    for li in soup.select('ul.bdt-pantheon-grille > li'):
        link = li.select_one('a.bdt-pantheon-nom')
        if not link:
            continue
        url = link.get('href', '').strip()
        name = clean_text(link) or link.get('title', '').strip()
        if not name or not url:
            continue
        photo = li.select_one('.bdt-pantheon-photo img')
        # "1951 · Allemagne" - l'année de naissance peut manquer
        meta_parts = [p.strip() for p in clean_text(li.select_one('.bdt-pantheon-meta')).split('·') if p.strip()]
        if len(meta_parts) >= 2:
            dates, country = meta_parts[0], ' · '.join(meta_parts[1:])
        elif meta_parts and re.search(r'\d', meta_parts[0]):
            dates, country = meta_parts[0], ''
        else:
            dates, country = '', (meta_parts[0] if meta_parts else '')
        # "Franco-Belge · 2022" - catégorie et année d'entrée au Panthéon
        cat_parts = [p.strip() for p in clean_text(li.select_one('.bdt-pantheon-cat')).split('·') if p.strip()]
        items.append({
            'rank': len(items) + 1,
            'name': name,
            'url': url,
            'photo': photo.get('src', '').strip() if photo else None,
            'dates': dates,
            'professions': clean_text(li.select_one('.bdt-pantheon-metiers')),
            'country': country,
            'notable_works': clean_text(li.select_one('.bdt-pantheon-oeuvres')),
            'category': cat_parts[0] if cat_parts else '',
            'year': cat_parts[1] if len(cat_parts) > 1 else '',
        })
    return items


def parse_theme_tiles(root):
    themes = []
    for tile in root.select('ul.bdt-themes-grille a.bdt-theme-tuile'):
        href = tile.get('href', '').strip()
        match = THEME_HREF_RE.search(href)
        label = tile.select_one('b')
        name = clean_text(label) or tile.get('title', '').strip()
        if match and name:
            themes.append({'name': name, 'slug': match.group(1), 'url': href})
    return themes


def parse_theme_groups(soup):
    """Groupes de la page /theme. Chaque groupe n'y montre qu'un aperçu de ses thèmes:
    `declared` (nombre annoncé, "25 thèmes") et `more_url` ("Tous les thèmes ›") permettent
    à l'appelant de récupérer la liste complète."""
    groups = []
    for block in soup.select('div.bdt-themes-groupe'):
        heading = block.select_one('h2')
        name = own_text(heading)
        if not name:
            continue
        more = block.select_one('a.bdt-themes-tout')
        groups.append({
            'name': name,
            'themes': parse_theme_tiles(block),
            'declared': _first_int(clean_text(heading.select_one('small'))) if heading else None,
            'more_url': more.get('href', '').strip() if more else None,
        })
    return groups


def parse_theme_page(soup, fallback_title=''):
    """(titre du thème, séries) d'une page /theme-BD-<slug>.html."""
    title = own_text(soup.select_one('section.bdt-themes h1')) or fallback_title
    items = []
    for li in soup.select('ul.bdt-theme-series > li'):
        link = li.select_one('h3 a')
        if not link:
            continue
        url = link.get('href', '').strip()
        name = clean_text(link)
        if not url or not name:
            continue
        cover = li.select_one('img')
        origin = li.select_one('.bdt-theme-meta img')
        note = li.select_one('.bdt-theme-note')
        items.append({
            'title': name,
            'url': url,
            'cover': cover.get('src', '').strip() if cover else None,
            'origin': origin.get('title', '').strip() if origin else '',
            'authors': clean_text(li.select_one('.bdt-theme-auteurs')),
            'note': note.get('title', '').strip() if note else '',
            'summary': clean_text(li.select_one('p')),
        })
    return title, items


def parse_bdgest_top(soup):
    """Top annuel BDGest: podium (a.bdg-rank) puis classement (ol.bdg-t5-list)."""
    items = []
    for link in soup.select('.bdg-t5-podium a.bdg-rank'):
        url = link.get('href', '').strip()
        title_node = link.select_one('.t')
        title = own_text(title_node)
        if not url or not title:
            continue
        cover = link.select_one('img')
        items.append({
            'rank': _first_int(clean_text(link.select_one('.n'))) or len(items) + 1,
            'title': title,
            'volume_label': clean_text(title_node.select_one('em')) if title_node else '',
            'url': url,
            'cover': cover.get('src', '').strip() if cover else None,
            'authors': '',
            'publisher': '',
            'release_date': '',
            'votes': str(_first_int(clean_text(link.select_one('.k'))) or ''),
            'summary': '',
        })
    for li in soup.select('ol.bdg-t5-list > li'):
        link = li.select_one('.t a')
        if not link:
            continue
        url = link.get('href', '').strip()
        title = own_text(link)
        if not url or not title:
            continue
        volume = clean_text(link.select_one('em'))
        subtitle = clean_text(link.select_one('span'))
        credits = [p.strip() for p in clean_text(li.select_one('.t small')).split('·') if p.strip()]
        cover = li.select_one('a.cv img')
        items.append({
            'rank': _first_int(clean_text(li.select_one('.r'))) or len(items) + 1,
            'title': title,
            'volume_label': ' '.join(part for part in (volume, subtitle) if part),
            'url': url,
            'cover': cover.get('src', '').strip() if cover else None,
            'authors': credits[0] if len(credits) > 1 else '',
            'publisher': credits[-1] if len(credits) > 1 else '',
            'release_date': clean_text(li.select_one('a.d')),
            'votes': clean_text(li.select_one('.v b')),
            'summary': '',
        })
    return items
