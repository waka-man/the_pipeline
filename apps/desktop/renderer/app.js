/* Grading Pipeline — renderer.
 *
 * One wizard, four stages, plus setup. Every long operation runs as a
 * background job in the sidecar and reports progress over the event stream, so
 * the UI never blocks and never polls.
 */

import {
  h, mount, clear, fmt, clock, chip, statusChip, marksStrip, markdown,
  empty, notice, field, card, icon,
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

/* Browser (Docker) fallback. Without Electron there is no preload script, so
 * window.pipeline does not exist and the renderer would die here. The sidecar
 * serves both this UI (/app/) and the API on one origin, so the same client
 * contract as preload.js can be implemented with plain fetch + EventSource.
 * Desktop-only actions (opening a host folder, restarting the sidecar) have
 * no browser equivalent and degrade to no-ops: in Docker the workspaces live
 * inside the container, and restart means restarting the container. */
function installWebBridge() {
  async function req(method, path, body) {
    const res = await fetch(path, {
      method,
      headers: body ? { 'Content-Type': 'application/json' } : undefined,
      body: body ? JSON.stringify(body) : undefined,
    });
    const text = await res.text();
    let data;
    try { data = text ? JSON.parse(text) : null; } catch (_) { data = { error: text }; }
    if (!res.ok) {
      const err = new Error((data && data.error) || `${res.status} ${res.statusText}`);
      err.status = res.status;
      err.payload = data;
      throw err;
    }
    return data;
  }
  window.pipeline = {
    info: async () => ({ baseUrl: '', platform: 'web' }),
    restartSidecar: async () => {
      throw new Error('Restart is unavailable in the browser build. Restart the container instead.');
    },
    openPath: async () => {},
    revealPath: async () => {},
    onMenu: () => {},
    api: {
      status: () => req('GET', '/api/status'),
      saveSettings: (b) => req('POST', '/api/settings', b),
      models: () => req('GET', '/api/models'),
      selectModel: (b) => req('POST', '/api/models/select', b),
      courses: () => req('GET', '/api/courses'),
      assignments: (id) => req('GET', `/api/courses/${id}/assignments`),
      createRun: (b) => req('POST', '/api/runs', b),
      runs: () => req('GET', '/api/runs'),
      labelRun: (id, label) => req('PUT', `/api/runs/${id}`, { label }),
      deleteRun: (id) => req('DELETE', `/api/runs/${id}`),
      resetGrades: (id, row_ids) => req('POST', `/api/runs/${id}/reset-grades`,
        row_ids ? { row_ids } : {}),
      run: (id) => req('GET', `/api/runs/${id}`),
      submissions: (id) => req('GET', `/api/runs/${id}/submissions`),
      collect: (id, b) => req('POST', `/api/runs/${id}/collect`, b || {}),
      grade: (id, b) => req('POST', `/api/runs/${id}/grade`, b || {}),
      render: (id) => req('POST', `/api/runs/${id}/render`, {}),
      publish: (id, b) => req('POST', `/api/runs/${id}/publish`, b),
      control: (id, b) => req('POST', `/api/runs/${id}/control`, b),
      agentLog: (row) => req('GET', `/api/reports/${row}/agent-log`),
      stopSubmission: (row) => req('POST', `/api/submissions/${row}/stop`),
      steerSubmission: (row, message) => req('POST', `/api/submissions/${row}/steer`, { message }),
      runtime: () => req('GET', '/api/runtime'),
      checkRuntime: () => req('POST', '/api/runtime/check'),
      updateRuntime: () => req('POST', '/api/runtime/update'),
      report: (row) => req('GET', `/api/reports/${row}`),
      saveReport: (row, markdown) => req('PUT', `/api/reports/${row}`, { markdown }),
    },
    events(onEvent, onError) {
      let closed = false;
      let source = null;
      let retry = null;
      const open = () => {
        if (closed) return;
        source = new EventSource('/api/events');
        source.onmessage = (ev) => {
          try { onEvent(JSON.parse(ev.data)); } catch (_) {}
        };
        source.onerror = () => {
          if (closed) return;
          if (source) source.close();
          if (onError) onError();
          retry = setTimeout(open, 2500);
        };
      };
      open();
      return () => {
        closed = true;
        if (retry) clearTimeout(retry);
        if (source) source.close();
      };
    },
  };
}
if (!window.pipeline) installWebBridge();

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
// How many log rows to paint at once. A long run is thousands of lines.
const LOG_ROWS = 250;

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
  // True while the course list is being fetched, so the button can say so
  // instead of sitting inert through a slow request.
  loadingCourses: false,
  // Same treatment for the slower setup actions: reading the model list
  // starts the agent runtime, which takes seconds, and saving a key
  // restarts it. Without a busy state both buttons read as dead.
  loadingModels: false,
  savingKey: false,
  modelError: null,
  // Credential fields, held here because render() rebuilds the inputs and a
  // value living only in the DOM would be lost on the next repaint.
  form: { canvasBase: '', canvasToken: '', openrouterKey: '' },
  // The bundled agent runtime's version. Read when the setup page is first
  // shown: answering it runs the bundled opencode, which has no business
  // happening while the app is still starting.
  runtime: null,
  runtimeRequested: false,
  // True while a version check is in flight, so the card can say so instead
  // of claiming to be checking for ever.
  runtimeChecking: false,
  assignmentsShown: ASSIGNMENT_PAGE,
  // True while a course's assignments are being fetched, so the panel can
  // say so. Without it the click on a course did nothing visible until the
  // request came back, which reads as a frozen window.
  loadingAssignments: false,
  assignmentsError: null,
  logEntries: [],
  logShown: 0,
  logTimer: null,
  // Review list filter: the whole cohort, or only what needs a human.
  reviewFilter: 'all',
  // Rows the person has asked to stop, so the button does not offer it twice.
  stoppingRows: new Set(),
  // Typed-but-unsent steering instructions, keyed by row. The log panel is
  // re-mounted on every poll tick; without the draft the text would vanish
  // and the caret would jump on each new log line.
  steerDraft: {},
  steerFocused: false,
  bootstrapping: false,
  // Model for the next run only. Empty means 'use the saved default'.
  runModel: '',
  savedRuns: [],
  // The session being renamed in the saved-sessions panel, and the name
  // being typed. Renaming used to open a browser prompt, which looked like
  // another program's dialog in the middle of this one.
  renamingRun: null,
  renameDraft: '',
  // The launcher shows until a session is chosen.
  launching: true,
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
  modal: document.getElementById('modal-root'),
  spineMeta: document.getElementById('spine-meta'),
};

/* Whether the main column has been scrolled past its first screenful.
   The sticky control bar answers this by turning translucent, so the rows
   underneath stay visible while it is pinned over them. */
let mainScrolled = false;

/* Which assignment request is the current one. A course clicked while another
   is still loading supersedes it, and the older response is discarded. */
let assignToken = 0;

function syncSticky() {
  const bar = document.querySelector('.sticky-acts');
  if (bar) bar.dataset.scrolled = String(mainScrolled);
}

els.main.addEventListener('scroll', () => {
  const next = els.main.scrollTop > 8;
  if (next === mainScrolled) return;
  mainScrolled = next;
  syncSticky();
}, { passive: true });

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
    // The status code is the useful part. "Could not load courses: 401" says
    // the credentials were rejected; the bare message often does not.
    const detail = err.status ? ` (${err.status})` : '';
    const cause = err.cause ? `: ${err.cause}` : '';
    toast(`${label}${detail}: ${err.message || err}${cause}`, 'bad');
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
  if (event === 'agent.steer') return 'accent';
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
    case 'agent.steer':      return `steered: ${d.message || ''}`;
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

/** Points earned, from the scorecard as the API sends it.
 *
 * Not `scorecard.total_earned`: that is a property on the model, and properties
 * do not survive model_dump, so the field never arrives and the total renders
 * as a dash above every report. The criterion scores are in the dump.
 */
function earnedOf(scorecard) {
  if (!scorecard || !Array.isArray(scorecard.criterion_scores)) return null;
  return scorecard.criterion_scores.reduce((sum, c) => sum + (c.points_earned || 0), 0);
}

/**
 * The model this run grades with, shown as the note on the Run grading card.
 *
 * One control that both says which model will be used and opens the picker.
 * This used to be a whole card of its own, whose name disagreed with the one on
 * Run grading -- it showed the run's model and that one showed the saved
 * default, so the two sat a few lines apart naming different models for the
 * same job.
 *
 * Kept as the run's model, falling back to the saved default, because that is
 * what the next press of Grade All will actually use.
 */
function modelNote() {
  const chosen = S.runModel || S.model?.selected || null;
  const label = chosen || 'no model selected';
  const differs = !!S.runModel && S.runModel !== S.model?.selected;
  const button = h('button', {
    class: 'model-pick',
    // The full id is in the tooltip because the label is allowed to be
    // shortened when the window is narrow.
    title: `${label} — choose the model this run grades with`,
    onclick: () => guard(async () => {
      await loadModels();
      S.panel = { kind: 'model', rowId: null };
      render();
    }, 'Could not read models'),
  },
    h('span', { class: 'model-pick-label' }, label),
    differs ? h('span', { class: 'chip', dataset: { tone: 'accent' } }, 'this run') : null);
  if (!differs) return button;
  // The saved default is context, not part of the control, so it sits on its
  // own line. Inside the button it used to wrap mid-model-name and push the
  // card wider than the window.
  return h('div', { class: 'model-note' },
    button,
    h('div', { class: 'model-pick-hint' },
      'saved default: ' + (S.model ? S.model.selected : 'none saved')));
}

/* ------------------------------------------------------------------ views */

/** What version of the agent runtime is installed, and whether one is newer.
 *
 *  The sidecar starts its probe in the background the first time anyone asks,
 *  so the first answer says "not checked yet" and the real one arrives a
 *  moment later. Reading it once left the card claiming to be checking for
 *  ever, which it was not.
 */
async function loadRuntime() {
  S.runtimeChecking = true;
  render();
  try {
    S.runtime = await api.runtime();
  } catch (_) {
    S.runtime = null;
  }
  for (let tries = 0; tries < 30 && S.runtime && !S.runtime.checked; tries += 1) {
    await new Promise((r) => setTimeout(r, 1000));
    try {
      S.runtime = await api.runtime();
    } catch (_) {
      break;
    }
  }
  S.runtimeChecking = false;
  render();
}

/** The runtime's version, without the tool's name in front of it.
 *
 *  `opencode --version` answers "opencode v2.2.6", and the card was showing
 *  that whole string with a "v" of its own in front: "vopencode v2.2.6".
 */
function versionLabel(version) {
  return String(version || '')
    .replace(/^opencode\s+/i, '')
    .replace(/^v/i, '')
    .trim();
}

function runtimeCard() {
  const rt = S.runtime;
  const downloading = rt && rt.downloading;
  const active = versionLabel(rt?.active_version);
  const latest = versionLabel(rt?.latest_version);
  const staged = versionLabel(rt?.pending_version);
  const updatable = !!(rt?.update_available && latest) && !downloading && !staged;

  return card('Agent runtime',
    S.runtimeChecking ? 'checking…'
      : rt?.checked ? (active ? `v${active}` : 'unknown')
        : rt ? 'not checked yet' : 'unknown',
    h('div', { class: 'runtime-bar' },
      h('span', { class: 'runtime-state' },
        staged
          ? h('span', { class: 'chip', dataset: { tone: 'flag' } }, `v${staged} will be used next launch`)
          : rt?.update_available && latest
            ? h('span', { class: 'chip', dataset: { tone: 'flag' } }, `v${latest} available`)
            : rt?.checked
              ? h('span', { class: 'chip', dataset: { tone: 'ok' } }, 'up to date')
              : null,
        downloading ? h('span', { class: 'dim' }, 'Downloading…') : null),
      h('div', { class: 'runtime-actions' },
        updatable ? h('button', {
          // Not the accent. The page's accent belongs to "Choose an
          // assignment", and two filled buttons of different sizes next to
          // each other do not read as one page.
          class: 'btn', 'data-size': 'sm',
          onclick: () => guard(async () => {
            const r = await api.updateRuntime();
            if (r) {
              S.runtime = { ...S.runtime, pending_version: r.staging, downloading: true };
              toast('Downloading. Restart the app to start using it.');
            }
          }, 'Could not start the update'),
        }, `Install v${latest}`) : null,
        busyButton('Check for updates', 'Checking…', S.runtimeChecking,
          () => guard(async () => {
            S.runtimeChecking = true;
            render();
            try {
              const r = await api.checkRuntime();
              if (r) S.runtime = r;
            } finally {
              S.runtimeChecking = false;
            }
          }, 'Could not check for updates'),
          { 'data-size': 'sm', 'data-variant': 'ghost', disabled: downloading }))),
    rt?.active_source === 'managed'
      ? h('div', { class: 'hint', style: 'margin-top:10px' },
          `Running an updated runtime (v${active}) rather than the one this app `
          + 'was built with. Restart the app after installing an update to pick it up.')
      : null);
}

function viewSetup() {
  const st = S.status || {};
  if (!S.runtimeRequested) {
    S.runtimeRequested = true;
    loadRuntime();
  }
  const connected = st.canvas_configured;
  // These three keep their contents in S, not in the DOM node. render() throws
  // the node away and builds a new one, so a value that only existed in the old
  // node was lost on the next status refresh -- which is what made a pasted
  // token look like it had never been pasted.
  const base = h('input', {
    class: 'input', id: 'cf-base', placeholder: 'https://school.instructure.com',
    value: S.form.canvasBase,
    oninput: (e) => { S.form.canvasBase = e.target.value; },
  });

  const hasKey = !!st.model_key_configured;
  // A saved key with an empty field is a request to remove it, and the
  // button says so rather than hiding it behind a cleared field.
  const removing = hasKey && !S.form.openrouterKey.trim();

  return h('div', {},
    h('div', { class: 'head' },
      h('h1', {}, 'Set up'),
      h('p', {}, 'Connect to Canvas and confirm which model should do the grading. '
               + 'Everything else is read from the assignment itself.')),

    card('Canvas',
      connected ? 'connected' : 'not connected',
      field('Canvas URL', base),
      field('API token',
        secretField('cf-token', 'Canvas API token', S.form.canvasToken,
          (v) => { S.form.canvasToken = v; }),
        h('span', {}, 'Account → Settings → Approved Integrations → New Access Token. ',
          'The token is stored in your system keychain, never in the project.')),

      h('div', { style: 'display:flex;gap:8px;margin-top:4px' },
        h('button', {
          class: 'btn', 'data-variant': 'primary',
          onclick: async () => {
            const body = { canvas_base_url: S.form.canvasBase.trim(),
                           canvas_api_token: S.form.canvasToken.trim() };
            if (!body.canvas_base_url || !body.canvas_api_token) {
              return toast('Enter both the Canvas URL and a token.', 'flag');
            }
            // Canvas is asked whether the token works before it is stored, so a
            // rejection is reported here rather than surfacing later as an
            // empty course list. `guard` shows the reason, which is the whole
            // point: "rejected" and "could not reach Canvas" are different
            // problems with different fixes.
            const res = await guard(() => api.saveSettings(body), 'Could not save');
            if (res) {
              S.status = res;
              // Wipe the token now it is stored: it is in the keychain, and a
              // password field that keeps showing a live credential is a hazard
              // on a shared machine.
              S.form.canvasToken = '';
              toast('Canvas accepted the token.');
            }
          },
        }, 'Save and connect')),

      // A quiet line, not a panel. The outcome of the action is stated where
      // the action happened, and a bordered box here read as one more thing
      // to deal with.
      connected ? h('div', { class: 'confirm' },
        h('span', { class: 'dot' }),
        'Connected. You can move on to choosing a course.') : null),

    card('OpenRouter key', hasKey ? 'saved' : 'not set',
      field('API key',
        secretField('or-key', hasKey ? 'A key is already saved' : 'sk-or-v1-...',
          S.form.openrouterKey, (v) => { S.form.openrouterKey = v; })),
      h('div', { class: 'hint', style: 'margin-top:8px' },
        'Only needed for paid models — free models work without a key.'),
      h('div', { style: 'display:flex;gap:8px;margin-top:10px' },
        h('button', {
          // One button, two honest states. A saved key with an empty field
          // removes it; anything else saves. "Clear the field and press Save
          // to remove" was a trick, and the label said neither.
          class: 'btn', 'data-variant': removing ? 'danger' : 'primary',
          disabled: S.savingKey,
          onclick: async () => {
            S.savingKey = true;
            const res = await guard(async () => {
              const r = await api.saveSettings({ openrouter_api_key: S.form.openrouterKey.trim() });
              if (r) {
                S.status = r;
                S.form.openrouterKey = '';
                // The key restarts the agent runtime, so the model list it
                // reports changes too. Refresh it now instead of leaving
                // the user to discover that Refresh is the missing step.
                await loadModels();
              }
              return r;
            }, 'Could not save the key');
            S.savingKey = false;
            render();
            if (res) {
              toast(res.model_key_configured
                ? 'Key saved. The agent runtime restarts on the next grading run.'
                : 'Key removed.');
            }
          },
        }, S.savingKey ? 'Saving…' : (removing ? 'Remove key' : 'Save key')))),

    card('Grading model',
      h('div', { class: 'head-note' },
        h('span', {}, S.model?.candidates?.length
          ? `${S.model.candidates.length} available`
          : 'none yet'),
        busyButton('Refresh models', 'Reading models…', S.loadingModels,
          () => loadModels(true), { 'data-size': 'sm', 'data-variant': 'ghost' })),
      S.modelError
        ? notice(`Could not read models: ${S.modelError}`, 'bad')
        : !S.model?.candidates?.length
          ? notice('No models available yet. Save a key above, then Refresh models.', 'flag')
          : null,
      S.model?.candidates?.length
        ? h('div', { class: 'picklist', style: 'margin-top:12px' },
            S.model.candidates.slice(0, 8).map((c) => {
              const current = c.id === S.model.selected;
              return h('button', {
                class: 'pick', 'aria-selected': String(current),
                onclick: async () => {
                  const r = await guard(() => api.selectModel({ model: c.id }), 'Could not select model');
                  if (r) { S.model = { ...S.model, ...r }; toast(`Grading model set to ${r.selected}.`); }
                },
              },
                h('div', {},
                  h('div', { class: 'pick-title' }, c.id),
                  h('div', { class: 'pick-sub' },
                    h('span', {}, c.provider),
                    current && S.model.output_budget
                      ? h('span', {}, `${fmt(S.model.output_budget)} token budget`) : null)),
                h('div', { class: 'pick-meta' },
                  current ? chip('current', 'ok') : null,
                  chip(c.tier, c.tier === 'free' ? 'accent' : null)));
            }))
        : null),

    runtimeCard(),

    h('div', { class: 'page-foot page-foot--end' },
      h('button', {
        class: 'btn', 'data-variant': 'primary', disabled: !connected,
        onclick: () => go('select'),
      }, 'Choose an assignment')),
  );
}

/** A panel that slides in from the right. Used for assignments and the log. */
function slideover(title, subtitle, body, foot, wide, narrow) {
  return h('div', {
    class: 'slideover',
    onclick: (e) => { if (e.target.classList.contains('slideover')) closePanel(); },
  },
    h('div', {
      // The log gets more width: agent output lines are long and a 760px
      // terminal wraps them into something hard to follow. A short list gets
      // less: a row of three words does not need 760px.
      class: wide ? 'slideover-panel slideover--wide'
        : narrow ? 'slideover-panel slideover--narrow' : 'slideover-panel',
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

/* The icon set. One stroke, one weight, one grid: a pencil, a play, a bin.
   Drawn rather than typed, so a row of actions is three small marks instead
   of three boxes shouting at each other. */
const ICONS = {
  resume: [{ d: 'M6 3.8l6.1 4.2L6 12.2z', fill: true }],
  rename: [{ d: 'M11.1 2.9l2 2-6.8 6.8-2.5.5.5-2.5z' }],
  delete: [
    { d: 'M3.4 4.6h9.2' },
    { d: 'M6.6 4.6v-1.2h2.8v1.2' },
    { d: 'M4.7 4.6l.5 8a1 1 0 00.95.9h3.7a1 1 0 00.95-.9l.5-8' },
  ],
};

function closePanel() {
  if (S.logTimer) { clearInterval(S.logTimer); S.logTimer = null; }
  S.panel = null;
  S.logEntries = [];
  render();
}

/** Polled rather than pushed: the sidecar keeps a bounded tail per row, and
 *  only an open panel pays for the polling. */
function openAgentLog(rowId) {
  S.panel = { kind: 'log', rowId };
  S.logEntries = [];
  render();
  if (S.logTimer) clearInterval(S.logTimer);
  // Polling runs for as long as the panel is open, and no longer. It used to
  // stop on the first poll that brought nothing new, on the assumption that a
  // quiet log means a finished one — but a model reasoning through a long
  // submission goes quiet for far longer than the interval between two polls.
  // The panel updated for the first few entries and then froze, and the only
  // way to see the rest was to close it and open it again.
  //
  // Compared on the tail rather than the length: the sidecar trims the log to a
  // fixed size, so a long turn holds the same length while the contents move.
  const newest = (lines) => {
    const last = lines && lines[lines.length - 1];
    return last ? `${last.t}:${(last.text || '').length}` : '';
  };
  const poll = async () => {
    // A panel closed between ticks leaves the timer cleared; stop here too
    // rather than polling a row nobody is looking at.
    if (!S.panel || S.panel.kind !== 'log' || S.panel.rowId !== rowId) {
      clearInterval(S.logTimer);
      S.logTimer = null;
      return;
    }
    const got = await api.agentLog(rowId).catch(() => null);
    if (!got || !Array.isArray(got.lines)) return;
    if (got.lines.length === S.logEntries.length
        && newest(got.lines) === newest(S.logEntries)) return;
    S.logEntries = got.lines;
    renderPanel();
  };
  poll();
  S.logTimer = setInterval(poll, 1200);
}

/** Put the caret back where the person left it, across a rebuild. */
function restoreFocus(id, caret) {
  if (!id) return;
  const el = document.getElementById(id);
  if (!el || typeof el.focus !== 'function') return;
  el.focus();
  if (caret !== null && typeof el.setSelectionRange === 'function') {
    // Password and number inputs throw on a range they cannot represent.
    try { el.setSelectionRange(caret, caret); } catch (_) { /* not settable */ }
  }
}

function renderPanel() {
  const el = document.getElementById('slideover-slot');
  if (!el) return;
  const panel = S.panel;
  if (!panel) { mount(el); return; }

  // Remember whether the reader was following the tail, and where they were.
  // The panel is re-mounted on every poll, so without this the position resets
  // and then snaps to the end: reading the agent's reasoning by scrolling up
  // becomes impossible, which is the opposite of what a live log is for.
  const prev = el.querySelector('.slideover-body');
  const stick = prev
    ? (prev.scrollHeight - prev.scrollTop - prev.clientHeight) < 48
    : true;
  const kept = stick ? null : prev.scrollTop;

  if (panel.kind === 'assign') {
    const course = S.course;
    const shown = S.assignments.slice(0, S.assignmentsShown);
    const remaining = S.assignments.length - S.assignmentsShown;
    const list = h('div', {},
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
        : null);
    mount(el, slideover(
      course ? course.name : 'Assignments',
      course ? `course ${course.id}` : null,
      S.loadingAssignments
        ? h('div', { class: 'loading-note' },
            h('span', { class: 'spinner', 'aria-hidden': 'true' }),
            h('span', {}, 'Reading assignments from Canvas…'))
        : S.assignmentsError
          ? h('div', { class: 'notice', dataset: { tone: 'bad' } }, S.assignmentsError)
          : list,
      h('button', {
        class: 'btn', 'data-variant': 'primary',
        disabled: !S.assignment || !S.assignment.has_rubric,
        onclick: () => { closePanel(); startRun(); },
      }, 'Use this assignment')));
    return;
  }

  if (panel.kind === 'model') {
    const cands = S.model?.candidates || [];
    mount(el, slideover(
      'Model for the next run',
      cands.length ? `${cands.length} available` : null,
      cands.length
        ? h('div', { class: 'picklist' }, cands.slice(0, 40).map((c) => h('button', {
            class: 'pick',
            'aria-selected': String(c.id === (S.runModel || S.model?.selected)),
            onclick: () => {
              // Picking the saved default clears the override instead of
              // setting one, which is what the "Use the saved default" button
              // did before the note became the only control.
              const isDefault = c.id === S.model?.selected;
              S.runModel = isDefault ? '' : c.id;
              closePanel();
              toast(isDefault
                ? `Using the saved default for the next run.`
                : `Next run grades with ${c.id}.`);
            },
          },
            h('div', {},
              h('div', { class: 'pick-title' }, c.id),
              h('div', { class: 'pick-sub' }, h('span', {}, c.provider))),
            h('div', { class: 'pick-meta' },
              chip(c.tier, c.tier === 'free' ? 'accent' : null)))))
        : h('div', { style: 'color:var(--ink-3);font-size:12px' },
            'No models available. Add an OpenRouter key on the setup step.')));
    return;
  }

  if (panel.kind === 'runs') {
    const runs = S.savedRuns;
    const resume = (r) => { closePanel(); resumeRun(r); };
    const rerender = () => renderPanel();
    mount(el, slideover(
      'Saved sessions',
      runs.length ? `${runs.length} session${runs.length === 1 ? '' : 's'}` : null,
      runs.length
        ? h('div', { class: 'session-list' }, runs.map((r) => sessionRow(r, resume, rerender)))
        : h('div', { class: 'session-empty' },
            'Nothing saved yet. A session appears here once you start one, and '
            + 'you can come back to it later from any machine.'),
      null, false, true));
    return;
  }

  if (panel.kind === 'log') {
    const row = S.submissions.find((x) => x.row_id === panel.rowId);
    // Stop sits here rather than on the grade stage because that is where you
    // are when you decide: watching a turn that is going nowhere, with the
    // student named at the top of the panel. The grade-stage controls are a
    // screen away, behind a list of 150 other rows.
    const running = row && row.status === 'grading';
    const stopping = S.stoppingRows.has(panel.rowId);
    const sendSteer = () => {
      const msg = (S.steerDraft[panel.rowId] || '').trim();
      if (!msg) return;
      guard(async () => {
        const res = await api.steerSubmission(panel.rowId, msg);
        if (res) {
          delete S.steerDraft[panel.rowId];
          S.steerFocused = true;
          toast('Instruction sent to the agent.');
          renderPanel();
        }
      }, 'Could not steer the agent');
    };
    mount(el, slideover(
      row ? (row.student || row.student_name || 'Student') : 'Grading agent',
      'Live agent output',
      logView(S.logEntries),
      h('div', { class: 'panel-foot' },
        // The instruction and its button are one object: a field with a
        // button inside its frame, not two controls that happen to sit near
        // each other.
        running ? h('div', { class: 'steer-group' },
          h('input', {
            class: 'input', id: 'steer-input',
            placeholder: 'Steer the agent…', value: S.steerDraft[panel.rowId] || '',
            oninput: (e) => { S.steerDraft[panel.rowId] = e.target.value; },
            onfocus: () => { S.steerFocused = true; },
            onblur: () => { S.steerFocused = false; },
            onkeydown: (e) => { if (e.key === 'Enter') { e.preventDefault(); sendSteer(); } },
          }),
          h('button', {
            class: 'btn steer-send', 'data-variant': 'primary',
            onclick: sendSteer,
          }, 'Send')) : null,
        // The panel header already carries Close. The foot carries the action
        // you take while watching.
        h('div', { class: 'panel-foot-actions' },
          running && !stopping ? h('button', {
            class: 'btn', 'data-size': 'sm', 'data-variant': 'danger',
            onclick: () => guard(async () => {
              S.stoppingRows.add(panel.rowId);
              renderPanel();
              await api.stopSubmission(panel.rowId);
              toast('Stopped. This one can be graded again.');
            }, 'Could not stop this submission'),
          }, 'Stop') : null,
          stopping ? h('span', { class: 'chip', dataset: { tone: 'flag' } }, 'stopping…') : null))));
    restoreLogScroll(el, stick, kept);
    if (S.steerFocused) {
      const input = el.querySelector('#steer-input');
      if (input) {
        input.focus();
        try { input.setSelectionRange(input.value.length, input.value.length); } catch (_) {}
      }
    }
  }
}

function restoreLogScroll(root, stick, kept) {
  const body = root.querySelector('.slideover-body');
  if (!body) return;
  requestAnimationFrame(() => {
    if (stick) body.scrollTop = body.scrollHeight;
    else if (kept !== null) body.scrollTop = kept;
  });
}

/** A line in the agent log. */
function logRow(entry) {
  const t = new Date((entry.t || 0) * 1000);
  const clock = `${String(t.getHours()).padStart(2, '0')}:${String(t.getMinutes()).padStart(2, '0')}:${String(t.getSeconds()).padStart(2, '0')}`;
  const time = h('span', { class: 'log-t' }, clock);

  if (entry.kind === 'error') {
    // Tag, text and detail as siblings of the clock, matching the other rows.
    // Wrapped in one div they became inline spans with no gap between them and
    // the line read "stoppedthe agent finished without calling ...".
    // A failure is red; a turn being retried is amber, because it has not
    // failed yet.
    return h('div', { class: `log-row ${entry.retryable ? 'log-warn' : 'log-bad'}` },
      time,
      h('span', { class: 'log-tag' }, entry.retryable ? 'retrying' : 'failed'),
      h('span', { class: 'log-text' }, entry.text),
      entry.detail ? h('div', { class: 'log-detail' }, entry.detail) : null);
  }
  if (entry.kind === 'step') {
    const t2 = entry.tokens || {};
    const bits = [];
    if (t2.input) bits.push(`${t2.input} in`);
    if (t2.output) bits.push(`${t2.output} out`);
    if (t2.reasoning) bits.push(`${t2.reasoning} think`);
    if (entry.cost) bits.push(`$${entry.cost.toFixed(4)}`);
    return h('div', { class: 'log-row log-step' }, time,
      h('span', { class: 'log-text' }, bits.join(' · ') || entry.text || 'step'));
  }
  if (entry.kind === 'think' || entry.kind === 'think-more') {
    return h('div', { class: 'log-row log-think' }, time,
      h('span', { class: 'log-text' }, entry.text));
  }
  if (entry.kind === 'tool') {
    return h('div', { class: `log-row log-tool${entry.error ? ' log-bad' : ''}` }, time,
      h('span', { class: 'log-tag' }, `-> ${entry.tool}`),
      h('span', { class: 'log-text' }, entry.text),
      entry.error && entry.detail
        ? h('div', { class: 'log-detail' }, entry.detail) : null);
  }
  if (entry.kind === 'meta') {
    return h('div', { class: 'log-row log-meta' }, time,
      h('span', { class: 'log-text' }, entry.text));
  }
  return h('div', { class: 'log-row log-text-only' }, time,
    h('span', { class: 'log-text' }, entry.text || ''));
}

/**
 * The log, with consecutive identical tool calls collapsed.
 *
 * A grading run makes the same call dozens of times. Twelve identical lines of
 * "-> grep" pushes everything else off the screen and hides the reasoning,
 * which is the part worth reading.
 */
function logView(entries) {
  const list = entries || [];
  if (!list.length) {
    return h('div', { style: 'color:var(--ink-3);font-size:12px' },
      'Waiting for the agent. Output appears here as it runs.');
  }
  const rows = [];
  let i = 0;
  while (i < list.length) {
    const e = list[i];
    const groupable = e.kind === 'tool';
    let n = 1;
    if (groupable) {
      while (i + n < list.length
        && list[i + n].kind === 'tool'
        && list[i + n].tool === e.tool
        && list[i + n].text === e.text
        && !list[i + n].error) n += 1;
    }
    const row = logRow(e);
    if (n > 1) row.appendChild(h('span', { class: 'log-count' }, `\u00d7${n}`));
    rows.push(row);
    i += n;
  }
  const cap = LOG_ROWS;
  const shown = rows.length > cap ? rows.slice(rows.length - cap) : rows;
  const el = h('div', { class: 'logview' }, shown);
  return rows.length > cap
    ? h('div', {},
        h('div', { class: 'log-trim', style: 'padding:6px 2px 10px' },
          `${rows.length - cap} earlier line(s) hidden`),
        el)
    : el;
}

function viewSelect() {
  const courses = S.courses;
  if (!courses.length) {
    return h('div', {},
      h('div', { class: 'head' }, h('h1', {}, 'Choose an assignment')),
      card('Courses', null,
        loadingCoursesButton('Load courses')),
      // One box, not two. The button already says the load is in flight; the
      // note below it only says how long it might take.
      S.loadingCourses
        ? h('div', { class: 'hint', style: 'margin-top:10px' },
            'Reading every course on your Canvas account. With a hundred '
            + 'or so this can take a moment.')
        : null);
  }

  // Most recent first, so the courses a lecturer actually teaches this term
  // are the ones that fit above the fold. Fall back to id (Canvas assigns
  // higher ids to newer courses) when start dates are missing.
  const byRecency = (a, b) => {
    const aDate = a.start_at ? new Date(a.start_at).getTime() : 0;
    const bDate = b.start_at ? new Date(b.start_at).getTime() : 0;
    if (aDate !== bDate) return bDate - aDate;
    return (b.id || 0) - (a.id || 0);
  };
  const active = courses.filter((c) => c.workflow_state === 'available').sort(byRecency);
  const inactive = courses.filter((c) => c.workflow_state !== 'available').sort(byRecency);
  const ordered = [...active, ...inactive];
  const shown = ordered.slice(0, S.coursesShown);

  const rows = shown.map((c) => h('button', {
    class: 'pick', 'aria-selected': String(S.course?.id === c.id),
    onclick: async () => {
      S.assignment = null;
      S.assignments = [];
      S.assignmentsShown = ASSIGNMENT_PAGE;
      S.assignmentsError = null;
      S.course = c;
      S.coursesShown = Math.max(S.coursesShown, COURSE_PAGE);
      S.loadingAssignments = true;
      // The panel opens straight away. Reading a course's assignments takes a
      // moment, and until it comes back the screen gave no sign that the
      // click had landed at all.
      S.panel = { kind: 'assign', rowId: null };
      render();
      // A second course clicked while the first is still loading wins; the
      // older response must not land on top of it.
      const token = ++assignToken;
      const ok = await guard(async () => {
        const data = await api.assignments(c.id);
        if (token !== assignToken) return false;
        S.assignments = data.assignments || [];
        return true;
      }, 'Could not load assignments');
      if (token !== assignToken) return;
      S.loadingAssignments = false;
      if (ok !== true) S.assignmentsError = 'Canvas did not return this course’s assignments.';
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
      S.loadingAssignments && S.course?.id === c.id
        ? h('span', { class: 'loading-inline' },
            h('span', { class: 'spinner', 'aria-hidden': 'true' }), 'loading…')
        : h('span', {}, 'open assignments'))));

  return h('div', {},
    h('div', { class: 'head' },
      h('h1', {}, 'Choose an assignment'),
      h('p', {}, 'Pick a course, then choose an assignment from the panel. '
               + 'The rubric and instructions come straight from Canvas.')),

    h('div', { style: 'margin-bottom:16px' },
      h('button', {
        class: 'btn', 'data-variant': 'ghost',
        onclick: () => guard(async () => { await loadRuns(); S.panel = { kind: 'runs', rowId: null }; render(); },
          'Could not load saved sessions'),
      }, `Saved sessions${S.savedRuns.length ? ` (${S.savedRuns.length})` : ''}`)),

    card('Courses', `${shown.length} of ${ordered.length}`,
      h('div', { class: 'picklist' }, rows),
      ordered.length > S.coursesShown
        ? h('div', { style: 'margin-top:14px' },
            h('button', {
              class: 'btn',
              onclick: () => { S.coursesShown += COURSE_PAGE; render(); },
            }, `Load more (${ordered.length - S.coursesShown} remaining)`))
        : null),

    h('div', { class: 'page-foot' },
      h('button', { class: 'btn', 'data-variant': 'ghost', onclick: () => go('setup') }, 'Back'),
      S.assignment
        ? h('button', {
            class: 'btn', 'data-variant': 'primary',
            disabled: !S.assignment.has_rubric,
            onclick: startRun,
          }, `Use ${S.assignment.name}`)
        // A disabled button says "this is a control you may not use yet". A
        // line of text says what to do instead.
        : h('span', { class: 'page-foot-note' }, 'Choose an assignment first.')));
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
  const collectJob = S.status?.jobs?.collect?.status;
  const collecting = ['running', 'paused'].includes(collectJob);
  const paused = collectJob === 'paused';
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
          class: 'btn', 'data-variant': 'danger',
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
  const jobRunning = (name) => ['running', 'paused']
    .includes(S.status?.jobs?.[name]?.status);
  const grading = jobRunning('grade');

  return h('div', {},
    h('div', { class: 'head' },
      h('h1', {}, 'Grade'),
      h('p', {}, 'Each submission is graded in its own agent session, so no student\'s work '
               + 'can influence another\'s. The agent returns structured scores; the report '
               + 'is rendered from them.')),

    // Sticky, so the controls stay reachable from the bottom of a long list.
    h('div', { class: 'sticky-acts', dataset: { scrolled: String(mainScrolled) } },
    card('Run grading', modelNote(),
      h('div', { class: 'stats', style: 'margin-bottom:16px' },
        h('div', { class: 'stat' }, h('div', { class: 'stat-v' }, graded), h('div', { class: 'stat-k' }, 'graded')),
        h('div', { class: 'stat' }, h('div', { class: 'stat-v' }, S.submissions.length), h('div', { class: 'stat-k' }, 'collected')),
        h('div', { class: 'stat', dataset: flagged ? { tone: 'flag' } : {} },
          h('div', { class: 'stat-v' }, flagged),
          h('div', { class: 'stat-k' }, 'needs attention'))),

      h('div', { style: 'display:flex;gap:8px;flex-wrap:wrap' },
        h('button', {
          class: 'btn', 'data-variant': 'primary',
          disabled: grading || !gradeable.length,
          onclick: () => guard(async () => {
            await api.grade(run.run_id, S.runModel ? { model: S.runModel } : {});
            toast(`Grading ${gradeable.length} submissions${S.runModel ? ` with ${S.runModel}` : ''}.`);
          }, 'Could not start grading'),
        }, grading ? 'Grading…' : `Grade All (${gradeable.length})`),
        h('button', {
          // Smart: grading a selection only makes sense when it contains
          // something still awaiting a score.
          class: 'btn', disabled: grading
            || ![...S.selected].some((id) => S.submissions.some(
              (s) => s.row_id === id && s.score === null)),
          onclick: () => guard(async () => {
            await api.grade(run.run_id, {
              row_ids: [...S.selected],
              ...(S.runModel ? { model: S.runModel } : {}),
            });
            toast(`Grading ${S.selected.size} selected${S.runModel ? ` with ${S.runModel}` : ''}.`);
          }, 'Could not start grading'),
        }, `Grade Selected (${S.selected.size})`),
        h('button', { class: 'btn', disabled: !graded, onclick: () => go('review') },
          'Continue to review'),
        // Undo grades (e.g. stub grades) without re-collecting: sources stay,
        // scores go. Refused once anything in scope has been published.
        h('button', {
          class: 'btn', 'data-variant': 'ghost', disabled: grading || !graded,
          onclick: async () => {
            const ok = window.confirm(
              'Clear all grades in this run so it can be graded again? '
              + 'Collected sources are kept. This cannot be used once grades '
              + 'have been published to Canvas.');
            if (!ok) return;
            await guard(async () => {
              await api.resetGrades(run.run_id);
              await refreshSubmissions();
              toast('Grades cleared. The run can be graded again from scratch.');
            }, 'Could not reset grades');
          },
        }, 'Reset grades'),
        h('button', {
          // Smart: resetting only makes sense for a selection containing
          // something already graded.
          class: 'btn', 'data-variant': 'ghost', disabled: grading
            || ![...S.selected].some((id) => S.submissions.some(
              (s) => s.row_id === id && s.score !== null)),
          onclick: async () => {
            const ok = window.confirm(
              `Clear grades for ${S.selected.size} selected submission(s) so `
              + 'they can be graded again? Collected sources are kept.');
            if (!ok) return;
            await guard(async () => {
              await api.resetGrades(run.run_id, [...S.selected]);
              await refreshSubmissions();
              toast(`Grades cleared for ${S.selected.size} selected.`);
            }, 'Could not reset grades');
          },
        }, `Reset Selected (${S.selected.size})`)))),

    // Outside the sticky block on purpose: it is advice, not a control, and
    // pinning it costs a screenful of rows on a long cohort. A line of text,
    // not a boxed notice — it explains nothing you cannot read.
    h('p', { class: 'page-foot-note', style: 'margin:12px 0 0' },
      'Posting is optional. If a run does not hold up, grade from the reports yourself.'),

    S.submissions.length ? submissionTable(false) : null);
}

function viewReview() {
  const run = S.run;
  if (!run) return empty('No run', 'Choose an assignment first.');
  const graded = S.submissions.filter((s) => s.score !== null);
  const blocked = graded.filter((s) => (s.flags || []).some((f) => f.severity === 'block')).length;

  // A report flagged for a human is the one that needs a decision, and on a
  // cohort of 73 buried at an arbitrary alphabetical position it is easy to
  // reach the end without having opened any of them.
  const isFlagged = (s) => (s.flags || []).some((f) => f.severity === 'block');
  const shown = S.reviewFilter === 'flagged' ? graded.filter(isFlagged) : graded;

  const list = h('div', { class: 'rows' },
    h('div', { class: 'row row-head' },
      h('span', {}, 'Student'),
      h('span', { style: 'text-align:right' }, 'Score')),
    shown.map((s) => h('div', {
      class: 'row', 'data-selected': String(S.current?.rowId === s.row_id),
      onclick: () => openReport(s.row_id),
    },
      h('span', { class: 'row-name', title: s.student }, s.student),
      h('span', { class: 'row-score' },
        fmt(s.score),
        isFlagged(s)
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
          h('span', { class: 'note' },
            `${shown.length} of ${graded.length}`)),
        // The filter, and a way to walk the flagged ones one at a time without
        // going back to the list each time.
        blocked ? h('div', { style: 'display:flex;gap:6px;margin-bottom:10px;flex-wrap:wrap' },
          h('button', {
            class: 'btn', 'data-size': 'sm',
            'data-variant': S.reviewFilter === 'flagged' ? 'primary' : null,
            onclick: () => {
              S.reviewFilter = S.reviewFilter === 'flagged' ? 'all' : 'flagged';
              // The open report may not be in the list any more.
              if (S.reviewFilter === 'flagged' && S.current
                  && !isFlagged(S.submissions.find((x) => x.row_id === S.current.rowId) || {})) {
                const first = graded.find(isFlagged);
                if (first) { openReport(first.row_id); return; }
              }
              render();
            },
          }, S.reviewFilter === 'flagged' ? 'Showing flagged' : `Open flagged (${blocked})`),
          h('button', {
            class: 'btn', 'data-size': 'sm', 'data-variant': 'ghost',
            disabled: blocked < 2,
            onclick: () => {
              const flaggedRows = graded.filter(isFlagged);
              const at = flaggedRows.findIndex((x) => x.row_id === S.current?.rowId);
              const next = flaggedRows[(at + 1) % flaggedRows.length];
              if (next) { openReport(next.row_id); }
            },
          }, 'Next flagged')) : null,
        list),
      h('div', { class: 'review-pane' + (S.tab === 'edit' ? ' review-pane--editing' : '') },
        reportPane())));
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
    class: 'btn', 'data-size': 'sm', 'data-variant': 'primary',
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
          scorecard
            ? chip(`${fmt(earnedOf(scorecard))} / ${fmt(run.points_possible)}`, 'accent')
            : null,
          (scorecard?.flags || []).map((f) => chip(f.code, f.severity === 'block' ? 'flag' : null)),
          h('button', { class: 'rowlink',
            onclick: () => bridge.revealPath(S.current.path) }, 'show file'))),
      h('div', { class: 'reader-actions' },
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
        h('button', { class: 'btn', 'data-variant': 'ghost', onclick: () => go('review') }, 'Back to reports'))),

    // Reference material, not another thing to do: a card would make it look
    // like a panel of controls, so it sits under the stage as a footnote.
    h('div', { class: 'explain' },
      h('h3', {}, 'What Canvas receives'),
      h('ul', {},
        h('li', {}, 'Per criterion: the awarded points, and the grader’s justification plus evidence as the criterion comment.'),
        h('li', {}, 'Overall: the sum of the criteria as the posted grade.'),
        h('li', {}, 'Group assignments: one write per group, which Canvas propagates to every member.'))));
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

/** The status a row is really in.

 *  A row says it is being graded while the agent works on it. If no grading
 *  is running, that marker is left over from a session that died, and the
 *  row is simply collected and waiting to be marked.
 */
function rowStatus(s) {
  if (s.status === 'grading' && !s.grading) return 'collected';
  return s.status;
}

function submissionTable(compact) {
  // Every row is pickable, graded or not: selecting a graded row is how
  // re-grading (Grade Selected) and resetting (Reset Selected) reach it.
  // Hiding the control on graded rows made stub-graded cohorts impossible
  // to fix one student at a time.
  const allSelected = S.submissions.length > 0
    && S.submissions.every((s) => S.selected.has(s.row_id));
  // A selected row keeps its checkbox even after it gains a score, so it
  // can always be taken out of the selection again.
  const pickable = (s) => !compact;
  const rows = S.submissions.map((s) => {
    const toggle = () => {
      S.selected.has(s.row_id) ? S.selected.delete(s.row_id) : S.selected.add(s.row_id);
      render();
    };
    return h('div', {
      class: 'row', 'data-selected': String(S.selected.has(s.row_id)),
      // The row is the hit target, so a ticked row can be unticked by clicking
      // anywhere on it even where no checkbox is drawn. Buttons and links keep
      // their own behaviour.
      onclick: (e) => {
        if (compact || (!pickable(s) && !S.selected.has(s.row_id))) return;
        if (e.target.closest('button, a, input')) return;
        toggle();
      },
      style: pickable(s) ? 'cursor:pointer' : null,
    },
    pickable(s) ? h('input', {
          type: 'checkbox',
          title: s.student || s.student_name || '',
          checked: S.selected.has(s.row_id),
          onchange: (e) => {
            e.target.checked ? S.selected.add(s.row_id) : S.selected.delete(s.row_id);
            render();
          },
          onclick: (e) => e.stopPropagation(),
        })
      : h('span', { style: 'color:var(--ink-3)' }, ''),
    h('span', { class: 'row-name', title: s.primary_url || '' }, s.student),
    h('span', { class: 'row-score' }, s.score === null ? '—' : fmt(s.score)),
    h('span', {}, statusChip(rowStatus(s))),
    h('span', { class: 'row-act', style: 'display:flex;gap:6px;justify-content:flex-end' },
      // The agent output is how you see what happened - especially when a
      // run finished without a scorecard and was flagged for review.
      (s.score !== null || ['grading', 'collected', 'needs_review', 'failed', 'reviewed'].includes(s.status))
        ? h('button', {
            class: 'btn', 'data-size': 'sm', 'data-variant': 'ghost',
            onclick: () => openAgentLog(s.row_id),
          }, 'agent log')
        : null,
      s.primary_url ? h('button', {
        class: 'btn', 'data-size': 'sm', 'data-variant': 'ghost',
        onclick: () => window.open(s.primary_url, '_blank'),
      }, 'repo') : null));
  });
  return h('div', { style: 'margin-top:18px' },
    h('div', { class: 'card-head' }, h('h2', {}, 'Submissions'), h('span', { class: 'note' }, `${S.submissions.length}`)),
    h('div', { class: 'rows' },
      h('div', { class: 'row row-head' },
        h('span', {},
          !compact && S.submissions.length
            ? h('input', {
                type: 'checkbox',
                class: 'pick-all',
                title: allSelected ? 'Clear selection' : 'Select all',
                checked: allSelected,
                // A half-ticked box is the honest state when some are chosen.
                indeterminate: S.selected.size > 0 && !allSelected,
                onchange: () => {
                  // Driven by what is selected, not by the new tick state.
                  // A half-ticked box reports `checked` after a click rather
                  // than `unchecked`, so reading it would re-select everything
                  // when the obvious intent is to clear.
                  const any = S.submissions.some((x) => S.selected.has(x.row_id));
                  S.submissions.forEach((x) => (any
                    ? S.selected.delete(x.row_id) : S.selected.add(x.row_id)));
                  render();
                },
              })
            : null),
        h('span', {}, 'Student'), h('span', { style: 'text-align:right' }, 'Score'),
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
  // Nothing to navigate between until a session is chosen, and a rail that
  // lights up a stage the user has not entered is a lie.
  if (S.launching) {
    mount(els.stages);
    els.spineMeta.textContent = '';
    return;
  }
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

  // A run that has not been fetched yet has no title and no criteria, so
  // nothing here may assume they are there.
  els.spineMeta.textContent = S.run
    ? [
        S.run.title,
        S.run.criteria?.length ? `${S.run.criteria.length} criteria` : null,
      ].filter(Boolean).join('\n')
    : 'No assignment selected';
  els.spineMeta.style.whiteSpace = 'pre-line';
}

function renderTopbar() {
  if (S.launching) {
    clear(els.topRun);
    els.runLabel.textContent = 'sessions';
    return;
  }
  // The breadcrumb says where you are; the run beside it says what you are
  // working on. The assignment's name used to be in both.
  els.runLabel.textContent = S.run
    ? (STAGES.find((s) => s.id === S.stage) || { label: 'setup' }).label
    : 'setup';
  if (S.run) {
    mount(els.topRun, h('span', { class: 'name' }, S.run.title),
      S.run.criteria?.length
        ? h('span', {}, `${S.run.criteria.length} criteria · ${fmt(S.run.points_possible)} pts`)
        : null);
  } else {
    clear(els.topRun);
  }
}

function renderAside() {
  const run = S.run;
  const extra = [];
  const title = document.getElementById('aside-title');

  if (run && S.stage === 'collect') {
    title.textContent = 'Activity';
    const p = run.progress || {};
    extra.push(h('div', { class: 'stats stats--rail' },
      h('div', { class: 'stat' }, h('div', { class: 'stat-k' }, 'submissions'), h('div', { class: 'stat-v' }, p.total || 0)),
      h('div', { class: 'stat' }, h('div', { class: 'stat-k' }, 'collected'), h('div', { class: 'stat-v' }, p.collected || 0)),
      h('div', { class: 'stat' }, h('div', { class: 'stat-k' }, 'no submission'), h('div', { class: 'stat-v' }, p.no_submission || 0)),
      h('div', { class: 'stat' }, h('div', { class: 'stat-k' }, 'failed'), h('div', { class: 'stat-v' }, p.collect_failed || 0))));
  } else if (run && S.stage === 'grade') {
    title.textContent = 'Activity';
    // The same three figures the Run grading card shows, counted the same
    // way. Two panels that disagree are worse than one panel.
    const graded = S.submissions.filter((s) => s.score !== null).length;
    const flagged = S.submissions.filter((s) => (s.flags || []).length).length;
    extra.push(h('div', { class: 'stats stats--rail' },
      h('div', { class: 'stat' }, h('div', { class: 'stat-k' }, 'graded'), h('div', { class: 'stat-v' }, graded)),
      h('div', { class: 'stat' }, h('div', { class: 'stat-k' }, 'collected'), h('div', { class: 'stat-v' }, S.submissions.length)),
      h('div', { class: 'stat', dataset: flagged ? { tone: 'flag' } : {} },
        h('div', { class: 'stat-k' }, 'needs attention'),
        h('div', { class: 'stat-v' }, flagged))));
  } else if (run && S.stage === 'review') {
    title.textContent = 'Rubric';
    // The marks belong to the report that is open. Showing the last report's
    // scores on the setup page said the marks were about the assignment when
    // they were about one student.
    const scorecard = S.current?.scorecard;
    const scores = scorecard
      ? Object.fromEntries(scorecard.criterion_scores.map((c) => [c.criterion, c.points_earned]))
      : null;
    extra.push(card(null, null, marksStrip(run.criteria, scores,
      { flagKeys: (scorecard?.flags || []).map((f) => f.code) })));
    if (scorecard?.model) {
      extra.push(h('div', { style: 'font-size:11px;color:var(--ink-3);line-height:1.7' },
        `Graded by ${scorecard.model} · prompt ${scorecard.prompt_version}`));
    }
  } else {
    title.textContent = '';
  }
  mount(els.extra, ...extra);
}

function render() {
  // Re-rendering rebuilds every input from scratch, so anything typed but not
  // yet saved lived only in a node that is about to be thrown away. A status
  // refresh, a background event or the Save button's own busy state was enough
  // to do it, and the text disappeared with no indication of why.
  //
  // The values live in S and are written back below; focus and caret position
  // are carried across here, because a field that keeps its text but loses the
  // caret mid-word is its own kind of broken.
  const active = document.activeElement;
  const focusId = active && active.id ? active.id : null;
  const caret = active && typeof active.selectionStart === 'number'
    ? active.selectionStart : null;
  renderSpine();
  renderTopbar();
  // The aside describes the run you are in. Before you have picked one there is
  // nothing to describe, and a stale count from the last session is worse than
  // an empty panel.
  if (S.launching) {
    // Clear the whole panel, not just the log: the counts beside it belong to
    // the run you were last in, not to the choice you have not made yet.
    mount(els.console);
    mount(els.extra);
    document.getElementById('aside-title').textContent = '';
  } else {
    renderAside();
  }

  const views = {
    setup: viewSetup, select: viewSelect, collect: viewCollect,
    grade: viewGrade, review: viewReview, publish: viewPublish,
  };
  const node = S.launching ? viewLauncher() : (views[S.stage] || viewSetup)();
  const banner = S.toast
    ? h('div', { style: 'margin-bottom:14px' }, notice(S.toast.message, S.toast.tone))
    : null;
  mount(els.main, banner, node);
  // Re-mounting empties the scroll container, so the sticky bar has to be
  // told the page is back at the top.
  mainScrolled = els.main.scrollTop > 8;
  syncSticky();
  restoreFocus(focusId, caret);
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
  // Repaint. Without this the new status only reached the DOM the next time
  // something else happened to render, which is why a finished job left its
  // button saying "Grading…" until an unrelated event came along.
  render();
}

async function loadModels(announce = false) {
  S.loadingModels = true;
  S.modelError = null;
  render();
  try {
    S.model = await api.models();
    if (S.model && !S.model.selected) {
      const pick = S.model.candidates?.[0];
      if (pick) {
        const r = await api.selectModel({ model: pick.id }).catch(() => null);
        if (r) S.model = { ...S.model, ...r };
      }
    }
    if (!S.model?.candidates?.length) {
      S.modelError = S.model
        ? 'The agent runtime returned no models.'
        : 'No response from the agent runtime.';
    }
  } catch (err) {
    // A swallowed failure here left the card at "not chosen" with no list
    // and no explanation: Refresh appeared to do nothing at all.
    S.model = null;
    S.modelError = (err && err.message) || String(err);
  } finally {
    S.loadingModels = false;
    render();
  }
  if (announce && S.modelError) {
    toast(`Could not read models: ${S.modelError}`, 'bad');
  }
  return !S.modelError;
}

async function loadRuns() {
  const data = await api.runs().catch(() => null);
  if (data) S.savedRuns = data.runs || [];
}

/** Where the run had got to, so resuming lands somewhere sensible. */
function resumeStage(run, submissions) {
  if (submissions) {
    const readable = submissions.some((s) => s.score !== null);
    const total = submissions.length;
    const finished = submissions.filter((s) => ['graded', 'needs_review', 'reviewed', 'no_submission'].includes(s.status)).length;
    if (readable) {
      if (total && finished >= total) return 'publish';
      return 'review';
    }
    // No readable reports yet: don't land on review even if rows are flagged.
    const hasWork = submissions.some((s) => !['no_submission'].includes(s.status));
    if (run.status === 'collected' || hasWork) return 'grade';
    return 'collect';
  }
  if (run.total && run.graded >= run.total) return 'publish';
  if (run.graded) return 'review';
  if (run.status === 'collected') return 'grade';
  return 'collect';
}

/** The three actions a saved session offers, as drawn marks.
 *
 *  Icons, one stroke and one weight: three boxes of text in a row of a
 *  session list were louder than the sessions they belonged to.
 */
function sessionActions(r, resume, rerender) {
  if (S.renamingRun === r.id) {
    return [
      h('button', {
        class: 'btn', 'data-size': 'sm', 'data-variant': 'primary',
        onclick: () => saveRunName(r, rerender),
      }, 'Save'),
      h('button', {
        class: 'btn', 'data-size': 'sm',
        onclick: () => {
          S.renamingRun = null;
          S.renameDraft = '';
          rerender();
        },
      }, 'Cancel'),
    ];
  }
  return [
    h('button', {
      class: 'icon-btn', title: 'Resume this session',
      onclick: () => resume(r),
    }, icon(ICONS.resume)),
    h('button', {
      class: 'icon-btn', title: 'Name this session',
      onclick: () => {
        S.renamingRun = r.id;
        S.renameDraft = r.label || '';
        rerender();
        const input = document.getElementById('run-rename');
        if (input) { input.focus(); input.select(); }
      },
    }, icon(ICONS.rename)),
    h('button', {
      class: 'icon-btn icon-btn--danger', title: 'Delete this session',
      onclick: async () => {
        const ok = window.confirm(
          `Delete "${r.label || r.title}" and all its collected work and scores? `
          + 'This cannot be undone.');
        if (!ok) return;
        await guard(async () => {
          await api.deleteRun(r.id);
          await loadRuns();
        }, 'Could not delete');
        rerender();
        toast('Session deleted.');
      },
    }, icon(ICONS.delete)),
  ];
}

async function saveRunName(r, rerender) {
  const name = (S.renameDraft || '').trim();
  S.renamingRun = null;
  S.renameDraft = '';
  await guard(async () => {
    await api.labelRun(r.id, name);
    await loadRuns();
  }, 'Could not rename');
  rerender();
}

/** One saved session: what it is, how far it got, and what can be done with
 *  it. The launcher and the all-sessions panel show the same thing, so they
 *  are the same row. */
function sessionRow(r, resume, rerender) {
  const renaming = S.renamingRun === r.id;
  // The model is only worth naming when it is not the one that is configured
  // now. Otherwise every row repeated the same string.
  const oddModel = r.model && r.model !== S.model?.selected;
  return h('div', {
    class: 'session-row',
    // The whole row resumes the session. The buttons inside it keep their own
    // meaning, and a row being renamed does nothing when it is clicked.
    onclick: (e) => {
      if (renaming) return;
      if (e.target.closest('button, input')) return;
      resume(r);
    },
  },
    h('div', { class: 'session-main' },
      renaming
        ? h('input', {
            class: 'input', id: 'run-rename',
            value: S.renameDraft,
            placeholder: 'Name this session',
            oninput: (e) => { S.renameDraft = e.target.value; },
            onkeydown: (e) => {
              if (e.key === 'Enter') { e.preventDefault(); saveRunName(r, rerender); }
              if (e.key === 'Escape') {
                e.preventDefault();
                S.renamingRun = null;
                S.renameDraft = '';
                rerender();
              }
            },
          })
        : h('button', {
            class: 'session-title', type: 'button',
            title: 'Resume this session',
            onclick: (e) => { e.stopPropagation(); resume(r); },
          }, r.label || r.title),
      h('div', { class: 'session-meta' },
        h('span', {}, `course ${r.course_id} · assignment ${r.assignment_id}`),
        r.published ? h('span', {}, `${r.published} published`) : null,
        statusChip(r.status),
        h('span', { class: 'session-resume' }, `resume at ${resumeStage(r)}`),
        oddModel ? h('span', { class: 'session-model' }, r.model) : null)),
    h('div', { class: 'session-progress' },
      h('span', { class: 'session-count' }, `${r.graded || 0}/${r.total || 0}`),
      h('span', { class: 'session-count-label' }, 'graded')),
    h('div', { class: 'session-actions' }, sessionActions(r, resume, rerender)));
}

/** The launcher: recent gradings, pick one. */
function viewLauncher() {
  const runs = S.savedRuns;
  const recent = runs.slice(0, 8);
  const resume = (r) => { S.launching = false; resumeRun(r); };
  const rerender = () => render();
  return h('div', {},
    h('div', { class: 'head' },
      h('h1', {}, 'Grading Pipeline'),
      h('p', {}, 'Pick up where you left off, or start something new.')),
    runs.length
      ? h('div', { class: 'card card--listing' },
          h('div', { class: 'card-head' },
            h('h2', {}, 'Recent sessions'),
            h('div', { class: 'note' }, `${runs.length} saved`)),
          h('div', { class: 'session-list' }, recent.map((r) => sessionRow(r, resume, rerender))))
      : card('No saved sessions yet', null,
          h('p', { class: 'hint' }, 'Sessions appear here automatically once you '
            + 'start one. There is nothing to save.')),
    runs.length
      ? h('div', { class: 'page-foot page-foot--end', style: 'gap:8px' },
          runs.length > recent.length
            ? h('button', {
                class: 'btn',
                onclick: () => { S.launching = false; S.panel = { kind: 'runs', rowId: null }; render(); },
              }, `All ${runs.length} sessions`)
            : null,
          h('button', {
            class: 'btn', 'data-variant': 'primary',
            onclick: () => { S.launching = false; render(); },
          }, 'Start something new'))
      : h('div', { class: 'page-foot page-foot--end' },
          h('button', {
            class: 'btn', 'data-variant': 'primary',
            onclick: () => { S.launching = false; render(); },
          }, 'Start something new')));
}

async function resumeRun(run) {
  await guard(async () => {
    S.run = await api.run(run.id);
    await refreshSubmissions();
    S.runModel = run.model || '';
    S.launching = false;
    go(resumeStage(run, S.submissions));
    toast(`Resumed ${run.label || run.title}.`);
  }, 'Could not open that run');
}

/** The Load courses button, with its busy state built in. */
function loadingCoursesButton(label) {
  return busyButton(label, `${label.replace(/^Load/, 'Loading')}…`, S.loadingCourses,
    () => guard(loadCourses, 'Could not load courses'));
}

/** A button whose work is slow. It spins and says what it is doing, rather
 *  than sitting inert through the wait. */
function busyButton(label, busyLabel, busy, onclick, attrs = {}) {
  const { disabled, ...rest } = attrs;
  return h('button', {
    class: 'btn',
    ...rest,
    disabled: busy || disabled,
    onclick,
  },
    busy ? h('span', { class: 'spinner', 'aria-hidden': 'true' }) : null,
    busy ? busyLabel : label);
}

/** A secret field with its reveal control inside the field's own frame.
 *
 *  The control sits over the input's right edge rather than beside it. A
 *  button beside the field made the field narrower than its neighbours, and
 *  the column of inputs lost its right edge. The reveal also belonged to the
 *  field it reveals: it used to sit next to the Canvas URL and toggled the
 *  API token instead.
 */
function secretField(id, placeholder, value, onInput) {
  const input = h('input', {
    class: 'input', id, type: 'password', placeholder,
    value,
    oninput: (e) => onInput(e.target.value),
  });
  const toggle = h('button', {
    class: 'field-trailing', type: 'button', 'aria-pressed': 'false',
    onclick: () => {
      const hidden = input.type === 'password';
      input.type = hidden ? 'text' : 'password';
      toggle.textContent = hidden ? 'hide' : 'show';
      toggle.setAttribute('aria-pressed', String(hidden));
    },
  }, 'show');
  return h('div', { class: 'field-control' }, input, toggle);
}

async function loadCourses() {
  // Canvas is asked for every course, all pages of them, so on an account with
  // a hundred of them this is a multi-second wait. The button used to sit there
  // unchanged through the whole of it, which reads as broken rather than busy
  // and invites clicking it again.
  S.loadingCourses = true;
  render();
  try {
    // Not `.catch(() => null)`. A rejected token makes Canvas answer 401, and
    // swallowing that rendered "No courses loaded yet" -- indistinguishable
    // from not having clicked the button, with nothing in any log.
    const data = await api.courses();
    S.courses = data.courses;
  } finally {
    S.loadingCourses = false;
    // Without this the button appears to do nothing: the state changed but
    // nothing was repainted.
    render();
  }
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
    // Repaint after the refresh, not before it: the rows that came back are
    // the ones on screen, and without this the list kept showing the state
    // from before the refresh.
    if (S.run) refreshSubmissions().then(() => render());
  }, 500);
}

bridge.events(
  (msg) => {
    log(msg.event, msg.data);
    if (msg.event === 'run.ready') {
      S.bootstrapping = false;
      // The id is enough to fetch the run from. It is deliberately not put
      // into S.run: a run with nothing but an id has no title and no
      // criteria, and the spine reads both, so a repaint in the moment
      // between the event and the fetch threw.
      refreshRun(msg.data.run_id);
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
    if (msg.event === 'grade.stopped' && msg.data && msg.data.submission_row_id) {
      // The row is on its way back to ungraded; drop the flag so the button
      // comes back if the person starts it again.
      S.stoppingRows.delete(msg.data.submission_row_id);
      render();
    }
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

async function refreshRun(runId) {
  const id = runId || S.run?.run_id;
  if (!id) return;
  const run = await api.run(id).catch(() => null);
  if (!run) {
    // The run could not be read. Leaving the previous one in place is
    // better than showing one with no title and no criteria.
    render();
    return;
  }
  S.run = run;
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

  // Saved sessions first: the launcher is the first thing seen, so it has to be
  // populated before the first paint rather than after it.
  await loadRuns().catch(() => {});
  if (!S.status?.canvas_configured) await loadCourses();
  render();
 } catch (err) {
   fatal(err, 'setting up');
 }
})();