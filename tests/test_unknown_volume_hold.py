import json
import os
import sqlite3
import tempfile
import unittest

from flask import Flask

from blueprints.library import routes


class UnknownVolumeHoldTest(unittest.TestCase):
    """Un numéro de tome absent de la série n\x27est jamais importé automatiquement."""

    def setUp(self):
        self.db = tempfile.NamedTemporaryFile(suffix=".db")
        self.app = Flask(__name__)
        self.app.config["DATABASE"] = self.db.name
        self.ctx = self.app.app_context()
        self.ctx.push()
        conn = sqlite3.connect(self.db.name)
        conn.execute("CREATE TABLE series (id INTEGER PRIMARY KEY, title TEXT, bedetheque_albums TEXT)")
        conn.execute("CREATE TABLE volumes (id INTEGER PRIMARY KEY, series_id INTEGER, volume_number INTEGER, filepath TEXT)")
        albums = [{"number": n, "title": f"Album {n}"} for n in range(1, 9)]
        conn.execute("INSERT INTO series VALUES (1, \x27Sambre\x27, ?)", (json.dumps(albums),))
        conn.execute("INSERT INTO series VALUES (2, \x27Sans catalogue\x27, NULL)")
        conn.execute("INSERT INTO volumes VALUES (1, 1, 20, NULL)")
        conn.execute("INSERT INTO volumes VALUES (2, 1, 13, \x27/x.cbz\x27)")
        conn.commit()
        conn.close()

    def tearDown(self):
        self.ctx.pop()
        self.db.close()

    def reason(self, parsed, series_id=1, **destination):
        return routes._unknown_volume_hold_reason(parsed, {"series_id": series_id, "series_title": "Sambre", **destination})

    def test_number_missing_from_the_series_is_held(self):
        reason = self.reason({"volume": 9})
        self.assertIn("le tome 9 n\x27existe pas", reason)
        self.assertIn("1, 2, 3, 4, 5, 6, 7, 8, 20", reason)

    def test_owned_file_without_catalog_entry_does_not_make_its_number_known(self):
        self.assertIsNotNone(self.reason({"volume": 13}))

    def test_known_numbers_and_placeholders_pass(self):
        self.assertIsNone(self.reason({"volume": 5}))
        self.assertIsNone(self.reason({"volume": 20}))

    def test_special_types_series_without_catalog_and_new_series_are_not_judged(self):
        self.assertIsNone(self.reason({"volume": 9, "is_hs": True}))
        self.assertIsNone(self.reason({"volume": 9, "is_integral": True}))
        self.assertIsNone(self.reason({"volume": 9}, series_id=2))
        self.assertIsNone(self.reason({"volume": 9}, is_new_series=True))
        self.assertIsNone(self.reason({"volume": None}))
