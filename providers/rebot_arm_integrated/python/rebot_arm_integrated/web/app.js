const $ = (id) => document.getElementById(id);
const ui = { snapshot: null, terminating: false, terminationLocked: false, formInitialized: false, lastButtons: [], safeComboStarted: 0, safeComboTriggered: false, refreshInFlight: false, teleopInFlight: false, pendingTeleop: null };

async function api(path, method = "GET", body = null, timeoutMs = 2000) {
  const options = { method, headers: {} };
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), timeoutMs);
  options.signal = controller.signal;
  if (body !== null) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  try {
    const response = await fetch(path, options);
    const text = await response.text();
    const data = text ? JSON.parse(text) : {};
    if (!response.ok) throw new Error(data.error || data.message || `${response.status} ${response.statusText}`);
    return data;
  } finally {
    window.clearTimeout(timeout);
  }
}

function showError(error) { $("fault").textContent = String(error?.message || error); }
function num(value, digits = 4) { return Number.isFinite(Number(value)) ? Number(value).toFixed(digits) : "—"; }
function vec(value) { return Array.isArray(value) ? value.map((item) => num(item, 4)).join(", ") : "—"; }
function frameText(frame) {
  if (!frame) return "—";
  return `xyz [${vec(frame.position_m)}] m | rpy [${vec(frame.rpy_rad)}] rad`;
}
function numeric(id) { return Number($(id).value); }

function applyModeConstraints() {
  const contact = $("executionMode").value === "CONTACT_WORK";
  if (contact) $("interactionMode").value = "ONE_SHOT";
  $("interactionMode").disabled = contact || Boolean(ui.snapshot?.trajectory?.active) || Boolean(ui.snapshot?.gripper?.requested_action) || ui.terminating;
}

function activeGamepad() {
  const pads = navigator.getGamepads ? navigator.getGamepads() : [];
  return Array.from(pads || []).find((pad) => pad && pad.connected) || null;
}

async function pollGamepad() {
  const pad = activeGamepad();
  if (!pad) {
    $("gamepadStatus").textContent = "No gamepad detected.";
    return;
  }
  $("gamepadStatus").textContent = `Connected: ${pad.id}`;
  const button = (index) => Boolean(pad.buttons?.[index]?.pressed);
  const rising = (index) => button(index) && !Boolean(ui.lastButtons[index]);
  const axis = (index) => Number(pad.axes?.[index] || 0);
  const payload = {
    x: axis(0),
    y: -axis(1),
    z: (button(12) ? 1 : 0) - (button(13) ? 1 : 0),
    yaw: axis(2),
    pitch: -axis(3),
    roll: (button(1) ? 1 : 0) - (button(2) ? 1 : 0),
    lb: button(4),
    gripper_open: button(5),
    gripper_close: button(7),
  };
  const viewAndMenu = button(8) && button(9);
  if (viewAndMenu) {
    if (!ui.safeComboStarted) ui.safeComboStarted = performance.now();
    if (!ui.safeComboTriggered && performance.now() - ui.safeComboStarted >= 2000) {
      ui.safeComboTriggered = true;
      requestSafeTerminate();
    }
  } else {
    ui.safeComboStarted = 0;
    ui.safeComboTriggered = false;
  }
  if (rising(3)) cycleExecutionMode();
  if (rising(6)) requestFloat();
  if (rising(10)) captureContactBaseline();
  ui.lastButtons = Array.from(pad.buttons || []).map((item) => Boolean(item?.pressed));
  ui.pendingTeleop = payload;
  flushTeleop();
}

async function flushTeleop() {
  if (ui.teleopInFlight || !ui.pendingTeleop) return;
  const payload = ui.pendingTeleop;
  ui.pendingTeleop = null;
  ui.teleopInFlight = true;
  try {
    await api("/v1/teleop", "POST", payload, 800);
  } catch (error) {
    showError(error);
  } finally {
    ui.teleopInFlight = false;
    if (ui.pendingTeleop) flushTeleop();
  }
}

async function requestFloat() {
  try { await api("/v1/float", "POST", {}, 10000); await refresh(); } catch (error) { showError(error); }
}

async function captureContactBaseline() {
  try { await api("/v1/contact-baseline", "POST", {}, 12000); await refresh(); } catch (error) { showError(error); }
}

async function requestGripper(action) {
  try { await api("/v1/gripper", "POST", { action }); await refresh(); } catch (error) { showError(error); }
}

async function cycleExecutionMode() {
  const order = ["PRESS_MIT", "TRANSIT_SPEED", "CONTACT_WORK"];
  const current = ui.snapshot?.execution_mode || "PRESS_MIT";
  const next = order[(order.indexOf(current) + 1) % order.length];
  try { await api("/v1/settings", "POST", { execution_mode: next }); $("executionMode").value = next; applyModeConstraints(); await refresh(); } catch (error) { showError(error); }
}

async function requestSafeTerminate() {
  ui.terminating = true;
  ui.terminationLocked = true;
  $("safeTerminateButton").disabled = true;
  $("terminationStatus").textContent = "Launching and verifying authoritative safe termination helper...";
  try {
    const result = await api("/v1/safe-terminate", "POST", {}, 4000);
    const termination = result.safe_termination || {};
    $("terminationStatus").textContent = `Safe termination: ${termination.state || "UNKNOWN"} — ${termination.message || ""}`;
    if (termination.state !== "RUNNING") {
      ui.terminating = false;
      $("safeTerminateButton").disabled = true;
      showError(new Error(termination.message || "Safe termination launch was not confirmed"));
    }
  } catch (error) {
    ui.terminating = false;
    ui.terminationLocked = false;
    $("safeTerminateButton").disabled = false;
    showError(error);
  }
}

function initializeForms(snapshot) {
  if (ui.formInitialized) return;
  const runtime = snapshot.runtime || {};
  $("executionMode").value = snapshot.execution_mode || "PRESS_MIT";
  $("interactionMode").value = snapshot.interaction_mode || "ONE_SHOT";
  $("ikMode").value = snapshot.ik_mode || "POSITION_3DOF";
  $("duration").value = runtime.duration_s ?? 3;
  $("replanInterval").value = runtime.replan_interval_s ?? 0.1;
  $("kpMultiplier").value = runtime.kp_multiplier ?? 1;
  const contactBudget = runtime.contact_torque_budget_nm || snapshot.contact_monitoring?.task_torque_budget_nm || [];
  $("contactBudgetMode").value = runtime.contact_budget_mode || snapshot.contact_monitoring?.budget_mode || "JOINT_6";
  ["contactBudget1", "contactBudget2", "contactBudget3", "contactBudget4", "contactBudget5", "contactBudget6"].forEach((id, i) => {
    $(id).value = contactBudget[i] ?? "";
  });
  const wrenchForce = runtime.contact_wrench_force_budget_n || [];
  const wrenchTorque = runtime.contact_wrench_torque_budget_nm || [];
  ["wrenchFx", "wrenchFy", "wrenchFz"].forEach((id, i) => { $(id).value = wrenchForce[i] ?? 0; });
  ["wrenchTx", "wrenchTy", "wrenchTz"].forEach((id, i) => { $(id).value = wrenchTorque[i] ?? 0; });
  $("isotropicForce").value = runtime.contact_isotropic_force_budget_n ?? 0;
  $("isotropicTorque").value = runtime.contact_isotropic_torque_budget_nm ?? 0;
  const offset = runtime.controlled_frame_offset_xyz_m || [0, 0, 0];
  const offsetRpy = runtime.controlled_frame_offset_rpy_rad || [0, 0, 0];
  ["offsetX", "offsetY", "offsetZ"].forEach((id, i) => { $(id).value = offset[i] ?? 0; });
  ["offsetRoll", "offsetPitch", "offsetYaw"].forEach((id, i) => { $(id).value = offsetRpy[i] ?? 0; });
  $("payloadMass").value = runtime.payload_mass_kg ?? 0;
  const com = runtime.payload_com_tool_m || [0, 0, 0];
  ["payloadX", "payloadY", "payloadZ"].forEach((id, i) => { $(id).value = com[i] ?? 0; });
  const gripper = snapshot.gripper || runtime.gripper || {};
  $("gripperMode").value = gripper.mode || "MIT";
  $("gripperOpenTarget").value = gripper.open_position_rad ?? -4.8869219056;
  $("gripperClosedTarget").value = gripper.closed_position_rad ?? -0.3490658504;
  $("gripperVelocity").value = gripper.velocity_limit_rad_s ?? 0.35;
  $("gripperTorqueRatio").value = gripper.torque_limit_ratio ?? 0.15;
  $("gripperKp").value = gripper.mit_kp ?? 8;
  $("gripperKd").value = gripper.mit_kd ?? 1;
  ui.formInitialized = true;
  applyModeConstraints();
}

function renderStatus(snapshot) {
  const healthy = snapshot.health === "HEALTHY" && snapshot.ready;
  $("healthBadge").textContent = `${snapshot.health} / ${snapshot.residency} / ${snapshot.execution_mode}`;
  $("healthBadge").className = `badge ${healthy ? "good" : "bad"}`;
  $("engageStatus").textContent = snapshot.engaged ? "ON" : "OFF";
  $("lbStatus").textContent = snapshot.input?.lb_pressed ? "PRESSED" : "RELEASED";
  $("controlState").textContent = snapshot.control_state;
  $("leaseStatus").textContent = snapshot.lease?.active ? `Owned, gen ${snapshot.lease.fencing_generation}` : snapshot.lease?.state || "NONE";
  $("platformStatus").textContent = snapshot.safety?.platform_ready ? "Manager + Fabric ready" : `Manager ${snapshot.safety?.manager_registered ? "OK" : "DOWN"}, Fabric ${snapshot.safety?.fabric_ready ? "OK" : "DOWN"}`;
  const fabricInput = snapshot.fabric_input || {};
  $("fabricInputStatus").textContent = `${fabricInput.last_result || "WAITING"}${fabricInput.last_sequence == null ? "" : ` seq ${fabricInput.last_sequence}`}`;
  $("fault").textContent = snapshot.fault_reason || snapshot.last_error || "";
  $("engageButton").textContent = snapshot.engaged ? "Physical control engaged" : "Engage physical control";
  $("engageButton").disabled = snapshot.engaged || !snapshot.safety?.platform_ready || ui.terminating;
  $("floatButton").disabled = ui.terminating;
  $("safeTerminateButton").disabled = ui.terminating || ui.terminationLocked;
  const active = Boolean(snapshot.trajectory?.active);
  const gripperLatched = Boolean(snapshot.gripper?.active_action);
  ["executionMode", "interactionMode", "ikMode", "duration", "replanInterval", "kpMultiplier", "applyMotionSettings", "baselineButton", "contactBudgetMode", "contactBudget1", "contactBudget2", "contactBudget3", "contactBudget4", "contactBudget5", "contactBudget6", "wrenchFx", "wrenchFy", "wrenchFz", "wrenchTx", "wrenchTy", "wrenchTz", "isotropicForce", "isotropicTorque", "applyContactBudgets", "applyOffset", "applyPayload"].forEach((id) => { $(id).disabled = active || ui.terminating; });
  ["gripperMode", "gripperOpenTarget", "gripperClosedTarget", "gripperVelocity", "gripperTorqueRatio", "gripperKp", "gripperKd", "applyGripperSettings"].forEach((id) => { $(id).disabled = active || gripperLatched || ui.terminating; });
  applyModeConstraints();
  ["gripperOpenButton", "gripperCloseButton"].forEach((id) => { $(id).disabled = active || !snapshot.engaged || ui.terminating; });
  $("gripperStopButton").disabled = !snapshot.gripper?.requested_action || ui.terminating;
  const termination = snapshot.safe_termination || {};
  $("terminationStatus").textContent = termination.state && termination.state !== "IDLE" ? `Safe termination: ${termination.state} — ${termination.message || ""}` : "";
}

function renderGripper(snapshot) {
  const gripper = snapshot.gripper || {};
  $("gripperBackend").textContent = `${gripper.mode || "—"} / ${gripper.basic_mode || "—"}`;
  $("gripperAction").textContent = `${gripper.requested_action || "NONE"} / ${gripper.active_action || "IDLE"}`;
  $("gripperPosition").textContent = `${num(gripper.measured_rad, 4)} / ${num(gripper.target_rad, 4)}`;
  $("gripperTorque").textContent = num(gripper.measured_torque_nm, 3);
  $("gripperError").textContent = gripper.last_error || "";
}

function renderContact(snapshot) {
  const contact = snapshot.contact_monitoring || {};
  $("contactCeilings").textContent = vec(contact.effective_torque_ceiling_nm);
  $("effectiveContactBudget").textContent = vec(contact.effective_joint_budget_nm);
  $("contactRatios").textContent = vec(contact.torque_limit_ratios);
  $("contactResidual").textContent = vec(contact.residual_nm);
  const violations = contact.limit_violation_joint_indices || [];
  const saturated = contact.saturated_joint_indices || [];
  $("contactStatus").textContent = `${contact.baseline_state || "NO BASELINE"} | ${contact.budget_mode || "JOINT_6"}${violations.length ? ` | BUDGET J${violations.map((index) => index + 1).join(", J")}` : ""}${saturated.length ? ` | SATURATED J${saturated.map((index) => index + 1).join(", J")}` : ""}`;
}

function renderGains(snapshot) {
  const rows = $("gainRows");
  rows.innerHTML = "";
  for (const gain of snapshot.runtime?.effective_gains || []) {
    const row = document.createElement("tr");
    const kpClass = gain.kp_clamped ? "clamped" : "";
    const kdClass = gain.kd_clamped ? "clamped" : "";
    row.innerHTML = `<td>${gain.joint_name}</td><td>${num(gain.base_kp, 1)}</td><td>${num(gain.requested_kp, 1)}</td><td class="${kpClass}">${num(gain.effective_kp, 1)}${gain.kp_clamped ? " CLAMP" : ""}</td><td>${num(gain.base_kd, 2)}</td><td>${num(gain.requested_kd, 2)}</td><td class="${kdClass}">${num(gain.effective_kd, 2)}${gain.kd_clamped ? " CLAMP" : ""}</td><td>${num(gain.tracking_effort_limit_nm, 1)}</td>`;
    rows.appendChild(row);
  }
}

function renderJoints(snapshot) {
  const state = snapshot.joint_state || {};
  const rows = $("jointRows");
  rows.innerHTML = "";
  for (let i = 0; i < 6; i += 1) {
    const row = document.createElement("tr");
    row.innerHTML = `<td>J${i + 1}</td><td>${num(state.measured_rad?.[i])}</td><td>${num(state.commanded_rad?.[i])}</td><td>${num(state.goal_rad?.[i])}</td><td>${num(state.commanded_velocity_rad_s?.[i])}</td><td>${num(state.provider_rate_caps_rad_s?.[i], 3)}</td><td>${num(state.measured_torque_nm?.[i], 3)}</td>`;
    rows.appendChild(row);
  }
  $("trackingError").textContent = `${num(state.max_tracking_error_rad, 4)} rad`;
}

function bounds(points) {
  const valid = points.filter((point) => Array.isArray(point) && point.length >= 2);
  if (!valid.length) return { minX: -0.4, maxX: 0.4, minY: -0.1, maxY: 0.7 };
  return { minX: Math.min(...valid.map((p) => p[0])), maxX: Math.max(...valid.map((p) => p[0])), minY: Math.min(...valid.map((p) => p[1])), maxY: Math.max(...valid.map((p) => p[1])) };
}

function drawFrameAxes(context, project, frame, plane, scalePixels) {
  if (!frame?.position_m || !frame?.rotation_matrix) return;
  const origin = frame.position_m;
  const r = frame.rotation_matrix;
  const length = 0.06;
  const endpoints = [0, 1, 2].map((axis) => origin.map((value, row) => value + r[row][axis] * length));
  const labels = ["X", "Y", "Z"];
  endpoints.forEach((point, i) => {
    const [ox, oy] = project(origin); const [px, py] = project(point);
    context.beginPath(); context.moveTo(ox, oy); context.lineTo(px, py); context.strokeStyle = ["#ff6b6b", "#77dd77", "#6aa9ff"][i]; context.lineWidth = 3; context.stroke();
    context.fillStyle = context.strokeStyle; context.fillText(labels[i], px + 4, py - 4);
  });
}

function drawChain(canvas, measured, commanded, targetFrame, measuredFrame, plane) {
  const context = canvas.getContext("2d");
  context.clearRect(0, 0, canvas.width, canvas.height);
  context.fillStyle = "#0a0a0a"; context.fillRect(0, 0, canvas.width, canvas.height);
  const projectRaw = (point) => plane === "TOP" ? [point[0], point[1]] : [point[0], point[2]];
  const all = [...measured, ...commanded];
  if (targetFrame?.position_m) all.push(targetFrame.position_m);
  const box = bounds(all.map(projectRaw));
  const margin = 55; const rangeX = Math.max(0.25, box.maxX - box.minX); const rangeY = Math.max(0.25, box.maxY - box.minY);
  const scale = Math.min((canvas.width - 2 * margin) / rangeX, (canvas.height - 2 * margin) / rangeY);
  const centerX = (box.minX + box.maxX) / 2; const centerY = (box.minY + box.maxY) / 2;
  const project = (point) => { const [x, y] = projectRaw(point); return [canvas.width / 2 + (x - centerX) * scale, canvas.height / 2 - (y - centerY) * scale]; };
  const render = (points, color, width, dashed) => {
    if (!Array.isArray(points) || points.length < 2) return;
    context.strokeStyle = color; context.lineWidth = width; context.setLineDash(dashed ? [8, 7] : []); context.beginPath();
    points.forEach((point, index) => { const [x, y] = project(point); if (index === 0) context.moveTo(x, y); else context.lineTo(x, y); }); context.stroke(); context.setLineDash([]);
  };
  render(commanded, "#f4b860", 3, true); render(measured, "#67b6e6", 6, false);
  if (targetFrame?.position_m) { const [x, y] = project(targetFrame.position_m); context.beginPath(); context.arc(x, y, 8, 0, Math.PI * 2); context.fillStyle = "#ffe35b"; context.fill(); context.fillStyle = "#ffe35b"; context.fillText("target", x + 10, y - 8); }
  drawFrameAxes(context, project, measuredFrame, plane, scale); drawFrameAxes(context, project, targetFrame, plane, scale);
}

function renderModel(snapshot) {
  const measured = snapshot.model_view?.measured_points_m || [];
  const commanded = snapshot.model_view?.commanded_points_m || [];
  const target = snapshot.model_view?.staged_controlled_frame || null;
  const measuredControl = snapshot.model_view?.measured_controlled_frame || null;
  drawChain($("topView"), measured, commanded, target, measuredControl, "TOP");
  drawChain($("sideView"), measured, commanded, target, measuredControl, "SIDE");
  $("measuredControl").textContent = frameText(measuredControl);
  $("targetControl").textContent = frameText(target);
  $("committedControl").textContent = frameText(snapshot.target?.last_committed);
  $("positionResidual").textContent = snapshot.target?.position_residual_m == null ? "—" : `${num(snapshot.target.position_residual_m, 5)} m`;
  $("orientationResidual").textContent = snapshot.target?.orientation_residual_rad == null ? "—" : `${num(snapshot.target.orientation_residual_rad, 5)} rad`;
  $("clampStatus").textContent = snapshot.target?.last_commit_clamped ? "CLAMPED TO 20 cm" : "not clamped";
  const trajectory = snapshot.trajectory || {};
  $("trajectoryDuration").textContent = trajectory.segment_duration_s == null ? "idle" : `${num(trajectory.segment_duration_s, 3)} s`;
  $("trajectoryProgress").textContent = trajectory.progress == null ? "idle" : `${(Number(trajectory.progress) * 100).toFixed(1)}%`;
  $("trajectoryFrames").textContent = trajectory.frames_sent == null ? "—" : `${trajectory.frames_sent} / ${trajectory.frames_skipped || 0}`;
  $("replans").textContent = `${snapshot.live_replan_count || 0} total; active ${trajectory.replan_count ?? "—"}`;
}

function renderDiagnostics(snapshot) {
  $("diagnostics").textContent = JSON.stringify({ control_state: snapshot.control_state, execution_mode: snapshot.execution_mode, interaction_mode: snapshot.interaction_mode, ik_mode: snapshot.ik_mode, command_count: snapshot.command_count, commit_count: snapshot.commit_count, live_replan_count: snapshot.live_replan_count, rejected_count: snapshot.rejected_count, runtime: snapshot.runtime, gripper: snapshot.gripper, input: snapshot.input, target: snapshot.target, planning: snapshot.planning, contact_monitoring: snapshot.contact_monitoring, trajectory: snapshot.trajectory, gravity_support: snapshot.gravity_support, safety: snapshot.safety, lease: snapshot.lease, basic_active_command_modes: snapshot.basic_state?.active_command_modes, basic_mode_transition: snapshot.basic_state?.mode_transition, basic_float_transition_pending_joint_indices: snapshot.basic_state?.float_transition_pending_joint_indices, basic_snapshot_delivery: snapshot.basic_state?.snapshot_delivery, basic_hardware_io: snapshot.basic_state?.hardware_io, basic_command_ingress: snapshot.basic_state?.command_ingress, basic_payload: snapshot.basic_state?.payload, basic_gravity_compensation: snapshot.basic_state?.gravity_compensation, safe_termination: snapshot.safe_termination, fabric_input: snapshot.fabric_input, scene_input: snapshot.scene_input, external_input: snapshot.external_input, platform_errors: snapshot.platform_errors }, null, 2);
}

async function refreshControlAudit() {
  try {
    const timeline = await api("/v1/control-audit?limit=30", "GET", null, 1800);
    $("controlAuditTimeline").textContent = JSON.stringify(timeline, null, 2);
  } catch (error) {
    $("controlAuditTimeline").textContent = `Audit timeline unavailable: ${String(error?.message || error)}`;
  }
}

async function refresh() {
  if (ui.refreshInFlight) return;
  ui.refreshInFlight = true;
  try {
    const snapshot = await api("/v1/state", "GET", null, 1200);
    ui.snapshot = snapshot; initializeForms(snapshot); renderStatus(snapshot); renderGripper(snapshot); renderContact(snapshot); renderGains(snapshot); renderJoints(snapshot); renderModel(snapshot); renderDiagnostics(snapshot);
  } catch (error) {
    if (ui.terminating) { $("terminationStatus").textContent = "Safe termination helper launched; services may now be stopping."; $("healthBadge").textContent = "Stopping / offline"; return; }
    showError(error); $("healthBadge").textContent = "Disconnected"; $("healthBadge").className = "badge bad";
  } finally {
    ui.refreshInFlight = false;
  }
}

$("engageButton").addEventListener("click", async () => { try { await api("/v1/engage", "POST", { enabled: true }, 10000); await refresh(); } catch (error) { showError(error); } });
$("floatButton").addEventListener("click", requestFloat);
$("baselineButton").addEventListener("click", captureContactBaseline);
$("executionMode").addEventListener("change", applyModeConstraints);
$("applyMotionSettings").addEventListener("click", async () => { try { await api("/v1/settings", "POST", { execution_mode: $("executionMode").value, interaction_mode: $("interactionMode").value, ik_mode: $("ikMode").value, duration_s: numeric("duration"), replan_interval_s: numeric("replanInterval"), kp_multiplier: numeric("kpMultiplier") }); await refresh(); } catch (error) { showError(error); } });
$("applyContactBudgets").addEventListener("click", async () => {
  try {
    const mode = $("contactBudgetMode").value;
    const payload = { contact_budget_mode: mode };
    if (mode === "JOINT_6") {
      payload.contact_torque_budget_nm = ["contactBudget1", "contactBudget2", "contactBudget3", "contactBudget4", "contactBudget5", "contactBudget6"].map(numeric);
    } else if (mode === "WRENCH_6") {
      payload.contact_wrench_force_budget_n = ["wrenchFx", "wrenchFy", "wrenchFz"].map(numeric);
      payload.contact_wrench_torque_budget_nm = ["wrenchTx", "wrenchTy", "wrenchTz"].map(numeric);
    } else {
      payload.contact_isotropic_force_budget_n = numeric("isotropicForce");
      payload.contact_isotropic_torque_budget_nm = numeric("isotropicTorque");
    }
    await api("/v1/settings", "POST", payload);
    await refresh();
  } catch (error) {
    showError(error);
  }
});
$("applyOffset").addEventListener("click", async () => { try { await api("/v1/settings", "POST", { controlled_frame_offset_xyz_m: [numeric("offsetX"), numeric("offsetY"), numeric("offsetZ")], controlled_frame_offset_rpy_rad: [numeric("offsetRoll"), numeric("offsetPitch"), numeric("offsetYaw")] }); await refresh(); } catch (error) { showError(error); } });
$("applyPayload").addEventListener("click", async () => { try { await api("/v1/settings", "POST", { payload_mass_kg: numeric("payloadMass"), payload_com_tool_m: [numeric("payloadX"), numeric("payloadY"), numeric("payloadZ")] }); await refresh(); } catch (error) { showError(error); } });
$("applyGripperSettings").addEventListener("click", async () => { try { await api("/v1/gripper/settings", "POST", { mode: $("gripperMode").value, open_position_rad: numeric("gripperOpenTarget"), closed_position_rad: numeric("gripperClosedTarget"), velocity_limit_rad_s: numeric("gripperVelocity"), torque_limit_ratio: numeric("gripperTorqueRatio"), mit_kp: numeric("gripperKp"), mit_kd: numeric("gripperKd") }); await refresh(); } catch (error) { showError(error); } });
function bindGripperHold(id, action) {
  const element = $(id);
  element.addEventListener("pointerdown", (event) => { event.preventDefault(); element.setPointerCapture(event.pointerId); requestGripper(action); });
  const stop = (event) => { event.preventDefault(); requestGripper("STOP"); };
  element.addEventListener("pointerup", stop);
  element.addEventListener("pointercancel", stop);
  element.addEventListener("lostpointercapture", () => requestGripper("STOP"));
}
bindGripperHold("gripperOpenButton", "OPEN");
bindGripperHold("gripperCloseButton", "CLOSE");
$("gripperStopButton").addEventListener("click", () => requestGripper("STOP"));
$("safeTerminateButton").addEventListener("click", requestSafeTerminate);

setInterval(() => { pollGamepad(); }, 40);
setInterval(() => { refresh(); }, 100);
setInterval(() => { refreshControlAudit(); }, 2000);
refresh();
refreshControlAudit();
