from blueprints.ebdz.scraper import MyBBScraper


def _row(link):
    return {
        "link": link,
        "filename": "Example%20-%20T01.cbz",
        "filesize": "123",
        "volume": 1,
        "thread_title": "Example",
        "thread_url": "https://ebdz.net/forum/showthread.php?tid=42",
        "thread_id": "42",
        "forum_category": "BD",
        "cover_image": None,
        "description": None,
        "is_integral": False,
        "integral_number": None,
        "is_hs": False,
        "hs_number": None,
        "is_episode": False,
        "episode_number": None,
    }


def test_same_thread_same_ed2k_hash_with_aich_variant_is_stored_once(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    scraper = MyBBScraper("https://ebdz.net", str(tmp_path / "ebdz.db"), "", "", "BD")
    scraper.create_table()

    short = "ed2k://|file|Example%20-%20T01.cbz|123|0123456789ABCDEF0123456789ABCDEF|/"
    with_aich = "ed2k://|file|Example%20-%20T01.cbz|123|0123456789abcdef0123456789abcdef|h=ABCDEFGHIJKLMNOPQRSTUV1234567890|/"

    assert scraper.save_to_db([_row(short), _row(with_aich)]) == 1
    with scraper.connect_db() as connection:
        assert connection.execute("SELECT COUNT(*) FROM ed2k_links").fetchone()[0] == 1
