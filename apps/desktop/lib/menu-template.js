'use strict';

/**
 * The application menu, as a pure function of the platform.
 *
 * Extracted from main.js so it can be tested without an Electron process.
 * It was inline, and the only test covering it grepped main.js for the string
 * `editMenu` -- which passes on formatting and on meaning equally well, and
 * did not notice the adjacent code threw on every use.
 */

/**
 * Menu template for the given platform.
 *
 * `isMac` rather than `process.platform` so both platforms can be exercised
 * from one test run.
 */
function applicationMenuTemplate(isMac, handlers = {}) {
  return [
    ...(isMac ? [{ role: 'appMenu' }] : []),
    {
      label: 'File',
      submenu: [
        {
          label: 'Open workspace folder',
          click: handlers.openWorkspace,
        },
        { type: 'separator' },
        isMac ? { role: 'close' } : { role: 'quit' },
      ],
    },
    // The Edit role, not a hand-built submenu: on macOS these accelerators
    // live in the application menu, so an app that omits this role cannot
    // paste, copy or select-all with the keyboard at all, and the default
    // right-click menu is suppressed along with them. Everything still works
    // with the mouse, which is how this goes unnoticed.
    { role: 'editMenu' },
    {
      label: 'View',
      submenu: [
        {
          label: 'Toggle theme',
          accelerator: 'CmdOrCtrl+Shift+D',
          click: handlers.toggleTheme,
        },
        { type: 'separator' },
        { role: 'reload' }, { role: 'toggleDevTools' },
        { type: 'separator' },
        { role: 'resetZoom' }, { role: 'zoomIn' }, { role: 'zoomOut' },
        { role: 'togglefullscreen' },
      ],
    },
    { role: 'windowMenu' },
  ];
}

module.exports = { applicationMenuTemplate };