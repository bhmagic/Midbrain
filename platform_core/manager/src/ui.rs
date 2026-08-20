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

#[derive(Clone, Debug)]
struct EffectorProfileRecord {
    document: Value,
    provider_relative_path: String,
    workspace_relative_path: String,
}

#[derive(Debug)]
struct EffectorCatalog {
    selection_path: PathBuf,
    selection: Value,
    arm_provider_id: String,
    provider_root: PathBuf,
    profiles: Vec<EffectorProfileRecord>,
    warnings: Vec<String>,
}

#[derive(Clone, Debug)]
struct ArmProfileRecord {
    document: Value,
    provider_relative_path: String,
    workspace_relative_path: String,
}

#[derive(Debug)]
struct ArmCatalog {
    selection_path: PathBuf,
    selection: Value,
    arm_provider_id: String,
    provider_root: PathBuf,
    profiles: Vec<ArmProfileRecord>,
    warnings: Vec<String>,
}

#[derive(Deserialize)]
pub(super) struct SelectArmRequest {
    profile_file: String,
    #[serde(default)]
    physical_arm_confirmed: bool,
}

#[derive(Deserialize)]
pub(super) struct SelectEffectorRequest {
    profile_id: String,
    profile_revision: String,
    #[serde(default)]
    physical_effector_confirmed: bool,
}

pub(super) async fn arm_profiles(
    State(state): State<AppState>,
) -> Result<Json<Value>, (StatusCode, Json<Value>)> {
    let _selection_guard = state.assembly_selection_lock.lock().await;
    let catalog = load_arm_catalog(&state.workspace_root).map_err(internal_ui_error)?;
    Ok(Json(arm_catalog_payload(&state, &catalog, None)))
}

pub(super) async fn select_arm(
    State(state): State<AppState>,
    Json(request): Json<SelectArmRequest>,
) -> Result<Json<Value>, (StatusCode, Json<Value>)> {
    if !request.physical_arm_confirmed {
        return Err(api_failure(
            StatusCode::BAD_REQUEST,
            "physical_arm_confirmed=true is required for a static assembly change",
        ));
    }
    reject_if_shutdown_fenced(&state, "arm-model selection").await?;
    let _selection_guard = state.assembly_selection_lock.lock().await;
    let catalog = load_arm_catalog(&state.workspace_root).map_err(internal_ui_error)?;
    let selected = catalog
        .profiles
        .iter()
        .find(|profile| profile.provider_relative_path == request.profile_file)
        .cloned()
        .ok_or_else(|| {
            api_failure(
                StatusCode::BAD_REQUEST,
                "the requested arm profile is not installed under the selected arm Provider",
            )
        })?;
    let active_reference = catalog
        .selection
        .pointer("/profiles/arm_model")
        .and_then(Value::as_object)
        .ok_or_else(|| internal_ui_error("assembly selection is missing profiles.arm_model"))?;
    let active_path = active_reference
        .get("relative_path")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .replace('\\', "/");
    if active_path == selected.provider_relative_path {
        return Ok(Json(arm_catalog_payload(
            &state,
            &catalog,
            Some("ALREADY_SELECTED"),
        )));
    }

    validate_arm_profile_compatibility(
        &catalog.provider_root,
        &catalog.selection,
        &selected.document,
    )
    .map_err(|error| api_failure(StatusCode::CONFLICT, error.to_string()))?;

    let affected = affected_provider_ids(&state.configs, &catalog.arm_provider_id);
    let provider_views = collect_provider_views(&state).await;
    let blockers: Vec<Value> = provider_views
        .iter()
        .filter(|view| affected.contains(&view.config.id))
        .filter(|view| {
            view.process_state != "stopped"
                || view.report.as_ref().is_some_and(|report| !report.expired)
        })
        .map(|view| {
            json!({
                "provider_id": view.config.id,
                "process_state": view.process_state,
                "residency": view.report.as_ref().map(|report| report.residency.as_str()),
            })
        })
        .collect();
    if !blockers.is_empty() {
        return Err((
            StatusCode::CONFLICT,
            Json(json!({
                "error": "stop the arm Provider and its running dependents before changing the static arm profile",
                "blocking_providers": blockers,
                "restart_required": true,
            })),
        ));
    }

    let model_id = required_string(selected.document.get("model_id"), "model_id")
        .map_err(internal_ui_error)?;
    let model_revision = required_string(selected.document.get("model_revision"), "model_revision")
        .map_err(internal_ui_error)?;
    let mut updated = catalog.selection.clone();
    let effector_revision = updated
        .pointer("/profiles/mounted_effector/expected_revision")
        .and_then(Value::as_str)
        .unwrap_or("unselected")
        .to_string();
    let selection_object = updated
        .as_object_mut()
        .ok_or_else(|| internal_ui_error("assembly selection must be a JSON object"))?;
    let assembly_id = selection_object
        .get("assembly_id")
        .and_then(Value::as_str)
        .unwrap_or("primary_manipulator")
        .to_string();
    selection_object.insert(
        "assembly_revision".to_string(),
        Value::String(format!(
            "{assembly_id}--{model_revision}--{effector_revision}"
        )),
    );
    let profiles = selection_object
        .get_mut("profiles")
        .and_then(Value::as_object_mut)
        .ok_or_else(|| internal_ui_error("assembly selection profiles must be an object"))?;
    profiles.insert(
        "arm_model".to_string(),
        json!({
            "relative_path": selected.provider_relative_path,
            "expected_schema": "physical_agent.robot_arm_model",
            "expected_id": model_id,
            "expected_revision": model_revision,
            "sha256": null,
        }),
    );
    write_assembly_selection(&catalog.selection_path, &updated).map_err(internal_ui_error)?;
    info!(
        model_id = %model_id,
        model_revision = %model_revision,
        profile_file = %request.profile_file,
        "static arm-model selection changed"
    );
    let refreshed = load_arm_catalog(&state.workspace_root).map_err(internal_ui_error)?;
    Ok(Json(arm_catalog_payload(
        &state,
        &refreshed,
        Some("SELECTED_RESTART_REQUIRED"),
    )))
}

pub(super) async fn effector_profiles(
    State(state): State<AppState>,
) -> Result<Json<Value>, (StatusCode, Json<Value>)> {
    let _selection_guard = state.assembly_selection_lock.lock().await;
    let catalog = load_effector_catalog(&state.workspace_root).map_err(internal_ui_error)?;
    Ok(Json(effector_catalog_payload(&state, &catalog, None)))
}

pub(super) async fn select_effector(
    State(state): State<AppState>,
    Json(request): Json<SelectEffectorRequest>,
) -> Result<Json<Value>, (StatusCode, Json<Value>)> {
    if !request.physical_effector_confirmed {
        return Err(api_failure(
            StatusCode::BAD_REQUEST,
            "physical_effector_confirmed=true is required for a static assembly change",
        ));
    }
    reject_if_shutdown_fenced(&state, "mounted-effector selection").await?;
    let _selection_guard = state.assembly_selection_lock.lock().await;
    let catalog = load_effector_catalog(&state.workspace_root).map_err(internal_ui_error)?;
    let selected = catalog
        .profiles
        .iter()
        .find(|profile| {
            profile.document.get("profile_id").and_then(Value::as_str)
                == Some(request.profile_id.as_str())
                && profile
                    .document
                    .get("profile_revision")
                    .and_then(Value::as_str)
                    == Some(request.profile_revision.as_str())
        })
        .cloned()
        .ok_or_else(|| {
            api_failure(
                StatusCode::BAD_REQUEST,
                "the requested mounted-effector identity is not an installed compatible profile",
            )
        })?;
    let active_reference = catalog
        .selection
        .pointer("/profiles/mounted_effector")
        .and_then(Value::as_object)
        .ok_or_else(|| {
            internal_ui_error("assembly selection is missing profiles.mounted_effector")
        })?;
    let already_selected = active_reference.get("expected_id").and_then(Value::as_str)
        == Some(request.profile_id.as_str())
        && active_reference
            .get("expected_revision")
            .and_then(Value::as_str)
            == Some(request.profile_revision.as_str());
    if already_selected {
        return Ok(Json(effector_catalog_payload(
            &state,
            &catalog,
            Some("ALREADY_SELECTED"),
        )));
    }

    let affected = affected_provider_ids(&state.configs, &catalog.arm_provider_id);
    let provider_views = collect_provider_views(&state).await;
    let blockers: Vec<Value> = provider_views
        .iter()
        .filter(|view| affected.contains(&view.config.id))
        .filter(|view| {
            view.process_state != "stopped"
                || view.report.as_ref().is_some_and(|report| !report.expired)
        })
        .map(|view| {
            json!({
                "provider_id": view.config.id,
                "process_state": view.process_state,
                "residency": view.report.as_ref().map(|report| report.residency.as_str()),
            })
        })
        .collect();
    if !blockers.is_empty() {
        return Err((
            StatusCode::CONFLICT,
            Json(json!({
                "error": "stop the arm Provider and its running dependents before changing the static mounted effector",
                "blocking_providers": blockers,
                "restart_required": true,
            })),
        ));
    }

    let mut updated = catalog.selection.clone();
    let selection_object = updated
        .as_object_mut()
        .ok_or_else(|| internal_ui_error("assembly selection must be a JSON object"))?;
    let assembly_id = selection_object
        .get("assembly_id")
        .and_then(Value::as_str)
        .unwrap_or("primary_manipulator")
        .to_string();
    selection_object.insert(
        "assembly_revision".to_string(),
        Value::String(format!("{assembly_id}--{}", request.profile_revision)),
    );
    let profiles = selection_object
        .get_mut("profiles")
        .and_then(Value::as_object_mut)
        .ok_or_else(|| internal_ui_error("assembly selection profiles must be an object"))?;
    profiles.insert(
        "mounted_effector".to_string(),
        json!({
            "relative_path": selected.provider_relative_path,
            "expected_schema": "midbrain.mounted_effector_profile",
            "expected_id": request.profile_id,
            "expected_revision": request.profile_revision,
            "sha256": null,
        }),
    );
    let has_effector_actuators = selected
        .document
        .get("actuator_groups")
        .and_then(Value::as_array)
        .is_some_and(|groups| !groups.is_empty());
    if !has_effector_actuators {
        if let Some(roles) = selection_object
            .get_mut("qualified_control_roles")
            .and_then(Value::as_object_mut)
        {
            roles.insert("grip".to_string(), Value::Null);
        }
    }
    write_assembly_selection(&catalog.selection_path, &updated).map_err(internal_ui_error)?;
    info!(
        profile_id = %request.profile_id,
        profile_revision = %request.profile_revision,
        "static mounted-effector selection changed"
    );
    let refreshed = load_effector_catalog(&state.workspace_root).map_err(internal_ui_error)?;
    Ok(Json(effector_catalog_payload(
        &state,
        &refreshed,
        Some("SELECTED_RESTART_REQUIRED"),
    )))
}

fn load_arm_catalog(workspace_root: &FsPath) -> Result<ArmCatalog> {
    let effector_catalog = load_effector_catalog(workspace_root)?;
    let provider_root = effector_catalog.provider_root.clone();
    let active_reference = effector_catalog
        .selection
        .pointer("/profiles/arm_model")
        .and_then(Value::as_object)
        .ok_or_else(|| anyhow::anyhow!("assembly selection is missing profiles.arm_model"))?;
    let active_relative = required_string(
        active_reference.get("relative_path"),
        "active arm profile path",
    )?;
    let mut paths = Vec::new();
    let registry_root = provider_root.join("config").join("arm_profiles");
    if registry_root.is_dir() {
        let resolved_registry = fs::canonicalize(&registry_root)
            .with_context(|| format!("resolving {}", registry_root.display()))?;
        if !resolved_registry.starts_with(&provider_root) {
            return Err(anyhow::anyhow!(
                "arm profile registry resolves outside the arm Provider"
            ));
        }
        paths.extend(
            fs::read_dir(&resolved_registry)
                .with_context(|| format!("reading {}", resolved_registry.display()))?
                .filter_map(|entry| entry.ok())
                .map(|entry| entry.path())
                .filter(|path| path.extension().and_then(|value| value.to_str()) == Some("json")),
        );
    }
    let active_path = provider_root.join(PathBuf::from(&active_relative));
    if active_path.is_file() {
        paths.push(active_path);
    }
    paths.sort();
    paths.dedup();

    let mut profiles = Vec::new();
    let mut warnings = Vec::new();
    let mut seen_paths = HashSet::new();
    for path in paths {
        match load_arm_profile(&effector_catalog.selection_path, &provider_root, &path) {
            Ok(profile) => {
                if seen_paths.insert(profile.provider_relative_path.clone()) {
                    profiles.push(profile);
                }
            }
            Err(error) => warnings.push(format!("{}: {error}", path.display())),
        }
    }
    if profiles.is_empty() {
        return Err(anyhow::anyhow!(
            "no valid arm profiles are installed under config/arm_profiles"
        ));
    }
    Ok(ArmCatalog {
        selection_path: effector_catalog.selection_path,
        selection: effector_catalog.selection,
        arm_provider_id: effector_catalog.arm_provider_id,
        provider_root,
        profiles,
        warnings,
    })
}

fn load_arm_profile(
    selection_path: &FsPath,
    provider_root: &FsPath,
    path: &FsPath,
) -> Result<ArmProfileRecord> {
    let resolved =
        fs::canonicalize(path).with_context(|| format!("resolving profile {}", path.display()))?;
    if !resolved.starts_with(provider_root) {
        return Err(anyhow::anyhow!(
            "arm profile resolves outside the selected arm Provider"
        ));
    }
    let document: Value = serde_json::from_slice(
        &fs::read(&resolved).with_context(|| format!("reading {}", resolved.display()))?,
    )
    .with_context(|| format!("parsing {}", resolved.display()))?;
    if document.get("schema").and_then(Value::as_str) != Some("physical_agent.robot_arm_model")
        || document.get("schema_version").and_then(Value::as_u64) != Some(1)
    {
        return Err(anyhow::anyhow!("unsupported robot arm model schema"));
    }
    required_string(document.get("model_id"), "model_id")?;
    required_string(document.get("model_revision"), "model_revision")?;
    required_string(document.get("display_name"), "display_name")?;
    if document
        .get("joints")
        .and_then(Value::as_array)
        .is_none_or(Vec::is_empty)
    {
        return Err(anyhow::anyhow!(
            "arm profile joints must be a non-empty array"
        ));
    }
    if document
        .get("links")
        .and_then(Value::as_array)
        .is_none_or(Vec::is_empty)
    {
        return Err(anyhow::anyhow!(
            "arm profile links must be a non-empty array"
        ));
    }
    if document
        .get("appendix")
        .is_some_and(|value| !value.is_object())
    {
        return Err(anyhow::anyhow!("arm profile appendix must be an object"));
    }
    let provider_relative_path = resolved
        .strip_prefix(provider_root)
        .map_err(|_| anyhow::anyhow!("arm profile is not Provider-relative"))?
        .to_string_lossy()
        .replace('\\', "/");
    let workspace_root = selection_path
        .parent()
        .and_then(FsPath::parent)
        .and_then(FsPath::parent)
        .ok_or_else(|| anyhow::anyhow!("cannot resolve workspace root from assembly selection"))?;
    Ok(ArmProfileRecord {
        document,
        provider_relative_path,
        workspace_relative_path: ui_path(&resolved, workspace_root),
    })
}

fn arm_catalog_payload(state: &AppState, catalog: &ArmCatalog, status: Option<&str>) -> Value {
    let active_relative = catalog
        .selection
        .pointer("/profiles/arm_model/relative_path")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .replace('\\', "/");
    let profiles: Vec<Value> = catalog
        .profiles
        .iter()
        .map(|profile| {
            let document = &profile.document;
            let locate_profile = document.pointer("/appendix/midbrain.skill.locate_arm_base.v1");
            let cad_path = locate_profile
                .and_then(|value| value.pointer("/mesh/path"))
                .and_then(Value::as_str);
            let reference_count = locate_profile
                .and_then(|value| value.get("reference_images"))
                .and_then(Value::as_array)
                .map_or(0, Vec::len);
            json!({
                "model_id": document.get("model_id"),
                "model_revision": document.get("model_revision"),
                "display_name": document.get("display_name"),
                "manufacturer": document.get("manufacturer"),
                "active": profile.provider_relative_path == active_relative,
                "profile_file": profile.workspace_relative_path,
                "provider_relative_path": profile.provider_relative_path,
                "root_frame": document.pointer("/coordinate_convention/root_frame"),
                "joint_count": document.get("joints").and_then(Value::as_array).map_or(0, Vec::len),
                "link_count": document.get("links").and_then(Value::as_array).map_or(0, Vec::len),
                "locate_arm_base": {
                    "configured": locate_profile.is_some(),
                    "cad_path": cad_path,
                    "reference_image_count": reference_count,
                },
            })
        })
        .collect();
    json!({
        "schema": "midbrain.arm_profile_catalog",
        "schema_version": 1,
        "status": status.unwrap_or("READY"),
        "selection_file": ui_path(&catalog.selection_path, &state.workspace_root),
        "assembly_id": catalog.selection.get("assembly_id"),
        "assembly_revision": catalog.selection.get("assembly_revision"),
        "arm_provider_id": catalog.arm_provider_id,
        "active_profile_file": active_relative,
        "profiles": profiles,
        "warnings": catalog.warnings,
        "selection_policy": {
            "physical_arm_confirmation_required": true,
            "affected_providers_must_be_stopped": true,
            "restart_required": true,
            "compatible_calibration_collision_and_effector_required": true,
        },
        "affected_provider_ids": affected_provider_ids(&state.configs, &catalog.arm_provider_id),
    })
}

fn referenced_profile(
    provider_root: &FsPath,
    selection: &Value,
    profile_key: &str,
) -> Result<Value> {
    let reference = selection
        .pointer(&format!("/profiles/{profile_key}"))
        .and_then(Value::as_object)
        .ok_or_else(|| anyhow::anyhow!("assembly selection is missing profiles.{profile_key}"))?;
    let relative = required_string(reference.get("relative_path"), "profile path")?;
    let relative_path = PathBuf::from(&relative);
    if relative_path.is_absolute() {
        return Err(anyhow::anyhow!(
            "profile path must remain Provider-relative"
        ));
    }
    let resolved = fs::canonicalize(provider_root.join(relative_path))
        .with_context(|| format!("resolving {profile_key} profile {relative}"))?;
    if !resolved.starts_with(provider_root) {
        return Err(anyhow::anyhow!(
            "{profile_key} profile resolves outside the arm Provider"
        ));
    }
    serde_json::from_slice(&fs::read(&resolved)?)
        .with_context(|| format!("parsing {}", resolved.display()))
}

fn validate_arm_profile_compatibility(
    provider_root: &FsPath,
    selection: &Value,
    selected: &Value,
) -> Result<()> {
    let model_id = required_string(selected.get("model_id"), "model_id")?;
    let model_revision = required_string(selected.get("model_revision"), "model_revision")?;
    let calibration = referenced_profile(provider_root, selection, "calibration")?;
    if calibration.get("model_id").and_then(Value::as_str) != Some(model_id.as_str()) {
        return Err(anyhow::anyhow!(
            "the selected arm profile is incompatible with the active calibration"
        ));
    }
    for key in ["collision_geometry", "mounted_effector"] {
        let profile = referenced_profile(provider_root, selection, key)?;
        let compatibility = profile
            .get("robot_compatibility")
            .and_then(Value::as_object)
            .ok_or_else(|| anyhow::anyhow!("{key} lacks robot_compatibility"))?;
        if compatibility.get("model_id").and_then(Value::as_str) != Some(model_id.as_str())
            || compatibility.get("model_revision").and_then(Value::as_str)
                != Some(model_revision.as_str())
        {
            return Err(anyhow::anyhow!(
                "the selected arm profile is incompatible with the active {key} profile"
            ));
        }
    }
    Ok(())
}

fn load_effector_catalog(workspace_root: &FsPath) -> Result<EffectorCatalog> {
    let workspace_root = fs::canonicalize(workspace_root)
        .with_context(|| format!("resolving workspace root {}", workspace_root.display()))?;
    let selection_path = workspace_root
        .join("config")
        .join("robot_assemblies")
        .join("primary_manipulator.json");
    let selection_path = fs::canonicalize(&selection_path).with_context(|| {
        format!(
            "resolving active assembly selection {}",
            selection_path.display()
        )
    })?;
    if !selection_path.starts_with(&workspace_root) {
        return Err(anyhow::anyhow!(
            "assembly selection resolves outside the workspace"
        ));
    }
    let selection: Value = serde_json::from_slice(
        &fs::read(&selection_path)
            .with_context(|| format!("reading {}", selection_path.display()))?,
    )
    .with_context(|| format!("parsing {}", selection_path.display()))?;
    if selection.get("schema").and_then(Value::as_str) != Some("midbrain.robot_assembly_selection")
        || selection.get("schema_version").and_then(Value::as_u64) != Some(1)
    {
        return Err(anyhow::anyhow!(
            "unsupported active robot assembly selection"
        ));
    }
    let arm_provider = selection
        .get("arm_provider")
        .and_then(Value::as_object)
        .ok_or_else(|| anyhow::anyhow!("assembly selection arm_provider must be an object"))?;
    let arm_provider_id = required_string(arm_provider.get("provider_id"), "arm provider ID")?;
    let provider_relative =
        required_string(arm_provider.get("provider_root"), "arm Provider root")?;
    let provider_relative_path = PathBuf::from(&provider_relative);
    if provider_relative_path.is_absolute() {
        return Err(anyhow::anyhow!(
            "arm Provider root must be workspace-relative"
        ));
    }
    let provider_root = fs::canonicalize(workspace_root.join(&provider_relative_path))
        .with_context(|| format!("resolving arm Provider root {provider_relative}"))?;
    if !provider_root.starts_with(&workspace_root) {
        return Err(anyhow::anyhow!(
            "arm Provider root resolves outside the workspace"
        ));
    }
    let effector_root = fs::canonicalize(provider_root.join("profiles").join("effectors"))
        .with_context(|| {
            format!("resolving effector profile directory under {provider_relative}")
        })?;
    if !effector_root.starts_with(&provider_root) {
        return Err(anyhow::anyhow!(
            "effector profile directory resolves outside the arm Provider"
        ));
    }
    let model_reference = selection
        .pointer("/profiles/arm_model")
        .and_then(Value::as_object)
        .ok_or_else(|| anyhow::anyhow!("assembly selection is missing its arm model reference"))?;
    let model_id = required_string(model_reference.get("expected_id"), "arm model ID")?;
    let model_revision = required_string(
        model_reference.get("expected_revision"),
        "arm model revision",
    )?;

    let mut paths: Vec<PathBuf> = fs::read_dir(&effector_root)
        .with_context(|| format!("reading {}", effector_root.display()))?
        .filter_map(|entry| entry.ok())
        .map(|entry| entry.path())
        .filter(|path| path.extension().and_then(|value| value.to_str()) == Some("json"))
        .collect();
    paths.sort();
    let mut profiles = Vec::new();
    let mut warnings = Vec::new();
    let mut identities = HashSet::new();
    for path in paths {
        match load_effector_profile(
            &workspace_root,
            &provider_root,
            &path,
            &model_id,
            &model_revision,
        ) {
            Ok(profile) => {
                let identity = (
                    profile
                        .document
                        .get("profile_id")
                        .and_then(Value::as_str)
                        .unwrap_or_default()
                        .to_string(),
                    profile
                        .document
                        .get("profile_revision")
                        .and_then(Value::as_str)
                        .unwrap_or_default()
                        .to_string(),
                );
                if !identities.insert(identity.clone()) {
                    return Err(anyhow::anyhow!(
                        "duplicate mounted-effector identity {} at revision {}",
                        identity.0,
                        identity.1
                    ));
                }
                profiles.push(profile);
            }
            Err(error) => warnings.push(format!("{}: {error}", ui_path(&path, &workspace_root))),
        }
    }
    if profiles.is_empty() {
        return Err(anyhow::anyhow!(
            "no compatible mounted-effector profiles are installed"
        ));
    }
    Ok(EffectorCatalog {
        selection_path,
        selection,
        arm_provider_id,
        provider_root,
        profiles,
        warnings,
    })
}

fn load_effector_profile(
    workspace_root: &FsPath,
    provider_root: &FsPath,
    path: &FsPath,
    model_id: &str,
    model_revision: &str,
) -> Result<EffectorProfileRecord> {
    let resolved =
        fs::canonicalize(path).with_context(|| format!("resolving profile {}", path.display()))?;
    if !resolved.starts_with(provider_root) {
        return Err(anyhow::anyhow!("profile resolves outside the arm Provider"));
    }
    let document: Value = serde_json::from_slice(
        &fs::read(&resolved).with_context(|| format!("reading {}", resolved.display()))?,
    )
    .with_context(|| format!("parsing {}", resolved.display()))?;
    if document.get("schema").and_then(Value::as_str) != Some("midbrain.mounted_effector_profile")
        || document.get("schema_version").and_then(Value::as_u64) != Some(1)
    {
        return Err(anyhow::anyhow!(
            "unsupported mounted-effector profile schema"
        ));
    }
    required_string(document.get("profile_id"), "profile_id")?;
    required_string(document.get("profile_revision"), "profile_revision")?;
    required_string(document.get("display_name"), "display_name")?;
    let compatibility = document
        .get("robot_compatibility")
        .and_then(Value::as_object)
        .ok_or_else(|| anyhow::anyhow!("robot_compatibility must be an object"))?;
    if compatibility.get("model_id").and_then(Value::as_str) != Some(model_id)
        || compatibility.get("model_revision").and_then(Value::as_str) != Some(model_revision)
    {
        return Err(anyhow::anyhow!(
            "profile is incompatible with the selected arm model"
        ));
    }
    let inertial = document
        .get("inertial")
        .and_then(Value::as_object)
        .ok_or_else(|| anyhow::anyhow!("inertial must be an object"))?;
    let mass = inertial
        .get("mass_kg")
        .and_then(Value::as_f64)
        .ok_or_else(|| anyhow::anyhow!("inertial.mass_kg must be a number"))?;
    if !mass.is_finite() || mass < 0.0 {
        return Err(anyhow::anyhow!(
            "inertial.mass_kg must be finite and non-negative"
        ));
    }
    let com = inertial
        .get("center_of_mass_m")
        .and_then(Value::as_array)
        .ok_or_else(|| anyhow::anyhow!("inertial.center_of_mass_m must be an array"))?;
    if com.len() != 3
        || com
            .iter()
            .any(|value| value.as_f64().is_none_or(|item| !item.is_finite()))
    {
        return Err(anyhow::anyhow!(
            "inertial.center_of_mass_m must contain three finite numbers"
        ));
    }
    if !document
        .get("collision_primitives")
        .is_some_and(Value::is_array)
    {
        return Err(anyhow::anyhow!("collision_primitives must be an array"));
    }
    let provider_relative_path = resolved
        .strip_prefix(provider_root)
        .map_err(|_| anyhow::anyhow!("profile is not Provider-relative"))?;
    Ok(EffectorProfileRecord {
        document,
        provider_relative_path: provider_relative_path.to_string_lossy().replace('\\', "/"),
        workspace_relative_path: ui_path(&resolved, workspace_root),
    })
}

fn effector_catalog_payload(
    state: &AppState,
    catalog: &EffectorCatalog,
    status: Option<&str>,
) -> Value {
    let active = catalog
        .selection
        .pointer("/profiles/mounted_effector")
        .and_then(Value::as_object);
    let active_id = active
        .and_then(|value| value.get("expected_id"))
        .and_then(Value::as_str);
    let active_revision = active
        .and_then(|value| value.get("expected_revision"))
        .and_then(Value::as_str);
    let profiles: Vec<Value> = catalog
        .profiles
        .iter()
        .map(|profile| {
            let document = &profile.document;
            let inertial = document.get("inertial").cloned().unwrap_or(Value::Null);
            let profile_id = document.get("profile_id").and_then(Value::as_str);
            let profile_revision = document.get("profile_revision").and_then(Value::as_str);
            json!({
                "profile_id": profile_id,
                "profile_revision": profile_revision,
                "display_name": document.get("display_name"),
                "assembly_type": document.get("assembly_type"),
                "qualification": document.get("qualification"),
                "active": profile_id == active_id && profile_revision == active_revision,
                "profile_file": profile.workspace_relative_path,
                "provider_relative_path": profile.provider_relative_path,
                "inertial": inertial,
                "controlled_frame": document.get("controlled_frame"),
                "collision_primitive_count": document
                    .get("collision_primitives")
                    .and_then(Value::as_array)
                    .map_or(0, Vec::len),
                "actuator_group_count": document
                    .get("actuator_groups")
                    .and_then(Value::as_array)
                    .map_or(0, Vec::len),
            })
        })
        .collect();
    json!({
        "schema": "midbrain.effector_profile_catalog",
        "schema_version": 1,
        "status": status.unwrap_or("READY"),
        "selection_file": ui_path(&catalog.selection_path, &state.workspace_root),
        "assembly_id": catalog.selection.get("assembly_id"),
        "assembly_revision": catalog.selection.get("assembly_revision"),
        "arm_provider_id": catalog.arm_provider_id,
        "active_profile_id": active_id,
        "active_profile_revision": active_revision,
        "profiles": profiles,
        "warnings": catalog.warnings,
        "selection_policy": {
            "physical_effector_confirmation_required": true,
            "affected_providers_must_be_stopped": true,
            "restart_required": true,
            "profile_content_edit_requires_restart": true,
        },
        "affected_provider_ids": affected_provider_ids(&state.configs, &catalog.arm_provider_id),
    })
}

fn affected_provider_ids(
    configs: &HashMap<String, super::ProviderConfig>,
    arm_provider_id: &str,
) -> HashSet<String> {
    let mut affected = HashSet::from([arm_provider_id.to_string()]);
    loop {
        let before = affected.len();
        for config in configs.values() {
            if config
                .dependencies
                .iter()
                .any(|dependency| affected.contains(dependency))
            {
                affected.insert(config.id.clone());
            }
        }
        if affected.len() == before {
            return affected;
        }
    }
}

fn write_assembly_selection(selection_path: &FsPath, selection: &Value) -> Result<()> {
    let payload = serde_json::to_string_pretty(selection)? + "\n";
    let next_path = selection_path.with_extension("json.next");
    let backup_path = selection_path.with_extension("json.previous");
    fs::copy(selection_path, &backup_path)
        .with_context(|| format!("backing up assembly selection to {}", backup_path.display()))?;
    fs::write(&next_path, payload.as_bytes())
        .with_context(|| format!("writing staged assembly selection {}", next_path.display()))?;
    let staged: Value = serde_json::from_slice(&fs::read(&next_path)?)?;
    if staged.get("schema").and_then(Value::as_str) != Some("midbrain.robot_assembly_selection") {
        let _ = fs::remove_file(&next_path);
        return Err(anyhow::anyhow!(
            "staged assembly selection failed validation"
        ));
    }
    if let Err(error) = fs::copy(&next_path, selection_path) {
        let _ = fs::copy(&backup_path, selection_path);
        let _ = fs::remove_file(&next_path);
        return Err(error).context("activating staged assembly selection");
    }
    fs::remove_file(&next_path)
        .with_context(|| format!("removing staged selection {}", next_path.display()))?;
    Ok(())
}

fn required_string(value: Option<&Value>, label: &str) -> Result<String> {
    let value = value
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .ok_or_else(|| anyhow::anyhow!("{label} must be a non-empty string"))?;
    Ok(value.to_string())
}

fn ui_path(path: &FsPath, workspace_root: &FsPath) -> String {
    path.strip_prefix(workspace_root)
        .unwrap_or(path)
        .to_string_lossy()
        .replace('\\', "/")
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

    fn provider_config(id: &str, dependencies: &[&str]) -> crate::ProviderConfig {
        crate::ProviderConfig {
            id: id.to_string(),
            display_name: id.to_string(),
            dependencies: dependencies.iter().map(|value| value.to_string()).collect(),
            command: "unused".to_string(),
            args: Vec::new(),
            cwd: None,
            control_url: None,
            auto_start: false,
            graceful_stop_timeout_ms: 5_000,
            force_kill_on_stop_timeout: true,
            heartbeat_timeout_ms: 3_500,
            safe_state_request_path: None,
            safe_state_timeout_ms: 35_000,
            env: HashMap::new(),
        }
    }

    #[test]
    fn mainframe_exposes_guarded_effector_selection() {
        assert!(MAINFRAME_HTML.contains("id=\"effectorSelect\""));
        assert!(MAINFRAME_HTML.contains("id=\"effectorPhysicalConfirmation\""));
        assert!(MAINFRAME_JS.contains("physical_effector_confirmed: true"));
        assert!(MAINFRAME_JS.contains("/v1/ui/robot-assembly/effectors"));
    }

    #[test]
    fn mainframe_exposes_guarded_arm_profile_selection() {
        assert!(MAINFRAME_HTML.contains("id=\"armSelect\""));
        assert!(MAINFRAME_HTML.contains("id=\"armPhysicalConfirmation\""));
        assert!(MAINFRAME_JS.contains("physical_arm_confirmed: true"));
        assert!(MAINFRAME_JS.contains("/v1/ui/robot-assembly/arms"));
        assert!(MAINFRAME_JS.contains("profile_file: profile.provider_relative_path"));
    }

    #[test]
    fn effector_selection_fences_transitive_arm_dependents() {
        let configs = HashMap::from([
            (
                "robot_arm.rebot_dm".to_string(),
                provider_config("robot_arm.rebot_dm", &[]),
            ),
            (
                "robot_arm.primary.integrated".to_string(),
                provider_config("robot_arm.primary.integrated", &["robot_arm.rebot_dm"]),
            ),
            (
                "world_model.arm_scene_compiler".to_string(),
                provider_config(
                    "world_model.arm_scene_compiler",
                    &["robot_arm.primary.integrated"],
                ),
            ),
            (
                "camera.femto_bolt".to_string(),
                provider_config("camera.femto_bolt", &[]),
            ),
        ]);

        let affected = affected_provider_ids(&configs, "robot_arm.rebot_dm");

        assert!(affected.contains("robot_arm.rebot_dm"));
        assert!(affected.contains("robot_arm.primary.integrated"));
        assert!(affected.contains("world_model.arm_scene_compiler"));
        assert!(!affected.contains("camera.femto_bolt"));
    }

    #[test]
    fn effector_catalog_reads_only_compatible_provider_owned_profiles() {
        let root = std::env::temp_dir().join(format!(
            "midbrain-effector-catalog-{}",
            uuid::Uuid::new_v4()
        ));
        let selection_dir = root.join("config").join("robot_assemblies");
        let profile_dir = root
            .join("providers")
            .join("test_arm")
            .join("profiles")
            .join("effectors");
        fs::create_dir_all(&selection_dir).unwrap();
        fs::create_dir_all(&profile_dir).unwrap();
        fs::write(
            selection_dir.join("primary_manipulator.json"),
            serde_json::to_vec_pretty(&json!({
                "schema": "midbrain.robot_assembly_selection",
                "schema_version": 1,
                "assembly_id": "primary_manipulator",
                "assembly_revision": "test-v1",
                "arm_provider": {
                    "provider_id": "robot_arm.test",
                    "provider_root": "providers/test_arm"
                },
                "profiles": {
                    "arm_model": {
                        "expected_id": "test_arm",
                        "expected_revision": "test-arm-v1"
                    },
                    "mounted_effector": {
                        "expected_id": "test_arm.blade",
                        "expected_revision": "blade-v1"
                    }
                }
            }))
            .unwrap(),
        )
        .unwrap();
        fs::write(
            profile_dir.join("blade.v1.json"),
            serde_json::to_vec_pretty(&json!({
                "schema": "midbrain.mounted_effector_profile",
                "schema_version": 1,
                "profile_id": "test_arm.blade",
                "profile_revision": "blade-v1",
                "display_name": "Test blade",
                "robot_compatibility": {
                    "model_id": "test_arm",
                    "model_revision": "test-arm-v1"
                },
                "inertial": {
                    "mass_kg": 0.7,
                    "center_of_mass_m": [0.0, 0.0, -0.06]
                },
                "collision_primitives": []
            }))
            .unwrap(),
        )
        .unwrap();

        let catalog = load_effector_catalog(&root).unwrap();

        assert_eq!(catalog.arm_provider_id, "robot_arm.test");
        assert_eq!(catalog.profiles.len(), 1);
        assert_eq!(
            catalog.profiles[0].provider_relative_path,
            "profiles/effectors/blade.v1.json"
        );
        assert!(catalog.warnings.is_empty());
        fs::remove_dir_all(&root).unwrap();
    }

    #[test]
    fn assembly_selection_write_keeps_recoverable_previous_copy() {
        let root = std::env::temp_dir().join(format!(
            "midbrain-effector-selection-{}",
            uuid::Uuid::new_v4()
        ));
        fs::create_dir_all(&root).unwrap();
        let selection_path = root.join("primary_manipulator.json");
        let previous = json!({
            "schema": "midbrain.robot_assembly_selection",
            "assembly_revision": "previous"
        });
        let next = json!({
            "schema": "midbrain.robot_assembly_selection",
            "assembly_revision": "next"
        });
        fs::write(
            &selection_path,
            serde_json::to_vec_pretty(&previous).unwrap(),
        )
        .unwrap();

        write_assembly_selection(&selection_path, &next).unwrap();

        let active: Value = serde_json::from_slice(&fs::read(&selection_path).unwrap()).unwrap();
        let backup: Value = serde_json::from_slice(
            &fs::read(selection_path.with_extension("json.previous")).unwrap(),
        )
        .unwrap();
        assert_eq!(active["assembly_revision"], "next");
        assert_eq!(backup["assembly_revision"], "previous");
        assert!(!selection_path.with_extension("json.next").exists());
        fs::remove_dir_all(&root).unwrap();
    }

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
