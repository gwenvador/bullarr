const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '..', 'static/js/nav.js'), 'utf8');
const end = source.indexOf('})();') + 5;
const calls = [];
const window = {
    location: { href: 'https://bullarr.example/settings', origin: 'https://bullarr.example' },
    fetch: async (input, options) => {
        calls.push({ input, options });
        return input === '/api/auth/csrf'
            ? { ok: true, json: async () => ({ token: 'session-token' }) }
            : { ok: true };
    },
};
vm.runInNewContext(source.slice(0, end), { window, URL, Request, Headers });

(async () => {
    await window.fetch('/api/settings/rename', { method: 'POST', body: '{}' });
    await window.fetch('/api/settings/rename', { method: 'POST', body: '{}' });
    assert.equal(calls.filter(call => call.input === '/api/auth/csrf').length, 1);
    assert.equal(calls[1].options.headers.get('X-CSRF-Token'), 'session-token');
    assert.equal(calls[2].options.headers.get('X-CSRF-Token'), 'session-token');
    console.log('CSRF fetch regression checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
