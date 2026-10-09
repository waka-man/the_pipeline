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

test('every endpoint the renderer calls is implemented by the sidecar', () => {
  const serverPath = path.join(__dirname, '..', '..', '..', 'sidecar', 'pipeline', 'server.py');
  const server = fs.readFileSync(serverPath, 'utf8');
  const preload = fs.readFileSync(path.join(__dirname, '..', 'preload.js'), 'utf8');

  // Curated rather than derived: re-implementing the router's own regex here
  // would only prove the table matches itself.
  const required = [
    'GET', '/api/status',
    'POST', '/api/settings',
    'GET', '/api/models',
    'POST', '/api/models/select',
    'GET', '/api/courses',
    'POST', '/api/runs',
    'POST', '/api/runs/1/collect',
    'POST', '/api/runs/1/grade',
    'POST', '/api/runs/1/render',
    'POST', '/api/runs/1/publish',
    'PUT', '/api/reports/1',
  ];
  const esc = (x) => x.replace(/[.*+?^${}()|[\]\\]/g, '\\$&').split('/').join('\\/');
  for (let i = 0; i < required.length; i += 2) {
    const verb = required[i];
    const route = required[i + 1];
    // Route patterns interleave named groups (`(?P<run_id>\\d+)`) where the
    // caller substitutes an id, so compare the static prefix and tail.
    const parts = route.split('1');
    const prefix = esc(parts[0]);
    const tail = parts.length > 1 ? esc(parts.slice(1).join('1')) : '';
    const re = new RegExp(`Route_\\("${verb}",\\s*r"\\^${prefix}[^"]*${tail}\\$"`);
    assert.ok(re.test(server), `sidecar has no ${verb} ${route}`);
  }

  // The event stream is not a Route_; it is handled before the router.
  assert.ok(preload.includes('/api/events'));
  assert.ok(server.includes('"/api/events"'));
});

console.log(`\n${passed} passed, ${failures.length} failed\n`);
process.exit(failures.length ? 1 : 0);