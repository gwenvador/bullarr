import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class GuideSourceTest(unittest.TestCase):
    """templates/partials/guide.html est recopié tel quel dans docs.html du site."""

    def setUp(self):
        self.guide = (ROOT / 'templates/partials/guide.html').read_text(encoding='utf-8')

    def test_guide_is_plain_html(self):
        for token in ('{{', '{%', '{#'):
            self.assertNotIn(token, self.guide)

    def test_toc_links_match_sections(self):
        import re
        anchors = re.findall(r'<a href="#([\w-]+)">', self.guide)
        sections = re.findall(r'<section id="([\w-]+)">', self.guide)
        self.assertEqual(anchors, sections)

    def test_app_page_includes_guide(self):
        page = (ROOT / 'templates/docs.html').read_text(encoding='utf-8')
        self.assertIn("{% include 'partials/guide.html' %}", page)
        self.assertIn('href="/docs"', (ROOT / 'templates/settings.html').read_text(encoding='utf-8'))


class SyncSiteDocsTest(unittest.TestCase):
    def test_replaces_content_between_markers(self):
        import importlib.util
        import tempfile
        spec = importlib.util.spec_from_file_location('sync_site_docs', ROOT / 'scripts/sync_site_docs.py')
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        with tempfile.TemporaryDirectory() as tmp:
            page = Path(tmp) / 'docs.html'
            page.write_text('<main>\n<!-- guide:start -->\nancien\n<!-- guide:end -->\n</main>', encoding='utf-8')
            self.assertTrue(mod.sync(page))
            text = page.read_text(encoding='utf-8')
            self.assertNotIn('ancien', text)
            self.assertIn('<section id="installation">', text)
            self.assertFalse(mod.sync(page))


if __name__ == '__main__':
    unittest.main()
