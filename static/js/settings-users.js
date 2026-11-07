// Carte « Utilisateurs et rôles » (onglet Connexion des paramètres). Les comptes
// apparaissent ici après leur première connexion. Rôle calculé : membre du groupe
// d'administrateurs Pocket ID → Admin, sinon Lecteur (aucun groupe configuré : tout le
// monde est Admin). Un rôle forcé ici l'emporte sur le groupe.
function usersEscape(value) {
    return String(value ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
}

const ROLE_LABELS = {admin: 'Admin', reader: 'Lecteur'};
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

async function loadUsersRoles() {
    const body = document.getElementById('usersRolesBody');
    if (!body) return;
    try {
        const response = await fetch('/api/auth/users');
        const data = await response.json();
        if (!response.ok || !data.success) throw new Error(data.error || 'Chargement impossible');
        document.getElementById('adminGroupInput').value = data.admin_group || '';
        if (!data.users.length) {
            body.innerHTML = '<tr><td colspan="5" class="users-empty">Aucun compte connecté pour le moment.</td></tr>';
            return;
        }
        body.innerHTML = data.users.map(user => {
            const select = user.locked
                ? '<span class="users-locked" data-tooltip="Le compte de connexion par mot de passe est toujours administrateur">Admin</span>'
                : `<select class="users-role-select" data-key="${usersEscape(user.key)}">
                        <option value="">Automatique (${ROLE_LABELS[user.computed_role]})</option>
                        <option value="admin" ${user.role_override === 'admin' ? 'selected' : ''}>Admin</option>
                        <option value="reader" ${user.role_override === 'reader' ? 'selected' : ''} ${user.is_me ? 'disabled' : ''}>Lecteur</option>
                   </select>`;
            return `<tr>
                <td><strong>${usersEscape(user.name || user.email || user.key)}</strong>${user.is_me ? ' <span class="users-me">vous</span>' : ''}
                    ${user.email && user.email !== user.name ? `<div class="users-sub">${usersEscape(user.email)}</div>` : ''}</td>
                <td>${user.groups.length ? user.groups.map(g => `<span class="users-group">${usersEscape(g)}</span>`).join(' ') : '<span class="users-sub">—</span>'}</td>
                <td><span class="users-role users-role-${user.role}">${ROLE_LABELS[user.role]}</span></td>
                <td>${select}</td>
                <td>${user.role === 'reader'
                    ? `<button type="button" class="btn users-access-btn" data-key="${usersEscape(user.key)}">${usersEscape(accessSummary(user))}</button>`
                    : '<span class="users-sub">Tout</span>'}</td>
            </tr>
            <tr class="users-access-row" id="users-access-${usersEscape(user.key)}" hidden><td colspan="5"></td></tr>`;
        }).join('');
        usersByKey = Object.fromEntries(data.users.map(user => [user.key, user]));
        body.querySelectorAll('.users-access-btn').forEach(button => {
            button.addEventListener('click', () => toggleAccessEditor(button.dataset.key));
        });
        body.querySelectorAll('.users-role-select').forEach(select => {
            select.addEventListener('change', () => setUserRole(select.dataset.key, select.value || null));
        });
    } catch (error) {
        body.innerHTML = `<tr><td colspan="5" class="users-empty">${usersEscape(error.message)}</td></tr>`;
    }
}

async function setUserRole(key, role) {
    const response = await fetch('/api/auth/users/role', {
        method: 'PUT', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({key, role}),
    });
    const data = await response.json().catch(() => ({}));
    showUsersMessage(response.ok && data.success ? 'Rôle enregistré' : (data.error || 'Erreur'), response.ok && data.success);
    loadUsersRoles();
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
