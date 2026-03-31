document.getElementById('login-form')?.addEventListener('submit', async (e) => {
  e.preventDefault();
  const f = e.target;
  const msg = document.getElementById('login-msg');
  const res = await fetch('/api/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username: f.username.value, password: f.password.value }),
  });
  if (res.ok) {
    window.location.href = '/';
    return;
  }
  const data = await res.json();
  msg.textContent = data.detail || data.error || 'Login failed';
});
