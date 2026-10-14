// Settings page: save targets/toggles, and the Device card (info chips,
// restart setup mode, factory reset). Uses the apiGet/apiPost helpers from
// base.js; every element lookup is guarded.

document.getElementById('settings-form')?.addEventListener('submit', async (e) => {
  e.preventDefault();
  const f = e.target;
  const btn = f.querySelector('button[type="submit"]');
  const payload = {
    target_temp_c: parseFloat(f.target_temp_c.value),
    target_humidity_pct: parseFloat(f.target_humidity_pct.value),
    alert_temp_tolerance_c: parseFloat(f.alert_temp_tolerance_c.value),
    alert_humidity_tolerance_pct: parseFloat(f.alert_humidity_tolerance_pct.value),
    incubation_day: parseInt(f.incubation_day.value, 10) || 0,
    heater_enabled: f.heater_enabled.checked,
    fan_enabled: f.fan_enabled.checked,
    turner_enabled: f.turner_enabled.checked,
    alarm_enabled: f.alarm_enabled.checked,
  };
  const msg = document.getElementById('settings-msg');

  function showMsg(text) {
    if (!msg) return;
    msg.textContent = text;
    msg.classList.remove('feedback-visible');
    // Trigger reflow so the animation replays on repeated saves
    void msg.offsetWidth;
    msg.classList.add('feedback-visible');
  }

  if (btn) btn.disabled = true;
  try {
    const res = await apiPost('/api/settings', payload);
    showMsg(res.ok ? 'Settings saved.' : res.message);
  } finally {
    if (btn) btn.disabled = false;
  }
});

// ── Device card ───────────────────────────────────────────────────────
(() => {
  const $ = (id) => document.getElementById(id);
  const chips = $('deviceInfoChips');
  if (!chips) return;   // not on the settings page

  const AP_IP_PREFIX = '10.42.0.';

  function chip(label, tone) {
    const el = document.createElement('span');
    el.className = `status-chip status-${tone || 'info'}`;
    const text = document.createElement('span');
    text.textContent = label;
    el.appendChild(text);
    return el;
  }
  function clear(el) { while (el && el.firstChild) el.removeChild(el.firstChild); }

  function renderInfo(info) {
    clear(chips);
    const board = info.board && typeof info.board === 'object' ? info.board : {};
    const model = (board.model || info.board_model || '').toString().trim();
    if (model) chips.appendChild(chip(`Board: ${model}`));
    if (info.gpio_backend) chips.appendChild(chip(`GPIO: ${info.gpio_backend}`, info.gpio_backend === 'mock' ? 'warn' : 'info'));
    if (info.sensor_backend) chips.appendChild(chip(`Sensor: ${info.sensor_backend}`, info.sensor_backend === 'mock' ? 'warn' : 'info'));
    if (info.camera_backend) chips.appendChild(chip(`Camera: ${info.camera_backend}`));
    if (info.wifi_iface) chips.appendChild(chip(`Wi-Fi: ${info.wifi_iface}`));
    if (info.hostname) chips.appendChild(chip(`Name: ${info.hostname}`));
    if (info.version) chips.appendChild(chip(`Version ${info.version}`));
    if (info.control_daemon_enabled === true) chips.appendChild(chip('Control loop: separate daemon', 'ok'));
    if (!chips.firstChild) chips.appendChild(chip('No device details reported'));

    const list = $('deviceAddressList');
    if (!list) return;
    clear(list);
    const addresses = [];
    if (info.hostname) addresses.push(`http://${info.hostname}.local:8000`);
    (Array.isArray(info.ips) ? info.ips : []).forEach((ip) => {
      if (typeof ip === 'string' && ip && !ip.startsWith('127.') && !ip.startsWith(AP_IP_PREFIX)) {
        addresses.push(`http://${ip}:8000`);
      }
    });
    if (!addresses.length) { list.hidden = true; return; }
    const head = document.createElement('li');
    head.appendChild(document.createTextNode('Reach this incubator at: '));
    list.appendChild(head);
    addresses.forEach((url) => {
      const li = document.createElement('li');
      const code = document.createElement('code');
      code.textContent = url;
      code.style.userSelect = 'all';
      code.style.webkitUserSelect = 'all';
      li.appendChild(code);
      const copy = document.createElement('button');
      copy.type = 'button';
      copy.className = 'btn btn-ghost btn-small';
      copy.style.marginLeft = '8px';
      copy.textContent = 'Copy';
      copy.addEventListener('click', async () => {
        const ok = window.copyTextToClipboard ? await window.copyTextToClipboard(url) : false;
        copy.textContent = ok ? 'Copied' : 'Select and copy';
        setTimeout(() => { copy.textContent = 'Copy'; }, 2000);
      });
      li.appendChild(copy);
      list.appendChild(li);
    });
    list.hidden = false;
  }

  async function loadInfo() {
    const msg = $('deviceInfoMsg');
    const res = await apiGet('/api/device/info');
    if (!res.ok) {
      clear(chips);
      chips.appendChild(chip(res.status === 404 ? 'Device details not available on this version' : 'Device details unavailable', 'warn'));
      if (msg) {
        msg.textContent = res.status === 404
          ? 'This software version does not report device details yet — it will after the next automatic update.'
          : res.message;
      }
      return;
    }
    if (msg) msg.textContent = '';
    renderInfo(res.data || {});
  }

  // Restart setup mode: the incubator raises its own Wi-Fi so a phone can
  // change the network. This page will stop responding when the radio flips.
  $('restartSetupBtn')?.addEventListener('click', async () => {
    const btn = $('restartSetupBtn');
    const msg = $('deviceActionMsg');
    if (!window.confirm('Start setup mode? The incubator switches to its own Wi-Fi network and this page will stop responding until you join it.')) return;
    if (btn) btn.disabled = true;
    if (msg) msg.textContent = 'Starting setup mode…';
    const res = await apiPost('/onboarding/start');
    if (btn) btn.disabled = false;
    if (!msg) return;
    if (res.ok || res.status === 0) {
      // status 0 = the connection dropped, which is what the AP switch does.
      const ssid = (res.data && res.data.ssid) || 'Incubator-XXXX';
      msg.textContent = `Setup mode is starting. On your phone, join the Wi-Fi network “${ssid}” — the setup page opens by itself.`;
    } else {
      msg.textContent = res.message;
    }
  });

  // Factory reset: confirm by typing RESET, then POST. The device wipes its
  // identity and comes back in setup mode; the response is sent before the
  // wipe so we can still tell the user what happens next.
  const resetBox = $('factoryResetConfirm');
  const resetInput = $('factoryResetInput');
  const resetGo = $('factoryResetGo');
  $('factoryResetBtn')?.addEventListener('click', () => {
    if (!resetBox) return;
    resetBox.hidden = !resetBox.hidden;
    if (!resetBox.hidden && resetInput) { try { resetInput.focus({ preventScroll: true }); } catch { /* ignore */ } }
  });
  $('factoryResetCancel')?.addEventListener('click', () => {
    if (resetBox) resetBox.hidden = true;
    if (resetInput) resetInput.value = '';
    if (resetGo) resetGo.disabled = true;
  });
  resetInput?.addEventListener('input', () => {
    if (resetGo) resetGo.disabled = resetInput.value.trim().toUpperCase() !== 'RESET';
  });
  resetGo?.addEventListener('click', async () => {
    const msg = $('deviceActionMsg');
    if (!resetInput || resetInput.value.trim().toUpperCase() !== 'RESET') return;
    if (!window.confirm('Really erase this incubator’s name, logins, Wi-Fi and settings? This cannot be undone.')) return;
    resetGo.disabled = true;
    if (msg) msg.textContent = 'Resetting…';
    const res = await apiPost('/api/device/factory-reset', { confirm: 'RESET' });
    if (!msg) return;
    if (res.ok) {
      msg.textContent = 'Factory reset started. Name, logins, Wi-Fi and settings are being wiped and the incubator restarts in setup mode — on your phone, join its “Incubator-XXXX” Wi-Fi to set it up again.';
      if (resetBox) resetBox.hidden = true;
      if (resetInput) resetInput.value = '';
      return;
    }
    if (res.status === 404) {
      msg.textContent = 'Factory reset isn’t available on this software version. Hold the setup button on the device for 15 seconds instead.';
    } else {
      msg.textContent = res.message;
    }
    resetGo.disabled = false;
  });

  loadInfo();
})();
