const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function buildContext({ token = 'csrf-token', forms = [], fetchImpl } = {}) {
  const listeners = {};
  const document = {
    querySelector(selector) {
      if (selector === 'meta[name="csrf-token"]') {
        return token ? { content: token } : null;
      }
      return null;
    },
    querySelectorAll(selector) {
      if (selector === 'form') return forms;
      return [];
    },
    addEventListener(name, callback) {
      listeners[name] = callback;
    },
    createElement() {
      return { type: '', name: '', value: '' };
    },
  };

  const context = {
    window: {
      location: { href: 'http://localhost/dashboard', origin: 'http://localhost' },
    },
    document,
    URL,
    Headers,
    Request,
    fetch: fetchImpl || (async () => ({ status: 200, redirected: false })),
  };
  context.window.fetch = context.fetch;
  vm.createContext(context);
  vm.runInContext(fs.readFileSync('static/security.js', 'utf8'), context);
  return { context, listeners };
}

test('adds X-CSRF-Token to same-origin unsafe fetch requests', async () => {
  let headers;
  const { context } = buildContext({
    fetchImpl: async (_input, options) => {
      headers = new Headers(options.headers);
      return { status: 200, redirected: false };
    },
  });
  await context.window.fetch('/api/camera/start', { method: 'POST' });
  assert.equal(headers.get('X-CSRF-Token'), 'csrf-token');
});

test('does not add X-CSRF-Token to cross-origin requests', async () => {
  let headers;
  const { context } = buildContext({
    fetchImpl: async (_input, options) => {
      headers = new Headers(options?.headers || {});
      return { status: 200, redirected: false };
    },
  });
  await context.window.fetch('https://example.com/api', { method: 'POST' });
  assert.equal(headers.get('X-CSRF-Token'), null);
});

test('injects hidden csrf field into unsafe forms on DOMContentLoaded', async () => {
  const appended = [];
  const unsafe = {
    method: 'POST',
    querySelector: () => null,
    append(field) { appended.push(field); },
  };
  const safe = {
    method: 'GET',
    querySelector: () => null,
    append() { throw new Error('safe form should not be modified'); },
  };
  const { listeners } = buildContext({ forms: [unsafe, safe] });
  listeners.DOMContentLoaded();
  assert.equal(appended.length, 1);
  assert.equal(appended[0].name, '_csrf_token');
  assert.equal(appended[0].value, 'csrf-token');
});
