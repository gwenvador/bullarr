# Changements

## v4.4 — 2026-10-04

[Comparer v4.3 et v4.4](https://github.com/gwenvador/bullarr/compare/v4.3...v4.4)

### Nouveautés et RSS

- Configuration des flux RSS par indexeur Prowlarr, avec catégories et origine visibles.
- Chargement des anciennes nouveautés par lots et affichage stable pendant l’actualisation des sources.
- Filtre de présence en bibliothèque, liens Bédéthèque sur les titres de séries et actions distinctes pour ajouter, rechercher ou remplacer un fichier déjà possédé.
- Meilleure reconnaissance des fichiers RSS déjà présents et de leurs tomes.

### Import et historique

- Démarrage plus rapide de la page Import et suivi plus cohérent des fichiers et des packs pendant le traitement.
- Conservation des tomes manquants après la confirmation d’un import ; recalcul des états devenus incohérents.
- Chargement de l’historique accéléré et pagination corrigée.

### Recherche et validation

- Validation des intégrales et des packs avec indication des tomes encore manquants.
- Classement des résultats Prowlarr avant limitation de la liste ; les torrents pertinents restent visibles aux côtés des autres sources dans Validation.
- Correction de l’envoi des torrents validés à qBittorrent et résolution des validations après un téléchargement automatique réussi.
- Lecture adaptée à la présentation actuelle des fiches Bédéthèque et classement distinct des suppléments numérotés.

### Fiabilité

- Renforcement des vérifications des téléchargements internes, des requêtes réseau et des scans de bibliothèque.
- Tests de régression ajoutés pour l’import, les nouveautés, la validation, Prowlarr et Bédéthèque.

Image : `ghcr.io/gwenvador/bullarr:v4.4` (`latest` pointe également vers cette version).

## v4.3

[Notes de version](https://github.com/gwenvador/bullarr/releases/tag/v4.3)
