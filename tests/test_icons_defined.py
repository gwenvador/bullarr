import glob
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class IconRegistryTest(unittest.TestCase):
    """svgIcon() renvoie une chaîne vide pour une icône inconnue: le bouton ou lien reste
    cliquable mais n affiche plus rien (cas de l icône du lien RSS dans Nouveautés)."""

    def test_every_icon_used_by_the_front_end_is_defined(self):
        with open(os.path.join(ROOT, "static/js/icons.js"), encoding="utf-8") as handle:
            defined = set(re.findall(r"^\s*[\x27\"]?([a-z0-9-]+)[\x27\"]?\s*:", handle.read(), re.M))
        used = {}
        for path in glob.glob(os.path.join(ROOT, "static/js/*.js")) + glob.glob(os.path.join(ROOT, "templates/*.html")):
            with open(path, encoding="utf-8") as handle:
                for name in re.findall(r"svgIcon\(\s*[\x27\"]([a-z0-9-]+)[\x27\"]", handle.read()):
                    used.setdefault(name, os.path.basename(path))
        missing = {name: used[name] for name in used if name not in defined}
        self.assertEqual(missing, {})


if __name__ == "__main__":
    unittest.main()
