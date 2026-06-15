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

// ── Shared API helpers ────────────────────────────────────────────────
// All page scripts use these instead of raw fetch so the user always sees
// a human-readable message instead of raw JSON or a JS error object.
async function apiRequest(url, options = {}) {
  let res;
  try {
    res = await fetch(url, options);
  } catch {
    return { ok: false, status: 0, data: null, message: 'Cannot reach the incubator — check your connection.' };
  }
  let data = null;
  try { data = await res.json(); } catch { /* non-JSON response */ }
  if (res.status === 401) {
    return { ok: false, status: 401, data, message: 'Please log in to do that.' };
  }
  if (!res.ok || (data && data.ok === false)) {
    const detail = data && (data.detail ?? data.error);
    const message = Array.isArray(detail)
      ? detail.map((d) => d.msg || '').filter(Boolean).join(' ') || 'Invalid input.'
      : detail || `Request failed (${res.status}).`;
    return { ok: false, status: res.status, data, message };
  }
  return { ok: true, status: res.status, data, message: '' };
}

window.apiGet = (url) => apiRequest(url);
window.apiPost = (url, body) => apiRequest(url, {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body ?? {}),
});

// ── Highlight the active nav link ─────────────────────────────────────
const navPath = window.location.pathname;
document.querySelectorAll('.nav-links a').forEach((a) => {
  const href = a.getAttribute('href');
  if (href === navPath || (href !== '/' && navPath.startsWith(href))) {
    a.classList.add('active');
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
