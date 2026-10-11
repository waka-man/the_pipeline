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

// Shaped like what the sidecar actually records: structured entries with a
// timestamp, because the panel colours by kind and collapses repeats.
let CLOCK = 1791727000;
const entry = (e) => ({ t: (CLOCK += 1), ...e });
const AGENT_LOG_ENTRIES = [
  entry({ kind: 'meta', text: 'agent started (openrouter/test-model, attempt 1)' }),
  entry({ kind: 'think', text: 'The student submitted a draft with the rubric attached.' }),
  entry({ kind: 'tool', tool: 'bash', status: 'completed', text: "grep -rn 'APA' material/" }),
  entry({ kind: 'tool', tool: 'grep', status: 'completed', text: "'APA'" }),
  entry({ kind: 'tool', tool: 'grep', status: 'completed', text: "'APA'" }),
  entry({ kind: 'tool', tool: 'grep', status: 'completed', text: "'APA'" }),
  entry({ kind: 'step', tokens: { input: 1200, output: 340, reasoning: 90 }, cost: 0.0031 }),
  entry({ kind: 'think', text: 'The references need checking against APA 7th.' }),
  entry({ kind: 'error', text: 'credit: 402 insufficient credits', retryable: false }),
];

// Saved runs: what "pick up where you left off" has to show.
const SAVED_RUNS = [
  { id: 1, course_id: 3130, assignment_id: 46805, title: 'Argumentative Essay',
    label: 'Essay marking, week 3', status: 'collected', model: 'openrouter/m2',
    total: 151, graded: 22, published: 0, updated_at: '2026-01-01T00:00:00Z' },
  { id: 2, course_id: 3130, assignment_id: 46806, title: 'Lab report', label: null,
    status: 'created', model: null, total: 30, graded: 0, published: 0,
    updated_at: '2025-12-01T00:00:00Z' },
];

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
    models: rec('models', {
      selected: 'm1',
      // Two candidates, so "pick a different model for this run" is testable.
      candidates: [
        { id: 'm1', name: 'cheap', provider: 'openrouter', tier: 'free' },
        { id: 'm2', name: 'strong', provider: 'openrouter', tier: 'paid' },
      ],
    }),
    selectModel: rec('selectModel', { selected: 'm1' }),
    courses: rec('courses', { courses: COURSES }),
    assignments: rec('assignments', { assignments: ASSIGNMENTS }),
    createRun: rec('createRun', RUN),
    runs: rec('runs', () => ({ runs: SAVED_RUNS })),
    labelRun: rec('labelRun', { run_id: 1, label: 'renamed' }),
    run: rec('run', () => ({ ...RUN, progress: stubState.progress })),
    submissions: rec('submissions',
      () => (stubState.noRunYet ? { submissions: [] } : { submissions: SUBMISSIONS })),
    collect: rec('collect', { job: 'collect' }),
    grade: rec('grade', { job: 'grade' }),
    render: rec('render', { job: 'render' }),
    publish: rec('publish', { job: 'publish', ok: 3 }),
    control: rec('control', { job: 'collect', action: 'pause', state: { status: 'paused' } }),
    agentLog: rec('agentLog', { row_id: 1, student: 'Ada Lovelace',
                                 lines: AGENT_LOG_ENTRIES }),
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
  // The launcher fronts the app at start, so getting to any stage means
  // dismissing it first, exactly as a person would.
  await win.eval(`(() => {
    const b = [...document.querySelectorAll('#main button')]
      .find((x) => x.textContent.trim() === 'Start something new');
    if (b) b.click();
  })()`);
  await tick(20);
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
  // The launcher fronts the app; get past it before walking the stages.
  await base.window.eval(`(() => {
    const b = [...document.querySelectorAll('#main button')]
      .find((x) => x.textContent.trim() === 'Start something new');
    if (b) b.click();
  })()`);
  await tick(60);
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

  await test('the app opens on the workspace picker, not a stage', async () => {
    const { window, calls: c } = await reboot();
    await tick(120);   // saved runs load at boot

    // First thing seen: recent gradings, with where each one got to.
    const head = await window.eval("document.getElementById('main').textContent");
    assert.match(head, /Grading Pipeline/, 'no launcher heading');
    assert.match(head, /Essay marking, week 3/, 'a recent session is missing');
    assert.match(head, /22\/151 graded/, 'progress is missing');
    assert.match(head, /Resume at review/, 'it does not say where it would resume');
    assert.strictEqual(await window.eval(
      "document.querySelectorAll('.stage-btn').length"), 0,
      'the stage rail should not be in front of the launcher');

    // Resuming lands on the stage that run had reached, not always on collect.
    c.length = 0;
    assert.strictEqual(await clickIn(window, '#main .pick', 'Essay marking'), 'clicked');
    await tick(150);
    assert.ok(c.some((x) => x.startsWith('run(1)')), `did not open the run: ${c.join(', ')}`);
    assert.match(await window.eval("document.getElementById('main').textContent"),
      /Review reports/, 'did not resume where the run left off');

    // And a fresh start is one click away.
    const { window: w2 } = await reboot();
    assert.strictEqual(await clickIn(w2, '#main button', 'Start something new'), 'clicked');
    await tick(60);
    assert.strictEqual(await w2.eval("document.querySelectorAll('.stage-btn').length"), 6,
      'dismissing the launcher should reveal the pipeline');
  });

  await test('saved sessions list what exists and can be resumed', async () => {
    const { window, calls: c } = await reboot();
    await gotoOn(window, 'Select');
    await clickIn(window, '#main button', 'Load courses');
    await tick(60);

    assert.strictEqual(await clickIn(window, '#main button', 'Saved sessions'), 'clicked',
      'no way into saved sessions');
    await tick(80);
    assert.strictEqual(await window.eval(
      "document.querySelectorAll('.slideover').length"), 1, 'the panel did not open');

    const text = await window.eval("document.querySelector('.slideover').textContent");
    // Where each one got to, which is the question this exists to answer.
    assert.match(text, /Essay marking, week 3/, 'a named session is missing');
    assert.match(text, /22\/151 graded/, 'progress is missing');
    assert.match(text, /Lab report/, 'an unnamed session should fall back to its title');

    // Resume switches to that run.
    c.length = 0;
    assert.strictEqual(await clickIn(window, '.slideover button', 'Resume'), 'clicked');
    await tick(120);
    assert.ok(c.some((x) => x.startsWith('run(1)')), `Resume did not open the run: ${c.join(', ')}`);
    assert.strictEqual(await window.eval(
      "document.querySelectorAll('.slideover').length"), 0, 'the panel stayed open');

    // Naming one is possible, because a bare assignment title is not enough
    // to tell three marking sessions apart.
    await gotoOn(window, 'Select');
    await clickIn(window, '#main button', 'Saved sessions');
    await tick(80);
    await window.eval("window.prompt = () => 'Essay marking, week 4'");
    assert.strictEqual(await clickIn(window, '.slideover button', 'Rename'), 'clicked');
    await tick(60);
    assert.ok(c.some((x) => x.startsWith('labelRun(')), `Rename did not save: ${c.join(', ')}`);
  });

  await test('a run can pick its own model without changing the saved default', async () => {
    const { window, calls: c } = await reboot();
    await gotoOn(window, 'Grade');

    // The catalogue is behind one control, not ten full-width rows: the list
    // is a panel, so the page keeps its shape.
    assert.strictEqual(await window.eval(
      "document.querySelectorAll('#main .pick').length"), 0,
      'the model list is still filling the page');
    // The model name on the Run grading card is the control: it says which
    // model will be used, and opens the picker. It used to be a whole card of
    // its own whose name disagreed with the one on Run grading.
    const shown = await window.eval(
      "(document.querySelector('#main .model-pick') || {}).textContent || ''");
    assert.match(shown, /m1/, `the model name is not on the Run grading card: ${shown}`);
    assert.strictEqual(await clickIn(window, '#main .model-pick', ''), 'clicked',
      'the model name is not clickable');
    await tick(80);
    assert.strictEqual(await window.eval(
      "document.querySelectorAll('.slideover .pick').length"), 2,
      'the panel should offer every candidate');

    c.length = 0;
    assert.strictEqual(await clickIn(window, '.slideover .pick', 'm2'), 'clicked',
      'no model to pick');
    await tick(60);

    await clickIn(window, '#main button', 'Grade All');
    await tick(80);
    const all = c.filter((x) => x.startsWith('grade(')).pop();
    assert.match(all, /"model":"m2"/, `the chosen model was not sent: ${all}`);

    // And it can be handed back to the saved default, by picking it.
    await gotoOn(window, 'Grade');
    assert.match(await window.eval(
      "(document.querySelector('#main .model-pick') || {}).textContent || ''"),
      /this run/, 'the override is not marked as being for this run only');
    await clickIn(window, '#main .model-pick', '');
    await tick(80);
    assert.strictEqual(await clickIn(window, '.slideover .pick', 'm1'), 'clicked',
      'no way back to the saved default');
    await tick(60);
    c.length = 0;
    await clickIn(window, '#main button', 'Grade All');
    await tick(80);
    const back = c.filter((x) => x.startsWith('grade(')).pop();
    assert.ok(!/"model":/.test(back), `the override was still sent: ${back}`);
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
  });  await test('the agent log shows what the agent was doing, not just verbs', async () => {
    const { window } = await reboot();
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

    const text = await window.eval(
      "document.querySelector('.slideover .logview').textContent");

    // The argument, not the verb alone. A log of bare "-> read" is unreadable.
    assert.match(text, /grep -rn 'APA' material\//, 'the tool argument is missing');
    // The thought process, which is the point of the panel.
    assert.match(text, /rubric attached/, 'the reasoning is missing');
    assert.match(text, /checking against APA 7th/, 'later reasoning is missing');
    // Tokens and cost, per step.
    assert.match(text, /1200 in .*340 out/, 'step tokens are missing');
    // A failure must be visible and must say whether anything follows.
    assert.match(text, /insufficient credits/, 'the failure is missing');
    assert.match(text, /stopped/, 'a non-retryable failure should say so');

    // Timestamps.
    assert.match(text, /\b\d{2}:\d{2}:\d{2}\b/, 'no timestamp on the log');

    // Three identical greps collapse into one row with a count.
    assert.strictEqual(await window.eval(
      "document.querySelectorAll('.slideover .log-tool').length"), 2,
      'four calls, three of them identical, should paint as two rows');
    assert.match(await window.eval(
      "(document.querySelector('.slideover .log-count') || {}).textContent || ''"),
      /3/, 'the repeat count is missing');

    // Kinds are styled apart so a failure is the thing you notice.
    assert.ok(await window.eval("document.querySelectorAll('.slideover .log-bad').length > 0"),
      'the failure is not distinguished');

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

  await test('scrolling up in the log survives a poll', async () => {
    const { window } = await reboot();
    stubState.jobs = { grade: { job: 'grade', status: 'running' } };
    await window.eval(`window.__emit(${JSON.stringify({
      event: 'job.done', data: { job: 'grade', ok: true },
    })})`);
    await tick(80);
    await gotoOn(window, 'Grade');
    assert.strictEqual(await clickIn(window, '#main button', 'agent log'), 'clicked');
    await tick(120);

    // Scroll to the top, the way someone reading the agent's reasoning does.
    await window.eval(`(() => {
      const b = document.querySelector('.slideover-body');
      b.scrollTop = 0;
    })()`);
    const before = await window.eval(
      "document.querySelector('.slideover-body').scrollTop");

    // A poll that brings nothing new must not move the reader.
    await window.eval(`window.__emit(${JSON.stringify({
      event: 'job.done', data: { job: 'grade', ok: true },
    })})`);
    await tick(120);
    assert.strictEqual(await window.eval(
      "document.querySelector('.slideover-body').scrollTop"), before,
      'the log snapped away from where the reader was');

    // New output arriving should still not yank them to the end.
    await window.eval(`(() => {
      window.pipeline.api.agentLog = () => Promise.resolve({
        row_id: 1, student: 'Ada Lovelace',
        lines: Array.from({ length: 400 }, (_, i) => ({
          t: 1791727000 + i, kind: 'think', text: 'line ' + i,
        })),
      });
    })()`);
    await tick(1500);
    assert.strictEqual(await window.eval(
      "document.querySelector('.slideover-body').scrollTop"), before,
      'new output snapped the view to the end');
    stubState.jobs = {};
  });

  await test('select all lives in the table header, not its own card', async () => {
    const { window } = await reboot();
    await gotoOn(window, 'Grade');

    assert.strictEqual(await window.eval(
      "[...document.querySelectorAll('#main .card-head h2')]"
      + ".some((h) => h.textContent === 'Select submissions')"), false,
      'the Select submissions card is still there');
    assert.strictEqual(await window.eval(
      "document.querySelectorAll('.row-head .pick-all').length"), 1,
      'no select-all in the table header');

    // It works, and reports a half-ticked state when part of the cohort is chosen.
    await window.eval("document.querySelector('.row-head .pick-all').click()");
    await tick(60);
    assert.strictEqual(await window.eval(
      "document.querySelectorAll('#main input[type=checkbox]:checked').length"), 3,
      'select all did not tick the gradeable rows');

    await window.eval("document.querySelector('.row-head .pick-all').click()");
    await tick(60);
    assert.strictEqual(await window.eval(
      "document.querySelectorAll('#main input[type=checkbox]:checked').length"), 0,
      'clearing left rows ticked');
  });

  await test('the grade button stops saying Grading once the job is done', async () => {
    const { window } = await reboot();
    stubState.jobs = { grade: { job: 'grade', status: 'running' } };
    await window.eval(`window.__emit(${JSON.stringify({
      event: 'job.done', data: { job: 'grade', ok: true },
    })})`);
    await tick(80);
    await gotoOn(window, 'Grade');
    let texts = await window.eval(
      "JSON.stringify([...document.querySelectorAll('#main button')].map((b) => b.textContent.trim()))");
    assert.ok(JSON.parse(texts).includes('Grading\u2026'), `expected a running state: ${texts}`);

    // job_state reports "done" for a finished job, which is not "idle": reading
    // it as "not idle" left the button claiming to grade for the rest of time.
    stubState.jobs = { grade: { job: 'grade', status: 'done' } };
    await window.eval(`window.__emit(${JSON.stringify({
      event: 'job.done', data: { job: 'grade', ok: true },
    })})`);
    await tick(80);
    texts = await window.eval(
      "JSON.stringify([...document.querySelectorAll('#main button')].map((b) => b.textContent.trim()))");
    assert.ok(!JSON.parse(texts).includes('Grading\u2026'),
      `the button still says it is grading: ${texts}`);
    assert.ok(JSON.parse(texts).some((t) => t.startsWith('Grade All')),
      'and the button to grade again is missing');
    stubState.jobs = {};
  });

  await test('the launcher does not show Activity for a run you have not picked', async () => {
    const { window } = await reboot();

    // On the launcher there is no run yet, so the aside has nothing to describe.
    // Leaving the previous run's counts there showed "36 graded" against a
    // screen whose only job is to choose a run, which reads as this run's
    // progress and is simply wrong.
    assert.strictEqual(await window.eval(
      "document.querySelector('#aside-title').textContent.trim()"), '',
      'the launcher titled the aside anyway');
    assert.strictEqual(await window.eval(
      "document.querySelector('#aside-extra').textContent.trim()"), '',
      'the launcher showed the previous run\'s counts');

    // And the aside must come back once a run is actually open.
    await gotoOn(window, 'Grade');
    assert.strictEqual(await window.eval(
      "document.querySelector('#aside-title').textContent.trim()"), 'Activity',
    'the aside did not come back on a real run');
  });

  await test('the grade stage offers no stub grading and no render buttons', async () => {
    const { window } = await reboot();
    await gotoOn(window, 'Grade');

    // Stub grading produced marks that were not real marks, on a screen whose
    // output is a grade that gets posted to a real course. Render reports
    // duplicated the review page, which renders on read anyway.
    const texts = await buttonTexts(window);
    for (const gone of ['Run stub grading', 'Render reports']) {
      assert.ok(!texts.some((t) => t.startsWith(gone)), `${gone} is still offered`);
    }
    assert.ok(texts.some((t) => t.startsWith('Grade All')),
      'the real grading controls should still be there');
  });

  await test('a report shows the earned total, not a dash', async () => {
    const { window } = await reboot();
    await gotoOn(window, 'Review');

    // The stub returns criterion scores summing to 8 + 6 + 7 over 25 possible.
    // `total_earned` is a property on the model and properties do not survive
    // model_dump, so reading it off the response gave undefined and the header
    // rendered "— / 25" above every report.
    const chip = await window.eval(
      "(document.querySelector('.reader-meta') || {}).textContent || ''");
    assert.match(chip, /\b21\s*\/\s*25(?![\d.])/,
      `expected 21 / 25 in the report header, got: ${JSON.stringify(chip)}`);
  });

  await test('every CSS custom property the stylesheet uses is defined', () => {
    // A border written as `var(--line)` when the token is `--rule` is not a
    // visible error: the declaration is invalid, the element silently gets no
    // border, and a checkbox with no border is an invisible checkbox. Nothing
    // else catches that.
    const css = fs.readFileSync(path.join(RENDERER, 'styles.css'), 'utf8');
    const used = new Set([...css.matchAll(/var\((--[a-z0-9-]+)/g)].map((m) => m[1]));
    const missing = [...used].filter(
      (name) => !new RegExp(`^\\s*${name}\\s*:`, 'm').test(css));
    assert.deepStrictEqual(missing, [], `undefined tokens: ${missing.join(', ')}`);
  });

  await test('a row graded while selected can still be deselected', async () => {
    const { window } = await reboot();
    await gotoOn(window, 'Grade');

    // Tick an ungraded row, then have it come back with a mark on it, which is
    // exactly what happens when grading is still running: the list refreshes
    // with a score on a row that was ticked a moment earlier.
    await window.eval(`(() => {
      const rows = [...document.querySelectorAll('.rows .row:not(.row-head)')];
      const row = rows.find((r) => r.querySelector('input[type=checkbox]'));
      row.querySelector('input[type=checkbox]').click();
    })()`);
    await tick(40);
    const before = (await buttonTexts(window)).find((t) => t.startsWith('Grade Selected'));
    assert.match(before, /Grade Selected \(1\)/, `expected 1 selected, got: ${before}`);

    // The row now has a score, so its checkbox is gone.
    await window.eval(`(() => {
      window.pipeline.api.submissions = () => Promise.resolve({ submissions: [
        { row_id: 1, student: 'Ada Lovelace', score: 8, status: 'graded', collected: true },
        { row_id: 2, student: 'Grace Hopper', score: 17, status: 'graded', collected: true },
        { row_id: 3, student: 'Alan Turing', score: null, status: 'collected', collected: true },
      ] });
      window.pipeline.api.run = () => Promise.resolve({ ...({
        run_id: 1, course_id: 3130, assignment_id: 46805, title: 'Assignment',
        status: 'graded', points_possible: 25,
        criteria: [{ id: 'c1', name: 'Correct extraction', points: 10 },
                   { id: 'c2', name: 'Edge cases', points: 8 },
                   { id: 'c3', name: 'Security awareness', points: 7 }],
      }), progress: { graded: 2, collected: 3, total: 3 } });
    })()`);
    await window.eval(`window.dispatchEvent(new Event('focus'))`);
    await window.eval(`(() => { document.querySelector('#main button'); })()`);
    await tick(200);

    // Ada still counts as selected, and there must be a way to take her out.
    const stillSelected = await window.eval(
      "[...document.querySelectorAll('#main button')].map((b) => b.textContent.trim())"
      + ".find((t) => t.startsWith('Grade Selected'))");
    assert.match(String(stillSelected), /Grade Selected \(1\)/,
      `the graded row fell out of the count entirely: ${stillSelected}`);

    const unticked = await window.eval(`(() => {
      const rows = [...document.querySelectorAll('.rows .row:not(.row-head)')];
      const row = rows.find((r) => r.textContent.includes('Grace Hopper'));
      if (!row) return 'no-row';
      const box = row.querySelector('input[type=checkbox]');
      if (box) { box.click(); return 'checkbox'; }
      row.click();
      return 'row';
    })()`);
    assert.ok(['checkbox', 'row'].includes(unticked), `nothing to click: ${unticked}`);
    await tick(60);

    const after = (await buttonTexts(window)).find((t) => t.startsWith('Grade Selected'));
    assert.match(after, /Grade Selected \(0\)/,
      `clicking the graded row did not clear the selection: ${after}`);
  });

  await test('clicking a row toggles it without disturbing its buttons', async () => {
    const { window } = await reboot();
    await gotoOn(window, 'Grade');

    // The row is the hit target now, so a click that lands on the agent-log
    // button must reach the button and not also select the row.
    await window.eval(`(() => {
      const rows = [...document.querySelectorAll('.rows .row:not(.row-head)')];
      const row = rows.find((r) => r.querySelector('button'));
      row.querySelector('button').click();
    })()`);
    await tick(60);
    const texts = await buttonTexts(window);
    const sel = texts.find((t) => t.startsWith('Grade Selected'));
    assert.match(sel, /Grade Selected \(0\)/,
      `a button click also toggled the row: ${sel}`);
  });

  await test('the grading controls stay on screen while scrolling the list', async () => {
    const { window } = await reboot();
    await gotoOn(window, 'Grade');

    // With 150 students you tick someone at the bottom of the list, and the
    // only control that can act on that tick is at the top. Scrolling back up
    // every time is the cost this removes.
    const inside = await window.eval(`(() => {
      const bar = document.querySelector('.sticky-acts');
      if (!bar) return 'no-bar';
      const btns = [...bar.querySelectorAll('button')].map((b) => b.textContent.trim());
      return btns.some((t) => t.startsWith('Grade Selected')) ? 'ok' : btns.join(' | ');
    })()`);
    assert.strictEqual(inside, 'ok', `Grade Selected is not inside the sticky bar: ${inside}`);

    // And the bar is actually sticky rather than merely present: jsdom has no
    // layout, so the declaration is what can be asserted here.
    const css = fs.readFileSync(path.join(RENDERER, 'styles.css'), 'utf8');
    const rule = css.match(/\.sticky-acts\s*\{[^}]*\}/);
    assert.ok(rule, 'no .sticky-acts rule in the stylesheet');
    assert.match(rule[0], /position:\s*sticky/, rule[0]);
  });

  await test('the log keeps updating through a quiet stretch', async () => {
    const { window } = await reboot();
    await gotoOn(window, 'Grade');

    // The poller used to stop on the first poll that brought nothing new, on the
    // assumption that a quiet log means a finished one. A model reasoning
    // through a long submission goes quiet for much longer than the interval,
    // so the panel updated for the first few entries and then froze until it was
    // closed and reopened.
    await window.eval(`(() => {
      let calls = 0;
      window.__logCalls = () => calls;
      window.pipeline.api.agentLog = () => {
        calls += 1;
        // Quiet for the first three polls, then more output.
        if (calls <= 3) return Promise.resolve({ row_id: 1, student: 'Ada Lovelace',
          lines: [{ t: 1, kind: 'think', text: 'only line' }] });
        return Promise.resolve({ row_id: 1, student: 'Ada Lovelace',
          lines: [{ t: 1, kind: 'think', text: 'only line' },
                  { t: 2, kind: 'think', text: 'much later' }] });
      };
    })()`);

    await window.eval(`(() => {
      const rows = [...document.querySelectorAll('.rows .row:not(.row-head)')];
      rows.find((r) => r.querySelector('button')).querySelector('button').click();
    })()`);
    // Long enough for several poll intervals at 1200ms.
    await tick(4200);

    const calls = await window.eval('window.__logCalls()');
    assert.ok(calls >= 4, `polling stopped after ${calls} call(s): a quiet log is not a finished one`);
    const text = await window.eval(
      "(document.querySelector('.slideover-body') || {}).textContent || ''");
    assert.match(text, /much later/,
      'the panel never picked up the output that arrived after a quiet stretch');
});

await test('the log panel offers Stop while a submission is being graded', async () => {
    const { window } = await reboot();
    await gotoOn(window, 'Grade');

    // Open the log of the row that is mid-grade. The fixture has one in the
    // `grading` state, which is the only state where stopping means anything.
    await window.eval(`(() => {
      const rows = [...document.querySelectorAll('.rows .row:not(.row-head)')];
      const row = rows.find((r) => r.textContent.includes('Alan Turing'));
      [...row.querySelectorAll('button')].find((b) => b.textContent.trim() === 'agent log').click();
    })()`);
    await tick(80);

    assert.ok(await window.eval(
      "[...document.querySelectorAll('.slideover-foot button, .slideover button')]"
      + ".some((b) => b.textContent.trim() === 'Stop')"),
      'no Stop button on a submission that is being graded');

    // Clicking it reaches the API.
    await window.eval(`(() => {
      window.__stops = 0;
      window.pipeline.api.stopSubmission = () => { window.__stops += 1; return Promise.resolve({}); };
      [...document.querySelectorAll('button')].find((b) => b.textContent.trim() === 'Stop').click();
    })()`);
    await tick(80);
    assert.strictEqual(await window.eval('window.__stops'), 1, 'Stop did not reach the API');

    // And a row that is not being graded does not offer it.
    await window.eval(`(() => {
      [...document.querySelectorAll('button')].find((b) => b.textContent.trim() === 'Close').click();
    })()`);
    await tick(60);
    await window.eval(`(() => {
      const rows = [...document.querySelectorAll('.rows .row:not(.row-head)')];
      const row = rows.find((r) => r.textContent.includes('Ada Lovelace'));
      [...row.querySelectorAll('button')].find((b) => b.textContent.trim() === 'agent log').click();
    })()`);
    await tick(80);
    assert.ok(!await window.eval(
      "[...document.querySelectorAll('button')].some((b) => b.textContent.trim() === 'Stop')"),
    'Stop is offered on a row that has already finished');
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
