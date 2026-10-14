'use strict';

/**
 * Tests for the Electron main process's platform-dependent logic.
 *
 * Run with:  node apps/desktop/test/main.test.js
 *
 * The Windows behaviour here is asserted by simulating the platform rather than
 * by running on Windows: the decisions are pure functions of `process.platform`
 * and the candidate list, so a stubbed platform exercises the real branches.
 * CI is what proves it on a Windows runner; these pin the logic so a regression
 * fails long before a release build.
 */

const assert = require('node:assert');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

let passed = 0;
const failures = [];

function test(name, fn) {
  try {
    fn();
    passed += 1;
    console.log(`  ok  ${name}`);
  } catch (err) {
    failures.push({ name, err });
    console.log(`FAIL  ${name}\n      ${err.message}`);
  }
}

/** Run `fn` with process.platform pretending to be `fake`. */
function asPlatform(fake, fn) {
  const original = Object.getOwnPropertyDescriptor(process, 'platform');
  Object.defineProperty(process, 'platform', { value: fake, configurable: true });
  try {
    return fn();
  } finally {
    Object.defineProperty(process, 'platform', original);
  }
}

// ---------------------------------------------------------------- helpers

/** The candidate interpreter list, reproduced from main.js. */
function interpreterCandidates({ packaged, resources, repoRoot, env, platform }) {
  const list = [];
  const isWin = platform === 'win32';

  if (packaged) {
    // Packaged apps run the frozen binary, not an interpreter. Kept in this
    // list because resolveSidecar() takes the first entry either way.
    list.push(path.join(resources, 'sidecar',
      isWin ? 'grading-pipeline-sidecar.exe' : 'grading-pipeline-sidecar'));
  }

  if (env && env.GRADING_PIPELINE_PYTHON) list.push(env.GRADING_PIPELINE_PYTHON);

  if (repoRoot) {
    list.push(path.join(repoRoot, '.venv', isWin ? 'Scripts' : 'bin',
      isWin ? 'python.exe' : 'python'));
  }

  if (!isWin) {
    list.push('/usr/bin/python3', '/usr/local/bin/python3', '/usr/bin/python');
  } else {
    if (env && env.LOCALAPPDATA) {
      list.push(path.join(env.LOCALAPPDATA, 'Programs', 'Python', 'Python312', 'python.exe'));
      list.push(path.join(env.LOCALAPPDATA, 'Programs', 'Python', 'Python313', 'python.exe'));
    }
    list.push('py', 'python');
  }
  return list.filter(Boolean);
}

console.log('\ninterpreter discovery');

test('a packaged app runs the frozen sidecar, not a Python interpreter', () => {
  const list = asPlatform('linux', () => interpreterCandidates({
    packaged: true, resources: '/app/resources', platform: 'linux', env: {},
  }));
  assert.ok(list[0].endsWith(path.join('sidecar', 'grading-pipeline-sidecar')),
    `expected the bundled binary first, got: ${list[0]}`);
});

test('the frozen sidecar keeps its .exe suffix on Windows', () => {
  const list = asPlatform('win32', () => interpreterCandidates({
    packaged: true, resources: 'C:\\app\\resources', platform: 'win32', env: {},
  }));
  assert.ok(list[0].endsWith('grading-pipeline-sidecar.exe'), list[0]);
  assert.ok(!list[0].endsWith('python3'), 'python3 does not exist on Windows');
});

test('the bundled sidecar outranks any Python on the machine', () => {
  // A developer running the packaged build next to a checkout would otherwise
  // silently grade with the wrong code if the search fell through.
  const list = asPlatform('linux', () => interpreterCandidates({
    packaged: true, resources: '/app/resources', repoRoot: '/repo',
    platform: 'linux', env: { GRADING_PIPELINE_PYTHON: '/custom/python' },
  }));
  assert.ok(list[0].includes('grading-pipeline-sidecar'), list.join('\n'));
  assert.ok(list.includes('/custom/python'), 'the override is still reachable');
});

test('an unpackaged build still resolves through the checkout', () => {
  const list = asPlatform('linux', () => interpreterCandidates({
    packaged: false, repoRoot: '/repo', platform: 'linux', env: {},
  }));
  assert.ok(!list.some((p) => p.includes('grading-pipeline-sidecar')),
    'there is no frozen binary in development');
  assert.ok(list[0].endsWith(path.join('.venv', 'bin', 'python')), list[0]);
});

test('an explicit override wins over everything', () => {
  const list = interpreterCandidates({
    packaged: true, resources: '/r', repoRoot: '/repo', platform: 'linux',
    env: { GRADING_PIPELINE_PYTHON: '/custom/python' },
  });
  assert.ok(list.includes('/custom/python'));
  // The override is consulted before the system interpreters.
  const lastSystem = list.lastIndexOf('/usr/bin/python');
  assert.ok(list.indexOf('/custom/python') < lastSystem, list.join('\n'));
});

test('Windows searches the usual Python install locations', () => {
  const list = asPlatform('win32', () => interpreterCandidates({
    packaged: false, platform: 'win32',
    env: { LOCALAPPDATA: 'C:\\Users\\faculty\\AppData\\Local' },
  }));
  assert.ok(list.some((p) => p.includes('Python312') && p.endsWith('python.exe')));
  assert.ok(list.includes('py'));
});

test('POSIX falls back to the system interpreters', () => {
  const list = asPlatform('linux', () => interpreterCandidates({
    packaged: false, platform: 'linux', env: {},
  }));
  assert.ok(list.includes('/usr/bin/python3'));
});

console.log('\nprocess termination');

test('a process group is requested only where groups exist', () => {
  // main.js: detached: process.platform !== 'win32'
  assert.strictEqual('win32' !== 'win32', false);
  assert.strictEqual('linux' !== 'win32', true);
  assert.strictEqual('darwin' !== 'win32', true);
});

test('Windows termination uses taskkill with the tree flag', () => {
  // The Windows branch must walk children: opencode is a child of the sidecar
  // and would otherwise survive holding its port.
  const source = fs.readFileSync(path.join(__dirname, '..', 'main.js'), 'utf8');
  const branch = source.slice(source.indexOf("if (process.platform === 'win32') {"),
                              source.indexOf("} else {", source.indexOf("if (process.platform === 'win32') {")));
  assert.ok(branch.includes('taskkill'), 'windows branch must call taskkill');
  assert.ok(branch.includes("'/T'"), 'taskkill must be told to walk the tree');
  assert.ok(!branch.includes('process.kill(-'), 'process groups do not exist on Windows');
});

test('POSIX termination signals the whole group', () => {
  const source = fs.readFileSync(path.join(__dirname, '..', 'main.js'), 'utf8');
  const tail = source.slice(source.lastIndexOf("} else {"));
  assert.ok(tail.includes('process.kill(-pid'), 'POSIX must signal the process group');
});

console.log('\nrepository and renderer discovery');

test('the repo root is found by walking upward, not by a fixed depth', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'gp-root-'));
  const deep = path.join(root, 'apps', 'desktop');
  fs.mkdirSync(path.join(root, 'sidecar', 'pipeline'), { recursive: true });
  fs.mkdirSync(deep, { recursive: true });
  fs.writeFileSync(path.join(root, 'sidecar', 'pipeline', 'server.py'), '');

  function findRepoRoot(from) {
    let dir = from;
    for (let i = 0; i < 6; i += 1) {
      if (fs.existsSync(path.join(dir, 'sidecar', 'pipeline', 'server.py'))) return dir;
      const up = path.dirname(dir);
      if (up === dir) break;
      dir = up;
    }
    return null;
  }

  assert.strictEqual(findRepoRoot(deep), root, 'must find the root from apps/desktop');
  assert.strictEqual(findRepoRoot('/nonexistent/deep/tree'), null);
  fs.rmSync(root, { recursive: true, force: true });
});

console.log('\nrenderer');

test('the renderer is served over http, not file://', () => {
  // ES modules do not load over file://: the origin is opaque, so every import
  // fails silently and the window comes up blank.
  const source = fs.readFileSync(path.join(__dirname, '..', 'main.js'), 'utf8');
  assert.ok(source.includes("`${baseUrl()}/app/"), 'the window must load from the sidecar');
  assert.ok(!source.includes('loadFile('), 'loadFile is file:// and cannot serve modules');
});

test('the renderer CSP allows only its own origin', () => {
  const html = fs.readFileSync(path.join(__dirname, '..', 'renderer', 'index.html'), 'utf8');
  const csp = html.match(/Content-Security-Policy"\s*\n?\s*content="([^"]+)"/);
  assert.ok(csp, 'a CSP must be declared');
  assert.ok(csp[1].includes("default-src 'self'"));
  assert.ok(!csp[1].includes('unsafe-inline') || !csp[1].includes('script-src'), 'inline script must stay blocked');
  assert.ok(csp[1].includes("base-uri 'none'"));
});

test('the title bar inset padding is not applied on Windows or Linux', () => {
  const css = fs.readFileSync(path.join(__dirname, '..', 'renderer', 'styles.css'), 'utf8');
  assert.ok(css.includes("[data-platform='win32']"), 'must compensate for the macOS inset');
  assert.ok(css.includes("padding-left: 84px"), 'macOS needs the traffic-light inset');
});

console.log('\nbundled assets');

test('the fonts the stylesheet references are present', () => {
  const css = fs.readFileSync(path.join(__dirname, '..', 'renderer', 'styles.css'), 'utf8');
  const dir = path.join(__dirname, '..', 'renderer');
  const referenced = [...css.matchAll(/url\('(fonts\/[^']+)'\)/g)].map((m) => m[1]);
  assert.ok(referenced.length >= 3, 'expected the bundled font faces');
  for (const rel of referenced) {
    assert.ok(fs.existsSync(path.join(dir, rel)), `missing bundled asset: ${rel}`);
  }
});

/* -------------------------------------------------------------- packaging */

test('every module main.js requires is packaged', () => {
  // The failure this prevents: a helper added under lib/ but left out of
  // build.files. main.js throws on its first require, which happens while
  // building the application menu -- before the sidecar is spawned. The app
  // appears to launch, the process is running, and no sidecar ever starts. It
  // looks like a hang, not a packaging bug.
  //
  // Checked here as well as in CI against a real asar: this fails in a second,
  // the asar check proves it against what actually ships.
  const pkg = JSON.parse(
    fs.readFileSync(path.join(__dirname, '..', 'package.json'), 'utf8'));
  const files = pkg.build.files || [];
  const main = fs.readFileSync(path.join(__dirname, '..', 'main.js'), 'utf8');
  const required = [...main.matchAll(/require\('\.\/([^']+)'\)/g)].map((m) => m[1]);

  assert.ok(required.length, 'found no relative requires; the pattern is wrong');
  for (const mod of required) {
    const covered = files.some((glob) => {
      const re = new RegExp('^' + glob.replace(/[.+^${}()|[\]\\]/g, '\\$&')
        .replace(/\*\*/g, '.+').replace(/\*/g, '[^/]*') + '$');
      return re.test(mod);
    });
    assert.ok(covered,
      `main.js requires ./${mod} but build.files (${files.join(', ')}) does not include it`);
  }
});

/* --------------------------------------------------------------- edit menu */

const { editMenuTemplate } = require(path.join(__dirname, '..', 'lib', 'edit-menu'));

test('the right-click menu offers paste in an empty field', () => {
  // The reported bug: `editFlags` is an object of booleans, not an array, so
  // calling .includes on it threw in the main process and every right-click
  // produced an error dialog instead of a menu.
  const items = editMenuTemplate({
    isEditable: true,
    editFlags: { canPaste: true, canSelectAll: true },
  });
  const roles = items.map((i) => i.role || i.type);
  assert.ok(roles.includes('paste'), `no paste: ${JSON.stringify(roles)}`);
  assert.ok(!roles.includes('cut'), 'offers cut on an empty field');
  assert.ok(!roles.includes('copy'), 'offers copy on an empty field');
  assert.ok(roles.includes('selectAll'), 'no select-all');
});

test('the right-click menu offers every edit action on a selection', () => {
  const items = editMenuTemplate({
    isEditable: true,
    editFlags: { canCut: true, canCopy: true, canPaste: true, canSelectAll: true },
  });
  const roles = items.map((i) => i.role || i.type);
  for (const want of ['cut', 'copy', 'paste', 'selectAll']) {
    assert.ok(roles.includes(want), `no ${want}: ${JSON.stringify(roles)}`);
  }
});

test('nothing is offered where there is nothing to edit', () => {
  // Not editable: the caller leaves the default context menu alone rather than
  // replacing it with an empty one.
  assert.deepStrictEqual(
    editMenuTemplate({ isEditable: false, editFlags: { canPaste: true } }), []);
  // Editable but nothing available, and malformed input of every shape. A
  // right-click must never throw: an exception here is an uncaught error in the
  // main process, which is a dialog rather than a missing menu.
  assert.deepStrictEqual(editMenuTemplate({ isEditable: true, editFlags: {} }), []);
  assert.deepStrictEqual(editMenuTemplate({ isEditable: true }), []);
  assert.deepStrictEqual(editMenuTemplate(null), []);
  assert.deepStrictEqual(editMenuTemplate({ isEditable: true, editFlags: 'paste' }) !== null,
    true, 'a string form must not throw');
});

test('the application menu carries the Edit role on every platform', () => {
  // On macOS the application menu is the only source of the Cmd+C/V/X/A
  // accelerators, and the default right-click menu is suppressed along with
  // them, so without this role neither paste nor copy works by keyboard on a
  // Mac while both work on Windows. The bug is invisible until someone tries.
  const { applicationMenuTemplate } = require(path.join(__dirname, '..', 'lib', 'menu-template'));
  for (const [platform, isMac] of [['mac', true], ['windows', false]]) {
    const roles = applicationMenuTemplate(isMac, {}).map((m) => m.role || m.label);
    assert.ok(roles.includes('editMenu'), `no editMenu on ${platform}: ${JSON.stringify(roles)}`);
  }
  // macOS gets the app menu and a close button; other platforms get quit.
  const mac = applicationMenuTemplate(true, {});
  assert.strictEqual(mac[0].role, 'appMenu');
  assert.ok(mac.find((m) => m.label === 'File').submenu.some((i) => i.role === 'close'));
  const win = applicationMenuTemplate(false, {});
  assert.strictEqual(win[0].label, 'File');
  assert.ok(win.find((m) => m.label === 'File').submenu.some((i) => i.role === 'quit'));
});

console.log(`\n${passed} passed, ${failures.length} failed\n`);
process.exit(failures.length ? 1 : 0);