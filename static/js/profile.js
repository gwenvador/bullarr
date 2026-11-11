// Page « Mon profil » (/profil) : photo de profil pour tous les comptes ; nom, e-mail et
// mot de passe pour les comptes locaux (ceux d'un compte SSO se changent chez le
// fournisseur). API : /api/auth/profile, /api/auth/profile/avatar (blueprints/auth/routes.py).
function pfEscape(value) {
    return String(value ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
}

function pfHue(text) {
    let hash = 0;
    for (const ch of String(text || '?')) hash = (hash * 31 + ch.charCodeAt(0)) % 360;
    return hash;
}

function pfInitials(text) {
    const words = String(text || '?').split(/[\s._@-]+/).filter(Boolean);
    return ((words[0] || '?')[0] + (words[1] ? words[1][0] : '')).toUpperCase();
}

function pfDate(value) {
    if (!value) return '—';
    const date = new Date(String(value).replace(' ', 'T') + 'Z');
    return Number.isNaN(date.getTime()) ? '—' : date.toLocaleDateString('fr-FR', {day: 'numeric', month: 'long', year: 'numeric'});
}

let pfProfile = null;

function pfMessage(id, text, error = false) {
    const box = document.getElementById(id);
    box.textContent = text || '';
    box.className = `pf-message${text ? (error ? ' is-error' : ' is-ok') : ''}`;
}

function pfRenderAvatar() {
    const box = document.getElementById('pf-avatar');
    const name = pfProfile.name || pfProfile.username || '?';
    box.style.setProperty('--pf-hue', pfHue(name));
    box.innerHTML = pfProfile.avatar_url
        ? `<img src="${pfEscape(pfProfile.avatar_url)}" alt="Photo de profil">`
        : `<span>${pfEscape(pfInitials(name))}</span>`;
    document.getElementById('pf-avatar-remove').hidden = !pfProfile.avatar_kind || pfProfile.avatar_kind === 'default';
    document.querySelectorAll('.header-user-avatar, .sidebar-user-avatar').forEach(el => {
        el.innerHTML = pfProfile.avatar_url ? `<img src="${pfEscape(pfProfile.avatar_url)}" alt="">` : pfEscape(pfInitials(name));
    });
}

function pfRender() {
    const p = pfProfile;
    document.getElementById('pf-name').textContent = p.name || p.username || '';
    document.getElementById('pf-sub').innerHTML = [
        `<span class="pf-badge ${p.role === 'admin' ? 'is-admin' : ''}">${svgIcon(p.role === 'admin' ? 'crown' : 'book-open')}${p.role === 'admin' ? 'Administrateur' : 'Lecteur'}</span>`,
        `<span class="pf-badge">${svgIcon(p.type === 'local' ? 'key-round' : 'shield-check')}${p.type === 'local' ? 'Compte local' : 'Compte SSO'}</span>`,
    ].join('');
    document.getElementById('pf-facts').innerHTML = [
        p.username ? `<div>${svgIcon('at-sign')}<span>Identifiant</span><strong>${pfEscape(p.username)}</strong></div>` : '',
        p.email ? `<div>${svgIcon('mail')}<span>E-mail</span><strong>${pfEscape(p.email)}</strong></div>` : '',
        `<div>${svgIcon('calendar')}<span>Membre depuis</span><strong>${pfDate(p.first_seen)}</strong></div>`,
    ].join('');
    pfRenderAvatar();
    const local = p.can_edit_identity;
    document.getElementById('pf-identity-card').hidden = !local;
    document.getElementById('pf-password-card').hidden = !local;
    document.getElementById('pf-sso-card').hidden = local;
    if (local) {
        document.getElementById('pf-input-name').value = p.name || '';
        document.getElementById('pf-input-email').value = p.email || '';
    }
}

async function pfLoad() {
    try {
        const response = await fetch('/api/auth/profile');
        const data = await response.json();
        if (!response.ok || !data.success) throw new Error(data.error || 'Profil indisponible');
        pfProfile = data.profile;
        pfRender();
        document.getElementById('pf-root').hidden = false;
        document.getElementById('pf-loading').remove();
    } catch (error) {
        document.getElementById('pf-loading').innerHTML = `<p>${pfEscape(error.message)}</p>`;
    }
}

async function pfSave(payload, messageId, button) {
    button.disabled = true;
    try {
        const response = await fetch('/api/auth/profile', {
            method: 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload),
        });
        const data = await response.json();
        if (!response.ok || !data.success) throw new Error(data.error || 'Enregistrement impossible');
        pfProfile = data.profile;
        pfRender();
        pfMessage(messageId, 'Enregistré.');
        document.querySelectorAll('.header-user-name-text, .sidebar-user-name').forEach(el => { el.textContent = pfProfile.name || pfProfile.username; });
        return true;
    } catch (error) {
        pfMessage(messageId, error.message, true);
        return false;
    } finally {
        button.disabled = false;
    }
}

async function pfUpload(file) {
    if (!file) return;
    if (file.size > 8 * 1024 * 1024) { pfMessage('pf-avatar-message', 'Image trop lourde (8 Mo maximum).', true); return; }
    const form = new FormData();
    form.append('avatar', file);
    const box = document.getElementById('pf-avatar');
    box.classList.add('is-busy');
    try {
        const response = await fetch('/api/auth/profile/avatar', {method: 'POST', body: form});
        const data = await response.json();
        if (!response.ok || !data.success) throw new Error(data.error || 'Envoi impossible');
        pfProfile.avatar_url = data.avatar_url;
        pfProfile.avatar_kind = data.avatar_kind;
        pfRenderAvatar();
        pfMessage('pf-avatar-message', 'Photo mise à jour.');
    } catch (error) {
        pfMessage('pf-avatar-message', error.message, true);
    } finally {
        box.classList.remove('is-busy');
        document.getElementById('pf-avatar-input').value = '';
    }
}

// Compte Komga personnel (/api/komga/me, blueprints/komga/user_link.py) : sans compte relié,
// l'avancement de lecture est enregistré par Bullarr pour ce compte seulement.
let pfKomga = null;
let pfKomgaUseKey = false;

function pfRenderKomga() {
    const k = pfKomga;
    const card = document.getElementById('pf-komga-card');
    const form = document.getElementById('pf-komga-form');
    const body = document.getElementById('pf-komga-body');
    card.hidden = !k || (!k.available && k.mode !== 'local');
    if (card.hidden) return;
    if (!k.available) {
        body.innerHTML = '<p class="pf-hint">Komga n’est pas configuré sur ce serveur : votre avancement de lecture est enregistré par Bullarr, pour votre compte uniquement.</p>';
        form.hidden = true;
        return;
    }
    if (k.linked) {
        const stats = k.stats ? `<p class="pf-hint">${k.stats.read} lu(s) · ${k.stats.reading} en cours</p>` : '';
        const error = k.last_error ? `<p class="pf-message is-error">${pfEscape(k.last_error)}</p>` : '';
        body.innerHTML = `<p>Relié au compte <strong>${pfEscape(k.komga_user || 'Komga')}</strong>. Ce que vous lisez dans Bullarr y est enregistré, et ce que vous lisez dans Komga (ou Mihon, Tachiyomi…) apparaît dans Bullarr.</p>${stats}${error}
            <div class="pf-form-foot"><div id="pf-komga-message" class="pf-message"></div>
            <button type="button" id="pf-komga-sync" class="rq-btn rq-btn-ghost">${svgIcon('refresh-cw')}<span>Synchroniser</span></button>
            <button type="button" id="pf-komga-unlink" class="rq-btn rq-btn-ghost pf-danger">${svgIcon('log-out')}<span>Délier</span></button></div>`;
        form.hidden = true;
        document.getElementById('pf-komga-sync').addEventListener('click', async event => {
            const button = event.currentTarget;
            button.disabled = true;
            const response = await fetch('/api/komga/read-progress/sync', {method: 'POST'});
            const data = await response.json().catch(() => ({}));
            if (!response.ok || !data.success) { pfMessage('pf-komga-message', data.error || 'Synchronisation impossible', true); button.disabled = false; return; }
            await pfLoadKomga();
            pfMessage('pf-komga-message', 'Lecture synchronisée.');
        });
        document.getElementById('pf-komga-unlink').addEventListener('click', async () => {
            if (!confirm('Délier votre compte Komga ? Votre compte Komga n’est pas modifié.')) return;
            const response = await fetch('/api/komga/me', {method: 'DELETE'});
            const data = await response.json().catch(() => ({}));
            if (response.ok && data.success) { pfKomga = data; pfRenderKomga(); }
        });
        return;
    }
    body.innerHTML = `<p class="pf-hint">Reliez votre compte Komga (${pfEscape(k.komga_url || '')}) pour y retrouver et y enregistrer votre avancement de lecture. Sans compte relié, il est enregistré par Bullarr, pour votre compte uniquement.</p>`;
    form.hidden = false;
}

let pfApiKey = '';

function pfRenderApiKey() {
    document.getElementById('pf-apikey-card').hidden = false;
    document.getElementById('pf-apikey').value = pfApiKey;
    document.getElementById('pf-apikey-copy').hidden = !pfApiKey;
    document.getElementById('pf-apikey-delete').hidden = !pfApiKey;
    document.querySelector('#pf-apikey-create span:last-child').textContent = pfApiKey ? 'Régénérer' : 'Générer une clé';
}

async function pfApiKeyCall(method, okMessage) {
    try {
        const response = await fetch('/api/auth/profile/api-key', {method});
        const data = await response.json().catch(() => ({}));
        if (!response.ok || !data.success) {
            if (method === 'GET') return; // carte masquée (pas de compte : authentification désactivée)
            throw new Error(data.error || 'Opération impossible');
        }
        pfApiKey = data.api_key || '';
        pfRenderApiKey();
        if (okMessage) pfMessage('pf-apikey-message', okMessage);
    } catch (error) {
        pfMessage('pf-apikey-message', error.message, true);
    }
}

async function pfLoadKomga() {
    try {
        const response = await fetch('/api/komga/me');
        const data = await response.json();
        if (response.ok && data.success) { pfKomga = data; pfRenderKomga(); }
    } catch (error) { /* carte masquée */ }
}

document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('[data-pf-icon]').forEach(el => { el.innerHTML = svgIcon(el.dataset.pfIcon); });
    const input = document.getElementById('pf-avatar-input');
    const pick = () => openAvatarPicker({
        title: 'Choisir mon avatar', user: pfProfile,
        onChange: data => {
            pfProfile.avatar_url = data.avatar_url;
            pfProfile.avatar_kind = data.avatar_kind;
            pfRenderAvatar();
            pfMessage('pf-avatar-message', 'Avatar mis à jour.');
        },
    });
    document.getElementById('pf-avatar-pick').addEventListener('click', pick);
    document.getElementById('pf-avatar').addEventListener('click', pick);
    input.addEventListener('change', () => pfUpload(input.files[0]));
    const drop = document.getElementById('pf-avatar');
    ['dragenter', 'dragover'].forEach(type => drop.addEventListener(type, e => { e.preventDefault(); drop.classList.add('is-drop'); }));
    ['dragleave', 'drop'].forEach(type => drop.addEventListener(type, e => { e.preventDefault(); drop.classList.remove('is-drop'); }));
    drop.addEventListener('drop', e => pfUpload(e.dataTransfer.files[0]));
    document.getElementById('pf-avatar-remove').addEventListener('click', async () => {
        const response = await fetch('/api/auth/profile/avatar', {method: 'DELETE'});
        const data = await response.json().catch(() => ({}));
        if (response.ok && data.success) {
            pfProfile.avatar_url = data.avatar_url;
            pfProfile.avatar_kind = data.avatar_kind;
            pfRenderAvatar();
            pfMessage('pf-avatar-message', 'Avatar par défaut rétabli.');
        }
    });
    document.getElementById('pf-identity-form').addEventListener('submit', event => {
        event.preventDefault();
        pfSave({name: document.getElementById('pf-input-name').value, email: document.getElementById('pf-input-email').value},
               'pf-identity-message', event.submitter || event.target.querySelector('button'));
    });
    document.getElementById('pf-password-form').addEventListener('submit', async event => {
        event.preventDefault();
        const form = event.target;
        const ok = await pfSave({
            current_password: form.current_password.value,
            new_password: form.new_password.value,
            new_password_confirm: form.new_password_confirm.value,
        }, 'pf-password-message', form.querySelector('button'));
        if (ok) { form.reset(); pfMessage('pf-password-message', 'Mot de passe changé. Vos autres sessions sont déconnectées.'); }
    });
    document.querySelectorAll('[data-pf-toggle]').forEach(button => {
        button.addEventListener('click', () => {
            const field = document.getElementById(button.dataset.pfToggle);
            field.type = field.type === 'password' ? 'text' : 'password';
            button.innerHTML = svgIcon(field.type === 'password' ? 'eye' : 'eye-off');
        });
    });
    document.getElementById('pf-komga-mode').addEventListener('click', event => {
        pfKomgaUseKey = !pfKomgaUseKey;
        document.getElementById('pf-komga-login').hidden = pfKomgaUseKey;
        document.getElementById('pf-komga-keyfield').hidden = !pfKomgaUseKey;
        event.currentTarget.textContent = pfKomgaUseKey ? 'Utiliser mon e-mail et mon mot de passe' : 'Utiliser une clé API';
    });
    document.getElementById('pf-komga-form').addEventListener('submit', async event => {
        event.preventDefault();
        const button = event.target.querySelector('button[type=submit]');
        const payload = pfKomgaUseKey
            ? {api_key: document.getElementById('pf-komga-key').value}
            : {username: document.getElementById('pf-komga-user').value, password: document.getElementById('pf-komga-pass').value};
        button.disabled = true;
        pfMessage('pf-komga-message', 'Connexion à Komga…');
        try {
            const response = await fetch('/api/komga/me', {method: 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)});
            const data = await response.json().catch(() => ({}));
            if (!response.ok || !data.success) throw new Error(data.error || 'Connexion impossible');
            event.target.reset();
            pfKomga = data;
            pfRenderKomga();
        } catch (error) {
            pfMessage('pf-komga-message', error.message, true);
        } finally {
            button.disabled = false;
        }
    });
    document.getElementById('pf-apikey-create').addEventListener('click', async () => {
        if (pfApiKey && !confirm('Remplacer la clé ? Les applications qui utilisent l’ancienne devront être reconfigurées.')) return;
        await pfApiKeyCall('POST', 'Nouvelle clé créée.');
    });
    document.getElementById('pf-apikey-delete').addEventListener('click', async () => {
        if (!confirm('Supprimer la clé ? Les applications qui l’utilisent perdront l’accès.')) return;
        await pfApiKeyCall('DELETE', 'Clé supprimée.');
    });
    document.getElementById('pf-apikey-copy').addEventListener('click', async () => {
        const field = document.getElementById('pf-apikey');
        try {
            await navigator.clipboard.writeText(field.value);
        } catch (error) {
            const type = field.type;
            field.type = 'text';
            field.select();
            document.execCommand('copy');
            field.type = type;
        }
        pfMessage('pf-apikey-message', 'Clé copiée.');
    });
    pfLoad();
    pfLoadKomga();
    pfApiKeyCall('GET');
});
