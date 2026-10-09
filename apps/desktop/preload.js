'use strict';

/**
 * The only bridge between the renderer and Node.
 *
 * The renderer gets a typed HTTP client for the sidecar and three filesystem
 * conveniences. It never sees a path it did not ask for, and it has no way to
 * reach anything but the local sidecar.
 */

const { contextBridge, ipcRenderer } = require('electron');

async function req(method, path, body) {
  const { baseUrl } = await ipcRenderer.invoke('app:info');
  const res = await fetch(baseUrl + path, {
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

contextBridge.exposeInMainWorld('pipeline', {
  info: () => ipcRenderer.invoke('app:info'),
  restartSidecar: () => ipcRenderer.invoke('app:restart-sidecar'),
  openPath: (p) => ipcRenderer.invoke('app:open-path', p),
  revealPath: (p) => ipcRenderer.invoke('app:reveal-path', p),
  onMenu: (cb) => ipcRenderer.on('menu', (_e, what) => cb(what)),

  api: {
    status: () => req('GET', '/api/status'),
    saveSettings: (b) => req('POST', '/api/settings', b),
    models: () => req('GET', '/api/models'),
    selectModel: (b) => req('POST', '/api/models/select', b),
    courses: () => req('GET', '/api/courses'),
    assignments: (id) => req('GET', `/api/courses/${id}/assignments`),
    createRun: (b) => req('POST', '/api/runs', b),
    run: (id) => req('GET', `/api/runs/${id}`),
    submissions: (id) => req('GET', `/api/runs/${id}/submissions`),
    collect: (id, b) => req('POST', `/api/runs/${id}/collect`, b || {}),
    grade: (id, b) => req('POST', `/api/runs/${id}/grade`, b || {}),
    render: (id) => req('POST', `/api/runs/${id}/render`, {}),
    publish: (id, b) => req('POST', `/api/runs/${id}/publish`, b),
    report: (row) => req('GET', `/api/reports/${row}`),
    saveReport: (row, markdown) => req('PUT', `/api/reports/${row}`, { markdown }),
  },

  /** Subscribe to the sidecar event stream. Returns an unsubscribe fn. */
  events(onEvent, onError) {
    let closed = false;
    let source = null;
    let retry = null;

    (async () => {
      const { baseUrl } = await ipcRenderer.invoke('app:info');
      const open = () => {
        if (closed) return;
        source = new EventSource(baseUrl + '/api/events');
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
    })();

    return () => {
      closed = true;
      if (retry) clearTimeout(retry);
      if (source) source.close();
    };
  },
});