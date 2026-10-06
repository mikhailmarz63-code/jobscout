/* jobscout helper — the side panel.
 *
 * Flow: find the active tab → ask every frame to scan its form → resolve the
 * page to a job on the board → fetch the prepared answers for exactly the
 * labels on the page → show each with Copy / Fill. Blockers first, in red.
 * The only writes are to the one field named and, on confirm, "I applied".
 * There is no code path that submits the form. */
'use strict';

const $ = s => document.querySelector(s);
const S = { base: 'http://127.0.0.1:8790', token: '', tabId: null, frameId: null, url: '',
            scan: null, job: null, kit: null, pinned: false, gen: 0, answers: null, online: true, bundle: null, note: '' };
const h = (tag, attrs = {}, ...kids) => {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === 'class') el.className = v; else if (k === 'text') el.textContent = v;
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), v); else el.setAttribute(k, v === true ? '' : v);
  }
  for (const c of kids.flat()) if (c != null && c !== false) el.appendChild(typeof c === 'string' ? document.createTextNode(c) : c);
  return el;
};
const put = (el, ...kids) => el.replaceChildren(...kids.flat(3).filter(Boolean));
function status(kind, text) {
  const el = $('#status'); el.className = 'status ' + kind; el.textContent = text; el.hidden = !text;
}

/* ------------------------------------------------------------- the board */
async function pair() {
  // The board answers /api/pair only over loopback and only to the
  // allow-listed extension origin; this is how the token gets here without
  // anyone pasting it.
  try {
    const r = await fetch(S.base + '/api/jobs/ext/pair?ext=' + chrome.runtime.id);
    if (!r.ok) return false;
    const d = await r.json();
    if (!d.token) return false;
    S.token = d.token; if (d.base) S.base = d.base;
    await chrome.storage.local.set({ token: S.token, base: S.base });
    status('ok', 'paired with the board');
    return true;
  } catch { return false; }
}
async function board(path, opts = {}) {
  const r = await fetch(S.base + path, {
    ...opts, headers: { 'X-Jobscout-Token': S.token, 'Content-Type': 'application/json', ...(opts.headers || {}) },
  });
  if (r.status === 401 && !opts._retried && await pair()) return board(path, { ...opts, _retried: true });
  if (!r.ok) throw new Error(`${r.status} ${await r.text().catch(() => '')}`.trim());
  return r.json();
}

/* -------------------------------------------------------------- scanning */
const scans = new Map();                            // frameId -> scan result
chrome.runtime.onMessage.addListener((msg, sender) => {
  if (!sender.tab || sender.tab.id !== S.tabId) return;
  if (msg.type === 'scanned') { scans.set(sender.frameId, { ...msg, frameId: sender.frameId }); scheduleRender(); }
  if (msg.type === 'changed') { clearTimeout(S.rescanTimer); S.rescanTimer = setTimeout(() => refresh(false), 600); }
});
let renderTimer;
function scheduleRender() { clearTimeout(renderTimer); renderTimer = setTimeout(afterScan, 350); }

async function requestScan() {
  scans.clear();
  try { await chrome.tabs.sendMessage(S.tabId, { type: 'scan' }); }
  catch { /* no content script on this page: offer the manual scan */
    put($('#fields'), h('div', { class: 'empty' }, h('b', {}, 'No form found on this page'),
      'This host is not on the list the helper watches. ',
      h('button', { onclick: injectAndScan }, 'Scan this page anyway')));
  }
}
async function injectAndScan() {
  try {
    await chrome.scripting.executeScript({ target: { tabId: S.tabId, allFrames: true }, files: ['content.js'] });
    await chrome.scripting.insertCSS({ target: { tabId: S.tabId, allFrames: true }, files: ['content.css'] });
    await requestScan();
  } catch (e) { status('error', 'Could not scan: ' + e.message); }
}
async function afterScan() {
  const all = [...scans.values()];
  if (!all.length) return;
  const best = all.sort((a, b) => b.fields.length - a.fields.length)[0];
  S.scan = best; S.frameId = best.frameId; S.url = best.url; S.note = best.note;
  if (!S.pinned && (!S.job || S.job.url !== best.url)) await resolve(best.url, all.find(s => s.top)?.url);
  if (!S.pinned) await fetchAnswers();
  render();
}

/* -------------------------------------------------------------- resolving */
async function resolve(frameUrl, tabUrl) {
  const gen = S.gen;
  S.job = null;
  for (const u of [frameUrl, tabUrl].filter(Boolean)) {
    try {
      const r = await board('/api/jobs/ext/resolve?url=' + encodeURIComponent(u));
      if (gen !== S.gen) return;                    // superseded while this was in flight
      S.online = true;
      if (r.job_id) {
        S.job = { ...r, url: frameUrl };
        await loadKit(r.job_id);
        if (gen !== S.gen) return;
        return;
      }
      S.job = { job_id: null, vendor: r.vendor, url: frameUrl };
    } catch (e) {
      if (gen !== S.gen) return;
      S.online = e instanceof TypeError ? false : S.online;   // a real fetch failure, not an HTTP error status
      S.job = { job_id: null, url: frameUrl, error: e.message };
      return;
    }
  }
}
async function loadKit(jobId) {
  const gen = S.gen;
  S.kit = null;
  if (!jobId) return;
  try {
    const kit = await board('/api/jobs/ext/kit?job_id=' + jobId);
    if (gen !== S.gen) return;
    S.kit = kit; S.online = true;
  } catch (e) { if (gen === S.gen && e instanceof TypeError) S.online = false; }
}
/* A manual lookup pins the header and the prepared-kit section to whatever
 * was found, independent of the live page: it exists precisely for looking
 * something up while you are not on that job's own page. It deliberately
 * does not touch S.scan or fetch field answers -- those stay about whichever
 * page is actually open, so a pinned job's blockers/kit can never be shown
 * next to another job's live form fields. */
async function lookup(raw) {
  const v = (raw || '').trim();
  if (!v) return;
  const gen = ++S.gen;                              // supersede any resolve()/loadKit() already in flight
  const btn = $('#lookupBtn');
  btn.disabled = true;
  status('', 'looking up…');
  try {
    let resolved = null;
    if (/^\d+$/.test(v)) {
      let job = null;
      try { job = await board('/api/jobs/ext/job?id=' + v); }
      catch (e) { if (e instanceof TypeError) throw e; /* else: not found -- resolved stays null, handled below */ }
      if (gen !== S.gen) return;
      if (job && job.id) {
        resolved = { job_id: job.id, title: job.title, company: job.company, applied: null, vendor: null };
        if (job.canonical_url) {
          try {
            const r2 = await board('/api/jobs/ext/resolve?url=' + encodeURIComponent(job.canonical_url));
            if (gen !== S.gen) return;
            if (r2.job_id === job.id) resolved = r2;          // carries the real applied/vendor
          } catch { /* keep the basic info from /api/jobs/ext/job -- applied status just won't show */ }
        }
      }
    } else {
      let encoded;
      try { encoded = encodeURIComponent(v); }
      catch { status('error', 'that text has characters a URL can\'t carry -- paste the plain job link'); btn.disabled = false; return; }
      const r = await board('/api/jobs/ext/resolve?url=' + encoded);
      if (gen !== S.gen) return;
      S.online = true;
      if (r.job_id) resolved = r;
      else {
        status('warn', r.vendor
          ? `${r.vendor} board -- not tracked yet; try the numeric job id from the board`
          : 'not recognised -- paste the exact application URL, or the numeric job id from the board');
        btn.disabled = false;
        return;
      }
    }
    if (gen !== S.gen) return;
    if (!resolved) { status('warn', 'no job with that id'); btn.disabled = false; return; }
    S.job = resolved; S.kit = null; S.pinned = true; S.note = 'looked up';
    await loadKit(resolved.job_id);
    if (gen !== S.gen) return;
    status('ok', 'loaded');
    render();
  } catch (e) {
    if (gen !== S.gen) return;
    if (e instanceof TypeError) S.online = false;
    status('error', 'lookup failed: ' + e.message);
  }
  if (gen === S.gen) btn.disabled = false;
}
async function fetchAnswers() {
  S.answers = null;
  if (!S.scan) return;
  const labels = S.scan.fields.map(f => ({ label: f.label, type: f.type, required: f.required, options: f.options || [] }));
  const jobId = S.job?.job_id || null;
  // The server answers even a job it has never seen -- identity, CTC, story
  // and project matching all still apply (kit.answers_for_labels_generic()).
  // This used to require a resolved job_id, which meant any untracked
  // posting fell straight to the local-only fallback below and left every
  // field blank, including his own name (chrome.storage.local's "identity"
  // key is never written anywhere, so that fallback had nothing to answer
  // with even for the fields it could in principle handle). Try the server
  // whenever it might be reachable; only a real failure drops to offline.
  if (S.online !== false) {
    try {
      S.answers = await board('/api/jobs/ext/answers', { method: 'POST', body: JSON.stringify({ job_id: jobId, labels }) });
      S.online = true;
      if (jobId) chrome.storage.local.set({ ['bundle:' + jobId]: { ...S.answers, fetched_at: Date.now() } }).catch(() => {});
      return;
    } catch (e) { if (e instanceof TypeError) S.online = false; }   // any error: fall through to offline below
  }
  // Genuinely offline: a cached bundle for this job, else identity + lines.
  const key = jobId ? 'bundle:' + jobId : null;
  const stored = key ? (await chrome.storage.local.get(key))[key] : null;
  S.bundle = stored || (await chrome.storage.local.get('bundle:generic'))['bundle:generic'] || null;
  S.answers = { offline: true, blockers: stored?.blockers || [], flags: stored?.flags || [],
    answers: labels.map(f => { const a = offlineAnswer(f.label, S.bundle || { identity: S.identity || {} });
      return { label: f.label, kind: a.kind, answer: a.answer || '', choice: a.choice || null, needs_you: !a.answer }; }) };
}

/* -------------------------------------------------------------- rendering */
function render() {
  const jobBox = $('#job'), blockBox = $('#blockers'), fieldsBox = $('#fields');
  const j = S.job || {};
  const hasFields = !!(S.scan && S.scan.fields.length);
  put(jobBox,
    j.job_id ? [h('h1', { text: j.title || '' }), h('div', { class: 'co', text: j.company || '' }),
      h('div', { class: 'meta' }, `#${j.job_id}` + (j.applied ? ` · already applied ${new Date(j.applied * 1000).toLocaleDateString()}` : '') + (S.note ? ` · ${S.note}` : ''))]
    : [h('h1', {}, S.online ? 'No job loaded' : 'Board offline'),
       h('div', { class: 'co' }, S.online
         ? (j.vendor === 'workday' ? 'Workday boards are not ingested -- paste the numeric job id from the board above.'
            : j.vendor ? `${j.vendor} board -- this posting is not tracked yet; paste the numeric job id above if you have it.`
            : 'Open a real application page, or paste a job URL or id above.')
         : `Could not reach ${S.base} — cached answers only.`)]);
  const A = S.answers;
  const blockers = A?.blockers?.length ? A.blockers : (S.kit?.blockers || []);
  const flags = A?.flags?.length ? A.flags : (S.kit?.flags || []);
  put(blockBox,
    blockers.length ? h('div', { class: 'block' }, h('b', {}, 'STOP — the form itself rules you out'), ...blockers.map(b => h('p', {}, b))) : null,
    flags.length ? h('div', { class: 'block flag' }, h('b', {}, 'Answer these yourself'), ...flags.map(f => h('p', {}, f))) : null);
  if (A?.offline) status('warn', S.bundle ? `offline — cached answers from ${new Date(S.bundle.fetched_at).toLocaleString()}` : 'offline — identity and prepared lines only');
  renderKit();
  if (!hasFields) {
    put(fieldsBox, h('div', { class: 'empty' }, h('b', {}, 'No form fields on this page yet'), 'Open the application form, then rescan.'));
  } else {
    const byLabel = new Map((A?.answers || []).map(a => [a.label, a]));
    put(fieldsBox, S.scan.fields.map(f => fieldRow(f, byLabel.get(f.label))));
  }
  $('#foot').hidden = !j.job_id && !hasFields;
  $('#mark').disabled = !j.job_id || !S.online;
  $('#fill-safe').disabled = !hasFields;
}
function kitBlock(heading, bodyText) {
  const btn = h('button', { class: 'copy' }, 'copy');
  btn.addEventListener('click', () => copy(bodyText, btn));
  return h('div', { class: 'kititem' }, h('div', { class: 'kithead' }, h('b', { text: heading }), btn), h('pre', { text: bodyText }));
}
function renderKit() {
  const box = $('#kit');
  if (!S.kit) { box.hidden = true; return; }
  const all = [...(S.kit.sections || []).map(x => [x.heading, x.body]),
               ...(S.kit.stories || []).map(x => [x.title, x.body]),
               ...(S.kit.defences || []).map(x => [`If they ask: ${x.heading}`, x.body])];
  if (!all.length) { box.hidden = true; return; }
  const copyAllBtn = h('button', { class: 'copy' }, 'copy all');
  copyAllBtn.addEventListener('click', () => copy(S.kit.markdown, copyAllBtn));
  box.hidden = false;
  put(box, h('div', { class: 'kithead' }, h('b', {}, 'Prepared for this job'), copyAllBtn),
      all.map(([hd, bd]) => kitBlock(hd, bd)));
}
function fieldRow(f, a) {
  const needs = !a || a.needs_you;
  const value = a?.answer || '';
  const res = h('span', { class: 'res' });
  const copyBtn = h('button', { class: 'copy', disabled: !value, onclick: () => copy(value, copyBtn) }, 'copy');
  const fillable = value && !needs && f.type !== 'file' && !(f.options && f.options.length && !a.choice && f.type !== 'text' && f.type !== 'textarea');
  const fillBtn = h('button', { class: 'fillbtn', disabled: !fillable, onclick: () => doFill(f, a, res) }, 'fill');
  return h('div', { class: 'field' + (needs ? ' needs' : '') },
    h('div', { class: 'lab' }, h('b', {}, (f.required ? '* ' : '') + (f.label || '(unlabelled)')), h('small', {}, f.type + (a?.kind ? ' · ' + a.kind : ''))),
    f.type === 'file'
      ? h('pre', {}, a?.file ? `Cannot be filled by script. Drag the PDF onto the field:\n${a.file}` : 'Cannot be filled by script — attach your CV by hand.')
      : needs ? h('pre', {}, h('span', { class: 'needs-tag' }, '[NEEDS YOU] '), value.replace(/^\[NEEDS YOU\]\s*/, '') || 'nothing in resume/ answers this')
      : h('pre', {}, a.choice ? [h('span', { class: 'choice' }, a.choice), (value !== a.choice ? '\n' + value : '')] : value),
    h('div', { class: 'acts' },
      f.type === 'file' && a?.pdf_url ? h('a', { href: S.base + a.pdf_url + '?t=' + encodeURIComponent(S.token), target: '_blank', rel: 'noopener' }, 'open PDF') : null,
      f.type === 'file' && a?.file ? h('button', { class: 'copy', onclick: e => copy(a.file, e.target) }, 'copy path') : null,
      f.type !== 'file' ? copyBtn : null, f.type !== 'file' ? fillBtn : null, res));
}
async function copy(text, btn) {
  try { await navigator.clipboard.writeText(text); btn.textContent = 'copied'; btn.classList.add('copied'); }
  catch { btn.textContent = 'copy failed'; }
  setTimeout(() => { btn.textContent = btn.classList.contains('copied') ? 'copy' : btn.textContent; btn.classList.remove('copied'); }, 1400);
}
async function doFill(f, a, res) {
  res.textContent = '…'; res.className = 'res';
  try {
    const r = await chrome.tabs.sendMessage(S.tabId, { type: 'fill', id: f.id, selector: f.selector, value: a.answer, choice: a.choice }, { frameId: S.frameId });
    res.textContent = r?.ok ? 'filled' : (r?.reason || 'not filled'); res.className = 'res ' + (r?.ok ? 'ok' : 'bad');
  } catch (e) { res.textContent = 'page changed — rescan'; res.className = 'res bad'; }
}
const SAFE_KINDS = new Set(['first_name', 'last_name', 'preferred_name', 'full_name', 'email', 'phone', 'linkedin', 'links']);
// Never one-click filled, whatever the server computed for the copy-paste
// pack: self-identification, work authorisation/sponsorship, and any figure
// of his own money. Mirrors kit.GUARD_KINDS on the server, which already
// blanks `choice` and forces `needs_you` for these -- this is the second
// layer the upgrade spec asked for, so a future answers-endpoint bug here
// still can't one-click a sponsorship or salary field.
const GUARD_KINDS = new Set(['self_id', 'work_auth', 'sponsorship', 'salary', 'current_ctc']);
async function fillSafe() {
  if (!S.scan) { status('warn', 'nothing on this page to fill'); return; }
  const byLabel = new Map((S.answers?.answers || []).map(a => [a.label, a]));
  let n = 0;
  for (const f of S.scan.fields) {
    const a = byLabel.get(f.label);
    if (!a || a.needs_you || f.type === 'file' || f.type === 'textarea') continue;
    if (GUARD_KINDS.has(a.kind)) continue;
    const identity = SAFE_KINDS.has(a.kind) && ['text', 'contenteditable'].includes(f.type);
    const exactChoice = a.choice && ['select', 'radio', 'checkbox', 'react-select', 'listbox'].includes(f.type);
    if (!identity && !exactChoice) continue;
    const r = await chrome.tabs.sendMessage(S.tabId, { type: 'fill', id: f.id, selector: f.selector, value: a.answer, choice: a.choice }, { frameId: S.frameId }).catch(() => null);
    if (r?.ok) n++;
  }
  status('ok', `filled ${n} safe field(s) — identity and exact choices only. Read the rest before you press the site's Submit.`);
}
async function markApplied() {
  if (!S.job?.job_id) return;
  if (!confirm(`Record that you applied to ${S.job.company} — ${S.job.title}?\n\nOnly press OK after the site confirmed the submission.`)) return;
  try {
    const via = S.url ? `via helper on ${new URL(S.url).host}` : 'via helper (looked up, no live page)';
    const r = await board('/api/jobs/ext/applied', { method: 'POST', body: JSON.stringify({ job_id: S.job.job_id, lane: 'portal', note: via }) });
    status('ok', r.already ? 'Already recorded.' : `Recorded — the follow-up clock started for ${r.company}.`);
    S.job.applied = r.sent_at || Date.now() / 1000; render();
  } catch (e) { status('error', 'Could not record: ' + e.message); }
}

/* ------------------------------------------------------------------ boot */
async function refresh(full = true) {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab) return;
  S.tabId = tab.id;
  if (full) {
    S.gen++;                                        // supersede anything still in flight from before
    S.job = null; S.kit = null; S.answers = null; S.pinned = false;
    status('', '');
    render();          // reflect the clear immediately -- otherwise a scan that
                        // fails or never replies leaves the previous job's info
                        // on screen looking current when the state underneath
                        // has already moved on.
  }
  await requestScan();
}
async function init() {
  const cfg = await chrome.storage.local.get(['base', 'token', 'identity']);
  if (cfg.base) S.base = cfg.base.replace(/\/$/, '');
  S.token = cfg.token || ''; S.identity = cfg.identity || {};
  if (!S.token) await pair();
  if (!S.token) status('warn', 'Not paired yet. Run `python3 helper.py install` (or `launch`) in jobscout/, then press rescan.');
  $('#rescan').addEventListener('click', () => refresh(true));
  $('#lookupBtn').addEventListener('click', () => lookup($('#lookupInput').value));
  $('#lookupInput').addEventListener('keydown', e => { if (e.key === 'Enter') lookup($('#lookupInput').value); });
  $('#settings').addEventListener('click', () => chrome.runtime.openOptionsPage());
  $('#fill-safe').addEventListener('click', fillSafe);
  $('#mark').addEventListener('click', markApplied);
  chrome.tabs.onActivated.addListener(() => refresh(true));
  chrome.tabs.onUpdated.addListener((id, info) => { if (id === S.tabId && info.status === 'complete') refresh(true); });
  await refresh(true);
}
init();
