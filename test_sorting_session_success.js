const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync('templates/sorting_session.html', 'utf8');
const source = html.match(/function showCameraSuccessModal\(title\) \{[\s\S]*?\n\}/)[0];
const extract = (start, end) => html.slice(html.indexOf(start), html.indexOf(end, html.indexOf(start)));
test('Sorting Sessions inline scripts parse without starting the page', () => {
  for (const match of html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)) new vm.Script(match[1]);
});
for (const title of ['Restarted Successfully!', 'Closed Successfully!']) {
  test(`${title} is centered, timed, buttonless and nonblocking`, () => {
    const dialogs = [];
    const context = {window: {Swal: {fire(options) {dialogs.push(options); return new Promise(() => {});}}}};
    vm.createContext(context); vm.runInContext(source, context);
    assert.equal(context.showCameraSuccessModal(title), undefined);
    assert.equal(dialogs.length, 1);
    const options = dialogs[0];
    assert.equal(options.title, title); assert.equal(options.icon, 'success');
    assert.equal(options.toast, false); assert.equal(options.position, 'center');
    assert.equal(options.showConfirmButton, false); assert.equal(options.timer, 1500);
  });
}
test('No SweetAlert library uses the existing status fallback', () => {
  const context = {window: {}, statusText: {textContent: ''}};
  vm.createContext(context); vm.runInContext(source, context);
  context.showCameraSuccessModal('Closed Successfully!');
  assert.equal(context.statusText.textContent, 'Closed Successfully!');
});

test('Camera becomes ready without an egg while the ESP32 is pending', () => {
  const messages = [], errors = [], successes = [];
  const context = {
    startSuccessPending: true,
    syncStartButton() {},
    feedConnected: true,
    startButton: {}, stopButton: {}, loadingPanel: {classList: {toggle() {}}},
    statusText: {textContent: ''}, setCameraState() {}, clearCameraError() {},
    showCameraError: value => errors.push(value), updateCounts() {},
    connectFeed() {}, showCameraSuccessModal: value => successes.push(value),
    document: {getElementById: () => ({classList: {toggle() {}}})},
    window: {}
  };
  vm.createContext(context);
  vm.runInContext(extract('function showRunningSession', 'function showStoppedSession'), context);
  context.showRunningSession({running: true, lifecycle_status: 'starting', frame_ready: true,
    detection_ready: false, counts: {}, detections: [], model: null, bridge_running: true,
    bridge_connected: false});
  messages.push(context.statusText.textContent);
  assert.equal(successes.length, 0);
  assert.equal(messages[0], 'Camera Ready — Waiting for ESP32.');
  context.showRunningSession({running: true, lifecycle_status: 'ready', recovery_required: false,
    frame_ready: true, detection_ready: false, counts: {}, detections: [], model: null,
    bridge_running: true, bridge_connected: true});
  assert.deepEqual(successes, ['Restarted Successfully!']);
  assert.match(context.statusText.textContent, /^Sorting System Ready/);
  context.startSuccessPending = true;
  context.showRunningSession({running: true, lifecycle_status: 'failed', recovery_required: true,
    failure_reason: 'USB link lost', frame_ready: true, detection_ready: false,
    counts: {}, detections: [], model: null, bridge_running: true, bridge_connected: false});
  assert.deepEqual(successes, ['Restarted Successfully!']);
  assert.match(errors.at(-1), /USB link lost/);
});

test('ESP32 readiness timeout remains a visible failed recovery state', () => {
  const errors = [];
  const context = {
    startSuccessPending: true, syncStartButton() {}, feedConnected: true,
    startButton: {}, stopButton: {}, loadingPanel: {classList: {toggle() {}}},
    statusText: {textContent: ''}, setCameraState() {}, clearCameraError() {},
    showCameraError: value => errors.push(value), updateCounts() {}, connectFeed() {},
    document: {getElementById: () => ({classList: {toggle() {}}})}, window: {}
  };
  vm.createContext(context);
  vm.runInContext(extract('function showRunningSession', 'function showStoppedSession'), context);
  context.showRunningSession({running: true, lifecycle_status: 'failed', recovery_required: true,
    failure_reason: 'Startup readiness timed out waiting for ESP32 controller readiness.',
    frame_ready: true, counts: {}, detections: [], model: null, bridge_running: true,
    bridge_connected: true});
  assert.match(context.statusText.textContent, /Session failed\. Recovery is required/);
  assert.match(errors[0], /ESP32 controller readiness/);
  assert.equal(context.startSuccessPending, true);
});

test('Camera startup remains clear while no frame exists; ready state is not claimed early', () => {
  const context = {
    startSuccessPending: true, syncStartButton() {}, feedConnected: true,
    startButton: {}, stopButton: {}, loadingPanel: {classList: {toggle() {}}},
    statusText: {textContent: ''}, setCameraState() {}, clearCameraError() {},
    showCameraError() {}, updateCounts() {}, connectFeed() {},
    document: {getElementById: () => ({classList: {toggle() {}}})}, window: {}
  };
  vm.createContext(context);
  vm.runInContext(extract('function showRunningSession', 'function showStoppedSession'), context);
  context.showRunningSession({running: true, lifecycle_status: 'starting', frame_ready: false,
    counts: {}, detections: [], model: null, bridge_running: true, bridge_connected: true});
  assert.match(context.statusText.textContent, /Starting camera; waiting for the first live frame/);
  assert.doesNotMatch(context.statusText.textContent, /Sorting System Ready/);
  assert.equal(context.startSuccessPending, true);
});

test('Missing lifecycle metadata never implies sorting readiness', () => {
  const context = {
    startSuccessPending: true, syncStartButton() {}, feedConnected: true,
    startButton: {}, stopButton: {}, loadingPanel: {classList: {toggle() {}}},
    statusText: {textContent: ''}, setCameraState() {}, clearCameraError() {},
    showCameraError() {}, updateCounts() {}, connectFeed() {},
    document: {getElementById: () => ({classList: {toggle() {}}})}, window: {}
  };
  vm.createContext(context);
  vm.runInContext(extract('function showRunningSession', 'function showStoppedSession'), context);
  context.showRunningSession({running: true, frame_ready: true, counts: {}, detections: [], model: null});
  assert.equal(context.statusText.textContent, 'Camera Ready — Waiting for ESP32.');
  assert.doesNotMatch(context.statusText.textContent, /Sorting System Ready/);
  assert.equal(context.startSuccessPending, true);
});

test('Repeated restart clicks during confirmation issue only one start request', async () => {
  let resolveConfirmation;
  let confirmations = 0;
  let starts = 0;
  const noop = () => {};
  const classes = {add: noop, remove: noop};
  const context = {
    startButton: {disabled: false, classList: classes},
    stopButton: {disabled: false}, cameraActionPending: false,
    EggSortAlerts: {confirm: () => { confirmations++; return new Promise(resolve => { resolveConfirmation = resolve; }); }},
    cameraActionRevision: 0, hardwareStatusRevision: 0,
    startSuccessPending: false, operationAuditWarning: null,
    clearCameraError: noop, setCameraState: noop,
    loadingPanel: {classList: {remove: noop}}, statusText: {textContent: ''},
    requestJson: async () => { starts++; return {camera: {lifecycle_status: 'starting', frame_ready: true}, hardware: {}}; },
    showRunningSession: noop, refreshHardwareState: noop, showCameraWarning: noop,
    showCameraSuccessModal: noop, showStoppedSession: noop, showCameraError: noop,
    syncStartButton: noop, refreshSessionState: noop
  };
  vm.createContext(context);
  vm.runInContext(extract('async function startCameraSession', 'async function stopCameraSession'), context);
  const first = context.startCameraSession();
  await context.startCameraSession();
  assert.equal(confirmations, 1);
  resolveConfirmation(true);
  await first;
  assert.equal(starts, 1);
});

test('Lifecycle display distinguishes stopping, completed, interrupted, failed, and finalization pending', () => {
  const errors = [];
  const classList = {toggle() {}, add() {}, remove() {}};
  const context = {
    startButton: {disabled: false}, stopButton: {disabled: false},
    cameraActionPending: false, lastLifecycleStatus: null, lastKnownCameraRunning: null,
    startSuccessPending: false,
    feedConnected: true, cameraFeed: {removeAttribute() {}},
    loadingPanel: {classList}, statusText: {textContent: ''},
    setCameraState() {}, clearCameraError() {},
    showCameraError: message => errors.push(message), showCameraSuccessModal() {}, updateCounts() {},
    connectFeed() {}, document: {getElementById: () => ({classList})}, window: {}
  };
  vm.createContext(context);
  vm.runInContext(extract('function syncStartButton', 'function showCameraError'), context);
  vm.runInContext(extract('function showRunningSession', 'async function refreshSessionState'), context);

  context.showRunningSession({running: true, lifecycle_status: 'stopping',
    finalization_pending: true, recovery_required: true, failure_reason: null,
    frame_ready: true, counts: {}, detections: [], model: null});
  assert.equal(context.statusText.textContent, 'Stopping session...');
  assert.equal(context.startButton.disabled, true);

  context.showStoppedSession({lifecycle_status: 'completed', recovery_required: false});
  assert.equal(context.statusText.textContent, 'Session completed.');
  assert.equal(context.startButton.disabled, false);
  context.showRunningSession({running: true, lifecycle_status: 'ready', recovery_required: false,
    frame_ready: true, detection_fps: 1, stream_fps: 30, counts: {}, detections: [], model: null,
    bridge_running: true, bridge_connected: true});
  assert.match(context.statusText.textContent, /^Sorting System Ready/);

  context.showStoppedSession({lifecycle_status: 'interrupted', recovery_required: true,
    failure_reason: 'unfinished egg cycle'});
  assert.match(context.statusText.textContent, /interrupted.*Recovery is required/i);
  assert.equal(errors.at(-1), 'unfinished egg cycle');

  context.showStoppedSession({lifecycle_status: 'failed', recovery_required: true,
    failure_reason: 'ESP32 failed'});
  assert.match(context.statusText.textContent, /failed.*Recovery is required/i);
  assert.equal(errors.at(-1), 'ESP32 failed');

  context.showStoppedSession({lifecycle_status: 'stopping', recovery_required: true,
    finalization_pending: true, failure_reason: 'terminal write failed'});
  assert.equal(context.statusText.textContent, 'Stopping session...');
  assert.match(errors.at(-1), /finalization is pending.*terminal write failed/i);
  assert.equal(context.startButton.disabled, true);
});

test('Camera status failure displays unknown and preserves the last known panel', async () => {
  const calls = [];
  const context = {
    cameraStatusPending: false, cameraActionPending: false, cameraActionRevision: 0,
    lastKnownCameraRunning: true,
    requestJson: async () => { throw new Error('simulated network outage'); },
    showStoppedSession: () => calls.push('stopped'), showRunningSession: () => calls.push('running'),
    showCameraError: value => calls.push(value), statusText: {textContent: ''}
  };
  vm.createContext(context);
  vm.runInContext(extract('async function refreshSessionState', 'async function refreshHardwareState'), context);
  await context.refreshSessionState();
  assert.deepEqual(calls, ['simulated network outage']);
  assert.match(context.statusText.textContent, /unavailable.*Last known state: running/);
});

test('Older hardware poll cannot overwrite a newer operation state', async () => {
  const pending = [];
  const node = () => ({textContent: '', className: ''});
  const context = {
    hardwareStatusPending: false, hardwareStatusRevision: 0,
    hardwareStatus: node(), hardwareEvent: node(), latestEggRecord: node(),
    requestJson: () => new Promise(resolve => pending.push(resolve))
  };
  vm.createContext(context);
  vm.runInContext(extract('async function refreshHardwareState', 'function startStatusPolling'), context);
  const stale = context.refreshHardwareState();
  context.hardwareStatusRevision++;
  pending[0]({connected: true, running: true, port: 'COM-test', baud_rate: 115200, diagnostics: {}});
  pending[1]({records: []});
  await stale;
  assert.equal(context.hardwareStatus.textContent, '');
  const current = context.refreshHardwareState();
  pending[2]({connected: false, running: false, diagnostics: {}, latest_event: {message: 'Stopped'}});
  pending[3]({records: []});
  await current;
  assert.equal(context.hardwareStatus.textContent, 'Stopped');
});

test('Request timeout remains active while the JSON body is being read', async () => {
  const context = {
    AbortController, URL, Headers, Request, setTimeout, clearTimeout,
    window: {setTimeout, clearTimeout, location: {href: 'http://local.test/'}}
  };
  context.fetch = (_url, options) => Promise.resolve({
    redirected: false, ok: true,
    json: () => new Promise((_resolve, reject) => options.signal.addEventListener('abort', () => {
      const error = new Error('aborted body'); error.name = 'AbortError'; reject(error);
    }))
  });
  vm.createContext(context);
  vm.runInContext(extract('async function requestJson', 'function connectFeed'), context);
  await assert.rejects(context.requestJson('/api/test', {timeoutMs: 15}), /request timed out/i);
});

test('Sorting Sessions heading is role-neutral', () => {
  assert.match(html, />Sorting Operations<\/p>/);
  assert.doesNotMatch(html, />Admin<\/p>/);
});

test('Nonfatal audit warning remains visible across status refreshes', () => {
  const classes = new Set(['hidden', 'bg-error-container', 'text-on-error-container']);
  const panel = {textContent: '', classList: {
    add: (...names) => names.forEach(name => classes.add(name)),
    remove: (...names) => names.forEach(name => classes.delete(name))
  }};
  const context = {errorPanel: panel, operationAuditWarning: null};
  vm.createContext(context);
  vm.runInContext(extract('function showCameraError', 'function setCameraState'), context);
  context.showCameraWarning('Operation completed; audit event could not be saved.');
  context.clearCameraError();
  assert.match(panel.textContent, /audit event could not be saved/);
  assert.equal(classes.has('hidden'), false);
  assert.equal(classes.has('bg-amber-50'), true);
});
