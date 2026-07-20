from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field

from .agent_driver import PrototypeAgentDriver
from .config import Settings
from .depth_capture import DepthCapture
from .fabric_client import FabricClient
from .gemini_pointing_skill import PointingIdentificationSkill
from .initialize_space_cognition_skill import InitializeSpaceCognitionSkill
from .manager_client import ManagerClient
from .rgb_capture import RgbCapture
from .world_point_cloud import WorldPointCloudAccumulator

settings = Settings()
fabric = FabricClient(settings.fabric_url)
manager = ManagerClient(settings.manager_url)
capture = RgbCapture(fabric, settings.screenshot_dir)
depth_capture = DepthCapture(fabric, settings.screenshot_dir)
pointing_skill = PointingIdentificationSkill(capture, settings.gemini_model)
driver = PrototypeAgentDriver(pointing_skill, settings.openai_model)
space_cognition_skill = InitializeSpaceCognitionSkill(
    manager,
    fabric,
    camera_provider_id=settings.head_camera_provider_id,
    vio_provider_id=settings.local_vio_provider_id,
    timeout_s=settings.space_cognition_timeout_s,
)
world_point_cloud = WorldPointCloudAccumulator(
    fabric,
    retention_s=settings.point_cloud_retention_s,
    sample_stride=settings.point_cloud_sample_stride,
    update_hz=settings.point_cloud_hz,
    max_points=settings.point_cloud_max_points,
)
auto_initialization_task: asyncio.Task[None] | None = None
auto_initialization_error: str | None = None
auto_initialization_state = "NOT_STARTED"
auto_initialization_result: dict[str, Any] | None = None


async def _auto_initialize() -> None:
    global auto_initialization_error, auto_initialization_result, auto_initialization_state
    try:
        auto_initialization_state = "SCHEDULED"
        await asyncio.sleep(1.0)
        auto_initialization_state = "RUNNING"
        auto_initialization_result = await space_cognition_skill.run(force_reset=False)
        auto_initialization_error = None
        auto_initialization_state = "SUCCEEDED"
    except asyncio.CancelledError:
        auto_initialization_state = "CANCELLED"
        raise
    except Exception as error:
        auto_initialization_error = str(error)
        auto_initialization_state = "FAILED"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global auto_initialization_task, auto_initialization_state
    await world_point_cloud.start()
    if settings.auto_initialize_space_cognition:
        auto_initialization_task = asyncio.create_task(
            _auto_initialize(),
            name="initialize-space-cognition",
        )
    else:
        auto_initialization_state = "DISABLED"
    yield
    if auto_initialization_task is not None:
        auto_initialization_task.cancel()
        try:
            await auto_initialization_task
        except asyncio.CancelledError:
            pass
        auto_initialization_task = None
    await world_point_cloud.stop()
    await fabric.close()
    await manager.close()


app = FastAPI(title="Physical Agent Test Scaffold", version="0.2.9", lifespan=lifespan)


class PromptRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=4000)


class InitializationRequest(BaseModel):
    force_reset: bool = False


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return PAGE


@app.get("/health")
async def ui_health() -> dict[str, str]:
    return {"status": "ok", "service": "physical-agent-ui"}


async def _safe(call) -> Any:
    try:
        return await call()
    except Exception as error:
        return {"status": "error", "error": str(error)}


@app.get("/api/status")
async def status() -> dict[str, Any]:
    (
        manager_health,
        fabric_health,
        providers,
        initialization,
        vio_status,
        body_pose,
        motion_inhibit,
        cloud_status,
    ) = await asyncio.gather(
        _safe(manager.health),
        _safe(fabric.health),
        _safe(manager.providers),
        _safe(lambda: fabric.latest_optional("skills.initialize_space_cognition.status")),
        _safe(lambda: fabric.latest_optional("localization.vio.status")),
        _safe(lambda: fabric.latest_optional("localization.body.pose")),
        _safe(manager.motion_inhibit_status),
        _safe(world_point_cloud.status),
    )
    return {
        "manager": manager_health,
        "fabric": fabric_health,
        "providers": providers,
        "space_cognition": initialization,
        "vio": vio_status,
        "body_pose": body_pose,
        "motion_inhibit": motion_inhibit,
        "world_point_cloud": cloud_status,
        "auto_initialize_enabled": settings.auto_initialize_space_cognition,
        "auto_initialize_state": auto_initialization_state,
        "auto_initialize_result": auto_initialization_result,
        "auto_initialize_error": auto_initialization_error,
        "openai_model": settings.openai_model,
        "gemini_model": settings.gemini_model,
    }


@app.post("/api/space-cognition/initialize")
async def initialize_space_cognition(request: InitializationRequest) -> dict[str, Any]:
    try:
        if request.force_reset:
            await world_point_cloud.begin_reinitialization()
        result = await space_cognition_skill.run(force_reset=request.force_reset)
        if request.force_reset:
            reset_result = result.get("result") or {}
            session_epoch = str(reset_result.get("session_epoch") or "")
            world_frame = str(reset_result.get("world_frame") or "")
            if not session_epoch or not world_frame:
                raise RuntimeError("forced reinitialization did not return a VIO session")
            await world_point_cloud.switch_session(
                session_epoch=session_epoch,
                world_frame=world_frame,
            )
            result["point_cloud_resumed"] = await world_point_cloud.wait_for_points(
                session_epoch=session_epoch,
                timeout_s=10.0,
            )
            if result["point_cloud_resumed"]:
                result["message"] = (
                    "Origin reinitialized. The previous map was cleared because it belonged to "
                    "the old coordinate epoch, and new point capture has resumed."
                )
            else:
                result["message"] = (
                    "Origin reinitialized and the new epoch is active, but point capture is still "
                    "waiting for visual TRACKING and a fresh RGB-D transform."
                )
        elif result.get("status") == "already_initialized":
            result["message"] = (
                "Space cognition was already initialized automatically at GUI startup. "
                "Use Force reinitialize only when a new local origin is required."
            )
        return result
    except httpx.HTTPStatusError as error:
        if request.force_reset:
            await world_point_cloud.resume_follow_latest()
        detail = error.response.text or str(error)
        raise HTTPException(status_code=502, detail=detail) from error
    except Exception as error:
        if request.force_reset:
            await world_point_cloud.resume_follow_latest()
        raise HTTPException(status_code=500, detail=str(error)) from error


@app.get("/api/world-point-cloud")
async def world_point_cloud_snapshot() -> Response:
    try:
        payload = await world_point_cloud.snapshot_binary()
        return Response(
            content=payload,
            media_type="application/octet-stream",
            headers={"Cache-Control": "no-store"},
        )
    except Exception as error:
        raise HTTPException(status_code=503, detail=str(error)) from error


@app.post("/api/world-point-cloud/clear")
async def clear_world_point_cloud() -> dict[str, str]:
    await world_point_cloud.clear()
    return {"status": "cleared"}


@app.post("/api/run")
async def run_prompt(request: PromptRequest) -> dict[str, Any]:
    try:
        answer = await driver.run(request.prompt)
        return {"answer": answer}
    except httpx.HTTPStatusError as error:
        detail = error.response.text or str(error)
        raise HTTPException(status_code=502, detail=detail) from error
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error)) from error


@app.get("/api/latest-image")
async def latest_image() -> Response:
    try:
        captured = await capture.capture_latest()
        return Response(
            content=captured.image_bytes,
            media_type=captured.mime_type,
            headers={"Cache-Control": "no-store"},
        )
    except Exception as error:
        raise HTTPException(status_code=503, detail=str(error)) from error


@app.get("/api/latest-depth")
async def latest_depth() -> Response:
    try:
        captured = await depth_capture.capture_latest()
        return Response(
            content=captured.image_bytes,
            media_type=captured.mime_type,
            headers={"Cache-Control": "no-store"},
        )
    except Exception as error:
        raise HTTPException(status_code=503, detail=str(error)) from error


def main() -> None:
    uvicorn.run(app, host=settings.ui_host, port=settings.ui_port, log_level="info")


PAGE = r"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Physical Agent Test Scaffold</title>
  <style>
    :root { font-family: Inter, Segoe UI, sans-serif; color-scheme: dark; }
    * { box-sizing: border-box; }
    body { margin: 0; background: #111827; color: #f3f4f6; }
    main { max-width: 1480px; margin: 0 auto; padding: 24px; }
    h1 { margin-bottom: 4px; }
    h2 { margin-top: 4px; }
    .sub { color: #9ca3af; margin-top: 0; }
    .grid { display: grid; grid-template-columns: minmax(360px, 0.8fr) minmax(520px, 1.2fr); gap: 18px; }
    .card { background: #1f2937; border: 1px solid #374151; border-radius: 12px; padding: 16px; margin-bottom: 18px; }
    textarea { width: 100%; min-height: 100px; resize: vertical; padding: 12px; border-radius: 8px; border: 1px solid #4b5563; background: #111827; color: #f9fafb; }
    button { margin-top: 10px; padding: 10px 14px; border: 0; border-radius: 8px; cursor: pointer; font-weight: 600; }
    button.primary { background: #2563eb; color: white; }
    button.secondary { background: #374151; color: white; margin-left: 8px; }
    button.danger { background: #991b1b; color: white; margin-left: 8px; }
    button:disabled { opacity: .55; cursor: progress; }
    pre { white-space: pre-wrap; word-break: break-word; background: #111827; padding: 12px; border-radius: 8px; min-height: 72px; max-height: 520px; overflow: auto; }
    img { width: 100%; min-height: 220px; object-fit: contain; background: #030712; border-radius: 8px; }
    .sensor-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
    .status { font-size: 12px; color: #d1d5db; }
    .viewer-wrap { position: relative; width: 100%; height: min(68vh, 720px); min-height: 430px; background: #030712; border-radius: 10px; overflow: hidden; }
    #cloud { display: block; width: 100%; height: 100%; cursor: grab; }
    #cloud:active { cursor: grabbing; }
    .viewer-overlay { position: absolute; left: 10px; top: 10px; background: rgba(3,7,18,.74); padding: 7px 9px; border-radius: 6px; font-size: 12px; pointer-events: none; }
    .gravity-overlay { position: absolute; right: 10px; top: 10px; background: rgba(3,7,18,.78); padding: 7px 9px; border-radius: 6px; font-size: 12px; pointer-events: none; text-align: right; white-space: pre-line; }
    .init-summary { margin-top: 10px; padding: 9px 10px; background: #111827; border-radius: 8px; color: #d1d5db; font-size: 13px; }
    .state-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin: 10px 0; }
    .state-card { border: 1px solid #374151; border-radius: 8px; background: #111827; padding: 9px 10px; min-height: 58px; }
    .state-label { color: #9ca3af; font-size: 11px; text-transform: uppercase; letter-spacing: .06em; }
    .state-value { margin-top: 3px; font-size: 14px; font-weight: 700; color: #e5e7eb; display: flex; align-items: center; gap: 7px; }
    .state-lamp { width: 10px; height: 10px; border-radius: 50%; background: #6b7280; box-shadow: 0 0 0 2px rgba(107,114,128,.18); flex: 0 0 auto; }
    .state-lamp.ok { background: #22c55e; box-shadow: 0 0 8px rgba(34,197,94,.75); }
    .state-lamp.warn { background: #f59e0b; box-shadow: 0 0 8px rgba(245,158,11,.72); }
    .state-lamp.bad { background: #ef4444; box-shadow: 0 0 8px rgba(239,68,68,.75); }
    .state-lamp.busy { background: #60a5fa; box-shadow: 0 0 8px rgba(96,165,250,.75); }
    .state-detail { margin-top: 2px; color: #9ca3af; font-size: 11px; }
    .state-card.ok { border-color: #166534; }
    .state-card.ok .state-value { color: #86efac; }
    .state-card.warn { border-color: #92400e; }
    .state-card.warn .state-value { color: #fde68a; }
    .state-card.bad { border-color: #991b1b; }
    .state-card.bad .state-value { color: #fca5a5; }
    .state-card.busy { border-color: #1d4ed8; }
    .state-card.busy .state-value { color: #93c5fd; }
    .action-status { margin-top: 8px; color: #cbd5e1; font-size: 12px; min-height: 18px; }
    .controls { color: #9ca3af; font-size: 12px; margin: 8px 0 0; }
    @media (max-width: 980px) { .grid { grid-template-columns: 1fr; } .viewer-wrap { height: 55vh; } }
    @media (max-width: 640px) { .sensor-grid { grid-template-columns: 1fr; } }
  </style>
</head>
<body>
<main>
  <h1>Physical Agent Test Scaffold</h1>
  <p class="sub">Startup space cognition, local VIO body pose, and fading world-frame RGB-D map</p>
  <div class="grid">
    <div>
      <section class="card">
        <h2>Space cognition</h2>
        <div class="state-grid">
          <div class="state-card" id="visualState"><div class="state-label">Visual correction</div><div class="state-value"><span class="state-lamp"></span><span class="state-text">UNKNOWN</span></div><div class="state-detail">RGB-D/IR updates correct inertial drift</div></div>
          <div class="state-card" id="poseState"><div class="state-label">Pose propagation</div><div class="state-value"><span class="state-lamp"></span><span class="state-text">UNKNOWN</span></div><div class="state-detail">IMU is the primary motion clock</div></div>
          <div class="state-card" id="rotationState"><div class="state-label">Rotation estimator</div><div class="state-value"><span class="state-lamp"></span><span class="state-text">UNKNOWN</span></div><div class="state-detail">Gyro propagation with visual error-state correction</div></div>
          <div class="state-card" id="gravityState"><div class="state-label">Gravity adjustment</div><div class="state-value"><span class="state-lamp"></span><span class="state-text">OFF</span></div><div class="state-detail">Quiet IMU enables slow roll/pitch leveling</div></div>
          <div class="state-card" id="featureState"><div class="state-label">Feature extraction</div><div class="state-value"><span class="state-lamp"></span><span class="state-text">UNKNOWN</span></div><div class="state-detail">Raw baseline always remains available</div></div>
          <div class="state-card" id="mapState"><div class="state-label">Map capture</div><div class="state-value"><span class="state-lamp"></span><span class="state-text">UNKNOWN</span></div><div class="state-detail">Waiting for RGB-D</div></div>
          <div class="state-card" id="initState"><div class="state-label">Initialization / reset</div><div class="state-value"><span class="state-lamp"></span><span class="state-text">UNKNOWN</span></div><div class="state-detail">No result yet</div></div>
        </div>
        <button class="primary" id="initialize">Initialize / verify</button>
        <button class="danger" id="reset">Force reinitialize origin</button>
        <button class="secondary" id="clearCloud">Clear point cloud</button>
        <div class="action-status" id="actionStatus">No manual action running.</div>
        <div class="init-summary" id="initSummary">Automatic startup initialization has not reported yet.</div>
        <pre id="spaceStatus" class="status">Loading…</pre>
      </section>
      <section class="card">
        <h2>Prompt</h2>
        <textarea id="prompt">Take a screenshot and identify the object I am pointing at. Use only the RGB image.</textarea>
        <div>
          <button class="primary" id="run">Run prompt</button>
          <button class="secondary" id="refresh">Refresh frames</button>
        </div>
        <h2>Answer</h2>
        <pre id="answer">Ready.</pre>
      </section>
      <section class="card">
        <h2>Latest sensor frames</h2>
        <div class="sensor-grid">
          <div><p>RGB</p><img id="image" alt="Latest RGB frame"></div>
          <div><p>Depth</p><img id="depth" alt="Latest depth frame"></div>
        </div>
      </section>
      <section class="card">
        <h2>Platform status</h2>
        <pre id="status" class="status">Loading…</pre>
      </section>
    </div>
    <section class="card">
      <h2>World RGB point cloud</h2>
      <button class="secondary" id="resetView">Reset isometric view</button>
      <div class="viewer-wrap">
        <canvas id="cloud"></canvas>
        <div class="viewer-overlay" id="cloudStats">Waiting for pose and RGB-D…</div>
        <div class="gravity-overlay" id="gravityStatus">↓ World gravity · -Y</div>
      </div>
      <p class="controls">Orthographic isometric view. Drag to orbit, mouse wheel to change parallel scale. Orange is world-down; cyan is the current camera pose. Points use world coordinates and fade linearly over 10 seconds.</p>
    </section>
  </div>
</main>
<script>
const runButton = document.getElementById('run');
const refreshButton = document.getElementById('refresh');
const initializeButton = document.getElementById('initialize');
const resetButton = document.getElementById('reset');
const clearCloudButton = document.getElementById('clearCloud');
const resetViewButton = document.getElementById('resetView');
const promptBox = document.getElementById('prompt');
const answer = document.getElementById('answer');
const image = document.getElementById('image');
const depth = document.getElementById('depth');
const statusBox = document.getElementById('status');
const spaceStatus = document.getElementById('spaceStatus');
const initSummary = document.getElementById('initSummary');
const cloudStats = document.getElementById('cloudStats');
const gravityStatus = document.getElementById('gravityStatus');
const visualState = document.getElementById('visualState');
const poseState = document.getElementById('poseState');
const rotationState = document.getElementById('rotationState');
const gravityState = document.getElementById('gravityState');
const featureState = document.getElementById('featureState');
const mapState = document.getElementById('mapState');
const initState = document.getElementById('initState');
const actionStatus = document.getElementById('actionStatus');
let cloudCaptureState = 'unknown';
let latestCameraPose = null;

function setStateCard(element, value, detail, kind) {
  element.className = 'state-card' + (kind ? ' ' + kind : '');
  const textElement = element.querySelector('.state-text');
  if (textElement) textElement.textContent = value;
  const lamp = element.querySelector('.state-lamp');
  if (lamp) lamp.className = 'state-lamp' + (kind ? ' ' + kind : '');
  element.querySelector('.state-detail').textContent = detail;
}

function stateKind(value) {
  const text = String(value || '').toUpperCase();
  if (['TRACKING', 'CAPTURING', 'SUCCEEDED', 'ALIGNED'].includes(text)) return 'ok';
  if (text.includes('RUNNING') || text.includes('SCHEDULED') || text.includes('INITIALIZ') || text.includes('RECOVERY') || text.includes('TRIM')) return 'busy';
  if (text.includes('FAILED') || text.includes('ERROR')) return 'bad';
  if (text.includes('DEGRADED') || text.includes('PAUSED') || text.includes('WAITING') || text.includes('DRIFT') || text.includes('MOVING')) return 'warn';
  return '';
}

function refreshImages() {
  const stamp = Date.now();
  image.src = '/api/latest-image?t=' + stamp;
  depth.src = '/api/latest-depth?t=' + stamp;
}

async function refreshStatus() {
  try {
    const response = await fetch('/api/status', {cache: 'no-store'});
    const data = await response.json();
    const initializationData = (data.space_cognition && data.space_cognition.data) || {};
    const vioData = (data.vio && data.vio.data) || {};
    const poseData = (data.body_pose && data.body_pose.data) || {};
    const cloudData = data.world_point_cloud || {};
    cloudCaptureState = cloudData.capture_state || 'unknown';
    latestCameraPose = poseData.world_from_camera || null;
    updateCameraMarker(latestCameraPose);
    const epoch = initializationData.session_epoch || vioData.session_epoch || 'none';
    const currentSkillState = initializationData.state || 'not published';
    const displayedAutoState = (
      data.auto_initialize_state === 'FAILED' &&
      (currentSkillState === 'RUNNING' || currentSkillState === 'SUCCEEDED')
    ) ? 'SUPERSEDED' : data.auto_initialize_state;
    initSummary.textContent = [
      'Startup auto-init: ' + displayedAutoState,
      'skill: ' + currentSkillState,
      'VIO: ' + (vioData.tracking_state || 'unknown'),
      'epoch: ' + String(epoch).slice(0, 12)
    ].join(' · ');
    const tilt = vioData.gravity_tilt_error_rad;
    const tiltDegrees = typeof tilt === 'number' ? (tilt * 180 / Math.PI).toFixed(2) : 'n/a';
    const visualTracking = vioData.tracking_state || 'UNKNOWN';
    const poseMode = vioData.pose_update_mode || 'UNKNOWN';
    const visualAccepted = Boolean(vioData.visual_update_accepted);
    const visualSensor = vioData.visual_sensor || 'NONE';
    const visualStale = typeof vioData.visual_stale_s === 'number' ? vioData.visual_stale_s : null;
    const propagationSteps = Number(vioData.imu_propagation_steps || 0);
    const accelHistoryCount = Number(vioData.imu_accelerometer_history_count || 0);
    const gyroHistoryCount = Number(vioData.imu_gyroscope_history_count || 0);
    const imuTimestampSkewUs = vioData.imu_timestamp_skew_us;
    const initializationBlocker = vioData.initialization_blocker || 'none';
    const initAccelWindowCount = Number(vioData.initialization_accelerometer_window_count || 0);
    const initGyroWindowCount = Number(vioData.initialization_gyroscope_window_count || 0);
    const initAccelRateHz = vioData.initialization_accelerometer_rate_hz;
    const initGyroRateHz = vioData.initialization_gyroscope_rate_hz;
    const rotationSource = vioData.rotation_source || 'UNKNOWN';
    const rotationDisagreement = typeof vioData.rotation_disagreement_rad === 'number'
      ? vioData.rotation_disagreement_rad
      : null;
    const gyroRotationAngle = typeof vioData.gyro_rotation_angle_rad === 'number'
      ? vioData.gyro_rotation_angle_rad
      : null;
    const gyroRotationSamples = Number(vioData.gyro_rotation_sample_count || 0);
    const gravityMode = vioData.gravity_correction_mode || 'UNKNOWN';
    const gravityAdjustment = vioData.gravity_adjustment_state ||
      (vioData.gravity_correction_applied ? 'ACTIVE' : 'OFF');
    const featureMode = vioData.feature_preprocess_mode || 'UNKNOWN';
    const initValue = initializationData.state || data.auto_initialize_state || 'UNKNOWN';
    const inliers = Number(vioData.visual_inlier_count || 0);
    const matches = Number(vioData.visual_match_count || 0);
    const rawKeypoints = Number(vioData.feature_raw_keypoint_count || 0);
    const normalizedKeypoints = Number(vioData.feature_normalized_keypoint_count || 0);
    const frameLuma = Number(vioData.frame_luma_median || 0);
    const irKeypoints = Number(vioData.ir_keypoint_count || 0);
    const irInliers = Number(vioData.ir_inlier_count || 0);
    const stationary = Number(vioData.gravity_stationary_duration_s || 0);
    const gyroGate = Number(vioData.gravity_gyro_effective_limit_radps || vioData.gravity_gyro_configured_limit_radps || 0.012);
    const gyroNoise = Number(vioData.gravity_gyro_noise_floor_radps || 0);
    const gyroRms = Number(vioData.gravity_gyro_rms_radps || 0);
    const gyroP95 = Number(vioData.gravity_gyro_p95_radps || 0);
    const secondsSinceChunk = typeof cloudData.seconds_since_last_chunk === 'number'
      ? cloudData.seconds_since_last_chunk.toFixed(1) + ' s since last chunk'
      : 'no map chunk yet';
    const visualValue = visualAccepted ? visualSensor + ' UPDATE' : (visualTracking === 'TRACKING' ? 'RECENT VISUAL' : 'VISUAL STALE');
    const visualDetail = inliers + '/' + matches + ' inliers/matches' +
      (visualStale === null ? '' : ' · ' + visualStale.toFixed(2) + ' s stale');
    setStateCard(
      visualState,
      visualValue,
      visualDetail,
      visualAccepted ? 'ok' : stateKind(visualTracking)
    );
    setStateCard(
      poseState,
      poseMode,
      propagationSteps + ' IMU integration steps · accel/gyro history ' +
        accelHistoryCount + '/' + gyroHistoryCount + ' · init window ' +
        initAccelWindowCount + '/' + initGyroWindowCount + ' @ ' +
        (typeof initAccelRateHz === 'number' ? initAccelRateHz.toFixed(1) : 'n/a') + '/' +
        (typeof initGyroRateHz === 'number' ? initGyroRateHz.toFixed(1) : 'n/a') + ' Hz · skew ' +
        (typeof imuTimestampSkewUs === 'number' ? imuTimestampSkewUs + ' µs' : 'n/a'),
      poseMode.includes('IMU_') ? (visualTracking === 'TRACKING' ? 'ok' : 'warn') : stateKind(poseMode)
    );
    const disagreementDegrees = rotationDisagreement === null
      ? 'n/a'
      : (rotationDisagreement * 180 / Math.PI).toFixed(1) + '°';
    const gyroAngleDegrees = gyroRotationAngle === null
      ? 'n/a'
      : (gyroRotationAngle * 180 / Math.PI).toFixed(1) + '°';
    const rotationKind = rotationSource.includes('IMU')
      ? (visualTracking === 'TRACKING' ? 'ok' : 'warn')
      : stateKind(visualTracking);
    setStateCard(
      rotationState,
      rotationSource,
      'visual innovation ' + disagreementDegrees +
        ' · high-rate gyro propagation' +
        (gyroRotationSamples ? ' · ' + gyroRotationSamples + ' samples' : ''),
      rotationKind
    );
    const gravityKind = gravityAdjustment === 'ACTIVE'
      ? 'busy'
      : (gravityAdjustment === 'READY' ? 'ok' : '');
    setStateCard(
      gravityState,
      gravityAdjustment,
      gravityMode + ' · tilt ' + tiltDegrees + '° · still ' + stationary.toFixed(1) +
        ' s · gyro p95 ' + gyroP95.toFixed(4) + '/' + gyroGate.toFixed(4) + ' rad/s',
      gravityKind
    );
    setStateCard(
      featureState,
      featureMode,
      'RGB raw ' + rawKeypoints + ' · normalized ' + normalizedKeypoints +
        ' · IR ' + irInliers + '/' + irKeypoints +
        ' · RGB luma ' + frameLuma.toFixed(0),
      featureMode.includes('CIRCULAR_LCN') ? 'busy' : (featureMode === 'RAW_BASELINE' ? 'ok' : '')
    );
    setStateCard(
      mapState,
      cloudCaptureState,
      Number(cloudData.point_count || 0).toLocaleString() + ' points · ' + secondsSinceChunk +
        ' · ' + (cloudData.capture_reason || 'no capture reason'),
      stateKind(cloudCaptureState)
    );
    setStateCard(
      initState,
      initValue,
      (initializationData.current_subskill || 'idle') + ' · epoch ' + String(epoch).slice(0, 8) +
        ' · ' + initializationBlocker,
      stateKind(initValue)
    );
    gravityStatus.textContent = '↓ World gravity · -Y\n' +
      'adjustment: ' + gravityAdjustment + ' · ' + gravityMode + '\n' +
      'tilt: ' + tiltDegrees + '° · gyro p95/gate ' + gyroP95.toFixed(4) + '/' + gyroGate.toFixed(4) + '\n' +
      'gyro rms/noise ' + gyroRms.toFixed(4) + '/' + gyroNoise.toFixed(4) + ' rad/s\n' +
      'visual: ' + visualValue + ' · pose: ' + poseMode + '\n' +
      'rotation: ' + rotationSource + ' · innovation ' + disagreementDegrees + '\n' +
      'features: ' + featureMode + ' · IR ' + irInliers + '/' + irKeypoints + ' · map: ' + cloudCaptureState;
    statusBox.textContent = JSON.stringify({
      manager: data.manager,
      fabric: data.fabric,
      providers: data.providers,
      motion_inhibit: data.motion_inhibit,
      auto_initialize_state: data.auto_initialize_state,
      auto_initialize_error: data.auto_initialize_error
    }, null, 2);
    spaceStatus.textContent = JSON.stringify({
      initialization: data.space_cognition,
      vio: data.vio,
      body_pose: data.body_pose,
      point_cloud: data.world_point_cloud
    }, null, 2);
  } catch (error) {
    statusBox.textContent = String(error);
  }
}

async function requestInitialization(forceReset) {
  initializeButton.disabled = true;
  resetButton.disabled = true;
  spaceStatus.textContent = forceReset ? 'Reinitializing local origin and restarting the map…' : 'Checking startup initialization…';
  actionStatus.textContent = forceReset
    ? 'Reset running: motion inhibited, new VIO epoch requested, map waiting for TRACKING.'
    : 'Initialization verification running.';
  try {
    const response = await fetch('/api/space-cognition/initialize', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({force_reset: forceReset})
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || JSON.stringify(data));
    spaceStatus.textContent = (data.message ? data.message + '\n\n' : '') + JSON.stringify(data, null, 2);
    actionStatus.textContent = data.message || (forceReset ? 'Reset request completed.' : 'Initialization verification completed.');
    await refreshStatus();
  } catch (error) {
    spaceStatus.textContent = 'Error: ' + error;
    actionStatus.textContent = 'Manual action failed: ' + error;
  } finally {
    initializeButton.disabled = false;
    resetButton.disabled = false;
  }
}

initializeButton.addEventListener('click', () => requestInitialization(false));
resetButton.addEventListener('click', () => requestInitialization(true));
clearCloudButton.addEventListener('click', async () => {
  await fetch('/api/world-point-cloud/clear', {method: 'POST'});
});

runButton.addEventListener('click', async () => {
  runButton.disabled = true;
  answer.textContent = 'Running…';
  try {
    const response = await fetch('/api/run', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({prompt: promptBox.value})
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || JSON.stringify(data));
    answer.textContent = data.answer;
    refreshImages();
    refreshStatus();
  } catch (error) {
    answer.textContent = 'Error: ' + error;
  } finally {
    runButton.disabled = false;
  }
});
refreshButton.addEventListener('click', refreshImages);

const canvas = document.getElementById('cloud');
const gl = canvas.getContext('webgl', {alpha: false, antialias: true});
let pointCount = 0;
let cloudBuffer = null;
const ISOMETRIC_YAW = -Math.PI / 4;
const ISOMETRIC_PITCH = Math.atan(1 / Math.sqrt(2));
let orbitYaw = ISOMETRIC_YAW;
let orbitPitch = ISOMETRIC_PITCH;
let viewDistance = 12.0;
let orthoScale = 3.5;
let target = [0, 0, 1.5];
let dragging = false;
let dragX = 0;
let dragY = 0;

function shader(type, source) {
  const result = gl.createShader(type);
  gl.shaderSource(result, source);
  gl.compileShader(result);
  if (!gl.getShaderParameter(result, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(result));
  return result;
}

const vertexSource = `
attribute vec3 aPosition;
attribute vec3 aColor;
attribute float aAge;
uniform mat4 uProjection;
uniform mat4 uView;
uniform float uRetention;
uniform float uPointSize;
varying vec4 vColor;
void main() {
  gl_Position = uProjection * uView * vec4(aPosition, 1.0);
  gl_PointSize = uPointSize;
  float alpha = clamp(1.0 - aAge / uRetention, 0.0, 1.0);
  vColor = vec4(aColor, alpha);
}`;
const fragmentSource = `
precision mediump float;
varying vec4 vColor;
void main() {
  vec2 delta = gl_PointCoord - vec2(0.5);
  if (dot(delta, delta) > 0.25) discard;
  gl_FragColor = vColor;
}`;
const lineVertexSource = `
attribute vec3 aPosition;
uniform mat4 uProjection;
uniform mat4 uView;
void main() {
  gl_Position = uProjection * uView * vec4(aPosition, 1.0);
}`;
const lineFragmentSource = `
precision mediump float;
uniform vec4 uColor;
void main() { gl_FragColor = uColor; }`;
const gravityArrowVertices = new Float32Array([
  0.0, 0.0, 0.0,   0.0, -1.2, 0.0,
  0.0, -1.2, 0.0, -0.15, -0.95, 0.0,
  0.0, -1.2, 0.0,  0.15, -0.95, 0.0,
  0.0, -1.2, 0.0,  0.0, -0.95, -0.15,
  0.0, -1.2, 0.0,  0.0, -0.95, 0.15
]);
const cameraMarkerLocal = [
  [0, 0, 0], [-0.18, -0.12, 0.35],
  [0, 0, 0], [ 0.18, -0.12, 0.35],
  [0, 0, 0], [ 0.18,  0.12, 0.35],
  [0, 0, 0], [-0.18,  0.12, 0.35],
  [-0.18, -0.12, 0.35], [ 0.18, -0.12, 0.35],
  [ 0.18, -0.12, 0.35], [ 0.18,  0.12, 0.35],
  [ 0.18,  0.12, 0.35], [-0.18,  0.12, 0.35],
  [-0.18,  0.12, 0.35], [-0.18, -0.12, 0.35],
  [0, 0, 0], [0, 0, 0.55]
];

let program = null;
let lineProgram = null;
let gravityArrowBuffer = null;
let cameraMarkerBuffer = null;
let cameraMarkerVertexCount = 0;
if (gl) {
  program = gl.createProgram();
  gl.attachShader(program, shader(gl.VERTEX_SHADER, vertexSource));
  gl.attachShader(program, shader(gl.FRAGMENT_SHADER, fragmentSource));
  gl.linkProgram(program);
  if (!gl.getProgramParameter(program, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(program));
  cloudBuffer = gl.createBuffer();
  lineProgram = gl.createProgram();
  gl.attachShader(lineProgram, shader(gl.VERTEX_SHADER, lineVertexSource));
  gl.attachShader(lineProgram, shader(gl.FRAGMENT_SHADER, lineFragmentSource));
  gl.linkProgram(lineProgram);
  if (!gl.getProgramParameter(lineProgram, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(lineProgram));
  gravityArrowBuffer = gl.createBuffer();
  gl.bindBuffer(gl.ARRAY_BUFFER, gravityArrowBuffer);
  gl.bufferData(gl.ARRAY_BUFFER, gravityArrowVertices, gl.STATIC_DRAW);
  cameraMarkerBuffer = gl.createBuffer();
  gl.enable(gl.DEPTH_TEST);
  gl.enable(gl.BLEND);
  gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);
}

function quaternionMatrix(q) {
  if (!Array.isArray(q) || q.length !== 4) return null;
  let [x, y, z, w] = q.map(Number);
  const norm = Math.hypot(x, y, z, w) || 1;
  x /= norm; y /= norm; z /= norm; w /= norm;
  return [
    [1 - 2*(y*y + z*z), 2*(x*y - z*w), 2*(x*z + y*w)],
    [2*(x*y + z*w), 1 - 2*(x*x + z*z), 2*(y*z - x*w)],
    [2*(x*z - y*w), 2*(y*z + x*w), 1 - 2*(x*x + y*y)]
  ];
}

function updateCameraMarker(pose) {
  if (!gl || !cameraMarkerBuffer || !pose) {
    cameraMarkerVertexCount = 0;
    return;
  }
  const rotation = quaternionMatrix(pose.rotation_xyzw);
  const translation = Array.isArray(pose.translation_m) ? pose.translation_m.map(Number) : null;
  if (!rotation || !translation || translation.length !== 3) {
    cameraMarkerVertexCount = 0;
    return;
  }
  const values = [];
  for (const point of cameraMarkerLocal) {
    values.push(
      translation[0] + rotation[0][0]*point[0] + rotation[0][1]*point[1] + rotation[0][2]*point[2],
      translation[1] + rotation[1][0]*point[0] + rotation[1][1]*point[1] + rotation[1][2]*point[2],
      translation[2] + rotation[2][0]*point[0] + rotation[2][1]*point[1] + rotation[2][2]*point[2]
    );
  }
  cameraMarkerVertexCount = values.length / 3;
  gl.bindBuffer(gl.ARRAY_BUFFER, cameraMarkerBuffer);
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(values), gl.DYNAMIC_DRAW);
}

function orthographic(left, right, bottom, top, near, far) {
  const lr = 1 / (left - right);
  const bt = 1 / (bottom - top);
  const nf = 1 / (near - far);
  return new Float32Array([
    -2 * lr, 0, 0, 0,
    0, -2 * bt, 0, 0,
    0, 0, 2 * nf, 0,
    (left + right) * lr, (top + bottom) * bt, (far + near) * nf, 1
  ]);
}

function normalize(v) {
  const length = Math.hypot(v[0], v[1], v[2]) || 1;
  return [v[0] / length, v[1] / length, v[2] / length];
}
function cross(a, b) {
  return [a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0]];
}
function dot(a, b) { return a[0]*b[0] + a[1]*b[1] + a[2]*b[2]; }
function lookAt(eye, center, up) {
  const z = normalize([eye[0]-center[0], eye[1]-center[1], eye[2]-center[2]]);
  const x = normalize(cross(up, z));
  const y = cross(z, x);
  return new Float32Array([
    x[0], y[0], z[0], 0,
    x[1], y[1], z[1], 0,
    x[2], y[2], z[2], 0,
    -dot(x, eye), -dot(y, eye), -dot(z, eye), 1
  ]);
}

function resizeCanvas() {
  if (!gl) return;
  const ratio = window.devicePixelRatio || 1;
  const width = Math.max(1, Math.floor(canvas.clientWidth * ratio));
  const height = Math.max(1, Math.floor(canvas.clientHeight * ratio));
  if (canvas.width !== width || canvas.height !== height) {
    canvas.width = width;
    canvas.height = height;
  }
  gl.viewport(0, 0, width, height);
}

function renderCloud() {
  if (!gl || !program || !lineProgram) return;
  resizeCanvas();
  gl.clearColor(0.012, 0.027, 0.071, 1.0);
  gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
  const cp = Math.cos(orbitPitch);
  const eye = [
    target[0] + viewDistance * cp * Math.sin(orbitYaw),
    target[1] + viewDistance * Math.sin(orbitPitch),
    target[2] + viewDistance * cp * Math.cos(orbitYaw)
  ];
  const aspect = canvas.width / Math.max(1, canvas.height);
  const halfHeight = orthoScale;
  const halfWidth = halfHeight * aspect;
  const projection = orthographic(-halfWidth, halfWidth, -halfHeight, halfHeight, 0.03, 100.0);
  const view = lookAt(eye, target, [0, 1, 0]);
  if (pointCount > 0) {
    gl.useProgram(program);
    gl.uniformMatrix4fv(gl.getUniformLocation(program, 'uProjection'), false, projection);
    gl.uniformMatrix4fv(gl.getUniformLocation(program, 'uView'), false, view);
    gl.uniform1f(gl.getUniformLocation(program, 'uRetention'), 10.0);
    gl.uniform1f(
      gl.getUniformLocation(program, 'uPointSize'),
      Math.min(5.0, 2.2 * (window.devicePixelRatio || 1))
    );
    gl.bindBuffer(gl.ARRAY_BUFFER, cloudBuffer);
    const stride = 7 * 4;
    const position = gl.getAttribLocation(program, 'aPosition');
    const color = gl.getAttribLocation(program, 'aColor');
    const age = gl.getAttribLocation(program, 'aAge');
    gl.enableVertexAttribArray(position);
    gl.enableVertexAttribArray(color);
    gl.enableVertexAttribArray(age);
    gl.vertexAttribPointer(position, 3, gl.FLOAT, false, stride, 0);
    gl.vertexAttribPointer(color, 3, gl.FLOAT, false, stride, 3 * 4);
    gl.vertexAttribPointer(age, 1, gl.FLOAT, false, stride, 6 * 4);
    gl.drawArrays(gl.POINTS, 0, pointCount);
  }
  gl.disable(gl.DEPTH_TEST);
  gl.useProgram(lineProgram);
  gl.uniformMatrix4fv(gl.getUniformLocation(lineProgram, 'uProjection'), false, projection);
  gl.uniformMatrix4fv(gl.getUniformLocation(lineProgram, 'uView'), false, view);
  gl.uniform4f(gl.getUniformLocation(lineProgram, 'uColor'), 1.0, 0.38, 0.18, 1.0);
  gl.bindBuffer(gl.ARRAY_BUFFER, gravityArrowBuffer);
  const linePosition = gl.getAttribLocation(lineProgram, 'aPosition');
  gl.enableVertexAttribArray(linePosition);
  gl.vertexAttribPointer(linePosition, 3, gl.FLOAT, false, 0, 0);
  gl.drawArrays(gl.LINES, 0, gravityArrowVertices.length / 3);
  if (cameraMarkerVertexCount > 0) {
    gl.uniform4f(gl.getUniformLocation(lineProgram, 'uColor'), 0.20, 0.85, 1.0, 1.0);
    gl.bindBuffer(gl.ARRAY_BUFFER, cameraMarkerBuffer);
    gl.vertexAttribPointer(linePosition, 3, gl.FLOAT, false, 0, 0);
    gl.drawArrays(gl.LINES, 0, cameraMarkerVertexCount);
  }
  gl.enable(gl.DEPTH_TEST);
  requestAnimationFrame(renderCloud);
}

async function refreshCloud() {
  if (!gl) {
    cloudStats.textContent = 'WebGL is unavailable in this browser.';
    return;
  }
  try {
    const response = await fetch('/api/world-point-cloud?t=' + Date.now(), {cache: 'no-store'});
    if (!response.ok) throw new Error(await response.text());
    const buffer = await response.arrayBuffer();
    if (buffer.byteLength < 4) return;
    const view = new DataView(buffer);
    pointCount = view.getUint32(0, true);
    const expectedBytes = 4 + pointCount * 7 * 4;
    if (buffer.byteLength < expectedBytes) throw new Error('truncated point-cloud payload');
    gl.bindBuffer(gl.ARRAY_BUFFER, cloudBuffer);
    gl.bufferData(gl.ARRAY_BUFFER, new Uint8Array(buffer, 4, pointCount * 28), gl.DYNAMIC_DRAW);
    cloudStats.textContent = pointCount.toLocaleString() + ' points · ' + cloudCaptureState + ' · 10 s retention';
  } catch (error) {
    cloudStats.textContent = 'Point cloud: ' + error + ' · ' + cloudCaptureState;
  }
}

canvas.addEventListener('pointerdown', event => {
  dragging = true; dragX = event.clientX; dragY = event.clientY; canvas.setPointerCapture(event.pointerId);
});
canvas.addEventListener('pointermove', event => {
  if (!dragging) return;
  orbitYaw -= (event.clientX - dragX) * 0.006;
  orbitPitch = Math.max(-1.45, Math.min(1.45, orbitPitch + (event.clientY - dragY) * 0.006));
  dragX = event.clientX; dragY = event.clientY;
});
canvas.addEventListener('pointerup', event => { dragging = false; canvas.releasePointerCapture(event.pointerId); });
canvas.addEventListener('wheel', event => {
  event.preventDefault();
  orthoScale = Math.max(0.25, Math.min(30, orthoScale * Math.exp(event.deltaY * 0.001)));
}, {passive: false});
resetViewButton.addEventListener('click', () => {
  orbitYaw = ISOMETRIC_YAW;
  orbitPitch = ISOMETRIC_PITCH;
  orthoScale = 3.5;
  target = [0, 0, 1.5];
});

refreshImages();
refreshStatus();
renderCloud();
refreshCloud();
setInterval(refreshStatus, 2500);
setInterval(refreshCloud, 300);
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
