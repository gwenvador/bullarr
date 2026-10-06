import os
import tempfile
import unittest

from path_safety import UnsafePathError, configured_root, resolve_within, resolve_within_any


class PathSafetyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = os.path.realpath(self.tmp.name)
        self.root = os.path.join(base, 'imports')
        self.sibling = os.path.join(base, 'imports-evil')   # même préfixe de nom que la racine
        self.outside = os.path.join(base, 'outside')
        for d in (self.root, self.sibling, self.outside):
            os.makedirs(d)
        open(os.path.join(self.root, 'a.cbz'), 'w').close()
        open(os.path.join(self.outside, 'secret.txt'), 'w').close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_path_inside_root_is_returned_resolved(self):
        self.assertEqual(resolve_within(os.path.join(self.root, 'a.cbz'), self.root), os.path.join(self.root, 'a.cbz'))

    def test_root_itself_is_allowed_and_returned_as_the_root(self):
        self.assertEqual(resolve_within(self.root, self.root), self.root)
        self.assertEqual(resolve_within(self.root + os.sep, self.root), self.root)

    def test_parent_traversal_is_refused(self):
        with self.assertRaises(UnsafePathError):
            resolve_within(os.path.join(self.root, '..', 'outside', 'secret.txt'), self.root)

    def test_sibling_directory_sharing_the_root_name_prefix_is_refused(self):
        with self.assertRaises(UnsafePathError):
            resolve_within(os.path.join(self.sibling, 'x.cbz'), self.root)

    def test_absolute_path_outside_is_refused(self):
        with self.assertRaises(UnsafePathError):
            resolve_within(os.path.join(self.outside, 'secret.txt'), self.root)

    def test_symlink_escaping_the_root_is_refused(self):
        link = os.path.join(self.root, 'link.txt')
        os.symlink(os.path.join(self.outside, 'secret.txt'), link)
        with self.assertRaises(UnsafePathError):
            resolve_within(link, self.root)

    def test_resolve_within_any_picks_the_matching_root(self):
        self.assertEqual(resolve_within_any(os.path.join(self.outside, 'secret.txt'), [self.root, self.outside]),
                         os.path.join(self.outside, 'secret.txt'))
        with self.assertRaises(UnsafePathError):
            resolve_within_any(os.path.join(self.sibling, 'x'), [self.root, self.outside])

    def test_configured_root_returns_the_configured_entry_only(self):
        self.assertEqual(configured_root(self.root + os.sep, [self.outside, self.root]), self.root)
        self.assertIsNone(configured_root(self.sibling, [self.root, self.outside]))
        self.assertIsNone(configured_root(os.path.join(self.root, 'sub'), [self.root]))
        self.assertIsNone(configured_root('', [self.root]))


if __name__ == '__main__':
    unittest.main()
