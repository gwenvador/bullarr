"""
Conversion optionnelle cbr -> cbz (extraction RAR + réarchivage ZIP), pour permettre
ensuite l'écriture du ComicInfo.xml (voir comicinfo_writer.py, qui ne sait écrire que
du cbz). Jamais automatique: appelée uniquement si l'utilisateur l'a explicitement
demandé (flag convert_cbr des routes update-metadata), un cbr non converti reste
simplement ignoré comme avant.
"""
import os
import shutil
import tempfile
import logging
import zipfile
import rarfile
from zipfile import ZipFile, ZIP_DEFLATED

logger = logging.getLogger(__name__)


class CbrConversionError(Exception):
    """Levée quand la conversion cbr -> cbz échoue (rar corrompu, cbz cible déjà
    existant...). Le fichier .cbr d'origine n'est jamais modifié dans ce cas."""
    pass


def convert_cbr_to_cbz(filepath):
    """
    Convertit le cbr à filepath en cbz (même dossier, même nom de base, extension
    .cbz), en préservant tous les membres de l'archive octet pour octet.

    Écrit d'abord dans un fichier temporaire du même répertoire, vérifie son intégrité
    (testzip + comparaison du nombre/des noms de membres avec le rar source), puis
    seulement alors: refuse d'écraser un .cbz cible déjà existant, sinon bascule le
    temporaire en place avec os.replace et supprime le .cbr d'origine - uniquement une
    fois le .cbz confirmé valide sur disque. En cas d'erreur à n'importe quelle étape,
    le fichier temporaire est supprimé et le .cbr d'origine reste intact.

    Returns:
        str: chemin du nouveau fichier .cbz

    Raises:
        CbrConversionError: cbz cible déjà existant, rar illisible/corrompu, ou
            vérification de l'archive produite en échec
    """
    directory = os.path.dirname(filepath) or '.'
    base, _ext = os.path.splitext(filepath)
    tmp_path = None

    try:
        source_writable = os.access(directory, os.W_OK)
    except OSError:
        source_writable = False
    output_dir = directory if source_writable else tempfile.mkdtemp(prefix='.cbrconv_ro_')
    new_cbz_path = os.path.join(output_dir, os.path.basename(base) + '.cbz')

    if zipfile.is_zipfile(filepath):
        if os.path.exists(new_cbz_path):
            raise CbrConversionError(f"Le fichier cible {new_cbz_path} existe déjà, conversion annulée")
        if source_writable:
            os.replace(filepath, new_cbz_path)
        else:
            shutil.copy2(filepath, new_cbz_path)
        return new_cbz_path

    try:
        try:
            rar = rarfile.RarFile(filepath)
        except Exception as e:
            raise CbrConversionError(f"Archive cbr illisible ou corrompue: {e}")

        with rar:
            try:
                rar.testrar()
            except rarfile.Error as e:
                raise CbrConversionError(f"Archive cbr corrompue: {e}")

            members = [m for m in rar.infolist() if not m.isdir()]
            member_names = [m.filename for m in members]

            fd, tmp_path = tempfile.mkstemp(prefix='.cbrconv_', suffix='.tmp', dir=output_dir)
            os.close(fd)
            # Voir même correctif dans pdf_converter.py: tempfile.mkstemp crée le fichier
            # en 0600 par défaut, conservé sur le .cbz final par os.replace - repassé en
            # 0644 pour qu'un outil tiers qui surveille aussi ce répertoire (Syncthing)
            # puisse le lire.
            try:
                os.chmod(tmp_path, 0o644)
            except OSError:
                pass

            try:
                with ZipFile(tmp_path, 'w', compression=ZIP_DEFLATED) as zout:
                    for member in members:
                        data = rar.read(member)
                        zout.writestr(member.filename, data)
            except Exception as e:
                raise CbrConversionError(f"Erreur lors de l'écriture de l'archive cbz: {e}")

        # Vérifie l'archive produite avant de toucher quoi que ce soit d'existant:
        # intégrité zip + même ensemble de membres que la source rar
        with ZipFile(tmp_path, 'r') as check:
            bad_file = check.testzip()
            if bad_file is not None:
                raise CbrConversionError(f"Archive cbz produite corrompue (membre invalide: {bad_file})")
            if sorted(check.namelist()) != sorted(member_names):
                raise CbrConversionError(
                    "Archive cbz produite incomplète (la liste des membres ne correspond pas au cbr source)"
                )

        # Cas rare mais réel: un .cbz du même nom existe déjà à côté du .cbr -> on
        # n'écrase jamais un fichier existant, même si la conversion a réussi
        if os.path.exists(new_cbz_path):
            raise CbrConversionError(f"Le fichier cible {new_cbz_path} existe déjà, conversion annulée")

        os.replace(tmp_path, new_cbz_path)
        tmp_path = None

        # Le .cbz est confirmé en place et valide: on peut maintenant supprimer le
        # .cbr d'origine sans jamais laisser le volume dans un état incomplet - sauf
        # source non-inscriptible (impossible ET non désiré, voir plus haut: le .cbr
        # d'origine reste intact sur le montage read-only).
        if source_writable:
            os.remove(filepath)

        return new_cbz_path

    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)
