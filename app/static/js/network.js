// Network settings panel: show connection state, scan, and switch Wi-Fi.
// Switching SSIDs can drop the browser's connection mid-request (single
// radio), so a timeout is treated as "probably switched", not as an error.

(() => {
  const statusEl = document.getElementById('network-status');
  const scanBtn = document.getElementById('wifi-scan-btn');
  const listEl = document.getElementById('network-wifi-list');
  const form = document.getElementById('network-connect-form');
  const ssidLabel = document.getElementById('network-selected-ssid');
  const pwInput = document.getElementById('network-wifi-password');
  const connectBtn = document.getElementById('wifi-connect-btn');
  const msg = document.getElementById('network-msg');
  if (!statusEl || !scanBtn) return;

  let selectedSsid = '';

  async function loadStatus() {
    const res = await apiGet('/api/network/status');
    if (!res.ok) {
      statusEl.textContent = res.message;
      return;
    }
    const d = res.data;
    if (d.hotspot_active) {
      statusEl.textContent = 'Setup hotspot is active.';
    } else if (d.connected_ssid) {
      statusEl.textContent = `Connected to “${d.connected_ssid}”.`;
    } else if (d.configured_ssid) {
      statusEl.textContent = `Configured for “${d.configured_ssid}” but not currently connected.`;
    } else {
      statusEl.textContent = 'No Wi-Fi configured yet.';
    }
  }

  scanBtn.addEventListener('click', async () => {
    scanBtn.disabled = true;
    msg.textContent = 'Scanning for networks…';
    listEl.innerHTML = '';
    form.hidden = true;
    selectedSsid = '';
    const res = await apiGet('/onboarding/wifi-scan');
    scanBtn.disabled = false;
    const networks = (res.data && res.data.networks) || [];
    if (!res.ok) {
      msg.textContent = res.message;
      return;
    }
    if (networks.length === 0) {
      msg.textContent = 'No networks found. Move the device closer to your router and scan again.';
      return;
    }
    msg.textContent = 'Select a network to connect.';
    networks.forEach((net) => {
      const item = document.createElement('button');
      item.className = 'wifi-item';
      item.type = 'button';
      item.innerHTML =
        `<span class="wifi-ssid">${net.ssid}</span>` +
        `<span class="wifi-strength">${net.strength}%${net.secure ? ' 🔒' : ''}</span>`;
      item.addEventListener('click', () => {
        document.querySelectorAll('.wifi-item').forEach((el) => el.classList.remove('selected'));
        item.classList.add('selected');
        selectedSsid = net.ssid;
        ssidLabel.textContent = net.ssid;
        form.hidden = false;
      });
      listEl.appendChild(item);
    });
  });

  connectBtn?.addEventListener('click', async () => {
    if (!selectedSsid) return;
    if (!window.confirm(`Switch Wi-Fi to “${selectedSsid}”? The incubator may briefly drop off the current network.`)) return;
    connectBtn.disabled = true;
    msg.textContent = `Connecting to “${selectedSsid}”…`;
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 30000);
    try {
      const res = await fetch('/api/network/connect', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ssid: selectedSsid, password: pwInput.value || '' }),
        signal: controller.signal,
      });
      const data = await res.json().catch(() => null);
      if (res.ok && data && data.ok) {
        msg.textContent = `Connected to “${selectedSsid}”.`;
        loadStatus();
      } else {
        msg.textContent = (data && (data.detail || data.error)) || 'Connection failed. Check the password and try again.';
      }
    } catch {
      msg.textContent = 'No response — if the incubator switched networks, join the new Wi-Fi and reload this page.';
    } finally {
      clearTimeout(timeout);
      connectBtn.disabled = false;
    }
  });

  loadStatus();
})();
