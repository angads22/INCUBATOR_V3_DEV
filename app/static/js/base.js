const logoutBtn = document.getElementById('logoutBtn');
if (logoutBtn) {
  logoutBtn.addEventListener('click', async () => {
    try {
      await fetch('/api/logout', { method: 'POST' });
    } catch (_err) {
      // Temporary-safe fallback for builds without logout endpoint.
    }
    window.location.href = '/login';
  });
}
