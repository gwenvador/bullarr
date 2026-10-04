import sqlite3

from blueprints.bedetheque.scraper import _main_album_total, _numbered_special_label, match_bedetheque_volume
from blueprints.bedetheque.routes import _classify_bedetheque_album
from blueprints.library.routes import _find_existing_volume_for_import


MAIN = {'number': 4, 'bis_suffix': None, 'title': "Vivons heureux sans en avoir l'air", 'pages': '54'}
SUP = {'number': 4, 'bis_suffix': 'SUP', 'title': 'Monsieur Jean Comics', 'pages': '28'}
TL = {'number': 4, 'bis_suffix': 'TL', 'title': 'Le disque perdu de monsieur jean', 'pages': '8'}


def test_monsieur_jean_extras_are_specials_but_regular_luxury_editions_are_not():
    assert _numbered_special_label(MAIN) is None
    assert _numbered_special_label(SUP) == '4 SUP'
    assert _numbered_special_label(TL) == '4 TL'
    assert _classify_bedetheque_album(MAIN, False)[0] == 4
    for album, label in ((SUP, '4 SUP'), (TL, '4 TL')):
        classified = _classify_bedetheque_album(album, False)
        assert classified[0] is None
        assert classified[7:] == (True, label)
    assert _numbered_special_label({'number': 4, 'bis_suffix': 'TL', 'pages': '60'}) is None
    assert _numbered_special_label({'number': 13, 'bis_suffix': 'Bis', 'pages': '48'}) is None


def test_specials_match_their_own_bedetheque_record():
    albums = [MAIN, SUP, TL]
    assert match_bedetheque_volume(albums, {'volume_number': 4}) is MAIN
    assert match_bedetheque_volume(albums, {'is_special': True, 'special_label': '4 SUP'}) is SUP
    assert match_bedetheque_volume(albums, {'is_special': True, 'special_label': '4 TL'}) is TL


def test_supplements_do_not_inflate_the_main_album_total():
    albums = [{'number': number, 'bis_suffix': None} for number in range(1, 8)] + [SUP, TL]
    assert _main_album_total({'total_volumes': 9, 'volumes': albums}) == 7
    assert _main_album_total({'total_volumes': 12, 'volumes': albums}) == 12


def test_regular_import_uses_main_tome_placeholder_even_if_variant_has_higher_id():
    conn = sqlite3.connect(':memory:')
    conn.execute('''CREATE TABLE volumes (
        id INTEGER PRIMARY KEY, series_id INTEGER, volume_number INTEGER, is_bis INTEGER,
        filepath TEXT, file_size INTEGER, format TEXT, is_episode INTEGER, episode_number INTEGER
    )''')
    conn.executemany('''INSERT INTO volumes
        (id, series_id, volume_number, is_bis, filepath, file_size, format, is_episode)
        VALUES (?, 1, 4, ?, NULL, 0, NULL, 0)''', [(10, 0), (11, 1)])
    assert _find_existing_volume_for_import(conn.cursor(), 1, {'volume': 4})[0] == 10
    conn.close()
