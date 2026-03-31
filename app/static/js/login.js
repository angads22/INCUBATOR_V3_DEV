document.getElementById('login-form')?.addEventListener('submit', async (e) => {
  e.preventDefault();
  const f = e.target;
  const msg = document.getElementById('login-msg');

  try {
    const res = await fetch('/api/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username: f.username.value, password: f.password.value }),
    });

    if (res.ok) {
      window.location.href = '/';
      return;
    }

    if (res.status === 404) {
      msg.textContent = 'Login API is not enabled in this build yet. Use setup flow first.';
      return;
    }

    const data = await res.json().catch(() => ({}));
    msg.textContent = data.detail || data.error || 'Login failed';
  } catch (err) {
    msg.textContent = `Request failed: ${err}`;
  }
});
