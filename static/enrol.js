// Enrol page: capture 5 frames, enrol, manage standing.
const video = $('#video');
const enrolBtn = $('#enrol');
let gate = { available: false, ready: false, hint: '' };
let busy = false;
let cameraOn = false;

function refreshControls() {
  enrolBtn.disabled = busy || !cameraOn || !gate.ready;
  $('#oval').classList.toggle('ready', cameraOn && gate.available && gate.ready);
  if (cameraOn && !busy) $('#hint').textContent = gate.hint;
}

let central = { configured: false, url: '' };

async function loadUsers() {
  const users = await api('/api/users');
  const actions = (u) => central.configured
    ? `<a href="${esc(central.url)}/" target="_blank" rel="noopener" class="muted">change on central</a>`
    : `<button class="ghost small" data-act="standing" data-id="${esc(u.id)}"
          data-next="${u.standing === 'active' ? 'suspended' : 'active'}">
          ${u.standing === 'active' ? 'Suspend' : 'Reinstate'}</button>
        <button class="ghost small" data-act="delete" data-id="${esc(u.id)}" data-name="${esc(u.name)}">Delete</button>`;
  $('#users').innerHTML = users.length ? users.map((u) => `
    <tr>
      <td>${esc(u.id)}</td>
      <td>${esc(u.name)}</td>
      <td><span class="pill"><span class="dot ${u.standing === 'active' ? 'good' : 'bad'}"></span>${esc(u.standing)}</span></td>
      <td style="white-space:nowrap">${actions(u)}</td>
    </tr>`).join('') : '<tr><td colspan="4" class="muted">Nobody is in this kiosk\'s snapshot yet.</td></tr>';
}

$('#users').addEventListener('click', async (ev) => {
  const btn = ev.target.closest('button');
  if (!btn) return;
  try {
    if (btn.dataset.act === 'standing') {
      await api(`/api/users/${btn.dataset.id}`, { method: 'PATCH', body: { standing: btn.dataset.next } });
    } else if (confirm(`Delete ${btn.dataset.name} (${btn.dataset.id})? This removes their face embedding.`)) {
      await api(`/api/users/${btn.dataset.id}`, { method: 'DELETE' });
    }
    await loadUsers();
  } catch (e) {
    $('#message').textContent = e.message;
  }
});

$('#enrol-form').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const name = $('#name').value.trim();
  if (!name) return;
  if (central.configured && !/^\d{4,8}$/.test($('#pin').value.trim())) {
    $('#message').textContent = 'Choose a 4 to 8 digit PIN. Offline, the kiosk asks for it with a blink.';
    return;
  }
  busy = true;
  refreshControls();
  $('#hint').textContent = 'Hold still, look at the camera…';
  try {
    const frames = await captureBurst(video, 5, 2000);
    const pin = $('#pin').value.trim();
    const body = { name, frames };
    if (pin) body.pin = pin;
    const r = await api('/api/enrol', { body });
    $('#message').textContent = central.configured
      ? `Enrolled ${name} as ${r.user_id} on central (rule: this kiosk's door, 07:00 to 22:00). Snapshot pulled.`
      : `Enrolled ${name} as ${r.user_id} (local only: no central service configured).`;
    $('#name').value = '';
    $('#pin').value = '';
    await loadUsers();
  } catch (e) {
    $('#message').textContent = `Not enrolled: ${e.message}`;
  }
  busy = false;
  refreshControls();
});

(async () => {
  showStatus();
  try {
    const s = await api('/api/status');
    central = { configured: s.central_configured, url: s.central_url };
    $('#pin').required = central.configured;
  } catch (e) { /* shown by showStatus */ }
  loadUsers().catch((e) => { $('#message').textContent = e.message; });
  try {
    await startCamera(video);
    cameraOn = true;
    startFaceGate(video, (state) => { gate = state; refreshControls(); });
  } catch (e) {
    $('#hint').textContent = 'Camera unavailable';
    $('#message').textContent = `Camera unavailable: ${e.message}`;
  }
  refreshControls();
})();
