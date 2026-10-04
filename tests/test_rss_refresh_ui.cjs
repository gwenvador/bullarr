const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('static/js/ebdz-latest.js', 'utf8');
const batch = source.slice(source.indexOf('async function _loadNouveautesBatch('), source.indexOf("window.addEventListener('download-clients-ready'"));
function harness(rssResponse, fail = false) {
    const urls = [], warnings = [];
    const ctx = {
        nouveautesLoadGeneration: 1, nouveautesDaysWindow: 7,
        allNouveautesEvents: [{type: 'rss', title: 'Old', date: '2026-09-01'}],
        fetch: async url => {
            urls.push(url);
            if (url.includes('/rss/')) {
                if (fail) throw Error('offline');
                return {ok: true, json: async () => rssResponse};
            }
            return {ok: true, json: async () => ({success: true, sessions: [], files: []})};
        },
        _mergeNouveautesClientState() {}, renderNouveautesEvents() {}, _saveNouveautesCache() {},
        showToast: (...args) => warnings.push(args), dismissToast() {},
        localStorage: {setItem() {}},
    };
    vm.createContext(ctx); vm.runInContext(batch, ctx);
    return {ctx, urls, warnings};
}
test('explicit refresh reaches RSS and replaces cached entries', async () => {
    const {ctx, urls} = harness({success: true, entries: [{title: 'Fresh', date: '2026-09-29'}], errors: []});
    await ctx._loadNouveautesBatch(100, 1, true, null, false, true);
    assert.ok(urls.some(url => url.includes('/rss/latest?limit=100&refresh=1')));
    assert.equal(ctx.allNouveautesEvents[0].title, 'Fresh');
});
test('network failure retains RSS rows and shows a warning', async () => {
    const {ctx, warnings} = harness(null, true);
    await ctx._loadNouveautesBatch(100, 1, true);
    assert.equal(ctx.allNouveautesEvents[0].title, 'Old');
    assert.equal(warnings.length, 1);
});
test('successful empty feed clears obsolete RSS entries', async () => {
    const {ctx} = harness({success: true, entries: [], errors: []});
    await ctx._loadNouveautesBatch(100, 1, true);
    assert.equal(ctx.allNouveautesEvents.length, 0);
});
test('stale provider data is shown with a warning', async () => {
    const {ctx, warnings} = harness({success: true, entries: [{title: 'Cached'}], errors: [{name: 'Demo'}]});
    await ctx._loadNouveautesBatch(100, 1, true);
    assert.equal(ctx.allNouveautesEvents[0].title, 'Cached');
    assert.equal(warnings.length, 1);
});

test('background refresh keeps the visible rows until every source has answered', async () => {
    const pending = {};
    const rendered = [];
    const original = [
        {type: 'ebdz', title: 'Old EBDZ', date: '2026-10-01T12:00:00Z'},
        {type: 'telegram', title: 'Old Telegram', date: '2026-10-01T11:00:00Z'},
        {type: 'rss', title: 'Old RSS', date: '2026-10-01T10:00:00Z'},
    ];
    const ctx = {
        nouveautesLoadGeneration: 1, nouveautesDaysWindow: 7,
        allNouveautesEvents: original,
        fetch: url => new Promise(resolve => {
            const source = url.includes('/rss/') ? 'rss' : url.includes('/telegram-') ? 'telegram' : 'ebdz';
            pending[source] = resolve;
        }),
        _mergeNouveautesClientState() {},
        renderNouveautesEvents: () => rendered.push(ctx.allNouveautesEvents.map(event => event.title || event.filename)),
        _saveNouveautesCache() {}, showToast() {}, dismissToast() {},
        localStorage: {setItem() {}},
    };
    vm.createContext(ctx); vm.runInContext(batch, ctx);

    const refresh = ctx._loadNouveautesBatch(null, 1, false);
    pending.ebdz({json: async () => ({success: true, sessions: [{scraped_at: '2026-10-02T12:00:00Z', results: [{title: 'New EBDZ'}]}]})});
    await new Promise(setImmediate);
    assert.equal(ctx.allNouveautesEvents, original);
    assert.equal(rendered.length, 0);

    pending.telegram({json: async () => ({success: true, files: [{filename: 'New Telegram', message_date: '2026-10-02T11:00:00Z'}]})});
    await new Promise(setImmediate);
    assert.equal(ctx.allNouveautesEvents, original);
    assert.equal(rendered.length, 0);

    pending.rss({ok: true, json: async () => ({success: true, entries: [{title: 'New RSS', date: '2026-10-02T10:00:00Z'}]})});
    await refresh;
    assert.equal(rendered.length, 1);
    assert.deepEqual(Array.from(rendered[0]), ['New EBDZ', 'New Telegram', 'New RSS']);
});
