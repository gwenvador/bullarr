"""
Chargement/sauvegarde du format de renommage personnalisable (volumes et dossier de
série), utilisé par rename_handler.py au lieu du format standard fixe d'origine.
"""
import json
import os
from flask import current_app

DEFAULT_RENAME_CONFIG = {
    # Nom de fichier de chaque tome (voir rename_handler.render_standard_template
    # pour la syntaxe des blocs `{ ... }` et tags `<...>`)
    'volume_template': "{<series>} { - #<number2>} { - <title>} { - (<year>)} { - [<quality>] } { - [<group>] }",
    # Les one-shots portent souvent le même titre dans le dossier et dans ComicInfo;
    # leur modèle dédié évite donc de répéter <title> dans le nom du fichier.
    'oneshot_template': "{<title>} { - (<year>)} { - [<quality>] } { - [<group>] }",
    # Nom du dossier de la série (<univers>/ seulement si la série appartient à un
    # univers Bédéthèque connu, voir series.universe_id)
    'series_template': "{<univers>/}<series>",
}


def load_rename_config():
    """Charge la configuration de renommage, complétée par les valeurs par défaut pour
    toute clé absente (ex: après l'ajout d'un nouveau tag/template)."""
    config_file = current_app.config['RENAME_CONFIG_FILE']

    if os.path.exists(config_file):
        with open(config_file, 'r') as f:
            cfg = json.load(f)
    else:
        cfg = {}

    merged = DEFAULT_RENAME_CONFIG.copy()
    merged.update({k: v for k, v in cfg.items() if v})
    return merged


def save_rename_config(config):
    """Sauvegarde la configuration de renommage"""
    config_file = current_app.config['RENAME_CONFIG_FILE']

    try:
        with open(config_file, 'w') as f:
            json.dump(config, f, indent=4)
        return True
    except Exception as e:
        print(f"Erreur sauvegarde config renommage : {e}")
        return False
