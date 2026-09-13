"""
Détection du format d'archive RÉEL d'un fichier cbz/cbr, par contenu plutôt que par la
seule extension/valeur DB déclarée - certains groupes de scan publient une archive ZIP
sous extension .cbr (constaté sur un cas réel, "Nef9.cbr": en-tête PK\\x03\\x04, ZIP
pur, alors que le fichier est nommé/classé cbr partout dans l'app). Sans ce contrôle,
toute lecture qui ouvre le fichier avec rarfile.RarFile() sur la seule foi du format
déclaré échoue immédiatement ("Not a RAR file") - ça percutait à la fois
_check_volume_file_validity (faux positif "corrompu"), get_page_count/read_comicinfo/
extract_volume_cover (silencieusement 0 page / pas de couverture / pas de métadonnées)
et convert_cbr_to_cbz (échec de conversion), pour le même fichier et la même raison.
Un seul point de correction ici plutôt que re-belder cette détection dans chacun.
"""
import zipfile
import rarfile
import os
import tarfile
from pathlib import Path
import os
import tarfile
from pathlib import Path


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
