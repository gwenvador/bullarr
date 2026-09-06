"""
Module pour gérer le renommage des fichiers/dossiers d'une série au format standard.

Format fixe (voir STANDARD_TEMPLATE): chaque bloc `{ ... }` est optionnel - si le(s)
tag(s) `<...>` qu'il contient n'ont pas de valeur pour un fichier donné, tout le bloc
(texte littéral compris, ex: " -
séparateur orphelin comme le ferait un simple remplacement de texte.

Tags supportés dans un bloc:
  <series>  - Titre de la série
  <number2> - Numéro de tome, complété par des zéros sur 2 chiffres (jamais tronqué:
              un numéro à 3 chiffres reste sur 3 chiffres)
  <title>   - Titre du tome (ComicInfo <Title>)
  <year>    - Année du tome (ComicInfo <Year>, repli sur volumes.year)
  <quality> - Tag de résolution/scan détecté dans le nom de fichier d'origine (ex:
              "Digital-1734", "1920" - voir volumes.resolution, alimenté par
              LibraryScanner.parse_filename). None si le fichier n'en portait aucun.
  <group>   - Groupe de scan/release détecté dans le nom de fichier d'origine (ex:
              "NEO RIP-Club", "TONER" - voir volumes.release_group, même parseur).
              "tous les fichiers n'ont pas la bonne nomenclature donc a faire au
              mieux" - None si aucun groupe n'a pu être extrait, comme <quality>.
  <univers> - Nom de l'univers Bédéthèque de la série (voir series.universe_id /
              BedethequeDatabase.sync_series_universe, "toutes ces series font parties
              du meme univers") - None si cette série n'appartient à aucun univers
              connu (la grande majorité des séries, qui n'ont pas de "Séries liées"
              sur Bédéthèque).

Le format par défaut ci-dessous reste utilisé quand l'utilisateur n'a pas configuré de
format personnalisé dans Paramètres > Bibliothèque (voir blueprints/settings/rename_config_store.py).
"""
import os
import re
from pathlib import Path
from typing import Dict, List, Optional
import logging

logger = logging.getLogger(__name__)

DEFAULT_VOLUME_TEMPLATE = "{<series>} { - #<number2>} { - <title>} { - (<year>)} { - [<quality>] } { - [<group>] }"
DEFAULT_SERIES_TEMPLATE = "{<univers>/}<series>"

# Alias conservé pour compatibilité avec le code existant qui référence encore ce nom
STANDARD_TEMPLATE = DEFAULT_VOLUME_TEMPLATE

_BLOCK_PATTERN = re.compile(r'\{([^{}]*)\}')
_TOKEN_PATTERN = re.compile(r'<([a-zA-Z0-9_]+)>')


def _is_within(path: Path, root: Path) -> bool:
    """Vérifie que `path` reste bien contenu dans `root` une fois les deux résolus
    (symlinks/`..` normalisés) - protège contre une évasion du dossier de la série via
    un nom de fichier (ou une métadonnée ComicInfo interpolée dans le nom) contenant
    des séparateurs de chemin ou des '../'"""
    try:
        return os.path.commonpath([str(path.resolve()), str(root)]) == str(root)
    except (OSError, ValueError):
        return False


def _sanitize_component(value: Optional[str]) -> Optional[str]:
    """Neutralise les séparateurs de chemin dans une valeur interpolée (titre de série,
    titre de tome ComicInfo...) pour qu'elle ne puisse jamais transformer un nom de
    fichier censé être plat en chemin multi-segments. `_is_within` reste le garde-fou
    final avant tout renommage réel, mais cette normalisation évite de rejeter tout un
    fichier juste parce que son titre contient un '/' usuel."""
    if not value:
        return value
    value = value.replace('/', '-')
    if os.sep != '/':
        value = value.replace(os.sep, '-')
    if os.altsep:
        value = value.replace(os.altsep, '-')
    return value


def render_standard_template(template: str, values: Dict[str, Optional[str]]) -> str:
    """Applique le format à blocs optionnels décrit en tête de module.

    values: dict tag -> valeur déjà formatée pour l'affichage (ex: number2 déjà
    zero-paddé) ou None/'' si absente pour ce fichier.
    """
    def render_block(match):
        block = match.group(1)
        tokens = _TOKEN_PATTERN.findall(block)
        # Si un seul des tags du bloc n'a pas de valeur, tout le bloc est omis
        if any(not values.get(tag) for tag in tokens):
            return ''
        return _TOKEN_PATTERN.sub(lambda m: _sanitize_component(str(values.get(m.group(1), ''))) or '', block)

    rendered = _BLOCK_PATTERN.sub(render_block, template)
    # Tag hors bloc {...} (ex: le format série par défaut "<series>", sans accolades):
    # pas de sémantique "optionnel" possible pour lui (rien à omettre autour), simple
    # remplacement direct - sans ce second passage un tag écrit sans accolades par
    # inattention (dans un format personnalisé saisi en Paramètres, par ex.) resterait
    # affiché tel quel ("<series>") au lieu d'être substitué
    rendered = _TOKEN_PATTERN.sub(lambda m: _sanitize_component(str(values.get(m.group(1)) or '')) or '', rendered)
    rendered = re.sub(r' {2,}', ' ', rendered).strip()
    return rendered


def compute_volume_tokens(series_title: str, volume: Dict, is_series_oneshot: bool,
                           universe_name: Optional[str] = None) -> Dict[str, Optional[str]]:
    """Détermine la valeur de chaque tag du format standard pour un tome donné.

    Priorité du numéro affiché (miroir de `numberLabel`/`buildVolumeItemHtml` côté
    frontend, seule source de vérité déjà existante pour cette logique):
      - is_integral -> integral_number (une intégrale garde son propre numéro, pas le
        numéro de tome qu'elle regroupe), préfixé "INT" (ex: #INT01, ou juste #INT si
        pas de numéro) pour rester visuellement distinct d'un tome normal - sans ce
        préfixe, une intégrale/un hors-série renommé perdait son marqueur et devenait
        indiscernable d'un tome classique de même numéro (ex: #01)
      - is_hs -> hs_number, préfixé "HS" de la même façon
      - sinon, si la série est un one-shot -> pas de numéro du tout
      - sinon -> volume_number, sans préfixe
    """
    ci = volume.get('comicinfo') or {}

    if volume.get('is_integral'):
        number = volume.get('integral_number')
        prefix = 'INT'
    elif volume.get('is_hs'):
        number = volume.get('hs_number')
        prefix = 'HS'
    elif is_series_oneshot:
        number = None
        prefix = ''
    else:
        number = volume.get('volume_number')
        prefix = ''

    if number is not None:
        number2 = f"{prefix}{str(number).zfill(2)}"
    elif prefix:
        number2 = prefix
    else:
        number2 = None

    # Un one-shot doit normalement reprendre le titre de l'album. Certaines fiches
    # importées n'ont toutefois pas de <title> ComicInfo : le nom de la série sert alors
    # de repli pour éviter un fichier qui commencerait par « - (année) ».
    title = ci.get('title') or (series_title if is_series_oneshot else None)

    year = ci.get('year') or volume.get('year')
    year = str(year) if year else None

    return {
        'series': series_title,
        'number2': number2,
        'title': title,
        'year': year,
        'quality': volume.get('resolution') or None,
        'group': volume.get('release_group') or None,
        'univers': universe_name or None,
    }


def render_series_folder_name(series_title: str, template: Optional[str] = None,
                               universe_name: Optional[str] = None) -> str:
    """Calcule le nom de dossier d'une série à partir du format configuré (<series> et
    <univers> ont un sens ici, pas de tome associé au dossier lui-même). Peut rendre un
    chemin où le dossier de la série porte le même nom que son propre dossier d'univers
    (ex: "Lanfeust de Troy/Lanfeust de Troy" - un univers est nommé d'après la série
    depuis laquelle il a été détecté en premier, voir sync_series_universe) - "I still
    want to have /BD/Lanfeust de Troy/Lanfeust de Troy" : voulu tel quel, le déplacement
    d'un dossier dans son propre sous-dossier est géré séparément (voir
    _rename_series_folder côté library/routes.py, os.rename seul ne le permet pas)."""
    template = template or DEFAULT_SERIES_TEMPLATE
    rendered = render_standard_template(template, {'series': series_title, 'univers': universe_name or None})
    return rendered or series_title


class FileRenamer:
    """Calcule et effectue le renommage des fichiers d'une série au format standard."""

    @staticmethod
    def build_rename_plan(series_title: str, volumes: List[Dict], is_series_oneshot: bool,
                           template: Optional[str] = None, universe_name: Optional[str] = None,
                           oneshot_template: Optional[str] = None) -> List[Dict]:
        """Calcule, sans toucher au disque, le nouveau nom de chaque fichier.

        volumes: liste de dicts avec au moins 'id', 'filename', 'volume_number',
        'is_integral', 'integral_number', 'is_hs', 'hs_number', 'year', 'comicinfo',
        'resolution', 'release_group'.

        template: format personnalisé (voir DEFAULT_VOLUME_TEMPLATE pour la syntaxe),
        celui configuré dans Paramètres si non fourni.

        universe_name: voir compute_volume_tokens - None si la série n'appartient à
        aucun univers Bédéthèque connu.

        Retourne une liste de dicts {volume_id, old_name, new_name, changed}.
        """
        template = template or DEFAULT_VOLUME_TEMPLATE
        if is_series_oneshot:
            template = oneshot_template or "{<title>} { - (<year>)} { - [<quality>] } { - [<group>] }"
        plan = []
        for v in volumes:
            filename = v.get('filename') or ''
            ext = Path(filename).suffix

            tokens = compute_volume_tokens(series_title, v, is_series_oneshot, universe_name)
            new_stem = render_standard_template(template, tokens)
            if not new_stem:
                # Filet de sécurité: ne jamais produire un nom vide (ex: série sans
                # titre) - on garde alors le nom de fichier d'origine
                new_stem = Path(filename).stem

            new_name = f"{new_stem}{ext}"
            plan.append({
                'volume_id': v.get('id'),
                'old_name': filename,
                'new_name': new_name,
                'changed': new_name != filename,
            })
        return plan

    @staticmethod
    def execute_rename_plan(series_path: str, plan: List[Dict]) -> List[Dict]:
        """Effectue réellement les renommages sur disque à partir d'un plan calculé par
        `build_rename_plan`. Reprend les garde-fous de l'ancien système: confinement au
        dossier de la série (`_is_within`), refus si la destination existe déjà, et
        isolation des erreurs fichier par fichier (un échec n'interrompt pas les autres).
        """
        series_path_obj = Path(series_path).resolve()
        results = []

        for item in plan:
            old_name = item['old_name']
            new_name = item['new_name']

            if not item['changed']:
                results.append({**item, 'success': True, 'skipped': True})
                continue

            old_path = series_path_obj / old_name
            new_path = series_path_obj / new_name

            if not _is_within(old_path, series_path_obj) or not _is_within(new_path, series_path_obj):
                logger.warning(f"Chemin hors du dossier de la série ignoré: {old_name} -> {new_name}")
                results.append({**item, 'success': False, 'error': 'Chemin invalide (hors du dossier de la série)'})
                continue

            if not old_path.exists():
                results.append({**item, 'success': False, 'error': 'Fichier introuvable'})
                continue

            if new_path.exists() and old_path != new_path:
                logger.warning(f"Fichier destination existe déjà: {new_path}")
                results.append({**item, 'success': False, 'error': 'Fichier destination existe déjà'})
                continue

            try:
                old_path.rename(new_path)
                results.append({**item, 'success': True})
                logger.info(f"Fichier renommé: {old_name} -> {new_name}")
            except Exception as e:
                results.append({**item, 'success': False, 'error': str(e)})
                logger.error(f"Erreur renommage {old_name} -> {new_name}: {e}")

        return results
