'use strict';

/**
 * The right-click Edit menu for text fields.
 *
 * Extracted from main.js as a pure function so it can be tested. It was inline
 * before, and the only test covering it asserted that the source file mentioned
 * a role -- which passed while the code threw `flags.includes is not a
 * function` on every right-click, because `editFlags` is not an array.
 */

/**
 * Build the menu template for an editable element.
 *
 * `editFlags` is an object of booleans (`canCut`, `canCopy`, `canPaste`,
 * `canSelectAll`), despite being documented in a way that reads like a list. An
 * array form is accepted too, because Electron has shipped both shapes across
 * versions and a right-click must never throw: an exception here comes up as an
 * uncaught error in the main process and a modal dialog, not as a missing menu.
 *
 * Returns an empty template when there is nothing to offer, which the caller
 * treats as "show nothing rather than show a broken menu".
 */
function editMenuTemplate(params) {
  if (!params || !params.isEditable) return [];

  const raw = params.editFlags;
  const offers = (flag) => {
    if (!raw) return false;
    if (Array.isArray(raw)) return raw.includes(flag);
    if (typeof raw === 'string') return raw.split(',').includes(flag);
    // The documented shape: { canCut: true, ... }.
    const key = `can${flag.charAt(0).toUpperCase()}${flag.slice(1)}`;
    return raw[key] === true;
  };

  const items = [];
  if (offers('cut')) items.push({ role: 'cut', label: 'Cut' });
  if (offers('copy')) items.push({ role: 'copy', label: 'Copy' });
  if (offers('paste')) items.push({ role: 'paste', label: 'Paste' });
  if (items.length) items.push({ type: 'separator' });
  if (offers('selectAll')) items.push({ role: 'selectAll', label: 'Select All' });
  return items;
}

module.exports = { editMenuTemplate };