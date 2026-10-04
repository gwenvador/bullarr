"""A rejected pack is actionable only when individual tome downloads leave a gap."""
import os
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from unittest.mock import Mock, patch

from flask import Flask

from blueprints.bedetheque.auto_acquire import (
    _run_auto_acquire_for_series_locked, _series_bundle_kind, get_manual_reviews,
    queue_manual_review,
)


class Searcher:
    def search_for_volume(self, *_args, **_kwargs):
        return [
            {'source': 'prowlarr', 'filename': 'Wrong pack T1-T2', 'size': 123456},
            {'source': 'telegram', 'filename': 'Series T1', 'size': 10000},
            {'source': 'telegram', 'filename': 'Series T2', 'size': 20000},
        ]

    def _confirms_requested_volume(self, filename, number, _label):
        return (f'T{number}' in filename and 'pack' not in filename, None)


class IntegralSearcher:
    def search_for_volume(self, *_args, **_kwargs):
        return [
            {'source': 'prowlarr', 'title': 'Les.enfants.de.la.mer.[INTEGRALE].FR.[CBZ]'},
            {'source': 'telegram', 'filename': 'Les enfants de la mer (Igarashi) Intégrale 5 tomes [CBZ]'},
            {'source': 'ebdz', 'filename': 'Enfants de la Mer (Les) (01-05) (Igarashi) [CBZ]'},
            {'source': 'prowlarr', 'title': 'Different Series T02 [PDF]'},
        ]

    def _confirms_requested_volume(self, filename, number, _label):
        return ('Different Series T02' in filename and number == 2, None)


class AutoAcquirePackReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.database = os.path.join(self.tmp.name, 'bullarr.db')
        with closing(sqlite3.connect(self.database)) as conn:
            conn.execute('CREATE TABLE series (id INTEGER PRIMARY KEY, ebdz_thread_id INTEGER, bedetheque_scenaristes TEXT, bedetheque_dessinateurs TEXT)')
            conn.execute('INSERT INTO series (id, ebdz_thread_id) VALUES (1, NULL)')
            conn.commit()
        self.app = Flask(__name__)
        self.app.config['DATABASE'] = self.database

    def _run(self, success_numbers, select_pack=False):
        def best(candidates, number, *_args, **_kwargs):
            if number not in success_numbers:
                return None
            return next((item for item in candidates if item['filename'] == f'Series T{number}'), None)
        download = Mock(return_value=(True, 'ok'))
        with patch('blueprints.library.routes.load_library_import_config', return_value={
            'auto_acquire_sources': ['telegram', 'prowlarr'],
            'auto_acquire_pack_search_enabled': True,
        }), patch('blueprints.missing_monitor.searcher.MissingVolumeSearcher', Searcher), \
             patch('blueprints.bedetheque.auto_acquire._enrich_ed2k_availability', side_effect=lambda items: items), \
             patch('blueprints.bedetheque.auto_acquire._best_pack_result', side_effect=lambda items, _title: items[0] if select_pack and len(items) > 1 else None), \
             patch('blueprints.bedetheque.auto_acquire._best_confident_result', side_effect=best), \
             patch('blueprints.bedetheque.auto_acquire._download_result', download), \
             patch('blueprints.library.action_history.log_action'):
            _run_auto_acquire_for_series_locked(self.app, 1, 'Series', [1, 2], False)
        return download.call_count

    def _reviews(self):
        with closing(sqlite3.connect(self.database)) as conn:
            return conn.execute('SELECT volume_label, status FROM auto_acquire_reviews ORDER BY id').fetchall()

    def test_all_individual_tomes_resolve_prior_pack_suggestion(self):
        with self.app.app_context():
            queue_manual_review(1, 'Series', None, 'Pack à confirmer', [{'filename': 'Wrong pack T1-T2'}], 'old', force_candidates=True)
        self._run({1, 2})
        self.assertEqual(self._reviews(), [('Pack à confirmer', 'resolved')])

    def test_pack_remains_for_review_when_a_tome_is_uncovered(self):
        self._run({1})
        self.assertEqual(self._reviews(), [('Tome 2', 'pending'), ('Pack à confirmer · Tome 2 manquant', 'pending')])

    def test_selected_pack_skips_individual_downloads(self):
        self.assertEqual(self._run(set(), select_pack=True), 1)
        self.assertEqual(self._reviews(), [])

    def test_pack_names_each_uncovered_tome(self):
        self._run(set())
        self.assertEqual(self._reviews()[-1], ('Pack à confirmer · Tomes 1, 2 manquants', 'pending'))

    def test_integral_creates_one_review_instead_of_one_per_tome(self):
        self.assertEqual(_series_bundle_kind('Enfants de la Mer (Les) (01-02+) [CBZ]', [1, 2, 3, 4, 5]), 'partial')
        with patch('blueprints.library.routes.load_library_import_config', return_value={
            'auto_acquire_sources': ['prowlarr', 'telegram', 'ebdz'],
            'auto_acquire_pack_search_enabled': True,
        }), patch('blueprints.missing_monitor.searcher.MissingVolumeSearcher', IntegralSearcher), \
             patch('blueprints.bedetheque.auto_acquire._best_pack_result', return_value=None), \
             patch('blueprints.bedetheque.auto_acquire._best_confident_result', return_value=None), \
             patch('blueprints.library.action_history.log_action'):
            _run_auto_acquire_for_series_locked(self.app, 1, 'Les enfants de la mer', [1, 2, 3, 4, 5], False)
        with closing(sqlite3.connect(self.database)) as conn:
            reviews = conn.execute('SELECT volume_number, volume_label, candidates_json FROM auto_acquire_reviews').fetchall()
        self.assertEqual(len(reviews), 1)
        self.assertIsNone(reviews[0][0])
        self.assertEqual(reviews[0][1], 'Intégrale à confirmer · Tomes 1, 2, 3, 4, 5 manquants')
        self.assertEqual(len(json.loads(reviews[0][2])), 3)

    def test_validation_shows_torrent_after_many_telegram_results_with_original_index(self):
        telegram = [{'source': 'telegram', 'title': f'Astoria T6 Telegram {index}'} for index in range(30)]
        torrent = {'source': 'prowlarr', 'title': 'Astoria T6 Tracker', 'info_url': 'https://tracker.example/torrent/6'}
        with self.app.app_context():
            queue_manual_review(1, 'Astoria', 6, 'Tome 6', telegram + [torrent], 'À valider', force_candidates=True)
            reviews = get_manual_reviews()
        self.assertEqual(len(reviews), 1)
        shown = reviews[0]['candidates']
        self.assertLessEqual(len(shown), 20)
        self.assertIn('prowlarr', [candidate['source'] for candidate in shown])
        self.assertEqual(next(candidate['candidate_index'] for candidate in shown if candidate['source'] == 'prowlarr'), 30)

    def test_successful_automatic_download_resolves_older_volume_review(self):
        with self.app.app_context():
            queue_manual_review(1, 'Series', 1, 'Tome 1',
                                [{'source': 'telegram', 'title': 'Series T1'}],
                                'Ancienne validation', force_candidates=True)
        self._run({1})
        self.assertIn(('Tome 1', 'resolved'), self._reviews())


if __name__ == '__main__':
    unittest.main()
