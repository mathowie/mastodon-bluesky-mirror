const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

class Element {
  constructor() { this.children = []; this.dataset = {}; this.textContent = ''; this.disabled = false; }
  appendChild(child) { this.children.push(child); return child; }
  replaceChildren(...children) { this.children = children; }
  addEventListener(name, fn) { this[name] = fn; }
}
const elements = new Map(['notice', 'image-status', 'stats', 'history'].map(id => [id, new Element()]));
let queued = false;
let rejectRetry = true;
let imagesReady = false;
const context = vm.createContext({
  document: { getElementById: id => elements.get(id), createElement: () => new Element() },
  setInterval: () => 1, Date, Set, Error, Promise, encodeURIComponent,
  fetch: async (url, options = {}) => {
    if (options.method === 'POST') {
      if (rejectRetry) return {ok: false, status: 409, json: async () => ({detail: 'Already mirrored'})};
      queued = true;
      return {ok: true, status: 202, json: async () => ({message: 'Queued for retry; completed parts will be reused'})};
    }
    if (url.endsWith('/stats')) return {ok: true, status: 200, json: async () => ({total_synced: 0, failed: queued ? 0 : 1, queued: queued ? 1 : 0, pending: 0, success_rate: '0.0%', image_processing: {ready: imagesReady, error: imagesReady ? null : 'Rebuild and recreate the container'}})};
    return {ok: true, status: 200, json: async () => [{mastodon_id: '100000000000000004', status: queued ? 'queued' : 'failed', parts_total: 2, parts_published: 1, synced_at: '2026-10-30T06:00:00Z', error: '<img src=x onerror=alert(1)>'}]};
  }
});
const html = fs.readFileSync(path.join(__dirname, '../app/web/static/index.html'), 'utf8');
const source = html.split('<script>')[1].split('</script>')[0];
vm.runInContext(source, context);
(async () => {
  await new Promise(setImmediate);
  assert.match(elements.get('image-status').textContent, /Rebuild and recreate/);
  assert.equal(elements.get('image-status').dataset.error, 'true');
  imagesReady = true;
  await context.loadDashboard();
  assert.equal(elements.get('image-status').textContent, '');
  assert.equal(elements.get('image-status').dataset.error, 'false');
  const row = elements.get('history').children[0];
  assert.equal(row.children[2].textContent, '1/2 published');
  assert.equal(row.children[4].textContent, '<img src=x onerror=alert(1)>');
  assert.equal(row.children[4].children.length, 0);
  const button = row.children[5].children[0];
  await context.retry('100000000000000004', button);
  assert.equal(elements.get('notice').textContent, 'Already mirrored');
  assert.equal(elements.get('notice').dataset.error, 'true');
  assert.equal(button.disabled, false);
  rejectRetry = false;
  await context.retry('100000000000000004', button);
  assert.equal(elements.get('notice').dataset.error, 'false');
  assert.match(elements.get('notice').textContent, /Queued for retry/);
  assert.equal(elements.get('history').children[0].children[1].textContent, 'queued');
  assert.equal(elements.get('history').children[0].children[5].children.length, 0);
  process.stdout.write('Dashboard functional checks passed: image readiness, progress, safe error text, rejected retry, queued retry.\n');
})().catch(error => { console.error(error); process.exitCode = 1; });
