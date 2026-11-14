// Shared helpers: API calls, webcam, frame capture, MediaPipe face gate.
const MP_VERSION = '0.4.1646425229';
const MP_BASE = `https://cdn.jsdelivr.net/npm/@mediapipe/face_detection@${MP_VERSION}/`;

const $ = (sel) => document.querySelector(sel);

function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g,
    (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

async function api(path, options = {}) {
  if (options.body !== undefined) {
    options = { method: 'POST', ...options, body: JSON.stringify(options.body),
      headers: { 'Content-Type': 'application/json' } };
  }
  const res = await fetch(path, options);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = typeof data.detail === 'string' ? data.detail
      : (data.detail || []).map((d) => d.msg).join('; ');
    throw new Error(detail || `${res.status} ${res.statusText}`);
  }
  return data;
}

// People see three outcomes. REQUEST_VERIFICATION is the internal biometric
// label; it always leads to a step-up, so it is shown with the same name.
const DECISION_LABEL = {
  ALLOW: 'ALLOW', DENY: 'DENY',
  STEP_UP_VERIFICATION: 'STEP-UP VERIFICATION', REQUEST_VERIFICATION: 'STEP-UP VERIFICATION',
};
const DECISION_ICON = { ALLOW: '✓', DENY: '✕', STEP_UP_VERIFICATION: '?', REQUEST_VERIFICATION: '?' };
const DOT_CLASS = { ALLOW: 'ALLOW', DENY: 'DENY', STEP_UP_VERIFICATION: 'REQUEST_VERIFICATION',
  REQUEST_VERIFICATION: 'REQUEST_VERIFICATION' };
const STATE_CLASS = { ONLINE: 'good', DEGRADED: 'warn', LOCKED: 'bad' };

function decisionPill(decision) {
  return `<span class="pill"><span class="dot ${esc(DOT_CLASS[decision] || decision)}"></span>` +
    `${DECISION_ICON[decision] || ''} ${esc(DECISION_LABEL[decision] || decision)}</span>`;
}

function fmtSeconds(s) {
  if (s == null) return 'n/a';
  s = Math.max(0, Math.round(s));
  return s >= 60 ? `${Math.floor(s / 60)} min ${s % 60} s` : `${s} s`;
}

// Kiosk state badge: ONLINE green, DEGRADED amber, LOCKED red.
function stateBadge(st) {
  if (!st) return '';
  const age = st.snapshot_age_s == null ? 'no snapshot'
    : `snapshot ${fmtSeconds(st.snapshot_age_s)} old, ${fmtSeconds(st.time_left_s)} left`;
  const sim = st.simulated_outage || st.force_offline ? ' · SIMULATED OUTAGE' : '';
  return `<span class="pill" title="${esc(st.reason)}"><span class="dot ${STATE_CLASS[st.state] || ''}"></span>` +
    `<b>${esc(st.state)}</b>&nbsp;· ${esc(age)}${sim}</span>`;
}

// Shows the liveness mode and kiosk state in the header, warns loudly when degraded.
async function showStatus() {
  const badge = $('#mode-badge');
  try {
    const s = await api('/api/status');
    const ok = s.liveness_mode === 'ensemble' && s.identity_available;
    badge.innerHTML = `<span class="pill"><span class="dot ${ok ? 'good' : 'warn'}"></span>` +
      `liveness: ${esc(s.liveness_mode)}${s.identity_available ? '' : ' · identity OFF'}</span>`;
    const stateBox = $('#state-badge');
    if (stateBox) {
      const refresh = async () => {
        try { stateBox.innerHTML = stateBadge(await api('/api/state')); } catch (e) { /* keep last */ }
      };
      refresh();
      setInterval(refresh, 3000);
    }
    const warn = $('#status-banner');
    if (warn && !ok) {
      warn.hidden = false;
      warn.innerHTML = (s.liveness_mode !== 'ensemble'
        ? `<b>Heuristic-only liveness.</b> MiniFASNet failed to load (${esc(s.liveness_error)}). ` +
          'No one can be allowed without a step-up. ' : '') +
        (s.identity_available ? ''
          : `<b>Identity matching is off.</b> ${esc(s.identity_error)}. Nobody can be allowed.`);
    }
    return s;
  } catch (e) {
    badge.innerHTML = '<span class="pill"><span class="dot bad"></span>server unreachable</span>';
    return null;
  }
}

async function startCamera(video) {
  const stream = await navigator.mediaDevices.getUserMedia({
    video: { width: { ideal: 640 }, height: { ideal: 480 }, facingMode: 'user' }, audio: false,
  });
  video.srcObject = stream;
  await video.play();
  return stream;
}

const _canvas = document.createElement('canvas');
function grabFrame(video, width = 640, quality = 0.9) {
  const scale = Math.min(1, width / video.videoWidth);
  _canvas.width = Math.round(video.videoWidth * scale);
  _canvas.height = Math.round(video.videoHeight * scale);
  _canvas.getContext('2d').drawImage(video, 0, 0, _canvas.width, _canvas.height);
  return _canvas.toDataURL('image/jpeg', quality).split(',')[1];
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// `count` frames spread over `durationMs` (first frame immediately).
async function captureBurst(video, count, durationMs, width, quality) {
  const frames = [];
  const gap = count > 1 ? durationMs / (count - 1) : 0;
  const start = performance.now();
  for (let i = 0; i < count; i++) {
    const due = start + i * gap;
    if (performance.now() < due) await sleep(due - performance.now());
    frames.push(grabFrame(video, width, quality));
  }
  return frames;
}

// Runs MediaPipe Face Detection on the video and reports whether one face is
// present, big enough and centred. If MediaPipe cannot load (offline), the
// gate reports unavailable and the page lets the server-side gate decide.
function startFaceGate(video, onState) {
  if (typeof FaceDetection === 'undefined') {
    onState({ available: false, ready: true, hint: 'Face guide unavailable (MediaPipe did not load)' });
    return;
  }
  const detector = new FaceDetection({ locateFile: (file) => MP_BASE + file });
  detector.setOptions({ model: 'short', minDetectionConfidence: 0.6 });
  detector.onResults((results) => {
    const faces = results.detections || [];
    if (faces.length === 0) return onState({ available: true, ready: false, hint: 'No face found' });
    if (faces.length > 1) return onState({ available: true, ready: false, hint: 'One person at a time' });
    const box = faces[0].boundingBox;
    let hint = '';
    if (box.height < 0.30) hint = 'Move closer';
    else if (box.height > 0.80) hint = 'Move back';
    else if (Math.abs(box.xCenter - 0.5) > 0.12 || Math.abs(box.yCenter - 0.5) > 0.15) {
      hint = 'Centre your face in the oval';
    }
    onState({ available: true, ready: !hint, hint: hint || 'Ready' });
  });
  let failures = 0;
  const loop = async () => {
    if (video.readyState >= 2 && !document.hidden) {
      try {
        await detector.send({ image: video });
        failures = 0;
      } catch (e) {
        if (++failures >= 5) {
          console.error('MediaPipe face gate stopped:', e);
          onState({ available: false, ready: true, hint: 'Face guide unavailable' });
          return;
        }
      }
    }
    setTimeout(loop, 120);
  };
  loop();
}
