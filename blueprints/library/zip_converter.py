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



def package_zip_folders_to_cbz(filepath, output_dir, match_tome_numbers=True, selected_folder_paths=None):
    """Create one CBZ copy per image-bearing folder in a ZIP; never alters source."""
    from pathlib import Path
    import re
    os.makedirs(output_dir, exist_ok=True)
    try:
        zf = zipfile.ZipFile(filepath)
    except Exception as exc:
        raise ZipConversionError(f"Archive zip illisible ou corrompue: {exc}")
    with zf:
        bad_file = zf.testzip()
        if bad_file is not None:
            raise ZipConversionError(f"Archive zip corrompue (membre invalide: {bad_file})")
        members = [n for n in zf.namelist() if not n.endswith('/') and os.path.splitext(n)[1].lower() in IMAGE_EXTENSIONS]
        if not members:
            raise ZipConversionError("Cette archive zip ne contient aucune image")
        parts = [Path(n).parts for n in members]
        common = parts[0][:-1]
        for p in parts[1:]:
            limit = min(len(common), len(p) - 1)
            i = 0
            while i < limit and common[i] == p[i]: i += 1
            common = common[:i]
        groups = {}
        for n in members:
            p = Path(n).parts
            key = p[len(common)] if len(p) > len(common) + 1 else '__root__'
            groups.setdefault(key, []).append(n)
        base = Path(filepath).stem
        created = []
        used = set()
        selected_folder_paths = set(selected_folder_paths or [])
        for index, (folder, names) in enumerate(sorted(groups.items())):
            folder_path = '/'.join((*common, folder)) if folder != '__root__' else ''
            if selected_folder_paths and folder_path not in selected_folder_paths:
                continue
            match = re.search(r'(?i)(?:^|[ _.-])(?:t(?:ome)?|vol(?:ume)?)[ _.-]*(\d{1,3})(?:$|[ _.-])', folder)
            if not match:
                match = re.match(r'^(\d{1,3})(?:$|[ _.-])', folder)
            tome = int(match.group(1)) if match else None
            label = f"{base} - #{tome:02d}" if match_tome_numbers and tome is not None else f"{base} - {folder}"
            safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', label).strip(' .')
            out = Path(output_dir) / f"{safe}.cbz"
            suffix = 2
            while str(out) in used or out.exists():
                out = Path(output_dir) / f"{safe} ({suffix}).cbz"; suffix += 1
            used.add(str(out))
            with zipfile.ZipFile(out, 'w', compression=zipfile.ZIP_DEFLATED) as dest:
                for name in names:
                    rel = Path(*Path(name).parts[len(common)+1:]) if folder != '__root__' else Path(name)
                    dest.writestr(str(rel), zf.read(name))
            created.append({'path': str(out), 'folder': folder_path or folder, 'tome_number': tome, 'file_count': len(names)})
        return created
