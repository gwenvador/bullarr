import os
import tempfile
import time
import unittest
from unittest import mock

from blueprints.bedetheque import catalog_index
from blueprints.bedetheque.scraper import BedethequeScraper

TITLE = "Le.Gang.De.La.Clef.A.Molette.Jean.Baptiste.Hostache.Edward.Abbey.2026.FR.[CBZ][PDF]-example"
REAL = {"title": "Gang de la clef à molette (Le)", "url": "https://www.bedetheque.com/serie-99233-BD-Gang-de-la-clef-a-molette.html", "genre": None}
UNRELATED = [{"title": name, "url": f"https://www.bedetheque.com/serie-{i}-BD-x.html", "genre": None}
             for i, name in enumerate(["Gang de gamins", "La clef des champs", "Mimolette party", "Le Gang des Moustachus"])]


class SearchSeriesFallbackTest(unittest.TestCase):
    """Cas « Le Gang de la clef à molette »: l index local (périmé) renvoie des séries sans
    rapport; il faut alors interroger Bédéthèque en direct au lieu de conclure « non trouvée »."""

    def search(self, local, live):
        scraper = BedethequeScraper()
        live_calls = []

        def raw(query, limit=20):
            live_calls.append(query)
            return live(query)

        with mock.patch("blueprints.bedetheque.catalog_index.search_catalog_index", side_effect=lambda q, limit=30: local(q)), \
                mock.patch.object(scraper, "_search_series_raw", side_effect=raw):
            return scraper.search_series(TITLE, limit=30), live_calls

    def test_unrelated_local_results_do_not_prevent_the_live_search(self):
        results, live_calls = self.search(
            local=lambda q: list(UNRELATED),
            live=lambda q: [REAL] if q == "Le Gang De La Clef A Molette" else [])
        self.assertEqual([r["title"] for r in results], [REAL["title"]])
        self.assertTrue(live_calls)

    def test_a_confident_local_result_is_used_without_any_live_request(self):
        results, live_calls = self.search(local=lambda q: [REAL] if q == "Le Gang De La Clef A Molette" else [], live=lambda q: [])
        self.assertEqual([r["title"] for r in results], [REAL["title"]])
        self.assertEqual(live_calls, [])

    def test_index_not_built_yet_still_falls_back_to_live(self):
        results, _ = self.search(local=lambda q: None, live=lambda q: [REAL] if q.endswith("Molette") else [])
        self.assertEqual([r["title"] for r in results], [REAL["title"]])


class CatalogIndexRefreshTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.object(catalog_index, "CATALOG_DB", os.path.join(self.tmp.name, "catalog.db"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def refresh(self):
        with mock.patch.object(catalog_index, "build_bedetheque_catalog_index_sync") as build:
            return catalog_index.refresh_catalog_index_if_stale(7, scraper=object()), build

    def set_built_at(self, days_ago):
        conn = catalog_index._connect_db()
        conn.execute("INSERT INTO bedetheque_series (title, url, title_normalized) VALUES (\'x\', \'u\', \'x\')")
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - days_ago * 86400))
        conn.execute("INSERT OR REPLACE INTO bedetheque_catalog_meta (key, value) VALUES (\'built_at\', ?)", (stamp,))
        conn.commit()
        conn.close()

    def test_builds_when_never_built_or_older_than_the_limit(self):
        rebuilt, build = self.refresh()
        self.assertTrue(rebuilt)
        build.assert_called_once()
        self.set_built_at(days_ago=8)
        rebuilt, build = self.refresh()
        self.assertTrue(rebuilt)

    def test_a_recent_index_is_left_alone(self):
        self.set_built_at(days_ago=2)
        rebuilt, build = self.refresh()
        self.assertFalse(rebuilt)
        build.assert_not_called()

    def test_a_concurrent_build_by_another_process_is_not_duplicated(self):
        import fcntl
        with open(catalog_index.CATALOG_DB + ".build.lock", "w") as other:
            fcntl.flock(other.fileno(), fcntl.LOCK_EX)
            rebuilt, build = self.refresh()
        self.assertFalse(rebuilt)
        build.assert_not_called()


class IndexRefreshSettingTest(unittest.TestCase):
    """Réglage « mettre à jour l index périodiquement » (Paramètres > Bédéthèque)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.object(catalog_index, "CATALOG_DB", os.path.join(self.tmp.name, "catalog.db"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_defaults_then_save_and_reload(self):
        self.assertEqual(catalog_index.load_index_refresh_config(), {"enabled": True, "interval_days": 7, "hour": 4})
        saved = catalog_index.save_index_refresh_config({"enabled": False, "interval_days": 14, "hour": 23})
        self.assertEqual(saved, {"enabled": False, "interval_days": 14, "hour": 23})
        self.assertEqual(catalog_index.load_index_refresh_config(), saved)

    def test_invalid_values_are_refused_and_nothing_is_written(self):
        for bad in ({"enabled": "oui", "interval_days": 7, "hour": 4}, {"enabled": True, "interval_days": 0, "hour": 4},
                    {"enabled": True, "interval_days": 91, "hour": 4}, {"enabled": True, "interval_days": 7, "hour": 24},
                    {"enabled": True, "interval_days": "x", "hour": 4}, None):
            with self.assertRaises(ValueError, msg=bad):
                catalog_index.save_index_refresh_config(bad)
        self.assertFalse(os.path.exists(catalog_index._refresh_config_path()))

    def test_corrupt_or_out_of_range_saved_file_falls_back_to_defaults(self):
        with open(catalog_index._refresh_config_path(), "w") as handle:
            handle.write("{broken")
        self.assertEqual(catalog_index.load_index_refresh_config()["interval_days"], 7)
        with open(catalog_index._refresh_config_path(), "w") as handle:
            handle.write('{"enabled": false, "interval_days": 500, "hour": 99}')
        self.assertEqual(catalog_index.load_index_refresh_config(), {"enabled": False, "interval_days": 7, "hour": 4})

    def test_hourly_tick_only_refreshes_when_enabled_at_the_chosen_hour(self):
        from blueprints.bedetheque import index_scheduler
        catalog_index.save_index_refresh_config({"enabled": True, "interval_days": 3, "hour": 4})
        at = lambda hour: time.struct_time((2026, 10, 8, hour, 30, 0, 3, 281, 0))
        with mock.patch.object(catalog_index, "refresh_catalog_index_if_stale", return_value=True) as refresh:
            self.assertFalse(index_scheduler.refresh_tick(at(3)))
            refresh.assert_not_called()
            self.assertTrue(index_scheduler.refresh_tick(at(4)))
            self.assertAlmostEqual(refresh.call_args[0][0], 2.9)             # 3 jours moins la tolérance
            catalog_index.save_index_refresh_config({"enabled": False, "interval_days": 3, "hour": 4})
            refresh.reset_mock()
            self.assertFalse(index_scheduler.refresh_tick(at(4)))
            refresh.assert_not_called()

    def test_settings_route_reads_and_validates(self):
        from flask import Flask
        from blueprints.bedetheque import bedetheque_bp
        app = Flask(__name__)
        app.register_blueprint(bedetheque_bp, url_prefix="/api/bedetheque")
        client = app.test_client()
        url = "/api/bedetheque/catalog-index/schedule"
        self.assertEqual(client.get(url).get_json()["interval_days"], 7)
        ok = client.post(url, json={"enabled": True, "interval_days": 10, "hour": 2})
        self.assertEqual((ok.status_code, ok.get_json()["interval_days"]), (200, 10))
        self.assertEqual(client.get(url).get_json()["hour"], 2)
        bad = client.post(url, json={"enabled": True, "interval_days": 0, "hour": 2})
        self.assertEqual(bad.status_code, 400)
        self.assertIn("1 et 90", bad.get_json()["error"])


if __name__ == "__main__":
    unittest.main()
