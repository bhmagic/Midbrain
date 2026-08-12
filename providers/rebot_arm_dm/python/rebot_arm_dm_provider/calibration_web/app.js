'use strict';

const state = {
  model: null,
  arm: null,
  lease: null,
  deadman: false,
  activeRow: null,
  activePointerId: null,
  commandSending: false,
  releaseInProgress: false,
  view: 'iso',
};

const $ = id => document.getElementById(id);
const deg = radians => radians * 180 / Math.PI;
const rad = degrees => degrees * Math.PI / 180;

async function api(path, method = 'GET', body = null) {
  const response = await fetch(path, {
    method,
    headers: {'Content-Type': 'application/json'},
    body: body ? JSON.stringify(body) : null,
    keepalive: method === 'POST',
  });
  const data = await response.json();
  if (!response.ok || data.error) throw new Error(data.error || response.statusText);
  return data;
}

function badge(id, text, kind = '') {
  const element = $(id);
  element.textContent = text;
  element.className = `badge ${kind}`;
}

function fmt(value, digits = 3) {
  return Number.isFinite(Number(value)) ? Number(value).toFixed(digits) : '—';
}

async function initialize() {
  state.model = await api('/api/model');
  $('model-view').textContent = JSON.stringify(state.model, null, 2);
  buildJointRows();
  bind();
  await poll();
  setInterval(poll, 100);
  setInterval(renewLease, 350);
  setInterval(refreshManualCommand, 50);
}

function buildJointRows() {
  const host = $('joint-table');
  host.innerHTML = '<div class="joint-row header"><span>Joint</span><span>Mode</span><span>Target</span><span>Test min °</span><span>Test max °</span><span>Slider</span><span>kp</span><span>kd</span><span>V limit</span><span>Torque ratio</span><span>Limits / state</span></div>';
  state.model.joints.forEach((joint, index) => {
    const defaults = joint.default_test;
    const limits = joint.hard_limit_rad;
    const caps = joint.provider_test_caps;
    const motorLimits = joint.motor_limits;
    const row = document.createElement('div');
    row.className = 'joint-row';
    row.dataset.index = index;
    row.dataset.targetInitialized = 'false';
    row.innerHTML = `<strong>${joint.name}</strong>
      <select class="mode"><option>IMPEDANCE</option><option>POSITION_VELOCITY_LIMITED</option><option>POSITION_EFFORT_LIMITED</option></select>
      <span class="joint-value target-value">0.000 rad</span>
      <input class="cal-min" type="number" step="1" value="${deg(joint.default_calibration_range_rad[0]).toFixed(1)}">
      <input class="cal-max" type="number" step="1" value="${deg(joint.default_calibration_range_rad[1]).toFixed(1)}">
      <input class="target" type="range" min="${deg(joint.default_calibration_range_rad[0])}" max="${deg(joint.default_calibration_range_rad[1])}" step="0.1" value="${deg(joint.home_position_rad)}" disabled aria-label="${joint.name} target angle">
      <input class="kp" type="number" min="${caps.min_kp ?? defaults.kp}" max="${caps.max_kp}" step="0.5" value="${defaults.kp}">
      <input class="kd" type="number" min="0" max="${caps.max_kd}" step="0.1" value="${Math.min(defaults.kd, caps.max_kd)}">
      <input class="vlim" type="number" min="0.01" step="0.01" value="${defaults.velocity_limit_rad_s}">
      <input class="ratio" type="number" min="0" max="1" step="0.01" value="${defaults.torque_limit_ratio}">
      <span class="limits">${joint.motor_model} ${joint.motor_revision} · hard ${deg(limits[0]).toFixed(0)}°…${deg(limits[1]).toFixed(0)}°<br>FORCE_POS 1.0 = configured TMAX ${motorLimits.configured_tmax_nm} N·m; rated ${motorLimits.manufacturer_rated_torque_nm} N·m; listed peak ${motorLimits.manufacturer_peak_torque_nm} N·m<br>Official/Unity MIT kp=${defaults.kp}, kd=${defaults.kd}; tracking-effort limit ${caps.mit_tracking_effort_limit_nm} N·m<br>Load-bearing MIT rule kp≥${caps.min_kp ?? defaults.kp}; kd may be low. Reviewed caps kp≤${caps.max_kp}, kd≤${caps.max_kd}<br><span class="live">waiting</span></span>`;
    host.appendChild(row);

    const minimum = row.querySelector('.cal-min');
    const maximum = row.querySelector('.cal-max');
    const slider = row.querySelector('.target');
    const updateRange = () => {
      const hardMinimum = deg(limits[0]);
      const hardMaximum = deg(limits[1]);
      const low = Math.max(hardMinimum + 1, Number(minimum.value));
      let high = Math.min(hardMaximum - 1, Number(maximum.value));
      if (low >= high) high = low + 1;
      minimum.value = low;
      maximum.value = high;
      slider.min = low;
      slider.max = high;
      slider.value = Math.min(high, Math.max(low, Number(slider.value)));
    };
    minimum.addEventListener('change', updateRange);
    maximum.addEventListener('change', updateRange);
    slider.addEventListener('focus', () => { state.activeRow = row; });
    slider.addEventListener('pointerdown', event => {
      if (!state.lease) {
        showError(new Error('Enable attended control before moving a slider.'));
        return;
      }
      if (state.deadman) return;
      state.activeRow = row;
      state.activePointerId = event.pointerId;
      state.deadman = true;
      row.dataset.dragging = 'true';
      updateManualStatus();
    });
    slider.addEventListener('input', () => {
      state.activeRow = row;
      row.querySelector('.target-value').textContent = `${rad(slider.value).toFixed(3)} rad`;
      if (state.deadman && state.activeRow === row) sendRow(row).catch(showError);
    });
  });
}

function bind() {
  document.querySelectorAll('.tab').forEach(button => {
    button.onclick = () => {
      document.querySelectorAll('.tab,.tab-page').forEach(element => element.classList.remove('active'));
      button.classList.add('active');
      $(button.dataset.tab).classList.add('active');
    };
  });
  document.querySelectorAll('[data-view]').forEach(button => {
    button.onclick = () => { state.view = button.dataset.view; draw(); };
  });
  $('acquire-lease').onclick = acquireLease;
  $('gravity-float').onclick = () => api('/api/gravity-float', 'POST').then(() => poll()).catch(showError);
  $('safe-home').onclick = () => api('/api/safe-home', 'POST').then(() => poll()).catch(showError);
  $('reset-manual-defaults').onclick = resetManualDefaults;
  for (const eventName of ['pointerup', 'pointercancel']) {
    window.addEventListener(eventName, event => {
      if (state.deadman && (state.activePointerId === null || event.pointerId === state.activePointerId)) releaseDeadman();
    }, true);
  }
  window.addEventListener('blur', () => releaseDeadman());
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) releaseDeadman();
  });
}

async function acquireLease() {
  state.lease = await api('/api/lease', 'POST', {
    holder: 'standalone_hardware_development_gui',
    duration_ms: 800,
  });
  setManualSlidersEnabled(true);
  badge('lease-state', 'Attended control lease', 'good');
  updateManualStatus();
}

async function renewLease() {
  if (!state.lease) return;
  try {
    state.lease = await api('/api/renew', 'POST', {...state.lease, duration_ms: 800});
  } catch (_) {
    state.lease = null;
    state.deadman = false;
    state.activePointerId = null;
    state.activeRow = null;
    setManualSlidersEnabled(false);
    badge('lease-state', 'Lease lost', 'bad');
    updateManualStatus();
  }
}

function setManualSlidersEnabled(enabled) {
  document.querySelectorAll('.joint-row[data-index] .target').forEach(slider => {
    slider.disabled = !enabled;
  });
}

function updateManualStatus() {
  const status = $('manual-motion-status');
  if (state.deadman) {
    status.classList.add('active');
    status.textContent = 'MOTION ENABLED — RELEASE THE POINTER FOR GRAVITY FLOAT';
  } else {
    status.classList.remove('active');
    status.textContent = state.lease
      ? 'PRESS AND DRAG A SLIDER — RELEASE ANYWHERE FOR GRAVITY FLOAT'
      : 'ENABLE ATTENDED CONTROL TO UNLOCK THE SLIDERS';
  }
}

async function releaseDeadman() {
  if (!state.deadman || state.releaseInProgress) return;
  const row = state.activeRow;
  state.deadman = false;
  state.activePointerId = null;
  if (row) row.dataset.dragging = 'false';
  state.releaseInProgress = true;
  updateManualStatus();
  snapRowToMeasured(row);
  try {
    await api('/api/gravity-float', 'POST');
    await poll();
    snapRowToMeasured(row);
  } catch (error) {
    showError(error);
  } finally {
    state.releaseInProgress = false;
  }
}

function snapRowToMeasured(row) {
  if (!row || !state.arm?.positions_rad) return;
  const index = Number(row.dataset.index);
  const slider = row.querySelector('.target');
  const value = deg(state.arm.positions_rad[index]);
  slider.value = Math.min(Number(slider.max), Math.max(Number(slider.min), value));
  row.querySelector('.target-value').textContent = `${state.arm.positions_rad[index].toFixed(3)} rad`;
  row.dataset.targetInitialized = 'true';
}

function resetManualDefaults() {
  document.querySelectorAll('.joint-row[data-index]').forEach(row => {
    const index = Number(row.dataset.index);
    const joint = state.model.joints[index];
    const defaults = joint.default_test;
    row.querySelector('.mode').value = 'IMPEDANCE';
    row.querySelector('.cal-min').value = deg(joint.default_calibration_range_rad[0]).toFixed(1);
    row.querySelector('.cal-max').value = deg(joint.default_calibration_range_rad[1]).toFixed(1);
    row.querySelector('.kp').value = defaults.kp;
    row.querySelector('.kd').value = defaults.kd;
    row.querySelector('.vlim').value = defaults.velocity_limit_rad_s;
    row.querySelector('.ratio').value = defaults.torque_limit_ratio;
    const slider = row.querySelector('.target');
    slider.min = deg(joint.default_calibration_range_rad[0]);
    slider.max = deg(joint.default_calibration_range_rad[1]);
    snapRowToMeasured(row);
  });
}

async function refreshManualCommand() {
  if (!state.deadman || !state.activeRow || !state.lease || state.commandSending) return;
  state.commandSending = true;
  try {
    await sendRow(state.activeRow);
  } catch (error) {
    state.deadman = false;
    updateManualStatus();
    showError(error);
  } finally {
    state.commandSending = false;
  }
}

async function sendRow(row) {
  if (!state.lease) throw new Error('Acquire an attended control lease first.');
  const index = Number(row.dataset.index);
  const mode = row.querySelector('.mode').value;
  const values = {position_rad: rad(Number(row.querySelector('.target').value))};
  if (mode === 'IMPEDANCE') {
    values.velocity_rad_s = 0;
    values.target_rate_limit_rad_s = Number(row.querySelector('.vlim').value);
    values.kp = Number(row.querySelector('.kp').value);
    values.kd = Number(row.querySelector('.kd').value);
    values.feedforward_torque_nm = 0;
  } else if (mode === 'POSITION_VELOCITY_LIMITED') {
    values.velocity_limit_rad_s = Number(row.querySelector('.vlim').value);
  } else {
    values.velocity_limit_rad_s = Number(row.querySelector('.vlim').value);
    values.torque_limit_ratio = Number(row.querySelector('.ratio').value);
  }
  await api('/api/command', 'POST', {
    ...state.lease,
    timeout_ms: 150,
    commands: [{joint_index: index, mode, values}],
  });
}

async function poll() {
  try {
    state.arm = await api('/api/state');
    badge('connection', 'Provider connected', 'good');
    badge('provider-state', state.arm.provider_state || state.arm.state, state.arm.health === 'HEALTHY' ? 'good' : 'warn');
    updateTelemetry();
    draw();
    checkCurrentCollision();
  } catch (_) {
    badge('connection', 'Disconnected', 'bad');
  }
}

function updateTelemetry() {
  if (!state.arm?.positions_rad) return;
  $('telemetry').innerHTML = state.arm.positions_rad.map((position, index) =>
    `<div><strong>J${index + 1} ${fmt(position)} rad</strong>${fmt(state.arm.velocities_rad_s[index])} rad/s<br>${fmt(state.arm.torques_nm[index])} N·m</div>`
  ).join('');
  document.querySelectorAll('.joint-row[data-index]').forEach(row => {
    const index = Number(row.dataset.index);
    row.querySelector('.live').textContent = `q ${fmt(state.arm.positions_rad[index])} · v ${fmt(state.arm.velocities_rad_s[index])} · τ ${fmt(state.arm.torques_nm[index])}`;
    if (row.dataset.targetInitialized === 'false' || !state.deadman || state.activeRow !== row) snapRowToMeasured(row);
  });
}

async function checkCurrentCollision() {
  if (!state.arm?.positions_rad) return;
  try {
    const result = await api('/api/collision/check', 'POST', {
      positions_rad: state.arm.positions_rad,
      table_height_m: Number($('table-height').value),
      table_clearance_m: Number($('table-clearance').value),
    });
    badge(
      'collision-state',
      result.safe ? `margin ${fmt(result.minimum_safety_margin_m ?? result.minimum_clearance_m)} m` : `unsafe: ${result.reason}`,
      result.safe ? 'good' : 'bad',
    );
  } catch (_) {
    badge('collision-state', 'Unavailable', 'warn');
  }
}

function project(point) {
  const [x, y, z] = point;
  if (state.view === 'front') return [x, z];
  if (state.view === 'top') return [x, y];
  return [(x - y) * 0.78, z + (x + y) * 0.28];
}

function draw() {
  const canvas = $('arm-canvas');
  const context = canvas.getContext('2d');
  const dpr = window.devicePixelRatio || 1;
  const width = canvas.clientWidth;
  const height = canvas.clientHeight;
  if (canvas.width !== width * dpr || canvas.height !== height * dpr) {
    canvas.width = width * dpr;
    canvas.height = height * dpr;
  }
  context.setTransform(dpr, 0, 0, dpr, 0, 0);
  context.clearRect(0, 0, width, height);
  if (!state.arm?.kinematic_points_m) return;

  const projected = state.arm.kinematic_points_m.map(project);
  const scale = Math.min(width, height) * 0.82 / 0.8;
  const originX = width / 2;
  const originY = height * 0.78;
  const points = projected.map(([horizontal, vertical]) => [originX + horizontal * scale, originY - vertical * scale]);
  const tableY = originY - Number($('table-height').value) * scale;
  context.strokeStyle = '#f4c45e';
  context.lineWidth = 2;
  context.setLineDash([10, 8]);
  context.beginPath();
  context.moveTo(20, tableY);
  context.lineTo(width - 20, tableY);
  context.stroke();
  context.setLineDash([]);
  context.fillStyle = '#f4c45e';
  context.fillText('local desktop plane', 24, tableY - 8);
  context.lineCap = 'round';
  for (let index = 0; index < points.length - 1; index += 1) {
    context.strokeStyle = index < 3 ? '#d9d9d9' : '#8f8f8f';
    context.lineWidth = Math.max(7, 18 - index * 1.5);
    context.beginPath();
    context.moveTo(...points[index]);
    context.lineTo(...points[index + 1]);
    context.stroke();
  }
  points.forEach((point, index) => {
    context.fillStyle = index === points.length - 1 ? '#ff8c96' : '#e8e8e8';
    context.beginPath();
    context.arc(point[0], point[1], index === 0 ? 10 : 6, 0, Math.PI * 2);
    context.fill();
  });
}

function showError(error) {
  console.error(error);
  alert(error.message || String(error));
}

initialize().catch(showError);
