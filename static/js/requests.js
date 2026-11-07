// Page « Demandes » : un lecteur cherche une série sur Bédéthèque et la demande. La série
// est ajoutée tout de suite à la bibliothèque par défaut et la recherche automatique des
// tomes démarre (voir blueprints/library/book_requests.py). Un administrateur voit
// toutes les demandes, avec le nom de la personne qui l'a faite.
function reqEscape(value) {
    return String(value ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
}

const REQUEST_STATUS = {
    added: 'Ajoutée, recherche des tomes lancée',
    already: 'Déjà dans la bibliothèque',
    error: 'Échec',
};

function formatRequestDate(value) {
    if (!value) return '';
    const date = new Date(String(value).replace(' ', 'T') + (String(value).endsWith('Z') ? '' : 'Z'));
    return Number.isNaN(date.getTime()) ? value : date.toLocaleDateString('fr-FR', {day: 'numeric', month: 'short', year: 'numeric'});
}

async function searchRequestable(event) {
    if (event) event.preventDefault();
    const input = document.getElementById('request-search-input');
    const box = document.getElementById('request-search-results');
    const query = input.value.trim();
    if (!query) return;
    box.innerHTML = '<div class="loading"><div class="spinner"></div><p>Recherche sur Bédéthèque…</p></div>';
    try {
        const response = await fetch(`/api/bedetheque/search?q=${encodeURIComponent(query)}`);
        const data = await response.json();
        if (!response.ok || !data.success) throw new Error(data.error || 'Recherche impossible');
        const results = data.results || [];
        if (!results.length) {
            box.innerHTML = '<p class="request-empty">Aucune série trouvée. Essayez un autre titre, ou collez l’adresse de la fiche Bédéthèque.</p>';
            return;
        }
        box.innerHTML = results.slice(0, 30).map((item, index) => `
            <div class="request-result">
                <div class="request-result-text">
                    <a href="${reqEscape(item.url)}" target="_blank" rel="noopener" class="request-result-title">${reqEscape(item.title)}</a>
                    ${item.genre ? `<span class="request-result-genre">${reqEscape(item.genre)}</span>` : ''}
                </div>
                <button type="button" class="btn btn-primary request-ask" data-index="${index}">Demander</button>
            </div>`).join('');
        box.querySelectorAll('.request-ask').forEach(button => {
            button.addEventListener('click', () => askForSeries(results[Number(button.dataset.index)], button));
        });
    } catch (error) {
        box.innerHTML = `<p class="request-empty">${reqEscape(error.message)}</p>`;
    }
}

async function askForSeries(item, button) {
    button.disabled = true;
    button.textContent = 'Envoi…';
    try {
        const response = await fetch('/api/requests', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({url: item.url, title: item.title}),
        });
        const data = await response.json();
        if (!response.ok || !data.success) throw new Error(data.error || 'Demande impossible');
        button.textContent = data.status === 'already' ? 'Déjà disponible' : 'Demandée';
        button.classList.remove('btn-primary');
        loadRequests();
    } catch (error) {
        button.disabled = false;
        button.textContent = 'Réessayer';
        alert(error.message);
    }
}

async function loadRequests() {
    const list = document.getElementById('request-list');
    try {
        const response = await fetch('/api/requests');
        const data = await response.json();
        if (!response.ok || !data.success) throw new Error(data.error || 'Chargement impossible');
        document.getElementById('request-list-title').textContent = data.mine_only ? 'Mes demandes' : 'Toutes les demandes';
        if (!data.requests.length) {
            list.innerHTML = '<p class="request-empty">Aucune demande pour le moment.</p>';
            return;
        }
        list.innerHTML = data.requests.map(item => {
            const title = item.series_id
                ? `<a href="/series/${item.series_id}" class="request-result-title">${reqEscape(item.title)}</a>`
                : `<span class="request-result-title">${reqEscape(item.title || item.url)}</span>`;
            const progress = item.series_id && item.total
                ? `<span class="request-progress">${item.owned} / ${item.total} tome${item.total > 1 ? 's' : ''} disponible${item.owned > 1 ? 's' : ''}</span>`
                : '';
            const who = data.mine_only ? '' : `<span class="request-who">${reqEscape(item.user_name || '?')}</span>`;
            const status = item.status === 'error' && item.error ? `Échec : ${item.error}` : (REQUEST_STATUS[item.status] || item.status);
            return `
                <div class="request-row request-${reqEscape(item.status)}">
                    <div class="request-result-text">
                        ${title}
                        <span class="request-meta">${who}${formatRequestDate(item.created_at)} · ${reqEscape(status)}</span>
                    </div>
                    ${progress}
                </div>`;
        }).join('');
    } catch (error) {
        list.innerHTML = `<p class="request-empty">${reqEscape(error.message)}</p>`;
    }
}

document.addEventListener('DOMContentLoaded', () => {
    document.getElementById('request-search-form').addEventListener('submit', searchRequestable);
    loadRequests();
});
