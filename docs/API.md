# API Bullarr

Toutes les routes de l'interface web sous `/api/` sont utilisables par une application externe (script, application mobile, domotique) : état des téléchargements, contrôle de l'import, bibliothèque, recherche, Nouveautés, réglages.

- **Liste complète des routes :** `/api/docs` dans un navigateur. La liste se lit dans la table de routage, elle est donc toujours à jour.
- **La même liste en JSON :** `/api/docs?format=json`, avec pour chaque route `path`, `methods`, `group` et `summary`.
- **Ce document :** l'authentification, les conventions et les opérations les plus utiles, avec des exemples.

> Ce sont les routes utilisées par l'interface web : elles peuvent évoluer d'une version à l'autre. Une application externe doit tolérer les champs inconnus et vérifier `success`.

## Authentification

1. Dans Bullarr, ouvrez **Paramètres → Login → Accès API** et cliquez sur **Générer une clé**.
2. Envoyez la clé à chaque requête, au choix :
   - en-tête `X-Api-Key: <clé>` (recommandé) ;
   - en-tête `Authorization: Bearer <clé>` ;
   - paramètre `?apikey=<clé>`, pour les clients qui ne savent pas ajouter d'en-tête. La clé apparaît alors dans les journaux du proxy.

```bash
curl -H "X-Api-Key: $BULLARR_KEY" https://bullarr.example.com/api/activity/status
```

Règles :

- **Portée de la clé :** elle ouvre les routes `/api/` uniquement, jamais les pages HTML.
- **Pas de jeton CSRF :** avec la clé, les requêtes `POST`, `PUT` et `DELETE` n'en ont pas besoin.
- **Clé fausse :** réponse `401` `{"success": false, "error": "Clé API invalide"}`, même si l'authentification est désactivée.
- **Sans clé :** si le mode Login est actif, un client qui envoie `Accept: application/json` reçoit `401`, et les autres sont redirigés vers `/login`.
- **Mode « Sans authentification » :** les routes répondent aussi sans clé. Mettez Bullarr derrière un mode Login si l'API est exposée.
- **Gestion de la clé :** la clé ne peut pas se lire, se régénérer ni se supprimer elle-même. Ces actions (`GET`, `POST` et `DELETE` `/api/auth/api-key`) demandent une session ouverte dans le navigateur.
- **Stockage :** la clé est chiffrée sur le disque, dans `data/oidc_config.json`. La régénérer coupe immédiatement l'ancienne.

## Conventions

- **Corps de requête :** JSON, avec l'en-tête `Content-Type: application/json`.
- **Réponses :** JSON. La plupart contiennent `success` (booléen). Quelques routes de lecture renvoient directement une liste (`/api/libraries`, `/api/library/<id>/series`, `/api/series/<id>/volumes`).
- **Erreurs :** code HTTP `4xx` ou `5xx`, et le corps `{"success": false, "error": "message lisible"}`. Le message est en français et détaillé, il peut être affiché tel quel.
- **Identifiants :** ce sont des entiers. Une **série** appartient à une **bibliothèque** et contient des **tomes** (*volumes*). Un téléchargement suivi a un **identifiant de suivi** (`tracking_id`), qui est le champ `id` des lignes `pending` de `/api/activity/status`.
- **Actions longues :** une recherche automatique, un scan ou un import démarre en arrière-plan et répond tout de suite. L'avancement se lit ensuite sur une route d'état.

## Téléchargements

### État de tous les téléchargements

`GET /api/activity/status`

```json
{
  "success": true,
  "clients": [{"client": "qbittorrent", "label": "qBittorrent", "error": null, "items": [ … ]}],
  "pending": [{"id": 6602, "client": "telegram", "title": "Alix - T12.cbz", "series_id": 42,
               "volume_number": 12, "bytes_downloaded": 1048576, "bytes_total": 52428800,
               "priority": 0, "paused": 0, "status": "pending"}],
  "completed_pending_ids": []
}
```

- **`clients` :** l'état brut de chaque client configuré (qBittorrent, rTorrent, Deluge, aMule).
- **`pending` :** les téléchargements suivis par Bullarr, quelle que soit la source.

### Mettre en premier, mettre en pause, reprendre

`POST /api/activity/download-control`

```json
{"tracking_id": 6602, "action": "top"}
```

- **`action` :** `top` (télécharger en premier), `pause` ou `resume`.
- **Téléchargement non suivi :** à la place de `tracking_id`, envoyez `client` (`qbittorrent`, `amule` ou `telegram`) et `id`, l'identifiant du client (le hash du torrent ou du fichier aMule).
- **Telegram :** `top` interrompt le transfert moins prioritaire, qui reprend plus tard depuis son fichier `.part`.

### Retirer un téléchargement suivi

`POST /api/activity/pending/remove` avec `{"id": 6602}`.

### Corriger la série ou le tome d'un téléchargement

`POST /api/activity/update-tracking` avec `{"tracking_id": 6602, "series_id": 42, "volume_number": 12}`. Pour une intégrale, un hors-série ou un épisode, utilisez à la place les paires `is_integral`/`integral_number`, `is_hs`/`hs_number` ou `is_episode`/`episode_number`.

## Import

| Route | Rôle |
|---|---|
| `GET /api/import/progress` | Léger, à interroger souvent. Renvoie l'import en cours (`currently_processing` : fichier, opération, durée), `importing_filepaths` et `import_paused`. |
| `GET /api/import/pending-count` | Fichiers en attente d'import. |
| `GET /api/import/state` | État complet de la page Import. Plus lourd. |
| `POST /api/import/scan` | Rescanne les répertoires d'import. |
| `POST /api/import/stop` | Arrête l'import après le fichier en cours et met l'import automatique en pause. |
| `POST /api/import/resume` | Reprend l'import automatique. |
| `GET /api/import/history` | Historique des imports. |
| `POST /api/import/history/<operation_id>/undo` | Annule un import. |

## Bibliothèque

| Route | Rôle |
|---|---|
| `GET /api/libraries` | Bibliothèques, avec `series_count` et `volumes_count`. |
| `GET /api/library/<library_id>/series` | Séries d'une bibliothèque (liste). |
| `GET /api/library/<library_id>/stats` | Statistiques. |
| `GET /api/series/<series_id>` | Détail d'une série, avec les données Bédéthèque. |
| `GET /api/series/<series_id>/volumes` | Tomes : fichier, format, numéro, métadonnées. |
| `POST /api/scan/<library_id>?force=1` | Scan de la bibliothèque. Répond `202` avec `job_id`, ou `409` si un scan tourne déjà. |
| `GET /api/scan/jobs/<job_id>` | Avancement du scan. |
| `POST /api/scan/series/<series_id>` | Scan d'une seule série. |
| `GET /api/volumes/<volume_id>/download` | Télécharge le fichier d'un tome. |
| `GET /api/series/<series_id>/download` | Archive ZIP de toute la série. |

### Ajouter une série

1. `GET /api/bedetheque/search?q=Blacksad` cherche la série dans Bédéthèque.
2. `POST /api/bedetheque/add-series` avec `{"url": "<url Bédéthèque>", "library_id": 2}` l'ajoute.

> Si « Recherche automatique à l'ajout » est activée dans les réglages, l'ajout lance aussi la recherche et le téléchargement des tomes manquants. Ajoutez `"skip_auto_acquire": true` pour l'éviter.

## Recherche de fichiers

### Chercher un tome sur les sources

`POST /api/missing-monitor/search`

```json
{"title": "Blake et Mortimer", "volume_num": 4, "series_id": 715,
 "sources": ["prowlarr", "ebdz", "telegram", "fourtoutici", "annas_archive", "vk"]}
```

- **Sources :** sans `sources`, toutes les sources actives sont interrogées.
- **Intégrale, hors-série ou épisode :** ajoutez `is_integral`, `is_hs` ou `is_episode` à `true`. `volume_num` porte alors le numéro de l'intégrale, du hors-série ou de l'épisode.
- **Réponse :** `{"success": true, "results": [...]}`.

Chaque résultat contient :

| Champ | Contenu |
|---|---|
| `source` | La source du résultat. |
| `filename` ou `title` | Le nom du fichier. |
| `size` | La taille. |
| `volume` | Le numéro de tome lu dans le nom. |
| `unconfirmed_volume` et `unconfirmed_reason` | Si le nom ne confirme pas le tome demandé, et pourquoi. |

Selon la source, le résultat contient aussi :

| Source | Champs |
|---|---|
| Prowlarr | `download_url`, `info_url`, `seeders` |
| EBDZ | `link` (ed2k), `thread_url` |
| Telegram | `channel`, `message_id`, `channel_title` |
| fourtoutici | `file_id`, `download_url` |
| VK | `download_url`, `thread_url` |

### Télécharger un résultat

Envoyez le résultat au client correspondant. Les champs communs sont les suivants :

| Champ | Contenu |
|---|---|
| `title` ou `filename` | Le nom du fichier. |
| `series_id`, `volume_id`, `volume_number` | Rattachent le téléchargement à la série et au tome. |
| `force_replace` | `true` remplace un tome déjà possédé. |
| `source_link` | La page de la release, affichée dans l'Historique. |

| Source | Route | Champs propres |
|---|---|---|
| Prowlarr (torrent) | `POST /api/qbittorrent/add`, `/api/rtorrent/add` ou `/api/deluge/add` | `torrent_url`, `source: "prowlarr"`, `category` (qBittorrent, optionnel) |
| EBDZ (ed2k) | `POST /api/emule/add` | `link`, `source: "ebdz"` |
| Telegram | `POST /api/telegram-channels/download` | `channel`, `message_id`, `channel_title`, `filename` |
| fourtoutici | `POST /api/fourtoutici/download` | `file_id`, `filename` |
| Forum VK | `POST /api/vk/download` | `url` (`download_url` du résultat), `filename` |

Exemple :

```bash
curl -X POST -H "X-Api-Key: $BULLARR_KEY" -H "Content-Type: application/json" \
     -d '{"url": "https://vk.com/doc625817526_640729992?hash=…", "filename": "Mamie Luger - 01.pdf", "series_id": 1364, "volume_number": 1}' \
     https://bullarr.example.com/api/vk/download
```

Le téléchargement apparaît ensuite dans `GET /api/activity/status`.

### Recherche automatique (chercher et télécharger)

`POST /api/bedetheque/auto-acquire/run`

| Corps | Effet |
|---|---|
| `{"series_id": 42}` | Tous les tomes manquants de la série. |
| `{"series_id": 42, "volume_number": 12}` | Un tome précis. |
| `{"series_id": 42, "oneshot": true}` | Un one-shot. |

- **Variantes :** `is_integral`, `is_hs` et `is_episode` fonctionnent comme pour la recherche.
- **Avancement :** `GET /api/bedetheque/auto-acquire/status/<series_id>`.
- **Validations en attente :** les résultats ambigus attendent une validation. `GET /api/auto-acquire/reviews` les liste, et `POST /api/auto-acquire/reviews/<id>/download` avec `{"candidate_index": 0}` télécharge le candidat choisi.

## Nouveautés

| Route | Contenu |
|---|---|
| `GET /api/ebdz/rss/latest?limit=100&refresh=1` | Flux RSS : DupeFR, Prowlarr, forum VK… `refresh=1` force la relecture des flux. |
| `GET /api/telegram-channels/latest?limit=100` | Derniers fichiers des canaux Telegram. |
| `GET /api/ebdz/latest?limit=10` | Dernières sessions de lecture d'EBDZ. |
| `GET /api/ebdz/nouveautes/new-count` | Nombre de nouveautés non vues. |

Chaque entrée indique si la série est en bibliothèque (`already_in_library`), si le tome est possédé (`already_owned`), la série reconnue (`series_id`, `series_title`, `bedetheque_url`) et les liens de téléchargement (`download_links`). Les entrées du forum VK ont `vk: true`.

## Surveillance des tomes manquants

| Route | Rôle |
|---|---|
| `GET /api/missing-monitor/stats` | Séries surveillées, tomes manquants, derniers envois. |
| `GET /api/missing-monitor/series` | Séries surveillées. |
| `POST /api/missing-monitor/series/<series_id>/monitor` | Active ou coupe la surveillance d'une série (`{"enabled": true}`). |
| `GET /api/missing-monitor/history` | Historique des téléchargements de la surveillance. |

## Exemple complet en Python

```python
import requests

BASE = "https://bullarr.example.com"
session = requests.Session()
session.headers["X-Api-Key"] = "VOTRE_CLE"

def api(method, path, **kwargs):
    response = session.request(method, BASE + path, timeout=60, **kwargs)
    data = response.json()
    if response.status_code >= 400 or (isinstance(data, dict) and data.get("success") is False):
        raise RuntimeError(data.get("error") if isinstance(data, dict) else response.text)
    return data

# Téléchargements en cours, avec leur progression
for item in api("GET", "/api/activity/status")["pending"]:
    total = item.get("bytes_total") or 0
    done = item.get("bytes_downloaded") or 0
    print(item["id"], item["client"], item["title"], f"{100 * done // total}%" if total else "")

# Chercher le tome 4 et envoyer le premier résultat VK confirmé
results = api("POST", "/api/missing-monitor/search",
              json={"title": "Blake et Mortimer", "volume_num": 4, "series_id": 715, "sources": ["vk"]})["results"]
best = next((r for r in results if not r.get("unconfirmed_volume")), None)
if best:
    api("POST", "/api/vk/download", json={"url": best["download_url"], "filename": best["filename"],
                                         "series_id": 715, "volume_number": 4})
```
