let currentStep = 0;
let selectedSsid = '';

function showStep(n) {
  document.querySelectorAll('.wizard-step').forEach((el) => el.classList.remove('active'));
  const target = document.querySelector(`.wizard-step[data-step="${n}"]`);
  if (target) target.classList.add('active');
  currentStep = n;
}

// Generic prev navigation
document.querySelectorAll('[data-prev]').forEach((btn) => {
  btn.addEventListener('click', () => showStep(currentStep - 1));
});

// Generic next navigation
document.querySelectorAll('[data-next]').forEach((btn) => {
  btn.addEventListener('click', () => {
    if (currentStep === 1 && !selectedSsid) return;
    const next = currentStep + 1;
    showStep(next);
    if (next === 2) {
      document.getElementById('selectedSsidLabel').textContent = selectedSsid;
    }
  });
});

// Step 0: Begin setup
document.getElementById('beginSetupBtn')?.addEventListener('click', async () => {
  const msg = document.getElementById('setupStartMsg');
  msg.textContent = 'Starting setup mode\u2026';
  try {
    const res = await fetch('/onboarding/start', { method: 'POST' });
    const data = await res.json();
    if (data.ok) {
      msg.textContent = '';
      showStep(1);
      loadWifiNetworks();
    } else {
      msg.textContent = 'Could not start setup mode. Please try again.';
    }
  } catch {
    msg.textContent = 'Connection error. Ensure the device is reachable.';
  }
});

// Step 1: Wi-Fi scan
async function loadWifiNetworks() {
  const listEl = document.getElementById('wifiList');
  const msgEl = document.getElementById('wifiScanMsg');
  const nextBtn = document.getElementById('wifiNextBtn');
  listEl.innerHTML = '';
  selectedSsid = '';
  nextBtn.disabled = true;
  msgEl.textContent = 'Scanning for networks\u2026';
  try {
    const res = await fetch('/onboarding/wifi-scan');
    const data = await res.json();
    if (!data.networks || data.networks.length === 0) {
      msgEl.textContent = 'No networks found. You can still continue and set Wi-Fi later in network settings.';
      nextBtn.disabled = false;
      return;
    }
    msgEl.textContent = '';
    data.networks.forEach((net) => {
      const item = document.createElement('button');
      item.className = 'wifi-item';
      item.type = 'button';
      item.innerHTML =
        `<span class="wifi-ssid">${net.ssid}</span>` +
        `<span class="wifi-strength">${net.strength}%${net.secure ? ' \uD83D\uDD12' : ''}</span>`;
      item.addEventListener('click', () => {
        document.querySelectorAll('.wifi-item').forEach((el) => el.classList.remove('selected'));
        item.classList.add('selected');
        selectedSsid = net.ssid;
        nextBtn.disabled = false;
      });
      listEl.appendChild(item);
    });
  } catch {
    msgEl.textContent = 'Scan failed. You can continue and configure Wi-Fi later in network settings.';
    nextBtn.disabled = false;
  }
}

// Step 3: Account fields toggle
document.getElementById('createAccountCheck')?.addEventListener('change', (e) => {
  const fields = document.getElementById('accountFields');
  fields.style.display = e.target.checked ? 'grid' : 'none';
});

// Step 5: Finish
document.getElementById('finishSetupBtn')?.addEventListener('click', () => {
  showStep(6);
  submitSetup();
});

async function submitSetup() {
  const title = document.getElementById('finishTitle');
  const msg = document.getElementById('finishMsg');
  const result = document.getElementById('finishResult');
  const link = document.getElementById('finishLink');

  const createAccount = document.getElementById('createAccountCheck')?.checked || false;
  const payload = {
    ssid: selectedSsid,
    wifi_password: document.getElementById('wifiPassword')?.value || '',
    device_name: document.getElementById('deviceName')?.value || 'My Incubator',
    create_account: createAccount,
    username: createAccount ? (document.getElementById('acctUsername')?.value || null) : null,
    email: createAccount ? (document.getElementById('acctEmail')?.value || null) : null,
    password: createAccount ? (document.getElementById('acctPassword')?.value || null) : null,
  };

  try {
    const res = await fetch('/onboarding/complete', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (data.ok) {
      title.textContent = 'Setup Complete!';
      msg.textContent =
        `Your incubator \u201c${data.device_name}\u201d has been configured. ` +
        'It will reconnect using your selected Wi-Fi network.';
      result.textContent = '';
      link.removeAttribute('hidden');
    } else {
      title.textContent = 'Setup Error';
      result.textContent = data.detail || 'Unknown error. Please retry.';
    }
  } catch (err) {
    title.textContent = 'Setup Error';
    result.textContent = `Request failed: ${err}`;
  }
}
