const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');

class Element {
  constructor(tagName = '') {
    this.tagName = tagName.toUpperCase();
    this.children = []; this.dataset = {}; this.textContent = ''; this.disabled = false;
    this.value = ''; this.hidden = false;
  }
  appendChild(child) { this.children.push(child); return child; }
  replaceChildren(...children) { this.children = children; }
  addEventListener(name, fn) { this[name] = fn; }
  set innerHTML(value) { throw new Error('Dashboard must render server data as text'); }
}
const html = fs.readFileSync(path.join(__dirname, '../app/web/static/index.html'), 'utf8');
const source = html.split('<script>')[1].split('</script>')[0];
const ID = '100000000000000004';
const flush = () => new Promise(setImmediate);
const response = (body, status = 200) => ({ok: status < 400, status, json: async () => body});
const deferred = () => {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return {promise, resolve};
};

function stats(destination, connection = {}) {
  return {
    total_synced: destination === 'threads' ? 7 : 2, failed: 1, queued: 0, pending: 0,
    success_rate: '75.0%', image_processing: {ready: true, error: null},
    ...(destination === 'threads' ? {threads_connection: {
      enabled: true, configured: true, account_id: '900000000000001', username: 'example',
      expires_at: 1804000000, status: 'ready', error: null, ...connection,
    }} : {}),
  };
}

function entry(status = 'failed', id = ID) {
  return {mastodon_id: id, status, parts_total: 2, parts_published: 1,
    synced_at: '2026-10-30T06:00:00Z', error: '<img src=x onerror=alert(1)>'};
}

function dashboard(respond) {
  // Build only elements that actually exist in the page, including select defaults.
  const elements = new Map();
  for (const match of html.matchAll(/<([a-z][\w-]*)\b([^>]*\bid="([^"]+)"[^>]*)>/g)) {
    const element = new Element(match[1]);
    element.hidden = /\bhidden\b/.test(match[2]);
    if (match[1] === 'select') {
      const options = html.slice(match.index + match[0].length).split('</select>')[0];
      element.value = options.match(/<option\b[^>]*value="([^"]+)"/)[1];
    }
    elements.set(match[3], element);
  }
  const calls = [];
  const context = vm.createContext({
    document: {getElementById: id => elements.get(id), createElement: tag => new Element(tag)},
    setInterval: () => 1, Date, Set, Error, Promise, encodeURIComponent,
    fetch: async (url, options = {}) => {
      calls.push({url, options});
      return respond(new URL(url, 'http://dashboard.test'), options);
    },
  });
  vm.runInContext(source, context);
  return {
    elements, calls, context,
    async choose(value) {
      const select = elements.get('destination');
      assert.ok(select, 'The page needs a destination selector');
      assert.equal(select.tagName, 'SELECT');
      select.value = value;
      assert.equal(typeof select.change, 'function', 'Changing destination must refresh the dashboard');
      await select.change();
      await flush();
    },
  };
}

test('existing Bluesky image readiness, progress, safe error text and retry behavior', async () => {
  let queued = false;
  let rejectRetry = true;
  let imagesReady = false;
  const {elements, context, calls} = dashboard((url, options) => {
    if (options.method === 'POST') {
      if (rejectRetry) return response({detail: 'Already mirrored'}, 409);
      queued = true;
      return response({message: 'Queued for retry; completed parts will be reused'}, 202);
    }
    if (url.pathname.endsWith('/stats')) return response({total_synced: 0, failed: queued ? 0 : 1, queued: queued ? 1 : 0, pending: 0, success_rate: '0.0%', image_processing: {ready: imagesReady, error: imagesReady ? null : 'Rebuild and recreate the container'}});
    return response([entry(queued ? 'queued' : 'failed')]);
  });
  await flush();
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
  await context.retry(ID, button);
  assert.equal(elements.get('notice').textContent, 'Already mirrored');
  assert.equal(elements.get('notice').dataset.error, 'true');
  assert.equal(button.disabled, false);
  rejectRetry = false;
  await context.retry(ID, button);
  assert.equal(elements.get('notice').dataset.error, 'false');
  assert.match(elements.get('notice').textContent, /Queued for retry/);
  assert.equal(elements.get('history').children[0].children[1].textContent, 'queued');
  assert.equal(elements.get('history').children[0].children[5].children.length, 0);
  assert.ok(calls.every(call => !call.url.includes('?')), 'Default URLs must remain unchanged');
  assert.ok(calls.every(call => call.options.credentials === 'same-origin'));
});

test('Threads selection scopes progress and queued retry to its destination', async () => {
  let queued = false;
  const app = dashboard((url, options) => {
    const destination = url.searchParams.get('destination') || 'bluesky';
    if (options.method === 'POST') {
      assert.equal(destination, 'threads');
      queued = true;
      return response({message: 'Threads retry queued'}, 202);
    }
    if (url.pathname.endsWith('/stats')) return response(stats(destination));
    return response([entry(queued ? 'pending' : 'queued')]);
  });
  await flush();
  assert.equal(app.elements.get('destination')?.value, 'bluesky');
  await app.choose('threads');
  assert.match(app.elements.get('stats').textContent, /Synced: 7/);
  const connection = app.elements.get('threads-connection');
  assert.equal(connection.hidden, false);
  assert.match(connection.textContent, /example/);
  assert.match(connection.textContent, /900000000000001/);
  assert.match(connection.textContent, /expir/i);
  const row = app.elements.get('history').children[0];
  assert.equal(row.children.length, 6);
  assert.equal(row.children[2].textContent, '1/2 published');
  assert.equal(row.children[4].textContent, '<img src=x onerror=alert(1)>');
  const button = row.children[5].children[0];
  assert.ok(button, 'A queued Threads failure can request a retry');
  await button.click();
  assert.ok(app.calls.some(call => call.url === `/dashboard/retry/${ID}?destination=threads` && call.options.method === 'POST'));
  assert.equal(app.elements.get('notice').textContent, 'Threads retry queued');
  assert.equal(app.elements.get('history').children[0].children[1].textContent, 'pending');
  await app.choose('bluesky');
  assert.match(app.elements.get('stats').textContent, /Synced: 2/);
  assert.equal(connection.hidden, true);
  assert.equal(app.elements.get('notice').textContent, '');
});

test('Threads disabled state and unsafe job states do not expose retry controls', async () => {
  let enabled = false;
  const app = dashboard(url => {
    const destination = url.searchParams.get('destination') || 'bluesky';
    if (url.pathname.endsWith('/stats')) return response(stats(destination, {enabled}));
    return response(['failed', 'queued', 'needs_review', 'pending', 'success'].map((status, i) => entry(status, String(i + 1))));
  });
  await flush();
  await app.choose('threads');
  assert.match(app.elements.get('threads-connection').textContent, /disabled/i);
  assert.ok(app.elements.get('history').children.every(row => row.children[5].children.length === 0));
  enabled = true;
  await app.context.loadDashboard();
  const rows = app.elements.get('history').children;
  assert.equal(rows[0].children[5].children.length, 1);
  assert.equal(rows[1].children[5].children.length, 1);
  assert.ok(rows.slice(2).every(row => row.children[5].children.length === 0));
});

test('connection metadata and server errors stay literal text', async () => {
  const injection = '<img src=x onerror=alert(1)>';
  const app = dashboard(url => url.pathname.endsWith('/stats')
    ? response(stats('threads', {status: 'invalid', username: injection, error: injection}))
    : response([entry('needs_review')]));
  await flush();
  await app.choose('threads');
  const connection = app.elements.get('threads-connection');
  assert.ok(connection.textContent.includes(injection));
  assert.equal(connection.children.length, 0);
  assert.equal(connection.dataset.error, 'true');
  assert.equal(app.elements.get('history').children[0].children[4].children.length, 0);
});

for (const fails of [false, true]) {
  test(`a stale Bluesky ${fails ? 'error' : 'response'} cannot overwrite Threads selection`, async () => {
    const oldStats = deferred();
    const oldHistory = deferred();
    const app = dashboard(url => {
      if (!url.searchParams.has('destination')) return url.pathname.endsWith('/stats') ? oldStats.promise : oldHistory.promise;
      return response(url.pathname.endsWith('/stats') ? stats('threads') : [entry('needs_review')]);
    });
    await app.choose('threads');
    assert.match(app.elements.get('stats').textContent, /Synced: 7/);
    oldStats.resolve(fails ? response({detail: 'stale private failure'}, 503) : response(stats('bluesky')));
    oldHistory.resolve(response([entry('success')]));
    await flush();
    assert.match(app.elements.get('stats').textContent, /Synced: 7/);
    assert.equal(app.elements.get('history').children[0].children[1].textContent, 'needs review');
    assert.equal(app.elements.get('notice').textContent, '');
  });

  test(`a stale Threads retry ${fails ? 'error' : 'response'} cannot alter the new selection`, async () => {
    const result = deferred();
    const app = dashboard((url, options) => {
      if (options.method === 'POST') return result.promise;
      const destination = url.searchParams.get('destination') || 'bluesky';
      return response(url.pathname.endsWith('/stats') ? stats(destination) : [entry()]);
    });
    await flush();
    await app.choose('threads');
    const retrying = app.elements.get('history').children[0].children[5].children[0].click();
    await app.choose('bluesky');
    result.resolve(fails ? response({detail: 'stale retry failure'}, 409) : response({message: 'stale retry success'}, 202));
    await retrying;
    await flush();
    assert.equal(app.elements.get('notice').textContent, '');
    assert.match(app.elements.get('stats').textContent, /Synced: 2/);
  });
}
