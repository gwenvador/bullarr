import json
import time
import unittest
from unittest.mock import patch

from blueprints.vk import scraper as vk


def _page(method, response, request=None):
    """Page VK publique : réponse de l'API embarquée dans le HTML."""
    block = {'method': method, 'request': request or {}, 'version': '5.289', 'response': response, 'error': None}
    return '<html><script>var data = [' + json.dumps(block, ensure_ascii=False, separators=(",", ":")) + '];</script></html>'


def _doc(doc_id, title, ext='pdf', size=50_000_000, date=None):
    return {'type': 'doc', 'doc': {
        'id': doc_id, 'owner_id': 625817526, 'title': title, 'ext': ext, 'size': size,
        'date': date or int(time.time()) - 3600,
        'url': f'https://vk.com/doc625817526_{doc_id}?hash=abc&dl=def&api=1&no_preview=1'}}


class VkParsingTest(unittest.TestCase):
    def test_embedded_api_response_is_read(self):
        page = _page('board.getTopics', {'count': 1, 'items': [{'id': 5, 'title': 'X'}]})
        blocks = vk.embedded_api_responses(page, 'board.getTopics')
        self.assertEqual(blocks[0]['response']['items'][0]['id'], 5)

    def test_entry_title_adds_series_name_only_when_missing(self):
        self.assertEqual(vk.entry_title('SCARLET QUEEN', 'T01 - Volume 1.pdf', 'pdf'), 'Scarlet Queen - T01 - Volume 1.pdf')
        self.assertEqual(vk.entry_title('LA VOIE DU GLAIVE', 'La.voie.du.glaive.T03.Rome.pdf', 'pdf'), 'La.voie.du.glaive.T03.Rome.pdf')
        self.assertEqual(vk.entry_title('Ailes du Singe (Les)', 'Les ailes du singe_T01 - Wakanda.pdf'), 'Les ailes du singe_T01 - Wakanda.pdf')
        self.assertEqual(vk.entry_title('ONE SHOT HUMOUR', 'La Pro', 'pdf'), 'La Pro.pdf')
        self.assertEqual(vk.entry_title('X', 'Alb&#233;ric.pdf'), 'X - Albéric.pdf')

    def test_only_vk_doc_links_are_downloadable(self):
        self.assertTrue(vk.is_vk_doc_url('https://vk.com/doc625817526_640729992?hash=x'))
        self.assertTrue(vk.is_vk_doc_url('https://vk.ru/doc-1_2'))
        self.assertFalse(vk.is_vk_doc_url('http://vk.com/doc1_2'))
        self.assertFalse(vk.is_vk_doc_url('https://evil.example/doc1_2'))
        self.assertFalse(vk.is_vk_doc_url('https://vk.com/topic-1_2'))

    def test_index_lines_accept_every_separator(self):
        text = ('► ► A ◄ ◄\n\nAdler : https://vk.com/topic-203785966_47458258\n'
                'Oliver Twist . https://vk.com/topic-203785966_49542939\n'
                "Ange d'Yeu (L') ; https://vk.ru/topic-203785966_47969255\n"
                'Mystère du Temps (Le) https://vk.com/topic-203785966_48885328\n'
                'RanXerox : RanXerox : https://vk.com/topic-203785966_47545458\n'
                'Champignac : https://vk.ru/topic-203785966_47461257?post=25076\n'
                'Pas un lien : https://example.com/topic-203785966_1\n')
        self.assertEqual(vk.parse_index_lines(text), [
            ('Adler', '47458258', None), ('Oliver Twist', '49542939', None),
            ("Ange d'Yeu (L')", '47969255', None), ('Mystère du Temps (Le)', '48885328', None),
            ('RanXerox', '47545458', None), ('Champignac', '47461257', '25076')])

    def test_series_lookup_ignores_articles_and_case(self):
        index = [{'title': 'Ailes du Singe (Les)', 'topic_id': '1'},
                 {'title': 'Petit Spirou [Présente...] (Le)', 'topic_id': '2'},
                 {'title': 'Spirou et Fantasio', 'topic_id': '3'},
                 {'title': 'Blake et Mortimer', 'topic_id': '4'}]
        self.assertEqual([e['topic_id'] for e in vk.find_vk_series('Les Ailes du Singe', index)], ['1'])
        self.assertEqual([e['topic_id'] for e in vk.find_vk_series('spirou et fantasio', index)], ['3'])
        self.assertEqual(vk.find_vk_series('Thorgal', index), [])


class VkTopicFilesTest(unittest.TestCase):
    def setUp(self):
        vk._topic_docs_cache.clear()

    def test_shared_topic_keeps_only_the_wanted_series(self):
        comments = {'count': 3, 'items': [
            {'id': 1, 'date': 1, 'text': 'Champignac (Scénario: BéKa)\n\nT01 - Enigma', 'attachments': [_doc(10, 'T01 - Enigma.pdf')]},
            {'id': 2, 'date': 1, 'text': 'Spirou et Fantasio (Série créée par Franquin)', 'attachments': [_doc(20, 'T02 - Il y a un Sorcier.pdf')]},
            {'id': 3, 'date': 1, 'text': 'T16 - suite', 'attachments': [_doc(30, "T16 - L'Ombre du Z.pdf")]},
        ]}
        with patch.object(vk, '_fetch_page', return_value=_page('board.getComments', comments)):
            spirou = vk.vk_topic_files('47461257', 'Spirou et Fantasio', 'Spirou et Fantasio')
            champignac = vk.vk_topic_files('47461257', 'Champignac', 'Champignac')
        self.assertEqual([f['filename'] for f in spirou],
                         ['Spirou et Fantasio - T02 - Il y a un Sorcier.pdf', "Spirou et Fantasio - T16 - L'Ombre du Z.pdf"])
        self.assertEqual([f['filename'] for f in champignac], ['Champignac - T01 - Enigma.pdf'])
        self.assertIn('?post=1#doc625817526_10', champignac[0]['link'])

    def test_topic_without_matching_header_uses_topic_title(self):
        comments = {'count': 1, 'items': [
            {'id': 1, 'date': 1, 'text': 'Collection Niffle (édition)', 'attachments': [_doc(10, 'T04 - Pyramide.pdf'), _doc(11, 'notes.txt', ext='txt')]}]}
        with patch.object(vk, '_fetch_page', return_value=_page('board.getComments', comments)):
            files = vk.vk_topic_files('4', 'Blake et Mortimer', 'Blake et Mortimer')
        self.assertEqual([f['filename'] for f in files], ['Blake et Mortimer - T04 - Pyramide.pdf'])


class VkFeedTest(unittest.TestCase):
    def setUp(self):
        vk._topic_cache.clear()

    def test_feed_lists_recent_files_of_recently_updated_topics(self):
        now = int(time.time())
        topics = {'count': 3, 'items': [
            {'id': 100, 'title': 'SCARLET QUEEN', 'updated': now - 60, 'comments': 2},
            {'id': 101, 'title': 'VOS DEMANDES / YOUR REQUESTS', 'updated': now - 60, 'comments': 90},
            {'id': 102, 'title': 'VIEUX SUJET', 'updated': now - 400 * 86400, 'comments': 5},
        ]}
        comments = {'count': 2, 'items': [
            {'id': 1, 'date': now - 120, 'text': '', 'attachments': [_doc(1, 'T01 - Volume 1.pdf', date=now - 120)]},
            {'id': 2, 'date': now - 500 * 86400, 'text': '', 'attachments': [_doc(2, 'Ancien.pdf', date=now - 500 * 86400)]},
        ]}
        fetched = []

        def fake_fetch(url):
            fetched.append(url)
            return _page('board.getTopics', topics) if url == vk.VK_BOARD_URL else _page('board.getComments', comments)

        with patch.object(vk, '_fetch_page', side_effect=fake_fetch):
            entries = vk.fetch_vk_feed()
            self.assertEqual(len(fetched), 2)  # forum + un seul sujet
            again = vk.fetch_vk_feed()
        self.assertEqual(len(fetched), 3)  # le sujet inchangé n'est pas relu
        self.assertEqual([e['title'] for e in entries], ['Scarlet Queen - T01 - Volume 1.pdf'])
        self.assertTrue(entries[0]['vk'])
        self.assertEqual(entries[0]['download_links'][0]['url'][:30], 'https://vk.com/doc625817526_1?')
        self.assertEqual(again, entries)


class VkRssAndMonitorWiringTest(unittest.TestCase):
    def test_vk_feed_is_a_provider(self):
        from blueprints.settings.rss import normalize_feeds, fetch_feed
        feeds = normalize_feeds([{'provider': 'vk'}, {'provider': 'vk', 'name': 'doublon'}])
        self.assertEqual(feeds, [{'name': 'VK – Au Phil des Bulles', 'provider': 'vk', 'enabled': True}])
        with patch('blueprints.vk.scraper.fetch_vk_feed', return_value=[{'title': 'a'}]) as fetch:
            self.assertEqual(fetch_feed(feeds[0]), [{'title': 'a'}])
        fetch.assert_called_once()

    def test_monitor_sends_vk_rss_file_to_vk_downloader(self):
        from flask import Flask
        from blueprints.missing_monitor import new_file_handler
        app = Flask('t')
        app.config['VK_IMPORT_DIRECTORY'] = '/downloads/vk'
        item = {'vk': True, 'link': 'https://vk.com/topic-203785966_1?post=2',
                'download_links': [{'url': 'https://vk.com/doc1_2?hash=x'}]}
        with app.app_context(), patch('blueprints.vk.scraper.download_vk_file_background') as start:
            ok, message = new_file_handler._send_monitor_download('rss', item, None, 'Série', 3, 7, 'Série - T03.pdf', volume_id=9)
        self.assertTrue(ok, message)
        args, kwargs = start.call_args
        self.assertEqual(args[:3], ('https://vk.com/doc1_2?hash=x', 'Série - T03.pdf', '/downloads/vk'))
        self.assertEqual((kwargs['series_id'], kwargs['volume_id'], kwargs['volume_number']), (7, 9, 3))


class VkDownloadTest(unittest.TestCase):
    def test_refuses_foreign_urls_and_paths(self):
        with self.assertRaises(ValueError):
            vk.download_vk_file('https://evil.example/doc1_2', 'a.pdf', '/tmp')
        self.assertEqual(vk.safe_filename('../../etc/passwd'), 'passwd')
        self.assertEqual(vk.safe_filename('a<b>:c.pdf'), 'a_b__c.pdf')


class VkSearchToggleTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        from flask import Flask
        from blueprints.vk import vk_bp
        self.tmp = tempfile.TemporaryDirectory()
        self.app = Flask('t')
        self.app.config.update(DATA_DIR=self.tmp.name, VK_CONFIG={'enabled': True},
                               VK_CONFIG_FILE=self.tmp.name + '/vk_config.json')
        self.app.register_blueprint(vk_bp)

    def tearDown(self):
        self.tmp.cleanup()

    def test_disabled_search_never_queries_vk(self):
        from blueprints.missing_monitor.searcher import MissingVolumeSearcher
        client = self.app.test_client()
        self.assertTrue(client.get('/api/vk/config').get_json()['enabled'])
        self.assertFalse(client.post('/api/vk/config', json={'enabled': False}).get_json()['enabled'])
        with self.app.app_context(), patch('blueprints.vk.scraper.search_vk_raw') as search:
            self.assertEqual(MissingVolumeSearcher()._search_vk('Thorgal', 1), [])
        search.assert_not_called()
        client.post('/api/vk/config', json={'enabled': True})
        with self.app.app_context(), patch('blueprints.vk.scraper.search_vk_raw', return_value=[
                {'filename': 'Thorgal - T01 - La Magicienne trahie.pdf', 'size': 1, 'download_url': 'https://vk.com/doc1_2', 'link': 'l'}]):
            results = MissingVolumeSearcher()._search_vk('Thorgal', 1)
        self.assertEqual([r['source'] for r in results], ['vk'])


class VkAddressTest(unittest.TestCase):
    def tearDown(self):
        vk.configure(vk.VK_DEFAULT_BASE_URL)

    def test_address_is_validated_and_applied(self):
        self.assertEqual(vk.normalize_base_url(''), 'https://vk.com/board203785966')
        self.assertEqual(vk.normalize_base_url('vk.ru/board203785966/'), 'https://vk.ru/board203785966')
        self.assertEqual(vk.normalize_base_url('https://m.vk.com/board203785966'), 'https://vk.com/board203785966')
        for bad in ('https://evil.example/board1', 'https://vk.com/topic-1_2', 'https://vk.com/board'):
            with self.assertRaises(ValueError):
                vk.normalize_base_url(bad)
        vk.configure('https://vk.ru/board42')
        self.assertEqual(vk.VK_BOARD_URL, 'https://vk.ru/board42')
        self.assertEqual(vk.VK_TOPIC_URL.format(topic_id=7), 'https://vk.ru/topic-42_7')
        self.assertEqual(vk.parse_index_lines('Alpha : https://vk.com/topic-42_9\nBeta : https://vk.com/topic-203785966_9'),
                         [('Alpha', '9', None)])


if __name__ == '__main__':
    unittest.main()
