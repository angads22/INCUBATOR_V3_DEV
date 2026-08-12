let currentStep = 0;
let selectedSsid = '';
let selectedSsidSecure = null;   // true=locked, false=open, null=typed by hand

const WIZARD_LABELS = [
  'Welcome', 'Wi-Fi network', 'Operator account',
  'Device name', 'Quick tour', 'Finishing up',
];
const FINISH_STEP = 5;

// Recovery mode: the last Wi-Fi join failed and the setup network came back.
// The server marks the page (wizard-wrap data attributes); the wizard then
// asks ONLY for the Wi-Fi, name and login are already saved on the device.
const _wrap = document.getElementById('wizardWrap');
const RETRY_MODE = !!(_wrap && _wrap.dataset.retry);
const RETRY_DEVICE_NAME = _wrap?.dataset.deviceName || '';
const RETRY_SSID = _wrap?.dataset.retrySsid || '';

// Mirrors the device-side SSID rule so a name the incubator would reject
// fails HERE, with a friendly message, not minutes later as a dead join.
const SSID_OK_RE = /^[\w\s\-\.@#!]{1,32}$/;

// Persist wizard fields so a brief Wi-Fi blip (or a captive-sheet reload) doesn't
// wipe what the user already typed, on reconnect the wizard resumes. Passwords
// are intentionally NOT persisted.
const WIZ_KEY = 'incubatorWizardState';
function saveWizard() {
  try {
    const g = (id) => document.getElementById(id);
    localStorage.setItem(WIZ_KEY, JSON.stringify({
      ssid: selectedSsid,
      manual: g('manualSsid')?.value || '',
      deviceName: g('deviceName')?.value || '',
      createAccount: g('createAccountCheck')?.checked || false,
      username: g('acctUsername')?.value || '',
      email: g('acctEmail')?.value || '',
    }));
  } catch { /* storage disabled, non-fatal */ }
}
function restoreWizard() {
  try {
    const s = JSON.parse(localStorage.getItem(WIZ_KEY) || 'null');
    if (!s) return;
    const g = (id) => document.getElementById(id);
    if (s.manual && g('manualSsid')) { g('manualSsid').value = s.manual; selectedSsid = s.manual; }
    else if (s.ssid) { selectedSsid = s.ssid; }
    if (s.deviceName && g('deviceName')) g('deviceName').value = s.deviceName;
    if (s.createAccount && g('createAccountCheck')) {
      g('createAccountCheck').checked = true;
      const f = g('accountFields'); if (f) f.style.display = 'grid';
    }
    if (s.username && g('acctUsername')) g('acctUsername').value = s.username;
    if (s.email && g('acctEmail')) g('acctEmail').value = s.email;
  } catch { /* ignore */ }
}
function clearWizard() { try { localStorage.removeItem(WIZ_KEY); } catch { /* ignore */ } }
// Save on any field change, and restore whatever was typed before a reload.
document.addEventListener('input', saveWizard);
document.addEventListener('change', saveWizard);
restoreWizard();

function showStep(n) {
  document.querySelectorAll('.wizard-step').forEach((el) => el.classList.remove('active'));
  const target = document.querySelector(`.wizard-step[data-step="${n}"]`);
  if (target) target.classList.add('active');
  currentStep = n;

  // Progress indicator (optional, guarded so it never breaks the wizard).
  const total = WIZARD_LABELS.length - 1; // steps 0..6
  const fill = document.getElementById('wizardProgressFill');
  const label = document.getElementById('wizardStepLabel');
  if (fill) fill.style.width = `${Math.round((n / total) * 100)}%`;
  if (label) {
    label.textContent =
      `Step ${Math.min(n + 1, WIZARD_LABELS.length)} of ${WIZARD_LABELS.length} · ${WIZARD_LABELS[n] || ''}`;
  }
}

// Generic prev navigation
document.querySelectorAll('[data-prev]').forEach((btn) => {
  btn.addEventListener('click', () => showStep(currentStep - 1));
});

// Fail-fast Wi-Fi checks: catch a doomed join BEFORE it is submitted, while
// the person is still looking at the screen that can fix it.
function wifiStepValid() {
  const msg = document.getElementById('wifiStepMsg');
  const say = (t) => { if (msg) msg.textContent = t; return false; };
  if (!selectedSsid) return say('Pick your Wi-Fi from the list, or type its name.');
  if (!SSID_OK_RE.test(selectedSsid)) {
    return say("That network name has characters the incubator can't use, double-check the spelling.");
  }
  const pw = document.getElementById('wifiPassword')?.value || '';
  if (pw && pw.length < 8) {
    return say('Wi-Fi passwords are at least 8 characters, double-check yours.');
  }
  if (selectedSsidSecure === true && !pw) {
    return say('This network is locked 🔒, it needs its Wi-Fi password.');
  }
  if (msg) msg.textContent = '';
  return true;
}

// Generic next navigation
document.querySelectorAll('[data-next]').forEach((btn) => {
  btn.addEventListener('click', () => {
    if (currentStep === 1) {
      if (!wifiStepValid()) return;
      if (RETRY_MODE) {
        // Everything else is already on the device, go straight to saving.
        showStep(FINISH_STEP);
        submitSetup();
        return;
      }
    }
    showStep(currentStep + 1);
  });
});

// Step 0: Begin setup
document.getElementById('beginSetupBtn')?.addEventListener('click', async () => {
  const msg = document.getElementById('setupStartMsg');
  msg.textContent = 'Starting setup mode\u2026';
  try {
    const res = await fetch('/onboarding/start', { method: 'POST' });
    const data = await res.json();
    if (data.ok) {
      msg.textContent = '';
      showStep(1);
      loadWifiNetworks();
    } else {
      msg.textContent = 'Could not start setup mode. Please try again.';
    }
  } catch {
    msg.textContent = 'Connection error. Ensure the device is reachable.';
  }
});

// Step 1: Wi-Fi scan
async function loadWifiNetworks() {
  const listEl = document.getElementById('wifiList');
  const msgEl = document.getElementById('wifiScanMsg');
  const nextBtn = document.getElementById('wifiNextBtn');
  listEl.innerHTML = '';
  selectedSsid = '';
  nextBtn.disabled = true;
  msgEl.textContent = 'Scanning for networks\u2026';
  try {
    const res = await fetch('/onboarding/wifi-scan');
    const data = await res.json();
    if (!data.networks || data.networks.length === 0) {
      msgEl.textContent = 'No networks found. You can still continue and set Wi-Fi later in network settings.';
      nextBtn.disabled = false;
      return;
    }
    msgEl.textContent = '';
    data.networks.forEach((net) => {
      const item = document.createElement('button');
      item.className = 'wifi-item';
      item.type = 'button';
      item.innerHTML =
        `<span class="wifi-ssid">${net.ssid}</span>` +
        `<span class="wifi-strength">${net.strength}%${net.secure ? ' \uD83D\uDD12' : ''}</span>`;
      item.addEventListener('click', () => {
        document.querySelectorAll('.wifi-item').forEach((el) => el.classList.remove('selected'));
        item.classList.add('selected');
        selectedSsid = net.ssid;
        selectedSsidSecure = !!net.secure;
        const manual = document.getElementById('manualSsid');
        if (manual) manual.value = '';
        _showPasswordFor(net.ssid, !!net.secure);
        nextBtn.disabled = false;
      });
      listEl.appendChild(item);
    });
  } catch {
    msgEl.textContent = 'Scan failed. You can still continue by typing your network name below.';
    nextBtn.disabled = false;
  }
}

// Reveal the inline password field for the chosen network. Open networks
// need no password, so the field stays hidden and the join is one tap.
function _showPasswordFor(ssid, secure) {
  const wrap = document.getElementById('wifiPasswordWrap');
  const label = document.getElementById('selectedSsidLabel');
  const pw = document.getElementById('wifiPassword');
  if (label) label.textContent = ssid;
  if (!wrap) return;
  if (secure === false) {
    wrap.setAttribute('hidden', '');
    if (pw) pw.value = '';
  } else {
    wrap.removeAttribute('hidden');   // locked, or typed by hand (unknown)
  }
}

// Manual SSID entry. The single-radio Pi often can't scan while its own
// hotspot is up, so let the operator just type the network name.
document.getElementById('manualSsid')?.addEventListener('input', (e) => {
  const value = e.target.value.trim();
  const nextBtn = document.getElementById('wifiNextBtn');
  if (value) {
    document.querySelectorAll('.wifi-item').forEach((el) => el.classList.remove('selected'));
    selectedSsid = value;
    selectedSsidSecure = null;        // unknown, password shown but optional
    _showPasswordFor(value, null);
    if (nextBtn) nextBtn.disabled = false;
  } else {
    selectedSsid = '';
    selectedSsidSecure = null;
    if (nextBtn) nextBtn.disabled = true;
  }
});

// Step 3: Account fields toggle
document.getElementById('createAccountCheck')?.addEventListener('change', (e) => {
  const fields = document.getElementById('accountFields');
  fields.style.display = e.target.checked ? 'grid' : 'none';
});

// Quick tour → Finish
document.getElementById('finishSetupBtn')?.addEventListener('click', () => {
  showStep(FINISH_STEP);
  submitSetup();
});

// Recovery mode: skip the welcome screen, make sure the AP session is
// registered (idempotent, never drops the phone), and land straight on
// the Wi-Fi step with the failed network preselected for a fast retry.
if (RETRY_MODE) {
  fetch('/onboarding/start', { method: 'POST' }).catch(() => {});
  showStep(1);
  loadWifiNetworks();
  if (RETRY_SSID) {
    const manual = document.getElementById('manualSsid');
    if (manual) { manual.value = RETRY_SSID; }
    selectedSsid = RETRY_SSID;
    selectedSsidSecure = null;
    _showPasswordFor(RETRY_SSID, null);
    const nextBtn = document.getElementById('wifiNextBtn');
    if (nextBtn) nextBtn.disabled = false;
  }
}

async function submitSetup() {
  const title = document.getElementById('finishTitle');
  const msg = document.getElementById('finishMsg');
  const result = document.getElementById('finishResult');
  const link = document.getElementById('finishLink');

  const createAccount = document.getElementById('createAccountCheck')?.checked || false;
  const username = createAccount ? (document.getElementById('acctUsername')?.value || '') : null;
  const email = createAccount ? (document.getElementById('acctEmail')?.value || '') : null;
  const password = createAccount ? (document.getElementById('acctPassword')?.value || '') : null;

  if (createAccount) {
    if (!username || username.length < 3) {
      title.textContent = 'Validation Error';
      result.textContent = 'Username must be at least 3 characters.';
      return;
    }
    if (!email || !email.includes('@') || email.length < 5) {
      title.textContent = 'Validation Error';
      result.textContent = 'Please enter a valid email address.';
      return;
    }
    if (!password || password.length < 8) {
      title.textContent = 'Validation Error';
      result.textContent = 'Password must be at least 8 characters.';
      return;
    }
  }

  const payload = {
    ssid: selectedSsid,
    wifi_password: document.getElementById('wifiPassword')?.value || '',
    // Recovery: keep the name the device already has, the wizard never
    // showed the naming step, so the input still holds its placeholder.
    device_name: RETRY_MODE
      ? (RETRY_DEVICE_NAME || document.getElementById('deviceName')?.value || 'My Incubator')
      : (document.getElementById('deviceName')?.value || 'My Incubator'),
    create_account: RETRY_MODE ? false : createAccount,
    username,
    email,
    password,
  };

  try {
    const res = await fetch('/onboarding/complete', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (data.ok) {
      clearWizard();   // setup succeeded \u2014 drop the saved draft
      title.textContent = 'Setup complete! \ud83c\udf89';
      msg.textContent =
        `Your incubator \u201c${data.device_name}\u201d is now joining your Wi-Fi. ` +
        'The setup network will switch off in a few seconds.';
      result.textContent = '';

      // openUrl = the fixed hotspot IP (10.42.0.1) \u2014 reachable RIGHT NOW because
      // the phone is still on the setup network, and needs no mDNS (which Android
      // browsers often can't resolve). appUrl = the durable <hostname>.local
      // address to bookmark for daily use once the device is on home Wi-Fi.
      const openUrl = data.open_url || `http://10.42.0.1:8000`;
      const appUrl = data.app_url || '/';
      const next = document.getElementById('finishNext');
      const urlEl = document.getElementById('finishUrl');
      const homeSsid = document.getElementById('finishHomeSsid');
      if (urlEl) urlEl.textContent = appUrl; // the durable address to save
      if (homeSsid) homeSsid.textContent = data.wifi_ssid ? `\u201c${data.wifi_ssid}\u201d` : '';
      if (next) next.removeAttribute('hidden');

      // "Open now" is a real tap (user gesture), so try to hand off to the
      // system default browser via window.open \u2014 that's the only way to escape
      // the captive-portal popup on most phones. It targets the DURABLE
      // address (not the hotspot IP): while the AP is still up, the captive
      // DNS hijack + avahi resolve it here too, and after the operator
      // reconnects to their home Wi-Fi the very same link keeps working \u2014
      // the hotspot IP would die seconds after the radio flips.
      if (link) {
        link.href = appUrl;
        link.setAttribute('rel', 'noopener');
        link.addEventListener('click', (e) => {
          const w = window.open(appUrl, '_blank');
          if (w) { e.preventDefault(); }   // opened in the real browser
          // else: let the href navigate in place
        });
      }

      // Copy the durable address so the user can paste it into their browser.
      // navigator.clipboard does NOT exist on plain-http pages (this setup page
      // is http://10.42.0.1), fall back to the legacy execCommand('copy')
      // path, and if even that fails, select the address and say exactly how
      // to copy it by hand.
      const copyBtn = document.getElementById('finishCopyBtn');
      const copyMsg = document.getElementById('finishCopyMsg');
      const copyText = (text) => {
        if (navigator.clipboard && window.isSecureContext) {
          return navigator.clipboard.writeText(text);
        }
        return new Promise((resolve, reject) => {
          const ta = document.createElement('textarea');
          ta.value = text;
          ta.setAttribute('readonly', '');
          ta.style.position = 'fixed';
          ta.style.opacity = '0';
          document.body.appendChild(ta);
          ta.focus();
          ta.select();
          ta.setSelectionRange(0, text.length);  // iOS Safari needs the range
          let ok = false;
          try { ok = document.execCommand('copy'); } catch { ok = false; }
          ta.remove();
          ok ? resolve() : reject(new Error('execCommand copy failed'));
        });
      };
      const selectUrl = () => {
        if (!urlEl) return;
        const range = document.createRange();
        range.selectNodeContents(urlEl);
        const sel = window.getSelection();
        if (sel) { sel.removeAllRanges(); sel.addRange(range); }
      };
      if (urlEl) urlEl.addEventListener('click', selectUrl);  // tap = select all
      if (copyBtn) {
        copyBtn.addEventListener('click', async () => {
          try {
            await copyText(appUrl);
            copyBtn.textContent = '✓ Copied';
            if (copyMsg) copyMsg.textContent = 'Copied! Paste it into Safari or Chrome once you’re back on your home Wi-Fi.';
          } catch {
            selectUrl();
            if (copyMsg) copyMsg.textContent = 'The address above is selected, press and hold it, then choose Copy.';
          }
        });
      }

      // Deliberately NO auto-redirect. This page needs no further contact with
      // the device, so it survives in the browser even after the setup network
      // drops \u2014 the copy button and instructions keep working while the
      // incubator switches to home Wi-Fi. The old 4-second auto-navigation
      // yanked the user to a dashboard that died seconds later when the AP
      // went down, erasing these instructions before anyone could read them.
      void openUrl; // kept in the response payload for API compatibility
      const openNow = document.getElementById('finishOpenNote');
      if (openNow) {
        openNow.textContent = 'Keep this page open \u2014 follow the steps below.';
      }
    } else {
      title.textContent = 'Setup Error';
      result.textContent = data.detail || 'Unknown error. Please retry.';
    }
  } catch (err) {
    title.textContent = 'Setup Error';
    result.textContent = `Request failed: ${err}`;
  }
}
