const $ = (id) => document.getElementById(id);
let selectedView = "overlay";
let lastPlanId = null;
let toastTimer = null;
let restartInProgress = false;
let failedSessionResetInProgress = false;

async function api(path, method = "GET", body = null) {
  const response = await fetch(path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : null,
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(payload.detail || `${method} ${path} failed`);
  }
  return payload;
}

function toast(message, error = false) {
  const node = $("toast");
  node.textContent = message;
  node.className = `toast show${error ? " error" : ""}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { node.className = "toast"; }, 4500);
}

function readyItem(name, ok, detail) {
  return `<div class="ready-item ${ok ? "ok" : "bad"}"><span>${name}</span><strong>${ok ? "READY" : "CHECK"}</strong><small>${detail || ""}</small></div>`;
}

function renderReadiness(progress) {
  const value = progress.provider_readiness || {};
  const manager = value.manager || {};
  const fabric = value.fabric || {};
  const integrated = value.integrated_state || {};
  const alignment = progress.alignment || {};
  const fixedCameraLock = alignment.fixed_camera_transform_lock || {};
  const fixedCameraLocked = (
    fixedCameraLock.applied === true
    && ["stopped", "killed"].includes(
      String((fixedCameraLock.local_vio_stop || {}).status || "").toLowerCase(),
    )
  );
  $("readinessGrid").innerHTML = [
    readyItem("Manager", manager.status === "ok", manager.status || manager.error),
    readyItem("Fabric", fabric.status === "ok", fabric.status || fabric.error),
    readyItem("Integrated", value.integrated_idle === true, value.integrated_idle ? "idle + disengaged" : (value.integrated_idle_reasons || []).join(", ")),
    readyItem("Alignment", alignment.valid === true, alignment.alignment_id || alignment.error || "not verified"),
    readyItem(
      "Localization",
      fixedCameraLocked || Boolean(alignment.vio_session_epoch),
      fixedCameraLocked
        ? "FIXED CAMERA LOCKED · VIO OFF BY DESIGN"
        : (alignment.vio_session_epoch || "VIO not established"),
    ),
  ].join("");
}

function renderPlan(plan) {
  if (!plan) {
    lastPlanId = null;
    $("planStatus").textContent = "No active plan";
    $("planSummary").innerHTML = [
      ["Plan", "Not generated for this session"],
      ["Motion submitted", "False"],
    ].map(([key, value]) => `<div><dt>${key}</dt><dd class="${key === "Motion submitted" ? "safe" : ""}">${value}</dd></div>`).join("");
    $("executionSequence").innerHTML = `<div class="sequence-empty">Generate a plan to inspect the proposed stages.</div>`;
    return;
  }
  lastPlanId = plan.plan_id;
  const blockerText = (plan.execution_blockers || []).join("; ");
  $("planStatus").textContent = "Reviewed plan ready";
  const registration = plan.blade_registration_candidate || {};
  const actingPoint = registration.acting_point_from_tool_m;
  const actingPointText = Array.isArray(actingPoint)
    ? `[${actingPoint.map((value) => Number(value).toFixed(3)).join(", ")}] m`
    : "Unavailable";
  const executionPreview = plan.execution_preview || {};
  const consistency = plan.blade_registration_consistency || {};
  const consistencyMetrics = consistency.quality_metrics || {};
  const firstCutAlignment = plan.first_cut_alignment_contract || {};
  $("planSummary").innerHTML = [
    ["Plan", plan.plan_id],
    ["Revision", plan.plan_revision],
    ["Cut paths", (plan.cuts || []).length],
    ["Spacing", `${plan.planning_parameters?.slice_spacing_mm ?? "—"} mm`],
    ["Board shape", "Display only; not used for planning"],
    ["Cut geometry", plan.planning_parameters?.path_source || "Two-point 3D line"],
    ["Tool registration", registration.status || "Unavailable"],
    ["Acting point from tool", actingPointText],
    ["Tool payload", "0.07 kg; zero COM offset"],
    ["Registration findings", (registration.quality_reasons || []).join("; ") || "Configured hard mount"],
    ["Registration consistency", consistency.status || "Unavailable"],
    ["VLM blade landmarks", "Not used for motion"],
    ["Observation window", `${consistency.valid_window_observations ?? 0} / ${consistency.required_observations ?? 0} required`],
    ["Acting-point deviation", `${Number(consistencyMetrics.maximum_acting_point_deviation_mm ?? 0).toFixed(2)} mm`],
    ["Execution preview", executionPreview.status || "Unavailable"],
    ["Preview segments", (executionPreview.segments || []).length],
    ["Approach above board", `${Number(executionPreview.approach_board_offset_mm ?? 0).toFixed(1)} mm`],
    ["Later-cut perception", "No coordinate recheck after human first-cut approval"],
    ["First-cut correction", firstCutAlignment.status || "Unavailable"],
    ["Correction limit", `${Number(firstCutAlignment.maximum_translation_mm ?? 0).toFixed(1)} mm; operator confirmation required`],
    ["Tool calibrated", plan.tool_calibration?.complete ? "True" : "False"],
    ["Motion submitted", plan.motion_submitted ? "True" : "False"],
    ["Execution blockers", blockerText || "Operator takeover boundary"],
  ].map(([key, value]) => `<div><dt>${key}</dt><dd class="${key === "Motion submitted" ? "safe" : ""}">${value}</dd></div>`).join("");
  const segments = executionPreview.segments || [];
  $("executionSequence").innerHTML = segments.length
    ? segments.map((segment) => {
      const isGate = ["PERCEPTION_GATE", "HUMAN_GATE"].includes(segment.backend);
      return `<div class="sequence-item ${isGate ? "perception" : "controller"}">
        <span class="sequence-number">${Number(segment.sequence) + 1}</span>
        <div>
          <strong>${segment.action}</strong>
          <small>Cut ${Number(segment.cut_index) + 1} · ${segment.backend || "N/A"}</small>
        </div>
        <span class="sequence-lock">TAKEOVER GATED</span>
      </div>`;
    }).join("")
    : `<div class="sequence-empty">No execution stages are available.</div>`;
  showImage(selectedView);
}

function renderTracking() {
  $("trackingSummary").innerHTML = [
    ["VLM after each cut", "False"],
    ["Coordinate recheck after approval", "False"],
    ["Sequence authority", "Human-approved first-cut placement"],
  ].map(([key, value]) => `<div><dt>${key}</dt><dd>${value}</dd></div>`).join("");
  return;
  if (!tracking) {
    return;
  }
  $("trackingSummary").innerHTML = [
    ["VLM after each cut", "False"],
    ["Fast tracking accepted", tracking.accepted_without_vlm ? "True" : "False"],
    ["VLM called", tracking.vlm_called ? "True" : "False"],
    ["Mask IoU", Number(tracking.mask_iou ?? 0).toFixed(3)],
    ["Centroid shift", `${Number(tracking.centroid_shift_mm ?? 0).toFixed(2)} mm`],
    ["Axis change", `${Number(tracking.axis_change_deg ?? 0).toFixed(2)}°`],
    ["Reasons", (tracking.reasons || []).join(", ") || "none"],
  ].map(([key, value]) => `<div><dt>${key}</dt><dd>${value}</dd></div>`).join("");
}

function renderControls(progress) {
  const phase = progress.phase || "IDLE";
  const executionState = String((progress.execution || {}).state || "");
  const runningExecution = [
    "TRANSFER_TO_FIRST_CUT",
    "CUTTING",
  ].includes(phase);
  $("executeButton").disabled = !(
    phase === "READY_FOR_OPERATOR_TAKEOVER"
    && progress.motion_submission_enabled
  );
  const waitingFirstCut = phase === "WAIT_FIRST_CUT_CONFIRMATION";
  $("firstCutYesButton").disabled = !waitingFirstCut;
  $("firstCutReadjustButton").disabled = !waitingFirstCut;
  $("firstCutStopButton").disabled = !waitingFirstCut;
  $("toolRemovedButton").disabled = phase !== "WAIT_TOOL_REMOVAL";
  $("abortButton").disabled = ["IDLE", "COMPLETED", "ABORTED"].includes(
    progress.state,
  );
  const restartBlocked = runningExecution || phase === "SAFE_TERMINATING";
  $("restartGuiButton").disabled = restartInProgress || restartBlocked;
  $("restartGuiButton").title = restartBlocked
    ? "Request Float or wait for the physical sequence to become idle."
    : "Reload only the cutting GUI; the Skill session and providers remain running.";
  const failedSessionResetAvailable = progress.state === "FAILED";
  $("resetFailedSessionButton").disabled = (
    failedSessionResetInProgress || !failedSessionResetAvailable
  );
  $("resetFailedSessionButton").title = failedSessionResetAvailable
    ? "Clear FAILED software state without motion, Float, or gripper commands."
    : "Available only after a cutting session enters FAILED.";
  if (runningExecution) {
    $("planStatus").textContent = "Physical execution active";
  } else if (phase === "WAIT_FIRST_CUT_CONFIRMATION") {
    $("planStatus").textContent = "Waiting for first-cut confirmation";
  } else if (phase === "WAIT_TOOL_REMOVAL") {
    $("planStatus").textContent = executionState.startsWith("ABORTED_")
      ? "Stopped — remove knife before safe home"
      : "Cuts complete — remove knife";
  } else if (progress.motion_submission_enabled) {
    $("planStatus").textContent = "Session calibration ready — takeover required";
  }
}

async function refresh() {
  try {
    const payload = await api("/api/status");
    const progress = payload.progress || {};
    $("phase").textContent = String(progress.phase || "IDLE").replaceAll("_", " ");
    $("stateBadge").textContent = progress.state || "IDLE";
    $("message").textContent = progress.message || "";
    const persistentError = $("persistentError");
    persistentError.textContent = progress.error
      ? `Last error: ${progress.error}`
      : "";
    persistentError.hidden = !progress.error;
    renderReadiness(progress);
    renderPlan(payload.latest_plan);
    renderTracking();
    renderControls(progress);
  } catch (error) {
    $("message").textContent = error.message;
  }
}

function showImage(kind) {
  selectedView = kind;
  document.querySelectorAll("[data-view]").forEach((button) => {
    button.classList.toggle("active", button.dataset.view === kind);
  });
  const image = $("visionImage");
  image.onload = () => {
    image.style.display = "block";
    $("imageEmpty").style.display = "none";
  };
  image.onerror = () => {
    image.style.display = "none";
    $("imageEmpty").style.display = "block";
  };
  image.src = `/api/image/${kind}?v=${Date.now()}`;
}

async function action(callback, success) {
  try {
    await callback();
    toast(success);
    await refresh();
  } catch (error) {
    toast(error.message, true);
    const persistentError = $("persistentError");
    persistentError.textContent = `Last action failed: ${error.message}`;
    persistentError.hidden = false;
    await refresh();
  }
}

$("bootstrapButton").addEventListener("click", () => action(
  () => api("/api/providers/bootstrap", "POST"),
  "Provider startup requested. No motion command was sent.",
));

$("restartGuiButton").addEventListener("click", async () => {
  const confirmed = window.confirm(
    "Reload the cutting GUI while preserving the current Skill session, "
    + "fixed-camera transform, and providers?",
  );
  if (!confirmed) {
    return;
  }
  const button = $("restartGuiButton");
  restartInProgress = true;
  button.disabled = true;
  button.textContent = "Reloading...";
  try {
    const result = await api("/api/gui/restart", "POST");
    toast(result.message || "Cutting GUI reload accepted.");
    window.location.reload();
  } catch (error) {
    restartInProgress = false;
    button.disabled = false;
    button.textContent = "Reload GUI";
    toast(error.message, true);
  }
});

$("resetFailedSessionButton").addEventListener("click", async () => {
  const confirmed = window.confirm(
    "Clear the FAILED Skill session without sending motion, Float, or gripper "
    + "commands? The fixed-camera transform and physical tool attachment are "
    + "preserved.",
  );
  if (!confirmed) {
    return;
  }
  const button = $("resetFailedSessionButton");
  failedSessionResetInProgress = true;
  button.disabled = true;
  button.textContent = "Resetting...";
  try {
    const result = await api("/api/session/reset-failed", "POST");
    toast(result.message || "Failed session state cleared.");
    $("persistentError").hidden = true;
    await refresh();
  } catch (error) {
    toast(error.message, true);
  } finally {
    failedSessionResetInProgress = false;
    button.textContent = "Reset failed session";
    await refresh();
  }
});

$("captureButton").addEventListener("click", () => action(
  async () => {
    await api("/api/camera/capture", "POST");
    showImage("rgb");
  },
  "Fresh RGB-D frame captured without motion.",
));

$("alignmentButton").addEventListener("click", async () => {
  const alignmentWindow = window.open("", "midbrain-stationary-alignment");
  const button = $("alignmentButton");
  button.disabled = true;
  try {
    const result = await api("/api/alignment/gui", "POST");
    if (alignmentWindow) {
      alignmentWindow.location.href = result.url;
    } else {
      toast(`Alignment GUI is ready at ${result.url}; allow pop-ups to open it.`);
    }
    toast(
      result.reused
        ? "Existing alignment GUI opened."
        : "Alignment GUI started without motion submission.",
    );
    await refresh();
  } catch (error) {
    if (alignmentWindow) {
      alignmentWindow.close();
    }
    toast(error.message, true);
  } finally {
    button.disabled = false;
  }
});

$("startButton").addEventListener("click", () => action(
  () => api("/api/session/start", "POST", {
    slice_spacing_mm: Number($("spacing").value),
    blade_yaw_deg: Number($("yaw").value),
    maximum_cut_count: Number($("maxCuts").value),
  }),
  "Planning session started.",
));

$("toolButton").addEventListener("click", () => action(
  () => api("/api/session/confirm-tool", "POST", {
    operator_confirms_knife_attached: $("toolAttached").checked,
  }),
  "Human knife-attachment confirmation recorded.",
));

$("workpieceButton").addEventListener("click", () => action(
  () => api("/api/session/confirm-workpiece", "POST", {
    operator_outside_workspace: $("withdrawn").checked,
  }),
  "Workpiece and operator-withdrawal confirmations recorded.",
));

$("planButton").addEventListener("click", () => action(
  () => api("/api/session/plan", "POST"),
  "Visual plan generated without motion submission.",
));

$("executeButton").addEventListener("click", () => action(
  () => api("/api/session/execute", "POST", {
    operator_takeover_confirmed: $("takeoverConfirmed").checked,
  }),
  "Operator takeover recorded; moving to the bounded first-cut review approach.",
));

$("firstCutYesButton").addEventListener("click", () => action(
  () => api("/api/session/first-cut-decision", "POST", { decision: "YES" }),
  "First-cut location confirmed; automatic cut sequence started.",
));

$("firstCutReadjustButton").addEventListener("click", () => action(
  () => api("/api/session/first-cut-decision", "POST", { decision: "NO_READJUST" }),
  "A bounded first-cut VLM correction round was requested.",
));

$("firstCutStopButton").addEventListener("click", () => action(
  () => api("/api/session/first-cut-decision", "POST", { decision: "FULL_STOP_GO_HOME" }),
  "Full stop requested; Float requested. Release and remove the knife before safe home.",
));

$("toolRemovedButton").addEventListener("click", () => action(
  () => api("/api/session/tool-removed-safe-terminate", "POST"),
  "Tool removal verified; safe termination started.",
));

$("abortButton").addEventListener("click", () => action(
  () => api("/api/session/abort", "POST", { reason: "GUI workflow abort" }),
  "Execution cancelled and Integrated gravity-float requested.",
));

document.querySelectorAll("[data-view]").forEach((button) => {
  button.addEventListener("click", () => showImage(button.dataset.view));
});

refresh();
setInterval(refresh, 1500);
