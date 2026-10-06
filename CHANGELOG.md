# Changements

## v5.0 — 2026-10-06

[Comparer v4.4 et v5.0](https://github.com/gwenvador/bullarr/compare/v4.4...v5.0)

Version de consolidation : mise à jour directe depuis v4.4, sans migration de base. Un seul changement de comportement : la page de connexion n'effectue plus de redirection automatique vers le SSO (voir « Authentification »).

### Authentification

- Page de connexion refondue : formulaire identifiant/mot de passe et/ou bouton « Se connecter avec le SSO » selon le mode configuré.
- Nouveau mode « Mot de passe + OIDC » (Configuration → SSO) : le formulaire et le bouton SSO sont proposés sur la même page.
- Plus de connexion automatique au SSO par défaut, y compris en mode OIDC seul. L'option « Redirection automatique vers le SSO » (désactivée par défaut) rétablit l'ancien comportement ; le formulaire reste alors accessible via `/login?noauto=1`, et la page de déconnexion renvoie vers la page de connexion sans redirection automatique.
- Validation à l'enregistrement : identifiant et mot de passe requis pour les modes avec mot de passe, URL de l'issuer et Client ID requis pour OIDC, afin d'éviter de se verrouiller avec une configuration incomplète.
- Le commutateur de récupération `BULLARR_AUTH_BYPASS_LOGIN` est enfin lu par la configuration (il était documenté mais sans effet) et signale son activation au démarrage. Le modèle `docker-compose.example.yml` le transmet au conteneur ; si vous utilisez votre propre fichier, ajoutez `BULLARR_AUTH_BYPASS_LOGIN: ${BULLARR_AUTH_BYPASS_LOGIN:-false}` à `environment`.

### Bédéthèque et BDGest : nouvelle mise en page

- Lecture adaptée à la refonte des deux sites : recherche de séries et d'auteurs, bibliographie d'un auteur, avis de lecteurs, Indispensables, Panthéon, Thèmes, page d'un thème et Top annuel BDGest.
- La liste des thèmes est de nouveau complète : la page d'index n'en montre plus qu'un aperçu par groupe, le reste est récupéré via « Tous les thèmes ».
- Une page qui ne renvoie plus aucun résultat produit une erreur explicite et n'est plus enregistrée dans le cache, qui n'expire jamais.
- Les lecteurs de pages sont regroupés dans `blueprints/bedetheque/parsers.py` et testés sur des pages de référence.

### Import et packs

- Un pack copié par le réseau n'est plus clos avant l'arrivée de ses derniers fichiers : tant que des fichiers apparaissent ou que des fichiers temporaires de transfert subsistent, il reste en attente (10 minutes de calme), et sa clôture est retentée à chaque passage.
- Un pack dont il ne reste que des fichiers déjà traités (doublons revenus par resynchronisation, sources en lecture seule) est clos automatiquement par la réconciliation périodique, au lieu de rester « Prêt — scan automatique sous peu ».
- Les fichiers non traités d'un pack déjà clos restent visibles sur la page Import.

### Correctifs

- La conversion en CBZ d'un tome déjà possédé (CBR, PDF, ZIP) depuis la molette d'un tome échouait systématiquement.
- Le sélecteur d'auteur de la page Bédéthèque n'affiche plus au plus 100 auteurs : la liste se complète au défilement.
- Les messages d'erreur détaillés de l'interface sont conservés ; la trace complète est en plus écrite dans les journaux.

### Sécurité

- Les chemins fournis par le client sont confinés aux dossiers d'import et de bibliothèque (nouveau module `path_safety.py`).
- Les requêtes sortantes vers Bédéthèque et Prowlarr ne peuvent viser que leur hôte attendu.
- Deux expressions régulières coûteuses sont bornées, et l'identifiant de série lu dans la page est converti en entier avant d'être réinséré dans le HTML.
- Analyse CodeQL : sur les 114 alertes ouvertes au 6 octobre, 111 sont corrigées et 3 sont classées comme faux positifs (deux sur des messages d'erreur volontairement détaillés, une sur un nom de fichier qui est toujours un simple nom produit par la conversion, sans séparateur de chemin possible).

### Nettoyage et tests

- Environ 380 lignes de code mort et 41 imports inutilisés supprimés.
- 67 tests ajoutés (authentification, conversion, packs, chemins, messages d'erreur, lecteurs de pages Bédéthèque et BDGest, sélecteur d'auteur).
- Le `.dockerignore` exclut toujours la configuration locale et les contenus importés de l'image.

### Points d'attention

- Un nom de fichier qui n'est pas en UTF-8 (typiquement un export aMule en Latin-1) ne peut pas être enregistré en base : le lot de fichiers découverts correspondant est ignoré.
- Un doublon est jugé sur la taille (marge de 5 %) sans tenir compte du format.

Image : `ghcr.io/gwenvador/bullarr:v5.0` (`latest` pointe également vers cette version).

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
