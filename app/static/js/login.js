document.getElementById('login-form')?.addEventListener('submit', async (e) => {
  e.preventDefault();
  const f = e.target;
  const btn = f.querySelector('button[type="submit"]');
  const msg = document.getElementById('login-msg');

  btn.disabled = true;
  msg.textContent = 'Signing in…';
  try {
    const res = await apiPost('/api/login', {
      username: f.username.value,
      password: f.password.value,
    });
    if (res.ok) {
      window.location.href = '/';
      return;
    }
    msg.textContent = res.status === 401 ? 'Invalid username or password.' : res.message;
  } finally {
    btn.disabled = false;
  }
});
