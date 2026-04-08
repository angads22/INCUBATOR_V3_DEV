const startSetupBtn = document.getElementById('startSetupBtn');
const onboardingMsg = document.getElementById('onboardingMsg');

startSetupBtn?.addEventListener('click', async () => {
  const res = await fetch('/onboarding/start', { method: 'POST' });
  const data = await res.json();
  onboardingMsg.textContent = data.ok
    ? `Setup mode enabled. Connect phone and open ${data.ap_url}`
    : 'Failed to start setup mode.';
});

document.getElementById('onboardingForm')?.addEventListener('submit', async (e) => {
  e.preventDefault();
  const f = e.target;
  const payload = {
    ssid: f.ssid.value,
    password: f.password.value,
    device_name: f.device_name.value,
    create_account: f.create_account.checked,
    username: f.username.value,
  };
  const res = await fetch('/onboarding/complete', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  const data = await res.json();
  onboardingMsg.textContent = data.ok
    ? 'Setup saved. Device will continue in normal mode using selected Wi-Fi.'
    : 'Setup failed. Please retry.';
});
