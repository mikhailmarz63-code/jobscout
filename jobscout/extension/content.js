/* jobscout helper — content script.
 *
 * Reads the form on the page: every visible control, the label it belongs
 * to, whether it is required, and its options. Fills one control when asked,
 * with the native setter so React notices. Never clicks Submit, never
 * touches anything but the one field named. Runs in every frame, because
 * company career pages embed the real form in an iframe. */
(() => {
  if (window.__jobscoutHelper) return;
  window.__jobscoutHelper = true;

  const registry = new Map();                 // id -> element
  let counter = 0;

  const text = el => (el?.innerText ?? el?.textContent ?? '').replace(/\s+/g, ' ').trim();
  const clean = s => s.replace(/\s*[*✱]\s*$/, '').replace(/\((required|optional)\)/ig, '')
    .replace(/\s*required\s*$/i, '').replace(/\s+/g, ' ').trim();
  const visible = el => !!(el.getClientRects().length) && getComputedStyle(el).visibility !== 'hidden';
  const humanise = s => s.replace(/[_\-]+/g, ' ').replace(/([a-z])([A-Z])/g, '$1 $2').trim();

  const CANDIDATES = 'input:not([type=hidden]):not([type=submit]):not([type=button]):not([type=reset]):not([type=image]), ' +
    'textarea, select, [contenteditable="true"], [role="combobox"], [role="radiogroup"], [role="listbox"], button[aria-haspopup="listbox"]';

  const ADAPTERS = [
    { name: 'greenhouse', match: u => /greenhouse\.io/.test(u), root: () => document.querySelector('#application_form, .application--form, form') || document.body },
    { name: 'ashby', match: u => /ashbyhq\.com/.test(u) },
    { name: 'lever', match: u => /lever\.co/.test(u), root: () => document.querySelector('#application-form, form') || document.body },
    { name: 'workable', match: u => /workable\.com/.test(u),
      labelFor: el => { const ui = el.closest('[data-ui]')?.getAttribute('data-ui'); return ui ? humanise(ui) : ''; } },
    { name: 'smartrecruiters', match: u => /smartrecruiters\.com/.test(u) },
    { name: 'workday', match: u => /myworkdayjobs\.com/.test(u),
      labelFor: el => { const id = el.closest('[data-automation-id]')?.getAttribute('data-automation-id'); return id ? humanise(id) : ''; },
      note: 'Workday: fields change per step — rescan after each Next.' },
  ];
  const adapter = ADAPTERS.find(a => a.match(location.href)) || { name: 'generic' };

  function labelFor(el) {
    const tries = [
      () => el.id && document.querySelector(`label[for="${CSS.escape(el.id)}"]`),
      () => { const ids = el.getAttribute('aria-labelledby'); return ids && ids.split(/\s+/).map(i => document.getElementById(i)).filter(Boolean).map(text).join(' '); },
      () => el.getAttribute('aria-label'),
      () => { const l = el.closest('label'); if (!l) return ''; const c = l.cloneNode(true); c.querySelectorAll('input,select,textarea').forEach(n => n.remove()); return text(c); },
      () => { const f = el.closest('fieldset'); return f && text(f.querySelector('legend')); },
      () => preceding(el),
      () => el.getAttribute('placeholder'),
      () => adapter.labelFor && adapter.labelFor(el),
      () => el.name && humanise(el.name),
    ];
    for (const t of tries) {
      try { const v = t(); const s = typeof v === 'string' ? v : (v ? text(v) : ''); if (s && s.trim()) return clean(s); } catch { /* next */ }
    }
    return '';
  }
  function preceding(el) {
    // label/legend/h1-h6/dt are trusted at any of the six levels: a real
    // question label sits there even for a radio/checkbox option nested
    // three ancestors inside its group (Ashby: each option is its own two-
    // input pair with no shared `name`, so the group's one real <label> is
    // found this way, several levels above any individual option).
    //
    // A bare div/span is not trustworthy that far out. It is capped to the
    // nearest four levels, one short line, and next to no children of its
    // own -- a real one-line label div is almost always childless. Without
    // this, an unlabelled field climbs past its own wrapper into whatever
    // unrelated content happens to sit nearby; on a live Ashby page this
    // matched the entire job-details sidebar (Location/Type/Compensation,
    // five rows folded into 175 characters) as the "label" for an unrelated
    // upload widget four ancestors away.
    let node = el, depth = 0;
    while (node && depth < 6) {
      let sib = node.previousElementSibling;
      while (sib) {
        if (sib.matches('label, legend, h1, h2, h3, h4, h5, h6, dt')) {
          const t = text(sib);
          if (t.length >= 2 && t.length <= 200 && !sib.querySelector('input,select,textarea')) return t;
        } else if (depth <= 3 && sib.matches('p, div, span')) {
          const t = text(sib);
          if (t.length >= 2 && t.length <= 80 && sib.querySelectorAll('*').length <= 2
              && !sib.querySelector('input,select,textarea')) return t;
        }
        sib = sib.previousElementSibling;
      }
      node = node.parentElement; depth++;
    }
    return '';
  }
  function headingAbove(el, re) {
    let node = el, depth = 0;
    while (node && depth < 8) {
      let sib = node.previousElementSibling;
      while (sib) {
        const t = text(sib);
        if (t && t.length <= 120 && re.test(t)) return t;
        sib = sib.previousElementSibling;
      }
      node = node.parentElement; depth++;
    }
    return '';
  }
  function kindOf(el) {
    const tag = el.tagName.toLowerCase();
    if (tag === 'select') return 'select';
    if (tag === 'textarea') return 'textarea';
    if (el.isContentEditable) return 'contenteditable';
    if (el.getAttribute('role') === 'combobox' || el.closest('.select__control, [class*="select__control"]')) return 'react-select';
    if (el.getAttribute('role') === 'listbox' || el.getAttribute('aria-haspopup') === 'listbox') return 'listbox';
    if (el.getAttribute('role') === 'radiogroup') return 'radio';
    if (tag === 'input') {
      const t = (el.type || 'text').toLowerCase();
      if (t === 'file') return 'file';
      if (t === 'radio' || t === 'checkbox') return t;
      if (t === 'date') return 'date';
      return 'text';
    }
    return 'text';
  }
  function optionsOf(el, kind) {
    if (kind === 'select') return [...el.options].map(o => o.text.trim()).filter(Boolean);
    if (kind === 'radio' || kind === 'checkbox') {
      const group = el.name ? [...document.querySelectorAll(`input[name="${CSS.escape(el.name)}"]`)] : [el];
      return group.map(i => labelFor(i) || i.value).filter(Boolean);
    }
    if (kind === 'react-select' || kind === 'listbox') return null;   // known only once opened
    return [];
  }
  function selectorFor(el) {
    const aid = el.getAttribute('data-automation-id');
    if (aid) return `[data-automation-id="${CSS.escape(aid)}"]`;
    if (el.id) return `#${CSS.escape(el.id)}`;
    if (el.name) return `${el.tagName.toLowerCase()}[name="${CSS.escape(el.name)}"]`;
    const parts = [];
    let n = el;
    while (n && n !== document.body && parts.length < 6) {
      const i = [...n.parentElement.children].filter(c => c.tagName === n.tagName).indexOf(n) + 1;
      parts.unshift(`${n.tagName.toLowerCase()}:nth-of-type(${i})`);
      n = n.parentElement;
    }
    return parts.join(' > ');
  }

  function scan() {
    registry.clear();
    const root = (adapter.root && adapter.root()) || document.body;
    const seen = new Set(), fields = [];
    for (const el of root.querySelectorAll(CANDIDATES)) {
      if (!visible(el) || el.disabled) continue;
      const kind = kindOf(el);
      // a react-select also renders a plain <input> for the form post; it is
      // the same control, so keep the combobox and drop the twin
      if (kind === 'text' && el.closest('[class*="select__"], [class*="select-shell"]')) continue;
      if (kind === 'text' && fields.length && fields[fields.length - 1].type === 'react-select' &&
          (!labelFor(el) || labelFor(el) === fields[fields.length - 1].label)) continue;
      if ((kind === 'radio' || kind === 'checkbox') && el.name) {
        if (seen.has('group:' + el.name)) continue;
        seen.add('group:' + el.name);
      }
      // a react-select renders a hidden-ish input inside the control; keep one per control
      if (kind === 'react-select') {
        const control = el.closest('.select__control, [class*="select__control"], [role="combobox"]') || el;
        if (seen.has(control)) continue;
        seen.add(control);
      }
      const id = 'f' + (++counter);
      registry.set(id, el);
      let label = labelFor(el);
      if (kind === 'file' && (!label || /^(attach|upload|browse|choose|select)( a)?( file)?$/i.test(label))) {
        // the upload button says "Attach"; the question is the heading above it
        label = headingAbove(el, /resume|cv\b|cover letter|portfolio/i) || preceding(el.closest('div') || el) || label;
      }
      if ((kind === 'radio' || kind === 'checkbox') && el.name) {
        const f = el.closest('fieldset');
        label = (f && text(f.querySelector('legend'))) || preceding(el.closest('div') || el) || label;
      }
      const required = !!(el.required || el.getAttribute('aria-required') === 'true' || /\*\s*$/.test(labelFor(el)) ||
        el.closest('[aria-required="true"], .required, [class*="required"]'));
      fields.push({ id, label: clean(label), type: kind, required, options: optionsOf(el, kind),
                    selector: selectorFor(el), placeholder: el.getAttribute('placeholder') || '', name: el.name || '' });
    }
    return { url: location.href, top: window === window.top, adapter: adapter.name, note: adapter.note || '', fields };
  }

  /* ------------------------------------------------------------------ fill */
  function setNative(el, value) {
    const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
    if (setter) setter.call(el, value); else el.value = value;
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
    el.dispatchEvent(new Event('blur', { bubbles: true }));
  }
  const norm = s => String(s || '').trim().toLowerCase();
  function pickOption(nodes, choice) {
    const want = norm(choice);
    return nodes.find(n => norm(text(n)) === want) || nodes.find(n => norm(text(n)).startsWith(want)) || nodes.find(n => norm(text(n)).includes(want));
  }
  const wait = ms => new Promise(r => setTimeout(r, ms));
  async function waitFor(sel, ms = 1500) {
    const t0 = Date.now();
    while (Date.now() - t0 < ms) {
      const nodes = [...document.querySelectorAll(sel)].filter(visible);
      if (nodes.length) return nodes;
      await wait(80);
    }
    return [];
  }
  function flash(el) {
    el.scrollIntoView({ block: 'center', behavior: 'smooth' });
    el.classList.add('jobscout-flash');
    setTimeout(() => el.classList.remove('jobscout-flash'), 1500);
  }
  async function fill({ id, value, choice, selector }) {
    let el = registry.get(id);
    if (!el || !document.contains(el)) el = selector ? document.querySelector(selector) : null;
    if (!el) return { ok: false, reason: 'field disappeared — rescan' };
    const kind = kindOf(el);
    try {
      if (kind === 'file') return { ok: false, reason: 'file inputs cannot be filled by script — drag the PDF onto it' };
      if (kind === 'select') {
        const opt = pickOption([...el.options], choice || value);
        if (!opt) return { ok: false, reason: 'no option matches' };
        el.value = opt.value; el.dispatchEvent(new Event('change', { bubbles: true })); flash(el); return { ok: true };
      }
      if (kind === 'radio' || kind === 'checkbox') {
        const group = el.name ? [...document.querySelectorAll(`input[name="${CSS.escape(el.name)}"]`)] : [el];
        const target = group.find(i => norm(labelFor(i) || i.value) === norm(choice || value)) ||
                       group.find(i => norm(labelFor(i) || i.value).startsWith(norm(choice || value)));
        if (!target) return { ok: false, reason: 'no option matches' };
        target.click(); flash(target); return { ok: true };
      }
      if (kind === 'react-select' || kind === 'listbox') {
        const input = el.matches('input') ? el : el.querySelector('input') || el;
        input.focus(); input.click?.();
        if (input.matches('input')) setNative(input, choice || value);
        const opts = await waitFor('[role="option"], .select__option, [class*="select__option"], [id*="-option-"]');
        const hit = pickOption(opts, choice || value);
        if (!hit) return { ok: false, reason: opts.length ? 'no option matches — pick it by hand' : 'menu did not open — pick it by hand' };
        hit.click(); flash(el); return { ok: true };
      }
      if (kind === 'contenteditable') {
        el.focus();
        const done = document.execCommand && document.execCommand('insertText', false, value);
        if (!done) { el.textContent = value; el.dispatchEvent(new Event('input', { bubbles: true })); }
        flash(el); return { ok: true };
      }
      setNative(el, value); flash(el); return { ok: true };
    } catch (e) { return { ok: false, reason: String(e.message || e) }; }
  }

  chrome.runtime.onMessage.addListener((msg, sender, reply) => {
    if (msg?.type === 'scan') {
      const result = scan();
      chrome.runtime.sendMessage({ type: 'scanned', ...result }).catch(() => {});
      reply(result);
    } else if (msg?.type === 'fill') {
      fill(msg).then(reply);
      return true;
    } else if (msg?.type === 'flash') {
      const el = registry.get(msg.id); if (el) flash(el); reply({ ok: !!el });
    }
    return false;
  });

  let timer;
  new MutationObserver(() => {
    clearTimeout(timer);
    timer = setTimeout(() => chrome.runtime.sendMessage({ type: 'changed', url: location.href }).catch(() => {}), 500);
  }).observe(document.documentElement, { childList: true, subtree: true });
})();
