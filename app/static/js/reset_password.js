document.getElementById('reset-form')?.addEventListener('submit', async (e) => {
  e.preventDefault();
  const f = e.target;
  const btn = f.querySelector('button[type="submit"]');
  const msg = document.getElementById('reset-msg');

  if (f.new_password.value !== f.confirm_password.value) {
    msg.textContent = 'Passwords do not match.';
    return;
  }

  btn.disabled = true;
  try {
    const res = await apiPost('/api/reset-password', {
      identifier: f.identifier.value,
      new_password: f.new_password.value,
    });
    msg.textContent = res.ok
      ? (res.data && res.data.message) || 'Password reset. You can now log in.'
      : res.message;
  } finally {
    btn.disabled = false;
  }
});
