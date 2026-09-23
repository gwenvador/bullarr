from blueprints.bedetheque.auto_acquire import _manual_match_target_from_info


def test_manual_album_match_resolves_selected_album_number_not_first_or_last():
    info = {
        'title': '(AUT) Juillard',
        'volumes': [
            {'number': 1999, 'title': 'Pêle Mêle - Monographie', 'url': 'https://www.bedetheque.com/BD-AUT-Juillard-1999-Pele-Mele-Monographie-26901.html'},
            {'number': 2026, 'title': 'La Honte : 50 Nuances De Rouge', 'url': 'https://www.bedetheque.com/BD-AUT-Juillard-2026-La-Honte-50-Nuances-De-Rouge-540377.html'},
        ],
    }
    target = _manual_match_target_from_info(info, info['volumes'][0]['url'])
    assert target == {'url': info['volumes'][0]['url'], 'number': 1999, 'title': 'Pêle Mêle - Monographie'}
