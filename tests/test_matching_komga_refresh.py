from pathlib import Path


def test_matching_table_filters_all_dynamic_sources_and_refreshes_komga_match():
    source = (Path(__file__).resolve().parents[1] / "static/js/verification.js").read_text()
    assert 'data-source="komga"' in source
    assert "Object.entries(statusFilters).some" in source
    start = source.index("async function selectVerificationKomgaCandidate")
    end = source.index("async function repairUnmatchedKomgaSeries")
    assert "_refreshMissingRowOrRemove(seriesId);" in source[start:end]
    assert "runVerificationCategory('unmatched_owned_komga')" not in source[start:end]
