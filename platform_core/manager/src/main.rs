use anyhow::{anyhow, Context, Result};
use axum::{
    extract::{Path, State},
    http::StatusCode,
    routing::{get, post},
    Json, Router,
};
use chrono::{DateTime, Duration as ChronoDuration, Utc};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::{
    collections::{HashMap, HashSet},
    env,
    path::{Path as FsPath, PathBuf},
    process::Stdio,
    sync::Arc,
    time::Duration,
};
use tokio::{
    io::{AsyncBufReadExt, BufReader},
    process::{Child, Command},
    sync::Mutex,
    time::{sleep, Instant},
};
use tower_http::{cors::CorsLayer, trace::TraceLayer};
use tracing::{error, info, warn};
use uuid::Uuid;

mod ui;

#[derive(Debug, Clone, Deserialize)]
struct ProviderFile {
    providers: Vec<ProviderConfig>,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
struct ProviderConfig {
    id: String,
    display_name: String,
    command: String,
    #[serde(default)]
    args: Vec<String>,
    cwd: Option<String>,
    control_url: Option<String>,
    #[serde(default)]
    auto_start: bool,
    #[serde(default = "default_stop_timeout")]
    graceful_stop_timeout_ms: u64,
    #[serde(default = "default_force_kill_on_stop_timeout")]
    force_kill_on_stop_timeout: bool,
    #[serde(default = "default_heartbeat_timeout")]
    heartbeat_timeout_ms: u64,
    #[serde(default)]
    safe_state_request_path: Option<String>,
    #[serde(default = "default_safe_state_timeout")]
    safe_state_timeout_ms: u64,
    #[serde(default)]
    env: HashMap<String, String>,
}

fn default_stop_timeout() -> u64 {
    5_000
}

fn default_force_kill_on_stop_timeout() -> bool {
    true
}

fn default_heartbeat_timeout() -> u64 {
    3_500
}

fn default_safe_state_timeout() -> u64 {
    35_000
}

fn should_force_terminate(
    explicit_force: bool,
    timeout_elapsed: bool,
    force_kill_on_stop_timeout: bool,
) -> bool {
    explicit_force || (timeout_elapsed && force_kill_on_stop_timeout)
}

fn provider_report_is_fresh(
    report: &ProviderReport,
    heartbeat_timeout_ms: u64,
    now: DateTime<Utc>,
) -> bool {
    if report.expired {
        return false;
    }
    let age_ms = now
        .signed_duration_since(report.last_seen)
        .num_milliseconds()
        .max(0) as u64;
    age_ms <= heartbeat_timeout_ms
}

fn provider_identity_conflicts(
    report: &ProviderReport,
    instance_id: &str,
    boot_id: &str,
    heartbeat_timeout_ms: u64,
    now: DateTime<Utc>,
) -> bool {
    provider_report_is_fresh(report, heartbeat_timeout_ms, now)
        && (report.instance_id != instance_id || report.boot_id != boot_id)
}

#[derive(Debug, Clone, Serialize)]
struct ProviderReport {
    provider_id: String,
    instance_id: String,
    boot_id: String,
    residency: String,
    health: String,
    ready: bool,
    pid: Option<u32>,
    details: Value,
    last_seen: DateTime<Utc>,
    expired: bool,
}

#[derive(Debug, Deserialize)]
struct RegisterRequest {
    provider_id: String,
    instance_id: String,
    boot_id: String,
    residency: String,
    health: String,
    ready: bool,
    pid: Option<u32>,
    #[serde(default)]
    details: Value,
}

#[derive(Debug, Deserialize)]
struct HeartbeatRequest {
    provider_id: String,
    instance_id: String,
    boot_id: String,
    residency: String,
    health: String,
    ready: bool,
    pid: Option<u32>,
    #[serde(default)]
    details: Value,
}

#[derive(Debug, Serialize)]
struct CapabilityView {
    capability: String,
    provider_id: String,
    provider_instance_id: Option<String>,
    available: bool,
    health: String,
    residency: String,
    ready: bool,
    expired: bool,
    last_seen: Option<DateTime<Utc>>,
}

#[derive(Debug, Clone, Deserialize)]
struct CapabilityBindingRequest {
    required_capabilities: Vec<String>,
    #[serde(default)]
    fallback_provider_ids: HashMap<String, String>,
    #[serde(default)]
    allowed_provider_ids: Vec<String>,
    #[serde(default)]
    excluded_provider_ids: Vec<String>,
    #[serde(default)]
    request_id: Option<String>,
    #[serde(default)]
    related_skill_id: Option<String>,
}

#[derive(Debug, Clone, Serialize)]
struct CapabilityBindingCandidate {
    provider_id: String,
    provider_instance_id: Option<String>,
    boot_id: Option<String>,
    advertised: bool,
    available: bool,
    ready: bool,
    health: String,
    residency: String,
    expired: bool,
}

#[derive(Debug, Clone, Serialize)]
struct CapabilityBindingSelection {
    capability: String,
    provider_id: String,
    provider_instance_id: Option<String>,
    boot_id: Option<String>,
    available: bool,
    compatibility_verified: bool,
    requires_activation: bool,
    selection_reason: String,
    candidates_considered: Vec<CapabilityBindingCandidate>,
}

#[derive(Debug, Clone, Serialize)]
struct CapabilityBindingRecord {
    binding_id: String,
    request_id: String,
    related_skill_id: Option<String>,
    created_at: DateTime<Utc>,
    validated_at: DateTime<Utc>,
    enforcement: String,
    validity: String,
    validation_issues: Vec<String>,
    status: String,
    selections: Vec<CapabilityBindingSelection>,
    unresolved_capabilities: Vec<String>,
}

#[derive(Debug, Serialize)]
struct ProviderView {
    config: ProviderConfig,
    process_state: String,
    pid: Option<u32>,
    last_exit: Option<String>,
    report: Option<ProviderReport>,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
struct MotionInhibitLease {
    owner_id: String,
    reason: String,
    related_skill_id: Option<String>,
    issued_at: DateTime<Utc>,
}

#[derive(Debug, Deserialize)]
struct MotionInhibitRequest {
    owner_id: String,
    reason: String,
    #[serde(default)]
    related_skill_id: Option<String>,
}

#[derive(Debug, Deserialize)]
struct MotionInhibitReleaseRequest {
    owner_id: String,
}

#[derive(Debug, Clone, Deserialize)]
struct AcquireControlAuthorityRequest {
    resource_id: String,
    owner_id: String,
    #[serde(default)]
    permissions: Vec<String>,
    #[serde(default = "default_authority_duration_ms")]
    duration_ms: u64,
    #[serde(default = "default_authority_renewal_interval_ms")]
    renewal_interval_ms: u64,
    #[serde(default)]
    preempt: bool,
    #[serde(default)]
    preemption_policy: Option<String>,
    #[serde(default)]
    safe_relinquish: Option<String>,
    #[serde(default)]
    related_skill_id: Option<String>,
}

#[derive(Debug, Clone, Deserialize)]
struct RenewControlAuthorityRequest {
    owner_id: String,
    #[serde(default = "default_authority_duration_ms")]
    duration_ms: u64,
}

#[derive(Debug, Clone, Deserialize)]
struct ReleaseControlAuthorityRequest {
    owner_id: String,
    #[serde(default)]
    reason: Option<String>,
}

#[derive(Debug, Clone, Serialize)]
struct ControlAuthorityLease {
    lease_id: String,
    resource_id: String,
    owner_id: String,
    permissions: Vec<String>,
    issued_at: DateTime<Utc>,
    expires_at: DateTime<Utc>,
    renewal_interval_ms: u64,
    fencing_generation: u64,
    preemption_policy: String,
    safe_relinquish: String,
    state: String,
    related_skill_id: Option<String>,
    last_transition_reason: String,
}

#[derive(Debug, Clone, Serialize)]
struct ControlAuthorityResourceView {
    resource_id: String,
    enforcement: String,
    active_lease: Option<ControlAuthorityLease>,
    latest_fencing_generation: u64,
}

#[derive(Debug, Clone, Deserialize)]
struct ShutdownPlanRequest {
    owner_id: String,
    reason: String,
    #[serde(default)]
    request_id: Option<String>,
}

#[derive(Debug, Clone, Serialize)]
struct ShutdownPlanStep {
    order: u32,
    action: String,
    provider_ids: Vec<String>,
    required_confirmation: String,
}

#[derive(Debug, Clone, Serialize)]
struct ShutdownPlanRecord {
    shutdown_id: String,
    request_id: String,
    requested_at: DateTime<Utc>,
    requested_by: String,
    reason: String,
    state: String,
    enforcement: String,
    steps: Vec<ShutdownPlanStep>,
    blockers: Vec<String>,
}

#[derive(Debug, Clone, Deserialize)]
struct ShutdownExecuteRequest {
    request_id: String,
    confirmation: String,
}

#[derive(Debug, Clone, Serialize)]
struct ShutdownProviderResult {
    provider_id: String,
    state: String,
    acknowledgement: Option<Value>,
    error: Option<String>,
}

#[derive(Debug, Clone, Serialize)]
struct ShutdownStepResult {
    order: u32,
    action: String,
    state: String,
    started_at: DateTime<Utc>,
    completed_at: Option<DateTime<Utc>>,
    provider_results: Vec<ShutdownProviderResult>,
    acknowledgement: Option<String>,
    error: Option<String>,
}

#[derive(Debug, Clone, Serialize)]
struct ShutdownExecutionRecord {
    execution_id: String,
    shutdown_id: String,
    request_id: String,
    requested_by: String,
    started_at: DateTime<Utc>,
    completed_at: Option<DateTime<Utc>>,
    state: String,
    enforcement: String,
    current_step: Option<u32>,
    step_results: Vec<ShutdownStepResult>,
    failures: Vec<String>,
    supervisor_actions: Vec<String>,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
struct WorkcellCalibrationActivationRequest {
    request_id: String,
    activated_by: String,
    candidate: Value,
    review_decision: Value,
    review_identity_assertion: String,
    #[serde(default = "default_workcell_activation_duration_ms")]
    duration_ms: u64,
}

#[derive(Debug, Clone, Deserialize)]
struct WorkcellCalibrationRevocationRequest {
    request_id: String,
    revoked_by: String,
    reason: String,
}

#[derive(Debug, Clone, Serialize)]
struct WorkcellCalibrationActivationRecord {
    activation_id: String,
    request_id: String,
    request_sha256: String,
    candidate_id: String,
    candidate_sha256: String,
    calibration_revision: String,
    review_decision_id: String,
    activated_by: String,
    activated_at: DateTime<Utc>,
    expires_at: DateTime<Utc>,
    expires_at_us: u64,
    state: String,
    enforcement: String,
    motion_usable: bool,
    session_epoch: String,
    world_frame: String,
    vio_world_frame: String,
    camera_frame: String,
    arm_base_frame: String,
    convention_id: String,
    camera_optical_convention_id: String,
    camera_provider_id: String,
    camera_provider_instance_id: String,
    camera_boot_id: String,
    camera_calibration_revision: String,
    vio_provider_id: String,
    vio_provider_instance_id: String,
    vio_boot_id: String,
    transforms: Value,
    reviewer: Value,
    last_transition_reason: String,
}

fn default_workcell_activation_duration_ms() -> u64 {
    120_000
}

fn default_authority_duration_ms() -> u64 {
    6_000
}

fn default_authority_renewal_interval_ms() -> u64 {
    1_000
}

#[derive(Debug, Deserialize, Serialize)]
struct ProviderRequest {
    action: String,
    #[serde(default)]
    payload: Value,
    #[serde(default)]
    request_id: Option<String>,
    #[serde(default)]
    related_skill_id: Option<String>,
}

struct ManagedProcess {
    child: Child,
    pid: u32,
    state: String,
    last_exit: Option<String>,
}

#[derive(Clone)]
struct AppState {
    configs: Arc<HashMap<String, ProviderConfig>>,
    processes: Arc<Mutex<HashMap<String, ManagedProcess>>>,
    reports: Arc<Mutex<HashMap<String, ProviderReport>>>,
    capability_bindings: Arc<Mutex<HashMap<String, CapabilityBindingRecord>>>,
    motion_inhibits: Arc<Mutex<HashMap<String, MotionInhibitLease>>>,
    control_authority_leases: Arc<Mutex<HashMap<String, ControlAuthorityLease>>>,
    control_authority_generations: Arc<Mutex<HashMap<String, u64>>>,
    shutdown_plan: Arc<Mutex<Option<ShutdownPlanRecord>>>,
    shutdown_execution: Arc<Mutex<Option<ShutdownExecutionRecord>>>,
    shutdown_fence: Arc<Mutex<Option<String>>>,
    workcell_calibrations: Arc<Mutex<HashMap<String, WorkcellCalibrationActivationRecord>>>,
    shutdown_execution_enabled: bool,
    review_auth_secret: Arc<Vec<u8>>,
    manager_instance_id: String,
    manager_boot_id: String,
    http: reqwest::Client,
    fabric_url: String,
    agent_ui_url: String,
    workspace_root: PathBuf,
    provider_autostart_enabled: bool,
    provider_manifests: Arc<HashMap<String, ui::ManifestRecord>>,
    skill_manifests: Arc<HashMap<String, ui::ManifestRecord>>,
}

#[tokio::main]
async fn main() -> Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "resource_provider_manager=info,tower_http=info".into()),
        )
        .init();

    let config_path = env::args()
        .nth(1)
        .or_else(|| env::var("PROVIDER_CONFIG").ok())
        .unwrap_or_else(|| "config/providers.json".to_string());
    let bind = env::var("MANAGER_BIND").unwrap_or_else(|_| "127.0.0.1:7001".to_string());
    let fabric_url = env::var("FABRIC_URL").unwrap_or_else(|_| "http://127.0.0.1:7002".to_string());
    let agent_ui_url =
        env::var("AGENT_UI_URL").unwrap_or_else(|_| "http://127.0.0.1:8000".to_string());
    let provider_autostart_enabled = env::var("MANAGER_PROVIDER_AUTOSTART_ENABLED")
        .ok()
        .is_some_and(|value| value.eq_ignore_ascii_case("true") || value == "1");
    let shutdown_execution_enabled = env::var("MANAGER_SHUTDOWN_EXECUTION_ENABLED")
        .ok()
        .is_some_and(|value| value.eq_ignore_ascii_case("true") || value == "1");
    let review_auth_secret = env::var("MIDBRAIN_REVIEW_AUTH_SECRET")
        .unwrap_or_default()
        .into_bytes();

    let config_text = tokio::fs::read_to_string(&config_path)
        .await
        .with_context(|| format!("reading provider config {config_path}"))?;
    let provider_file: ProviderFile = serde_json::from_str(&config_text)
        .with_context(|| format!("parsing provider config {config_path}"))?;
    let configs: HashMap<String, ProviderConfig> = provider_file
        .providers
        .into_iter()
        .map(|provider| (provider.id.clone(), provider))
        .collect();
    let config_path = std::fs::canonicalize(&config_path)
        .with_context(|| format!("resolving provider config {config_path}"))?;
    let workspace_root = config_path
        .parent()
        .and_then(FsPath::parent)
        .ok_or_else(|| anyhow!("provider config must be inside a workspace config directory"))?;
    let manifest_catalog = ui::load_manifest_catalog(workspace_root)?;

    let state = AppState {
        configs: Arc::new(configs),
        processes: Arc::new(Mutex::new(HashMap::new())),
        reports: Arc::new(Mutex::new(HashMap::new())),
        capability_bindings: Arc::new(Mutex::new(HashMap::new())),
        motion_inhibits: Arc::new(Mutex::new(HashMap::new())),
        control_authority_leases: Arc::new(Mutex::new(HashMap::new())),
        control_authority_generations: Arc::new(Mutex::new(HashMap::new())),
        shutdown_plan: Arc::new(Mutex::new(None)),
        shutdown_execution: Arc::new(Mutex::new(None)),
        shutdown_fence: Arc::new(Mutex::new(None)),
        workcell_calibrations: Arc::new(Mutex::new(HashMap::new())),
        shutdown_execution_enabled,
        review_auth_secret: Arc::new(review_auth_secret),
        manager_instance_id: Uuid::new_v4().to_string(),
        manager_boot_id: Uuid::new_v4().to_string(),
        http: reqwest::Client::new(),
        fabric_url,
        agent_ui_url,
        workspace_root: workspace_root.to_path_buf(),
        provider_autostart_enabled,
        provider_manifests: Arc::new(manifest_catalog.providers),
        skill_manifests: Arc::new(manifest_catalog.skills),
    };

    let app = Router::new()
        .route("/", get(ui::mainframe))
        .route("/assets/manager.css", get(ui::manager_css))
        .route("/assets/mainframe.js", get(ui::mainframe_js))
        .route("/assets/component.js", get(ui::component_js))
        .route(
            "/assets/developer-confirm.js",
            get(ui::developer_confirm_js),
        )
        .route("/assets/shutdown.js", get(ui::shutdown_js))
        .route("/observe/provider/:id", get(ui::component_page))
        .route("/observe/skill/:id", get(ui::component_page))
        .route("/developer/provider/:id", get(ui::developer_page))
        .route("/developer/skill/:id", get(ui::developer_page))
        .route("/shutdown", get(ui::shutdown_page))
        .route("/v1/ui/overview", get(ui::overview))
        .route("/v1/ui/providers/:id", get(ui::provider_detail))
        .route("/v1/ui/skills/:id", get(ui::skill_detail))
        .route(
            "/v1/ui/developer/:kind/:id/activate",
            post(ui::activate_developer_surface),
        )
        .route("/v1/ui/shutdown", post(ui::shutdown_midbrain))
        .route("/health", get(health))
        .route("/v1/providers", get(list_providers))
        .route("/v1/capabilities", get(list_capabilities))
        .route("/v1/capability-bindings", post(create_capability_binding))
        .route("/v1/capability-bindings/:id", get(get_capability_binding))
        .route("/v1/providers/register", post(register_provider))
        .route("/v1/providers/heartbeat", post(provider_heartbeat))
        .route("/v1/providers/:id/start", post(start_provider))
        .route("/v1/providers/:id/warm", post(warm_provider))
        .route("/v1/providers/:id/hot", post(hot_provider))
        .route("/v1/providers/:id/stop", post(stop_provider))
        .route("/v1/providers/:id/kill", post(kill_provider))
        .route("/v1/providers/:id/request", post(provider_request))
        .route("/v1/motion/inhibit", get(motion_inhibit_status))
        .route("/v1/motion/inhibit/acquire", post(acquire_motion_inhibit))
        .route("/v1/motion/inhibit/release", post(release_motion_inhibit))
        .route(
            "/v1/control-authority/leases",
            post(acquire_control_authority),
        )
        .route(
            "/v1/control-authority/leases/:id/renew",
            post(renew_control_authority),
        )
        .route(
            "/v1/control-authority/leases/:id/release",
            post(release_control_authority),
        )
        .route(
            "/v1/control-authority/resources/:id",
            get(get_control_authority_resource),
        )
        .route("/v1/shutdown", get(get_shutdown_plan))
        .route("/v1/shutdown/plan", post(create_shutdown_plan))
        .route("/v1/shutdown/:id/execute", post(execute_shutdown_plan))
        .route("/v1/shutdown/executions/:id", get(get_shutdown_execution))
        .route("/v1/workcell-calibrations", get(list_workcell_calibrations))
        .route(
            "/v1/workcell-calibrations/activate",
            post(activate_workcell_calibration),
        )
        .route(
            "/v1/workcell-calibrations/:id/revoke",
            post(revoke_workcell_calibration),
        )
        .layer(CorsLayer::permissive())
        .layer(TraceLayer::new_for_http())
        .with_state(state.clone());

    let monitor_state = state.clone();
    tokio::spawn(async move {
        heartbeat_expiry_loop(monitor_state).await;
    });

    let listener = tokio::net::TcpListener::bind(&bind).await?;
    info!(%bind, "Resource Provider Manager listening");

    if state.provider_autostart_enabled {
        let auto_state = state.clone();
        tokio::spawn(async move {
            let adoption_grace_ms = auto_state
                .configs
                .values()
                .filter(|provider| provider.auto_start)
                .map(|provider| provider.heartbeat_timeout_ms)
                .max()
                .unwrap_or_else(default_heartbeat_timeout)
                .clamp(1_000, 10_000);
            sleep(Duration::from_millis(adoption_grace_ms)).await;
            for provider in auto_state.configs.values().filter(|p| p.auto_start) {
                if let Err(err) = start_provider_inner(&auto_state, &provider.id).await {
                    error!(provider_id = %provider.id, error = %err, "auto-start failed");
                }
            }
        });
    } else {
        info!("Provider auto-start is disabled; Manager is starting in observation-first mode");
    }

    axum::serve(listener, app).await?;
    Ok(())
}

async fn health(State(state): State<AppState>) -> Json<Value> {
    Json(json!({
        "status": "ok",
        "service": "resource-provider-manager",
        "instance_id": state.manager_instance_id,
        "boot_id": state.manager_boot_id,
        "provider_autostart_enabled": state.provider_autostart_enabled,
        "shutdown_execution_enabled": state.shutdown_execution_enabled,
        "workcell_calibration_activation_identity_configured":
            state.review_auth_secret.len() >= 32,
        "features": [
            "capability_catalog",
            "advisory_capability_binding",
            "advisory_control_authority",
            "manager_owned_shutdown_shadow",
            "manager_owned_shutdown_execution_gated",
            "heartbeat_expiry",
            "provider_requests",
            "motion_inhibit",
            "reviewed_workcell_calibration_activation",
            "workcell_calibration_revocation"
        ]
    }))
}

async fn list_workcell_calibrations(State(state): State<AppState>) -> Json<Value> {
    let now = Utc::now();
    let mut records = state.workcell_calibrations.lock().await;
    for record in records.values_mut() {
        if record.state == "ACTIVE" && record.expires_at <= now {
            record.state = "EXPIRED".to_string();
            record.motion_usable = false;
            record.last_transition_reason = "activation lifetime expired".to_string();
        }
    }
    let mut values: Vec<WorkcellCalibrationActivationRecord> = records.values().cloned().collect();
    values.sort_by(|left, right| {
        right
            .activated_at
            .cmp(&left.activated_at)
            .then_with(|| left.activation_id.cmp(&right.activation_id))
    });
    Json(json!({
        "enforcement": "ENFORCED",
        "identity_verification_configured": state.review_auth_secret.len() >= 32,
        "activations": values,
    }))
}

async fn activate_workcell_calibration(
    State(state): State<AppState>,
    Json(request): Json<WorkcellCalibrationActivationRequest>,
) -> Result<(StatusCode, Json<WorkcellCalibrationActivationRecord>), (StatusCode, Json<Value>)> {
    if state.review_auth_secret.len() < 32 {
        return Err(api_error(
            StatusCode::SERVICE_UNAVAILABLE,
            "workcell calibration activation requires MIDBRAIN_REVIEW_AUTH_SECRET",
        ));
    }
    let request_sha256 = canonical_json_sha256(
        &serde_json::to_value(&request)
            .map_err(|error| api_error(StatusCode::BAD_REQUEST, error.to_string()))?,
    );
    {
        let records = state.workcell_calibrations.lock().await;
        if let Some(existing) = records
            .values()
            .find(|record| record.request_id == request.request_id)
        {
            if existing.request_sha256 != request_sha256 {
                return Err(api_error(
                    StatusCode::CONFLICT,
                    "activation request_id was already used for different content",
                ));
            }
            return Ok((StatusCode::OK, Json(existing.clone())));
        }
    }

    let reports = state.reports.lock().await.clone();
    let record = build_workcell_activation_record(
        &request,
        request_sha256,
        &reports,
        &state.review_auth_secret,
        Utc::now(),
    )
    .map_err(|error| api_error(StatusCode::CONFLICT, error.to_string()))?;
    publish_workcell_calibration(&state, &record, true)
        .await
        .map_err(|error| {
            api_error(
                StatusCode::BAD_GATEWAY,
                format!("workcell calibration was not activated because Fabric publication failed: {error}"),
            )
        })?;
    let mut records = state.workcell_calibrations.lock().await;
    supersede_active_workcell_calibrations(&mut records, &record, Utc::now());
    records.insert(record.activation_id.clone(), record.clone());
    Ok((StatusCode::CREATED, Json(record)))
}

fn supersede_active_workcell_calibrations(
    records: &mut HashMap<String, WorkcellCalibrationActivationRecord>,
    replacement: &WorkcellCalibrationActivationRecord,
    now: DateTime<Utc>,
) {
    for record in records.values_mut() {
        if record.state != "ACTIVE" {
            continue;
        }
        record.motion_usable = false;
        if record.expires_at <= now {
            record.state = "EXPIRED".to_string();
            record.last_transition_reason = "activation lifetime expired".to_string();
        } else {
            record.state = "SUPERSEDED".to_string();
            record.last_transition_reason = format!(
                "superseded by newer reviewed activation {}",
                replacement.activation_id
            );
        }
    }
}

async fn revoke_workcell_calibration(
    State(state): State<AppState>,
    Path(activation_id): Path<String>,
    Json(request): Json<WorkcellCalibrationRevocationRequest>,
) -> Result<Json<WorkcellCalibrationActivationRecord>, (StatusCode, Json<Value>)> {
    if request.request_id.trim().is_empty()
        || request.revoked_by.trim().is_empty()
        || request.reason.trim().is_empty()
    {
        return Err(api_error(
            StatusCode::BAD_REQUEST,
            "request_id, revoked_by, and reason are required",
        ));
    }
    let mut record = {
        let records = state.workcell_calibrations.lock().await;
        records.get(&activation_id).cloned().ok_or_else(|| {
            api_error(
                StatusCode::NOT_FOUND,
                "workcell calibration activation does not exist",
            )
        })?
    };
    if record.state == "REVOKED" {
        return Ok(Json(record));
    }
    record.state = "REVOKED".to_string();
    record.motion_usable = false;
    record.last_transition_reason = format!(
        "{} revoked activation: {}",
        request.revoked_by.trim(),
        request.reason.trim()
    );
    publish_workcell_calibration(&state, &record, false)
        .await
        .map_err(|error| {
            api_error(
                StatusCode::BAD_GATEWAY,
                format!("workcell calibration revocation was not published: {error}"),
            )
        })?;
    state
        .workcell_calibrations
        .lock()
        .await
        .insert(activation_id, record.clone());
    Ok(Json(record))
}

fn build_workcell_activation_record(
    request: &WorkcellCalibrationActivationRequest,
    request_sha256: String,
    reports: &HashMap<String, ProviderReport>,
    review_auth_secret: &[u8],
    now: DateTime<Utc>,
) -> Result<WorkcellCalibrationActivationRecord> {
    if request.request_id.trim().is_empty() || request.activated_by.trim().is_empty() {
        return Err(anyhow!("request_id and activated_by are required"));
    }
    if !(1_000..=300_000).contains(&request.duration_ms) {
        return Err(anyhow!("duration_ms must be between 1000 and 300000"));
    }
    let candidate = request
        .candidate
        .as_object()
        .ok_or_else(|| anyhow!("candidate must be an object"))?;
    require_json_string(&request.candidate, "schema", "candidate")?;
    if request.candidate["schema"]
        != "midbrain.skill.stationary_world_arm_alignment.calibration_candidate"
        || request.candidate["schema_version"] != 3
        || request.candidate["review_state"] != "CANDIDATE_REVIEW_REQUIRED"
        || request.candidate["motion_usable"] != false
    {
        return Err(anyhow!(
            "candidate must be an immutable version-3 review-required, non-motion-usable calibration"
        ));
    }
    let candidate_id = require_json_string(&request.candidate, "candidate_id", "candidate")?;
    let calibration_revision = require_json_string(
        &request.candidate,
        "workcell_calibration_revision",
        "candidate",
    )?;
    if candidate_id != calibration_revision {
        return Err(anyhow!(
            "candidate_id and workcell_calibration_revision must match"
        ));
    }
    let candidate_expires_at_us = request.candidate["expires_at_us"]
        .as_u64()
        .ok_or_else(|| anyhow!("candidate.expires_at_us must be a positive integer"))?;
    let now_us = now.timestamp_micros().max(0) as u64;
    if candidate_expires_at_us <= now_us {
        return Err(anyhow!("calibration candidate has expired"));
    }
    let semantic_alignment = &request.candidate["quality_provenance"]["semantic_alignment"];
    let semantic_status = semantic_alignment["status"]
        .as_str()
        .ok_or_else(|| anyhow!("candidate semantic alignment status is required"))?;
    let base_x_relation_to_gripper = semantic_alignment["base_x_relation_to_gripper"]
        .as_str()
        .ok_or_else(|| anyhow!("candidate base-X relation to the gripper is required"))?;
    let selected_base_yaw_flip_deg = semantic_alignment["selected_base_yaw_flip_deg"]
        .as_i64()
        .ok_or_else(|| anyhow!("candidate selected base-yaw flip is required"))?;
    let fitted_base_yaw_deg = semantic_alignment["fitted_base_yaw_deg"]
        .as_f64()
        .ok_or_else(|| anyhow!("candidate fitted base yaw is required"))?;
    let yaw_correction_translation_norm_m = semantic_alignment["yaw_correction_translation_norm_m"]
        .as_f64()
        .ok_or_else(|| anyhow!("candidate yaw-correction translation norm is required"))?;
    let world_up_available = semantic_alignment["world_up_available"]
        .as_bool()
        .ok_or_else(|| anyhow!("candidate world-up availability is required"))?;
    let raw_base_z_dot_world_up = semantic_alignment["raw_base_z_dot_world_up"]
        .as_f64()
        .ok_or_else(|| anyhow!("candidate raw base-Z/world-up dot is required"))?;
    let corrected_base_z_dot_world_up = semantic_alignment["corrected_base_z_dot_world_up"]
        .as_f64()
        .ok_or_else(|| anyhow!("candidate corrected base-Z/world-up dot is required"))?;
    let upright_hemisphere_flip_required = semantic_alignment["upright_hemisphere_flip_required"]
        .as_bool()
        .ok_or_else(|| anyhow!("candidate upright-hemisphere decision is required"))?;
    let selected_orientation_correction_axis = semantic_alignment
        ["selected_orientation_correction_axis"]
        .as_str()
        .ok_or_else(|| anyhow!("candidate orientation-correction axis is required"))?;
    let selected_orientation_correction_deg = semantic_alignment
        ["selected_orientation_correction_deg"]
        .as_i64()
        .ok_or_else(|| anyhow!("candidate orientation-correction angle is required"))?;
    let orientation_correction_count = semantic_alignment["orientation_correction_count"]
        .as_u64()
        .ok_or_else(|| anyhow!("candidate orientation-correction count is required"))?;
    let orientation_correction_translation_norm_m = semantic_alignment
        ["orientation_correction_translation_norm_m"]
        .as_f64()
        .ok_or_else(|| anyhow!("candidate orientation-correction translation is required"))?;
    let orientation_application_origin = semantic_alignment["orientation_application_origin"]
        .as_str()
        .ok_or_else(|| anyhow!("candidate orientation-application origin is required"))?;
    let orientation_application_order = semantic_alignment["orientation_application_order"]
        .as_str()
        .ok_or_else(|| anyhow!("candidate orientation-application order is required"))?;
    let mesh_hypothesis_correction_translation_norm_m = semantic_alignment
        ["mesh_hypothesis_correction_translation_norm_m"]
        .as_f64()
        .ok_or_else(|| anyhow!("candidate mesh-hypothesis translation norm is required"))?;
    let mesh_center_translation_preserved = semantic_alignment["mesh_center_translation_preserved"]
        .as_bool()
        .ok_or_else(|| anyhow!("candidate mesh-center preservation result is required"))?;
    let semantic_root_translation_adjustment_norm_m = semantic_alignment
        ["semantic_root_translation_adjustment_norm_m"]
        .as_f64()
        .ok_or_else(|| anyhow!("candidate semantic-root adjustment norm is required"))?;
    let expected_yaw_flip_deg = match base_x_relation_to_gripper {
        "TOWARD_GRIPPER" | "UNCLEAR" => Some(0),
        "AWAY_FROM_GRIPPER" => Some(180),
        _ => None,
    };
    let expected_upright_flip = raw_base_z_dot_world_up < 0.0;
    let expected_x_flip = base_x_relation_to_gripper == "AWAY_FROM_GRIPPER";
    let expected_orientation_axis = match (expected_upright_flip, expected_x_flip) {
        (false, false) => "NONE",
        (false, true) => "Z",
        (true, false) => "X",
        (true, true) => "Y",
    };
    let expected_orientation_count = if expected_orientation_axis == "NONE" {
        0
    } else {
        1
    };
    let expected_orientation_deg = if expected_orientation_count == 0 {
        0
    } else {
        180
    };
    if !matches!(semantic_status, "PASSED" | "PASSED_WITH_WARNINGS")
        || expected_yaw_flip_deg != Some(selected_base_yaw_flip_deg)
        || (fitted_base_yaw_deg - selected_base_yaw_flip_deg as f64).abs() > 1e-9
        || !fitted_base_yaw_deg.is_finite()
        || yaw_correction_translation_norm_m.abs() > 1e-9
        || !yaw_correction_translation_norm_m.is_finite()
        || !world_up_available
        || !raw_base_z_dot_world_up.is_finite()
        || !corrected_base_z_dot_world_up.is_finite()
        || corrected_base_z_dot_world_up < -1e-9
        || upright_hemisphere_flip_required != expected_upright_flip
        || selected_orientation_correction_axis != expected_orientation_axis
        || selected_orientation_correction_deg != expected_orientation_deg
        || orientation_correction_count != expected_orientation_count
        || !orientation_correction_translation_norm_m.is_finite()
        || orientation_correction_translation_norm_m.abs() > 1e-9
        || orientation_application_origin != "FOUNDATIONPOSE_CENTERED_CAD_MESH_ORIGIN"
        || orientation_application_order
            != "parent_from_mesh @ mesh_hypothesis_correction @ mesh_from_semantic"
        || !mesh_hypothesis_correction_translation_norm_m.is_finite()
        || mesh_hypothesis_correction_translation_norm_m.abs() > 1e-9
        || !mesh_center_translation_preserved
        || !semantic_root_translation_adjustment_norm_m.is_finite()
        || semantic_root_translation_adjustment_norm_m < 0.0
    {
        return Err(anyhow!(
            "candidate does not satisfy exact base-yaw review invariants or single base-orientation invariants"
        ));
    }

    let frame_contract = &request.candidate["frame_contract"];
    let world_frame =
        require_json_string(frame_contract, "world_frame", "candidate.frame_contract")?;
    let vio_world_frame = require_json_string(
        frame_contract,
        "vio_world_frame",
        "candidate.frame_contract",
    )?;
    let camera_frame =
        require_json_string(frame_contract, "camera_frame", "candidate.frame_contract")?;
    let arm_base_frame =
        require_json_string(frame_contract, "arm_base_frame", "candidate.frame_contract")?;
    let convention_id =
        require_json_string(frame_contract, "convention_id", "candidate.frame_contract")?;
    let camera_optical_convention_id = require_json_string(
        frame_contract,
        "camera_optical_convention_id",
        "candidate.frame_contract",
    )?;
    if convention_id != "MIDBRAIN_X_FORWARD_Y_LEFT_Z_UP_V2"
        || camera_optical_convention_id != "CAMERA_OPTICAL_X_RIGHT_Y_DOWN_Z_FORWARD_V1"
        || frame_contract["legacy_candidate_compatibility"] != "REJECT"
    {
        return Err(anyhow!(
            "candidate uses an unsupported or legacy spatial convention"
        ));
    }
    if frame_contract["transform_semantics"] != "PARENT_FROM_CHILD" {
        return Err(anyhow!(
            "candidate frame transform semantics must be PARENT_FROM_CHILD"
        ));
    }
    let vio = &request.candidate["vio_provenance"];
    let vio_provider_id = require_json_string(vio, "provider_id", "candidate.vio_provenance")?;
    let vio_provider_instance_id =
        require_json_string(vio, "provider_instance_id", "candidate.vio_provenance")?;
    let vio_boot_id = require_json_string(vio, "boot_id", "candidate.vio_provenance")?;
    let session_epoch = require_json_string(vio, "session_epoch", "candidate.vio_provenance")?;
    if require_json_string(vio, "world_frame", "candidate.vio_provenance")? != vio_world_frame {
        return Err(anyhow!("candidate VIO frame contract is inconsistent"));
    }
    validate_transform_payload(
        &request.candidate["transforms"]["world_from_camera"],
        "candidate.transforms.world_from_camera",
    )?;
    validate_transform_payload(
        &request.candidate["transforms"]["world_from_vio"],
        "candidate.transforms.world_from_vio",
    )?;
    validate_transform_payload(
        &request.candidate["transforms"]["world_from_base"],
        "candidate.transforms.world_from_base",
    )?;
    let documented_base_z_dot_world_up = transform_z_dot_parent_z(
        &request.candidate["transforms"]["world_from_base"],
        "candidate.transforms.world_from_base",
    )?;
    if documented_base_z_dot_world_up < -1e-9
        || (documented_base_z_dot_world_up - corrected_base_z_dot_world_up).abs() > 1e-6
    {
        return Err(anyhow!(
            "documented world_from_base +Z does not match the reviewed world-up orientation"
        ));
    }

    let candidate_sha256 = canonical_json_sha256(&request.candidate);
    let identity = verify_review_identity_assertion(
        &request.review_identity_assertion,
        review_auth_secret,
        &candidate_id,
        &candidate_sha256,
        now_us,
    )?;
    let decision = request
        .review_decision
        .as_object()
        .ok_or_else(|| anyhow!("review_decision must be an object"))?;
    if request.review_decision["schema"]
        != "midbrain.skill.stationary_world_arm_alignment.candidate_review_decision"
        || request.review_decision["decision"] != "APPROVE"
        || request.review_decision["decision_state"] != "APPROVED_FOR_ACTIVATION"
        || request.review_decision["activation_state"] != "NOT_ACTIVATED"
        || request.review_decision["motion_usable"] != false
        || request.review_decision["candidate_id"] != candidate_id
        || request.review_decision["candidate_sha256"] != candidate_sha256
    {
        return Err(anyhow!(
            "review decision is not an exact approval for this candidate"
        ));
    }
    let review_decision_id =
        require_json_string(&request.review_decision, "decision_id", "review_decision")?;
    let reviewer = &request.review_decision["reviewer"];
    for field in ["issuer", "reviewer_id", "assertion_nonce"] {
        if reviewer[field] != identity[field] {
            return Err(anyhow!(
                "review decision identity does not match the signed assertion"
            ));
        }
    }
    if decision.is_empty() || candidate.is_empty() {
        return Err(anyhow!("candidate and review decision cannot be empty"));
    }

    let camera = &request.candidate["camera_provenance"];
    let camera_provider_id =
        require_json_string(camera, "provider_id", "candidate.camera_provenance")?;
    let camera_provider_instance_id = require_json_string(
        camera,
        "provider_instance_id",
        "candidate.camera_provenance",
    )?;
    let camera_boot_id = require_json_string(camera, "boot_id", "candidate.camera_provenance")?;
    let camera_calibration_revision = require_json_string(
        camera,
        "calibration_revision",
        "candidate.camera_provenance",
    )?;
    let camera_report = reports
        .get(&camera_provider_id)
        .ok_or_else(|| anyhow!("candidate camera provider has no current Manager report"))?;
    if camera_report.expired
        || !camera_report.ready
        || camera_report.health != "HEALTHY"
        || camera_report.instance_id != camera_provider_instance_id
        || camera_report.boot_id != camera_boot_id
    {
        return Err(anyhow!(
            "candidate camera provider identity or health is no longer current"
        ));
    }
    let current_camera_calibration_revision = require_json_string(
        &camera_report.details,
        "calibration_revision",
        "current camera provider report details",
    )?;
    if current_camera_calibration_revision != camera_calibration_revision {
        return Err(anyhow!(
            "current camera calibration provenance does not match the candidate"
        ));
    }
    let vio_report = reports
        .get(&vio_provider_id)
        .ok_or_else(|| anyhow!("candidate VIO provider has no current Manager report"))?;
    if vio_report.expired
        || !vio_report.ready
        || vio_report.health != "HEALTHY"
        || vio_report.residency != "HOT"
        || vio_report.instance_id != vio_provider_instance_id
        || vio_report.boot_id != vio_boot_id
    {
        return Err(anyhow!(
            "candidate VIO provider identity, health, or readiness is no longer current"
        ));
    }
    if vio_report.details["session_epoch"] != session_epoch
        || vio_report.details["world_frame"] != vio_world_frame
        || vio_report.details["convention_id"] != convention_id
        || vio_report.details["tracking_state"] != "TRACKING"
    {
        return Err(anyhow!(
            "current VIO epoch, frame, convention, or tracking state does not match the candidate"
        ));
    }

    let requested_expires_at_us = now_us.saturating_add(request.duration_ms.saturating_mul(1000));
    let expires_at_us = candidate_expires_at_us.min(requested_expires_at_us);
    let expires_at = DateTime::<Utc>::from_timestamp_micros(expires_at_us as i64)
        .ok_or_else(|| anyhow!("activation expiration is outside the supported time range"))?;
    Ok(WorkcellCalibrationActivationRecord {
        activation_id: Uuid::new_v4().to_string(),
        request_id: request.request_id.trim().to_string(),
        request_sha256,
        candidate_id,
        candidate_sha256,
        calibration_revision,
        review_decision_id,
        activated_by: request.activated_by.trim().to_string(),
        activated_at: now,
        expires_at,
        expires_at_us,
        state: "ACTIVE".to_string(),
        enforcement: "ENFORCED".to_string(),
        motion_usable: true,
        session_epoch,
        world_frame,
        vio_world_frame,
        camera_frame,
        arm_base_frame,
        convention_id,
        camera_optical_convention_id,
        camera_provider_id,
        camera_provider_instance_id,
        camera_boot_id,
        camera_calibration_revision,
        vio_provider_id,
        vio_provider_instance_id,
        vio_boot_id,
        transforms: request.candidate["transforms"].clone(),
        reviewer: reviewer.clone(),
        last_transition_reason: "reviewed calibration activated".to_string(),
    })
}

fn canonical_json_sha256(value: &Value) -> String {
    let encoded = serde_json::to_vec(&canonical_typed_tree(value))
        .expect("serializing canonical typed JSON tree cannot fail");
    hex_lower(&sha256(&encoded))
}

fn canonical_typed_tree(value: &Value) -> Value {
    match value {
        Value::Null => json!(["null"]),
        Value::Bool(flag) => json!(["boolean", if *flag { "1" } else { "0" }]),
        Value::Number(number) => {
            if let Some(value) = number.as_i64() {
                json!(["integer", value.to_string()])
            } else if let Some(value) = number.as_u64() {
                json!(["integer", value.to_string()])
            } else {
                json!(["decimal", normalized_canonical_float(number)])
            }
        }
        Value::String(text) => json!(["utf8", hex_lower(text.as_bytes())]),
        Value::Array(items) => json!([
            "array",
            items.iter().map(canonical_typed_tree).collect::<Vec<_>>()
        ]),
        Value::Object(items) => {
            let mut entries = items
                .iter()
                .map(|(key, value)| json!([hex_lower(key.as_bytes()), canonical_typed_tree(value)]))
                .collect::<Vec<_>>();
            entries.sort_by(|left, right| {
                left[0]
                    .as_str()
                    .expect("canonical object key must be a string")
                    .cmp(
                        right[0]
                            .as_str()
                            .expect("canonical object key must be a string"),
                    )
            });
            json!(["object", entries])
        }
    }
}

fn normalized_canonical_float(number: &serde_json::Number) -> String {
    let token = number.to_string().to_ascii_lowercase();
    let (sign, unsigned) = if let Some(value) = token.strip_prefix('-') {
        ("-", value)
    } else {
        ("", token.as_str())
    };
    let (mantissa, exponent_text) = unsigned
        .split_once('e')
        .map_or((unsigned, None), |(mantissa, exponent)| {
            (mantissa, Some(exponent))
        });
    let mut exponent = exponent_text
        .map(str::parse::<i64>)
        .transpose()
        .expect("JSON exponent must be an integer")
        .unwrap_or(0);
    let (whole, fraction) = mantissa
        .split_once('.')
        .map_or((mantissa, ""), |(whole, fraction)| (whole, fraction));
    exponent -= fraction.len() as i64;
    let combined = format!("{whole}{fraction}");
    let significant = combined.trim_start_matches('0');
    if significant.is_empty() {
        return "0e+0".to_string();
    }
    let digits = significant.trim_end_matches('0');
    exponent += (significant.len() - digits.len()) as i64;
    format!("{sign}{digits}e{exponent:+}")
}

fn require_json_string(value: &Value, field: &str, scope: &str) -> Result<String> {
    let result = value[field]
        .as_str()
        .map(str::trim)
        .filter(|text| !text.is_empty())
        .ok_or_else(|| anyhow!("{scope}.{field} must be a non-empty string"))?;
    Ok(result.to_string())
}

fn validate_transform_payload(value: &Value, scope: &str) -> Result<()> {
    let translation = value["translation_m"]
        .as_array()
        .ok_or_else(|| anyhow!("{scope}.translation_m must be an array"))?;
    let rotation = value["rotation_xyzw"]
        .as_array()
        .ok_or_else(|| anyhow!("{scope}.rotation_xyzw must be an array"))?;
    if translation.len() != 3 || rotation.len() != 4 {
        return Err(anyhow!(
            "{scope} must contain a 3-value translation and 4-value quaternion"
        ));
    }
    let translation_values: Vec<f64> = translation
        .iter()
        .map(|item| {
            item.as_f64()
                .ok_or_else(|| anyhow!("{scope} contains a non-number"))
        })
        .collect::<Result<_>>()?;
    let rotation_values: Vec<f64> = rotation
        .iter()
        .map(|item| {
            item.as_f64()
                .ok_or_else(|| anyhow!("{scope} contains a non-number"))
        })
        .collect::<Result<_>>()?;
    if !translation_values.iter().all(|value| value.is_finite())
        || !rotation_values.iter().all(|value| value.is_finite())
    {
        return Err(anyhow!("{scope} contains a non-finite number"));
    }
    let norm = rotation_values
        .iter()
        .map(|value| value * value)
        .sum::<f64>()
        .sqrt();
    if !(0.99..=1.01).contains(&norm) {
        return Err(anyhow!("{scope} quaternion is not normalized"));
    }
    Ok(())
}

fn transform_z_dot_parent_z(value: &Value, scope: &str) -> Result<f64> {
    let rotation = value["rotation_xyzw"]
        .as_array()
        .ok_or_else(|| anyhow!("{scope}.rotation_xyzw must be an array"))?;
    if rotation.len() != 4 {
        return Err(anyhow!("{scope}.rotation_xyzw must contain four values"));
    }
    let quaternion = rotation
        .iter()
        .map(|item| {
            item.as_f64()
                .ok_or_else(|| anyhow!("{scope}.rotation_xyzw contains a non-number"))
        })
        .collect::<Result<Vec<_>>>()?;
    if !quaternion.iter().all(|item| item.is_finite()) {
        return Err(anyhow!(
            "{scope}.rotation_xyzw contains a non-finite number"
        ));
    }
    let norm_squared = quaternion.iter().map(|item| item * item).sum::<f64>();
    if norm_squared <= 1e-18 {
        return Err(anyhow!("{scope}.rotation_xyzw has zero norm"));
    }
    let x = quaternion[0];
    let y = quaternion[1];
    Ok(1.0 - 2.0 * (x * x + y * y) / norm_squared)
}

fn verify_review_identity_assertion(
    assertion: &str,
    secret: &[u8],
    candidate_id: &str,
    candidate_sha256: &str,
    now_us: u64,
) -> Result<Value> {
    let (payload_token, signature_token) = assertion
        .split_once('.')
        .ok_or_else(|| anyhow!("review identity assertion is malformed"))?;
    let payload_bytes =
        base64url_decode(payload_token).context("decoding review identity payload")?;
    let signature =
        base64url_decode(signature_token).context("decoding review identity signature")?;
    let expected_signature = hmac_sha256(secret, &payload_bytes);
    if signature.len() != expected_signature.len()
        || !constant_time_equal(&signature, &expected_signature)
    {
        return Err(anyhow!("review identity assertion signature is invalid"));
    }
    let payload: Value =
        serde_json::from_slice(&payload_bytes).context("parsing review identity assertion")?;
    if payload["candidate_id"] != candidate_id
        || payload["candidate_sha256"] != candidate_sha256
        || payload["decision"] != "APPROVE"
    {
        return Err(anyhow!(
            "review identity assertion does not match the activation candidate"
        ));
    }
    let issued_at_us = payload["issued_at_us"]
        .as_u64()
        .ok_or_else(|| anyhow!("review identity issued_at_us is missing"))?;
    let expires_at_us = payload["expires_at_us"]
        .as_u64()
        .ok_or_else(|| anyhow!("review identity expires_at_us is missing"))?;
    if issued_at_us > now_us.saturating_add(5_000_000) || expires_at_us <= now_us {
        return Err(anyhow!(
            "review identity assertion is expired or issued in the future"
        ));
    }
    for field in ["issuer", "reviewer_id", "nonce"] {
        require_json_string(&payload, field, "review_identity")?;
    }
    Ok(json!({
        "issuer": payload["issuer"],
        "reviewer_id": payload["reviewer_id"],
        "assertion_nonce": payload["nonce"],
    }))
}

fn hex_lower(bytes: &[u8]) -> String {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut output = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        output.push(HEX[(byte >> 4) as usize] as char);
        output.push(HEX[(byte & 0x0f) as usize] as char);
    }
    output
}

fn constant_time_equal(left: &[u8], right: &[u8]) -> bool {
    if left.len() != right.len() {
        return false;
    }
    let mut difference = 0u8;
    for (left_byte, right_byte) in left.iter().zip(right) {
        difference |= left_byte ^ right_byte;
    }
    difference == 0
}

fn base64url_decode(value: &str) -> Result<Vec<u8>> {
    let mut output = Vec::with_capacity(value.len() * 3 / 4);
    let mut accumulator = 0u32;
    let mut bits = 0u8;
    for byte in value.bytes() {
        if byte == b'=' {
            break;
        }
        let decoded = match byte {
            b'A'..=b'Z' => byte - b'A',
            b'a'..=b'z' => byte - b'a' + 26,
            b'0'..=b'9' => byte - b'0' + 52,
            b'-' => 62,
            b'_' => 63,
            _ => return Err(anyhow!("invalid base64url character")),
        };
        accumulator = (accumulator << 6) | u32::from(decoded);
        bits += 6;
        if bits >= 8 {
            bits -= 8;
            output.push(((accumulator >> bits) & 0xff) as u8);
        }
    }
    if bits > 0 && (accumulator & ((1u32 << bits) - 1)) != 0 {
        return Err(anyhow!("non-zero base64url padding bits"));
    }
    Ok(output)
}

fn hmac_sha256(key: &[u8], message: &[u8]) -> [u8; 32] {
    let mut normalized_key = [0u8; 64];
    if key.len() > normalized_key.len() {
        normalized_key[..32].copy_from_slice(&sha256(key));
    } else {
        normalized_key[..key.len()].copy_from_slice(key);
    }
    let mut inner = Vec::with_capacity(64 + message.len());
    let mut outer = Vec::with_capacity(64 + 32);
    for byte in normalized_key {
        inner.push(byte ^ 0x36);
        outer.push(byte ^ 0x5c);
    }
    inner.extend_from_slice(message);
    outer.extend_from_slice(&sha256(&inner));
    sha256(&outer)
}

fn sha256(message: &[u8]) -> [u8; 32] {
    const INITIAL: [u32; 8] = [
        0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab,
        0x5be0cd19,
    ];
    const ROUND: [u32; 64] = [
        0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4,
        0xab1c5ed5, 0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe,
        0x9bdc06a7, 0xc19bf174, 0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f,
        0x4a7484aa, 0x5cb0a9dc, 0x76f988da, 0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7,
        0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967, 0x27b70a85, 0x2e1b2138, 0x4d2c6dfc,
        0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85, 0xa2bfe8a1, 0xa81a664b,
        0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070, 0x19a4c116,
        0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
        0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7,
        0xc67178f2,
    ];
    let bit_length = (message.len() as u64).wrapping_mul(8);
    let mut padded = Vec::with_capacity(message.len() + 72);
    padded.extend_from_slice(message);
    padded.push(0x80);
    while padded.len() % 64 != 56 {
        padded.push(0);
    }
    padded.extend_from_slice(&bit_length.to_be_bytes());

    let mut hash = INITIAL;
    for chunk in padded.chunks_exact(64) {
        let mut words = [0u32; 64];
        for (index, word) in words.iter_mut().take(16).enumerate() {
            let offset = index * 4;
            *word = u32::from_be_bytes([
                chunk[offset],
                chunk[offset + 1],
                chunk[offset + 2],
                chunk[offset + 3],
            ]);
        }
        for index in 16..64 {
            let s0 = words[index - 15].rotate_right(7)
                ^ words[index - 15].rotate_right(18)
                ^ (words[index - 15] >> 3);
            let s1 = words[index - 2].rotate_right(17)
                ^ words[index - 2].rotate_right(19)
                ^ (words[index - 2] >> 10);
            words[index] = words[index - 16]
                .wrapping_add(s0)
                .wrapping_add(words[index - 7])
                .wrapping_add(s1);
        }
        let mut a = hash[0];
        let mut b = hash[1];
        let mut c = hash[2];
        let mut d = hash[3];
        let mut e = hash[4];
        let mut f = hash[5];
        let mut g = hash[6];
        let mut h = hash[7];
        for index in 0..64 {
            let upper_e = e.rotate_right(6) ^ e.rotate_right(11) ^ e.rotate_right(25);
            let choose = (e & f) ^ ((!e) & g);
            let temporary_one = h
                .wrapping_add(upper_e)
                .wrapping_add(choose)
                .wrapping_add(ROUND[index])
                .wrapping_add(words[index]);
            let upper_a = a.rotate_right(2) ^ a.rotate_right(13) ^ a.rotate_right(22);
            let majority = (a & b) ^ (a & c) ^ (b & c);
            let temporary_two = upper_a.wrapping_add(majority);
            h = g;
            g = f;
            f = e;
            e = d.wrapping_add(temporary_one);
            d = c;
            c = b;
            b = a;
            a = temporary_one.wrapping_add(temporary_two);
        }
        for (target, value) in hash.iter_mut().zip([a, b, c, d, e, f, g, h]) {
            *target = target.wrapping_add(value);
        }
    }
    let mut output = [0u8; 32];
    for (index, word) in hash.iter().enumerate() {
        output[index * 4..index * 4 + 4].copy_from_slice(&word.to_be_bytes());
    }
    output
}

async fn publish_workcell_calibration(
    state: &AppState,
    record: &WorkcellCalibrationActivationRecord,
    active: bool,
) -> Result<()> {
    let observed_at_us = Utc::now().timestamp_micros().max(0) as u64;
    let review_state = if active { "ACCEPTED" } else { "REVOKED" };
    let activation_state = if active { "ACTIVE" } else { "REVOKED" };
    let motion_usable = active;
    let valid_until_us = if active {
        record.expires_at_us
    } else {
        observed_at_us.saturating_add(1_000_000)
    };
    let transform = |stream: &str, child_frame: &str, payload: &Value, offset: u64| {
        json!({
            "schema": "physical_agent.transform",
            "schema_version": 1,
            "stream": stream,
            "provider_id": "manager.workcell_calibration",
            "provider_instance_id": state.manager_instance_id,
            "boot_id": state.manager_boot_id,
            "sequence": observed_at_us.saturating_add(offset),
            "observed_at_us": observed_at_us,
            "coordinate_frame": record.world_frame,
            "calibration_revision": record.calibration_revision,
            "expires_at_us": valid_until_us,
            "related_skill_id": record.candidate_id,
            "valid": true,
            "data": {
                "parent_frame": record.world_frame,
                "child_frame": child_frame,
                "translation_m": payload["translation_m"],
                "rotation_xyzw": payload["rotation_xyzw"],
                "is_static": true,
                "authority": "manager.workcell_calibration_activation",
                "session_epoch": record.session_epoch,
                "calibration_revision": record.calibration_revision,
                "convention_id": record.convention_id,
                "camera_optical_convention_id": record.camera_optical_convention_id,
                "continuity": "REVIEWED_CALIBRATION_ACTIVATION",
                "review_state": review_state,
                "activation_state": activation_state,
                "activation_id": record.activation_id,
                "candidate_sha256": record.candidate_sha256,
                "review_decision_id": record.review_decision_id,
                "motion_usable": motion_usable,
                "expires_at_us": valid_until_us
            }
        })
    };
    let observations = vec![
        transform(
            "transform.stationary_world.camera",
            &record.camera_frame,
            &record.transforms["world_from_camera"],
            0,
        ),
        transform(
            "transform.stationary_world.vio",
            &record.vio_world_frame,
            &record.transforms["world_from_vio"],
            1,
        ),
        transform(
            "transform.stationary_world.arm_base",
            &record.arm_base_frame,
            &record.transforms["world_from_base"],
            2,
        ),
        json!({
            "schema": "physical_agent.workcell_calibration_activation",
            "schema_version": 1,
            "stream": "manager.workcell_calibration.activation",
            "provider_id": "manager.workcell_calibration",
            "provider_instance_id": state.manager_instance_id,
            "boot_id": state.manager_boot_id,
            "sequence": observed_at_us.saturating_add(3),
            "observed_at_us": observed_at_us,
            "coordinate_frame": record.world_frame,
            "calibration_revision": record.calibration_revision,
            "expires_at_us": valid_until_us,
            "related_skill_id": record.candidate_id,
            "valid": true,
            "data": record
        }),
    ];
    let response = state
        .http
        .post(format!(
            "{}/v1/observations/batch",
            state.fabric_url.trim_end_matches('/')
        ))
        .json(&json!({"observations": observations}))
        .timeout(Duration::from_secs(3))
        .send()
        .await?;
    if !response.status().is_success() {
        return Err(anyhow!("Fabric returned {}", response.status()));
    }
    Ok(())
}

async fn list_providers(State(state): State<AppState>) -> Json<Vec<ProviderView>> {
    Json(collect_provider_views(&state).await)
}

async fn collect_provider_views(state: &AppState) -> Vec<ProviderView> {
    let mut processes = state.processes.lock().await;
    let reports = state.reports.lock().await;
    let mut result = Vec::new();

    for config in state.configs.values() {
        let (process_state, pid, last_exit) = if let Some(process) = processes.get_mut(&config.id) {
            refresh_process_state(process);
            (
                process.state.clone(),
                Some(process.pid),
                process.last_exit.clone(),
            )
        } else {
            ("stopped".to_string(), None, None)
        };
        result.push(ProviderView {
            config: config.clone(),
            process_state,
            pid,
            last_exit,
            report: reports.get(&config.id).cloned(),
        });
    }
    result.sort_by(|a, b| a.config.id.cmp(&b.config.id));
    result
}

async fn list_capabilities(State(state): State<AppState>) -> Json<Vec<CapabilityView>> {
    let reports = state.reports.lock().await;
    let mut result = Vec::new();

    for config in state.configs.values() {
        let report = reports.get(&config.id);
        let capability_map = report
            .and_then(|value| value.details.get("capability_readiness"))
            .and_then(Value::as_object);

        if let Some(capabilities) = capability_map {
            for (capability, available_value) in capabilities {
                let available = available_value.as_bool().unwrap_or(false)
                    && report.is_some_and(|value| {
                        !value.expired && value.residency == "HOT" && value.health != "UNHEALTHY"
                    });
                let current = report.expect("report exists when capability map exists");
                result.push(CapabilityView {
                    capability: capability.clone(),
                    provider_id: config.id.clone(),
                    provider_instance_id: Some(current.instance_id.clone()),
                    available,
                    health: current.health.clone(),
                    residency: current.residency.clone(),
                    ready: current.ready,
                    expired: current.expired,
                    last_seen: Some(current.last_seen),
                });
            }
        }
    }

    result.sort_by(|a, b| {
        a.capability
            .cmp(&b.capability)
            .then_with(|| a.provider_id.cmp(&b.provider_id))
    });
    Json(result)
}

async fn create_capability_binding(
    State(state): State<AppState>,
    Json(request): Json<CapabilityBindingRequest>,
) -> Result<Json<CapabilityBindingRecord>, (StatusCode, Json<Value>)> {
    let reports = state.reports.lock().await.clone();
    let binding = build_capability_binding(&state.configs, &reports, request)
        .map_err(|error| api_error(StatusCode::BAD_REQUEST, error.to_string()))?;
    state
        .capability_bindings
        .lock()
        .await
        .insert(binding.binding_id.clone(), binding.clone());
    if let Err(error) = publish_capability_binding(&state, &binding).await {
        warn!(
            binding_id = %binding.binding_id,
            error = %error,
            "failed to publish advisory capability binding to Fabric"
        );
    }
    Ok(Json(binding))
}

async fn get_capability_binding(
    State(state): State<AppState>,
    Path(id): Path<String>,
) -> Result<Json<CapabilityBindingRecord>, (StatusCode, Json<Value>)> {
    let binding = state
        .capability_bindings
        .lock()
        .await
        .get(&id)
        .cloned()
        .ok_or_else(|| api_error(StatusCode::NOT_FOUND, format!("unknown binding {id}")))?;
    let reports = state.reports.lock().await.clone();
    let binding = refresh_binding_validity(binding, &reports);
    state
        .capability_bindings
        .lock()
        .await
        .insert(id, binding.clone());
    Ok(Json(binding))
}

fn build_capability_binding(
    configs: &HashMap<String, ProviderConfig>,
    reports: &HashMap<String, ProviderReport>,
    request: CapabilityBindingRequest,
) -> Result<CapabilityBindingRecord> {
    let mut required_capabilities = Vec::new();
    let mut seen_capabilities = HashSet::new();
    for capability in request.required_capabilities {
        let capability = capability.trim().to_string();
        if capability.is_empty() {
            return Err(anyhow!(
                "required_capabilities must not contain empty values"
            ));
        }
        if seen_capabilities.insert(capability.clone()) {
            required_capabilities.push(capability);
        }
    }
    if required_capabilities.is_empty() {
        return Err(anyhow!("required_capabilities must not be empty"));
    }

    let required_set: HashSet<&str> = required_capabilities.iter().map(String::as_str).collect();
    for capability in request.fallback_provider_ids.keys() {
        if !required_set.contains(capability.as_str()) {
            return Err(anyhow!(
                "fallback provider supplied for unrequested capability {capability}"
            ));
        }
    }

    let allowed: HashSet<String> = request
        .allowed_provider_ids
        .into_iter()
        .map(|value| value.trim().to_string())
        .filter(|value| !value.is_empty())
        .collect();
    let excluded: HashSet<String> = request
        .excluded_provider_ids
        .into_iter()
        .map(|value| value.trim().to_string())
        .filter(|value| !value.is_empty())
        .collect();
    for provider_id in allowed.iter().chain(excluded.iter()) {
        if !configs.contains_key(provider_id) {
            return Err(anyhow!("unknown provider constraint {provider_id}"));
        }
    }
    for provider_id in request.fallback_provider_ids.values() {
        if !configs.contains_key(provider_id) {
            return Err(anyhow!("unknown fallback provider {provider_id}"));
        }
    }

    let provider_allowed = |provider_id: &str| {
        (allowed.is_empty() || allowed.contains(provider_id)) && !excluded.contains(provider_id)
    };
    let mut selections = Vec::new();
    let mut unresolved_capabilities = Vec::new();

    for capability in required_capabilities {
        let mut candidates: Vec<CapabilityBindingCandidate> = configs
            .keys()
            .filter(|provider_id| provider_allowed(provider_id))
            .filter_map(|provider_id| {
                capability_candidate(provider_id, reports.get(provider_id), &capability)
            })
            .collect();
        candidates.sort_by(|left, right| {
            right
                .available
                .cmp(&left.available)
                .then_with(|| right.ready.cmp(&left.ready))
                .then_with(|| (right.residency == "HOT").cmp(&(left.residency == "HOT")))
                .then_with(|| left.provider_id.cmp(&right.provider_id))
        });

        if let Some(selected) = candidates.iter().find(|candidate| candidate.available) {
            selections.push(CapabilityBindingSelection {
                capability,
                provider_id: selected.provider_id.clone(),
                provider_instance_id: selected.provider_instance_id.clone(),
                boot_id: selected.boot_id.clone(),
                available: true,
                compatibility_verified: true,
                requires_activation: false,
                selection_reason: "AVAILABLE_CAPABILITY".to_string(),
                candidates_considered: candidates,
            });
            continue;
        }

        let fallback = request
            .fallback_provider_ids
            .get(&capability)
            .filter(|provider_id| provider_allowed(provider_id));
        if let Some(provider_id) = fallback {
            let selected = capability_candidate(provider_id, reports.get(provider_id), &capability)
                .unwrap_or_else(|| {
                    unavailable_fallback_candidate(provider_id, reports.get(provider_id))
                });
            selections.push(CapabilityBindingSelection {
                capability,
                provider_id: selected.provider_id.clone(),
                provider_instance_id: selected.provider_instance_id.clone(),
                boot_id: selected.boot_id.clone(),
                available: selected.available,
                compatibility_verified: selected.advertised,
                requires_activation: !selected.available,
                selection_reason: "EXPLICIT_PROVIDER_FALLBACK".to_string(),
                candidates_considered: candidates,
            });
        } else {
            unresolved_capabilities.push(capability);
        }
    }

    let status = if unresolved_capabilities.is_empty() {
        "RESOLVED"
    } else if selections.is_empty() {
        "UNRESOLVED"
    } else {
        "PARTIAL"
    };
    let binding = CapabilityBindingRecord {
        binding_id: Uuid::new_v4().to_string(),
        request_id: request
            .request_id
            .filter(|value| !value.trim().is_empty())
            .unwrap_or_else(|| Uuid::new_v4().to_string()),
        related_skill_id: request.related_skill_id,
        created_at: Utc::now(),
        validated_at: Utc::now(),
        enforcement: "ADVISORY".to_string(),
        validity: "PENDING_VALIDATION".to_string(),
        validation_issues: Vec::new(),
        status: status.to_string(),
        selections,
        unresolved_capabilities,
    };
    Ok(refresh_binding_validity(binding, reports))
}

fn refresh_binding_validity(
    mut binding: CapabilityBindingRecord,
    reports: &HashMap<String, ProviderReport>,
) -> CapabilityBindingRecord {
    let mut issues = Vec::new();
    if !binding.unresolved_capabilities.is_empty() {
        issues.push(format!(
            "UNRESOLVED_CAPABILITIES:{}",
            binding.unresolved_capabilities.join(",")
        ));
    }
    for selection in &binding.selections {
        if selection.requires_activation {
            issues.push(format!(
                "FALLBACK_REQUIRES_ACTIVATION:{}:{}",
                selection.capability, selection.provider_id
            ));
            continue;
        }
        let Some(report) = reports.get(&selection.provider_id) else {
            issues.push(format!(
                "PROVIDER_UNAVAILABLE:{}:{}",
                selection.capability, selection.provider_id
            ));
            continue;
        };
        if selection.provider_instance_id.as_deref() != Some(report.instance_id.as_str())
            || selection.boot_id.as_deref() != Some(report.boot_id.as_str())
        {
            issues.push(format!(
                "PROVIDER_RESTARTED:{}:{}",
                selection.capability, selection.provider_id
            ));
            continue;
        }
        let currently_available =
            capability_candidate(&selection.provider_id, Some(report), &selection.capability)
                .is_some_and(|candidate| candidate.available);
        if !currently_available {
            issues.push(format!(
                "PROVIDER_UNAVAILABLE:{}:{}",
                selection.capability, selection.provider_id
            ));
        }
    }
    binding.validated_at = Utc::now();
    binding.validity = if issues.is_empty() {
        "CURRENT"
    } else if issues
        .iter()
        .any(|issue| issue.starts_with("UNRESOLVED_CAPABILITIES:"))
    {
        "UNRESOLVED"
    } else if issues
        .iter()
        .any(|issue| issue.starts_with("PROVIDER_RESTARTED:"))
    {
        "STALE_PROVIDER_RESTARTED"
    } else if issues
        .iter()
        .any(|issue| issue.starts_with("PROVIDER_UNAVAILABLE:"))
    {
        "STALE_PROVIDER_UNAVAILABLE"
    } else {
        "FALLBACK_REQUIRES_ACTIVATION"
    }
    .to_string();
    binding.validation_issues = issues;
    binding
}

fn capability_candidate(
    provider_id: &str,
    report: Option<&ProviderReport>,
    capability: &str,
) -> Option<CapabilityBindingCandidate> {
    let report = report?;
    let readiness = report
        .details
        .get("capability_readiness")
        .and_then(Value::as_object)?
        .get(capability)?;
    let advertised = readiness.is_boolean();
    let capability_ready = readiness.as_bool().unwrap_or(false);
    let available = advertised
        && capability_ready
        && report.ready
        && !report.expired
        && report.residency == "HOT"
        && !report.health.eq_ignore_ascii_case("UNHEALTHY");
    Some(CapabilityBindingCandidate {
        provider_id: provider_id.to_string(),
        provider_instance_id: Some(report.instance_id.clone()),
        boot_id: Some(report.boot_id.clone()),
        advertised,
        available,
        ready: report.ready,
        health: report.health.clone(),
        residency: report.residency.clone(),
        expired: report.expired,
    })
}

fn unavailable_fallback_candidate(
    provider_id: &str,
    report: Option<&ProviderReport>,
) -> CapabilityBindingCandidate {
    CapabilityBindingCandidate {
        provider_id: provider_id.to_string(),
        provider_instance_id: report.map(|value| value.instance_id.clone()),
        boot_id: report.map(|value| value.boot_id.clone()),
        advertised: false,
        available: false,
        ready: report.is_some_and(|value| value.ready),
        health: report
            .map(|value| value.health.clone())
            .unwrap_or_else(|| "UNKNOWN".to_string()),
        residency: report
            .map(|value| value.residency.clone())
            .unwrap_or_else(|| "COLD".to_string()),
        expired: report.is_some_and(|value| value.expired),
    }
}

async fn register_provider(
    State(state): State<AppState>,
    Json(request): Json<RegisterRequest>,
) -> Result<Json<Value>, (StatusCode, Json<Value>)> {
    let heartbeat_timeout_ms = state
        .configs
        .get(&request.provider_id)
        .map(|config| config.heartbeat_timeout_ms)
        .ok_or_else(|| {
            api_error(
                StatusCode::NOT_FOUND,
                format!("unknown provider {}", request.provider_id),
            )
        })?;
    let now = Utc::now();
    let report = ProviderReport {
        provider_id: request.provider_id.clone(),
        instance_id: request.instance_id,
        boot_id: request.boot_id,
        residency: request.residency,
        health: request.health,
        ready: request.ready,
        pid: request.pid,
        details: request.details,
        last_seen: now,
        expired: false,
    };
    {
        let mut reports = state.reports.lock().await;
        if let Some(existing) = reports.get(&request.provider_id) {
            if provider_identity_conflicts(
                existing,
                &report.instance_id,
                &report.boot_id,
                heartbeat_timeout_ms,
                now,
            ) {
                return Err(api_error(
                    StatusCode::CONFLICT,
                    "a different live provider instance is already registered",
                ));
            }
        }
        reports.insert(request.provider_id, report.clone());
    }
    if let Err(err) = publish_provider_report(&state, &report).await {
        warn!(provider_id = %report.provider_id, error = %err, "failed to publish provider registration to Fabric");
    }
    Ok(Json(
        json!({"accepted": true, "heartbeat_interval_ms": 1000}),
    ))
}

async fn provider_heartbeat(
    State(state): State<AppState>,
    Json(request): Json<HeartbeatRequest>,
) -> Result<Json<Value>, (StatusCode, Json<Value>)> {
    let heartbeat_timeout_ms = state
        .configs
        .get(&request.provider_id)
        .map(|config| config.heartbeat_timeout_ms)
        .ok_or_else(|| api_error(StatusCode::NOT_FOUND, "unknown provider"))?;
    let now = Utc::now();
    let mut reports = state.reports.lock().await;
    if let Some(report) = reports.get(&request.provider_id) {
        if provider_identity_conflicts(
            report,
            &request.instance_id,
            &request.boot_id,
            heartbeat_timeout_ms,
            now,
        ) {
            return Err(api_error(
                StatusCode::CONFLICT,
                "provider instance or boot id does not match live registration",
            ));
        }
    }
    let report = reports
        .entry(request.provider_id.clone())
        .or_insert_with(|| ProviderReport {
            provider_id: request.provider_id.clone(),
            instance_id: request.instance_id.clone(),
            boot_id: request.boot_id.clone(),
            residency: request.residency.clone(),
            health: request.health.clone(),
            ready: request.ready,
            pid: request.pid,
            details: request.details.clone(),
            last_seen: now,
            expired: false,
        });
    report.instance_id = request.instance_id;
    report.boot_id = request.boot_id;
    report.residency = request.residency;
    report.health = request.health;
    report.ready = request.ready;
    report.pid = request.pid;
    report.details = request.details;
    report.last_seen = now;
    report.expired = false;
    let report_snapshot = report.clone();
    drop(reports);
    if let Err(err) = publish_provider_report(&state, &report_snapshot).await {
        warn!(provider_id = %report_snapshot.provider_id, error = %err, "failed to publish provider heartbeat to Fabric");
    }
    Ok(Json(json!({"accepted": true})))
}

async fn reject_if_shutdown_fenced(
    state: &AppState,
    operation: &str,
) -> Result<(), (StatusCode, Json<Value>)> {
    let shutdown_id = state.shutdown_fence.lock().await.clone();
    if let Some(shutdown_id) = shutdown_id {
        return Err(api_error(
            StatusCode::CONFLICT,
            format!("{operation} is fenced by active shutdown execution {shutdown_id}"),
        ));
    }
    Ok(())
}

async fn start_provider(
    State(state): State<AppState>,
    Path(id): Path<String>,
) -> Result<Json<Value>, (StatusCode, Json<Value>)> {
    reject_if_shutdown_fenced(&state, "provider start").await?;
    start_provider_inner(&state, &id)
        .await
        .map(Json)
        .map_err(internal_error)
}

async fn hot_provider(
    State(state): State<AppState>,
    Path(id): Path<String>,
) -> Result<Json<Value>, (StatusCode, Json<Value>)> {
    reject_if_shutdown_fenced(&state, "provider HOT transition").await?;
    ensure_provider_hot(&state, &id)
        .await
        .map(Json)
        .map_err(internal_error)
}

async fn warm_provider(
    State(state): State<AppState>,
    Path(id): Path<String>,
) -> Result<Json<Value>, (StatusCode, Json<Value>)> {
    call_control(&state, &id, "/v1/control/warm")
        .await
        .map(Json)
        .map_err(internal_error)
}

async fn stop_provider(
    State(state): State<AppState>,
    Path(id): Path<String>,
) -> Result<Json<Value>, (StatusCode, Json<Value>)> {
    stop_provider_inner(&state, &id, false)
        .await
        .map(Json)
        .map_err(internal_error)
}

async fn kill_provider(
    State(state): State<AppState>,
    Path(id): Path<String>,
) -> Result<Json<Value>, (StatusCode, Json<Value>)> {
    stop_provider_inner(&state, &id, true)
        .await
        .map(Json)
        .map_err(internal_error)
}

async fn provider_request(
    State(state): State<AppState>,
    Path(id): Path<String>,
    Json(request): Json<ProviderRequest>,
) -> Result<Json<Value>, (StatusCode, Json<Value>)> {
    if request.action.trim().is_empty() {
        return Err(api_error(StatusCode::BAD_REQUEST, "action is required"));
    }
    ensure_started(&state, &id).await.map_err(internal_error)?;
    call_control_json(&state, &id, "/v1/control/request", &request)
        .await
        .map(Json)
        .map_err(internal_error)
}

async fn motion_inhibit_status(State(state): State<AppState>) -> Json<Value> {
    Json(motion_inhibit_view(&state).await)
}

async fn acquire_motion_inhibit(
    State(state): State<AppState>,
    Json(request): Json<MotionInhibitRequest>,
) -> Result<Json<Value>, (StatusCode, Json<Value>)> {
    let owner_id = request.owner_id.trim().to_string();
    if owner_id.is_empty() {
        return Err(api_error(StatusCode::BAD_REQUEST, "owner_id is required"));
    }
    let lease = MotionInhibitLease {
        owner_id: owner_id.clone(),
        reason: request.reason.trim().to_string(),
        related_skill_id: request.related_skill_id,
        issued_at: Utc::now(),
    };
    state.motion_inhibits.lock().await.insert(owner_id, lease);
    let view = motion_inhibit_view(&state).await;
    publish_motion_inhibit(&state, &view)
        .await
        .map_err(internal_error)?;
    Ok(Json(view))
}

async fn release_motion_inhibit(
    State(state): State<AppState>,
    Json(request): Json<MotionInhibitReleaseRequest>,
) -> Result<Json<Value>, (StatusCode, Json<Value>)> {
    let owner_id = request.owner_id.trim();
    if owner_id.is_empty() {
        return Err(api_error(StatusCode::BAD_REQUEST, "owner_id is required"));
    }
    state.motion_inhibits.lock().await.remove(owner_id);
    let view = motion_inhibit_view(&state).await;
    publish_motion_inhibit(&state, &view)
        .await
        .map_err(internal_error)?;
    Ok(Json(view))
}

async fn acquire_control_authority(
    State(state): State<AppState>,
    Json(request): Json<AcquireControlAuthorityRequest>,
) -> Result<Json<ControlAuthorityLease>, (StatusCode, Json<Value>)> {
    reject_if_shutdown_fenced(&state, "control-authority acquisition").await?;
    let resource_id = request.resource_id.trim().to_string();
    let owner_id = request.owner_id.trim().to_string();
    if resource_id.is_empty() || owner_id.is_empty() {
        return Err(api_error(
            StatusCode::BAD_REQUEST,
            "resource_id and owner_id are required",
        ));
    }
    validate_authority_duration(request.duration_ms, request.renewal_interval_ms)
        .map_err(|error| api_error(StatusCode::BAD_REQUEST, error.to_string()))?;
    let mut permissions: Vec<String> = request
        .permissions
        .into_iter()
        .map(|value| value.trim().to_string())
        .filter(|value| !value.is_empty())
        .collect();
    permissions.sort();
    permissions.dedup();
    if permissions.is_empty() {
        return Err(api_error(
            StatusCode::BAD_REQUEST,
            "permissions must not be empty",
        ));
    }

    let now = Utc::now();
    let mut leases = state.control_authority_leases.lock().await;
    expire_control_authority_locked(&mut leases, now);
    let active_lease_id = leases
        .values()
        .filter(|lease| lease.resource_id == resource_id && lease.state == "ACTIVE")
        .max_by_key(|lease| lease.fencing_generation)
        .map(|lease| lease.lease_id.clone());
    if let Some(active_lease_id) = active_lease_id {
        if !request.preempt {
            let active = leases.get(&active_lease_id).expect("active lease exists");
            return Err(api_error(
                StatusCode::CONFLICT,
                format!(
                    "resource {resource_id} already has active advisory lease {} owned by {}",
                    active.lease_id, active.owner_id
                ),
            ));
        }
        if let Some(active) = leases.get_mut(&active_lease_id) {
            active.state = "PREEMPTED".to_string();
            active.last_transition_reason = format!("preempted by advisory owner {owner_id}");
        }
    }

    let mut generations = state.control_authority_generations.lock().await;
    let generation = generations.entry(resource_id.clone()).or_insert(0);
    *generation += 1;
    let lease = ControlAuthorityLease {
        lease_id: Uuid::new_v4().to_string(),
        resource_id: resource_id.clone(),
        owner_id,
        permissions,
        issued_at: now,
        expires_at: now + ChronoDuration::milliseconds(request.duration_ms as i64),
        renewal_interval_ms: request.renewal_interval_ms,
        fencing_generation: *generation,
        preemption_policy: request
            .preemption_policy
            .unwrap_or_else(|| "DENY_UNLESS_EXPLICIT_PREEMPT".to_string()),
        safe_relinquish: request
            .safe_relinquish
            .unwrap_or_else(|| "PROVIDER_LOCAL_POLICY".to_string()),
        state: "ACTIVE".to_string(),
        related_skill_id: request.related_skill_id,
        last_transition_reason: "issued in advisory mode".to_string(),
    };
    leases.insert(lease.lease_id.clone(), lease.clone());
    drop(generations);
    drop(leases);

    if let Err(error) = publish_control_authority_resource(&state, &resource_id).await {
        warn!(resource_id = %resource_id, error = %error, "failed to publish advisory authority");
    }
    Ok(Json(lease))
}

async fn renew_control_authority(
    State(state): State<AppState>,
    Path(id): Path<String>,
    Json(request): Json<RenewControlAuthorityRequest>,
) -> Result<Json<ControlAuthorityLease>, (StatusCode, Json<Value>)> {
    validate_authority_duration(request.duration_ms, 1)
        .map_err(|error| api_error(StatusCode::BAD_REQUEST, error.to_string()))?;
    let owner_id = request.owner_id.trim();
    if owner_id.is_empty() {
        return Err(api_error(StatusCode::BAD_REQUEST, "owner_id is required"));
    }
    let now = Utc::now();
    let mut leases = state.control_authority_leases.lock().await;
    expire_control_authority_locked(&mut leases, now);
    let lease = leases
        .get_mut(&id)
        .ok_or_else(|| api_error(StatusCode::NOT_FOUND, format!("unknown lease {id}")))?;
    if lease.owner_id != owner_id {
        return Err(api_error(StatusCode::FORBIDDEN, "lease owner mismatch"));
    }
    if lease.state != "ACTIVE" {
        return Err(api_error(
            StatusCode::CONFLICT,
            format!("lease {} is {}", lease.lease_id, lease.state),
        ));
    }
    lease.expires_at = now + ChronoDuration::milliseconds(request.duration_ms as i64);
    lease.last_transition_reason = "renewed in advisory mode".to_string();
    let renewed = lease.clone();
    let resource_id = lease.resource_id.clone();
    drop(leases);

    if let Err(error) = publish_control_authority_resource(&state, &resource_id).await {
        warn!(resource_id = %resource_id, error = %error, "failed to publish renewed authority");
    }
    Ok(Json(renewed))
}

async fn release_control_authority(
    State(state): State<AppState>,
    Path(id): Path<String>,
    Json(request): Json<ReleaseControlAuthorityRequest>,
) -> Result<Json<ControlAuthorityLease>, (StatusCode, Json<Value>)> {
    let owner_id = request.owner_id.trim();
    if owner_id.is_empty() {
        return Err(api_error(StatusCode::BAD_REQUEST, "owner_id is required"));
    }
    let mut leases = state.control_authority_leases.lock().await;
    let lease = leases
        .get_mut(&id)
        .ok_or_else(|| api_error(StatusCode::NOT_FOUND, format!("unknown lease {id}")))?;
    if lease.owner_id != owner_id {
        return Err(api_error(StatusCode::FORBIDDEN, "lease owner mismatch"));
    }
    if lease.state == "ACTIVE" {
        lease.state = "RELEASED".to_string();
        lease.last_transition_reason = request
            .reason
            .filter(|value| !value.trim().is_empty())
            .unwrap_or_else(|| "released by owner".to_string());
    }
    let released = lease.clone();
    let resource_id = lease.resource_id.clone();
    drop(leases);

    if let Err(error) = publish_control_authority_resource(&state, &resource_id).await {
        warn!(resource_id = %resource_id, error = %error, "failed to publish released authority");
    }
    Ok(Json(released))
}

async fn get_control_authority_resource(
    State(state): State<AppState>,
    Path(id): Path<String>,
) -> Json<ControlAuthorityResourceView> {
    expire_control_authority_state(&state).await;
    Json(control_authority_resource_view(&state, &id).await)
}

fn validate_authority_duration(duration_ms: u64, renewal_interval_ms: u64) -> Result<()> {
    if !(500..=60_000).contains(&duration_ms) {
        return Err(anyhow!("duration_ms must be in [500, 60000]"));
    }
    if renewal_interval_ms == 0 || renewal_interval_ms >= duration_ms {
        return Err(anyhow!(
            "renewal_interval_ms must be positive and less than duration_ms"
        ));
    }
    Ok(())
}

fn expire_control_authority_locked(
    leases: &mut HashMap<String, ControlAuthorityLease>,
    now: DateTime<Utc>,
) -> HashSet<String> {
    let mut changed_resources = HashSet::new();
    for lease in leases.values_mut() {
        if lease.state == "ACTIVE" && lease.expires_at <= now {
            lease.state = "EXPIRED".to_string();
            lease.last_transition_reason = "advisory lease expired".to_string();
            changed_resources.insert(lease.resource_id.clone());
        }
    }
    changed_resources
}

async fn expire_control_authority_state(state: &AppState) -> HashSet<String> {
    let mut leases = state.control_authority_leases.lock().await;
    expire_control_authority_locked(&mut leases, Utc::now())
}

async fn control_authority_resource_view(
    state: &AppState,
    resource_id: &str,
) -> ControlAuthorityResourceView {
    let leases = state.control_authority_leases.lock().await;
    let active_lease = leases
        .values()
        .filter(|lease| lease.resource_id == resource_id && lease.state == "ACTIVE")
        .max_by_key(|lease| lease.fencing_generation)
        .cloned();
    drop(leases);
    let latest_fencing_generation = state
        .control_authority_generations
        .lock()
        .await
        .get(resource_id)
        .copied()
        .unwrap_or(0);
    ControlAuthorityResourceView {
        resource_id: resource_id.to_string(),
        enforcement: "ADVISORY".to_string(),
        active_lease,
        latest_fencing_generation,
    }
}

async fn create_shutdown_plan(
    State(state): State<AppState>,
    Json(request): Json<ShutdownPlanRequest>,
) -> Result<Json<ShutdownPlanRecord>, (StatusCode, Json<Value>)> {
    let owner_id = request.owner_id.trim().to_string();
    let reason = request.reason.trim().to_string();
    let request_id = request
        .request_id
        .map(|value| value.trim().to_string())
        .filter(|value| !value.is_empty())
        .unwrap_or_else(|| Uuid::new_v4().to_string());
    if owner_id.is_empty() || reason.is_empty() {
        return Err(api_error(
            StatusCode::BAD_REQUEST,
            "owner_id and reason are required",
        ));
    }
    let reports = state.reports.lock().await.clone();
    let running_provider_ids = manager_running_provider_ids(&state).await;
    let plan = build_shutdown_plan(
        &state.configs,
        &reports,
        &running_provider_ids,
        request_id,
        owner_id,
        reason,
    );
    *state.shutdown_plan.lock().await = Some(plan.clone());
    if let Err(error) = publish_shutdown_plan(&state, &plan).await {
        warn!(shutdown_id = %plan.shutdown_id, error = %error, "failed to publish shutdown shadow plan");
    }
    Ok(Json(plan))
}

async fn get_shutdown_plan(State(state): State<AppState>) -> Json<Value> {
    let plan = state.shutdown_plan.lock().await.clone();
    Json(match plan {
        Some(plan) => json!(plan),
        None => json!({
            "state": "NOT_REQUESTED",
            "enforcement": "SHADOW_DRY_RUN",
        }),
    })
}

async fn execute_shutdown_plan(
    State(state): State<AppState>,
    Path(id): Path<String>,
    Json(request): Json<ShutdownExecuteRequest>,
) -> Result<(StatusCode, Json<ShutdownExecutionRecord>), (StatusCode, Json<Value>)> {
    if !state.shutdown_execution_enabled {
        return Err(api_error(
            StatusCode::CONFLICT,
            "Manager shutdown execution is disabled; the shadow plan and local safety fallback remain available",
        ));
    }
    let request_id = request.request_id.trim().to_string();
    if request_id.is_empty() {
        return Err(api_error(StatusCode::BAD_REQUEST, "request_id is required"));
    }
    if request.confirmation != "EXECUTE_MANAGER_PROVIDER_SHUTDOWN" {
        return Err(api_error(
            StatusCode::BAD_REQUEST,
            "confirmation must equal EXECUTE_MANAGER_PROVIDER_SHUTDOWN",
        ));
    }
    let plan = state
        .shutdown_plan
        .lock()
        .await
        .clone()
        .filter(|plan| plan.shutdown_id == id)
        .ok_or_else(|| api_error(StatusCode::NOT_FOUND, format!("unknown shutdown plan {id}")))?;
    if !plan.blockers.is_empty() {
        return Err(api_error(
            StatusCode::CONFLICT,
            format!(
                "shutdown plan {} has unresolved blockers: {}",
                plan.shutdown_id,
                plan.blockers.join("; ")
            ),
        ));
    }

    {
        let execution = state.shutdown_execution.lock().await;
        if let Some(existing) = execution.as_ref() {
            if existing.shutdown_id == plan.shutdown_id && existing.request_id == request_id {
                return Ok((StatusCode::OK, Json(existing.clone())));
            }
            if existing.state == "ACCEPTED" || existing.state == "RUNNING" {
                return Err(api_error(
                    StatusCode::CONFLICT,
                    format!(
                        "shutdown execution {} is already active",
                        existing.execution_id
                    ),
                ));
            }
        }
    }

    let record = ShutdownExecutionRecord {
        execution_id: Uuid::new_v4().to_string(),
        shutdown_id: plan.shutdown_id.clone(),
        request_id,
        requested_by: plan.requested_by.clone(),
        started_at: Utc::now(),
        completed_at: None,
        state: "ACCEPTED".to_string(),
        enforcement: "MANAGER_PROVIDER_SEQUENCE".to_string(),
        current_step: None,
        step_results: Vec::new(),
        failures: Vec::new(),
        supervisor_actions: vec![
            "Stop Fabric only after provider sequence acknowledgement".to_string(),
            "Stop Manager last from the workspace supervisor".to_string(),
        ],
    };
    *state.shutdown_fence.lock().await = Some(plan.shutdown_id.clone());
    *state.shutdown_execution.lock().await = Some(record.clone());

    let execution_state = state.clone();
    let execution_id = record.execution_id.clone();
    tokio::spawn(async move {
        run_shutdown_execution(execution_state, plan, execution_id).await;
    });
    Ok((StatusCode::ACCEPTED, Json(record)))
}

async fn get_shutdown_execution(
    State(state): State<AppState>,
    Path(id): Path<String>,
) -> Result<Json<ShutdownExecutionRecord>, (StatusCode, Json<Value>)> {
    state
        .shutdown_execution
        .lock()
        .await
        .clone()
        .filter(|execution| execution.execution_id == id)
        .map(Json)
        .ok_or_else(|| {
            api_error(
                StatusCode::NOT_FOUND,
                format!("unknown shutdown execution {id}"),
            )
        })
}

async fn run_shutdown_execution(state: AppState, plan: ShutdownPlanRecord, execution_id: String) {
    {
        let mut execution = state.shutdown_execution.lock().await;
        if let Some(record) = execution
            .as_mut()
            .filter(|record| record.execution_id == execution_id)
        {
            record.state = "RUNNING".to_string();
        }
    }

    let mut ordinary_failure = false;
    for step in plan.steps.clone() {
        {
            let mut execution = state.shutdown_execution.lock().await;
            if let Some(record) = execution
                .as_mut()
                .filter(|record| record.execution_id == execution_id)
            {
                record.current_step = Some(step.order);
            }
        }
        let (result, safety_blocked, step_had_failure) =
            execute_shutdown_step(&state, &plan, &step).await;
        ordinary_failure |= step_had_failure && !safety_blocked;
        {
            let mut execution = state.shutdown_execution.lock().await;
            if let Some(record) = execution
                .as_mut()
                .filter(|record| record.execution_id == execution_id)
            {
                for provider in &result.provider_results {
                    if let Some(error) = &provider.error {
                        record.failures.push(format!(
                            "{}: {}: {}",
                            step.action, provider.provider_id, error
                        ));
                    }
                }
                if let Some(error) = &result.error {
                    record.failures.push(format!("{}: {error}", step.action));
                }
                record.step_results.push(result);
                if safety_blocked {
                    record.state = "BLOCKED_SAFETY_SUPPORT_RETAINED".to_string();
                    record.completed_at = Some(Utc::now());
                    record.current_step = None;
                    record.supervisor_actions = vec![
                        "Do not stop Basic, Fabric, or Manager".to_string(),
                        "Use the local authoritative arm shutdown fallback after operator inspection"
                            .to_string(),
                    ];
                }
            }
        }
        if safety_blocked {
            publish_current_shutdown_execution(&state).await;
            return;
        }
    }

    {
        let mut execution = state.shutdown_execution.lock().await;
        if let Some(record) = execution
            .as_mut()
            .filter(|record| record.execution_id == execution_id)
        {
            record.state = if ordinary_failure {
                "PARTIAL_FAILURE_AWAITING_SUPERVISOR".to_string()
            } else {
                "AWAITING_SUPERVISOR".to_string()
            };
            record.completed_at = Some(Utc::now());
            record.current_step = None;
        }
    }
    publish_current_shutdown_execution(&state).await;
}

async fn execute_shutdown_step(
    state: &AppState,
    plan: &ShutdownPlanRecord,
    step: &ShutdownPlanStep,
) -> (ShutdownStepResult, bool, bool) {
    let started_at = Utc::now();
    let mut result = ShutdownStepResult {
        order: step.order,
        action: step.action.clone(),
        state: "RUNNING".to_string(),
        started_at,
        completed_at: None,
        provider_results: Vec::new(),
        acknowledgement: None,
        error: None,
    };
    let mut safety_blocked = false;
    let mut had_failure = false;

    match step.action.as_str() {
        "FENCE_NEW_MANAGER_AUTHORITY" => {
            result.acknowledgement = Some(format!(
                "New Manager provider starts, HOT transitions, and authority acquisitions are fenced by {}",
                plan.shutdown_id
            ));
        }
        "REQUEST_MOTION_PROVIDERS_SAFE_RELINQUISH"
        | "STOP_NON_SAFETY_PROVIDERS"
        | "STOP_BASIC_PROVIDERS_AFTER_CONFIRMATION" => {
            for provider_id in &step.provider_ids {
                match stop_tracked_provider_for_shutdown(state, provider_id).await {
                    Ok(acknowledgement) => {
                        result.provider_results.push(ShutdownProviderResult {
                            provider_id: provider_id.clone(),
                            state: "CONFIRMED".to_string(),
                            acknowledgement: Some(acknowledgement),
                            error: None,
                        });
                    }
                    Err(error) => {
                        had_failure = true;
                        if step.action != "STOP_NON_SAFETY_PROVIDERS" {
                            safety_blocked = true;
                        }
                        result.provider_results.push(ShutdownProviderResult {
                            provider_id: provider_id.clone(),
                            state: "FAILED".to_string(),
                            acknowledgement: None,
                            error: Some(error.to_string()),
                        });
                    }
                }
            }
        }
        "CONFIRM_BASIC_SAFE_STATE" => {
            for provider_id in &step.provider_ids {
                match confirm_provider_safe_state(state, provider_id).await {
                    Ok(acknowledgement) => {
                        result.provider_results.push(ShutdownProviderResult {
                            provider_id: provider_id.clone(),
                            state: "CONFIRMED".to_string(),
                            acknowledgement: Some(acknowledgement),
                            error: None,
                        });
                    }
                    Err(error) => {
                        had_failure = true;
                        safety_blocked = true;
                        result.provider_results.push(ShutdownProviderResult {
                            provider_id: provider_id.clone(),
                            state: "FAILED".to_string(),
                            acknowledgement: None,
                            error: Some(error.to_string()),
                        });
                    }
                }
            }
        }
        "STOP_FABRIC_AFTER_AUDIT_FLUSH" => {
            result.acknowledgement = Some(
                "Provider sequence is complete; Fabric stop remains a workspace-supervisor action"
                    .to_string(),
            );
        }
        "SUPERVISOR_STOPS_MANAGER_LAST" => {
            result.acknowledgement = Some(
                "Manager remains alive to expose this acknowledgement and must be stopped last"
                    .to_string(),
            );
        }
        _ => {
            had_failure = true;
            safety_blocked = true;
            result.error = Some(format!("unsupported shutdown action {}", step.action));
        }
    }

    result.state = if had_failure {
        "FAILED".to_string()
    } else {
        "CONFIRMED".to_string()
    };
    result.completed_at = Some(Utc::now());
    (result, safety_blocked, had_failure)
}

async fn manager_tracked_process_state(state: &AppState, provider_id: &str) -> Option<String> {
    let mut processes = state.processes.lock().await;
    let process = processes.get_mut(provider_id)?;
    refresh_process_state(process);
    Some(process.state.clone())
}

async fn manager_running_provider_ids(state: &AppState) -> HashSet<String> {
    let mut processes = state.processes.lock().await;
    processes
        .iter_mut()
        .filter_map(|(provider_id, process)| {
            refresh_process_state(process);
            (process.state == "running" || process.state == "starting").then(|| provider_id.clone())
        })
        .collect()
}

async fn provider_has_fresh_live_report(state: &AppState, provider_id: &str) -> bool {
    state
        .reports
        .lock()
        .await
        .get(provider_id)
        .is_some_and(|report| !report.expired && report.ready)
}

async fn stop_tracked_provider_for_shutdown(state: &AppState, provider_id: &str) -> Result<Value> {
    match manager_tracked_process_state(state, provider_id).await {
        Some(process_state) if process_state == "running" || process_state == "starting" => {
            stop_provider_inner(state, provider_id, false).await
        }
        Some(_) => Ok(json!({
            "provider_id": provider_id,
            "status": "already_stopped"
        })),
        None if provider_has_fresh_live_report(state, provider_id).await => Err(anyhow!(
            "provider is live but is not owned by this Manager process"
        )),
        None => Ok(json!({
            "provider_id": provider_id,
            "status": "already_stopped"
        })),
    }
}

async fn confirm_provider_safe_state(state: &AppState, provider_id: &str) -> Result<Value> {
    match manager_tracked_process_state(state, provider_id).await {
        Some(process_state) if process_state == "running" || process_state == "starting" => {}
        Some(_) => {
            return Ok(json!({
                "provider_id": provider_id,
                "status": "already_stopped",
                "safe_state_confirmed": true
            }));
        }
        None if provider_has_fresh_live_report(state, provider_id).await => {
            return Err(anyhow!(
                "provider is live but is not owned by this Manager process"
            ));
        }
        None => {
            return Ok(json!({
                "provider_id": provider_id,
                "status": "already_stopped",
                "safe_state_confirmed": true
            }));
        }
    }
    let config = state
        .configs
        .get(provider_id)
        .ok_or_else(|| anyhow!("unknown provider {provider_id}"))?;
    let base = config
        .control_url
        .as_ref()
        .ok_or_else(|| anyhow!("provider {provider_id} has no control URL"))?;
    let path = config
        .safe_state_request_path
        .as_ref()
        .ok_or_else(|| anyhow!("provider {provider_id} has no safe-state request path"))?;
    if !path.starts_with('/') {
        return Err(anyhow!(
            "provider {provider_id} safe-state request path must start with /"
        ));
    }
    let response = state
        .http
        .post(format!("{base}{path}"))
        .json(&json!({}))
        .timeout(Duration::from_millis(config.safe_state_timeout_ms))
        .send()
        .await
        .with_context(|| format!("requesting safe state from provider {provider_id}"))?;
    let status = response.status();
    let body: Value = response
        .json()
        .await
        .unwrap_or_else(|_| json!({"status": status.as_u16()}));
    if !status.is_success() || body.get("success").and_then(Value::as_bool) != Some(true) {
        return Err(anyhow!(
            "provider {provider_id} did not confirm safe state: HTTP {status}: {body}"
        ));
    }
    Ok(json!({
        "provider_id": provider_id,
        "safe_state_confirmed": true,
        "response": body
    }))
}

async fn publish_current_shutdown_execution(state: &AppState) {
    let execution = state.shutdown_execution.lock().await.clone();
    if let Some(execution) = execution {
        if let Err(error) = publish_shutdown_execution(state, &execution).await {
            warn!(
                execution_id = %execution.execution_id,
                error = %error,
                "failed to publish shutdown execution state"
            );
        }
    }
}

fn build_shutdown_plan(
    configs: &HashMap<String, ProviderConfig>,
    reports: &HashMap<String, ProviderReport>,
    running_provider_ids: &HashSet<String>,
    request_id: String,
    owner_id: String,
    reason: String,
) -> ShutdownPlanRecord {
    let mut motion_providers = Vec::new();
    let mut basic_providers = Vec::new();
    let mut other_providers = Vec::new();
    let mut blockers = Vec::new();

    for config in configs.values() {
        let capabilities = reports
            .get(&config.id)
            .and_then(|report| report.details.get("capability_readiness"))
            .and_then(Value::as_object);
        let motion_capable = capabilities.is_some_and(|values| {
            values
                .keys()
                .any(|capability| capability.starts_with("robot.motion"))
        }) || config.id.contains("integrated");
        let basic_arm = config.id.contains("rebot_dm")
            || capabilities.is_some_and(|values| {
                values
                    .keys()
                    .any(|capability| capability == "robot_arm.gravity_float")
            });

        if basic_arm {
            basic_providers.push(config.id.clone());
            if config.safe_state_request_path.is_none() {
                blockers.push(format!(
                    "basic provider {} has no configured safe-state request path",
                    config.id
                ));
            }
        } else if motion_capable {
            motion_providers.push(config.id.clone());
            if running_provider_ids.contains(&config.id) {
                match reports.get(&config.id) {
                    Some(report) if report.expired => blockers.push(format!(
                        "motion provider {} heartbeat is expired; physical outcome requires operator verification",
                        config.id
                    )),
                    None => blockers.push(format!(
                        "motion provider {} has no current heartbeat; physical outcome requires operator verification",
                        config.id
                    )),
                    _ => {}
                }
            }
        } else {
            other_providers.push(config.id.clone());
        }
    }
    motion_providers.sort();
    basic_providers.sort();
    other_providers.sort();

    let steps = vec![
        ShutdownPlanStep {
            order: 1,
            action: "FENCE_NEW_MANAGER_AUTHORITY".to_string(),
            provider_ids: Vec::new(),
            required_confirmation: "No new advisory or enforced authority may be issued"
                .to_string(),
        },
        ShutdownPlanStep {
            order: 2,
            action: "REQUEST_MOTION_PROVIDERS_SAFE_RELINQUISH".to_string(),
            provider_ids: motion_providers,
            required_confirmation:
                "Every motion provider reports stopped, holding, or verified gravity-float"
                    .to_string(),
        },
        ShutdownPlanStep {
            order: 3,
            action: "CONFIRM_BASIC_SAFE_STATE".to_string(),
            provider_ids: basic_providers.clone(),
            required_confirmation:
                "Basic arm providers confirm their declared safe state before process stop"
                    .to_string(),
        },
        ShutdownPlanStep {
            order: 4,
            action: "STOP_NON_SAFETY_PROVIDERS".to_string(),
            provider_ids: other_providers,
            required_confirmation: "Normal providers flush diagnostics and exit".to_string(),
        },
        ShutdownPlanStep {
            order: 5,
            action: "STOP_BASIC_PROVIDERS_AFTER_CONFIRMATION".to_string(),
            provider_ids: basic_providers,
            required_confirmation:
                "Safety-critical powered support is preserved when process stop is unsafe"
                    .to_string(),
        },
        ShutdownPlanStep {
            order: 6,
            action: "STOP_FABRIC_AFTER_AUDIT_FLUSH".to_string(),
            provider_ids: Vec::new(),
            required_confirmation: "Pending control audit copies are flushed or retained locally"
                .to_string(),
        },
        ShutdownPlanStep {
            order: 7,
            action: "SUPERVISOR_STOPS_MANAGER_LAST".to_string(),
            provider_ids: Vec::new(),
            required_confirmation:
                "Workspace supervisor, not a provider, terminates the Manager last".to_string(),
        },
    ];
    ShutdownPlanRecord {
        shutdown_id: Uuid::new_v4().to_string(),
        request_id,
        requested_at: Utc::now(),
        requested_by: owner_id,
        reason,
        state: if blockers.is_empty() {
            "PLANNED".to_string()
        } else {
            "PLANNED_WITH_BLOCKERS".to_string()
        },
        enforcement: "SHADOW_DRY_RUN".to_string(),
        steps,
        blockers,
    }
}

async fn motion_inhibit_view(state: &AppState) -> Value {
    let leases: Vec<MotionInhibitLease> = state
        .motion_inhibits
        .lock()
        .await
        .values()
        .cloned()
        .collect();
    let reports = state.reports.lock().await;
    let motion_provider_count = reports
        .values()
        .filter(|report| {
            report
                .details
                .get("capability_readiness")
                .and_then(Value::as_object)
                .is_some_and(|capabilities| {
                    capabilities
                        .keys()
                        .any(|name| name.starts_with("robot.motion"))
                })
        })
        .count();
    json!({
        "inhibited": !leases.is_empty(),
        "owners": leases,
        "motion_provider_count": motion_provider_count,
        "enforcement": if motion_provider_count == 0 {
            "NO_MOTION_PROVIDERS_PRESENT"
        } else {
            "PROVIDERS_MUST_OBSERVE_AND_ACKNOWLEDGE"
        },
    })
}

async fn publish_motion_inhibit(state: &AppState, view: &Value) -> Result<()> {
    let now_us = Utc::now().timestamp_micros().max(0) as u64;
    let observation = json!({
        "schema": "physical_agent.motion_inhibit",
        "schema_version": 1,
        "stream": "system.motion.inhibit",
        "provider_id": "resource-provider-manager",
        "provider_instance_id": "manager-local",
        "boot_id": "manager-local",
        "sequence": now_us,
        "observed_at_us": now_us,
        "freshness_ms": null,
        "valid": true,
        "data": view,
    });
    let response = state
        .http
        .post(format!(
            "{}/v1/observations",
            state.fabric_url.trim_end_matches('/')
        ))
        .json(&observation)
        .timeout(Duration::from_secs(2))
        .send()
        .await?;
    if !response.status().is_success() {
        return Err(anyhow!("Fabric returned {}", response.status()));
    }
    Ok(())
}

async fn ensure_started(state: &AppState, id: &str) -> Result<()> {
    let mut processes = state.processes.lock().await;
    let running = if let Some(process) = processes.get_mut(id) {
        refresh_process_state(process);
        process.state == "running" || process.state == "starting"
    } else {
        false
    };
    drop(processes);
    if !running {
        start_provider_inner(state, id).await?;
    }
    Ok(())
}

async fn ensure_provider_hot(state: &AppState, id: &str) -> Result<Value> {
    ensure_started(state, id).await?;
    let deadline = Instant::now() + Duration::from_secs(15);
    loop {
        match call_control_with_timeout(state, id, "/v1/control/hot", Duration::from_millis(750))
            .await
        {
            Ok(result) => return Ok(result),
            Err(error) if Instant::now() >= deadline => {
                return Err(anyhow!(
                    "provider {id} did not accept HOT within 15 seconds: {error}"
                ));
            }
            Err(_) => {}
        }
        sleep(Duration::from_millis(250)).await;
    }
}

async fn start_provider_inner(state: &AppState, id: &str) -> Result<Value> {
    let config = state
        .configs
        .get(id)
        .cloned()
        .ok_or_else(|| anyhow!("unknown provider {id}"))?;

    {
        let mut processes = state.processes.lock().await;
        if let Some(existing) = processes.get_mut(id) {
            refresh_process_state(existing);
            if existing.state == "running" || existing.state == "starting" {
                return Ok(
                    json!({"provider_id": id, "status": "already_running", "pid": existing.pid}),
                );
            }
        }
    }

    let existing_report = {
        let reports = state.reports.lock().await;
        reports.get(id).cloned()
    };
    if let Some(report) = existing_report {
        if provider_report_is_fresh(&report, config.heartbeat_timeout_ms, Utc::now()) {
            return Ok(json!({
                "provider_id": id,
                "status": "already_registered",
                "instance_id": report.instance_id,
                "boot_id": report.boot_id,
                "pid": report.pid,
                "managed_process": false
            }));
        }
    }

    let command_text = expand_vars(&config.command)?;
    let args = config
        .args
        .iter()
        .map(|arg| expand_vars(arg))
        .collect::<Result<Vec<_>>>()?;
    let cwd = config.cwd.as_deref().map(expand_vars).transpose()?;

    let mut command = Command::new(&command_text);
    command
        .args(&args)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .kill_on_drop(false);

    if let Some(cwd) = cwd {
        command.current_dir(cwd);
    }
    for (key, value) in &config.env {
        command.env(key, expand_vars(value)?);
    }

    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NEW_PROCESS_GROUP: u32 = 0x0000_0200;
        command
            .as_std_mut()
            .creation_flags(CREATE_NEW_PROCESS_GROUP);
    }

    let mut child = command
        .spawn()
        .with_context(|| format!("starting provider {id} using {command_text}"))?;
    let pid = child
        .id()
        .ok_or_else(|| anyhow!("provider has no process id"))?;

    if let Some(stdout) = child.stdout.take() {
        let id = id.to_string();
        tokio::spawn(async move {
            let mut lines = BufReader::new(stdout).lines();
            while let Ok(Some(line)) = lines.next_line().await {
                info!(provider_id = %id, stream = "stdout", "{line}");
            }
        });
    }
    if let Some(stderr) = child.stderr.take() {
        let id = id.to_string();
        tokio::spawn(async move {
            let mut lines = BufReader::new(stderr).lines();
            while let Ok(Some(line)) = lines.next_line().await {
                warn!(provider_id = %id, stream = "stderr", "{line}");
            }
        });
    }

    state.processes.lock().await.insert(
        id.to_string(),
        ManagedProcess {
            child,
            pid,
            state: "starting".to_string(),
            last_exit: None,
        },
    );
    info!(provider_id = %id, pid, "provider process started");
    Ok(json!({"provider_id": id, "status": "starting", "pid": pid}))
}

async fn call_control(state: &AppState, id: &str, path: &str) -> Result<Value> {
    call_control_with_timeout(state, id, path, Duration::from_secs(10)).await
}

async fn call_control_with_timeout(
    state: &AppState,
    id: &str,
    path: &str,
    timeout: Duration,
) -> Result<Value> {
    let config = state
        .configs
        .get(id)
        .ok_or_else(|| anyhow!("unknown provider {id}"))?;
    let base = config
        .control_url
        .as_ref()
        .ok_or_else(|| anyhow!("provider {id} does not declare a control_url"))?;
    let response = state
        .http
        .post(format!("{base}{path}"))
        .timeout(timeout)
        .send()
        .await
        .with_context(|| format!("calling provider control endpoint {path}"))?;
    let status = response.status();
    let body: Value = response
        .json()
        .await
        .unwrap_or_else(|_| json!({"status": status.as_u16()}));
    if !status.is_success() {
        return Err(anyhow!("provider control returned {status}: {body}"));
    }
    Ok(body)
}

async fn call_control_json<T: Serialize + ?Sized>(
    state: &AppState,
    id: &str,
    path: &str,
    payload: &T,
) -> Result<Value> {
    let config = state
        .configs
        .get(id)
        .ok_or_else(|| anyhow!("unknown provider {id}"))?;
    let base = config
        .control_url
        .as_ref()
        .ok_or_else(|| anyhow!("provider {id} does not declare a control_url"))?;
    let response = state
        .http
        .post(format!("{base}{path}"))
        .json(payload)
        .timeout(Duration::from_secs(30))
        .send()
        .await
        .with_context(|| format!("calling provider control endpoint {path}"))?;
    let status = response.status();
    let body: Value = response
        .json()
        .await
        .unwrap_or_else(|_| json!({"status": status.as_u16()}));
    if !status.is_success() {
        return Err(anyhow!("provider control returned {status}: {body}"));
    }
    Ok(body)
}

async fn stop_provider_inner(state: &AppState, id: &str, force: bool) -> Result<Value> {
    let config = state
        .configs
        .get(id)
        .cloned()
        .ok_or_else(|| anyhow!("unknown provider {id}"))?;

    if !force && config.control_url.is_some() {
        if let Err(err) = call_control(state, id, "/v1/control/stop").await {
            warn!(provider_id = %id, error = %err, "graceful stop request failed");
        }
    }

    let deadline = Instant::now() + Duration::from_millis(config.graceful_stop_timeout_ms);
    loop {
        let exited = {
            let mut processes = state.processes.lock().await;
            match processes.get_mut(id) {
                Some(process) => {
                    refresh_process_state(process);
                    process.state == "exited"
                }
                None => true,
            }
        };
        if exited {
            return Ok(json!({"provider_id": id, "status": "stopped"}));
        }
        let timeout_elapsed = Instant::now() >= deadline;
        if should_force_terminate(force, timeout_elapsed, config.force_kill_on_stop_timeout) {
            break;
        }
        if timeout_elapsed {
            return Err(anyhow!(
                "provider {id} did not exit after graceful stop; automatic force termination is disabled"
            ));
        }
        sleep(Duration::from_millis(200)).await;
    }

    let pid = {
        let processes = state.processes.lock().await;
        processes.get(id).map(|p| p.pid)
    };
    if let Some(pid) = pid {
        kill_process_tree(pid).await?;
    }
    let mut processes = state.processes.lock().await;
    if let Some(process) = processes.get_mut(id) {
        process.state = "exited".to_string();
        process.last_exit = Some("terminated by manager".to_string());
    }
    Ok(json!({"provider_id": id, "status": "killed", "pid": pid}))
}

async fn heartbeat_expiry_loop(state: AppState) {
    loop {
        sleep(Duration::from_millis(500)).await;
        let now = Utc::now();
        let mut expired_reports = Vec::new();
        {
            let mut reports = state.reports.lock().await;
            for (provider_id, report) in reports.iter_mut() {
                let timeout_ms = state
                    .configs
                    .get(provider_id)
                    .map(|config| config.heartbeat_timeout_ms)
                    .unwrap_or_else(default_heartbeat_timeout);
                let age_ms = now
                    .signed_duration_since(report.last_seen)
                    .num_milliseconds()
                    .max(0) as u64;
                if !report.expired && age_ms > timeout_ms {
                    report.expired = true;
                    report.ready = false;
                    report.health = "UNHEALTHY".to_string();
                    report.details = merge_manager_status(
                        report.details.clone(),
                        json!({
                            "heartbeat_expired": true,
                            "heartbeat_age_ms": age_ms,
                            "heartbeat_timeout_ms": timeout_ms,
                            "last_error": "provider heartbeat expired"
                        }),
                    );
                    expired_reports.push(report.clone());
                }
            }
        }
        for report in expired_reports {
            warn!(provider_id = %report.provider_id, "provider heartbeat expired");
            if let Err(err) = publish_provider_report(&state, &report).await {
                warn!(provider_id = %report.provider_id, error = %err, "failed to publish heartbeat expiry to Fabric");
            }
        }
        for resource_id in expire_control_authority_state(&state).await {
            if let Err(error) = publish_control_authority_resource(&state, &resource_id).await {
                warn!(resource_id = %resource_id, error = %error, "failed to publish expired advisory authority");
            }
        }
    }
}

fn merge_manager_status(mut details: Value, manager_status: Value) -> Value {
    if !details.is_object() {
        details = json!({"provider_details": details});
    }
    if let Some(object) = details.as_object_mut() {
        object.insert("manager_status".to_string(), manager_status);
    }
    details
}

async fn publish_provider_report(state: &AppState, report: &ProviderReport) -> Result<()> {
    let now_us = Utc::now().timestamp_micros().max(0) as u64;
    let observation = json!({
        "schema": "physical_agent.resource_provider_status",
        "schema_version": 1,
        "stream": format!("providers.{}.status", report.provider_id),
        "provider_id": "resource-provider-manager",
        "provider_instance_id": "manager-local",
        "boot_id": "manager-local",
        "sequence": now_us,
        "observed_at_us": now_us,
        "freshness_ms": 3000,
        "valid": true,
        "data": report
    });
    let response = state
        .http
        .post(format!(
            "{}/v1/observations",
            state.fabric_url.trim_end_matches('/')
        ))
        .json(&observation)
        .timeout(Duration::from_secs(2))
        .send()
        .await?;
    if !response.status().is_success() {
        return Err(anyhow!("Fabric returned {}", response.status()));
    }
    Ok(())
}

async fn publish_capability_binding(
    state: &AppState,
    binding: &CapabilityBindingRecord,
) -> Result<()> {
    let now_us = Utc::now().timestamp_micros().max(0) as u64;
    let observation = json!({
        "schema": "physical_agent.capability_binding",
        "schema_version": 1,
        "stream": "manager.capability_binding",
        "provider_id": "resource-provider-manager",
        "provider_instance_id": "manager-local",
        "boot_id": "manager-local",
        "sequence": now_us,
        "observed_at_us": now_us,
        "freshness_ms": null,
        "valid": true,
        "data": binding
    });
    let response = state
        .http
        .post(format!(
            "{}/v1/observations",
            state.fabric_url.trim_end_matches('/')
        ))
        .json(&observation)
        .timeout(Duration::from_secs(2))
        .send()
        .await?;
    if !response.status().is_success() {
        return Err(anyhow!("Fabric returned {}", response.status()));
    }
    Ok(())
}

async fn publish_control_authority_resource(state: &AppState, resource_id: &str) -> Result<()> {
    let now_us = Utc::now().timestamp_micros().max(0) as u64;
    let view = control_authority_resource_view(state, resource_id).await;
    let stream_suffix: String = resource_id
        .chars()
        .map(|value| {
            if value.is_ascii_alphanumeric() || matches!(value, '.' | '_' | '-') {
                value
            } else {
                '_'
            }
        })
        .collect();
    let observation = json!({
        "schema": "physical_agent.control_authority_state",
        "schema_version": 1,
        "stream": format!("manager.control_authority.{stream_suffix}"),
        "provider_id": "resource-provider-manager",
        "provider_instance_id": "manager-local",
        "boot_id": "manager-local",
        "sequence": now_us,
        "observed_at_us": now_us,
        "freshness_ms": 1500,
        "valid": true,
        "data": view
    });
    let response = state
        .http
        .post(format!(
            "{}/v1/observations",
            state.fabric_url.trim_end_matches('/')
        ))
        .json(&observation)
        .timeout(Duration::from_secs(2))
        .send()
        .await?;
    if !response.status().is_success() {
        return Err(anyhow!("Fabric returned {}", response.status()));
    }
    Ok(())
}

async fn publish_shutdown_plan(state: &AppState, plan: &ShutdownPlanRecord) -> Result<()> {
    let now_us = Utc::now().timestamp_micros().max(0) as u64;
    let observation = json!({
        "schema": "physical_agent.shutdown_plan",
        "schema_version": 1,
        "stream": "manager.shutdown.plan",
        "provider_id": "resource-provider-manager",
        "provider_instance_id": "manager-local",
        "boot_id": "manager-local",
        "sequence": now_us,
        "observed_at_us": now_us,
        "freshness_ms": null,
        "valid": true,
        "data": plan
    });
    let response = state
        .http
        .post(format!(
            "{}/v1/observations",
            state.fabric_url.trim_end_matches('/')
        ))
        .json(&observation)
        .timeout(Duration::from_secs(2))
        .send()
        .await?;
    if !response.status().is_success() {
        return Err(anyhow!("Fabric returned {}", response.status()));
    }
    Ok(())
}

async fn publish_shutdown_execution(
    state: &AppState,
    execution: &ShutdownExecutionRecord,
) -> Result<()> {
    let now_us = Utc::now().timestamp_micros().max(0) as u64;
    let observation = json!({
        "schema": "physical_agent.shutdown_execution",
        "schema_version": 1,
        "stream": "manager.shutdown.execution",
        "provider_id": "resource-provider-manager",
        "provider_instance_id": "manager-local",
        "boot_id": "manager-local",
        "sequence": now_us,
        "observed_at_us": now_us,
        "freshness_ms": null,
        "valid": true,
        "data": execution
    });
    let response = state
        .http
        .post(format!(
            "{}/v1/observations",
            state.fabric_url.trim_end_matches('/')
        ))
        .json(&observation)
        .timeout(Duration::from_secs(2))
        .send()
        .await?;
    if !response.status().is_success() {
        return Err(anyhow!("Fabric returned {}", response.status()));
    }
    Ok(())
}

fn refresh_process_state(process: &mut ManagedProcess) {
    match process.child.try_wait() {
        Ok(Some(status)) => {
            process.state = "exited".to_string();
            process.last_exit = Some(status.to_string());
        }
        Ok(None) => {
            if process.state == "starting" {
                process.state = "running".to_string();
            }
        }
        Err(err) => {
            process.state = "error".to_string();
            process.last_exit = Some(err.to_string());
        }
    }
}

async fn kill_process_tree(pid: u32) -> Result<()> {
    #[cfg(windows)]
    {
        let pid_text = pid.to_string();
        let status = Command::new("taskkill")
            .args(["/PID", pid_text.as_str(), "/T", "/F"])
            .status()
            .await
            .context("running taskkill")?;
        if !status.success() {
            return Err(anyhow!("taskkill failed with {status}"));
        }
        Ok(())
    }

    #[cfg(not(windows))]
    {
        let pid_text = pid.to_string();
        let status = Command::new("kill")
            .args(["-TERM", pid_text.as_str()])
            .status()
            .await
            .context("running kill")?;
        if !status.success() {
            return Err(anyhow!("kill failed with {status}"));
        }
        Ok(())
    }
}

fn expand_vars(input: &str) -> Result<String> {
    let mut output = String::new();
    let chars: Vec<char> = input.chars().collect();
    let mut index = 0;
    while index < chars.len() {
        if chars[index] == '$' && index + 1 < chars.len() && chars[index + 1] == '{' {
            let start = index + 2;
            let mut end = start;
            while end < chars.len() && chars[end] != '}' {
                end += 1;
            }
            if end >= chars.len() {
                return Err(anyhow!("unterminated environment variable in {input}"));
            }
            let key: String = chars[start..end].iter().collect();
            let value =
                env::var(&key).with_context(|| format!("missing environment variable {key}"))?;
            output.push_str(&value);
            index = end + 1;
        } else {
            output.push(chars[index]);
            index += 1;
        }
    }
    Ok(normalize_path_if_needed(output))
}

fn normalize_path_if_needed(value: String) -> String {
    if cfg!(windows)
        && value.contains('/')
        && (value.contains(":/") || FsPath::new(&value).is_absolute())
    {
        PathBuf::from(value).to_string_lossy().to_string()
    } else {
        value
    }
}

fn api_error(status: StatusCode, message: impl Into<String>) -> (StatusCode, Json<Value>) {
    (status, Json(json!({"error": message.into()})))
}

fn internal_error(error: anyhow::Error) -> (StatusCode, Json<Value>) {
    error!(error = %error, "request failed");
    api_error(StatusCode::INTERNAL_SERVER_ERROR, error.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn provider_config_json() -> Value {
        json!({
            "id": "example",
            "display_name": "Example",
            "command": "example.exe"
        })
    }

    fn provider_config(id: &str) -> ProviderConfig {
        let mut value = json!({
            "id": id,
            "display_name": id,
            "command": "example.exe"
        });
        if id.contains("rebot_dm") {
            value["safe_state_request_path"] = json!("/v1/calibration/safe-home");
            value["safe_state_timeout_ms"] = json!(35_000);
        }
        serde_json::from_value(value).expect("provider config should parse")
    }

    fn provider_report(
        provider_id: &str,
        capability: &str,
        capability_ready: bool,
        ready: bool,
        residency: &str,
    ) -> ProviderReport {
        let capability_readiness =
            serde_json::Map::from_iter([(capability.to_string(), json!(capability_ready))]);
        ProviderReport {
            provider_id: provider_id.to_string(),
            instance_id: format!("{provider_id}-instance"),
            boot_id: format!("{provider_id}-boot"),
            residency: residency.to_string(),
            health: "HEALTHY".to_string(),
            ready,
            pid: None,
            details: json!({"capability_readiness": capability_readiness}),
            last_seen: Utc::now(),
            expired: false,
        }
    }

    fn binding_request(capability: &str) -> CapabilityBindingRequest {
        CapabilityBindingRequest {
            required_capabilities: vec![capability.to_string()],
            fallback_provider_ids: HashMap::new(),
            allowed_provider_ids: Vec::new(),
            excluded_provider_ids: Vec::new(),
            request_id: Some("request-1".to_string()),
            related_skill_id: Some("skill-1".to_string()),
        }
    }

    fn test_app_state(
        configs: HashMap<String, ProviderConfig>,
        shutdown_execution_enabled: bool,
    ) -> AppState {
        AppState {
            configs: Arc::new(configs),
            processes: Arc::new(Mutex::new(HashMap::new())),
            reports: Arc::new(Mutex::new(HashMap::new())),
            capability_bindings: Arc::new(Mutex::new(HashMap::new())),
            motion_inhibits: Arc::new(Mutex::new(HashMap::new())),
            control_authority_leases: Arc::new(Mutex::new(HashMap::new())),
            control_authority_generations: Arc::new(Mutex::new(HashMap::new())),
            shutdown_plan: Arc::new(Mutex::new(None)),
            shutdown_execution: Arc::new(Mutex::new(None)),
            shutdown_fence: Arc::new(Mutex::new(None)),
            workcell_calibrations: Arc::new(Mutex::new(HashMap::new())),
            shutdown_execution_enabled,
            review_auth_secret: Arc::new(
                b"test-review-auth-secret-with-at-least-32-bytes".to_vec(),
            ),
            manager_instance_id: "manager-test-instance".to_string(),
            manager_boot_id: "manager-test-boot".to_string(),
            http: reqwest::Client::new(),
            fabric_url: "http://127.0.0.1:9".to_string(),
            agent_ui_url: "http://127.0.0.1:9".to_string(),
            workspace_root: PathBuf::from("."),
            provider_autostart_enabled: false,
            provider_manifests: Arc::new(HashMap::new()),
            skill_manifests: Arc::new(HashMap::new()),
        }
    }

    fn base64url_encode(bytes: &[u8]) -> String {
        const ALPHABET: &[u8; 64] =
            b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
        let mut output = String::new();
        let mut index = 0;
        while index < bytes.len() {
            let first = bytes[index];
            let second = bytes.get(index + 1).copied();
            let third = bytes.get(index + 2).copied();
            output.push(ALPHABET[(first >> 2) as usize] as char);
            output.push(
                ALPHABET[(((first & 0x03) << 4) | second.unwrap_or(0) >> 4) as usize] as char,
            );
            if let Some(second) = second {
                output.push(
                    ALPHABET[(((second & 0x0f) << 2) | third.unwrap_or(0) >> 6) as usize] as char,
                );
            }
            if let Some(third) = third {
                output.push(ALPHABET[(third & 0x3f) as usize] as char);
            }
            index += 3;
        }
        output
    }

    fn activation_fixture(
        now: DateTime<Utc>,
        semantic_status: &str,
    ) -> (
        WorkcellCalibrationActivationRequest,
        HashMap<String, ProviderReport>,
    ) {
        let now_us = now.timestamp_micros() as u64;
        let secret = b"test-review-auth-secret-with-at-least-32-bytes";
        let candidate = json!({
            "schema": "midbrain.skill.stationary_world_arm_alignment.calibration_candidate",
            "schema_version": 3,
            "candidate_id": "alignment-1",
            "workcell_calibration_revision": "alignment-1",
            "created_at_us": now_us - 1_000_000,
            "expires_at_us": now_us + 60_000_000,
            "review_state": "CANDIDATE_REVIEW_REQUIRED",
            "review_mode": "ENFORCED",
            "motion_usable": false,
            "method": {"skill_version": "0.8.5"},
            "frame_contract": {
                "world_frame": "world/stationary_camera/alignment-1",
                "vio_world_frame": "local_vio/epoch-1",
                "camera_frame": "femto_bolt_color_optical_frame",
                "arm_base_frame": "rebot_arm_base",
                "convention_id": "MIDBRAIN_X_FORWARD_Y_LEFT_Z_UP_V2",
                "camera_optical_convention_id": "CAMERA_OPTICAL_X_RIGHT_Y_DOWN_Z_FORWARD_V1",
                "legacy_candidate_compatibility": "REJECT",
                "transform_semantics": "PARENT_FROM_CHILD"
            },
            "confidence": 0.0,
            "bounded_error_estimate": {
                "translation_m": 99.0,
                "rotation_rad": 99.0
            },
            "quality_provenance": {
                "semantic_alignment": {
                    "status": semantic_status,
                    "source": "CURRENT_FOUNDATIONPOSE_VLM_BASE_X_REVIEW",
                    "base_x_relation_to_gripper": "AWAY_FROM_GRIPPER",
                    "selected_base_yaw_flip_deg": 180,
                    "fitted_base_yaw_deg": 180.0,
                    "yaw_correction_translation_norm_m": 0.0,
                    "world_up_available": true,
                    "raw_base_z_dot_world_up": -1.0,
                    "corrected_base_z_dot_world_up": 1.0,
                    "upright_hemisphere_flip_required": true,
                    "selected_orientation_correction_axis": "Y",
                    "selected_orientation_correction_deg": 180,
                    "orientation_correction_count": 1,
                    "orientation_correction_translation_norm_m": 0.0,
                    "orientation_application_origin": "FOUNDATIONPOSE_CENTERED_CAD_MESH_ORIGIN",
                    "orientation_application_order": "parent_from_mesh @ mesh_hypothesis_correction @ mesh_from_semantic",
                    "mesh_hypothesis_correction_translation_norm_m": 0.0,
                    "mesh_center_translation_preserved": true,
                    "semantic_root_translation_adjustment_norm_m": 0.089249989
                }
            },
            "camera_provenance": {
                "provider_id": "camera.femto_bolt",
                "provider_instance_id": "camera.femto_bolt-instance",
                "boot_id": "camera.femto_bolt-boot",
                "route_id": "camera.rgbd.shared_memory.flexible.v1",
                "calibration_revision": "camera-calibration",
                "reference_timestamp_us": now_us - 2_000_000,
                "source_buffer_refs": {}
            },
            "vio_provenance": {
                "provider_id": "localization.local_vio",
                "provider_instance_id": "localization.local_vio-instance",
                "boot_id": "localization.local_vio-boot",
                "world_frame": "local_vio/epoch-1",
                "session_epoch": "epoch-1",
                "reference_timestamp_us": now_us - 100_000
            },
            "transforms": {
                "world_from_camera": {
                    "translation_m": [0.4, 0.5, 0.6],
                    "rotation_xyzw": [0.0, 0.0, 0.0, 1.0]
                },
                "world_from_vio": {
                    "translation_m": [0.0, 0.0, 0.0],
                    "rotation_xyzw": [0.0, 0.0, 0.0, 1.0]
                },
                "world_from_base": {
                    "translation_m": [0.1, 0.2, 0.3],
                    "rotation_xyzw": [0.0, 0.0, 0.0, 1.0]
                }
            }
        });
        let candidate_sha256 = canonical_json_sha256(&candidate);
        let identity_payload = json!({
            "issuer": "test.identity",
            "reviewer_id": "operator@example.test",
            "candidate_id": "alignment-1",
            "candidate_sha256": candidate_sha256,
            "decision": "APPROVE",
            "issued_at_us": now_us - 100_000,
            "expires_at_us": now_us + 30_000_000,
            "nonce": "nonce-1"
        });
        let identity_bytes = serde_json::to_vec(&identity_payload).unwrap();
        let signature = hmac_sha256(secret, &identity_bytes);
        let assertion = format!(
            "{}.{}",
            base64url_encode(&identity_bytes),
            base64url_encode(&signature)
        );
        let review_decision = json!({
            "schema": "midbrain.skill.stationary_world_arm_alignment.candidate_review_decision",
            "schema_version": 1,
            "decision_id": "decision-1",
            "alignment_id": "alignment-1",
            "candidate_id": "alignment-1",
            "candidate_sha256": candidate_sha256,
            "decision": "APPROVE",
            "decision_state": "APPROVED_FOR_ACTIVATION",
            "activation_state": "NOT_ACTIVATED",
            "motion_usable": false,
            "reviewer": {
                "issuer": "test.identity",
                "reviewer_id": "operator@example.test",
                "assurance": "TEST_VERIFIED",
                "assertion_nonce": "nonce-1",
                "assertion_expires_at_us": now_us + 30_000_000
            }
        });
        let request = WorkcellCalibrationActivationRequest {
            request_id: "activate-1".to_string(),
            activated_by: "test-agent".to_string(),
            candidate,
            review_decision,
            review_identity_assertion: assertion,
            duration_ms: 20_000,
        };
        let mut camera_report =
            provider_report("camera.femto_bolt", "camera.rgbd.bundle", true, true, "HOT");
        camera_report.details["calibration_revision"] = json!("camera-calibration");
        let mut vio_report = provider_report(
            "localization.local_vio",
            "localization.vio.tracking_status",
            true,
            true,
            "HOT",
        );
        vio_report.details["session_epoch"] = json!("epoch-1");
        vio_report.details["world_frame"] = json!("local_vio/epoch-1");
        vio_report.details["convention_id"] = json!("MIDBRAIN_X_FORWARD_Y_LEFT_Z_UP_V2");
        vio_report.details["tracking_state"] = json!("TRACKING");
        let reports = HashMap::from([
            ("camera.femto_bolt".to_string(), camera_report),
            ("localization.local_vio".to_string(), vio_report),
        ]);
        (request, reports)
    }

    #[test]
    fn offline_sha256_and_hmac_match_standard_vectors() {
        assert_eq!(
            hex_lower(&sha256(b"")),
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        );
        assert_eq!(
            hex_lower(&hmac_sha256(
                b"key",
                b"The quick brown fox jumps over the lazy dog"
            )),
            "f7bc83f430538424b13298e6aa6fb143ef4d59a14946175997479dbc2d1a3cd8"
        );
        assert_eq!(base64url_decode("SGVsbG8td29ybGQ").unwrap(), b"Hello-world");
    }

    #[test]
    fn canonical_digest_matches_cross_language_precision_vector() {
        let value = json!({
            "z": 0.10911479224498183,
            "a": [-0.49381709129731677, 0.0, 1, true, "世界"]
        });
        assert_eq!(
            canonical_json_sha256(&value),
            "15b524dce44a65210d4047af8258aa6a24e1a39d03ed9d747226f48abaa5ac76"
        );
    }

    #[test]
    fn canonical_digest_matches_problematic_transform_tokens() {
        let value: Value = serde_json::from_str(
            r#"{"world_from_camera":{"translation_m":[-0.011727320462452727,0.0015374757156448454,-0.00846359167984511],"rotation_xyzw":[-0.008771022963335652,-0.20525675842452193,0.9786415876301645,-0.00730583588273101]}}"#,
        )
        .expect("transform vector must be valid JSON");
        assert_eq!(
            canonical_json_sha256(&value),
            "2e4412aebeb711d1661955413a91572446af6ac4a2ecd3807f19dd82ee5f82f9"
        );
    }

    #[test]
    fn reviewed_workcell_activation_requires_current_exact_provenance() {
        let now = Utc::now();
        let (request, reports) = activation_fixture(now, "PASSED");
        let record = build_workcell_activation_record(
            &request,
            "request-digest".to_string(),
            &reports,
            b"test-review-auth-secret-with-at-least-32-bytes",
            now,
        )
        .expect("exact reviewed candidate should activate");
        assert_eq!(record.state, "ACTIVE");
        assert!(record.motion_usable);
        assert_eq!(record.camera_frame, "femto_bolt_color_optical_frame");
        assert_eq!(record.arm_base_frame, "rebot_arm_base");
        assert_eq!(record.convention_id, "MIDBRAIN_X_FORWARD_Y_LEFT_Z_UP_V2");

        let mut changed_calibration_reports = reports.clone();
        changed_calibration_reports
            .get_mut("camera.femto_bolt")
            .expect("camera report must exist")
            .details["calibration_revision"] = json!("different-calibration");
        let error = build_workcell_activation_record(
            &request,
            "request-digest".to_string(),
            &changed_calibration_reports,
            b"test-review-auth-secret-with-at-least-32-bytes",
            now,
        )
        .expect_err("camera calibration change must invalidate the candidate");
        assert!(error.to_string().contains("camera calibration provenance"));

        let mut changed_vio_reports = reports;
        changed_vio_reports
            .get_mut("localization.local_vio")
            .expect("VIO report must exist")
            .details["session_epoch"] = json!("different-epoch");
        let error = build_workcell_activation_record(
            &request,
            "request-digest".to_string(),
            &changed_vio_reports,
            b"test-review-auth-secret-with-at-least-32-bytes",
            now,
        )
        .expect_err("VIO epoch change must invalidate the candidate");
        assert!(error.to_string().contains("current VIO epoch"));
    }

    #[test]
    fn reviewed_workcell_activation_accepts_warning_semantics_without_retired_geometry_gates() {
        let now = Utc::now();
        let (request, reports) = activation_fixture(now, "PASSED_WITH_WARNINGS");
        let record = build_workcell_activation_record(
            &request,
            "request-digest".to_string(),
            &reports,
            b"test-review-auth-secret-with-at-least-32-bytes",
            now,
        )
        .expect("warning-only semantics and retired quality bounds must not block activation");
        assert_eq!(record.state, "ACTIVE");
        assert!(record.motion_usable);
    }

    #[test]
    fn newer_reviewed_workcell_activation_supersedes_the_current_one() {
        let now = Utc::now();
        let (request, reports) = activation_fixture(now, "PASSED");
        let replacement = build_workcell_activation_record(
            &request,
            "replacement-request-digest".to_string(),
            &reports,
            b"test-review-auth-secret-with-at-least-32-bytes",
            now,
        )
        .expect("replacement fixture should activate");
        let mut active = replacement.clone();
        active.activation_id = "active-before-recalibration".to_string();
        active.motion_usable = true;
        let mut expired = replacement.clone();
        expired.activation_id = "expired-before-recalibration".to_string();
        expired.expires_at = now - ChronoDuration::milliseconds(1);
        expired.motion_usable = true;
        let mut records = HashMap::from([
            (active.activation_id.clone(), active),
            (expired.activation_id.clone(), expired),
        ]);

        supersede_active_workcell_calibrations(&mut records, &replacement, now);

        let superseded = records
            .get("active-before-recalibration")
            .expect("the prior active record should remain auditable");
        assert_eq!(superseded.state, "SUPERSEDED");
        assert!(!superseded.motion_usable);
        assert!(superseded
            .last_transition_reason
            .contains(&replacement.activation_id));
        let expired = records
            .get("expired-before-recalibration")
            .expect("the expired record should remain auditable");
        assert_eq!(expired.state, "EXPIRED");
        assert!(!expired.motion_usable);
    }

    #[test]
    fn reviewed_workcell_activation_rejects_continuous_base_yaw_correction() {
        let now = Utc::now();
        let (mut request, reports) = activation_fixture(now, "PASSED");
        request.candidate["quality_provenance"]["semantic_alignment"]
            ["selected_base_yaw_flip_deg"] = json!(34);
        request.candidate["quality_provenance"]["semantic_alignment"]["fitted_base_yaw_deg"] =
            json!(34.0);
        let error = build_workcell_activation_record(
            &request,
            "request-digest".to_string(),
            &reports,
            b"test-review-auth-secret-with-at-least-32-bytes",
            now,
        )
        .expect_err("a continuous post-fit base yaw correction must fail closed");
        assert!(error
            .to_string()
            .contains("exact base-yaw review invariants"));
    }

    #[test]
    fn reviewed_workcell_activation_rejects_direction_flip_mismatch() {
        let now = Utc::now();
        let (mut request, reports) = activation_fixture(now, "PASSED");
        request.candidate["quality_provenance"]["semantic_alignment"]
            ["base_x_relation_to_gripper"] = json!("TOWARD_GRIPPER");
        let error = build_workcell_activation_record(
            &request,
            "request-digest".to_string(),
            &reports,
            b"test-review-auth-secret-with-at-least-32-bytes",
            now,
        )
        .expect_err("the selected yaw must match the reviewed base-X direction");
        assert!(error
            .to_string()
            .contains("exact base-yaw review invariants"));
    }

    #[test]
    fn reviewed_workcell_activation_rejects_translated_yaw_correction() {
        let now = Utc::now();
        let (mut request, reports) = activation_fixture(now, "PASSED");
        request.candidate["quality_provenance"]["semantic_alignment"]
            ["yaw_correction_translation_norm_m"] = json!(0.001);
        let error = build_workcell_activation_record(
            &request,
            "request-digest".to_string(),
            &reports,
            b"test-review-auth-secret-with-at-least-32-bytes",
            now,
        )
        .expect_err("a base-root yaw decision must never translate the base origin");
        assert!(error
            .to_string()
            .contains("exact base-yaw review invariants"));
    }

    #[test]
    fn reviewed_workcell_activation_accepts_one_upside_down_away_hypothesis() {
        let now = Utc::now();
        let (request, reports) = activation_fixture(now, "PASSED_WITH_WARNINGS");

        build_workcell_activation_record(
            &request,
            "request-digest".to_string(),
            &reports,
            b"test-review-auth-secret-with-at-least-32-bytes",
            now,
        )
        .expect("one mesh-centered Y-180 hypothesis must be activation-eligible");
    }

    #[test]
    fn reviewed_workcell_activation_rejects_duplicate_orientation_corrections() {
        let now = Utc::now();
        let (mut request, reports) = activation_fixture(now, "PASSED");
        request.candidate["quality_provenance"]["semantic_alignment"]
            ["orientation_correction_count"] = json!(2);
        let error = build_workcell_activation_record(
            &request,
            "request-digest".to_string(),
            &reports,
            b"test-review-auth-secret-with-at-least-32-bytes",
            now,
        )
        .expect_err("a duplicated orientation correction must fail closed");
        assert!(error
            .to_string()
            .contains("single base-orientation invariants"));
    }

    #[test]
    fn reviewed_workcell_activation_rejects_downward_corrected_base_z() {
        let now = Utc::now();
        let (mut request, reports) = activation_fixture(now, "PASSED");
        request.candidate["quality_provenance"]["semantic_alignment"]
            ["corrected_base_z_dot_world_up"] = json!(-0.998);
        let error = build_workcell_activation_record(
            &request,
            "request-digest".to_string(),
            &reports,
            b"test-review-auth-secret-with-at-least-32-bytes",
            now,
        )
        .expect_err("a downward corrected base +Z must fail closed");
        assert!(error
            .to_string()
            .contains("single base-orientation invariants"));
    }

    #[test]
    fn reviewed_workcell_activation_rejects_moved_mesh_center() {
        let now = Utc::now();
        let (mut request, reports) = activation_fixture(now, "PASSED");
        request.candidate["quality_provenance"]["semantic_alignment"]
            ["mesh_center_translation_preserved"] = json!(false);
        let error = build_workcell_activation_record(
            &request,
            "request-digest".to_string(),
            &reports,
            b"test-review-auth-secret-with-at-least-32-bytes",
            now,
        )
        .expect_err("moving the observed CAD center must fail closed");
        assert!(error
            .to_string()
            .contains("single base-orientation invariants"));
    }

    #[test]
    fn reviewed_workcell_activation_rejects_documented_downward_base_z() {
        let now = Utc::now();
        let (mut request, reports) = activation_fixture(now, "PASSED");
        request.candidate["transforms"]["world_from_base"]["rotation_xyzw"] =
            json!([1.0, 0.0, 0.0, 0.0]);
        let error = build_workcell_activation_record(
            &request,
            "request-digest".to_string(),
            &reports,
            b"test-review-auth-secret-with-at-least-32-bytes",
            now,
        )
        .expect_err("a serialized downward base +Z must fail closed");
        assert!(error.to_string().contains("documented world_from_base +Z"));
    }

    #[test]
    fn provider_config_defaults_to_force_kill_after_timeout() {
        let config: ProviderConfig =
            serde_json::from_value(provider_config_json()).expect("provider config should parse");
        assert!(config.force_kill_on_stop_timeout);
    }

    #[test]
    fn provider_config_can_disable_automatic_force_kill() {
        let mut value = provider_config_json();
        value["force_kill_on_stop_timeout"] = json!(false);
        let config: ProviderConfig =
            serde_json::from_value(value).expect("provider config should parse");
        assert!(!config.force_kill_on_stop_timeout);
    }

    #[test]
    fn automatic_force_kill_policy_preserves_safety_critical_processes() {
        assert!(!should_force_terminate(false, false, true));
        assert!(should_force_terminate(false, true, true));
        assert!(!should_force_terminate(false, true, false));
        assert!(should_force_terminate(true, false, false));
    }

    #[test]
    fn fresh_external_provider_report_is_adoptable() {
        let now = Utc::now();
        let mut report = provider_report("camera.external", "camera.rgb", true, true, "HOT");
        report.last_seen = now - ChronoDuration::milliseconds(900);

        assert!(provider_report_is_fresh(&report, 1_000, now));
        report.expired = true;
        assert!(!provider_report_is_fresh(&report, 1_000, now));
    }

    #[test]
    fn live_provider_identity_cannot_be_silently_replaced() {
        let now = Utc::now();
        let mut report = provider_report("camera.external", "camera.rgb", true, true, "HOT");
        report.last_seen = now;

        assert!(!provider_identity_conflicts(
            &report,
            &report.instance_id,
            &report.boot_id,
            1_000,
            now,
        ));
        assert!(provider_identity_conflicts(
            &report,
            "duplicate-instance",
            "duplicate-boot",
            1_000,
            now,
        ));
        report.last_seen = now - ChronoDuration::milliseconds(1_001);
        assert!(!provider_identity_conflicts(
            &report,
            "replacement-after-expiry",
            "replacement-boot",
            1_000,
            now,
        ));
    }

    #[test]
    fn advisory_binding_selects_an_available_provider_deterministically() {
        let configs = HashMap::from([
            ("provider.z".to_string(), provider_config("provider.z")),
            ("provider.a".to_string(), provider_config("provider.a")),
        ]);
        let reports = HashMap::from([
            (
                "provider.z".to_string(),
                provider_report("provider.z", "camera.rgb", true, true, "HOT"),
            ),
            (
                "provider.a".to_string(),
                provider_report("provider.a", "camera.rgb", true, true, "HOT"),
            ),
        ]);

        let binding = build_capability_binding(&configs, &reports, binding_request("camera.rgb"))
            .expect("binding should resolve");

        assert_eq!(binding.enforcement, "ADVISORY");
        assert_eq!(binding.status, "RESOLVED");
        assert_eq!(binding.validity, "CURRENT");
        assert!(binding.validation_issues.is_empty());
        assert_eq!(binding.selections[0].provider_id, "provider.a");
        assert_eq!(
            binding.selections[0].selection_reason,
            "AVAILABLE_CAPABILITY"
        );
        assert!(binding.selections[0].compatibility_verified);
        assert!(!binding.selections[0].requires_activation);
    }

    #[test]
    fn advisory_binding_uses_explicit_provider_only_as_fallback() {
        let configs = HashMap::from([
            (
                "provider.live".to_string(),
                provider_config("provider.live"),
            ),
            (
                "provider.explicit".to_string(),
                provider_config("provider.explicit"),
            ),
        ]);
        let reports = HashMap::from([(
            "provider.live".to_string(),
            provider_report("provider.live", "camera.rgb", true, true, "HOT"),
        )]);
        let mut request = binding_request("camera.rgb");
        request
            .fallback_provider_ids
            .insert("camera.rgb".to_string(), "provider.explicit".to_string());

        let binding = build_capability_binding(&configs, &reports, request)
            .expect("available provider should win");

        assert_eq!(binding.selections[0].provider_id, "provider.live");
        assert_eq!(
            binding.selections[0].selection_reason,
            "AVAILABLE_CAPABILITY"
        );
    }

    #[test]
    fn advisory_binding_preserves_cold_explicit_provider_fallback() {
        let configs = HashMap::from([(
            "camera.femto_bolt".to_string(),
            provider_config("camera.femto_bolt"),
        )]);
        let mut request = binding_request("camera.rgb");
        request
            .fallback_provider_ids
            .insert("camera.rgb".to_string(), "camera.femto_bolt".to_string());

        let binding = build_capability_binding(&configs, &HashMap::new(), request)
            .expect("explicit fallback should resolve");

        assert_eq!(binding.status, "RESOLVED");
        assert_eq!(binding.validity, "FALLBACK_REQUIRES_ACTIVATION");
        assert_eq!(binding.selections[0].provider_id, "camera.femto_bolt");
        assert_eq!(
            binding.selections[0].selection_reason,
            "EXPLICIT_PROVIDER_FALLBACK"
        );
        assert!(!binding.selections[0].compatibility_verified);
        assert!(binding.selections[0].requires_activation);
    }

    #[test]
    fn advisory_binding_promotes_activated_explicit_fallback_to_current() {
        let configs = HashMap::from([(
            "camera.femto_bolt".to_string(),
            provider_config("camera.femto_bolt"),
        )]);
        let reports = HashMap::from([(
            "camera.femto_bolt".to_string(),
            provider_report("camera.femto_bolt", "camera.rgb", true, true, "HOT"),
        )]);
        let mut request = binding_request("camera.rgb");
        request
            .fallback_provider_ids
            .insert("camera.rgb".to_string(), "camera.femto_bolt".to_string());

        let binding = build_capability_binding(&configs, &reports, request)
            .expect("activated fallback provider should become a current binding");

        assert_eq!(binding.status, "RESOLVED");
        assert_eq!(binding.validity, "CURRENT");
        assert_eq!(binding.selections[0].provider_id, "camera.femto_bolt");
        assert_eq!(
            binding.selections[0].selection_reason,
            "AVAILABLE_CAPABILITY"
        );
        assert!(binding.selections[0].compatibility_verified);
        assert!(!binding.selections[0].requires_activation);
    }

    #[test]
    fn binding_revalidation_exposes_provider_restart() {
        let configs = HashMap::from([("provider.a".to_string(), provider_config("provider.a"))]);
        let reports = HashMap::from([(
            "provider.a".to_string(),
            provider_report("provider.a", "camera.rgb", true, true, "HOT"),
        )]);
        let binding = build_capability_binding(&configs, &reports, binding_request("camera.rgb"))
            .expect("binding should resolve");
        assert_eq!(binding.validity, "CURRENT");

        let mut restarted_reports = reports;
        restarted_reports
            .get_mut("provider.a")
            .expect("provider report should exist")
            .boot_id = "provider.a-new-boot".to_string();
        let binding = refresh_binding_validity(binding, &restarted_reports);

        assert_eq!(binding.validity, "STALE_PROVIDER_RESTARTED");
        assert!(binding.validation_issues[0].starts_with("PROVIDER_RESTARTED:"));
    }

    #[test]
    fn advisory_binding_reports_unresolved_without_an_available_or_fallback_provider() {
        let configs = HashMap::from([(
            "provider.cold".to_string(),
            provider_config("provider.cold"),
        )]);
        let reports = HashMap::from([(
            "provider.cold".to_string(),
            provider_report("provider.cold", "camera.rgb", true, false, "WARM"),
        )]);

        let binding = build_capability_binding(&configs, &reports, binding_request("camera.rgb"))
            .expect("unresolved is a valid advisory result");

        assert_eq!(binding.status, "UNRESOLVED");
        assert!(binding.selections.is_empty());
        assert_eq!(binding.unresolved_capabilities, vec!["camera.rgb"]);
    }

    #[test]
    fn advisory_authority_duration_requires_a_renewal_window() {
        assert!(validate_authority_duration(6_000, 1_000).is_ok());
        assert!(validate_authority_duration(499, 100).is_err());
        assert!(validate_authority_duration(6_000, 6_000).is_err());
    }

    #[test]
    fn advisory_authority_expiry_preserves_history_and_fencing_generation() {
        let now = Utc::now();
        let lease = ControlAuthorityLease {
            lease_id: "lease-1".to_string(),
            resource_id: "robot_arm.primary".to_string(),
            owner_id: "skill-1".to_string(),
            permissions: vec!["plan".to_string(), "execute".to_string()],
            issued_at: now - ChronoDuration::seconds(10),
            expires_at: now - ChronoDuration::seconds(1),
            renewal_interval_ms: 1_000,
            fencing_generation: 7,
            preemption_policy: "DENY_UNLESS_EXPLICIT_PREEMPT".to_string(),
            safe_relinquish: "PROVIDER_LOCAL_POLICY".to_string(),
            state: "ACTIVE".to_string(),
            related_skill_id: Some("skill-1".to_string()),
            last_transition_reason: "issued".to_string(),
        };
        let mut leases = HashMap::from([(lease.lease_id.clone(), lease)]);

        let changed = expire_control_authority_locked(&mut leases, now);

        assert!(changed.contains("robot_arm.primary"));
        let expired = leases.get("lease-1").expect("lease remains inspectable");
        assert_eq!(expired.state, "EXPIRED");
        assert_eq!(expired.fencing_generation, 7);
    }

    #[test]
    fn shutdown_shadow_plan_preserves_safety_order_and_stops_manager_last() {
        let configs = HashMap::from([
            (
                "robot_arm.integrated".to_string(),
                provider_config("robot_arm.integrated"),
            ),
            (
                "robot_arm.rebot_dm".to_string(),
                provider_config("robot_arm.rebot_dm"),
            ),
            (
                "camera.femto_bolt".to_string(),
                provider_config("camera.femto_bolt"),
            ),
        ]);
        let mut basic_report = provider_report(
            "robot_arm.rebot_dm",
            "robot_arm.gravity_float",
            true,
            true,
            "HOT",
        );
        basic_report.details["capability_readiness"]["robot.motion.arm.basic.command"] =
            json!(true);
        let reports = HashMap::from([
            (
                "robot_arm.integrated".to_string(),
                provider_report(
                    "robot_arm.integrated",
                    "robot.motion.arm.integrated.plan.direct.nonphysical",
                    true,
                    true,
                    "HOT",
                ),
            ),
            ("robot_arm.rebot_dm".to_string(), basic_report),
        ]);

        let plan = build_shutdown_plan(
            &configs,
            &reports,
            &HashSet::from([
                "robot_arm.integrated".to_string(),
                "robot_arm.rebot_dm".to_string(),
            ]),
            "request-1".to_string(),
            "test-agent".to_string(),
            "test".to_string(),
        );

        assert_eq!(plan.enforcement, "SHADOW_DRY_RUN");
        assert_eq!(plan.state, "PLANNED");
        assert_eq!(plan.request_id, "request-1");
        assert_eq!(
            plan.steps[1].action,
            "REQUEST_MOTION_PROVIDERS_SAFE_RELINQUISH"
        );
        assert_eq!(
            plan.steps[1].provider_ids,
            vec!["robot_arm.integrated".to_string()]
        );
        assert_eq!(plan.steps[2].action, "CONFIRM_BASIC_SAFE_STATE");
        assert_eq!(
            plan.steps[2].provider_ids,
            vec!["robot_arm.rebot_dm".to_string()]
        );
        assert_eq!(
            plan.steps[4].provider_ids,
            vec!["robot_arm.rebot_dm".to_string()]
        );
        assert_eq!(plan.steps[5].action, "STOP_FABRIC_AFTER_AUDIT_FLUSH");
        assert_eq!(plan.steps[6].action, "SUPERVISOR_STOPS_MANAGER_LAST");
    }

    #[test]
    fn shutdown_shadow_plan_blocks_on_unobserved_motion_provider() {
        let configs = HashMap::from([(
            "robot_arm.integrated".to_string(),
            provider_config("robot_arm.integrated"),
        )]);

        let plan = build_shutdown_plan(
            &configs,
            &HashMap::new(),
            &HashSet::from(["robot_arm.integrated".to_string()]),
            "request-1".to_string(),
            "test-agent".to_string(),
            "test".to_string(),
        );

        assert_eq!(plan.state, "PLANNED_WITH_BLOCKERS");
        assert_eq!(plan.blockers.len(), 1);
        assert!(plan.blockers[0].contains("no current heartbeat"));
    }

    #[test]
    fn shutdown_plan_blocks_basic_without_safe_state_route() {
        let mut basic = provider_config("robot_arm.rebot_dm");
        basic.safe_state_request_path = None;
        let configs = HashMap::from([("robot_arm.rebot_dm".to_string(), basic)]);
        let reports = HashMap::from([(
            "robot_arm.rebot_dm".to_string(),
            provider_report(
                "robot_arm.rebot_dm",
                "robot_arm.gravity_float",
                true,
                true,
                "HOT",
            ),
        )]);

        let plan = build_shutdown_plan(
            &configs,
            &reports,
            &HashSet::from(["robot_arm.rebot_dm".to_string()]),
            "request-1".to_string(),
            "test-agent".to_string(),
            "test".to_string(),
        );

        assert_eq!(plan.state, "PLANNED_WITH_BLOCKERS");
        assert!(plan
            .blockers
            .iter()
            .any(|value| value.contains("safe-state request path")));
    }

    #[tokio::test]
    async fn shutdown_execution_is_disabled_by_default() {
        let state = test_app_state(HashMap::new(), false);
        let plan = build_shutdown_plan(
            &state.configs,
            &HashMap::new(),
            &HashSet::new(),
            "plan-request".to_string(),
            "test-agent".to_string(),
            "test".to_string(),
        );
        *state.shutdown_plan.lock().await = Some(plan.clone());

        let result = execute_shutdown_plan(
            State(state),
            Path(plan.shutdown_id),
            Json(ShutdownExecuteRequest {
                request_id: "execute-request".to_string(),
                confirmation: "EXECUTE_MANAGER_PROVIDER_SHUTDOWN".to_string(),
            }),
        )
        .await;

        assert_eq!(
            result.expect_err("execution must remain gated").0,
            StatusCode::CONFLICT
        );
    }

    #[tokio::test]
    async fn empty_shutdown_execution_reaches_supervisor_gate_and_fences_new_authority() {
        let state = test_app_state(HashMap::new(), true);
        let plan = build_shutdown_plan(
            &state.configs,
            &HashMap::new(),
            &HashSet::new(),
            "plan-request".to_string(),
            "test-agent".to_string(),
            "test".to_string(),
        );
        *state.shutdown_plan.lock().await = Some(plan.clone());

        let (status, Json(accepted)) = execute_shutdown_plan(
            State(state.clone()),
            Path(plan.shutdown_id.clone()),
            Json(ShutdownExecuteRequest {
                request_id: "execute-request".to_string(),
                confirmation: "EXECUTE_MANAGER_PROVIDER_SHUTDOWN".to_string(),
            }),
        )
        .await
        .expect("execution should be accepted");
        assert_eq!(status, StatusCode::ACCEPTED);
        assert_eq!(accepted.state, "ACCEPTED");
        assert_eq!(
            state.shutdown_fence.lock().await.as_deref(),
            Some(plan.shutdown_id.as_str())
        );
        assert!(reject_if_shutdown_fenced(&state, "test acquisition")
            .await
            .is_err());

        let deadline = Instant::now() + Duration::from_secs(1);
        let completed = loop {
            let snapshot = state
                .shutdown_execution
                .lock()
                .await
                .clone()
                .expect("execution should exist");
            if snapshot.state != "ACCEPTED" && snapshot.state != "RUNNING" {
                break snapshot;
            }
            assert!(
                Instant::now() < deadline,
                "shutdown execution did not finish within one second"
            );
            sleep(Duration::from_millis(10)).await;
        };
        assert_eq!(completed.state, "AWAITING_SUPERVISOR");
        assert_eq!(completed.step_results.len(), 7);
        assert!(completed.failures.is_empty());
    }
}
