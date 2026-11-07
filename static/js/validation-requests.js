// Page Validation, section « Demandes » : demandes de BD des lecteurs et demandes de
// compte (inscription depuis la page de connexion). Un administrateur valide ou refuse ;
// la cloche de l'en-tête compte aussi ces demandes (voir nav.js, initManualReviewBadge).
// API : blueprints/library/book_requests.py et /api/auth/users(/approve).
function vrEscape(value) {
    return String(value ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
}

function vrDate(value) {
    if (!value) return '';
    const date = new Date(String(value).replace(' ', 'T') + (/[zZ]|[+-]\d\d:?\d\d$/.test(String(value)) ? '' : 'Z'));
    if (Number.isNaN(date.getTime())) return '';
    const diff = (Date.now() - date.getTime()) / 1000;
    if (diff < 60) return 'à l’instant';
    if (diff < 3600) return `il y a ${Math.round(diff / 60)} min`;
    if (diff < 86400) return `il y a ${Math.round(diff / 3600)} h`;
    if (diff < 86400 * 7) return `il y a ${Math.round(diff / 86400)} j`;
    return date.toLocaleDateString('fr-FR', {day: 'numeric', month: 'short', year: 'numeric'});
}

function vrHue(text) {
    let hash = 0;
    for (const ch of String(text || '?')) hash = (hash * 31 + ch.charCodeAt(0)) % 360;
    return hash;
}

function vrInitials(text) {
    const words = String(text || '?').replace(/\(.*?\)/g, ' ').replace(/[^\p{L}\p{N}\s]/gu, ' ')
        .replace(/^\s*(les?|la|l|the)\s+/i, '').split(/\s+/).filter(Boolean);
    return ((words[0] || '?')[0] + (words[1] ? words[1][0] : '')).toUpperCase();
}

let vrLibraries = [];

async function vrJson(url, options) {
    const response = await fetch(url, options);
    const data = await response.json().catch(() => ({}));
    if (!response.ok || data.success === false) throw new Error(data.error || `Erreur ${response.status}`);
    return data;
}

function vrBookCard(item) {
    const libs = vrLibraries.length > 1
        ? `<label class="vr-lib" data-tooltip="Bibliothèque où ajouter la série">${svgIcon('library')}
               <select data-vr-library>${vrLibraries.map(lib => `<option value="${lib.id}"${lib.id === item.library_id ? ' selected' : ''}>${vrEscape(lib.name)}</option>`).join('')}</select>
           </label>`
        : '';
    return `
        <article class="vr-card" data-vr-book="${item.id}">
            <div class="vr-thumb" style="--vr-hue:${vrHue(item.title)}">${vrEscape(vrInitials(item.title))}</div>
            <div class="vr-body">
                <div class="vr-title-row">
                    <h3>${vrEscape(item.title || item.url)}</h3>
                    <a class="vr-ext" href="${vrEscape(item.url)}" target="_blank" rel="noopener" data-tooltip="Voir la fiche Bédéthèque">${svgIcon('external-link')}</a>
                </div>
                <div class="vr-meta">${svgIcon('circle-user')} ${vrEscape(item.user_name || 'Lecteur')} <span class="vr-dot">·</span> ${svgIcon('clock')} ${vrEscape(vrDate(item.created_at))}</div>
                <div class="vr-actions">
                    ${libs}
                    <button type="button" class="vr-btn vr-btn-ok" data-vr-approve>${svgIcon('check')}<span>Ajouter</span></button>
                    <button type="button" class="vr-btn vr-btn-no" data-vr-reject>${svgIcon('x')}<span>Refuser</span></button>
                </div>
                <div class="vr-reject-box" hidden>
                    <input type="text" maxlength="300" placeholder="Motif (facultatif, visible par le lecteur)" data-vr-reason>
                    <button type="button" class="vr-btn vr-btn-no" data-vr-reject-confirm>${svgIcon('x')}<span>Confirmer le refus</span></button>
                </div>
                <div class="vr-error" hidden></div>
            </div>
        </article>`;
}

function vrAccountCard(user) {
    const name = user.name || user.username || '?';
    return `
        <article class="vr-card" data-vr-account="${vrEscape(user.key)}">
            <div class="vr-thumb vr-thumb-round" style="--vr-hue:${vrHue(name)}">${vrEscape(vrInitials(name))}</div>
            <div class="vr-body">
                <div class="vr-title-row"><h3>${vrEscape(name)}</h3><span class="vr-chip">${svgIcon('user-plus')} Demande de compte</span></div>
                <div class="vr-meta">${svgIcon('at-sign')} ${vrEscape(user.username || '')}${user.email ? ` <span class="vr-dot">·</span> ${svgIcon('mail')} ${vrEscape(user.email)}` : ''} <span class="vr-dot">·</span> ${svgIcon('clock')} ${vrEscape(vrDate(user.first_seen))}</div>
                <div class="vr-actions">
                    <label class="vr-lib" data-tooltip="Rôle du compte">${svgIcon('shield-check')}
                        <select data-vr-role><option value="reader" selected>Lecteur</option><option value="admin">Admin</option></select>
                    </label>
                    <button type="button" class="vr-btn vr-btn-ok" data-vr-approve>${svgIcon('user-check')}<span>Valider</span></button>
                    <button type="button" class="vr-btn vr-btn-no" data-vr-reject>${svgIcon('user-x')}<span>Refuser</span></button>
                </div>
                <div class="vr-error" hidden></div>
            </div>
        </article>`;
}

function vrBusy(card, busy) {
    card.querySelectorAll('button, select, input').forEach(el => { el.disabled = busy; });
    card.classList.toggle('is-busy', busy);
}

function vrFail(card, error) {
    vrBusy(card, false);
    const box = card.querySelector('.vr-error');
    box.textContent = error.message || String(error);
    box.hidden = false;
}

function vrDone(card, message, ok = true) {
    card.classList.add(ok ? 'is-done' : 'is-rejected');
    card.querySelector('.vr-actions').innerHTML = `<span class="vr-result">${svgIcon(ok ? 'check' : 'x')} ${message}</span>`;
    const box = card.querySelector('.vr-reject-box');
    if (box) box.hidden = true;
    setTimeout(() => { card.classList.add('is-leaving'); setTimeout(() => { card.remove(); vrRefreshCounts(); }, 350); }, 1600);
}

function vrRefreshCounts() {
    const section = document.getElementById('requests-validation');
    const books = section.querySelectorAll('[data-vr-book]').length;
    const accounts = section.querySelectorAll('[data-vr-account]').length;
    document.getElementById('vr-books-count').textContent = books;
    document.getElementById('vr-accounts-count').textContent = accounts;
    document.getElementById('vr-books-group').hidden = !books;
    document.getElementById('vr-accounts-group').hidden = !accounts;
    section.hidden = !(books + accounts);
    if (typeof window.refreshReviewBadge === 'function') window.refreshReviewBadge();
}

function vrBind(section) {
    section.addEventListener('click', async event => {
        const card = event.target.closest('.vr-card');
        if (!card) return;
        const bookId = card.dataset.vrBook;
        const accountKey = card.dataset.vrAccount;
        if (event.target.closest('[data-vr-approve]')) {
            vrBusy(card, true);
            try {
                if (bookId) {
                    const select = card.querySelector('[data-vr-library]');
                    const data = await vrJson(`/api/requests/${bookId}/approve`, {
                        method: 'POST', headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify(select ? {library_id: Number(select.value)} : {}),
                    });
                    vrDone(card, data.status === 'already'
                        ? 'Déjà dans la bibliothèque'
                        : `Ajoutée dans ${vrEscape(data.library || 'la bibliothèque')}, recherche des tomes lancée`);
                } else {
                    await vrJson('/api/auth/users/approve', {
                        method: 'POST', headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({key: accountKey, role: card.querySelector('[data-vr-role]').value}),
                    });
                    vrDone(card, 'Compte validé');
                }
            } catch (error) { vrFail(card, error); }
        } else if (event.target.closest('[data-vr-reject]')) {
            if (bookId) {
                const box = card.querySelector('.vr-reject-box');
                box.hidden = !box.hidden;
                if (!box.hidden) box.querySelector('input').focus();
                return;
            }
            if (!confirm('Refuser cette demande de compte ? Elle sera supprimée.')) return;
            vrBusy(card, true);
            try {
                await vrJson('/api/auth/users', {method: 'DELETE', headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({key: accountKey})});
                vrDone(card, 'Demande refusée', false);
            } catch (error) { vrFail(card, error); }
        } else if (event.target.closest('[data-vr-reject-confirm]')) {
            vrBusy(card, true);
            try {
                await vrJson(`/api/requests/${bookId}/reject`, {method: 'POST', headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({reason: card.querySelector('[data-vr-reason]').value})});
                vrDone(card, 'Demande refusée', false);
            } catch (error) { vrFail(card, error); }
        }
    });
    section.addEventListener('keydown', event => {
        if (event.key === 'Enter' && event.target.matches('[data-vr-reason]')) {
            event.target.closest('.vr-card').querySelector('[data-vr-reject-confirm]').click();
        }
    });
}

async function loadRequestValidation() {
    const section = document.getElementById('requests-validation');
    if (!section) return;
    const [books, users, libraries] = await Promise.all([
        vrJson('/api/requests?status=pending').catch(() => ({requests: []})),
        vrJson('/api/auth/users').catch(() => ({users: []})),
        vrJson('/api/libraries').catch(() => null),
    ]);
    const libList = Array.isArray(libraries) ? libraries : (libraries && (libraries.libraries || libraries.data)) || [];
    vrLibraries = libList.filter(lib => lib && lib.id != null).map(lib => ({id: lib.id, name: lib.name}));
    const pendingBooks = books.requests || [];
    const pendingAccounts = (users.users || []).filter(user => user.status === 'pending');
    document.getElementById('vr-books-list').innerHTML = pendingBooks.map(vrBookCard).join('');
    document.getElementById('vr-accounts-list').innerHTML = pendingAccounts.map(vrAccountCard).join('');
    const auto = document.getElementById('vr-auto-note');
    if (auto) auto.hidden = !books.auto_approve;
    vrRefreshCounts();
    if (location.hash === '#demandes' && !section.hidden) section.scrollIntoView({behavior: 'smooth'});
}

document.addEventListener('DOMContentLoaded', () => {
    const section = document.getElementById('requests-validation');
    if (!section) return;
    document.querySelectorAll("[data-vr-icon]").forEach(el => { el.innerHTML = svgIcon(el.dataset.vrIcon); });
    vrBind(section);
    loadRequestValidation();
});
