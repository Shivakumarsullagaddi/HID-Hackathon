// Kiosk page: camera, face guide, Verify, result card, step-up (blink, plus
// PIN when DEGRADED), kiosk state, room selector, simulated outage.
const video = $('#video');
const verifyBtn = $('#verify');
const resultCard = $('#result');
const stepup = $('#stepup');
let gate = { available: false, ready: false, hint: '' };
let busy = false;
let cameraOn = false;
let cooldownUntil = 0;
let pending = null;          // {request_id, kiosk_state} awaiting a step-up
let kiosk = null;            // last /api/state
const failures = [];         // times of failed low-score attempts (2 min window)

const MAX_AUTO_RETRIES = 2;
const FAIL_WINDOW_MS = 2 * 60 * 1000;
const COOLDOWN_MS = 30 * 1000;

const HINTS = {
  no_face: 'Move closer and face the camera',
  too_dark: 'Face the light',
  too_bright: 'Move out of direct light',
  blurry: 'Hold still',
};
const BIO_WORDS = {
  ALLOW: 'live and recognised',
  REQUEST_VERIFICATION: 'not sure',
  DENY: 'failed',
};
const SCHEDULE_WORDS = {
  in_window: 'inside permitted hours',
  outside_window: 'outside permitted hours',
  no_rule: 'no rule for this door',
};

function retryHint(r) {
  if (r.bio_decision !== 'REQUEST_VERIFICATION') return null;
  if (!r.gate_passed) return HINTS[r.gate_reason] || 'Hold still';
  if (r.liveness >= 0.70 && r.liveness < 0.85) return 'Hold still and look straight at the camera';
  return null;
}

function refreshControls() {
  const cooling = Date.now() < cooldownUntil;
  verifyBtn.disabled = busy || !cameraOn || !gate.ready || cooling;
  $('#oval').classList.toggle('ready', cameraOn && gate.available && gate.ready);
  if (cooling) {
    $('#hint').textContent = `Too many tries. Wait ${Math.ceil((cooldownUntil - Date.now()) / 1000)} s`;
  } else if (cameraOn && !busy) {
    $('#hint').textContent = gate.hint;
  }
}

function renderResult(r, totalMs) {
  const final = r.final_decision || 'DENY';
  const who = r.matched_name ? `${esc(r.matched_name)} (${esc(r.matched_user)})` : 'no match';
  const s = r.signals || {};
  resultCard.className = `card result ${DOT_CLASS[final]}`;
  resultCard.innerHTML = `
    <div class="verdict"><span class="dot ${esc(DOT_CLASS[final])}"></span>
      ${DECISION_ICON[final]} ${esc(DECISION_LABEL[final])}</div>
    <div class="reason">${esc(r.final_reason || '')}</div>
    <dl class="kv">
      <dt>Biometric check</dt><dd>${esc(BIO_WORDS[r.bio_decision] || r.bio_decision)}
        <span class="muted">(${esc(r.reason)})</span></dd>
      <dt>Identity</dt><dd>${who}</dd>
      <dt>Schedule</dt><dd>${esc(SCHEDULE_WORDS[r.schedule_status] || 'not checked')}
        <span class="muted">(${esc(r.room_id || '')})</span></dd>
      <dt>Kiosk</dt><dd>${esc(r.kiosk_state || '')} <span class="muted">snapshot v${esc(r.snapshot_version ?? 'none')}</span></dd>
      <dt>Liveness</dt><dd>${r.liveness.toFixed(2)} <span class="muted">(${esc(r.liveness_mode || '')})</span></dd>
      <dt>Similarity</dt><dd>${r.similarity == null ? 'n/a' : r.similarity.toFixed(1)}</dd>
      ${r.gate_reason ? `<dt>Image quality</dt><dd>${r.gate_passed ? 'ok' : esc(r.gate_reason)}</dd>` : ''}
      ${r.blinked == null ? '' : `<dt>Blink</dt><dd>${r.blinked ? 'detected' : 'not detected'}</dd>`}
      ${r.pin_checked == null ? '' : `<dt>PIN</dt><dd>${r.pin_checked ? 'correct' : 'not accepted'}</dd>`}
      <dt>Server time</dt><dd>${r.latency_ms.toFixed(0)} ms</dd>
      <dt>Press to result</dt><dd>${totalMs.toFixed(0)} ms</dd>
    </dl>
    <p class="muted" style="margin:12px 0 0;font-size:12px">
      passive ${s.passive ?? 'n/a'} · moiré ${s.moire_ratio ?? 'n/a'} · motion ${s.motion ?? 'n/a'}
      · record ${esc(r.record_id || '').slice(0, 8)}</p>`;
}

function renderError(message) {
  resultCard.className = 'card result';
  resultCard.innerHTML = `<h2>Could not verify</h2><p class="sub">${esc(message)}</p>`;
}

// A low-score attempt that ended without ALLOW. Three within 2 minutes:
// cooldown, and an audit record (central raises an alert).
async function noteFailure() {
  const now = Date.now();
  failures.push(now);
  while (failures.length && now - failures[0] > FAIL_WINDOW_MS) failures.shift();
  if (failures.length >= 3) {
    failures.length = 0;
    cooldownUntil = now + COOLDOWN_MS;
    try { await api('/api/attempts/failed', { body: { room_id: $('#room').value } }); } catch (e) { /* logged server side */ }
    const tick = setInterval(() => { refreshControls(); if (Date.now() >= cooldownUntil) clearInterval(tick); }, 1000);
  }
}

function showStepup(r) {
  pending = { request_id: r.request_id, kiosk_state: r.kiosk_state };
  const offline = r.kiosk_state === 'DEGRADED';
  $('#pin-row').hidden = !offline;
  $('#pin').value = '';
  $('#stepup-text').textContent = offline
    ? 'The kiosk is offline. Enter your PIN, press Start, then blink twice slowly.'
    : 'Press Start, then blink twice slowly while looking at the camera.';
  $('#blink-status').textContent = '';
  stepup.hidden = false;
}

async function verifyOnce() {
  const frames = await captureBurst(video, 5, 500);
  $('#hint').textContent = 'Checking…';
  return api('/api/verify', { body: { frames, room_id: $('#room').value } });
}

verifyBtn.addEventListener('click', async () => {
  busy = true;
  stepup.hidden = true;
  pending = null;
  refreshControls();
  try {
    let lowScore = false;
    for (let attempt = 0; ; attempt++) {
      $('#hint').textContent = 'Hold still…';
      const t0 = performance.now();
      const r = await verifyOnce();
      renderResult(r, performance.now() - t0);
      if (r.bio_decision === 'REQUEST_VERIFICATION') lowScore = true;
      const hint = retryHint(r);
      if (r.final_decision === 'STEP_UP_VERIFICATION' && hint && attempt < MAX_AUTO_RETRIES) {
        $('#hint').textContent = `${hint}. Trying again…`;
        await sleep(1200);
        continue;
      }
      if (r.final_decision === 'STEP_UP_VERIFICATION') showStepup(r);
      else if (r.final_decision !== 'ALLOW' && lowScore) await noteFailure();
      break;
    }
  } catch (e) {
    renderError(e.message);
  }
  busy = false;
  refreshControls();
});

$('#blink-start').addEventListener('click', async () => {
  if (!pending) return;
  const offline = pending.kiosk_state === 'DEGRADED';
  const pin = $('#pin').value.trim();
  const status = $('#blink-status');
  if (offline && !/^\d{4,8}$/.test(pin)) {
    status.textContent = 'Enter your 4 to 8 digit PIN first';
    return;
  }
  busy = true;
  refreshControls();
  $('#blink-start').disabled = true;
  try {
    for (const n of [3, 2, 1]) { status.textContent = `${n}`; await sleep(600); }
    status.textContent = 'Blink now';
    $('#hint').textContent = 'Blink twice, slowly';
    const t0 = performance.now();
    const frames = await captureBurst(video, 24, 2200, 480, 0.8);
    status.textContent = 'Checking…';
    const body = { frames, request_id: pending.request_id };
    if (offline) body.pin = pin;
    const r = await api('/api/stepup/blink', { body });
    $('#pin').value = '';
    if (r.final_decision) {
      renderResult(r, performance.now() - t0);
      if (r.final_decision !== 'ALLOW') await noteFailure();
    } else {
      renderError('The earlier check expired. Press Verify to start again.');
    }
    stepup.hidden = true;
    pending = null;
  } catch (e) {
    status.textContent = '';
    renderError(e.message);
  }
  $('#blink-start').disabled = false;
  busy = false;
  refreshControls();
});

// ---- kiosk state, room selector, simulated outage --------------------
function renderState(st) {
  kiosk = st;
  $('#state-badge').innerHTML = stateBadge(st);
  const locked = st.state === 'LOCKED';
  $('#locked-banner').hidden = !locked;
  $('#locked-reason').textContent = locked ? ` (${st.reason})` : '';
  const sim = st.simulated_outage || st.force_offline;
  $('#sim-tag').hidden = !sim;
  $('#simulate').checked = st.simulated_outage;
  $('#simulate').disabled = st.force_offline;
  const unsynced = st.outbox ? st.outbox.unsynced : 0;
  $('#state-detail').textContent = `${st.reason} · outbox ${unsynced} unsynced` +
    (st.warnings && st.warnings.length ? ` · ${st.warnings.join('; ')}` : '');
  const sel = $('#room');
  if (!sel.options.length) {
    sel.innerHTML = st.rooms.map((r) => `<option value="${esc(r)}">${esc(r)}${r === st.room_id ? ' (this kiosk)' : ''}</option>`).join('');
    sel.value = st.room_id;
  }
}

async function pollState() {
  try { renderState(await api('/api/state')); } catch (e) { /* server restarting */ }
}

$('#simulate').addEventListener('change', async (ev) => {
  try { renderState(await api('/api/state/simulate', { body: { outage: ev.target.checked } })); } catch (e) {
    renderError(e.message);
  }
});

(async () => {
  showStatus();
  pollState();
  setInterval(pollState, 1000);
  try {
    await startCamera(video);
    cameraOn = true;
    startFaceGate(video, (state) => { gate = state; refreshControls(); });
  } catch (e) {
    $('#hint').textContent = 'Camera unavailable';
    renderError(`Camera unavailable: ${e.message}. Allow camera access and open the page on http://127.0.0.1:8000.`);
  }
  refreshControls();
})();
