"""
Conversion optionnelle zip -> cbz pour un fichier .zip nu qui traîne dans un répertoire
d'import (souvent des planches déjà empaquetées par un outil tiers qui n'a pas pensé à
utiliser l'extension .cbz). Un .cbz n'est rien d'autre qu'une archive zip: cette
conversion est donc essentiellement un renommage validé, pas un ré-encodage complet
(contrairement à pdf_converter.convert_pdf_to_cbz, qui doit vraiment rendre des pages).
Jamais automatique: appelée uniquement à la demande explicite de l'utilisateur depuis
/import (voir POST /api/import/convert), même principe que
blueprints/bedetheque/cbr_converter.convert_cbr_to_cbz.
"""
import os
import shutil
import tempfile
import zipfile

# Extensions considérées comme des planches de bande dessinée - sert uniquement à
# vérifier que l'archive n'est pas un zip quelconque (sauvegarde, export d'un autre
# outil...) avant de la faire passer pour un cbz
IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp'}


class ZipConversionError(Exception):
    """Levée quand la conversion zip -> cbz échoue (zip illisible/corrompu, cbz cible
    déjà existant, ou archive ne contenant aucune image reconnaissable comme planche de
    bande dessinée). Le fichier .zip d'origine n'est jamais modifié dans ce cas."""
    pass


def convert_zip_to_cbz(filepath):
    """
    "Convertit" le zip à filepath en cbz (même dossier, même nom de base): valide que
    l'archive n'est pas corrompue et contient au moins une image, puis renomme
    l'extension - pas de ré-empaquetage nécessaire puisqu'un cbz EST un zip.

    Returns:
        str: chemin du nouveau fichier .cbz

    Raises:
        ZipConversionError: cbz cible déjà existant, zip illisible/corrompu, ou aucune
            image trouvée dans l'archive
    """
    directory = os.path.dirname(filepath) or '.'
    base, _ext = os.path.splitext(filepath)
    try:
        source_writable = os.access(directory, os.W_OK)
    except OSError:
        source_writable = False
    output_dir = directory if source_writable else tempfile.mkdtemp(prefix='.zipconv_ro_')
    new_cbz_path = os.path.join(output_dir, os.path.basename(base) + '.cbz')

    # Vérifié avant même d'ouvrir l'archive: pas la peine de valider un zip potentiellement
    # volumineux si la conversion est de toute façon vouée à échouer sur ce point
    if os.path.exists(new_cbz_path):
        raise ZipConversionError(f"Le fichier cible {new_cbz_path} existe déjà, conversion annulée")

    try:
        zf = zipfile.ZipFile(filepath)
    except Exception as e:
        raise ZipConversionError(f"Archive zip illisible ou corrompue: {e}")

    with zf:
        bad_file = zf.testzip()
        if bad_file is not None:
            raise ZipConversionError(f"Archive zip corrompue (membre invalide: {bad_file})")

        has_image = any(
            os.path.splitext(name)[1].lower() in IMAGE_EXTENSIONS
            for name in zf.namelist()
            if not name.endswith('/')
        )
        if not has_image:
            raise ZipConversionError(
                "Cette archive zip ne contient aucune image reconnue, ce n'est probablement pas un pack de planches"
            )

    # Simple renommage: cbz et zip sont le même format d'archive, aucune donnée à
    # réécrire. os.replace est atomique tant que source/cible sont sur le même
    # système de fichiers, ce qui est toujours le cas ici (même dossier) - sauf source
    # non-inscriptible, où une copie vers le dossier temporaire ci-dessus remplace le
    # renommage (impossible sur un montage read-only).
    if source_writable:
        os.replace(filepath, new_cbz_path)
    else:
        shutil.copy2(filepath, new_cbz_path)
    return new_cbz_path



def package_zip_folders_to_cbz(filepath, output_dir, selected_folder_paths=None):
    from pathlib import Path
    import re
    os.makedirs(output_dir, exist_ok=True)
    selected = {str(Path(x)).replace(chr(92), '/').strip('/') for x in (selected_folder_paths or []) if str(x).strip('/')}
    if not selected: raise ZipConversionError('Aucun dossier sélectionné')
    try: zf = zipfile.ZipFile(filepath)
    except Exception as exc: raise ZipConversionError(f'Archive zip illisible ou corrompue: {exc}')
    with zf:
        bad_file = zf.testzip()
        if bad_file is not None: raise ZipConversionError(f'Archive zip corrompue (membre invalide: {bad_file})')
        members = [n for n in zf.namelist() if not n.endswith('/') and os.path.splitext(n)[1].lower() in IMAGE_EXTENSIONS]
        created = []; used = set()
        # Accepter aussi le basename envoyé par une ancienne version du rendu
        # de l’arbre, si ce basename est unique dans l’archive.
        archive_dirs = {n.rsplit('/', 1)[0] for n in members if '/' in n}
        for value in list(selected):
            if '/' not in value:
                matches = [d for d in archive_dirs if Path(d).name == value]
                if len(matches) == 1:
                    selected.remove(value); selected.add(matches[0])
        expanded = set(selected)
        for selected_path in list(selected):
            prefix = selected_path + '/'
            child_dirs = {n[len(prefix):].split('/', 1)[0] for n in members if n.startswith(prefix) and '/' in n[len(prefix):]}
            if child_dirs and not any(n.startswith(prefix) and '/' not in n[len(prefix):] for n in members):
                expanded.update(prefix + child for child in child_dirs)
        for selected_path in sorted(expanded):
            prefix = selected_path + '/'
            names = [n for n in members if n.startswith(prefix) and '/' not in n[len(prefix):]]
            if not names: continue
            safe = Path(selected_path).name.replace('/', '_').strip(' .')
            out = Path(output_dir) / f'{safe}.cbz'; suffix = 2
            while str(out) in used or out.exists(): out = Path(output_dir) / f'{safe} ({suffix}).cbz'; suffix += 1
            used.add(str(out))
            with zipfile.ZipFile(out, 'w', compression=zipfile.ZIP_DEFLATED) as dest:
                for name in names: dest.writestr(Path(name).name, zf.read(name))
            created.append({'path': str(out), 'folder': selected_path, 'file_count': len(names)})
        if not created: raise ZipConversionError('Aucun des dossiers sélectionnés ne contient d’image')
        return created
