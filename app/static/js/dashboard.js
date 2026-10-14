// Home page: live sensor polling with a freshness indicator, the alert
// banner, the first-run welcome card and the "reach your incubator here"
// notice. Every element lookup is guarded.

// ── Welcome card + address notice (dismissible, remembered per browser) ──
(() => {
  const store = {
    get(k) { try { return localStorage.getItem(k); } catch { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch { /* private mode */ } },
  };

  const welcome = document.getElementById('welcomeCard');
  if (welcome) {
    const KEY = 'lifeloop-welcome-dismissed';
    if (store.get(KEY) !== '1') welcome.hidden = false;
    document.getElementById('welcomeDismissBtn')?.addEventListener('click', () => {
      store.set(KEY, '1');
      welcome.hidden = true;
    });
  }

  const notice = document.getElementById('accessNotice');
  if (notice) {
    const KEY = 'incubatorAccessNoticeDismissed';
    if (store.get(KEY) !== '1') notice.hidden = false;
    const urlEl = document.getElementById('accessUrl');
    const url = urlEl ? urlEl.textContent.trim() : '';
    const copy = document.getElementById('accessCopyBtn');
    copy?.addEventListener('click', async () => {
      const ok = window.copyTextToClipboard ? await window.copyTextToClipboard(url) : false;
      copy.textContent = ok ? 'Copied!' : 'Select the address and copy it';
      setTimeout(() => { copy.textContent = 'Copy'; }, 2500);
    });
    document.getElementById('accessDismissBtn')?.addEventListener('click', () => {
      store.set(KEY, '1');
      notice.hidden = true;
    });
  }
})();

// ── Live readings ─────────────────────────────────────────────────────
(() => {
  const POLL_MS = 10000;
  const tempEl = document.getElementById('liveTemp');
  const humEl = document.getElementById('liveHum');
  const updatedEl = document.getElementById('updatedAgo');
  const offlineRow = document.getElementById('sensorOfflineRow');
  const offlineChip = document.getElementById('sensorOfflineChip');
  const banner = document.getElementById('alertBanner');
  const bannerText = document.getElementById('alertBannerText');
  const silenceBtn = document.getElementById('alertSilenceBtn');
  if (!tempEl || !humEl) return;

  let lastFetched = null;
  let pollTimer = null;

  function fmt(value, suffix) {
    return value == null ? '—' : `${Number(value).toFixed(1)}${suffix}`;
  }

  function render(data) {
    if (data.temperature_c == null) {
      tempEl.removeAttribute('data-temp-c');
      tempEl.textContent = '—';
    } else {
      tempEl.dataset.tempC = data.temperature_c;
      // Respect the °C/°F header toggle when one is present.
      tempEl.textContent = window.formatTemp ? window.formatTemp(data.temperature_c) : fmt(data.temperature_c, '°C');
    }
    humEl.textContent = fmt(data.humidity_pct, '%');
    if (offlineRow) offlineRow.hidden = !!data.online;
    if (!data.online && offlineChip) {
      const last = data.read_at ? ` — last reading ${new Date(data.read_at).toLocaleString()}` : '';
      offlineChip.textContent = `Sensor offline${last}`;
    }
    const alerts = data.alerts || { active: [], alarm_on: false };
    const active = Array.isArray(alerts.active) ? alerts.active : [];
    if (banner) banner.hidden = active.length === 0;
    if (bannerText) bannerText.textContent = active.map((a) => a.message).join(' · ');
    if (silenceBtn) silenceBtn.hidden = !alerts.alarm_on;
  }

  async function poll() {
    const res = await apiGet('/api/sensors/latest');
    if (res.ok && res.data) {
      lastFetched = Date.now();
      render(res.data);
    }
  }

  function tick() {
    if (lastFetched == null || !updatedEl) return;
    const secs = Math.round((Date.now() - lastFetched) / 1000);
    updatedEl.textContent = secs > (3 * POLL_MS) / 1000
      ? `No update for ${secs}s — connection to the incubator may be lost.`
      : `Updated ${secs}s ago.`;
  }

  silenceBtn?.addEventListener('click', async () => {
    silenceBtn.disabled = true;
    const res = await apiPost('/api/alerts/silence');
    if (res.ok) silenceBtn.hidden = true;
    silenceBtn.disabled = false;
  });

  // Pause polling while the tab is hidden — saves the board's CPU and the
  // phone's battery; refresh immediately when the page comes back.
  function startPolling() {
    if (pollTimer) return;
    poll();
    pollTimer = setInterval(poll, POLL_MS);
  }
  function stopPolling() {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  }
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) stopPolling(); else startPolling();
  });

  startPolling();
  setInterval(tick, 1000);
})();
