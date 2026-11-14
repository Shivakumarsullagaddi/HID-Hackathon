// Metrics page: harness table, decision chart, thresholds, kiosk log.
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
let chart = null;

const pct = (rate, num, den) => (rate == null
  ? '<span class="muted">n/a</span>'
  : `${(rate * 100).toFixed(1)}% <span class="muted">(${num}/${den})</span>`);
const ms = (v) => (v == null ? 'n/a' : v.toFixed(0));

function tile(value, label, note) {
  return `<div class="tile"><div class="v">${value}</div><div class="l">${label}</div><div class="n">${note}</div></div>`;
}

function typeRow(name, m, bold) {
  const b = (x) => (bold ? `<b>${x}</b>` : x);
  return `<tr>
    <td>${b(esc(name))}</td>
    <td class="num">${m.n}</td>
    <td class="num">${pct(m.accuracy, m.correct, m.n)}</td>
    <td class="num">${pct(m.far, m.false_accepts, m.n_spoof)}</td>
    <td class="num">${pct(m.frr, m.false_rejects, m.n_genuine)}</td>
    <td class="num">${m.decisions.REQUEST_VERIFICATION}</td>
    <td class="num">${ms(m.p50_ms)}</td>
    <td class="num">${ms(m.p95_ms)}</td></tr>`;
}

function drawChart(h) {
  if (typeof Chart === 'undefined') return;
  const types = Object.keys(h.by_type);
  const series = [
    ['ALLOW', 'ALLOW', css('--good')],
    ['REQUEST_VERIFICATION', 'Verification needed', css('--warn')],
    ['DENY', 'DENY', css('--bad')],
  ];
  if (chart) chart.destroy();
  chart = new Chart($('#chart'), {
    type: 'bar',
    data: {
      labels: types.map((t) => `${t} (n=${h.by_type[t].n})`),
      datasets: series.map(([key, label, color]) => ({
        label,
        data: types.map((t) => h.by_type[t].decisions[key]),
        backgroundColor: color,
        borderColor: css('--surface'),   // 2px surface gap between segments
        borderWidth: 2,
        borderRadius: 4,
        maxBarThickness: 26,
      })),
    },
    options: {
      indexAxis: 'y',
      maintainAspectRatio: false,
      scales: {
        x: { stacked: true, beginAtZero: true, title: { display: true, text: 'Clips', color: css('--text-2') },
          ticks: { precision: 0, color: css('--text-2') }, grid: { color: css('--line') }, border: { display: false } },
        y: { stacked: true, ticks: { color: css('--text') }, grid: { display: false }, border: { color: css('--line') } },
      },
      plugins: {
        legend: { position: 'bottom', labels: { color: css('--text-2'), boxWidth: 12, boxHeight: 12 } },
        tooltip: { mode: 'index', axis: 'y', intersect: false },
      },
    },
  });
}

function render(data) {
  const t = data.thresholds;
  $('#thresholds').innerHTML = `
    <dt>Liveness to allow</dt><dd>&ge; ${t.liveness_allow}</dd>
    <dt>Liveness treated as spoof</dt><dd>&lt; ${t.liveness_deny}</dd>
    <dt>Similarity to allow</dt><dd>&ge; ${t.similarity_allow}</dd>
    <dt>Similarity to count as enrolled</dt><dd>&ge; ${t.match_min_similarity}</dd>
    <dt>Liveness floor after a blink step-up</dt><dd>&ge; ${t.stepup_liveness_min}</dd>
    <dt>Brightness (mean grey)</dt><dd>${t.brightness_min} to ${t.brightness_max}</dd>
    <dt>Sharpness (Laplacian variance)</dt><dd>&gt; ${t.sharpness_min}</dd>`;

  const k = data.kiosk_log;
  const counts = (obj) => Object.entries(obj || {})
    .map(([key, n]) => `${esc(DECISION_LABEL[key] || key)} ${n}`).join(' · ') || 'none';
  $('#kiosk').innerHTML = `
    <dt>Verify attempts</dt><dd>${k.n}</dd>
    <dt>Access decisions</dt><dd>${counts(k.final_decisions)}</dd>
    <dt>Biometric results</dt><dd>${counts(k.decisions)}</dd>
    <dt>Step-up outcomes</dt><dd>${counts(k.stepups)}</dd>
    <dt>Liveness mode</dt><dd>${counts(k.liveness_modes)}</dd>
    <dt>Server latency</dt><dd>p50 ${ms(k.p50_ms)} ms · p95 ${ms(k.p95_ms)} ms</dd>`;

  const h = data.harness;
  if (!h || !h.clips.length) {
    $('#conditions').textContent = 'No harness run yet. Record clips in the spoof lab, then press Run harness.';
    $('#tiles').innerHTML = '';
    $('#by-type').innerHTML = '<tr><td colspan="8" class="muted">No results.</td></tr>';
    $('#misses').innerHTML = '<tr><td colspan="6" class="muted">No results.</td></tr>';
    return;
  }
  const c = h.conditions;
  const o = h.overall;
  $('#conditions').textContent =
    `${o.n} clips · people: ${c.people.join(', ')} · lighting: ${c.lighting.join(', ')}` +
    ` · liveness mode: ${h.liveness_mode} · run ${new Date(h.ran_at).toLocaleString()}`;
  const missing = $('#missing');
  missing.hidden = !c.spoof_types_missing.length && !h.skipped.length;
  missing.innerHTML =
    (c.spoof_types_missing.length ? `<b>Not tested:</b> ${esc(c.spoof_types_missing.join(', '))} spoofs. No claim is made about them. ` : '') +
    (h.skipped.length ? `<b>Skipped:</b> ${esc(h.skipped.map((s) => s.name).join(', '))} (frames missing).` : '');

  const rate = (r) => (r == null ? 'n/a' : `${(r * 100).toFixed(1)}%`);
  $('#tiles').innerHTML =
    tile(rate(o.accuracy), 'Accuracy', `${o.correct} of ${o.n} clips`) +
    tile(rate(o.far), 'False acceptance rate', `${o.false_accepts} of ${o.n_spoof} spoof clips`) +
    tile(rate(o.frr), 'False rejection rate', `${o.false_rejects} of ${o.n_genuine} live enrolled clips`) +
    tile(ms(o.p50_ms), 'p50 latency (ms)', `${o.n} clips`) +
    tile(ms(o.p95_ms), 'p95 latency (ms)', 'limit 2000');

  $('#by-type').innerHTML = Object.entries(h.by_type).map(([name, m]) => typeRow(name, m)).join('') +
    typeRow('All', o, true);
  const misses = h.clips.filter((r) => !r.correct);
  $('#misses').innerHTML = misses.length ? misses.map((r) => `<tr>
    <td>${esc(r.name)}</td><td>${decisionPill(r.expected)}</td><td>${decisionPill(r.actual)}</td>
    <td>${esc(r.reason)}</td><td class="num">${r.liveness.toFixed(2)}</td><td class="num">${r.similarity.toFixed(1)}</td>
    </tr>`).join('') : '<tr><td colspan="6" class="muted">Every clip matched its expected decision.</td></tr>';
  drawChart(h);
}

function renderSync(st) {
  const ls = st.last_sync;
  const pushed = ls ? `${ls.push.accepted} accepted, ${ls.push.duplicates} duplicates` : 'none yet';
  $('#sync').innerHTML = `
    <dt>State</dt><dd>${stateBadge(st)} <span class="muted">${esc(st.reason)}</span></dd>
    <dt>Outbox</dt><dd>${st.outbox.total} rows, ${st.outbox.unsynced} not yet synced</dd>
    <dt>Last sync</dt><dd>${st.last_sync_at ? esc(new Date(st.last_sync_at).toLocaleString()) : 'never'}
      <span class="muted">(last push: ${esc(pushed)})</span></dd>
    <dt>Last error</dt><dd>${esc(st.last_error || 'none')}</dd>
    <dt>Snapshot</dt><dd>${st.snapshot_version == null ? 'none'
      : `v${st.snapshot_version}, issued ${esc(new Date(st.snapshot_issued_at).toLocaleString())}, TTL ${st.ttl_s} s`}</dd>
    ${st.warnings.length ? `<dt>Warnings</dt><dd>${esc(st.warnings.join('; '))}</dd>` : ''}`;
  $('#history').innerHTML = st.history.length ? st.history.map((h) => `<tr>
    <td>${esc(new Date(h.at).toLocaleString())}</td><td>${esc(h.from || 'start')}</td>
    <td><span class="pill"><span class="dot ${STATE_CLASS[h.to] || ''}"></span>${esc(h.to)}</span></td>
    <td>${esc(h.reason)}</td></tr>`).join('')
    : '<tr><td colspan="4" class="muted">No state changes yet.</td></tr>';
}

async function load() {
  render(await api('/api/metrics'));
  renderSync(await api('/api/state'));
}
setInterval(() => api('/api/state').then(renderSync).catch(() => {}), 3000);

$('#run').addEventListener('click', async () => {
  const btn = $('#run');
  btn.disabled = true;
  btn.textContent = 'Running…';
  try {
    await api('/api/evaluate', { method: 'POST' });
    await load();
  } catch (e) {
    $('#conditions').textContent = `Harness failed: ${e.message}`;
  }
  btn.disabled = false;
  btn.textContent = 'Run harness';
});

showStatus();
load().catch((e) => { $('#conditions').textContent = e.message; });
