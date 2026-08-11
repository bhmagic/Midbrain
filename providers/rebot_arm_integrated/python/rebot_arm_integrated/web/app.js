const $ = (id) => document.getElementById(id);
const ui = { refreshInFlight: false, terminating: false };

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

function num(value, digits = 4) {
  return Number.isFinite(Number(value)) ? Number(value).toFixed(digits) : "—";
}

function renderJoints(snapshot) {
  const state = snapshot.joint_state || {};
  const rows = $("jointRows");
  rows.innerHTML = "";
  for (let index = 0; index < 6; index += 1) {
    const row = document.createElement("tr");
    row.innerHTML = `<td>J${index + 1}</td><td>${num(state.measured_rad?.[index])}</td><td>${num(state.commanded_rad?.[index])}</td><td>${num(state.goal_rad?.[index])}</td><td>${num(state.commanded_velocity_rad_s?.[index])}</td><td>${num(state.provider_rate_caps_rad_s?.[index], 3)}</td><td>${num(state.measured_torque_nm?.[index], 3)}</td>`;
    rows.appendChild(row);
  }
  $("trackingError").textContent = `${num(state.max_tracking_error_rad, 4)} rad`;
}

function render(snapshot) {
  const healthy = snapshot.health === "HEALTHY" && snapshot.ready;
  $("healthBadge").textContent = `${snapshot.health || "UNKNOWN"} / ${snapshot.residency || "UNKNOWN"}`;
  $("healthBadge").className = `badge ${healthy ? "good" : "bad"}`;
  $("controllerRole").textContent = snapshot.controller_role || "FREE_SPACE";
  $("controlState").textContent = snapshot.control_state || "—";
  $("leaseStatus").textContent = snapshot.lease?.active ? `Owned, generation ${snapshot.lease.fencing_generation}` : snapshot.lease?.state || "NONE";
  $("platformStatus").textContent = snapshot.safety?.platform_ready ? "Ready" : `Manager ${snapshot.safety?.manager_registered ? "OK" : "DOWN"}; Fabric ${snapshot.safety?.fabric_ready ? "OK" : "DOWN"}`;
  const scene = snapshot.scene_input || {};
  $("sceneStatus").textContent = `${scene.last_result || "WAITING"}${scene.last_sequence == null ? "" : `; sequence ${scene.last_sequence}`}`;
  const assembly = snapshot.assembly || {};
  $("assemblyStatus").textContent = `${assembly.assembly_id || "unavailable"}${snapshot.assembly_fingerprint ? `; ${snapshot.assembly_fingerprint.slice(0, 12)}` : ""}`;
  const effector = assembly.mounted_effector || {};
  const sphereCount = (effector.collision_primitives || []).filter((item) => item.shape?.type === "SPHERE").length;
  $("effectorGeometry").textContent = `${sphereCount} profile sphere${sphereCount === 1 ? "" : "s"}`;
  const planning = snapshot.planning || {};
  $("planStatus").textContent = planning.last_preview?.preview_id || planning.last_rejection || "idle";
  const trajectory = snapshot.trajectory || {};
  $("trajectoryStatus").textContent = trajectory.active ? `active; ${(Number(trajectory.progress || 0) * 100).toFixed(1)}%` : "idle";
  $("completionStatus").textContent = trajectory.last_completed?.completion_outcome || "—";
  $("fault").textContent = snapshot.fault_reason || snapshot.last_error || "";
  const termination = snapshot.safe_termination || {};
  $("terminationStatus").textContent = termination.state && termination.state !== "IDLE" ? `${termination.state}: ${termination.message || ""}` : "";
  $("diagnostics").textContent = JSON.stringify(snapshot, null, 2);
  renderJoints(snapshot);
}

async function refresh() {
  if (ui.refreshInFlight) return;
  ui.refreshInFlight = true;
  try {
    const [snapshot, catalog] = await Promise.all([
      api("/v1/state", "GET", null, 1200),
      api("/v1/capabilities", "GET", null, 1200),
    ]);
    render(snapshot);
    $("capabilities").textContent = JSON.stringify(catalog, null, 2);
  } catch (error) {
    $("fault").textContent = String(error?.message || error);
    $("healthBadge").textContent = ui.terminating ? "Stopping / offline" : "Disconnected";
    $("healthBadge").className = "badge bad";
  } finally {
    ui.refreshInFlight = false;
  }
}

async function requestFloat() {
  try {
    await api("/v1/float", "POST", {}, 10000);
    await refresh();
  } catch (error) {
    $("fault").textContent = String(error?.message || error);
  }
}

async function requestSafeTerminate() {
  ui.terminating = true;
  $("safeTerminateButton").disabled = true;
  try {
    const result = await api("/v1/safe-terminate", "POST", {}, 4000);
    const termination = result.safe_termination || {};
    $("terminationStatus").textContent = `${termination.state || "UNKNOWN"}: ${termination.message || ""}`;
  } catch (error) {
    ui.terminating = false;
    $("safeTerminateButton").disabled = false;
    $("fault").textContent = String(error?.message || error);
  }
}

async function refreshControlAudit() {
  try {
    const timeline = await api("/v1/control-audit?limit=30", "GET", null, 1800);
    $("controlAuditTimeline").textContent = JSON.stringify(timeline, null, 2);
  } catch (error) {
    $("controlAuditTimeline").textContent = `Audit timeline unavailable: ${String(error?.message || error)}`;
  }
}

$("floatButton").addEventListener("click", requestFloat);
$("safeTerminateButton").addEventListener("click", requestSafeTerminate);
setInterval(refresh, 250);
setInterval(refreshControlAudit, 2000);
refresh();
refreshControlAudit();
