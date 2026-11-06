const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync('templates/dashboard.html', 'utf8');
const script = [...html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)]
  .find((m) => m[1].includes('async function refreshDashboard'))[1];
const settle = () => new Promise((resolve) => setImmediate(resolve));
function payload(overrides = {}) {
  return {total_sorted: 0, latest_record: null, trays_completed: 0, tray_progress: [],
    quality_rate: 0, quality_counts: {}, non_good_count: 0,
    selected_start_date: '2026-10-23', selected_end_date: '2026-10-29',
    daily_trend: [{date: '2026-10-29', label: 'Thu', count: 0}], daily_trend_sparse: false,
    size_counts: {Small: 0, Medium: 0, Large: 0, 'Extra Large': 0, Jumbo: 0}, ...overrides};
}
const response = (data, ok = true) => ({ok, redirected: false, json: async () => data});
function build(fetchImpl, width = 1280) {
  const ids = new Map(), writes = [], intervals = [], errors = [];
  class Element {
    constructor() {
      this.children = []; this.attrs = {}; this.listeners = {}; this.style = {};
      this.value = ''; this.hidden = false; this.className = ''; this.clientWidth = 600;
      this.classList = {toggle() {}};
    }
    set id(value) {this._id = value; ids.set(value, this);}
    get id() {return this._id;}
    set textContent(value) {this._text = value; writes.push([this.id, value]);}
    get textContent() {return this._text;}
    appendChild(child) {child.parent = this; this.children.push(child); return child;}
    append(...children) {for (const child of children) this.appendChild(typeof child === 'string' ? {textContent: child} : child);}
    replaceChildren(...children) {for (const child of [...this.children]) child.remove?.(); this.children = []; this.append(...children);}
    remove() {
      for (const child of [...this.children]) child.remove?.();
      if (this.id && ids.get(this.id) === this) ids.delete(this.id);
      if (this.parent) this.parent.children = this.parent.children.filter((child) => child !== this);
    }
    setAttribute(key, value) {this.attrs[key] = value;}
    addEventListener(name, callback) {this.listeners[name] = callback;}
  }
  for (const id of ['sidebar', 'sidebar-overlay', 'dashboard-tray-progress', 'dashboard-daily-trend',
    'refresh-dashboard', 'refresh-dashboard-icon', 'dashboard-status-text', 'dashboard-status-dot',
    'dashboard-message', 'dashboard-total-sorted', 'dashboard-total-note', 'dashboard-trays',
    'dashboard-trays-note', 'dashboard-quality-rate', 'dashboard-quality-note', 'dashboard-non-good',
    'dashboard-size-distribution']) {const element = new Element(); element.id = id;}
  class FixedDate extends Date {constructor(...args) {super(...(args.length ? args : ['2026-10-29T03:00:00Z']));}}
  const document = {getElementById: (id) => ids.get(id) || null, createElement: () => new Element()};
  const context = {document, URL, Date: FixedDate, Intl, queueMicrotask, console: {error() {}},
    fetch: fetchImpl, EggSortAlerts: {error: async (message) => errors.push(message), success: async () => {}},
    window: {innerWidth: width, location: {origin: 'http://test.invalid'},
      setInterval: (callback, milliseconds) => intervals.push(milliseconds), setTimeout: () => 1, clearTimeout() {}}};
  vm.createContext(context); vm.runInContext(script, context);
  return {context, ids, writes, intervals, errors, refresh: (options) => context.refreshDashboard(options)};
}

test('rapid date changes preserve input nodes and discard old cards', async () => {
  const urls = []; let release;
  const ui = build(async (url) => {
    urls.push(url.toString());
    if (urls.length === 2) return await new Promise((resolve) => {release = resolve;});
    return response(payload({total_sorted: urls.length === 3 ? 7 : 0}));
  });
  await settle();
  const from = ui.ids.get('dashboard-start-date');
  from.value = '2026-10-24'; from.listeners.change();
  from.value = '2026-10-25'; from.listeners.change();
  release(response(payload({total_sorted: 99})));
  await settle(); await settle();
  assert.equal(ui.ids.get('dashboard-start-date'), from);
  assert.equal(from.value, '2026-10-25');
  assert.match(urls[2], /start_date=2026-10-25/);
  assert.equal(ui.ids.get('dashboard-total-sorted').textContent, 7);
  assert.equal(ui.writes.some(([id, value]) => id === 'dashboard-total-sorted' && value === 99), false);
});

for (const width of [360, 1280]) {
  test(`initial error is visible at ${width}px and does not certify zero totals`, async () => {
    const ui = build(async () => response({error: 'Database temporarily unavailable.'}, false), width);
    await settle();
    const message = ui.ids.get('dashboard-message');
    assert.equal(message.hidden, false);
    assert.equal(message.attrs.role, 'alert');
    assert.match(message.textContent, /Database temporarily unavailable/);
    assert.match(message.textContent, /not confirmed totals/);
    const tag = html.match(/<p id="dashboard-message"[\s\S]*?>/)[0];
    assert.doesNotMatch(tag, /\bhidden\b|sm:flex/);
  });
}

test('failed refresh keeps old values, warns stale, and recovers', async () => {
  let call = 0;
  const ui = build(async () => {
    call++;
    if (call === 2) throw new Error('Network offline');
    return response(payload({total_sorted: call}));
  });
  await settle(); await ui.refresh();
  assert.equal(ui.ids.get('dashboard-total-sorted').textContent, 1);
  assert.match(ui.ids.get('dashboard-message').textContent, /Displayed data is stale/);
  await ui.refresh();
  assert.equal(ui.ids.get('dashboard-total-sorted').textContent, 3);
  assert.equal(ui.ids.get('dashboard-message').hidden, true);
  assert.deepEqual(ui.intervals, [750]);
});

test('invalid range gives visible validation without fetching', async () => {
  let calls = 0;
  const ui = build(async () => {calls++; return response(payload());});
  await settle();
  ui.ids.get('dashboard-start-date').value = '2026-10-30';
  ui.ids.get('dashboard-start-date').listeners.change();
  await settle();
  assert.equal(calls, 1);
  assert.match(ui.ids.get('dashboard-message').textContent, /From date cannot be after To date/);
});

test('rejected eggs have visible N/A counts and correct scale', async () => {
  const ui = build(async () => response(payload({total_sorted: 3, non_good_count: 3,
    size_counts: {Small: 0, Medium: 0, Large: 0, 'Extra Large': 0, Jumbo: 0, 'N/A': 3}})));
  await settle();
  const rendered = ui.ids.get('dashboard-size-distribution').innerHTML;
  assert.match(rendered, /Unmeasured\/N\/A<\/span><strong>3/);
  assert.match(rendered, /width:100%/);
  assert.equal(ui.ids.get('dashboard-non-good').textContent, 3);
});

test('unknown sizes are shown separately without calling them unmeasured', async () => {
  const ui = build(async () => response(payload({size_counts: {Small: 2, Mystery: 1}})));
  await settle();
  const rendered = ui.ids.get('dashboard-size-distribution').innerHTML;
  assert.match(rendered, /Other\/Unknown<\/span><strong>1/);
  assert.match(rendered, /Unmeasured\/N\/A<\/span><strong>0/);
});

test('empty and sparse trends explain their scope and show actual dates', async () => {
  const ui = build(async () => response(payload())); await settle();
  assert.match(ui.ids.get('dashboard-trend-chart').children[0].textContent, /No eggs sorted/);
  const ui2 = build(async () => response(payload({daily_trend_sparse: true,
    daily_trend: [{date: '2026-10-19', label: 'Mon', count: 1}, {date: '2026-10-26', label: 'Mon', count: 1}]})));
  await settle();
  const viewport = ui2.ids.get('dashboard-trend-chart');
  assert.match(viewport.children[0].textContent, /longer than 366 days/);
  assert.equal(viewport.children[1].children[0].title, '2026-10-19: 1 eggs');
  assert.equal(viewport.children[1].children[1].children[2].textContent, '2026-10-26');
});

test('audit clear has no active browser command path and labels are cumulative', () => {
  assert.match(html, /id="clear-audit-logs"\s+type="button"\s+disabled/);
  assert.doesNotMatch(script, /\/api\/dashboard\/audit-logs\/clear/);
  assert.match(html, /Cumulative counts are not affected by the date filter/);
  assert.doesNotMatch(html, /Operations Portal \/ Admin/);
});
