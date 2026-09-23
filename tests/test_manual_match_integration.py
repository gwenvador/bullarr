import sqlite3
from pathlib import Path
from unittest.mock import patch

from flask import Flask

from blueprints.bedetheque.auto_acquire import (
    _ensure_manual_review_table,
    match_manual_review_series,
)


def test_manual_match_persists_series_album_and_keeps_review_pending(tmp_path):
    db = tmp_path / 'reviews.sqlite'
    conn = sqlite3.connect(db)
    conn.execute('create table series (id integer primary key, title text, bedetheque_url text)')
    conn.execute('insert into series values (1243, ?, ?)', ('(AUT) Juillard', 'https://www.bedetheque.com/serie-4697-BD-AUT-Juillard.html'))
    _ensure_manual_review_table(conn)
    conn.execute(
        'insert into auto_acquire_reviews (series_title, candidates_json) values (?, ?)',
        ('Pêle-Mêle', '[]'),
    )
    conn.commit(); conn.close()

    selected = 'https://www.bedetheque.com/BD-AUT-Juillard-1999-Pele-Mele-Monographie-26901.html'
    info = {
        'title': '(AUT) Juillard',
        'url': 'https://www.bedetheque.com/serie-4697-BD-AUT-Juillard.html',
        'volumes': [{'number': 1999, 'title': 'Pêle Mêle - Monographie', 'url': selected}],
    }
    app = Flask(__name__)
    app.config['DATABASE'] = str(db)
    with app.app_context(), \
         patch('blueprints.bedetheque.scraper.BedethequeScraper') as scraper_cls, \
         patch('blueprints.bedetheque.scraper.BedethequeDatabase') as database_cls:
        scraper_cls.return_value.get_series_info.return_value = info
        ok, error, series_id = match_manual_review_series(1, selected)

    assert (ok, error, series_id) == (True, None, 1243)
    conn = sqlite3.connect(db); conn.row_factory = sqlite3.Row
    row = conn.execute('select * from auto_acquire_reviews where id=1').fetchone()
    assert row['status'] == 'pending'
    assert row['series_id'] == 1243
    assert row['volume_number'] == 1999
    assert row['matched_bedetheque_url'] == selected
