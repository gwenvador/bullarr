"""
Calcul de l'identifiant qu'un client BitTorrent (qBittorrent/rTorrent/Deluge) utilisera
lui-même pour désigner un téléchargement - "once you add the file to the client ask for
its id so you can put in the db and use it later": aucune de ces API "add" ne renvoie cet
id à l'ajout (qBittorrent répond juste "Ok.", rTorrent ne répond rien d'exploitable), donc
plutôt que d'interroger le client après coup (qui obligerait à re-deviner LEQUEL de ses
torrents vient d'être ajouté), on le calcule nous-mêmes: le hash BitTorrent standard est
une fonction pure du fichier .torrent (SHA1 du dictionnaire bencodé "info") ou déjà présent
tel quel dans un lien magnet (`btih:`) - connu AVANT même que le client ne le voie, avec la
certitude que c'est exactement l'id qu'il rapportera ensuite (voir
find_active_downloads_by_client_item_ids côté missing_monitor/downloader.py).
"""
import hashlib
import re


def extract_magnet_btih(magnet_uri):
    """Hash contenu directement dans un lien magnet (`xt=urn:btih:<hash>`) - hex 40
    caractères ou base32 32 caractères selon la source, normalisé en hex minuscule pour
    correspondre au format que qBittorrent/rTorrent/Deluge rapportent tous. None si le
    lien n'est pas un magnet ou ne contient pas de btih exploitable."""
    match = re.search(r'xt=urn:btih:([A-Za-z0-9]+)', magnet_uri or '')
    if not match:
        return None
    raw = match.group(1)
    if re.fullmatch(r'[0-9A-Fa-f]{40}', raw):
        return raw.lower()
    if re.fullmatch(r'[A-Za-z2-7]{32}', raw):
        try:
            import base64
            return base64.b32decode(raw.upper()).hex()
        except Exception:
            return None
    return None


def _bdecode(data, pos):
    """Décodeur bencode minimal (entier/chaîne d'octets/liste/dictionnaire - les 4 seuls
    types du format) - juste assez pour retrouver le dictionnaire "info" d'un .torrent,
    pas un décodeur bencode généraliste."""
    c = data[pos:pos + 1]
    if c == b'i':
        end = data.index(b'e', pos)
        return int(data[pos + 1:end]), end + 1
    if c == b'l':
        pos += 1
        items = []
        while data[pos:pos + 1] != b'e':
            item, pos = _bdecode(data, pos)
            items.append(item)
        return items, pos + 1
    if c == b'd':
        pos += 1
        result = {}
        while data[pos:pos + 1] != b'e':
            key, pos = _bdecode(data, pos)
            value, pos = _bdecode(data, pos)
            result[key] = value
        return result, pos + 1
    # Chaîne d'octets: "<longueur>:<octets>"
    colon = data.index(b':', pos)
    length = int(data[pos:colon])
    start = colon + 1
    return data[start:start + length], start + length


def _bencode(value):
    """Ré-encodage bencode canonique (clés de dictionnaire triées - imposé par le format
    lui-même, voir la spec BitTorrent) - décoder puis ré-encoder le dict "info" reproduit
    donc exactement ses octets d'origine, la propriété nécessaire pour que le SHA1 calculé
    ici corresponde au hash que le client calculera lui-même."""
    if isinstance(value, int):
        return b'i' + str(value).encode() + b'e'
    if isinstance(value, bytes):
        return str(len(value)).encode() + b':' + value
    if isinstance(value, list):
        return b'l' + b''.join(_bencode(v) for v in value) + b'e'
    if isinstance(value, dict):
        items = sorted(value.items(), key=lambda kv: kv[0])
        return b'd' + b''.join(_bencode(k) + _bencode(v) for k, v in items) + b'e'
    raise TypeError(f"Type non bencodable: {type(value)}")


def compute_torrent_info_hash(torrent_bytes):
    """Hash BitTorrent standard (SHA1 du dict "info" bencodé) d'un fichier .torrent déjà
    téléchargé - hex 40 caractères minuscules, même format que ce que qBittorrent/
    rTorrent/Deluge rapportent. None si le contenu n'est pas un .torrent bencodé valide
    (fichier corrompu/mauvaise réponse HTTP) - un échec ici n'empêche jamais l'ajout au
    client, seul le lien id->ligne active_downloads reste à établir plus tard par
    correspondance de nom (voir mark_download_pending, qui accepte client_item_id=None)."""
    try:
        decoded, _ = _bdecode(torrent_bytes, 0)
        info = decoded[b'info']
        return hashlib.sha1(_bencode(info)).hexdigest()
    except Exception:
        return None
