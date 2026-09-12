from pathlib import Path

def test_top_series_search_replays_query_after_async_data_load():
    source = (Path(__file__).resolve().parents[1] / 'static/js/nav.js').read_text()
    assert 'navHeaderSearchLoadingPromise' in source
    assert 'const libraries = Array.isArray(librariesPayload)' in source
    assert 'const series = Array.isArray(seriesPayload)' in source
    loader_start = source.index('async function ensureHeaderSearchDataLoaded()')
    loader_end = source.index('function handleHeaderSearchInput()', loader_start)
    assert 'handleHeaderSearchInput();' in source[loader_start:loader_end]

