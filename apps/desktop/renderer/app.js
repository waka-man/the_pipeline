/* Grading Pipeline — renderer.
 *
 * One wizard, four stages, plus setup. Every long operation runs as a
 * background job in the sidecar and reports progress over the event stream, so
 * the UI never blocks and never polls.
 */

import {
  h, mount, clear, fmt, clock, chip, statusChip, marksStrip, markdown,
  empty, notice, field, card,
} from './ui.js';

/* Error handling is registered first, before anything touches the preload
   bridge: a module-level throw would otherwise kill the file silently and
   leave a blank window with no explanation. */
function fatalEarly(err) {
  const msg = (err && (err.stack || err.message)) || String(err);
  try {
    const main = document.getElementById('main');
    if (main) {
      main.innerHTML =
        '<div class="head"><h1>Something went wrong</h1>' +
        '<p>The interface could not start.</p></div>' +
        '<div class="notice" data-tone="bad"></div>';
      main.querySelector('.notice').textContent = msg;
    }
  } catch (_) {}
  console.error('[fatal]', msg);
}
window.addEventListener('error', (e) => fatalEarly(e.error || e.message));
window.addEventListener('unhandledrejection', (e) => fatalEarly(e.reason));

const bridge = window.pipeline;
if (!bridge || !bridge.api) {
  fatalEarly('The preload bridge did not load. Restart the app; if it persists ' +
             'the preload script failed to run.');
  throw new Error('preload bridge missing');
}

const api = bridge.api;

/* ----------------------------------------------------------------- state */

// Pagination sizes. Five recent courses is the number that fits without
// scrolling on a laptop, which is the complaint: a faculty member with 40
// courses had to scroll past all of them to reach the one they wanted.
const COURSE_PAGE = 5;
const ASSIGNMENT_PAGE = 8;

const S = {
  stage: 'setup',
  status: null,
  model: null,
  courses: [],
  course: null,
  assignments: [],
  assignment: null,
  run: null,
  submissions: [],
  selected: new Set(),
  current: null,          // { rowId, markdown, scorecard }
  tab: 'read',
  // Slide-over panels. `assign` lists a course's assignments; `log` shows one
  // student's agent output live.
  panel: null,          // null | { kind: 'assign' | 'log', rowId }
  coursesShown: COURSE_PAGE,
  assignmentsShown: ASSIGNMENT_PAGE,
  logLines: [],
  logTimer: null,
  bootstrapping: false,
  editing: false,
  events: [],
  toast: null,
  busy: false,
};

const STAGES = [
  { id: 'setup',    label: 'Setup',     title: 'Set up' },
  { id: 'select',   label: 'Select',    title: 'Choose an assignment' },
  { id: 'collect',  label: 'Collect',   title: 'Collect submissions' },
  { id: 'grade',    label: 'Grade',     title: 'Grade' },
  { id: 'review',   label: 'Review',    title: 'Review reports' },
  { id: 'publish',  label: 'Publish',   title: 'Publish to Canvas' },
];

const els = {
  main: document.getElementById('main'),
  slideover: document.getElementById('slideover-slot'),
  stages: document.getElementById('stages'),
  console: document.getElementById('console'),
  extra: document.getElementById('aside-extra'),
  topRun: document.getElementById('topbar-run'),
  runLabel: document.getElementById('run-label'),
  modelChip: document.getElementById('model-chip'),
  modal: document.getElementById('modal-root'),
  spineMeta: document.getElementById('spine-meta'),
};

/* ------------------------------------------------------------------ utils */

function toast(message, tone) {
  S.toast = message ? { message, tone } : null;
  render();
  if (message) setTimeout(() => { if (S.toast?.message === message) { S.toast = null; render(); } }, 5200);
}

async function guard(fn, label) {
  try {
    S.busy = true; render();
    return await fn();
  } catch (err) {
    toast(`${label}: ${err.message || err}`, 'bad');
    return null;
  } finally {
    S.busy = false; render();
  }
}

const log = (event, data) => {
  S.events.push({ event, data, ts: new Date().toISOString() });
  if (S.events.length > 400) S.events.splice(0, S.events.length - 400);
  const t = toneFor(event, data);
  els.console.append(
    h('div', { class: 'log-line', dataset: t ? { tone: t } : {} },
      h('span', { class: 'log-time' }, clock(new Date().toISOString())),
      h('span', { class: 'log-text' }, describe(event, data))),
  );
  els.console.scrollTop = els.console.scrollHeight;
  while (els.console.children.length > 300) els.console.removeChild(els.console.firstChild);
};

function toneFor(event, d) {
  if (event.endsWith('.error')) return 'bad';
  if (event === 'agent.budget' || event === 'agent.retry') return 'flag';
  if (event === 'agent.done') return 'ok';
  if (event === 'job.start') return 'accent';
  return null;
}

function describe(event, d) {
  switch (event) {
    case 'job.start':    return `${d.job} started${d.stub ? ' (stub)' : ''}`;
    case 'job.done':     return `${d.job} finished${d.message ? ' — ' + d.message : ''}`;
    case 'job.error':    return `${d.job} failed — ${d.message}`;
    case 'collect.progress': return `${d.index}/${d.total} ${d.student}`;
    case 'collect.source':   return `${d.kind} ${d.ok ? 'ready' : 'failed'} — ${d.origin?.slice(0, 60)}`;
    case 'collect.error':    return `${d.student}: ${d.message}`;
    case 'grade.progress':   return `${d.student} — ${d.status}${d.earned != null ? ` ${fmt(d.earned)}/${fmt(d.possible)}` : ''}${d.stub ? ' (stub)' : ''}`;
    case 'grade.error':      return `${d.student}: ${d.message}`;
    case 'agent.start':      return `${d.student} · attempt ${d.attempt}`;
    case 'agent.budget':     return `output budget ${d.from} → ${d.to}`;
    case 'agent.retry':      return `retrying: ${(d.issues || []).slice(0, 2).join('; ')}`;
    case 'agent.done':       return `${d.student} — ${fmt(d.earned)}/${fmt(d.possible)}`;
    case 'agent.warn':       return `${d.student}: ${d.message}`;
    case 'publish.progress': return `${d.student} — ${d.ok ? 'published' : d.message}`;
    case 'run.ready':        return `run ${d.run_id} ready — ${d.title}, ${d.submissions} submissions`;
    default: return event;
  }
}

/* ------------------------------------------------------------- navigation */

function stageEnabled(id) {
  switch (id) {
    case 'setup': case 'select': return true;
    case 'collect': return !!S.run || S.bootstrapping;
    case 'grade': return !!S.run;
    case 'review': return S.submissions.some((s) => s.score !== null);
    case 'publish': return S.submissions.some((s) => s.score !== null);
    default: return false;
  }
}

function stageState(id) {
  const order = STAGES.map((s) => s.id);
  const here = order.indexOf(S.stage);
  const at = order.indexOf(id);
  if (id === S.stage) return 'active';
  if (at < here) return 'done';
  if (S.status?.jobs?.[id]?.status === 'running') return 'active';
  return 'idle';
}

function go(stage) {
  if (!stageEnabled(stage) && stage !== S.stage) return;
  S.stage = stage;
  S.editing = false;
  if (location.hash.slice(1) !== stage) history.replaceState(null, '', `#${stage}`);
  render();
  els.main.focus();
  if (stage === 'review') loadFirstReport();
  if (stage === 'publish') refreshSubmissions();
}

async function refreshSubmissions() {
  if (!S.run) return;
  const data = await api.submissions(S.run.run_id).catch(() => null);
  if (data) S.submissions = data.submissions;
}

/* ------------------------------------------------------------------ views */

function viewSetup() {
  const st = S.status || {};
  const connected = st.canvas_configured;
  const base = h('input', { class: 'input', id: 'cf-base', placeholder: 'https://school.instructure.com' });
  const token = h('input', { class: 'input', id: 'cf-token', type: 'password', placeholder: 'Canvas API token' });
  const show = h('button', {
    class: 'btn', 'data-size': 'sm', 'data-variant': 'ghost',
    onclick: () => { token.type = token.type === 'password' ? 'text' : 'password'; },
  }, 'show');

  const hasKey = !!st.model_key_configured;
  const orKey = h('input', {
    class: 'input', id: 'or-key', type: 'password',
    placeholder: hasKey ? 'A key is already saved' : 'sk-or-v1-...',
    // Never prefilled. The saved value is not sent to the renderer, so this
    // field only ever holds a newly typed key, and clearing it removes it.
    value: '',
  });
  const showOr = h('button', {
    class: 'btn', 'data-size': 'sm', 'data-variant': 'ghost',
    onclick: () => { orKey.type = orKey.type === 'password' ? 'text' : 'password'; },
  }, 'show');

  return h('div', {},
    h('div', { class: 'head' },
      h('h1', {}, 'Set up'),
      h('p', {}, 'Connect to Canvas and confirm which model should do the grading. '
               + 'Everything else is read from the assignment itself.')),

    card('Canvas',
      connected ? 'connected' : 'not connected',
      field('Canvas URL', h('div', { style: 'display:flex;gap:8px' },
        h('div', { style: 'flex:1' }, base), show)),
      field('API token', token,
        h('span', {}, 'Account → Settings → Approved Integrations → New Access Token. ',
          'The token is stored in your system keychain, never in the project.')),

      h('div', { style: 'display:flex;gap:8px;margin-top:4px' },
        h('button', {
          class: 'btn', 'data-variant': 'primary',
          onclick: async () => {
            const body = { canvas_base_url: base.value.trim(), canvas_api_token: token.value.trim() };
            if (!body.canvas_base_url || !body.canvas_api_token) {
              return toast('Enter both the Canvas URL and a token.', 'flag');
            }
            const res = await guard(() => api.saveSettings(body), 'Could not save');
            if (res) { S.status = res; toast('Canvas credentials saved.'); }
          },
        }, 'Save and connect')),

      connected ? h('div', { style: 'margin-top:14px' },
        notice('Connected. You can move on to choosing a course.', 'ok')) : null),

    card('OpenRouter key', hasKey ? 'saved' : 'not set',
      field('API key', h('div', { style: 'display:flex;gap:8px' },
        h('div', { style: 'flex:1' }, orKey), showOr)),
      h('div', { class: 'hint', style: 'margin-top:8px' },
        'Used to authenticate the bundled agent runtime. Free OpenRouter models '
        + 'work without one, so this is only needed for paid models. Clearing the '
        + 'field and saving removes the stored key.'),
      h('div', { style: 'display:flex;gap:8px;margin-top:10px' },
        h('button', {
          class: 'btn', 'data-variant': hasKey ? 'ghost' : 'primary',
          onclick: async () => {
            const res = await guard(
              () => api.saveSettings({ openrouter_api_key: orKey.value.trim() }),
              'Could not save the key');
            if (res) {
              S.status = res;
              orKey.value = '';
              toast(res.model_key_configured
                ? 'Key saved. The agent runtime restarts on the next grading run.'
                : 'Key removed.');
            }
          },
        }, hasKey ? 'Replace or remove key' : 'Save key'))),

    card('Grading model', S.model ? S.model.selected : 'not chosen',
      h('div', { style: 'display:flex;gap:8px;align-items:center;flex-wrap:wrap' },
        h('button', { class: 'btn', onclick: () => guard(loadModels, 'Could not read models') },
          'Refresh models'),
        S.model ? h('span', { class: 'chip', dataset: { tone: 'accent' } },
          `${S.model.selected} · ${S.model.tier} · ${S.model.output_budget} tokens`) : null),
      h('div', { class: 'hint', style: 'margin-top:10px' },
        'Models are read from the bundled agent runtime, which holds the API key. ',
        'Free OpenRouter models are tried first, then low-cost models, then the strongest available.'),
      S.model?.candidates?.length
        ? h('div', { class: 'picklist', style: 'margin-top:12px' },
            S.model.candidates.slice(0, 8).map((c) => h('button', {
              class: 'pick', 'aria-selected': String(c.id === S.model.selected),
              onclick: async () => {
                const r = await guard(() => api.selectModel({ model: c.id }), 'Could not select model');
                if (r) { S.model = { ...S.model, ...r }; toast(`Grading model set to ${r.selected}.`); }
              },
            },
              h('div', {},
                h('div', { class: 'pick-title' }, c.id),
                h('div', { class: 'pick-sub' }, h('span', {}, c.provider))),
              h('div', { class: 'pick-meta' }, chip(c.tier, c.tier === 'free' ? 'accent' : null)))))
        : null),

    h('div', { style: 'margin-top:16px;display:flex;justify-content:flex-end' },
      h('button', {
        class: 'btn', 'data-variant': 'primary', disabled: !connected,
        onclick: () => go('select'),
      }, 'Choose an assignment')),
  );
}

/** A panel that slides in from the right. Used for assignments and the log. */
function slideover(title, subtitle, body, foot, wide) {
  return h('div', {
    class: 'slideover',
    onclick: (e) => { if (e.target.classList.contains('slideover')) closePanel(); },
  },
    h('div', {
      // The log gets more width: agent output lines are long and a 760px
      // terminal wraps them into something hard to follow.
      class: wide ? 'slideover-panel slideover--wide' : 'slideover-panel',
      role: 'dialog', 'aria-label': title,
    },
      h('div', { class: 'slideover-head' },
        h('div', {},
          h('h2', {}, title),
          subtitle ? h('div', { class: 'dim' }, subtitle) : null),
        h('button', { class: 'btn', 'data-size': 'sm', 'data-variant': 'ghost',
                      onclick: closePanel }, 'Close')),
      h('div', { class: 'slideover-body' }, body),
      foot ? h('div', { class: 'slideover-foot' }, foot) : null));
}

function closePanel() {
  if (S.logTimer) { clearInterval(S.logTimer); S.logTimer = null; }
  S.panel = null;
  S.logLines = [];
  render();
}

/** Polled rather than pushed: the sidecar keeps a bounded tail per row, and
 *  only an open panel pays for the polling. */
function openAgentLog(rowId) {
  S.panel = { kind: 'log', rowId };
  S.logLines = [];
  render();
  if (S.logTimer) clearInterval(S.logTimer);
  const poll = async () => {
    const got = await api.agentLog(rowId).catch(() => null);
    if (got && Array.isArray(got.lines)) S.logLines = got.lines;
    if (S.panel && S.panel.kind === 'log') renderPanel();
    else if (S.logTimer) { clearInterval(S.logTimer); S.logTimer = null; }
  };
  poll();
  S.logTimer = setInterval(poll, 1200);
}

function renderPanel() {
  const el = document.getElementById('slideover-slot');
  if (!el) return;
  const panel = S.panel;
  if (!panel) { mount(el); return; }

  if (panel.kind === 'assign') {
    const course = S.course;
    const shown = S.assignments.slice(0, S.assignmentsShown);
    const remaining = S.assignments.length - S.assignmentsShown;
    mount(el, slideover(
      course ? course.name : 'Assignments',
      course ? `course ${course.id}` : null,
      h('div', {},
        h('div', { class: 'picklist' }, shown.length ? shown.map((a) => h('button', {
          class: 'pick', 'aria-selected': String(S.assignment?.id === a.id),
          disabled: !a.published,
          onclick: () => {
            S.assignment = a;
            closePanel();
            toast(`Selected ${a.name}.`);
          },
        },
          h('div', {},
            h('div', { class: 'pick-title' }, a.name),
            h('div', { class: 'pick-sub' },
              h('span', {}, `${a.criteria} criteria`),
              h('span', {}, `${fmt(a.points_possible)} points`),
              a.group ? h('span', {}, 'group') : null)),
          h('div', { class: 'pick-meta' },
            a.needs_grading != null ? h('span', {}, `${a.needs_grading} ungraded`) : null,
            a.has_rubric ? null : chip('no rubric', 'flag'))))
          : h('div', { style: 'padding:12px;color:var(--ink-3)' }, 'No assignments returned.')),
        remaining > 0
          ? h('div', { style: 'margin-top:14px' },
              h('button', {
                class: 'btn',
                onclick: () => { S.assignmentsShown += ASSIGNMENT_PAGE; renderPanel(); },
              }, `Load more (${remaining} remaining)`))
          : null),
      h('button', {
        class: 'btn', 'data-variant': 'primary',
        disabled: !S.assignment || !S.assignment.has_rubric,
        onclick: () => { closePanel(); startRun(); },
      }, 'Use this assignment')));
    return;
  }

  if (panel.kind === 'log') {
    const row = S.submissions.find((x) => x.row_id === panel.rowId);
    mount(el, slideover(
      row ? (row.student || row.student_name || 'Student') : 'Grading agent',
      'Live agent output',
      S.logLines.length
        ? h('pre', { class: 'logview' }, S.logLines.map((line) => h('div', {
            class: line.startsWith('->') ? 'log-tool'
              : (line === 'turn complete' || line.startsWith('agent started')) ? 'log-done' : '',
          }, line)))
        : h('div', { style: 'color:var(--ink-3);font-size:12px' },
            'Waiting for the agent. Output appears here as it runs.'),
      null,
      true));
  }
}

function viewSelect() {
  const courses = S.courses;
  if (!courses.length) {
    return h('div', {},
      h('div', { class: 'head' }, h('h1', {}, 'Choose an assignment')),
      card('Courses', null,
        h('button', { class: 'btn', onclick: () => guard(loadCourses, 'Could not load courses') },
          'Load courses')),
      empty('No courses loaded yet', 'Load your Canvas courses to begin.'));
  }

  // Most recently active first, so the courses a lecturer actually teaches this
  // term are the ones that fit above the fold.
  const active = courses.filter((c) => c.workflow_state === 'available');
  const ordered = [...active, ...courses.filter((c) => c.workflow_state !== 'available')];
  const shown = ordered.slice(0, S.coursesShown);

  const rows = shown.map((c) => h('button', {
    class: 'pick', 'aria-selected': String(S.course?.id === c.id),
    onclick: async () => {
      S.assignment = null;
      S.assignments = [];
      S.assignmentsShown = ASSIGNMENT_PAGE;
      await guard(async () => {
        S.course = c;
        S.coursesShown = Math.max(S.coursesShown, COURSE_PAGE);
        const data = await api.assignments(c.id);
        S.assignments = data.assignments || [];
        // Assignments open in a panel rather than below the course list, so
        // picking a course never pushes the page around under the user.
        S.panel = { kind: 'assign', rowId: null };
      }, 'Could not load assignments');
      render();
    },
  },
    h('div', {},
      h('div', { class: 'pick-title' }, c.name),
      h('div', { class: 'pick-sub' },
        h('span', {}, `course ${c.id}`),
        c.code ? h('span', {}, c.code) : null,
        c.term ? h('span', {}, c.term) : null)),
    h('div', { class: 'pick-meta' },
      c.workflow_state === 'available' ? chip('available') : null,
      h('span', {}, 'open assignments'))));

  return h('div', {},
    h('div', { class: 'head' },
      h('h1', {}, 'Choose an assignment'),
      h('p', {}, 'Pick a course, then choose an assignment from the panel. The rubric and '
               + 'instructions come straight from Canvas — the pipeline derives its criteria, '
               + 'point values and report structure from it.')),

    card('Courses', `${shown.length} of ${ordered.length}`,
      h('div', { class: 'picklist' }, rows),
      ordered.length > S.coursesShown
        ? h('div', { style: 'margin-top:14px' },
            h('button', {
              class: 'btn',
              onclick: () => { S.coursesShown += COURSE_PAGE; render(); },
            }, `Load more (${ordered.length - S.coursesShown} remaining)`))
        : null),

    h('div', { style: 'margin-top:16px;display:flex;justify-content:space-between;gap:8px' },
      h('button', { class: 'btn', onclick: () => go('setup') }, 'Back'),
      S.assignment
        ? h('button', {
            class: 'btn', 'data-variant': 'primary',
            disabled: !S.assignment.has_rubric,
            onclick: startRun,
          }, `Use ${S.assignment.name}`)
        : h('button', { class: 'btn', disabled: true }, 'Choose an assignment first')));
}

function waiting(title, detail) {
  return h('div', { class: 'waiting' },
    h('div', { style: 'font-size:14px;color:var(--ink-1)' }, title),
    h('div', { class: 'waiting-bar' }),
    h('div', { style: 'max-width:420px;font-size:12px;line-height:1.6' }, detail));
}

function viewCollect() {
  const run = S.run;
  if (!run) {
    // A blank "No run, choose an assignment first" right after choosing one
    // reads as a dead end. Bootstrap is genuinely in flight here.
    if (S.bootstrapping) {
      return waiting('Preparing this run',
        'Reading the assignment, rubric and submissions from Canvas. '
        + 'This takes a moment for a large cohort.');
    }
    return empty('No run', 'Choose an assignment first.');
  }
  const progress = run.progress || {};
  const collecting = (S.status?.jobs?.collect?.status || 'idle') !== 'idle';
  const paused = S.status?.jobs?.collect?.status === 'paused';
  const total = progress.total || 0;
  const done = total - (progress.pending || 0) - (progress.collecting || 0);
  const pct = total ? Math.round((done / total) * 100) : 0;

  return h('div', {},
    h('div', { class: 'head' },
      h('h1', {}, 'Collect submissions'),
      h('p', {}, 'Downloads every attachment, clones each repository, and converts documents '
               + 'to Markdown so the grader reads material rather than links.')),

    card(run.title,
      `${run.criteria.length} criteria · ${fmt(run.points_possible)} points`
      + (collecting ? ` · ${paused ? 'paused' : 'running'}` : ''),
      h('div', { class: 'stats', style: 'margin-bottom:16px' },
        h('div', { class: 'stat' }, h('div', { class: 'stat-v' }, total), h('div', { class: 'stat-k' }, 'submissions')),
        h('div', { class: 'stat' }, h('div', { class: 'stat-v' }, progress.collected || 0), h('div', { class: 'stat-k' }, 'collected')),
        h('div', { class: 'stat' }, h('div', { class: 'stat-v' }, progress.no_submission || 0), h('div', { class: 'stat-k' }, 'no submission')),
        h('div', { class: 'stat' }, h('div', { class: 'stat-v' }, progress.collect_failed || 0), h('div', { class: 'stat-k' }, 'failed'))),

      h('div', { class: 'meter', style: 'margin-bottom:16px' },
        h('div', { class: 'meter-fill', style: `width:${pct}%` })),

      h('div', { style: 'display:flex;gap:8px;flex-wrap:wrap' },
        h('button', {
          class: 'btn', 'data-variant': 'primary', disabled: collecting,
          onclick: () => guard(async () => {
            await api.collect(run.run_id, { limit: null });
            toast('Collecting. Progress updates live.');
          }, 'Could not start collection'),
        }, collecting ? 'Collecting…' : 'Collect'),
        collecting ? h('button', {
          class: 'btn',
          onclick: () => guard(
            () => api.control(run.run_id, { job: 'collect', action: paused ? 'resume' : 'pause' }),
            'Could not change the collection'),
        }, paused ? 'Resume' : 'Pause') : null,
        collecting ? h('button', {
          class: 'btn', 'data-variant': 'ghost',
          onclick: () => guard(
            () => api.control(run.run_id, { job: 'collect', action: 'stop' }),
            'Could not stop the collection'),
        }, 'Stop') : null,
        h('button', {
          class: 'btn', disabled: !done,
          onclick: () => go('grade'),
        }, 'Continue to grading'))),

    S.submissions.length ? submissionTable(true) : null);
}

function viewGrade() {
  const run = S.run;
  if (!run) return empty('No run', 'Choose an assignment first.');
  const graded = S.submissions.filter((s) => s.score !== null).length;
  const flagged = S.submissions.filter((s) => (s.flags || []).some((f) => f.severity === 'block')).length;
  const gradeable = S.submissions.filter((s) => s.score === null);
  const grading = (S.status?.jobs?.grade?.status || 'idle') !== 'idle';

  return h('div', {},
    h('div', { class: 'head' },
      h('h1', {}, 'Grade'),
      h('p', {}, 'Each submission is graded in its own agent session, so no student\'s work '
               + 'can influence another\'s. Scores arrive as structured data and are rendered '
               + 'into a report — the agent never formats the page itself.')),

    card('Select submissions',
      `${gradeable.length} awaiting, ${S.selected.size} selected`,
      h('div', { style: 'display:flex;gap:8px;flex-wrap:wrap;align-items:center' },
        h('button', {
          class: 'btn', 'data-size': 'sm', disabled: !gradeable.length,
          onclick: () => {
            const all = gradeable.every((s) => S.selected.has(s.row_id));
            gradeable.forEach((s) => (all ? S.selected.delete(s.row_id) : S.selected.add(s.row_id)));
            render();
          },
        }, gradeable.length && gradeable.every((s) => S.selected.has(s.row_id))
            ? `Clear selection (${gradeable.length})`
            : `Select all ${gradeable.length ? `(${gradeable.length})` : ''}`.trim()),
        h('button', {
          class: 'btn', 'data-size': 'sm', 'data-variant': 'ghost', disabled: !S.selected.size,
          onclick: () => { S.selected.clear(); render(); },
        }, 'Clear'))),

    card('Run grading', S.model?.selected ? `model ${S.model.selected}` : null,
      h('div', { class: 'stats', style: 'margin-bottom:16px' },
        h('div', { class: 'stat' }, h('div', { class: 'stat-v' }, graded), h('div', { class: 'stat-k' }, 'graded')),
        h('div', { class: 'stat' }, h('div', { class: 'stat-v' }, S.submissions.length), h('div', { class: 'stat-k' }, 'collected')),
        h('div', { class: 'stat' },
          h('div', { class: 'stat-v', style: flagged ? 'color:var(--flag)' : '' }, flagged),
          h('div', { class: 'stat-k' }, 'needs attention'))),

      h('div', { style: 'display:flex;gap:8px;flex-wrap:wrap' },
        h('button', {
          class: 'btn', 'data-variant': 'primary',
          disabled: grading || !gradeable.length,
          onclick: () => guard(async () => {
            await api.grade(run.run_id, {});
            toast(`Grading ${gradeable.length} submissions.`);
          }, 'Could not start grading'),
        }, grading ? 'Grading…' : `Grade All (${gradeable.length})`),
        h('button', {
          class: 'btn', disabled: grading || !S.selected.size,
          onclick: () => guard(async () => {
            await api.grade(run.run_id, { row_ids: [...S.selected] });
            toast(`Grading ${S.selected.size} selected.`);
          }, 'Could not start grading'),
        }, `Grade Selected (${S.selected.size})`),
        h('button', {
          class: 'btn',
          title: 'Produce reports without calling a model, to exercise the review and publish paths',
          onclick: () => guard(async () => {
            await api.grade(run.run_id, { stub: true });
            toast('Stub grading started — these are not real marks.');
          }, 'Could not start stub grading'),
        }, 'Run stub grading'),
        h('button', {
          class: 'btn',
          onclick: () => guard(async () => {
            await api.render(run.run_id);
            toast('Rendering reports.');
          }, 'Could not render'),
        }, 'Render reports'),
        h('button', { class: 'btn', disabled: !graded, onclick: () => go('review') },
          'Continue to review'))),

    notice('Posting is always optional. If a run does not hold up you can grade from the reports in your own words.', null),

    S.submissions.length ? submissionTable(false) : null);
}

function viewReview() {
  const run = S.run;
  if (!run) return empty('No run', 'Choose an assignment first.');
  const graded = S.submissions.filter((s) => s.score !== null);
  const blocked = graded.filter((s) => (s.flags || []).some((f) => f.severity === 'block')).length;

  const list = h('div', { class: 'rows' },
    h('div', { class: 'row row-head' },
      h('span', {}, 'Student'),
      h('span', { style: 'text-align:right' }, 'Score')),
    graded.map((s) => h('div', {
      class: 'row', 'data-selected': String(S.current?.rowId === s.row_id),
      onclick: () => openReport(s.row_id),
    },
      h('span', { class: 'row-name', title: s.student }, s.student),
      h('span', { class: 'row-score' },
        fmt(s.score),
        (s.flags || []).some((f) => f.severity === 'block')
          ? h('span', { class: 'dot', style: 'color:var(--flag);margin-left:6px' })
          : null))));

  return h('div', {},
    h('div', { class: 'head' },
      h('h1', {}, 'Review reports'),
      h('p', {}, 'Read what the grader concluded and the evidence behind it. Edit any report '
               + 'before you decide whether it goes back to Canvas.'),
      blocked
        ? h('div', { style: 'margin-top:10px' },
            notice(`${blocked} report${blocked === 1 ? '' : 's'} flagged for a human decision.`,
                   'flag'))
        : null),

    h('div', { class: 'review-split' },
      h('div', { class: 'review-list' },
        h('div', { class: 'card-head' },
          h('h2', {}, 'Cohort'),
          h('span', { class: 'note' }, `${graded.length}`)),
        list),
      h('div', { class: 'review-pane' }, reportPane())));
}

function reportPane() {
  if (!S.current) {
    return h('div', { class: 'card', style: 'min-height:320px;display:grid;place-items:center' },
      empty('Nothing open', 'Choose a student from the cohort to read their report.'));
  }
  const { markdown: md, scorecard, rowId } = S.current;
  const run = S.run;

  const tabs = h('div', { class: 'tabs' },
    h('button', { class: 'tab', 'aria-selected': String(S.tab === 'read'),
      onclick: () => { S.tab = 'read'; render(); } }, 'Read'),
    h('button', { class: 'tab', 'aria-selected': String(S.tab === 'edit'),
      onclick: () => { S.tab = 'edit'; render(); } }, 'Edit source'));

  const body = S.tab === 'read'
    ? h('div', { class: 'prose', html: markdown(md) })
    : h('textarea', {
        class: 'editor', id: 'report-editor',
        oninput: (e) => { S.current.markdown = e.target.value; },
      }, md);

  const saving = S.tab === 'edit' ? h('button', {
    class: 'btn', 'data-variant': 'primary',
    onclick: async () => {
      const res = await guard(() => api.saveReport(rowId, S.current.markdown), 'Could not save');
      if (res) toast('Report saved.');
    },
  }, 'Save report') : null;

  return card(null, null,
    h('div', { class: 'reader-head' },
      h('div', {},
        h('h2', { class: 'reader-title' }, S.current.student),
        h('div', { class: 'reader-meta' },
          scorecard ? chip(`${fmt(scorecard.total_earned)} / ${fmt(run.points_possible)}`, 'accent') : null,
          (scorecard?.flags || []).map((f) => chip(f.code, f.severity === 'block' ? 'flag' : null)),
          h('button', { class: 'rowlink',
            onclick: () => bridge.revealPath(S.current.path) }, 'show file'))),
      h('div', { style: 'display:flex;gap:8px' },
        tabs,
        saving)),
    h('div', { class: 'editor-wrap' }, body));
}

function viewPublish() {
  const run = S.run;
  if (!run) return empty('No run', 'Choose an assignment first.');
  const graded = S.submissions.filter((s) => s.score !== null && !s.published);

  return h('div', {},
    h('div', { class: 'head' },
      h('h1', {}, 'Publish to Canvas'),
      h('p', {}, 'Scores are written per rubric criterion with the justification as the criterion '
               + 'comment. Nothing is written until you confirm the plan.')),

    card('Select submissions',
      `${graded.length} graded, ${graded.filter((s) => S.selected.has(s.row_id)).length} selected`,

      // A full cohort is the normal case, so the common action is one click.
      // Without this the only route is a checkbox per student, which for a
      // 150-submission assignment means 150 clicks.
      h('div', { style: 'display:flex;gap:8px;margin-bottom:10px;flex-wrap:wrap;align-items:center' },
        h('button', {
          class: 'btn', 'data-size': 'sm', disabled: !graded.length,
          onclick: () => {
            const allSelected = graded.length > 0
              && graded.every((s) => S.selected.has(s.row_id));
            if (allSelected) {
              // Drop only the rows on screen, so a selection made elsewhere
              // survives a visit to this stage.
              graded.forEach((s) => S.selected.delete(s.row_id));
            } else {
              graded.forEach((s) => S.selected.add(s.row_id));
            }
            render();
          },
        }, graded.length > 0 && graded.every((s) => S.selected.has(s.row_id))
            ? `Clear selection (${graded.length})`
            : `Select all ${graded.length ? `(${graded.length})` : ''}`.trim()),
        graded.filter((s) => !s.published).length !== graded.length
          ? h('span', { class: 'hint' }, 'Already-published submissions are excluded.')
          : null),

      h('div', { class: 'rows' },
        h('div', { class: 'row row-head' },
          h('span', {}, ''), h('span', {}, 'Student'), h('span', { style: 'text-align:right' }, 'Score'),
          h('span', {}, 'Flags'), h('span', { style: 'text-align:right' }, '')),
        graded.length ? graded.map((s) => h('div', {
          class: 'row', 'data-selected': String(S.selected.has(s.row_id)),
          onclick: (e) => {
            if (e.target.tagName === 'INPUT') return;
            S.selected.has(s.row_id) ? S.selected.delete(s.row_id) : S.selected.add(s.row_id);
            render();
          },
        },
          h('input', {
            type: 'checkbox', checked: S.selected.has(s.row_id),
            onchange: (e) => {
              e.target.checked ? S.selected.add(s.row_id) : S.selected.delete(s.row_id);
              render();
            },
          }),
          h('span', { class: 'row-name' }, s.student),
          h('span', { class: 'row-score' }, fmt(s.score)),
          h('span', {}, (s.flags || []).length ? chip(`${s.flags.length}`, 'flag') : h('span', { style: 'color:var(--ink-3)' }, '—')),
          h('span', {})))
        : h('div', { style: 'padding:14px;color:var(--ink-3)' }, 'Nothing graded yet.')),

      h('div', { style: 'display:flex;gap:8px;margin-top:16px;flex-wrap:wrap' },
        h('button', {
          class: 'btn', 'data-variant': 'primary', disabled: !graded.length,
          onclick: () => guard(async () => {
            if (!S.selected.size) graded.forEach((s) => S.selected.add(s.row_id));
            const plan = await api.publish(run.run_id, { row_ids: [...S.selected] });
            showPublishPlan(plan);
          }, 'Could not plan publish'),
        }, 'Review what will be written'),
        h('button', { class: 'btn', onclick: () => go('review') }, 'Back to reports'))),

    card('What Canvas receives', null,
      h('div', { style: 'font-size:12px;color:var(--ink-2);line-height:1.7' },
        h('div', {}, 'Per criterion: the awarded points, and the grader\'s justification plus evidence as the criterion comment.'),
        h('div', {}, 'Overall: the sum of the criteria as the posted grade.'),
        h('div', {}, 'Group assignments: one write per group, which Canvas propagates to every member.'))));
}

function showPublishPlan(plan) {
  openModal(h('div', { class: 'dialog' },
    h('h2', {}, plan.message || 'Ready to publish'),
    h('p', {}, 'Check each change before it is written. Existing marks are shown for comparison.'),
    ...(plan.entries || []).map((e) => h('div', { class: 'plan-row' },
      h('span', {}, e.student),
      h('span', { class: 'plan-change' },
        e.existing == null ? 'new' : `${fmt(e.existing)} → `,
        h('b', {}, fmt(e.new))),
      (e.blocking || []).length ? chip(`${e.blocking.length} flag`, 'flag') : h('span'))),
    h('div', { class: 'dialog-actions' },
      h('button', { class: 'btn', onclick: closeModal }, 'Cancel'),
      h('button', {
        class: 'btn', 'data-variant': 'primary',
        onclick: async () => {
          closeModal();
          await guard(async () => {
            await api.publish(S.run.run_id, { row_ids: [...S.selected], confirm: true });
            toast('Publishing started.');
          }, 'Could not publish');
        },
      }, 'Write to Canvas'))));
}

/* -------------------------------------------------------------- fragments */

function submissionTable(compact) {
  const rows = S.submissions.map((s) => h('div', {
    class: 'row', 'data-selected': String(S.selected.has(s.row_id)),
  },
    compact ? h('span', { style: 'color:var(--ink-3)' }, '')
      : h('input', {
          type: 'checkbox',
          checked: S.selected.has(s.row_id),
          onchange: (e) => {
            e.target.checked ? S.selected.add(s.row_id) : S.selected.delete(s.row_id);
            render();
          },
        }),
    h('span', { class: 'row-name', title: s.primary_url || '' }, s.student),
    h('span', { class: 'row-score' }, s.score === null ? '—' : fmt(s.score)),
    h('span', {}, statusChip(s.status)),
    h('span', { class: 'row-act', style: 'display:flex;gap:6px;justify-content:flex-end' },
      // While a row is being graded the agent output is the only way to see
      // what it is doing, so that row gets a way in.
      ['grading', 'collected'].includes(s.status) && s.score === null
        ? h('button', {
            class: 'btn', 'data-size': 'sm', 'data-variant': 'ghost',
            onclick: () => openAgentLog(s.row_id),
          }, 'agent log')
        : null,
      s.primary_url ? h('button', {
        class: 'btn', 'data-size': 'sm', 'data-variant': 'ghost',
        onclick: () => window.open(s.primary_url, '_blank'),
      }, 'repo') : null)));
  return h('div', { style: 'margin-top:18px' },
    h('div', { class: 'card-head' }, h('h2', {}, 'Submissions'), h('span', { class: 'note' }, `${S.submissions.length}`)),
    h('div', { class: 'rows' },
      h('div', { class: 'row row-head' },
        h('span', {}), h('span', {}, 'Student'), h('span', { style: 'text-align:right' }, 'Score'),
        h('span', {}, 'Status'), h('span', {})),
      rows));
}

async function loadFirstReport() {
  const first = S.submissions.find((s) => s.score !== null);
  if (first && !S.current) await openReport(first.row_id);
}

async function openReport(rowId) {
  const data = await guard(() => api.report(rowId), 'Could not load report');
  if (!data) return;
  S.current = { rowId, markdown: data.markdown, scorecard: data.scorecard,
                student: data.student, path: data.path };
  S.tab = 'read';
  S.stage = 'review';
  render();
}

async function startRun() {
  const res = await guard(() => api.createRun({
    course_id: S.course.id, assignment_id: S.assignment.id,
  }), 'Could not start the run');
  if (!res) return;
  S.stage = 'collect';
  S.run = null;
  render();
  // The run id arrives with the `run.ready` event, which the stream handler
  // turns into a full run. Nothing to poll for here.
}

/* -------------------------------------------------------------- rendering */

function renderSpine() {
  mount(els.stages, STAGES.map((s) => {
    const enabled = stageEnabled(s.id);
    const st = stageState(s.id);
    const running = S.status?.jobs?.[s.id]?.status === 'running';
    return h('li', { class: 'stage', 'aria-current': String(S.stage === s.id), dataset: { state: st } },
      h('button', {
        class: 'stage-btn', 'data-enabled': String(enabled),
        onclick: () => go(s.id),
      },
        h('span', { class: 'stage-node' }),
        h('span', {}, s.label),
        h('span', { class: 'stage-count' }, running ? '···' : '')));
  }));

  els.spineMeta.textContent = S.run
    ? `${S.run.title}\n${S.run.criteria.length} criteria`
    : 'No assignment selected';
  els.spineMeta.style.whiteSpace = 'pre-line';
}

function renderTopbar() {
  if (S.run) {
    mount(els.topRun, h('span', { class: 'name' }, S.run.title),
      h('span', {}, `${S.run.criteria.length} criteria · ${fmt(S.run.points_possible)} pts`));
    els.runLabel.textContent = S.run.title;
  } else {
    clear(els.topRun);
    els.runLabel.textContent = 'setup';
  }
  mount(els.modelChip, S.model?.selected
    ? chip(S.model.selected, S.model.tier === 'free' ? 'accent' : null) : h('span'));
}

function renderAside() {
  const run = S.run;
  const extra = [];
  document.getElementById('aside-title').textContent =
    ['collect', 'grade'].includes(S.stage) ? 'Activity' : 'Rubric';

  if (run && !['collect', 'grade'].includes(S.stage)) {
    const scores = S.current?.scorecard
      ? Object.fromEntries(S.current.scorecard.criterion_scores.map((c) => [c.criterion, c.points_earned]))
      : null;
    extra.push(card(null, null, marksStrip(run.criteria, scores,
      { flagKeys: (S.current?.scorecard?.flags || []).map((f) => f.code) })));
    if (S.current?.scorecard?.model) {
      extra.push(h('div', { style: 'font-size:11px;color:var(--ink-3);line-height:1.7' },
        `Graded by ${S.current.scorecard.model} · prompt ${S.current.scorecard.prompt_version}`));
    }
  } else if (run) {
    const p = run.progress || {};
    extra.push(h('div', { class: 'stats' },
      h('div', { class: 'stat' }, h('div', { class: 'stat-v' }, p.graded || 0), h('div', { class: 'stat-k' }, 'graded')),
      h('div', { class: 'stat' }, h('div', { class: 'stat-v' }, p.needs_review || 0), h('div', { class: 'stat-k' }, 'flagged')),
      h('div', { class: 'stat' }, h('div', { class: 'stat-v' }, p.collected || 0), h('div', { class: 'stat-k' }, 'collected'))));
  }
  mount(els.extra, ...extra);
}

function render() {
  renderSpine();
  renderTopbar();
  renderAside();

  const views = {
    setup: viewSetup, select: viewSelect, collect: viewCollect,
    grade: viewGrade, review: viewReview, publish: viewPublish,
  };
  const node = (views[S.stage] || viewSetup)();
  const banner = S.toast
    ? h('div', { style: 'margin-bottom:14px' }, notice(S.toast.message, S.toast.tone))
    : null;
  mount(els.main, banner, node);
  // The slide-over lives outside #main so a stage re-render cannot wipe it
  // open, which means it has to be re-rendered explicitly on every paint.
  renderPanel();
}

function openModal(node) { mount(els.modal, h('div', { class: 'scrim' }, node)); }
function closeModal() { clear(els.modal); }

/* ---------------------------------------------------------------- loading */

async function loadStatus() {
  S.status = await api.status().catch(() => null);
}

async function refreshStatus() {
  S.status = await api.status().catch(() => null);
}

async function loadModels() {
  S.model = await api.models().catch(() => null);
  if (S.model && !S.model.selected) {
    const pick = S.model.candidates?.[0];
    if (pick) {
      const r = await api.selectModel({ model: pick.id }).catch(() => null);
      if (r) S.model = { ...S.model, ...r };
    }
  }
}

async function loadCourses() {
  const data = await api.courses().catch(() => null);
  if (data) S.courses = data.courses;
  // Without this the button appears to do nothing: the state changed but
  // nothing was repainted.
  render();
}

/* ------------------------------------------------------------------- init */

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  document.getElementById('theme-toggle').textContent = theme === 'dark' ? 'light' : 'dark';
  try { localStorage.setItem('gp-theme', theme); } catch (_) {}
}

// Progress events arrive per submission. Re-fetching the whole submission list
// on each one is a request per student across a 150-row cohort, so the list
// refresh is coalesced while the counters are applied immediately from the
// event payload.
let submissionsPending = false;
function scheduleSubmissionsRefresh() {
  if (submissionsPending) return;
  submissionsPending = true;
  setTimeout(() => {
    submissionsPending = false;
    if (S.run) refreshSubmissions();
  }, 500);
}

bridge.events(
  (msg) => {
    log(msg.event, msg.data);
    if (msg.event === 'run.ready') {
      S.bootstrapping = false;
      S.run = { run_id: msg.data.run_id };
      refreshRun();
    }
    if (msg.event === 'job.start' && msg.data.job === 'bootstrap') {
      S.bootstrapping = true;
      render();
    }
    // The counters live on the run object, so the meters only moved when
    // something else happened to refresh it. The payload carries them now.
    if (msg.data && msg.data.progress && S.run) {
      S.run.progress = msg.data.progress;
      render();
    }
    if (msg.event === 'job.done') { refreshStatus(); render(); }
    if (msg.event === 'collect.stopped') {
      toast(msg.data.remaining
        ? `Collection stopped with ${msg.data.remaining} remaining.`
        : 'Collection stopped.');
    }
    if (/^(grade|collect|publish|render|agent)\./.test(msg.event) || msg.event === 'job.done') {
      scheduleSubmissionsRefresh();
    }
  },
  () => log('error', { message: 'Lost connection to the service; retrying.' }),
);

async function refreshRun() {
  if (!S.run?.run_id) return;
  S.run = await api.run(S.run.run_id).catch(() => null);
  await refreshSubmissions();
  render();
}

function fatal(err, where) {
  const msg = (err && (err.stack || err.message)) || String(err);
  console.error('[fatal]', where, msg);
  try {
    mount(els.main, h('div', { class: 'head' },
      h('h1', {}, 'Something went wrong'),
      h('p', {}, `The interface stopped while ${where}.`)),
      notice(msg, 'bad'));
  } catch (_) {}
}

window.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && S.panel) closePanel();
});

window.addEventListener('error', (e) => fatal(e.error || e.message, 'loading'));
window.addEventListener('unhandledrejection', (e) => fatal(e.reason, 'loading'));

(async function init() {
 try {
  try { document.documentElement.dataset.platform =
    await bridge.info().then((i) => i.platform); } catch (_) {}

  let theme = new URLSearchParams(location.search).get('theme')
    || (location.hash.startsWith('#light') ? 'light' : null);
  try { theme = theme || localStorage.getItem('gp-theme') || 'dark'; } catch (_) {}
  applyTheme(theme);

  document.getElementById('theme-toggle')?.addEventListener('click', () => {
    applyTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark');
  });
  document.getElementById('aside-clear')?.addEventListener('click', () => {
    S.events = []; clear(els.console);
  });
  document.getElementById('restart-sidecar')?.addEventListener('click', async () => {
    try { await bridge.restartSidecar(); } catch (e) { toast(String(e.message || e), 'bad'); }
  });
  bridge.onMenu((what) => {
    if (what === 'toggle-theme') {
      applyTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark');
    } else if (what === 'open-workspace' && S.status?.workspace) {
      bridge.openPath(S.status.workspace);
    }
  });

  await loadStatus();
  await loadModels();

  // Restore the last stage when the data dir already holds a run, so a reload
  // or a deep link lands somewhere useful instead of on Setup.
  if (S.status?.canvas_configured) {
    try {
      const runs = await api.submissions(1);
      if (runs.submissions.length) {
        S.run = await api.run(1);
        S.submissions = runs.submissions;
      }
    } catch (_) {}
  }

  const wanted = location.hash.slice(1);
  if (wanted && STAGES.some((s) => s.id === wanted) && stageEnabled(wanted)) {
    S.stage = wanted;
    if (wanted === 'review') loadFirstReport();
  } else if (S.run) {
    S.stage = 'review';
    loadFirstReport();
  }

  render();
  if (!S.status?.canvas_configured) await loadCourses();
 } catch (err) {
   fatal(err, 'setting up');
 }
})();