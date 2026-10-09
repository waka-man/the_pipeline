'use strict';

/**
 * Renderer test. Runs in plain Node against jsdom:
 *
 *     node test/renderer.test.js
 *
 * Why this exists. Two defects reached v0.1.0 that no existing test could see,
 * because both only appear when the renderer actually runs its handlers:
 *
 *   - The Collect button threw `ReferenceError: args is not defined` on every
 *     click. It referenced a name that does not exist, and only a click executes
 *     the handler. Assertions over source text cannot see that.
 *   - `/app/` returned 404 in the packaged app, because the renderer lives inside
 *     the asar and the sidecar is a separate process that cannot read it. The
 *     frozen-sidecar CI job now curls /app/, which is what catches that one.
 *
 * This drives the real renderer module in a real DOM and clicks every button in
 * every stage, so a handler that throws is a test failure rather than something
 * a user discovers.
 *
 * jsdom is not a browser. It is used here because the failure being prevented is
 * a JavaScript ReferenceError in a DOM event handler, which needs a DOM and a
 * module loader and nothing more. Anything needing layout, painting or Chromium
 * is out of scope and would be tested in Electron.
 */

const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert');
const esbuild = require('esbuild');
const { JSDOM, VirtualConsole } = require('jsdom');

const ROOT = path.join(__dirname, '..');
const RENDERER = path.join(ROOT, 'renderer');

let passed = 0;
const failures = [];

function test(name, fn) {
  try {
    fn();
    passed += 1;
    console.log(`  ok  ${name}`);
  } catch (err) {
    failures.push({ name, err });
    console.log(`  FAIL ${name}\n       ${err.message}`);
  }
}

/* ------------------------------------------------------------------ stubs */

// Only the fields the renderer actually reads. A stub that is missing one shows
// up as a confusing TypeError here rather than as a clear stub gap, so the shape
// is derived from the renderer's usage rather than guessed at.
const RUN = {
  run_id: 1,
  course_id: 3130,
  assignment_id: 46805,
  title: 'Assignment',
  status: 'graded',
  points_possible: 25,
  criteria: [
    { id: 'c1', name: 'Correct extraction', points: 10 },
    { id: 'c2', name: 'Edge cases', points: 8 },
    { id: 'c3', name: 'Security awareness', points: 7 },
  ],
};

const SUBMISSIONS = [
  { row_id: 1, student_name: 'Ada Lovelace', score: 8, status: 'graded', collected: true },
  { row_id: 2, student_name: 'Grace Hopper', score: 6, status: 'graded', collected: true },
  { row_id: 3, student_name: 'Alan Turing', score: 5, status: 'graded', collected: true },
];

const STAGES = ['Setup', 'Select', 'Collect', 'Grade', 'Review', 'Publish'];

/** Records every API call so a click can be proven to have reached the API. */
const calls = [];

function stubApi() {
  const rec = (name, value) => (...args) => {
    calls.push(`${name}(${JSON.stringify(args[0] ?? null)})`);
    return Promise.resolve(value);
  };
  return {
    status: rec('status', { canvas_configured: true, jobs: {}, model: { selected: 'm1' } }),
    saveSettings: rec('saveSettings', { ok: true }),
    models: rec('models', { selected: 'm1', candidates: [{ id: 'm1', name: 'test-model' }] }),
    selectModel: rec('selectModel', { selected: 'm1' }),
    courses: rec('courses', { courses: [{ id: 3130, name: 'Course' }] }),
    assignments: rec('assignments', { assignments: [{ id: 46805, name: 'Assignment' }] }),
    createRun: rec('createRun', RUN),
    run: rec('run', RUN),
    submissions: rec('submissions', { submissions: SUBMISSIONS }),
    collect: rec('collect', { job: 'collect' }),
    grade: rec('grade', { job: 'grade' }),
    render: rec('render', { job: 'render' }),
    publish: rec('publish', { job: 'publish', ok: 3 }),
    report: rec('report', {
      markdown: '# Report\n\nA student report.\n',
      scorecard: {
        criterion_scores: [
          { criterion: 'c1', points_earned: 8, rating_label: 'Good',
            analysis: 'Solid.', justification: 'Works.', evidence: ['file.py:1'],
            confidence: 'high' },
          { criterion: 'c2', points_earned: 6, rating_label: 'Partial',
            analysis: 'Some gaps.', justification: 'Missed one.', evidence: [],
            confidence: 'medium' },
          { criterion: 'c3', points_earned: 7, rating_label: 'Good',
            analysis: 'Fine.', justification: 'Handled.', evidence: [],
            confidence: 'high' },
        ],
        summary: 'A good submission.',
        flags: [],
        test_results: {},
        model: 'm1',
        agent_session_id: 's1',
        prompt_version: 'v1',
        created_at: '2026-01-01T00:00:00Z',
      },
    }),
    saveReport: rec('saveReport', { ok: true }),
  };
}

/** Boot the renderer module in a DOM with a stubbed bridge. */
async function boot() {
  const html = fs.readFileSync(path.join(RENDERER, 'index.html'), 'utf8');
  const virtualConsole = new VirtualConsole();
  const consoleErrors = [];
  virtualConsole.on('jsdomError', (e) => consoleErrors.push(e.message));
  virtualConsole.on('error', (...a) => consoleErrors.push(a.join(' ')));

  const dom = new JSDOM(html, {
    url: 'http://127.0.0.1:45991/app/',
    runScripts: 'outside-only',
    pretendToBeVisual: true,
    virtualConsole,
  });
  const { window } = dom;

  // The recorder has to exist before app.js evaluates, because a ReferenceError
  // during module evaluation would otherwise be thrown, not reported.
  window.__errs = [];
  window.addEventListener('error', (e) => window.__errs.push(
    (e.error && (e.error.stack || e.error.message)) || String(e.message)));
  window.addEventListener('unhandledrejection', (e) => window.__errs.push(
    `unhandledrejection: ${(e.reason && (e.reason.stack || e.reason.message)) || e.reason}`));

  const api = stubApi();
  window.pipeline = {
    info: () => Promise.resolve({ baseUrl: 'http://127.0.0.1:45991' }),
    restartSidecar: () => Promise.resolve('http://127.0.0.1:45991'),
    openPath: () => Promise.resolve(undefined),
    revealPath: () => Promise.resolve(undefined),
    onMenu: () => {},
    events: () => () => {},
    api,
  };

  // jsdom has no ES module loader, so the real modules are bundled to a single
  // classic script first. The source files themselves are untouched and are what
  // ships; only the test sees the bundle.
  const bundle = esbuild.buildSync({
    entryPoints: [path.join(RENDERER, 'app.js')],
    bundle: true,
    format: 'iife',
    write: false,
    platform: 'browser',
    target: 'es2022',
  });
  window.eval(bundle.outputFiles[0].text);

  await new Promise((r) => setTimeout(r, 60));
  return { window, api, calls, consoleErrors };
}

const tick = (ms = 25) => new Promise((r) => setTimeout(r, ms));

/* ------------------------------------------------------------------ tests */

async function run() {
  console.log('\nrenderer');

  const { window, calls: apiCalls, consoleErrors } = await boot();

  test('the bridge is present', () => {
    assert.ok(window.pipeline && window.pipeline.api, 'window.pipeline.api missing');
  });

  test('the first stage rendered', () => {
    const main = window.document.getElementById('main');
    assert.ok(main && main.children.length > 0, '#main is empty');
  });

  test('a run was loaded so later stages are reachable', () => {
    assert.ok(
      apiCalls.some((c) => c.startsWith('run(')),
      `never called run(); calls: ${apiCalls.join(', ')}`);
  });

  // The regression: every stage, every button. A handler that throws a
  // ReferenceError records it in __errs and this fails.
  const clicked = {};
  for (const label of STAGES) {
    const state = await window.eval(`(() => {
      const btn = [...document.querySelectorAll('.stage-btn')]
        .find((b) => b.textContent.trim().startsWith(${JSON.stringify(label)}));
      if (!btn) return 'missing';
      if (btn.dataset.enabled !== 'true') return 'disabled';
      btn.click();
      return 'clicked';
    })()`);
    if (state !== 'clicked') {
      failures.push({ name: `stage ${label} reachable`, err: new Error(state) });
      console.log(`  FAIL stage ${label} reachable\n       ${state}`);
      continue;
    }
    await tick(40);
    clicked[label] = await window.eval(`(() => {
      const out = [];
      for (const b of document.querySelectorAll('#main button')) {
        if (b.disabled) continue;
        const t = b.textContent.trim().slice(0, 28);
        try { b.click(); out.push(t); } catch (e) { out.push('THREW ' + t + ': ' + e.message); }
      }
      return out;
    })()`);
    await tick(40);
  }
  test('every stage was reachable and had buttons clicked', () => {
    for (const label of STAGES) {
      assert.ok(clicked[label] && clicked[label].length > 0,
        `${label} clicked nothing: ${JSON.stringify(clicked[label])}`);
    }
  });

  // Behaviour, not just presence: a Select all that selects nothing would pass
  // the click-everything sweep above.
  test('Select all selects every unpublished row, then clears them', async () => {
    await window.eval(`(() => {
      const b = [...document.querySelectorAll('.stage-btn')]
        .find((x) => x.textContent.trim().startsWith('Publish'));
      b.click();
    })()`);
    await tick(40);

    const checkedCount = () => window.eval(
      '[...document.querySelectorAll(\'#main input[type=checkbox]\')].filter((c) => c.checked).length');
    const toggle = (prefix) => window.eval(`(() => {
      const b = [...document.querySelectorAll('#main button')]
        .find((x) => x.textContent.trim().startsWith(${JSON.stringify(prefix)}));
      if (!b) return 'missing';
      b.click();
      return 'clicked';
    })()`);

    // Each click re-renders the stage, replacing the DOM, so a node list
    // collected up front goes stale after the first click. Re-query every time.
    for (let i = 0; i < SUBMISSIONS.length + 1; i += 1) {
      const remaining = await window.eval(
        "[...document.querySelectorAll('#main input[type=checkbox]')].filter((c) => c.checked).length");
      if (!remaining) break;
      await window.eval(
        "(() => { const c = [...document.querySelectorAll('#main input[type=checkbox]')]"
        + ".find((x) => x.checked); if (c) c.click(); })()");
      await tick(30);
    }
    assert.strictEqual(await checkedCount(), 0, 'could not start from an empty selection');

    assert.strictEqual(await toggle('Select all'), 'clicked');
    await tick(40);
    const total = SUBMISSIONS.length;
    assert.strictEqual(await checkedCount(), total,
      `Select all checked ${await checkedCount()} of ${total}`);

    assert.strictEqual(await toggle('Clear selection'), 'clicked');
    await tick(40);
    assert.strictEqual(await checkedCount(), 0, 'Clear selection left rows selected');
  });

  test('no handler threw while every button was clicked', () => {
    assert.deepStrictEqual(window.__errs, [],
      `uncaught in renderer:\n  ${window.__errs.join('\n  ')}`);
  });

  test('clicking Collect reached the API', () => {
    assert.ok(apiCalls.some((c) => c.startsWith('collect(')),
      `Collect never called the API; calls: ${apiCalls.join(', ')}`);
  });

  test('no jsdom errors', () => {
    assert.deepStrictEqual(consoleErrors, [], consoleErrors.join('\n'));
  });

  console.log('\nclicks per stage');
  for (const [stage, labels] of Object.entries(clicked)) {
    console.log(`  ${stage.padEnd(8)} ${labels.length}: ${labels.join(', ')}`);
  }

  console.log(`\n${passed} passed, ${failures.length} failed`);
  if (failures.length) process.exit(1);
}

run().catch((err) => {
  console.error('renderer test crashed:', err);
  process.exit(1);
});
