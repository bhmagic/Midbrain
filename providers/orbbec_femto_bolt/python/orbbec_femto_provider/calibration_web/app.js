const poses = [
  { id: "x+", title: "+X", detail: "Rotate until raw X is strongly positive and dominates Y/Z." },
  { id: "x-", title: "−X", detail: "Rotate until raw X is strongly negative and dominates Y/Z." },
  { id: "y+", title: "+Y", detail: "Rotate until raw Y is strongly positive and dominates X/Z." },
  { id: "y-", title: "−Y", detail: "Rotate until raw Y is strongly negative and dominates X/Z." },
  { id: "z+", title: "+Z", detail: "Rotate until raw Z is strongly positive and dominates X/Y." },
  { id: "z-", title: "−Z", detail: "Rotate until raw Z is strongly negative and dominates X/Y." },
];

const state = { status: null, busy: false };
const poseGrid = document.querySelector("#poseGrid");
const message = document.querySelector("#message");
const solveButton = document.querySelector("#solveButton");

for (const pose of poses) {
  const card = document.createElement("article");
  card.className = "pose-card";
  card.id = `pose-${pose.id.replace("+", "plus").replace("-", "minus")}`;
  card.innerHTML = `
    <div class="pose-target">${pose.title}</div>
    <h3>Target orientation</h3>
    <p class="pose-detail">${pose.detail}</p>
    <div class="pose-stats" data-stats="${pose.id}">Not captured</div>
    <button type="button" data-pose="${pose.id}">Capture 2 seconds</button>
  `;
  poseGrid.appendChild(card);
}

poseGrid.addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-pose]");
  if (!button || state.busy) return;
  await capturePose(button.dataset.pose, button);
});

document.querySelector("#resetButton").addEventListener("click", async () => {
  if (state.busy) return;
  await postJson("/api/reset", {});
  showMessage("Capture session reset.", "success");
  await refreshStatus();
});

solveButton.addEventListener("click", async () => {
  if (state.busy) return;
  setBusy(true);
  showMessage("Solving and writing the device-specific calibration…");
  try {
    const result = await postJson("/api/solve-and-write", {});
    renderResult(result);
    showMessage("Calibration written successfully.", "success");
  } catch (error) {
    showMessage(error.message, "error");
  } finally {
    setBusy(false);
    await refreshStatus();
  }
});

async function capturePose(pose, button) {
  setBusy(true);
  const original = button.textContent;
  const duration = state.status?.capture_seconds ?? 2;
  const started = performance.now();
  const timer = setInterval(() => {
    const elapsed = (performance.now() - started) / 1000;
    button.textContent = `Hold still… ${Math.max(0, duration - elapsed).toFixed(1)} s`;
  }, 80);
  showMessage(`Capturing ${pose}. Keep the camera completely still.`);
  try {
    const result = await postJson("/api/capture", { pose });
    showMessage(`${pose} accepted: ${result.capture.sample_count} samples.`, "success");
  } catch (error) {
    showMessage(`${pose} rejected: ${error.message}`, "error");
  } finally {
    clearInterval(timer);
    button.textContent = original;
    setBusy(false);
    await refreshStatus();
  }
}

async function refreshStatus() {
  if (state.busy) return;
  try {
    const response = await fetch("/api/status", { cache: "no-store" });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Camera is unavailable");
    state.status = data;
    renderStatus(data);
  } catch (error) {
    document.querySelector("#connectionBadge").textContent = "Camera unavailable";
    document.querySelector("#connectionBadge").className = "badge bad";
    showMessage(`${error.message}. Start the workspace and keep the camera Provider HOT.`, "error");
  }
}

function renderStatus(data) {
  const badge = document.querySelector("#connectionBadge");
  badge.textContent = "Camera connected";
  badge.className = "badge good";
  setText("deviceName", data.device.name || "Femto Bolt");
  setText("serialNumber", data.device.serial_number);
  setText("calibrationStatus", `${data.calibration.status} · ${data.calibration.revision}`);
  setText("calibrationPath", data.calibration.path);

  const raw = data.raw_accelerometer;
  setText("liveX", formatNumber(raw?.x));
  setText("liveY", formatNumber(raw?.y));
  setText("liveZ", formatNumber(raw?.z));
  setText("liveMagnitude", formatNumber(raw?.magnitude_m_s2));

  for (const pose of poses) {
    const capture = data.captures[pose.id];
    const stats = document.querySelector(`[data-stats="${pose.id}"]`);
    const card = stats.closest(".pose-card");
    const button = card.querySelector("button");
    if (capture) {
      const mean = capture.mean_m_s2.map((value) => value.toFixed(4)).join(", ");
      stats.textContent = `mean [${mean}]\nσ ${capture.quality.rms_noise_m_s2.toFixed(4)} · n=${capture.sample_count}`;
      stats.style.whiteSpace = "pre-line";
      card.classList.add("done");
      button.textContent = "Retake 2 seconds";
    } else {
      stats.textContent = "Not captured";
      card.classList.remove("done");
      button.textContent = "Capture 2 seconds";
    }
  }
  solveButton.disabled = state.busy || poses.some((pose) => !data.captures[pose.id]);
}

function renderResult(result) {
  const solution = result.solution;
  const write = result.write;
  setText("resultScale", solution.scale.map((value) => value.toFixed(10)).join(" / "));
  setText("resultOffset", solution.offset.map((value) => value.toFixed(10)).join(" / "));
  setText("resultRms", `${solution.rms_residual_m_s2.toExponential(4)} m/s²`);
  setText("resultMax", `${solution.max_abs_residual_m_s2.toExponential(4)} m/s²`);
  setText("resultCondition", solution.design_condition_number.toExponential(4));
  setText("resultRevision", write.revision);
  const reload = write.provider_reload || {};
  document.querySelector("#writeMessage").textContent = reload.status === "reloaded"
    ? `Saved to ${write.path}. The running camera Provider reloaded the new calibration.`
    : `Saved to ${write.path}. Provider reload was not confirmed; restart the Provider before relying on the new coefficients.`;
  document.querySelector("#resultPanel").classList.remove("hidden");
}

function setBusy(value) {
  state.busy = value;
  for (const button of document.querySelectorAll("button")) button.disabled = value;
  if (!value && state.status) {
    solveButton.disabled = poses.some((pose) => !state.status.captures[pose.id]);
  }
}

async function postJson(url, body) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
  return data;
}

function setText(id, value) {
  document.querySelector(`#${id}`).textContent = value ?? "—";
}

function formatNumber(value) {
  return Number.isFinite(value) ? `${value.toFixed(4)} m/s²` : "—";
}

function showMessage(text, kind = "") {
  message.textContent = text;
  message.className = `message ${kind}`.trim();
}

refreshStatus();
setInterval(refreshStatus, 350);
