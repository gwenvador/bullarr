#!/usr/bin/env python3
"""Recopie la documentation de l'application dans docs.html du site (branche gh-pages).

La documentation a une source unique, templates/partials/guide.html, affichée dans
l'application sur /docs. Le site la contient entre les marqueurs
<!-- guide:start --> et <!-- guide:end --> ; ce script remplace ce qui s'y trouve.

Usage :
    git worktree add ../bullarr-site gh-pages
    python scripts/sync_site_docs.py ../bullarr-site/docs.html
"""
import sys
from pathlib import Path

START, END = '<!-- guide:start -->', '<!-- guide:end -->'
GUIDE = Path(__file__).resolve().parents[1] / 'templates' / 'partials' / 'guide.html'


def sync(site_docs: Path) -> bool:
    """Remplace le contenu entre les marqueurs. Renvoie True si le fichier a changé."""
    page = site_docs.read_text(encoding='utf-8')
    start, end = page.find(START), page.find(END)
    if start < 0 or end < start:
        raise SystemExit(f'{site_docs} : marqueurs {START} / {END} introuvables')
    new = page[:start + len(START)] + '\n' + GUIDE.read_text(encoding='utf-8').strip() + '\n' + page[end:]
    if new == page:
        return False
    site_docs.write_text(new, encoding='utf-8')
    return True


if __name__ == '__main__':
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    print('docs.html mis à jour' if sync(Path(sys.argv[1])) else 'docs.html déjà à jour')
