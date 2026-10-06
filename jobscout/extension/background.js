// The service worker. Two jobs: open the panel from the toolbar button, and
// pair with the board the moment the extension is installed or the browser
// starts -- so the token arrives with zero clicks, before any panel opens.
const BASE_DEFAULT = 'http://127.0.0.1:8790';

async function pair() {
  const cfg = await chrome.storage.local.get(['base', 'token']);
  const base = (cfg.base || BASE_DEFAULT).replace(/\/$/, '');
  try {
    const r = await fetch(base + '/api/jobs/ext/pair?ext=' + chrome.runtime.id);
    if (!r.ok) return false;
    const d = await r.json();
    if (!d.token) return false;
    await chrome.storage.local.set({ token: d.token, base: d.base || base });
    return true;
  } catch { return false; }
}
chrome.runtime.onInstalled.addListener(() => { pair(); });
chrome.runtime.onStartup.addListener(() => { pair(); });
chrome.runtime.onMessage.addListener((msg, sender, reply) => {
  if (msg?.type === 'pair') { pair().then(reply); return true; }
  return false;
});

if (chrome.sidePanel && chrome.sidePanel.setPanelBehavior) {
  chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true }).catch(() => {});
} else {
  chrome.action.onClicked.addListener(() => {
    chrome.windows.create({ url: chrome.runtime.getURL('sidepanel.html'), type: 'popup', width: 420, height: 760 });
  });
}
