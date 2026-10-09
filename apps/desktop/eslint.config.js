'use strict';

/**
 * Lint config for the desktop app.
 *
 * `no-undef` is the rule that matters most here. Two shipping bugs were of the
 * same shape: a reference to a name that does not exist, which no amount of
 * string-matching a test file could see, because the handler only runs when a
 * user clicks the button. v0.1.0 shipped a Collect button that threw
 * ReferenceError: args is not defined on every click.
 *
 * The renderer is a browser script and the main process is Node, so they get
 * different globals rather than one permissive union that would hide a Node
 * global used in the renderer.
 */

const globals = require('globals');
const js = require('@eslint/js');

const nodeGlobals = {
  ...globals.node,
  ...globals.es2024,
  // Global since Node 22 but absent from the bundled globals package.
  EventSource: 'readonly',
};

const rendererGlobals = {
  ...globals.browser,
  ...globals.es2024,
};

module.exports = [
  {
    ignores: ['node_modules/**', 'dist/**', 'resources/**'],
  },
  js.configs.recommended,
  {
    files: ['main.js', 'preload.js', 'test/**/*.js', 'eslint.config.js'],
    languageOptions: {
      ecmaVersion: 2024,
      sourceType: 'commonjs',
      globals: nodeGlobals,
    },
    rules: {
      'no-undef': 'error',
      'no-unused-vars': ['error', { args: 'none', caughtErrors: 'none' }],
      eqeqeq: ['error', 'smart'],
      'prefer-const': 'error',
      'no-var': 'error',
      // Empty catch blocks are used deliberately to mean "nothing to clean up".
      'no-empty': ['error', { allowEmptyCatch: true }],
    },
  },
  {
    files: ['renderer/**/*.js'],
    languageOptions: {
      ecmaVersion: 2024,
      sourceType: 'module',
      globals: rendererGlobals,
    },
    rules: {
      'no-undef': 'error',
      'no-unused-vars': ['error', { args: 'none', caughtErrors: 'none' }],
      eqeqeq: ['error', 'smart'],
      'prefer-const': 'error',
      'no-var': 'error',
      'no-empty': ['error', { allowEmptyCatch: true }],
    },
  },
];
