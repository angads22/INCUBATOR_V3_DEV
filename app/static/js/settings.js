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
    msg.textContent = text;
    msg.classList.remove('feedback-visible');
    // Trigger reflow so the animation replays on repeated saves
    void msg.offsetWidth;
    msg.classList.add('feedback-visible');
  }

  btn.disabled = true;
  try {
    const res = await apiPost('/api/settings', payload);
    showMsg(res.ok ? 'Settings saved.' : res.message);
  } finally {
    btn.disabled = false;
  }
});
