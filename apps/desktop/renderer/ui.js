/* Small DOM + markdown helpers. No framework, no build step. */

export function h(tag, attrs = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k === 'html') el.innerHTML = v;
    else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
    else if (k === 'dataset') Object.assign(el.dataset, v);
    else if (v === true) el.setAttribute(k, '');
    else el.setAttribute(k, v);
  }
  for (const kid of kids.flat(3)) {
    if (kid === null || kid === undefined || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

export const clear = (el) => { while (el.firstChild) el.removeChild(el.firstChild); return el; };

export function mount(el, ...kids) {
  clear(el);
  for (const kid of kids.flat(3)) if (kid) el.append(kid);
  return el;
}

export const fmt = (n) =>
  n === null || n === undefined ? '—' : (Math.round(n * 100) / 100).toString();

export function clock(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  return Number.isNaN(+d) ? '' : d.toTimeString().slice(0, 8);
}

export function pluralise(n, one, many) {
  return `${n} ${n === 1 ? one : many}`;
}

/* ------------------------------------------------------------------ chips */

export function chip(text, tone) {
  return h('span', { class: 'chip', dataset: tone ? { tone } : {} }, text);
}

export function statusChip(status) {
  const map = {
    graded: ['graded', 'ok'], collected: ['collected', 'accent'],
    pending: ['not started', null], grading: ['grading', 'flag'],
    collecting: ['collecting', 'flag'], needs_review: ['needs review', 'flag'],
    failed: ['failed', 'bad'], collect_failed: ['collect failed', 'bad'],
    no_submission: ['no submission', null], published: ['published', 'ok'],
  };
  const [label, tone] = map[status] || [status, null];
  return chip(label, tone);
}

/* ----------------------------------------------------------- marks strip */

/**
 * The signature element: one proportional bar per criterion with the exact
 * fraction beside it. Amber only when a human must look.
 */
export function marksStrip(criteria, scores, { flagKeys = [] } = {}) {
  const wrap = h('div', { class: 'marks' });
  for (const c of criteria) {
    const got = scores ? scores[c.id] : null;
    const pct = got === null || got === undefined || !c.points
      ? 0 : Math.max(0, Math.min(100, (got / c.points) * 100));
    const tone = flagKeys.includes(c.id) ? 'flag' : pct >= 99 ? 'ok' : null;
    wrap.append(
      h('div', { class: 'mark-row', dataset: tone ? { tone } : {} },
        h('div', {},
          h('div', { class: 'mark-name', title: c.title }, c.title),
          h('div', { class: 'mark-bar' },
            h('div', { class: 'mark-fill', style: `width:${pct}%` })),
        ),
        h('div', { class: 'mark-score' },
          got === null || got === undefined ? '—' : fmt(got),
          h('span', { class: 'of' }, ` / ${fmt(c.points)}`)),
      ),
    );
  }
  return wrap;
}

/* --------------------------------------------------------------- markdown */

const esc = (s) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;')
  .replace(/>/g, '&gt;').replace(/"/g, '&quot;');

function inline(s) {
  return esc(s)
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/(^|[\s(])\*([^*\n]+)\*/g, '$1<em>$2</em>')
    .replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g, '<a href="$2">$1</a>');
}

/**
 * Deliberately small: the reports this renders use a known subset of markdown
 * (headings, paragraphs, lists, tables, code, rules, quotes, emphasis).
 */
export function markdown(src) {
  if (!src) return '';
  const lines = String(src).replace(/\r\n?/g, '\n').split('\n');
  const out = [];
  let i = 0;

  const flushList = (tag, items) => out.push(`<${tag}>${items.map((t) => `<li>${inline(t)}</li>`).join('')}</${tag}>`);

  while (i < lines.length) {
    const line = lines[i];

    if (!line.trim()) { i++; continue; }

    const fence = line.match(/^```(\w*)\s*$/);
    if (fence) {
      const body = [];
      i++;
      while (i < lines.length && !/^```\s*$/.test(lines[i])) body.push(lines[i++]);
      i++;
      out.push(`<pre><code>${esc(body.join('\n'))}</code></pre>`);
      continue;
    }

    if (/^(-{3,}|\*{3,}|_{3,})\s*$/.test(line)) { out.push('<hr>'); i++; continue; }

    const h = line.match(/^(#{1,6})\s+(.*)$/);
    if (h) { out.push(`<h${h[1].length}>${inline(h[2])}</h${h[1].length}>`); i++; continue; }

    if (/^\|/.test(line) && /^\|[\s:|-]+\|?\s*$/.test(lines[i + 1] || '')) {
      const head = splitRow(line);
      i += 2;
      const body = [];
      while (i < lines.length && /^\|/.test(lines[i])) body.push(splitRow(lines[i++]));
      out.push(
        `<table><thead><tr>${head.map((c) => `<th>${inline(c)}</th>`).join('')}</tr></thead>` +
        `<tbody>${body.map((r) => `<tr>${r.map((c) => `<td>${inline(c)}</td>`).join('')}</tr>`).join('')}</tbody></table>`,
      );
      continue;
    }

    const quote = line.match(/^>\s?(.*)$/);
    if (quote) {
      const body = [];
      while (i < lines.length && /^>\s?/.test(lines[i])) body.push(lines[i++].replace(/^>\s?/, ''));
      out.push(`<blockquote>${inline(body.join(' '))}</blockquote>`);
      continue;
    }

    const bullet = line.match(/^\s*[-*+]\s+(.*)$/);
    const ordered = line.match(/^\s*\d+[.)]\s+(.*)$/);
    if (bullet || ordered) {
      const items = [];
      while (i < lines.length) {
        const m = lines[i].match(/^\s*[-*+]\s+(.*)$/) || lines[i].match(/^\s*\d+[.)]\s+(.*)$/);
        if (!m) break;
        items.push(m[1]);
        i++;
      }
      flushList(bullet ? 'ul' : 'ol', items);
      continue;
    }

    const para = [];
    while (i < lines.length && lines[i].trim()
           && !/^(#{1,6}\s|[-*+]\s|\d+[.)]\s|>|\||```)/.test(lines[i])) para.push(lines[i++]);
    if (para.length) out.push(`<p>${inline(para.join(' '))}</p>`);
    else i++;
  }
  return out.join('\n');
}

const splitRow = (l) => l.trim().replace(/^\||\|$/g, '').split('|').map((c) => c.trim());

/* ------------------------------------------------------------------ misc */

export function empty(title, detail, action) {
  return h('div', { class: 'empty' },
    h('div', { class: 'big' }, title),
    detail ? h('div', {}, detail) : null,
    action || null);
}

export function notice(text, tone) {
  return h('div', { class: 'notice', dataset: tone ? { tone } : {} }, text);
}

export function field(label, control, hint) {
  return h('div', { class: 'field' },
    h('label', {}, label),
    control,
    hint ? h('div', { class: 'hint' }, hint) : null);
}

export function card(title, note, ...body) {
  return h('div', { class: 'card' },
    title ? h('div', { class: 'card-head' },
      h('h2', {}, title),
      note ? h('div', { class: 'note' }, note) : null) : null,
    ...body);
}