const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync('templates/sorting_session.html', 'utf8');
const source = html.match(/function showCameraSuccessModal\(title\) \{[\s\S]*?\n\}/)[0];
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
