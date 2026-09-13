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
- 📥 **Import automatique** : surveillance de dossiers (aMule, torrents, Telegram),
  détection de série/tome, conversion cbr/rar/pdf/zip → cbz, remplacement par format
  préféré (cbz > cbr/rar > pdf), choix déplacement ou hardlink, historique + annulation
- 🔍 **Recherche multi-sources** : forum [EBDZ.net](https://ebdz.net) (liens ed2k),
  [Prowlarr](https://prowlarr.com/), [fourtoutici.cc](https://fourtoutici.cc) et des
  canaux Telegram dédiés — page "Nouveautés" avec suivi des sujets/fichiers déjà scrapés
- ☑️ **Téléchargement groupé** : sélection de plusieurs résultats dans une recherche,
  sélection globale limitée aux lignes visibles, conservation des sélections pendant les
  rafraîchissements asynchrones, et envoi groupé vers les clients configurés (Prowlarr,
  fourtoutici, Telegram, EBDZ/eMule ou Shelfmark)
- 📊 **Surveillance des tomes manquants** (façon Sonarr) : détection des tomes non
  possédés par série, recherche + téléchargement automatique sur les sources
  configurées, acquisition immédiate à l'ajout d'une nouvelle série
- 📤 **Clients de téléchargement** : aMule/eMule, qBittorrent, rTorrent, Deluge
- 🗄️ **Intégration Komga** : rescan automatique déclenché à chaque import/renommage/
  écriture de métadonnées
- ✏️ **Renommage configurable** : templates de nommage fichiers/dossiers à partir des
  métadonnées Bédéthèque
- ✅ **Vérification de bibliothèque** : détection des séries sans métadonnées, noms non
  standard, et test d'intégrité réel des archives (zip/rar/pdf), avec actions de
  correction en masse
- 🕓 **Historique** : imports et actions (renommage/suppression/déplacement) journalisés
- 🔐 **Sécurité** : secrets (mots de passe/clés API) chiffrés sur disque (Fernet), SSO/OIDC
  optionnel (désactivé par défaut)
- 🌓 **Mode clair/sombre**
- 🐳 **100 % Docker**, base SQLite (aucune base externe à gérer)

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
#    d'import (aMule/torrents/Telegram)
cp docker-compose.example.yml docker-compose.yml
# Éditer docker-compose.yml

# 4. Démarrer
docker compose up -d

# 5. Accéder à l'application (port hôte défini dans docker-compose.yml)
# http://localhost:4000
```

#### Commandes essentielles

```bash
docker compose up -d           # démarrer l'image GHCR
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

# 3. Dépendances système (Debian/Ubuntu)
sudo apt install amule-utils unrar

# 4. Démarrer l'application
python app.py   # FLASK_ENV=development par défaut

# Application accessible à http://localhost:5000
```

---

## 📋 Prérequis

### Docker (recommandé)
- Docker >= 20.10
- Docker Compose >= 1.29

### Installation locale
- Python >= 3.9
- `amule-utils` (intégration aMule)
- `unrar` (décompression des archives RAR)
- Accès (optionnel) à un serveur aMule, Prowlarr, qBittorrent, rTorrent, Deluge ou Komga

---

## 🔧 Configuration

### Variables d'environnement (.env)

```bash
SECRET_KEY=your-secure-secret-key-here   # clé secrète Flask - à changer en production
FLASK_ENV=production
BULLARR_AUTH_BYPASS_LOGIN=false  # dépannage uniquement : contourne temporairement le login
```

`BULLARR_AUTH_BYPASS_LOGIN=true` désactive temporairement la garde d'authentification pour récupérer l'accès après une mauvaise configuration OIDC. Redémarrez Bullarr, corrigez le réglage depuis **Configuration → Login**, remettez la variable à `false`, puis redémarrez à nouveau. Ne laissez pas cette variable activée en fonctionnement normal.

La configuration aMule/eMule (ainsi que les indexeurs, clients de téléchargement, Komga,
Telegram, SSO...) se fait depuis l'application, page **Configuration**. Chaque intégration
est optionnelle et désactivée tant qu'elle n'est pas explicitement activée.

### Onglets de Configuration

| Onglet | Rôle |
|---|---|
| 🔌 Indexeurs | Sources de recherche des tomes manquants : Prowlarr, EBDZ.net, Telegram (canaux), Komga, sources web (fourtoutici.cc) |
| 💻 Clients | Clients de téléchargement : aMule/eMule, qBittorrent, rTorrent, Deluge |
| 📱 Notifications | Notifications Telegram (bot) |
| 🔐 SSO / OIDC | Authentification externe (désactivée par défaut) |
| 📊 Surveillance | Détection et acquisition automatique des tomes manquants |
| 📥 Imports | Activation et règles de l'import automatique de fichiers |
| 🌓 Thème | Clair / sombre |
| 🗂️ Recherche | Priorité des formats dans les résultats de recherche |
| ✏️ Format | Templates de renommage fichiers/dossiers |
| 💾 Backup | Export/import d'une sauvegarde (bases + configs + clé de chiffrement) |

### Configuration aMule/eMule

1. **Configuration** → `Indexeurs` → `aMule / eMule`
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
2. Une fois activé, les dossiers surveillés (aMule, torrents, Telegram — voir les
   montages `/downloads/...` de `docker-compose.yml`) sont scannés en continu : titre de
   série et numéro de tome extraits du nom de fichier, conversion vers cbz si activée
3. En mode **Déplacer**, le fichier source est supprimé après l'import. En mode
   **Hardlink**, le fichier source est conservé et le fichier de bibliothèque partage
   les mêmes données physiques : l'espace disque n'est pas doublé et supprimer l'un des
   deux liens ne supprime pas les données tant que l'autre existe.
4. Le hardlink ne fonctionne que si la source et la bibliothèque sont sur le même
   filesystem (notamment le même export NFS). Avec des montages NFS différents, ou entre
   un filesystem local et un NFS, Bullarr utilise automatiquement une copie en conservant
   la source. Les fichiers convertis en CBZ sont toujours copiés.

### 📊 Surveillance des tomes manquants

**Configuration** → `Surveillance` : active la détection périodique des tomes manquants
par série (comparaison avec Bédéthèque) et, si souhaité, la recherche + téléchargement
automatique sur les sources activées. L'ajout d'une nouvelle série depuis Bédéthèque peut
aussi déclencher une passe d'acquisition immédiate pour tous ses tomes manquants.

---

## 🆕 Nouveautés de la version v1.2.0

- Ajout de cases à cocher devant les résultats de recherche de tome
- Ajout de **Tout sélectionner**, limité aux résultats visibles après filtrage
- Ajout du bouton **Télécharger la sélection** pour lancer plusieurs téléchargements
- Conservation des sélections quand les résultats, la possession ou la disponibilité des
  sources sont rafraîchis
- Prise en charge du téléchargement groupé pour Prowlarr, fourtoutici, Telegram,
  EBDZ/eMule et Shelfmark

---

### Technologies utilisées

- **Back-end** : Flask 3.1.2, APScheduler 3.10.4 (tâches planifiées), Authlib 1.3.2 (OIDC)
- **Base de données** : SQLite3 (WAL)
- **Front-end** : HTML5, CSS3, JavaScript vanilla (pas de framework/build step)
- **Scraping** : BeautifulSoup 4.12.2, defusedxml 0.7.1
- **Images/archives** : Pillow 12.3.0, PyMuPDF 1.26.7, rarfile 4.1, pypdf 6.17.0
- **Telegram** : Telethon 1.36.0
- **Chiffrement** : cryptography 41.0.4 (Fernet)
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
