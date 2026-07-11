/* Sign in with Google / Apple + linked accounts.
 *
 * Used by two pages with two intents:
 *   - /login          → intent "login"  (buttons + panels in login.html)
 *   - /settings       → intent "link"   (linked-accounts card in settings.html)
 * Buttons stay hidden unless /api/identity/config says the fleet configured
 * that provider, so an unconfigured unit shows exactly the old UI.
 */
(function () {
  const $ = (id) => document.getElementById(id);
  const intent = document.body?.dataset?.identityIntent
    || (location.pathname.startsWith('/settings') ? 'link' : 'login');

  const post = (url, body) => fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
  }).then(async (r) => ({ status: r.status, body: await r.json().catch(() => ({})) }));

  // ── Capability discovery ────────────────────────────────────────────────
  async function init() {
    let cfg;
    try {
      cfg = await (await fetch('/api/identity/config')).json();
    } catch { return; }
    if (!cfg || (!cfg.google && !cfg.apple)) {
      $('identityUnavailableNote')?.removeAttribute('hidden');
      const list = $('linkedAccountsList');
      if (list) list.innerHTML = '';
      return;
    }

    const wrap = $('identityProviders');
    if (wrap) wrap.removeAttribute('hidden');
    if (cfg.google) $('googleSignInBtn')?.removeAttribute('hidden');
    if (cfg.apple) {
      const btn = $('appleSignInBtn');
      if (btn) {
        btn.removeAttribute('hidden');
        btn.dataset.brokerUrl = cfg.broker_url || '';
      }
    }
    refreshLinks();
  }

  // ── Google device-code flow ─────────────────────────────────────────────
  let pollTimer = null;

  $('googleSignInBtn')?.addEventListener('click', async () => {
    const panel = $('googleDevicePanel');
    const msg = $('googleFlowMsg');
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
    const { status, body } = await post('/api/identity/google/begin');
    if (status !== 200 || !body.ok) {
      if (panel) panel.removeAttribute('hidden');
      if (msg) msg.textContent = 'Google is unreachable right now — check the incubator is online and try again.';
      return;
    }
    if ($('googleVerifyUrl')) $('googleVerifyUrl').textContent = body.verification_url;
    if ($('googleUserCode')) $('googleUserCode').textContent = body.user_code;
    if (panel) panel.removeAttribute('hidden');
    if (msg) msg.textContent = 'Waiting for you to approve on the other device…';

    const interval = Math.max(3, body.interval || 5) * 1000;
    pollTimer = setInterval(async () => {
      const res = await post('/api/identity/google/poll', { flow_id: body.flow_id, intent });
      const b = res.body;
      if (b.status === 'pending') return;
      clearInterval(pollTimer); pollTimer = null;
      if (res.status === 200 && b.ok) {
        if (intent === 'link') {
          if (msg) msg.textContent = `Linked ${b.email || 'Google account'} ✓`;
          refreshLinks();
        } else {
          if (msg) msg.textContent = 'Signed in! Opening your incubator…';
          window.location.href = '/';
        }
        return;
      }
      if (msg) msg.textContent = b.detail || b.error === 'access_denied'
        ? 'Sign-in was cancelled or refused.' : (b.detail || 'Sign-in failed — try again.');
      if (res.status === 403 && b.detail) msg.textContent = b.detail;
      if (b.status === 'expired' && msg) msg.textContent = 'The code expired — tap Continue with Google to get a new one.';
    }, interval);
  });

  // ── Apple (or Google) via the fleet's HTTPS account portal ──────────────
  $('appleSignInBtn')?.addEventListener('click', (e) => {
    const url = e.currentTarget.dataset.brokerUrl;
    $('applePanel')?.removeAttribute('hidden');
    if (url) window.open(url, '_blank', 'noopener');
  });

  $('brokerSubmitBtn')?.addEventListener('click', async () => {
    const msg = $('appleFlowMsg');
    const assertion = ($('brokerAssertion')?.value || '').trim();
    if (!assertion) { if (msg) msg.textContent = 'Paste the code from the account portal first.'; return; }
    const { status, body } = await post('/api/identity/broker', { assertion, intent });
    if (status === 200 && body.ok) {
      if (intent === 'link') {
        if (msg) msg.textContent = `Linked ${body.email || 'account'} ✓`;
        if ($('brokerAssertion')) $('brokerAssertion').value = '';
        refreshLinks();
      } else {
        if (msg) msg.textContent = 'Signed in! Opening your incubator…';
        window.location.href = '/';
      }
      return;
    }
    if (msg) msg.textContent = body.detail || 'That code was not accepted — get a fresh one from the portal.';
  });

  // ── Linked-accounts list (settings page) ────────────────────────────────
  async function refreshLinks() {
    const listEl = $('linkedAccountsList');
    if (!listEl) return;
    let data;
    try { data = await (await fetch('/api/identity/links')).json(); } catch { return; }
    if (!data.ok) return;
    listEl.innerHTML = '';
    if (!data.links.length) {
      listEl.innerHTML = '<p class="muted">No Google or Apple account linked yet.</p>';
      return;
    }
    data.links.forEach((l) => {
      const row = document.createElement('div');
      row.className = 'button-row';
      row.style.justifyContent = 'space-between';
      row.style.alignItems = 'center';
      const label = document.createElement('span');
      label.innerHTML = `<strong>${l.provider === 'google' ? 'Google' : 'Apple'}</strong> · ${l.email || 'linked'}`;
      const btn = document.createElement('button');
      btn.className = 'btn btn-ghost';
      btn.type = 'button';
      btn.textContent = 'Unlink';
      btn.addEventListener('click', async () => {
        await fetch(`/api/identity/links/${l.id}`, { method: 'DELETE' });
        refreshLinks();
      });
      row.appendChild(label);
      row.appendChild(btn);
      listEl.appendChild(row);
    });
  }

  init();
})();
