// Setup wizard — smart-vacuum flow.
//
//   pane 0  Not in setup mode: "Start setup mode" → join the incubator's Wi-Fi
//   pane 1  Step 1 of 2: pick Wi-Fi (password only when needed)
//   pane 2  Step 2 of 2: name + optional login → Connect
//   pane 3  Connecting: checklist → finds the device on the home network and
//           opens it; detects the hotspot coming back (join failed); after
//           180 s falls back to manual instructions.
//
// Served from http://<ap_ip>:8000 while the phone is on the setup hotspot. The
// discovery probes therefore hit OTHER origins (hostname.local, LAN IP) with
// mode:'cors'; the device answers /api/setup/ping with Access-Control-Allow-
// Origin: *. Any thrown fetch error simply means "not there yet".
//
// Vanilla ES2017, no dependencies, every element lookup guarded.

(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const wrap = $('wizardWrap');
  if (!wrap) return;
  const D = wrap.dataset;

  const RETRY_MODE = D.retry === '1';
  const RETRY_DEVICE_NAME = D.deviceName || '';
  const RETRY_SSID = D.retrySsid || '';
  const IN_SETUP = D.inSetup === '1';
  const AP_SSID = D.apSsid || 'Incubator';
  const AP_IP = D.apIp || '10.42.0.1';
  const PING_PATH = D.pingPath || '/api/setup/ping';
  const HOSTNAME = D.hostname || '';

  // Discovery timing (ms). The setup AP stays up ~12 s on purpose so this page
  // can finish loading; only treat an answer from the AP address as "the
  // hotspot came back" once that window is well past.
  const PROBE_EVERY_MS = 3000;
  const PROBE_TIMEOUT_MS = 2500;
  const AP_RETURN_GRACE_MS = 25000;
  const SWITCH_DONE_MS = 15000;
  const REJOIN_DONE_MS = 25000;
  // Longer than the device's worst-case failed-join path (grace + nmcli
  // association + profile fallback ≈ 2.5 min), so a returning setup AP is
  // still reported as a failed join rather than as 'could not confirm'.
  const DISCOVERY_TIMEOUT_MS = 240000;
  const AUTO_OPEN_SECONDS = 5;

  // Mirrors the device-side SSID rule so a name the incubator would reject
  // fails HERE, with a friendly message — not minutes later as a dead join.
  // Mirrors WiFiService.valid_ssid: any printable characters (no control
  // chars), non-blank, at most 32 bytes of UTF-8. Names like "John's iPhone"
  // or "Café (2.4G)" are fine; the device passes them to nmcli as argv.
  const SSID_OK_RE = /^[^\u0000-\u001F\u007F-\u009F]{1,32}$/u;
  function ssidOk(s) {
    if (typeof s !== 'string' || !SSID_OK_RE.test(s) || !s.trim()) return false;
    try { return new TextEncoder().encode(s).length <= 32; } catch { return s.length <= 32; }
  }

  let currentStep = IN_SETUP ? 1 : 0;
  let selectedSsid = '';
  let selectedSsidSecure = null;   // true=locked, false=open, null=typed by hand

  function templateCandidates() {
    try {
      const raw = $('wizardCandidates');
      const list = raw ? JSON.parse(raw.textContent || '[]') : [];
      return Array.isArray(list) ? list : [];
    } catch {
      return [];
    }
  }

  // ── Draft persistence (no passwords) ──────────────────────────────────
  // A brief Wi-Fi blip or a captive-sheet reload must not wipe what the user
  // already typed — on reconnect the wizard resumes.
  const WIZ_KEY = 'incubatorWizardState';
  function saveWizard() {
    try {
      localStorage.setItem(WIZ_KEY, JSON.stringify({
        ssid: selectedSsid,
        manual: ($('manualSsid') || {}).value || '',
        deviceName: ($('deviceName') || {}).value || '',
        createAccount: !!(($('createAccountCheck') || {}).checked),
        username: ($('acctUsername') || {}).value || '',
        email: ($('acctEmail') || {}).value || '',
      }));
    } catch { /* storage disabled — non-fatal */ }
  }
  function restoreWizard() {
    try {
      const s = JSON.parse(localStorage.getItem(WIZ_KEY) || 'null');
      if (!s) return;
      if (s.manual && $('manualSsid')) { $('manualSsid').value = s.manual; selectedSsid = s.manual; }
      else if (s.ssid) { selectedSsid = s.ssid; }
      if (s.deviceName && $('deviceName')) $('deviceName').value = s.deviceName;
      if (s.createAccount && $('createAccountCheck')) {
        $('createAccountCheck').checked = true;
        showAccountFields(true);
      }
      if (s.username && $('acctUsername')) $('acctUsername').value = s.username;
      if (s.email && $('acctEmail')) $('acctEmail').value = s.email;
    } catch { /* ignore */ }
  }
  function clearWizard() { try { localStorage.removeItem(WIZ_KEY); } catch { /* ignore */ } }
  document.addEventListener('input', saveWizard);
  document.addEventListener('change', saveWizard);

  // ── Pane navigation ───────────────────────────────────────────────────
  const STEP_LABELS = { 1: 'Step 1 of 2 · Wi-Fi', 2: 'Step 2 of 2 · Finish up' };
  function showStep(n) {
    document.querySelectorAll('.wizard-step').forEach((el) => el.classList.remove('active'));
    const target = document.querySelector(`.wizard-step[data-step="${n}"]`);
    if (target) target.classList.add('active');
    currentStep = n;
    const progress = $('wizardProgress');
    const fill = $('wizardProgressFill');
    const label = $('wizardStepLabel');
    const numbered = n === 1 || n === 2;
    if (progress) progress.hidden = !numbered;
    if (fill) fill.style.width = numbered ? `${n * 50}%` : '100%';
    if (label && STEP_LABELS[n]) label.textContent = STEP_LABELS[n];
    try { window.scrollTo({ top: 0, behavior: 'smooth' }); } catch { window.scrollTo(0, 0); }
  }

  document.querySelectorAll('[data-prev]').forEach((btn) => {
    btn.addEventListener('click', () => showStep(Math.max(1, currentStep - 1)));
  });

  // ── Pane 0: start setup mode from the home network ────────────────────
  $('startSetupModeBtn')?.addEventListener('click', async () => {
    const btn = $('startSetupModeBtn');
    const msg = $('setupStartMsg');
    if (btn) btn.disabled = true;
    if (msg) msg.textContent = 'Starting setup mode…';
    try {
      const res = await fetch('/onboarding/start', { method: 'POST' });
      const data = await res.json().catch(() => null);
      if (res.ok && data && data.ok !== false) {
        if (msg) msg.textContent = '';
        if (btn) btn.hidden = true;
        const note = $('setupStartedNote');
        if (note) note.hidden = false;
      } else {
        if (msg) msg.textContent = (data && data.detail) || 'Could not start setup mode. Please try again.';
        if (btn) btn.disabled = false;
      }
    } catch {
      // The AP switch drops this very connection on single-radio boards, so a
      // network error here most likely means it worked.
      if (msg) msg.textContent = '';
      if (btn) btn.hidden = true;
      const note = $('setupStartedNote');
      if (note) note.hidden = false;
    }
  });

  $('continueHereLink')?.addEventListener('click', (e) => {
    e.preventDefault();
    showStep(1);
    loadWifiNetworks();
  });

  // ── Step 1: Wi-Fi list ────────────────────────────────────────────────
  function signalBars(strength) {
    const pct = Math.max(0, Math.min(100, Number(strength) || 0));
    const lit = pct >= 75 ? 4 : pct >= 50 ? 3 : pct >= 25 ? 2 : 1;
    const bars = document.createElement('span');
    bars.className = 'wifi-bars';
    bars.title = `${pct}% signal`;
    bars.setAttribute('aria-label', `${pct}% signal`);
    for (let i = 1; i <= 4; i++) {
      const bar = document.createElement('i');
      if (i <= lit) bar.className = 'on';
      bars.appendChild(bar);
    }
    return bars;
  }

  // Build a list row with createElement/textContent only — SSIDs come from
  // the air and must never be interpreted as HTML.
  function networkRow(net) {
    const item = document.createElement('button');
    item.className = 'wifi-item';
    item.type = 'button';
    item.setAttribute('role', 'option');
    item.setAttribute('aria-selected', 'false');

    const name = document.createElement('span');
    name.className = 'wifi-ssid';
    if (net.secure) {
      const lock = document.createElement('span');
      lock.className = 'wifi-lock';
      lock.textContent = '🔒';
      lock.setAttribute('aria-label', 'password protected');
      name.appendChild(lock);
    }
    name.appendChild(document.createTextNode(net.ssid));

    const right = document.createElement('span');
    right.className = 'wifi-strength';
    right.appendChild(signalBars(net.strength));

    item.appendChild(name);
    item.appendChild(right);
    item.addEventListener('click', () => {
      document.querySelectorAll('.wifi-item').forEach((el) => {
        el.classList.remove('selected');
        el.setAttribute('aria-selected', 'false');
      });
      item.classList.add('selected');
      item.setAttribute('aria-selected', 'true');
      selectedSsid = net.ssid;
      selectedSsidSecure = !!net.secure;
      const manual = $('manualSsid');
      if (manual) manual.value = '';
      showPasswordFor(net.ssid, !!net.secure);
      const nextBtn = $('wifiNextBtn');
      if (nextBtn) nextBtn.disabled = false;
      const msg = $('wifiStepMsg');
      if (msg) msg.textContent = '';
      saveWizard();
    });
    return item;
  }

  function tidyNetworks(list) {
    const seen = new Map();
    (list || []).forEach((net) => {
      if (!net || typeof net.ssid !== 'string') return;
      const ssid = net.ssid.trim();
      if (!ssid || ssid === AP_SSID) return;          // hidden networks, our own AP
      const prev = seen.get(ssid);
      if (!prev || (Number(net.strength) || 0) > (Number(prev.strength) || 0)) {
        seen.set(ssid, { ssid, strength: Number(net.strength) || 0, secure: !!net.secure });
      }
    });
    return Array.from(seen.values()).sort((a, b) => b.strength - a.strength);
  }

  let scanning = false;
  async function loadWifiNetworks() {
    if (scanning) return;
    const listEl = $('wifiList');
    const msgEl = $('wifiScanMsg');
    const nextBtn = $('wifiNextBtn');
    const refresh = $('wifiRefreshBtn');
    if (!listEl) return;
    scanning = true;
    if (refresh) refresh.disabled = true;
    while (listEl.firstChild) listEl.removeChild(listEl.firstChild);
    if (msgEl) msgEl.textContent = 'Scanning for networks…';
    try {
      const res = await fetch('/onboarding/wifi-scan', { cache: 'no-store' });
      const data = await res.json();
      const networks = tidyNetworks(data && data.networks);
      if (networks.length === 0) {
        if (msgEl) msgEl.textContent = 'No networks found yet — tap Refresh, or type your Wi-Fi name below.';
      } else {
        if (msgEl) msgEl.textContent = 'Tap your Wi-Fi:';
        networks.forEach((net) => {
          const row = networkRow(net);
          if (net.ssid === selectedSsid) {
            row.classList.add('selected');
            row.setAttribute('aria-selected', 'true');
          }
          listEl.appendChild(row);
        });
      }
    } catch {
      if (msgEl) msgEl.textContent = "Couldn't scan right now — type your Wi-Fi name below.";
    } finally {
      scanning = false;
      if (refresh) refresh.disabled = false;
      if (nextBtn) nextBtn.disabled = !selectedSsid;
    }
  }
  $('wifiRefreshBtn')?.addEventListener('click', loadWifiNetworks);

  // Reveal the inline password field for the chosen network. Open networks
  // need no password, so the field stays hidden and the join is one tap.
  function showPasswordFor(ssid, secure) {
    const pwWrap = $('wifiPasswordWrap');
    const label = $('selectedSsidLabel');
    const pw = $('wifiPassword');
    if (label) label.textContent = ssid;
    if (!pwWrap) return;
    if (secure === false) {
      pwWrap.setAttribute('hidden', '');
      if (pw) pw.value = '';
    } else {
      pwWrap.removeAttribute('hidden');   // locked, or typed by hand (unknown)
      if (pw && secure === true) { try { pw.focus({ preventScroll: true }); } catch { /* ignore */ } }
    }
  }

  $('wifiShowPassword')?.addEventListener('change', (e) => {
    const pw = $('wifiPassword');
    if (pw) pw.type = e.target.checked ? 'text' : 'password';
  });

  // Manual SSID entry — a single-radio board often can't scan while its own
  // hotspot is up, so let the operator just type the network name.
  $('manualSsid')?.addEventListener('input', (e) => {
    const value = e.target.value.trim();
    const nextBtn = $('wifiNextBtn');
    if (value) {
      document.querySelectorAll('.wifi-item').forEach((el) => {
        el.classList.remove('selected');
        el.setAttribute('aria-selected', 'false');
      });
      selectedSsid = value;
      selectedSsidSecure = null;        // unknown — password shown but optional
      showPasswordFor(value, null);
      if (nextBtn) nextBtn.disabled = false;
    } else {
      selectedSsid = '';
      selectedSsidSecure = null;
      if (nextBtn) nextBtn.disabled = true;
    }
  });

  // Fail-fast Wi-Fi checks: catch a doomed join BEFORE it is submitted, while
  // the person is still looking at the screen that can fix it.
  function wifiStepValid() {
    const msg = $('wifiStepMsg');
    const say = (t) => { if (msg) msg.textContent = t; return false; };
    if (!selectedSsid) return say('Pick your Wi-Fi from the list, or type its name.');
    if (!ssidOk(selectedSsid)) {
      return say("That network name has characters the incubator can't use — double-check the spelling.");
    }
    const pw = ($('wifiPassword') || {}).value || '';
    if (pw && pw.length < 8) {
      return say('Wi-Fi passwords are at least 8 characters — double-check yours.');
    }
    if (selectedSsidSecure === true && !pw) {
      return say('This network is locked 🔒 — it needs its Wi-Fi password.');
    }
    if (msg) msg.textContent = '';
    return true;
  }

  $('wifiNextBtn')?.addEventListener('click', () => {
    if (!wifiStepValid()) return;
    if (RETRY_MODE) {
      // Everything else is already on the device — go straight to saving.
      submitSetup();
      return;
    }
    showStep(2);
    const name = $('deviceName');
    if (name) { try { name.focus({ preventScroll: true }); } catch { /* ignore */ } }
  });

  // ── Step 2: name + optional login ─────────────────────────────────────
  function showAccountFields(on) {
    const fields = $('accountFields');
    if (fields) fields.style.display = on ? 'grid' : 'none';
  }
  $('createAccountCheck')?.addEventListener('change', (e) => {
    showAccountFields(e.target.checked);
    if (e.target.checked) {
      const u = $('acctUsername');
      if (u) { try { u.focus({ preventScroll: true }); } catch { /* ignore */ } }
    }
  });

  function accountValid() {
    const msg = $('accountMsg');
    const say = (t) => { if (msg) msg.textContent = t; return false; };
    const name = (($('deviceName') || {}).value || '').trim();
    if (!name) return say('Give your incubator a name.');
    if (!(($('createAccountCheck') || {}).checked)) { if (msg) msg.textContent = ''; return true; }
    const username = (($('acctUsername') || {}).value || '').trim();
    const email = (($('acctEmail') || {}).value || '').trim();
    const password = ($('acctPassword') || {}).value || '';
    if (username.length < 3) return say('Username must be at least 3 characters.');
    if (!email.includes('@') || email.length < 5) return say('Please enter a valid email address.');
    if (password.length < 8) return say('Password must be at least 8 characters.');
    if (msg) msg.textContent = '';
    return true;
  }

  $('finishSetupBtn')?.addEventListener('click', () => {
    if (!accountValid()) return;
    submitSetup();
  });

  // ── Submit ────────────────────────────────────────────────────────────
  let submitting = false;
  async function submitSetup() {
    if (submitting) return;
    const createAccount = !RETRY_MODE && !!(($('createAccountCheck') || {}).checked);
    const payload = {
      ssid: selectedSsid,
      wifi_password: ($('wifiPassword') || {}).value || '',
      // Recovery: keep the name the device already has — the wizard never
      // showed the naming step.
      device_name: RETRY_MODE
        ? (RETRY_DEVICE_NAME || (($('deviceName') || {}).value || '').trim() || 'My Incubator')
        : ((($('deviceName') || {}).value || '').trim() || 'My Incubator'),
      create_account: createAccount,
      username: createAccount ? (($('acctUsername') || {}).value || '').trim() : null,
      email: createAccount ? (($('acctEmail') || {}).value || '').trim() : null,
      password: createAccount ? (($('acctPassword') || {}).value || '') : null,
    };

    const errorTarget = RETRY_MODE ? $('wifiStepMsg') : $('accountMsg');
    const busyBtn = RETRY_MODE ? $('wifiNextBtn') : $('finishSetupBtn');
    submitting = true;
    if (busyBtn) busyBtn.disabled = true;
    if (errorTarget) errorTarget.textContent = 'Saving…';
    try {
      const res = await fetch('/onboarding/complete', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const data = await res.json().catch(() => null);
      if (res.ok && data && data.ok) {
        clearWizard();   // setup succeeded — drop the saved draft
        if (errorTarget) errorTarget.textContent = '';
        showStep(3);
        startConnecting(data);
        return;
      }
      const detail = data && (data.detail ?? data.error);
      const message = Array.isArray(detail)
        ? detail.map((d) => d.msg || '').filter(Boolean).join(' ') || 'Please check the details and try again.'
        : (detail || 'Something went wrong — please try again.');
      if (errorTarget) errorTarget.textContent = message;
    } catch {
      if (errorTarget) errorTarget.textContent = "Couldn't reach the incubator — are you still on its setup Wi-Fi?";
    } finally {
      submitting = false;
      if (busyBtn) busyBtn.disabled = false;
    }
  }

  // ── Connecting: stages + discovery ────────────────────────────────────
  function setStage(stage, state) {
    const li = document.querySelector(`#connectSteps li[data-stage="${stage}"]`);
    if (li) li.setAttribute('data-state', state);
  }
  function updateStages(elapsed) {
    setStage('saved', 'done');
    if (elapsed < SWITCH_DONE_MS) {
      setStage('switching', 'active'); setStage('rejoin', 'todo'); setStage('looking', 'todo');
    } else if (elapsed < REJOIN_DONE_MS) {
      setStage('switching', 'done'); setStage('rejoin', 'active'); setStage('looking', 'todo');
    } else {
      setStage('switching', 'done'); setStage('rejoin', 'done'); setStage('looking', 'active');
    }
  }

  function normaliseUrl(u) {
    if (typeof u !== 'string') return '';
    let s = u.trim();
    if (!s) return '';
    if (!/^https?:\/\//i.test(s)) s = `http://${s}`;
    return s.replace(/\/+$/, '');
  }
  function uniqueUrls(lists) {
    const out = [];
    lists.forEach((list) => (list || []).forEach((u) => {
      const n = normaliseUrl(u);
      if (n && !out.includes(n)) out.push(n);
    }));
    return out;
  }

  // One probe: resolves to the ping payload (object) or null. Never rejects.
  function probe(base) {
    return new Promise((resolve) => {
      let settled = false;
      let timer = null;
      const done = (v) => { if (!settled) { settled = true; clearTimeout(timer); resolve(v); } };
      const ctrl = typeof AbortController === 'function' ? new AbortController() : null;
      timer = setTimeout(() => { if (ctrl) ctrl.abort(); done(null); }, PROBE_TIMEOUT_MS);
      let req;
      try {
        req = fetch(base + PING_PATH, {
          mode: 'cors',
          cache: 'no-store',
          credentials: 'omit',
          signal: ctrl ? ctrl.signal : undefined,
        });
      } catch {
        done(null);
        return;
      }
      req
        .then((r) => (r && r.ok ? r.json() : null))
        .then((data) => done(data && typeof data === 'object' ? data : null))
        .catch(() => done(null));
    });
  }

  function startConnecting(data) {
    const title = $('finishTitle');
    const msg = $('finishMsg');
    if (title) title.textContent = 'Connecting your incubator…';
    if (msg) msg.textContent = 'Keep this page open — we’ll find your incubator on your Wi-Fi and open it for you. This usually takes under a minute.';
    const homeSsid = (data && data.wifi_ssid) || selectedSsid || 'your Wi-Fi';
    document.querySelectorAll('.home-ssid').forEach((el) => { el.textContent = `“${homeSsid}”`; });
    const finishHome = $('finishHomeSsid');
    if (finishHome) finishHome.textContent = `“${homeSsid}”`;

    const hostname = (data && data.hostname) || HOSTNAME;
    const candidates = uniqueUrls([
      templateCandidates(),
      (data && data.candidate_urls) || [],
      data && data.app_url ? [data.app_url] : [],
      hostname ? [`http://${hostname}.local:8000`, `http://${hostname}:8000`] : [],
    ]);
    const apBase = `http://${AP_IP}:8000`;
    const startedAt = Date.now();
    let finished = false;
    let timer = null;

    const stop = () => { finished = true; clearTimeout(timer); };

    async function round() {
      if (finished) return;
      const elapsed = Date.now() - startedAt;
      updateStages(elapsed);

      const results = await Promise.all([probe(apBase)].concat(candidates.map(probe)));
      if (finished) return;

      // The setup hotspot answering again, well after the switch began, means
      // the join failed and the device fell back to its own Wi-Fi.
      const apAnswer = results[0];
      const now = Date.now() - startedAt;
      if (apAnswer && now > AP_RETURN_GRACE_MS && apAnswer.hotspot_active !== false) {
        stop();
        showFailed();
        return;
      }

      for (let i = 0; i < candidates.length; i++) {
        const payload = results[i + 1];
        if (!payload || payload.ok === false) continue;
        // While the AP is still up, captive DNS may resolve ANY name to the
        // hotspot — only trust an answer that says the hotspot is down, or
        // (older firmware without the flag) one that arrives after the AP
        // window has passed.
        const onHomeNetwork = payload.hotspot_active === false
          || (payload.hotspot_active === undefined && now > AP_RETURN_GRACE_MS);
        if (onHomeNetwork) {
          stop();
          showFound(candidates[i], payload);
          return;
        }
      }

      if (now > DISCOVERY_TIMEOUT_MS) {
        stop();
        showManual(candidates, hostname);
        return;
      }
      timer = setTimeout(round, PROBE_EVERY_MS);
    }
    round();
  }

  function lanIps(payload) {
    const ips = (payload && Array.isArray(payload.ips)) ? payload.ips : [];
    return ips.filter((ip) => typeof ip === 'string' && ip && ip !== AP_IP && !ip.startsWith('127.') && !ip.startsWith('10.42.0.'));
  }

  function showFound(appUrl, payload) {
    ['saved', 'switching', 'rejoin', 'looking'].forEach((s) => setStage(s, 'done'));
    const title = $('finishTitle');
    const msg = $('finishMsg');
    const deviceName = payload && typeof payload.device_name === 'string' ? payload.device_name.trim() : '';
    if (title) title.textContent = 'Connected! 🎉';
    if (msg) {
      msg.textContent = deviceName
        ? `Your incubator “${deviceName}” is on your Wi-Fi.`
        : 'Your incubator is on your Wi-Fi.';
    }
    const found = $('finishFound');
    const urlEl = $('finishFoundUrl');
    const link = $('finishLink');
    const ipNote = $('finishIpNote');
    if (urlEl) urlEl.textContent = appUrl;
    const ips = lanIps(payload);
    if (ipNote && ips.length) {
      ipNote.textContent = `On Android, if that address won't open, use http://${ips[0]}:8000 instead.`;
      ipNote.hidden = false;
    }
    if (found) found.hidden = false;

    // "Open my incubator" is a real tap (user gesture), so try to hand off to
    // the system browser via window.open — the only way out of a captive-
    // portal sheet on most phones. Otherwise the href navigates in place.
    if (link) {
      link.href = appUrl;
      link.setAttribute('rel', 'noopener');
      link.addEventListener('click', (e) => {
        cancelCountdown();
        let w = null;
        try { w = window.open(appUrl, '_blank'); } catch { w = null; }
        if (w) e.preventDefault();
      });
    }

    // Auto-open countdown (cancellable). Safe now: the device has answered from
    // the home network, so the page we navigate to will not die under us.
    let remaining = AUTO_OPEN_SECONDS;
    const countdownEl = $('finishCountdown');
    const tick = () => {
      if (remaining <= 0) {
        cancelCountdown();
        try { window.location.assign(appUrl); } catch { /* ignore */ }
        return;
      }
      if (countdownEl) countdownEl.textContent = `Opening in ${remaining}…`;
      remaining -= 1;
    };
    let countdown = setInterval(tick, 1000);
    tick();
    function cancelCountdown() {
      if (countdown) { clearInterval(countdown); countdown = null; }
      if (countdownEl) countdownEl.textContent = '';
    }
    $('finishStayBtn')?.addEventListener('click', () => {
      cancelCountdown();
      const stay = $('finishStayBtn');
      if (stay) stay.hidden = true;
      if (countdownEl) countdownEl.textContent = 'Tap the button above whenever you’re ready.';
    });
  }

  function showFailed() {
    setStage('switching', 'fail'); setStage('rejoin', 'todo'); setStage('looking', 'todo');
    const title = $('finishTitle');
    const msg = $('finishMsg');
    if (title) title.textContent = 'That didn’t work';
    if (msg) msg.textContent = `The setup Wi-Fi “${AP_SSID}” is back on, which means the join failed.`;
    const failed = $('finishFailed');
    if (failed) failed.hidden = false;
  }

  function showManual(candidates, hostname) {
    setStage('looking', 'fail');
    const title = $('finishTitle');
    const msg = $('finishMsg');
    if (title) title.textContent = 'Almost there';
    if (msg) msg.textContent = 'We couldn’t confirm the connection from here, but your incubator may well be online already.';
    const manual = $('finishManual');
    const urlEl = $('finishUrl');
    const listEl = $('finishAddressList');
    const primary = candidates[0] || (hostname ? `http://${hostname}.local:8000` : 'http://incubator.local:8000');
    if (urlEl) urlEl.textContent = primary;
    if (listEl) {
      candidates.slice(1).forEach((u) => {
        const li = document.createElement('li');
        const a = document.createElement('a');
        a.href = u;
        a.textContent = u;
        a.setAttribute('rel', 'noopener');
        li.appendChild(a);
        listEl.appendChild(li);
      });
    }
    if (manual) manual.hidden = false;

    const selectUrl = () => {
      if (!urlEl) return;
      try {
        const range = document.createRange();
        range.selectNodeContents(urlEl);
        const sel = window.getSelection();
        if (sel) { sel.removeAllRanges(); sel.addRange(range); }
      } catch { /* ignore */ }
    };
    if (urlEl) urlEl.addEventListener('click', selectUrl);  // tap = select all
    const copyBtn = $('finishCopyBtn');
    const copyMsg = $('finishCopyMsg');
    if (copyBtn) {
      copyBtn.addEventListener('click', async () => {
        const ok = window.copyTextToClipboard ? await window.copyTextToClipboard(primary) : false;
        if (ok) {
          copyBtn.textContent = '✓ Copied';
          if (copyMsg) copyMsg.textContent = 'Copied! Paste it into Safari or Chrome once you’re back on your home Wi-Fi.';
        } else {
          selectUrl();
          if (copyMsg) copyMsg.textContent = 'The address above is selected — press and hold it, then choose Copy.';
        }
      });
    }
  }

  // ── Boot ──────────────────────────────────────────────────────────────
  restoreWizard();
  if (IN_SETUP) {
    // Make sure the AP session is registered (idempotent — never drops the
    // phone), then land straight on the Wi-Fi step.
    fetch('/onboarding/start', { method: 'POST' }).catch(() => {});
    showStep(1);
    if (RETRY_MODE && RETRY_SSID) {
      const manual = $('manualSsid');
      if (manual) manual.value = RETRY_SSID;
      selectedSsid = RETRY_SSID;
      selectedSsidSecure = null;
      showPasswordFor(RETRY_SSID, null);
      const nextBtn = $('wifiNextBtn');
      if (nextBtn) nextBtn.disabled = false;
    } else if (selectedSsid) {
      showPasswordFor(selectedSsid, null);
      const nextBtn = $('wifiNextBtn');
      if (nextBtn) nextBtn.disabled = false;
    }
    loadWifiNetworks();
  } else {
    showStep(0);
  }
})();
