# 📚 Bullarr

> **Gestionnaire self-hosted de bibliothèque BD : métadonnées Bédéthèque, intégration Komga, recherche multi-sources et acquisition automatique façon Sonarr/Radarr**

Bullarr est une application web Flask (interface en français) qui gère une bibliothèque
personnelle de bandes dessinées sur disque : elle scanne vos dossiers, associe
chaque série à sa fiche [Bédéthèque](https://www.bedetheque.com/) (référence unique pour
les métadonnées), écrit le `ComicInfo.xml` dans chaque archive, tient à jour un serveur
[Komga](https://komga.org/) en aval, et peut rechercher/télécharger automatiquement les
tomes manquants.

## ✨ Fonctionnalités

- 📖 **Multi-bibliothèques** : plusieurs dossiers racine, chacun avec ses propres séries/tomes
- 🏷️ **Bédéthèque comme référence métadonnées** : recherche/association automatique (score
  de similarité, gestion des intégrales/hors-séries/one-shots), écriture du `ComicInfo.xml`
  dans les archives, badge de complétude par série
- 📥 **Import automatique** : surveillance de dossiers (aMule, torrents, Telegram, sources web),
  détection de série/tome, conversion cbr/rar/pdf/zip → cbz, remplacement par format
  préféré (cbz > cbr/rar > pdf), choix déplacement ou hardlink, historique + annulation
- 🔍 **Recherche multi-sources** : forum [EBDZ.net](https://ebdz.net) (liens ed2k),
  [Prowlarr](https://prowlarr.com/), canaux Telegram dédiés et sources web :
  [fourtoutici.cc](https://fourtoutici.cc), Anna's Archive (via Shelfmark) et le forum VK
  [Au Phil des Bulles](https://vk.com/board203785966) (PDF, sans compte VK)
- 🆕 **Nouveautés** : derniers fichiers EBDZ et Telegram, et flux RSS (DupeFR, Prowlarr,
  forum VK), avec la série reconnue, les tomes déjà possédés et un téléchargement en un clic
- 📊 **Surveillance des tomes manquants** (façon Sonarr) : détection des tomes non
  possédés par série, recherche + téléchargement automatique sur les sources
  configurées, acquisition immédiate à l'ajout d'une nouvelle série
- 📤 **Clients de téléchargement** : aMule/eMule, qBittorrent, rTorrent, Deluge, Shelfmark ;
  Telegram, fourtoutici et le forum VK sont téléchargés directement par Bullarr (reprise du
  fichier partiel pour Telegram et VK). Priorité « en premier » et pause depuis la page Import
  pour Telegram, qBittorrent et aMule
- 🗄️ **Intégration Komga** : rescan automatique déclenché à chaque import/renommage/
  écriture de métadonnées
- ✏️ **Renommage configurable** : templates de nommage fichiers/dossiers à partir des
  métadonnées Bédéthèque
- ✅ **Vérification de bibliothèque** : détection des séries sans métadonnées, noms non
  standard, et test d'intégrité réel des archives (zip/rar/pdf), avec actions de
  correction en masse
- 🕓 **Historique** : imports et actions (renommage/suppression/déplacement) journalisés
- 🔐 **Sécurité** : secrets (mots de passe/clés API) chiffrés sur disque (Fernet),
  authentification optionnelle (désactivée par défaut) : identifiant/mot de passe, SSO/OIDC,
  ou les deux sur la même page de connexion
- 🐳 **100 % Docker**, base SQLite (aucune base externe à gérer)
- 🔌 **API** : toutes les fonctions de l'interface pilotables par une application externe
  avec une clé API — voir [📖 la documentation de l'API](docs/API.md)

---

## 🚀 Démarrage rapide (Docker Compose)

```bash
# 1. Cloner le projet
git clone https://github.com/gwenvador/bullarr.git
cd bullarr

# 2. Configurer l'environnement
cp .env.example .env
# Éditer .env : SECRET_KEY

# 3. Créer votre docker-compose.yml à partir du modèle (non versionné, chemins propres
#    à votre machine) et l'adapter : chemins de vos bibliothèques et de vos dossiers
#    d'import (aMule, torrents, Telegram, fourtoutici, forum VK)
cp docker-compose.example.yml docker-compose.yml
# Éditer docker-compose.yml

# 4. Démarrer
docker compose up -d

# 5. Accéder à l'application (port hôte défini dans docker-compose.yml)
# http://localhost:4000
```

#### Commandes essentielles

```bash
docker compose up -d           # démarrer l'image GHCR (ghcr.io/gwenvador/bullarr)
docker compose logs -f bullarr # voir les logs
docker compose down            # arrêter
```

### Installation locale (sans Docker)

```bash
# 1. Créer un environnement virtuel Python
python3 -m venv venv
source venv/bin/activate

# 2. Installer les dépendances Python
pip install -r requirements.txt

# 3. Dépendances système (Debian/Ubuntu ; unrar est dans la section non-free)
sudo apt install amule-utils unrar p7zip-full

# 4. Démarrer l'application
python app.py   # mode production

# Application accessible à http://localhost:5000
```

---

### Lancer les tests

```bash
pip3 install -r requirements.txt -r requirements-dev.txt
python3 -m pytest tests
```

Utilisez `pytest` : une partie des tests sont de simples fonctions `test_…` que
`python3 -m unittest` ne découvre pas.

## 📋 Prérequis

### Docker (recommandé)
- Docker >= 20.10
- Docker Compose v2 (commande `docker compose`)

### Installation locale
- Python 3 récent (l'image Docker utilise Python 3.13)
- `amule-utils` (intégration aMule)
- `unrar` (décompression des archives RAR)
- Accès (optionnel) à un serveur aMule, Prowlarr, qBittorrent, rTorrent, Deluge ou Komga

---

## 🔧 Configuration

### Variables d'environnement (.env)

Générez une clé aléatoire avant le premier démarrage :

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

Copiez le résultat dans `.env` et ne le publiez jamais :

```bash
SECRET_KEY=<clé-aléatoire-générée>  # obligatoire, conserver la même valeur
BULLARR_AUTH_BYPASS_LOGIN=false     # dépannage uniquement, ne pas laisser activé
```

`SECRET_KEY` sert à signer les sessions Flask et certaines données protégées. Une nouvelle
valeur invalide les sessions existantes ; ne la changez donc pas après le démarrage sauf
si vous acceptez de forcer les utilisateurs à se reconnecter.

`BULLARR_AUTH_BYPASS_LOGIN=true` désactive temporairement la garde d'authentification pour récupérer l'accès après une mauvaise configuration OIDC. Redémarrez Bullarr, corrigez le réglage depuis **Configuration → Login**, remettez la variable à `false`, puis redémarrez à nouveau. Ne laissez pas cette variable activée en fonctionnement normal.

La configuration aMule/eMule (ainsi que les indexeurs, clients de téléchargement, Komga,
Telegram, SSO...) se fait depuis l'application, page **Configuration**. Chaque intégration
est optionnelle et désactivée tant qu'elle n'est pas explicitement activée.

### Onglets de Configuration

| Onglet | Rôle |
|---|---|
| 🔌 Indexeurs | Sources de recherche et de Nouveautés : Prowlarr, EBDZ.net, Telegram (canaux), Komga, Flux RSS, Web Archive (fourtoutici.cc, Anna's Archive, forum VK) |
| 💻 Clients | Clients de téléchargement : aMule/eMule, qBittorrent, rTorrent, Deluge, Shelfmark |
| 📱 Notifications | Notifications Telegram (bot) |
| 🔐 Login | Authentification : mot de passe, OIDC ou les deux (désactivée par défaut). La page de connexion affiche le formulaire et/ou un bouton SSO ; la redirection automatique vers le SSO est une option, désactivée par défaut. Carte **Accès API** : clé pour les applications externes |
| 📊 Surveillance | Détection et acquisition automatique des tomes manquants |
| 📥 Imports | Activation et règles de l'import automatique de fichiers |
| 🗂️ Recherche | Priorité des formats dans les résultats de recherche et ordre des sources |
| ✏️ Format | Templates de renommage fichiers/dossiers |
| 💾 Backup | Export/import d'une sauvegarde (bases + configs + clé de chiffrement) |

### Configuration aMule/eMule

1. **Configuration** → `Clients` → `aMule / eMule`
2. Activer, choisir le type (aMule/Linux ou eMule/Windows), renseigner host/port EC
   (défaut `4712`) et mot de passe EC si configuré côté serveur
3. Côté aMule : Préférences → Connexion EC → activer le serveur EC, accepter les
   connexions externes
4. Tester la connexion depuis l'onglet

### Configuration EBDZ.net

1. **Configuration** → `Indexeurs` → `EBDZ.net`
2. Renseigner identifiant/mot de passe du forum et les sous-forums à scraper
3. Le scraping automatique (planifié) indexe les liens ed2k dans `data/ebdz.db`,
   indépendamment de la base principale

### Flux RSS

**Configuration** → `Indexeurs` → `Flux RSS` : les flux affichés dans **Nouveautés** et lus par
la surveillance des tomes manquants (un tome manquant d'une série surveillée qui apparaît dans
un flux est téléchargé automatiquement). Trois types de flux :

- un flux RSS classique, par son URL ;
- **Prowlarr** : les derniers ajouts des indexeurs Prowlarr choisis ;
- **Forum VK** (bouton « + Forum VK ») : les fichiers postés récemment sur le forum VK
  Au Phil des Bulles, téléchargés directement par Bullarr.

### Canaux Telegram

Bullarr lit des canaux Telegram publics de BD (par exemple `@bd_frBE`) avec votre compte
Telegram personnel : un bot ne peut pas lire l'historique d'un canal dont il n'est pas
administrateur.

- **Connexion** : **Configuration** → `Indexeurs` → `Telegram (canaux)`. Renseignez `api_id`
  et `api_hash` (gratuits sur [my.telegram.org](https://my.telegram.org), section « API
  development tools »), votre numéro au format international (`+33…`), puis le code reçu
  dans Telegram et, si la vérification en deux étapes est active, son mot de passe. La
  session est chiffrée sur disque.
- **Canaux** : ajoutez chaque canal par son nom d'utilisateur, sans `@`. « Scraper
  maintenant » lit les nouveaux messages ; le **scraping automatique** les relit à
  intervalle régulier (en heures ou en jours).
- **Recherche** : « Indexer tout l'historique » enregistre les fichiers de tout l'historique
  des canaux dans une base locale (`data/telegram_messages.db`). La recherche se fait dans
  cette base : instantanée, sans requête vers Telegram. Les résultats apparaissent dans
  **Recherche**, la fiche série et l'acquisition automatique.
- **Nouveautés** : les derniers fichiers des canaux, avec la série reconnue et les tomes
  déjà possédés ; la surveillance télécharge d'elle-même les tomes manquants des séries
  surveillées.
- **Téléchargement** : par votre compte, un fichier à la fois, dans le dossier monté sur
  `/downloads/telegram`, puis import automatique. Depuis la page **Import** : « en premier »
  (le transfert en cours s'interrompt et reprendra ensuite), pause et reprise. Une coupure,
  un redémarrage ou une attente imposée par Telegram reprend depuis le fichier partiel.
- Les **notifications** Telegram (bot) sont un réglage distinct : onglet `Notifications`.

### Forum VK « Au Phil des Bulles »

Le forum [vk.com/board203785966](https://vk.com/board203785966) partage des BD et mangas en
français, surtout en PDF. Bullarr le lit sans compte VK :

- **Recherche** : **Configuration** → `Indexeurs` → `Web Archive` → carte **Forum VK** (case
  d'activation, adresse du site, test). Bullarr construit un index des séries à partir des
  archives du forum (reconstruit une fois par jour, environ 30 s à la première recherche),
  puis lit les fichiers du sujet de la série cherchée. Les résultats apparaissent avec la
  recherche web de **Recherche** et de la fiche série, et dans l'acquisition automatique
  (à placer dans l'ordre des sources de l'onglet `Recherche`).
- **Nouveautés** : ajoutez le flux « Forum VK » dans `Flux RSS`.
- **Téléchargement** : direct, dans le dossier monté sur `/downloads/vk`, avec reprise du
  fichier partiel, puis import automatique. Certains anciens fichiers ne sont plus servis par
  VK (erreur « Fichier indisponible sur VK »).

### Configuration Prowlarr, Komga, Web (Indexeurs) / qBittorrent, rTorrent, Deluge (Clients)

Chaque source a sa propre carte cliquable dans `Indexeurs` (recherche) ou `Clients`
(téléchargement) : URL, identifiants/clé API, puis "Tester la connexion". qBittorrent/
rTorrent/Deluge servent de cibles d'envoi pour les résultats de recherche et pour
l'acquisition automatique des tomes manquants. Pour Komga, une fois configuré, un rescan
est déclenché automatiquement après chaque import/renommage/écriture de métadonnées —
inutile de forcer un scan manuel côté Komga.

### 📥 Import automatique de fichiers

1. **Configuration** → `Imports` : activer l'import automatique, autoriser ou non
   l'auto-assignation à une série existante, activer la conversion automatique en cbz
   (cbr/rar/pdf/zip → cbz), et choisir le mode de transfert (déplacement ou hardlink)
2. Une fois activé, les dossiers surveillés (aMule, torrents, Telegram, fourtoutici,
   forum VK, Shelfmark — voir les montages `/downloads/...` de `docker-compose.yml`)
   sont scannés en continu : titre de série et numéro de tome extraits du nom de fichier,
   conversion vers cbz si activée
3. En mode **Déplacer**, le fichier source est supprimé après l'import. En mode
   **Hardlink**, le fichier source est conservé et le fichier de bibliothèque partage
   les mêmes données physiques : l'espace disque n'est pas doublé et supprimer l'un des
   deux liens ne supprime pas les données tant que l'autre existe.
4. Le hardlink ne fonctionne que si la source et la bibliothèque sont sur le même
   filesystem (notamment le même export NFS). Avec des montages NFS différents, ou entre
   un filesystem local et un NFS, Bullarr utilise automatiquement une copie en conservant
   la source. Les fichiers convertis en CBZ sont toujours copiés.

### 🔌 API

Bullarr se pilote aussi par une API HTTP/JSON (application mobile, scripts, domotique) :
générez une clé dans **Configuration** → `Login` → **Accès API**, puis envoyez-la dans
l'en-tête `X-Api-Key`.

- 📖 [Documentation de l'API](docs/API.md) : authentification, conventions, téléchargements,
  import, bibliothèque, recherche, Nouveautés, exemples `curl` et Python
- `/api/docs` sur votre instance : liste à jour de toutes les routes (`?format=json` pour
  la version JSON)

### 📊 Surveillance des tomes manquants

**Configuration** → `Surveillance` : active la détection périodique des tomes manquants
par série (comparaison avec Bédéthèque) et, si souhaité, la recherche + téléchargement
automatique sur les sources activées. L'ajout d'une nouvelle série depuis Bédéthèque peut
aussi déclencher une passe d'acquisition immédiate pour tous ses tomes manquants.

---

### Technologies utilisées

- **Back-end** : Flask 3.1.3, gunicorn 23, APScheduler 3.10.4 (tâches planifiées), Authlib 1.6.12 (OIDC)
- **Base de données** : SQLite3 (WAL)
- **Front-end** : HTML5, CSS3, JavaScript vanilla (pas de framework/build step)
- **Scraping** : requests 2.33.0, BeautifulSoup 4.12.2, defusedxml 0.7.1
- **Images/archives** : Pillow 12.3.0, PyMuPDF 1.26.7, rarfile 4.1, pypdf 6.19.0
- **Telegram** : Telethon 1.36.0
- **Chiffrement** : cryptography 50.0.0 (Fernet)
- **Conteneurisation** : Docker

## 📊 Formats de fichiers supportés

- ✅ `.cbz`, `.cbr`, `.zip`, `.rar`, `.pdf`

Format préféré à l'import en cas de doublon : `.cbz` > `.cbr`/`.rar` > `.pdf` (la taille
ne départage qu'entre deux fichiers du même format).

### Format de nommage

Le nom de série et le numéro de tome sont extraits automatiquement du nom de fichier
(`LibraryScanner.parse_filename`, seul et unique analyseur de nom dans tout le projet).
Exemples reconnus :

```
"Astérix Vol 01.cbz"
"Les Légendaires - Volume 15.zip"
"Blacksad T05.rar"
```

Le renommage final (fichiers et dossiers) suit ensuite les templates configurables de
l'onglet `Format`, générés à partir des métadonnées Bédéthèque plutôt que du nom
d'origine.
