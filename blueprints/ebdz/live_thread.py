"""
Relecture immédiate d'un sujet EBDZ : adresse collée dans « Matcher manuellement sur EBDZ ».

Le scraper habituel ne relit un sujet que si son nombre de réponses change, et un sujet scrapé sans
aucun lien est tout de même marqué « à jour » : si les liens sont ajoutés ensuite en modifiant le
premier message, Bullarr ne les voit jamais. Ici, une adresse de sujet collée par l'utilisateur est
lue en direct (connexion avec les identifiants EBDZ déjà enregistrés), ses liens sont enregistrés et
son titre est mémorisé, avec ou sans lien.
"""
import re

from bs4 import BeautifulSoup

from .scraper import MyBBScraper

THREAD_URL = 'https://ebdz.net/forum/showthread.php?tid={tid}'
SITE_NAME = 'emulebdz'


class EbdzThreadError(Exception):
    """Relecture impossible (identifiants, page illisible...) ; le message est affichable."""


def thread_id_from_text(text):
    """Numéro de sujet dans une adresse EBDZ collée (tid=...), None sinon."""
    match = re.search(r'tid=(\d+)', str(text or ''))
    return match.group(1) if match else None


def parse_thread_page(html):
    """(titre du sujet, numéro de forum) lus sur la page d'un sujet MyBB ; (None, None) si la page
    n'est pas un sujet (page de connexion, erreur...)."""
    soup = BeautifulSoup(html or '', 'html.parser')
    title = None
    for selector in ('.navigation span.active', 'span.active', 'h1'):
        node = soup.select_one(selector)
        text = node.get_text(' ', strip=True) if node else ''
        if text and text.lower() != SITE_NAME:
            title = text
            break
    if not title and soup.title:
        head = soup.title.get_text(' ', strip=True)
        head = re.split(r'\s+[-–|]\s+', head)[0].strip()
        if head and head.lower() != SITE_NAME:
            title = head
    forum_id = None
    for link in soup.select('.navigation a[href*="forumdisplay.php"], a[href*="forumdisplay.php?fid="]'):
        match = re.search(r'fid=(\d+)', link.get('href', ''))
        if match:
            forum_id = match.group(1)
    return title, forum_id


def scrape_thread_now(thread_id, config, db_file):
    """Lit le sujet `thread_id`, enregistre ses liens ed2k et son titre. Retourne
    {thread_id, title, url, forum_category, links}. Lève EbdzThreadError avec un message clair."""
    username = config.get('username', '')
    password = config.get('password_decrypted', '')
    if not username or not password:
        raise EbdzThreadError('Identifiants EBDZ non configurés (Configuration → Indexeurs → EBDZ)')
    forums = config.get('forums') or []

    scraper = MyBBScraper(base_url='https://ebdz.net/forum/forumdisplay.php', db_file=db_file,
                          username=username, password=password, forum_category='')
    if not scraper.login():
        raise EbdzThreadError('Connexion à EBDZ impossible : vérifiez les identifiants')

    url = THREAD_URL.format(tid=thread_id)
    try:
        response = scraper.session.get(url, timeout=30)
    except Exception as exc:
        raise EbdzThreadError(f'EBDZ injoignable : {exc}') from exc
    title, forum_id = parse_thread_page(response.text)
    if not title:
        raise EbdzThreadError("Sujet EBDZ introuvable ou illisible (numéro inexistant, ou section non accessible)")

    category = next((f.get('category') for f in forums if str(f.get('fid')) == str(forum_id)), None)
    if not category and forums:
        category = forums[0].get('category')
    scraper.forum_category = category or ''
    data = scraper.scrape_thread(url, title)
    if data:
        scraper.save_to_db(data)
    scraper._save_thread_state(str(thread_id), None, title=title, url=url)
    return {'thread_id': str(thread_id), 'title': title, 'url': url,
            'forum_category': scraper.forum_category, 'links': len(data)}
