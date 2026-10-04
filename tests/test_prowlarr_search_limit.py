"""Prowlarr limits must apply after matching and ranking all indexers."""
import unittest
from unittest.mock import Mock, patch

from blueprints.prowlarr.search import search_prowlarr_raw


class ProwlarrSearchLimitTests(unittest.TestCase):
    def test_exact_torrent_after_first_thirty_raw_results_is_kept(self):
        rows = [
            {'title': f'Astoria unrelated release {index}', 'seeders': 0,
             'infoUrl': f'https://other.example/torrents/{index}'}
            for index in range(30)
        ]
        rows.append({
            'title': 'Astoria.Tome.6.Martin.Au.loin.2026.fr.[CBZ]-example',
            'seeders': 8,
            'infoUrl': 'https://tracker.example/torrents/exact',
            'downloadUrl': 'https://prowlarr.example/17/download/exact',
        })
        response = Mock(status_code=200)
        response.json.return_value = rows
        config = {'enabled': True, 'url': 'https://prowlarr.example',
                  'api_key_decrypted': 'test-key', 'selected_indexers': [17]}
        with patch('blueprints.prowlarr.search.load_prowlarr_config', return_value=config), \
             patch('blueprints.prowlarr.search.requests.get', return_value=response), \
             patch('blueprints.library.scanner.LibraryScanner.parse_filename', return_value={
                 'volume': None, 'is_integral': False, 'integral_number': None,
                 'is_hs': False, 'hs_number': None, 'resolution': None,
             }):
            results = search_prowlarr_raw('Astoria', limit=30)
        self.assertEqual(len(results), 30)
        self.assertEqual(results[0]['info_url'], 'https://tracker.example/torrents/exact')


if __name__ == '__main__':
    unittest.main()
