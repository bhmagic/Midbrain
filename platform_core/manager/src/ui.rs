use super::{
    collect_provider_views, ensure_provider_hot, reject_if_shutdown_fenced, AppState, ProviderView,
};
use anyhow::{Context, Result};
use axum::{
    extract::{Path, State},
    http::{header, StatusCode},
    response::{Html, IntoResponse},
    Json,
};
use chrono::Utc;
use serde::Deserialize;
use serde_json::{json, Map, Value};
use std::{
    collections::{HashMap, HashSet},
    fs,
    path::{Path as FsPath, PathBuf},
    process::Stdio,
    time::Duration,
};
use tokio::process::Command;
use tracing::{info, warn};

const MAINFRAME_HTML: &str = include_str!("../web/index.html");
const COMPONENT_HTML: &str = include_str!("../web/component.html");
const DEVELOPER_HTML: &str = include_str!("../web/developer-confirm.html");
const SHUTDOWN_HTML: &str = include_str!("../web/shutdown.html");
const MANAGER_CSS: &str = include_str!("../web/manager.css");
const MAINFRAME_JS: &str = include_str!("../web/mainframe.js");
const COMPONENT_JS: &str = include_str!("../web/component.js");
const DEVELOPER_CONFIRM_JS: &str = include_str!("../web/developer-confirm.js");
const SHUTDOWN_JS: &str = include_str!("../web/shutdown.js");

#[derive(Clone, Debug)]
pub(super) struct ManifestRecord {
    pub(super) manifest: Value,
    pub(super) directory: PathBuf,
}

#[derive(Default)]
pub(super) struct ManifestCatalog {
    pub(super) providers: HashMap<String, ManifestRecord>,
    pub(super) skills: HashMap<String, ManifestRecord>,
}

pub(super) fn load_manifest_catalog(workspace_root: &FsPath) -> Result<ManifestCatalog> {
    let mut catalog = ManifestCatalog::default();
    load_manifest_group(
        &workspace_root.join("providers"),
        "provider_type",
        "manager_ids",
        &mut catalog.providers,
    )?;
    load_manifest_group(
        &workspace_root.join("skills"),
        "skill_type",
        "manager_ids",
        &mut catalog.skills,
    )?;
    Ok(catalog)
}

fn load_manifest_group(
    root: &FsPath,
    identity_field: &str,
    aliases_field: &str,
    output: &mut HashMap<String, ManifestRecord>,
) -> Result<()> {
    if !root.is_dir() {
        return Ok(());
    }
    for entry in fs::read_dir(root).with_context(|| format!("reading {}", root.display()))? {
        let entry = entry?;
        if !entry.file_type()?.is_dir() {
            continue;
        }
        let manifest_path = entry.path().join("manifest.json");
        if !manifest_path.is_file() {
            continue;
        }
        let manifest: Value = serde_json::from_slice(
            &fs::read(&manifest_path)
                .with_context(|| format!("reading {}", manifest_path.display()))?,
        )
        .with_context(|| format!("parsing {}", manifest_path.display()))?;
        let Some(identity) = manifest.get(identity_field).and_then(Value::as_str) else {
            continue;
        };
        let record = ManifestRecord {
            manifest: manifest.clone(),
            directory: entry.path(),
        };
        output.insert(identity.to_string(), record.clone());
        if let Some(aliases) = manifest.get(aliases_field).and_then(Value::as_array) {
            for alias in aliases.iter().filter_map(Value::as_str) {
                output.insert(alias.to_string(), record.clone());
            }
        }
    }
    Ok(())
}

pub(super) async fn mainframe() -> Html<&'static str> {
    Html(MAINFRAME_HTML)
}

pub(super) async fn component_page() -> Html<&'static str> {
    Html(COMPONENT_HTML)
}

pub(super) async fn developer_page() -> Html<&'static str> {
    Html(DEVELOPER_HTML)
}

pub(super) async fn shutdown_page() -> Html<&'static str> {
    Html(SHUTDOWN_HTML)
}

pub(super) async fn manager_css() -> impl IntoResponse {
    static_asset("text/css; charset=utf-8", MANAGER_CSS)
}

pub(super) async fn mainframe_js() -> impl IntoResponse {
    static_asset("text/javascript; charset=utf-8", MAINFRAME_JS)
}

pub(super) async fn component_js() -> impl IntoResponse {
    static_asset("text/javascript; charset=utf-8", COMPONENT_JS)
}

pub(super) async fn developer_confirm_js() -> impl IntoResponse {
    static_asset("text/javascript; charset=utf-8", DEVELOPER_CONFIRM_JS)
}

pub(super) async fn shutdown_js() -> impl IntoResponse {
    static_asset("text/javascript; charset=utf-8", SHUTDOWN_JS)
}

fn static_asset(content_type: &'static str, body: &'static str) -> impl IntoResponse {
    (
        [
            (header::CONTENT_TYPE, content_type),
            (header::CACHE_CONTROL, "no-store"),
        ],
        body,
    )
}

pub(super) async fn overview(State(state): State<AppState>) -> Json<Value> {
    let provider_views = collect_provider_views(&state).await;
    let live_catalog = load_manifest_catalog(&state.workspace_root).ok();
    let skill_manifests = live_catalog
        .as_ref()
        .map(|catalog| &catalog.skills)
        .unwrap_or(state.skill_manifests.as_ref());
    let fabric_health = fetch_json(&state, &format!("{}/health", state.fabric_url)).await;
    let streams = fetch_json(&state, &format!("{}/v1/streams", state.fabric_url)).await;
    let snapshot = fetch_json(&state, &format!("{}/v1/snapshot", state.fabric_url)).await;
    let agent_health = fetch_json_with_timeout(
        &state,
        &format!("{}/health", state.agent_ui_url),
        Duration::from_millis(500),
    )
    .await;
    let agent_online = agent_health
        .as_ref()
        .ok()
        .and_then(|value| value.get("status"))
        .and_then(Value::as_str)
        .is_some_and(|value| value.eq_ignore_ascii_case("ok"));

    let providers: Vec<Value> = provider_views
        .iter()
        .map(|view| provider_summary(&state, view))
        .collect();
    let skills: Vec<Value> = unique_manifest_records(skill_manifests)
        .into_iter()
        .map(|record| skill_summary(record, snapshot.as_ref().ok(), agent_online))
        .collect();

    let provider_counts = count_statuses(&providers);
    let skill_counts = count_statuses(&skills);
    Json(json!({
        "schema": "midbrain.mainframe_overview",
        "schema_version": 1,
        "observed_at": Utc::now(),
        "core": {
            "manager": {
                "status": "ok",
                "service": "resource-provider-manager",
                "provider_autostart_enabled": state.provider_autostart_enabled,
            },
            "fabric": value_or_unavailable(fabric_health),
        },
        "analytics": {
            "provider_counts": provider_counts,
            "skill_counts": skill_counts,
            "fabric_stream_count": streams
                .as_ref()
                .ok()
                .and_then(Value::as_array)
                .map_or(0, Vec::len),
        },
        "providers": providers,
        "skills": skills,
        "agents": {
            "online": agent_online,
            "health": value_or_unavailable(agent_health),
            "regular_url": format!("{}/", state.agent_ui_url),
            "developer_url": format!("{}/dev", state.agent_ui_url),
            "start_policy": "ON_DEMAND",
        },
    }))
}

pub(super) async fn provider_detail(
    State(state): State<AppState>,
    Path(id): Path<String>,
) -> Result<Json<Value>, (StatusCode, Json<Value>)> {
    let provider_views = collect_provider_views(&state).await;
    let view = provider_views
        .iter()
        .find(|view| view.config.id == id)
        .ok_or_else(|| not_found("provider", &id))?;
    let manifest = state.provider_manifests.get(&id);
    let streams = fetch_json(&state, &format!("{}/v1/streams", state.fabric_url))
        .await
        .unwrap_or_else(|error| json!({"error": error}));
    let snapshot = fetch_json(&state, &format!("{}/v1/snapshot", state.fabric_url))
        .await
        .unwrap_or_else(|error| json!({"error": error}));
    let matching_streams = matching_provider_streams(&id, manifest, &streams);
    let latest = latest_for_streams(&snapshot, &matching_streams);
    let (status, tone) = provider_status(view);

    Ok(Json(json!({
        "schema": "midbrain.provider_observation",
        "schema_version": 1,
        "observed_at": Utc::now(),
        "kind": "provider",
        "id": view.config.id,
        "display_name": view.config.display_name,
        "status": status,
        "tone": tone,
        "process": {
            "state": view.process_state,
            "pid": view.pid,
            "last_exit": view.last_exit,
        },
        "registry": {
            "control_url": view.config.control_url,
            "configured_auto_start": view.config.auto_start,
            "heartbeat_timeout_ms": view.config.heartbeat_timeout_ms,
            "graceful_stop_timeout_ms": view.config.graceful_stop_timeout_ms,
        },
        "report": view.report,
        "manifest": manifest.map(|record| record.manifest.clone()),
        "streams": matching_streams,
        "latest": latest,
        "developer": resolve_provider_developer(view, manifest),
        "observation_policy": {
            "read_only": true,
            "mutation_routes_exposed_by_this_page": false,
        },
    })))
}

pub(super) async fn skill_detail(
    State(state): State<AppState>,
    Path(id): Path<String>,
) -> Result<Json<Value>, (StatusCode, Json<Value>)> {
    let live_catalog = load_manifest_catalog(&state.workspace_root).ok();
    let record = live_catalog
        .as_ref()
        .and_then(|catalog| catalog.skills.get(&id))
        .or_else(|| state.skill_manifests.get(&id))
        .cloned()
        .ok_or_else(|| not_found("skill", &id))?;
    let streams = fetch_json(&state, &format!("{}/v1/streams", state.fabric_url))
        .await
        .unwrap_or_else(|error| json!({"error": error}));
    let snapshot = fetch_json(&state, &format!("{}/v1/snapshot", state.fabric_url))
        .await
        .unwrap_or_else(|error| json!({"error": error}));
    let matching_streams = matching_manifest_streams(&record, &streams);
    let latest = latest_for_streams(&snapshot, &matching_streams);
    let agent_health = fetch_json_with_timeout(
        &state,
        &format!("{}/health", state.agent_ui_url),
        Duration::from_millis(500),
    )
    .await;
    let agent_online = agent_health
        .as_ref()
        .ok()
        .and_then(|value| value.get("status"))
        .and_then(Value::as_str)
        .is_some_and(|value| value.eq_ignore_ascii_case("ok"));
    let summary = skill_summary(&record, Some(&snapshot), agent_online);

    Ok(Json(json!({
        "schema": "midbrain.skill_observation",
        "schema_version": 1,
        "observed_at": Utc::now(),
        "kind": "skill",
        "id": id,
        "display_name": record.manifest.get("display_name"),
        "status": summary.get("status"),
        "tone": summary.get("tone"),
        "availability": summary.get("availability"),
        "manifest": record.manifest,
        "streams": matching_streams,
        "latest": latest,
        "developer": resolve_manifest_developer(&record.manifest, None),
        "observation_policy": {
            "read_only": true,
            "mutation_routes_exposed_by_this_page": false,
        },
    })))
}

#[derive(Deserialize)]
pub(super) struct DeveloperActivationRequest {
    confirmation: String,
}

pub(super) async fn activate_developer_surface(
    State(state): State<AppState>,
    Path((kind, id)): Path<(String, String)>,
    Json(request): Json<DeveloperActivationRequest>,
) -> Result<(StatusCode, Json<Value>), (StatusCode, Json<Value>)> {
    if request.confirmation != "ACTIVATE_DEVELOPER_SURFACE" {
        return Err(api_failure(
            StatusCode::BAD_REQUEST,
            "exact developer activation confirmation is required",
        ));
    }
    reject_if_shutdown_fenced(&state, "developer surface activation").await?;

    let live_catalog = load_manifest_catalog(&state.workspace_root).ok();
    let manifest = match kind.as_str() {
        "provider" => state.provider_manifests.get(&id),
        "skill" => live_catalog
            .as_ref()
            .and_then(|catalog| catalog.skills.get(&id))
            .or_else(|| state.skill_manifests.get(&id)),
        _ => {
            return Err(api_failure(
                StatusCode::BAD_REQUEST,
                "component kind must be provider or skill",
            ))
        }
    }
    .ok_or_else(|| not_found(&kind, &id))?
    .manifest
    .clone();
    let developer = manifest.pointer("/ui/developer").cloned().ok_or_else(|| {
        api_failure(
            StatusCode::CONFLICT,
            "this component does not advertise a developer surface",
        )
    })?;

    let provider_activation = if kind == "provider" {
        Some(ensure_provider_hot(&state, &id).await.map_err(|error| {
            api_failure(
                StatusCode::BAD_GATEWAY,
                format!("Provider activation failed: {error}"),
            )
        })?)
    } else {
        None
    };
    let launch = match developer.get("launch_command").and_then(Value::as_str) {
        Some(command) => Some(
            spawn_developer_launcher(&state, &kind, &id, command).map_err(|error| {
                api_failure(
                    StatusCode::INTERNAL_SERVER_ERROR,
                    format!("Developer UI launch failed: {error}"),
                )
            })?,
        ),
        None => None,
    };
    let descriptor = resolve_manifest_developer(
        &manifest,
        if kind == "provider" {
            state
                .configs
                .get(&id)
                .and_then(|config| config.control_url.as_deref())
        } else {
            None
        },
    );
    Ok((
        StatusCode::ACCEPTED,
        Json(json!({
            "status": "ACTIVATION_REQUESTED",
            "kind": kind,
            "id": id,
            "provider_activation": provider_activation,
            "development_launcher": launch,
            "developer": descriptor,
        })),
    ))
}

#[derive(Deserialize)]
pub(super) struct ShutdownRequest {
    confirmation: String,
}

pub(super) async fn shutdown_midbrain(
    State(state): State<AppState>,
    Json(request): Json<ShutdownRequest>,
) -> Result<(StatusCode, Json<Value>), (StatusCode, Json<Value>)> {
    if request.confirmation != "SHUT_DOWN_MIDBRAIN" {
        return Err(api_failure(
            StatusCode::BAD_REQUEST,
            "exact Midbrain shutdown confirmation is required",
        ));
    }
    let script = state
        .workspace_root
        .join("platform_core")
        .join("scripts")
        .join("stop_workspace.ps1");
    let script = resolve_workspace_script(&state, &script).map_err(internal_ui_error)?;
    let powershell_script = powershell_compatible_path(&script);
    let powershell_workspace = powershell_compatible_path(&state.workspace_root);
    let logs = state.workspace_root.join("platform_core").join("logs");
    fs::create_dir_all(&logs).map_err(internal_ui_error)?;
    let stdout = fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(logs.join("ui_shutdown.out.log"))
        .map_err(internal_ui_error)?;
    let stderr = fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(logs.join("ui_shutdown.err.log"))
        .map_err(internal_ui_error)?;
    let mut command = Command::new("powershell.exe");
    command
        .arg("-NoProfile")
        .arg("-ExecutionPolicy")
        .arg("Bypass")
        .arg("-File")
        .arg(&powershell_script)
        .arg("-DelayMilliseconds")
        .arg("750")
        .arg("-Quiet")
        .current_dir(&powershell_workspace)
        .stdin(Stdio::null())
        .stdout(Stdio::from(stdout))
        .stderr(Stdio::from(stderr))
        .kill_on_drop(false);
    configure_detached_process(&mut command);
    let child = command.spawn().map_err(internal_ui_error)?;
    let pid = child.id();
    drop(child);
    info!(?pid, "whole-workspace shutdown requested from Midbrain UI");
    Ok((
        StatusCode::ACCEPTED,
        Json(json!({
            "status": "SHUTDOWN_REQUESTED",
            "supervisor_pid": pid,
            "script": "platform_core/scripts/stop_workspace.ps1",
            "log": "platform_core/logs/ui_shutdown.out.log",
        })),
    ))
}

fn spawn_developer_launcher(
    state: &AppState,
    kind: &str,
    id: &str,
    launch_command: &str,
) -> Result<Value> {
    let relative = launch_command
        .trim()
        .trim_start_matches(".\\")
        .trim_start_matches("./")
        .replace('\\', std::path::MAIN_SEPARATOR_STR);
    let script = resolve_workspace_script(state, &state.workspace_root.join(relative))?;
    let powershell_script = powershell_compatible_path(&script);
    let powershell_workspace = powershell_compatible_path(&state.workspace_root);
    let logs = state.workspace_root.join("platform_core").join("logs");
    fs::create_dir_all(&logs)?;
    let safe_id: String = format!("{kind}_{id}")
        .chars()
        .map(|character| {
            if character.is_ascii_alphanumeric() {
                character
            } else {
                '_'
            }
        })
        .collect();
    let stdout_path = logs.join(format!("developer_{safe_id}.out.log"));
    let stderr_path = logs.join(format!("developer_{safe_id}.err.log"));
    let stdout = fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(&stdout_path)?;
    let stderr = fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(&stderr_path)?;
    let mut command = Command::new("powershell.exe");
    command
        .arg("-NoProfile")
        .arg("-ExecutionPolicy")
        .arg("Bypass")
        .arg("-File")
        .arg(&powershell_script)
        .arg("-NoBrowser")
        .current_dir(&powershell_workspace)
        .stdin(Stdio::null())
        .stdout(Stdio::from(stdout))
        .stderr(Stdio::from(stderr))
        .kill_on_drop(false);
    configure_detached_process(&mut command);
    let mut child = command
        .spawn()
        .with_context(|| format!("starting developer surface using {}", script.display()))?;
    let pid = child.id();
    let logged_id = id.to_string();
    tokio::spawn(async move {
        match child.wait().await {
            Ok(status) if status.success() => {
                info!(component_id = %logged_id, "developer launcher completed")
            }
            Ok(status) => warn!(
                component_id = %logged_id,
                ?status,
                "developer launcher exited with an error"
            ),
            Err(error) => warn!(
                component_id = %logged_id,
                %error,
                "developer launcher wait failed"
            ),
        }
    });
    Ok(json!({
        "status": "STARTING",
        "pid": pid,
        "stdout_log": stdout_path.strip_prefix(&state.workspace_root).unwrap_or(&stdout_path),
        "stderr_log": stderr_path.strip_prefix(&state.workspace_root).unwrap_or(&stderr_path),
    }))
}

fn resolve_workspace_script(state: &AppState, candidate: &FsPath) -> Result<PathBuf> {
    let resolved = fs::canonicalize(candidate)?;
    if !resolved.starts_with(&state.workspace_root)
        || resolved.extension().and_then(|value| value.to_str()) != Some("ps1")
    {
        return Err(anyhow::anyhow!(
            "command must resolve to a workspace PowerShell script"
        ));
    }
    Ok(resolved)
}

fn powershell_compatible_path(path: &FsPath) -> PathBuf {
    let value = path.as_os_str().to_string_lossy();
    if let Some(stripped) = value.strip_prefix(r"\\?\UNC\") {
        return PathBuf::from(format!(r"\\{stripped}"));
    }
    if let Some(stripped) = value.strip_prefix(r"\\?\") {
        return PathBuf::from(stripped);
    }
    path.to_path_buf()
}

fn configure_detached_process(command: &mut Command) {
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NEW_PROCESS_GROUP: u32 = 0x0000_0200;
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        command
            .as_std_mut()
            .creation_flags(CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW);
    }
    #[cfg(not(windows))]
    let _ = command;
}

async fn fetch_json(state: &AppState, url: &str) -> std::result::Result<Value, String> {
    fetch_json_with_timeout(state, url, Duration::from_millis(1_200)).await
}

async fn fetch_json_with_timeout(
    state: &AppState,
    url: &str,
    timeout: Duration,
) -> std::result::Result<Value, String> {
    let response = state
        .http
        .get(url)
        .timeout(timeout)
        .send()
        .await
        .map_err(|error| error.to_string())?;
    let status = response.status();
    if !status.is_success() {
        return Err(format!("{url} returned {status}"));
    }
    response
        .json::<Value>()
        .await
        .map_err(|error| error.to_string())
}

fn provider_summary(state: &AppState, view: &ProviderView) -> Value {
    let (status, tone) = provider_status(view);
    let manifest = state.provider_manifests.get(&view.config.id);
    json!({
        "id": view.config.id,
        "display_name": view.config.display_name,
        "status": status,
        "tone": tone,
        "process_state": view.process_state,
        "residency": view.report.as_ref().map(|report| report.residency.clone()),
        "health": view.report.as_ref().map(|report| report.health.clone()),
        "ready": view.report.as_ref().map(|report| report.ready),
        "expired": view.report.as_ref().map(|report| report.expired),
        "last_seen": view.report.as_ref().map(|report| report.last_seen),
        "configured_auto_start": view.config.auto_start,
        "observation_url": format!("/observe/provider/{}", view.config.id),
        "has_developer_surface": manifest
            .and_then(|record| record.manifest.pointer("/ui/developer"))
            .is_some(),
    })
}

fn provider_status(view: &ProviderView) -> (&'static str, &'static str) {
    let Some(report) = view.report.as_ref() else {
        return match view.process_state.as_str() {
            "running" => ("STARTING", "warning"),
            "stopping" => ("STOPPING", "warning"),
            _ => ("COLD", "muted"),
        };
    };
    if report.expired {
        return ("STALE", "danger");
    }
    if !matches!(
        report.health.to_ascii_uppercase().as_str(),
        "HEALTHY" | "OK"
    ) {
        return ("UNHEALTHY", "danger");
    }
    match (report.residency.to_ascii_uppercase().as_str(), report.ready) {
        ("HOT", true) => ("HOT / READY", "ok"),
        ("HOT", false) => ("HOT / NOT READY", "warning"),
        ("WARM", _) => ("WARM", "muted"),
        ("STARTING", _) | ("WAKING", _) | ("GOING_WARM", _) => ("TRANSITIONING", "warning"),
        ("COLD", _) => ("COLD", "muted"),
        _ => ("DEGRADED", "warning"),
    }
}

fn skill_summary(record: &ManifestRecord, snapshot: Option<&Value>, agent_online: bool) -> Value {
    let manifest = &record.manifest;
    let skill_type = manifest
        .get("skill_type")
        .and_then(Value::as_str)
        .unwrap_or("unknown");
    let availability = skill_availability(record, agent_online);
    let runtime_state = status_from_snapshot(manifest, snapshot);
    let (status, last_state, tone) = match runtime_state.as_deref() {
        Some("PENDING") => ("PENDING", None, "warning"),
        Some("RUNNING") => ("RUNNING", None, "ok"),
        Some("FAILED") => ("IDLE", Some("FAILED"), "danger"),
        Some("DEGRADED") => ("IDLE", Some("DEGRADED"), "warning"),
        Some("SUCCEEDED") => ("IDLE", Some("SUCCEEDED"), "ok"),
        Some("CANCELLED") => ("IDLE", Some("CANCELLED"), "muted"),
        Some(other) => ("IDLE", Some(other), "muted"),
        None if availability["available"] == Value::Bool(false) => ("UNAVAILABLE", None, "danger"),
        None => ("IDLE", None, "muted"),
    };
    json!({
        "id": skill_type,
        "display_name": manifest.get("display_name"),
        "status": status,
        "last_state": last_state,
        "tone": tone,
        "lifecycle": manifest.get("lifecycle"),
        "discoverable": manifest.pointer("/agent_discovery/discoverable"),
        "safety_class": manifest.pointer("/agent_discovery/safety_class"),
        "availability": availability,
        "observation_url": format!("/observe/skill/{skill_type}"),
        "has_developer_surface": manifest.pointer("/ui/developer").is_some(),
    })
}

fn skill_availability(record: &ManifestRecord, agent_online: bool) -> Value {
    let manifest = &record.manifest;
    let environment_path = manifest
        .pointer("/environment/path")
        .and_then(Value::as_str);
    let environment_ready = environment_path.is_none_or(|relative| {
        let environment = record.directory.join(relative);
        environment.join("Scripts").join("python.exe").is_file()
            || environment.join("bin").join("python").is_file()
    });
    let adapter_kind = manifest
        .pointer("/agent_discovery/execution_adapter/kind")
        .and_then(Value::as_str);
    let entrypoint = manifest
        .pointer("/agent_discovery/execution_adapter/entrypoint")
        .and_then(Value::as_str);
    let entrypoint_ready =
        entrypoint.is_none_or(|relative| record.directory.join(relative).exists());
    let runtime_ready = match adapter_kind {
        Some("IN_PROCESS_BOUND_INSTANCE") => agent_online,
        Some("MANUAL_LOCAL_ONLY") => false,
        _ => environment_ready && entrypoint_ready,
    };
    json!({
        "available": environment_ready && entrypoint_ready,
        "environment_ready": environment_ready,
        "entrypoint_ready": entrypoint_ready,
        "runtime_ready": runtime_ready,
        "agent_online": agent_online,
        "adapter_kind": adapter_kind,
    })
}

fn status_from_snapshot(manifest: &Value, snapshot: Option<&Value>) -> Option<String> {
    let snapshot = snapshot?.as_object()?;
    let published = manifest.get("published_streams")?.as_array()?;
    for stream in published.iter().filter_map(Value::as_str) {
        if !stream.ends_with(".status") {
            continue;
        }
        let Some(observation) = snapshot.get(stream) else {
            continue;
        };
        let data = observation.get("data").unwrap_or(observation);
        for key in ["state", "status", "lifecycle_state"] {
            if let Some(value) = data.get(key).and_then(Value::as_str) {
                return Some(value.to_ascii_uppercase());
            }
        }
    }
    None
}

fn matching_provider_streams(
    provider_id: &str,
    manifest: Option<&ManifestRecord>,
    streams: &Value,
) -> Vec<Value> {
    let published = manifest
        .and_then(|record| record.manifest.get("published_streams"))
        .and_then(Value::as_array)
        .map(|values| {
            values
                .iter()
                .filter_map(Value::as_str)
                .collect::<HashSet<_>>()
        })
        .unwrap_or_default();
    let provider_type = manifest
        .and_then(|record| record.manifest.get("provider_type"))
        .and_then(Value::as_str);
    streams
        .as_array()
        .into_iter()
        .flatten()
        .filter(|stream| {
            let name = stream.get("stream").and_then(Value::as_str).unwrap_or("");
            let producer = stream
                .get("provider_id")
                .and_then(Value::as_str)
                .unwrap_or("");
            published.contains(name)
                || producer == provider_id
                || provider_type.is_some_and(|value| value == producer)
        })
        .cloned()
        .collect()
}

fn matching_manifest_streams(record: &ManifestRecord, streams: &Value) -> Vec<Value> {
    let published = record
        .manifest
        .get("published_streams")
        .and_then(Value::as_array)
        .map(|values| {
            values
                .iter()
                .filter_map(Value::as_str)
                .collect::<HashSet<_>>()
        })
        .unwrap_or_default();
    streams
        .as_array()
        .into_iter()
        .flatten()
        .filter(|stream| {
            stream
                .get("stream")
                .and_then(Value::as_str)
                .is_some_and(|name| published.contains(name))
        })
        .cloned()
        .collect()
}

fn latest_for_streams(snapshot: &Value, streams: &[Value]) -> Value {
    let Some(snapshot) = snapshot.as_object() else {
        return json!({});
    };
    let mut output = Map::new();
    for stream in streams {
        let Some(name) = stream.get("stream").and_then(Value::as_str) else {
            continue;
        };
        if let Some(observation) = snapshot.get(name) {
            output.insert(name.to_string(), observation.clone());
        }
    }
    Value::Object(output)
}

fn resolve_provider_developer(view: &ProviderView, manifest: Option<&ManifestRecord>) -> Value {
    resolve_manifest_developer(
        manifest
            .map(|record| &record.manifest)
            .unwrap_or(&Value::Null),
        view.config.control_url.as_deref(),
    )
}

fn resolve_manifest_developer(manifest: &Value, control_url: Option<&str>) -> Value {
    let Some(developer) = manifest.pointer("/ui/developer") else {
        return json!({"available": false});
    };
    let direct_url = developer.get("url").and_then(Value::as_str);
    let relative = developer.get("url_from_control").and_then(Value::as_str);
    let url = direct_url.map(str::to_string).or_else(|| {
        control_url.zip(relative).map(|(base, path)| {
            format!(
                "{}/{}",
                base.trim_end_matches('/'),
                path.trim_start_matches('/')
            )
        })
    });
    json!({
        "available": url.is_some() || developer.get("launch_command").is_some(),
        "label": developer.get("label"),
        "url": url,
        "launch_command": developer.get("launch_command"),
        "stop_command": developer.get("stop_command"),
        "availability": developer.get("availability"),
        "activation_supported": true,
        "confirmation_required": true,
    })
}

fn unique_manifest_records(manifests: &HashMap<String, ManifestRecord>) -> Vec<&ManifestRecord> {
    let mut seen = HashSet::new();
    let mut records = Vec::new();
    for record in manifests.values() {
        let identity = record
            .manifest
            .get("skill_type")
            .or_else(|| record.manifest.get("provider_type"))
            .and_then(Value::as_str)
            .unwrap_or("unknown");
        if seen.insert(identity.to_string()) {
            records.push(record);
        }
    }
    records.sort_by_key(|record| {
        record
            .manifest
            .get("display_name")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_ascii_lowercase()
    });
    records
}

fn count_statuses(items: &[Value]) -> Value {
    let mut counts: HashMap<String, usize> = HashMap::new();
    for item in items {
        let status = item
            .get("status")
            .and_then(Value::as_str)
            .unwrap_or("UNKNOWN")
            .to_string();
        *counts.entry(status).or_default() += 1;
    }
    json!(counts)
}

fn value_or_unavailable(result: std::result::Result<Value, String>) -> Value {
    result.unwrap_or_else(|error| json!({"status": "unavailable", "error": error}))
}

fn not_found(kind: &str, id: &str) -> (StatusCode, Json<Value>) {
    (
        StatusCode::NOT_FOUND,
        Json(json!({"error": format!("unknown {kind} {id}")})),
    )
}

fn api_failure(status: StatusCode, message: impl Into<String>) -> (StatusCode, Json<Value>) {
    (status, Json(json!({"error": message.into()})))
}

fn internal_ui_error(error: impl std::fmt::Display) -> (StatusCode, Json<Value>) {
    api_failure(StatusCode::INTERNAL_SERVER_ERROR, error.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn terminal_skill_state_is_presented_as_idle_with_history() {
        let record = ManifestRecord {
            directory: PathBuf::from("."),
            manifest: json!({
                "skill_type": "example",
                "display_name": "Example",
                "published_streams": ["skills.example.status"],
            }),
        };
        let snapshot = json!({
            "skills.example.status": {
                "data": {"state": "SUCCEEDED"}
            }
        });
        let summary = skill_summary(&record, Some(&snapshot), false);
        assert_eq!(summary["status"], "IDLE");
        assert_eq!(summary["last_state"], "SUCCEEDED");
    }

    #[test]
    fn running_skill_state_remains_live() {
        let record = ManifestRecord {
            directory: PathBuf::from("."),
            manifest: json!({
                "skill_type": "example",
                "display_name": "Example",
                "published_streams": ["skills.example.status"],
            }),
        };
        let snapshot = json!({
            "skills.example.status": {
                "data": {"status": "RUNNING"}
            }
        });
        let summary = skill_summary(&record, Some(&snapshot), false);
        assert_eq!(summary["status"], "RUNNING");
        assert_eq!(summary["last_state"], Value::Null);
    }

    #[test]
    fn provider_developer_url_is_derived_from_control_url() {
        let manifest = json!({
            "ui": {
                "developer": {
                    "url_from_control": "/"
                }
            }
        });
        let resolved = resolve_manifest_developer(&manifest, Some("http://127.0.0.1:8793"));
        assert_eq!(resolved["url"], "http://127.0.0.1:8793/");
        assert_eq!(resolved["confirmation_required"], true);
    }

    #[test]
    fn skill_status_skips_unpublished_status_candidates() {
        let manifest = json!({
            "published_streams": [
                "skills.example.legacy.status",
                "skills.example.status"
            ]
        });
        let snapshot = json!({
            "skills.example.status": {
                "data": {"state": "RUNNING"}
            }
        });
        assert_eq!(
            status_from_snapshot(&manifest, Some(&snapshot)).as_deref(),
            Some("RUNNING")
        );
    }

    #[cfg(windows)]
    #[test]
    fn powershell_path_removes_windows_extended_prefix() {
        let input =
            FsPath::new(r"\\?\C:\Projects\Midbrain\platform_core\scripts\stop_workspace.ps1");
        assert_eq!(
            powershell_compatible_path(input),
            PathBuf::from(r"C:\Projects\Midbrain\platform_core\scripts\stop_workspace.ps1")
        );
    }
}
