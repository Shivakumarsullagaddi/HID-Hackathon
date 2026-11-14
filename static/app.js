// Kiosk page: camera, face guide, Verify, result card, blink step-up.
const video = $('#video');
const verifyBtn = $('#verify');
const resultCard = $('#result');
const stepup = $('#stepup');
let gate = { available: false, ready: false, hint: '' };
let busy = false;
let cameraOn = false;
let pendingRequestId = null;

function refreshControls() {
  verifyBtn.disabled = busy || !cameraOn || !gate.ready;
  $('#oval').classList.toggle('ready', cameraOn && gate.available && gate.ready);
  if (cameraOn && !busy) $('#hint').textContent = gate.hint;
}

function renderResult(r, totalMs) {
  const who = r.matched_name ? `${esc(r.matched_name)} (${esc(r.matched_user)})` : 'no match';
  const s = r.signals || {};
  resultCard.className = `card result ${r.decision}`;
  resultCard.innerHTML = `
    <div class="verdict"><span class="dot ${esc(r.decision)}"></span>
      ${DECISION_ICON[r.decision]} ${esc(DECISION_LABEL[r.decision])}</div>
    <div class="reason">${esc(r.reason)}</div>
    <dl class="kv">
      <dt>Identity</dt><dd>${who}</dd>
      <dt>Liveness</dt><dd>${r.liveness.toFixed(2)} <span class="muted">(${esc(r.liveness_mode)})</span></dd>
      <dt>Similarity</dt><dd>${r.similarity == null ? 'n/a' : r.similarity.toFixed(1)}</dd>
      ${r.gate_reason ? `<dt>Image quality</dt><dd>${r.gate_passed ? 'ok' : esc(r.gate_reason)}</dd>` : ''}
      ${r.blinked == null ? '' : `<dt>Blink</dt><dd>${r.blinked ? 'detected' : 'not detected'}</dd>`}
      <dt>Server time</dt><dd>${r.latency_ms.toFixed(0)} ms</dd>
      <dt>Press to result</dt><dd>${totalMs.toFixed(0)} ms</dd>
    </dl>
    <p class="muted" style="margin:12px 0 0;font-size:12px">
      passive ${s.passive ?? 'n/a'} · moiré ${s.moire_ratio ?? 'n/a'} · glare ${s.glare_fraction ?? 'n/a'}
      · motion ${s.motion ?? 'n/a'} · id ${esc(r.request_id || '').slice(0, 8)}</p>`;
}

function renderError(message) {
  resultCard.className = 'card result';
  resultCard.innerHTML = `<h2>Could not verify</h2><p class="sub">${esc(message)}</p>`;
}

verifyBtn.addEventListener('click', async () => {
  busy = true;
  stepup.hidden = true;
  pendingRequestId = null;
  refreshControls();
  $('#hint').textContent = 'Hold still…';
  const t0 = performance.now();
  try {
    const frames = await captureBurst(video, 5, 500);
    $('#hint').textContent = 'Checking…';
    const r = await api('/api/verify', { body: { frames } });
    renderResult(r, performance.now() - t0);
    if (r.decision === 'REQUEST_VERIFICATION') {
      pendingRequestId = r.request_id;
      $('#blink-status').textContent = '';
      stepup.hidden = false;
    }
  } catch (e) {
    renderError(e.message);
  }
  busy = false;
  refreshControls();
});

$('#blink-start').addEventListener('click', async () => {
  busy = true;
  refreshControls();
  const status = $('#blink-status');
  $('#blink-start').disabled = true;
  try {
    for (const n of [3, 2, 1]) { status.textContent = `${n}`; await sleep(600); }
    status.textContent = 'Blink now';
    $('#hint').textContent = 'Blink twice, slowly';
    const t0 = performance.now();
    const frames = await captureBurst(video, 24, 2200, 480, 0.8);
    status.textContent = 'Checking…';
    const r = await api('/api/stepup/blink', { body: { frames, request_id: pendingRequestId } });
    if (r.decision) {
      renderResult(r, performance.now() - t0);
    } else {
      renderError('The earlier check expired. Press Verify to start again.');
    }
    stepup.hidden = true;
    pendingRequestId = null;
  } catch (e) {
    status.textContent = '';
    renderError(e.message);
  }
  $('#blink-start').disabled = false;
  busy = false;
  refreshControls();
});

(async () => {
  showStatus();
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
