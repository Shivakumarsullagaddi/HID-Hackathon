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

async function loadUsers() {
  const users = await api('/api/users');
  $('#users').innerHTML = users.length ? users.map((u) => `
    <tr>
      <td>${esc(u.id)}</td>
      <td>${esc(u.name)}</td>
      <td><span class="pill"><span class="dot ${u.standing === 'active' ? 'good' : 'bad'}"></span>${esc(u.standing)}</span></td>
      <td style="white-space:nowrap">
        <button class="ghost small" data-act="standing" data-id="${esc(u.id)}"
          data-next="${u.standing === 'active' ? 'suspended' : 'active'}">
          ${u.standing === 'active' ? 'Suspend' : 'Reinstate'}</button>
        <button class="ghost small" data-act="delete" data-id="${esc(u.id)}" data-name="${esc(u.name)}">Delete</button>
      </td>
    </tr>`).join('') : '<tr><td colspan="4" class="muted">Nobody is enrolled yet.</td></tr>';
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
  busy = true;
  refreshControls();
  $('#hint').textContent = 'Hold still, look at the camera…';
  try {
    const frames = await captureBurst(video, 5, 2000);
    const r = await api('/api/enrol', { body: { name, frames } });
    $('#message').textContent = `Enrolled ${name} as ${r.user_id}.`;
    $('#name').value = '';
    await loadUsers();
  } catch (e) {
    $('#message').textContent = `Not enrolled: ${e.message}`;
  }
  busy = false;
  refreshControls();
});

(async () => {
  showStatus();
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
