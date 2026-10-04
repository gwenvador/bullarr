const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.resolve(__dirname, '..');
const files = ['search.js', 'library.js', 'import.js', 'settings.js', 'verification.js', 'bedetheque-enrich.js'];
for (const name of files) {
    const source = fs.readFileSync(path.join(root, 'static/js', name), 'utf8');
    const match = source.match(/function escapeForAttribute\(text\) \{\s*(return [^\n]+;)\s*\}/);
    assert.ok(match, `${name} defines the attribute helper`);
    const escapeForAttribute = vm.runInNewContext(`(function escapeForAttribute(text) { ${match[1]} })`);
    assert.equal(escapeForAttribute('&#39;);alert(1);//'), '&amp;#39;);alert(1);//', name);
    assert.equal(escapeForAttribute("O'Neil"), "O\\'Neil", name);
}
console.log('Attribute escaping regression checks passed');
