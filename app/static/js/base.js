// Shared page chrome: profile menu, API helpers, nav state, theme + unit
// toggles, update popup. Every element lookup is guarded so this file never
// throws on a page that lacks a given control (login, onboarding, ...).

// ── Profile dropdown with animated open/close ────────────────────────
const profileBtn  = document.getElementById('profileBtn');
const profileMenu = document.getElementById('profileMenu');

function openMenu() {
  if (!profileMenu) return;
  profileMenu.hidden = false;
  // Double rAF: let browser paint the element before the transition fires
  requestAnimationFrame(() => requestAnimationFrame(() => {
    profileMenu.classList.add('open');
  }));
  if (profileBtn) profileBtn.setAttribute('aria-expanded', 'true');
}

function closeMenu() {
  if (!profileMenu) return;
  profileMenu.classList.remove('open');
  profileMenu.addEventListener('transitionend', () => {
    if (!profileMenu.classList.contains('open')) profileMenu.hidden = true;
  }, { once: true });
  // Reduced-motion users get no transitionend; hide on a timer as a fallback.
  setTimeout(() => { if (!profileMenu.classList.contains('open')) profileMenu.hidden = true; }, 260);
  if (profileBtn) profileBtn.setAttribute('aria-expanded', 'false');
}

if (profileBtn && profileMenu) {
  profileBtn.addEventListener('click', () => {
    profileMenu.classList.contains('open') ? closeMenu() : openMenu();
  });
  document.addEventListener('click', (e) => {
    if (
      profileMenu.classList.contains('open') &&
      !profileBtn.contains(e.target) &&
      !profileMenu.contains(e.target)
    ) {
      closeMenu();
    }
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && profileMenu.classList.contains('open')) closeMenu();
  });
}

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
    if (res.status === 404 && (!detail || detail === 'Not Found')) {
      // Route missing entirely (older software) rather than a lookup miss.
      return { ok: false, status: 404, data, message: 'This feature is not available on this software version.' };
    }
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

// Copy helper shared by pages that show the device address. The app is served
// over plain http, where navigator.clipboard is unavailable, so fall back to
// the legacy execCommand path; resolves true/false, never throws.
window.copyTextToClipboard = async (text) => {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch { /* fall through to the legacy path */ }
  try {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.setAttribute('readonly', '');
    ta.style.position = 'fixed';
    ta.style.top = '0';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.focus();
    ta.select();
    ta.setSelectionRange(0, text.length);  // iOS Safari needs the range
    let ok = false;
    try { ok = document.execCommand('copy'); } catch { ok = false; }
    ta.remove();
    return !!ok;
  } catch {
    return false;
  }
};

// ── Highlight the active nav link (top nav + mobile tab bar) ──────────
(() => {
  const navPath = window.location.pathname;
  document.querySelectorAll('.nav-links a, .tabbar a').forEach((a) => {
    const href = a.getAttribute('href');
    if (!href) return;
    if (href === navPath || (href !== '/' && navPath.startsWith(href))) {
      a.classList.add('active');
      a.setAttribute('aria-current', 'page');
    }
  });
})();

// ── Logout ────────────────────────────────────────────────────────────
const logoutBtn = document.getElementById('logoutBtn');
if (logoutBtn) {
  logoutBtn.addEventListener('click', async () => {
    try { await fetch('/api/logout', { method: 'POST' }); } catch { /* go to login anyway */ }
    window.location.href = '/login';
  });
}

// ── Life Loop: accent theme + temperature unit ────────────────────────
// Persisted in localStorage so the operator's choice survives reloads. The
// theme attribute is applied to <html> (see tokens.css [data-theme] rules).
(() => {
  const THEME_KEY = 'lifeloop-theme';
  const UNIT_KEY = 'lifeloop-unit';
  const THEMES = ['clay', 'sage', 'harvest'];
  const root = document.documentElement;
  const store = {
    get(k) { try { return localStorage.getItem(k); } catch { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch { /* private mode */ } },
  };

  // -- Accent theme --
  function applyTheme(name) {
    const theme = THEMES.includes(name) ? name : 'clay';
    root.setAttribute('data-theme', theme);
    document.querySelectorAll('.theme-dot').forEach((dot) => {
      dot.classList.toggle('active', dot.dataset.themeChoice === theme);
    });
  }
  applyTheme(store.get(THEME_KEY) || 'clay');
  document.querySelectorAll('.theme-dot').forEach((dot) => {
    dot.addEventListener('click', () => {
      const choice = dot.dataset.themeChoice;
      store.set(THEME_KEY, choice);
      applyTheme(choice);
    });
  });

  // -- Temperature unit (°C / °F) --
  const getUnit = () => (store.get(UNIT_KEY) === 'F' ? 'F' : 'C');
  const cToF = (c) => (c * 9) / 5 + 32;

  // Shared formatter so live-polling scripts render in the chosen unit too.
  window.formatTemp = (c) => {
    if (c == null || Number.isNaN(Number(c))) return '—';
    return getUnit() === 'F'
      ? `${cToF(Number(c)).toFixed(1)}°F`
      : `${Number(c).toFixed(1)}°C`;
  };

  // Re-render every element tagged with a Celsius source value.
  window.applyTempUnit = () => {
    document.body.setAttribute('data-unit', getUnit());
    document.querySelectorAll('[data-temp-c]').forEach((el) => {
      const c = el.getAttribute('data-temp-c');
      if (c === '' || c == null) return;
      el.textContent = (el.dataset.tempPrefix || '') + window.formatTemp(parseFloat(c));
    });
  };
  applyTempUnit();

  document.getElementById('unitToggle')?.addEventListener('click', () => {
    store.set(UNIT_KEY, getUnit() === 'F' ? 'C' : 'F');
    applyTempUnit();
  });
})();

// ── Life Loop: update-available popup (dashboard only) ────────────────
// Quiet by design: only on the dashboard, only when online, and only when a
// newer release exists that hasn't already been dismissed for that version.
(() => {
  if (window.location.pathname !== '/') return;
  if (navigator.onLine === false) return;
  const popup = document.getElementById('updatePopup');
  if (!popup) return;
  const KEY = 'lifeloop-update-dismissed';
  const dismissed = () => { try { return localStorage.getItem(KEY); } catch { return null; } };

  fetch('/api/update-check')
    .then((r) => r.json())
    .then((d) => {
      if (!d || !d.update_available || !d.latest) return;
      if (dismissed() === d.latest) return;

      const ver = document.getElementById('updateVersion');
      if (ver) ver.textContent = d.latest;
      const notes = document.getElementById('updateNotes');
      if (notes && d.notes) { notes.textContent = d.notes; notes.hidden = false; }
      const link = document.getElementById('updateLink');
      if (link) { if (d.url) link.href = d.url; else link.hidden = true; }

      popup.hidden = false;
      requestAnimationFrame(() => requestAnimationFrame(() => popup.classList.add('show')));

      document.getElementById('updateDismiss')?.addEventListener('click', () => {
        try { localStorage.setItem(KEY, d.latest); } catch { /* ignore */ }
        popup.classList.remove('show');
        popup.addEventListener('transitionend', () => { popup.hidden = true; }, { once: true });
        setTimeout(() => { popup.hidden = true; }, 320);
      });
    })
    .catch(() => { /* offline / unavailable — stay quiet */ });
})();
