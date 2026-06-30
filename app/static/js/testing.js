// Testing tab — choose images, run vision_service.predict_stage on each, and
// record predicted-vs-actual to measure MAE. Uses the shared apiGet/apiPost
// helpers (base.js) for JSON; predict uses multipart FormData directly.

(() => {
  const predictBtn = document.getElementById('testingPredictBtn');
  if (!predictBtn) return; // not on the Testing page

  const msg = document.getElementById('testingMsg');
  const stagedEl = document.getElementById('testingStaged');
  const resultsEl = document.getElementById('testingResults');
  const resultsEmpty = document.getElementById('testingResultsEmpty');
  const capturesGrid = document.getElementById('testingCapturesGrid');

  // Stage pill class reuse (no new CSS): map a stage to an existing pill style.
  const STAGE_PILL = {
    early: 'is-healthy',
    mid: 'is-lockdown',
    late: 'is-attention',
    hatching: 'is-hatching',
    infertile: 'is-attention',
    unclear: 'is-attention',
  };

  let counter = 0;
  const staged = new Map(); // id -> {kind:'upload'|'path', file?, path?, url, name}

  const imageUrl = (path) => `/api/testing/image?path=${encodeURIComponent(path)}`;
  const thumbStyle =
    'width:64px;height:64px;object-fit:cover;border-radius:var(--radius-sm);border:1px solid var(--line)';

  function setMsg(text) { if (msg) msg.textContent = text; }

  function addUpload(file, url) {
    const id = `s${counter++}`;
    staged.set(id, { kind: 'upload', file, url: url || URL.createObjectURL(file), name: file.name });
    renderStaged();
  }
  function addPath(path, name) {
    // Avoid duplicates of the same server path.
    for (const v of staged.values()) if (v.kind === 'path' && v.path === path) return;
    const id = `s${counter++}`;
    staged.set(id, { kind: 'path', path, url: imageUrl(path), name: name || path.split('/').pop() });
    renderStaged();
  }

  function renderStaged() {
    stagedEl.innerHTML = '';
    if (!staged.size) {
      stagedEl.innerHTML = '<span class="muted">Nothing selected.</span>';
      return;
    }
    for (const [id, item] of staged.entries()) {
      const wrap = document.createElement('div');
      wrap.style.cssText = 'position:relative;display:inline-block';
      wrap.innerHTML =
        `<img src="${item.url}" alt="${item.name}" title="${item.name}" style="${thumbStyle}">` +
        `<button type="button" data-remove="${id}" aria-label="Remove" ` +
        `style="position:absolute;top:-6px;right:-6px;width:20px;height:20px;border-radius:50%;` +
        `border:1px solid var(--line);background:var(--panel);cursor:pointer;line-height:1;padding:0">&times;</button>`;
      stagedEl.appendChild(wrap);
    }
  }

  stagedEl.addEventListener('click', (e) => {
    const btn = e.target.closest('button[data-remove]');
    if (!btn) return;
    staged.delete(btn.dataset.remove);
    renderStaged();
  });

  // -- Upload input --
  document.getElementById('testingUpload')?.addEventListener('change', (e) => {
    for (const file of e.target.files) addUpload(file);
    e.target.value = '';
    setMsg(`${staged.size} image(s) selected.`);
  });

  // -- Capture from camera (full frame or selected egg's ROI tile) --
  document.getElementById('testingCaptureBtn')?.addEventListener('click', async (e) => {
    const eggId = document.getElementById('testingEggSelect')?.value || '';
    const url = eggId ? `/api/camera/egg/${eggId}` : '/api/camera/snapshot';
    e.target.disabled = true;
    setMsg('Capturing…');
    try {
      const res = await fetch(url);
      if (!res.ok) { setMsg(res.status === 401 ? 'Please log in to use the camera.' : `Capture failed (${res.status}).`); return; }
      const blob = await res.blob();
      const file = new File([blob], `capture_${Date.now()}.jpg`, { type: blob.type || 'image/jpeg' });
      addUpload(file, URL.createObjectURL(blob));
      setMsg('Captured frame added to selection.');
    } catch {
      setMsg('Cannot reach the camera.');
    } finally {
      e.target.disabled = false;
    }
  });

  // -- Load saved captures --
  document.getElementById('testingLoadCaptures')?.addEventListener('click', async () => {
    const res = await apiGet('/api/testing/captures');
    if (!res.ok) { setMsg(res.message); return; }
    const items = res.data.captures || [];
    capturesGrid.innerHTML = items.length ? '' : '<span class="muted">No saved captures found.</span>';
    items.forEach((c) => {
      const img = document.createElement('img');
      img.src = imageUrl(c.path);
      img.alt = c.name;
      img.title = c.name;
      img.style.cssText = thumbStyle + ';cursor:pointer';
      img.addEventListener('click', () => { addPath(c.path, c.name); setMsg(`${staged.size} image(s) selected.`); });
      capturesGrid.appendChild(img);
    });
  });

  document.getElementById('testingClearStaged')?.addEventListener('click', () => {
    staged.clear();
    renderStaged();
    setMsg('Selection cleared.');
  });

  // -- Predict all (batch) --
  function fmtDay(p) {
    const lo = p.day_range ? p.day_range[0] : null;
    const hi = p.day_range ? p.day_range[1] : null;
    const range = (lo != null && hi != null) ? ` (day ${lo}–${hi})` : '';
    return `≈ day ${Number(p.day_estimate).toFixed(1)}${range}`;
  }

  function featureChips(features) {
    if (!features || typeof features !== 'object') return '';
    return Object.entries(features)
      .map(([k, v]) => `<span class="status-chip status-info">${k}: ${typeof v === 'number' ? Number(v).toFixed(3) : v}</span>`)
      .join('');
  }

  function renderPrediction(p) {
    const card = document.createElement('div');
    card.className = 'card';
    card.style.cssText = 'display:grid;gap:8px';
    const pillClass = STAGE_PILL[p.stage] || 'is-attention';
    const ok = p.ok !== false;
    card.innerHTML =
      `<div style="display:flex;gap:12px;align-items:center">` +
        `<img src="${imageUrl(p.path)}" alt="${p.name || ''}" style="${thumbStyle}">` +
        `<div style="display:grid;gap:4px">` +
          `<strong style="font-family:var(--font-mono)">${ok ? fmtDay(p) : 'Prediction failed'}</strong>` +
          `<div class="chip-row">` +
            `<span class="status-pill ${pillClass}">${p.stage || 'unclear'}</span>` +
            `<span class="status-chip status-info">conf ${Number(p.confidence || 0).toFixed(2)}</span>` +
            `<span class="status-chip status-info">${p.backend || '—'}</span>` +
          `</div>` +
        `</div>` +
      `</div>` +
      `<div class="chip-row">${featureChips(p.features)}</div>` +
      `<div class="button-row">` +
        `<label style="display:flex;gap:8px;align-items:center;color:var(--muted);font-weight:600;font-size:0.9rem">` +
          `Actual day <input type="number" min="0" step="0.5" class="testing-actual" style="max-width:110px" placeholder="e.g. 8"></label>` +
        `<button type="button" class="btn btn-secondary testing-record">Record</button>` +
      `</div>` +
      `<p class="hint testing-record-msg"></p>`;

    const recordBtn = card.querySelector('.testing-record');
    recordBtn.addEventListener('click', async () => {
      const actualInput = card.querySelector('.testing-actual');
      const recMsg = card.querySelector('.testing-record-msg');
      const actualRaw = actualInput.value.trim();
      const body = {
        image_path: p.path,
        predicted_day: Number(p.day_estimate) || 0,
        stage: p.stage || 'unclear',
        confidence: Number(p.confidence) || 0,
        backend: p.backend || 'unknown',
        actual_day: actualRaw === '' ? null : Number(actualRaw),
      };
      recordBtn.disabled = true;
      const res = await apiPost('/api/testing/record', body);
      recMsg.textContent = res.ok
        ? `Recorded.${res.data.mae != null ? ` MAE now ${res.data.mae} d (${res.data.count}).` : ''}`
        : res.message;
      recordBtn.disabled = false;
      if (res.ok) loadResults();
    });
    return card;
  }

  predictBtn.addEventListener('click', async () => {
    if (!staged.size) { setMsg('Select at least one image first.'); return; }
    const fd = new FormData();
    for (const item of staged.values()) {
      if (item.kind === 'upload') fd.append('files', item.file, item.name);
      else fd.append('paths', item.path);
    }
    predictBtn.disabled = true;
    setMsg('Predicting…');
    let res;
    try {
      res = await fetch('/api/testing/predict', { method: 'POST', body: fd });
    } catch {
      setMsg('Cannot reach the incubator.');
      predictBtn.disabled = false;
      return;
    }
    predictBtn.disabled = false;
    if (res.status === 401) { setMsg('Please log in to do that.'); return; }
    let data = null;
    try { data = await res.json(); } catch { /* ignore */ }
    if (!res.ok || !data || data.ok === false) {
      setMsg((data && (data.detail || data.error)) || `Predict failed (${res.status}).`);
      return;
    }
    resultsEl.innerHTML = '';
    (data.predictions || []).forEach((p) => resultsEl.appendChild(renderPrediction(p)));
    resultsEmpty.hidden = data.predictions && data.predictions.length > 0;
    setMsg(`Predicted ${data.count} image(s). Enter actual days and record to measure MAE.`);
  });

  // -- Results table + MAE --
  function renderResults(data) {
    const mae = document.getElementById('testingMae');
    const recorded = document.getElementById('testingRecorded');
    const body = document.getElementById('testingSavedBody');
    const empty = document.getElementById('testingSavedEmpty');
    if (mae) mae.textContent = data.mae != null ? `${data.mae}` : '—';
    if (recorded) recorded.textContent = `${data.count || 0}`;
    const rows = data.results || [];
    body.innerHTML = '';
    rows.forEach((r) => {
      const tr = document.createElement('tr');
      const cell = (v) => `<td style="padding:6px 8px;border-bottom:1px solid var(--line)">${v}</td>`;
      const name = (r.image_path || '').split('/').pop();
      tr.innerHTML =
        cell(`<span title="${r.image_path}">${name}</span>`) +
        cell(Number(r.predicted_day).toFixed(1)) +
        cell(r.actual_day != null ? Number(r.actual_day).toFixed(1) : '—') +
        cell(r.error != null ? r.error : '—') +
        cell(r.stage) +
        cell(r.backend);
      body.appendChild(tr);
    });
    empty.hidden = rows.length > 0;
  }

  async function loadResults() {
    const res = await apiGet('/api/testing/results');
    if (res.ok) renderResults(res.data);
  }

  document.getElementById('testingRefresh')?.addEventListener('click', loadResults);
  document.getElementById('testingClearResults')?.addEventListener('click', async () => {
    if (!window.confirm('Clear all saved test results?')) return;
    const res = await apiPost('/api/testing/clear', {});
    if (res.ok) loadResults();
  });

  renderStaged();
  loadResults();
})();
