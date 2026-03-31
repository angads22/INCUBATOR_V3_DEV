async function sendHardware(action, value = null) {
  const payload = { action };
  if (value !== null) payload.value = value;
  const msg = document.getElementById('hardware-msg');
  try {
    const res = await fetch('/hardware/send', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    msg.textContent = data.ok ? `Command accepted: ${JSON.stringify(data)}` : `Command error: ${JSON.stringify(data)}`;
  } catch (err) {
    msg.textContent = `Request failed: ${err}`;
  }
}
