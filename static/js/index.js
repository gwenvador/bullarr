let libraries = [];

async function loadLibraries() {
    const container = document.getElementById('libraries-container');

    try {
        const response = await fetch('/api/libraries');
        libraries = await response.json();

        if (libraries.length === 0) {
            container.innerHTML = `
                <div class="no-data">
                    <div class="no-data-icon">📚</div>
                    <h3>Aucune bibliothèque</h3>
                    <p>Créez votre première bibliothèque pour commencer</p>
                </div>
            `;
            return;
        }

        // Une seule bibliothèque: on l'ouvre directement, pas la peine de faire cliquer sur
        // sa carte à chaque fois. ?all=1 (lien "Gérer les bibliothèques" dans Configuration)
        // permet de revenir à cette liste, par exemple pour en créer une deuxième
        const params = new URLSearchParams(window.location.search);
        if (libraries.length === 1 && !params.has('all')) {
            window.location.href = `/library/${libraries[0].id}`;
            return;
        }

        displayLibraries(libraries);
    } catch (error) {
        container.innerHTML = `
            <div class="no-data">
                <h3>Erreur de chargement</h3>
                <p>${escapeHtml(String(error.message))}</p>
            </div>
        `;
    }
}

function displayLibraries(libs) {
    const container = document.getElementById('libraries-container');

    const cardsHtml = libs.map(lib => {
        return `
            <div class="library-card" onclick="window.location.href='/library/${lib.id}'" style="cursor: pointer;">
                <div class="library-header">
                    <div style="flex: 1;">
                        <div class="library-name">${escapeHtml(lib.name)}</div>
                        ${lib.description ? `<div class="library-description">${escapeHtml(lib.description)}</div>` : ''}
                    </div>
                </div>

                <div class="library-path">${escapeHtml(lib.path)}</div>

                <div class="library-stats">
                    <div class="stat-item">
                        <div class="stat-value">${lib.series_count}</div>
                        <div class="stat-label">Séries</div>
                    </div>
                    <div class="stat-item">
                        <div class="stat-value">${lib.volumes_count}</div>
                        <div class="stat-label">Volumes</div>
                    </div>
                </div>

                <div class="library-actions" onclick="event.stopPropagation()">
                    <button class="btn" onclick="window.location.href='/library/${lib.id}'">
                        ${svgIcon('book-open')} Ouvrir
                    </button>
                    <button class="btn" onclick="scanLibrary(${lib.id})">
                        ${svgIcon('refresh-cw')} Scanner
                    </button>
                    <button class="btn btn-danger" onclick="deleteLibraryConfirm(${lib.id})">
                        ${svgIcon('trash-2')} Supprimer
                    </button>
                </div>

                <div class="library-footer">
                    ${lib.last_scanned ?
                        `<span class="badge badge-success">Dernière analyse: ${parseDbUtcDate(lib.last_scanned)?.toLocaleString('fr-FR', { timeZone: BULLARR_DISPLAY_TZ }) ?? '—'}</span>` :
                        `<span class="badge badge-warning">Jamais analysée</span>`
                    }
                </div>
            </div>
        `;
    }).join('');

    container.innerHTML = `<div class="libraries-grid">${cardsHtml}</div>`;
}

function openCreateModal() {
    document.getElementById('create-modal').classList.add('active');
}

function closeCreateModal() {
    document.getElementById('create-modal').classList.remove('active');
    document.getElementById('create-form').reset();
    document.getElementById('create-form').style.display = '';
    document.getElementById('onboard-progress').style.display = 'none';
    document.getElementById('onboard-summary').style.display = 'none';
    document.getElementById('onboard-summary').innerHTML = '';
}

async function createLibrary(event) {
    event.preventDefault();

    const name = document.getElementById('library-name').value;
    const path = document.getElementById('library-path').value;
    const description = document.getElementById('library-description').value;
    const onboardExisting = document.getElementById('library-onboard-existing').checked;

    try {
        const response = await fetch('/api/libraries', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({ name, path, description })
        });

        const data = await response.json();

        if (data.success) {
            if (onboardExisting && data.id) {
                startLibraryOnboarding(data.id);
            } else {
                alert('✅ Bibliothèque créée avec succès !');
                closeCreateModal();
                loadLibraries();
            }
        } else {
            alert('❌ Erreur: ' + (data.error || 'Erreur inconnue'));
        }
    } catch (error) {
        alert('❌ Erreur de connexion: ' + error.message);
    }
}

// Enchaîne scan + matching Bédéthèque + résumé ComicInfo pour une bibliothèque qui
// contient déjà des fichiers (case "library-onboard-existing" cochée) - voir
// blueprints/library/onboarding.py pour la logique serveur. Remplace le formulaire par
// une vue de progression pendant le job, puis par un résumé une fois terminé, plutôt que
// le simple alert() de succès utilisé pour une bibliothèque vide.
async function startLibraryOnboarding(libraryId) {
    document.getElementById('create-form').style.display = 'none';
    document.getElementById('onboard-progress').style.display = '';

    try {
        const response = await fetch(`/api/libraries/${libraryId}/onboard`, { method: 'POST' });
        const data = await response.json();
        if (!data.success) {
            alert('❌ Erreur: ' + (data.error || 'Erreur inconnue'));
            closeCreateModal();
            loadLibraries();
            return;
        }
    } catch (error) {
        alert('❌ Erreur de connexion: ' + error.message);
        closeCreateModal();
        loadLibraries();
        return;
    }

    pollLibraryOnboarding(libraryId);
}

const ONBOARD_PHASE_LABELS = {
    scanning: 'Analyse du dossier...',
    matching: 'Recherche des séries sur Bédéthèque...',
    checking_comicinfo: 'Vérification des métadonnées ComicInfo...',
};

function pollLibraryOnboarding(libraryId) {
    const progressText = document.getElementById('onboard-progress-text');

    const tick = async () => {
        let status;
        try {
            const response = await fetch(`/api/libraries/${libraryId}/onboard/status`);
            const data = await response.json();
            status = data.status;
        } catch (error) {
            // Une erreur réseau ponctuelle ne doit pas arrêter le suivi - le job continue
            // côté serveur, on retente au prochain tick.
            setTimeout(tick, 2000);
            return;
        }

        if (!status) {
            setTimeout(tick, 2000);
            return;
        }

        if (status.phase === 'matching' && status.total) {
            progressText.textContent = `Recherche des séries sur Bédéthèque... (${status.done}/${status.total}${status.current_series ? ' - ' + status.current_series : ''})`;
        } else {
            progressText.textContent = ONBOARD_PHASE_LABELS[status.phase] || 'Traitement en cours...';
        }

        if (status.phase === 'done') {
            renderOnboardSummary(libraryId, status);
        } else if (status.phase === 'error') {
            renderOnboardError(status.error);
        } else {
            setTimeout(tick, 2000);
        }
    };

    tick();
}

function renderOnboardError(error) {
    document.getElementById('onboard-progress').style.display = 'none';
    const summary = document.getElementById('onboard-summary');
    summary.style.display = '';
    summary.innerHTML = `
        <div style="background: #fee2e2; padding: 15px; border-radius: 8px; color: #991b1b; margin-bottom: 15px;">
            ❌ L'import guidé a échoué : ${escapeHtml(error || 'erreur inconnue')}
        </div>
        <div class="form-actions">
            <button type="button" class="btn btn-success" onclick="closeCreateModal(); loadLibraries();">Fermer</button>
        </div>
    `;
}

function renderOnboardSummary(libraryId, status) {
    document.getElementById('onboard-progress').style.display = 'none';
    const summary = document.getElementById('onboard-summary');
    summary.style.display = '';

    const matchedCount = status.matched.length;
    const uncertainCount = status.uncertain.length;
    const comicInfo = status.comicinfo_summary || { series_count: 0, series_unmatched: 0, volumes_with_issues: 0 };

    let uncertainHtml = '';
    if (uncertainCount > 0) {
        const items = status.uncertain.slice(0, 10).map(u => `<li>${escapeHtml(u.title)}${u.reason ? ` — <span style="color:#666;">${escapeHtml(u.reason)}</span>` : ''}</li>`).join('');
        uncertainHtml = `
            <div style="margin-top: 10px;">
                <strong>${uncertainCount} ${pluralize(uncertainCount, 'série')} non ${pluralize(uncertainCount, 'trouvée')} sur Bédéthèque :</strong>
                <ul style="margin: 8px 0 0 20px; padding: 0;">${items}</ul>
                ${uncertainCount > 10 ? `<p style="color:#666; margin: 5px 0 0 0;">... et ${uncertainCount - 10} autre(s). Voir la page Vérification pour matcher manuellement.</p>` : ''}
            </div>
        `;
    }

    summary.innerHTML = `
        <div style="background: #f0fdf4; padding: 15px; border-radius: 8px; margin-bottom: 15px;">
            <h3 style="margin: 0 0 10px 0;">✅ Bibliothèque créée et analysée</h3>
            <p style="margin: 0 0 5px 0;">${matchedCount} ${pluralize(matchedCount, 'série')} ${pluralize(matchedCount, 'trouvée')} et ${pluralize(matchedCount, 'matchée')} sur Bédéthèque.</p>
            <p style="margin: 0 0 5px 0;">
                ComicInfo : ${comicInfo.series_count} ${pluralize(comicInfo.series_count, 'série')} ${pluralize(comicInfo.series_count, 'analysée')},
                ${comicInfo.series_unmatched} ${pluralize(comicInfo.series_unmatched, 'non matchée')},
                ${comicInfo.volumes_with_issues} ${pluralize(comicInfo.volumes_with_issues, 'volume')} avec métadonnées manquantes/incomplètes.
            </p>
            ${uncertainHtml}
        </div>
        <div class="form-actions">
            <a href="/verification" class="btn">Voir la Vérification</a>
            <button type="button" class="btn btn-success" onclick="window.location.href='/library/${libraryId}'">Ouvrir la bibliothèque</button>
        </div>
    `;
}

async function scanLibrary(libraryId) {
    if (!confirm('Voulez-vous scanner cette bibliothèque ? Cela peut prendre du temps.')) {
        return;
    }

    try {
        const response = await fetch(`/api/scan/${libraryId}`);
        const data = await response.json();

        if (data.success) {
            alert(`✅ Scan terminé ! ${data.series_count} séries trouvées.`);
            loadLibraries();
        } else {
            alert('❌ Erreur: ' + (data.error || 'Erreur inconnue'));
        }
    } catch (error) {
        alert('❌ Erreur: ' + error.message);
    }
}

async function deleteLibraryConfirm(libraryId) {
    const library = libraries.find(lib => lib.id === libraryId);
    if (!library) return;

    const libraryName = library.name;

    if (!confirm(`Voulez-vous vraiment supprimer la bibliothèque "${libraryName}" ?\n\nCela supprimera toutes les données associées (séries et volumes scannés).\nLes fichiers sur votre disque ne seront PAS supprimés.`)) {
        return;
    }

    try {
        const response = await fetch(`/api/libraries/${libraryId}`, {
            method: 'DELETE'
        });

        const data = await response.json();

        if (data.success) {
            alert('✅ Bibliothèque supprimée avec succès !');
            loadLibraries();
        } else {
            alert('❌ Erreur: ' + (data.error || 'Erreur inconnue'));
        }
    } catch (error) {
        alert('❌ Erreur: ' + error.message);
    }
}


function handleFolderSelect(event) {
    const files = event.target.files;
    if (files.length > 0) {
        const firstFile = files[0];
        let folderPath = firstFile.webkitRelativePath || firstFile.name;

        const pathParts = folderPath.split('/');
        if (pathParts.length > 1) {
            pathParts.pop();
            folderPath = pathParts.join('/');
        }

        if (firstFile.path) {
            const fullPath = firstFile.path;
            const fileName = firstFile.name;
            folderPath = fullPath.substring(0, fullPath.lastIndexOf(fileName.split('/').pop()));
            folderPath = folderPath.replace(/\\/g, '/').replace(/\/$/, '');
        }

        document.getElementById('library-path').value = folderPath;
    }
}

async function searchSeriesGlobal() {
    const query = navNormalizeSearch(document.getElementById('global-search').value.trim());
    const resultsContainer = document.getElementById('search-results-container');

    if (!query) {
        resultsContainer.innerHTML = '';
        return;
    }

    try {
        // Récupérer toutes les séries de toutes les bibliothèques
        const results = [];

        for (const library of libraries) {
            try {
                const response = await fetch(`/api/library/${library.id}/series`);
                if (!response.ok) continue;

                const series = await response.json();

                // Filtrer les séries qui correspondent à la recherche
                const matching = series.filter(s => navNormalizeSearch(s.title).includes(query));

                matching.forEach(s => {
                    results.push({
                        ...s,
                        library_id: library.id,
                        library_name: library.name
                    });
                });
            } catch (error) {
                console.error(`Erreur recherche bibliothèque ${library.id}:`, error);
            }
        }

        // Afficher les résultats
        if (results.length === 0) {
            resultsContainer.innerHTML = `
                <div style="background: #f3f4f6; padding: 20px; border-radius: 8px; text-align: center; color: #666;">
                    Aucune série trouvée
                </div>
            `;
            return;
        }

        // Grouper par bibliothèque
        const grouped = {};
        results.forEach(series => {
            if (!grouped[series.library_name]) {
                grouped[series.library_name] = [];
            }
            grouped[series.library_name].push(series);
        });

        let html = `
            <div style="background: white; border-radius: 8px; padding: 20px;">
                <h3 style="margin: 0 0 15px 0; color: #333;">📚 ${results.length} ${pluralize(results.length, 'série')} ${pluralize(results.length, 'trouvée')}</h3>
        `;

        for (const [libraryName, seriesList] of Object.entries(grouped)) {
            html += `
                <div style="margin-bottom: 20px;">
                    <h4 style="color: #667eea; margin: 0 0 10px 0;">📖 ${escapeHtml(libraryName)}</h4>
                    <div style="display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 10px;">
            `;

            seriesList.forEach(series => {
                html += `
                    <div style="background: #f9fafb; padding: 12px; border-radius: 6px; cursor: pointer; border: 1px solid #e5e7eb; transition: all 0.2s;"
                         onmouseover="this.style.borderColor='#667eea'; this.style.boxShadow='0 2px 8px rgba(102, 126, 234, 0.1)';"
                         onmouseout="this.style.borderColor='#e5e7eb'; this.style.boxShadow='none';"
                         onclick="viewSeries(${series.id})">
                        <div style="font-weight: 600; color: #333; margin-bottom: 5px;">${escapeHtml(series.title)}</div>
                        <div style="font-size: 0.85em; color: #666;">📖 ${series.total_volumes} ${pluralize(series.total_volumes, 'volume')}</div>
                    </div>
                `;
            });

            html += `
                    </div>
                </div>
            `;
        }

        html += `</div>`;
        resultsContainer.innerHTML = html;
    } catch (error) {
        console.error('Erreur recherche:', error);
        resultsContainer.innerHTML = `
            <div style="background: #fee2e2; padding: 15px; border-radius: 8px; color: #991b1b;">
                ❌ Erreur lors de la recherche
            </div>
        `;
    }
}

function escapeHtml(text) {
    const map = {
        '&': '&amp;',
        '<': '&lt;',
        '>': '&gt;',
        '"': '&quot;',
        "'": '&#039;'
    };
    return text.replace(/[&<>"']/g, m => map[m]);
}



window.onclick = function(event) {
    const createModal = document.getElementById('create-modal');
    const seriesModal = document.getElementById('series-modal');

    if (event.target == createModal) {
        closeCreateModal();
    }
    if (event.target == seriesModal) {
        closeModal();
    }
}

window.addEventListener('load', loadLibraries);