const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync('templates/sorting_session.html', 'utf8');
const source = html.slice(html.indexOf('async function requestJson'), html.indexOf('function connectFeed'));

function request(fetch) {
  const context = {fetch, AbortController, window: {setTimeout, clearTimeout, location: {}}};
  vm.createContext(context);
  vm.runInContext(source, context);
  return context.requestJson;
}

for (const status of [500, 502, 200]) {
  test(`Non-JSON HTTP ${status} produces readable fallback without exposing HTML`, async () => {
    const call = request(async () => new Response('<html>private traceback</html>', {status}));
    await assert.rejects(call('/api/camera/start'), error => {
      assert.match(error.message, /sorting computer returned an unreadable response/);
      assert.doesNotMatch(error.message, /Unexpected token|private traceback|<html>/);
      return true;
    });
  });
}

test('Expected JSON startup error preserves useful hardware message', async () => {
  const call = request(async () => Response.json({error: 'ESP32 startup unavailable.'}, {status: 503}));
  await assert.rejects(call('/api/camera/start'), /ESP32 startup unavailable\./);
});

test('Null JSON error body gets fallback rather than property access exception', async () => {
  const call = request(async () => Response.json(null, {status: 500}));
  await assert.rejects(call('/api/camera/start'), /Camera request failed\./);
});

test('Successful JSON payload is unchanged', async () => {
  const call = request(async () => Response.json({camera: {lifecycle_status: 'starting'}}));
  assert.equal((await call('/api/camera/start')).camera.lifecycle_status, 'starting');
});

test('Abort while reading response body still gets timeout message', async () => {
  const call = request(async (_url, options) => ({redirected: false, ok: true,
    json: () => new Promise((_resolve, reject) => options.signal.addEventListener('abort', () => {
      const error = new Error('aborted'); error.name = 'AbortError'; reject(error);
    }))}));
  await assert.rejects(call('/api/camera/start', {timeoutMs: 10}), /camera request timed out/);
});

test('Transport failures retain their message', async () => {
  const call = request(async () => {throw new Error('Network unavailable');});
  await assert.rejects(call('/api/camera/start'), /Network unavailable/);
});
