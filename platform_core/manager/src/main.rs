use anyhow::{anyhow, Context, Result};
use axum::{
    extract::{Path, State},
    http::StatusCode,
    routing::{get, post},
    Json, Router,
};
use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::{
    collections::HashMap,
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
    #[serde(default = "default_heartbeat_timeout")]
    heartbeat_timeout_ms: u64,
    #[serde(default)]
    env: HashMap<String, String>,
}

fn default_stop_timeout() -> u64 {
    5_000
}

fn default_heartbeat_timeout() -> u64 {
    3_500
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
    motion_inhibits: Arc<Mutex<HashMap<String, MotionInhibitLease>>>,
    http: reqwest::Client,
    fabric_url: String,
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

    let state = AppState {
        configs: Arc::new(configs),
        processes: Arc::new(Mutex::new(HashMap::new())),
        reports: Arc::new(Mutex::new(HashMap::new())),
        motion_inhibits: Arc::new(Mutex::new(HashMap::new())),
        http: reqwest::Client::new(),
        fabric_url,
    };

    let app = Router::new()
        .route("/health", get(health))
        .route("/v1/providers", get(list_providers))
        .route("/v1/capabilities", get(list_capabilities))
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
        .layer(CorsLayer::permissive())
        .layer(TraceLayer::new_for_http())
        .with_state(state.clone());

    let monitor_state = state.clone();
    tokio::spawn(async move {
        heartbeat_expiry_loop(monitor_state).await;
    });

    let listener = tokio::net::TcpListener::bind(&bind).await?;
    info!(%bind, "Resource Provider Manager listening");

    let auto_state = state.clone();
    tokio::spawn(async move {
        sleep(Duration::from_millis(500)).await;
        for provider in auto_state.configs.values().filter(|p| p.auto_start) {
            if let Err(err) = start_provider_inner(&auto_state, &provider.id).await {
                error!(provider_id = %provider.id, error = %err, "auto-start failed");
            }
        }
    });

    axum::serve(listener, app).await?;
    Ok(())
}

async fn health() -> Json<Value> {
    Json(json!({
        "status": "ok",
        "service": "resource-provider-manager",
        "features": ["capability_catalog", "heartbeat_expiry", "provider_requests", "motion_inhibit"]
    }))
}

async fn list_providers(State(state): State<AppState>) -> Json<Vec<ProviderView>> {
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
    Json(result)
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
                    last_seen: Some(current.last_seen.clone()),
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

async fn register_provider(
    State(state): State<AppState>,
    Json(request): Json<RegisterRequest>,
) -> Result<Json<Value>, (StatusCode, Json<Value>)> {
    if !state.configs.contains_key(&request.provider_id) {
        return Err(api_error(
            StatusCode::NOT_FOUND,
            format!("unknown provider {}", request.provider_id),
        ));
    }
    let report = ProviderReport {
        provider_id: request.provider_id.clone(),
        instance_id: request.instance_id,
        boot_id: request.boot_id,
        residency: request.residency,
        health: request.health,
        ready: request.ready,
        pid: request.pid,
        details: request.details,
        last_seen: Utc::now(),
        expired: false,
    };
    state
        .reports
        .lock()
        .await
        .insert(request.provider_id, report.clone());
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
    let mut reports = state.reports.lock().await;
    let report = reports
        .get_mut(&request.provider_id)
        .ok_or_else(|| api_error(StatusCode::NOT_FOUND, "provider is not registered"))?;
    if report.instance_id != request.instance_id || report.boot_id != request.boot_id {
        return Err(api_error(
            StatusCode::CONFLICT,
            "provider instance or boot id does not match registration",
        ));
    }
    report.residency = request.residency;
    report.health = request.health;
    report.ready = request.ready;
    report.pid = request.pid;
    report.details = request.details;
    report.last_seen = Utc::now();
    report.expired = false;
    let report_snapshot = report.clone();
    drop(reports);
    if let Err(err) = publish_provider_report(&state, &report_snapshot).await {
        warn!(provider_id = %report_snapshot.provider_id, error = %err, "failed to publish provider heartbeat to Fabric");
    }
    Ok(Json(json!({"accepted": true})))
}

async fn start_provider(
    State(state): State<AppState>,
    Path(id): Path<String>,
) -> Result<Json<Value>, (StatusCode, Json<Value>)> {
    start_provider_inner(&state, &id)
        .await
        .map(Json)
        .map_err(internal_error)
}

async fn hot_provider(
    State(state): State<AppState>,
    Path(id): Path<String>,
) -> Result<Json<Value>, (StatusCode, Json<Value>)> {
    ensure_started(&state, &id).await.map_err(internal_error)?;
    call_control(&state, &id, "/v1/control/hot")
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
        .timeout(Duration::from_secs(10))
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
        if force || Instant::now() >= deadline {
            break;
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
                    .signed_duration_since(report.last_seen.clone())
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
        return Ok(());
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
