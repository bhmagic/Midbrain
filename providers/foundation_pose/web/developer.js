const byId = (id) => document.getElementById(id);
const providerState = byId("providerState");
const providerIdentity = byId("providerIdentity");
const backendState = byId("backendState");
const backendDetail = byId("backendDetail");
const resourceState = byId("resourceState");
const resourceDetail = byId("resourceDetail");
const cameraState = byId("cameraState");
const cameraDetail = byId("cameraDetail");
const actionStatus = byId("actionStatus");
const sessionsNode = byId("sessions");
const measurementsNode = byId("measurements");
const modelsNode = byId("models");
const modelId = byId("modelId");
const rawStatus = byId("rawStatus");
let selectedSessionId = null;
let latestStatus = null;

function setMetric(node, value, detail, kind = "") {
  const card = node.closest(".metric");
  card.className = "metric" + (kind ? ` ${kind}` : "");
  node.textContent = value;
  node.nextElementSibling.textContent = detail;
}

function formatNumber(value, digits = 3) {
  return typeof value === "number" && Number.isFinite(value)
    ? value.toFixed(digits)
    : "n/a";
}

function makeRow(title, state, fields, selectableId = null) {
  const row = document.createElement("article");
  row.className = "row" + (selectableId ? " selectable" : "");
  if (selectableId && selectableId === selectedSessionId) {
    row.classList.add("selected");
  }
  const head = document.createElement("div");
  head.className = "row-head";
  const name = document.createElement("div");
  name.className = "row-title";
  name.textContent = title;
  const pill = document.createElement("span");
  pill.className = "pill";
  pill.textContent = state;
  head.append(name, pill);
  const grid = document.createElement("div");
  grid.className = "row-grid";
  for (const [label, value] of fields) {
    const item = document.createElement("div");
    const key = document.createElement("b");
    key.textContent = `${label}: `;
    item.append(key, document.createTextNode(String(value ?? "n/a")));
    grid.append(item);
  }
  row.append(head, grid);
  if (selectableId) {
    row.addEventListener("click", () => {
      selectedSessionId = selectableId;
      renderSessions(latestStatus?.details?.sessions || []);
    });
  }
  return row;
}

function renderSessions(sessions) {
  byId("sessionCount").textContent = String(sessions.length);
  sessionsNode.replaceChildren();
  sessionsNode.className = sessions.length ? "list" : "list empty";
  if (!sessions.length) {
    sessionsNode.textContent = "No sessions recorded.";
    selectedSessionId = null;
    return;
  }
  if (
    selectedSessionId &&
    !sessions.some((session) => session.session_id === selectedSessionId)
  ) {
    selectedSessionId = null;
  }
  for (const session of sessions) {
    sessionsNode.append(makeRow(
      session.session_id,
      session.state || "UNKNOWN",
      [
        ["operation", session.operation],
        ["model", session.model_id],
        ["frame", `${session.parent_frame} → ${session.child_frame}`],
        ["results", session.result_count],
        ["latency", `${formatNumber(session.last_latency_ms, 1)} ms`],
        ["error", session.last_error || "none"],
      ],
      session.session_id,
    ));
  }
}

function renderMeasurements(measurements) {
  byId("measurementCount").textContent = String(measurements.length);
  measurementsNode.replaceChildren();
  measurementsNode.className = measurements.length ? "list" : "list empty";
  if (!measurements.length) {
    measurementsNode.textContent = "No pose result published.";
    return;
  }
  for (const pose of measurements) {
    const translation = Array.isArray(pose.translation_m)
      ? pose.translation_m.map((value) => formatNumber(value)).join(", ")
      : "n/a";
    const rotation = Array.isArray(pose.quaternion_xyzw)
      ? pose.quaternion_xyzw.map((value) => formatNumber(value, 4)).join(", ")
      : "n/a";
    measurementsNode.append(makeRow(
      pose.model_id || pose.target_id || "Unknown model",
      pose.tracking_state || pose.mode || "MEASUREMENT",
      [
        ["session", pose.tracking_session_id],
        ["translation m", `[${translation}]`],
        ["rotation xyzw", `[${rotation}]`],
        ["frames", `${pose.parent_frame} → ${pose.child_frame}`],
        ["source frame", pose.source_frame_number],
        ["latency", `${formatNumber(pose.latency_ms, 1)} ms`],
      ],
    ));
  }
}

function renderModels(payload) {
  const models = Array.isArray(payload.models) ? payload.models : [];
  byId("modelCount").textContent = String(models.length);
  modelsNode.replaceChildren();
  modelsNode.className = models.length ? "list" : "list empty";
  modelId.replaceChildren();
  if (!models.length) {
    modelsNode.textContent = "No enabled models are registered.";
    return;
  }
  for (const model of models) {
    const option = document.createElement("option");
    option.value = model.model_id;
    option.textContent = model.model_id;
    modelId.append(option);
    modelsNode.append(makeRow(
      model.model_id,
      model.enabled ? "ENABLED" : "DISABLED",
      [
        ["semantic frame", model.semantic_frame],
        ["role hint", model.role],
        ["revision", model.revision],
        ["mesh", model.mesh_exists ? "available" : "missing"],
      ],
    ));
  }
}

async function readJson(response) {
  const data = await response.json();
  if (!response.ok) {
    throw new Error(data.error || data.detail || JSON.stringify(data));
  }
  return data;
}

async function refresh() {
  try {
    const [statusResponse, modelsResponse] = await Promise.all([
      fetch("/health", {cache: "no-store"}),
      fetch("/v1/dev/models", {cache: "no-store"}),
    ]);
    const status = await readJson(statusResponse);
    const models = await readJson(modelsResponse);
    latestStatus = status;
    const details = status.details || {};
    const backend = details.backend_diagnostics || {};
    const runtimeLoaded = Boolean(backend.runtime_loaded);
    const activeEstimators = Number(backend.active_estimators || 0);
    const cachedEstimators = Number(backend.cached_prepared_estimators || 0);
    setMetric(
      providerState,
      `${status.health || "UNKNOWN"} · ${status.residency || "UNKNOWN"}`,
      `${status.provider_id || "unknown"} · ${String(status.instance_id || "").slice(0, 8)}`,
      status.health === "HEALTHY" ? "ok" : "warn",
    );
    setMetric(
      backendState,
      runtimeLoaded ? "CUDA runtime loaded" : "Runtime released",
      `${details.backend || "unknown"} · ${backend.foundationpose_root || "backend path unavailable"}`,
      runtimeLoaded ? "ok" : "",
    );
    setMetric(
      resourceState,
      `${activeEstimators} active · ${cachedEstimators} cached`,
      `cache limit ${backend.prepared_model_cache_size ?? "n/a"} · GPU ${details.resource_profile?.gpu_required ? "required" : "not required"}`,
      activeEstimators ? "ok" : "",
    );
    const frameNumber = Number(details.last_camera_frame_number ?? -1);
    setMetric(
      cameraState,
      frameNumber >= 0 ? `Frame ${frameNumber}` : "Waiting",
      `${details.camera_frame || "unknown"} · calibration ${details.camera_calibration_revision || "unknown"}`,
      frameNumber >= 0 ? "ok" : "warn",
    );
    renderSessions(details.sessions || []);
    renderMeasurements(details.latest_measurements || []);
    renderModels(models);
    rawStatus.textContent = JSON.stringify(status, null, 2);
  } catch (error) {
    setMetric(providerState, "Unavailable", String(error), "bad");
    rawStatus.textContent = String(error);
  }
}

function boundingBoxPayload() {
  const values = [
    Number(byId("boxYMin").value),
    Number(byId("boxXMin").value),
    Number(byId("boxYMax").value),
    Number(byId("boxXMax").value),
  ];
  if (values.some((value) => !Number.isFinite(value))) {
    throw new Error("All four bounding-box coordinates are required.");
  }
  return {
    box_2d: values,
    coordinate_space: "normalized_0_1000",
    padding_fraction: 0,
  };
}

async function request(path, body, description) {
  actionStatus.className = "action-status";
  actionStatus.textContent = `${description}…`;
  try {
    const response = await fetch(path, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(body),
    });
    const data = await readJson(response);
    actionStatus.textContent = `${description} completed: ${data.status || data.state || "accepted"}`;
    await refresh();
    return data;
  } catch (error) {
    actionStatus.className = "action-status error";
    actionStatus.textContent = `${description} failed: ${error.message}`;
    throw error;
  }
}

byId("refreshButton").addEventListener("click", refresh);
byId("hotButton").addEventListener("click", () => request(
  "/v1/control/hot",
  {},
  "HOT transition",
).catch(() => {}));
byId("warmButton").addEventListener("click", () => {
  if (!window.confirm("Stop all Provider-owned pose sessions and release FoundationPose GPU resources?")) return;
  request("/v1/control/warm", {}, "WARM transition").catch(() => {});
});
byId("releaseButton").addEventListener("click", () => {
  if (!window.confirm("Release unused FoundationPose estimators and GPU resources? Active sessions make this request fail.")) return;
  request(
    "/v1/control/request",
    {action: "release_resources", payload: {reason: "provider development UI"}},
    "Resource release",
  ).catch(() => {});
});
byId("startButton").addEventListener("click", () => {
  const selectedModel = modelId.value;
  if (!selectedModel) {
    actionStatus.className = "action-status error";
    actionStatus.textContent = "Select a registered model first.";
    return;
  }
  const maskPath = byId("maskPath").value.trim();
  const payload = {
    session_id: byId("sessionId").value.trim() || undefined,
    model_id: selectedModel,
    target_id: byId("targetId").value.trim() || selectedModel,
    parent_frame: byId("parentFrame").value.trim(),
    child_frame: byId("childFrame").value.trim() || `observed_object/${selectedModel}`,
    max_duration_s: Number(byId("duration").value),
    max_update_hz: Number(byId("updateRate").value),
  };
  if (maskPath) payload.mask_path = maskPath;
  else payload.bounding_box = boundingBoxPayload();
  request(
    "/v1/control/request",
    {action: byId("operation").value, payload},
    "Pose-session request",
  ).then((data) => {
    selectedSessionId = data.session_id || payload.session_id || null;
  }).catch(() => {});
});
byId("relocalizeButton").addEventListener("click", () => {
  if (!selectedSessionId) {
    actionStatus.className = "action-status error";
    actionStatus.textContent = "Select a session first.";
    return;
  }
  const maskPath = byId("maskPath").value.trim();
  const payload = {session_id: selectedSessionId};
  if (maskPath) payload.mask_path = maskPath;
  else payload.bounding_box = boundingBoxPayload();
  request(
    "/v1/control/request",
    {action: "relocalize", payload},
    "Relocalization request",
  ).catch(() => {});
});
byId("stopButton").addEventListener("click", () => {
  if (!selectedSessionId) {
    actionStatus.className = "action-status error";
    actionStatus.textContent = "Select a session first.";
    return;
  }
  if (!window.confirm(`Stop pose session ${selectedSessionId}?`)) return;
  request(
    "/v1/control/request",
    {action: "stop", payload: {session_id: selectedSessionId}},
    "Session stop",
  ).catch(() => {});
});

refresh();
setInterval(refresh, 1500);
