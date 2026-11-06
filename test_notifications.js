const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function setup(confirm = true) {
  const dialogs = [];
  const listeners = {};
  const context = {
    window: { Swal: { fire: async (options) => {
      dialogs.push(options);
      return { isConfirmed: confirm };
    } } },
    document: { addEventListener(type, callback) { listeners[type] = callback; } },
    URL,
  };
  context.window.location = { href: 'http://localhost/login' };
  vm.createContext(context);
  vm.runInContext(fs.readFileSync('static/notifications.js', 'utf8'), context);
  context.EggSortAlerts = context.window.EggSortAlerts;
  return { context, alerts: context.window.EggSortAlerts, dialogs, listeners };
}

function authForm(path) {
  const button = { disabled: false };
  return { action: `http://localhost${path}`, dataset: {}, querySelector: () => button };
}

test('cancelling logout leaves the session untouched', async () => {
  const { context, listeners } = setup(false);
  let requests = 0;
  context.fetch = async () => requests++;
  const form = authForm('/logout');
  await listeners.submit({ target: form, preventDefault() {} });
  assert.equal(requests, 0);
  assert.equal(form.querySelector().disabled, false);
  assert.equal(form.dataset.submitting, undefined);
});

test('invalid login credentials produce an error without navigating', async () => {
  const { context, listeners, dialogs } = setup();
  context.FormData = class {};
  context.DOMParser = class {
    parseFromString() { return { querySelector: () => ({ textContent: 'Invalid username/email or password.' }) }; }
  };
  context.fetch = async () => ({ ok: true, redirected: false, url: 'http://localhost/login', text: async () => '' });
  const form = authForm('/login');
  await listeners.submit({ target: form, preventDefault() {} });
  assert.equal(dialogs.at(-1).icon, 'error');
  assert.match(dialogs.at(-1).text, /Invalid username/);
  assert.equal(form.querySelector().disabled, false);
});

test('confirmed logout navigates directly to login without a success dialog', async () => {
  const { context, listeners, dialogs } = setup();
  context.FormData = class {};
  let requests = 0;
  context.fetch = async () => {
    requests++;
    return { ok: true, redirected: true, url: 'http://localhost/login' };
  };
  let destination;
  context.window.location.assign = (url) => {
    destination = url;
  };
  await listeners.submit({ target: authForm('/logout'), preventDefault() {} });
  assert.equal(destination, 'http://localhost/login');
  assert.equal(requests, 1);
  assert.equal(dialogs.length, 1);
  assert.equal(dialogs[0].title, 'Log out?');
});

test('failed logout shows an error and stays on the current page', async () => {
  const { context, listeners, dialogs } = setup();
  context.FormData = class {};
  context.fetch = async () => { throw new Error('Offline'); };
  let navigated = false;
  context.window.location.assign = () => { navigated = true; };
  const form = authForm('/logout');
  await listeners.submit({ target: form, preventDefault() {} });
  assert.equal(navigated, false);
  assert.equal(dialogs.at(-1).icon, 'error');
  assert.equal(dialogs.at(-1).text, 'Offline');
  assert.equal(form.querySelector().disabled, false);
  assert.equal(form.dataset.submitting, undefined);
});

test('cancel prevents the action and restores the button', async () => {
  const { alerts, dialogs } = setup(false);
  const button = { disabled: false };
  let requests = 0;
  await alerts.run({ button, title: 'Delete?', action: () => requests++ });
  assert.equal(requests, 0);
  assert.equal(button.disabled, false);
  assert.equal(dialogs.length, 1);
});

test('success appears only after the confirmed action completes', async () => {
  const { alerts, dialogs } = setup();
  const button = { disabled: false };
  const result = await alerts.run({ button, title: 'Save?', success: 'Saved', action: async () => {
    assert.equal(button.disabled, true);
    assert.equal(dialogs.length, 1);
    return { id: 7 };
  } });
  assert.equal(result.id, 7);
  assert.equal(dialogs[1].icon, 'success');
  assert.equal(button.disabled, false);
});

test('network errors show failure and restore the button without a success message', async () => {
  const { alerts, dialogs } = setup();
  const button = { disabled: false };
  await alerts.run({ button, title: 'Save?', action: async () => { throw new Error('Offline'); } });
  assert.equal(dialogs[1].icon, 'error');
  assert.equal(dialogs[1].text, 'Offline');
  assert.equal(button.disabled, false);
  assert.equal(dialogs.some((dialog) => dialog.icon === 'success'), false);
});

test('duplicate clicks cannot execute a pending action twice', async () => {
  const { alerts } = setup();
  const button = { disabled: false };
  let finish;
  let calls = 0;
  const options = { button, title: 'Save?', action: () => {
    calls++;
    return new Promise((resolve) => { finish = resolve; });
  } };
  const pending = alerts.run(options);
  await Promise.resolve();
  await Promise.resolve();
  await alerts.run(options);
  assert.equal(calls, 1);
  finish();
  await pending;
});

test('server validation and invalid error responses are reported as failures', async () => {
  const { context, alerts } = setup();
  context.fetch = async () => ({ ok: false, json: async () => ({ error: 'Email already exists.' }) });
  await assert.rejects(alerts.request('/api/users'), /Email already exists/);
  context.fetch = async () => ({ ok: false, json: async () => { throw new Error('Invalid JSON'); } });
  await assert.rejects(alerts.request('/api/users'), /could not be completed/);
});

test('dashboard scripts remain valid JavaScript', () => {
  for (const name of ['dashboard', 'login', 'user_management', 'alerts', 'sorting_session', 'egg_records', 'reports']) {
    const html = fs.readFileSync(`templates/${name}.html`, 'utf8');
    for (const match of html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)) {
      new vm.Script(match[1], { filename: `templates/${name}.html` });
    }
    assert.ok(html.includes('_notifications.html'), name);
  }
});
