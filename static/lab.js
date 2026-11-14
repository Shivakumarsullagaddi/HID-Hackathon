// Spoof lab: record labelled clips and run each one through the pipeline.
const video = $('#video');
const results = {};  // clip name -> last run

async function loadClips() {
  const clips = await api('/api/lab/clips');
  $('#count').textContent = `(${clips.length})`;
  $('#clips').innerHTML = clips.length ? clips.map((c) => {
    const run = results[c.name];
    const actual = run
      ? `${decisionPill(run.actual)} <span class="pill"><span class="dot ${run.correct ? 'good' : 'bad'}"></span>` +
        `${run.correct ? 'as expected' : 'MISMATCH'}</span>` +
        `<div class="muted" style="font-size:12px">${esc(run.result.reason)} · live ${run.result.liveness.toFixed(2)}` +
        ` · sim ${run.result.similarity.toFixed(1)} · ${run.result.latency_ms.toFixed(0)} ms</div>`
      : '<span class="muted">not run</span>';
    return `<tr>
      <td>${esc(c.name)}${c.frames < 3 ? ' <span class="muted">(frames missing)</span>' : ''}</td>
      <td>${esc(c.type)}</td>
      <td>${esc(c.person)}${c.enrolled ? '' : ' <span class="muted">(not enrolled)</span>'}</td>
      <td>${esc(c.lighting)}</td>
      <td>${decisionPill(c.expected)}</td>
      <td>${actual}</td>
      <td style="white-space:nowrap">
        <button class="small" data-act="run" data-name="${esc(c.name)}">Run</button>
        <button class="ghost small" data-act="delete" data-name="${esc(c.name)}">Delete</button>
      </td></tr>`;
  }).join('') : '<tr><td colspan="7" class="muted">No clips yet. Record one above.</td></tr>';
}

$('#clips').addEventListener('click', async (ev) => {
  const btn = ev.target.closest('button');
  if (!btn) return;
  const name = btn.dataset.name;
  btn.disabled = true;
  try {
    if (btn.dataset.act === 'run') {
      results[name] = await api(`/api/lab/clips/${encodeURIComponent(name)}/run`, { method: 'POST' });
    } else if (confirm(`Delete clip ${name}?`)) {
      await api(`/api/lab/clips/${encodeURIComponent(name)}`, { method: 'DELETE' });
      delete results[name];
    }
  } catch (e) {
    $('#message').textContent = e.message;
  }
  await loadClips();
});

$('#clip-form').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  $('#record').disabled = true;
  $('#message').textContent = 'Recording…';
  try {
    const frames = await captureBurst(video, 5, 500);
    const clip = await api('/api/lab/clips', { body: {
      type: $('#type').value,
      person: $('#person').value,
      enrolled: $('#enrolled').value === 'true',
      lighting: $('#lighting').value,
      expected: $('#expected').value || null,
      frames,
    } });
    $('#message').textContent = `Saved ${clip.name} (expected ${clip.expected}).`;
    await loadClips();
  } catch (e) {
    $('#message').textContent = `Not saved: ${e.message}`;
  }
  $('#record').disabled = false;
});

(async () => {
  showStatus();
  loadClips().catch((e) => { $('#message').textContent = e.message; });
  try {
    await startCamera(video);
    $('#hint').textContent = '';
    $('#record').disabled = false;
  } catch (e) {
    $('#hint').textContent = 'Camera unavailable';
    $('#message').textContent = `Camera unavailable: ${e.message}`;
  }
})();
