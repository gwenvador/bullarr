"""An unresolved Nouveautés series keeps its known catalogue URL and releases."""
import os
import sqlite3
import tempfile
import unittest

from flask import Flask

from blueprints.bedetheque.auto_acquire import get_manual_reviews, queue_series_match_review


class NouveautesSeriesReviewTests(unittest.TestCase):
    def test_repeated_failure_keeps_one_review_and_both_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            app = Flask(__name__)
            app.config['DATABASE'] = os.path.join(directory, 'bullarr.db')
            with sqlite3.connect(app.config['DATABASE']) as conn:
                conn.execute('CREATE TABLE series (id INTEGER PRIMARY KEY, bedetheque_scenaristes TEXT, bedetheque_dessinateurs TEXT)')
            url = 'https://www.bedetheque.com/serie-12345-BD-Astoria.html'
            with app.app_context():
                queue_series_match_review('Astoria', [{
                    'source': 'prowlarr', 'filename': 'Astoria Tome 6',
                    'info_url': 'https://example.com/6', 'bedetheque_url': url,
                }], 'Fiche Bédéthèque inaccessible')
                queue_series_match_review('Astoria', [{
                    'source': 'prowlarr', 'filename': 'Astoria Tome 5',
                    'info_url': 'https://example.com/5', 'bedetheque_url': url,
                }], 'Fiche Bédéthèque inaccessible')
                reviews = get_manual_reviews()
            self.assertEqual(len(reviews), 1)
            self.assertEqual(reviews[0]['volume_label'], 'Série à ajouter')
            self.assertEqual(
                {item['filename'] for item in reviews[0]['candidates']},
                {'Astoria Tome 5', 'Astoria Tome 6'},
            )
            self.assertEqual(
                {item['bedetheque_url'] for item in reviews[0]['candidates']}, {url}
            )


if __name__ == '__main__':
    unittest.main()
