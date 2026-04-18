// ── Profile dropdown with animated open/close ────────────────────────
const profileBtn  = document.getElementById('profileBtn');
const profileMenu = document.getElementById('profileMenu');

function openMenu() {
  profileMenu.hidden = false;
  // Double rAF: let browser paint the element before the transition fires
  requestAnimationFrame(() => requestAnimationFrame(() => {
    profileMenu.classList.add('open');
  }));
  profileBtn.setAttribute('aria-expanded', 'true');
}

function closeMenu() {
  profileMenu.classList.remove('open');
  profileMenu.addEventListener('transitionend', () => {
    profileMenu.hidden = true;
  }, { once: true });
  profileBtn.setAttribute('aria-expanded', 'false');
}

profileBtn?.addEventListener('click', () => {
  profileMenu.classList.contains('open') ? closeMenu() : openMenu();
});

document.addEventListener('click', (e) => {
  if (
    profileMenu &&
    profileMenu.classList.contains('open') &&
    !profileBtn?.contains(e.target) &&
    !profileMenu.contains(e.target)
  ) {
    closeMenu();
  }
});

// ── Logout ────────────────────────────────────────────────────────────
const logoutBtn = document.getElementById('logoutBtn');
if (logoutBtn) {
  logoutBtn.addEventListener('click', async () => {
    await fetch('/api/logout', { method: 'POST' });
    window.location.href = '/login';
  });
}
