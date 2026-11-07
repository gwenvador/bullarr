// Carte « Utilisateurs et rôles » (onglet Connexion des paramètres).
// Comptes locaux : créés ici (identifiant + mot de passe), modifiables et supprimables.
// Comptes OIDC : apparaissent après leur première connexion ; rôle calculé depuis le groupe
// d'administrateurs Pocket ID (aucun groupe : tout le monde est Admin), forçable ici.
function usersEscape(value) {
    return String(value ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
}

const ROLE_LABELS = {admin: 'Admin', reader: 'Lecteur'};
const USERS_COLS = 6;
let usersByKey = {};
let accessCatalog = null;

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
    row.querySelector('td').innerHTML = `
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

async function usersRequest(method, payload) {
    const response = await fetch('/api/auth/users', {
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

// Formulaire de création (user absent) ou d'édition, affiché dans une ligne sous le compte.
function accountForm(user) {
    const local = !user || user.type === 'local';
    return `<form class="users-account-form" autocomplete="off">
        ${!user ? `<label>Identifiant<input type="text" name="username" required minlength="2" maxlength="64"
                    pattern="[A-Za-z0-9._@\\-]+" placeholder="ex. camille" autocomplete="off"></label>` : ''}
        ${local ? `<label>Nom affiché<input type="text" name="name" maxlength="120" value="${usersEscape(user ? user.name : '')}"></label>
            <label>E-mail<input type="email" name="email" maxlength="200" value="${usersEscape(user ? user.email : '')}"></label>
            <label>${user ? 'Nouveau mot de passe <small>(vide : inchangé)</small>' : 'Mot de passe <small>(8 caractères min.)</small>'}
                <input type="password" name="password" ${user ? '' : 'required'} minlength="8" autocomplete="new-password"></label>` : ''}
        <label>Rôle<select name="role">${roleOptions(user)}</select></label>
        <div class="users-form-actions">
            <button type="button" class="btn users-form-cancel">Annuler</button>
            <button type="submit" class="btn btn-primary">${user ? 'Enregistrer' : 'Créer le compte'}</button>
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

function openEditAccount(key) {
    const row = document.getElementById(`users-edit-${key}`);
    const user = usersByKey[key];
    if (!row || !user) return;
    if (!row.hidden) { row.hidden = true; return; }
    const cell = row.querySelector('td');
    cell.innerHTML = accountForm(user);
    row.hidden = false;
    bindAccountForm(cell, user, () => { row.hidden = true; });
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
        if (!data.users.length) {
            body.innerHTML = `<tr><td colspan="${USERS_COLS}" class="users-empty">Aucun compte pour le moment. Créez-en un avec « Créer un compte ».</td></tr>`;
            return;
        }
        body.innerHTML = data.users.map(user => {
            const key = usersEscape(user.key);
            const title = user.name || user.username || user.email || user.key;
            const sub = [user.type === 'local' && user.username && user.username !== title ? user.username : '',
                         user.email && user.email !== title ? user.email : ''].filter(Boolean).join(' · ');
            return `<tr>
                <td><strong>${usersEscape(title)}</strong>${user.is_me ? ' <span class="users-me">vous</span>' : ''}
                    ${sub ? `<div class="users-sub">${usersEscape(sub)}</div>` : ''}</td>
                <td><span class="users-type users-type-${user.type}">${user.type === 'oidc' ? 'OIDC' : 'Local'}</span></td>
                <td>${user.groups.length ? user.groups.map(g => `<span class="users-group">${usersEscape(g)}</span>`).join(' ') : '<span class="users-sub">—</span>'}</td>
                <td><span class="users-role users-role-${user.role}">${ROLE_LABELS[user.role]}</span>${user.role_override && user.type === 'oidc' ? ' <span class="users-sub">(forcé)</span>' : ''}</td>
                <td>${user.role === 'reader'
                    ? `<button type="button" class="btn users-access-btn" data-key="${key}">${usersEscape(accessSummary(user))}</button>`
                    : '<span class="users-sub">Tout</span>'}</td>
                <td class="users-actions">
                    <button type="button" class="btn users-edit-btn" data-key="${key}">Modifier</button>
                    ${user.is_me ? '' : `<button type="button" class="btn btn-danger users-delete-btn" data-key="${key}">Supprimer</button>`}
                </td>
            </tr>
            <tr class="users-access-row" id="users-edit-${key}" hidden><td colspan="${USERS_COLS}"></td></tr>
            <tr class="users-access-row" id="users-access-${key}" hidden><td colspan="${USERS_COLS}"></td></tr>`;
        }).join('');
        usersByKey = Object.fromEntries(data.users.map(user => [user.key, user]));
        body.querySelectorAll('.users-access-btn').forEach(button => {
            button.addEventListener('click', () => toggleAccessEditor(button.dataset.key));
        });
        body.querySelectorAll('.users-edit-btn').forEach(button => {
            button.addEventListener('click', () => openEditAccount(button.dataset.key));
        });
        body.querySelectorAll('.users-delete-btn').forEach(button => {
            button.addEventListener('click', () => deleteAccount(button.dataset.key));
        });
    } catch (error) {
        body.innerHTML = `<tr><td colspan="${USERS_COLS}" class="users-empty">${usersEscape(error.message)}</td></tr>`;
    }
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

document.addEventListener('DOMContentLoaded', loadUsersRoles);
