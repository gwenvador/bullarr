"""
Gestionnaire du scraping automatique pour l'importation de fichiers
"""
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
import json
import os
import time
from datetime import datetime
from flask import current_app


AUTO_IMPORT_FALLBACK_INTERVAL_SECONDS = 30

MAX_AUTO_IMPORT_FAILURES = 1

AUTO_IMPORT_CORRUPTION_RETRY_DELAYS = (5, 10, 30)
# Nombre de nouvelles tentatives après le premier échec. Après ce budget (~20 minutes
# cumulées) épuisé, un fichier n'est plus retenté qu'au rythme, bien plus espacé, de
# AUTO_IMPORT_CORRUPTION_COOLDOWN_SECONDS ci-dessous plutôt que jamais - voir sa
# docstring pour l'incident ayant motivé ce changement (pack qBittorrent Yojimbot: 3
# volumes tombés en "Fichier corrompu" à répétition, budget d'origine ~105s épuisé bien
# avant la fin de la copie réseau réelle du pack, plus aucune tentative automatique
# ensuite - fichiers redevenus parfaitement valides quelques minutes plus tard mais
# jamais réimportés seuls, un redémarrage de l'app étant jusqu'ici le seul moyen de
# vider self._failure_counts et leur redonner une chance).
MAX_AUTO_IMPORT_CORRUPTION_RETRIES = len(AUTO_IMPORT_CORRUPTION_RETRY_DELAYS)
# "add a manual rescan. why the import automatic was triggered if the file was not
# good. a lot of error in the logs" - une fois le budget de tentatives rapprochées
# ci-dessus épuisé, self._failure_counts restait élevé POUR TOUJOURS (aucun code ne le
# remettait à zéro), excluant le fichier de tout passage automatique suivant jusqu'à un
# redémarrage complet de l'app. Un fichier encore recopié sur un montage réseau/lent
# finit pourtant presque toujours par devenir lisible - retenté ici automatiquement,
# mais à un rythme bien plus espacé (une fois toutes les 30 min) pour ne pas revenir au
# bruit de logs d'origine sur un fichier réellement corrompu qui, lui, échouera pour
# toujours à ce rythme réduit. Une "Fichier corrompu" est le SEUL type d'échec à
# bénéficier de ce filet de sécurité additionnel (voir is_corruption plus bas) -
# jamais appliqué à une permission refusée ou une série introuvable, causes stables qui
# gardent le budget d'1 seul essai (MAX_AUTO_IMPORT_FAILURES) sans ce filet.
AUTO_IMPORT_CORRUPTION_COOLDOWN_SECONDS = 30 * 60

STALE_PART_FILE_MINUTES = 10


def _cleanup_stale_part_files(import_directories):
    """Supprime tout fichier .part dont la dernière écriture remonte à plus de
    STALE_PART_FILE_MINUTES - voir la constante ci-dessus pour le raisonnement complet.
    Best-effort par fichier (une suppression ratée n'empêche pas les autres)."""
    now = datetime.now().timestamp()
    threshold_seconds = STALE_PART_FILE_MINUTES * 60
    for import_path in import_directories:
        if not os.path.exists(import_path):
            continue
        for root, dirs, files in os.walk(import_path):
            dirs[:] = [d for d in dirs if d not in ('_uploads',)]
            for filename in files:
                if not filename.endswith('.part'):
                    continue
                part_path = os.path.join(root, filename)
                try:
                    if now - os.path.getmtime(part_path) > threshold_seconds:
                        os.remove(part_path)
                        print(f"🧹 Fichier .part abandonné supprimé: {part_path}")
                except OSError as e:
                    print(f"Erreur nettoyage .part abandonné ({part_path}): {e}")


def _load_notified_files(state_file):
    """Fichiers d'import déjà signalés par une notification "import disponible" (voir
    _check_new_files_available) - persisté pour ne notifier qu'une fois par fichier tant
    qu'il reste dans le répertoire surveillé (pas à chaque tick du planificateur)."""
    if not os.path.exists(state_file):
        return set()
    try:
        with open(state_file, 'r') as f:
            return set(json.load(f).get('known_files', []))
    except Exception:
        return set()


def _save_notified_files(state_file, known_files):
    try:
        with open(state_file, 'w') as f:
            json.dump({'known_files': sorted(known_files)}, f, indent=2)
    except Exception as e:
        print(f"Erreur sauvegarde état notification import: {e}")


class LibraryImportScheduler:
    """Gestionnaire de l'import automatique de fichiers"""

    def __init__(self, app=None):
        self.scheduler = None
        self.app = app
        self.job_id = 'library_auto_import'
        self.stalled_telegram_job_id = 'retry_stalled_telegram'
        self._file_size_history = {}
        self._failure_counts = {}
        # Dernier message d'erreur (str(e) côté _execute_import_batch) par fichier -
        # "l'échec n'est pas expliqué": le compteur seul ne dit pas POURQUOI ça échoue,
        # affiché à côté du compteur dans _repeated_failure_skip_reason (routes.py).
        self._failure_last_error = {}
        # Horodatage du dernier échec, utilisé pour ne pas retenter une archive
        # réseau à chaque tick de 5 secondes avant l'expiration de son backoff.
        self._failure_last_at = {}

    def init_app(self, app):
        """Initialiser le scheduler avec l'app Flask"""
        self.app = app

    def start(self):
        """Démarrer le scheduler"""
        if self.scheduler is None:
            self.scheduler = BackgroundScheduler(daemon=True)
            self.scheduler.start()
            print("✓ Scheduler d'import automatique démarré")

    def stop(self):
        """Arrêter le scheduler"""
        if self.scheduler and self.scheduler.running:
            self.scheduler.shutdown(wait=False)
            self.scheduler = None
            print("✓ Scheduler d'import automatique arrêté")

    def add_job(self, interval_value, interval_unit):
        """Ajouter une tâche d'import automatique"""
        self.start()

        # Supprimer la tâche existante si elle existe
        if self.scheduler.get_job(self.job_id):
            self.scheduler.remove_job(self.job_id)

        # Ajouter la nouvelle tâche
        self.scheduler.add_job(
            func=self._auto_import,
            trigger=IntervalTrigger(**{interval_unit: interval_value}),
            id=self.job_id,
            name='Library Auto Import',
            replace_existing=True
        )

        print(f"✓ Tâche d'import automatique programmée: tous les {interval_value} {interval_unit}")

    def remove_job(self):
        """Supprimer la tâche d'import automatique"""
        if self.scheduler and self.scheduler.get_job(self.job_id):
            self.scheduler.remove_job(self.job_id)
            print("✓ Tâche d'import automatique supprimée")

    def _retry_stalled_telegram_tick(self):
        """"looks like [...] is stucked. should i restart automatically?" - relance des
        téléchargements Telegram bloqués (retry_stalled_telegram_downloads, downloader.py),
        TOUJOURS active - contrairement à _auto_import (voir add_job), indépendante de
        auto_import_enabled/notify_available: un téléchargement Telegram bloqué doit être
        relancé que l'utilisateur veuille ou non l'import automatique/la notification
        "import disponible" activés, ce sont deux préoccupations différentes. Avant ce
        job dédié, cette relance ne tournait QUE piggybackée sur le planning de
        _auto_import ("même planning que l'import automatique lui-même plutôt qu'un
        scheduler dédié") - un téléchargement Telegram resté bloqué à retry_count=0
        indéfiniment (confirmé en réel) quand ces deux réglages étaient désactivés était
        la conséquence directe de ce piggyback. Contrairement à _auto_import, ne fait
        AUCUN os.walk du répertoire surveillé - uniquement des requêtes SQLite sur
        active_downloads, donc sans le coût qui justifiait de garder l'import automatique
        optionnel."""
        if not self.app:
            return
        with self.app.app_context():
            try:
                from blueprints.missing_monitor.downloader import retry_stalled_telegram_downloads
                retry_stalled_telegram_downloads()
            except Exception as e:
                print(f"Erreur lors de la relance des téléchargements Telegram bloqués: {e}")

    def add_stalled_telegram_job(self):
        """Programme _retry_stalled_telegram_tick - TOUJOURS appelée au démarrage (voir
        app.py), sans condition contrairement à add_job/sync_auto_import_schedule (voir
        la docstring de _retry_stalled_telegram_tick pour pourquoi). Intervalle aligné sur
        TELEGRAM_RETRY_AFTER_STALLED_MINUTES (downloader.py, 1 minute) pour détecter un
        blocage dans un délai comparable, pas des minutes plus tard."""
        self.start()
        if self.scheduler.get_job(self.stalled_telegram_job_id):
            return
        self.scheduler.add_job(
            func=self._retry_stalled_telegram_tick,
            trigger=IntervalTrigger(seconds=30),
            id=self.stalled_telegram_job_id,
            name='Retry Stalled Telegram Downloads',
            replace_existing=True
        )
        print("✓ Tâche de relance des téléchargements Telegram bloqués programmée: toutes les 30s")

    def _check_new_files_available(self, all_relpaths):
        """Notification Telegram best-effort ("notification pour... import disponible")
        quand de nouveaux fichiers apparaissent dans les répertoires surveillés depuis le
        dernier tick - piggybacke sur le planning de l'Import Automatique plutôt qu'un
        planificateur dédié (ne tourne donc que si l'import automatique est activé, voir
        Settings). all_relpaths: chemins relatifs (racine d'import incluse) de TOUS les
        fichiers supportés actuellement présents, pas seulement ceux auto-assignables -
        un fichier qui ne peut pas être auto-assigné (titre non reconnu) est justement le
        cas où l'utilisateur a le plus besoin d'être prévenu pour aller l'assigner
        manuellement, contrairement à un fichier auto-importé qui déclenche de toute
        façon la notification "import effectué" séparément."""
        state_file = current_app.config['IMPORT_NOTIFY_STATE_FILE']
        known = _load_notified_files(state_file)
        new_files = all_relpaths - known

        if new_files:
            try:
                from blueprints.telegram.routes import load_telegram_config, send_telegram_notification
                # Le suivi des fichiers "déjà connus" continue même si la notification est
                # décochée dans Settings > Telegram (voir _save_notified_files plus bas,
                # inconditionnel) - sans ça, ré-activer plus tard renverrait d'un coup tout
                # le backlog accumulé pendant que c'était désactivé.
                if load_telegram_config().get('notify_import_available', True):
                    sample = sorted(new_files)[:5]
                    message = f"📥 {len(new_files)} nouveau(x) fichier(s) disponible(s) pour import :\n" + "\n".join(sample)
                    if len(new_files) > len(sample):
                        message += f"\n… et {len(new_files) - len(sample)} autre(s)"
                    send_telegram_notification(message)
            except Exception as e:
                print(f"Erreur notification Telegram (import disponible): {e}")

        # Baseline = l'état courant du répertoire, qu'il y ait eu de nouveaux fichiers ou
        # non : un fichier déjà connu qui reste sur place ne doit pas re-notifier au tick
        # suivant, et un fichier importé/retiré depuis disparaît naturellement de
        # all_relpaths donc de cette baseline sans traitement particulier.
        _save_notified_files(state_file, all_relpaths)

    def _auto_import(self):
        """Fonction appelée par le scheduler pour importer automatiquement les fichiers"""
        if not self.app:
            print("Erreur: App Flask non initialisée")
            return

        with self.app.app_context():
            try:
                print(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 📦 Import automatique en cours...")


                try:
                    _cleanup_stale_part_files(current_app.config['IMPORT_DIRECTORIES'])
                except Exception as e:
                    print(f"Erreur lors du nettoyage des fichiers .part abandonnés: {e}")

                # Import local pour éviter les boucles circulaires (current_app est déjà
                # importé au niveau du module, voir le haut de ce fichier - une seconde
                # importation locale ici rendait le nom local pour TOUTE la fonction,
                # cassant toute référence à current_app plus haut dans le corps de
                # _auto_import, y compris celle du nettoyage de .part ajouté juste avant)
                from . import routes
                from .scanner import LibraryScanner
                import sqlite3

                # Charger la configuration d'import
                config = routes.load_library_import_config()
                auto_import_enabled = config.get('auto_import_enabled', False)


                # Répertoires d'import surveillés (toujours scannés ensemble)
                import_directories = current_app.config['IMPORT_DIRECTORIES']

                # Scanner les fichiers à importer
                scanner = LibraryScanner()
                supported_extensions = set(config.get(
                    'monitored_extensions', ['.cbz', '.cbr', '.zip', '.rar', '.tar', '.pdf']
                ))

                files_to_import = []
                all_relpaths = set()
                # Reconstruit à chaque passage à partir de self._file_size_history (voir
                # __init__) - un fichier disparu depuis le dernier passage (déjà importé,
                # supprimé...) ne doit pas s'y accumuler indéfiniment.
                new_size_history = {}

                # Chargé une seule fois pour tout le scan plutôt qu'une requête
                # active_downloads par fichier (voir même correctif côté
                # scan_import_directory, routes.py - "pourquoi ca met scanner en loading").
                from blueprints.missing_monitor.downloader import get_trackable_active_downloads
                trackable_downloads = get_trackable_active_downloads()

                # Même raisonnement que trackable_downloads ci-dessus: un seul appel groupé
                # à l'API qBittorrent pour tout le tick (voir get_qbittorrent_torrent_names,
                # find_active_download_destination_by_torrent_name, routes.py).
                from blueprints.qbittorrent.routes import get_qbittorrent_torrent_names
                qbittorrent_hashes = {
                    d['client_item_id'] for d in trackable_downloads
                    if d.get('client') == 'qbittorrent' and d.get('client_item_id')
                }
                torrent_names_by_hash = get_qbittorrent_torrent_names(qbittorrent_hashes)

                # Même correctif, jamais appliqué ici jusqu'à présent: find_auto_assign_
                # destination rouvrait sa PROPRE connexion et refetchait TOUTES les séries
                # de la base (jointes à libraries) pour CHAQUE fichier candidat, à CHAQUE
                # passage de ce tick de 5s - le genre de coût qui passe inaperçu avec
                # quelques dizaines de séries mais se voit avec plusieurs centaines.
                # Aucun matching flou global : les fichiers non suivis ne sont jamais
                # auto-associés à une série existante.

                # Fichiers assignés/corrigés à la main sur /import ("si jai a faire
                # manuelement un matching alors met un flag pas dimport automayique pour
                # ce vokume. import manuel seulement") - ne doivent plus jamais être
                # repris par CE scheduler automatique, seul un clic explicite sur
                # "Importer" doit les finaliser avec la correction humaine.
                from .import_history import get_manual_override_filepaths, get_in_progress_filepaths
                manual_override_filepaths = get_manual_override_filepaths()
                in_progress_filepaths = get_in_progress_filepaths()

                for import_path in import_directories:
                    if not os.path.exists(import_path):
                        print(f"⚠️ Répertoire d'import introuvable, ignoré: {import_path}")
                        continue

                    # Parcourir le répertoire
                    for root, dirs, files in os.walk(import_path):
                        # Ignorer les répertoires spéciaux
                        dirs[:] = [d for d in dirs if d not in ['_uploads']]

                        for filename in files:
                            ext = os.path.splitext(filename)[1].lower()

                            if ext in supported_extensions:
                                filepath = os.path.join(root, filename)
                                # Chemin relatif à la racine d'import concernée (préfixé
                                # par cette racine pour rester unique entre plusieurs
                                # répertoires surveillés) - identité stable d'un tick à
                                # l'autre pour _check_new_files_available, contrairement
                                # au chemin absolu qui suffirait aussi mais est moins
                                # lisible dans le message Telegram.
                                all_relpaths.add(os.path.join(
                                    os.path.basename(os.path.normpath(import_path)),
                                    os.path.relpath(filepath, import_path)
                                ))
                                # Assigné/corrigé à la main sur /import: jamais repris par
                                # ce scheduler, quel que soit ce que find_active_download_
                                # destination/can_auto_assign en déduiraient - seul un clic
                                # explicite sur "Importer" doit le finaliser (voir plus
                                # haut). La notification "nouveau fichier disponible" reste
                                # inchangée (all_relpaths déjà mis à jour ci-dessus), seule
                                # l'éligibilité à l'import AUTOMATIQUE est court-circuitée ici.
                                if filepath in manual_override_filepaths:
                                    continue

                                # Déjà réclamé par un batch d'import en cours (voir
                                # get_in_progress_filepaths ci-dessus) - ne pas le
                                # reproposer ici, l'opération en cours s'en charge déjà.
                                if filepath in in_progress_filepaths:
                                    continue

                                try:
                                    current_size = os.path.getsize(filepath)
                                except OSError:
                                    continue
                                previous_size = self._file_size_history.get(filepath)
                                new_size_history[filepath] = current_size
                                if previous_size != current_size:
                                    continue

                                parsed = scanner.parse_filename(filename)
                                relative_path = os.path.relpath(filepath, import_path)
                                parent_dir = os.path.dirname(relative_path)
                                folder_name = os.path.basename(parent_dir) if parent_dir else None
                                root_folder_name = relative_path.split(os.sep)[0] if os.sep in relative_path else None

                                # Priorité à la série connue au moment du téléchargement
                                # (voir find_active_download_destination - "get the volume
                                # number and album name not from a matching but from when
                                # the file was added") plutôt qu'à la re-dérivation depuis
                                # le nom de fichier parsé, qui reste le repli pour un
                                # fichier déposé sans être passé par cette app (aucune
                                # ligne active_downloads à retrouver).
                                destination = routes.find_active_download_destination(filename, trackable_downloads)
                                # "il ne faut pas que l'import automatique soit activé" sans
                                # numéro de tome connu nulle part (order.md) - une série
                                # connue avec certitude ne suffit pas à elle seule, voir
                                # apply_tracked_volume_and_gate (complète aussi parsed
                                # depuis le tome suivi si le nom de fichier ne l'a pas).
                                if destination and not routes.apply_tracked_volume_and_gate(parsed, destination):
                                    destination = None
                                # Repli par nom de torrent (voir même ordre côté
                                # scan_import_directory) avant le repli le plus faible par
                                # nom de dossier/fichier reparsé.
                                matched_by_torrent_container = False
                                if not destination and folder_name:
                                    destination = routes.find_active_download_destination_by_torrent_name(
                                        folder_name, trackable_downloads, torrent_names_by_hash
                                    )
                                    matched_by_torrent_container = bool(destination)
                                    if destination and not routes.apply_tracked_volume_and_gate(parsed, destination):
                                        destination = None
                                # Repli supplémentaire par dossier RACINE (voir root_folder_name
                                # ci-dessus) quand le parent direct ne matche rien: cas d'un
                                # fichier niché dans un sous-dossier du pack ("Hors série/",
                                # "Bonus/"), où seul le nom du dossier racine correspond au
                                # vrai nom du torrent.
                                if not destination and root_folder_name and root_folder_name != folder_name:
                                    destination = routes.find_active_download_destination_by_torrent_name(
                                        root_folder_name, trackable_downloads, torrent_names_by_hash
                                    )
                                    matched_by_torrent_container = bool(destination)
                                    if destination and not routes.apply_tracked_volume_and_gate(parsed, destination):
                                        destination = None
                                if not destination:
                                    destination = routes.find_active_download_destination_by_torrent_name(
                                        filename, trackable_downloads, torrent_names_by_hash
                                    )
                                    if destination and not routes.apply_tracked_volume_and_gate(parsed, destination):
                                        destination = None
                                # Every auto-import, regardless of client or whether it was
                                # found by a direct filename or a torrent-folder fallback, must
                                # prove that its parsed series title is the tracked series. A
                                # download association establishes provenance, never identity.
                                if destination and not routes._pack_file_matches_destination(parsed, destination):
                                    from .import_history import mark_import_file_manual
                                    mark_import_file_manual(filepath)
                                    destination = None
                                # Sécurité : l’auto-import ne devine jamais une série à
                                # partir d’un titre ou d’un dossier local. Seuls les fichiers
                                # rattachés à un téléchargement suivi (nom de fichier ou nom
                                # de torrent) peuvent être importés automatiquement ; le reste
                                # reste disponible pour un matching manuel dans /import.

                                last_error = self._failure_last_error.get(filepath) or ''
                                failure_count = self._failure_counts.get(filepath, 0)
                                is_corruption = last_error.startswith('Fichier corrompu')
                                if is_corruption:
                                    # Le premier échec est immédiat; les suivants sont
                                    # espacés selon AUTO_IMPORT_CORRUPTION_RETRY_DELAYS.
                                    # Le scheduler tourne toujours toutes les 5s, mais ne
                                    # relance donc pas inutilement un fichier encore copié
                                    # sur le réseau. Budget rapproché épuisé (voir
                                    # AUTO_IMPORT_CORRUPTION_COOLDOWN_SECONDS): pas
                                    # d'exclusion permanente, un passage est retenté
                                    # toutes les COOLDOWN secondes indéfiniment plutôt que
                                    # plus jamais.
                                    if failure_count > MAX_AUTO_IMPORT_CORRUPTION_RETRIES:
                                        last_failed_at = self._failure_last_at.get(filepath, 0)
                                        if time.time() - last_failed_at < AUTO_IMPORT_CORRUPTION_COOLDOWN_SECONDS:
                                            destination = None
                                    elif failure_count:
                                        last_failed_at = self._failure_last_at.get(filepath, 0)
                                        retry_delay = AUTO_IMPORT_CORRUPTION_RETRY_DELAYS[
                                            min(failure_count - 1, len(AUTO_IMPORT_CORRUPTION_RETRY_DELAYS) - 1)
                                        ]
                                        if time.time() - last_failed_at < retry_delay:
                                            destination = None
                                elif failure_count >= MAX_AUTO_IMPORT_FAILURES:
                                    destination = None

                                if destination:
                                    files_to_import.append({
                                        'filename': filename,
                                        'filepath': filepath,
                                        'import_root': import_path,
                                        'file_size': os.path.getsize(filepath),
                                        'parsed': parsed,
                                        'destination': destination
                                    })

                self._file_size_history = new_size_history
                self._check_new_files_available(all_relpaths)

                if not auto_import_enabled:
                    print("⚠️ Import automatique désactivé (notification seule)")
                    return

                if not files_to_import:
                    print("ℹ️ Aucun fichier à auto-importer trouvé")
                    return

                print(f"📦 {len(files_to_import)} fichier(s) trouvé(s) pour import automatique")

                # Exécuter l'import
                from . import routes as lib_routes
                success, stats = lib_routes.execute_auto_import(files_to_import)

                failed_errors = {f['filepath']: f.get('error') for f in stats.get('failures', []) if f.get('filepath')}
                for file_data in files_to_import:
                    filepath = file_data['filepath']
                    if filepath in failed_errors:
                        self._failure_counts[filepath] = self._failure_counts.get(filepath, 0) + 1
                        self._failure_last_error[filepath] = failed_errors[filepath]
                        self._failure_last_at[filepath] = time.time()
                    else:
                        self._failure_counts.pop(filepath, None)
                        self._failure_last_error.pop(filepath, None)
                        self._failure_last_at.pop(filepath, None)

                if success:
                    print(f"✓ Import automatique complété: {stats['imported_count']} importés")
                else:
                    print(f"✗ Erreur lors de l'import automatique")

            except Exception as e:
                print(f"✗ Erreur lors de l'import automatique: {e}")
                import traceback
                traceback.print_exc()


# Instance globale du scheduler
library_import_scheduler = LibraryImportScheduler()


def sync_auto_import_schedule():
    ""
    from . import routes
    from blueprints.telegram.routes import load_telegram_config

    import_config = routes.load_library_import_config()
    telegram_config = load_telegram_config()

    auto_import_enabled = import_config.get('auto_import_enabled', False)
    notify_available = telegram_config.get('enabled', False) and telegram_config.get('notify_import_available', True)

    if auto_import_enabled or notify_available:
        library_import_scheduler.add_job(AUTO_IMPORT_FALLBACK_INTERVAL_SECONDS, 'seconds')
    else:
        library_import_scheduler.remove_job()
