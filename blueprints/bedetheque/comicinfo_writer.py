"""
Écriture de métadonnées Bedetheque dans le ComicInfo.xml embarqué d'un volume.

Seul le format cbz est supporté en écriture: un cbr est une archive RAR, et rarfile
(déjà utilisé en lecture ailleurs dans l'app) ne sait pas écrire de RAR - il n'existe
pas de bibliothèque Python libre pour ça. Les cbr/pdf sont donc ignorés par défaut
(voir UnsupportedFormatError), sans conversion ni contournement automatique. Un cbr peut
toutefois être converti en cbz à la demande explicite de l'utilisateur (voir
cbr_converter.convert_cbr_to_cbz, appelé par les routes update-metadata avec
convert_cbr=true) avant d'être traité ici normalement.

Le format des champs écrits reste compatible avec LibraryScanner.read_comicinfo (mêmes
noms de balises que COMICINFO_FIELDS, mêmes règles "que les champs non vides").
"""
import os
import json
import sqlite3
import tempfile
import logging
from zipfile import ZipFile, ZIP_DEFLATED
from xml.etree.ElementTree import Element, SubElement, tostring
import defusedxml.ElementTree as ET

logger = logging.getLogger(__name__)

# Formats d'archive pour lesquels l'écriture est possible (cbz = zip renommé)
WRITABLE_FORMATS = ('cbz', 'zip')


class UnsupportedFormatError(Exception):
    """Levée quand le format du volume ne permet pas l'écriture du ComicInfo.xml (cbr, pdf...)"""
    pass


def derive_author_year_from_comicinfo(comicinfo_dict):
    """"all the data from an album is taken from bedetheque no need to do any matching
    or guessing here. only the releaser/quality are not from bedetheque" -
    volumes.author/year ne sont QUE le reflet direct de comicinfo.writer/penciller/
    colorist/year, jamais une devinette depuis le nom de fichier: seule fonction qui
    calcule ces deux valeurs, réutilisée par apply_volume_comicinfo (Bédéthèque/édition
    manuelle) ET par le chemin d'import qui écrit directement ces colonnes
    (_execute_import_batch, routes.py) - "ONE parser" (CLAUDE.md), pas une seconde
    implémentation qui pourrait diverger.

    author combine writer/penciller/colorist dédupliqués par valeur exacte (un même nom
    répété entre rôles, ex. penciller ET colorist tous deux "Roger (1)", ne compte
    qu'une fois) plutôt qu'un seul rôle: la BD franco-belge sépare couramment scénario
    et dessin entre deux personnes différentes, perdre l'un des deux serait pire qu'une
    chaîne un peu plus longue.

    Retourne (author_value, year_value) - author_value: str ou None. year_value: int ou
    None (comicinfo.year est toujours une chaîne, voir build_comicinfo_fields)."""
    ci = comicinfo_dict or {}
    author_value = ', '.join(dict.fromkeys(
        v for v in (ci.get('writer'), ci.get('penciller'), ci.get('colorist')) if v
    )) or None
    try:
        year_value = int(ci['year']) if ci.get('year') else None
    except (TypeError, ValueError):
        year_value = None
    return author_value, year_value


def build_comicinfo_fields(local_volume_number, series_title, series_info, bedetheque_volume):
    """
    Construit le dict des champs ComicInfo.xml (clés = noms de balises) à écrire pour un
    volume, à partir des infos Bedetheque. Ne renvoie que les champs pour lesquels on a
    une valeur non vide: on n'écrase jamais un champ existant avec du vide.

    Args:
        local_volume_number: volumes.volume_number local (peut être None: hors-série/one-shot)
        series_title: titre de la série (local)
        series_info: dict série retourné par BedethequeScraper.get_series_info
        bedetheque_volume: dict du volume Bedetheque matché par numéro (ou None si aucun match)

    Returns:
        dict {NomDeBalise: valeur str}
    """
    series_info = series_info or {}
    bd_vol = bedetheque_volume or {}
    fields = {}

    if series_title:
        fields['Series'] = series_title

    if local_volume_number is not None:
        fields['Number'] = str(local_volume_number)

    if bd_vol.get('title'):
        fields['Title'] = bd_vol['title']

    # Le résumé propre à l'album (renseigné par les routes via fetch_album_description)
    # prime sur celui de la série, souvent absent ou identique pour tous les tomes
    summary = bd_vol.get('description') or series_info.get('description')
    if summary:
        fields['Summary'] = summary

    writer = bd_vol.get('scenario') or ', '.join(series_info.get('scenaristes') or []) or None
    if writer:
        fields['Writer'] = writer

    penciller = bd_vol.get('dessin') or ', '.join(series_info.get('dessinateurs') or []) or None
    if penciller:
        fields['Penciller'] = penciller

    if bd_vol.get('couleurs'):
        fields['Colorist'] = bd_vol['couleurs']

    publisher = bd_vol.get('editeur') or ', '.join(series_info.get('editeurs') or []) or None
    if publisher:
        fields['Publisher'] = publisher

    if series_info.get('genre'):
        fields['Genre'] = series_info['genre']

    web = bd_vol.get('url') or series_info.get('url')
    if web:
        fields['Web'] = web

    date_publication = bd_vol.get('date_publication')
    if date_publication:
        date_parts = date_publication.split('-')
        if len(date_parts) >= 1 and date_parts[0].isdigit():
            fields['Year'] = str(int(date_parts[0]))
        if len(date_parts) >= 2 and date_parts[1].isdigit():
            fields['Month'] = str(int(date_parts[1]))
        if len(date_parts) >= 3 and date_parts[2].isdigit():
            fields['Day'] = str(int(date_parts[2]))

    # Note des lecteurs Bédéthèque ("parse les notes des volumes bedetheque pour
    # recuperer la note / nb review. a ajouter dans les metadatas et dans le tableau
    # volume") - propre à CET album (bd_vol), jamais un repli sur la série: chaque tome a
    # sa propre note, contrairement à résumé/genre/éditeur qui peuvent raisonnablement
    # retomber sur l'info série faute de mieux.
    if bd_vol.get('rating') is not None:
        fields['CommunityRating'] = str(bd_vol['rating'])
    if bd_vol.get('rating_count') is not None:
        fields['CommunityRatingCount'] = str(bd_vol['rating_count'])

    return fields


def write_comicinfo_cbz(filepath, new_fields):
    """
    Fusionne new_fields (dict {NomDeBalise: valeur}) dans le ComicInfo.xml embarqué du
    cbz à filepath, en préservant tous les autres membres de l'archive (octet pour octet)
    et tous les champs déjà présents dans le ComicInfo.xml existant qu'on ne touche pas
    (ex: PageCount, AgeRating positionnés manuellement).

    Écrit dans un fichier temporaire du même répertoire (pour que le remplacement final
    soit un renommage atomique sur le même système de fichiers) puis remplace l'original
    avec os.replace. En cas d'erreur à n'importe quelle étape, le fichier original n'est
    jamais touché (le fichier temporaire est supprimé et l'exception est propagée).
    """
    directory = os.path.dirname(filepath) or '.'
    tmp_path = None

    try:
        with ZipFile(filepath, 'r') as zin:
            comicinfo_name = next((n for n in zin.namelist() if n.lower().endswith('comicinfo.xml')), None)
            existing_xml = zin.read(comicinfo_name) if comicinfo_name else None

            root = None
            if existing_xml:
                try:
                    root = ET.fromstring(existing_xml)
                except Exception as e:
                    logger.warning(f"ComicInfo.xml existant illisible dans {filepath}, remplacé entièrement: {e}")
                    root = None

            if root is None:
                root = Element('ComicInfo', {
                    'xmlns:xsd': 'http://www.w3.org/2001/XMLSchema',
                    'xmlns:xsi': 'http://www.w3.org/2001/XMLSchema-instance'
                })

            # Ne remplace que les balises pour lesquelles on a une nouvelle valeur,
            # toutes les autres (existantes) restent intactes
            for tag, value in new_fields.items():
                el = root.find(tag)
                if el is None:
                    el = SubElement(root, tag)
                el.text = str(value)

            new_xml_bytes = b'<?xml version="1.0" encoding="utf-8"?>\n' + tostring(root, encoding='utf-8')
            member_name = comicinfo_name or 'ComicInfo.xml'

            fd, tmp_path = tempfile.mkstemp(prefix='.comicinfo_', suffix='.tmp', dir=directory)
            os.close(fd)

            with ZipFile(tmp_path, 'w', compression=ZIP_DEFLATED) as zout:
                for item in zin.infolist():
                    if comicinfo_name and item.filename == comicinfo_name:
                        continue
                    zout.writestr(item, zin.read(item.filename))
                zout.writestr(member_name, new_xml_bytes)

        # Vérifie que la nouvelle archive est valide avant d'écraser l'original
        with ZipFile(tmp_path, 'r') as check:
            bad_file = check.testzip()
            if bad_file is not None:
                raise ValueError(f"Archive corrompue produite après réécriture (membre invalide: {bad_file})")

        os.replace(tmp_path, filepath)
        tmp_path = None

    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)


def update_volume_comicinfo(filepath, format_type, new_fields):
    """
    Point d'entrée utilisé par les routes: écrit new_fields dans le ComicInfo.xml du
    volume si le format le permet.

    Raises:
        UnsupportedFormatError: si le format n'est pas cbz (cbr/pdf...)
    """
    format_type = (format_type or '').lower()
    if format_type not in WRITABLE_FORMATS:
        raise UnsupportedFormatError(
            f"Format '{format_type}' non supporté pour l'écriture (seul cbz est modifiable)"
        )
    write_comicinfo_cbz(filepath, new_fields)


def apply_volume_comicinfo(db_path, volume_id, filepath, format_type, new_fields):
    """
    Point d'entrée DB-first pour toute écriture de métadonnées d'un tome (Bédéthèque OU
    édition manuelle): la base est la référence, le ComicInfo.xml du fichier n'est qu'une
    projection de ce qu'elle contient. Principe: Bédéthèque (ou saisie manuelle) ->
    volumes.comicinfo (DB) -> ComicInfo.xml (fichier) - jamais l'inverse. Remplace
    l'ancien enchaînement "écrire le fichier -> le relire -> mettre le résultat en cache
    DB", qui faisait du fichier la source de vérité de fait (la DB ne faisait que
    refléter ce que la relecture du fichier avait réussi à parser).

    new_fields: dict {NomDeBalise: valeur} (mêmes noms que ComicInfo.xml, ex: 'Title',
    'Summary'...) - une valeur vide efface le champ (aussi bien en DB que dans le
    fichier), une clé absente de new_fields laisse le champ existant inchangé.

    Écrit d'abord volumes.comicinfo (toujours, y compris si le format n'est pas
    réinscriptible - la DB doit refléter la métadonnée voulue même si elle ne peut pas
    encore être poussée dans un cbr/pdf), puis tente de la propager dans le fichier
    si le format le permet.

    Retourne le dict comicinfo fusionné (clés en minuscules, format DB - voir
    LibraryScanner.read_comicinfo) tel qu'il est maintenant en base.

    Raises:
        UnsupportedFormatError: si le format ne permet pas d'écrire dans le fichier - la
        DB a déjà été mise à jour à ce stade (elle reste la référence), seule la
        propagation vers le fichier a échoué.
    """
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute('SELECT comicinfo, release_group, author FROM volumes WHERE id = ?', (volume_id,))
    row = cursor.fetchone()
    existing = json.loads(row['comicinfo']) if row and row['comicinfo'] else {}

    merged = dict(existing)
    for tag, value in new_fields.items():
        key = tag.lower()
        if value in (None, ''):
            merged.pop(key, None)
        else:
            merged[key] = value

    if row and row['release_group']:
        author_text = ' '.join(
            merged.get(k, '') for k in ('writer', 'penciller', 'colorist')
        ).strip()
        if author_text:
            from blueprints.bedetheque.scraper import BedethequeScraper
            group_tokens = {
                t for t in BedethequeScraper._normalize_for_match(row['release_group']).split()
                if len(t) >= 3
            }
            author_tokens = set(BedethequeScraper._normalize_for_match(author_text).split())
            if group_tokens and group_tokens.issubset(author_tokens):
                cursor.execute('UPDATE volumes SET release_group = NULL WHERE id = ?', (volume_id,))

    # release_group/resolution restent entièrement en dehors de ce mécanisme, voir le
    # commentaire ci-dessus - author/year, eux, sont dérivés de `merged` (voir
    # derive_author_year_from_comicinfo) que leur source soit Bédéthèque ou une
    # édition manuelle: ce n'est plus une devinette depuis le nom de fichier une fois
    # qu'un writer/penciller réel existe.
    author_value, year_value = derive_author_year_from_comicinfo(merged)
    cursor.execute(
        'UPDATE volumes SET author = ?, year = ? WHERE id = ?',
        (author_value, year_value, volume_id)
    )

    cursor.execute('UPDATE volumes SET comicinfo = ? WHERE id = ?',
                    (json.dumps(merged) if merged else None, volume_id))
    conn.commit()
    conn.close()

    update_volume_comicinfo(filepath, format_type, new_fields)

    return merged
