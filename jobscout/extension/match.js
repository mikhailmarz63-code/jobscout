/* Offline fallback: the identity routes and the prepared-lines matcher,
 * ported from kit.py. Used only when the board cannot be reached. */
const IDENTITY_ROUTES = [
  [/^preferred first name/i, 'preferred_name'], [/first name/i, 'first_name'],
  [/last name|surname|family name/i, 'last_name'], [/^e-?mail|email address/i, 'email'],
  [/phone|mobile|contact number/i, 'phone'], [/linkedin/i, 'linkedin'],
  [/website|portfolio|github|personal site/i, 'links'], [/^\s*(?:full )?name\s*$/i, 'full_name'],
];
const STOP = new Set(['the','a','an','you','your','do','did','does','have','has','in','of','to','and','or','for','with','what','which','how','why','are','is','be','been','any','please','us','we','our','this','that','at','on','it','will','would','can','many']);
const words = s => new Set((s.toLowerCase().match(/[a-z]+/g) || []).filter(w => w.length > 2 && !STOP.has(w)));
function matchLine(label, lines, floor = 0.34) {
  const asked = words(label);
  if (!asked.size) return '';
  let best = '', score = 0;
  for (const [heading, answer] of Object.entries(lines || {})) {
    const known = words(heading);
    if (!known.size) continue;
    const inter = [...asked].filter(w => known.has(w)).length;
    const union = new Set([...asked, ...known]).size;
    const s = inter / union;
    if (s > score) { best = answer.trim(); score = s; }
  }
  return score >= floor ? best : '';
}
function offlineAnswer(label, bundle) {
  const id = bundle?.identity || {};
  const kind = (IDENTITY_ROUTES.find(([re]) => re.test(label)) || [])[1];
  if (kind === 'first_name' || kind === 'preferred_name') return { kind, answer: (id.name || '').split(' ')[0] };
  if (kind === 'last_name') return { kind, answer: (id.name || '').split(' ').slice(-1)[0] };
  if (kind === 'full_name') return { kind, answer: id.name || '' };
  if (kind === 'email') return { kind, answer: id.email || '' };
  if (kind === 'phone') return { kind, answer: id.phone || '' };
  if (kind === 'linkedin' || kind === 'links') return { kind, answer: id.linkedin || '' };
  const cached = (bundle?.answers || []).find(a => a.label.toLowerCase() === label.toLowerCase());
  if (cached) return { kind: cached.kind, answer: cached.answer, choice: cached.choice };
  return { kind: null, answer: matchLine(label, bundle?.lines) };
}
