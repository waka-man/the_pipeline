#!/usr/bin/env node
'use strict';

/**
 * Check that the packaged desktop app contains everything main.js needs.
 *
 * A helper under lib/ that is left out of build.files makes the first require
 * in main.js throw. That happens while the application menu is built, before
 * the sidecar is spawned, so the app appears to launch and then nothing
 * happens: the process runs, the launcher reports the app is up, and there is
 * no sidecar. It reads as a hang rather than a packaging mistake.
 *
 * Run against a built app:
 *   npx electron-builder --dir && node scripts/check_packaged_app.js
 *
 * Takes the path so it works for any platform's output directory, rather than
 * assuming Linux.
 */

const fs = require('node:fs');
const path = require('node:path');

const ROOT = path.join(__dirname, '..');
const asarDir = process.argv[2];
if (!asarDir) {
  console.error('usage: node scripts/check_packaged_app.js <path to app.asar>');
  process.exit(2);
}
if (!fs.existsSync(asarDir)) {
  console.error(`no asar at ${asarDir}; build the app first`);
  process.exit(2);
}

const asar = require(path.join(ROOT, 'apps/desktop/node_modules/@electron/asar'));
const inAsar = new Set(
  asar.listPackage(asarDir).map((f) => f.split(path.sep).join('/')));

// Every module main.js requires relatively, plus the entry points. Read from
// the source rather than a hand-written list, so a new require is covered by
// being added.
const main = fs.readFileSync(path.join(ROOT, 'apps/desktop/main.js'), 'utf8');
// Node resolves a relative require to the .js file; the asar holds that file,
// so the extension has to be put back on before looking it up.
const required = [...main.matchAll(/require\('\.\/([^']+)'\)/g)]
  .map((m) => m[1])
  .map((m) => (path.extname(m) ? m : `${m}.js`));
const needed = [...new Set([...required, 'main.js', 'preload.js'])];

const missing = needed.filter((m) => !inAsar.has(`/${m}`));
if (missing.length) {
  console.error('the packaged app is missing: ' + missing.join(', '));
  console.error('they must be inside build.files in apps/desktop/package.json');
  process.exit(1);
}
console.log(`every main-process module is packaged: ${needed.join(', ')}`);