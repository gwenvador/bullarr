// Carte « Utilisateurs et rôles » (onglet Connexion des paramètres).
// Comptes locaux : créés ici ou par inscription (validée par un admin), modifiables et
// supprimables. Comptes OIDC : apparaissent après leur première connexion ; rôle calculé
// depuis le groupe d'administrateurs Pocket ID (aucun groupe : tout le monde est Admin).
function usersEscape(value) {
    return String(value ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
}

const ROLE_LABELS = {admin: 'Admin', reader: 'Lecteur'};
let usersByKey = {};
let accessCatalog = null;

function uaIcon(name) {
    return typeof svgIcon === 'function' ? svgIcon(name, 'ua-icon') : '';
}

function uaInitials(text) {
    const words = String(text || '?').replace(/[@._-]+/g, ' ').trim().split(/\s+/);
    return ((words[0] || '?')[0] + (words.length > 1 ? words[words.length - 1][0] : (words[0][1] || ''))).toUpperCase();
}

function uaHue(text) {
    let hash = 0;
    for (const char of String(text)) hash = (hash * 31 + char.charCodeAt(0)) % 360;
    return hash;
}

// Dates SQLite (UTC, « AAAA-MM-JJ HH:MM:SS ») → « 7 nov. 2026 ».
function uaDate(value) {
    if (!value) return '';
    const date = new Date(String(value).replace(' ', 'T') + 'Z');
    return isNaN(date) ? '' : date.toLocaleDateString('fr-FR', {day: 'numeric', month: 'short', year: 'numeric'});
}

function uaIdentity(user) {
    const title = user.name || user.username || user.email || user.key;
    const meta = [];
    if (user.type === 'local' && user.username) meta.push(`${uaIcon('at-sign')}${usersEscape(user.username)}`);
    if (user.email) meta.push(`${uaIcon('mail')}${usersEscape(user.email)}`);
    const face = user.avatar_url ? `<img src="${usersEscape(user.avatar_url)}" alt="" loading="lazy">` : usersEscape(uaInitials(title));
    const avatar = user.status === 'pending'
        ? `<span class="ua-avatar" style="--ua-hue:${uaHue(user.key)}" aria-hidden="true">${face}</span>`
        : `<button type="button" class="ua-avatar users-avatar-btn" data-key="${usersEscape(user.key)}" style="--ua-hue:${uaHue(user.key)}" aria-label="Changer l’avatar de ${usersEscape(title)}" data-tooltip="Changer l’avatar">${face}</button>`;
    return `${avatar}
        <div class="ua-ident">
            <div class="ua-name">${usersEscape(title)}${user.is_me ? '<span class="ua-me">vous</span>' : ''}</div>
            ${meta.length ? `<div class="ua-meta">${meta.map(m => `<span>${m}</span>`).join('')}</div>` : ''}
        </div>`;
}

function accessSummary(user) {
    const parts = [];
    parts.push(user.allowed_libraries === null ? 'Toutes les bibliothèques'
        : `${user.allowed_libraries.length} bibliothèque${user.allowed_libraries.length > 1 ? 's' : ''}`);
    if (user.blocked_genres.length) parts.push(`${user.blocked_genres.length} genre${user.blocked_genres.length > 1 ? 's' : ''} masqué${user.blocked_genres.length > 1 ? 's' : ''}`);
    return parts.join(' · ');
}

async function loadAccessCatalog() {
    if (accessCatalog) return accessCatalog;
    const [libsResponse, genresResponse] = await Promise.all([fetch('/api/libraries'), fetch('/api/auth/genres')]);
    const libraries = await libsResponse.json();
    const genres = await genresResponse.json();
    accessCatalog = {libraries, genres: genres.genres || [], preset: genres.preset || []};
    return accessCatalog;
}

// Panneau sous la ligne du compte : bibliothèques permises + contrôle parental (genres).
async function toggleAccessEditor(key) {
    const row = document.getElementById(`users-access-${key}`);
    if (!row) return;
    if (!row.hidden) { row.hidden = true; return; }
    const user = usersByKey[key];
    const catalog = await loadAccessCatalog();
    const blocked = new Set(user.blocked_genres.map(g => g.toLocaleLowerCase('fr')));
    // Genres bloqués absents de la bibliothèque actuelle (ou du préréglage) restent listés.
    const genreNames = [...new Set([...catalog.genres.map(g => g.name), ...catalog.preset, ...user.blocked_genres])];
    const counts = Object.fromEntries(catalog.genres.map(g => [g.name, g.count]));
    row.innerHTML = `
        <div class="users-access-editor">
            <div class="users-access-section">
                <h4>Bibliothèques visibles</h4>
                ${catalog.libraries.map(lib => `
                    <label class="users-check"><input type="checkbox" class="access-lib" value="${lib.id}"
                        ${user.allowed_libraries === null || user.allowed_libraries.includes(lib.id) ? 'checked' : ''}> ${usersEscape(lib.name)}</label>`).join('')}
            </div>
            <div class="users-access-section">
                <h4>Contrôle parental : genres masqués</h4>
                <div class="users-access-tools">
                    <button type="button" class="btn access-preset">Préréglage « Enfant »</button>
                    <button type="button" class="btn access-clear">Aucun</button>
                </div>
                <div class="users-genres">
                    ${genreNames.map(name => `
                        <label class="users-genre-chip"><input type="checkbox" class="access-genre" value="${usersEscape(name)}"
                            ${blocked.has(name.toLocaleLowerCase('fr')) ? 'checked' : ''}> ${usersEscape(name)}${counts[name] ? ` <small>${counts[name]}</small>` : ''}</label>`).join('')}
                </div>
            </div>
            <div class="users-access-actions">
                <button type="button" class="btn btn-primary access-save">Enregistrer</button>
            </div>
        </div>`;
    const presetSet = new Set(catalog.preset.map(g => g.toLocaleLowerCase('fr')));
    row.querySelector('.access-preset').addEventListener('click', () => {
        row.querySelectorAll('.access-genre').forEach(box => { box.checked = box.checked || presetSet.has(box.value.toLocaleLowerCase('fr')); });
    });
    row.querySelector('.access-clear').addEventListener('click', () => {
        row.querySelectorAll('.access-genre').forEach(box => { box.checked = false; });
    });
    row.querySelector('.access-save').addEventListener('click', () => saveUserAccess(key, row));
    row.hidden = false;
}

async function saveUserAccess(key, row) {
    const libBoxes = [...row.querySelectorAll('.access-lib')];
    const checkedLibs = libBoxes.filter(box => box.checked).map(box => Number(box.value));
    if (!checkedLibs.length) {
        showUsersMessage('Cochez au moins une bibliothèque', false);
        return;
    }
    const response = await fetch('/api/auth/users/access', {
        method: 'PUT', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
            key,
            allowed_libraries: checkedLibs.length === libBoxes.length ? null : checkedLibs,
            blocked_genres: [...row.querySelectorAll('.access-genre')].filter(box => box.checked).map(box => box.value),
        }),
    });
    const data = await response.json().catch(() => ({}));
    showUsersMessage(response.ok && data.success ? 'Accès enregistrés' : (data.error || 'Erreur'), response.ok && data.success);
    if (response.ok && data.success) loadUsersRoles();
}

function showUsersMessage(text, ok) {
    const box = document.getElementById('usersRolesMessage');
    if (!box) return;
    box.textContent = text;
    box.className = `message ${ok ? 'success' : 'error'}`;
    box.style.display = text ? 'block' : 'none';
}

async function usersRequest(method, payload, url = '/api/auth/users') {
    const response = await fetch(url, {
        method, headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload),
    });
    const data = await response.json().catch(() => ({}));
    return {ok: response.ok && data.success, data};
}

function roleOptions(user) {
    // Local : Admin ou Lecteur. OIDC : Automatique (groupe) / Admin / Lecteur.
    const current = user ? user.role_override : 'reader';
    const auto = user && user.type === 'oidc'
        ? `<option value="" ${!current ? 'selected' : ''}>Automatique (${ROLE_LABELS[user.computed_role]})</option>` : '';
    const isAdmin = user && user.type !== 'oidc' ? (current || 'admin') === 'admin' : current === 'admin';
    const isReader = current === 'reader';
    return `${auto}<option value="admin" ${isAdmin ? 'selected' : ''}>Admin</option>
            <option value="reader" ${isReader ? 'selected' : ''} ${user && user.is_me ? 'disabled' : ''}>Lecteur</option>`;
}

// Formulaire de création (user absent) ou d'édition, affiché sous le compte.
function accountForm(user) {
    const local = !user || user.type === 'local';
    return `<form class="ua-form" autocomplete="off">
        <div class="ua-form-title">${uaIcon(user ? 'pencil' : 'user-plus')}${user ? 'Modifier le compte' : 'Nouveau compte local'}</div>
        <div class="ua-form-grid">
        ${!user ? `<label>Identifiant<input type="text" name="username" required minlength="2" maxlength="64"
                    pattern="[A-Za-z0-9._@\\-]+" placeholder="ex. camille" autocomplete="off"></label>` : ''}
        ${local ? `<label>Nom affiché<input type="text" name="name" maxlength="120" value="${usersEscape(user ? user.name : '')}"></label>
            <label>E-mail<input type="email" name="email" maxlength="200" value="${usersEscape(user ? user.email : '')}"></label>
            <label>${user ? 'Nouveau mot de passe <small>(vide : inchangé)</small>' : 'Mot de passe <small>(8 caractères min.)</small>'}
                <input type="password" name="password" ${user ? '' : 'required'} minlength="8" autocomplete="new-password"></label>`
            : '<p class="ua-form-note">Le nom et l’e-mail d’un compte OIDC viennent de Pocket ID.</p>'}
        <label>Rôle<select name="role">${roleOptions(user)}</select></label>
        </div>
        <div class="ua-form-actions">
            <button type="button" class="btn ua-btn-ghost users-form-cancel">Annuler</button>
            <button type="submit" class="btn btn-primary ua-btn">${uaIcon('check')}${user ? 'Enregistrer' : 'Créer le compte'}</button>
        </div>
    </form>`;
}

function bindAccountForm(container, user, onClose) {
    const form = container.querySelector('form');
    form.querySelector('.users-form-cancel').addEventListener('click', onClose);
    form.addEventListener('submit', async event => {
        event.preventDefault();
        const values = Object.fromEntries(new FormData(form).entries());
        const payload = {role: values.role || null};
        if (!user || user.type === 'local') {
            payload.name = (values.name || '').trim();
            payload.email = (values.email || '').trim();
            if (values.password) payload.password = values.password;
        }
        let result;
        if (user) {
            payload.key = user.key;
            result = await usersRequest('PUT', payload);
        } else {
            payload.username = (values.username || '').trim();
            result = await usersRequest('POST', payload);
        }
        showUsersMessage(result.ok ? (user ? 'Compte modifié' : 'Compte créé') : (result.data.error || 'Erreur'), result.ok);
        if (result.ok) loadUsersRoles();
    });
    const first = form.querySelector('input, select');
    if (first) first.focus();
}

function openCreateAccount() {
    const panel = document.getElementById('usersCreatePanel');
    if (!panel) return;
    if (!panel.hidden) { panel.hidden = true; return; }
    panel.innerHTML = accountForm(null);
    panel.hidden = false;
    bindAccountForm(panel, null, () => { panel.hidden = true; });
}

function closeDrawers(except) {
    document.querySelectorAll('#usersRolesCard .ua-drawer').forEach(drawer => {
        if (drawer !== except) drawer.hidden = true;
    });
}

function openEditAccount(key) {
    const drawer = document.getElementById(`users-edit-${key}`);
    const user = usersByKey[key];
    if (!drawer || !user) return;
    if (!drawer.hidden) { drawer.hidden = true; return; }
    closeDrawers(drawer);
    drawer.innerHTML = accountForm(user);
    drawer.hidden = false;
    bindAccountForm(drawer, user, () => { drawer.hidden = true; });
}

async function deleteAccount(key) {
    const user = usersByKey[key];
    if (!user) return;
    const label = user.name || user.username || user.email || key;
    const warning = user.type === 'oidc'
        ? `Supprimer le compte « ${label} » de Bullarr ?\n\nSes listes personnelles et ses accès seront effacés et sa session fermée. `
          + 'Il pourra se reconnecter via OIDC tant qu\'il existe dans Pocket ID : retirez-le là-bas pour le bloquer.'
        : `Supprimer le compte « ${label} » ?\n\nSes listes personnelles seront effacées et sa session fermée. Action irréversible.`;
    if (!confirm(warning)) return;
    const result = await usersRequest('DELETE', {key});
    showUsersMessage(result.ok ? 'Compte supprimé' : (result.data.error || 'Erreur'), result.ok);
    if (result.ok) loadUsersRoles();
}

async function approveRequest(key, role) {
    const result = await usersRequest('POST', {key, role}, '/api/auth/users/approve');
    showUsersMessage(result.ok ? 'Compte validé : la personne peut maintenant se connecter' : (result.data.error || 'Erreur'), result.ok);
    if (result.ok) loadUsersRoles();
}

async function rejectRequest(key) {
    const user = usersByKey[key];
    if (!user || !confirm(`Refuser la demande de « ${user.name || user.username} » ? Elle sera supprimée.`)) return;
    const result = await usersRequest('DELETE', {key});
    showUsersMessage(result.ok ? 'Demande refusée' : (result.data.error || 'Erreur'), result.ok);
    if (result.ok) loadUsersRoles();
}

function pendingRow(user) {
    const key = usersEscape(user.key);
    return `<div class="ua-row ua-row-pending">
        <div class="ua-main">
            ${uaIdentity(user)}
            <div class="ua-tags"><span class="ua-tag ua-tag-wait">${uaIcon('clock')}Demandé le ${usersEscape(uaDate(user.first_seen))}</span></div>
            <div class="ua-actions">
                <select class="ua-approve-role" data-key="${key}" aria-label="Rôle à donner">
                    <option value="reader" selected>Lecteur</option><option value="admin">Admin</option>
                </select>
                <button type="button" class="btn ua-btn ua-btn-ok ua-approve" data-key="${key}">${uaIcon('check')}Valider</button>
                <button type="button" class="btn ua-btn ua-btn-no ua-reject" data-key="${key}">${uaIcon('x')}Refuser</button>
            </div>
        </div>
    </div>`;
}

function userRow(user) {
    const key = usersEscape(user.key);
    const typeTag = user.type === 'oidc'
        ? `<span class="ua-tag ua-tag-oidc" data-tooltip="Connexion via Pocket ID">${uaIcon('shield-check')}OIDC</span>`
        : `<span class="ua-tag ua-tag-local" data-tooltip="Identifiant et mot de passe Bullarr">${uaIcon('key-round')}Local</span>`;
    const roleTag = user.role === 'admin'
        ? `<span class="ua-tag ua-tag-admin">${uaIcon('crown')}Admin</span>`
        : `<span class="ua-tag ua-tag-reader">${uaIcon('book-open')}Lecteur</span>`;
    const forced = user.type === 'oidc' && user.role_override ? '<span class="ua-tag ua-tag-muted" data-tooltip="Rôle forcé, le groupe Pocket ID est ignoré">forcé</span>' : '';
    const groups = user.groups.map(g => `<span class="ua-tag ua-tag-muted">${usersEscape(g)}</span>`).join('');
    const seen = user.last_seen ? `<span class="ua-seen" data-tooltip="Dernière connexion">${uaIcon('clock')}${usersEscape(uaDate(user.last_seen))}</span>` : '';
    const access = user.role === 'reader'
        ? `<button type="button" class="ua-access users-access-btn" data-key="${key}" data-tooltip="Bibliothèques et contrôle parental">${uaIcon('sliders-horizontal')}<span>${usersEscape(accessSummary(user))}</span></button>`
        : `<span class="ua-access ua-access-all">${uaIcon('library')}Accès complet</span>`;
    return `<div class="ua-row">
        <div class="ua-main">
            ${uaIdentity(user)}
            <div class="ua-tags">${roleTag}${typeTag}${forced}${groups}</div>
            <div class="ua-side">${access}${seen}</div>
            <div class="ua-actions">
                <button type="button" class="ua-icon-btn users-edit-btn" data-key="${key}" aria-label="Modifier" data-tooltip="Modifier">${uaIcon('pencil')}</button>
                ${user.is_me ? '' : `<button type="button" class="ua-icon-btn ua-icon-danger users-delete-btn" data-key="${key}" aria-label="Supprimer" data-tooltip="Supprimer">${uaIcon('trash-2')}</button>`}
            </div>
        </div>
        <div class="ua-drawer" id="users-edit-${key}" hidden></div>
        <div class="ua-drawer users-access-row" id="users-access-${key}" hidden></div>
    </div>`;
}

async function loadUsersRoles() {
    const body = document.getElementById('usersRolesBody');
    if (!body) return;
    const createPanel = document.getElementById('usersCreatePanel');
    if (createPanel) createPanel.hidden = true;
    try {
        const response = await fetch('/api/auth/users');
        const data = await response.json();
        if (!response.ok || !data.success) throw new Error(data.error || 'Chargement impossible');
        document.getElementById('adminGroupInput').value = data.admin_group || '';
        const groupBlock = document.getElementById('adminGroupBlock');
        if (groupBlock) groupBlock.hidden = !data.oidc_enabled;
        const signup = document.getElementById('allowSignup');
        if (signup) signup.checked = !!data.allow_signup;
        const autoApprove = document.getElementById('autoApproveRequests');
        if (autoApprove) autoApprove.checked = !!data.auto_approve_requests;
        usersByKey = Object.fromEntries(data.users.map(user => [user.key, user]));

        const pending = data.users.filter(user => user.status === 'pending');
        const active = data.users.filter(user => user.status !== 'pending');
        const pendingSection = document.getElementById('usersPendingSection');
        pendingSection.hidden = !pending.length;
        document.getElementById('usersPendingCount').textContent = pending.length || '';
        document.getElementById('usersPendingList').innerHTML = pending.map(pendingRow).join('');
        document.getElementById('usersCount').textContent = active.length || '';
        body.innerHTML = active.length ? active.map(userRow).join('')
            : '<div class="ua-empty">Aucun compte pour le moment. Créez-en un avec « Créer un compte ».</div>';

        const card = document.getElementById('usersRolesCard');
        card.querySelectorAll('.users-access-btn').forEach(button => {
            button.addEventListener('click', () => {
                closeDrawers(document.getElementById(`users-access-${button.dataset.key}`));
                toggleAccessEditor(button.dataset.key);
            });
        });
        card.querySelectorAll('.users-avatar-btn').forEach(button => {
            button.addEventListener('click', () => {
                const user = usersByKey[button.dataset.key];
                openAvatarPicker({
                    title: 'Changer l’avatar', admin: true,
                    user: {...user, name: user.name || user.username || user.email},
                    onChange: data => {
                        if (user.is_me) {
                            document.querySelectorAll('.header-user-avatar, .sidebar-user-avatar').forEach(el => {
                                el.innerHTML = `<img src="${usersEscape(data.avatar_url)}" alt="">`;
                            });
                        }
                        loadUsersRoles();
                    },
                });
            });
        });
        card.querySelectorAll('.users-edit-btn').forEach(button => {
            button.addEventListener('click', () => openEditAccount(button.dataset.key));
        });
        card.querySelectorAll('.users-delete-btn').forEach(button => {
            button.addEventListener('click', () => deleteAccount(button.dataset.key));
        });
        card.querySelectorAll('.ua-approve').forEach(button => {
            const select = card.querySelector(`.ua-approve-role[data-key="${CSS.escape(button.dataset.key)}"]`);
            button.addEventListener('click', () => approveRequest(button.dataset.key, select ? select.value : 'reader'));
        });
        card.querySelectorAll('.ua-reject').forEach(button => {
            button.addEventListener('click', () => rejectRequest(button.dataset.key));
        });
    } catch (error) {
        body.innerHTML = `<div class="ua-empty">${usersEscape(error.message)}</div>`;
    }
}

async function saveAllowSignup() {
    const box = document.getElementById('allowSignup');
    const result = await usersRequest('PUT', {enabled: box.checked}, '/api/auth/signup');
    if (!result.ok) box.checked = !box.checked;
    showUsersMessage(result.ok ? (box.checked ? 'Inscription ouverte : le lien « Créer un compte » est sur la page de connexion'
                                              : 'Inscription fermée') : (result.data.error || 'Erreur'), result.ok);
}

async function saveAutoApproveRequests() {
    const box = document.getElementById('autoApproveRequests');
    const result = await usersRequest('PUT', {auto_approve: box.checked}, '/api/requests/settings');
    if (!result.ok) box.checked = !box.checked;
    showUsersMessage(result.ok ? (box.checked ? 'Les demandes de BD sont ajoutées tout de suite'
                                              : 'Les demandes de BD attendent votre validation (page Validation)') : (result.data.error || 'Erreur'), result.ok);
}

async function saveAdminGroup() {
    const response = await fetch('/api/auth/roles', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({admin_group: document.getElementById('adminGroupInput').value.trim()}),
    });
    const data = await response.json().catch(() => ({}));
    showUsersMessage(response.ok && data.success ? 'Groupe enregistré. Les comptes doivent se reconnecter pour que leur groupe soit relu.' : (data.error || 'Erreur'),
                     response.ok && data.success);
    loadUsersRoles();
}

document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('#usersRolesCard [data-ua-icon]').forEach(el => {
        el.innerHTML = uaIcon(el.dataset.uaIcon);
    });
    loadUsersRoles();
});
