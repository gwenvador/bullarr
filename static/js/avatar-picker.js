// Fenêtre « Choisir un avatar » : galerie d'avatars proposés, envoi d'une photo, retour à
// l'avatar par défaut. Partagée par /profil (son propre compte, /api/auth/profile/avatar)
// et Configuration → Utilisateurs et rôles (un administrateur, /api/auth/users/avatar).
//
// openAvatarPicker({title, user: {key?, name, avatar_url, avatar_kind}, admin, onChange(data)})
let apPresets = null;

function apEscape(value) {
    return String(value ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
}

async function apLoadPresets() {
    if (apPresets) return apPresets;
    const response = await fetch('/api/auth/avatar-presets');
    const data = await response.json();
    apPresets = data.presets || [];
    return apPresets;
}

function apIcon(name) {
    return typeof svgIcon === 'function' ? svgIcon(name) : '';
}

async function openAvatarPicker({title, user, admin = false, onChange}) {
    document.querySelector('.ap-backdrop')?.remove();
    const presets = await apLoadPresets();
    const url = admin ? '/api/auth/users/avatar' : '/api/auth/profile/avatar';
    const backdrop = document.createElement('div');
    backdrop.className = 'ap-backdrop';
    backdrop.innerHTML = `
        <div class="ap-dialog" role="dialog" aria-modal="true" aria-labelledby="ap-title">
            <div class="ap-head">
                <span class="ap-current"><img src="${apEscape(user.avatar_url || '')}" alt=""></span>
                <div class="ap-head-text">
                    <h2 id="ap-title">${apEscape(title || 'Choisir un avatar')}</h2>
                    <p>${apEscape(user.name || '')}</p>
                </div>
                <button type="button" class="ap-close" data-ap-close aria-label="Fermer">${apIcon('x')}</button>
            </div>
            <div class="ap-grid">
                ${presets.map(p => `
                    <button type="button" class="ap-preset${user.avatar_url === p.url ? ' is-current' : ''}" data-ap-preset="${apEscape(p.name)}" aria-label="Avatar ${apEscape(p.name)}" data-tooltip="${apEscape(p.name)}">
                        <img src="${apEscape(p.url)}" alt="" loading="lazy">
                    </button>`).join('')}
            </div>
            <div class="ap-foot">
                <button type="button" class="rq-btn rq-btn-ghost" data-ap-upload>${apIcon('upload')}<span>Envoyer une photo</span></button>
                ${user.avatar_kind && user.avatar_kind !== 'default'
                    ? `<button type="button" class="rq-btn rq-btn-ghost" data-ap-reset>${apIcon('undo')}<span>Avatar par défaut</span></button>` : ''}
                <span class="ap-message" data-ap-message></span>
                <input type="file" accept="image/png,image/jpeg,image/webp,image/gif" hidden data-ap-file>
            </div>
        </div>`;
    document.body.appendChild(backdrop);
    const message = backdrop.querySelector('[data-ap-message]');
    const close = () => { backdrop.remove(); document.removeEventListener('keydown', onKey); };
    const onKey = event => { if (event.key === 'Escape') close(); };
    document.addEventListener('keydown', onKey);

    async function send(options) {
        backdrop.classList.add('is-busy');
        message.textContent = '';
        try {
            const response = await fetch(url, options);
            const data = await response.json();
            if (!response.ok || !data.success) throw new Error(data.error || 'Changement impossible');
            onChange?.(data);
            close();
        } catch (error) {
            message.textContent = error.message;
            backdrop.classList.remove('is-busy');
        }
    }
    const json = payload => ({headers: {'Content-Type': 'application/json'}, body: JSON.stringify(admin ? {...payload, key: user.key} : payload)});

    backdrop.addEventListener('click', event => {
        if (event.target === backdrop || event.target.closest('[data-ap-close]')) { close(); return; }
        const preset = event.target.closest('[data-ap-preset]');
        if (preset) { send({method: 'PUT', ...json({preset: preset.dataset.apPreset})}); return; }
        if (event.target.closest('[data-ap-reset]')) { send({method: 'DELETE', ...json({})}); return; }
        if (event.target.closest('[data-ap-upload]')) backdrop.querySelector('[data-ap-file]').click();
    });
    backdrop.querySelector('[data-ap-file]').addEventListener('change', event => {
        const file = event.target.files[0];
        if (!file) return;
        if (file.size > 8 * 1024 * 1024) { message.textContent = 'Image trop lourde (8 Mo maximum).'; return; }
        const form = new FormData();
        form.append('avatar', file);
        if (admin) form.append('key', user.key);
        send({method: 'POST', body: form});
    });
    backdrop.querySelector('.ap-preset.is-current, .ap-preset')?.focus();
}
