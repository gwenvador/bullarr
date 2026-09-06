import requests
from bs4 import BeautifulSoup
import re
import sqlite3
from urllib.parse import urljoin, unquote
import html as html_module
import time
import os
import hashlib

class MyBBScraper:
    def __init__(self, base_url, db_file, username, password, forum_category=""):
        self.base_url = base_url
        self.db_file = db_file
        self.username = username
        self.password = password
        self.forum_category = forum_category
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        })
        self.logged_in = False
        self.new_links = []
        
        # Créer les répertoires nécessaires
        os.makedirs('./data/covers', exist_ok=True)
        
    def connect_db(self):
        """Connexion à la base SQLite"""
        try:
            connection = sqlite3.connect(self.db_file)
            return connection
        except Exception as e:
            print(f"Erreur de connexion SQLite: {e}")
            return None
    
    def login(self):
        """Se connecter au forum myBB"""
        try:
            # Récupère la page principale pour obtenir les cookies et le my_post_key
            home_url = "https://ebdz.net/forum/index.php"
            # timeout: sans lui, une requête qui ne répond jamais (site down, proxy qui
            # coupe la connexion sans FIN/RST) bloque le thread de scraping (route Flask
            # synchrone ou job APScheduler) indéfiniment - ne change rien à la fréquence
            # des requêtes envoyées, borne juste le temps d'attente d'une réponse.
            response = self.session.get(home_url, timeout=30)
            soup = BeautifulSoup(response.content, 'html.parser')
            
            # Extrait le my_post_key du HTML
            my_post_key = None
            for script in soup.find_all('script'):
                if script.string and 'my_post_key' in script.string:
                    match = re.search(r'my_post_key = "([^"]+)"', script.string)
                    if match:
                        my_post_key = match.group(1)
                        break
            
            # Prépare les données de connexion selon le formulaire myBB
            login_data = {
                'action': 'do_login',
                'url': home_url,
                'quick_login': '1',
                'my_post_key': my_post_key,
                'quick_username': self.username,
                'quick_password': self.password,
                'quick_remember': 'yes',
                'submit': 'Se connecter'
            }
            
            # Envoie le formulaire de login
            login_url = "https://ebdz.net/forum/member.php"
            response = self.session.post(login_url, data=login_data, allow_redirects=True, timeout=30)
            
            # Vérifie si connecté
            if 'action=logout' in response.text or 'Déconnexion' in response.text:
                print(f"✓ Connecté en tant que {self.username}")
                self.logged_in = True
                return True
            else:
                print("✗ Échec de connexion - vérifie tes identifiants")
                return False
                
        except Exception as e:
            print(f"Erreur lors de la connexion: {e}")
            import traceback
            traceback.print_exc()
            return False
    
    def parse_volume_info(self, filename):
        """Extrait numéro de tome/intégrale/hors-série/épisode depuis le nom de fichier -
        "je ne veux pas deux parsers, un seul parser pour tout": délègue entièrement à
        LibraryScanner.parse_filename (le même parser que l'import et que
        _parsed_volume_label côté recherche Prowlarr/Telegram), au lieu du jeu de regex
        séparé et divergent que ce module avait auparavant (extract_volume_number,
        supprimée - un peu plus permissif sur certaines conventions mais confondait le
        numéro d'une INTÉGRALE avec un numéro de tome, ex: "Natacha - Intégrale 02..."
        renvoyait volume=2 comme si c'était le tome 2).

        Retourne un dict avec les mêmes clés que les colonnes ed2k_links correspondantes
        (volume, is_integral, integral_number, is_hs, hs_number, is_episode,
        episode_number), toutes à None/0 si filename est vide."""
        empty = {
            'volume': None, 'is_integral': False, 'integral_number': None,
            'is_hs': False, 'hs_number': None, 'is_episode': False, 'episode_number': None,
        }
        if not filename:
            return empty

        from blueprints.library.scanner import LibraryScanner

        # Les noms de fichiers issus des liens ed2k sont encodés en URL (%20 pour les
        # espaces, etc.) - on décode avant le parsing sinon les patterns basés sur des
        # espaces/tirets autour du numéro ne correspondent jamais.
        decoded = unquote(filename)
        parsed = LibraryScanner.parse_filename(decoded)
        return {
            'volume': parsed.get('volume'),
            'is_integral': bool(parsed.get('is_integral')),
            'integral_number': parsed.get('integral_number'),
            'is_hs': bool(parsed.get('is_hs')),
            'hs_number': parsed.get('hs_number'),
            'is_episode': bool(parsed.get('is_episode')),
            'episode_number': parsed.get('episode_number'),
        }

    def create_table(self):
        """Crée la table pour stocker les liens ed2k avec colonne volume"""
        connection = self.connect_db()
        if connection:
            cursor = connection.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS ed2k_links (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    link TEXT NOT NULL UNIQUE,
                    filename TEXT,
                    filesize TEXT,
                    volume INTEGER,
                    thread_title TEXT,
                    thread_url TEXT,
                    thread_id TEXT,
                    forum_category TEXT,
                    cover_image TEXT,
                    description TEXT,
                    date_scraped TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cursor.execute("PRAGMA table_info(ed2k_links)")
            existing_columns = {row[1] for row in cursor.fetchall()}
            for col_name, col_type in [
                ('is_integral', 'INTEGER DEFAULT 0'),
                ('integral_number', 'INTEGER'),
                ('is_hs', 'INTEGER DEFAULT 0'),
                ('hs_number', 'INTEGER'),
                ('is_episode', 'INTEGER DEFAULT 0'),
                ('episode_number', 'INTEGER'),
            ]:
                if col_name not in existing_columns:
                    cursor.execute(f"ALTER TABLE ed2k_links ADD COLUMN {col_name} {col_type}")
            # Index ajoutés a posteriori (table déjà en prod avec 60k+ lignes) : sans eux,
            # `ORDER BY date_scraped DESC` (latest_scrape(), routes.py) et les `COUNT(*)
            # WHERE forum_category = ?` (scrape()/scheduler.py, comptage post-scrape) font
            # un scan complet de la table à chaque appel. IF NOT EXISTS les rend gratuits
            # une fois créés (appelé à chaque run() - une fois par forum scrapé).
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_ed2k_links_date_scraped ON ed2k_links(date_scraped)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_ed2k_links_thread_id ON ed2k_links(thread_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_ed2k_links_forum_category ON ed2k_links(forum_category)")
            if 'thread_title_normalized' not in existing_columns:
                cursor.execute("ALTER TABLE ed2k_links ADD COLUMN thread_title_normalized TEXT")
            if 'filename_normalized' not in existing_columns:
                cursor.execute("ALTER TABLE ed2k_links ADD COLUMN filename_normalized TEXT")
            cursor.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS ed2k_links_fts USING fts5(
                    thread_title_normalized, filename_normalized,
                    content='ed2k_links', content_rowid='id', tokenize='trigram'
                )
            """)
            # Triggers de synchronisation (INSERT/DELETE/UPDATE futurs) créés dans
            # ensure_ed2k_search_index() APRÈS le peuplement initial, pas ici - sinon le
            # tout premier peuplement (UPDATE en masse de ~84k lignes déjà existantes)
            # déclencherait le trigger AU sur chacune, doublant son propre travail en
            # écritures FTS supplémentaires pendant la même grosse transaction ("database
            # disk image is malformed" observé une fois avec les triggers déjà en place -
            # jamais reproduit une fois le fichier réouvert, mais plus la peine de risquer
            # cette charge d'écriture amplifiée pour un peuplement qui n'en a pas besoin).
            # État "vu pour la dernière fois" par thread ("improve the ebdz scraper. find
            # the new thread that have been updated and just scrape them. so no need of
            # number of pages") - reply_count est le nombre de réponses affiché dans la
            # liste des threads du forum (colonne "Réponses" de MyBB) : un thread dont le
            # nombre de réponses n'a pas changé depuis le dernier scrape n'a reçu aucun
            # nouveau message, donc aucun nouveau lien ed2k à y trouver. Voir
            # get_thread_links() pour comment ça arrête la pagination.
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS ebdz_thread_state (
                    thread_id TEXT NOT NULL,
                    forum_category TEXT NOT NULL,
                    reply_count INTEGER,
                    last_scraped_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (thread_id, forum_category)
                )
            """)
            connection.commit()
            cursor.close()
            connection.close()
            print("✓ Table créée/vérifiée dans ebdz.db")

    def _load_known_thread_state(self, connection):
        """{thread_id: reply_count} déjà connus pour ce forum_category - une seule requête
        au début de get_thread_links() plutôt qu'une requête par thread rencontré."""
        cursor = connection.cursor()
        cursor.execute(
            "SELECT thread_id, reply_count FROM ebdz_thread_state WHERE forum_category = ?",
            (self.forum_category,)
        )
        known = {row[0]: row[1] for row in cursor.fetchall()}
        cursor.close()
        return known

    def _save_thread_state(self, thread_id, reply_count):
        connection = self.connect_db()
        if not connection:
            return
        cursor = connection.cursor()
        cursor.execute("""
            INSERT INTO ebdz_thread_state (thread_id, forum_category, reply_count, last_scraped_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(thread_id, forum_category) DO UPDATE SET
                reply_count = excluded.reply_count,
                last_scraped_at = excluded.last_scraped_at
        """, (thread_id, self.forum_category, reply_count))
        connection.commit()
        cursor.close()
        connection.close()
    
    def extract_ed2k_links(self, html):
        """Extrait les liens ed2k du HTML.

        `html` est le texte BRUT de la réponse (response.text), entités HTML non
        décodées: un "&" dans un nom de fichier ressort donc échappé "&amp;" dans le
        markup source (MyBB), et le regex ci-dessous le capture tel quel. Sans
        html_module.unescape ici, ce "&amp;" (et tout autre &lt;/&gt;/&quot;/&#39;)
        restait ensuite figé dans `link`/`filename` jusqu'à l'affichage final -
        aucun endroit en aval ne s'en charge (decodeFilename côté JS ne gère que le
        %-encoding, pas les entités HTML). Décodé UNE FOIS ici, à la source, plutôt
        que remis à la charge de chaque consommateur."""
        ed2k_pattern = r'ed2k://\|file\|[^\s<>"]+'
        links = re.findall(ed2k_pattern, html)
        return [html_module.unescape(link) for link in links]
    
    def parse_ed2k_link(self, link):
        """Parse un lien ed2k pour extraire infos"""
        parts = link.split('|')
        filename = parts[2] if len(parts) > 2 else None
        filesize = parts[3] if len(parts) > 3 else None
        return filename, filesize
    
    # Borne défensive uniquement (boucle infinie si un forum a une pagination cassée) -
    # plus un réglage normal côté utilisateur ("find the new thread that have been
    # updated and just scrape them. so no need of number of pages"): la pagination
    # s'arrête maintenant d'elle-même dès qu'elle retrouve un thread déjà connu et
    # inchangé, voir get_thread_links().
    _MAX_SAFETY_PAGES = 40

    def _parse_thread_row(self, row, forum_url):
        """Extrait (thread_url, thread_title, thread_id, reply_count, is_sticky) d'une
        <tr class="inline_row"> de la liste des threads d'un forum, ou None si la ligne ne
        ressemble pas à un thread exploitable (rangée de séparation "Sujets importants"/
        "Sujets normaux", etc. - celles-ci n'ont pas de lien showthread direct)."""
        # Lien du SUJET lui-même (pas les liens de pagination "page 2/3/..." du même thread,
        # ni le lien "Dernier message"/"who posted" qui pointent aussi vers showthread.php/
        # misc.php avec des paramètres supplémentaires) - ancré en fin de chaîne pour ça.
        subject_link = row.find('a', href=re.compile(r'showthread\.php\?tid=\d+$'))
        if not subject_link:
            return None

        href = subject_link.get('href', '')
        tid_match = re.search(r'tid=(\d+)', href)
        if not tid_match:
            return None
        thread_id = tid_match.group(1)

        thread_url = urljoin(forum_url.split('forumdisplay.php')[0], href)
        thread_url = thread_url.split('#')[0].split('&page=')[0]
        thread_title = subject_link.get_text(strip=True)
        if not thread_title:
            return None

        # Nombre de réponses ("Réponses", colonne à côté des vues) - le lien "qui a
        # répondu" (misc.php?action=whoposted) porte ce nombre comme texte, c'est la seule
        # occurrence fiable sur la ligne (le nombre de vues est un <td> voisin sans lien).
        reply_count = None
        whoposted_link = row.find('a', href=re.compile(r'action=whoposted'))
        if whoposted_link:
            digits = re.sub(r'[^\d]', '', whoposted_link.get_text())
            if digits:
                reply_count = int(digits)

        is_sticky = 'forumdisplay_sticky' in str(row.get('class', [])) or bool(row.find(class_='forumdisplay_sticky'))

        return (thread_url, thread_title, thread_id, reply_count, is_sticky)

    def get_thread_links(self, forum_url):
        """Parcourt la liste des threads du forum, triée par défaut par MyBB "dernier
        message" décroissant (sortby=lastpost), et s'arrête dès qu'elle retombe sur un
        thread DÉJÀ connu dont le nombre de réponses n'a pas changé depuis le dernier
        scrape ("find the new thread that have been updated and just scrape them") - tout
        ce qui suit dans une liste triée par activité décroissante est nécessairement
        aussi vieux ou plus vieux, donc déjà vu. Les threads épinglés ("Sujets importants",
        `forumdisplay_sticky`) ne déclenchent jamais cet arrêt (ils restent en haut quelle
        que soit leur activité réelle) mais sont eux aussi ignorés s'ils sont inchangés.

        Retourne une liste de (thread_url, thread_title, thread_id, reply_count) -
        uniquement les threads nouveaux ou mis à jour depuis le dernier scrape."""
        threads_to_scrape = []
        seen_urls = set()
        page = 1

        connection = self.connect_db()
        known_state = self._load_known_thread_state(connection) if connection else {}
        if connection:
            connection.close()

        skipped_unchanged = 0

        try:
            while True:
                if page == 1:
                    page_url = forum_url
                else:
                    if '?' in forum_url:
                        page_url = f"{forum_url}&page={page}"
                    else:
                        page_url = f"{forum_url}?page={page}"

                print(f"  Lecture page {page} du forum...")
                response = self.session.get(page_url, timeout=30)
                soup = BeautifulSoup(response.content, 'html.parser')

                rows = soup.find_all('tr', class_='inline_row')
                if not rows:
                    break

                page_new_count = 0
                reached_known_unchanged = False

                for row in rows:
                    parsed = self._parse_thread_row(row, forum_url)
                    if not parsed:
                        continue
                    thread_url, thread_title, thread_id, reply_count, is_sticky = parsed

                    if thread_url in seen_urls:
                        continue
                    seen_urls.add(thread_url)

                    # reply_count=None (parsing du nombre de réponses a échoué) traité
                    # comme "changé" par prudence - mieux rescraper un thread pour rien
                    # qu'en manquer un nouveau lien.
                    already_known = (
                        thread_id in known_state
                        and reply_count is not None
                        and known_state[thread_id] == reply_count
                    )
                    if already_known:
                        skipped_unchanged += 1
                        # Un thread épinglé inchangé n'indique rien sur la fraîcheur du
                        # reste de la liste (il resterait en haut même très ancien) - seul
                        # un thread NON épinglé inchangé confirme qu'on est repassé dans
                        # la partie déjà entièrement connue de la liste triée par activité.
                        if not is_sticky:
                            reached_known_unchanged = True
                            break
                        continue

                    threads_to_scrape.append((thread_url, thread_title, thread_id, reply_count))
                    page_new_count += 1

                print(f"  → {page_new_count} thread(s) nouveau(x)/modifié(s) sur cette page"
                      + (f", {skipped_unchanged} déjà à jour ignoré(s) au total" if skipped_unchanged else ""))

                if reached_known_unchanged:
                    print("  ✓ Thread déjà connu et inchangé atteint - reste de la liste ignoré")
                    break

                if page >= self._MAX_SAFETY_PAGES:
                    print(f"  ⚠️ Limite de sécurité de {self._MAX_SAFETY_PAGES} pages atteinte")
                    break

                # Vérifie s'il y a une page suivante - cherche plusieurs patterns
                pagination = soup.find_all('a', class_='pagination_page')
                has_next = any(str(page + 1) in link.get_text() for link in pagination)
                if not has_next:
                    break

                page += 1
                time.sleep(0.5)  # Petite pause entre les pages

            print(f"✓ {len(threads_to_scrape)} thread(s) à scraper ({skipped_unchanged} déjà à jour ignoré(s))")
        except Exception as e:
            print(f"Erreur lors du scraping du forum: {e}")
            import traceback
            traceback.print_exc()

        return threads_to_scrape
    
    def download_cover(self, image_url):
        """Télécharge une couverture et retourne le chemin local"""
        if not image_url:
            return None


        try:
            # Génère un nom de fichier unique basé sur l'URL
            url_hash = hashlib.md5(image_url.encode()).hexdigest()
            ext = os.path.splitext(image_url)[1] or '.jpg'
            filename = f"{url_hash}{ext}"
            filepath = os.path.join('./data/covers', filename)
            
            # Télécharge seulement si pas déjà présent
            if not os.path.exists(filepath):
                print(f"    Téléchargement de la couverture...")
                from network_safety import safe_external_get
                response = safe_external_get(
                    image_url, session=self.session, timeout=10,
                    max_bytes=20 * 1024 * 1024,
                )
                if response.status_code == 200:
                    with open(filepath, 'wb') as f:
                        f.write(response.content)
                    print(f"    ✓ Couverture sauvegardée: {filename}")
                    return f"covers/{filename}"
                else:
                    print(f"    ✗ Échec du téléchargement: HTTP {response.status_code}")
                    return None
            else:
                print(f"    ✓ Couverture existe déjà: {filename}")
                return f"covers/{filename}"
        except Exception as e:
            print(f"    ✗ Erreur téléchargement couverture: {e}")
            return None
    
    def scrape_thread(self, thread_url, thread_title):
        """Scrappe la première page d'un thread pour extraire les liens ed2k"""
        ed2k_data = []
        try:
            # Assure qu'on est sur la première page (pas de paramètre &page=)
            if '&page=' in thread_url:
                thread_url = thread_url.split('&page=')[0]
            
            # Extrait le thread_id de l'URL
            thread_id = ""
            tid_match = re.search(r'tid=(\d+)', thread_url)
            if tid_match:
                thread_id = tid_match.group(1)
            
            response = self.session.get(thread_url, timeout=30)
            html = response.text
            soup = BeautifulSoup(response.content, 'html.parser')
            
            # Récupère la couverture
            cover_image = None
            couv_li = soup.find('li', class_='couv')
            if couv_li:
                img_tag = couv_li.find('img')
                if img_tag and img_tag.get('src'):
                    cover_url = img_tag['src']
                    print(f"  → Couverture trouvée: {cover_url[:60]}...")
                    cover_image = self.download_cover(cover_url)
            
            # Récupère la description
            description = None
            desc_p = soup.find('p', class_='indent')
            if desc_p:
                # Nettoie la description (enlève les balises <br />)
                description = desc_p.get_text(separator=' ', strip=True)
                print(f"  → Description trouvée ({len(description)} caractères)")
            
            links = self.extract_ed2k_links(html)
            for link in links:
                filename, filesize = self.parse_ed2k_link(link)

                # Extrait tome/intégrale/hors-série/épisode du nom de fichier
                volume_info = self.parse_volume_info(filename)

                ed2k_data.append({
                    'link': link,
                    'filename': filename,
                    'filesize': filesize,
                    'thread_title': thread_title,
                    'thread_url': thread_url,
                    'thread_id': thread_id,
                    'forum_category': self.forum_category,
                    'cover_image': cover_image,
                    'description': description,
                    **volume_info,
                })
            
            if links:
                volumes_found = [str(d['volume']) for d in ed2k_data if d['volume'] is not None]
                volumes_info = f" (volumes: {', '.join(volumes_found)})" if volumes_found else ""
                print(f"  → {len(links)} liens ed2k trouvés{volumes_info} dans: {thread_title[:50]}")
                
        except Exception as e:
            print(f"Erreur lors du scraping du thread: {e}")
        
        return ed2k_data
    
    def save_to_db(self, ed2k_data):
        """Sauvegarde les liens ed2k dans SQLite.

        Un run typique (hors tout premier import) re-scrape des threads déjà
        connus et ne trouve que quelques liens réellement nouveaux au milieu
        d'une grande majorité de doublons - avec une table qui dépasse
        maintenant les 60k lignes, faire un INSERT + except IntegrityError
        par lien (une exception Python levée pour chaque doublon) coûte
        sensiblement plus cher qu'un seul executemany avec INSERT OR IGNORE,
        qui laisse SQLite ignorer les doublons nativement en une seule
        transaction. Le compte avant/après remplace le comptage par exception
        (executemany ne renvoie pas un rowcount par ligne)."""
        connection = self.connect_db()
        if not connection:
            return

        from blueprints.search.routes import normalize_search_text

        cursor = connection.cursor()
        cursor.execute("SELECT COUNT(*) FROM ed2k_links")
        before = cursor.fetchone()[0]

        # Identifier les liens absents avant l insertion afin de traiter uniquement
        # les nouveautés de ce passage, sans relancer une recherche globale.
        candidate_links = list(dict.fromkeys(data["link"] for data in ed2k_data))
        existing_links = set()
        for offset in range(0, len(candidate_links), 900):
            chunk = candidate_links[offset:offset + 900]
            placeholders = ",".join("?" for _ in chunk)
            cursor.execute(f"SELECT link FROM ed2k_links WHERE link IN ({placeholders})", chunk)
            existing_links.update(row[0] for row in cursor.fetchall())
        self.new_links = [data for data in ed2k_data if data["link"] not in existing_links]

        rows = [
            (data['link'], data['filename'], data['filesize'], data['volume'],
             data['thread_title'], data['thread_url'], data['thread_id'],
             data['forum_category'], data['cover_image'], data['description'],
             int(data.get('is_integral', False)), data.get('integral_number'),
             int(data.get('is_hs', False)), data.get('hs_number'),
             int(data.get('is_episode', False)), data.get('episode_number'),
             normalize_search_text(data['thread_title']), normalize_search_text(data['filename']))
            for data in ed2k_data
        ]
        cursor.executemany("""
            INSERT OR IGNORE INTO ed2k_links (
                link, filename, filesize, volume, thread_title, thread_url, thread_id,
                forum_category, cover_image, description,
                is_integral, integral_number, is_hs, hs_number, is_episode, episode_number,
                thread_title_normalized, filename_normalized
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, rows)
        connection.commit()

        cursor.execute("SELECT COUNT(*) FROM ed2k_links")
        after = cursor.fetchone()[0]
        saved = after - before
        duplicates = len(ed2k_data) - saved

        cursor.close()
        connection.close()

        print(f"✓ {saved} nouveaux liens sauvegardés, {duplicates} doublons ignorés")
        return saved
    
    def run(self):
        """Lance le scraping complet - uniquement les threads nouveaux/mis à jour depuis
        le dernier scrape (voir get_thread_links), plus de "max_pages" à régler à la main.

        Retourne le nombre de liens ed2k réellement insérés ce passage (0 si aucun) -
        utilisé par le scheduler (voir _scrape_ebdz) pour ne déclencher la Surveillance
        des volumes manquants que quand ce scrape a effectivement rapporté du neuf, plutôt
        que sur un simple minuteur ("oui uniquement quand il y a de nouveaux fichiers")."""
        print("=== Démarrage du scraper myBB ===\n")

        # Connexion au forum
        print("Connexion au forum...")
        if not self.login():
            print("Impossible de continuer sans connexion.")
            self.new_links = []
            return 0

        # Crée la table
        self.create_table()

        print(f"\nScraping du forum: {self.base_url}")
        threads_to_scrape = self.get_thread_links(self.base_url)

        # Scrappe chaque thread
        print(f"\nScraping des threads...\n")
        all_ed2k_data = []

        for i, (thread_url, thread_title, thread_id, reply_count) in enumerate(threads_to_scrape, 1):
            print(f"[{i}/{len(threads_to_scrape)}] {thread_title[:60]}...")
            ed2k_data = self.scrape_thread(thread_url, thread_title)
            all_ed2k_data.extend(ed2k_data)
            # Marqué à jour même si ce passage n'a trouvé aucun lien ed2k (thread de
            # discussion sans release, ou lien retiré) - c'est le nombre de réponses qui
            # définit "à jour", pas le nombre de liens trouvés.
            self._save_thread_state(thread_id, reply_count)
            time.sleep(1)  # Politesse envers le serveur

        # Sauvegarde dans la base
        if all_ed2k_data:
            print(f"\n=== Sauvegarde de {len(all_ed2k_data)} liens ===")
            saved = self.save_to_db(all_ed2k_data) or 0
        else:
            print("\nAucun nouveau lien ed2k trouvé.")
            self.new_links = []
            saved = 0

        print("\n=== Scraping terminé ===")
        return saved


def ensure_ed2k_search_index(db_file="./data/ebdz.db"):
    """Peuple thread_title_normalized/filename_normalized + l'index FTS5 pour les lignes
    déjà présentes avant l'ajout de ces colonnes, puis crée les triggers de synchronisation
    - migration défensive appelée une seule fois au démarrage (voir app.py), pas depuis
    create_table() (appelé à chaque scrape, un scan de la table à chaque appel serait du
    travail répété pour rien une fois la migration terminée). Colonnes+table FTS (sans
    triggers) déjà créées par create_table() - appelé indirectement ici via une instance
    MyBBScraper jetable pour réutiliser ce code plutôt que dupliquer le schéma.

    Ordre volontaire - normaliser PUIS peupler la FTS PUIS créer les triggers (pas l'ordre
    naturel création-de-schéma-d'abord): les triggers ne doivent exister qu'une fois ce
    peuplement initial terminé, sinon l'UPDATE en masse ci-dessous déclencherait le futur
    trigger AU sur chacune de ses ~84k lignes, doublant son propre travail en écritures FTS
    supplémentaires DANS LA MÊME grosse transaction ("database disk image is malformed"
    observé une fois avec les triggers déjà en place - le fichier repassait "ok" à
    l'intégrity_check une fois réouvert, donc pas une corruption permanente, mais pas la
    peine de retenter cette charge d'écriture amplifiée)."""
    from blueprints.search.routes import normalize_search_text

    MyBBScraper("", db_file, "", "").create_table()

    conn = sqlite3.connect(db_file)
    conn.create_function('search_normalize', 1, normalize_search_text)
    cursor = conn.cursor()

    cursor.execute("SELECT COUNT(*) FROM ed2k_links WHERE thread_title_normalized IS NULL OR filename_normalized IS NULL")
    pending = cursor.fetchone()[0]
    if pending:
        print(f"⏳ Indexation recherche EBDZ: normalisation de {pending} lien(s)...")
        # Une seule UPDATE (SQLite appelle la fonction par ligne en interne) plutôt qu'un
        # aller-retour Python (fetchall de ~84k lignes + executemany) - plus simple, moins
        # de mémoire retenue côté Python, et surtout aucun trigger encore présent à ce
        # stade (voir docstring) donc aucune écriture FTS en cascade ici.
        cursor.execute("""
            UPDATE ed2k_links
            SET thread_title_normalized = search_normalize(thread_title),
                filename_normalized = search_normalize(filename)
            WHERE thread_title_normalized IS NULL OR filename_normalized IS NULL
        """)
        conn.commit()

    cursor.execute("SELECT COUNT(*) FROM ed2k_links_fts_idx")
    if cursor.fetchone()[0] == 0:
        cursor.execute("SELECT COUNT(*) FROM ed2k_links")
        total = cursor.fetchone()[0]
        if total:
            print(f"⏳ Indexation recherche EBDZ: peuplement FTS5 ({total} lien(s))...")
            # Commande officielle de (re)construction d'un index "external content" à
            # partir de sa table de contenu - plus fiable qu'un INSERT...SELECT manuel.
            cursor.execute("INSERT INTO ed2k_links_fts(ed2k_links_fts) VALUES('rebuild')")
            conn.commit()

    # Triggers de synchronisation pour les écritures FUTURES (un seul lien à la fois need
    # jamais la charge en masse ci-dessus) - créés en dernier, une fois l'historique déjà
    # indexé sans leur aide.
    cursor.execute("""
        CREATE TRIGGER IF NOT EXISTS ed2k_links_ai AFTER INSERT ON ed2k_links BEGIN
            INSERT INTO ed2k_links_fts(rowid, thread_title_normalized, filename_normalized)
            VALUES (new.id, new.thread_title_normalized, new.filename_normalized);
        END
    """)
    cursor.execute("""
        CREATE TRIGGER IF NOT EXISTS ed2k_links_ad AFTER DELETE ON ed2k_links BEGIN
            INSERT INTO ed2k_links_fts(ed2k_links_fts, rowid, thread_title_normalized, filename_normalized)
            VALUES('delete', old.id, old.thread_title_normalized, old.filename_normalized);
        END
    """)
    cursor.execute("""
        CREATE TRIGGER IF NOT EXISTS ed2k_links_au AFTER UPDATE ON ed2k_links BEGIN
            INSERT INTO ed2k_links_fts(ed2k_links_fts, rowid, thread_title_normalized, filename_normalized)
            VALUES('delete', old.id, old.thread_title_normalized, old.filename_normalized);
            INSERT INTO ed2k_links_fts(rowid, thread_title_normalized, filename_normalized)
            VALUES (new.id, new.thread_title_normalized, new.filename_normalized);
        END
    """)
    conn.commit()
    conn.close()
    print("✓ Index de recherche EBDZ prêt")


def load_config_from_json(config_path):
    """
    Charge la configuration depuis ebdz_config.json (fichier partagé avec l'appli web).
    Déchiffre le mot de passe avec Fernet si la clé existe, sinon utilise la valeur telle quelle.
    Retourne un dict : { 'username', 'password', 'forums': [...] }
    """
    import json

    if not os.path.exists(config_path):
        print(f"✗ Fichier de config introuvable : {config_path}")
        print("  → Lancez d'abord l'appli web et configurez les identifiants dans Settings > ebdz.net")
        return None

    with open(config_path, 'r') as f:
        config = json.load(f)

    # Déchiffrement du mot de passe (même logique que app.py)
    encrypted_password = config.get('password', '')
    key_file = os.path.join(os.path.dirname(config_path), '.emule_key')

    if encrypted_password and os.path.exists(key_file):
        try:
            from cryptography.fernet import Fernet
            with open(key_file, 'rb') as kf:
                key = kf.read()
            config['password'] = Fernet(key).decrypt(encrypted_password.encode()).decode()
        except Exception as e:
            print(f"⚠️  Impossible de déchiffrer le mot de passe : {e}")
            print("    → Le mot de passe sera utilisé tel quel (peut être incorrect)")
    
    return config


if __name__ == "__main__":
    DB_FILE = "./data/ebdz.db"
    CONFIG_PATH = "./data/ebdz_config.json"

    os.makedirs('./data', exist_ok=True)

    # ─── Chargement de la config depuis le fichier partagé ───
    config = load_config_from_json(CONFIG_PATH)

    if not config:
        exit(1)

    USERNAME = config.get('username', '').strip()
    PASSWORD = config.get('password', '').strip()
    forums_raw = config.get('forums', [])

    if not USERNAME or not PASSWORD:
        print("✗ Identifiants manquants dans la config.")
        print("  → Configurez-les dans l'appli web : Settings > ebdz.net")
        exit(1)

    if not forums_raw:
        print("✗ Aucun forum configuré.")
        print("  → Ajoutez des forums dans l'appli web : Settings > ebdz.net")
        exit(1)

    # Reconstruction de FORUMS_TO_SCRAPE depuis les fid
    FORUMS_TO_SCRAPE = []
    for f in forums_raw:
        fid = f.get('fid')
        if fid is None:
            continue
        FORUMS_TO_SCRAPE.append({
            'url': f"https://ebdz.net/forum/forumdisplay.php?fid={fid}",
            'category': f.get('category', f'Forum {fid}'),
        })

    # ─── Lancement ───
    print("\n" + "=" * 60)
    print("🚀 SCRAPER ED2K - EmuleBDZ")
    print(f"   Config depuis : {CONFIG_PATH}")
    print(f"   Utilisateur   : {USERNAME}")
    print(f"   Forums        : {len(FORUMS_TO_SCRAPE)}")
    print("=" * 60)

    for forum_config in FORUMS_TO_SCRAPE:
        print(f"\n📂 Catégorie : {forum_config['category']}")
        print(f"🔗 URL : {forum_config['url']}")

        scraper = MyBBScraper(
            forum_config['url'],
            DB_FILE,
            USERNAME,
            PASSWORD,
            forum_config['category']
        )

        scraper.run()

        print("\n" + "-" * 60)

    print("\n✅ Scraping terminé pour toutes les catégories !")
    print("=" * 60)
    
