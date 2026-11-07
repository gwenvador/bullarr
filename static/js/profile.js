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
    document.getElementById('pf-avatar-remove').hidden = !pfProfile.avatar_url;
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
        pfRenderAvatar();
        pfMessage('pf-avatar-message', 'Photo mise à jour.');
    } catch (error) {
        pfMessage('pf-avatar-message', error.message, true);
    } finally {
        box.classList.remove('is-busy');
        document.getElementById('pf-avatar-input').value = '';
    }
}

document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('[data-pf-icon]').forEach(el => { el.innerHTML = svgIcon(el.dataset.pfIcon); });
    const input = document.getElementById('pf-avatar-input');
    document.getElementById('pf-avatar-pick').addEventListener('click', () => input.click());
    document.getElementById('pf-avatar').addEventListener('click', () => input.click());
    input.addEventListener('change', () => pfUpload(input.files[0]));
    const drop = document.getElementById('pf-avatar');
    ['dragenter', 'dragover'].forEach(type => drop.addEventListener(type, e => { e.preventDefault(); drop.classList.add('is-drop'); }));
    ['dragleave', 'drop'].forEach(type => drop.addEventListener(type, e => { e.preventDefault(); drop.classList.remove('is-drop'); }));
    drop.addEventListener('drop', e => pfUpload(e.dataTransfer.files[0]));
    document.getElementById('pf-avatar-remove').addEventListener('click', async () => {
        if (!confirm('Retirer votre photo de profil ?')) return;
        const response = await fetch('/api/auth/profile/avatar', {method: 'DELETE'});
        if (response.ok) { pfProfile.avatar_url = null; pfRenderAvatar(); pfMessage('pf-avatar-message', 'Photo retirée.'); }
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
    pfLoad();
});
