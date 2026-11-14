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

const ROLES = ['student', 'faculty', 'contractor', 'security_admin'];

async function loadUsers() {
  const users = await api('/api/users');
  $('#users').innerHTML = users.length ? users.map((u) => {
    const roleVal = u.role || 'student';
    const roleOptions = ROLES.map((r) =>
      `<option value="${r}" ${r === roleVal ? 'selected' : ''}>${r}</option>`
    ).join('');
    return `
    <tr>
      <td>${esc(u.id)}</td>
      <td>${esc(u.name)}</td>
      <td>
        <select class="role-select" data-id="${esc(u.id)}" style="padding:3px 6px;font-size:12px;">
          ${roleOptions}
        </select>
      </td>
      <td><span class="pill"><span class="dot ${u.standing === 'active' ? 'good' : 'bad'}"></span>${esc(u.standing)}</span></td>
      <td style="white-space:nowrap">
        <button class="ghost small" data-act="standing" data-id="${esc(u.id)}"
          data-next="${u.standing === 'active' ? 'suspended' : 'active'}">
          ${u.standing === 'active' ? 'Suspend' : 'Reinstate'}</button>
        <button class="ghost small" data-act="delete" data-id="${esc(u.id)}" data-name="${esc(u.name)}">Delete</button>
      </td>
    </tr>`;
  }).join('') : '<tr><td colspan="5" class="muted">Nobody is enrolled yet.</td></tr>';
}

$('#users').addEventListener('change', async (ev) => {
  const sel = ev.target.closest('.role-select');
  if (!sel) return;
  const userId = sel.dataset.id;
  const newRole = sel.value;
  try {
    await api(`/api/users/${encodeURIComponent(userId)}`, { method: 'PATCH', body: { role: newRole } });
    $('#message').textContent = `Updated role for ${userId} to ${newRole}.`;
  } catch (e) {
    $('#message').textContent = `Role update failed: ${e.message}`;
    await loadUsers();
  }
});

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
  const role = $('#role-select') ? $('#role-select').value : 'student';
  if (!name) return;
  busy = true;
  refreshControls();
  $('#hint').textContent = 'Hold still, look at the camera…';
  try {
    const frames = await captureBurst(video, 5, 2000);
    const r = await api('/api/enrol', { body: { name, frames, role } });
    $('#message').textContent = `Enrolled ${name} as ${r.user_id} (${role}).`;
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
