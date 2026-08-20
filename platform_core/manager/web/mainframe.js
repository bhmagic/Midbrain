"use strict";

const $ = (id) => document.getElementById(id);
let armCatalog = null;
let armSelectionDirty = false;
let effectorCatalog = null;
let effectorSelectionDirty = false;

function setLamp(id, tone) {
  const lamp = $(id);
  lamp.className = `lamp ${tone || "muted"}`;
}

function statusChip(status, tone) {
  const chip = document.createElement("span");
  chip.className = `status-chip ${tone || "muted"}`;
  chip.textContent = status || "UNKNOWN";
  return chip;
}

function textCell(value, fallback = "—") {
  const cell = document.createElement("td");
  cell.textContent = value == null || value === "" ? fallback : String(value);
  return cell;
}

function componentCell(item) {
  const cell = document.createElement("td");
  const name = document.createElement("span");
  const identity = document.createElement("span");
  name.className = "component-name";
  identity.className = "component-id";
  name.textContent = item.display_name || item.id;
  identity.textContent = item.id;
  cell.append(name, identity);
  return cell;
}

function statusCell(item) {
  const cell = document.createElement("td");
  cell.append(statusChip(item.status, item.tone));
  return cell;
}

function observationCell(item) {
  const cell = document.createElement("td");
  const link = document.createElement("a");
  link.className = "observation-link";
  link.href = item.observation_url;
  link.target = "_blank";
  link.rel = "noreferrer";
  link.textContent = "Open observation";
  cell.append(link);
  return cell;
}

function renderProviders(providers) {
  const body = $("providerRows");
  body.replaceChildren();
  if (!providers.length) {
    const row = document.createElement("tr");
    const cell = textCell("No Providers are configured.");
    cell.colSpan = 5;
    cell.className = "empty-cell";
    row.append(cell);
    body.append(row);
    return;
  }
  for (const provider of providers) {
    const row = document.createElement("tr");
    const lastSeen = provider.last_seen
      ? new Date(provider.last_seen).toLocaleTimeString()
      : "No heartbeat";
    row.append(
      componentCell(provider),
      statusCell(provider),
      textCell(provider.residency || provider.process_state),
      textCell(lastSeen),
      observationCell(provider),
    );
    body.append(row);
  }
}

function renderSkills(skills) {
  const body = $("skillRows");
  body.replaceChildren();
  if (!skills.length) {
    const row = document.createElement("tr");
    const cell = textCell("No Skill manifests were found.");
    cell.colSpan = 5;
    cell.className = "empty-cell";
    row.append(cell);
    body.append(row);
    return;
  }
  for (const skill of skills) {
    const row = document.createElement("tr");
    const adapter = skill.availability || {};
    const adapterState = [
      adapter.adapter_kind || "NO ADAPTER",
      adapter.runtime_ready ? "ready" : "not running",
    ].join(" · ");
    row.append(
      componentCell(skill),
      statusCell(skill),
      textCell(skill.last_state || "No completed run"),
      textCell(adapterState),
      observationCell(skill),
    );
    body.append(row);
  }
}

function summarizeProviders(items) {
  const active = items.filter((item) => item.status !== "COLD").length;
  const ready = items.filter((item) => item.status === "HOT / READY").length;
  return `${active} active · ${ready} ready`;
}

function summarizeSkills(items) {
  const running = items.filter((item) => item.status === "RUNNING").length;
  const unavailable = items.filter((item) => item.status === "UNAVAILABLE").length;
  return `${running} running · ${unavailable} unavailable`;
}

function selectedArmProfile() {
  if (!armCatalog) {
    return null;
  }
  const key = $("armSelect").value;
  return (armCatalog.profiles || []).find((profile) => profile.provider_relative_path === key) || null;
}

function renderArmDetails(profile) {
  if (!profile) {
    $("armActiveProfile").textContent = "Unavailable";
    $("armModelRevision").textContent = "Unavailable";
    $("armRootFrame").textContent = "Unavailable";
    $("armJointCount").textContent = "Unavailable";
    $("armLocateBaseCad").textContent = "Unavailable";
    $("armReferenceCount").textContent = "Unavailable";
    $("armProfileFile").textContent = "Unavailable";
    return;
  }
  $("armActiveProfile").textContent = `${profile.display_name} · ${profile.model_id}`;
  $("armModelRevision").textContent = profile.model_revision;
  $("armRootFrame").textContent = profile.root_frame || "unknown";
  $("armJointCount").textContent = `${profile.joint_count || 0} joints`;
  $("armLocateBaseCad").textContent = profile.locate_arm_base?.cad_path || "not configured";
  $("armReferenceCount").textContent = `${profile.locate_arm_base?.reference_image_count || 0} configured`;
  $("armProfileFile").textContent = profile.profile_file || "unknown";
}

function updateArmActionState() {
  const profile = selectedArmProfile();
  const confirmed = $("armPhysicalConfirmation").checked;
  $("selectArmButton").disabled = !profile || !confirmed || Boolean(profile.active);
  renderArmDetails(profile);
}

function renderArmCatalog(data) {
  armCatalog = data;
  const select = $("armSelect");
  const previousValue = select.value;
  select.replaceChildren();
  for (const profile of data.profiles || []) {
    const option = document.createElement("option");
    option.value = profile.provider_relative_path;
    option.textContent = `${profile.display_name} · ${profile.model_revision}${profile.active ? " · ACTIVE" : ""}`;
    select.append(option);
  }
  select.disabled = select.options.length === 0;
  if (armSelectionDirty && [...select.options].some((option) => option.value === previousValue)) {
    select.value = previousValue;
  } else {
    const active = (data.profiles || []).find((profile) => profile.active);
    if (active) {
      select.value = active.provider_relative_path;
    }
  }
  const active = (data.profiles || []).find((profile) => profile.active);
  renderArmDetails(armSelectionDirty ? selectedArmProfile() : active);
  $("armActionStatus").textContent = data.status === "SELECTED_RESTART_REQUIRED"
    ? "Arm selection saved. Restart the arm Provider and its dependents to load this profile."
    : "Arm-profile edits and selections take effect after the affected Providers restart.";
  $("armActionStatus").className = data.status === "SELECTED_RESTART_REQUIRED"
    ? "activation-status warning-text"
    : "activation-status";
  updateArmActionState();
}

async function refreshArmCatalog() {
  try {
    const response = await fetch("/v1/ui/robot-assembly/arms", { cache: "no-store" });
    const data = await response.json();
    if (!response.ok) {
      throw new Error(data.error || `Arm catalog returned ${response.status}`);
    }
    renderArmCatalog(data);
  } catch (error) {
    $("armActionStatus").textContent = error.message;
    $("armActionStatus").className = "activation-status danger-text";
    renderArmDetails(null);
  }
}

async function selectArm() {
  const profile = selectedArmProfile();
  if (!profile || !$("armPhysicalConfirmation").checked) {
    return;
  }
  $("selectArmButton").disabled = true;
  $("armActionStatus").textContent = "Saving static arm-profile selection...";
  $("armActionStatus").className = "activation-status warning-text";
  try {
    const response = await fetch("/v1/ui/robot-assembly/arms", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        profile_file: profile.provider_relative_path,
        physical_arm_confirmed: true,
      }),
    });
    const data = await response.json();
    if (!response.ok) {
      const blockers = (data.blocking_providers || [])
        .map((item) => item.provider_id)
        .join(", ");
      throw new Error(`${data.error || `Selection returned ${response.status}`}${blockers ? `: ${blockers}` : ""}`);
    }
    armSelectionDirty = false;
    $("armPhysicalConfirmation").checked = false;
    renderArmCatalog(data);
    await refreshEffectorCatalog();
  } catch (error) {
    $("armActionStatus").textContent = error.message;
    $("armActionStatus").className = "activation-status danger-text";
    updateArmActionState();
  }
}

function effectorKey(profile) {
  return JSON.stringify([profile.profile_id, profile.profile_revision]);
}

function selectedEffectorProfile() {
  if (!effectorCatalog) {
    return null;
  }
  const key = $("effectorSelect").value;
  return (effectorCatalog.profiles || []).find((profile) => effectorKey(profile) === key) || null;
}

function renderEffectorDetails(profile) {
  if (!profile) {
    $("effectorActiveProfile").textContent = "Unavailable";
    $("effectorMass").textContent = "Unavailable";
    $("effectorCom").textContent = "Unavailable";
    $("effectorInertialQualification").textContent = "Unavailable";
    $("effectorColliderCount").textContent = "Unavailable";
    $("effectorProfileFile").textContent = "Unavailable";
    return;
  }
  const inertial = profile.inertial || {};
  const com = Array.isArray(inertial.center_of_mass_m)
    ? inertial.center_of_mass_m.map((value) => Number(value).toFixed(4)).join(", ")
    : "unknown";
  $("effectorActiveProfile").textContent = `${profile.display_name} · ${profile.profile_revision}`;
  $("effectorMass").textContent = `${Number(inertial.mass_kg || 0).toFixed(3)} kg`;
  $("effectorCom").textContent = `[${com}] m in ${inertial.reference_frame || "unknown frame"}`;
  $("effectorInertialQualification").textContent = inertial.qualification || "unspecified";
  $("effectorColliderCount").textContent = `${profile.collision_primitive_count || 0} profile-owned primitives`;
  $("effectorProfileFile").textContent = profile.profile_file || "unknown";
}

function updateEffectorActionState() {
  const profile = selectedEffectorProfile();
  const confirmed = $("effectorPhysicalConfirmation").checked;
  $("selectEffectorButton").disabled = !profile || !confirmed || Boolean(profile.active);
  renderEffectorDetails(profile);
}

function renderEffectorCatalog(data) {
  effectorCatalog = data;
  const select = $("effectorSelect");
  const previousValue = select.value;
  select.replaceChildren();
  for (const profile of data.profiles || []) {
    const option = document.createElement("option");
    option.value = effectorKey(profile);
    const mass = Number(profile.inertial?.mass_kg || 0).toFixed(3);
    option.textContent = `${profile.display_name} · ${mass} kg${profile.active ? " · ACTIVE" : ""}`;
    select.append(option);
  }
  select.disabled = select.options.length === 0;
  if (effectorSelectionDirty && [...select.options].some((option) => option.value === previousValue)) {
    select.value = previousValue;
  } else {
    const active = (data.profiles || []).find((profile) => profile.active);
    if (active) {
      select.value = effectorKey(active);
    }
  }
  const active = (data.profiles || []).find((profile) => profile.active);
  renderEffectorDetails(effectorSelectionDirty ? selectedEffectorProfile() : active);
  $("effectorActionStatus").textContent = data.status === "SELECTED_RESTART_REQUIRED"
    ? "Selection saved. Restart the affected arm Providers to load this profile."
    : "Profile edits and selections take effect only after the affected arm Providers restart.";
  $("effectorActionStatus").className = data.status === "SELECTED_RESTART_REQUIRED"
    ? "activation-status warning-text"
    : "activation-status";
  updateEffectorActionState();
}

async function refreshEffectorCatalog() {
  try {
    const response = await fetch("/v1/ui/robot-assembly/effectors", { cache: "no-store" });
    const data = await response.json();
    if (!response.ok) {
      throw new Error(data.error || `Effector catalog returned ${response.status}`);
    }
    renderEffectorCatalog(data);
  } catch (error) {
    $("effectorActionStatus").textContent = error.message;
    $("effectorActionStatus").className = "activation-status danger-text";
    renderEffectorDetails(null);
  }
}

async function selectEffector() {
  const profile = selectedEffectorProfile();
  if (!profile || !$("effectorPhysicalConfirmation").checked) {
    return;
  }
  $("selectEffectorButton").disabled = true;
  $("effectorActionStatus").textContent = "Saving static assembly selection...";
  $("effectorActionStatus").className = "activation-status warning-text";
  try {
    const response = await fetch("/v1/ui/robot-assembly/effectors", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        profile_id: profile.profile_id,
        profile_revision: profile.profile_revision,
        physical_effector_confirmed: true,
      }),
    });
    const data = await response.json();
    if (!response.ok) {
      const blockers = (data.blocking_providers || [])
        .map((item) => item.provider_id)
        .join(", ");
      throw new Error(`${data.error || `Selection returned ${response.status}`}${blockers ? `: ${blockers}` : ""}`);
    }
    effectorSelectionDirty = false;
    $("effectorPhysicalConfirmation").checked = false;
    renderEffectorCatalog(data);
  } catch (error) {
    $("effectorActionStatus").textContent = error.message;
    $("effectorActionStatus").className = "activation-status danger-text";
    updateEffectorActionState();
  }
}

async function refresh() {
  $("refreshState").textContent = "Refreshing";
  try {
    const response = await fetch("/v1/ui/overview", { cache: "no-store" });
    if (!response.ok) {
      throw new Error(`Overview returned ${response.status}`);
    }
    const data = await response.json();
    const manager = data.core?.manager || {};
    const fabric = data.core?.fabric || {};
    const providers = data.providers || [];
    const skills = data.skills || [];

    $("managerStatus").textContent = String(manager.status || "unknown").toUpperCase();
    $("managerPolicy").textContent = manager.provider_autostart_enabled
      ? "Registry Provider autostart enabled"
      : "Observation-first · Provider autostart disabled";
    setLamp("managerLamp", manager.status === "ok" ? "ok" : "danger");

    $("fabricStatus").textContent = String(fabric.status || "unavailable").toUpperCase();
    $("fabricDetails").textContent = fabric.status === "ok"
      ? `${fabric.stream_count || 0} streams · ${fabric.transform_edge_count || 0} transform edges`
      : (fabric.error || "Fabric health unavailable");
    setLamp("fabricLamp", fabric.status === "ok" ? "ok" : "danger");

    $("providerMetric").textContent = `${providers.length} configured`;
    $("providerDetails").textContent = summarizeProviders(providers);
    $("skillMetric").textContent = `${skills.length} installed`;
    $("skillDetails").textContent = summarizeSkills(skills);

    const agents = data.agents || {};
    $("agentState").textContent = agents.online
      ? "Agent runtime is online. The developer view and run journal share one autonomous Agent backend."
      : "Agent runtime is offline by design. Start it explicitly when the Developer Agent is needed.";
    $("developerAgentLink").href = agents.developer_url || "http://127.0.0.1:8000/dev";
    const agentBaseUrl = agents.developer_url || agents.regular_url ||
      "http://127.0.0.1:8000/dev";
    $("runJournalLink").href = agents.journal_url ||
      new URL("/dev/run-journal", agentBaseUrl).toString();
    $("developerAgentLink").dataset.online = String(Boolean(agents.online));
    $("runJournalLink").dataset.online = String(Boolean(agents.online));

    renderProviders(providers);
    renderSkills(skills);
    await Promise.all([refreshArmCatalog(), refreshEffectorCatalog()]);
    $("observedAt").textContent = `Observed ${new Date(data.observed_at).toLocaleString()}`;
    $("refreshState").textContent = "Live";
  } catch (error) {
    $("refreshState").textContent = "Unavailable";
    setLamp("managerLamp", "danger");
    $("managerStatus").textContent = "UNAVAILABLE";
    $("managerPolicy").textContent = error.message;
  }
}

$("refreshButton").addEventListener("click", refresh);
$("armSelect").addEventListener("change", () => {
  armSelectionDirty = true;
  $("armPhysicalConfirmation").checked = false;
  updateArmActionState();
});
$("armPhysicalConfirmation").addEventListener("change", updateArmActionState);
$("selectArmButton").addEventListener("click", selectArm);
$("effectorSelect").addEventListener("change", () => {
  effectorSelectionDirty = true;
  $("effectorPhysicalConfirmation").checked = false;
  updateEffectorActionState();
});
$("effectorPhysicalConfirmation").addEventListener("change", updateEffectorActionState);
$("selectEffectorButton").addEventListener("click", selectEffector);
refresh();
window.setInterval(refresh, 2000);
