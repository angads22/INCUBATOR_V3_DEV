// Manual hardware panel: buttons carry data-action / data-value / data-confirm
// (see partials/action_card.html). Feedback is plain language, never raw JSON.

const ACTION_FEEDBACK = {
  open_lock: 'Lock opened.',
  close_lock: 'Lock closed.',
  open_door: 'Door opened.',
  close_door: 'Door closed.',
  move_motor: 'Egg turner moved.',
  capture_image: 'Image captured.',
  alarm_test: 'Alarm test chirp sent.',
  alarm_off: 'Alarm switched off and silenced.',
  alarm_on: 'Alarm switched on.',
};

function successMessage(action, value, data) {
  if (action === 'set_candle') {
    return value === 'true' ? 'Candling light on.' : 'Candling light off.';
  }
  if (action === 'read_environment' && data) {
    return `Temperature ${data.temperature_c}°C, humidity ${data.humidity_pct}%.`;
  }
  return ACTION_FEEDBACK[action] || 'Command accepted.';
}

document.addEventListener('click', async (e) => {
  const btn = e.target.closest('button[data-action]');
  if (!btn) return;
  const action = btn.dataset.action;
  const value = btn.dataset.value;
  const msg = document.getElementById('hardware-msg');

  if (btn.dataset.confirm && !window.confirm(btn.dataset.confirm)) return;

  const payload = { action };
  if (value !== undefined) payload.value = value;

  btn.disabled = true;
  msg.textContent = 'Working…';
  try {
    const res = await apiPost('/hardware/send', payload);
    msg.textContent = res.ok ? successMessage(action, value, res.data) : res.message;
  } finally {
    btn.disabled = false;
  }
});
