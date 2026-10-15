# Changements

## v7.0 — 2026-10-15

[Comparer v6.2 et v7.0](https://github.com/gwenvador/bullarr/compare/v6.2...v7.0)

Mise à jour directe depuis v6.2, sans migration manuelle. Au premier démarrage, les releasers enregistrés qui ne sont qu'un format (« PDF », « CBZ ») sont effacés.

### Import

- Bouton « Arrêter l'import » : la file en cours s'arrête après le fichier en cours et l'import automatique reste en pause jusqu'à « Reprendre ».
- Un tome dont le numéro n'existe pas dans le catalogue Bédéthèque de la série n'est plus importé automatiquement : il attend une vérification manuelle.
- Un torrent de plusieurs fichiers est suivi comme un pack et ne se ferme qu'une fois tous ses fichiers traités ; la page Import l'affiche en pack dès le premier fichier.
- Un téléchargement enregistré sans extension (fichier de torrent nommé d'après la release) reçoit l'extension lue dans son contenu (PDF, ZIP, RAR) et est importé.
- Les tomes importés sans résumé reçoivent en arrière-plan le résumé de leur page d'album Bédéthèque.
- Assigner une nouvelle série depuis la fenêtre d'import la crée dans la bibliothèque sans lancer de téléchargement automatique.

### Lecture des noms de fichiers

- « T01 à 15 et 2 HS » est un pack de tomes, plus un hors-série.
- Une mention de format entre crochets ou parenthèses (« [PDF] », « [CBR].[CBZ] ») n'est jamais un releaser ni un auteur.

### Surveillance et validation

- Les tomes du catalogue sans fichier comptent comme manquants pour la surveillance RSS, EBDZ et Telegram.
- « Déjà téléchargé » compare le tome, et plus seulement la série.
- Validation : la série manquante est créée au rattachement ; un candidat sans lien est retrouvé via Prowlarr.
- Délai de recherche Prowlarr porté à 30 s.

### Nouveautés

- Les lignes RSS, Telegram et EBDZ ont la même présentation : une ligne par fichier EBDZ, sans repli.
- Une intégrale, numérotée ou non, est marquée possédée lorsque tous les albums réguliers de la série le sont.
- « Télécharger le fichier torrent » passe par le serveur pour les liens Prowlarr, qui ajoute la clé API configurée (le lien direct renvoyait une erreur 401).
- Le crayon de choix de la série est placé à côté du nom de la série.

### Bibliothèque

- Dates des albums au mois et à l'année, date complète en infobulle ; le filtre de date fonctionne par année.
- Panier d'achat (Fnac, Amazon, BDfugue) par album, sur les one-shots et pour le premier album d'une série, avec recherche par ISBN quand il est connu.
- Déplacer un album vers une autre série permet de choisir le tome de destination.
- Modifier la qualité ou le releaser d'un tome renomme son fichier.
- Univers Bédéthèque : prise en charge de la nouvelle mise en page des séries liées.

### Interface

- Vérification : badges d'effet par section (Modifie / Supprime) et actions groupées nommées par leur conséquence.
- Accessibilité : vrais liens sur les affiches et titres de séries, libellés sur les boutons à icône, cases et filtres ; actions des affiches visibles au clavier.
- Icônes SVG dans les titres de page, états vides plus compacts, contraste renforcé du gris et du vert.
- Mobile et tablette : déconnexion dans le menu latéral sur mobile, marges latérales réduites sur tablette ; mode sombre corrigé pour les options de connexion.

## v6.2 — 2026-10-11

[Comparer v6.1 et v6.2](https://github.com/gwenvador/bullarr/compare/v6.1...v6.2)

Mise à jour directe depuis v6.1, sans migration de base.

- Flux RSS Prowlarr : le lien de téléchargement de chaque entrée est conservé, sans la clé API. Le serveur ajoute la clé configurée au moment d'envoyer le torrent à qBittorrent, rTorrent ou Deluge. L'éclair de Nouveautés et la surveillance RSS téléchargent ainsi le fichier du flux, sans dépendre d'une recherche Prowlarr (qui pouvait échouer sur un délai dépassé).
- Nouveautés : un pack de plusieurs tomes (« T01-T03 », « [T01.T31] », « T1 à T5 ») est marqué possédé lorsque tous ses tomes le sont, au lieu d'être lu comme une intégrale manquante.
- Nouveautés : une « Intégrale » sans numéro d'une série dont le catalogue ne contient aucune intégrale est marquée possédée lorsque la série est complète.

## v6.1 — 2026-10-10

[Comparer v6.0 et v6.1](https://github.com/gwenvador/bullarr/compare/v6.0...v6.1)

Mise à jour directe depuis v6.0, sans migration de base.

### Import : lecture du numéro de tome

- Un numéro en tête de nom (« Série - 12 - Titre », « Série-12-Titre », « Série - T12 - Titre ») l'emporte sur un « Tome 2 » ou « T2 » plus loin dans le nom, quand la partie de tête est bien le titre de la série. « Blake Et Mortimer-26-La Vallée Des Immortels - T2 » était importé comme tome 2.
- Les titres d'album Bédéthèque valident le numéro : un nom qui contient le titre complet d'un album est rangé dans cet album. Si le nom ne partage aucun mot avec l'album du tome lu, mais contient une partie du titre d'un autre album, c'est ce dernier qui est retenu (« T1 » ou « T2 » départage deux parties d'un même cycle).
- Les hors-séries, intégrales et épisodes sans numéro sont rapprochés par leur titre au lieu d'être écartés comme doublons.
- La fenêtre de recherche d'un tome applique la même lecture à chaque résultat et met à jour la mention « tome non confirmé ».

### Import : packs et téléchargements

- Une archive est classée pack ou album unique d'après son contenu ; une archive qui ne contient que des images n'est plus extraite.
- Un téléchargement multi-fichiers reste ouvert tant que ses fichiers arrivent ; la page Import indique l'état de la copie locale (absente, en cours, terminée).
- Un fichier déjà importé puis téléchargé de nouveau est de nouveau importable.
- Un pack est retrouvé par le nom de son dossier, même sans identifiant de torrent.
- Au démarrage, les imports interrompus par un redémarrage sont libérés.
- L'import automatique ne tourne que lorsque des fichiers sont prêts ; l'état des clients de téléchargement est relevé chaque minute. Les fichiers sont importés par ordre de format préféré.
- Les entrées de suivi d'import terminées sont purgées.
- Libellés plus clairs sur la page Import (« Import automatique en attente », « Import manuel à valider », « N sur M fichiers importables ») ; les tomes déjà possédés sont signalés.

### Surveillance

- Nouvelle surveillance des flux RSS : les nouveautés des séries suivies sont téléchargées automatiquement, avec plusieurs tentatives si le fichier n'est pas encore trouvé.
- L'index local Bédéthèque peut être rafraîchi automatiquement (vérification toutes les heures).
- Dans Nouveautés, l'éclair ajoute directement la série reconnue et télécharge le fichier de la ligne, quelle que soit la source ; sans correspondance, il passe en validation.

### Interface

- Le tableau des tomes d'une série tient dans la largeur de la page, sans défilement horizontal.
- La fenêtre de recherche ne se rafraîchit plus de façon intempestive (défilement et focus conservés).
- Les cases à cocher de la configuration sont remplacées par des interrupteurs.
- Nouveautés : colonnes Type et Origine, liste des séries et filtre des détails élargis sur grand écran.

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
