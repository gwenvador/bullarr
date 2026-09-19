from blueprints.emule.routes import _validate_ed2k_link


def test_ed2k_validator_accepts_canonical_and_extended_file_links():
    canonical = "ed2k://|file|album.cbz|157643802|0123456789ABCDEF0123456789ABCDEF|/"
    extended = "ed2k://|file|album.cbz|157643802|0123456789ABCDEF0123456789ABCDEF|h=QFMWK6RZYNNKUWBPDAQ4QDTVE4LOHDZD|/"

    assert _validate_ed2k_link(canonical)
    assert _validate_ed2k_link(extended)


def test_ed2k_validator_rejects_unsafe_or_malformed_links():
    prefix = "ed2k://|file|album.cbz|157643802|0123456789ABCDEF0123456789ABCDEF|h="
    assert not _validate_ed2k_link(prefix + "A" * 31 + "|/")
    assert not _validate_ed2k_link(prefix + "A" * 33 + "|/")
    assert not _validate_ed2k_link(prefix + "A" * 31 + "!|/")
    assert not _validate_ed2k_link(prefix + "A" * 32 + "|p=unsafe|/")
    assert not _validate_ed2k_link("ed2k://|file|album.cbz|157643802|0123456789ABCDEF0123456789ABCDEF|h=bad;rm -rf /|/")
    assert not _validate_ed2k_link("ed2k://|file|album.cbz|not-a-size|0123456789ABCDEF0123456789ABCDEF|/")
    assert not _validate_ed2k_link("https://example.invalid/file")
