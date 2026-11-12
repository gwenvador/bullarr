"""Historique de lecture (Suivi → Lectures) et exports CSV de la bibliothèque.

Historique : l'avancement du compte courant, à la même source que le reste de l'app
(user_link.progress_source) : compte Komga relié, compte Komga du serveur (administrateur,
synchronisé toutes les 15 minutes) ou avancement local enregistré par le lecteur de Bullarr.
Export CSV pour sauvegarde ; l'import remet l'avancement sans jamais faire reculer un tome
déjà plus avancé (et l'envoie à Komga quand c'est là que l'avancement est gardé).

Les CSV sont en UTF-8 avec BOM et séparateur « ; » : ils s'ouvrent tels quels dans Excel
ou LibreOffice en français. L'import accepte aussi la virgule."""
import csv
import io
import json
import os
import sqlite3
from datetime import datetime, timezone

from flask import Response, current_app, jsonify, request

from error_utils import error_message
from . import library_bp

READING_COLUMNS = ['bibliotheque', 'serie', 'tome', 'numero', 'fichier', 'page', 'pages', 'lu',
                   'date_lecture', 'komga_book_id', 'serie_id', 'volume_id']
# Une ligne par album : ses propres données (fichier, ComicInfo, Bédéthèque, lecture) puis
# celles de sa série, soit toutes les colonnes des tableaux Bibliothèque et Série.
VOLUME_COLUMNS = [
    'bibliotheque', 'serie', 'tome', 'numero', 'integrale', 'hors_serie', 'titre_album', 'resume_album',
    'date_parution', 'annee', 'scenariste', 'dessinateur', 'coloriste', 'auteur', 'editeur', 'genre',
    'isbn', 'note_bedetheque', 'votes_bedetheque', 'lien_bedetheque', 'releaser', 'format', 'pages',
    'taille_mo', 'largeur_px', 'hauteur_px', 'qualite', 'lu', 'page_lue', 'date_lecture',
    'serie_statut', 'serie_complete', 'serie_type', 'serie_langue', 'serie_annee_debut', 'serie_annee_fin',
    'serie_resume', 'serie_auteur', 'serie_editeurs', 'serie_genre', 'serie_univers', 'serie_tags',
    'serie_one_shot', 'serie_tomes_bedetheque', 'serie_tomes_manquants', 'serie_date_ajout',
    'serie_derniere_update', 'serie_lien_bedetheque', 'serie_matching_komga', 'serie_matching_ebdz',
    'fichier', 'chemin', 'komga_book_id', 'serie_id', 'volume_id']
SOURCE_LABELS = {
    'komga': 'Compte Komga du serveur',
    'komga_user': 'Votre compte Komga',
    'local': 'Lecteur de Bullarr (avancement propre au compte)',
}


def _db():
    conn = sqlite3.connect(current_app.config['DATABASE'], timeout=60.0)
    conn.row_factory = sqlite3.Row
    return conn


def volume_label(row):
    """« Tome 3 », « Intégrale 2 », « HS 1 »… comme « Continuer la lecture »."""
    if row['is_integral']:
        return f"Intégrale {_num(row['integral_number'])}".strip()
    if row['is_hs']:
        return f"HS {_num(row['hs_number'])}".strip()
    if row['volume_number'] is not None:
        return f"Tome {_num(row['volume_number'])}"
    return os.path.splitext(row['filename'] or '')[0]


def _num(value):
    if value is None:
        return ''
    try:
        number = float(value)
        return str(int(number)) if number.is_integer() else str(number).replace('.', ',')
    except (TypeError, ValueError):
        return str(value)


def _csv_response(columns, rows, filename):
    buffer = io.StringIO()
    buffer.write('﻿')
    writer = csv.writer(buffer, delimiter=';', lineterminator='\r\n')
    writer.writerow(columns)
    writer.writerows(rows)
    response = Response(buffer.getvalue(), mimetype='text/csv; charset=utf-8')
    response.headers['Content-Disposition'] = f'attachment; filename="{filename}"'
    response.headers['Cache-Control'] = 'no-store'
    return response


def _stamp():
    return datetime.now().strftime('%Y-%m-%d')


# ----- Historique de lecture -----
def _source():
    from blueprints.komga.user_link import current_progress_source
    return current_progress_source()


def _account_role(row, admin_group):
    """Rôle d'un compte de la table users (même règle que roles.role_for_user)."""
    from blueprints.auth.roles import ROLES, ROLE_ADMIN, computed_role
    if row['role_override'] in ROLES:
        return row['role_override']
    if row['auth_method'] == 'password':
        return ROLE_ADMIN
    return computed_role(json.loads(row['groups_json'] or '[]'), admin_group)


def _accounts():
    from blueprints.auth.config_store import load_oidc_config
    admin_group = load_oidc_config().get('admin_group')
    conn = _db()
    try:
        rows = conn.execute('SELECT user_key, name, email, auth_method, groups_json, role_override, status '
                            'FROM users ORDER BY COALESCE(last_seen, first_seen) DESC').fetchall()
    finally:
        conn.close()
    return [(row, _account_role(row, admin_group)) for row in rows]


def _request_source():
    """(source, user_key) de l'historique demandé. Un administrateur peut choisir le compte
    (?user=<user_key>) ; un lecteur voit toujours le sien."""
    from blueprints.auth.roles import ROLE_READER, current_user_key, is_reader
    from blueprints.komga.user_link import progress_source
    own = current_user_key()
    wanted = request.values.get('user') or ''
    if not wanted or wanted == own or is_reader():
        return _source(), own
    for row, role in _accounts():
        if row['user_key'] == wanted:
            return progress_source(wanted, role == ROLE_READER), wanted
    return None, wanted


def _unknown_user():
    return jsonify({'success': False, 'error': 'Compte introuvable'}), 404


@library_bp.route('/api/reading-history/users', methods=['GET'])
def get_reading_history_users():
    """Comptes dont un administrateur peut voir l'historique (route réservée aux
    administrateurs : absente de READER_ENDPOINTS), avec la source de chacun."""
    from blueprints.auth.roles import ROLE_READER, current_user_key
    from blueprints.komga.user_link import progress_source
    own = current_user_key()
    users = []
    for row, role in _accounts():
        source = progress_source(row['user_key'], role == ROLE_READER)
        users.append({'user_key': row['user_key'], 'name': row['name'] or row['email'] or row['user_key'],
                      'email': row['email'], 'role': role, 'me': row['user_key'] == own,
                      'source': source.kind, 'source_label': SOURCE_LABELS.get(source.kind, source.kind)})
    if own and not any(u['me'] for u in users):
        users.insert(0, {'user_key': own, 'name': 'Moi', 'email': None, 'role': 'admin', 'me': True,
                         'source': _source().kind, 'source_label': SOURCE_LABELS.get(_source().kind)})
    return jsonify({'success': True, 'users': users})


def reading_rows(source, library_id=None):
    conn = _db()
    try:
        return conn.execute('''
            SELECT p.page, p.pages_count, p.completed, p.read_at, p.synced_at,
                   v.id AS volume_id, v.series_id, v.filename, v.volume_number, v.is_integral,
                   v.integral_number, v.is_hs, v.hs_number, v.page_count, v.komga_book_id, v.cover_path,
                   s.title AS series_title, s.local_cover_path, s.bedetheque_cover_path, s.komga_cover_path,
                   l.id AS library_id, l.name AS library_name
            FROM {source} p
            JOIN volumes v ON v.id = p.vol_id
            JOIN series s ON s.id = v.series_id
            LEFT JOIN libraries l ON l.id = s.library_id
            WHERE (COALESCE(p.page, 0) > 0 OR COALESCE(p.completed, 0) = 1)
              AND (? IS NULL OR s.library_id = ?)
            ORDER BY COALESCE(p.read_at, p.synced_at) DESC, s.title, v.volume_number
        '''.format(source=source.sql), (*source.params, library_id, library_id)).fetchall()
    finally:
        conn.close()


def _last_sync(source):
    if source.kind == 'local':
        return None
    try:
        if source.kind == 'komga_user':
            from blueprints.komga.user_link import _link_row
            row = _link_row(source.params[0])
            return row['last_sync_at'] if row else None
        from blueprints.komga import read_progress
        at = (read_progress.last_sync or {}).get('at')
        if at:
            return at
        # Pas encore de synchronisation depuis le démarrage : la plus récente enregistrée.
        conn = _db()
        try:
            row = conn.execute('SELECT MAX(synced_at) FROM komga_read_progress').fetchone()
        finally:
            conn.close()
        return row[0] if row else None
    except Exception:  # noqa: BLE001 - simple information
        return None


@library_bp.route('/lectures')
def reading_history_page():
    from flask import render_template
    return render_template('reading-history.html')


@library_bp.route('/api/reading-history', methods=['GET'])
def get_reading_history():
    """Tomes lus ou commencés par le compte courant, du plus récent au plus ancien."""
    source, _ = _request_source()
    if source is None:
        return _unknown_user()
    items = []
    for row in reading_rows(source, request.args.get('library_id', type=int)):
        pages = row['pages_count'] or row['page_count']
        items.append({
            'volume_id': row['volume_id'], 'series_id': row['series_id'], 'series_title': row['series_title'],
            'library_id': row['library_id'], 'library_name': row['library_name'],
            'label': volume_label(row), 'filename': row['filename'],
            'page': row['page'], 'pages_count': pages, 'completed': bool(row['completed']),
            'read_at': row['read_at'] or row['synced_at'],
            'cover_path': row['cover_path'] or row['local_cover_path'] or row['bedetheque_cover_path']
                          or row['komga_cover_path'],
        })
    last = _last_sync(source)
    if isinstance(last, (int, float)):
        last = datetime.fromtimestamp(last, timezone.utc).isoformat()
    return jsonify({'success': True, 'source': source.kind, 'source_label': SOURCE_LABELS.get(source.kind, source.kind),
                    'last_sync': last, 'items': items})


@library_bp.route('/api/reading-history/export.csv', methods=['GET'])
def export_reading_history():
    source, _ = _request_source()
    if source is None:
        return _unknown_user()
    rows = [[row['library_name'] or '', row['series_title'], volume_label(row), _num(row['volume_number']),
             row['filename'], row['page'] or '', row['pages_count'] or row['page_count'] or '',
             'oui' if row['completed'] else 'non', row['read_at'] or row['synced_at'] or '',
             row['komga_book_id'] or '', row['series_id'], row['volume_id']]
            for row in reading_rows(source)]
    return _csv_response(READING_COLUMNS, rows, f'bullarr-lectures-{_stamp()}.csv')


def parse_csv(raw):
    """Lignes d'un CSV (dict colonne → valeur), séparateur « ; » ou « , »."""
    text = raw.decode('utf-8-sig', errors='replace') if isinstance(raw, bytes) else raw
    first = text.split('\n', 1)[0]
    delimiter = ';' if first.count(';') >= first.count(',') else ','
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    return [{(k or '').strip().lower(): (v or '').strip() for k, v in row.items()} for row in reader]


def _truthy(value):
    return str(value or '').strip().lower() in ('oui', 'yes', 'true', '1', 'vrai', 'x', 'lu')


def _int(value):
    try:
        return int(float(str(value).replace(',', '.')))
    except (TypeError, ValueError):
        return None


def _norm(text):
    return ' '.join(str(text or '').lower().split())


def match_volumes(conn, rows):
    """Tome visé par chaque ligne : identifiant Komga, sinon série + fichier, sinon série + numéro."""
    volumes = conn.execute('''
        SELECT v.id, v.series_id, v.filename, v.volume_number, v.is_integral, v.is_hs, v.komga_book_id,
               v.page_count, s.title AS series_title
        FROM volumes v JOIN series s ON s.id = v.series_id''').fetchall()
    by_book = {v['komga_book_id']: v for v in volumes if v['komga_book_id']}
    by_file = {(_norm(v['series_title']), _norm(v['filename'])): v for v in volumes}
    by_number = {}
    for v in volumes:
        if v['volume_number'] is not None and not v['is_integral'] and not v['is_hs']:
            by_number.setdefault((_norm(v['series_title']), float(v['volume_number'])), v)
    out = []
    for row in rows:
        volume = by_book.get(row.get('komga_book_id') or '')
        if volume is None:
            volume = by_file.get((_norm(row.get('serie')), _norm(row.get('fichier'))))
        if volume is None and row.get('numero'):
            try:
                volume = by_number.get((_norm(row.get('serie')), float(row['numero'].replace(',', '.'))))
            except ValueError:
                volume = None
        out.append(volume)
    return out


def _write_progress(source, user_key, volume, page, completed, read_at):
    """Écrit l'avancement d'un tome là où ce compte le garde. Renvoie None ou une erreur."""
    from blueprints.komga import read_progress, user_link
    from blueprints.komga.client import KomgaClient, KomgaError
    pages_count = volume['page_count']
    if source.kind == 'local':
        conn = user_link._connect()
        try:
            conn.execute(
                'INSERT INTO user_local_read_progress (user_key, series_id, filename, page, pages_count, completed, read_at, updated_at) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP) '
                'ON CONFLICT(user_key, series_id, filename) DO UPDATE SET page = excluded.page, completed = excluded.completed, '
                'pages_count = COALESCE(excluded.pages_count, user_local_read_progress.pages_count), '
                'read_at = excluded.read_at, updated_at = excluded.updated_at',
                (user_key, volume['series_id'], volume['filename'], page, pages_count, int(completed), read_at))
            conn.commit()
        finally:
            conn.close()
        return None
    if not volume['komga_book_id']:
        return "Tome absent de Komga"
    client = user_link.user_client(user_key) if source.kind == 'komga_user' else KomgaClient()
    try:
        client.update_read_progress(volume['komga_book_id'], page, completed)
    except KomgaError as e:
        return error_message(e)
    table = 'user_komga_read_progress' if source.kind == 'komga_user' else 'komga_read_progress'
    try:
        if source.kind == 'komga_user':
            user_link.record_user_progress(user_key, volume['komga_book_id'], page, completed, pages_count)
        else:
            read_progress.record_local_progress(volume['komga_book_id'], page, completed, pages_count)
        if read_at:
            # Date d'origine gardée dans Bullarr (Komga note la date de l'import).
            conn = _db()
            try:
                where = 'komga_book_id = ?' + (' AND user_key = ?' if source.kind == 'komga_user' else '')
                params = (read_at, volume['komga_book_id']) + ((user_key,) if source.kind == 'komga_user' else ())
                conn.execute(f'UPDATE {table} SET read_at = ? WHERE {where}', params)
                conn.commit()
            finally:
                conn.close()
    except sqlite3.Error as e:
        print(f'Avancement importé non reflété localement : {e}')
    return None


def import_reading_rows(source, user_key, rows):
    """Applique les lignes d'un export : un tome n'est jamais ramené en arrière (déjà lu, ou
    page plus loin). Renvoie le bilan."""
    stats = {'total': len(rows), 'applied': 0, 'unchanged': 0, 'unmatched': 0, 'errors': []}
    current = {row['volume_id']: row for row in reading_rows(source)}
    conn = _db()
    try:
        volumes = match_volumes(conn, rows)
    finally:
        conn.close()
    done = set()
    for line, (row, volume) in enumerate(zip(rows, volumes), start=2):
        if volume is None:
            stats['unmatched'] += 1
            continue
        completed = _truthy(row.get('lu'))
        page = _int(row.get('page')) or (volume['page_count'] if completed else None)
        if not page or page < 1:
            stats['unchanged'] += 1
            continue
        if volume['page_count']:
            page = min(page, volume['page_count'])
        now = current.get(volume['id'])
        if volume['id'] in done or (now is not None and (now['completed'] or (not completed and (now['page'] or 0) >= page))):
            stats['unchanged'] += 1
            continue
        error = _write_progress(source, user_key, volume, page, completed, row.get('date_lecture') or None)
        if error:
            stats['errors'].append({'line': line, 'serie': row.get('serie'), 'tome': row.get('tome'), 'error': error})
        else:
            stats['applied'] += 1
            done.add(volume['id'])
    return stats


@library_bp.route('/api/reading-history/import', methods=['POST'])
def import_reading_history():
    """POST multipart (file) : remet l'avancement d'un export CSV de l'historique de lecture."""
    upload = request.files.get('file')
    if not upload:
        return jsonify({'success': False, 'error': 'Aucun fichier envoyé'}), 400
    try:
        rows = parse_csv(upload.read())
    except (csv.Error, UnicodeError) as e:
        return jsonify({'success': False, 'error': f'CSV illisible : {e}'}), 400
    if rows and 'serie' not in rows[0] and 'komga_book_id' not in rows[0]:
        return jsonify({'success': False, 'error': "Colonnes attendues : celles d'un export de l'historique (serie, fichier, page, lu…)"}), 400
    source, user_key = _request_source()
    if source is None:
        return _unknown_user()
    if source.kind == 'komga':
        from blueprints.komga.config_store import load_komga_config
        if not load_komga_config().get('enabled'):
            return jsonify({'success': False, 'error': "Komga n'est pas activé : l'avancement ne peut pas être enregistré"}), 400
    stats = import_reading_rows(source, user_key, rows)
    return jsonify({'success': True, **stats, 'errors': stats['errors'][:50], 'error_count': len(stats['errors'])})


# ----- Export CSV des BD -----
def _json(value, default):
    try:
        data = json.loads(value) if value else default
    except (TypeError, ValueError):
        return default
    return data if isinstance(data, type(default)) else default


def _date_parution(ci):
    year, month, day = ci.get('year'), ci.get('month'), ci.get('day')
    if not year:
        return ''
    if month and day and not (str(month) == '1' and str(day) == '1'):
        return f'{int(year):04d}-{int(month):02d}-{int(day):02d}' if str(month).isdigit() and str(day).isdigit() else str(year)
    return str(year)


def _quality(width, height, low_def):
    side = max(width or 0, height or 0)
    if not side:
        return ''
    return 'Basse déf.' if side <= low_def else ('Standard' if side < 2500 else 'HD')


def _rating(ci):
    value = ci.get('communityrating')
    return str(value).replace('.', ',') if value not in (None, '') else ''


@library_bp.route('/api/export/volumes.csv', methods=['GET'])
def export_volumes_csv():
    """Toutes les BD (tomes présents sur le disque) d'une bibliothèque (?library_id=) ou de
    toutes les bibliothèques, avec les données de l'album et de sa série."""
    from . import upscale
    from .routes import _series_last_change_dates
    library_id = request.args.get('library_id', type=int)
    source = _source()
    conn = _db()
    try:
        name = None
        if library_id:
            lib = conn.execute('SELECT name FROM libraries WHERE id = ?', (library_id,)).fetchone()
            if not lib:
                return jsonify({'success': False, 'error': 'Bibliothèque introuvable'}), 404
            name = lib['name']
        rows = conn.execute('''
            SELECT v.*, s.title AS series_title, l.name AS library_name,
                   COALESCE(NULLIF(TRIM(s.manual_genre), ''), NULLIF(TRIM(s.bedetheque_genre), ''), NULLIF(TRIM(s.local_genre), '')) AS series_genre,
                   COALESCE(NULLIF(TRIM(s.manual_author), ''), NULLIF(TRIM(s.local_author), '')) AS series_author,
                   COALESCE(NULLIF(TRIM(s.manual_summary), ''), NULLIF(TRIM(s.local_summary), ''), NULLIF(TRIM(s.bedetheque_description), ''), NULLIF(TRIM(s.komga_summary), '')) AS series_summary,
                   COALESCE(NULLIF(TRIM(s.manual_status), ''), s.bedetheque_status) AS series_status,
                   CAST(COALESCE(s.manual_year_start, s.bedetheque_year_start, s.local_year) AS TEXT) AS series_year_start,
                   CAST(COALESCE(s.manual_year_end, s.bedetheque_year_end) AS TEXT) AS series_year_end,
                   s.bedetheque_editeurs, s.bedetheque_origin, s.bedetheque_language, s.bedetheque_url AS series_url,
                   s.bedetheque_total_volumes, s.missing_volumes, s.tags, s.is_oneshot, s.added_at AS series_added_at,
                   s.manual_complete_override, s.bedetheque_complete, s.bedetheque_albums,
                   s.komga_match_status, s.ebdz_match_status, u.name AS universe_name,
                   p.page AS read_page, p.completed AS read_completed, p.read_at AS read_at
            FROM volumes v JOIN series s ON s.id = v.series_id
            LEFT JOIN libraries l ON l.id = s.library_id
            LEFT JOIN universes u ON u.id = s.universe_id
            LEFT JOIN {progress} p ON p.vol_id = v.id
            WHERE v.filepath IS NOT NULL AND (? IS NULL OR s.library_id = ?)
            ORDER BY l.name, s.title COLLATE NOCASE, v.is_integral, v.is_hs, v.volume_number, v.filename
        '''.format(progress=source.sql), (*source.params, library_id, library_id)).fetchall()
        try:
            last_changes = _series_last_change_dates(conn.cursor())
        except sqlite3.Error:
            last_changes = {}
    finally:
        conn.close()
    low_def = int(upscale.low_def_max_side())
    isbn_cache = {}
    out = []
    for row in rows:
        ci = _json(row['comicinfo'], {})
        if row['series_id'] not in isbn_cache:
            isbn_cache[row['series_id']] = {str(a.get('url') or '').rstrip('/'): a.get('isbn')
                                            for a in _json(row['bedetheque_albums'], []) if isinstance(a, dict) and a.get('isbn')}
        web = str(ci.get('web') or '').rstrip('/')
        size = row['file_size'] or 0
        missing = _json(row['missing_volumes'], [])
        complete = row['manual_complete_override'] or row['bedetheque_complete']
        changed = max(filter(None, (last_changes.get(row['series_id']), row['series_added_at'])), default=None)
        out.append([
            row['library_name'] or '', row['series_title'], volume_label(row), _num(row['volume_number']),
            _num(row['integral_number']) if row['is_integral'] else '', _num(row['hs_number']) if row['is_hs'] else '',
            ci.get('title') or '', ci.get('summary') or '', _date_parution(ci), row['year'] or ci.get('year') or '',
            ci.get('writer') or '', ci.get('penciller') or '', ci.get('colorist') or '',
            row['author'] or row['series_author'] or '', ci.get('publisher') or _editeur(row['bedetheque_editeurs']),
            ci.get('genre') or row['series_genre'] or '', isbn_cache[row['series_id']].get(web) or '',
            _rating(ci), ci.get('communityratingcount') or '', web, row['release_group'] or '',
            (row['format'] or '').lower(), row['page_count'] or '',
            f'{size / 1048576:.1f}'.replace('.', ',') if size else '',
            row['page_width'] or '', row['page_height'] or '', _quality(row['page_width'], row['page_height'], low_def),
            'oui' if row['read_completed'] else ('en cours' if (row['read_page'] or 0) > 0 else 'non'),
            row['read_page'] or '', row['read_at'] or '',
            row['series_status'] or '', '' if complete is None else ('oui' if complete else 'non'),
            row['bedetheque_origin'] or '', row['bedetheque_language'] or '',
            row['series_year_start'] or '', row['series_year_end'] or '', row['series_summary'] or '',
            row['series_author'] or '', _editeur(row['bedetheque_editeurs']), row['series_genre'] or '',
            row['universe_name'] or '', ', '.join(_json(row['tags'], [])), 'oui' if row['is_oneshot'] else 'non',
            row['bedetheque_total_volumes'] or '', ', '.join(_num(m) for m in missing),
            row['series_added_at'] or '', changed or '', row['series_url'] or '',
            'oui' if row['komga_match_status'] == 'matched' else 'non',
            'oui' if row['ebdz_match_status'] == 'matched' else 'non',
            row['filename'], row['filepath'], row['komga_book_id'] or '', row['series_id'], row['id']])
    slug = ''.join(c if c.isalnum() else '-' for c in (name or 'toutes-les-bibliotheques').lower()).strip('-')
    return _csv_response(VOLUME_COLUMNS, out, f'bullarr-bd-{slug}-{_stamp()}.csv')


def _editeur(value):
    """bedetheque_editeurs est une liste JSON ou un texte."""
    if not value:
        return ''
    try:
        import json
        data = json.loads(value)
        if isinstance(data, list):
            return ', '.join(str(item.get('name') if isinstance(item, dict) else item) for item in data if item)
    except (ValueError, TypeError):
        pass
    return str(value)
