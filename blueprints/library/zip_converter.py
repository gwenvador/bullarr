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
