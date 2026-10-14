// Network settings panel: show connection state, scan, and switch Wi-Fi.
// Switching SSIDs can drop the browser's connection mid-request (single
// radio), so a timeout is treated as "probably switched", not as an error.
// SSIDs come from the air: rendered with textContent only, never as HTML.

(() => {
  const statusEl = document.getElementById('network-status');
  const scanBtn = document.getElementById('wifi-scan-btn');
  const listEl = document.getElementById('network-wifi-list');
  const form = document.getElementById('network-connect-form');
  const ssidLabel = document.getElementById('network-selected-ssid');
  const pwInput = document.getElementById('network-wifi-password');
  const connectBtn = document.getElementById('wifi-connect-btn');
  const msg = document.getElementById('network-msg');
  if (!statusEl || !scanBtn || !listEl) return;

  let selectedSsid = '';
  const say = (text) => { if (msg) msg.textContent = text; };

  async function loadStatus() {
    const res = await apiGet('/api/network/status');
    if (!res.ok) {
      statusEl.textContent = res.message;
      return;
    }
    const d = res.data || {};
    if (d.hotspot_active) {
      statusEl.textContent = 'Setup Wi-Fi is active.';
    } else if (d.connected_ssid) {
      statusEl.textContent = `Connected to “${d.connected_ssid}”.`;
    } else if (d.configured_ssid) {
      statusEl.textContent = `Configured for “${d.configured_ssid}” but not currently connected.`;
    } else {
      statusEl.textContent = 'No Wi-Fi configured yet.';
    }
  }

  function signalBars(strength) {
    const pct = Math.max(0, Math.min(100, Number(strength) || 0));
    const lit = pct >= 75 ? 4 : pct >= 50 ? 3 : pct >= 25 ? 2 : 1;
    const bars = document.createElement('span');
    bars.className = 'wifi-bars';
    bars.title = `${pct}% signal`;
    for (let i = 1; i <= 4; i++) {
      const bar = document.createElement('i');
      if (i <= lit) bar.className = 'on';
      bars.appendChild(bar);
    }
    return bars;
  }

  function networkRow(net) {
    const item = document.createElement('button');
    item.className = 'wifi-item';
    item.type = 'button';
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
      listEl.querySelectorAll('.wifi-item').forEach((el) => el.classList.remove('selected'));
      item.classList.add('selected');
      selectedSsid = net.ssid;
      if (ssidLabel) ssidLabel.textContent = net.ssid;
      if (form) form.hidden = false;
      if (pwInput) { try { pwInput.focus({ preventScroll: true }); } catch { /* ignore */ } }
    });
    return item;
  }

  scanBtn.addEventListener('click', async () => {
    scanBtn.disabled = true;
    say('Scanning for networks…');
    while (listEl.firstChild) listEl.removeChild(listEl.firstChild);
    if (form) form.hidden = true;
    selectedSsid = '';
    const res = await apiGet('/onboarding/wifi-scan');
    scanBtn.disabled = false;
    if (!res.ok) {
      say(res.message);
      return;
    }
    const seen = new Set();
    const networks = ((res.data && res.data.networks) || [])
      .filter((n) => n && typeof n.ssid === 'string' && n.ssid.trim() && !seen.has(n.ssid) && seen.add(n.ssid))
      .sort((a, b) => (Number(b.strength) || 0) - (Number(a.strength) || 0));
    if (networks.length === 0) {
      say('No networks found. Move the device closer to your router and scan again.');
      return;
    }
    say('Select a network to connect.');
    networks.forEach((net) => listEl.appendChild(networkRow(net)));
  });

  connectBtn?.addEventListener('click', async () => {
    if (!selectedSsid) return;
    if (!window.confirm(`Switch Wi-Fi to “${selectedSsid}”? The incubator may briefly drop off the current network.`)) return;
    connectBtn.disabled = true;
    say(`Connecting to “${selectedSsid}”…`);
    const controller = typeof AbortController === 'function' ? new AbortController() : null;
    const timeout = setTimeout(() => { if (controller) controller.abort(); }, 30000);
    try {
      const res = await fetch('/api/network/connect', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ssid: selectedSsid, password: (pwInput && pwInput.value) || '' }),
        signal: controller ? controller.signal : undefined,
      });
      const data = await res.json().catch(() => null);
      if (res.ok && data && data.ok) {
        say(`Connected to “${selectedSsid}”.`);
        loadStatus();
      } else {
        say((data && (data.detail || data.error)) || 'Connection failed. Check the password and try again.');
      }
    } catch {
      say('No response — if the incubator switched networks, join the new Wi-Fi and reload this page.');
    } finally {
      clearTimeout(timeout);
      connectBtn.disabled = false;
    }
  });

  loadStatus();
})();
