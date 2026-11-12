"""Historique de lecture : lecture des CSV, correspondance des tomes, import sans recul."""
import sqlite3
import unittest
from types import SimpleNamespace
from unittest import mock

from blueprints.library import reading_history as rh


def _volumes_db():
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    conn.executescript('''
        CREATE TABLE series (id INTEGER PRIMARY KEY, title TEXT);
        CREATE TABLE volumes (id INTEGER PRIMARY KEY, series_id INTEGER, filename TEXT, volume_number REAL,
                              is_integral INTEGER DEFAULT 0, is_hs INTEGER DEFAULT 0, komga_book_id TEXT, page_count INTEGER);
        INSERT INTO series VALUES (1, 'Alix'), (2, 'Blake et Mortimer');
        INSERT INTO volumes VALUES (10, 1, 'Alix - #01.cbz', 1, 0, 0, 'K1', 48),
                                   (11, 1, 'Alix - #02.cbz', 2, 0, 0, NULL, 48),
                                   (20, 2, 'BM - #01.cbz', 1, 0, 0, NULL, 64);
    ''')
    return conn


class ReadingCsvTest(unittest.TestCase):
    def test_parse_semicolon_with_bom(self):
        rows = rh.parse_csv('﻿Serie;Tome;Page;Lu\r\nAlix;Tome 1;12;non\r\n'.encode('utf-8'))
        self.assertEqual(rows, [{'serie': 'Alix', 'tome': 'Tome 1', 'page': '12', 'lu': 'non'}])

    def test_parse_comma(self):
        rows = rh.parse_csv('serie,page,lu\n"Blake, Mortimer",3,oui\n')
        self.assertEqual(rows[0]['serie'], 'Blake, Mortimer')
        self.assertEqual(rows[0]['lu'], 'oui')

    def test_volume_label(self):
        base = {'is_integral': 0, 'integral_number': None, 'is_hs': 0, 'hs_number': None, 'volume_number': None, 'filename': 'x.cbz'}
        self.assertEqual(rh.volume_label({**base, 'volume_number': 3.0}), 'Tome 3')
        self.assertEqual(rh.volume_label({**base, 'volume_number': 3.5}), 'Tome 3,5')
        self.assertEqual(rh.volume_label({**base, 'is_integral': 1, 'integral_number': 2}), 'Intégrale 2')
        self.assertEqual(rh.volume_label({**base, 'is_hs': 1}), 'HS')
        self.assertEqual(rh.volume_label(base), 'x')

    def test_match_by_komga_file_then_number(self):
        conn = _volumes_db()
        rows = [{'komga_book_id': 'K1', 'serie': 'autre'},
                {'serie': 'alix', 'fichier': 'Alix - #02.cbz'},
                {'serie': 'Blake  et Mortimer', 'numero': '1'},
                {'serie': 'Inconnue', 'numero': '1'}]
        self.assertEqual([v['id'] if v else None for v in rh.match_volumes(conn, rows)], [10, 11, 20, None])


class ReadingImportTest(unittest.TestCase):
    def _run(self, rows, current):
        source = SimpleNamespace(kind='local', sql='', params=())
        writes = []
        with mock.patch.object(rh, 'reading_rows', return_value=current), \
             mock.patch.object(rh, '_db', side_effect=_volumes_db), \
             mock.patch.object(rh, '_write_progress', side_effect=lambda s, u, v, p, c, d: writes.append((v['id'], p, c, d))):
            stats = rh.import_reading_rows(source, 'user', rows)
        return stats, writes

    def test_never_moves_backwards(self):
        current = [{'volume_id': 10, 'page': 30, 'completed': 0}, {'volume_id': 11, 'page': 48, 'completed': 1}]
        rows = [{'komga_book_id': 'K1', 'page': '12', 'lu': 'non'},          # 12 < 30 : ignoré
                {'serie': 'Alix', 'fichier': 'Alix - #02.cbz', 'page': '5'},  # déjà lu : ignoré
                {'serie': 'Blake et Mortimer', 'numero': '1', 'lu': 'oui', 'date_lecture': '2026-01-02 10:00:00'},
                {'serie': 'Inconnue', 'numero': '4', 'page': '3'}]
        stats, writes = self._run(rows, current)
        self.assertEqual(writes, [(20, 64, True, '2026-01-02 10:00:00')])
        self.assertEqual((stats['applied'], stats['unchanged'], stats['unmatched']), (1, 2, 1))

    def test_page_clamped_and_progress_forward(self):
        stats, writes = self._run([{'komga_book_id': 'K1', 'page': '99', 'lu': 'non'}], [{'volume_id': 10, 'page': 4, 'completed': 0}])
        self.assertEqual(writes, [(10, 48, False, None)])
        self.assertEqual(stats['applied'], 1)


if __name__ == '__main__':
    unittest.main()
