# Changements

## v6.0 — 2026-10-08

[Comparer v5.0 et v6.0](https://github.com/gwenvador/bullarr/compare/v5.0...v6.0)

Mise à jour directe depuis v5.0, sans migration de base. Les corrections de suivi des téléchargements rassemblées ici n'étaient pas dans la v5.0. Les packs livrés sous forme d'archive (ZIP) ont désormais un vrai parcours dans la page Import.

### Import : la base de données fait référence

- Tout téléchargement encore en attente, en cours ou en attente d'import reste affiché sur la page Import, y compris lorsque le tome est déjà possédé (une mention « déjà possédé » l'indique). Une ligne ne disparaît plus tant que la base ne la considère pas comme terminée.
- Une ligne n'est plus close tant que le client de téléchargement signale la release comme inachevée ; les lignes closes à tort sont rouvertes.
- Les lignes jumelles d'une même release (même téléchargement ajouté plusieurs fois) sont finalisées ensemble.
- Les lignes restées « En attente… » alors que leurs fichiers ont déjà été importés, et les packs terminés dont il ne reste que des fichiers traités, sont réconciliés automatiquement.
- Les lignes « Prêt — import automatique » sont verrouillées (destination, volume, sélection et suppression), comme un import en cours. Le bouton « Reprendre la main » les repasse en import manuel.
- Le menu déroulant de volume reste dans sa colonne au lieu de déborder sur la colonne voisine.

### Packs livrés en archive (ZIP)

- Un ZIP dont les images sont réparties en plusieurs dossiers (un dossier par tome) est reconnu comme un pack. Il n'est plus converti en un seul CBZ importé comme un tome unique (un pack « 01 à 03 + HS » avait été importé comme un seul hors-série).
- Dans la page Import, le pack est une ligne repliable qui sert de simple repère, non importable. Son contenu s'affiche dans le dépli : dossiers, fichiers, cases à cocher. Seuls les dossiers qui contiennent des images sont empaquetables.
- « Empaqueter les dossiers cochés » crée un CBZ par dossier ; l'archive source n'est jamais modifiée. Les CBZ créés apparaissent dessous, décalés, avec leur menu de volume, et s'importent, se convertissent ou se suppriment comme les autres fichiers. Ils sont conservés dans le volume de données (`data/package-temp`) et survivent à un redéploiement.
- Le bouton « Valider le pack comme terminé » clôt le téléchargement et retire le pack de la page ; l'historique l'indique (« Validation de pack »). L'archive n'est jamais supprimée du disque.
- Une archive mise en attente manuelle n'est plus extraite puis supprimée d'office au chargement de la page Import.
- Le bouton « Voir le contenu » fonctionne de nouveau : la route serveur correspondante manquait et le navigateur recevait une page HTML à la place de la réponse attendue.
- Le placeholder du pack n'est plus compté dans les fichiers sélectionnés ni dans la sélection groupée.

### Nouveautés

- Les annotations des entrées RSS suivent maintenant l'état de la bibliothèque : une release annotée avant l'ajout de sa série (ou avant l'import de son tome) est recalculée, au lieu de rester « absente de la bibliothèque ».
- Le calcul d'annotation passe de 13 s à moins d'une seconde pour 100 releases : les noms des téléchargements actifs ne sont plus relus pour chaque release.
- Le premier affichage se limite aux 100 entrées les plus récentes ; le reste de la fenêtre n'est chargé qu'avec « Charger plus ». Elle n'est plus rechargée en entier d'office en arrière-plan.
- L'icône « ouvrir le lien » des entrées RSS s'affiche de nouveau, ainsi que deux autres icônes qui manquaient au registre (`alert-triangle`, `folder-open`).
- Le tableau tient dans la fenêtre sans défilement horizontal : sous 900 px, chaque ligne devient une carte, et la colonne Actions ne passe plus par-dessus la colonne Série. Les largeurs de colonnes mémorisées d'un ancien redimensionnement ne s'appliquent plus à ce tableau.

### Bédéthèque

- Quand l'index local ne contient aucun candidat fiable, la recherche de série interroge Bédéthèque en direct, au lieu de s'arrêter sur des séries sans rapport qui partagent seulement un mot. Une série récente absente de l'index est de nouveau trouvée.
- Mise à jour périodique de l'index : nouveau réglage dans Paramètres → Bédéthèque (activée, tous les N jours, à l'heure choisie ; 7 jours et 4 h par défaut). Un contrôle horaire relit le réglage et ne reconstruit l'index que s'il a au moins l'âge demandé ; un verrou évite une reconstruction simultanée par deux processus. La carte du réglage a été refaite.

### Correctifs

- Un nom de fichier qui n'est pas en UTF-8 ne fait plus perdre tout le lot de fichiers découverts : seul ce fichier est ignoré.
- La suppression, la conversion et l'import d'un CBZ empaqueté n'étaient pas possibles (« Répertoire d'import non autorisé ») : le dossier d'empaquetage fait maintenant partie des dossiers autorisés.

### Sécurité

- Analyse CodeQL : les deux alertes ouvertes sur la première publication de la v6.0 sont corrigées. Le nom du CBZ créé à l'empaquetage, issu d'un nom de dossier de l'archive, est vérifié comme restant dans le dossier de sortie (un nom hostile est refusé). Le message d'erreur du réglage de l'index passe par le même outil que les autres routes.

### Tests

- Plus de cent tests ajoutés : protection contre les requêtes vers des adresses internes, chiffrement, calcul d'identifiant de torrent, limiteur de requêtes, planificateur d'import, historique et réservations d'import, règles de décision d'import (doublons, tome suivi, archives), index du catalogue et sa mise à jour, archives et empaquetage, validation de pack, annotation RSS, registre d'icônes.
- La suite compte 375 tests ; la couverture mesurée sous pytest passe de 28 % à 41 %.

### Points d'attention

- Les dossiers d'import montés en lecture seule le restent : l'application n'y supprime aucune archive.
- Un doublon est jugé sur la taille (marge de 5 %) sans tenir compte du format.

Image : `ghcr.io/gwenvador/bullarr:v6.0` (`latest` pointe également vers cette version).

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
