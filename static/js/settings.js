
let passwordVisible = false;
let ebdzPasswordVisible = false;

// ===== AMULE =====
async function loadSettings() {
    try {
        const response = await fetch('/api/emule/config');
        const config = await response.json();
        
        document.getElementById('emuleEnabled').checked = config.enabled;
        document.getElementById('emuleType').value = config.type || 'amule';
        document.getElementById('emuleHost').value = config.host;
        document.getElementById('emuleEcPort').value = config.ec_port;
        
        // Pré-remplit avec '****' (masque) plutôt que de laisser le champ vide quand un
        // mot de passe est déjà enregistré: sinon "enregistrer" échoue en exigeant de le
        // retaper alors qu'il est déjà là ("il faut que je remette les jetons... pas
        // besoin c'est deja la") - le backend traite déjà '****' littéral comme "ne pas
        // changer" (voir save_emule_config), donc le laisser tel quel au ré-enregistrement
        // est sans danger.
        if (config.password) {
            document.getElementById('emulePassword').value = config.password;
        }
    } catch (error) {
        showMessage('settingsMessage', '❌ Erreur lors du chargement de la configuration', 'error');
    }
}

async function saveSettings() {
    const config = {
        enabled: document.getElementById('emuleEnabled').checked,
        type: document.getElementById('emuleType').value,
        host: document.getElementById('emuleHost').value,
        ec_port: parseInt(document.getElementById('emuleEcPort').value),
        password: document.getElementById('emulePassword').value
    };

    if (config.enabled && !config.password) {
        showMessage('settingsMessage', '⚠️ Veuillez entrer un mot de passe EC', 'warning');
        return;
    }

    try {
        const response = await fetch('/api/emule/config', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(config)
        });
        const data = await response.json();
        
        if (data.success) {
            showMessage('settingsMessage', '✅ Configuration enregistrée avec succès !', 'success');
        } else {
            showMessage('settingsMessage', '❌ Erreur: ' + data.error, 'error');
        }
    } catch (error) {
        showMessage('settingsMessage', '❌ Erreur de connexion: ' + error.message, 'error');
    }
}

async function testConnection() {
    showMessage('settingsMessage', '⏳ Test de connexion en cours...', 'info');
    try {
        const response = await fetch('/api/emule/test');
        const data = await response.json();
        if (data.success) {
            showMessage('settingsMessage', '✅ Connexion réussie à aMule/eMule !', 'success');
        } else {
            showMessage('settingsMessage', '❌ Échec de la connexion: ' + data.error, 'error');
        }
    } catch (error) {
        showMessage('settingsMessage', '❌ Erreur: ' + error.message, 'error');
    }
}

function resetSettings() {
    if (!confirm('Voulez-vous réinitialiser la configuration aMule/eMule ?')) return;
    document.getElementById('emuleEnabled').checked = false;
    document.getElementById('emuleType').value = 'amule';
    document.getElementById('emuleHost').value = '127.0.0.1';
    document.getElementById('emuleEcPort').value = '4712';
    document.getElementById('emulePassword').value = '';
    showMessage('settingsMessage', '🔄 Configuration réinitialisée', 'info');
}

function togglePassword() {
    const passwordInput = document.getElementById('emulePassword');
    const toggleButton = passwordInput.closest('.password-input-group').querySelector('.btn-toggle-password');
    passwordVisible = !passwordVisible;
    passwordInput.type = passwordVisible ? 'text' : 'password';
    toggleButton.innerHTML = passwordVisible ? `${svgIcon('eye-off')} Masquer` : `${svgIcon('eye')} Afficher`;
}

// ===== EBDZ.NET =====
async function loadEbdzConfig() {
    try {
        const response = await fetch('/api/ebdz/config');
        const config = await response.json();

        document.getElementById('ebdzUsername').value = config.username || '';

        // Le serveur retourne '****' si un mot de passe existe - l'affiche tel quel
        // (même correctif que loadSettings/aMule) plutôt que de laisser le champ vide,
        // qui donnait l'impression qu'aucun mot de passe n'était enregistré
        document.getElementById('ebdzPassword').value = config.password || '';

        // Charger les forums
        renderForumsList(config.forums || []);
    } catch (error) {
        showMessage('ebdzMessage', '❌ Erreur lors du chargement de la config ebdz', 'error');
    }
}

function renderForumsList(forums) {
    const list = document.getElementById('forumsList');
    const emptyState = document.getElementById('forumsEmptyState');

    list.innerHTML = '';

    if (forums.length === 0) {
        emptyState.style.display = 'block';
        return;
    }
    emptyState.style.display = 'none';

    forums.forEach((forum, index) => {
        list.appendChild(createForumRow(forum, index));
    });
    updateSelectAllState();
}

function createForumRow(forum = {}, index = 0) {
    const row = document.createElement('div');
    row.className = 'forum-row';
    row.dataset.index = index;
    row.innerHTML = `
        <div class="forum-row-header">
            <div style="display:flex; align-items:center; gap:10px;">
                <input type="checkbox" class="forum-select" checked onchange="updateSelectAllState()">
                <span class="forum-row-label">Forum #${index + 1}</span>
            </div>
            <button class="btn-remove-forum" onclick="removeForumRow(this)">${svgIcon('x')}</button>
        </div>
        <div class="forum-row-fields">
            <div class="form-group" style="flex:0 0 120px;">
                <label>Code du forum (fid)</label>
                <input type="number" class="forum-fid" value="${forum.fid || ''}" placeholder="ex: 29" min="1">
            </div>
            <div class="form-group" style="flex:1;">
                <label>Nom de la catégorie</label>
                <input type="text" class="forum-category" value="${forum.category || ''}" placeholder="ex: BD">
            </div>
        </div>
    `;
    return row;
}

function addForumRow() {
    const list = document.getElementById('forumsList');
    const emptyState = document.getElementById('forumsEmptyState');
    const currentCount = list.querySelectorAll('.forum-row').length;

    list.appendChild(createForumRow({}, currentCount));
    emptyState.style.display = 'none';
    updateSelectAllState();
}

function removeForumRow(btn) {
    const row = btn.closest('.forum-row');
    row.remove();

    // Re-numéroter
    const list = document.getElementById('forumsList');
    list.querySelectorAll('.forum-row').forEach((r, i) => {
        r.dataset.index = i;
        r.querySelector('.forum-row-label').textContent = `Forum #${i + 1}`;
    });

    if (list.querySelectorAll('.forum-row').length === 0) {
        document.getElementById('forumsEmptyState').style.display = 'block';
    }
    updateSelectAllState();
}

function collectForums() {
    const forums = [];
    document.querySelectorAll('.forum-row').forEach(row => {
        const fid = row.querySelector('.forum-fid').value.trim();
        const category = row.querySelector('.forum-category').value.trim();

        if (fid) {
            forums.push({
                fid: parseInt(fid),
                category: category || `Forum ${fid}`,
            });
        }
    });
    return forums;
}

async function saveEbdzConfig() {
    const username = document.getElementById('ebdzUsername').value.trim();
    const password = document.getElementById('ebdzPassword').value;
    const forums = collectForums();

    if (!username) {
        showMessage('ebdzMessage', '⚠️ Veuillez entrer un nom d\'utilisateur', 'warning');
        return;
    }

    // Validation des forums
    for (const f of forums) {
        if (!f.category || f.category.trim() === '') {
            showMessage('ebdzMessage', '⚠️ Chaque forum doit avoir un nom de catégorie', 'warning');
            return;
        }
    }

    try {
        const response = await fetch('/api/ebdz/config', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ username, password, forums })
        });
        const data = await response.json();

        if (data.success) {
            showMessage('ebdzMessage', '✅ Configuration ebdz.net enregistrée !', 'success');
            showMessage('ebdzMessage2', '✅ Configuration enregistrée !', 'success');
            loadEbdzConfig(); // Recharger depuis le serveur pour sync
        } else {
            showMessage('ebdzMessage', '❌ Erreur: ' + data.error, 'error');
        }
    } catch (error) {
        showMessage('ebdzMessage', '❌ Erreur: ' + error.message, 'error');
    }
}

// ===== SELECTION DES FORUMS =====
function toggleSelectAll(checkbox) {
    document.querySelectorAll('.forum-select').forEach(cb => {
        cb.checked = checkbox.checked;
    });
}

function updateSelectAllState() {
    const all = document.querySelectorAll('.forum-select');
    const selectAll = document.getElementById('selectAllCheckbox');
    const label = document.getElementById('selectAllLabel');

    // Cacher "Tout sélectionner" si moins de 2 forums
    label.style.display = all.length >= 2 ? 'flex' : 'none';

    // Mettre à jour la classe visuelle sur chaque ligne
    all.forEach(cb => {
        cb.closest('.forum-row').classList.toggle('forum-selected', cb.checked);
    });

    if (all.length === 0) {
        selectAll.checked = false;
        selectAll.indeterminate = false;
        return;
    }

    const checkedCount = [...all].filter(cb => cb.checked).length;
    if (checkedCount === all.length) {
        selectAll.checked = true;
        selectAll.indeterminate = false;
    } else if (checkedCount === 0) {
        selectAll.checked = false;
        selectAll.indeterminate = false;
    } else {
        selectAll.indeterminate = true;
    }
}

async function runScraper() {
    // Collecter uniquement les fid des forums cochés
    const selectedFids = [];
    document.querySelectorAll('.forum-row').forEach(row => {
        if (row.querySelector('.forum-select').checked) {
            const fid = row.querySelector('.forum-fid').value.trim();
            if (fid) selectedFids.push(parseInt(fid));
        }
    });

    if (selectedFids.length === 0) {
        showMessage('ebdzMessage2', '⚠️ Aucun forum sélectionné. Cochez au moins un forum.', 'warning');
        return;
    }

    const btn = document.getElementById('btnRunScraper');
    btn.disabled = true;
    btn.textContent = '⏳ Scraping...';
    showMessage('ebdzMessage2', `⏳ Scraping en cours pour ${selectedFids.length} ${pluralize(selectedFids.length, 'forum')}… cela peut prendre du temps.`, 'info');

    try {
        const response = await fetch('/api/ebdz/scrape', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ fids: selectedFids })
        });
        const data = await response.json();

        if (data.success) {
            showMessage('ebdzMessage2', `✅ Scraping terminé ! ${data.total_links} liens récupérés sur ${data.forums_scraped} ${pluralize(data.forums_scraped, 'forum')}.`, 'success');
        } else {
            showMessage('ebdzMessage2', '❌ Erreur: ' + data.error, 'error');
        }
    } catch (error) {
        showMessage('ebdzMessage2', '❌ Erreur: ' + error.message, 'error');
    } finally {
        btn.disabled = false;
        // innerHTML (pas textContent): le bouton contient une icône SVG inline (voir
        // svgIcon), que textContent effacerait définitivement à chaque scraping
        btn.innerHTML = `${svgIcon('rocket')} Lancer le scraper`;
    }
}

function toggleEbdzPassword() {
    const input = document.getElementById('ebdzPassword');
    const btn = input.closest('.password-input-group').querySelector('.btn-toggle-password');
    ebdzPasswordVisible = !ebdzPasswordVisible;
    input.type = ebdzPasswordVisible ? 'text' : 'password';
    btn.innerHTML = ebdzPasswordVisible ? `${svgIcon('eye-off')} Masquer` : `${svgIcon('eye')} Afficher`;
}

// ===== PROWLARR =====
let prowlarrPasswordVisible = false;

async function loadProwlarrSettings() {
    try {
        const response = await fetch('/api/prowlarr/config');
        const config = await response.json();
        
        document.getElementById('prowlarrEnabled').checked = config.enabled;
        document.getElementById('prowlarrUrl').value = config.url || '';
        document.getElementById('prowlarrPort').value = config.port || 9696;
        
        // Voir le commentaire équivalent dans loadSettings (aMule) - même correctif
        if (config.api_key) {
            document.getElementById('prowlarrApiKey').value = config.api_key;
        }
    } catch (error) {
        showMessage('prowlarrMessage', '❌ Erreur lors du chargement de la configuration Prowlarr', 'error');
    }
}

async function saveProwlarrSettings() {
    const config = {
        enabled: document.getElementById('prowlarrEnabled').checked,
        url: document.getElementById('prowlarrUrl').value.trim(),
        port: parseInt(document.getElementById('prowlarrPort').value),
        api_key: document.getElementById('prowlarrApiKey').value
    };

    if (config.enabled && !config.url) {
        showMessage('prowlarrMessage', '⚠️ Veuillez entrer l\'URL du serveur Prowlarr', 'warning');
        return;
    }

    if (config.enabled && !config.api_key) {
        showMessage('prowlarrMessage', '⚠️ Veuillez entrer la clé API Prowlarr', 'warning');
        return;
    }

    try {
        const response = await fetch('/api/prowlarr/config', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(config)
        });
        const data = await response.json();
        
        if (data.success) {
            showMessage('prowlarrMessage', '✅ Configuration Prowlarr enregistrée avec succès !', 'success');
        } else {
            showMessage('prowlarrMessage', '❌ Erreur: ' + data.error, 'error');
        }
    } catch (error) {
        showMessage('prowlarrMessage', '❌ Erreur de connexion: ' + error.message, 'error');
    }
}

async function testProwlarrConnection() {
    showMessage('prowlarrMessage', '⏳ Test de connexion en cours...', 'info');
    try {
        const response = await fetch('/api/prowlarr/test');
        const data = await response.json();
        if (data.success) {
            showMessage('prowlarrMessage', '✅ Connexion réussie à Prowlarr !', 'success');
        } else {
            showMessage('prowlarrMessage', '❌ Échec de la connexion: ' + data.error, 'error');
        }
    } catch (error) {
        showMessage('prowlarrMessage', '❌ Erreur: ' + error.message, 'error');
    }
}

function resetProwlarrSettings() {
    if (!confirm('Voulez-vous réinitialiser la configuration Prowlarr ?')) return;
    document.getElementById('prowlarrEnabled').checked = false;
    document.getElementById('prowlarrUrl').value = '';
    document.getElementById('prowlarrPort').value = '9696';
    document.getElementById('prowlarrApiKey').value = '';
    showMessage('prowlarrMessage', '🔄 Configuration réinitialisée', 'info');
}

function toggleProwlarrPassword() {
    const input = document.getElementById('prowlarrApiKey');
    const btn = input.closest('.password-input-group').querySelector('.btn-toggle-password');
    prowlarrPasswordVisible = !prowlarrPasswordVisible;
    input.type = prowlarrPasswordVisible ? 'text' : 'password';
    btn.innerHTML = prowlarrPasswordVisible ? `${svgIcon('eye-off')} Masquer` : `${svgIcon('eye')} Afficher`;
}

// ===== KOMGA =====
let komgaPasswordVisible = false;

async function loadKomgaSettings() {
    try {
        const response = await fetch('/api/komga/config');
        const config = await response.json();

        document.getElementById('komgaEnabled').checked = config.enabled;
        document.getElementById('komgaUrl').value = config.url || '';

        // Voir le commentaire équivalent dans loadSettings (aMule) - même correctif
        if (config.api_key) {
            document.getElementById('komgaApiKey').value = config.api_key;
        }
    } catch (error) {
        showMessage('komgaMessage', '❌ Erreur lors du chargement de la configuration Komga', 'error');
    }
}

async function saveKomgaSettings() {
    const config = {
        enabled: document.getElementById('komgaEnabled').checked,
        url: document.getElementById('komgaUrl').value.trim(),
        api_key: document.getElementById('komgaApiKey').value
    };

    if (config.enabled && !config.url) {
        showMessage('komgaMessage', '⚠️ Veuillez entrer l\'URL du serveur Komga', 'warning');
        return;
    }

    if (config.enabled && !config.api_key) {
        showMessage('komgaMessage', '⚠️ Veuillez entrer la clé API Komga', 'warning');
        return;
    }

    try {
        const response = await fetch('/api/komga/config', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(config)
        });
        const data = await response.json();

        if (data.success) {
            showMessage('komgaMessage', '✅ Configuration Komga enregistrée avec succès !', 'success');
        } else {
            showMessage('komgaMessage', '❌ Erreur: ' + data.error, 'error');
        }
    } catch (error) {
        showMessage('komgaMessage', '❌ Erreur de connexion: ' + error.message, 'error');
    }
}

async function loadAnnasArchiveSettings() {
    try {
        const response = await fetch('/api/annas-archive/config');
        const config = await response.json();
        document.getElementById('annasArchiveEnabled').checked = config.enabled !== false;
        document.getElementById('annasArchiveBaseUrl').value = config.base_url || '';
    } catch (error) {
        showMessage('annasArchiveMessage', '❌ Erreur lors du chargement de la configuration Anna’s Archive', 'error');
    }
}

async function saveAnnasArchiveSettings() {
    try {
        const response = await fetch('/api/annas-archive/config', { method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ enabled: document.getElementById('annasArchiveEnabled').checked, base_url: document.getElementById('annasArchiveBaseUrl').value.trim() }) });
        const data = await response.json();
        showMessage('annasArchiveMessage', data.success ? '✅ Configuration Anna’s Archive enregistrée' : '❌ Erreur: ' + data.error, data.success ? 'success' : 'error');
        if (data.success) { if (typeof refreshEnabledIntegrations === 'function') refreshEnabledIntegrations(); loadIndexeurCardStatuses(); }
    } catch (error) { showMessage('annasArchiveMessage', '❌ Erreur de connexion: ' + error.message, 'error'); }
}

async function testAnnasArchiveConnection() {
    showMessage('annasArchiveMessage', '⏳ Test de connexion en cours...', 'info');
    try {
        const data = await (await fetch('/api/annas-archive/test')).json();
        showMessage('annasArchiveMessage', data.success ? '✅ Anna’s Archive est joignable' : '❌ Échec de la connexion: ' + data.error, data.success ? 'success' : 'error');
    } catch (error) { showMessage('annasArchiveMessage', '❌ Erreur de connexion: ' + error.message, 'error'); }
}

async function loadFourtouticiSettings() {
    try {
        const response = await fetch('/api/fourtoutici/config');
        const config = await response.json();
        document.getElementById('fourtouticiEnabled').checked = config.enabled !== false;
        document.getElementById('fourtouticiBaseUrl').value = config.base_url || '';
    } catch (error) {
        showMessage('fourtouticiMessage', '❌ Erreur lors du chargement de la configuration fourtoutici', 'error');
    }
}

async function saveFourtouticiSettings() {
    try {
        const response = await fetch('/api/fourtoutici/config', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                enabled: document.getElementById('fourtouticiEnabled').checked,
                base_url: document.getElementById('fourtouticiBaseUrl').value.trim()
            })
        });
        const data = await response.json();
        if (data.success) {
            showMessage('fourtouticiMessage', '✅ Configuration fourtoutici enregistrée avec succès !', 'success');
            // La case "fourtoutici" de Découvrir (voir discover.js) doit refléter ce
            // changement sans attendre un rechargement complet de la page.
            if (typeof refreshEnabledIntegrations === 'function') refreshEnabledIntegrations();
            loadIndexeurCardStatuses();
        } else {
            showMessage('fourtouticiMessage', '❌ Erreur: ' + data.error, 'error');
        }
    } catch (error) {
        showMessage('fourtouticiMessage', '❌ Erreur de connexion: ' + error.message, 'error');
    }
}

async function testFourtouticiConnection() {
    showMessage('fourtouticiMessage', '⏳ Test de connexion en cours...', 'info');
    try {
        const response = await fetch('/api/fourtoutici/test');
        const data = await response.json();
        if (data.success) {
            showMessage('fourtouticiMessage', '✅ fourtoutici.cc est joignable', 'success');
        } else {
            showMessage('fourtouticiMessage', '❌ Échec de la connexion: ' + data.error, 'error');
        }
    } catch (error) {
        showMessage('fourtouticiMessage', '❌ Erreur de connexion: ' + error.message, 'error');
    }
}

async function loadShelfmarkSettings() {
    try {
        const config = await (await fetch('/api/shelfmark/config')).json();
        document.getElementById('shelfmarkEnabled').checked = config.enabled === true;
        document.getElementById('shelfmarkBaseUrl').value = config.base_url || '';
        document.getElementById('shelfmarkUsername').value = config.username || '';
        const password = document.getElementById('shelfmarkPassword');
        // "pour shelfmark le mot de passe met 4 .... pas 3 ... comme les autres clients" -
        // qBittorrent/rTorrent/Deluge/eMule/EBDZ/Komga/Prowlarr renvoient TOUS déjà
        // '****' côté Flask sur un simple GET (jamais le secret en clair, voir le
        // commentaire "docs/security audit 2026-07-10" dans blueprints/qbittorrent/
        // routes.py) - Shelfmark n'a pas cette même chaîne littérale renvoyée par son
        // backend (GET /api/shelfmark/config ne renvoie qu'un booléen), donc ce sentinel
        // reste une pure convention côté client, mais autant reprendre EXACTEMENT le même
        // masque visuel ('****') que partout ailleurs plutôt qu'inventer '....' pour
        // cette seule intégration.
        password.value = config.password ? '****' : '';
        password.dataset.configured = config.password ? 'true' : 'false';
    } catch (error) { showMessage('shelfmarkMessage', '❌ Erreur de chargement Shelfmark', 'error'); }
}

async function saveShelfmarkSettings() {
    const passwordInput = document.getElementById('shelfmarkPassword');
    const payload = {
        enabled: document.getElementById('shelfmarkEnabled').checked,
        base_url: document.getElementById('shelfmarkBaseUrl').value.trim(),
        username: document.getElementById('shelfmarkUsername').value.trim(),
        // Le masque « **** » signifie conserver le secret déjà enregistré.
        password: passwordInput.value === '****' ? '' : passwordInput.value
    };
    try {
        const response = await fetch('/api/shelfmark/config', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload) });
        const data = await response.json();
        showMessage('shelfmarkMessage', data.success ? '✅ Configuration Shelfmark enregistrée' : '❌ ' + (data.error || 'Erreur'), data.success ? 'success' : 'error');
        if (data.success) {
            if (payload.password) {
                passwordInput.value = '****';
                passwordInput.dataset.configured = 'true';
            }
            loadClientCardStatuses();
        }
    } catch (error) { showMessage('shelfmarkMessage', '❌ ' + error.message, 'error'); }
}

function toggleShelfmarkPassword() {
    const input = document.getElementById('shelfmarkPassword');
    const button = input?.nextElementSibling;
    if (!input) return;
    input.type = input.type === 'password' ? 'text' : 'password';
    if (button) button.textContent = input.type === 'password' ? 'Afficher' : 'Masquer';
}

async function testShelfmarkConnection() {
    showMessage('shelfmarkMessage', '⏳ Test Shelfmark...', 'info');
    try {
        const password = document.getElementById('shelfmarkPassword').value;
        const payload = {base_url: document.getElementById('shelfmarkBaseUrl').value.trim(), username: document.getElementById('shelfmarkUsername').value.trim(), password: password === '****' ? '' : password};
        const data = await (await fetch('/api/shelfmark/test', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)})).json();
        showMessage('shelfmarkMessage', data.success ? '✅ Connexion Shelfmark réussie' : '❌ ' + (data.error || 'Échec'), data.success ? 'success' : 'error');
    } catch (error) { showMessage('shelfmarkMessage', '❌ ' + error.message, 'error'); }
}

async function testKomgaConnection() {
    showMessage('komgaMessage', '⏳ Test de connexion en cours...', 'info');
    try {
        // Teste les valeurs actuellement saisies dans le formulaire (pas besoin
        // d'enregistrer ni de cocher "Activer" au préalable)
        const response = await fetch('/api/komga/test', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                url: document.getElementById('komgaUrl').value.trim(),
                api_key: document.getElementById('komgaApiKey').value
            })
        });
        const data = await response.json();
        if (data.success) {
            showMessage('komgaMessage', '✅ ' + data.message, 'success');
        } else {
            showMessage('komgaMessage', '❌ Échec de la connexion: ' + data.error, 'error');
        }
    } catch (error) {
        showMessage('komgaMessage', '❌ Erreur: ' + error.message, 'error');
    }
}

function resetKomgaSettings() {
    if (!confirm('Voulez-vous réinitialiser la configuration Komga ?')) return;
    document.getElementById('komgaEnabled').checked = false;
    document.getElementById('komgaUrl').value = '';
    document.getElementById('komgaApiKey').value = '';
    showMessage('komgaMessage', '🔄 Configuration réinitialisée', 'info');
}

function toggleKomgaPassword() {
    const input = document.getElementById('komgaApiKey');
    const btn = input.closest('.password-input-group').querySelector('.btn-toggle-password');
    komgaPasswordVisible = !komgaPasswordVisible;
    input.type = komgaPasswordVisible ? 'text' : 'password';
    btn.innerHTML = komgaPasswordVisible ? `${svgIcon('eye-off')} Masquer` : `${svgIcon('eye')} Afficher`;
}

// ===== TELEGRAM =====
// Infrastructure seule pour l'instant ("ajouté une notification telegram" -> "just build
// the settings/infrastructure") - aucun événement de l'app n'appelle encore
// send_telegram_notification(), voir blueprints/telegram/routes.py.
let telegramPasswordVisible = false;

async function loadTelegramSettings() {
    try {
        const response = await fetch('/api/telegram/config');
        const config = await response.json();

        document.getElementById('telegramEnabled').checked = config.enabled;
        document.getElementById('telegramChatId').value = config.chat_id || '';
        document.getElementById('telegramNotifyImportCompleted').checked = config.notify_import_completed !== false;
        document.getElementById('telegramNotifyImportAvailable').checked = config.notify_import_available !== false;

        // Voir le commentaire équivalent dans loadSettings (aMule) - même correctif
        if (config.bot_token) {
            document.getElementById('telegramBotToken').value = config.bot_token;
        }
    } catch (error) {
        showMessage('telegramMessage', '❌ Erreur lors du chargement de la configuration Telegram', 'error');
    }
}

async function saveTelegramSettings() {
    const config = {
        enabled: document.getElementById('telegramEnabled').checked,
        bot_token: document.getElementById('telegramBotToken').value,
        chat_id: document.getElementById('telegramChatId').value.trim(),
        notify_import_completed: document.getElementById('telegramNotifyImportCompleted').checked,
        notify_import_available: document.getElementById('telegramNotifyImportAvailable').checked
    };

    if (config.enabled && !config.bot_token) {
        showMessage('telegramMessage', '⚠️ Veuillez entrer le jeton du bot Telegram', 'warning');
        return;
    }

    if (config.enabled && !config.chat_id) {
        showMessage('telegramMessage', '⚠️ Veuillez entrer le Chat ID', 'warning');
        return;
    }

    try {
        const response = await fetch('/api/telegram/config', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(config)
        });
        const data = await response.json();

        if (data.success) {
            showMessage('telegramMessage', '✅ Configuration Telegram enregistrée', 'success');
            loadTelegramSettings();
        } else {
            showMessage('telegramMessage', '❌ Erreur: ' + data.error, 'error');
        }
    } catch (error) {
        showMessage('telegramMessage', '❌ Erreur lors de la sauvegarde', 'error');
    }
}

async function testTelegramNotification() {
    showMessage('telegramMessage', '⏳ Envoi du message de test...', 'info');
    try {
        // Teste les valeurs actuellement saisies dans le formulaire (pas besoin
        // d'enregistrer ni de cocher "Activer" au préalable)
        const response = await fetch('/api/telegram/test', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                bot_token: document.getElementById('telegramBotToken').value,
                chat_id: document.getElementById('telegramChatId').value.trim()
            })
        });
        const data = await response.json();
        if (data.success) {
            showMessage('telegramMessage', '✅ Message de test envoyé - vérifiez Telegram', 'success');
        } else {
            showMessage('telegramMessage', '❌ Échec de l\'envoi: ' + data.error, 'error');
        }
    } catch (error) {
        showMessage('telegramMessage', '❌ Erreur: ' + error.message, 'error');
    }
}

function toggleTelegramPassword() {
    const input = document.getElementById('telegramBotToken');
    const btn = input.closest('.password-input-group').querySelector('.btn-toggle-password');
    telegramPasswordVisible = !telegramPasswordVisible;
    input.type = telegramPasswordVisible ? 'text' : 'password';
    btn.innerHTML = telegramPasswordVisible ? `${svgIcon('eye-off')} Masquer` : `${svgIcon('eye')} Afficher`;
}

// ===== TELEGRAM (CANAUX) - connexion "compte utilisateur" MTProto (Telethon), distincte
// des notifications bot ci-dessus. Flux en 3 étapes possibles: identifiants -> code reçu
// par Telegram -> mot de passe 2FA (uniquement si activé sur le compte) - voir
// blueprints/telegram_channels/routes.py pour le détail de chaque étape côté serveur. =====
let telegramChannelsApiHashVisible = false;

function toggleTelegramChannelsApiHashVisibility() {
    const input = document.getElementById('telegramChannelsApiHash');
    const btn = input.closest('.password-input-group').querySelector('.btn-toggle-password');
    telegramChannelsApiHashVisible = !telegramChannelsApiHashVisible;
    input.type = telegramChannelsApiHashVisible ? 'text' : 'password';
    btn.innerHTML = telegramChannelsApiHashVisible ? `${svgIcon('eye-off')} Masquer` : `${svgIcon('eye')} Afficher`;
}

// Affiche uniquement l'étape pertinente (identifiants / code / mot de passe 2FA) - les
// trois blocs coexistent dans le HTML (voir settings.html), display:none sur les autres.
function telegramChannelsShowStep(step) {
    const connected = document.getElementById('telegramChannelsConnected');
    const steps = {
        credentials: document.getElementById('telegramChannelsStepCredentials'),
        code: document.getElementById('telegramChannelsStepCode'),
        password: document.getElementById('telegramChannelsStepPassword')
    };
    connected.style.display = step === 'connected' ? 'block' : 'none';
    Object.entries(steps).forEach(([key, el]) => { el.style.display = key === step ? 'block' : 'none'; });
}

async function loadTelegramChannelsConfig() {
    document.getElementById('telegramChannelsMessage').innerHTML = '';
    try {
        const response = await fetch('/api/telegram-channels/config');
        const config = await response.json();

        if (config.connected) {
            document.getElementById('telegramChannelsWhoami').textContent =
                config.username ? `@${config.username}` : (config.first_name || 'compte connecté');
            telegramChannelsShowStep('connected');
            renderTelegramChannelsList(config.channels || []);
        } else {
            document.getElementById('telegramChannelsApiId').value = config.api_id || '';
            // api_hash/phone masqués ('****') ne sont volontairement pas reproposés en
            // clair dans le champ - un envoi de code sans y retoucher réutilise la valeur
            // déjà enregistrée côté serveur (voir /login/start, même convention que le
            // sentinel de mot de passe des autres intégrations)
            document.getElementById('telegramChannelsApiHash').value = '';
            document.getElementById('telegramChannelsApiHash').placeholder = config.api_hash ? '••••••••  (déjà enregistré)' : "Généré avec l'api_id ci-dessus";
            document.getElementById('telegramChannelsPhone').value = '';
            document.getElementById('telegramChannelsPhone').placeholder = config.phone ? '••••••••  (déjà enregistré)' : '+33612345678';
            telegramChannelsShowStep('credentials');
        }
    } catch (error) {
        showMessage('telegramChannelsMessage', '❌ Erreur lors du chargement: ' + error.message, 'error');
    }
}

async function telegramChannelsSendCode() {
    const btn = document.getElementById('telegramChannelsSendCodeBtn');
    const apiId = document.getElementById('telegramChannelsApiId').value.trim();
    const apiHash = document.getElementById('telegramChannelsApiHash').value.trim();
    const phone = document.getElementById('telegramChannelsPhone').value.trim();

    btn.disabled = true;
    showMessage('telegramChannelsMessage', '⏳ Envoi du code...', 'info');
    try {
        const response = await fetch('/api/telegram-channels/login/start', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ api_id: apiId || null, api_hash: apiHash, phone: phone })
        });
        const data = await response.json();
        if (data.success) {
            showMessage('telegramChannelsMessage', '✅ Code envoyé - vérifiez votre application Telegram', 'success');
            document.getElementById('telegramChannelsCode').value = '';
            telegramChannelsShowStep('code');
        } else {
            showMessage('telegramChannelsMessage', '❌ ' + (data.error || 'Erreur inconnue'), 'error');
        }
    } catch (error) {
        showMessage('telegramChannelsMessage', '❌ Erreur de connexion: ' + error.message, 'error');
    } finally {
        btn.disabled = false;
    }
}

async function telegramChannelsSubmitCode() {
    const btn = document.getElementById('telegramChannelsSubmitCodeBtn');
    const code = document.getElementById('telegramChannelsCode').value.trim();
    if (!code) {
        showMessage('telegramChannelsMessage', '⚠️ Entrez le code reçu', 'warning');
        return;
    }

    btn.disabled = true;
    showMessage('telegramChannelsMessage', '⏳ Vérification du code...', 'info');
    try {
        const response = await fetch('/api/telegram-channels/login/submit-code', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ code: code })
        });
        const data = await response.json();
        if (!data.success) {
            showMessage('telegramChannelsMessage', '❌ ' + (data.error || 'Erreur inconnue'), 'error');
            return;
        }
        if (data.needs_password) {
            document.getElementById('telegramChannelsMessage').innerHTML = '';
            document.getElementById('telegramChannelsPassword').value = '';
            telegramChannelsShowStep('password');
            return;
        }
        showMessage('telegramChannelsMessage', '✅ Connecté', 'success');
        await loadTelegramChannelsConfig();
        loadIndexeurCardStatuses();
    } catch (error) {
        showMessage('telegramChannelsMessage', '❌ Erreur de connexion: ' + error.message, 'error');
    } finally {
        btn.disabled = false;
    }
}

async function telegramChannelsSubmitPassword() {
    const btn = document.getElementById('telegramChannelsSubmitPasswordBtn');
    const password = document.getElementById('telegramChannelsPassword').value;
    if (!password) {
        showMessage('telegramChannelsMessage', '⚠️ Entrez le mot de passe', 'warning');
        return;
    }

    btn.disabled = true;
    showMessage('telegramChannelsMessage', '⏳ Vérification...', 'info');
    try {
        const response = await fetch('/api/telegram-channels/login/submit-password', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ password: password })
        });
        const data = await response.json();
        if (!data.success) {
            showMessage('telegramChannelsMessage', '❌ ' + (data.error || 'Erreur inconnue'), 'error');
            return;
        }
        showMessage('telegramChannelsMessage', '✅ Connecté', 'success');
        await loadTelegramChannelsConfig();
        loadIndexeurCardStatuses();
    } catch (error) {
        showMessage('telegramChannelsMessage', '❌ Erreur de connexion: ' + error.message, 'error');
    } finally {
        btn.disabled = false;
    }
}

function telegramChannelsResetFlow() {
    document.getElementById('telegramChannelsMessage').innerHTML = '';
    telegramChannelsShowStep('credentials');
}

async function telegramChannelsLogout() {
    if (!confirm('Se déconnecter de ce compte Telegram ?')) return;
    try {
        const response = await fetch('/api/telegram-channels/logout', { method: 'POST' });
        const data = await response.json();
        if (data.success) {
            await loadTelegramChannelsConfig();
            loadIndexeurCardStatuses();
        } else {
            showMessage('telegramChannelsMessage', '❌ ' + (data.error || 'Erreur inconnue'), 'error');
        }
    } catch (error) {
        showMessage('telegramChannelsMessage', '❌ Erreur de connexion: ' + error.message, 'error');
    }
}

// ===== Canaux surveillés + fichiers récents ("integre le channel @bd_frBE et
// N_art_BD_FRBE... telecharger et copier les fichiers... rajouter à la bonne série et
// volume" - item #16) - le téléchargement dépose le fichier directement dans un
// répertoire d'import surveillé (voir download_channel_file côté serveur), il suit
// ensuite exactement le même chemin qu'un fichier aMule/torrent (page /import, sélecteur
// de tome). =====
function renderTelegramChannelsList(channels) {
    const container = document.getElementById('telegramChannelsList');
    if (channels.length === 0) {
        container.innerHTML = '<p class="help-text">Aucun canal configuré.</p>';
        return;
    }
    container.innerHTML = channels.map(c => `
        <div style="display:flex; align-items:center; justify-content:space-between; padding:6px 0; border-bottom:1px solid var(--color-border);">
            <span>${escapeHtml(c.title)} <span class="help-text">(@${escapeHtml(c.username)})</span></span>
            <button class="btn-icon-only" onclick="telegramChannelsRemoveChannel('${escapeForAttribute(c.username)}')" data-tooltip="Retirer ce canal">${svgIcon('x')}</button>
        </div>
    `).join('');
}

async function telegramChannelsAddChannel() {
    const input = document.getElementById('telegramChannelsNewChannel');
    const channel = input.value.trim();
    if (!channel) return;

    showMessage('telegramChannelsMessage', '⏳ Vérification du canal...', 'info');
    try {
        const response = await fetch('/api/telegram-channels/channels', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ channel })
        });
        const data = await response.json();
        if (!data.success) {
            showMessage('telegramChannelsMessage', '❌ ' + (data.error || 'Erreur inconnue'), 'error');
            return;
        }
        input.value = '';
        showMessage('telegramChannelsMessage', `✅ Canal ajouté: ${data.channel.title}`, 'success');
        await loadTelegramChannelsConfig();
    } catch (error) {
        showMessage('telegramChannelsMessage', '❌ Erreur de connexion: ' + error.message, 'error');
    }
}

async function telegramChannelsRemoveChannel(channel) {
    try {
        const response = await fetch(`/api/telegram-channels/channels/${encodeURIComponent(channel)}`, { method: 'DELETE' });
        const data = await response.json();
        if (data.success) {
            await loadTelegramChannelsConfig();
        } else {
            showMessage('telegramChannelsMessage', '❌ ' + (data.error || 'Erreur inconnue'), 'error');
        }
    } catch (error) {
        showMessage('telegramChannelsMessage', '❌ Erreur de connexion: ' + error.message, 'error');
    }
}

async function telegramChannelsScrape() {
    const btn = document.getElementById('telegramChannelsScrapeBtn');
    btn.disabled = true;
    showMessage('telegramChannelsMessage', '⏳ Scraping des canaux en cours...', 'info');
    try {
        const response = await fetch('/api/telegram-channels/scrape', { method: 'POST' });
        const data = await response.json();
        if (!data.success) {
            showMessage('telegramChannelsMessage', '❌ ' + (data.error || 'Erreur inconnue'), 'error');
            return;
        }
        const parts = Object.values(data.summary).map(s => s.error ? `${s.title}: ❌ ${s.error}` : `${s.title}: ${s.new_count} nouveau(x)`);
        showMessage('telegramChannelsMessage', '✅ ' + parts.join(' · '), 'success');
    } catch (error) {
        showMessage('telegramChannelsMessage', '❌ Erreur de connexion: ' + error.message, 'error');
    } finally {
        btn.disabled = false;
    }
}

// "put in db all the telegram files like ebdz so we have instant search" - indexation de
// l'historique complet des canaux (voir /api/telegram-channels/backfill/*, scraper.py
// run_backfill_background), pour que la recherche (Recherche, static/js/search.js) porte
// sur toute l'archive plutôt que sur les seuls derniers messages scrapés. Sondage (10s,
// même cadence que EBDZ) UNIQUEMENT pendant qu'un backfill tourne réellement - pas de
// sondage permanent pour un état qui ne bouge presque jamais.
let _telegramBackfillPollTimer = null;

function _telegramBackfillStatusText(status) {
    // "quand tu redemarres le docker ca reste en cours malgre que le process est stoppé" -
    // s.done ne devient vrai qu'à la toute fin du backfill d'UN canal; un redémarrage du
    // conteneur (ou un arrêt manuel) tue le thread en plein milieu et fige l'état persisté
    // (telegram_backfill_state.json) sur un canal encore !done pour toujours - sans tenir
    // compte de status.running (repassé à false côté serveur au redémarrage), ce canal
    // affichait "⏳ en cours" indéfiniment alors que rien ne tournait plus réellement (le
    // bouton "Indexer" réapparaissait pourtant déjà correctement, contradiction visible).
    const perChannel = Object.entries(status.channels || {}).map(([channel, s]) => {
        const label = s.channel_title || channel;
        const state = s.error ? `❌ ${s.error}`
            : s.done ? '✅ terminé'
            : status.running ? '⏳ en cours'
            : '⏸️ interrompu (reprendra où il s\'est arrêté)';
        return `${label}: ${s.total_indexed || 0} ${pluralize(s.total_indexed || 0, 'fichier')} ${pluralize(s.total_indexed || 0, 'indexé')} - ${state}`;
    });
    return perChannel.length ? perChannel.join('<br>') : 'Aucune indexation lancée pour le moment.';
}

async function telegramChannelsBackfillPoll() {
    try {
        const response = await fetch('/api/telegram-channels/backfill/status');
        const data = await response.json();
        if (!data.success) return;

        document.getElementById('telegramBackfillStatusDiv').style.display = 'block';
        document.getElementById('telegramBackfillStatusText').innerHTML = _telegramBackfillStatusText(data);
        document.getElementById('telegramBackfillStartBtn').style.display = data.running ? 'none' : '';
        document.getElementById('telegramBackfillStopBtn').style.display = data.running ? '' : 'none';

        if (data.running && !_telegramBackfillPollTimer) {
            _telegramBackfillPollTimer = setInterval(telegramChannelsBackfillPoll, 10000);
        } else if (!data.running && _telegramBackfillPollTimer) {
            clearInterval(_telegramBackfillPollTimer);
            _telegramBackfillPollTimer = null;
        }
    } catch (error) {
        // best-effort - un sondage raté n'a pas besoin d'interrompre quoi que ce soit,
        // le suivant (10s plus tard) réessaiera de lui-même
    }
}

async function telegramChannelsBackfillStart() {
    const btn = document.getElementById('telegramBackfillStartBtn');
    btn.disabled = true;
    try {
        const response = await fetch('/api/telegram-channels/backfill/start', { method: 'POST' });
        const data = await response.json();
        if (!data.success) {
            showMessage('telegramChannelsMessage', '❌ ' + (data.error || 'Erreur inconnue'), 'error');
            return;
        }
        telegramChannelsBackfillPoll();
    } catch (error) {
        showMessage('telegramChannelsMessage', '❌ Erreur de connexion: ' + error.message, 'error');
    } finally {
        btn.disabled = false;
    }
}

async function telegramChannelsBackfillStop() {
    try {
        await fetch('/api/telegram-channels/backfill/stop', { method: 'POST' });
        telegramChannelsBackfillPoll();
    } catch (error) {
        showMessage('telegramChannelsMessage', '❌ Erreur de connexion: ' + error.message, 'error');
    }
}

// ===== Telegram (canaux): scraping automatique - même principe que loadAutoScrapeConfig
// (EBDZ) mais restreint à heures/jours (voir scheduler.py côté serveur). =====
async function loadTelegramAutoScrapeConfig() {
    try {
        const response = await fetch('/api/telegram-channels/auto-scrape/config');
        const config = await response.json();

        document.getElementById('telegramAutoScrapeEnabled').checked = config.auto_scrape_enabled;
        document.getElementById('telegramAutoScrapeInterval').value = config.auto_scrape_interval;
        document.getElementById('telegramAutoScrapeUnit').value = config.auto_scrape_interval_unit;

        updateTelegramAutoScrapeUI();
        checkTelegramAutoScrapeStatus();
    } catch (error) {
        console.error('Erreur lors du chargement de la config auto scrape Telegram:', error);
    }
}

function updateTelegramAutoScrapeUI() {
    const enabled = document.getElementById('telegramAutoScrapeEnabled').checked;
    document.getElementById('telegramAutoScrapeControlsDiv').style.display = enabled ? 'flex' : 'none';
    document.getElementById('telegramAutoScrapeStatusDiv').style.display = enabled ? 'block' : 'none';
}

async function saveTelegramAutoScrapeConfig() {
    try {
        const enabled = document.getElementById('telegramAutoScrapeEnabled').checked;
        const interval = parseInt(document.getElementById('telegramAutoScrapeInterval').value);
        const unit = document.getElementById('telegramAutoScrapeUnit').value;

        if (enabled && interval < 1) {
            showMessage('telegramChannelsMessage', "⚠️ L'intervalle doit être >= 1", 'warning');
            return;
        }

        const response = await fetch('/api/telegram-channels/auto-scrape/config', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                auto_scrape_enabled: enabled,
                auto_scrape_interval: interval,
                auto_scrape_interval_unit: unit
            })
        });
        const data = await response.json();

        if (data.success) {
            showMessage('telegramChannelsMessage',
                enabled ? `✅ Scraping automatique activé: tous les ${interval} ${unit}` : '✅ Scraping automatique désactivé',
                'success');
            updateTelegramAutoScrapeUI();
            checkTelegramAutoScrapeStatus();
        } else {
            showMessage('telegramChannelsMessage', '❌ Erreur: ' + data.error, 'error');
        }
    } catch (error) {
        showMessage('telegramChannelsMessage', '❌ Erreur: ' + error.message, 'error');
    }
}

async function checkTelegramAutoScrapeStatus() {
    try {
        const response = await fetch('/api/telegram-channels/auto-scrape/status');
        const data = await response.json();
        if (!data.success) return;

        const statusText = document.getElementById('telegramAutoScrapeStatusText');
        const nextRunText = document.getElementById('telegramAutoScrapeNextRunText');

        if (data.is_running) {
            statusText.textContent = '🟢 Actif';
            statusText.style.color = '#4CAF50';
            nextRunText.textContent = data.next_run ? new Date(data.next_run).toLocaleString('fr-FR', { timeZone: BULLARR_DISPLAY_TZ }) : 'Calcul en cours...';
        } else {
            statusText.textContent = '🔴 Inactif';
            statusText.style.color = '#f44336';
            nextRunText.textContent = '-';
        }
    } catch (error) {
        console.error('Erreur lors de la vérification du statut auto scrape Telegram:', error);
    }
}

// ===== SSO / OIDC =====
let oidcPasswordVisible = false;

async function loadOidcSettings() {
    try {
        const response = await fetch('/api/auth/config');
        const config = await response.json();

        document.getElementById('oidcEnabled').checked = config.enabled;
        document.getElementById('oidcIssuer').value = config.issuer || '';
        document.getElementById('oidcClientId').value = config.client_id || '';
        document.getElementById('oidcScopes').value = config.scopes || 'openid profile email';

        // Voir le commentaire équivalent dans loadSettings (aMule) - même correctif
        if (config.client_secret) {
            document.getElementById('oidcClientSecret').value = config.client_secret;
        }
    } catch (error) {
        showMessage('oidcMessage', '❌ Erreur lors du chargement de la configuration SSO', 'error');
    }
}

async function saveOidcSettings() {
    const config = {
        enabled: document.getElementById('oidcEnabled').checked,
        issuer: document.getElementById('oidcIssuer').value.trim(),
        client_id: document.getElementById('oidcClientId').value.trim(),
        client_secret: document.getElementById('oidcClientSecret').value,
        scopes: document.getElementById('oidcScopes').value.trim() || 'openid profile email'
    };

    if (config.enabled && !config.issuer) {
        showMessage('oidcMessage', "⚠️ Veuillez entrer l'URL de l'issuer OIDC", 'warning');
        return;
    }

    if (config.enabled && !config.client_id) {
        showMessage('oidcMessage', '⚠️ Veuillez entrer le Client ID OIDC', 'warning');
        return;
    }

    try {
        const response = await fetch('/api/auth/config', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(config)
        });
        const data = await response.json();

        if (data.success) {
            showMessage('oidcMessage', '✅ Configuration SSO enregistrée avec succès !', 'success');
        } else {
            showMessage('oidcMessage', '❌ Erreur: ' + data.error, 'error');
        }
    } catch (error) {
        showMessage('oidcMessage', '❌ Erreur de connexion: ' + error.message, 'error');
    }
}

async function testOidcConnection() {
    showMessage('oidcMessage', '⏳ Test de la découverte OIDC en cours...', 'info');
    try {
        const response = await fetch('/api/auth/test', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                issuer: document.getElementById('oidcIssuer').value.trim()
            })
        });
        const data = await response.json();
        if (data.success) {
            showMessage('oidcMessage', '✅ ' + data.message, 'success');
        } else {
            showMessage('oidcMessage', '❌ Échec de la découverte: ' + data.error, 'error');
        }
    } catch (error) {
        showMessage('oidcMessage', '❌ Erreur: ' + error.message, 'error');
    }
}

function resetOidcSettings() {
    if (!confirm('Voulez-vous réinitialiser la configuration SSO ?')) return;
    document.getElementById('oidcEnabled').checked = false;
    document.getElementById('oidcIssuer').value = '';
    document.getElementById('oidcClientId').value = '';
    document.getElementById('oidcClientSecret').value = '';
    document.getElementById('oidcScopes').value = 'openid profile email';
    showMessage('oidcMessage', '🔄 Configuration réinitialisée', 'info');
}

function toggleOidcPassword() {
    const input = document.getElementById('oidcClientSecret');
    const btn = input.closest('.password-input-group').querySelector('.btn-toggle-password');
    oidcPasswordVisible = !oidcPasswordVisible;
    input.type = oidcPasswordVisible ? 'text' : 'password';
    btn.innerHTML = oidcPasswordVisible ? `${svgIcon('eye-off')} Masquer` : `${svgIcon('eye')} Afficher`;
}

// ===== PROWLARR INDEXERS =====
async function loadProwlarrIndexers() {
    showMessage('indexersMessage', '⏳ Récupération des indexeurs en cours...', 'info');
    try {
        const response = await fetch('/api/prowlarr/indexers');
        const data = await response.json();
        
        if (!data.success) {
            // Afficher le message d'erreur de manière lisible
            let errorMsg = data.error || 'Erreur inconnue';
            if (errorMsg.includes('URLs essayées')) {
                // Le message contient des infos de debug, l'afficher complètement
                showMessage('indexersMessage', '❌ ' + errorMsg, 'error');
            } else {
                showMessage('indexersMessage', '❌ Erreur: ' + errorMsg, 'error');
            }
            return;
        }
        
        if (!data.indexers || data.indexers.length === 0) {
            showMessage('indexersMessage', '⚠️ Aucun indexeur trouvé dans Prowlarr', 'warning');
        }
        
        displayIndexers(data.indexers || []);
        showMessage('indexersMessage', '✅ Indexeurs chargés avec succès !', 'success');
    } catch (error) {
        showMessage('indexersMessage', '❌ Erreur de connexion: ' + error.message, 'error');
    }
}

function displayIndexers(indexers) {
    const list = document.getElementById('indexersList');
    
    if (indexers.length === 0) {
        list.innerHTML = '<p style="color: #999;">Aucun indexeur trouvé</p>';
        return;
    }
    
    let html = '';
    indexers.forEach(indexer => {
        let categoriesHtml = '';
        
        if (indexer.categories && indexer.categories.length > 0) {
            categoriesHtml = '<div style="margin-top: 12px; padding-top: 12px; border-top: 1px solid #e0e0e0;">';
            categoriesHtml += '<strong style="display: block; margin-bottom: 8px; font-size: 0.9em;">Catégories:</strong>';
            categoriesHtml += '<div style="display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap: 8px;">';
            
            indexer.categories.forEach(category => {
                const isSubcategory = category.name.startsWith('  ↳');
                categoriesHtml += `
                    <label style="display: flex; align-items: center; gap: 8px; cursor: pointer; font-size: 0.85em; padding: ${isSubcategory ? '2px 0 2px 10px' : '0'};">
                        <input type="checkbox" class="category-checkbox" data-indexer-id="${indexer.id}" data-category-id="${category.id}" ${category.selected ? 'checked' : ''}>
                        <span>${category.name}</span>
                    </label>
                `;
            });
            
            categoriesHtml += '</div></div>';
        } else {
            categoriesHtml = '<div style="margin-top: 10px; padding: 10px; background: #f9f9f9; border-radius: 3px; border-left: 3px solid #ffc107; font-size: 0.85em; color: #666;">⚠️ Pas de catégories trouvées pour cet indexeur</div>';
        }
        
        html += `
            <div style="padding: 15px; border: 1px solid #e0e0e0; border-radius: 5px; margin-bottom: 15px; background: #fafafa;">
                <div style="display: flex; align-items: center; gap: 15px; margin-bottom: 8px;">
                    <input type="checkbox" id="indexer-${indexer.id}" class="indexer-checkbox" value="${indexer.id}" ${indexer.selected ? 'checked' : ''}>
                    <div style="flex: 1;">
                        <label for="indexer-${indexer.id}" style="cursor: pointer; margin: 0;">
                            <strong>${indexer.name}</strong>
                            <span style="color: #999; font-size: 0.9em; margin-left: 10px;">(ID: ${indexer.id})</span>
                        </label>
                        ${indexer.language ? `<div style="font-size: 0.85em; color: #666;">🌐 ${indexer.language}</div>` : ''}
                    </div>
                </div>
                ${categoriesHtml}
            </div>
        `;
    });
    
    list.innerHTML = html;
}

async function saveProwlarrIndexers() {
    const selected = [];
    const selectedCategories = {};
    
    // Récupérer les indexeurs sélectionnés
    document.querySelectorAll('.indexer-checkbox:checked').forEach(checkbox => {
        selected.push(parseInt(checkbox.value));
        selectedCategories[checkbox.value.toString()] = [];
    });
    
    // Récupérer les catégories sélectionnées pour chaque indexeur
    document.querySelectorAll('.category-checkbox:checked').forEach(checkbox => {
        const indexerId = checkbox.getAttribute('data-indexer-id');
        const categoryId = parseInt(checkbox.getAttribute('data-category-id'));
        
        if (selectedCategories[indexerId]) {
            if (!Array.isArray(selectedCategories[indexerId])) {
                selectedCategories[indexerId] = [];
            }
            selectedCategories[indexerId].push(categoryId);
        }
    });
    
    try {
        const response = await fetch('/api/prowlarr/indexers', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ 
                selected_indexers: selected,
                selected_categories: selectedCategories
            })
        });
        const data = await response.json();
        
        if (data.success) {
            showMessage('indexersMessage', '✅ Indexeurs et catégories enregistrés !', 'success');
        } else {
            showMessage('indexersMessage', '❌ Erreur: ' + data.error, 'error');
        }
    } catch (error) {
        showMessage('indexersMessage', '❌ Erreur de connexion: ' + error.message, 'error');
    }
}


// ===== TABS =====
// Navigation par sous-liens dans la colonne "Configuration" de la barre latérale (voir
// #settings-nav-subgroup dans settings.html) plutôt que par une barre d'onglets
// horizontale en haut de page, qui débordait et rendait certains onglets inaccessibles.
function switchTab(tabName) {
    // Supprimer les classes active de tous les liens et contenus
    document.querySelectorAll('.nav-sublink').forEach(link => link.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(content => content.classList.remove('active'));

    // Ajouter la classe active au lien cliqué (sous-menu "Configuration" de la barre
    // latérale) en utilisant data-tab
    const activeLink = document.querySelector(`.nav-sublink[data-tab="${tabName}"]`);
    if (activeLink) {
        activeLink.classList.add('active');
        activeLink.scrollIntoView({ behavior: 'smooth', inline: 'nearest', block: 'nearest' });
    }

    // Afficher le contenu de l'onglet
    const tabElement = document.getElementById('tab-' + tabName);
    if (tabElement) {
        tabElement.classList.add('active');
        // Mettre à jour l'URL avec le hash
        window.history.replaceState(null, null, '#' + tabName);
    }
    
    // Charger les données spécifiques à chaque onglet
    if (tabName === 'indexeurs') {
        loadIndexeurCardStatuses();
    } else if (tabName === 'clients') {
        loadClientCardStatuses();
    } else if (tabName === 'telegram') {
        loadTelegramSettings();
    } else if (tabName === 'oidc') {
        loadOidcSettings();
    } else if (tabName === 'theme') {
        initThemeSwitch();
    } else if (tabName === 'search-formats') {
        initSearchFormatPriority();
        initSearchSourcePriority();
        // "quand tu restart l'app c'est encore désactivé" - la case "Téléchargement
        // automatique à l'ajout" vit visuellement dans CET onglet (déplacée ici à côté de
        // "Ordre des sources", voir le commentaire dans settings.html), mais seul l'onglet
        // import-auto appelait loadAutoImportConfig() - la valeur réellement sauvegardée
        // en base n'était donc jamais relue ici, la case retombant à chaque fois sur son
        // état HTML par défaut (décoché), qu'elle soit vraiment activée ou non côté serveur.
        loadAutoImportConfig();
    } else if (tabName === 'monitoring') {
        loadMonitoringConfig();
    } else if (tabName === 'import-auto') {
        loadAutoImportConfig();
    } else if (tabName === 'libraries') {
        loadRenameTemplateSettings();
    } else if (tabName === 'backup') {
        // Rien à charger: la carte "Créer un backup" est un simple lien de téléchargement
    }
}

// ===== CARTES INDEXEURS / CLIENTS =====
// Prowlarr+ebdz.net (Indexeurs) et aMule+qBittorrent (Clients) ne sont plus des onglets
// séparés mais des cartes cliquables qui ouvrent leur formulaire de configuration
// existant (inchangé) dans une modale - voir templates/settings.html, .modal#tab-amule/
// #tab-ebdz/#tab-prowlarr/#tab-qbittorrent. "change prowlarr, ebz à une seule page de
// configuration nommée indexeur... carrés et en cliquant dessus tu ouvres une fenetre
// modale... fait la meme chose pour les clients avec qbittorent, amule"
const INTEGRATION_MODAL_LOADERS = {
    amule: () => loadSettings(),
    ebdz: () => { loadEbdzConfig(); loadAutoScrapeConfig(); },
    prowlarr: () => loadProwlarrSettings(),
    qbittorrent: () => loadQbittorrentSettings(),
    rtorrent: () => loadRtorrentSettings(),
    deluge: () => loadDelugeSettings(),
    shelfmark: () => loadShelfmarkSettings(),
    komga: () => loadKomgaSettings(),
    web: () => { loadFourtouticiSettings(); loadAnnasArchiveSettings(); loadShelfmarkSettings(); },
    'telegram-channels': () => { loadTelegramChannelsConfig(); loadTelegramAutoScrapeConfig(); telegramChannelsBackfillPoll(); }
};

function openIntegrationModal(name) {
    const modal = document.getElementById('tab-' + name);
    if (!modal) return;
    modal.classList.add('active');
    const loader = INTEGRATION_MODAL_LOADERS[name];
    if (loader) loader();
}

// name: 'amule'|'ebdz'|'prowlarr'|'qbittorrent'|'rtorrent'|'deluge'|'shelfmark'|'komga'. Rafraîchit
// aussi le badge de statut de la carte correspondante à la fermeture (l'utilisateur vient
// peut-être de sauvegarder un changement d'état activé/désactivé).
function closeIntegrationModal(name) {
    const modal = document.getElementById('tab-' + name);
    if (modal) modal.classList.remove('active');
    if (name === 'amule' || name === 'qbittorrent' || name === 'rtorrent' || name === 'deluge' || name === 'shelfmark') {
        loadClientCardStatuses();
    } else {
        loadIndexeurCardStatuses();
    }
}

// Fonctions zéro-argument dédiées pour le fermeture générique de modale (Échap/clic sur
// le fond, voir MODAL_CLOSE_FUNCTIONS dans nav.js) qui appelle window[fnName]() sans
// paramètre - closeIntegrationModal(name) seul ne peut pas être référencé directement là
function closeAmuleModal() { closeIntegrationModal('amule'); }
function closeEbdzConfigModal() { closeIntegrationModal('ebdz'); }
function closeProwlarrModal() { closeIntegrationModal('prowlarr'); }
function closeQbittorrentModal() { closeIntegrationModal('qbittorrent'); }
function closeRtorrentModal() { closeIntegrationModal('rtorrent'); }
function closeDelugeModal() { closeIntegrationModal('deluge'); }
function closeKomgaConfigModal() { closeIntegrationModal('komga'); }
function closeWebSourcesModal() { closeIntegrationModal('web'); }
function closeTelegramChannelsModal() { closeIntegrationModal('telegram-channels'); }

async function updateIntegrationCardStatus(name, endpoint, isEnabled) {
    const badge = document.getElementById('card-status-' + name);
    if (!badge) return;
    try {
        const response = await fetch(endpoint);
        const config = await response.json();
        const result = isEnabled(config);
        badge.textContent = result.label;
        badge.className = 'integration-card-status ' + (result.on ? 'integration-card-status-on' : 'integration-card-status-off');
    } catch (error) {
        badge.textContent = '?';
    }
}

function loadIndexeurCardStatuses() {
    // Nombre d'indexeurs sélectionnés (config.selected_indexers, déjà en base - pas
    // d'appel live à Prowlarr ici pour un simple badge de carte) plutôt qu'un simple
    // Activé/Désactivé - "résumé des indexeurs Prowlarr activés" directement visible sans
    // ouvrir la modale.
    updateIntegrationCardStatus('prowlarr', '/api/prowlarr/config', config => {
        const count = (config.selected_indexers || []).length;
        if (!config.enabled) return { on: false, label: 'Désactivé' };
        return { on: count > 0, label: `${count} indexeur${count > 1 ? 's' : ''} activé${count > 1 ? 's' : ''}` };
    });
    // ebdz.net n'a pas de simple bascule "activé/désactivé" comme les autres intégrations
    // (voir GET /api/ebdz/config) - "configuré" est déduit de la présence d'au moins un
    // forum enregistré, seul signal disponible côté API sans dupliquer sa logique.
    updateIntegrationCardStatus('ebdz', '/api/ebdz/config', config => {
        const configured = (config.forums || []).length > 0;
        return { on: configured, label: configured ? 'Configuré' : 'Non configuré' };
    });
    updateIntegrationCardStatus('komga', '/api/komga/config', config => ({
        on: !!config.enabled,
        label: config.enabled ? 'Activé' : 'Désactivé'
    }));
    updateIntegrationCardStatus('telegram-channels', '/api/telegram-channels/config', config => ({
        on: !!config.connected,
        label: config.connected ? `Connecté (${config.username ? '@' + config.username : config.first_name})` : 'Non connecté'
    }));
    // Web Archive regroupe les deux sources publiques configurables dans sa modale.
    Promise.all([fetch('/api/fourtoutici/config'), fetch('/api/annas-archive/config')])
        .then(async ([four, anna]) => {
            const fourConfig = await four.json();
            const annaConfig = await anna.json();
            const active = [fourConfig.enabled !== false, annaConfig.enabled !== false].filter(Boolean).length;
            const badge = document.getElementById('card-status-web');
            if (badge) {
                badge.textContent = `${active}/2 sources actives`;
                badge.className = 'integration-card-status ' + (active ? 'integration-card-status-on' : 'integration-card-status-off');
            }
        }).catch(() => {});
}

function loadClientCardStatuses() {
    updateIntegrationCardStatus('amule', '/api/emule/config', config => ({
        on: !!config.enabled,
        label: config.enabled ? 'Activé' : 'Désactivé'
    }));
    updateIntegrationCardStatus('qbittorrent', '/api/qbittorrent/config', config => ({
        on: !!config.enabled,
        label: config.enabled ? 'Activé' : 'Désactivé'
    }));
    updateIntegrationCardStatus('rtorrent', '/api/rtorrent/config', config => ({
        on: !!config.enabled,
        label: config.enabled ? 'Activé' : 'Désactivé'
    }));
    updateIntegrationCardStatus('deluge', '/api/deluge/config', config => ({
        on: !!config.enabled,
        label: config.enabled ? 'Activé' : 'Désactivé'
    }));
    updateIntegrationCardStatus('shelfmark', '/api/shelfmark/config', config => ({
        on: !!config.enabled,
        label: config.enabled ? 'Activé' : 'Désactivé'
    }));
}


// ===== FORMAT DE RENOMMAGE (tomes + dossier de série) =====
// "j'aimerais qu'il y ait un exemple quand je met les parametres en bas" - aperçu en
// direct sous chaque champ plutôt qu'un exemple statique unique plus haut dans la page
// (retiré): reflète le format RÉELLEMENT saisi, pas seulement le format par défaut.
// Réimplémentation JS minimale de render_standard_template (rename_handler.py, même
// règle de bloc optionnel: un bloc {...} disparaît entièrement si l'un de ses tags <...>
// n'a pas de valeur) - inévitable ici (aperçu instantané, sans aller-retour serveur à
// chaque frappe), mais tenue volontairement comme un simple miroir de cette fonction:
// toute évolution de la syntaxe des blocs côté Python doit être répercutée ici aussi.
const RENAME_PREVIEW_SAMPLE_TOKENS = {
    series: 'Dans Les Forêts De Bambous',
    number2: '01',
    title: 'Pandas Dans La Brume',
    year: '2010',
    quality: 'Digital-1734',
    group: 'NEO RIP-Club',
    univers: 'Thorgal',
};

function _renderRenameTemplatePreview(template, tokens) {
    if (!template) return '';
    let rendered = template.replace(/\{([^{}]*)\}/g, (fullMatch, block) => {
        const blockTags = block.match(/<([a-zA-Z0-9_]+)>/g) || [];
        const missing = blockTags.some(t => !tokens[t.slice(1, -1)]);
        if (missing) return '';
        return block.replace(/<([a-zA-Z0-9_]+)>/g, (m, tag) => tokens[tag] || '');
    });
    rendered = rendered.replace(/<([a-zA-Z0-9_]+)>/g, (m, tag) => tokens[tag] || '');
    return rendered.replace(/ {2,}/g, ' ').trim();
}

function _updateRenameTemplatePreviews() {
    const volumeName = _renderRenameTemplatePreview(
        document.getElementById('renameVolumeTemplate').value, RENAME_PREVIEW_SAMPLE_TOKENS
    );
    document.getElementById('renameVolumeTemplatePreview').textContent =
        volumeName ? `Exemple : ${volumeName}.cbz` : '';

    const oneshotName = _renderRenameTemplatePreview(
        document.getElementById('renameOneshotTemplate').value, RENAME_PREVIEW_SAMPLE_TOKENS
    );
    document.getElementById('renameOneshotTemplatePreview').textContent =
        oneshotName ? `Exemple : ${oneshotName}.cbz` : '';

    const seriesName = _renderRenameTemplatePreview(
        document.getElementById('renameSeriesTemplate').value, RENAME_PREVIEW_SAMPLE_TOKENS
    );
    document.getElementById('renameSeriesTemplatePreview').textContent =
        seriesName ? `Exemple : ${seriesName}` : '';
}

async function loadRenameTemplateSettings() {
    try {
        const response = await fetch('/api/settings/rename');
        const config = await response.json();
        document.getElementById('renameVolumeTemplate').value = config.volume_template;
        document.getElementById('renameOneshotTemplate').value = config.oneshot_template;
        document.getElementById('renameSeriesTemplate').value = config.series_template;
        _updateRenameTemplatePreviews();
    } catch (error) {
        console.error('Erreur lors du chargement du format de renommage:', error);
    }
}

async function saveRenameTemplateSettings() {
    const config = {
        volume_template: document.getElementById('renameVolumeTemplate').value.trim(),
        oneshot_template: document.getElementById('renameOneshotTemplate').value.trim(),
        series_template: document.getElementById('renameSeriesTemplate').value.trim()
    };

    try {
        const response = await fetch('/api/settings/rename', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(config)
        });
        const data = await response.json();

        const msg = document.getElementById('renameTemplateMessage');
        if (data.success) {
            document.getElementById('renameVolumeTemplate').value = data.volume_template;
            document.getElementById('renameOneshotTemplate').value = data.oneshot_template;
            document.getElementById('renameSeriesTemplate').value = data.series_template;
            _updateRenameTemplatePreviews();
            msg.textContent = '✓ Format enregistré avec succès!';
            msg.className = 'message success';
        } else {
            msg.textContent = '✗ Erreur: ' + data.error;
            msg.className = 'message error';
        }
        msg.style.display = 'block';
        setTimeout(() => { msg.style.display = 'none'; }, 5000);
    } catch (error) {
        console.error('Erreur lors de la sauvegarde du format de renommage:', error);
    }
}

async function resetRenameTemplateSettings() {
    try {
        const response = await fetch('/api/settings/rename');
        const config = await response.json();
        document.getElementById('renameVolumeTemplate').value = config.defaults.volume_template;
        document.getElementById('renameOneshotTemplate').value = config.defaults.oneshot_template;
        document.getElementById('renameSeriesTemplate').value = config.defaults.series_template;
        _updateRenameTemplatePreviews();
    } catch (error) {
        console.error('Erreur lors de la réinitialisation du format de renommage:', error);
    }
}

// ===== THÈME =====
// setTheme/getTheme vivent dans nav.js (chargé sur toutes les pages, pas seulement ici) -
// ce switch ne fait que refléter/déclencher l'état déjà géré là-bas.
function initThemeSwitch() {
    _updateThemeSwitchUI(getTheme());
}

function selectThemeOption(mode) {
    // setTheme (nav.js) appelle déjà _updateThemeSwitchUI lui-même, pour rester synchronisé
    // avec le bouton clair/sombre de l'en-tête si ce dernier bascule le thème à la place.
    setTheme(mode);
}

function _updateThemeSwitchUI(mode) {
    document.querySelectorAll('#themeSwitch .theme-switch-option').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.themeValue === mode);
    });
}

// ===== BADGES =====
// ===== FORMATS DE RECHERCHE =====
// Ordre de PRÉFÉRENCE (pas un filtre - voir search-results-table.js) entre formats dans
// les résultats EBDZ/Prowlarr (voir getSearchFormatPriority dans search-results-table.js,
// chargé aussi sur cette page pour partager SEARCH_FORMAT_PRIORITY_DEFAULT plutôt que de
// dupliquer la liste des formats connus ici). Édité en mémoire (searchFormatPriorityDraft)
// et seulement écrit en localStorage au clic sur "Sauvegarder", comme le reste de cette
// page (pas d'auto-save à chaque case cochée/déplacement).
let searchFormatPriorityDraft = [];

function initSearchFormatPriority() {
    searchFormatPriorityDraft = getSearchFormatPriority().map(entry => ({ ...entry }));
    renderSearchFormatPriorityList();
}

function renderSearchFormatPriorityList() {
    const list = document.getElementById('searchFormatPriorityList');
    if (!list) return;
    list.innerHTML = searchFormatPriorityDraft.map((entry, i) => `
        <li class="format-priority-item${entry.enabled ? '' : ' disabled'}">
            <span class="format-priority-rank">${i + 1}</span>
            <label style="display:flex; align-items:center; gap:8px; flex:1; cursor:pointer;">
                <input type="checkbox" ${entry.enabled ? 'checked' : ''} onchange="toggleSearchFormatEnabled(${i})">
                <span class="format-priority-name">${entry.format}</span>
            </label>
            <div class="format-priority-controls">
                <button type="button" onclick="moveSearchFormatPriority(${i}, -1)" ${i === 0 ? 'disabled' : ''} title="Monter">▲</button>
                <button type="button" onclick="moveSearchFormatPriority(${i}, 1)" ${i === searchFormatPriorityDraft.length - 1 ? 'disabled' : ''} title="Descendre">▼</button>
            </div>
        </li>
    `).join('');
}

function toggleSearchFormatEnabled(index) {
    searchFormatPriorityDraft[index].enabled = !searchFormatPriorityDraft[index].enabled;
    renderSearchFormatPriorityList();
}

function moveSearchFormatPriority(index, delta) {
    const target = index + delta;
    if (target < 0 || target >= searchFormatPriorityDraft.length) return;
    [searchFormatPriorityDraft[index], searchFormatPriorityDraft[target]] =
        [searchFormatPriorityDraft[target], searchFormatPriorityDraft[index]];
    renderSearchFormatPriorityList();
}

function saveSearchFormatPriority() {
    localStorage.setItem('searchFormatPriority', JSON.stringify(searchFormatPriorityDraft));
    showToast('search-format-priority-saved', '✅ Formats de recherche sauvegardés !', { icon: 'check', autoHideMs: 3000 });
}

function resetSearchFormatPriority() {
    if (!confirm('Êtes-vous sûr de vouloir réinitialiser les formats de recherche aux valeurs par défaut ?')) {
        return;
    }
    searchFormatPriorityDraft = SEARCH_FORMAT_PRIORITY_DEFAULT.map(entry => ({ ...entry }));
    renderSearchFormatPriorityList();
    saveSearchFormatPriority();
}

// Ordre de préférence entre sources (Prowlarr/EBDZ) - même mécanique que le tri des
// formats juste au-dessus mais sans case à cocher (on ne peut pas "désactiver" une source
// ici, juste la faire passer en second, voir getSearchSourcePriority dans
// search-results-table.js). Demandé explicitement en plus du filtre de formats: "ajoute
// aussi ordre prowlarr > ebdz".
let searchSourcePriorityDraft = [];

// "dans l'ordre des sources mets bien les icones de prowlarr, telegram" - les mêmes
// logos que ceux affichés dans la colonne Source des résultats de recherche (voir
// _searchResultSourceIconHtml, search-results-table.js), pas des emojis/texte brut;
// telegram manquait entièrement (SEARCH_SOURCE_PRIORITY_DEFAULT l'inclut pourtant déjà).
// Taille en style inline plutôt que via .replace-results-source-icon ("les icones sont
// gigantesques"): cette classe vit dans style-library-search.css, jamais chargé sur la
// page Settings - sans règle de taille applicable, l'image s'affichait à sa résolution
// native.
const _SEARCH_SOURCE_ICON_STYLE = 'width:16px; height:16px; object-fit:contain; vertical-align:-3px;';
const SEARCH_SOURCE_LABELS = {
    prowlarr: `<img src="/static/img/prowlarr-logo.svg" alt="" style="${_SEARCH_SOURCE_ICON_STYLE}"> Prowlarr`,
    ebdz: `<img src="/static/img/ebdz-logo.png" alt="" style="${_SEARCH_SOURCE_ICON_STYLE}"> EBDZ`,
    telegram: `<img src="/static/img/telegram-logo.svg" alt="" style="${_SEARCH_SOURCE_ICON_STYLE}"> Telegram`,
    fourtoutici: `<img src="/static/img/fourtoutici-logo.svg" alt="" style="${_SEARCH_SOURCE_ICON_STYLE}"> fourtoutici`,
    annas_archive: `<img src="/static/img/annas-archive-favicon.ico" alt="" style="${_SEARCH_SOURCE_ICON_STYLE}"> Anna’s Archive`
};

async function initSearchSourcePriority() {
    // Affiche d'abord la valeur locale connue (rendu instantané), puis se resynchronise
    // sur le serveur (référence désormais, voir syncSearchSourcePriorityFromServer côté
    // search-results-table.js) - sans ça, ouvrir cet onglet dans un navigateur qui n'a
    // jamais sauvegardé cet ordre localement affichait à tort l'ordre par défaut plutôt
    // que l'ordre réellement actif.
    searchSourcePriorityDraft = [...getSearchSourcePriority()];
    try { if (typeof refreshEnabledIntegrations === 'function') await refreshEnabledIntegrations(); } catch (e) {}
    const active = source => source === 'prowlarr' ? enabledIntegrations.prowlarr
        : source === 'ebdz' ? enabledIntegrations.ebdz
        : source === 'fourtoutici' ? enabledIntegrations.fourtoutici
        : source === 'annas_archive' ? enabledIntegrations.annas_archive
        : true;
    try {
        const config = await (await fetch('/api/import/config')).json();
        if (Array.isArray(config.auto_acquire_sources) && config.auto_acquire_sources.length) {
            searchSourcePriorityDraft = [...config.auto_acquire_sources];
        }
    } catch (e) {}
    searchSourcePriorityDraft = searchSourcePriorityDraft.filter(active);
    SEARCH_SOURCE_PRIORITY_DEFAULT.filter(active).forEach(source => {
        if (!searchSourcePriorityDraft.includes(source)) searchSourcePriorityDraft.push(source);
    });
    renderSearchSourcePriorityList();
}

function renderSearchSourcePriorityList() {
    const list = document.getElementById('searchSourcePriorityList');
    if (!list) return;
    list.innerHTML = searchSourcePriorityDraft.map((source, i) => `
        <li class="format-priority-item">
            <span class="format-priority-rank">${i + 1}</span>
            <span class="format-priority-name">${SEARCH_SOURCE_LABELS[source] || source}</span>
            <div class="format-priority-controls">
                <button type="button" onclick="moveSearchSourcePriority(${i}, -1)" ${i === 0 ? 'disabled' : ''} title="Monter">▲</button>
                <button type="button" onclick="moveSearchSourcePriority(${i}, 1)" ${i === searchSourcePriorityDraft.length - 1 ? 'disabled' : ''} title="Descendre">▼</button>
            </div>
        </li>
    `).join('');
}

function moveSearchSourcePriority(index, delta) {
    const target = index + delta;
    if (target < 0 || target >= searchSourcePriorityDraft.length) return;
    [searchSourcePriorityDraft[index], searchSourcePriorityDraft[target]] =
        [searchSourcePriorityDraft[target], searchSourcePriorityDraft[index]];
    renderSearchSourcePriorityList();
}

function saveSearchSourcePriority() {
    localStorage.setItem('searchSourcePriority', JSON.stringify(searchSourcePriorityDraft));
    showToast('search-source-priority-saved', '✅ Ordre des sources sauvegardé !', { icon: 'check', autoHideMs: 3000 });

    // Cet ordre n'existait jusqu'ici qu'en localStorage (uniquement lu côté client, pour
    // trier le tableau de résultats de /search - voir getSearchSourcePriority,
    // search-results-table.js). L'acquisition automatique à l'ajout d'une série
    // (blueprints/bedetheque/auto_acquire.py) tourne elle côté serveur, dans un thread
    // d'arrière-plan sans accès au localStorage du navigateur - "l'ordre des sources
    // c'est déjà dans recherche, supprime cette partie de import" (pas de 2ème réglage
    // dupliqué) implique donc de transmettre CE même ordre au serveur aussi, plutôt que
    // de le re-régler ailleurs. Best-effort, silencieux: ne doit jamais faire échouer la
    // sauvegarde locale qui vient de réussir juste au-dessus.
    fetch('/api/import/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ auto_acquire_sources: searchSourcePriorityDraft })
    }).catch(() => {});
}

function resetSearchSourcePriority() {
    if (!confirm('Êtes-vous sûr de vouloir réinitialiser l\'ordre des sources aux valeurs par défaut ?')) {
        return;
    }
    const active = source => source === 'prowlarr' ? enabledIntegrations.prowlarr
        : source === 'ebdz' ? enabledIntegrations.ebdz
        : source === 'fourtoutici' ? enabledIntegrations.fourtoutici
        : source === 'annas_archive' ? enabledIntegrations.annas_archive
        : true;
    searchSourcePriorityDraft = SEARCH_SOURCE_PRIORITY_DEFAULT.filter(active);
    renderSearchSourcePriorityList();
    saveSearchSourcePriority();
}

// ===== QBITTORRENT =====
let qbittorrentPasswordVisible = false;

async function loadQbittorrentSettings() {
    try {
        const response = await fetch('/api/qbittorrent/config');
        const config = await response.json();
        
        document.getElementById('qbittorrentEnabled').checked = config.enabled;
        document.getElementById('qbittorrentUrl').value = config.url || '';
        document.getElementById('qbittorrentPort').value = config.port || '';
        document.getElementById('qbittorrentUsername').value = config.username || '';
        
        // Charger le mot de passe déchiffré
        if (config.password) {
            document.getElementById('qbittorrentPassword').value = config.password;
        }
        
        // Charger les catégories disponibles D'ABORD
        await loadQbittorrentCategories();
        
        // PUIS mettre la catégorie sauvegardée après que les options soient chargées
        if (config.default_category) {
            document.getElementById('qbittorrentDefaultCategory').value = config.default_category;
        }
    } catch (error) {
        showMessage('qbittorrentMessage', '❌ Erreur lors du chargement de la configuration', 'error');
    }
}

async function loadQbittorrentCategories() {
    try {
        const response = await fetch('/api/qbittorrent/categories_and_tags');
        const data = await response.json();
        
        if (data.success && data.categories.length > 0) {
            const select = document.getElementById('qbittorrentDefaultCategory');
            const currentValue = select.value;
            
            // Ajouter les catégories
            const optionsHtml = data.categories
                .map(cat => `<option value="${cat}">${cat}</option>`)
                .join('');
            
            select.innerHTML = `<option value="">-- Aucune catégorie --</option>` + optionsHtml;
            select.value = currentValue;
        }
    } catch (error) {
        console.warn('Erreur lors du chargement des catégories:', error);
    }
}

async function saveQbittorrentSettings() {
    const config = {
        enabled: document.getElementById('qbittorrentEnabled').checked,
        url: document.getElementById('qbittorrentUrl').value.trim(),
        port: parseInt(document.getElementById('qbittorrentPort').value),
        username: document.getElementById('qbittorrentUsername').value.trim(),
        password: document.getElementById('qbittorrentPassword').value,
        default_category: document.getElementById('qbittorrentDefaultCategory').value.trim()
    };

    try {
        const response = await fetch('/api/qbittorrent/config', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(config)
        });
        const data = await response.json();
        
        if (data.success) {
            showMessage('qbittorrentMessage', '✅ Configuration qBittorrent enregistrée !', 'success');
        } else {
            showMessage('qbittorrentMessage', '❌ Erreur: ' + data.error, 'error');
        }
    } catch (error) {
        showMessage('qbittorrentMessage', '❌ Erreur de connexion: ' + error.message, 'error');
    }
}

async function testQbittorrentConnection() {
    showMessage('qbittorrentMessage', '⏳ Test de connexion en cours...', 'info');
    
    // Préparer la configuration du formulaire
    const config = {
        enabled: true,  // Forcer enabled à true pour le test
        url: document.getElementById('qbittorrentUrl').value.trim(),
        port: parseInt(document.getElementById('qbittorrentPort').value),
        username: document.getElementById('qbittorrentUsername').value.trim(),
        password_decrypted: document.getElementById('qbittorrentPassword').value  // Texte clair du formulaire
    };
    
    // Vérifier que l'URL est remplie
    if (!config.url) {
        showMessage('qbittorrentMessage', '❌ Veuillez entrer une URL', 'error');
        return;
    }
    
    try {
        const response = await fetch('/api/qbittorrent/test', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(config)
        });
        const data = await response.json();
        if (data.success) {
            showMessage('qbittorrentMessage', '✅ ' + data.message, 'success');
            // Charger les catégories disponibles après un test réussi
            setTimeout(() => loadQbittorrentCategories(), 500);
        } else {
            showMessage('qbittorrentMessage', '❌ Échec: ' + data.error, 'error');
        }
    } catch (error) {
        showMessage('qbittorrentMessage', '❌ Erreur: ' + error.message, 'error');
    }
}

function resetQbittorrentSettings() {
    if (!confirm('Voulez-vous réinitialiser la configuration qBittorrent ?')) return;
    document.getElementById('qbittorrentEnabled').checked = false;
    document.getElementById('qbittorrentUrl').value = 'http://localhost';
    document.getElementById('qbittorrentPort').value = '8080';
    document.getElementById('qbittorrentUsername').value = '';
    document.getElementById('qbittorrentPassword').value = '';
    showMessage('qbittorrentMessage', '🔄 Configuration réinitialisée', 'info');
}

function toggleQbittorrentPassword() {
    const passwordInput = document.getElementById('qbittorrentPassword');
    const toggleButton = passwordInput.closest('.password-input-group').querySelector('.btn-toggle-password');
    qbittorrentPasswordVisible = !qbittorrentPasswordVisible;
    passwordInput.type = qbittorrentPasswordVisible ? 'text' : 'password';
    toggleButton.innerHTML = qbittorrentPasswordVisible ? `${svgIcon('eye-off')} Masquer` : `${svgIcon('eye')} Afficher`;
}

// ===== RTORRENT =====
let rtorrentPasswordVisible = false;

async function loadRtorrentSettings() {
    try {
        const response = await fetch('/api/rtorrent/config');
        const config = await response.json();

        document.getElementById('rtorrentEnabled').checked = config.enabled;
        document.getElementById('rtorrentUrl').value = config.url || '';
        document.getElementById('rtorrentPort').value = config.port || '';
        document.getElementById('rtorrentRpcPath').value = config.rpc_path || '/RPC2';
        document.getElementById('rtorrentUsername').value = config.username || '';

        // Voir le commentaire équivalent dans loadSettings (aMule) - même correctif
        if (config.password) {
            document.getElementById('rtorrentPassword').value = config.password;
        }
    } catch (error) {
        showMessage('rtorrentMessage', '❌ Erreur lors du chargement de la configuration', 'error');
    }
}

async function saveRtorrentSettings() {
    const config = {
        enabled: document.getElementById('rtorrentEnabled').checked,
        url: document.getElementById('rtorrentUrl').value.trim(),
        port: parseInt(document.getElementById('rtorrentPort').value),
        rpc_path: document.getElementById('rtorrentRpcPath').value.trim() || '/RPC2',
        username: document.getElementById('rtorrentUsername').value.trim(),
        password: document.getElementById('rtorrentPassword').value
    };

    if (config.enabled && !config.url) {
        showMessage('rtorrentMessage', '⚠️ Veuillez entrer l\'URL du serveur rTorrent', 'warning');
        return;
    }

    try {
        const response = await fetch('/api/rtorrent/config', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(config)
        });
        const data = await response.json();

        if (data.success) {
            showMessage('rtorrentMessage', '✅ Configuration rTorrent enregistrée !', 'success');
        } else {
            showMessage('rtorrentMessage', '❌ Erreur: ' + data.error, 'error');
        }
    } catch (error) {
        showMessage('rtorrentMessage', '❌ Erreur de connexion: ' + error.message, 'error');
    }
}

async function testRtorrentConnection() {
    showMessage('rtorrentMessage', '⏳ Test de connexion en cours...', 'info');

    const config = {
        enabled: true,
        url: document.getElementById('rtorrentUrl').value.trim(),
        port: parseInt(document.getElementById('rtorrentPort').value),
        rpc_path: document.getElementById('rtorrentRpcPath').value.trim() || '/RPC2',
        username: document.getElementById('rtorrentUsername').value.trim(),
        password_decrypted: document.getElementById('rtorrentPassword').value
    };

    if (!config.url) {
        showMessage('rtorrentMessage', '❌ Veuillez entrer une URL', 'error');
        return;
    }

    try {
        const response = await fetch('/api/rtorrent/test', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(config)
        });
        const data = await response.json();
        if (data.success) {
            showMessage('rtorrentMessage', '✅ ' + data.message, 'success');
        } else {
            showMessage('rtorrentMessage', '❌ Échec: ' + data.error, 'error');
        }
    } catch (error) {
        showMessage('rtorrentMessage', '❌ Erreur: ' + error.message, 'error');
    }
}

function toggleRtorrentPassword() {
    const passwordInput = document.getElementById('rtorrentPassword');
    const toggleButton = passwordInput.closest('.password-input-group').querySelector('.btn-toggle-password');
    rtorrentPasswordVisible = !rtorrentPasswordVisible;
    passwordInput.type = rtorrentPasswordVisible ? 'text' : 'password';
    toggleButton.innerHTML = rtorrentPasswordVisible ? `${svgIcon('eye-off')} Masquer` : `${svgIcon('eye')} Afficher`;
}

// ===== DELUGE =====
let delugePasswordVisible = false;

async function loadDelugeSettings() {
    try {
        const response = await fetch('/api/deluge/config');
        const config = await response.json();

        document.getElementById('delugeEnabled').checked = config.enabled;
        document.getElementById('delugeUrl').value = config.url || '';
        document.getElementById('delugePort').value = config.port || '';

        // Voir le commentaire équivalent dans loadSettings (aMule) - même correctif
        if (config.password) {
            document.getElementById('delugePassword').value = config.password;
        }
    } catch (error) {
        showMessage('delugeMessage', '❌ Erreur lors du chargement de la configuration', 'error');
    }
}

async function saveDelugeSettings() {
    const config = {
        enabled: document.getElementById('delugeEnabled').checked,
        url: document.getElementById('delugeUrl').value.trim(),
        port: parseInt(document.getElementById('delugePort').value),
        password: document.getElementById('delugePassword').value
    };

    if (config.enabled && !config.url) {
        showMessage('delugeMessage', '⚠️ Veuillez entrer l\'URL du serveur Deluge', 'warning');
        return;
    }

    if (config.enabled && !config.password) {
        showMessage('delugeMessage', '⚠️ Veuillez entrer le mot de passe de la Web UI Deluge', 'warning');
        return;
    }

    try {
        const response = await fetch('/api/deluge/config', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(config)
        });
        const data = await response.json();

        if (data.success) {
            showMessage('delugeMessage', '✅ Configuration Deluge enregistrée !', 'success');
        } else {
            showMessage('delugeMessage', '❌ Erreur: ' + data.error, 'error');
        }
    } catch (error) {
        showMessage('delugeMessage', '❌ Erreur de connexion: ' + error.message, 'error');
    }
}

async function testDelugeConnection() {
    showMessage('delugeMessage', '⏳ Test de connexion en cours...', 'info');

    const config = {
        enabled: true,
        url: document.getElementById('delugeUrl').value.trim(),
        port: parseInt(document.getElementById('delugePort').value),
        password_decrypted: document.getElementById('delugePassword').value
    };

    if (!config.url) {
        showMessage('delugeMessage', '❌ Veuillez entrer une URL', 'error');
        return;
    }

    try {
        const response = await fetch('/api/deluge/test', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(config)
        });
        const data = await response.json();
        if (data.success) {
            showMessage('delugeMessage', '✅ ' + data.message, 'success');
        } else {
            showMessage('delugeMessage', '❌ Échec: ' + data.error, 'error');
        }
    } catch (error) {
        showMessage('delugeMessage', '❌ Erreur: ' + error.message, 'error');
    }
}

function toggleDelugePassword() {
    const passwordInput = document.getElementById('delugePassword');
    const toggleButton = passwordInput.closest('.password-input-group').querySelector('.btn-toggle-password');
    delugePasswordVisible = !delugePasswordVisible;
    passwordInput.type = delugePasswordVisible ? 'text' : 'password';
    toggleButton.innerHTML = delugePasswordVisible ? `${svgIcon('eye-off')} Masquer` : `${svgIcon('eye')} Afficher`;
}

// ===== MESSAGES =====
function showMessage(elementId, text, type) {
    try {
        const msg = document.getElementById(elementId);
        if (msg) {
            msg.textContent = text;
            msg.className = 'message ' + type;
            msg.style.display = 'block';
            setTimeout(() => { msg.style.display = 'none'; }, 6000);
        }
    } catch (error) {
        console.error('Erreur showMessage:', error);
    }
}

// ===== IMPORT AUTOMATIQUE =====
// Anciennement une carte sur la page Import elle-même ("⚙️ Configuration de l'Import
// Automatique") - de la config, pas une action liée au scan en cours sur cette page,
// donc regroupée ici avec le reste de la configuration ("met le dans les settings").
// Le bouton "Exécuter maintenant" d'origine (scan + auto-assignation + import en
// aveugle, sans revoir les fichiers) n'a pas été repris: la page Import scanne et
// auto-assigne déjà automatiquement à l'ouverture, donc "📥 Aller à l'import"
// ci-dessus offre la même action réelle avec en plus une revue visuelle avant import.
// The watched-file extension filter remains a backend safety default; it is intentionally not user-editable here.

// "je ne veux pas avoir epub etre download. ajoute une section pour desactiver les
// extensions qui peuvent etre affiche et download" - liste des extensions non-BD déjà
// identifiées côté serveur (voir _NON_COMIC_EXTENSIONS_RE, bedetheque/auto_acquire.py),
// désormais réglable au lieu de figée dans le code, plus les formats vidéo/audio
// ("ajoute aussi les formats video (mkv, mp4) and audio mp3") qu'un résultat de
// recherche BD peut occasionnellement renvoyer (scan/release mal étiqueté). EPUB coché
// par défaut (voir blocked_search_extensions, config.py) - mkv/mp4/mp3 pas activés par
// défaut, seulement proposés (jamais demandé pour ceux-là).
const BLOCKED_EXTENSION_CHOICES = ['epub', 'mobi', 'azw', 'azw3', 'djvu', 'txt', 'mkv', 'mp4', 'mp3'];

// Draft en mémoire, même mécanique que searchFormatPriorityDraft juste au-dessus
// (édité au clic, écrit en base seulement à "Sauvegarder") - "ne met pas des checkbox
// mais des tags": chaque extension est un tag cliquable (actif = bloqué) plutôt qu'une
// case à cocher, voir renderBlockedSearchExtensionsList/toggleBlockedExtensionTag.
let blockedSearchExtensionsDraft = new Set();

// "ajoute une option pour ajouter une extension manuelement" - une extension déjà
// bloquée mais absente des choix proposés (ajoutée manuellement, ou reprise d'un
// réglage sauvegardé précédemment) doit quand même apparaître comme tag : union plutôt
// que BLOCKED_EXTENSION_CHOICES seul. Décocher un tag qui n'est PAS dans les choix
// proposés le fait donc disparaître entièrement (il n'existait à l'écran que parce
// qu'il était dans le Set) - pas besoin d'un bouton "supprimer" séparé.
function renderBlockedSearchExtensionsList() {
    const blockedList = document.getElementById('blockedSearchExtensionsList');
    if (!blockedList) return;
    const allExtensions = [...new Set([...BLOCKED_EXTENSION_CHOICES, ...blockedSearchExtensionsDraft])];
    blockedList.innerHTML = allExtensions.map(ext => `
        <button type="button" class="ext-tag${blockedSearchExtensionsDraft.has(ext) ? ' active' : ''}"
                aria-pressed="${blockedSearchExtensionsDraft.has(ext)}"
                onclick="toggleBlockedExtensionTag('${ext}')">.${ext}</button>
    `).join('');
}

function toggleBlockedExtensionTag(ext) {
    if (blockedSearchExtensionsDraft.has(ext)) blockedSearchExtensionsDraft.delete(ext);
    else blockedSearchExtensionsDraft.add(ext);
    renderBlockedSearchExtensionsList();
}

function addCustomBlockedExtension() {
    const input = document.getElementById('blockedExtensionCustomInput');
    if (!input) return;
    // Sans le point ("cbr7", pas ".cbr7") pour rester cohérent avec BLOCKED_EXTENSION_CHOICES
    // (voir _blocked_extension_pattern côté serveur, missing_monitor/searcher.py, qui
    // travaille aussi sur l'extension nue) - un point tapé par erreur est toléré et retiré.
    const ext = input.value.trim().toLowerCase().replace(/^\.+/, '');
    if (!ext || !/^[a-z0-9]+$/.test(ext)) {
        showToast('blocked-extension-invalid', 'Extension invalide (lettres/chiffres uniquement, sans le point)', { icon: 'alert-triangle', autoHideMs: 4000 });
        return;
    }
    blockedSearchExtensionsDraft.add(ext);
    input.value = '';
    renderBlockedSearchExtensionsList();
}

async function loadAutoImportConfig() {
    try {
        const response = await fetch('/api/import/config');
        const config = await response.json();

        document.getElementById('auto-import-enabled').checked = config.auto_import_enabled;
        document.getElementById('auto-assign-enabled').checked = config.auto_assign_enabled;
        document.getElementById('auto-convert-to-cbz').checked = config.auto_convert_to_cbz !== false;
        document.getElementById('import-mode').value = config.import_mode === 'hardlink' ? 'hardlink' : 'move';


        const packSearchEnabled = document.getElementById('auto-acquire-pack-search-enabled');
        if (packSearchEnabled) packSearchEnabled.checked = !!config.auto_acquire_pack_search_enabled;

        const acquireOnAddEnabled = document.getElementById('auto-acquire-on-add-enabled');
        if (acquireOnAddEnabled) acquireOnAddEnabled.checked = !!config.auto_acquire_on_add_enabled;

        blockedSearchExtensionsDraft = new Set((config.blocked_search_extensions || ['epub']).map(e => e.toLowerCase()));
        renderBlockedSearchExtensionsList();
    } catch (error) {
        console.error('Erreur lors du chargement de la configuration import auto:', error);
        showMessage('autoImportMessage', 'Erreur lors du chargement', 'error');
    }
}

async function saveBlockedSearchExtensions() {
    try {
        const blockedExtensions = Array.from(blockedSearchExtensionsDraft);
        const response = await fetch('/api/import/config', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ blocked_search_extensions: blockedExtensions })
        });
        const result = await response.json();
        if (response.ok && result.success) {
            showToast('blocked-extensions-saved', '✅ Extensions désactivées enregistrées !', { icon: 'check', autoHideMs: 3000 });
        } else {
            showToast('blocked-extensions-error', result.error || 'Erreur lors de la sauvegarde', { icon: 'alert-triangle', autoHideMs: 4000 });
        }
    } catch (error) {
        console.error('Erreur lors de la sauvegarde des extensions désactivées:', error);
        showToast('blocked-extensions-error', 'Erreur lors de la sauvegarde', { icon: 'alert-triangle', autoHideMs: 4000 });
    }
}

async function saveAutoImportConfig() {
    try {
        const config = {
            auto_import_enabled: document.getElementById('auto-import-enabled').checked,
            auto_assign_enabled: document.getElementById('auto-assign-enabled').checked,
            auto_convert_to_cbz: document.getElementById('auto-convert-to-cbz').checked,
            import_mode: document.getElementById('import-mode').value,
            auto_acquire_pack_search_enabled: document.getElementById('auto-acquire-pack-search-enabled')?.checked || false,
            auto_acquire_on_add_enabled: document.getElementById('auto-acquire-on-add-enabled')?.checked || false
        };

        const response = await fetch('/api/import/config', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(config)
        });

        const result = await response.json();
        if (response.ok && result.success) {
            showMessage('autoImportMessage', 'Configuration sauvegardée' + (config.auto_import_enabled ? ' - import automatique activé ✓' : ''), 'success');
        } else {
            showMessage('autoImportMessage', 'Erreur: ' + (result.error || 'Erreur inconnue'), 'error');
        }
    } catch (error) {
        console.error('Erreur lors de la sauvegarde de la configuration import auto:', error);
        showMessage('autoImportMessage', 'Erreur lors de la sauvegarde', 'error');
    }
}

// ===== EBDZ AUTO SCRAPE =====
async function loadAutoScrapeConfig() {
    try {
        const response = await fetch('/api/ebdz/auto-scrape/config');
        const config = await response.json();

        document.getElementById('ebdzAutoScrapeEnabled').checked = config.auto_scrape_enabled;
        document.getElementById('ebdzAutoScrapeInterval').value = config.auto_scrape_interval;
        document.getElementById('ebdzAutoScrapeUnit').value = config.auto_scrape_interval_unit;

        updateAutoScrapeUI();
        checkAutoScrapeStatus();
    } catch (error) {
        console.error('Erreur lors du chargement de la config auto scrape:', error);
    }
}

function updateAutoScrapeUI() {
    const enabled = document.getElementById('ebdzAutoScrapeEnabled').checked;
    const controlsDiv = document.getElementById('autoScrapeControlsDiv');
    const statusDiv = document.getElementById('autoScrapeStatusDiv');

    if (enabled) {
        controlsDiv.style.display = 'flex';
        statusDiv.style.display = 'block';
    } else {
        controlsDiv.style.display = 'none';
        statusDiv.style.display = 'none';
    }
}

async function saveAutoScrapeConfig() {
    try {
        const enabled = document.getElementById('ebdzAutoScrapeEnabled').checked;
        const interval = parseInt(document.getElementById('ebdzAutoScrapeInterval').value);
        const unit = document.getElementById('ebdzAutoScrapeUnit').value;

        if (enabled && interval < 1) {
            showMessage('ebdzAutoMessage', '⚠️ L\'intervalle doit être >= 1', 'warning');
            return;
        }

        const response = await fetch('/api/ebdz/auto-scrape/config', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                auto_scrape_enabled: enabled,
                auto_scrape_interval: interval,
                auto_scrape_interval_unit: unit
            })
        });
        const data = await response.json();

        if (data.success) {
            showMessage('ebdzAutoMessage', 
                enabled ? `✅ Scraping automatique activé: tous les ${interval} ${unit}` : '✅ Scraping automatique désactivé', 
                'success');
            updateAutoScrapeUI();
            checkAutoScrapeStatus();
        } else {
            showMessage('ebdzAutoMessage', '❌ Erreur: ' + data.error, 'error');
        }
    } catch (error) {
        showMessage('ebdzAutoMessage', '❌ Erreur: ' + error.message, 'error');
    }
}

async function checkAutoScrapeStatus() {
    try {
        const response = await fetch('/api/ebdz/auto-scrape/status');
        const data = await response.json();

        if (data.success) {
            const statusText = document.getElementById('autoScrapeStatusText');
            const nextRunText = document.getElementById('autoScrapeNextRunText');

            if (data.is_running) {
                statusText.textContent = '🟢 Actif';
                statusText.style.color = '#4CAF50';
                
                if (data.next_run) {
                    const nextRun = new Date(data.next_run);
                    nextRunText.textContent = nextRun.toLocaleString('fr-FR', { timeZone: BULLARR_DISPLAY_TZ });
                } else {
                    nextRunText.textContent = 'Calcul en cours...';
                }
            } else {
                statusText.textContent = '🔴 Inactif';
                statusText.style.color = '#f44336';
                nextRunText.textContent = '-';
            }
        }
    } catch (error) {
        console.error('Erreur lors du vérification du statut:', error);
    }
}

// Ajouter l'event listener pour le checkbox auto scrape
document.addEventListener('DOMContentLoaded', function() {
    const checkbox = document.getElementById('ebdzAutoScrapeEnabled');
    if (checkbox) {
        checkbox.addEventListener('change', updateAutoScrapeUI);
    }
});

// ===== BÉDÉTHÈQUE: index local du catalogue (voir catalog_index.py) =====
// "apres ce qu'on pourrait faire c'est telecharger deja en db toutes l'index des series
// et chercher prendrait tres peu de temsp" - construction manuelle uniquement (~27
// requêtes, de l'ordre de la minute), pollée pendant qu'elle tourne pour refléter la
// progression (voir _build_progress côté catalog_index.py) plutôt qu'un simple
// "en cours..." figé.
let _bedethequeCatalogIndexPollTimer = null;

function _formatBedethequeCatalogIndexStatus(data) {
    if (data.running) {
        const p = data.progress || {};
        return `<p style="margin:0;"><strong>Construction en cours...</strong> ${p.done ?? 0}/${p.total ?? 27} (${p.current || ''})</p>`;
    }
    if (data.progress && data.progress.error) {
        return `<p style="margin:0; color:#dc3545;"><strong>Échec de la dernière construction:</strong> ${escapeHtml(data.progress.error)}</p>`;
    }
    if (!data.built) {
        return `<p style="margin:0;">Index pas encore construit - toute recherche/matching Bédéthèque retombe sur la recherche live habituelle.</p>`;
    }
    return `<p style="margin:0;"><strong>${data.count}</strong> séries indexées - dernière construction: ${escapeHtml(data.built_at || '?')}</p>`;
}

async function loadBedethequeCatalogIndexStatus() {
    const el = document.getElementById('bedethequeCatalogIndexStatus');
    const btn = document.getElementById('bedethequeCatalogIndexBuildBtn');
    if (!el) return;
    try {
        const data = await (await fetch('/api/bedetheque/catalog-index/status')).json();
        el.innerHTML = _formatBedethequeCatalogIndexStatus(data);
        if (btn) btn.disabled = !!data.running;
        // "construire l'index s'il est deja construit met mis à jour de l'update" -
        // le libellé reflète l'état réel (rien à "construire" une fois que ça existe déjà).
        const btnLabel = document.getElementById('bedethequeCatalogIndexBuildBtnLabel');
        if (btnLabel) btnLabel.textContent = data.built ? "Mettre à jour l'index" : "Construire l'index";
        if (data.running) {
            if (!_bedethequeCatalogIndexPollTimer) {
                _bedethequeCatalogIndexPollTimer = setInterval(loadBedethequeCatalogIndexStatus, 3000);
            }
        } else if (_bedethequeCatalogIndexPollTimer) {
            clearInterval(_bedethequeCatalogIndexPollTimer);
            _bedethequeCatalogIndexPollTimer = null;
        }
    } catch (error) {
        el.innerHTML = '<p style="margin:0; color:#dc3545;">Erreur de chargement du statut</p>';
    }
}

async function buildBedethequeCatalogIndex() {
    const btn = document.getElementById('bedethequeCatalogIndexBuildBtn');
    if (btn) btn.disabled = true;
    try {
        const response = await fetch('/api/bedetheque/catalog-index/build', { method: 'POST' });
        const data = await response.json();
        if (!data.success) {
            alert('❌ ' + (data.error || 'Impossible de démarrer la construction'));
            if (btn) btn.disabled = false;
            return;
        }
        await loadBedethequeCatalogIndexStatus();
    } catch (error) {
        alert('❌ Erreur: ' + error.message);
        if (btn) btn.disabled = false;
    }
}

// ===== INIT =====
window.addEventListener('load', () => {
    // Vérifier si une tab est spécifiée dans l'URL (ex: #monitoring)
    if (window.location.hash) {
        const tabName = window.location.hash.substring(1); // Enlever le '#'
        switchTab(tabName);
    }

    try {
        loadSettings();
    } catch (e) {
        console.error('Erreur loadSettings:', e);
    }

    try {
        loadBedethequeCatalogIndexStatus();
    } catch (e) {
        console.error('Erreur loadBedethequeCatalogIndexStatus:', e);
    }
});

// Gérer les changements de hash (navigation sans rechargement)
window.addEventListener('hashchange', () => {
    if (window.location.hash) {
        const tabName = window.location.hash.substring(1);
        switchTab(tabName);
    }
});

// ===== SURVEILLANCE =====

async function loadMonitoringConfig() {
    try {
        const response = await fetch('/api/missing-monitor/config');
        const config = await response.json();
        
        // Configuration des volumes manquants
        const missingConfig = config.monitor_missing_volumes || {};

        const settingsMonitorMissing = document.getElementById('settingsMonitorMissing');
        const settingsMissingAction = document.getElementById('settingsMissingAction');

        if (settingsMonitorMissing) settingsMonitorMissing.checked = missingConfig.enabled !== false;
        if (settingsMissingAction) settingsMissingAction.value = missingConfig.action || 'download';

        // Configuration de l'upgrade qualité (tomes déjà possédés)
        const qualityConfig = config.quality_upgrade || {};

        const settingsQualityUpgrade = document.getElementById('settingsQualityUpgrade');
        const settingsQualityMinIncrease = document.getElementById('settingsQualityMinIncrease');

        if (settingsQualityUpgrade) settingsQualityUpgrade.checked = qualityConfig.enabled === true;
        if (settingsQualityMinIncrease) settingsQualityMinIncrease.value = qualityConfig.min_size_increase_percent ?? 20;

    } catch (error) {
        console.error('Erreur chargement config surveillance:', error);
    }
}

async function saveMonitoringConfig() {
    try {
        // Vérifier que tous les éléments existent avant d'accéder à leurs valeurs
        const settingsMonitorMissing = document.getElementById('settingsMonitorMissing');
        const settingsMissingAction = document.getElementById('settingsMissingAction');

        if (!settingsMonitorMissing || !settingsMissingAction) {
            console.error('Éléments manquants pour la section volumes manquants');
            return;
        }

        // Sources de recherche: plus de sélection dédiée ici (redondante avec l'ordre des
        // sources de l'onglet Recherche) - EBDZ+Prowlarr systématiquement, comme avant
        // quand les deux cases étaient cochées par défaut.

        const settingsQualityUpgrade = document.getElementById('settingsQualityUpgrade');
        const settingsQualityMinIncrease = document.getElementById('settingsQualityMinIncrease');

        const config = {
            monitor_missing_volumes: {
                enabled: settingsMonitorMissing.checked,
                action: settingsMissingAction.value
            },
            quality_upgrade: {
                enabled: settingsQualityUpgrade ? settingsQualityUpgrade.checked : false,
                min_size_increase_percent: settingsQualityMinIncrease ? parseInt(settingsQualityMinIncrease.value, 10) || 20 : 20
            }
        };

        const response = await fetch('/api/missing-monitor/config', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(config)
        });

        const data = await response.json();

        if (data.success) {
            showSettingsMessage('monitoringMessage', '✅ Configuration de surveillance sauvegardée avec succès !', 'success');
        } else {
            showSettingsMessage('monitoringMessage', '❌ Erreur: ' + data.error, 'error');
        }
    } catch (error) {
        console.error('Erreur sauvegarde config surveillance:', error);
        showSettingsMessage('monitoringMessage', '❌ Erreur de connexion', 'error');
    }
}

function showSettingsMessage(elementId, message, type) {
    const element = document.getElementById(elementId);
    if (element) {
        element.className = 'message ' + type;
        element.textContent = message;
        element.style.display = 'block';
        
        // Masquer après 5 secondes
        if (type === 'success') {
            setTimeout(() => {
                element.style.display = 'none';
            }, 5000);
        }
    }
}


// ===== UTILITAIRES PARTAGÉS (pas d'autre script chargé sur cette page qui les fournisse) =====
function escapeHtml(text) {
    if (!text) return '';
    const map = {
        '&': '&amp;',
        '<': '&lt;',
        '>': '&gt;',
        '"': '&quot;',
        "'": '&#039;'
    };
    return String(text).replace(/[&<>"']/g, m => map[m]);
}

// Pour un argument de handler onclick="fn('...')" - voir la convention documentée dans
// CLAUDE.md (escapeForAttribute pour un littéral JS entre guillemets simples dans un
// onclick, jamais pour un attribut HTML classique comme value=/title=)
function escapeForAttribute(text) {
    return String(text).replace(/'/g, "\\'").replace(/"/g, '&quot;');
}

// ===== BACKUP / RESTAURATION =====
async function restoreBackup() {
    const fileInput = document.getElementById('backupRestoreFile');
    const messageEl = document.getElementById('backupRestoreMessage');
    const file = fileInput.files[0];

    if (!file) {
        messageEl.innerHTML = '<span style="color:#dc2626;">⚠️ Sélectionne un fichier .zip</span>';
        return;
    }

    if (!confirm("Ceci va écraser les bases de données et fichiers de configuration actuels (une copie .before_restore de chaque fichier remplacé sera gardée). Continuer ?")) {
        return;
    }

    messageEl.innerHTML = '⏳ Restauration en cours...';

    try {
        const formData = new FormData();
        formData.append('file', file);

        const response = await fetch('/api/settings/backup/restore', {
            method: 'POST',
            body: formData
        });
        const data = await response.json();

        if (data.success) {
            messageEl.innerHTML = `<span style="color:#16a34a;">✅ ${escapeHtml(data.message)}</span><br><span class="help-text">Fichiers restaurés: ${data.restored.map(escapeHtml).join(', ')}</span>`;
        } else {
            messageEl.innerHTML = `<span style="color:#dc2626;">❌ ${escapeHtml(data.error || 'Erreur inconnue')}</span>`;
        }
    } catch (error) {
        messageEl.innerHTML = `<span style="color:#dc2626;">❌ Erreur de connexion: ${escapeHtml(error.message)}</span>`;
    }
}
