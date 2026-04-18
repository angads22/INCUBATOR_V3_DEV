document.getElementById('settings-form')?.addEventListener('submit', async (e) => {
  e.preventDefault();
  const f = e.target;
  const payload = {
    target_temp_c: parseFloat(f.target_temp_c.value),
    target_humidity_pct: parseFloat(f.target_humidity_pct.value),
    heater_enabled: f.heater_enabled.checked,
    fan_enabled: f.fan_enabled.checked,
    turner_enabled: f.turner_enabled.checked,
    alarm_enabled: f.alarm_enabled.checked,
  };
  const msg = document.getElementById('settings-msg');

  function showMsg(text) {
    msg.textContent = text;
    msg.classList.remove('feedback-visible');
    // Trigger reflow so the animation replays on repeated saves
    void msg.offsetWidth;
    msg.classList.add('feedback-visible');
  }

  try {
    const res = await fetch('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    showMsg(data.ok ? 'Settings saved.' : (data.error || data.detail || 'Save failed.'));
  } catch (err) {
    showMsg(`Request failed: ${err}`);
  }
});
