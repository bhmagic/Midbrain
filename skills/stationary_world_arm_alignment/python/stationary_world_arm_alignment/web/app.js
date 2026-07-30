const el = id => document.getElementById(id);
let selectedImage = "overlay";
let imageRevision = 0;
let statusValue = null;
let candidateReviewValue = null;

function toast(message) {
  const node = el("toast");
  node.textContent = message;
  node.classList.add("show");
  setTimeout(() => node.classList.remove("show"), 3200);
}

async function post(url, body) {
  const response = await fetch(url, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: body ? JSON.stringify(body) : undefined
  });
  if (!response.ok) {
    const value = await response.json().catch(() => ({}));
    const detail = value.detail || {};
    throw new Error(detail.message || detail || `Request failed: ${response.status}`);
  }
  return response.json();
}

function reviewedProvenance(candidate) {
  const camera = candidate.camera_provenance || {};
  const vio = candidate.vio_provenance || {};
  return {
    workcell_calibration_revision: candidate.workcell_calibration_revision,
    camera_provider_id: camera.provider_id ?? null,
    camera_provider_instance_id: camera.provider_instance_id ?? null,
    camera_boot_id: camera.boot_id ?? null,
    camera_calibration_revision: camera.calibration_revision ?? null,
    vio_session_epoch: vio.session_epoch ?? null
  };
}

function renderCandidateReviews(value) {
  candidateReviewValue = value;
  const available = value.identity_verification_available === true;
  const authState = el("review-auth-state");
  authState.textContent = available ? "EXTERNAL IDENTITY READY" : "FAIL-CLOSED";
  authState.className = `runtime-state ${available ? "ready" : "degraded"}`;
  el("review-auth-message").textContent = available
    ? "A trusted upstream identity service must attach a decision-scoped assertion."
    : "No external identity verifier is configured. Approval and rejection are disabled.";
  const candidates = value.candidates || [];
  el("candidate-list").innerHTML = candidates.length ? candidates.map(view => {
    const candidate = view.candidate;
    const camera = candidate.camera_provenance || {};
    const vio = candidate.vio_provenance || {};
    const finalDecision = view.decision;
    const disabled = !available || view.expired || Boolean(finalDecision);
    const state = finalDecision
      ? finalDecision.decision_state
      : view.expired ? "EXPIRED" : "REVIEW REQUIRED";
    return `
      <article class="candidate-card">
        <div class="candidate-head">
          <div>
            <strong class="mono">${escapeHtml(candidate.candidate_id)}</strong>
            <span>${escapeHtml(state)}</span>
          </div>
          <small class="mono">${escapeHtml(view.candidate_sha256)}</small>
        </div>
        <dl>
          <div><dt>Method</dt><dd>${escapeHtml((candidate.method || {}).base_pose_engine_route || "unknown")}</dd></div>
          <div><dt>Camera boot</dt><dd class="mono">${escapeHtml(camera.boot_id || "unavailable")}</dd></div>
          <div><dt>Calibration</dt><dd class="mono">${escapeHtml(camera.calibration_revision || "unavailable")}</dd></div>
          <div><dt>VIO epoch</dt><dd class="mono">${escapeHtml(vio.session_epoch || "unavailable")}</dd></div>
          <div><dt>Expires</dt><dd>${new Date(Number(candidate.expires_at_us) / 1000).toLocaleString()}</dd></div>
          <div><dt>Motion usable</dt><dd>false</dd></div>
        </dl>
        <div class="review-actions">
          <button data-review="${escapeHtml(view.alignment_id)}" data-decision="APPROVE" ${disabled ? "disabled" : ""}>Approve for activation</button>
          <button class="danger" data-review="${escapeHtml(view.alignment_id)}" data-decision="REJECT" ${disabled ? "disabled" : ""}>Reject</button>
        </div>
      </article>`;
  }).join("") : '<p class="muted">No calibration candidate is available.</p>';
  document.querySelectorAll("[data-review]").forEach(button => {
    button.onclick = () => submitCandidateReview(
      button.dataset.review,
      button.dataset.decision
    );
  });
}

async function submitCandidateReview(alignmentId, decision) {
  const view = (candidateReviewValue.candidates || []).find(
    item => item.alignment_id === alignmentId
  );
  if (!view) return;
  const rationale = window.prompt(
    `${decision} ${alignmentId}. Enter a review rationale:`
  );
  if (rationale === null) return;
  try {
    await post(`/api/candidate-reviews/${encodeURIComponent(alignmentId)}/decision`, {
      decision,
      candidate_sha256: view.candidate_sha256,
      expected_provenance: reviewedProvenance(view.candidate),
      idempotency_key: crypto.randomUUID(),
      rationale
    });
    toast(`${decision} decision recorded; transform remains inactive`);
    await refreshCandidateReviews();
  } catch (error) {
    toast(error.message);
  }
}

async function refreshCandidateReviews() {
  try {
    const response = await fetch("/api/candidate-reviews", {cache: "no-store"});
    renderCandidateReviews(await response.json());
  } catch (_) {
    el("review-auth-state").textContent = "OFFLINE";
    el("review-auth-state").className = "runtime-state degraded";
  }
}

function start(mode) {
  post("/api/run", {
    mode,
    arm_is_home: el("arm-home").checked,
    allow_active_control_interrupt: el("allow-active").checked
  }).then(() => toast(`${mode} alignment accepted`)).catch(error => toast(error.message));
}

el("run-auto").onclick = () => start("auto");
el("run-base-vlm").onclick = () => start("foundation_base_vlm_gripper");
el("run-foundation-dual").onclick = () => start("foundation_base_gripper");
el("run-refine").onclick = () => start("vlm_gripper_only");
el("cancel").onclick = () => post("/api/cancel").then(() => toast("Cancellation requested"));
el("request-providers").onclick = () => post("/api/providers/request")
  .then(value => toast(value.accepted ? "Provider request started" : value.reason))
  .catch(error => toast(error.message));
el("clear-cloud").onclick = () => post("/api/point-cloud/clear").then(() => toast("Point trail cleared"));
document.querySelectorAll("[data-image]").forEach(button => {
  button.onclick = () => {
    selectedImage = button.dataset.image;
    document.querySelectorAll("[data-image]").forEach(item => item.classList.toggle("active", item === button));
    refreshImage();
  };
});

function updateStatus(value) {
  statusValue = value;
  updateRuntime(value.runtime || {});
  const progress = value.progress;
  const state = String(progress.state || "PENDING");
  const pill = el("state-pill");
  pill.textContent = state;
  pill.className = `pill ${state.toLowerCase()}`;
  el("phase").textContent = progress.phase || "IDLE";
  el("elapsed").textContent = `${Number(progress.elapsed_s || 0).toFixed(1)} s`;
  el("message").textContent = progress.message || "";
  const bar = el("progress-bar");
  const fraction = progress.total_units ? progress.completed_units / progress.total_units : 0;
  bar.style.width = `${Math.max(0, Math.min(100, fraction * 100))}%`;
  bar.classList.toggle("indeterminate", progress.progress_kind === "indeterminate" && state === "RUNNING");
  const samples = (progress.details || {}).samples || {};
  const poseValidation = (progress.details || {}).pose_validation || {};
  el("base-samples").textContent = samples.base || 0;
  el("pose-attempt").textContent = `${poseValidation.attempt || 0} / ${poseValidation.maximum_attempts || 2}`;
  el("provider-state").textContent = progress.provider_responsive === true ? "responsive" :
    progress.provider_responsive === false ? "unreachable" : "—";
  el("point-count").textContent = Number(value.point_cloud.point_count || 0).toLocaleString();
  const busy = state === "RUNNING";
  ["run-auto", "run-base-vlm", "run-foundation-dual", "run-refine"].forEach(id => el(id).disabled = busy);
  el("cancel").disabled = !busy;

  const sessions = progress.provider_sessions || [];
  const resultCount = sessions.reduce(
    (total, session) => total + Number(session.result_count || 0),
    0
  );
  el("foundation-summary").textContent = sessions.length
    ? `${sessions.length} run session${sessions.length === 1 ? "" : "s"} · ${resultCount} result${resultCount === 1 ? "" : "s"}`
    : "Idle; starts on demand for base alignment.";
  el("sessions").innerHTML = sessions.length ? sessions.map(session => `
    <div class="session">
      <strong>${escapeHtml(session.model_id || session.session_id || "session")}</strong>
      <span>${escapeHtml(session.state || "UNKNOWN")}</span>
      <small>${Number(session.result_count || 0)} results</small>
      <small>${session.latency_ms ? `${Number(session.latency_ms).toFixed(0)} ms` : ""}</small>
    </div>`).join("") : '<p class="muted">No sessions.</p>';

  const result = progress.result || value.latest_calibration;
  if (result) {
    el("alignment-id").textContent = result.alignment_id;
    const t = result.world_from_base.translation_m;
    el("result-summary").innerHTML = `
      <div><span>Mode</span><strong>${escapeHtml(result.mode)}</strong></div>
      <div><span>World frame</span><strong class="mono">${escapeHtml(result.world_frame)}</strong></div>
      <div><span>Base translation</span><strong>${t.map(v => Number(v).toFixed(3)).join(", ")} m</strong></div>
      <div><span>VIO epoch</span><strong class="mono">${escapeHtml(result.vio_session_epoch)}</strong></div>`;
  }
}

function updateRuntime(runtime) {
  const state = String(runtime.state || "PENDING");
  el("runtime-state").textContent = state;
  el("runtime-state").className = `runtime-state ${state.toLowerCase()}`;
  el("runtime-message").textContent = runtime.message || "Runtime status is unavailable.";
  el("request-providers").disabled = ["REQUESTING", "WAITING_FOR_VIO"].includes(state);
  const providers = runtime.providers || {};
  const cards = [
    coreCard("Manager", runtime.manager_reachable),
    coreCard("Fabric", runtime.fabric_reachable),
    providerCard("RGB-D camera", providers["camera.femto_bolt"]),
    providerCard("Local VIO", providers["localization.local_vio"]),
    providerCard("Robot arm pose", providers["robot_arm.rebot_dm"]),
    providerCard(
      "FoundationPose",
      providers["perception.object_pose.foundation_pose"],
      true
    )
  ];
  el("provider-grid").innerHTML = cards.join("");
}

function coreCard(label, reachable) {
  const state = reachable === true ? "reachable" : reachable === false ? "offline" : "checking";
  const tone = reachable === true ? "ready" : reachable === false ? "failed" : "waiting";
  return `
    <div class="provider-card ${tone}">
      <span>${escapeHtml(label)}</span>
      <strong>${escapeHtml(state)}</strong>
      <small>Midbrain core</small>
    </div>`;
}

function providerCard(label, provider, onDemand = false) {
  if (!provider) {
    return `
      <div class="provider-card waiting">
        <span>${escapeHtml(label)}</span>
        <strong>not reported</strong>
        <small>${onDemand ? "on demand" : "required"}</small>
      </div>`;
  }
  const processState = String(provider.process_state || "unknown");
  const residency = String(provider.residency || "no heartbeat");
  const ready = provider.ready === true && residency.toUpperCase() === "HOT";
  const expectedIdle = onDemand && ["stopped", "exited"].includes(processState.toLowerCase());
  const tone = ready ? "ready" : expectedIdle ? "waiting" :
    processState.toLowerCase() === "stopped" || provider.expired ? "failed" : "working";
  const headline = ready ? "ready / HOT" : expectedIdle ? "on demand" : `${processState} / ${residency}`;
  const detail = provider.last_error ||
    (provider.tracking_state ? `${provider.tracking_state} / ${provider.health || "starting"}` : null) ||
    provider.health ||
    (provider.expired ? "heartbeat expired" : onDemand ? "starts for base alignment" : "waiting for readiness");
  return `
    <div class="provider-card ${tone}">
      <span>${escapeHtml(label)}</span>
      <strong>${escapeHtml(headline)}</strong>
      <small title="${escapeHtml(detail)}">${escapeHtml(detail)}</small>
    </div>`;
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, character => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  })[character]);
}

async function pollStatus() {
  try {
    const response = await fetch("/api/status", {cache: "no-store"});
    updateStatus(await response.json());
  } catch (error) {
    el("provider-state").textContent = "GUI offline";
  }
}

function refreshImage() {
  const image = el("diagnostic-image");
  image.onload = () => {
    image.style.display = "block";
    el("image-placeholder").style.display = "none";
  };
  image.onerror = () => {
    image.style.display = "none";
    el("image-placeholder").style.display = "block";
  };
  image.src = `/api/image/${selectedImage}?v=${++imageRevision}`;
}

const canvas = el("viewport");
const gl = canvas.getContext("webgl", {antialias: true});
const vertexSource = `
attribute vec3 position;
attribute vec3 color;
uniform mat4 viewProjection;
uniform float pointSize;
varying vec3 vertexColor;
void main() {
  gl_Position = viewProjection * vec4(position, 1.0);
  gl_PointSize = pointSize;
  vertexColor = color;
}`;
const fragmentSource = `
precision mediump float;
varying vec3 vertexColor;
void main() {
  gl_FragColor = vec4(vertexColor, 1.0);
}`;

function shader(type, source) {
  const value = gl.createShader(type);
  gl.shaderSource(value, source);
  gl.compileShader(value);
  return value;
}
const program = gl.createProgram();
gl.attachShader(program, shader(gl.VERTEX_SHADER, vertexSource));
gl.attachShader(program, shader(gl.FRAGMENT_SHADER, fragmentSource));
gl.linkProgram(program);
const positionLocation = gl.getAttribLocation(program, "position");
const colorLocation = gl.getAttribLocation(program, "color");
const matrixLocation = gl.getUniformLocation(program, "viewProjection");
const sizeLocation = gl.getUniformLocation(program, "pointSize");
const pointBuffer = gl.createBuffer();
const lineBuffer = gl.createBuffer();
let cloudRecords = new Float32Array(0);
let lineRecords = new Float32Array(0);
let yaw = -0.65, pitch = -0.35, distance = 2.3, target = [0, 0, 0.8];
let dragging = false, lastX = 0, lastY = 0;

canvas.onpointerdown = event => {
  dragging = true; lastX = event.clientX; lastY = event.clientY;
  canvas.setPointerCapture(event.pointerId);
};
canvas.onpointerup = () => dragging = false;
canvas.onpointermove = event => {
  if (!dragging) return;
  yaw += (event.clientX - lastX) * .007;
  pitch = Math.max(-1.45, Math.min(1.45, pitch + (event.clientY - lastY) * .007));
  lastX = event.clientX; lastY = event.clientY;
};
canvas.onwheel = event => {
  event.preventDefault();
  distance = Math.max(.25, Math.min(12, distance * Math.exp(event.deltaY * .001)));
};

function multiply(a, b) {
  const out = new Float32Array(16);
  for (let row = 0; row < 4; row++) for (let column = 0; column < 4; column++) {
    let sum = 0;
    for (let i = 0; i < 4; i++) sum += a[i * 4 + row] * b[column * 4 + i];
    out[column * 4 + row] = sum;
  }
  return out;
}

function perspective(fov, aspect, near, far) {
  const f = 1 / Math.tan(fov / 2), nf = 1 / (near - far);
  return new Float32Array([f/aspect,0,0,0, 0,f,0,0, 0,0,(far+near)*nf,-1, 0,0,2*far*near*nf,0]);
}

function lookAt(eye, center, up) {
  const normalize = value => {
    const length = Math.hypot(...value) || 1;
    return value.map(item => item / length);
  };
  const cross = (a,b) => [a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0]];
  const z = normalize(eye.map((value,index) => value-center[index]));
  const x = normalize(cross(up,z));
  const y = cross(z,x);
  return new Float32Array([
    x[0],y[0],z[0],0, x[1],y[1],z[1],0, x[2],y[2],z[2],0,
    -x.reduce((sum,value,index)=>sum+value*eye[index],0),
    -y.reduce((sum,value,index)=>sum+value*eye[index],0),
    -z.reduce((sum,value,index)=>sum+value*eye[index],0),1
  ]);
}

function quaternionMatrix(q, t) {
  const [x,y,z,w] = q;
  return [
    1-2*(y*y+z*z), 2*(x*y+z*w), 2*(x*z-y*w), 0,
    2*(x*y-z*w), 1-2*(x*x+z*z), 2*(y*z+x*w), 0,
    2*(x*z+y*w), 2*(y*z-x*w), 1-2*(x*x+y*y), 0,
    t[0],t[1],t[2],1
  ];
}

function transformPoint(matrix, point) {
  return [
    matrix[0]*point[0]+matrix[4]*point[1]+matrix[8]*point[2]+matrix[12],
    matrix[1]*point[0]+matrix[5]*point[1]+matrix[9]*point[2]+matrix[13],
    matrix[2]*point[0]+matrix[6]*point[1]+matrix[10]*point[2]+matrix[14]
  ];
}

async function refreshGeometry() {
  try {
    const geometry = await (await fetch("/api/geometry", {cache:"no-store"})).json();
    const lines = [];
    const colors = [[1,.28,.34],[.18,.84,.45],[.24,.55,1]];
    (geometry.frames || []).forEach(frame => {
      const value = frame.transform;
      const matrix = quaternionMatrix(value.rotation_xyzw, value.translation_m);
      const origin = transformPoint(matrix, [0,0,0]);
      [[.18,0,0],[0,.18,0],[0,0,.18]].forEach((axis,index) => {
        const end = transformPoint(matrix, axis);
        lines.push(...origin,...colors[index],...end,...colors[index]);
      });
    });
    (geometry.points || []).forEach(point => {
      const p = point.position_m, s = .025, c = [1,.62,.26];
      lines.push(p[0]-s,p[1],p[2],...c,p[0]+s,p[1],p[2],...c);
      lines.push(p[0],p[1]-s,p[2],...c,p[0],p[1]+s,p[2],...c);
      lines.push(p[0],p[1],p[2]-s,...c,p[0],p[1],p[2]+s,...c);
    });
    lineRecords = new Float32Array(lines);
  } catch (_) {}
}

async function refreshCloud() {
  try {
    const buffer = await (await fetch("/api/point-cloud", {cache:"no-store"})).arrayBuffer();
    const count = new DataView(buffer).getUint32(0, true);
    cloudRecords = new Float32Array(buffer, 4, count * 6).slice();
  } catch (_) {}
}

function drawRecords(buffer, records, primitive, pointSize) {
  if (!records.length) return;
  gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
  gl.bufferData(gl.ARRAY_BUFFER, records, gl.DYNAMIC_DRAW);
  gl.enableVertexAttribArray(positionLocation);
  gl.vertexAttribPointer(positionLocation, 3, gl.FLOAT, false, 24, 0);
  gl.enableVertexAttribArray(colorLocation);
  gl.vertexAttribPointer(colorLocation, 3, gl.FLOAT, false, 24, 12);
  gl.uniform1f(sizeLocation, pointSize);
  gl.drawArrays(primitive, 0, records.length / 6);
}

function render() {
  const ratio = window.devicePixelRatio || 1;
  const width = Math.floor(canvas.clientWidth * ratio), height = Math.floor(canvas.clientHeight * ratio);
  if (canvas.width !== width || canvas.height !== height) { canvas.width = width; canvas.height = height; }
  gl.viewport(0,0,width,height);
  gl.clearColor(.018,.035,.047,1);
  gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
  gl.enable(gl.DEPTH_TEST);
  gl.useProgram(program);
  const eye = [
    target[0] + distance*Math.cos(pitch)*Math.sin(yaw),
    target[1] + distance*Math.sin(pitch),
    target[2] + distance*Math.cos(pitch)*Math.cos(yaw)
  ];
  gl.uniformMatrix4fv(matrixLocation, false, multiply(perspective(.78,width/height,.02,50), lookAt(eye,target,[0,1,0])));
  drawRecords(pointBuffer, cloudRecords, gl.POINTS, Math.max(1, ratio * 1.25));
  drawRecords(lineBuffer, lineRecords, gl.LINES, 1);
  requestAnimationFrame(render);
}

setInterval(pollStatus, 800);
setInterval(refreshCloud, 1000);
setInterval(refreshGeometry, 1000);
setInterval(refreshImage, 2500);
setInterval(refreshCandidateReviews, 2500);
pollStatus();
refreshCloud();
refreshGeometry();
refreshImage();
refreshCandidateReviews();
render();
