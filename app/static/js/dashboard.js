async function sendHardware(action, value = null) {
  const payload = { action };
  if (value !== null) payload.value = value;

  const resultEl = document.getElementById('actionResult');
  try {
    const res = await fetch('/hardware/send', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    resultEl.textContent = data.ok ? `Success: ${JSON.stringify(data)}` : `Error: ${JSON.stringify(data)}`;
  } catch (err) {
    resultEl.textContent = `Request failed: ${err}`;
  }
}
