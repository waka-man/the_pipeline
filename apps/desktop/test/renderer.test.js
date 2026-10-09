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

/**
 * Runs a test, awaiting it.
 *
 * Awaiting matters more than it looks. Without it an async test's rejection
 * escapes after the summary has already printed, so the suite reports success
 * while a test failed. That is the same class of invisible failure as the two
 * bugs this file exists to catch.
 */
async function test(name, fn) {
  try {
    await fn();
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

// Mixed statuses on purpose: with everything already graded there is nothing
// for Grade All or Grade Selected to do, so those controls would sit disabled
// and untested.
const SUBMISSIONS = [
  { row_id: 1, student: 'Ada Lovelace', score: 8, status: 'graded', collected: true },
  { row_id: 2, student: 'Grace Hopper', score: null, status: 'collected', collected: true },
  { row_id: 3, student: 'Alan Turing', score: null, status: 'grading', collected: true },
];

/** Mutable so a test can put a job into "running" and exercise its controls. */
const stubState = {
  jobs: {},
  // When true, boot finds no submissions and therefore no run, which is the
  // state a faculty member is in between choosing an assignment and bootstrap
  // finishing.
  noRunYet: false,
  progress: { total: 3, collected: 1, pending: 1, collecting: 1, no_submission: 0,
              collect_failed: 0, graded: 1, needs_review: 0 },
};

const STAGES = ['Setup', 'Select', 'Collect', 'Grade', 'Review', 'Publish'];

// Deliberately more than COURSE_PAGE, so "Load more" has something to load.
const COURSES = Array.from({ length: 11 }, (_, i) => ({
  id: 3130 + i,
  name: `Course ${i + 1}`,
  term: `term${i}`,
  workflow_state: 'available',
}));

const ASSIGNMENTS = Array.from({ length: 14 }, (_, i) => ({
  id: 46805 + i,
  name: `Assignment ${i + 1}`,
  criteria: 5,
  points_possible: 25,
  published: true,
  has_rubric: true,
  needs_grading: 150,
}));

/** Records every API call so a click can be proven to have reached the API. */
const calls = [];

function stubApi() {
  const rec = (name, value) => (...args) => {
    calls.push(`${name}(${JSON.stringify(args.length > 1 ? args[1] : args[0] ?? null)})`);
    return Promise.resolve(typeof value === 'function' ? value(...args) : value);
  };
  return {
    status: rec('status', () => ({ canvas_configured: true, model_key_configured: false,
                                   jobs: stubState.jobs, model: { selected: 'm1' } })),
    saveSettings: rec('saveSettings', { ok: true }),
    models: rec('models', { selected: 'm1', candidates: [{ id: 'm1', name: 'test-model' }] }),
    selectModel: rec('selectModel', { selected: 'm1' }),
    courses: rec('courses', { courses: COURSES }),
    assignments: rec('assignments', { assignments: ASSIGNMENTS }),
    createRun: rec('createRun', RUN),
    run: rec('run', () => ({ ...RUN, progress: stubState.progress })),
    submissions: rec('submissions',
      () => (stubState.noRunYet ? { submissions: [] } : { submissions: SUBMISSIONS })),
    collect: rec('collect', { job: 'collect' }),
    grade: rec('grade', { job: 'grade' }),
    render: rec('render', { job: 'render' }),
    publish: rec('publish', { job: 'publish', ok: 3 }),
    control: rec('control', { job: 'collect', action: 'pause', state: { status: 'paused' } }),
    agentLog: rec('agentLog', { row_id: 1, student: 'Ada Lovelace',
                                 lines: ['agent started (m1, attempt 1)', '-> read', 'turn complete'] }),
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
    // Captured so a test can push a server event and assert the UI reacted.
    events: (onEvent) => {
      window.__emit = onEvent;
      // The renderer polls the agent log on a setInterval. Tracked so a
      // discarded DOM cannot leave a timer running against a dead window.
      window.__timers = new Set();
      const realSetInterval = window.setInterval.bind(window);
      window.setInterval = (fn, ms) => {
        const id = realSetInterval(fn, ms);
        window.__timers.add(id);
        return id;
      };
      window.__stopTimers = () => {
        for (const id of window.__timers) window.clearInterval(id);
        window.__timers.clear();
      };
      return () => {};
    },
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

async function gotoIn(win, stage) {
  await win.eval(`(() => {
    const b = [...document.querySelectorAll('.stage-btn')]
      .find((x) => x.textContent.trim().startsWith(${JSON.stringify(stage)}));
    if (b) b.click();
  })()`);
  await tick(40);
}

/* ------------------------------------------------------------------ tests */

async function run() {
  console.log('\nrenderer');

  // `base` is the renderer used by the click-everything sweep and the
  // assertions about it. The tests below boot their own instances, so they must
  // not share this one: a sweep that has already clicked through every stage
  // leaves no run, no panel and no unsaved course behind for them to observe.
  const base = await boot();
  // Every boot so far, so no timer outlives the run.
  const booted = [base];
  const reboot = async () => {
    const next = await boot();
    booted.push(next);
    return next;
  };
  const stopAllTimers = () => {
    for (const b of booted) {
      try { b.window.__stopTimers?.(); } catch (_) { /* already torn down */ }
    }
  };

  await test('the bridge is present', () => {
    assert.ok(base.window.pipeline && base.window.pipeline.api, 'window.pipeline.api missing');
  });

  await test('the first stage rendered', () => {
    const main = base.window.document.getElementById('main');
    assert.ok(main && main.children.length > 0, '#main is empty');
  });

  await test('a run was loaded so later stages are reachable', () => {
    assert.ok(
      base.calls.some((c) => c.startsWith('run(')),
      `never called run(); calls: ${base.calls.join(', ')}`);
  });

  // The regression: every stage, every button. A handler that throws a
  // ReferenceError records it in __errs and this fails.
  const clicked = {};
  for (const label of STAGES) {
    const state = await base.window.eval(`(() => {
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
    clicked[label] = await base.window.eval(`(() => {
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
  await test('every stage was reachable and had buttons clicked', () => {
    for (const label of STAGES) {
      assert.ok(clicked[label] && clicked[label].length > 0,
        `${label} clicked nothing: ${JSON.stringify(clicked[label])}`);
    }
  });

  // Behaviour, not just presence: a Select all that selects nothing would pass
  // the click-everything sweep above.
  await test('Select all selects every unpublished row, then clears them', async () => {
    await base.window.eval(`(() => {
      const b = [...document.querySelectorAll('.stage-btn')]
        .find((x) => x.textContent.trim().startsWith('Publish'));
      b.click();
    })()`);
    await tick(40);

    const checkedCount = () => base.window.eval(
      '[...document.querySelectorAll(\'#main input[type=checkbox]\')].filter((c) => c.checked).length');
    const toggle = (prefix) => base.window.eval(`(() => {
      const b = [...document.querySelectorAll('#main button')]
        .find((x) => x.textContent.trim().startsWith(${JSON.stringify(prefix)}));
      if (!b) return 'missing';
      b.click();
      return 'clicked';
    })()`);

    // Each click re-renders the stage, replacing the DOM, so a node list
    // collected up front goes stale after the first click. Re-query every time.
    for (let i = 0; i < SUBMISSIONS.length + 1; i += 1) {
      const remaining = await base.window.eval(
        "[...document.querySelectorAll('#main input[type=checkbox]')].filter((c) => c.checked).length");
      if (!remaining) break;
      await base.window.eval(
        "(() => { const c = [...document.querySelectorAll('#main input[type=checkbox]')]"
        + ".find((x) => x.checked); if (c) c.click(); })()");
      await tick(30);
    }
    assert.strictEqual(await checkedCount(), 0, 'could not start from an empty selection');

    // Count what publish actually offers rather than assuming every submission
    // is publishable.
    const total = await base.window.eval(
      "document.querySelectorAll('#main input[type=checkbox]').length");
    assert.ok(total > 0, 'publish offered no rows to select');

    assert.strictEqual(await toggle('Select all'), 'clicked');
    await tick(40);
    assert.strictEqual(await checkedCount(), total,
      `Select all checked ${await checkedCount()} of ${total}`);

    assert.strictEqual(await toggle('Clear selection'), 'clicked');
    await tick(40);
    assert.strictEqual(await checkedCount(), 0, 'Clear selection left rows selected');
  });

  // ---- the controls added for a large-cohort workflow -------------------

  const gotoOn = async (win, stage) => gotoIn(win, stage);

  const buttonTexts = (win) => win.eval(
    "[...document.querySelectorAll('#main button')].map((b) => b.textContent.trim())");

  const clickIn = async (win, scope, prefix) => win.eval(`(() => {
    const b = [...document.querySelectorAll(${JSON.stringify(scope)})]
      .find((x) => x.textContent.trim().startsWith(${JSON.stringify(prefix)}));
    if (!b) return 'missing';
    if (b.disabled) return 'disabled';
    b.click();
    return 'clicked';
  })()`);

  await test('the course list shows five and Load more reveals the rest', async () => {
    const { window } = await reboot();
    await gotoOn(window, 'Select');
    await clickIn(window, '#main button', 'Load courses');
    await tick(60);

    const texts = await buttonTexts(window);
    const loadMore = texts.find((t) => t.startsWith('Load more'));
    assert.ok(loadMore, `expected a Load more button, got: ${texts.join(' | ')}`);
    // 11 courses, five per page, so two pages.
    assert.match(loadMore, /Load more \(6 remaining\)/, loadMore);

    const listed = () => window.eval(
      "document.querySelectorAll('#main .picklist .pick').length");
    assert.strictEqual(await listed(), 5, 'did not start at five courses');

    // Click until exhausted rather than assuming one click is enough: the page
    // size is five and there are eleven courses.
    for (let guardCount = 0; guardCount < 10 && (await listed()) < COURSES.length; guardCount += 1) {
      assert.strictEqual(await clickIn(window, '#main button', 'Load more'), 'clicked');
      await tick(30);
    }
    assert.strictEqual(await listed(), COURSES.length, 'Load more did not load the rest');
  });

  await test('clicking a course opens the assignment panel, not a list below', async () => {
    const { window } = await reboot();
    await gotoOn(window, 'Select');
    await clickIn(window, '#main button', 'Load courses');
    await tick(60);

    assert.strictEqual(await window.eval(
      "document.querySelectorAll('.slideover').length"), 0, 'panel was open already');

    await window.eval("document.querySelectorAll('#main .picklist .pick')[0].click()");
    await tick(80);

    assert.strictEqual(await window.eval(
      "document.querySelectorAll('.slideover').length"), 1, 'the slide-over did not open');
    // Assignments live in the panel, not on the page behind it.
    assert.strictEqual(await window.eval(
      "document.querySelectorAll('.slideover .pick').length"), 8,
      'the panel should start with eight assignments');
    // The course list legitimately uses .picklist on the page, so assert on the
    // assignments themselves not appearing behind the panel.
    const onPage = await window.eval(
      "document.getElementById('main').textContent");
    assert.ok(!onPage.includes(ASSIGNMENTS[0].name),
      'assignments still render on the page behind the panel');

    // The panel has its own Load more.
    const panelCount = () => window.eval(
      "document.querySelectorAll('.slideover .pick').length");
    for (let guardCount = 0; guardCount < 10
      && (await panelCount()) < ASSIGNMENTS.length; guardCount += 1) {
      const more = await window.eval("(() => {"
        + "const b=[...document.querySelectorAll('.slideover button')]"
        + ".find((x)=>x.textContent.trim().startsWith('Load more'));"
        + "if(!b) return 'missing'; b.click(); return 'clicked'; })()");
      assert.strictEqual(more, 'clicked');
      await tick(30);
    }
    assert.strictEqual(await panelCount(), ASSIGNMENTS.length,
      'the panel Load more did not load the remaining assignments');
  });

  await test('Escape closes the slide-over', async () => {
    const { window } = await reboot();
    await gotoOn(window, 'Select');
    await clickIn(window, '#main button', 'Load courses');
    await tick(60);
    await window.eval("document.querySelectorAll('#main .picklist .pick')[0].click()");
    await tick(80);
    assert.strictEqual(await window.eval(
      "document.querySelectorAll('.slideover').length"), 1);
    await window.eval(
      "window.dispatchEvent(new window.KeyboardEvent('keydown', { key: 'Escape' }))");
    await tick(40);
    assert.strictEqual(await window.eval(
      "document.querySelectorAll('.slideover').length"), 0, 'Escape did not close the panel');
  });

  await test('a running collection offers Pause and Stop', async () => {
    const { window, calls: apiCalls } = await reboot();
    stubState.jobs = { collect: { job: 'collect', status: 'running' } };
    // The renderer caches status, so it has to be told to re-read it. A done
    // event is what triggers that in the real app.
    await window.eval(`window.__emit(${JSON.stringify({
      event: 'job.done', data: { job: 'collect', ok: true },
    })})`);
    await tick(80);
    await gotoOn(window, 'Collect');
    const texts = await buttonTexts(window);
    assert.ok(texts.includes('Collecting…'), `expected a running state, got: ${texts.join(' | ')}`);
    assert.ok(texts.includes('Pause'), `no Pause button: ${texts.join(' | ')}`);
    assert.ok(texts.includes('Stop'), `no Stop button: ${texts.join(' | ')}`);

    apiCalls.length = 0;
    assert.strictEqual(await clickIn(window, '#main button', 'Pause'), 'clicked');
    await tick(40);
    assert.ok(apiCalls.some((c) => c.startsWith('control(')
      && c.includes('"action":"pause"')), `Pause did not call control: ${apiCalls.join(', ')}`);

    stubState.jobs = { collect: { job: 'collect', status: 'paused' } };
    await window.eval(`window.__emit(${JSON.stringify({
      event: 'job.done', data: { job: 'collect', ok: true },
    })})`);
    await tick(80);
    await gotoOn(window, 'Collect');
    assert.ok((await buttonTexts(window)).includes('Resume'), 'a paused job needs Resume');

    apiCalls.length = 0;
    await clickIn(window, '#main button', 'Stop');
    await tick(40);
    assert.ok(apiCalls.some((c) => c.startsWith('control(')
      && c.includes('"action":"stop"')), `Stop did not call control: ${apiCalls.join(', ')}`);
    stubState.jobs = {};
  });

  await test('Grade All and Grade Selected send different payloads', async () => {
    const { window, calls: apiCalls } = await reboot();
    stubState.jobs = {};
    await gotoOn(window, 'Grade');

    apiCalls.length = 0;
    await clickIn(window, '#main button', 'Grade All');
    await tick(60);
    const all = apiCalls.filter((c) => c.startsWith('grade(')).pop();
    assert.ok(all, 'Grade All did not call grade');

    // Select one specific row, then grade just that one.
    await gotoOn(window, 'Grade');
    await window.eval("(() => {"
      + "const rows=[...document.querySelectorAll('#main .rows .row')]"
      + ".filter((r)=>r.querySelector('input[type=checkbox]'));"
      + "const cb=rows[0] && rows[0].querySelector('input[type=checkbox]');"
      + "if(cb) cb.click(); })()");
    await tick(60);
    apiCalls.length = 0;
    const clicked = await clickIn(window, '#main button', 'Grade Selected');
    await tick(60);
    assert.strictEqual(clicked, 'clicked', 'Grade Selected was not clickable');
    const sel = apiCalls.filter((c) => c.startsWith('grade(')).pop();
    assert.ok(sel, 'Grade Selected did not call grade');
    assert.match(sel, /"row_ids":\[[0-9]/, `Grade Selected sent no selection: ${sel}`);
  });

  await test('the agent log panel opens and shows the sidecar output', async () => {
    const { window, calls: apiCalls } = await reboot();
    stubState.jobs = { grade: { job: 'grade', status: 'running' } };
    await window.eval(`window.__emit(${JSON.stringify({
      event: 'job.done', data: { job: 'grade', ok: true },
    })})`);
    await tick(80);
    await gotoOn(window, 'Grade');
    const opened = await clickIn(window, '#main button', 'agent log');
    assert.strictEqual(opened, 'clicked', 'no agent log button on a gradable row');
    await tick(80);
    assert.strictEqual(await window.eval(
      "document.querySelectorAll('.slideover').length"), 1, 'the log panel did not open');
    assert.match(await window.eval(
      "document.querySelector('.slideover .logview').textContent"),
      /agent started/, 'the log body was not rendered');
    apiCalls.length = 0;
    await window.eval(
      "window.dispatchEvent(new window.KeyboardEvent('keydown', { key: 'Escape' }))");
    await tick(40);
    stubState.jobs = {};
  });

  await test('a progress event moves the counters without a reload', async () => {
    // The bug from the report: progress lives on the run object and the event
    // handler only refreshed submissions, so the meters sat still until
    // something else reloaded the page.
    const { window } = await reboot();
    await gotoOn(window, 'Collect');

    const emit = async (collected, pending) => {
      const progress = { total: 120, collected, pending, collecting: 1,
                         no_submission: 0, collect_failed: 0, graded: 0, needs_review: 0 };
      await window.eval(`window.__emit(${JSON.stringify({
        event: 'collect.progress',
        data: { run_id: 1, index: collected + 1, total: 120, student: 'Ada Lovelace',
                progress },
      })})`);
      await tick(50);
      return window.eval("document.querySelector('#main .meter-fill').style.width");
    };

    const first = await emit(10, 109);
    const second = await emit(60, 59);
    assert.ok(parseFloat(second) > parseFloat(first),
      `the meter did not advance on a progress event (${first} -> ${second})`);

    // And the counters, not just the bar.
    assert.ok((await window.eval("document.getElementById('main').textContent")).includes('60'),
      'the collected counter did not update');
  });

  await test('a bootstrap in flight shows a waiting state, not a dead end', async () => {
    // A boot that finds no run at all, which is the state right after choosing
    // an assignment and before bootstrap returns.
    stubState.noRunYet = true;
    const W = (await reboot()).window;
    await gotoIn(W, 'Select');
    await clickIn(W, '#main button', 'Load courses');
    await tick(60);
    await W.eval("document.querySelectorAll('#main .picklist .pick')[0].click()");
    await tick(80);
    await clickIn(W, '.slideover button', 'Use this assignment');
    await tick(80);

    // No run yet, but a bootstrap is running: the stage must say so rather than
    // showing the dead-end "No run" message.
    stubState.noRunYet = false;
    await W.eval(`window.__emit(${JSON.stringify({
      event: 'job.start', data: { job: 'bootstrap' },
    })})`);
    await tick(60);
    await gotoIn(W, 'Collect');
    assert.strictEqual(await W.eval("document.querySelectorAll('.waiting').length"), 1,
      'no waiting indicator while bootstrapping');
    assert.doesNotMatch(await W.eval("document.getElementById('main').textContent"),
      /No run/, 'still shows the dead-end No run message');

    await W.eval(`window.__emit(${JSON.stringify({
      event: 'run.ready', data: { run_id: 1, title: 'Assignment' },
    })})`);
    await tick(80);
    assert.strictEqual(await W.eval("document.querySelectorAll('.waiting').length"), 0,
      'waiting state stuck after run.ready');
  });

  await test('no handler threw while every button was clicked', () => {
    assert.deepStrictEqual(base.window.__errs, [],
      `uncaught in renderer:\n  ${base.window.__errs.join('\n  ')}`);
  });

  // Asserted on a fresh renderer rather than on the sweep's. The sweep walks
  // stages in order and the Select stage now opens a slide-over, so by the time
  // it reaches Collect it is clicking through state it has already mutated.
  // "Does this button reach the API" belongs in a clean test.
  await test('clicking Collect reaches the API', async () => {
    const { window, calls: c } = await reboot();
    await gotoOn(window, 'Collect');
    c.length = 0;
    assert.strictEqual(
      await clickIn(window, '#main button', 'Collect'), 'clicked',
      'Collect was missing or disabled on a fresh collect stage');
    await tick(80);
    assert.ok(c.some((x) => x.startsWith('collect(')),
      `Collect did not call the API; calls: ${c.join(', ')}`);
  });

  await test('no jsdom errors', () => {
    assert.deepStrictEqual(base.consoleErrors, [], base.consoleErrors.join('\n'));
  });

  console.log('\nclicks per stage');
  for (const [stage, labels] of Object.entries(clicked)) {
    console.log(`  ${stage.padEnd(8)} ${labels.length}: ${labels.join(', ')}`);
  }

  stopAllTimers();
  console.log(`\n${passed} passed, ${failures.length} failed`);
  // Explicit exit either way. The agent-log poll runs on a setInterval inside
  // the jsdom window, which keeps the event loop alive and hangs a *passing*
  // run. A suite that only exits when it fails is worse than one that fails.
  process.exit(failures.length ? 1 : 0);
}

run().catch((err) => {
  console.error('renderer test crashed:', err);
  process.exit(1);
});
