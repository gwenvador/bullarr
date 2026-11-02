"""Technical rationale and compatibility constraints for this code path."""
import zipfile
import rarfile
import os
import tarfile
from pathlib import Path


_TAR_COMPRESSION_MAGICS = (b'\x1f\x8b', b'BZh', b'\xfd7zXZ\x00')


def looks_like_tar(filepath):
    """Vrai si l'en-tête annonce un tar (« ustar ») ou une compression connue. tarfile.is_tarfile seul prend un
    fichier rempli d'octets nuls (copie inachevée) pour une archive tar vide et relit tout fichier compressé."""
    try:
        with open(filepath, 'rb') as handle:
            head = handle.read(512)
    except OSError:
        return False
    return head[257:262] == b'ustar' or any(head.startswith(magic) for magic in _TAR_COMPRESSION_MAGICS)


def has_archive_signature(filepath):
    """Vrai si le fichier est une archive lisible (ZIP, RAR ou tar). Une copie encore en cours (Syncthing crée le
    fichier à sa taille finale, rempli de zéros, puis écrit le contenu par blocs) n'a pas encore son annuaire
    central en fin de fichier: elle n'est reconnue comme aucune archive."""
    try:
        return bool(zipfile.is_zipfile(filepath) or rarfile.is_rarfile(filepath)
                    or (looks_like_tar(filepath) and tarfile.is_tarfile(filepath)))
    except (OSError, rarfile.Error):
        return False


def is_blank_file(filepath, sample_size=4096):
    """Vrai si le début du fichier n'est que du vide (octets nuls) ou si le fichier est vide: copie inachevée
    (fichier préalloué) ou fichier corrompu, jamais une archive exploitable."""
    try:
        with open(filepath, 'rb') as handle:
            head = handle.read(sample_size)
    except OSError:
        return False
    return not head.strip(b'\x00')


def detect_actual_format(filepath, declared_format):
    """Renvoie le format RÉEL ('cbz' ou 'cbr') d'un fichier archive, en se fiant au
    contenu binaire quand il contredit le format déclaré. Retombe sur declared_format
    normalisé (minuscule) si rien ne le contredit - fichier absent, format non
    concerné (pdf...), ou format déclaré déjà correct."""
    fmt = (declared_format or '').lower()
    if fmt in ('cbr', 'rar') and zipfile.is_zipfile(filepath):
        return 'cbz'
    if fmt in ('cbz', 'zip') and rarfile.is_rarfile(filepath):
        return 'cbr'
    if fmt in ('cbz', 'zip') and looks_like_tar(filepath) and tarfile.is_tarfile(filepath):
        return 'tar'
    return fmt



def list_archive_members(filepath, limit=5000):
    filepath = os.fspath(filepath)
    suffix = Path(filepath).suffix.lower()
    if suffix in (".tar", ".gz", ".tgz", ".bz2", ".xz", ".zst") and tarfile.is_tarfile(filepath):
        with tarfile.open(filepath, "r:*") as archive:
            entries = [{"path": m.name.rstrip("/") or m.name, "kind": "directory" if m.isdir() else "file" if m.isfile() else "other", "size": m.size if m.isfile() else 0} for m in archive.getmembers()]
        fmt = "tar"
    elif zipfile.is_zipfile(filepath):
        with zipfile.ZipFile(filepath) as archive:
            entries = [{"path": i.filename.rstrip("/") or i.filename, "kind": "directory" if i.is_dir() else "file", "size": 0 if i.is_dir() else i.file_size} for i in archive.infolist()]
        fmt = "zip"
    elif rarfile.is_rarfile(filepath):
        with rarfile.RarFile(filepath) as archive:
            entries = [{"path": i.filename.rstrip("/") or i.filename, "kind": "directory" if i.isdir() else "file", "size": 0 if i.isdir() else i.file_size} for i in archive.infolist()]
        fmt = "rar"
    elif tarfile.is_tarfile(filepath):
        with tarfile.open(filepath, "r:*") as archive:
            entries = [{"path": m.name.rstrip("/") or m.name, "kind": "directory" if m.isdir() else "file" if m.isfile() else "other", "size": m.size if m.isfile() else 0} for m in archive.getmembers()]
        fmt = "tar"
    else:
        raise ValueError("Format d archive non supporté ou archive invalide")
    return {"format": fmt, "entries": entries[:limit], "total_count": len(entries), "truncated": len(entries) > limit}
