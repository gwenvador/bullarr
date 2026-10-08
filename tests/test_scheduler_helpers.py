import json
import os
import tempfile
import time
import unittest
from unittest import mock

from blueprints.library import scheduler


class SkipRuleTest(unittest.TestCase):
    def test_a_path_is_skipped_if_manual_in_progress_or_already_finalised(self):
        manual, in_progress, finalised = {'/a.cbz'}, {'/b.cbz'}, {'/c.cbz'}
        for path in ('/a.cbz', '/b.cbz', '/c.cbz'):
            self.assertTrue(scheduler._should_skip_auto_import_path(path, manual, in_progress, finalised), path)
        self.assertFalse(scheduler._should_skip_auto_import_path('/d.cbz', manual, in_progress, finalised))
        self.assertFalse(scheduler._should_skip_auto_import_path('/d.cbz', set(), set(), set()))


class NotifiedFilesStateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = os.path.join(self.tmp.name, 'state.json')

    def test_round_trip_is_sorted_and_deduplicated(self):
        scheduler._save_notified_files(self.state, {'torrents/b.cbz', 'amule/a.cbz', 'amule/a.cbz'})
        self.assertEqual(json.load(open(self.state, encoding='utf-8')), {'known_files': ['amule/a.cbz', 'torrents/b.cbz']})
        self.assertEqual(scheduler._load_notified_files(self.state), {'amule/a.cbz', 'torrents/b.cbz'})

    def test_missing_or_corrupt_state_means_nothing_is_known_yet(self):
        self.assertEqual(scheduler._load_notified_files(self.state), set())
        with open(self.state, 'w', encoding='utf-8') as handle:
            handle.write('{not json')
        self.assertEqual(scheduler._load_notified_files(self.state), set())

    def test_saving_to_an_unwritable_location_does_not_raise(self):
        scheduler._save_notified_files(os.path.join(self.tmp.name, 'absent-dir', 'state.json'), {'x'})


class StalePartFilesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name

    def make(self, relative, age_minutes):
        path = os.path.join(self.root, relative)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, 'w').close()
        moment = time.time() - age_minutes * 60
        os.utime(path, (moment, moment))
        return path

    def test_only_abandoned_part_files_are_removed(self):
        old = self.make('telegram/old.cbz.part', scheduler.STALE_PART_FILE_MINUTES + 5)
        nested = self.make('telegram/sub/old2.part', scheduler.STALE_PART_FILE_MINUTES + 60)
        recent = self.make('telegram/recent.cbz.part', 1)
        finished = self.make('telegram/old.cbz', 600)                     # pas un .part
        uploads = self.make('_uploads/1/forgotten.part', 600)             # dossier réservé aux envois
        scheduler._cleanup_stale_part_files([self.root, os.path.join(self.root, 'does-not-exist')])
        self.assertFalse(os.path.exists(old))
        self.assertFalse(os.path.exists(nested))
        self.assertTrue(os.path.exists(recent))
        self.assertTrue(os.path.exists(finished))
        self.assertTrue(os.path.exists(uploads))

    def test_a_failed_removal_does_not_stop_the_others(self):
        first = self.make('a/one.part', 100)
        second = self.make('b/two.part', 100)
        real_remove = os.remove

        def flaky(path):
            if path == first:
                raise OSError('read-only file system')
            real_remove(path)

        with mock.patch.object(scheduler.os, 'remove', side_effect=flaky):
            scheduler._cleanup_stale_part_files([self.root])
        self.assertTrue(os.path.exists(first))
        self.assertFalse(os.path.exists(second))


class AutoImportConstantsTest(unittest.TestCase):
    def test_failure_policy_constants_are_coherent(self):
        self.assertEqual(scheduler.MAX_AUTO_IMPORT_FAILURES, 1)
        self.assertEqual(scheduler.MAX_AUTO_IMPORT_CORRUPTION_RETRIES, len(scheduler.AUTO_IMPORT_CORRUPTION_RETRY_DELAYS))
        self.assertEqual(list(scheduler.AUTO_IMPORT_CORRUPTION_RETRY_DELAYS), sorted(scheduler.AUTO_IMPORT_CORRUPTION_RETRY_DELAYS))


if __name__ == '__main__':
    unittest.main()
