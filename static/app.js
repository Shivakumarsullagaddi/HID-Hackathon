// Kiosk page: camera, face guide, Verify, result card, blink step-up.
const video = $('#video');
const verifyBtn = $('#verify');
const resultCard = $('#result');
const stepup = $('#stepup');
const zoneSelect = $('#zone-select');
const hourSelect = $('#hour-select');
const connBtn = $('#connectivity-btn');
const queueBtn = $('#offline-queue-btn');
const queueBadge = $('#offline-queue-badge');
const queueDrawer = $('#queue-drawer');
const closeQueueDrawer = $('#close-queue-drawer');
const queueContent = $('#queue-content');
const syncBanner = $('#sync-banner');

let gate = { available: false, ready: false, hint: '' };
let busy = false;
let cameraOn = false;
let pendingRequestId = null;
let isOnline = true;

function refreshControls() {
  verifyBtn.disabled = busy || !cameraOn || !gate.ready;
  $('#oval').classList.toggle('ready', cameraOn && gate.available && gate.ready);
  if (cameraOn && !busy) $('#hint').textContent = gate.hint;
}

function getAccessContext() {
  const zone = zoneSelect ? zoneSelect.value : 'main_gate';
  const hourVal = hourSelect && hourSelect.value !== '' ? parseInt(hourSelect.value, 10) : new Date().getHours();
  return { zone, access_hour: hourVal };
}

async function refreshOfflineQueue() {
  try {
    const s = await api('/api/offline-queue');
    isOnline = s.online;
    if (connBtn) {
      connBtn.className = `conn-btn ${isOnline ? 'conn-online' : 'conn-offline'}`;
      connBtn.textContent = isOnline ? 'Central Server: ONLINE' : 'FAIL-SAFE: OFFLINE (Local Fallback)';
    }
    if (queueBadge) {
      queueBadge.textContent = `Cached Offline: ${s.queue_size} (0 Biometrics)`;
    }
    if (queueContent && queueDrawer && !queueDrawer.hidden) {
      queueContent.textContent = s.queue.length ? JSON.stringify(s.queue, null, 2) : 'No offline decisions currently cached.';
    }
  } catch (e) {
    console.warn('Could not refresh offline queue:', e);
  }
}

if (connBtn) {
  connBtn.addEventListener('click', async () => {
    try {
      const nextOnline = !isOnline;
      const res = await api('/api/connectivity', { body: { online: nextOnline } });
      isOnline = res.online;
      if (res.last_sync && res.last_sync.synced_count > 0 && syncBanner) {
        syncBanner.hidden = false;
        syncBanner.textContent = `Auto-Synced ${res.last_sync.synced_count} offline decision(s) to Central Audit Log (biometrics_scrubbed=true)`;
        setTimeout(() => { if (syncBanner) syncBanner.hidden = true; }, 6000);
      }
      await refreshOfflineQueue();
      showStatus();
    } catch (e) {
      alert(`Could not toggle connectivity: ${e.message}`);
    }
  });
}

if (queueBtn && queueDrawer) {
  queueBtn.addEventListener('click', async () => {
    queueDrawer.hidden = !queueDrawer.hidden;
    if (!queueDrawer.hidden) await refreshOfflineQueue();
  });
}
if (closeQueueDrawer && queueDrawer) {
  closeQueueDrawer.addEventListener('click', () => { queueDrawer.hidden = true; });
}

function renderResult(r, totalMs) {
  const who = r.matched_name ? `${esc(r.matched_name)} (${esc(r.matched_user)})` : 'no match';
  const s = r.signals || {};
  const isStepUpReq = r.decision === 'REQUEST_VERIFICATION';
  const label = isStepUpReq ? 'STEP-UP VERIFICATION' : (DECISION_LABEL[r.decision] || r.decision);
  resultCard.className = `card result ${r.decision}`;
  resultCard.innerHTML = `
    <div class="verdict"><span class="dot ${esc(r.decision)}"></span>
      ${DECISION_ICON[r.decision]} ${esc(label)}</div>
    <div class="reason">${esc(r.reason)}</div>
    <dl class="kv">
      <dt>Identity</dt><dd>${who}</dd>
      <dt>Campus Role</dt><dd><b>${esc(r.role || 'student')}</b></dd>
      <dt>Zone & Time</dt><dd>${esc(r.zone || 'main_gate')} · ${r.access_hour != null ? `${String(r.access_hour).padStart(2, '0')}:00` : 'live'}</dd>
      <dt>Schedule Status</dt><dd><span class="pill"><span class="dot ${r.schedule_status === 'ALLOW' ? 'good' : (r.schedule_status === 'STEPUP' ? 'warn' : 'bad')}"></span>${esc(r.schedule_status || 'ALLOW')}</span> ${r.schedule_reason ? `<span class="muted">(${esc(r.schedule_reason)})</span>` : ''}</dd>
      <dt>Network Mode</dt><dd>${r.offline_mode ? '<span class="pill"><span class="dot bad"></span>FAIL-SAFE OFFLINE (Scrubbed)</span>' : '<span class="pill"><span class="dot good"></span>ONLINE (Central Log)</span>'}</dd>
      <dt>Liveness</dt><dd>${r.liveness.toFixed(2)} <span class="muted">(${esc(r.liveness_mode)})</span></dd>
      <dt>Similarity</dt><dd>${r.similarity == null ? 'n/a' : r.similarity.toFixed(1)}</dd>
      ${r.gate_reason ? `<dt>Image quality</dt><dd>${r.gate_passed ? 'ok' : esc(r.gate_reason)}</dd>` : ''}
      ${r.blinked == null ? '' : `<dt>Blink</dt><dd>${r.blinked ? 'detected' : 'not detected'}</dd>`}
      <dt>Server time</dt><dd>${r.latency_ms.toFixed(0)} ms</dd>
      <dt>Press to result</dt><dd>${totalMs.toFixed(0)} ms</dd>
    </dl>
    <p class="muted" style="margin:12px 0 0;font-size:12px">
      passive ${s.passive ?? 'n/a'} · moiré ${s.moire_ratio ?? 'n/a'} · glare ${s.glare_fraction ?? 'n/a'}
      · motion ${s.motion ?? 'n/a'} · queue ${r.offline_queue_size ?? 0} · id ${esc(r.request_id || '').slice(0, 8)}</p>`;
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
    const ctx = getAccessContext();
    $('#hint').textContent = 'Checking…';
    const r = await api('/api/verify', { body: { frames, zone: ctx.zone, access_hour: ctx.access_hour } });
    renderResult(r, performance.now() - t0);
    await refreshOfflineQueue();
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
    const ctx = getAccessContext();
    const r = await api('/api/stepup/blink', {
      body: { frames, request_id: pendingRequestId, zone: ctx.zone, access_hour: ctx.access_hour },
    });
    if (r.decision) {
      renderResult(r, performance.now() - t0);
    } else {
      renderError('The earlier check expired. Press Verify to start again.');
    }
    await refreshOfflineQueue();
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
  refreshOfflineQueue();
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

