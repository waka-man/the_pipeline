'use strict';

/**
 * Electron main process.
 *
 * Owns three child processes and the window:
 *   - the Python sidecar (pipeline.server), which does every real operation
 *   - opencode serve, which runs the grading agent
 *   - neither is visible to the user; the renderer only ever talks HTTP
 *
 * The sidecar's interpreter is resolved so the same code runs from a checkout
 * during development and from a bundled runtime in a packaged app.
 */

const { app, BrowserWindow, shell, Menu, dialog, ipcMain } = require('electron');
const { spawn, spawnSync } = require('child_process');
const http = require('http');
const path = require('path');
const fs = require('fs');

const HOST = '127.0.0.1';
const SIDECAR_TIMEOUT_MS = 30_000;

let win = null;
let sidecar = null;
let sidecarPort = null;

// --------------------------------------------------------------- sidecar

/** Walk up from this file looking for the checkout that holds the sidecar. */
function findRepoRoot() {
  let dir = __dirname;
  for (let i = 0; i < 6; i++) {
    if (fs.existsSync(path.join(dir, 'sidecar', 'pipeline', 'server.py'))) return dir;
    const up = path.dirname(dir);
    if (up === dir) break;
    dir = up;
  }
  return null;
}

function candidates(root) {
  const list = [];

  if (app.isPackaged) {
    // A bundled runtime ships as resources/sidecar with its venv inside.
    const res = process.resourcesPath || '';
    const isWin = process.platform === 'win32';
    list.push(path.join(res, 'sidecar', '.venv', isWin ? 'Scripts' : 'bin',
      isWin ? 'python.exe' : 'python'));
    list.push(path.join(res, 'sidecar', 'python', isWin ? '' : 'bin',
      isWin ? 'python.exe' : 'python3'));
  }

  if (process.env.GRADING_PIPELINE_PYTHON) list.push(process.env.GRADING_PIPELINE_PYTHON);

  if (root) {
    const isWin = process.platform === 'win32';
    list.push(path.join(root, '.venv', isWin ? 'Scripts' : 'bin',
      isWin ? 'python.exe' : 'python'));
  }

  if (process.platform !== 'win32') {
    list.push('/usr/bin/python3', '/usr/local/bin/python3', '/usr/bin/python');
  } else {
    // The launcher shims live beside Python on Windows, not on PATH by default.
    const local = process.env.LOCALAPPDATA;
    if (local) {
      list.push(path.join(local, 'Programs', 'Python', 'Python312', 'python.exe'));
      list.push(path.join(local, 'Programs', 'Python', 'Python313', 'python.exe'));
    }
    list.push('py', 'python');
  }
  return list.filter(Boolean);
}

function interpreterWithSidecar() {
  const root = findRepoRoot();
  if (!root) return null;
  const wanted = path.join(root, 'sidecar');
  const attempts = [];
  for (const exe of candidates(root)) {
    const probe = spawnSync(exe, ['-c',
      'import sys;sys.path.insert(0,sys.argv[1]);import pipeline.server',
      wanted], { encoding: 'utf8', timeout: 20000 });
    if (!probe.error && probe.status === 0) return { exe, codeRoot: wanted };
    attempts.push(`${exe}: ${(probe.stderr || probe.error?.message || '').trim().split('\n').pop() || `exit ${probe.status}`}`);
  }
  console.error('[sidecar] no usable interpreter:\n  ' + attempts.join('\n  '));
  return null;
}

function portFor(url) {
  return new Promise((resolve, reject) => {
    const req = http.get(url, (res) => { res.resume(); resolve(res.statusCode); });
    req.on('error', reject);
    req.setTimeout(2000, () => { req.destroy(); reject(new Error('timeout')); });
  });
}

async function startSidecar() {
  const found = interpreterWithSidecar();
  if (!found) {
    throw new Error(
      'Could not find a Python runtime with the sidecar importable.\n\n' +
      'Set GRADING_PIPELINE_PYTHON to an interpreter that can import pipeline.server.'
    );
  }

  const { exe, codeRoot } = found;
  sidecar = spawn(exe, ['-c', [
    'import sys, json',
    `sys.path.insert(0, ${JSON.stringify(codeRoot)})`,
    'from pipeline.server import serve',
    'httpd, port = serve()',
    "print(json.dumps({'port': port}), flush=True)",
    'import time',
    'while True: time.sleep(3600)',
  ].join('\n')], {
    env: {
      ...process.env,
      PYTHONUNBUFFERED: '1',
      GRADING_PIPELINE_HOME: app.getPath('userData'),
      GRADING_PIPELINE_RENDERER: path.join(__dirname, 'renderer'),
    },
    stdio: ['ignore', 'pipe', 'pipe'],
    // Own process group on POSIX, so opencode (a child of the sidecar) is
    // signalled together with the sidecar. On Windows `detached` would instead
    // spawn a visible console window, so it is left off there and the kill is
    // done with taskkill's process-tree flag instead.
    detached: process.platform !== 'win32',
  });

  let stderr = '';
  sidecar.stderr.on('data', (d) => { stderr += d.toString(); });

  sidecarPort = await new Promise((resolve, reject) => {
    const timer = setTimeout(
      () => reject(new Error(`sidecar did not start.\n${stderr.slice(-1500)}`)),
      SIDECAR_TIMEOUT_MS);
    let buf = '';
    sidecar.stdout.on('data', (d) => {
      buf += d.toString();
      const m = buf.match(/\{"port":\s*(\d+)\}/);
      if (m) { clearTimeout(timer); resolve(Number(m[1])); }
    });
    sidecar.on('exit', (code) => {
      clearTimeout(timer);
      reject(new Error(`sidecar exited (${code}).\n${stderr.slice(-1500)}`));
    });
  });

  // Wait for the port to answer before the window asks it anything.
  const health = `http://${HOST}:${sidecarPort}/health`;
  const deadline = Date.now() + 10_000;
  for (;;) {
    try { if ((await portFor(health)) === 200) return sidecarPort; } catch (_) {}
    if (Date.now() > deadline) throw new Error('sidecar never became healthy');
    await new Promise((r) => setTimeout(r, 200));
  }
}

function stopSidecar() {
  if (!sidecar) return;
  const pid = sidecar.pid;
  if (process.platform === 'win32') {
    // Process groups do not exist on Windows, and killing the sidecar alone
    // would orphan opencode holding its port. taskkill /T walks the tree.
    try {
      spawnSync('taskkill', ['/pid', String(pid), '/T', '/F'], { stdio: 'ignore' });
    } catch (_) {}
    try { sidecar.kill(); } catch (_) {}
  } else {
    try {
      // A negative pid targets the process group, which includes opencode.
      process.kill(-pid, 'SIGTERM');
    } catch (_) {
      try { process.kill(pid, 'SIGTERM'); } catch (_) {}
    }
  }
  sidecar = null;
}

// ----------------------------------------------------------------- window

function baseUrl() {
  return `http://${HOST}:${sidecarPort}`;
}

async function createWindow() {
  win = new BrowserWindow({
    width: 1440,
    height: 940,
    minWidth: 1040,
    minHeight: 680,
    backgroundColor: '#0e0f11',
    title: 'Grading Pipeline',
    titleBarStyle: process.platform === 'darwin' ? 'hiddenInset' : 'default',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
    },
  });

  const icon = path.join(__dirname, 'renderer', 'icon.png');
  if (fs.existsSync(icon)) win.setIcon(icon);

  // A start stage can be requested for development and for deep links.
  // Served over HTTP by the sidecar: Chromium blocks ES modules on file://.
  const startStage = process.env.GRADING_PIPELINE_STAGE;
  const theme = process.env.GRADING_PIPELINE_THEME;
  const query = theme ? `?theme=${encodeURIComponent(theme)}` : '';
  const hash = startStage ? `#${startStage}` : '';
  const target = `${baseUrl()}/app/${query}${hash}`;
  console.log('[main] loading', target);
  await win.loadURL(target);

  // Surface renderer errors in the main log; otherwise they vanish into devtools.
  // Electron >=30 passes a single details object; older builds pass positional
  // arguments. Accept both so renderer errors always reach the main log.
  win.webContents.on('console-message', (...args) => {
    const d = args[1] && typeof args[1] === 'object' ? args[1] : null;
    const level = d ? d.level : args[1];
    const message = d ? d.message : args[2];
    const line = d ? d.lineNumber : args[3];
    const source = d ? d.sourceId : args[4];
    if (level >= 2) console.log(`[renderer ${source}:${line}] ${message}`);
  });
  win.webContents.on('render-process-gone', (_e, details) =>
    console.error('[renderer gone]', JSON.stringify(details)));
  win.webContents.on('preload-error', (_e, file, err) =>
    console.error('[preload error]', file, err.message));

  win.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: 'deny' };
  });

  win.on('closed', () => { win = null; });

  // Raise rather than sit behind whatever was in front of it.
  win.once('ready-to-show', () => { win.show(); win.focus(); win.moveTop(); });
  win.show();

  // Development aid: capture the window itself rather than relying on whatever
  // screenshot tool and window manager happen to be on the machine.
  const capture = process.env.GRADING_PIPELINE_CAPTURE;
  if (capture) {
    setTimeout(async () => {
      try {
        // An occluded window produces no compositor frames, so a hidden
        // window captures blank. Raise it for the shot, then get out of the way.
        win.setAlwaysOnTop(true);
        win.show();
        win.focus();
        win.moveTop();
        await new Promise((r) => setTimeout(r, 1200));
        const image = await win.webContents.capturePage();
        win.setAlwaysOnTop(false);
        fs.writeFileSync(capture, image.toPNG());
        console.log('[capture] wrote', capture);
      } catch (err) {
        console.error('[capture] failed:', err.message);
      }
    }, Number(process.env.GRADING_PIPELINE_CAPTURE_DELAY || 6000));
  }
}

function menu() {
  const isMac = process.platform === 'darwin';
  Menu.setApplicationMenu(Menu.buildFromTemplate([
    ...(isMac ? [{ role: 'appMenu' }] : []),
    {
      label: 'File',
      submenu: [
        {
          label: 'Open workspace folder',
          click: () => win?.webContents.send('menu', 'open-workspace'),
        },
        { type: 'separator' },
        isMac ? { role: 'close' } : { role: 'quit' },
      ],
    },
    {
      label: 'View',
      submenu: [
        { label: 'Toggle theme', accelerator: 'CmdOrCtrl+Shift+D', click: () => win?.webContents.send('menu', 'toggle-theme') },
        { type: 'separator' },
        { role: 'reload' }, { role: 'toggleDevTools' },
        { type: 'separator' },
        { role: 'resetZoom' }, { role: 'zoomIn' }, { role: 'zoomOut' },
        { role: 'togglefullscreen' },
      ],
    },
    { role: 'windowMenu' },
  ]));
}

// -------------------------------------------------------------------- ipc

ipcMain.handle('app:info', async () => ({
  baseUrl: baseUrl(),
  platform: process.platform,
  version: app.getVersion(),
  userData: app.getPath('userData'),
}));

ipcMain.handle('app:open-path', async (_e, p) => {
  if (p && fs.existsSync(p)) shell.openPath(p);
});

ipcMain.handle('app:reveal-path', async (_e, p) => {
  if (p && fs.existsSync(p)) shell.showItemInFolder(p);
});

ipcMain.handle('app:restart-sidecar', async () => {
  // The sidecar binds an ephemeral port, so a restart lands on a new origin.
  // Reloading would re-request the old, now-dead URL and show a browser error
  // page. Navigate to the new origin instead, keeping the user where they were.
  const previous = win?.webContents.getURL() || '';
  const hash = (() => { try { return new URL(previous).hash; } catch (_) { return ''; } })();
  stopSidecar();
  sidecarPort = await startSidecar();
  if (win) {
    await win.loadURL(`${baseUrl()}/app/${hash}`);
    win.show();
    win.focus();
  }
  return baseUrl();
});

// ------------------------------------------------------------------ boot

app.whenReady().then(async () => {
  menu();
  try {
    await startSidecar();
  } catch (err) {
    dialog.showErrorBox('Grading Pipeline could not start', String(err.message || err));
    app.quit();
    return;
  }
  await createWindow();

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on('window-all-closed', () => { if (process.platform !== 'darwin') app.quit(); });
app.on('before-quit', stopSidecar);
process.on('exit', stopSidecar);