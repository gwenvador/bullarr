"""Safe, explicit conversion for archives whose filename extension is misleading."""
import os
import re
import tarfile
import tempfile
import zipfile

_IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp'}
_MAX_MEMBERS = 5000
_MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
_MAX_MEMBER_BYTES = 500 * 1024 * 1024
_CHUNK = 1024 * 1024


def classify_archive(path):
    if zipfile.is_zipfile(path):
        return 'zip'
    if tarfile.is_tarfile(path):
        return 'tar'
    with open(path, 'rb') as fh:
        magic = fh.read(7)
    if magic.startswith(b'Rar!'):
        return 'rar'
    return 'unknown'


def _output_path(source):
    base, _ = os.path.splitext(source)
    return base + '.converted.cbz'


def _safe_member_name(name, seen):
    if not isinstance(name, str) or not name or '\x00' in name:
        raise ValueError('Nom de membre d archive invalide')
    if '\\' in name or name.startswith('/') or re.match(r'^[A-Za-z]:', name):
        raise ValueError(f'Chemin d archive non portable: {name}')
    parts = name.split('/')
    if any(part in ('', '.', '..') for part in parts):
        raise ValueError(f'Chemin d archive dangereux: {name}')
    if name in seen:
        raise ValueError(f'Membre d archive dupliqué: {name}')
    seen.add(name)
    return name


def _write_members_to_cbz(members, output):
    seen = set()
    image_count = 0
    total_bytes = 0
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as dest:
        for name, stream in members:
            safe_name = _safe_member_name(name, seen)
            if os.path.splitext(safe_name)[1].lower() not in _IMAGE_EXTENSIONS:
                stream.close()
                continue
            image_count += 1
            written = 0
            with dest.open(safe_name, 'w') as target:
                while True:
                    chunk = stream.read(_CHUNK)
                    if not chunk:
                        break
                    written += len(chunk)
                    total_bytes += len(chunk)
                    if written > _MAX_MEMBER_BYTES or total_bytes > _MAX_TOTAL_BYTES:
                        raise ValueError('Archive trop volumineuse pour une conversion sûre')
                    target.write(chunk)
            stream.close()
    if image_count == 0:
        raise ValueError('L archive ne contient aucune image reconnue')


def convert_mislabeled_archive_to_cbz(source):
    """Create a valid CBZ beside a misleading source, preserving the source."""
    kind = classify_archive(source)
    if kind == 'zip':
        raise ValueError('Le fichier est déjà un ZIP/CBZ valide')
    if kind not in ('rar', 'tar'):
        raise ValueError('Format d archive non reconnu')

    output = _output_path(source)
    directory = os.path.dirname(source) or '.'
    if os.path.exists(output):
        raise FileExistsError(output)
    fd, temp = tempfile.mkstemp(prefix='.cbz-convert-', suffix='.tmp', dir=directory)
    os.close(fd)
    try:
        if kind == 'tar':
            with tarfile.open(source, 'r:*') as archive:
                members = []
                for member in archive.getmembers():
                    if not member.isfile():
                        if member.issym() or member.islnk():
                            raise ValueError('Archive contenant un lien refusée')
                        continue
                    stream = archive.extractfile(member)
                    if stream is not None:
                        members.append((member.name, stream))
                if len(members) > _MAX_MEMBERS:
                    raise ValueError('Archive contenant trop de fichiers')
                _write_members_to_cbz(members, temp)
        else:
            import rarfile
            with rarfile.RarFile(source) as archive:
                archive.testrar()
                infos = [m for m in archive.infolist() if not m.isdir()]
                if len(infos) > _MAX_MEMBERS:
                    raise ValueError('Archive contenant trop de fichiers')
                _write_members_to_cbz(((m.filename, archive.open(m)) for m in infos), temp)
        with zipfile.ZipFile(temp) as check:
            if check.testzip() is not None:
                raise ValueError('Le CBZ produit est invalide')
        # Publication sans écraser un fichier créé en parallèle.
        os.link(temp, output)
        os.unlink(temp)
        return output
    finally:
        if os.path.exists(temp):
            os.remove(temp)
