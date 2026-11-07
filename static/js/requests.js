// Page « Demandes » des lecteurs : chercher une série sur Bédéthèque, la demander, suivre
// ses demandes. Selon le réglage « Valider automatiquement les demandes », une demande
// est ajoutée tout de suite ou attend la décision d'un administrateur (page Validation).
// Les administrateurs n'ont pas cette page : /demandes les renvoie vers /validation.
// API : blueprints/library/book_requests.py.
function reqEscape(value) {
    return String(value ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
}

const REQUEST_STATES = {
    pending:   {label: 'En attente de validation', icon: 'clock', tone: 'wait'},
    added:     {label: 'Ajoutée', icon: 'check', tone: 'ok'},
    already:   {label: 'Déjà disponible', icon: 'library', tone: 'ok'},
    rejected:  {label: 'Refusée', icon: 'circle-x', tone: 'no'},
    error:     {label: 'Échec', icon: 'triangle-alert', tone: 'no'},
    cancelled: {label: 'Annulée', icon: 'undo', tone: 'muted'},
};
const REQUEST_FILTERS = [
    ['all', 'Toutes'], ['pending', 'En attente'], ['done', 'Ajoutées'], ['rejected', 'Refusées'],
];

let reqAll = [];
let reqFilter = 'all';
let reqAutoApprove = false;

function reqDate(value) {
    if (!value) return '';
    const date = new Date(String(value).replace(' ', 'T') + (/[zZ]$/.test(String(value)) ? '' : 'Z'));
    return Number.isNaN(date.getTime()) ? '' : date.toLocaleDateString('fr-FR', {day: 'numeric', month: 'short', year: 'numeric'});
}

function reqHue(text) {
    let hash = 0;
    for (const ch of String(text || '?')) hash = (hash * 31 + ch.charCodeAt(0)) % 360;
    return hash;
}

function reqInitials(text) {
    const words = String(text || '?').replace(/\(.*?\)/g, ' ').replace(/[^\p{L}\p{N}\s]/gu, ' ')
        .replace(/^\s*(les?|la|l|the)\s+/i, '').split(/\s+/).filter(Boolean);
    return ((words[0] || '?')[0] + (words[1] ? words[1][0] : '')).toUpperCase();
}

function reqThumb(title, coverUrl) {
    if (coverUrl) return `<div class="rq-thumb"><img src="${reqEscape(coverUrl)}" alt="" loading="lazy" onerror="this.parentNode.classList.add('rq-thumb-empty');this.remove()"></div>`;
    return `<div class="rq-thumb rq-thumb-letters" style="--rq-hue:${reqHue(title)}">${reqEscape(reqInitials(title))}</div>`;
}

function reqProgress(owned, total) {
    if (!total) return '';
    const pct = Math.round(100 * Math.min(owned, total) / total);
    return `<div class="rq-progress" data-tooltip="${owned} / ${total} tome${total > 1 ? 's' : ''} disponible${owned > 1 ? 's' : ''}">
        <div class="rq-progress-bar"><span style="width:${pct}%"></span></div>
        <span class="rq-progress-text">${owned}/${total}</span></div>`;
}

function reqChip(status) {
    const state = REQUEST_STATES[status] || {label: status, icon: 'info', tone: 'muted'};
    return `<span class="rq-chip rq-tone-${state.tone}">${svgIcon(state.icon)}${reqEscape(state.label)}</span>`;
}

// ----- Recherche -----
function resultAction(item, info) {
    if (info && info.state === 'library') {
        return `<a class="rq-btn rq-btn-ghost" href="/series/${info.series_id}">${svgIcon('book-open')}<span>Ouvrir</span></a>`;
    }
    if (info && info.state === 'pending') return reqChip('pending');
    return `<button type="button" class="rq-btn rq-btn-primary" data-rq-ask>${svgIcon('book-plus')}<span>Demander</span></button>`;
}

function resultCard(item, index, info) {
    const inLibrary = info && info.state === 'library';
    return `
        <article class="rq-result${inLibrary ? ' is-owned' : ''}" data-rq-index="${index}">
            ${reqThumb(item.title, info && info.cover_url)}
            <div class="rq-result-body">
                <h3>${reqEscape(item.title)}</h3>
                <div class="rq-result-meta">
                    ${item.genre ? `<span class="rq-genre">${reqEscape(item.genre)}</span>` : ''}
                    ${inLibrary ? `<span class="rq-chip rq-tone-ok">${svgIcon('library')}Dans la bibliothèque</span>` : ''}
                    <a class="rq-ext" href="${reqEscape(item.url)}" target="_blank" rel="noopener" data-tooltip="Fiche Bédéthèque">${svgIcon('external-link')}Bédéthèque</a>
                </div>
                ${inLibrary ? reqProgress(info.owned, info.total) : ''}
            </div>
            <div class="rq-result-action">${resultAction(item, info)}</div>
        </article>`;
}

async function searchRequestable(event) {
    if (event) event.preventDefault();
    const input = document.getElementById('request-search-input');
    const box = document.getElementById('request-search-results');
    const query = input.value.trim();
    if (!query) return;
    box.innerHTML = '<div class="rq-loading"><div class="spinner"></div><span>Recherche sur Bédéthèque…</span></div>';
    try {
        const response = await fetch(`/api/bedetheque/search?q=${encodeURIComponent(query)}`);
        const data = await response.json();
        if (!response.ok || !data.success) throw new Error(data.error || 'Recherche impossible');
        // « Correspondance exacte » : mention de Bédéthèque collée au titre, pas le titre.
        const results = (data.results || []).slice(0, 30)
            .map(item => ({...item, title: String(item.title || '').replace(/\s*Correspondance exacte\s*$/i, '')}));
        if (!results.length) {
            box.innerHTML = `<div class="rq-empty">${svgIcon('search')}<p>Aucune série trouvée.<br>Essayez un autre titre, ou collez l’adresse de la fiche Bédéthèque.</p></div>`;
            return;
        }
        let lookup = {};
        try {
            const lookupResponse = await fetch('/api/requests/lookup', {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({urls: results.map(item => item.url)}),
            });
            lookup = (await lookupResponse.json()).items || {};
        } catch (e) { /* état facultatif */ }
        box.innerHTML = `<div class="rq-results-head">${results.length} résultat${results.length > 1 ? 's' : ''}</div>`
            + results.map((item, index) => resultCard(item, index, lookup[item.url])).join('');
        box.querySelectorAll('[data-rq-ask]').forEach(button => {
            const card = button.closest('[data-rq-index]');
            button.addEventListener('click', () => askForSeries(results[Number(card.dataset.rqIndex)], card));
        });
    } catch (error) {
        box.innerHTML = `<div class="rq-empty">${svgIcon('triangle-alert')}<p>${reqEscape(error.message)}</p></div>`;
    }
}

async function askForSeries(item, card) {
    const slot = card.querySelector('.rq-result-action');
    const button = slot.querySelector('button');
    button.disabled = true;
    button.innerHTML = `${svgIcon('loader-circle', 'icon-spin')}<span>Envoi…</span>`;
    try {
        const response = await fetch('/api/requests', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({url: item.url, title: item.title}),
        });
        const data = await response.json();
        if (!response.ok || !data.success) throw new Error(data.error || 'Demande impossible');
        if (data.series_id && data.status !== 'pending') {
            slot.innerHTML = `<a class="rq-btn rq-btn-ghost" href="/series/${data.series_id}">${svgIcon('book-open')}<span>Ouvrir</span></a>`;
            reqToast(data.status === 'already' ? 'Cette série est déjà dans la bibliothèque.'
                : 'Série ajoutée : la recherche des tomes est lancée.');
        } else {
            slot.innerHTML = reqChip('pending');
            reqToast(data.duplicate ? 'Vous avez déjà demandé cette série.' : 'Demande envoyée à un administrateur.');
        }
        card.classList.add('is-asked');
        loadRequests();
    } catch (error) {
        button.disabled = false;
        button.innerHTML = `${svgIcon('refresh-cw')}<span>Réessayer</span>`;
        reqToast(error.message, true);
    }
}

function reqToast(message, error = false) {
    let toast = document.getElementById('rq-toast');
    if (!toast) {
        toast = document.createElement('div');
        toast.id = 'rq-toast';
        toast.setAttribute('role', 'status');
        document.body.appendChild(toast);
    }
    toast.className = `rq-toast${error ? ' is-error' : ''} is-visible`;
    toast.textContent = message;
    clearTimeout(reqToast.timer);
    reqToast.timer = setTimeout(() => toast.classList.remove('is-visible'), 3200);
}

// ----- Mes demandes -----
function requestMatches(item) {
    if (reqFilter === 'all') return true;
    if (reqFilter === 'done') return item.status === 'added' || item.status === 'already';
    if (reqFilter === 'rejected') return item.status === 'rejected' || item.status === 'error';
    return item.status === reqFilter;
}

function requestRow(item) {
    const title = item.series_id
        ? `<a href="/series/${item.series_id}">${reqEscape(item.title)}</a>`
        : reqEscape(item.title || item.url);
    const note = item.status === 'rejected' && item.error ? `<div class="rq-note">${svgIcon('info')}${reqEscape(item.error)}</div>`
        : item.status === 'error' && item.error ? `<div class="rq-note">${svgIcon('triangle-alert')}${reqEscape(item.error)}</div>` : '';
    const when = item.decided_at && item.status !== 'pending'
        ? `Demandée le ${reqDate(item.created_at)} · traitée le ${reqDate(item.decided_at)}`
        : `Demandée le ${reqDate(item.created_at)}`;
    const cancel = item.status === 'pending'
        ? `<button type="button" class="rq-icon-btn" data-rq-cancel="${item.id}" data-tooltip="Annuler la demande" aria-label="Annuler la demande">${svgIcon('trash-2')}</button>` : '';
    return `
        <article class="rq-row rq-row-${reqEscape(item.status)}">
            ${reqThumb(item.title, item.cover_url)}
            <div class="rq-row-body">
                <div class="rq-row-top"><h3>${title}</h3>${reqChip(item.status)}</div>
                <div class="rq-row-meta">${svgIcon('calendar')}${reqEscape(when)}${item.library_name && item.series_id ? ` · ${svgIcon('library')}${reqEscape(item.library_name)}` : ''}</div>
                ${item.series_id ? reqProgress(item.owned, item.total) : ''}
                ${note}
            </div>
            <div class="rq-row-action">${cancel}</div>
        </article>`;
}

function renderRequests() {
    const list = document.getElementById('request-list');
    const counts = {all: reqAll.length};
    for (const [key] of REQUEST_FILTERS.slice(1)) {
        const saved = reqFilter; reqFilter = key;
        counts[key] = reqAll.filter(requestMatches).length;
        reqFilter = saved;
    }
    document.getElementById('request-filters').innerHTML = REQUEST_FILTERS.map(([key, label]) =>
        `<button type="button" class="rq-filter${key === reqFilter ? ' is-active' : ''}" data-rq-filter="${key}">${label}<span>${counts[key]}</span></button>`
    ).join('');
    const items = reqAll.filter(requestMatches);
    if (!reqAll.length) {
        list.innerHTML = `<div class="rq-empty">${svgIcon('inbox')}<p>Aucune demande pour le moment.<br>Cherchez une série ci-dessus pour la demander.</p></div>`;
        return;
    }
    list.innerHTML = items.length ? items.map(requestRow).join('')
        : `<div class="rq-empty rq-empty-small"><p>Rien dans cette catégorie.</p></div>`;
}

async function loadRequests() {
    const list = document.getElementById('request-list');
    try {
        const response = await fetch('/api/requests');
        const data = await response.json();
        if (!response.ok || !data.success) throw new Error(data.error || 'Chargement impossible');
        reqAll = (data.requests || []).filter(item => item.status !== 'cancelled');
        reqAutoApprove = !!data.auto_approve;
        document.getElementById('request-mode').innerHTML = reqAutoApprove
            ? `${svgIcon('zap')}<span>Les séries demandées sont ajoutées tout de suite, et la recherche des tomes démarre seule.</span>`
            : `${svgIcon('shield-check')}<span>Chaque demande est validée par un administrateur avant d’être ajoutée.</span>`;
        renderRequests();
    } catch (error) {
        list.innerHTML = `<div class="rq-empty">${svgIcon('triangle-alert')}<p>${reqEscape(error.message)}</p></div>`;
    }
}

async function cancelRequest(id, button) {
    if (!confirm('Annuler cette demande ?')) return;
    button.disabled = true;
    try {
        const response = await fetch(`/api/requests/${id}`, {method: 'DELETE'});
        const data = await response.json();
        if (!response.ok || !data.success) throw new Error(data.error || 'Annulation impossible');
        reqToast('Demande annulée.');
        loadRequests();
    } catch (error) {
        button.disabled = false;
        reqToast(error.message, true);
    }
}

document.addEventListener('DOMContentLoaded', () => {
    document.getElementById('request-search-form').addEventListener('submit', searchRequestable);
    document.querySelectorAll('[data-rq-icon]').forEach(el => { el.innerHTML = svgIcon(el.dataset.rqIcon); });
    document.getElementById('request-filters').addEventListener('click', event => {
        const button = event.target.closest('[data-rq-filter]');
        if (!button) return;
        reqFilter = button.dataset.rqFilter;
        renderRequests();
    });
    document.getElementById('request-list').addEventListener('click', event => {
        const button = event.target.closest('[data-rq-cancel]');
        if (button) cancelRequest(button.dataset.rqCancel, button);
    });
    loadRequests();
});
