'use strict';
const $ = s => document.querySelector(s);
(async () => {
  const cfg = await chrome.storage.local.get(['base', 'token']);
  $('#base').value = cfg.base || 'http://127.0.0.1:8790';
  $('#token').value = cfg.token || '';
})();
$('#save').addEventListener('click', async () => {
  await chrome.storage.local.set({ base: $('#base').value.trim().replace(/\/$/, ''), token: $('#token').value.trim() });
  $('#out').textContent = 'saved';
});
$('#test').addEventListener('click', async () => {
  const base = $('#base').value.trim().replace(/\/$/, ''), token = $('#token').value.trim();
  try {
    const r = await fetch(base + '/api/jobs/ext/stats', { headers: { 'X-Jobscout-Token': token } });
    if (!r.ok) throw new Error(r.status + ' ' + await r.text());
    const s = await r.json();
    const idm = await fetch(base + '/api/jobs/ext/bundle?job_id=0', { headers: { 'X-Jobscout-Token': token } }).catch(() => null);
    $('#out').textContent = `connected — ${s.jobs.toLocaleString()} open jobs, ${s.reachable} you can take. Extension id: ${chrome.runtime.id} (put chrome-extension://${chrome.runtime.id} in jobscout/.board-extension)`;
  } catch (e) { $('#out').textContent = 'failed: ' + e.message + ' — is the board running, is the token right, is the extension id allow-listed?'; }
});
$('#pair').addEventListener('click', async () => {
  const base = $('#base').value.trim().replace(/\/$/, '');
  try {
    const r = await fetch(base + '/api/jobs/ext/pair?ext=' + chrome.runtime.id);
    if (!r.ok) throw new Error(r.status + ' ' + await r.text());
    const d = await r.json();
    await chrome.storage.local.set({ base, token: d.token });
    $('#token').value = d.token;
    $('#out').textContent = 'paired — the board handed over its token';
  } catch (e) { $('#out').textContent = 'pair failed: ' + e.message + ' — is this id allow-listed (python3 helper.py install) and the board restarted?'; }
});
$('#forget').addEventListener('click', async () => { await chrome.storage.local.clear(); $('#base').value = ''; $('#token').value = ''; $('#out').textContent = 'forgotten'; });
