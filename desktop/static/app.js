'use strict';
const $ = id => document.getElementById(id);
const token = document.querySelector('meta[name="aria-session"]').content;
const fields = ['device', 'distro', 'linux_user', 'sdk_path', 'aria_serial', 'profile'];
const cameras = ['rgb', 'slam1', 'slam2'];
let state = null, tab = 'live', dirty = false, initialized = false, operationPending = false, settingsRevision = 0;
const imageUrls = {}, history = {'0': [], '1': []}, imuTimestamps = {};

async function api(path, body) {
  const headers = {'X-Aria-Token': token};
  const options = {headers, cache: 'no-store', signal: AbortSignal.timeout(8000)};
  if (body !== undefined) {
    headers['Content-Type'] = 'application/json'; options.method = 'POST'; options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  if (!response.ok) {
    let message = 'Request failed (' + response.status + ')';
    try { message = (await response.json()).error || message; } catch (_) {}
    throw new Error(message);
  }
  return response;
}

function settings() {
  return Object.fromEntries(fields.map(key => [key, $('config-' + key).value.trim()]));
}

function feedback(message, error = false) {
  $('job-feedback').textContent = message;
  $('job-feedback').className = 'alert' + (error ? ' error' : '') + (message ? '' : ' hidden');
}

async function action(name, extra = {}) {
  if (operationPending || (state && state.job.running)) return;
  operationPending = true;
  const revision = settingsRevision;
  document.querySelectorAll('[data-action], form button').forEach(button => button.disabled = true);
  feedback('Working…');
  try {
    await api('/api/action/' + name, name === 'stop' ? {} : {config: settings(), ...extra});
    if (!['stop', 'pair'].includes(name) && revision === settingsRevision) dirty = false;
  } catch (error) {
    feedback(error.message, true);
  } finally {
    operationPending = false;
  }
  await poll();
}

function openTab(name) {
  tab = name;
  document.querySelectorAll('[data-tab]').forEach(button => button.classList.toggle('active', button.dataset.tab === name));
  document.querySelectorAll('.view').forEach(view => view.classList.toggle('hidden', view.id !== 'view-' + name));
  if (name === 'live') ['0', '1'].forEach(drawChart);
}
document.querySelectorAll('[data-tab]').forEach(button => button.addEventListener('click', () => openTab(button.dataset.tab)));
document.querySelectorAll('[data-action]').forEach(button => button.addEventListener('click', () => action(button.dataset.action)));
fields.forEach(key => $('config-' + key).addEventListener('input', () => {dirty = true; settingsRevision++;}));
$('settings-form').addEventListener('submit', event => {event.preventDefault(); action('save');});
$('connect-form').addEventListener('submit', event => {event.preventDefault(); action('connect', {address: $('connect-address').value});});
$('pair-form').addEventListener('submit', event => {
  event.preventDefault(); const code = $('pair-code').value;
  $('pair-code').value = ''; action('pair', {address: $('pair-address').value, code});
});
$('export-diagnostics').addEventListener('click', async () => {
  try {
    const response = await api('/api/diagnostics');
    const url = URL.createObjectURL(await response.blob());
    const link = document.createElement('a'); link.href = url; link.download = 'aria-relay-diagnostics.json'; link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  } catch (error) {feedback(error.message, true);}
});

function element(tag, className, text) {
  const node = document.createElement(tag); if (className) node.className = className;
  if (text !== undefined) node.textContent = text; return node;
}

let devicesKey = '', checksKey = '', discoveryKey = '', logKey = '';
function renderSetup(s) {
  const key = JSON.stringify([s.devices, s.config.device, s.job.running]);
  if (key !== devicesKey) {
    devicesKey = key; const list = $('devices'); list.replaceChildren();
    if (!s.devices.length) list.append(element('p', 'muted', 'No devices listed yet. Find devices, or connect using the address below.'));
    for (const device of s.devices) {
      const row = element('div', 'device' + (device.id === s.config.device ? ' selected' : ''));
      const label = element('div'); label.append(element('b', '', device.model), element('span', '', device.id + ' · ' + device.state));
      const button = element('button', 'button small secondary', device.id === s.config.device ? 'Selected' : 'Use phone');
      button.disabled = device.state !== 'device' || s.job.running;
      button.addEventListener('click', () => {$('config-device').value = device.id; action('save');});
      row.append(label, button); list.append(row);
    }
  }
  const foundKey = JSON.stringify(s.discovered);
  if (foundKey !== discoveryKey) {
    discoveryKey = foundKey; $('discovered').replaceChildren();
    for (const device of s.discovered) {
      const line = element('div'); const button = element('button', 'button small secondary', 'Use ' + device.kind + ' address');
      button.addEventListener('click', () => {
        $(device.kind === 'pairing' ? 'pair-address' : 'connect-address').value = device.address;
        if (device.kind === 'pairing') $('pair-form').closest('details').open = true;
      });
      line.append(document.createTextNode(device.address + ' '), button); $('discovered').append(line);
    }
  }
  const checkKey = JSON.stringify(s.checks);
  if (checkKey !== checksKey && s.checks.checks.length) {
    checksKey = checkKey; $('checks').replaceChildren();
    for (const check of s.checks.checks) {
      const row = element('div', 'check ' + (['pass', 'warn', 'fail'].includes(check.status) ? check.status : 'warn'));
      const text = element('div'); text.append(element('b', '', check.label), element('p', '', check.detail));
      row.append(element('span', 'check-icon', check.status === 'pass' ? '✓' : '!'), text); $('checks').append(row);
    }
  }
  $('distro-list').replaceChildren(...s.distros.map(name => {const option = document.createElement('option'); option.value = name; return option;}));
}

function ageLabel(id, age) {
  $(id).textContent = age == null ? 'No samples' : (age >= 3000 ? 'Paused · ' : '') + (age / 1000).toFixed(1) + 's ago';
  $(id).className = 'age ' + (age == null ? '' : age >= 3000 ? 'late' : 'fresh');
}
function number(n) {return Number(n || 0).toLocaleString();}
function duration(seconds) {
  const total = Math.floor(seconds || 0); return String(Math.floor(total / 3600)).padStart(2, '0') + ':' + String(Math.floor(total / 60) % 60).padStart(2, '0') + ':' + String(total % 60).padStart(2, '0');
}
function vector(value) {return value ? value.map(v => Number.isFinite(v) ? v.toFixed(3).padStart(7) : '      —').join('  ') : 'unavailable';}

function render(s) {
  const first = !initialized;
  if (state && state.preview && s.preview && s.preview.uptime_seconds < state.preview.uptime_seconds) {
    history['0'] = []; history['1'] = []; delete imuTimestamps['0']; delete imuTimestamps['1'];
  }
  state = s;
  if (!dirty && !s.job.running && !operationPending) fields.forEach(key => $('config-' + key).value = s.config[key]);
  if (first) {
    fields.forEach(key => $('config-' + key).value = s.config[key]);
    $('connect-address').value = s.config.device.includes(':') ? s.config.device : '';
    initialized = true;
    document.querySelectorAll('.windows-only').forEach(node => node.classList.toggle('hidden', s.platform !== 'windows'));
    $('platform-label').textContent = s.platform === 'windows' ? 'WINDOWS + WSL WORKSPACE' : 'LINUX WORKSPACE';
    $('setup-command').textContent = s.platform === 'windows'
      ? 'In your WSL distribution, from the repository:\n\nbash scripts/setup-wsl.sh\n\nThen use the official aria auth pair flow with Aria Companion.'
      : 'From a terminal in the repository:\n\nbash scripts/setup-wsl.sh\nbash scripts/open-dashboard.sh\n\nThen use the official aria auth pair flow with Aria Companion.';
    $('sudo-help').textContent = s.platform === 'linux' ? 'USB import needs administrative access. The Linux dashboard launcher requests sudo visibly in your terminal. If it expires, stop and relaunch the dashboard there.' : 'Use WSL2 mirrored networking. Setup checks report the required USB kernel drivers and installed SDK.';
  }
  const p = s.preview, live = !!(p && p.receiving), paused = !!p && !live;
  const label = live ? 'Receiving glasses data' : paused ? 'Waiting for sensor data' : s.session_running ? 'Starting connection' : 'Ready to connect';
  $('top-status').className = 'connection-pill' + (live ? ' live' : paused ? ' paused' : '');
  $('top-status').lastElementChild.textContent = label;
  $('glasses-path').textContent = live ? 'Connected over USB' : p ? 'Stream active · no fresh data' : 'Waiting for glasses';
  $('phone-path').textContent = s.config.device || 'Choose a phone in setup';
  $('decoder-path').textContent = p ? 'Aria SDK ' + (p.sdk_version || '') : s.session_running ? 'Preparing decoder…' : 'Decoder ready when you are';
  const busy = s.job.running || operationPending;
  document.querySelectorAll('[data-action], form button').forEach(button => button.disabled = busy);
  $('start').disabled = busy || !s.config.device || !!p || s.session_running;
  $('stop').disabled = busy || (!p && !s.session_running);
  if (s.job.action || (p && p.error)) {
    const error = s.job.error || (p && p.error);
    feedback(s.job.running ? 'Working: ' + s.job.action + '…' : error || (live && s.job.action === 'start' ? '' : s.job.message), !!error);
  }
  $('session-time').textContent = p ? duration(p.uptime_seconds) : '—';
  $('frame-count').textContent = number(p && p.image_count); $('imu-count').textContent = number(p && p.imu_sample_count);
  $('stream-phase').textContent = live ? 'Live' : paused ? 'Paused' : s.session_running ? 'Connecting' : s.session_exit && ![130,143].includes(s.session_exit) ? 'Needs attention' : 'Ready';
  for (const id of cameras) {
    const frame = p && p.cameras && p.cameras[id];
    ageLabel(id + '-age', frame ? frame.age_ms : null);
    $('frame-' + id).classList.toggle('stale', !frame || frame.age_ms >= 3000);
    if (id === 'rgb') $('rgb-size').textContent = frame ? frame.width + ' × ' + frame.height + ' · RGB' : 'RGB · waiting for frames';
  }
  for (const id of ['0', '1']) {
    const sample = p && p.imu && p.imu[id];
    ageLabel('imu' + id + '-age', sample ? sample.age_ms : null);
    $('imu' + id + '-values').textContent = sample ? 'ACC  ' + vector(sample.acceleration_m_s2) + '  m/s²\nGYR  ' + vector(sample.angular_velocity_rad_s) + '  rad/s' : 'Waiting for motion data';
    if (sample && sample.age_ms < 3000 && sample.capture_timestamp_ns !== imuTimestamps[id]) {
      imuTimestamps[id] = sample.capture_timestamp_ns;
      history[id].push(sample.acceleration_m_s2 || [null, null, null]);
      if (history[id].length > 90) history[id].shift();
    }
    if (tab === 'live') drawChart(id);
  }
  renderSetup(s);
  const logs = s.logs.map(line => line.time + '  ' + line.text).join('\n');
  if (logs !== logKey) {logKey = logs; $('event-log').textContent = logs || 'No events yet.'; $('event-log').scrollTop = $('event-log').scrollHeight;}
}

function drawChart(id) {
  const canvas = $('imu' + id + '-chart'), width = canvas.clientWidth - 40;
  if (width <= 0) return;
  const ratio = window.devicePixelRatio || 1, height = 75;
  canvas.width = Math.round(width * ratio); canvas.height = Math.round(height * ratio);
  const ctx = canvas.getContext('2d'); ctx.scale(ratio, ratio);
  ctx.strokeStyle = '#eaf0e4'; ctx.lineWidth = 1;
  for (let y = 15; y <= 60; y += 15) {ctx.beginPath(); ctx.moveTo(0,y); ctx.lineTo(width,y); ctx.stroke();}
  const samples = history[id], range = Math.max(12, ...samples.flat().filter(Number.isFinite).map(Math.abs)) * 1.1;
  ['#599b7a', '#d3a36f', '#849fbd'].forEach((color, axis) => {
    ctx.strokeStyle = color; ctx.lineWidth = 1.4; ctx.beginPath(); let started = false;
    samples.forEach((sample, index) => {
      if (!Number.isFinite(sample[axis])) {started = false; return;}
      const x = index * width / 89, y = height / 2 - sample[axis] / range * (height / 2 - 5);
      if (started) ctx.lineTo(x,y); else ctx.moveTo(x,y); started = true;
    }); ctx.stroke();
  });
}

let polling = false;
async function poll() {
  if (polling) return;
  polling = true;
  try {const response = await api('/api/status'); render(await response.json()); $('connection-warning').classList.add('hidden');}
  catch (_) {$('connection-warning').classList.remove('hidden'); cameras.forEach(id => $('frame-' + id).classList.add('stale'));}
  finally {polling = false;}
}

async function loadCamera(id) {
  const frame = state && state.preview && state.preview.cameras && state.preview.cameras[id];
  if (tab !== 'live' || document.hidden || !frame || frame.age_ms >= 3000) return;
  try {
    const response = await api('/api/frame/' + id); const blob = await response.blob();
    if (blob.type !== 'image/jpeg') return;
    const old = imageUrls[id], url = URL.createObjectURL(blob); imageUrls[id] = url;
    const img = $('img-' + id); img.onload = () => {if (old) URL.revokeObjectURL(old); $('frame-' + id).classList.add('has-frame');};
    img.src = url;
  } catch (_) {/* The status loop exposes a paused or disconnected stream. */}
}
async function cameraLoop() {await Promise.all(cameras.map(loadCamera)); setTimeout(cameraLoop, 180);}
window.addEventListener('resize', () => ['0','1'].forEach(drawChart));
poll().then(() => {if (!state || !state.config.device) openTab('setup');});
setInterval(poll, 1000); cameraLoop();
