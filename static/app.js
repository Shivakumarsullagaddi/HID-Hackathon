// Kiosk page: camera, face guide, Verify, result card, blink step-up, OTP challenge.
const video = $('#video');
const verifyBtn = $('#verify');
const resultCard = $('#result');
const stepup = $('#stepup');
const otpCard = $('#otp-challenge');
let gate = { available: false, ready: false, hint: '' };
let busy = false;
let cameraOn = false;
let pendingRequestId = null;
let currentChallengeId = null;
let otpTimerInterval = null;
let otpSecondsLeft = 60;

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
  if (otpCard) otpCard.hidden = true;
  if (otpTimerInterval) clearInterval(otpTimerInterval);
  pendingRequestId = null;
  currentChallengeId = null;
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

      // If OTP challenge created, show OTP card immediately above blink
      if (r.challenge_id && otpCard) {
        currentChallengeId = r.challenge_id;
        $('#otp-masked-email').textContent = r.masked_email || 'registered email';
        $('#otp-cid').textContent = r.challenge_id.slice(0, 8);

        // Ensure OTP input starts completely empty and gets auto-focused
        const otpInput = $('#otp-input');
        if (otpInput) {
          otpInput.value = '';
          setTimeout(() => otpInput.focus(), 50);
        }

        const statusMsg = $('#otp-status-msg');
        // Show small fallback text below input ONLY if SMTP failed/blocked
        if (r.email_delivery_status === 'smtp_blocked_fallback' && r.demo_otp) {
          statusMsg.style.display = 'block';
          statusMsg.style.background = 'rgba(234, 179, 8, 0.15)';
          statusMsg.style.border = '1px solid #eab308';
          statusMsg.style.color = '#fde047';
          statusMsg.style.padding = '8px 12px';
          statusMsg.style.borderRadius = '6px';
          statusMsg.textContent = `Venue Wi-Fi blocked SMTP — Backup Code: ${r.demo_otp}`;
        } else {
          statusMsg.style.display = 'none';
          statusMsg.textContent = '';
        }
        otpCard.hidden = false;

        otpSecondsLeft = 60;
        $('#otp-timer').textContent = `${otpSecondsLeft}s`;
        $('#otp-timer').style.color = '#10b981';
        otpTimerInterval = setInterval(() => {
          otpSecondsLeft--;
          if (otpSecondsLeft <= 0) {
            clearInterval(otpTimerInterval);
            $('#otp-timer').textContent = 'Expired';
            $('#otp-timer').style.color = '#ef4444';
          } else {
            $('#otp-timer').textContent = `${otpSecondsLeft}s`;
          }
        }, 1000);
      }
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

// OTP Step-Up verification handlers
if ($('#otp-verify-btn')) {
  $('#otp-verify-btn').addEventListener('click', async () => {
    const code = $('#otp-input').value.trim();
    if (!code || !currentChallengeId) return;
    const statusMsg = $('#otp-status-msg');
    statusMsg.style.display = 'none';
    $('#otp-verify-btn').disabled = true;
    $('#otp-verify-btn').textContent = 'Verifying...';
    try {
      const res = await api('/verify-otp', {
        body: { challenge_id: currentChallengeId, otp: code }
      });
      if (res.decision === 'ALLOW') {
        renderResult({
          decision: 'ALLOW',
          reason: 'OTP verified successfully: Identity confirmed',
          liveness: 1.0,
          similarity: 100.0,
          matched_name: res.user_id,
          matched_user: res.user_id,
          latency_ms: 0,
          gate_passed: true,
          liveness_mode: 'mfa-otp',
          request_id: currentChallengeId,
        }, 0);
        if (otpCard) otpCard.hidden = true;
        stepup.hidden = true;
        if (otpTimerInterval) clearInterval(otpTimerInterval);
      } else {
        statusMsg.style.display = 'block';
        statusMsg.style.color = '#ef4444';
        statusMsg.textContent = res.reason || 'Invalid OTP code';
      }
    } catch (err) {
      statusMsg.style.display = 'block';
      statusMsg.style.color = '#ef4444';
      statusMsg.textContent = err.message || 'OTP Verification failed';
    } finally {
      $('#otp-verify-btn').disabled = false;
      $('#otp-verify-btn').textContent = 'Verify OTP';
    }
  });
}

if ($('#otp-resend-btn')) {
  $('#otp-resend-btn').addEventListener('click', async () => {
    if (!currentChallengeId) return;
    const btn = $('#otp-resend-btn');
    const statusMsg = $('#otp-status-msg');
    btn.disabled = true;
    btn.textContent = 'Sending...';
    try {
      const res = await api('/resend-otp', { body: { challenge_id: currentChallengeId } });
      statusMsg.style.display = 'block';
      if (res.email_delivery_status === 'smtp_blocked_fallback' && res.demo_otp) {
        statusMsg.style.background = 'rgba(234, 179, 8, 0.15)';
        statusMsg.style.border = '1px solid #eab308';
        statusMsg.style.color = '#fde047';
        statusMsg.style.padding = '8px 12px';
        statusMsg.style.borderRadius = '6px';
        statusMsg.textContent = res.message || `Venue Wi-Fi blocked SMTP — Backup Code: ${res.demo_otp}`;
        // Input remains completely untouched - user types code manually
      } else {
        statusMsg.style.background = 'rgba(16, 185, 129, 0.15)';
        statusMsg.style.border = '1px solid #10b981';
        statusMsg.style.color = '#6ee7b7';
        statusMsg.style.padding = '8px 12px';
        statusMsg.style.borderRadius = '6px';
        statusMsg.textContent = res.message || 'OTP resent!';
      }
    } catch (err) {
      statusMsg.style.display = 'block';
      statusMsg.style.background = 'rgba(239, 68, 68, 0.15)';
      statusMsg.style.border = '1px solid #ef4444';
      statusMsg.style.color = '#fca5a5';
      statusMsg.style.padding = '8px 12px';
      statusMsg.style.borderRadius = '6px';
      statusMsg.textContent = err.message || 'Failed to resend';
    } finally {
      setTimeout(() => {
        btn.disabled = false;
        btn.textContent = 'Resend Email';
      }, 5000);
    }
  });
}

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
