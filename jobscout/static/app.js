/* jobscout — the board, rewritten.
 *
 * One file, five views, no framework. Everything that comes from a job advert
 * is set as textContent; the only markup this file builds is its own. The CSP
 * forbids inline style, so widths and colours are classes and SVG attributes.
 * State lives in the URL hash so a reload lands where you were. */

'use strict';

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];

/* h(tag, attrs, ...children): the one DOM builder. Strings become text nodes,
 * never markup. `class`, `dataset`, `aria-*`, `on*` handled; everything else
 * is an attribute. */
function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k === 'dataset') Object.assign(el.dataset, v);
    else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
    else if (k === 'text') el.textContent = v;
    else if (v === true) el.setAttribute(k, '');
    else el.setAttribute(k, v);
  }
  for (const c of children.flat()) {
    if (c == null || c === false) continue;
    el.appendChild(typeof c === 'string' || typeof c === 'number' ? document.createTextNode(String(c)) : c);
  }
  return el;
}
const svgEl = (tag, attrs) => {
  const el = document.createElementNS('http://www.w3.org/2000/svg', tag);
  for (const [k, v] of Object.entries(attrs || {})) if (v != null) el.setAttribute(k, v);
  return el;
};

const fmt = n => n == null ? '—' : '$' + Math.round(n).toLocaleString();
const plural = (n, w) => `${n.toLocaleString()} ${w}${n === 1 ? '' : 's'}`;

/* Every URL here came out of a job advert. Three schemes, nothing else. */
function safeUrl(raw) {
  if (!raw) return null;
  try {
    const url = new URL(String(raw), location.origin);
    return ['http:', 'https:', 'mailto:'].includes(url.protocol) ? url.href : null;
  } catch { return null; }
}

/* ---------------------------------------------------------------- token */
let TOKEN = new URLSearchParams(location.search).get('t') || '';
if (TOKEN) history.replaceState(null, '', location.pathname + location.hash);
if (!TOKEN) {
  fetch('/api/csrf', { credentials: 'same-origin' })
    .then(r => r.ok ? r.json() : null).then(d => { if (d && d.token) TOKEN = d.token; }).catch(() => {});
}

/* ------------------------------------------------------------------ http */
class HttpError extends Error {
  constructor(status, text) { super(text || `HTTP ${status}`); this.status = status; }
}
const inflight = new Map();
async function api(path, key) {
  if (key) { inflight.get(key)?.abort(); }
  const ctl = new AbortController();
  if (key) inflight.set(key, ctl);
  let r;
  try {
    r = await fetch(path, { credentials: 'same-origin', signal: ctl.signal });
  } catch (e) {
    if (e.name === 'AbortError') throw e;
    throw new HttpError(0, 'network');
  }
  if (!r.ok) throw new HttpError(r.status, await r.text().catch(() => ''));
  const data = await r.json();
  const total = r.headers.get('X-Total-Count');
  if (total != null && Array.isArray(data)) data.total = Number(total);
  return data;
}
async function post(path, body) {
  let r;
  try {
    r = await fetch(path, {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'X-Jobscout-Token': TOKEN },
      body: JSON.stringify(body || {}),
    });
  } catch { throw new HttpError(0, 'network'); }
  if (!r.ok) throw new HttpError(r.status, await r.text().catch(() => ''));
  return r.json().catch(() => ({}));
}

/* ---------------------------------------------------------------- banner */
function banner(kind, message, extra) {
  const el = $('#banner');
  el.className = 'banner ' + kind;
  el.replaceChildren(h('span', {}, message), extra || null,
    h('button', { class: 'ghost', onclick: () => { el.hidden = true; } }, 'dismiss'));
  el.hidden = false;
}
function failed(err, what) {
  if (err.name === 'AbortError') return;
  if (err.status === 401 || err.status === 403) {
    banner('error', `Not authorised (${err.status}). The session cookie has expired — reopen the board with the ?t= link printed when it started.`);
  } else if (err.status === 0) {
    banner('error', `The board is not answering. Is python3 board.py still running?`);
  } else {
    banner('error', `${what || 'Request'} failed: ${err.status} ${err.message}`.trim());
  }
}
const say = t => { $('#live').textContent = t; };

/* --------------------------------------------------------------- copying */
const copyTimers = new WeakMap();
async function copyText(text, button) {
  const done = ok => {
    clearTimeout(copyTimers.get(button));
    if (!button.dataset.was) button.dataset.was = button.textContent;
    button.textContent = ok ? 'copied' : 'select & copy';
    button.classList.toggle('copied', ok);
    say(ok ? 'copied to clipboard' : 'could not copy; the text is selected');
    copyTimers.set(button, setTimeout(() => {
      button.textContent = button.dataset.was; button.classList.remove('copied');
    }, 1600));
  };
  try {
    if (navigator.clipboard && window.isSecureContext) { await navigator.clipboard.writeText(text); return done(true); }
  } catch { /* fall through */ }
  const area = h('textarea', { class: 'copysink', readonly: true });
  area.value = text;
  document.body.appendChild(area);
  area.select();
  let ok = false;
  try { ok = document.execCommand('copy'); } catch { ok = false; }
  document.body.removeChild(area);
  done(ok);
}

/* ----------------------------------------------------------------- state */
const DEFAULTS = { view: 'today', q: '', state: '', reach: '', band: '', sort: 'score',
                   mode: 'list', layer: 'timezone', country: '', job: '', cvtab: 'platforms',
                   variant: '', ghost: '' };
const state = { ...DEFAULTS };
const cache = { stats: null, statsAt: 0, world: null, map: null, jobs: [], offset: 0,
                total: 0, selected: new Set(), focus: -1, cv: null, cvList: null, report: null };
const PAGE = 100;

function readHash() {
  const p = new URLSearchParams(location.hash.slice(1));
  for (const k of Object.keys(DEFAULTS)) state[k] = p.has(k) ? p.get(k) : DEFAULTS[k];
}
function writeHash() {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(state)) if (v !== DEFAULTS[k] && v !== '') p.set(k, v);
  const next = '#' + p.toString();
  if (next !== location.hash) history.replaceState(null, '', next || location.pathname);
}
function set(patch, opts = {}) {
  Object.assign(state, patch);
  writeHash();
  if (!opts.silent) render();
}

/* ---------------------------------------------------------------- labels */
const REACH = { likely: ['your level', 'good'], plausible: ['reachable', 'good'],
                stretch: ['a stretch', 'warn'], no_chance: ['no chance', 'bad'] };
const MODE = { remote: ['remote', ''], hybrid: ['hybrid', 'warn'], onsite: ['on-site', 'warn'], unknown: ['mode unknown', ''] };
// stale and restamped share a colour: both mean "read the date with suspicion",
// where ghost means "hidden by default" and fresh needs no chip at all.
const VERDICT = { stale: ['stale', 'warn'], restamped: ['re-stamped', 'warn'], ghost: ['ghost', 'bad'] };
const LABEL = { ONSITE_LK: 'Sri Lanka', OPEN_WORLDWIDE: 'Worldwide', OPEN_REGION: 'Region OK',
                OPEN_CONTRACTOR: 'Contractor', ONSITE_SPONSORED: 'Sponsored', UNKNOWN: 'Needs a read',
                BLOCKED: 'Closed', ONSITE_NO_SPONSOR: 'On-site, no visa' };
const OUTCOME = { waiting: ['waiting', ''], due: ['follow up', 'warn'], stale: ['update this', 'bad'],
                  callback: ['callback', 'good'], rejected: ['rejected', 'bad'], silent: ['silent', ''] };
const SETTLE = { 1: 'OPEN_WORLDWIDE', 2: 'OPEN_REGION', 3: 'OPEN_CONTRACTOR', 0: 'BLOCKED' };

function age(job) {
  const t = job.posted_at || job.first_seen;
  if (!t) return '';
  const d = Math.floor((Date.now() / 1000 - t) / 86400);
  return d <= 0 ? 'today' : d + 'd';
}
function payText(job) {
  if (!job.pay_known) return ['—', 'not published'];
  const { lo, hi } = job;
  if (lo && hi && Math.abs(hi - lo) > 1) return [fmt(hi), 'from ' + fmt(lo)];
  return [fmt(hi || lo), 'per month'];
}
const chip = (text, cls = '') => h('span', { class: 'chip ' + cls }, text);

/* ------------------------------------------------------------------ rows */
function row(job, opts = {}) {
  const [big, small] = payText(job);
  const reach = REACH[job.reach], mode = MODE[job.work_mode];
  const tags = h('div', { class: 'tags' },
    h('span', { class: 'grp' },
      chip(LABEL[job.state] || job.state, 'state' + (job.state === 'UNKNOWN' ? ' warn' : '')),
      reach ? chip(reach[0], reach[1]) : null),
    h('span', { class: 'grp' },
      mode && !['remote', 'unknown'].includes(job.work_mode) ? chip(mode[0], mode[1]) : null,
      job.band ? chip(`${job.overlap_hours}h`, job.band) : null,
      job.country_name ? chip(job.country_name) : null),
    h('span', { class: 'grp' },
      age(job) ? chip(age(job)) : null,
      VERDICT[job.verdict] ? chip(VERDICT[job.verdict][0], VERDICT[job.verdict][1]) : null,
      job.source_count > 1 ? chip(`${job.source_count} boards`) : null));
  const sel = h('input', { class: 'sel', type: 'checkbox', 'aria-label': 'select',
    onclick: e => { e.stopPropagation(); toggleSelect(job.id, e.target.checked, e.target.closest('.row')); } });
  sel.checked = cache.selected.has(job.id);
  const el = h('article', {
    class: 'row' + (sel.checked ? ' selected' : ''), tabindex: 0, role: 'button',
    dataset: { id: job.id }, 'aria-label': `${job.title}, ${job.company}`,
    onclick: () => openPanel(job.id),
    onkeydown: e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openPanel(job.id); } },
  },
    sel,
    h('div', { class: 'who' }, h('div', { class: 'title', text: job.title || '' }),
      h('div', { class: 'co', text: job.company || '' })),
    tags,
    h('div', { class: 'pay' }, h('b', {}, big), h('span', {}, small)),
    h('div', { class: 'score' + (job.score >= 75 ? ' hot' : '') },
      job.score == null ? '–' : Math.round(job.score), h('small', {}, 'score')));
  return el;
}

function skeleton(n = 8) {
  return Array.from({ length: n }, () => h('div', { class: 'row skeleton', 'aria-hidden': 'true' },
    h('span', {}), h('div', { class: 'who' }, h('div', { class: 'title' }, 'loading'), h('div', { class: 'co' }, 'loading')),
    h('div', {}), h('div', { class: 'pay' }, h('b', {}, '0000')), h('div', {})));
}
function emptyState(title, hint, action) {
  return h('div', { class: 'empty' }, h('b', {}, title), hint ? h('span', {}, hint) : null, action || null);
}

function card(job, rank) {
  const [big, small] = payText(job);
  const reach = REACH[job.reach];
  return h('article', { class: 'card', tabindex: 0, role: 'button', dataset: { id: job.id },
    onclick: () => openPanel(job.id),
    onkeydown: e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openPanel(job.id); } } },
    h('div', { class: 'head' },
      h('div', {}, h('div', { class: 'title', text: job.title || '' }), h('div', { class: 'co', text: job.company || '' })),
      h('span', { class: 'rank' }, `#${rank}`)),
    h('div', { class: 'tags' },
      chip(LABEL[job.state] || job.state, 'state'), reach ? chip(reach[0], reach[1]) : null,
      job.band ? chip(`${job.overlap_hours}h overlap`, job.band) : null,
      job.country_name ? chip(job.country_name) : null),
    h('div', { class: 'foot' },
      h('div', { class: 'pay' }, h('b', {}, big), h('span', {}, small)),
      h('div', { class: 'score' }, job.score == null ? '–' : Math.round(job.score))));
}

/* ------------------------------------------------------------ selection */
function toggleSelect(id, on, rowEl) {
  if (on) cache.selected.add(id); else cache.selected.delete(id);
  rowEl?.classList.toggle('selected', on);
  const bar = $('#bulkbar');
  bar.hidden = !cache.selected.size;
  $('#bulk-n').textContent = `${cache.selected.size} selected`;
}
function clearSelection() {
  cache.selected.clear();
  $$('#jobs-rows .row').forEach(r => { r.classList.remove('selected'); $('.sel', r).checked = false; });
  $('#bulkbar').hidden = true;
}
async function settle(ids, stateName) {
  if (!ids.length) return;
  try {
    const res = await post('/api/review', { items: ids.map(job_id => ({ job_id, state: stateName })) });
    say(`${res.settled.length} settled as ${LABEL[stateName] || stateName}`);
    banner('ok', `${plural(res.settled.length, 'job')} settled as ${LABEL[stateName] || stateName}.`);
    clearSelection();
    cache.statsAt = 0;
    render();
  } catch (e) { failed(e, 'Settle'); }
}

/* ----------------------------------------------------------------- stats */
async function loadStats(force) {
  if (!force && cache.stats && Date.now() - cache.statsAt < 30000) return cache.stats;
  const s = await api('/api/stats', 'stats');
  cache.stats = s; cache.statsAt = Date.now();
  const nums = $$('#summary .num');
  nums[0].textContent = s.reachable.toLocaleString(); nums[0].className = 'num good';
  nums[1].textContent = s.at_level.toLocaleString(); nums[1].className = 'num';
  nums[2].textContent = s.first_run ? '–' : s.fresh.toLocaleString();
  nums[2].className = 'num' + (s.fresh && !s.first_run ? ' good' : '');
  $('#stat-chase').hidden = !s.followups;
  if (s.followups) nums[3].textContent = s.followups;
  return s;
}

/* ----------------------------------------------------------------- views */
async function renderToday() {
  const top = $('#today-top'), rows = $('#today-rows');
  rows.replaceChildren(...skeleton(6));
  try {
    const [s, js] = await Promise.all([loadStats(), api('/api/jobs?limit=60', 'today')]);
    $('#today-note').textContent = s.first_run ? 'first run — the "new today" number starts tomorrow'
      : (s.fresh ? `${plural(s.fresh, 'new job')} since yesterday` : 'nothing new since yesterday');
    const first = (s.top || []).slice(0, 5);
    top.replaceChildren(...first.map((j, i) => card(j, i + 1)));
    const rest = js.filter(j => !first.some(f => f.id === j.id));
    $('#today-count').textContent = js.total ? `${js.total.toLocaleString()} open to you` : '';
    rows.replaceChildren(...(rest.length ? rest.map(j => row(j))
      : [emptyState('Nothing in the queue', 'Run the daily pass, then come back:',
          h('code', {}, 'python3 run.py'))]));
    if (!first.length) top.replaceChildren();
  } catch (e) { rows.replaceChildren(emptyState('Could not load', e.message)); failed(e, 'Today'); }
}

function jobsQuery(offset) {
  const p = new URLSearchParams({ limit: PAGE, offset, sort: state.sort });
  for (const k of ['q', 'state', 'reach', 'band', 'country']) if (state[k]) p.set(k, state[k]);
  if (state.ghost) p.set('show_ghost', '1');
  return '/api/jobs?' + p;
}
const mapMode = () => state.view === 'map' || state.mode === 'map';
function syncToolbar() {
  $('#q').value = state.q; $('#f-state').value = state.state; $('#f-reach').value = state.reach;
  $('#f-band').value = state.band; $('#f-sort').value = state.sort;
  $('#f-ghost').checked = !!state.ghost;
  $$('.seg [data-mode]').forEach(b => { const on = (b.dataset.mode === 'map') === mapMode(); b.classList.toggle('on', on); b.setAttribute('aria-pressed', on); });
  $$('.maptabs button').forEach(b => { const on = b.dataset.layer === state.layer; b.classList.toggle('on', on); b.setAttribute('aria-pressed', on); });
  const active = ['q', 'state', 'reach', 'band', 'country'].filter(k => state[k]);
  $('#clear').hidden = !active.length;
  $('#chips').replaceChildren(...active.map(k => {
    const label = k === 'country' ? (cache.world?.countries?.[state.country]?.name || state.country)
      : k === 'state' ? (LABEL[state.state] || $(`#f-state option[value="${state.state}"]`)?.textContent || state.state)
      : k === 'q' ? `“${state.q}”` : $(`#f-${k} option[value="${state[k]}"]`)?.textContent || state[k];
    return h('button', { class: 'chip filter', onclick: () => set({ [k]: '' }) }, label);
  }));
}
async function renderJobs(append) {
  syncToolbar();
  const rows = $('#jobs-rows');
  const review = state.state === 'review';
  rows.classList.toggle('selectable', review);
  if (!review && cache.selected.size) clearSelection();
  $('#mapwrap').hidden = !mapMode();
  if (mapMode()) await renderMap();
  if (!append) { cache.offset = 0; cache.jobs = []; rows.replaceChildren(...skeleton(8)); }
  try {
    const js = await api(jobsQuery(cache.offset), 'jobs');
    cache.total = js.total || 0;
    cache.jobs = append ? cache.jobs.concat(js) : js;
    const els = js.map(j => row(j));
    if (append) { $$('.skeleton', rows).forEach(s => s.remove()); rows.append(...els); }
    else rows.replaceChildren(...(els.length ? els : [emptyState(
      review ? 'Nothing waiting' : 'Nothing matches', review ? 'The rules placed everything.' : 'Loosen a filter, or search for something else.')]));
    cache.offset += js.length;
    $('#jobs-count').textContent = cache.total ? `${Math.min(cache.offset, cache.total)} of ${cache.total.toLocaleString()}` : '';
    $('#more').hidden = cache.offset >= cache.total;
    if (review) say(`${cache.total} jobs need a read. Press x to select, 1 2 3 or 0 to settle.`);
  } catch (e) { rows.replaceChildren(emptyState('Could not load', e.message)); failed(e, 'Jobs'); }
}

/* ------------------------------------------------------------------- map */
const W = 1000, H = 500;
const project = (lon, lat) => [(lon + 180) / 360 * W, (90 - lat) / 180 * H];
function pathOf(rings) {
  let d = '';
  for (const ring of rings) {
    if (ring.length < 3) continue;
    ring.forEach((p, i) => { const [x, y] = project(p[0], p[1]); d += (i ? 'L' : 'M') + x.toFixed(1) + ' ' + y.toFixed(1); });
    d += 'Z';
  }
  return d;
}
const ramp = (v, max) => (!max || !v) ? 0 : Math.min(4, Math.ceil(v / max * 4));
const CAPTIONS = {
  timezone: 'Every country shaded by how much of its working day overlaps yours. The whole US east coast is a zero-hour overlap with a Colombo nine-to-six.',
  pins: 'One dot per job you can actually take, at its company’s country, spread so a hundred roles are not one dot.',
  hires: 'Countries shaded by how many of their remote jobs are open to you — not by how many they have. Hover for how many are closed.',
  salary: 'Median monthly pay of the priced jobs in each country. Median, not mean: one radiologist would otherwise repaint a continent.',
};
async function renderMap() {
  if (!cache.world || !cache.map) {
    try { [cache.map, cache.world] = await Promise.all([api('/api/map', 'map'), api('/api/world', 'world')]); }
    catch (e) { failed(e, 'Map'); return; }
  }
  const svg = $('#world'), m = cache.map, world = cache.world, layer = state.layer;
  svg.replaceChildren();
  const me = m.me.country;
  const maxHires = Math.max(1, ...Object.values(m.hires));
  const maxPay = Math.max(1, ...Object.values(m.salary).map(s => s.median));
  for (const [iso, c] of Object.entries(world.countries)) {
    if (!c.rings || !c.rings.length) continue;
    const p = svgEl('path', { d: pathOf(c.rings) });
    let title = c.name, cls = '', n = 0;
    if (iso === me) { cls = 'me'; title = `${c.name} — you are here, UTC${c.utc_offset >= 0 ? '+' : ''}${c.utc_offset}`; }
    else if (layer === 'timezone') {
      const t = m.timezone[iso];
      if (t) { cls = t.band; title = `${c.name} — ${t.hours}h overlap with your working day` + (t.multi ? ' (several zones; principal one shown)' : ''); }
    } else if (layer === 'hires' || layer === 'pins') {
      n = m.hires[iso] || 0;
      const step = ramp(n, maxHires);
      if (step) cls = `hot s${step}`;
      const blocked = m.blocked[iso] || 0;
      title = `${c.name} — ${n} open to you` + (blocked ? `, ${blocked} closed to you` : '');
    } else if (layer === 'salary') {
      const s = m.salary[iso];
      if (s) { cls = `hot s${ramp(s.median, maxPay) || 1}`; title = `${c.name} — median ${fmt(s.median)}/mo across ${plural(s.n, 'priced job')}`; n = s.n; }
    }
    if (iso === state.country) cls += ' picked';
    if (cls.trim()) p.setAttribute('class', cls.trim());
    const t = svgEl('title'); t.textContent = title; p.appendChild(t);
    if (n || layer === 'timezone') {
      p.classList.add('clickable');
      p.setAttribute('tabindex', '0');
      p.setAttribute('role', 'button');
      p.setAttribute('aria-label', title);
      const pick = () => set({ country: iso === state.country ? '' : iso, view: 'map' });
      p.addEventListener('click', pick);
      p.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); pick(); } });
    }
    svg.appendChild(p);
  }
  if (layer === 'pins') {
    const seen = {};
    for (const pin of m.pins) {
      const key = pin.country || 'x';
      const i = (seen[key] = (seen[key] || 0) + 1);
      const ring = Math.floor(Math.sqrt(i)), angle = i * 2.399;
      const [x, y] = project(pin.lon, pin.lat);
      const c = svgEl('circle', { cx: (x + Math.cos(angle) * ring * 2.4).toFixed(1), cy: (y + Math.sin(angle) * ring * 2.4).toFixed(1),
        r: pin.score >= 70 ? 3.4 : 2.2, class: pin.band || '', tabindex: 0, role: 'button', 'aria-label': `${pin.company} — ${pin.title}` });
      const t = svgEl('title'); t.textContent = `${pin.company} — ${pin.title}`; c.appendChild(t);
      const open = e => { e.stopPropagation(); openPanel(pin.id); };
      c.addEventListener('click', open);
      c.addEventListener('keydown', e => { if (e.key === 'Enter') open(e); });
      svg.appendChild(c);
    }
  }
  $('#mapcaption').textContent = CAPTIONS[layer];
  const L = $('#legend'), sw = cls => h('i', { class: cls });
  const [a, b] = m.me.workday;
  L.replaceChildren(...(layer === 'timezone' ? [
      h('span', {}, sw('green'), '4h+ overlap — a normal day'), h('span', {}, sw('amber'), '2–4h — early start or late finish'),
      h('span', {}, sw('red'), 'under 2h — a night shift'), h('span', {}, sw('me'), `you, ${a}:00–${b}:00 at UTC+${m.me.offset}`)]
    : layer === 'salary' ? [h('span', {}, 'pale → deep = lower → higher median pay'), h('span', {}, `floor ${fmt(m.floor)}/mo`)]
    : layer === 'hires' ? [h('span', {}, 'pale → deep = more jobs open to you')]
    : [h('span', {}, sw('dot green'), 'good hours'), h('span', {}, sw('dot amber'), 'awkward hours'), h('span', {}, sw('dot red'), 'night shift'), h('span', {}, 'bigger dot = score 70+')]));
}

/* --------------------------------------------------------------- applied */
async function renderApplied() {
  const body = $('#applied-body');
  body.replaceChildren(...skeleton(4));
  try {
    const d = await api('/api/applied', 'applied');
    if (!d.items.length) {
      body.replaceChildren(emptyState('Nothing sent yet', 'Open a job and press "I applied" once you have. It drops out of Today and the follow-up clock starts.'));
      return;
    }
    const order = ['stale', 'due', 'waiting', 'callback', 'rejected', 'silent'];
    const heading = { stale: 'Update these — 14+ days on "sent"', due: 'Follow up — 7+ days, no reply', waiting: 'Waiting',
                      callback: 'Callbacks', rejected: 'Rejected', silent: 'Silent' };
    body.replaceChildren(...order.flatMap(st => {
      const items = d.items.filter(i => i.state === st);
      if (!items.length) return [];
      return [h('section', { class: 'applied-group' }, h('h3', {}, `${heading[st]} · ${items.length}`),
        h('div', { class: 'rows' }, ...items.map(appliedRow)))];
    }));
  } catch (e) { body.replaceChildren(emptyState('Could not load', e.message)); failed(e, 'Applied'); }
}
function appliedRow(item) {
  const [text, cls] = OUTCOME[item.state] || OUTCOME.waiting;
  const act = r => h('button', { onclick: async () => {
    try { await post('/api/outcome', { job_id: item.job_id, result: r }); cache.statsAt = 0; renderApplied(); }
    catch (e) { failed(e, 'Outcome'); } } }, r);
  return h('div', { class: 'arow' },
    h('div', { class: 'days' }, item.days, h('small', {}, 'days')),
    h('div', { class: 'who' }, h('div', { class: 'title', text: item.title || '' }), h('div', { class: 'co', text: item.company || '' })),
    h('div', { class: 'tags' }, chip(text, cls), chip(`via ${item.lane}`)),
    h('div', { class: 'acts' }, act('callback'), act('rejected'), act('silent'),
      h('button', { class: 'ghost', onclick: () => openPanel(item.job_id) }, 'open')));
}

/* -------------------------------------------------------------- questions */
async function renderQuestions() {
  const body = $('#questions-body');
  body.replaceChildren(...skeleton(4));
  try {
    const d = await api('/api/questions', 'questions');
    if (!d.items.length) {
      body.replaceChildren(emptyState('Nothing on file yet', 'Fetch some forms first:', h('code', {}, 'python3 forms.py --fetch 40')));
      return;
    }
    const unanswered = d.items.filter(q => !q.answered);
    const answered = d.items.filter(q => q.answered);
    const row = q => h('div', { class: 'arow' },
      h('div', { class: 'days' }, q.count, h('small', {}, q.count === 1 ? 'time' : 'times')),
      h('div', { class: 'who' }, h('div', { class: 'title', text: q.label || '' }),
        q.answer ? h('div', { class: 'co', text: q.answer.replace(/^\[NEEDS YOU\]\s*/, '') }) : null),
      h('div', { class: 'tags' },
        q.guarded ? chip('you answer this', 'warn') : (q.answered ? chip('answered', 'good') : chip('unanswered', 'bad')),
        q.required ? chip(`required on ${q.required}`) : null));
    body.replaceChildren(
      h('div', { class: 'listhead' }, h('h2', {}, 'Unanswered, most-repeated first'),
        h('span', { class: 'count' }, `${unanswered.length}`)),
      h('div', { class: 'rows' }, ...(unanswered.length ? unanswered.map(row)
        : [emptyState('Nothing unanswered', 'Every question on file has something written for it.')])),
      h('div', { class: 'listhead' }, h('h2', {}, 'Already answered'), h('span', { class: 'count' }, `${answered.length}`)),
      h('div', { class: 'rows' }, ...answered.map(row)));
  } catch (e) { body.replaceChildren(emptyState('Could not load', e.message)); failed(e, 'Questions'); }
}

/* ----------------------------------------------------------------- stats */
const svgBar = (pct, cls = '') => {
  const s = svgEl('svg', { class: 'bartrack', 'aria-hidden': 'true' });
  s.appendChild(svgEl('rect', { class: cls, x: 0, y: 0, height: '100%', rx: 5, width: Math.max(1.2, pct).toFixed(2) + '%' }));
  return s;
};
function bars(entries) {
  const max = Math.max(...entries.map(e => e[1]), 1);
  return h('div', { class: 'bars' }, ...entries.map(([label, n]) =>
    h('div', { class: 'bar' }, h('span', { title: label, text: label }), svgBar(n / max * 100), h('b', {}, n.toLocaleString()))));
}
const tile = (value, label, note, cls = '') => h('div', { class: 'tile ' + cls },
  h('b', {}, Number(value).toLocaleString()), h('span', {}, label), h('small', {}, note || ''));
function funnel(s) {
  const total = s.jobs || 1, dropped = s.dropped || {};
  const pick = re => Object.entries(dropped).filter(([k]) => re.test(k)).reduce((a, [, n]) => a + n, 0);
  const gates = [
    ['Every open job collected', 0], ['Open to someone in Sri Lanka', pick(/closed to anyone/i)],
    ['A level you could be shortlisted for', pick(/shortlisted/i)], ['Actually your profession', pick(/different profession/i)],
    ['Genuinely remote (or in Sri Lanka, or sponsored)', pick(/remote|hybrid|on-site/i)]];
  let left = total;
  return h('div', { class: 'funnel' }, ...gates.map(([name, lost], i) => {
    left -= lost;
    return h('div', { class: 'step' },
      h('div', { class: 'stephead' }, h('span', {}, name), h('b', {}, left.toLocaleString())),
      svgBar(left / total * 100, i === gates.length - 1 ? 'good' : ''),
      lost ? h('div', { class: 'steploss' }, `− ${lost.toLocaleString()} dropped here`) : null);
  }));
}
async function renderStats() {
  const body = $('#stats-body');
  try {
    const s = await loadStats(true);
    body.replaceChildren(
      h('div', { class: 'tiles' },
        tile(s.reachable, 'worth applying to', 'right now', 'good'),
        tile(s.at_level, 'at your level', 'junior, graduate, associate', 'good'),
        s.first_run ? tile(s.jobs, 'jobs scanned', 'first run — check back tomorrow')
          : tile(s.fresh, 'new since yesterday', s.fresh ? 'start here' : 'nothing new today', s.fresh ? 'good' : ''),
        tile(s.review, 'need a read', 'the rules could not place them', s.review ? 'warn' : ''),
        tile(s.applied, 'applied', s.followups ? `${s.followups} to chase` : 'none to chase', s.followups ? 'warn' : ''),
        tile(s.priced, 'priced', `floor ${fmt(s.floor)}/mo`)),
      h('h4', {}, 'Tonight, in order'),
      h('div', { class: 'rows' }, ...(s.top || []).map(j => row(j))),
      h('div', { class: 'two' },
        h('div', {}, h('h4', {}, 'Each gate, and what it cost'), funnel(s)),
        h('div', {}, h('h4', {}, 'Where they came from'),
          bars(Object.entries(s.by_source || {}).sort((a, b) => b[1] - a[1])),
          h('h4', {}, 'How they sit against your day'),
          bars(['green', 'amber', 'red'].map(b => [{ green: '4h+ overlap', amber: '2–4h', red: 'under 2h' }[b], s.by_band?.[b] || 0])),
          h('h4', {}, 'Would they take you'),
          bars(Object.entries(s.by_reach || {}).map(([k, v]) => [REACH[k]?.[0] || k, v])))));
  } catch (e) { body.replaceChildren(emptyState('Could not load', e.message)); failed(e, 'Analytics'); }
}

/* ----------------------------------------------------------------- panel */
let lastFocus = null;
async function openPanel(id) {
  lastFocus = document.activeElement;
  const dlg = $('#panel'), body = $('#panel-body');
  $('#panel-title').textContent = 'Loading…'; $('#panel-co').textContent = '';
  body.replaceChildren(...skeleton(3));
  if (!dlg.open) dlg.showModal();
  set({ job: String(id) }, { silent: true });
  let j;
  try { j = await api('/api/job/' + id, 'job'); }
  catch (e) { body.replaceChildren(emptyState('Could not load this job', e.message)); failed(e, 'Job'); return; }
  $('#panel-title').textContent = j.title || ''; $('#panel-co').textContent = j.company || '';
  const [big, small] = payText(j);
  const reach = REACH[j.reach] || ['?', 'warn'], mode = MODE[j.work_mode] || ['?', ''];
  const blockBox = h('div', {});
  const kitBox = h('div', { class: 'kit' }, h('p', { class: 'hint' }, 'building the kit…'));
  const settleBtn = (label, st, cls = '') => h('button', { class: cls, onclick: () => settleOne(j.id, st) }, label);
  const applyBtn = (label, lane, cls = '') => h('button', { class: cls, onclick: async e => {
    try { const res = await post('/api/applied', { job_id: j.id, lane });
      e.target.textContent = res.already ? 'already recorded' : 'recorded'; cache.statsAt = 0;
      setTimeout(() => { closePanel(); render(); }, 600); } catch (err) { failed(err, 'Mark applied'); } } }, label);
  const links = (j.sources || []).map(src => {
    const href = safeUrl(src.apply_url || src.url), mail = safeUrl(src.apply_email ? `mailto:${src.apply_email}` : null);
    if (!href && !mail) return null;
    return h('p', {}, href ? h('a', { href, target: '_blank', rel: 'noreferrer noopener' }, src.source) : src.source,
      mail ? [' — ', h('a', { href: mail }, src.apply_email)] : null);
  }).filter(Boolean);
  let breakdown = null;
  try { breakdown = j.breakdown ? JSON.parse(j.breakdown) : null; } catch { breakdown = null; }
  body.replaceChildren(...[
    blockBox,
    h('div', { class: 'tags' }, chip(LABEL[j.state] || j.state, 'state'), j.decided_by === 'human' ? chip('settled by you') : null,
      chip(reach[0], reach[1]), chip(mode[0], mode[1]), j.band ? chip(`${j.overlap_hours}h overlap`, j.band) : null,
      j.country_name ? chip(j.country_name) : null, age(j) ? chip(age(j)) : null,
      VERDICT[j.verdict] ? chip(VERDICT[j.verdict][0], VERDICT[j.verdict][1]) : null),
    VERDICT[j.verdict] ? h('p', { class: 'hint' },
      `${j.verdict === 'ghost' ? 'open' : 'aged'} ${Math.round(j.verdict_days)} days` +
      (j.verdict === 'restamped' ? ' — the source re-dated it without it ever closing' : '')) : null,
    h('h4', {}, 'Can you take it'),
    h('p', { class: 'hint' }, `rule: ${j.rule || '—'}`),
    j.evidence_quote ? h('div', { class: 'evidence' }, j.evidence_quote) : null,
    h('h4', {}, 'Would they take you'),
    j.reach_why ? h('div', { class: 'evidence' }, j.reach_why) : null,
    j.mode_quote ? h('div', { class: 'evidence' }, j.mode_quote) : null,
    h('h4', {}, 'Hours and pay'),
    h('table', {},
      h('tr', {}, h('td', {}, 'Pay'), h('td', {}, big === '—' ? 'not published' : `${big} /mo (${small})`)),
      j.pay_known ? h('tr', {}, h('td', { class: 'why' }, `read from ${j.pay_from}, ${j.cur} per ${j.per}`), h('td', {})) : null,
      h('tr', {}, h('td', {}, 'Where'), h('td', {}, j.country_name || 'not resolved')),
      h('tr', {}, h('td', {}, 'Their clock'), h('td', {}, j.utc_offset == null ? '—' : 'UTC' + (j.utc_offset >= 0 ? '+' : '') + j.utc_offset)),
      h('tr', {}, h('td', {}, 'Overlap with your day'), h('td', {}, j.overlap_hours == null ? '—' : `${j.overlap_hours} hours`))),
    breakdown ? [h('h4', {}, 'Why it ranks here'),
      h('table', {}, ...Object.entries(breakdown).filter(([k]) => k !== '_raw').flatMap(([k, v]) => [
        h('tr', {}, h('td', {}, k), h('td', {}, (v.points > 0 ? '+' : '') + Number(v.points))),
        h('tr', {}, h('td', { class: 'why', colspan: 2 }, v.why || ''))]))] : null,
    j.judge ? [h('h4', {}, "The judge's read"),
      h('p', { class: 'hint' }, `overall ${j.judge.overall || '—'}${j.judge.variant ? ` · CV: ${j.judge.variant}` : ''}`),
      h('table', {}, ...(j.judge.criteria || []).flatMap(c => [
        h('tr', {}, h('td', {}, c.name), h('td', {}, c.verified ? `${c.rating}/4` : 'unverified')),
        c.cv_quote ? h('tr', {}, h('td', { class: 'why', colspan: 2 }, `CV: “${c.cv_quote}”`)) : null,
        c.advert_quote ? h('tr', {}, h('td', { class: 'why', colspan: 2 }, `advert: “${c.advert_quote}”`)) : null,
        h('tr', {}, h('td', { class: 'why', colspan: 2 }, c.note || ''))])),
      j.judge.summary ? h('p', {}, j.judge.summary) : null] : null,
    h('h4', {}, 'Your CV against this advert'),
    h('div', { class: 'actions' }, h('button', { onclick: () => { closePanel(); set({ view: 'cv' }, { silent: true }); $('#cv-job').value = j.id; render(); scoreCv(); } }, 'Score my CV against this')),
    h('h4', {}, 'Apply'),
    ...(links.length ? links : [h('p', { class: 'hint' }, 'no link recorded')]),
    h('h4', {}, 'Settle it by hand'),
    h('div', { class: 'actions' }, settleBtn('Open to anyone', 'OPEN_WORLDWIDE'), settleBtn('Open to Sri Lanka', 'OPEN_REGION'),
      settleBtn('Contractor / EOR', 'OPEN_CONTRACTOR'), settleBtn('Sponsored', 'ONSITE_SPONSORED'), settleBtn('Closed to me', 'BLOCKED', 'no')),
    h('h4', {}, 'When you have applied'),
    h('div', { class: 'actions' }, applyBtn('I applied — portal', 'portal', 'primary'), applyBtn('by email', 'email'), applyBtn('some other way', 'manual')),
    h('h4', {}, 'Copy-paste kit'),
    kitBox,
    j.description ? [h('h4', {}, 'The advert'), h('div', { class: 'desc' }, j.description)] : null,
  ].flat(2).filter(Boolean));
  $('#panel-close').focus();
  api('/api/kit/' + j.id, 'kit').then(pack => {
    if (pack.blockers?.length || pack.flags?.length) {
      blockBox.replaceChildren(
        pack.blockers?.length ? h('div', { class: 'blockers' }, h('b', {}, 'STOP — the form itself rules you out'), ...pack.blockers.map(b => h('p', {}, b))) : null,
        pack.flags?.length ? h('div', { class: 'blockers flags' }, h('b', {}, 'Answer these yourself'), ...pack.flags.map(f => h('p', {}, f))) : null);
    }
    const all = [...pack.sections.map(x => [x.heading, x.body]), ...pack.stories.map(x => [x.title, x.body]),
                 ...pack.defences.map(x => [`If they ask: ${x.heading}`, x.body])];
    const copyAll = h('button', { class: 'copy' }, 'copy all');
    copyAll.addEventListener('click', () => copyText(pack.markdown, copyAll));
    kitBox.replaceChildren(h('div', { class: 'kithead' }, h('h5', {}, 'Everything, as one document'), copyAll),
      ...all.map(([heading, text]) => {
        const btn = h('button', { class: 'copy' }, 'copy');
        btn.addEventListener('click', () => copyText(text, btn));
        return h('div', { class: 'kitblock' }, h('div', { class: 'kithead' }, h('h5', {}, heading), btn), h('pre', { class: 'kittext' }, text));
      }));
  }).catch(e => { if (e.name !== 'AbortError') kitBox.replaceChildren(h('p', { class: 'hint' }, 'could not build the kit')); });
}
async function settleOne(id, st) {
  try { await post('/api/review', { job_id: id, state: st }); cache.statsAt = 0; closePanel(); render(); }
  catch (e) { failed(e, 'Settle'); }
}
function closePanel() {
  const dlg = $('#panel');
  if (dlg.open) dlg.close();
  set({ job: '' }, { silent: true });
  if (lastFocus && document.contains(lastFocus)) lastFocus.focus();
}

/* -------------------------------------------------------------------- CV */
async function renderCv() {
  const sel = $('#cv-variant');
  if (!cache.cvList) {
    try { cache.cvList = await api('/api/cv', 'cvlist'); } catch (e) { failed(e, 'CV'); return; }
    sel.replaceChildren(...cache.cvList.variants.map(v => h('option', { value: v.slug }, v.slug + (v.canonical ? ' (canonical)' : ''))));
    if (!state.variant) state.variant = cache.cvList.canonical;
  }
  sel.value = state.variant || cache.cvList.canonical;
  if (!cache.cv || cache.cv.slug !== sel.value) {
    try { cache.cv = await api('/api/cv/' + encodeURIComponent(sel.value), 'cv'); } catch (e) { failed(e, 'CV'); return; }
    $('#cv-md').value = cache.cv.markdown;
    $('#cv-ats').textContent = cache.cv.plain;
    cache.report = null;
    $('#cv-platforms').replaceChildren(emptyState('Not scored yet', 'Press Score for the six platforms, with a job id to score against an advert.'));
    $('#cv-edits').replaceChildren(); $('#cv-lint').replaceChildren();
    renderPdfPane();
    counter();
    cvStatus('');
  }
  $$('.cvtabs button').forEach(b => { const on = b.dataset.cvtab === state.cvtab; b.classList.toggle('on', on); b.setAttribute('aria-selected', on); });
  $$('.cvpane').forEach(p => p.classList.toggle('on', p.id === 'cv-' + state.cvtab));
}
function cvStatus(text, cls = '') { const s = $('#cv-status'); s.textContent = text; s.className = 'hint ' + cls; }
function counter() {
  const md = $('#cv-md').value, budget = cache.cvList?.budget || {};
  const bullets = md.split('\n').filter(l => /^\s*[-*•]\s+/.test(l)).map(l => l.replace(/^\s*[-*•]\s+/, '').replace(/\*\*/g, ''));
  const long = bullets.filter(b => b.length > (budget.two_line_max || 218)).length;
  const zone = budget.awkward_zone || [112, 188];
  const awkward = bullets.filter(b => b.length >= zone[0] && b.length <= zone[1]).length;
  $('#cv-count').textContent = `${md.split('\n').length} lines · ${md.length.toLocaleString()} chars · ${bullets.length} bullets`;
  $('#cv-budget').textContent = (long ? `${long} over ${budget.two_line_max || 218} chars` : 'no bullet over the hard max')
    + (awkward ? ` · ${awkward} in the short-second-line zone` : '');
}
function renderPdfPane() {
  const pane = $('#cv-pdf'), cv = cache.cv;
  pane.replaceChildren(cv?.pdf_url
    ? h('div', { class: 'checks' }, h('p', {}, 'Rendered PDF: ', h('a', { href: cv.pdf_url, target: '_blank', rel: 'noopener' }, cv.pdf.split('/').pop())),
        h('p', { class: 'hint' }, 'Opens in a new tab; drag it from there onto an upload field. Press Render PDF after saving to refresh it.'))
    : emptyState('No PDF yet', 'Press Render PDF to build one from this markdown.'));
}
async function scoreCv() {
  const slug = $('#cv-variant').value, job = $('#cv-job').value.trim();
  cvStatus('scoring…');
  $('#cv-platforms').replaceChildren(...skeleton(6));
  try {
    const rep = await api(job ? `/api/ats/${encodeURIComponent(job)}?variant=${encodeURIComponent(slug)}` : `/api/ats/self?variant=${encodeURIComponent(slug)}`, 'ats');
    cache.report = rep;
    renderPlatforms(rep); renderEdits(rep); renderLint(rep);
    cvStatus(rep.job ? `scored against #${rep.job.id} ${rep.job.title} (${rep.job.profile || 'no vendor'})`
      : `scored alone${rep.pdf.analysed ? ', from the rendered PDF' : ', from the markdown'}`);
  } catch (e) { $('#cv-platforms').replaceChildren(emptyState('Could not score', e.message)); failed(e, 'Score'); cvStatus(''); }
}
function renderPlatforms(rep) {
  const pane = $('#cv-platforms');
  const src = rep.pdf.analysed ? `from the PDF text layer (${rep.pdf.pages} pages)` : 'from the markdown projection (render a PDF to test the real file)';
  pane.replaceChildren(
    h('p', { class: 'hint' }, `${src} · ${rep.signals.words} words · ${rep.signals.bullets} bullets, ${rep.signals.quantified} quantified, ${rep.signals.action_verbs} verb-led`
      + (rep.keywords ? ` · keywords exact ${rep.keywords.score_by_strategy.exact} / fuzzy ${rep.keywords.score_by_strategy.fuzzy} / semantic ${rep.keywords.score_by_strategy.semantic}` : '')),
    ...rep.platforms.map(p => h('div', { class: 'plat' },
      h('div', { class: 'name' }, p.name, h('small', {}, p.vendor)),
      svgBar(p.score, p.passes ? 'good' : 'warn'),
      h('div', { class: 'num' + (p.passes ? ' ok' : ' bad') }, p.score),
      h('div', { class: 'pass' }, chip(p.passes ? `passes ${p.passing}` : `below ${p.passing}`, p.passes ? 'good' : 'warn'), p.this_job ? chip('this job', 'filter') : null),
      p.note ? h('div', { class: 'note' }, p.note) : null,
      h('details', {}, h('summary', {}, 'breakdown'),
        h('p', {}, `formatting ${p.breakdown.formatting.score} · sections ${p.breakdown.sections.score} · experience ${p.breakdown.experience.score} · education ${p.breakdown.education.score} · quantified ${p.breakdown.quantification}%`
          + (p.breakdown.keywords ? ` · keywords ${p.breakdown.keywords.score} (${p.breakdown.keywords.strategy})` : '')),
        ...p.quirks.map(q => h('p', {}, `${q.delta > 0 ? '+' : ''}${q.delta} ${q.message}`)),
        ...p.suggestions.map(s => h('p', {}, '→ ' + s))))),
    rep.keywords?.missing?.length ? h('p', { class: 'hint' }, 'missing from the advert: ' + rep.keywords.missing.slice(0, 14).map(m => m.term + (m.required ? '*' : '')).join(', ')) : null,
    h('p', { class: 'hint' }, rep.notes[0]));
}
function renderEdits(rep) {
  const pane = $('#cv-edits');
  if (!rep.edits.length) { pane.replaceChildren(emptyState('Nothing to change', 'No evidence-backed edit would move a score.')); return; }
  const safeKinds = new Set(['add_skill', 'exact_form', 'header_rename']);
  const applyAll = h('button', { onclick: () => applyEdits(rep.edits.filter(e => e.patch && safeKinds.has(e.kind))) }, 'Apply all safe');
  pane.replaceChildren(
    h('div', { class: 'actions' }, applyAll, h('span', { class: 'hint' }, 'safe = add a skill you already claim elsewhere, exact spelling, header rename. Nothing is saved until you press Save.')),
    ...rep.edits.map(e => {
      const best = Object.entries(e.delta || {}).sort((a, b) => b[1] - a[1])[0];
      const btn = e.patch ? h('button', { onclick: () => applyEdits([e]) }, 'Apply') : chip('NEEDS YOU', 'warn');
      return h('div', { class: 'edit' + (e.needs_you ? ' needs' : ''), dataset: { id: e.id } },
        h('div', {}, chip(e.needs_you ? 'you' : e.impact, e.needs_you ? 'warn' : e.impact === 'high' ? 'good' : ''),
          best && best[1] ? h('div', { class: 'hint' }, `+${best[1]} ${best[0]}`) : null),
        h('div', {}, h('div', {}, e.summary), h('div', { class: 'why' }, e.why),
          e.evidence.length ? h('div', { class: 'ev' }, ...e.evidence.slice(0, 2).map(p => h('span', {}, `${p.file}:${p.line}  ${p.text}`))) : null),
        btn);
    }));
}
async function applyEdits(edits) {
  if (!edits.length) return;
  const slug = $('#cv-variant').value;
  try {
    const res = await post(`/api/cv/${encodeURIComponent(slug)}/apply`, { markdown: $('#cv-md').value, patches: edits.map(e => e.patch) });
    $('#cv-md').value = res.markdown;
    counter();
    edits.forEach(e => $(`.edit[data-id="${e.id}"]`)?.classList.add('applied'));
    if (res.rejected.length) banner('warn', `${res.rejected.length} edit(s) no longer apply: ${res.rejected.map(r => r.reason).join('; ')}`);
    cvStatus(`${res.applied.length} applied — unsaved, press Save`, 'warn');
  } catch (e) { failed(e, 'Apply'); }
}
function renderLint(rep) {
  const pane = $('#cv-lint');
  if (!rep.lint.length) { pane.replaceChildren(emptyState('Clean', 'No lint findings.')); return; }
  pane.replaceChildren(h('div', { class: 'rows' }, ...rep.lint.map(f => h('div', { class: 'lintrow' },
    h('span', { class: 'sev ' + f.severity }, f.severity), h('span', {}, f.rule + (f.line ? ` L${f.line}` : '')),
    h('span', { class: 'txt' }, h('b', {}, f.fix), ' — ', f.text)))));
}
async function saveCv() {
  const slug = $('#cv-variant').value;
  cvStatus('saving…');
  try {
    const res = await post(`/api/cv/${encodeURIComponent(slug)}`, { markdown: $('#cv-md').value });
    cache.cv = null; cache.cvList = null;
    cvStatus(`saved · backup ${res.backup.split('/').pop()}`, 'ok');
    await renderCv();
    if (cache.report) scoreCv();
  } catch (e) { failed(e, 'Save'); cvStatus(''); }
}
async function renderPdf() {
  const slug = $('#cv-variant').value;
  cvStatus('rendering…');
  try {
    const res = await post(`/api/cv/${encodeURIComponent(slug)}/render`, {});
    $('#cv-pdf').replaceChildren(h('div', { class: 'checks' },
      h('p', {}, res.ok ? h('span', { class: 'ok' }, 'rendered · every check passed') : h('span', { class: 'bad' }, 'rendered, with a failing check')),
      h('p', {}, h('a', { href: res.url, target: '_blank', rel: 'noopener' }, res.pdf.split('/').pop()), ` · ${res.pages} page(s)`),
      ...res.checks.map(c => h('p', { class: c.ok ? 'ok' : 'bad' }, (c.ok ? '✓ ' : '✗ ') + c.check)),
      ...(res.warnings || []).map(w => h('p', { class: 'bad' }, '! ' + w))));
    set({ cvtab: 'pdf' });
    cvStatus(res.ok ? 'PDF rendered' : 'PDF rendered — read the checks', res.ok ? 'ok' : 'warn');
    if (cache.cv) { cache.cv.pdf_url = res.url; cache.cv.pdf = res.pdf; }
  } catch (e) { failed(e, 'Render'); cvStatus(''); }
}

/* ---------------------------------------------------------------- render */
function render() {
  // Map is its own tab, but it is the Jobs view with the map open: same
  // filters, same rows underneath, same URL state.
  const section = state.view === 'map' ? 'jobs' : state.view;
  $$('#tabs button').forEach(b => { const on = b.dataset.view === state.view; b.setAttribute('aria-selected', on); b.tabIndex = on ? 0 : -1; });
  $$('.view').forEach(v => v.classList.toggle('on', v.id === 'view-' + section));
  loadStats().catch(e => failed(e, 'Stats'));
  ({ today: renderToday, jobs: () => renderJobs(false), map: () => renderJobs(false), applied: renderApplied,
     questions: renderQuestions, cv: renderCv, stats: renderStats }[state.view] || renderToday)();
  if (state.job && !$('#panel').open) openPanel(Number(state.job));
}

/* -------------------------------------------------------------- keyboard */
let pendingG = false;
function focusedRow() { return document.activeElement?.closest?.('.row, .card') || null; }
function moveFocus(delta) {
  const rows = $$('.view.on .row:not(.skeleton), .view.on .card');
  if (!rows.length) return;
  const cur = focusedRow();
  let i = cur ? rows.indexOf(cur) + delta : (delta > 0 ? 0 : rows.length - 1);
  i = Math.max(0, Math.min(rows.length - 1, i));
  rows[i].focus(); rows[i].scrollIntoView({ block: 'nearest' });
}
document.addEventListener('keydown', e => {
  const tag = (e.target.tagName || '').toLowerCase();
  const typing = ['input', 'textarea', 'select'].includes(tag) || e.target.isContentEditable;
  if (e.key === 'Escape') { if ($('#panel').open) closePanel(); else if ($('#helpdlg').open) $('#helpdlg').close(); return; }
  if (typing || e.metaKey || e.ctrlKey || e.altKey) return;
  if (pendingG) {
    pendingG = false;
    const go = { t: 'today', j: 'jobs', m: 'map', p: 'applied', c: 'cv', s: 'stats' }[e.key];
    if (go) { e.preventDefault(); set({ view: go }); }
    return;
  }
  switch (e.key) {
    case '/': e.preventDefault(); if (!['jobs', 'map'].includes(state.view)) set({ view: 'jobs' }); $('#q').focus(); $('#q').select(); break;
    case 'j': e.preventDefault(); moveFocus(1); break;
    case 'k': e.preventDefault(); moveFocus(-1); break;
    case 'g': pendingG = true; setTimeout(() => { pendingG = false; }, 900); break;
    case '?': e.preventDefault(); $('#helpdlg').showModal(); break;
    case 'x': {
      const r = focusedRow(); if (r && $('#jobs-rows').classList.contains('selectable')) {
        const box = $('.sel', r); box.checked = !box.checked; toggleSelect(Number(r.dataset.id), box.checked, r); }
      break; }
    case 'a': {
      const r = focusedRow(); if (r && confirm('Record that you applied to this job (portal)?')) {
        post('/api/applied', { job_id: Number(r.dataset.id), lane: 'portal' })
          .then(res => { banner('ok', res.already ? 'Already recorded.' : `Recorded — ${res.company}: ${res.title}.`); cache.statsAt = 0; render(); })
          .catch(err => failed(err, 'Mark applied')); }
      break; }
    case '1': case '2': case '3': case '0': {
      if (!['jobs', 'map'].includes(state.view) || state.state !== 'review') break;
      const st = SETTLE[e.key];
      const ids = cache.selected.size ? [...cache.selected] : (focusedRow() ? [Number(focusedRow().dataset.id)] : []);
      if (ids.length) { e.preventDefault(); settle(ids, st); }
      break; }
  }
});

/* ------------------------------------------------------------------ wire */
function wire() {
  $$('#tabs button').forEach(b => b.addEventListener('click', () => set({ view: b.dataset.view })));
  $('#tabs').addEventListener('keydown', e => {
    const tabs = $$('#tabs button'), i = tabs.indexOf(document.activeElement);
    if (i < 0) return;
    if (e.key === 'ArrowRight' || e.key === 'ArrowLeft') {
      e.preventDefault(); const n = tabs[(i + (e.key === 'ArrowRight' ? 1 : tabs.length - 1)) % tabs.length]; n.focus(); set({ view: n.dataset.view });
    }
  });
  let qTimer;
  $('#q').addEventListener('input', () => { clearTimeout(qTimer); qTimer = setTimeout(() => set({ q: $('#q').value.trim() }), 220); });
  for (const k of ['state', 'reach', 'band', 'sort']) $(`#f-${k}`).addEventListener('change', e => set({ [k]: e.target.value }));
  $('#f-ghost').addEventListener('change', e => set({ ghost: e.target.checked ? '1' : '' }));
  $$('.seg [data-mode]').forEach(b => b.addEventListener('click', () => set(b.dataset.mode === 'map' ? { view: 'map', mode: 'map' } : { view: 'jobs', mode: 'list' })));
  $$('.maptabs button').forEach(b => b.addEventListener('click', () => set({ layer: b.dataset.layer })));
  $('#clear').addEventListener('click', () => set({ q: '', state: '', reach: '', band: '', country: '' }));
  $('#more').addEventListener('click', () => renderJobs(true));
  $$('#bulkbar [data-set]').forEach(b => b.addEventListener('click', () => settle([...cache.selected], b.dataset.set)));
  $('#bulk-clear').addEventListener('click', clearSelection);
  $('#panel-close').addEventListener('click', closePanel);
  $('#panel').addEventListener('close', () => { if (state.job) set({ job: '' }, { silent: true }); if (lastFocus && document.contains(lastFocus)) lastFocus.focus(); });
  $('#panel').addEventListener('click', e => { if (e.target === $('#panel')) closePanel(); });
  $('#help').addEventListener('click', () => $('#helpdlg').showModal());
  $('#help-close').addEventListener('click', () => $('#helpdlg').close());
  $('#theme').addEventListener('click', () => {
    const next = document.documentElement.dataset.theme === 'light' ? 'dark' : 'light';
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem('theme', next); } catch { /* private mode */ }
  });
  try { const t = localStorage.getItem('theme'); if (t) document.documentElement.dataset.theme = t; } catch { /* ignore */ }
  $('#cv-variant').addEventListener('change', e => set({ variant: e.target.value }));
  $$('.cvtabs button').forEach(b => b.addEventListener('click', () => set({ cvtab: b.dataset.cvtab })));
  $('#cv-score').addEventListener('click', scoreCv);
  $('#cv-save').addEventListener('click', saveCv);
  $('#cv-render').addEventListener('click', renderPdf);
  let cTimer;
  $('#cv-md').addEventListener('input', () => { clearTimeout(cTimer); cTimer = setTimeout(counter, 150); cvStatus('unsaved', 'warn'); });
  $('#cv-md').addEventListener('keydown', e => { if ((e.metaKey || e.ctrlKey) && e.key === 's') { e.preventDefault(); saveCv(); } });
  window.addEventListener('hashchange', () => { readHash(); render(); });
  window.addEventListener('beforeunload', e => { if ($('#cv-status').textContent.startsWith('unsaved') || $('#cv-status').textContent.includes('unsaved')) { e.preventDefault(); e.returnValue = ''; } });
}

readHash();
wire();
render();
