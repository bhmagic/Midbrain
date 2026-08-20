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
    #[serde(default)]
    dependencies: Vec<String>,
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

#[derive(Debug, Serialize)]
struct AgentProviderView {
    provider_id: String,
    display_name: String,
    dependencies: Vec<String>,
    process_state: String,
    instance_id: Option<String>,
    boot_id: Option<String>,
    residency: Option<String>,
    health: Option<String>,
    ready: bool,
    expired: bool,
    last_seen: Option<DateTime<Utc>>,
    last_error: Option<Value>,
    manager_error: Option<Value>,
    blocking_prerequisite: Option<Value>,
    detail_schema: &'static str,
    detail_sections: Vec<&'static str>,
}

#[derive(Debug, Serialize)]
struct AgentCapabilityView {
    capability: String,
    provider_id: String,
    provider_instance_id: Option<String>,
    available: bool,
}

#[derive(Debug, Serialize)]
struct AgentRuntimeCatalog {
    schema: &'static str,
    schema_version: u32,
    providers: Vec<AgentProviderView>,
    capabilities: Vec<AgentCapabilityView>,
}

#[derive(Debug, Clone, Deserialize)]
struct CapabilityBindingRequest {
    required_capabilities: Vec<String>,
    #[serde(default)]
    required_resources_by_capability: HashMap<String, String>,
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
    required_resource_id: Option<String>,
    resource_compatible: bool,
}

#[derive(Debug, Clone, Serialize)]
struct CapabilityBindingSelection {
    capability: String,
    required_resource_id: Option<String>,
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
    active_overlapping_leases: Vec<ControlAuthorityLease>,
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
}

#[derive(Debug, Clone, Deserialize)]
struct WorkcellCalibrationRevocationRequest {
    request_id: String,
    revoked_by: String,
    reason: String,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
struct WorkcellTranslationRefinementRequest {
    request_id: String,
    updated_by: String,
    activation_id: String,
    expected_refinement_revision: u64,
    source_world_from_base: Value,
    proposed_world_from_base: Value,
    refinement: Value,
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
    expires_at: Option<DateTime<Utc>>,
    expires_at_us: Option<u64>,
    validity_policy: String,
    invalidation_conditions: Vec<String>,
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
    camera_canonical_device_id: String,
    camera_provider_instance_id: String,
    camera_boot_id: String,
    camera_calibration_revision: String,
    vio_provider_id: String,
    vio_provider_instance_id: String,
    vio_boot_id: String,
    transforms: Value,
    reviewer: Value,
    translation_refinement_revision: u64,
    last_translation_refinement: Option<Value>,
    translation_refinement_journal: Vec<Value>,
    last_transition_reason: String,
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
    assembly_selection_lock: Arc<Mutex<()>>,
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
        assembly_selection_lock: Arc::new(Mutex::new(())),
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
        .route(
            "/v1/ui/robot-assembly/arms",
            get(ui::arm_profiles).post(ui::select_arm),
        )
        .route(
            "/v1/ui/robot-assembly/effectors",
            get(ui::effector_profiles).post(ui::select_effector),
        )
        .route("/v1/ui/providers/:id", get(ui::provider_detail))
        .route("/v1/ui/skills/:id", get(ui::skill_detail))
        .route(
            "/v1/ui/developer/:kind/:id/activate",
            post(ui::activate_developer_surface),
        )
        .route("/v1/ui/shutdown", post(ui::shutdown_midbrain))
        .route("/health", get(health))
        .route("/v1/providers", get(list_providers))
        .route("/v1/agent-runtime-catalog", get(agent_runtime_catalog))
        .route("/v1/providers/:id/detail", get(get_provider_detail))
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
            "/v1/workcell-calibrations/refine-translation",
            post(refine_workcell_calibration_translation),
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
            "compact_workcell_translation_refinement",
            "workcell_calibration_revocation"
        ]
    }))
}

async fn list_workcell_calibrations(State(state): State<AppState>) -> Json<Value> {
    let reports = state.reports.lock().await.clone();
    let mut records = state.workcell_calibrations.lock().await;
    for record in records.values_mut() {
        refresh_workcell_motion_usability(record, &reports);
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

fn refresh_workcell_motion_usability(
    record: &mut WorkcellCalibrationActivationRecord,
    reports: &HashMap<String, ProviderReport>,
) {
    if record.state != "ACTIVE" {
        record.motion_usable = false;
        return;
    }
    let Some(camera) = reports.get(&record.camera_provider_id) else {
        record.motion_usable = false;
        record.last_transition_reason =
            "motion suspended: camera provider report is unavailable".to_string();
        return;
    };
    let current_camera_canonical_device_id = camera_report_canonical_device_id(camera);
    if current_camera_canonical_device_id
        .as_deref()
        .is_some_and(|value| value != record.camera_canonical_device_id)
    {
        record.state = "INVALIDATED".to_string();
        record.motion_usable = false;
        record.last_transition_reason =
            "activation invalidated: mounted camera canonical device changed".to_string();
        return;
    }
    if camera.details["calibration_revision"] != record.camera_calibration_revision {
        record.state = "INVALIDATED".to_string();
        record.motion_usable = false;
        record.last_transition_reason =
            "activation invalidated: mounted camera calibration revision changed".to_string();
        return;
    }
    if camera.expired
        || !camera.ready
        || camera.health != "HEALTHY"
        || current_camera_canonical_device_id.is_none()
    {
        record.motion_usable = false;
        record.last_transition_reason =
            "motion suspended: mounted camera health or stable identity evidence is insufficient"
                .to_string();
        return;
    }
    record.motion_usable = true;
    record.last_transition_reason =
        "mounted calibration remains usable under canonical camera identity and calibration evidence"
            .to_string();
}

fn camera_report_canonical_device_id(report: &ProviderReport) -> Option<String> {
    report
        .details
        .get("canonical_device_id")
        .and_then(Value::as_str)
        .or_else(|| {
            report
                .details
                .get("accelerometer_calibration")
                .and_then(|value| value.get("canonical_device_id"))
                .and_then(Value::as_str)
        })
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(str::to_string)
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
    let superseded = {
        let records = state.workcell_calibrations.lock().await;
        superseded_active_workcell_calibrations(&records, &record)
    };
    publish_workcell_calibration_supersession(&state, &record, &superseded)
        .await
        .map_err(|error| {
            api_error(
                StatusCode::BAD_GATEWAY,
                format!(
                    "workcell calibration was not activated because the Fabric supersession publication failed: {error}"
                ),
            )
        })?;
    let mut records = state.workcell_calibrations.lock().await;
    for superseded_record in superseded {
        records.insert(superseded_record.activation_id.clone(), superseded_record);
    }
    records.insert(record.activation_id.clone(), record.clone());
    Ok((StatusCode::CREATED, Json(record)))
}

async fn refine_workcell_calibration_translation(
    State(state): State<AppState>,
    Json(request): Json<WorkcellTranslationRefinementRequest>,
) -> Result<Json<WorkcellCalibrationActivationRecord>, (StatusCode, Json<Value>)> {
    if request.request_id.trim().is_empty()
        || request.updated_by.trim().is_empty()
        || request.activation_id.trim().is_empty()
    {
        return Err(api_error(
            StatusCode::BAD_REQUEST,
            "request_id, updated_by, and activation_id are required",
        ));
    }
    let request_sha256 = canonical_json_sha256(
        &serde_json::to_value(&request)
            .map_err(|error| api_error(StatusCode::BAD_REQUEST, error.to_string()))?,
    );
    let reports = state.reports.lock().await.clone();
    let mut records = state.workcell_calibrations.lock().await;
    let record = records
        .get_mut(request.activation_id.trim())
        .ok_or_else(|| {
            api_error(
                StatusCode::NOT_FOUND,
                "workcell calibration activation does not exist",
            )
        })?;
    if let Some(existing) = record
        .translation_refinement_journal
        .iter()
        .find(|entry| entry["request_id"] == request.request_id.trim())
    {
        if existing["request_sha256"] != request_sha256 {
            return Err(api_error(
                StatusCode::CONFLICT,
                "translation-refinement request_id was reused for different content",
            ));
        }
        return Ok(Json(record.clone()));
    }
    refresh_workcell_motion_usability(record, &reports);
    let updated =
        build_translation_refined_record(record, &request, &request_sha256, &reports, Utc::now())
            .map_err(|error| api_error(StatusCode::CONFLICT, error.to_string()))?;
    publish_workcell_calibration(&state, &updated, true)
        .await
        .map_err(|error| {
            api_error(
                StatusCode::BAD_GATEWAY,
                format!(
                    "translation refinement was not activated because Fabric publication failed: {error}"
                ),
            )
        })?;
    *record = updated.clone();
    Ok(Json(updated))
}

fn build_translation_refined_record(
    current: &WorkcellCalibrationActivationRecord,
    request: &WorkcellTranslationRefinementRequest,
    request_sha256: &str,
    reports: &HashMap<String, ProviderReport>,
    now: DateTime<Utc>,
) -> Result<WorkcellCalibrationActivationRecord> {
    if current.state != "ACTIVE" || current.enforcement != "ENFORCED" || !current.motion_usable {
        return Err(anyhow!(
            "translation refinement requires one enforced motion-usable active calibration"
        ));
    }
    if request.expected_refinement_revision != current.translation_refinement_revision {
        return Err(anyhow!(
            "translation refinement expected revision {} but active revision is {}",
            request.expected_refinement_revision,
            current.translation_refinement_revision
        ));
    }
    if request.source_world_from_base != current.transforms["world_from_base"] {
        return Err(anyhow!(
            "translation refinement source world_from_base is stale"
        ));
    }
    validate_transform_payload(
        &request.source_world_from_base,
        "translation_refinement.source_world_from_base",
    )?;
    validate_transform_payload(
        &request.proposed_world_from_base,
        "translation_refinement.proposed_world_from_base",
    )?;
    if request.source_world_from_base["rotation_xyzw"]
        != request.proposed_world_from_base["rotation_xyzw"]
    {
        return Err(anyhow!(
            "translation refinement attempted to change active rotation"
        ));
    }
    // The dedicated Skill owns observation quality, capture-motion, VLM-review,
    // and adoption policy. Reaching this endpoint is the Skill's apply decision;
    // Manager only protects the authoritative state transition below.
    let refinement = &request.refinement;
    let identities = &refinement["identities"];
    let identity_matches = [
        ("world_frame", current.world_frame.as_str()),
        ("vio_session_epoch", current.session_epoch.as_str()),
        ("spatial_convention", current.convention_id.as_str()),
        ("camera_provider_id", current.camera_provider_id.as_str()),
        (
            "camera_provider_instance_id",
            current.camera_provider_instance_id.as_str(),
        ),
        ("camera_boot_id", current.camera_boot_id.as_str()),
        (
            "camera_calibration_revision",
            current.camera_calibration_revision.as_str(),
        ),
    ];
    for (field, expected) in identity_matches {
        if identities[field].as_str() != Some(expected) {
            return Err(anyhow!(
                "translation refinement identity {field} does not match the active calibration"
            ));
        }
    }
    let arm_provider_id = require_json_string(
        identities,
        "arm_provider_id",
        "translation_refinement.identities",
    )?;
    let arm_provider_instance_id = require_json_string(
        identities,
        "arm_provider_instance_id",
        "translation_refinement.identities",
    )?;
    let arm_boot_id = require_json_string(
        identities,
        "arm_boot_id",
        "translation_refinement.identities",
    )?;
    let arm_report = reports
        .get(&arm_provider_id)
        .ok_or_else(|| anyhow!("translation refinement arm Provider report is unavailable"))?;
    if arm_report.instance_id != arm_provider_instance_id
        || arm_report.boot_id != arm_boot_id
        || arm_report.expired
        || !arm_report.ready
        || arm_report.health != "HEALTHY"
    {
        return Err(anyhow!(
            "translation refinement arm Provider identity or health changed"
        ));
    }
    let source_translation = finite_json_array(
        &request.source_world_from_base["translation_m"],
        3,
        "translation_refinement.source_world_from_base.translation_m",
    )?;
    let proposed_translation = finite_json_array(
        &request.proposed_world_from_base["translation_m"],
        3,
        "translation_refinement.proposed_world_from_base.translation_m",
    )?;
    let adopted_delta = finite_json_array(
        &refinement["adopted_translation_delta_m"],
        3,
        "translation_refinement.adopted_translation_delta_m",
    )?;
    for index in 0..3 {
        let expected = source_translation[index] + adopted_delta[index];
        if (proposed_translation[index] - expected).abs() > 1e-9 {
            return Err(anyhow!(
                "translation refinement proposed translation does not match its adopted delta"
            ));
        }
    }
    let next_revision = current.translation_refinement_revision + 1;
    let next_calibration_revision = format!(
        "{}:translation-refinement:{}",
        current.candidate_id, next_revision
    );
    let journal_entry = json!({
        "request_id": request.request_id.trim(),
        "request_sha256": request_sha256,
        "updated_by": request.updated_by.trim(),
        "updated_at_us": now.timestamp_micros().max(0) as u64,
        "revision_before": current.translation_refinement_revision,
        "revision_after": next_revision,
        "calibration_revision_before": current.calibration_revision,
        "calibration_revision_after": next_calibration_revision,
        "previous_translation_m": source_translation,
        "translation_m": proposed_translation,
        "adopted_translation_delta_m": adopted_delta,
        "raw_translation_delta_norm_m": refinement["raw_translation_delta_norm_m"],
        "adoption_factor": refinement["adoption_factor"],
        "landmark_id": refinement["landmark_id"],
        "visual_evidence_id": refinement["visual_evidence"]["evidence_id"],
        "quality_review_verdict": refinement["quality_review"]["verdict"],
        "parent_state_link": null,
    });
    let mut updated = current.clone();
    updated.transforms["world_from_base"] = request.proposed_world_from_base.clone();
    updated.translation_refinement_revision = next_revision;
    updated.calibration_revision = next_calibration_revision;
    updated.last_translation_refinement = Some(journal_entry.clone());
    updated.translation_refinement_journal.push(journal_entry);
    if updated.translation_refinement_journal.len() > 32 {
        let excess = updated.translation_refinement_journal.len() - 32;
        updated.translation_refinement_journal.drain(0..excess);
    }
    updated.last_transition_reason = format!(
        "XYZ-only translation refinement revision {} activated with locked rotation",
        next_revision
    );
    Ok(updated)
}

fn finite_json_array(value: &Value, length: usize, scope: &str) -> Result<Vec<f64>> {
    let items = value
        .as_array()
        .ok_or_else(|| anyhow!("{scope} must be an array"))?;
    if items.len() != length {
        return Err(anyhow!("{scope} must contain {length} values"));
    }
    let values = items
        .iter()
        .map(|item| {
            item.as_f64()
                .filter(|value| value.is_finite())
                .ok_or_else(|| anyhow!("{scope} contains a non-finite number"))
        })
        .collect::<Result<Vec<_>>>()?;
    Ok(values)
}

fn superseded_active_workcell_calibrations(
    records: &HashMap<String, WorkcellCalibrationActivationRecord>,
    replacement: &WorkcellCalibrationActivationRecord,
) -> Vec<WorkcellCalibrationActivationRecord> {
    records
        .values()
        .filter(|record| record.state == "ACTIVE")
        .cloned()
        .map(|mut record| {
            record.motion_usable = false;
            record.state = "SUPERSEDED".to_string();
            record.last_transition_reason = format!(
                "superseded by newer reviewed activation {}",
                replacement.activation_id
            );
            record
        })
        .collect()
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

fn validate_bounded_vlm_selection(
    selected_id: &str,
    confidence: f64,
    minimum_confidence: f64,
    minimum_consensus_confidence: f64,
    decision_basis: &str,
    attempts: &Value,
    scope: &str,
) -> Result<()> {
    if !confidence.is_finite()
        || !minimum_confidence.is_finite()
        || !minimum_consensus_confidence.is_finite()
        || !(0.0..=1.0).contains(&confidence)
        || !(0.0..=1.0).contains(&minimum_confidence)
        || !(0.0..=minimum_confidence).contains(&minimum_consensus_confidence)
    {
        return Err(anyhow!("{scope} confidence policy is invalid"));
    }
    let attempt_values = attempts
        .as_array()
        .ok_or_else(|| anyhow!("{scope} attempts are required"))?;
    if attempt_values.is_empty() {
        return Err(anyhow!("{scope} attempts cannot be empty"));
    }
    let selected_attempt_exists = attempt_values.iter().any(|attempt| {
        attempt["candidate_id"].as_str() == Some(selected_id)
            && attempt["confidence"]
                .as_f64()
                .is_some_and(|value| value.is_finite() && (0.0..=1.0).contains(&value))
    });
    if !selected_attempt_exists {
        return Err(anyhow!(
            "{scope} selected candidate is absent from its attempts"
        ));
    }
    let accepted = match decision_basis {
        "FIRST_ATTEMPT_CONFIDENCE" | "RETRY_CONFIDENCE" => confidence >= minimum_confidence,
        "REPEATED_CANDIDATE_CONSENSUS" => {
            attempt_values.len() >= 2
                && confidence >= minimum_consensus_confidence
                && attempt_values.iter().all(|attempt| {
                    attempt["candidate_id"].as_str() == Some(selected_id)
                        && attempt["confidence"].as_f64().is_some_and(|value| {
                            value.is_finite()
                                && value >= minimum_consensus_confidence
                                && value <= 1.0
                        })
                })
        }
        "QUALIFIED_MAJORITY_CANDIDATE_CONSENSUS" => {
            let mut qualified_counts = HashMap::<String, usize>::new();
            for attempt in attempt_values {
                let Some(candidate_id) = attempt["candidate_id"].as_str() else {
                    continue;
                };
                let Some(attempt_confidence) = attempt["confidence"].as_f64() else {
                    continue;
                };
                if attempt_confidence.is_finite()
                    && attempt_confidence >= minimum_consensus_confidence
                    && attempt_confidence <= 1.0
                {
                    *qualified_counts
                        .entry(candidate_id.to_string())
                        .or_default() += 1;
                }
            }
            let selected_count = qualified_counts.get(selected_id).copied().unwrap_or(0);
            let runner_up_count = qualified_counts
                .iter()
                .filter(|(candidate_id, _)| candidate_id.as_str() != selected_id)
                .map(|(_, count)| *count)
                .max()
                .unwrap_or(0);
            confidence >= minimum_consensus_confidence
                && selected_count >= 2
                && selected_count > runner_up_count
        }
        _ => false,
    };
    if !accepted {
        return Err(anyhow!(
            "{scope} confidence or consensus proof is insufficient"
        ));
    }
    Ok(())
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
    let candidate = request
        .candidate
        .as_object()
        .ok_or_else(|| anyhow!("candidate must be an object"))?;
    if request.candidate["schema"] != "midbrain.skill.locate_arm_base.calibration_candidate"
        || request.candidate["schema_version"] != 1
        || request.candidate["review_state"] != "PENDING_REVIEW"
        || request.candidate["motion_usable"] != false
        || request.candidate["activation_owner"] != "RESOURCE_PROVIDER_MANAGER"
    {
        return Err(anyhow!(
            "candidate must be an immutable locate_arm_base v1 review candidate"
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

    let orientation = &request.candidate["quality_provenance"]["orientation_resolution"];
    if !matches!(
        orientation["status"].as_str(),
        Some("PASSED") | Some("PASSED_WITH_WARNINGS")
    ) || orientation["method"] != "BOUNDED_REFERENCE_IMAGE_VLM"
        || orientation["application_origin"] != "FOUNDATIONPOSE_CENTERED_CAD_MESH_ORIGIN"
        || orientation["application_order"]
            != "camera_from_centered_mesh @ orientation_correction @ centered_mesh_from_arm_base"
        || orientation["mesh_center_translation_preserved"] != true
    {
        return Err(anyhow!(
            "candidate orientation proof does not satisfy the bounded reference-image contract"
        ));
    }
    for field in [
        "profile_sha256",
        "reference_set_sha256",
        "source_evidence_sha256",
    ] {
        let hash = require_json_string(orientation, field, "candidate orientation proof")?;
        if hash.len() != 64 || !hash.bytes().all(|byte| byte.is_ascii_hexdigit()) {
            return Err(anyhow!(
                "candidate orientation proof {field} is not SHA-256"
            ));
        }
    }
    let selected_id = require_json_string(
        orientation,
        "selected_candidate_id",
        "candidate orientation proof",
    )?;
    let selected_axis =
        require_json_string(orientation, "selected_axis", "candidate orientation proof")?;
    let selected_degrees = orientation["selected_degrees"]
        .as_i64()
        .ok_or_else(|| anyhow!("candidate selected orientation degrees are required"))?;
    let allowed = orientation["allowed_candidates"]
        .as_array()
        .ok_or_else(|| anyhow!("candidate allowed orientation set is required"))?;
    let selected = allowed
        .iter()
        .find(|entry| entry["candidate_id"] == selected_id)
        .ok_or_else(|| anyhow!("selected orientation is outside the model profile"))?;
    if selected["axis"] != selected_axis
        || selected["degrees"] != selected_degrees
        || selected["rotation_xyzw"] != orientation["selected_rotation_xyzw"]
        || selected_axis != "Z"
        || !matches!(selected_degrees, 0 | 90 | 180 | 270)
    {
        return Err(anyhow!(
            "selected orientation does not exactly match its allowed candidate"
        ));
    }
    validate_transform_payload(
        &json!({
            "translation_m": [0.0, 0.0, 0.0],
            "rotation_xyzw": orientation["selected_rotation_xyzw"].clone()
        }),
        "candidate.orientation_resolution.selected_rotation",
    )?;
    let world_up_normalization = &orientation["world_up_normalization"];
    if world_up_normalization["method"] != "WORLD_UP_BOUNDED_LOCAL_X_HALF_TURN"
        || world_up_normalization["axis"] != "X"
    {
        return Err(anyhow!(
            "candidate orientation proof lacks bounded world-up normalization"
        ));
    }
    let normalization_degrees = world_up_normalization["degrees"]
        .as_i64()
        .ok_or_else(|| anyhow!("candidate world-up normalization degrees are required"))?;
    let expected_normalization_status = match normalization_degrees {
        0 => "NOT_REQUIRED",
        180 => "APPLIED_LOCAL_X_180",
        _ => {
            return Err(anyhow!(
                "candidate world-up normalization must be 0 or local-X 180 degrees"
            ))
        }
    };
    if world_up_normalization["status"] != expected_normalization_status {
        return Err(anyhow!(
            "candidate world-up normalization status does not match its half-turn"
        ));
    }
    let raw_up_dot = world_up_normalization["raw_arm_base_positive_z_dot_world"]
        .as_f64()
        .ok_or_else(|| anyhow!("candidate raw arm-base world-up dot is required"))?;
    let corrected_up_dot = world_up_normalization["corrected_arm_base_positive_z_dot_world"]
        .as_f64()
        .ok_or_else(|| anyhow!("candidate corrected arm-base world-up dot is required"))?;
    let minimum_up_dot = world_up_normalization["minimum_arm_base_up_dot_world"]
        .as_f64()
        .ok_or_else(|| anyhow!("candidate minimum arm-base world-up dot is required"))?;
    if !raw_up_dot.is_finite()
        || !corrected_up_dot.is_finite()
        || !minimum_up_dot.is_finite()
        || !(-1.0..=1.0).contains(&raw_up_dot)
        || !(-1.0..=1.0).contains(&corrected_up_dot)
        || !(0.0..=1.0).contains(&minimum_up_dot)
        || corrected_up_dot < minimum_up_dot
    {
        return Err(anyhow!(
            "candidate world-up normalization alignment proof is invalid"
        ));
    }
    let correction = &world_up_normalization["correction"];
    validate_transform_payload(correction, "candidate.world_up_normalization.correction")?;
    let correction_translation = correction["translation_m"]
        .as_array()
        .ok_or_else(|| anyhow!("candidate world-up correction translation is required"))?;
    if correction_translation.len() != 3
        || correction_translation.iter().any(|value| {
            value
                .as_f64()
                .is_none_or(|component| component.abs() > 1e-9)
        })
    {
        return Err(anyhow!(
            "candidate world-up normalization must preserve mesh-center translation"
        ));
    }
    let correction_rotation = correction["rotation_xyzw"]
        .as_array()
        .ok_or_else(|| anyhow!("candidate world-up correction rotation is required"))?;
    let expected_rotation = if normalization_degrees == 0 {
        [0.0, 0.0, 0.0, 1.0]
    } else {
        [1.0, 0.0, 0.0, 0.0]
    };
    if correction_rotation.len() != 4
        || correction_rotation
            .iter()
            .zip(expected_rotation)
            .any(|(value, expected)| {
                value
                    .as_f64()
                    .is_none_or(|component| (component - expected).abs() > 1e-6)
            })
    {
        return Err(anyhow!(
            "candidate world-up normalization is not the declared local-X half-turn"
        ));
    }
    let expected_corrected_up_dot = if normalization_degrees == 0 {
        raw_up_dot
    } else {
        -raw_up_dot
    };
    if (corrected_up_dot - expected_corrected_up_dot).abs() > 1e-6
        || (normalization_degrees == 0 && raw_up_dot < 0.0)
        || (normalization_degrees == 180 && raw_up_dot >= 0.0)
    {
        return Err(anyhow!(
            "candidate world-up normalization does not match the observed pose family"
        ));
    }
    let confidence = orientation["vlm"]["confidence"]
        .as_f64()
        .ok_or_else(|| anyhow!("candidate VLM confidence is required"))?;
    let minimum_confidence = orientation["vlm"]["minimum_confidence"]
        .as_f64()
        .ok_or_else(|| anyhow!("candidate VLM minimum confidence is required"))?;
    let orientation_consensus_confidence = orientation["minimum_consensus_confidence"]
        .as_f64()
        .ok_or_else(|| anyhow!("candidate VLM orientation consensus floor is required"))?;
    let orientation_decision_basis = require_json_string(
        orientation,
        "selection_decision_basis",
        "candidate orientation proof",
    )?;
    validate_bounded_vlm_selection(
        &selected_id,
        confidence,
        minimum_confidence,
        orientation_consensus_confidence,
        &orientation_decision_basis,
        &orientation["selection_attempts"],
        "candidate VLM orientation",
    )?;
    let world_axis = &request.candidate["quality_provenance"]["world_axis"];
    if !matches!(
        world_axis["status"].as_str(),
        Some("PASSED") | Some("PASSED_WITH_REPLAY_OVERRIDE")
    ) {
        return Err(anyhow!(
            "candidate lacks a valid timestamped world-axis proof"
        ));
    }
    let world_axis_frame =
        require_json_string(world_axis, "world_frame", "candidate world-axis proof")?;
    let world_axis_session_epoch =
        require_json_string(world_axis, "session_epoch", "candidate world-axis proof")?;
    let world_axis_convention =
        require_json_string(world_axis, "convention_id", "candidate world-axis proof")?;
    let foundation_pose = &request.candidate["quality_provenance"]["foundation_pose"];
    let score_semantics = foundation_pose["score_semantics"].as_str();
    if foundation_pose["fit_policy"] != "REPEATED_INDEPENDENT_FITS_ON_VOTED_DILATED_MASK"
        || !matches!(
            score_semantics,
            Some("RAW_MODEL_RANKING_ONLY") | Some("AUDIT_ONLY_NOT_SELECTION_INPUT")
        )
    {
        return Err(anyhow!(
            "candidate FoundationPose proof does not use the bounded repeated-fit policy"
        ));
    }
    let pose_score = foundation_pose["ranking_score_raw"]
        .as_f64()
        .ok_or_else(|| anyhow!("candidate FoundationPose raw ranking score is required"))?;
    if !pose_score.is_finite() {
        return Err(anyhow!(
            "candidate FoundationPose raw ranking score is invalid"
        ));
    }
    let selected_fit_id = require_json_string(
        foundation_pose,
        "selected_fit_candidate_id",
        "candidate FoundationPose proof",
    )?;
    let selected_mask_id = require_json_string(
        foundation_pose,
        "selected_mask_candidate_id",
        "candidate FoundationPose proof",
    )?;
    let mask_review = &request.candidate["quality_provenance"]["mask_review"];
    if mask_review["accepted"] != true {
        return Err(anyhow!("candidate mask review was not accepted"));
    }
    let mask_review_confidence = mask_review["confidence"]
        .as_f64()
        .ok_or_else(|| anyhow!("candidate mask-review confidence is required"))?;
    let mask_review_minimum = mask_review["minimum_confidence"]
        .as_f64()
        .ok_or_else(|| anyhow!("candidate mask-review minimum confidence is required"))?;
    if !mask_review_confidence.is_finite()
        || !mask_review_minimum.is_finite()
        || mask_review_confidence < mask_review_minimum
    {
        return Err(anyhow!("candidate mask review confidence is insufficient"));
    }
    let accepted_masks = mask_review["accepted_candidate_ids"]
        .as_array()
        .ok_or_else(|| anyhow!("candidate accepted mask IDs are required"))?;
    if accepted_masks.is_empty() || accepted_masks.iter().any(|value| value.as_str().is_none()) {
        return Err(anyhow!("candidate accepted mask IDs are invalid"));
    }
    let accepted_mask_ids = accepted_masks
        .iter()
        .map(|value| value.as_str().unwrap_or_default())
        .collect::<std::collections::HashSet<_>>();
    if accepted_mask_ids.len() != accepted_masks.len() {
        return Err(anyhow!("candidate accepted mask IDs contain duplicates"));
    }
    let mask_vote = &request.candidate["quality_provenance"]["mask_vote"];
    let vote_survivors = mask_vote["survivor_count"]
        .as_u64()
        .ok_or_else(|| anyhow!("candidate mask-vote survivor count is required"))?;
    let vote_threshold = mask_vote["vote_threshold"]
        .as_u64()
        .ok_or_else(|| anyhow!("candidate mask-vote threshold is required"))?;
    let vote_accepted = mask_vote["accepted_candidate_ids"]
        .as_array()
        .ok_or_else(|| anyhow!("candidate mask-vote accepted IDs are required"))?;
    let vote_accepted_ids = vote_accepted
        .iter()
        .filter_map(|value| value.as_str())
        .collect::<std::collections::HashSet<_>>();
    let expected_vote_threshold = (accepted_masks.len() as u64 + 1) / 2;
    let dilation_radius = mask_vote["dilation_radius_px"]
        .as_u64()
        .ok_or_else(|| anyhow!("candidate final-mask dilation radius is required"))?;
    if mask_vote["mask_id"].as_str() != Some(selected_mask_id.as_str())
        || mask_vote["vote_policy"] != "AT_LEAST_HALF_OF_VLM_ACCEPTED_MASKS"
        || vote_survivors != accepted_masks.len() as u64
        || vote_threshold != expected_vote_threshold
        || vote_accepted_ids != accepted_mask_ids
        || vote_accepted.len() != accepted_masks.len()
        || dilation_radius > 64
    {
        return Err(anyhow!(
            "candidate mask-vote proof does not match the accepted mask ensemble"
        ));
    }
    let pose_candidates = foundation_pose["candidates"]
        .as_array()
        .ok_or_else(|| anyhow!("candidate FoundationPose fits are required"))?;
    let pose_candidate_count = foundation_pose["candidate_count"]
        .as_u64()
        .ok_or_else(|| anyhow!("candidate FoundationPose fit count is required"))?;
    if pose_candidates.is_empty()
        || pose_candidate_count != pose_candidates.len() as u64
        || !pose_candidates.iter().any(|fit| {
            fit["candidate_id"].as_str() == Some(selected_fit_id.as_str())
                && fit["ranking_score_raw"]
                    .as_f64()
                    .is_some_and(|value| value.is_finite())
        })
        || !pose_candidates
            .iter()
            .all(|fit| fit["source_mask_candidate_id"].as_str() == Some(selected_mask_id.as_str()))
    {
        return Err(anyhow!(
            "candidate FoundationPose fits do not match the selected fit and mask"
        ));
    }
    let fit_selection = &request.candidate["quality_provenance"]["fit_selection"];
    if fit_selection["accepted"] != true
        || fit_selection["candidate_id"].as_str() != Some(selected_fit_id.as_str())
    {
        return Err(anyhow!(
            "candidate FoundationPose fit selection was not accepted"
        ));
    }
    let fit_confidence = fit_selection["confidence"]
        .as_f64()
        .ok_or_else(|| anyhow!("candidate fit-selection confidence is required"))?;
    let fit_minimum = fit_selection["minimum_confidence"]
        .as_f64()
        .ok_or_else(|| anyhow!("candidate fit-selection minimum confidence is required"))?;
    let fit_consensus_minimum = fit_selection["minimum_consensus_confidence"]
        .as_f64()
        .ok_or_else(|| anyhow!("candidate fit-selection consensus floor is required"))?;
    let fit_decision_basis =
        require_json_string(fit_selection, "decision_basis", "candidate fit selection")?;
    validate_bounded_vlm_selection(
        &selected_fit_id,
        fit_confidence,
        fit_minimum,
        fit_consensus_minimum,
        &fit_decision_basis,
        &fit_selection["attempts"],
        "candidate FoundationPose fit selection",
    )?;
    validate_transform_payload(
        &request.candidate["world_from_arm_base"],
        "candidate.world_from_arm_base",
    )?;
    let final_arm_base_up_dot = transform_z_dot_parent_z(
        &request.candidate["world_from_arm_base"],
        "candidate.world_from_arm_base",
    )?;
    if final_arm_base_up_dot < -1e-9 {
        return Err(anyhow!("candidate arm-base +Z points below world +Z"));
    }
    if (final_arm_base_up_dot - corrected_up_dot).abs() > 1e-5 {
        return Err(anyhow!(
            "candidate world-up normalization does not match world_from_arm_base"
        ));
    }

    let mut hash_payload = request.candidate.clone();
    if let Some(object) = hash_payload.as_object_mut() {
        object.remove("candidate_sha256");
        object.remove("candidate_path");
    }
    let candidate_sha256 = canonical_json_sha256(&hash_payload);
    if request.candidate["candidate_sha256"] != candidate_sha256 {
        return Err(anyhow!(
            "candidate SHA-256 does not match its immutable payload"
        ));
    }
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
        != "midbrain.skill.locate_arm_base.candidate_review_decision"
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
    let review_decided_at_us = request.review_decision["decided_at_us"]
        .as_u64()
        .ok_or_else(|| anyhow!("review_decision.decided_at_us must be a positive integer"))?;
    if review_decided_at_us > candidate_expires_at_us {
        return Err(anyhow!("candidate was not reviewed before its deadline"));
    }
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

    let frame_contract = &request.candidate["frame_contract"];
    let world_frame =
        require_json_string(frame_contract, "world_frame", "candidate.frame_contract")?;
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
        || frame_contract["transform_semantics"] != "PARENT_FROM_CHILD"
        || frame_contract["legacy_candidate_compatibility"] != "REJECT"
    {
        return Err(anyhow!(
            "candidate uses an unsupported spatial frame contract"
        ));
    }
    if world_axis_frame != world_frame
        || world_axis_convention != convention_id
        || request.candidate["parent_frame"] != world_frame
    {
        return Err(anyhow!(
            "candidate world-axis epoch identity does not match its frame contract"
        ));
    }
    let camera = &request.candidate["camera_provenance"];
    let camera_provider_id =
        require_json_string(camera, "provider_id", "candidate.camera_provenance")?;
    let camera_canonical_device_id =
        require_json_string(camera, "canonical_device_id", "candidate.camera_provenance")?;
    let camera_calibration_revision = require_json_string(
        camera,
        "calibration_revision",
        "candidate.camera_provenance",
    )?;
    let camera_report = reports
        .get(&camera_provider_id)
        .ok_or_else(|| anyhow!("candidate camera provider has no current Manager report"))?;
    if camera_report.expired || !camera_report.ready || camera_report.health != "HEALTHY" {
        return Err(anyhow!("candidate camera provider health is not current"));
    }
    let current_camera_canonical_device_id = camera_report_canonical_device_id(camera_report)
        .ok_or_else(|| anyhow!("current camera report lacks a canonical device identity"))?;
    if current_camera_canonical_device_id != camera_canonical_device_id {
        return Err(anyhow!(
            "current mounted camera does not match the candidate"
        ));
    }
    let current_camera_calibration_revision = require_json_string(
        &camera_report.details,
        "calibration_revision",
        "current camera provider report details",
    )?;
    if current_camera_calibration_revision != camera_calibration_revision {
        return Err(anyhow!(
            "current camera calibration does not match the candidate"
        ));
    }
    let transforms = json!({
        "world_from_camera": request.candidate["composition"]["world_from_camera"].clone(),
        "world_from_vio": {
            "translation_m": [0.0, 0.0, 0.0],
            "rotation_xyzw": [0.0, 0.0, 0.0, 1.0]
        },
        "world_from_base": request.candidate["world_from_arm_base"].clone()
    });
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
        expires_at: None,
        expires_at_us: None,
        validity_policy: "MOUNTED_CANONICAL_CAMERA_CALIBRATION_GATED_V3".to_string(),
        invalidation_conditions: vec![
            "EXPLICIT_REVOCATION".to_string(),
            "SUPERSEDED_BY_NEW_REVIEWED_ACTIVATION".to_string(),
            "MOUNTED_CAMERA_CANONICAL_DEVICE_CHANGED".to_string(),
            "CAMERA_CALIBRATION_REVISION_CHANGED".to_string(),
        ],
        state: "ACTIVE".to_string(),
        enforcement: "ENFORCED".to_string(),
        motion_usable: true,
        session_epoch: world_axis_session_epoch,
        world_frame: world_frame.clone(),
        vio_world_frame: world_frame,
        camera_frame,
        arm_base_frame,
        convention_id,
        camera_optical_convention_id,
        camera_provider_id,
        camera_canonical_device_id,
        camera_provider_instance_id: camera_report.instance_id.clone(),
        camera_boot_id: camera_report.boot_id.clone(),
        camera_calibration_revision,
        vio_provider_id: "world_state_fabric.transform_graph".to_string(),
        vio_provider_instance_id: "CAPTURE_PROVENANCE".to_string(),
        vio_boot_id: "CAPTURE_PROVENANCE".to_string(),
        transforms,
        reviewer: reviewer.clone(),
        translation_refinement_revision: 0,
        last_translation_refinement: None,
        translation_refinement_journal: Vec::new(),
        last_transition_reason: "bounded reference-image orientation and timestamped world-axis candidate activated after exact review".to_string(),
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
    publish_workcell_calibration_transitions(state, &[(record, active)]).await
}

async fn publish_workcell_calibration_supersession(
    state: &AppState,
    replacement: &WorkcellCalibrationActivationRecord,
    superseded: &[WorkcellCalibrationActivationRecord],
) -> Result<()> {
    let mut transitions = superseded
        .iter()
        .map(|record| (record, false))
        .collect::<Vec<_>>();
    transitions.push((replacement, true));
    publish_workcell_calibration_transitions(state, &transitions).await
}

async fn publish_workcell_calibration_transitions(
    state: &AppState,
    transitions: &[(&WorkcellCalibrationActivationRecord, bool)],
) -> Result<()> {
    let observed_at_us = Utc::now().timestamp_micros().max(0) as u64;
    let observations = workcell_calibration_transition_observations(
        transitions,
        &state.manager_instance_id,
        &state.manager_boot_id,
        observed_at_us,
    );
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

fn workcell_calibration_transition_observations(
    transitions: &[(&WorkcellCalibrationActivationRecord, bool)],
    manager_instance_id: &str,
    manager_boot_id: &str,
    observed_at_us: u64,
) -> Vec<Value> {
    let mut observations = Vec::with_capacity(transitions.len().saturating_mul(4));
    for (index, (record, active)) in transitions.iter().enumerate() {
        let sequence_base = observed_at_us.saturating_add((index as u64).saturating_mul(4));
        observations.extend(workcell_calibration_observations(
            record,
            *active,
            manager_instance_id,
            manager_boot_id,
            observed_at_us,
            sequence_base,
        ));
    }
    observations
}

fn workcell_calibration_observations(
    record: &WorkcellCalibrationActivationRecord,
    active: bool,
    manager_instance_id: &str,
    manager_boot_id: &str,
    observed_at_us: u64,
    sequence_base: u64,
) -> Vec<Value> {
    let review_state = if active { "ACCEPTED" } else { "REVOKED" };
    let activation_state = if active { "ACTIVE" } else { "REVOKED" };
    let motion_usable = active;
    let envelope_expires_at_us = if active {
        None
    } else {
        Some(observed_at_us.saturating_add(1_000_000))
    };
    let transform = |stream: &str, child_frame: &str, payload: &Value, offset: u64| {
        json!({
            "schema": "physical_agent.transform",
            "schema_version": 1,
            "stream": stream,
            "provider_id": "manager.workcell_calibration",
            "provider_instance_id": manager_instance_id,
            "boot_id": manager_boot_id,
            "sequence": sequence_base.saturating_add(offset),
            "observed_at_us": observed_at_us,
            "coordinate_frame": record.world_frame,
            "calibration_revision": record.calibration_revision,
            "expires_at_us": envelope_expires_at_us,
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
                "translation_refinement_revision": record.translation_refinement_revision,
                "motion_usable": motion_usable,
                "expires_at_us": null,
                "validity_policy": record.validity_policy,
                "invalidation_conditions": record.invalidation_conditions
            }
        })
    };
    vec![
        transform(
            "transform.world.arm_base",
            &record.arm_base_frame,
            &record.transforms["world_from_base"],
            0,
        ),
        json!({
            "schema": "physical_agent.workcell_calibration_activation",
            "schema_version": 1,
            "stream": "manager.workcell_calibration.activation",
            "provider_id": "manager.workcell_calibration",
            "provider_instance_id": manager_instance_id,
            "boot_id": manager_boot_id,
            "sequence": sequence_base.saturating_add(1),
            "observed_at_us": observed_at_us,
            "coordinate_frame": record.world_frame,
            "calibration_revision": record.calibration_revision,
            "expires_at_us": envelope_expires_at_us,
            "related_skill_id": record.candidate_id,
            "valid": true,
            "data": record
        }),
    ]
}

async fn list_providers(State(state): State<AppState>) -> Json<Vec<ProviderView>> {
    Json(collect_provider_views(&state).await)
}

async fn get_provider_detail(
    State(state): State<AppState>,
    Path(id): Path<String>,
) -> Result<Json<ProviderView>, (StatusCode, Json<Value>)> {
    collect_provider_views(&state)
        .await
        .into_iter()
        .find(|view| view.config.id == id)
        .map(Json)
        .ok_or_else(|| api_error(StatusCode::NOT_FOUND, "unknown provider"))
}

async fn agent_runtime_catalog(State(state): State<AppState>) -> Json<AgentRuntimeCatalog> {
    let provider_views = collect_provider_views(&state).await;
    let capabilities = collect_capability_views(&state).await;
    let providers = provider_views
        .into_iter()
        .map(|view| {
            let report = view.report.as_ref();
            let details = report.map(|value| &value.details);
            let diagnostics = details.and_then(|value| value.get("diagnostics"));
            AgentProviderView {
                provider_id: view.config.id,
                display_name: view.config.display_name,
                dependencies: view.config.dependencies,
                process_state: view.process_state,
                instance_id: report.map(|value| value.instance_id.clone()),
                boot_id: report.map(|value| value.boot_id.clone()),
                residency: report.map(|value| value.residency.clone()),
                health: report.map(|value| value.health.clone()),
                ready: report.is_some_and(|value| value.ready),
                expired: report.is_some_and(|value| value.expired),
                last_seen: report.map(|value| value.last_seen),
                last_error: bounded_agent_catalog_value(
                    details.and_then(|value| value.get("last_error")),
                ),
                manager_error: bounded_agent_catalog_value(
                    details.and_then(|value| value.get("manager_error")),
                ),
                blocking_prerequisite: bounded_agent_catalog_value(
                    diagnostics.and_then(|value| value.get("blocking_prerequisite")),
                ),
                detail_schema: "midbrain.manager.provider_detail.v1",
                detail_sections: vec!["/config", "/process_state", "/last_exit", "/report"],
            }
        })
        .collect();
    let capabilities = capabilities
        .into_iter()
        .map(|view| AgentCapabilityView {
            capability: view.capability,
            provider_id: view.provider_id,
            provider_instance_id: view.provider_instance_id,
            available: view.available,
        })
        .collect();
    Json(AgentRuntimeCatalog {
        schema: "midbrain.manager.agent_runtime_catalog",
        schema_version: 1,
        providers,
        capabilities,
    })
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
    Json(collect_capability_views(&state).await)
}

async fn collect_capability_views(state: &AppState) -> Vec<CapabilityView> {
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
    result
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
    for (capability, resource_id) in &request.required_resources_by_capability {
        if !required_set.contains(capability.as_str()) {
            return Err(anyhow!(
                "required resource supplied for unrequested capability {capability}"
            ));
        }
        validate_resource_id(resource_id)?;
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
        let required_resource_id = request
            .required_resources_by_capability
            .get(&capability)
            .map(String::as_str);
        let mut candidates: Vec<CapabilityBindingCandidate> = configs
            .keys()
            .filter(|provider_id| provider_allowed(provider_id))
            .filter_map(|provider_id| {
                capability_candidate_for_resource(
                    provider_id,
                    reports.get(provider_id),
                    &capability,
                    required_resource_id,
                )
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
                required_resource_id: required_resource_id.map(str::to_string),
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
            let selected = capability_candidate_for_resource(
                provider_id,
                reports.get(provider_id),
                &capability,
                required_resource_id,
            )
            .unwrap_or_else(|| {
                unavailable_fallback_candidate(
                    provider_id,
                    reports.get(provider_id),
                    required_resource_id,
                )
            });
            selections.push(CapabilityBindingSelection {
                capability,
                required_resource_id: required_resource_id.map(str::to_string),
                provider_id: selected.provider_id.clone(),
                provider_instance_id: selected.provider_instance_id.clone(),
                boot_id: selected.boot_id.clone(),
                available: selected.available,
                compatibility_verified: selected.advertised && selected.resource_compatible,
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
        let currently_available = capability_candidate_for_resource(
            &selection.provider_id,
            Some(report),
            &selection.capability,
            selection.required_resource_id.as_deref(),
        )
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

fn capability_candidate_for_resource(
    provider_id: &str,
    report: Option<&ProviderReport>,
    capability: &str,
    required_resource_id: Option<&str>,
) -> Option<CapabilityBindingCandidate> {
    let report = report?;
    let readiness = report
        .details
        .get("capability_readiness")
        .and_then(Value::as_object)?
        .get(capability)?;
    let advertised = readiness.is_boolean();
    let capability_ready = readiness.as_bool().unwrap_or(false);
    let resource_compatible = required_resource_id.is_none_or(|required| {
        report
            .details
            .get("resource_groups")
            .and_then(Value::as_array)
            .is_some_and(|groups| {
                groups
                    .iter()
                    .any(|group| group.get("resource_id").and_then(Value::as_str) == Some(required))
            })
    });
    let available = advertised
        && capability_ready
        && resource_compatible
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
        required_resource_id: required_resource_id.map(str::to_string),
        resource_compatible,
    })
}

fn unavailable_fallback_candidate(
    provider_id: &str,
    report: Option<&ProviderReport>,
    required_resource_id: Option<&str>,
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
        required_resource_id: required_resource_id.map(str::to_string),
        resource_compatible: required_resource_id.is_none(),
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
    validate_resource_id(&resource_id)
        .map_err(|error| api_error(StatusCode::BAD_REQUEST, error.to_string()))?;
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
    let active_lease_ids: Vec<String> = leases
        .values()
        .filter(|lease| {
            lease.state == "ACTIVE" && resource_scopes_overlap(&lease.resource_id, &resource_id)
        })
        .map(|lease| lease.lease_id.clone())
        .collect();
    let mut preempted_resources = HashSet::new();
    if !active_lease_ids.is_empty() {
        if !request.preempt {
            let active = leases
                .get(&active_lease_ids[0])
                .expect("active overlapping lease exists");
            return Err(api_error(
                StatusCode::CONFLICT,
                format!(
                    "resource {resource_id} overlaps active advisory lease {} on {} owned by {}",
                    active.lease_id, active.resource_id, active.owner_id
                ),
            ));
        }
        for active_lease_id in active_lease_ids {
            if let Some(active) = leases.get_mut(&active_lease_id) {
                preempted_resources.insert(active.resource_id.clone());
                active.state = "PREEMPTED".to_string();
                active.last_transition_reason = format!(
                    "preempted by advisory owner {owner_id} on overlapping resource {resource_id}"
                );
            }
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

    for preempted_resource in preempted_resources {
        if let Err(error) = publish_control_authority_resource(&state, &preempted_resource).await {
            warn!(resource_id = %preempted_resource, error = %error, "failed to publish preempted advisory authority");
        }
    }
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

fn validate_resource_id(resource_id: &str) -> Result<()> {
    let value = resource_id.trim();
    if value != resource_id || value.is_empty() || value.starts_with('/') || value.ends_with('/') {
        return Err(anyhow!("resource_id must be a non-empty canonical path"));
    }
    if value.split('/').any(|segment| {
        segment.is_empty()
            || segment == "."
            || segment == ".."
            || !segment.chars().all(|character| {
                character.is_ascii_alphanumeric() || matches!(character, '.' | '_' | '-')
            })
    }) {
        return Err(anyhow!("resource_id contains an invalid path segment"));
    }
    Ok(())
}

fn resource_scopes_overlap(left: &str, right: &str) -> bool {
    left == right
        || left
            .strip_prefix(right)
            .is_some_and(|suffix| suffix.starts_with('/'))
        || right
            .strip_prefix(left)
            .is_some_and(|suffix| suffix.starts_with('/'))
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
    let mut active_overlapping_leases: Vec<ControlAuthorityLease> = leases
        .values()
        .filter(|lease| {
            lease.state == "ACTIVE" && resource_scopes_overlap(&lease.resource_id, resource_id)
        })
        .cloned()
        .collect();
    active_overlapping_leases.sort_by(|left, right| {
        left.resource_id
            .cmp(&right.resource_id)
            .then_with(|| left.fencing_generation.cmp(&right.fencing_generation))
    });
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
        active_overlapping_leases,
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

fn provider_response_allows_termination(status_success: bool, body: &Value) -> bool {
    if !status_success {
        return false;
    }
    body.get("termination_allowed")
        .and_then(Value::as_bool)
        .unwrap_or_else(|| body.get("success").and_then(Value::as_bool) == Some(true))
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
    if !provider_response_allows_termination(status.is_success(), &body) {
        return Err(anyhow!(
            "provider {provider_id} did not permit termination: HTTP {status}: {body}"
        ));
    }
    let safe_state_confirmed = body
        .get("safe_state_confirmed")
        .and_then(Value::as_bool)
        .unwrap_or_else(|| body.get("success").and_then(Value::as_bool) == Some(true));
    Ok(json!({
        "provider_id": provider_id,
        "safe_state_confirmed": safe_state_confirmed,
        "termination_allowed": true,
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
                "Basic arm providers confirm safe state or explicitly permit process release after measured stationary retry or loss of control"
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

fn provider_dependency_order(
    configs: &HashMap<String, ProviderConfig>,
    target_id: &str,
) -> Result<Vec<String>> {
    fn visit(
        configs: &HashMap<String, ProviderConfig>,
        provider_id: &str,
        visiting: &mut HashSet<String>,
        visited: &mut HashSet<String>,
        order: &mut Vec<String>,
    ) -> Result<()> {
        if visited.contains(provider_id) {
            return Ok(());
        }
        let provider = configs
            .get(provider_id)
            .ok_or_else(|| anyhow!("unknown provider dependency {provider_id}"))?;
        if !visiting.insert(provider_id.to_string()) {
            return Err(anyhow!("provider dependency cycle includes {provider_id}"));
        }
        for dependency in &provider.dependencies {
            visit(configs, dependency, visiting, visited, order)?;
        }
        visiting.remove(provider_id);
        visited.insert(provider_id.to_string());
        order.push(provider_id.to_string());
        Ok(())
    }

    let mut visiting = HashSet::new();
    let mut visited = HashSet::new();
    let mut order = Vec::new();
    visit(configs, target_id, &mut visiting, &mut visited, &mut order)?;
    Ok(order)
}

async fn ensure_single_provider_hot(state: &AppState, id: &str) -> Result<Value> {
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

async fn ensure_provider_hot(state: &AppState, id: &str) -> Result<Value> {
    let order = provider_dependency_order(&state.configs, id)?;
    let mut target_result = Value::Null;
    for provider_id in &order {
        let result = ensure_single_provider_hot(state, provider_id).await?;
        if provider_id == id {
            target_result = result;
        }
    }
    let dependencies = order
        .iter()
        .filter(|provider_id| provider_id.as_str() != id)
        .cloned()
        .collect::<Vec<_>>();
    match target_result {
        Value::Object(mut result) => {
            result.insert("manager_hot_dependencies".to_string(), json!(dependencies));
            Ok(Value::Object(result))
        }
        result => Ok(json!({
            "provider_id": id,
            "status": "hot",
            "manager_hot_dependencies": dependencies,
            "provider_result": result,
        })),
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

fn bounded_agent_catalog_value(value: Option<&Value>) -> Option<Value> {
    const MAX_SERIALIZED_BYTES: usize = 2_048;
    const PREVIEW_CHARS: usize = 400;

    let value = value?;
    if value.is_null() {
        return None;
    }
    let serialized = serde_json::to_string(value).ok()?;
    if serialized.len() <= MAX_SERIALIZED_BYTES {
        return Some(value.clone());
    }
    Some(json!({
        "truncated": true,
        "serialized_bytes": serialized.len(),
        "preview": serialized.chars().take(PREVIEW_CHARS).collect::<String>(),
    }))
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

    #[test]
    fn agent_catalog_detail_values_are_bounded() {
        let small = json!({"status": "blocked", "message": "calibrate"});
        assert_eq!(bounded_agent_catalog_value(Some(&small)), Some(small));

        let large = json!({"samples": vec!["diagnostic"; 1000]});
        let bounded = bounded_agent_catalog_value(Some(&large))
            .expect("large detail should produce a bounded summary");
        assert_eq!(bounded["truncated"], true);
        assert!(bounded["serialized_bytes"].as_u64().unwrap_or(0) > 2_048);
        assert!(serde_json::to_vec(&bounded).unwrap().len() < 2_048);
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

    fn provider_config_with_dependencies(id: &str, dependencies: &[&str]) -> ProviderConfig {
        let mut value = json!({
            "id": id,
            "display_name": id,
            "command": "example.exe",
            "dependencies": dependencies
        });
        if id.contains("rebot_dm") {
            value["safe_state_request_path"] = json!("/v1/calibration/safe-home");
            value["safe_state_timeout_ms"] = json!(35_000);
        }
        serde_json::from_value(value).expect("provider config with dependencies should parse")
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
            required_resources_by_capability: HashMap::new(),
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
            assembly_selection_lock: Arc::new(Mutex::new(())),
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

    fn locate_arm_base_activation_fixture(
        now: DateTime<Utc>,
    ) -> (
        WorkcellCalibrationActivationRequest,
        HashMap<String, ProviderReport>,
    ) {
        let now_us = now.timestamp_micros() as u64;
        let secret = b"test-review-auth-secret-with-at-least-32-bytes";
        let mut candidate = json!({
            "schema": "midbrain.skill.locate_arm_base.calibration_candidate",
            "schema_version": 1,
            "candidate_id": "arm-base-1",
            "workcell_calibration_revision": "arm-base-1",
            "run_id": "run-1",
            "created_at": now.to_rfc3339(),
            "expires_at_us": now_us + 60_000_000,
            "observed_at_us": now_us - 2_000_000,
            "parent_frame": "local_vio/epoch-7",
            "child_frame": "rebot_arm_base",
            "camera_frame": "femto_bolt_color_optical_frame",
            "motion_usable": false,
            "review_state": "PENDING_REVIEW",
            "activation_owner": "RESOURCE_PROVIDER_MANAGER",
            "world_from_arm_base": {
                "translation_m": [0.1, 0.2, 0.3],
                "rotation_xyzw": [0.0, 0.0, 0.0, 1.0]
            },
            "composition": {
                "world_from_camera": {
                    "translation_m": [0.4, 0.5, 0.6],
                    "rotation_xyzw": [0.0, 0.0, 0.0, 1.0]
                },
                "camera_from_centered_mesh": {
                    "translation_m": [0.1, 0.2, 0.3],
                    "rotation_xyzw": [0.0, 0.0, 0.0, 1.0]
                },
                "orientation_correction": {
                    "translation_m": [0.0, 0.0, 0.0],
                    "rotation_xyzw": [0.0, 0.0, 0.0, 1.0]
                },
                "centered_mesh_from_arm_base": {
                    "translation_m": [0.0, 0.0, -0.0446249945],
                    "rotation_xyzw": [0.0, 0.0, 0.0, 1.0]
                }
            },
            "quality_provenance": {
                "foundation_pose": {
                    "selected_fit_candidate_id": "fit_1",
                    "selected_mask_candidate_id": "voted_mask_dilated_r4",
                    "fit_policy": "REPEATED_INDEPENDENT_FITS_ON_VOTED_DILATED_MASK",
                    "ranking_score_raw": -11.75,
                    "score_semantics": "AUDIT_ONLY_NOT_SELECTION_INPUT",
                    "candidate_count": 2,
                    "candidates": [
                        {
                            "candidate_id": "fit_1",
                            "source_mask_candidate_id": "voted_mask_dilated_r4",
                            "ranking_score_raw": -11.75
                        },
                        {
                            "candidate_id": "fit_2",
                            "source_mask_candidate_id": "voted_mask_dilated_r4",
                            "ranking_score_raw": -12.1
                        }
                    ]
                },
                "mask_review": {
                    "accepted_candidate_ids": ["mask_1", "mask_3", "mask_4"],
                    "rejected_candidate_ids": ["mask_2"],
                    "confidence": 0.84,
                    "minimum_confidence": 0.60,
                    "accepted": true
                },
                "mask_vote": {
                    "mask_id": "voted_mask_dilated_r4",
                    "accepted_candidate_ids": ["mask_1", "mask_3", "mask_4"],
                    "rejected_candidate_ids": ["mask_2"],
                    "survivor_count": 3,
                    "vote_threshold": 2,
                    "vote_policy": "AT_LEAST_HALF_OF_VLM_ACCEPTED_MASKS",
                    "dilation_radius_px": 4
                },
                "fit_selection": {
                    "candidate_id": "fit_1",
                    "confidence": 0.81,
                    "minimum_confidence": 0.60,
                    "minimum_consensus_confidence": 0.45,
                    "accepted": true,
                    "decision_basis": "FIRST_ATTEMPT_CONFIDENCE",
                    "attempts": [
                        {"candidate_id": "fit_1", "confidence": 0.81}
                    ]
                },
                "orientation_resolution": {
                    "status": "PASSED_WITH_WARNINGS",
                    "method": "BOUNDED_REFERENCE_IMAGE_VLM",
                    "profile_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                    "reference_set_sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                    "source_evidence_sha256": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
                    "allowed_candidates": [
                        {"candidate_id": "z0", "axis": "Z", "degrees": 0, "rotation_xyzw": [0.0, 0.0, 0.0, 1.0]},
                        {"candidate_id": "z90", "axis": "Z", "degrees": 90, "rotation_xyzw": [0.0, 0.0, 0.7071067811865475, 0.7071067811865476]},
                        {"candidate_id": "z180", "axis": "Z", "degrees": 180, "rotation_xyzw": [0.0, 0.0, 1.0, 0.0]},
                        {"candidate_id": "z270", "axis": "Z", "degrees": 270, "rotation_xyzw": [0.0, 0.0, 0.7071067811865476, -0.7071067811865475]}
                    ],
                    "selected_candidate_id": "z0",
                    "selected_axis": "Z",
                    "selected_degrees": 0,
                    "selected_rotation_xyzw": [0.0, 0.0, 0.0, 1.0],
                    "vlm": {
                        "confidence": 0.62,
                        "minimum_confidence": 0.72
                    },
                    "minimum_consensus_confidence": 0.55,
                    "selection_decision_basis": "REPEATED_CANDIDATE_CONSENSUS",
                    "selection_attempts": [
                        {"candidate_id": "z0", "confidence": 0.60},
                        {"candidate_id": "z0", "confidence": 0.62}
                    ],
                    "application_origin": "FOUNDATIONPOSE_CENTERED_CAD_MESH_ORIGIN",
                    "application_order": "camera_from_centered_mesh @ orientation_correction @ centered_mesh_from_arm_base",
                    "mesh_center_translation_preserved": true
                },
                "world_axis": {
                    "status": "PASSED",
                    "source": "WORLD_STATE_FABRIC_TIMESTAMPED_TRANSFORM_GRAPH",
                    "at_us": now_us - 2_000_000,
                    "world_frame": "local_vio/epoch-7",
                    "session_epoch": "epoch-7",
                    "convention_id": "MIDBRAIN_X_FORWARD_Y_LEFT_Z_UP_V2"
                }
            },
            "frame_contract": {
                "world_frame": "local_vio/epoch-7",
                "camera_frame": "femto_bolt_color_optical_frame",
                "arm_base_frame": "rebot_arm_base",
                "convention_id": "MIDBRAIN_X_FORWARD_Y_LEFT_Z_UP_V2",
                "camera_optical_convention_id": "CAMERA_OPTICAL_X_RIGHT_Y_DOWN_Z_FORWARD_V1",
                "transform_semantics": "PARENT_FROM_CHILD",
                "legacy_candidate_compatibility": "REJECT"
            },
            "camera_provenance": {
                "provider_id": "camera.femto_bolt",
                "provider_instance_id": "camera.femto_bolt-instance",
                "boot_id": "camera.femto_bolt-boot",
                "canonical_device_id": "orbbec:femto-bolt:test-camera",
                "calibration_revision": "camera-calibration"
            }
        });
        candidate["quality_provenance"]["orientation_resolution"]["world_up_normalization"] = json!({
            "status": "NOT_REQUIRED",
            "method": "WORLD_UP_BOUNDED_LOCAL_X_HALF_TURN",
            "axis": "X",
            "degrees": 0,
            "minimum_arm_base_up_dot_world": 0.5,
            "raw_arm_base_positive_z_dot_world": 1.0,
            "corrected_arm_base_positive_z_dot_world": 1.0,
            "correction": {
                "translation_m": [0.0, 0.0, 0.0],
                "rotation_xyzw": [0.0, 0.0, 0.0, 1.0],
                "matrix": [
                    [1.0, 0.0, 0.0, 0.0],
                    [0.0, 1.0, 0.0, 0.0],
                    [0.0, 0.0, 1.0, 0.0],
                    [0.0, 0.0, 0.0, 1.0]
                ]
            }
        });
        let candidate_sha256 = canonical_json_sha256(&candidate);
        candidate["candidate_sha256"] = json!(candidate_sha256);
        let identity_payload = json!({
            "issuer": "test.identity",
            "reviewer_id": "operator@example.test",
            "candidate_id": "arm-base-1",
            "candidate_sha256": candidate_sha256,
            "decision": "APPROVE",
            "issued_at_us": now_us - 100_000,
            "expires_at_us": now_us + 300_000_000,
            "nonce": "nonce-locate-arm-base-1"
        });
        let identity_bytes = serde_json::to_vec(&identity_payload).unwrap();
        let signature = hmac_sha256(secret, &identity_bytes);
        let assertion = format!(
            "{}.{}",
            base64url_encode(&identity_bytes),
            base64url_encode(&signature)
        );
        let review_decision = json!({
            "schema": "midbrain.skill.locate_arm_base.candidate_review_decision",
            "schema_version": 1,
            "decision_id": "decision-locate-arm-base-1",
            "candidate_id": "arm-base-1",
            "candidate_sha256": candidate_sha256,
            "decision": "APPROVE",
            "decision_state": "APPROVED_FOR_ACTIVATION",
            "activation_state": "NOT_ACTIVATED",
            "motion_usable": false,
            "decided_at_us": now_us,
            "reviewer": {
                "issuer": "test.identity",
                "reviewer_id": "operator@example.test",
                "assertion_nonce": "nonce-locate-arm-base-1"
            }
        });
        let request = WorkcellCalibrationActivationRequest {
            request_id: "activate-locate-arm-base-1".to_string(),
            activated_by: "test-agent".to_string(),
            candidate,
            review_decision,
            review_identity_assertion: assertion,
        };
        let mut camera_report =
            provider_report("camera.femto_bolt", "camera.rgbd.bundle", true, true, "HOT");
        camera_report.details["calibration_revision"] = json!("camera-calibration");
        camera_report.details["canonical_device_id"] = json!("orbbec:femto-bolt:test-camera");
        (
            request,
            HashMap::from([("camera.femto_bolt".to_string(), camera_report)]),
        )
    }

    #[test]
    fn locate_arm_base_candidate_activates_only_through_manager_review() {
        let now = Utc::now();
        let (request, reports) = locate_arm_base_activation_fixture(now);
        let request_sha = canonical_json_sha256(&serde_json::to_value(&request).unwrap());
        let record = super::build_workcell_activation_record(
            &request,
            request_sha,
            &reports,
            b"test-review-auth-secret-with-at-least-32-bytes",
            now,
        )
        .expect("bounded locate_arm_base candidate should activate");
        assert_eq!(record.candidate_id, "arm-base-1");
        assert_eq!(record.world_frame, "local_vio/epoch-7");
        assert_eq!(record.session_epoch, "epoch-7");
        assert_eq!(record.arm_base_frame, "rebot_arm_base");
        assert!(record.motion_usable);
        assert_eq!(record.state, "ACTIVE");
    }

    #[test]
    fn locate_arm_base_accepts_qualified_majority_selection_proof() {
        let attempts = json!([
            {"candidate_id": "z90", "confidence": 0.56},
            {"candidate_id": "z0", "confidence": 0.68},
            {"candidate_id": "z0", "confidence": 0.64}
        ]);
        super::validate_bounded_vlm_selection(
            "z0",
            0.68,
            0.72,
            0.55,
            "QUALIFIED_MAJORITY_CANDIDATE_CONSENSUS",
            &attempts,
            "test orientation selection",
        )
        .expect("two qualified votes must resolve a three-call tie break");
    }

    #[test]
    fn locate_arm_base_activation_accepts_bounded_upside_down_normalization() {
        let now = Utc::now();
        let now_us = now.timestamp_micros() as u64;
        let secret = b"test-review-auth-secret-with-at-least-32-bytes";
        let (mut request, reports) = locate_arm_base_activation_fixture(now);
        request.candidate["quality_provenance"]["orientation_resolution"]
            ["world_up_normalization"] = json!({
            "status": "APPLIED_LOCAL_X_180",
            "method": "WORLD_UP_BOUNDED_LOCAL_X_HALF_TURN",
            "axis": "X",
            "degrees": 180,
            "minimum_arm_base_up_dot_world": 0.5,
            "raw_arm_base_positive_z_dot_world": -1.0,
            "corrected_arm_base_positive_z_dot_world": 1.0,
            "correction": {
                "translation_m": [0.0, 0.0, 0.0],
                "rotation_xyzw": [1.0, 0.0, 0.0, 0.0],
                "matrix": [
                    [1.0, 0.0, 0.0, 0.0],
                    [0.0, -1.0, 0.0, 0.0],
                    [0.0, 0.0, -1.0, 0.0],
                    [0.0, 0.0, 0.0, 1.0]
                ]
            }
        });
        request
            .candidate
            .as_object_mut()
            .unwrap()
            .remove("candidate_sha256");
        let candidate_sha = canonical_json_sha256(&request.candidate);
        request.candidate["candidate_sha256"] = json!(candidate_sha);
        request.review_decision["candidate_sha256"] = json!(candidate_sha);
        let identity_payload = json!({
            "issuer": "test.identity",
            "reviewer_id": "operator@example.test",
            "candidate_id": "arm-base-1",
            "candidate_sha256": candidate_sha,
            "decision": "APPROVE",
            "issued_at_us": now_us - 100_000,
            "expires_at_us": now_us + 300_000_000,
            "nonce": "nonce-locate-arm-base-1"
        });
        let identity_bytes = serde_json::to_vec(&identity_payload).unwrap();
        request.review_identity_assertion = format!(
            "{}.{}",
            base64url_encode(&identity_bytes),
            base64url_encode(&hmac_sha256(secret, &identity_bytes))
        );
        let request_sha = canonical_json_sha256(&serde_json::to_value(&request).unwrap());
        super::build_workcell_activation_record(&request, request_sha, &reports, secret, now)
            .expect("one exact local-X half-turn should normalize an upside-down fit");
    }

    #[test]
    fn locate_arm_base_activation_accepts_review_completed_before_deadline() {
        let now = Utc::now();
        let (mut request, reports) = locate_arm_base_activation_fixture(now);
        request.candidate["expires_at_us"] = json!((now.timestamp_micros() - 1) as u64);
        request.review_decision["decided_at_us"] = json!((now.timestamp_micros() - 2) as u64);
        let mut hash_payload = request.candidate.clone();
        hash_payload
            .as_object_mut()
            .unwrap()
            .remove("candidate_sha256");
        let candidate_sha = canonical_json_sha256(&hash_payload);
        request.candidate["candidate_sha256"] = json!(candidate_sha);
        request.review_decision["candidate_sha256"] = json!(candidate_sha);

        let secret = b"test-review-auth-secret-with-at-least-32-bytes";
        let identity_payload = json!({
            "issuer": "test.identity",
            "reviewer_id": "operator@example.test",
            "candidate_id": "arm-base-1",
            "candidate_sha256": candidate_sha,
            "decision": "APPROVE",
            "issued_at_us": (now.timestamp_micros() - 100_000) as u64,
            "expires_at_us": (now.timestamp_micros() + 300_000_000) as u64,
            "nonce": "nonce-locate-arm-base-1"
        });
        let identity_bytes = serde_json::to_vec(&identity_payload).unwrap();
        request.review_identity_assertion = format!(
            "{}.{}",
            base64url_encode(&identity_bytes),
            base64url_encode(&hmac_sha256(secret, &identity_bytes))
        );
        let request_sha = canonical_json_sha256(&serde_json::to_value(&request).unwrap());
        super::build_workcell_activation_record(&request, request_sha, &reports, secret, now)
            .expect("a timely review should remain activatable after its review deadline");
    }

    #[test]
    fn locate_arm_base_activation_rejects_unprofiled_orientation() {
        let now = Utc::now();
        let (mut request, reports) = locate_arm_base_activation_fixture(now);
        request.candidate["quality_provenance"]["orientation_resolution"]
            ["selected_candidate_id"] = json!("arbitrary-vlm-rotation");
        let request_sha = canonical_json_sha256(&serde_json::to_value(&request).unwrap());
        let error = super::build_workcell_activation_record(
            &request,
            request_sha,
            &reports,
            b"test-review-auth-secret-with-at-least-32-bytes",
            now,
        )
        .expect_err("an unprofiled orientation must fail closed");
        assert!(error.to_string().contains("outside the model profile"));
    }

    #[test]
    fn locate_arm_base_activation_rejects_invalid_mask_vote_threshold() {
        let now = Utc::now();
        let (mut request, reports) = locate_arm_base_activation_fixture(now);
        request.candidate["quality_provenance"]["mask_vote"]["vote_threshold"] = json!(1);
        let request_sha = canonical_json_sha256(&serde_json::to_value(&request).unwrap());
        let error = super::build_workcell_activation_record(
            &request,
            request_sha,
            &reports,
            b"test-review-auth-secret-with-at-least-32-bytes",
            now,
        )
        .expect_err("an invalid mask-vote threshold must fail closed");
        assert!(error.to_string().contains("mask-vote proof does not match"));
    }

    fn translation_refinement_request(
        record: &WorkcellCalibrationActivationRecord,
        request_id: &str,
        delta_m: [f64; 3],
    ) -> WorkcellTranslationRefinementRequest {
        let source = record.transforms["world_from_base"].clone();
        let source_translation = source["translation_m"]
            .as_array()
            .expect("source translation")
            .iter()
            .map(|value| value.as_f64().expect("numeric source translation"))
            .collect::<Vec<_>>();
        let proposed_translation = vec![
            source_translation[0] + delta_m[0],
            source_translation[1] + delta_m[1],
            source_translation[2] + delta_m[2],
        ];
        let proposed = json!({
            "translation_m": proposed_translation,
            "rotation_xyzw": source["rotation_xyzw"],
        });
        let raw_norm = delta_m
            .iter()
            .map(|value| value * value)
            .sum::<f64>()
            .sqrt();
        WorkcellTranslationRefinementRequest {
            request_id: request_id.to_string(),
            updated_by: "test-agent".to_string(),
            activation_id: record.activation_id.clone(),
            expected_refinement_revision: record.translation_refinement_revision,
            source_world_from_base: source,
            proposed_world_from_base: proposed,
            refinement: json!({
                "schema": "midbrain.arm_root_translation_refinement",
                "schema_version": 1,
                "status": "TRANSLATION_UPDATE_READY",
                "workflow_complete": true,
                "eligible_for_state_update": true,
                "physical_motion_submitted": false,
                "physical_motion_authorized": false,
                "rotation_change_allowed": false,
                "rotation_change_rad": 0.0,
                "source_revision": record.translation_refinement_revision,
                "adoption_factor": 1.0,
                "raw_translation_delta_m": delta_m,
                "raw_translation_delta_norm_m": raw_norm,
                "adopted_translation_delta_m": delta_m,
                "landmark_id": "profile_landmark",
                "quality_review": {
                    "required": false,
                    "verdict": "NOT_RUN"
                },
                "context_revalidation": {
                    "capture_context_unchanged": true
                },
                "capture_motion": {
                    "policy_id": "TEMPORAL_FK_LANDMARK_MOTION_BOUND_V1",
                    "sample_count": 5,
                    "measured_maximum_landmark_motion_m": 0.001,
                    "maximum_allowed_landmark_motion_m": 0.005,
                    "fk_extrapolation_allowed": false
                },
                "visual_evidence": {
                    "schema": "midbrain.visual_evidence",
                    "schema_version": 1,
                    "evidence_id": format!("evidence-{request_id}")
                },
                "identities": {
                    "world_frame": record.world_frame,
                    "vio_session_epoch": record.session_epoch,
                    "spatial_convention": record.convention_id,
                    "camera_provider_id": record.camera_provider_id,
                    "camera_provider_instance_id": record.camera_provider_instance_id,
                    "camera_boot_id": record.camera_boot_id,
                    "camera_calibration_revision": record.camera_calibration_revision,
                    "arm_provider_id": "robot_arm.example",
                    "arm_provider_instance_id": "robot_arm.example-instance",
                    "arm_boot_id": "robot_arm.example-boot",
                    "arm_model_id": "example_arm",
                    "arm_model_revision": "example-arm-v1",
                    "effector_profile_revision": "example-effector-v1"
                }
            }),
        }
    }

    #[test]
    fn compact_translation_refinement_locks_rotation_and_bounds_journal() {
        let now = Utc::now();
        let (activation, mut reports) = locate_arm_base_activation_fixture(now);
        let activation_sha =
            canonical_json_sha256(&serde_json::to_value(&activation).expect("activation JSON"));
        let mut record = super::build_workcell_activation_record(
            &activation,
            activation_sha,
            &reports,
            b"test-review-auth-secret-with-at-least-32-bytes",
            now,
        )
        .expect("activation record");
        reports.insert(
            "robot_arm.example".to_string(),
            provider_report(
                "robot_arm.example",
                "robot_arm.transforms.local",
                true,
                true,
                "HOT",
            ),
        );
        let initial_rotation = record.transforms["world_from_base"]["rotation_xyzw"].clone();
        for revision in 0..35 {
            let request = translation_refinement_request(
                &record,
                &format!("refine-{revision}"),
                [0.001, 0.0, 0.0],
            );
            let digest =
                canonical_json_sha256(&serde_json::to_value(&request).expect("refinement JSON"));
            record = build_translation_refined_record(&record, &request, &digest, &reports, now)
                .expect("translation refinement");
        }
        assert_eq!(record.translation_refinement_revision, 35);
        assert_eq!(
            record.calibration_revision,
            format!("{}:translation-refinement:35", record.candidate_id)
        );
        assert_eq!(record.translation_refinement_journal.len(), 32);
        assert_eq!(
            record.transforms["world_from_base"]["rotation_xyzw"],
            initial_rotation
        );
        assert_eq!(
            record.translation_refinement_journal[0]["revision_after"],
            4
        );
        assert!(record.translation_refinement_journal[0]["parent_state_link"].is_null());
        assert_eq!(
            record.translation_refinement_journal[31]["calibration_revision_after"],
            record.calibration_revision
        );
    }

    #[test]
    fn compact_translation_refinement_rejects_rotation_change() {
        let now = Utc::now();
        let (activation, mut reports) = locate_arm_base_activation_fixture(now);
        let activation_sha =
            canonical_json_sha256(&serde_json::to_value(&activation).expect("activation JSON"));
        let record = super::build_workcell_activation_record(
            &activation,
            activation_sha,
            &reports,
            b"test-review-auth-secret-with-at-least-32-bytes",
            now,
        )
        .expect("activation record");
        reports.insert(
            "robot_arm.example".to_string(),
            provider_report(
                "robot_arm.example",
                "robot_arm.transforms.local",
                true,
                true,
                "HOT",
            ),
        );
        let mut request =
            translation_refinement_request(&record, "refine-rotation", [0.001, 0.0, 0.0]);
        request.proposed_world_from_base["rotation_xyzw"] = json!([0.0, 0.0, 1.0, 0.0]);
        let error = build_translation_refined_record(&record, &request, "digest", &reports, now)
            .expect_err("rotation change must be rejected");
        assert!(error.to_string().contains("change active rotation"));
    }

    #[test]
    fn compact_translation_refinement_accepts_skill_owned_policy_decision() {
        let now = Utc::now();
        let (activation, mut reports) = locate_arm_base_activation_fixture(now);
        let activation_sha =
            canonical_json_sha256(&serde_json::to_value(&activation).expect("activation JSON"));
        let record = super::build_workcell_activation_record(
            &activation,
            activation_sha,
            &reports,
            b"test-review-auth-secret-with-at-least-32-bytes",
            now,
        )
        .expect("activation record");
        reports.insert(
            "robot_arm.example".to_string(),
            provider_report(
                "robot_arm.example",
                "robot_arm.transforms.local",
                true,
                true,
                "HOT",
            ),
        );
        let mut request =
            translation_refinement_request(&record, "refine-skill-owned-policy", [0.001, 0.0, 0.0]);
        let identities = request.refinement["identities"].clone();
        let adopted_delta = request.refinement["adopted_translation_delta_m"].clone();
        request.refinement = json!({
            "adopted_translation_delta_m": adopted_delta,
            "identities": identities,
            "skill_policy_metadata": {
                "capture_accepted": true,
                "quality_review_accepted": true
            }
        });
        let updated = build_translation_refined_record(&record, &request, "digest", &reports, now)
            .expect("Manager must accept the dedicated Skill's apply decision");
        assert_eq!(updated.translation_refinement_revision, 1);
        assert_eq!(
            updated.transforms["world_from_base"],
            request.proposed_world_from_base
        );
        assert!(
            updated.last_translation_refinement.as_ref().unwrap()["quality_review_verdict"]
                .is_null()
        );
    }

    #[test]
    fn compact_translation_refinement_rejects_inconsistent_adopted_delta() {
        let now = Utc::now();
        let (activation, mut reports) = locate_arm_base_activation_fixture(now);
        let activation_sha =
            canonical_json_sha256(&serde_json::to_value(&activation).expect("activation JSON"));
        let record = super::build_workcell_activation_record(
            &activation,
            activation_sha,
            &reports,
            b"test-review-auth-secret-with-at-least-32-bytes",
            now,
        )
        .expect("activation record");
        reports.insert(
            "robot_arm.example".to_string(),
            provider_report(
                "robot_arm.example",
                "robot_arm.transforms.local",
                true,
                true,
                "HOT",
            ),
        );
        let mut request =
            translation_refinement_request(&record, "refine-bad-adopted-delta", [0.001, 0.0, 0.0]);
        request.refinement["adopted_translation_delta_m"] = json!([0.0005, 0.0, 0.0]);
        let error = build_translation_refined_record(&record, &request, "digest", &reports, now)
            .expect_err("inconsistent adopted delta must be rejected");
        assert!(error.to_string().contains("adopted delta"));
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
    fn provider_hot_dependency_order_is_transitive_and_deduplicated() {
        let configs = HashMap::from([
            (
                "camera.femto_bolt".to_string(),
                provider_config("camera.femto_bolt"),
            ),
            (
                "robot_arm.rebot_dm".to_string(),
                provider_config("robot_arm.rebot_dm"),
            ),
            (
                "perception.sam2_scene_tracker".to_string(),
                provider_config_with_dependencies(
                    "perception.sam2_scene_tracker",
                    &["camera.femto_bolt", "robot_arm.rebot_dm"],
                ),
            ),
            (
                "world_model.arm_scene_compiler".to_string(),
                provider_config_with_dependencies(
                    "world_model.arm_scene_compiler",
                    &["perception.sam2_scene_tracker", "robot_arm.rebot_dm"],
                ),
            ),
        ]);

        let order = provider_dependency_order(&configs, "world_model.arm_scene_compiler")
            .expect("valid dependency graph should resolve");
        assert_eq!(
            order,
            vec![
                "camera.femto_bolt",
                "robot_arm.rebot_dm",
                "perception.sam2_scene_tracker",
                "world_model.arm_scene_compiler",
            ]
        );
    }

    #[test]
    fn provider_hot_dependency_order_rejects_cycles_and_unknown_ids() {
        let cyclic = HashMap::from([
            (
                "provider.a".to_string(),
                provider_config_with_dependencies("provider.a", &["provider.b"]),
            ),
            (
                "provider.b".to_string(),
                provider_config_with_dependencies("provider.b", &["provider.a"]),
            ),
        ]);
        let cycle_error = provider_dependency_order(&cyclic, "provider.a")
            .expect_err("dependency cycle must be rejected");
        assert!(cycle_error.to_string().contains("dependency cycle"));

        let missing = HashMap::from([(
            "provider.a".to_string(),
            provider_config_with_dependencies("provider.a", &["provider.missing"]),
        )]);
        let missing_error = provider_dependency_order(&missing, "provider.a")
            .expect_err("unknown dependency must be rejected");
        assert!(missing_error.to_string().contains("provider.missing"));
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
    fn authority_resource_hierarchy_allows_siblings_and_conflicts_with_parent() {
        assert!(!resource_scopes_overlap(
            "robot_arm.primary/arm",
            "robot_arm.primary/gripper"
        ));
        assert!(resource_scopes_overlap(
            "robot_arm.primary",
            "robot_arm.primary/arm"
        ));
        assert!(resource_scopes_overlap(
            "robot_arm.primary/gripper",
            "robot_arm.primary"
        ));
        assert!(!resource_scopes_overlap(
            "robot_arm.primary",
            "robot_arm.secondary"
        ));
        assert!(validate_resource_id("robot_arm.primary/arm").is_ok());
        assert!(validate_resource_id(" robot_arm.primary/arm").is_err());
        assert!(validate_resource_id("robot_arm.primary/arm bad").is_err());
        assert!(validate_resource_id("robot_arm.primary\\arm").is_err());
    }

    #[test]
    fn capability_binding_can_require_the_selected_resource_group() {
        let configs = HashMap::from([
            (
                "provider.arm-a".to_string(),
                provider_config("provider.arm-a"),
            ),
            (
                "provider.arm-b".to_string(),
                provider_config("provider.arm-b"),
            ),
        ]);
        let mut report_a = provider_report(
            "provider.arm-a",
            "robot.motion.free_space",
            true,
            true,
            "HOT",
        );
        report_a.details["resource_groups"] = json!([{
            "resource_id": "robot_arm.secondary/arm"
        }]);
        let mut report_b = provider_report(
            "provider.arm-b",
            "robot.motion.free_space",
            true,
            true,
            "HOT",
        );
        report_b.details["resource_groups"] = json!([{
            "resource_id": "robot_arm.primary/arm"
        }]);
        let reports = HashMap::from([
            ("provider.arm-a".to_string(), report_a),
            ("provider.arm-b".to_string(), report_b),
        ]);
        let mut request = binding_request("robot.motion.free_space");
        request.required_resources_by_capability.insert(
            "robot.motion.free_space".to_string(),
            "robot_arm.primary/arm".to_string(),
        );

        let binding = build_capability_binding(&configs, &reports, request)
            .expect("resource-qualified binding should resolve");

        assert_eq!(binding.validity, "CURRENT");
        assert_eq!(binding.selections[0].provider_id, "provider.arm-b");
        assert_eq!(
            binding.selections[0].required_resource_id.as_deref(),
            Some("robot_arm.primary/arm")
        );
        assert!(binding.selections[0].compatibility_verified);
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
                    "robot_arm.motion.free_space.preview_commit.v1",
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
    fn shutdown_accepts_explicit_stationary_or_control_unavailable_release() {
        assert!(provider_response_allows_termination(
            true,
            &json!({
                "success": true,
                "termination_allowed": true,
                "safe_state_confirmed": true,
                "details": {
                    "termination_confirmation_method": "MEASURED_STATIONARY_RETRY"
                }
            }),
        ));
        assert!(provider_response_allows_termination(
            true,
            &json!({
                "success": true,
                "termination_allowed": true,
                "safe_state_confirmed": false,
                "details": {
                    "termination_confirmation_method": "CONTROL_UNAVAILABLE_RETRY",
                    "physical_outcome_known": false
                }
            }),
        ));
    }

    #[test]
    fn shutdown_rejects_explicit_termination_denial_and_transport_failure() {
        assert!(!provider_response_allows_termination(
            true,
            &json!({"success": true, "termination_allowed": false}),
        ));
        assert!(!provider_response_allows_termination(
            false,
            &json!({"success": true, "termination_allowed": true}),
        ));
        assert!(provider_response_allows_termination(
            true,
            &json!({"success": true}),
        ));
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

        {
            let mut execution = state.shutdown_execution.lock().await;
            execution
                .as_mut()
                .expect("completed execution remains inspectable")
                .state = "BLOCKED_SAFETY_SUPPORT_RETAINED".to_string();
        }
        let retry_plan = build_shutdown_plan(
            &state.configs,
            &HashMap::new(),
            &HashSet::new(),
            "retry-plan-request".to_string(),
            "test-agent".to_string(),
            "retry after stationary confirmation".to_string(),
        );
        *state.shutdown_plan.lock().await = Some(retry_plan.clone());
        let (retry_status, Json(retry)) = execute_shutdown_plan(
            State(state.clone()),
            Path(retry_plan.shutdown_id.clone()),
            Json(ShutdownExecuteRequest {
                request_id: "retry-execute-request".to_string(),
                confirmation: "EXECUTE_MANAGER_PROVIDER_SHUTDOWN".to_string(),
            }),
        )
        .await
        .expect("a completed blocked shutdown must allow a new execution");
        assert_eq!(retry_status, StatusCode::ACCEPTED);
        assert_eq!(retry.shutdown_id, retry_plan.shutdown_id);
        assert_eq!(
            state.shutdown_fence.lock().await.as_deref(),
            Some(retry_plan.shutdown_id.as_str())
        );
    }
}
