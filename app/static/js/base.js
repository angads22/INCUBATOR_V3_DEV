// Profile bubble
const profileBtn = document.getElementById('profileBtn');
const profileMenu = document.getElementById('profileMenu');

profileBtn?.addEventListener('click', () => {
  const expanded = profileBtn.getAttribute('aria-expanded') === 'true';
  profileBtn.setAttribute('aria-expanded', String(!expanded));
  profileMenu.hidden = expanded;
});

document.addEventListener('click', (e) => {
  if (
    profileMenu &&
    !profileMenu.hidden &&
    !profileBtn?.contains(e.target) &&
    !profileMenu.contains(e.target)
  ) {
    profileMenu.hidden = true;
    profileBtn?.setAttribute('aria-expanded', 'false');
  }
});

// Logout
const logoutBtn = document.getElementById('logoutBtn');
if (logoutBtn) {
  logoutBtn.addEventListener('click', async () => {
    await fetch('/api/logout', { method: 'POST' });
    window.location.href = '/login';
  });
}
