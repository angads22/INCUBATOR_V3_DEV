// Live dashboard updates: polls the cached sensor state, keeps a freshness
// indicator ticking, and surfaces active alerts in the banner.

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

  function fmt(value, suffix) {
    return value == null ? '—' : `${value.toFixed(1)}${suffix}`;
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
    offlineRow.hidden = !!data.online;
    if (!data.online) {
      const last = data.read_at ? ` — last reading ${new Date(data.read_at).toLocaleString()}` : '';
      offlineChip.textContent = `Sensor offline${last}`;
    }
    const alerts = data.alerts || { active: [], alarm_on: false };
    banner.hidden = alerts.active.length === 0;
    bannerText.textContent = alerts.active.map((a) => a.message).join(' · ');
    silenceBtn.hidden = !alerts.alarm_on;
  }

  async function poll() {
    const res = await apiGet('/api/sensors/latest');
    if (res.ok && res.data) {
      lastFetched = Date.now();
      render(res.data);
    }
  }

  function tick() {
    if (lastFetched == null) return;
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

  poll();
  setInterval(poll, POLL_MS);
  setInterval(tick, 1000);
})();
