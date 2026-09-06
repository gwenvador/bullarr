"""
Conversion optionnelle pdf -> cbz (rendu de chaque page en image + réarchivage ZIP),
alternative explicite à l'import direct d'un pdf (déjà accepté tel quel côté import, voir
monitored_extensions/FORMAT_PRIORITY dans blueprints/library/routes.py - mais aucune
écriture de ComicInfo.xml n'est jamais possible sur un pdf, voir WRITABLE_FORMATS dans
comicinfo_writer.py). Jamais automatique: appelée uniquement à la demande explicite de
l'utilisateur depuis /import (voir POST /api/import/convert), même principe que
cbr_converter.convert_cbr_to_cbz (à qui ce module emprunte son schéma
tmp-fichier + vérification + bascule atomique).
"""
import os
import tempfile
import logging
import fitz  # PyMuPDF
from zipfile import ZipFile, ZIP_DEFLATED

logger = logging.getLogger(__name__)

# Résolution de rendu des pages - 200 dpi est un compromis lisibilité/poids raisonnable
# pour une page de bande dessinée scannée (un rendu 300+ dpi produirait des cbz
# démesurés pour un gain de netteté imperceptible dans un lecteur à l'écran)
RENDER_DPI = 200

# Qualité JPEG des pages rendues - les planches de bande dessinée n'ont pas besoin d'un
# PNG sans perte (fichiers 3-5x plus lourds pour un gain visuel négligeable une fois
# affichées dans un lecteur)
JPEG_QUALITY = 90


class PdfConversionError(Exception):
    """Levée quand la conversion pdf -> cbz échoue (pdf corrompu/protégé par mot de
    passe, cbz cible déjà existant...). Le fichier .pdf d'origine n'est jamais modifié
    dans ce cas."""
    pass


# Couverture minimale (proportion de la page) que doit occuper l'unique image d'une page
# pour la considérer comme "une page scannée" plutôt qu'une illustration décorative sur
# une page par ailleurs textuelle/vectorielle - une bande scannée remplit toujours la
# quasi-totalité de la page, une icône ou un bandeau ne devrait jamais déclencher le
# repli sur l'image brute à la place d'un rendu complet de la page.
FULL_PAGE_IMAGE_MIN_COVERAGE = 0.9


def _extract_full_page_image(doc, page):
    """"if the pdf is 50mb i expect the cbz to be the same" - une page de BD scannée est
    presque toujours UNE SEULE image occupant toute la page, jamais du texte/vectoriel
    réel. Re-rendre cette page en pixmap PUIS la ré-encoder en JPEG (l'ancien
    comportement, seul chemin avant ce correctif) transcode une deuxième fois une image
    déjà compressée - mesuré en pratique: même sur un cas déjà propre, le rendu+ré-
    encodage produit ~14% de plus que l'image d'origine, et un PDF dont le MediaBox
    déclaré est plus grand que le contenu réel amplifie encore l'écart (constaté: un pdf
    de 70 Mo devenu 600 Mo). Extrait directement les octets de l'image telle
    qu'embarquée dans le PDF quand la page n'en contient qu'UNE SEULE couvrant au moins
    FULL_PAGE_IMAGE_MIN_COVERAGE de la page - taille et qualité IDENTIQUES à la source,
    aucun transcodage. Retourne (bytes, extension) ou (None, None) si cette page n'est
    pas ce cas simple (plusieurs images, aucune, ou une image qui ne couvre qu'une partie
    de la page - texte/mise en page réels, illustration décorative...), auquel cas
    l'appelant retombe sur le rendu pixmap classique."""
    try:
        images = page.get_images(full=True)
        if len(images) != 1:
            return None, None
        xref = images[0][0]
        bbox = page.get_image_bbox(images[0])
        page_area = page.rect.get_area()
        if not page_area or (bbox.get_area() / page_area) < FULL_PAGE_IMAGE_MIN_COVERAGE:
            return None, None
        info = doc.extract_image(xref)
        image_bytes = info.get('image')
        ext = info.get('ext')
        if not image_bytes or not ext:
            return None, None
        return image_bytes, ext
    except Exception:
        # Best-effort: n'importe quel souci d'extraction retombe silencieusement sur le
        # rendu pixmap classique plutôt que de faire échouer toute la conversion pour
        # UNE page qui ne s'y prêtait pas.
        return None, None


def convert_pdf_to_cbz(filepath):
    """
    Convertit le pdf à filepath en cbz (même dossier, même nom de base, extension
    .cbz): rend chaque page en image JPEG (voir RENDER_DPI/JPEG_QUALITY) puis empaquette
    les pages en zip, nommées séquentiellement (page_0001.jpg, page_0002.jpg...) avec
    un padding calé sur le nombre total de pages pour trier correctement dans n'importe
    quel lecteur même au-delà de 9999 pages.

    Écrit d'abord dans un fichier temporaire du même répertoire, vérifie son intégrité
    (testzip + nombre de membres = nombre de pages), puis seulement alors: refuse
    d'écraser un .cbz cible déjà existant, sinon bascule le temporaire en place avec
    os.replace et supprime le .pdf d'origine - uniquement une fois le .cbz confirmé
    valide sur disque (même garantie que cbr_converter.convert_cbr_to_cbz: jamais de
    volume laissé dans un état incomplet).

    Returns:
        str: chemin du nouveau fichier .cbz

    Raises:
        PdfConversionError: cbz cible déjà existant, pdf illisible/corrompu/protégé par
            mot de passe, ou vérification de l'archive produite en échec
    """
    directory = os.path.dirname(filepath) or '.'
    base, _ext = os.path.splitext(filepath)
    tmp_path = None

    try:
        source_writable = os.access(directory, os.W_OK)
    except OSError:
        source_writable = False
    output_dir = directory if source_writable else tempfile.mkdtemp(prefix='.pdfconv_ro_')
    new_cbz_path = os.path.join(output_dir, os.path.basename(base) + '.cbz')

    try:
        try:
            doc = fitz.open(filepath)
        except Exception as e:
            raise PdfConversionError(f"PDF illisible ou corrompu: {e}")

        with doc:
            if doc.is_encrypted:
                # authenticate('') réussit si le pdf n'a en réalité qu'un mot de passe
                # "propriétaire" (restriction d'impression/copie, pas d'ouverture) -
                # authenticate() renvoie 0 (falsy) si le mot de passe vide est refusé,
                # càd un pdf réellement protégé par un vrai mot de passe d'ouverture
                if not doc.authenticate(''):
                    raise PdfConversionError("PDF protégé par mot de passe, conversion impossible")

            page_count = doc.page_count
            if page_count == 0:
                raise PdfConversionError("PDF vide (aucune page)")

            # Padding minimum de 4 chiffres (convention courante des packs cbz), élargi
            # si le pdf a lui-même plus de 9999 pages
            width = max(4, len(str(page_count)))
            zoom = RENDER_DPI / 72
            matrix = fitz.Matrix(zoom, zoom)

            fd, tmp_path = tempfile.mkstemp(prefix='.pdfconv_', suffix='.tmp', dir=output_dir)
            os.close(fd)
            # tempfile.mkstemp crée le fichier en 0600 (propriétaire seul) par défaut -
            # os.replace plus bas conserve ce mode sur le .cbz final, jamais remis à un
            # mode plus ouvert ensuite. Repassé ici en 0644 (mêmes droits que n'importe
            # quel autre fichier de la bibliothèque) - constaté en réel: un outil tiers
            # qui surveille aussi ce répertoire (Syncthing) ne pouvait ni lire le fichier
            # temporaire en cours d'écriture ni le .cbz produit ("permission denied").
            try:
                os.chmod(tmp_path, 0o644)
            except OSError:
                pass

            try:
                with ZipFile(tmp_path, 'w', compression=ZIP_DEFLATED) as zout:
                    for i in range(page_count):
                        page = doc.load_page(i)

                        image_bytes, image_ext = _extract_full_page_image(doc, page)
                        if image_bytes is None:
                            pix = page.get_pixmap(matrix=matrix)
                            image_bytes = pix.tobytes('jpg', jpg_quality=JPEG_QUALITY)
                            image_ext = 'jpg'
                            pix = None

                        page_name = f"page_{i + 1:0{width}d}.{image_ext}"
                        zout.writestr(page_name, image_bytes)

                        # "convert to pdf is very memory intensive... my application is
                        # unusable" - PyMuPDF garde en interne un cache mémoire (polices/
                        # images décodées) qui grossit au fil des pages traitées et n'est
                        # PAS libéré par le simple fait que `page`/`pix` sortent de portée
                        # (comportement documenté de la bibliothèque, pas un bug de ce
                        # fichier) - chaque planche d'une BD scannée étant une image
                        # PROPRE à sa page (jamais réutilisée d'une page à l'autre), ce
                        # cache n'apporte ici aucun bénéfice de réutilisation, seulement
                        # une accumulation continue proportionnelle au nombre de pages
                        # déjà traitées. Vidé après CHAQUE page plutôt que périodiquement:
                        # sans intérêt à le garder ne serait-ce qu'une page de plus vu
                        # l'absence totale de réutilisation, et ça plafonne la mémoire au
                        # coût d'UNE SEULE page quelle que soit la taille du document (un
                        # gros scan de plusieurs centaines de pages ne coûte alors pas
                        # plus qu'un petit).
                        pix = None
                        page = None
                        fitz.TOOLS.store_shrink(100)
            except Exception as e:
                raise PdfConversionError(f"Erreur lors du rendu/écriture des pages: {e}")

        # Vérifie l'archive produite avant de toucher quoi que ce soit d'existant: intégrité
        # zip + autant de membres que de pages sources
        with ZipFile(tmp_path, 'r') as check:
            bad_file = check.testzip()
            if bad_file is not None:
                raise PdfConversionError(f"Archive cbz produite corrompue (membre invalide: {bad_file})")
            if len(check.namelist()) != page_count:
                raise PdfConversionError(
                    "Archive cbz produite incomplète (le nombre de pages ne correspond pas au pdf source)"
                )

        # Cas rare mais réel: un .cbz du même nom existe déjà à côté du .pdf -> on
        # n'écrase jamais un fichier existant, même si la conversion a réussi
        if os.path.exists(new_cbz_path):
            raise PdfConversionError(f"Le fichier cible {new_cbz_path} existe déjà, conversion annulée")

        os.replace(tmp_path, new_cbz_path)
        tmp_path = None

        # Le .cbz est confirmé en place et valide: on peut maintenant supprimer le .pdf
        # d'origine sans jamais laisser le fichier dans un état incomplet - sauf source
        # non-inscriptible (voir plus haut: le .pdf d'origine reste intact).
        if source_writable:
            os.remove(filepath)

        return new_cbz_path

    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)
