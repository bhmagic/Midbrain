use anyhow::Result;
use axum::{
    extract::{Path, Query, State},
    http::StatusCode,
    routing::{get, post},
    Json, Router,
};
use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::{
    collections::{HashMap, HashSet, VecDeque},
    env,
    hash::{Hash, Hasher},
    sync::Arc,
};
use tokio::sync::{Notify, RwLock};
use tokio::time::{timeout, Duration, Instant};
use tower_http::{cors::CorsLayer, trace::TraceLayer};
use tracing::info;
use uuid::Uuid;

const DEFAULT_HISTORY_PER_STREAM: usize = 256;
const DEFAULT_TRANSFORM_HISTORY_PER_EDGE: usize = 4096;
const DEFAULT_SYNC_MAX_DELTA_US: u64 = 50_000;
const DEFAULT_TRANSFORM_MAX_EXTRAPOLATION_US: u64 = 100_000;
const MAX_TRANSFORM_BRACKET_WAIT_MS: u64 = 30_000;
const TRANSFORM_SCHEMA: &str = "physical_agent.transform";
const SEMANTIC_SPHERE_SCENE_SCHEMA: &str = "physical_agent.arm_semantic_sphere_scene";
const ARM_POINT_CLOUD_SCHEMA: &str = "physical_agent.arm_point_cloud";
const ARM_SEMANTIC_ASSERTIONS_SCHEMA: &str = "physical_agent.arm_semantic_assertions";
const SEMANTIC_SCENE_CONTRACT_VERSION: u64 = 2;
const GRIPPER_ROI_SCOPE: &str = "GRIPPER_0P5M";
const ARM_BASE_ROI_SCOPE: &str = "ARM_BASE_1P2M";

#[derive(Debug, Clone, Deserialize, Serialize)]
struct Observation {
    #[serde(default = "new_observation_id")]
    observation_id: String,
    schema: String,
    schema_version: u32,
    stream: String,
    provider_id: String,
    provider_instance_id: String,
    boot_id: String,
    sequence: u64,
    observed_at_us: u64,
    #[serde(default)]
    received_at: Option<DateTime<Utc>>,
    #[serde(default)]
    freshness_ms: Option<u64>,
    #[serde(default)]
    frame_id: Option<String>,
    #[serde(default)]
    coordinate_frame: Option<String>,
    #[serde(default)]
    calibration_revision: Option<String>,
    #[serde(default)]
    clock_domain: Option<String>,
    #[serde(default)]
    expires_at_us: Option<u64>,
    #[serde(default)]
    related_skill_id: Option<String>,
    #[serde(default)]
    confidence: Option<f64>,
    #[serde(default)]
    valid: Option<bool>,
    data: Value,
}

fn new_observation_id() -> String {
    Uuid::new_v4().to_string()
}

#[derive(Debug, Deserialize)]
struct ObservationBatch {
    observations: Vec<Observation>,
}

#[derive(Debug, Deserialize)]
struct RecentQuery {
    limit: Option<usize>,
}

#[derive(Debug, Deserialize)]
struct SyncQuery {
    streams: String,
    anchor_stream: Option<String>,
    max_delta_us: Option<u64>,
    require_all: Option<bool>,
}

#[derive(Debug, Serialize)]
struct SyncBundle {
    anchor_stream: String,
    anchor_observed_at_us: u64,
    max_delta_us: u64,
    require_all: bool,
    complete: bool,
    observations: HashMap<String, Observation>,
    deltas_us: HashMap<String, i64>,
    missing_streams: Vec<String>,
    stale_streams: Vec<String>,
}

#[derive(Debug, Serialize)]
struct StreamSummary {
    stream: String,
    schema: String,
    schema_version: u32,
    provider_id: String,
    provider_instance_id: String,
    boot_id: String,
    latest_sequence: u64,
    latest_observed_at_us: u64,
    received_at: Option<DateTime<Utc>>,
    freshness_ms: Option<u64>,
    age_ms: Option<u64>,
    stale: bool,
    history_count: usize,
    coordinate_frame: Option<String>,
    calibration_revision: Option<String>,
    clock_domain: Option<String>,
    valid: Option<bool>,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
struct TransformData {
    parent_frame: String,
    child_frame: String,
    translation_m: [f64; 3],
    rotation_xyzw: [f64; 4],
    #[serde(default)]
    is_static: bool,
    #[serde(default)]
    authority: Option<String>,
    #[serde(default)]
    session_epoch: Option<String>,
    #[serde(default)]
    covariance_6x6: Option<Vec<f64>>,
    #[serde(default)]
    continuity: Option<String>,
}

#[derive(Debug, Clone, Eq, Serialize)]
struct TransformEdgeKey {
    parent_frame: String,
    child_frame: String,
}

impl PartialEq for TransformEdgeKey {
    fn eq(&self, other: &Self) -> bool {
        self.parent_frame == other.parent_frame && self.child_frame == other.child_frame
    }
}

impl Hash for TransformEdgeKey {
    fn hash<H: Hasher>(&self, state: &mut H) {
        self.parent_frame.hash(state);
        self.child_frame.hash(state);
    }
}

#[derive(Debug, Serialize)]
struct TransformEdgeSummary {
    parent_frame: String,
    child_frame: String,
    authority: String,
    provider_id: String,
    provider_instance_id: String,
    latest_observed_at_us: u64,
    is_static: bool,
    session_epoch: Option<String>,
    calibration_revision: Option<String>,
    sample_count: usize,
    stale: bool,
}

#[derive(Debug, Clone, Deserialize)]
struct TransformQuery {
    from_frame: String,
    to_frame: String,
    at_us: Option<u64>,
    max_extrapolation_us: Option<u64>,
    session_epoch: Option<String>,
    wait_for_bracket_ms: Option<u64>,
}

#[derive(Debug, Clone, Copy, Serialize)]
struct RigidTransform {
    translation_m: [f64; 3],
    rotation_xyzw: [f64; 4],
}

impl RigidTransform {
    fn identity() -> Self {
        Self {
            translation_m: [0.0, 0.0, 0.0],
            rotation_xyzw: [0.0, 0.0, 0.0, 1.0],
        }
    }

    fn inverse(self) -> Self {
        let q_inv = quat_conjugate(normalize_quat(self.rotation_xyzw));
        let neg = [
            -self.translation_m[0],
            -self.translation_m[1],
            -self.translation_m[2],
        ];
        Self {
            translation_m: quat_rotate(q_inv, neg),
            rotation_xyzw: q_inv,
        }
    }

    fn compose(self, rhs: Self) -> Self {
        let rotated = quat_rotate(self.rotation_xyzw, rhs.translation_m);
        Self {
            translation_m: [
                self.translation_m[0] + rotated[0],
                self.translation_m[1] + rotated[1],
                self.translation_m[2] + rotated[2],
            ],
            rotation_xyzw: normalize_quat(quat_multiply(self.rotation_xyzw, rhs.rotation_xyzw)),
        }
    }
}

#[derive(Debug, Clone, Serialize)]
struct TransformPathStep {
    from_frame: String,
    to_frame: String,
    parent_frame: String,
    child_frame: String,
    direction: String,
    authority: String,
    provider_id: String,
    provider_instance_id: String,
    observed_at_us: u64,
    interpolated: bool,
    extrapolated_by_us: u64,
    session_epoch: Option<String>,
    calibration_revision: Option<String>,
}

#[derive(Debug, Clone, Serialize)]
struct TransformQueryResult {
    from_frame: String,
    to_frame: String,
    at_us: u64,
    translation_m: [f64; 3],
    rotation_xyzw: [f64; 4],
    path: Vec<TransformPathStep>,
}

#[derive(Debug, Clone)]
struct ResolvedEdge {
    key: TransformEdgeKey,
    transform_parent_from_child: RigidTransform,
    authority: String,
    provider_id: String,
    provider_instance_id: String,
    observed_at_us: u64,
    interpolated: bool,
    extrapolated_by_us: u64,
    session_epoch: Option<String>,
    calibration_revision: Option<String>,
}

#[derive(Debug, Clone)]
struct GraphArc {
    to_frame: String,
    transform_to_from: RigidTransform,
    step: TransformPathStep,
}

#[derive(Debug, Serialize)]
struct SchemaDescriptor {
    schema: &'static str,
    version: u32,
    description: &'static str,
}

#[derive(Debug)]
struct StoredTransform {
    observation: Observation,
    data: TransformData,
    authority: String,
    insertion_id: u64,
}

#[derive(Default)]
struct TransformAuthorityHistory {
    static_samples: Vec<Arc<StoredTransform>>,
    dynamic_samples: Vec<Arc<StoredTransform>>,
}

impl TransformAuthorityHistory {
    fn is_empty(&self) -> bool {
        self.static_samples.is_empty() && self.dynamic_samples.is_empty()
    }

    fn insert(&mut self, sample: Arc<StoredTransform>) {
        let samples = if sample.data.is_static {
            &mut self.static_samples
        } else {
            &mut self.dynamic_samples
        };
        let position = samples.partition_point(|current| {
            current.observation.observed_at_us < sample.observation.observed_at_us
                || (current.observation.observed_at_us == sample.observation.observed_at_us
                    && current.insertion_id < sample.insertion_id)
        });
        samples.insert(position, sample);
    }

    fn remove(&mut self, sample: &StoredTransform) {
        let samples = if sample.data.is_static {
            &mut self.static_samples
        } else {
            &mut self.dynamic_samples
        };
        if let Some(position) = samples
            .iter()
            .position(|current| current.insertion_id == sample.insertion_id)
        {
            samples.remove(position);
        }
    }
}

#[derive(Default)]
struct TransformEdgeHistory {
    insertion_order: VecDeque<Arc<StoredTransform>>,
    authorities: HashMap<String, TransformAuthorityHistory>,
}

impl TransformEdgeHistory {
    fn len(&self) -> usize {
        self.insertion_order.len()
    }

    fn insert(&mut self, sample: Arc<StoredTransform>, maximum_samples: usize) {
        self.authorities
            .entry(sample.authority.clone())
            .or_default()
            .insert(sample.clone());
        self.insertion_order.push_back(sample);

        while self.insertion_order.len() > maximum_samples {
            let Some(evicted) = self.insertion_order.pop_front() else {
                break;
            };
            let remove_authority =
                if let Some(history) = self.authorities.get_mut(&evicted.authority) {
                    history.remove(&evicted);
                    history.is_empty()
                } else {
                    false
                };
            if remove_authority {
                self.authorities.remove(&evicted.authority);
            }
        }
    }
}

#[derive(Default)]
struct FabricStore {
    latest: HashMap<String, Observation>,
    history: HashMap<String, VecDeque<Observation>>,
    transforms: HashMap<TransformEdgeKey, TransformEdgeHistory>,
    accepted: u64,
    next_transform_insertion_id: u64,
}

#[derive(Clone)]
struct AppState {
    store: Arc<RwLock<FabricStore>>,
    transform_updates: Arc<Notify>,
    history_per_stream: usize,
    transform_history_per_edge: usize,
}

#[tokio::main]
async fn main() -> Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "world_state_fabric=info,tower_http=info".into()),
        )
        .init();

    let bind = env::var("FABRIC_BIND").unwrap_or_else(|_| "127.0.0.1:7002".to_string());
    let history_per_stream = env::var("FABRIC_HISTORY_PER_STREAM")
        .ok()
        .and_then(|value| value.parse().ok())
        .unwrap_or(DEFAULT_HISTORY_PER_STREAM);
    let transform_history_per_edge = env::var("FABRIC_TRANSFORM_HISTORY_PER_EDGE")
        .ok()
        .and_then(|value| value.parse().ok())
        .unwrap_or(DEFAULT_TRANSFORM_HISTORY_PER_EDGE);
    let state = AppState {
        store: Arc::new(RwLock::new(FabricStore::default())),
        transform_updates: Arc::new(Notify::new()),
        history_per_stream,
        transform_history_per_edge,
    };

    let app = Router::new()
        .route("/health", get(health))
        .route("/v1/observations", post(publish_observation))
        .route("/v1/observations/batch", post(publish_batch))
        .route("/v1/latest/:stream", get(latest_observation))
        .route("/v1/recent/:stream", get(recent_observations))
        .route("/v1/snapshot", get(snapshot))
        .route("/v1/streams", get(stream_catalog))
        .route("/v1/sync", get(synchronized_bundle))
        .route("/v1/schemas", get(schema_catalog))
        .route("/v1/transforms", get(transform_catalog))
        .route("/v1/transform", get(query_transform))
        .layer(CorsLayer::permissive())
        .layer(TraceLayer::new_for_http())
        .with_state(state);

    let listener = tokio::net::TcpListener::bind(&bind).await?;
    info!(%bind, "World State Fabric listening");
    axum::serve(listener, app).await?;
    Ok(())
}

async fn health(State(state): State<AppState>) -> Json<Value> {
    let store = state.store.read().await;
    Json(json!({
        "status": "ok",
        "service": "world-state-fabric",
        "stream_count": store.latest.len(),
        "transform_edge_count": store.transforms.len(),
        "accepted_observations": store.accepted,
        "features": [
            "stream_catalog",
            "timestamp_nearest_sync",
            "schema_catalog",
            "timestamped_transform_graph",
            "event_driven_transform_bracket_wait"
        ]
    }))
}

async fn schema_catalog() -> Json<Vec<SchemaDescriptor>> {
    Json(vec![
        SchemaDescriptor {
            schema: "physical_agent.buffer_ref",
            version: 1,
            description: "Generation-validated reference to a large payload.",
        },
        SchemaDescriptor {
            schema: "physical_agent.imu_sample",
            version: 1,
            description: "Timestamped calibrated IMU sample.",
        },
        SchemaDescriptor {
            schema: TRANSFORM_SCHEMA,
            version: 1,
            description: "Timestamped rigid transform using metres and xyzw quaternion order.",
        },
        SchemaDescriptor {
            schema: "physical_agent.pose_estimate",
            version: 1,
            description: "Pose, velocity, uncertainty, and localization epoch.",
        },
        SchemaDescriptor {
            schema: "physical_agent.vio_status",
            version: 1,
            description: "Visual-inertial tracking and initialization status.",
        },
        SchemaDescriptor {
            schema: "physical_agent.vio_bias",
            version: 1,
            description: "Session-scoped online IMU bias estimate or explicit unavailable state.",
        },
        SchemaDescriptor {
            schema: "physical_agent.imu_accelerometer_calibration",
            version: 1,
            description: "Device-bound affine accelerometer correction and provenance.",
        },
        SchemaDescriptor {
            schema: "physical_agent.skill_status",
            version: 1,
            description: "Finite Skill lifecycle and structured outcome.",
        },
        SchemaDescriptor {
            schema: "physical_agent.motion_inhibit",
            version: 1,
            description: "Requested or effective whole-robot motion inhibition state.",
        },
        SchemaDescriptor {
            schema: SEMANTIC_SPHERE_SCENE_SCHEMA,
            version: 1,
            description: "Canonical arm-base semantic sphere scene with bounded ROI layers, minimum sphere radii, and explicit contact semantics.",
        },
        SchemaDescriptor {
            schema: ARM_POINT_CLOUD_SCHEMA,
            version: 1,
            description: "Fabric-hosted point-cloud input for the HOT arm scene compiler, using inline metric XYZ or an expiring BufferRef.",
        },
        SchemaDescriptor {
            schema: ARM_SEMANTIC_ASSERTIONS_SCHEMA,
            version: 1,
            description: "Fresh upstream obstacle, pushable, or work-object assertions merged by the HOT arm scene compiler.",
        },
    ])
}

async fn publish_observation(
    State(state): State<AppState>,
    Json(observation): Json<Observation>,
) -> Result<(StatusCode, Json<Value>), (StatusCode, Json<Value>)> {
    let mut store = state.store.write().await;
    let (accepted, transform_accepted) =
        insert_observation_locked(&state, &mut store, observation)?;
    drop(store);
    if transform_accepted {
        state.transform_updates.notify_waiters();
    }
    Ok((StatusCode::ACCEPTED, Json(json!({"accepted": accepted}))))
}

async fn publish_batch(
    State(state): State<AppState>,
    Json(batch): Json<ObservationBatch>,
) -> Result<(StatusCode, Json<Value>), (StatusCode, Json<Value>)> {
    let mut accepted = 0usize;
    let mut transform_accepted = false;
    let mut store = state.store.write().await;
    for observation in batch.observations {
        match insert_observation_locked(&state, &mut store, observation) {
            Ok((was_accepted, was_transform)) => {
                if was_accepted {
                    accepted += 1;
                }
                transform_accepted |= was_transform;
            }
            Err(error) => {
                drop(store);
                if transform_accepted {
                    state.transform_updates.notify_waiters();
                }
                return Err(error);
            }
        }
    }
    drop(store);
    if transform_accepted {
        state.transform_updates.notify_waiters();
    }
    Ok((StatusCode::ACCEPTED, Json(json!({"accepted": accepted}))))
}

fn insert_observation_locked(
    state: &AppState,
    store: &mut FabricStore,
    mut observation: Observation,
) -> Result<(bool, bool), (StatusCode, Json<Value>)> {
    if observation.stream.trim().is_empty() || observation.schema.trim().is_empty() {
        return Err(api_error(
            StatusCode::BAD_REQUEST,
            "stream and schema are required",
        ));
    }
    if observation.observation_id.is_empty() {
        observation.observation_id = new_observation_id();
    }
    if observation.schema == TRANSFORM_SCHEMA {
        validate_transform_observation(&observation)?;
    }
    if observation.schema == SEMANTIC_SPHERE_SCENE_SCHEMA {
        validate_semantic_sphere_scene_observation(&observation)?;
    }
    if observation.schema == ARM_POINT_CLOUD_SCHEMA {
        validate_arm_point_cloud_observation(&observation)?;
    }
    if observation.schema == ARM_SEMANTIC_ASSERTIONS_SCHEMA {
        validate_arm_semantic_assertions_observation(&observation)?;
    }
    observation.received_at = Some(Utc::now());
    let is_transform = observation.schema == TRANSFORM_SCHEMA;

    if let Some(current) = store.latest.get(&observation.stream) {
        if current.provider_instance_id == observation.provider_instance_id
            && current.boot_id == observation.boot_id
            && observation.sequence <= current.sequence
        {
            return Ok((false, false));
        }
    }

    let stream = observation.stream.clone();
    store.latest.insert(stream.clone(), observation.clone());
    let history = store.history.entry(stream).or_default();
    history.push_back(observation.clone());
    while history.len() > state.history_per_stream {
        history.pop_front();
    }

    if is_transform {
        let mut transform = parse_transform(&observation)?;
        transform.rotation_xyzw = normalize_quat(transform.rotation_xyzw);
        let authority = transform_authority(&observation, &transform);
        let key = TransformEdgeKey {
            parent_frame: transform.parent_frame.clone(),
            child_frame: transform.child_frame.clone(),
        };
        let insertion_id = store.next_transform_insertion_id;
        store.next_transform_insertion_id = store.next_transform_insertion_id.wrapping_add(1);
        let sample = Arc::new(StoredTransform {
            observation,
            data: transform,
            authority,
            insertion_id,
        });
        let edge_history = store.transforms.entry(key).or_default();
        edge_history.insert(sample, state.transform_history_per_edge);
    }

    store.accepted += 1;
    Ok((true, is_transform))
}

fn validate_transform_observation(
    observation: &Observation,
) -> Result<(), (StatusCode, Json<Value>)> {
    let transform = parse_transform(observation)?;
    if let Some(motion_usable) = observation.data.get("motion_usable") {
        if !motion_usable.is_boolean() {
            return Err(api_error(
                StatusCode::BAD_REQUEST,
                "transform motion_usable must be boolean when present",
            ));
        }
    }
    if observation.data.get("review_state").and_then(Value::as_str)
        == Some("CANDIDATE_REVIEW_REQUIRED")
        && observation
            .data
            .get("motion_usable")
            .and_then(Value::as_bool)
            != Some(false)
    {
        return Err(api_error(
            StatusCode::BAD_REQUEST,
            "review-required transform must explicitly set motion_usable=false",
        ));
    }
    if transform.parent_frame.trim().is_empty() || transform.child_frame.trim().is_empty() {
        return Err(api_error(
            StatusCode::BAD_REQUEST,
            "transform parent_frame and child_frame are required",
        ));
    }
    if transform.parent_frame == transform.child_frame {
        return Err(api_error(
            StatusCode::BAD_REQUEST,
            "transform parent_frame and child_frame must differ",
        ));
    }
    if !transform
        .translation_m
        .iter()
        .all(|value| value.is_finite())
        || !transform
            .rotation_xyzw
            .iter()
            .all(|value| value.is_finite())
    {
        return Err(api_error(
            StatusCode::BAD_REQUEST,
            "transform values must be finite",
        ));
    }
    let norm = quat_norm(transform.rotation_xyzw);
    if norm < 1e-9 {
        return Err(api_error(
            StatusCode::BAD_REQUEST,
            "transform quaternion must have non-zero norm",
        ));
    }
    if let Some(covariance) = transform.covariance_6x6.as_ref() {
        if covariance.len() != 36 || !covariance.iter().all(|value| value.is_finite()) {
            return Err(api_error(
                StatusCode::BAD_REQUEST,
                "transform covariance_6x6 must contain 36 finite values",
            ));
        }
    }
    Ok(())
}

fn parse_transform(observation: &Observation) -> Result<TransformData, (StatusCode, Json<Value>)> {
    serde_json::from_value(observation.data.clone()).map_err(|error| {
        api_error(
            StatusCode::BAD_REQUEST,
            format!("invalid physical_agent.transform payload: {error}"),
        )
    })
}

fn finite_vec3(value: Option<&Value>) -> Option<[f64; 3]> {
    let items = value?.as_array()?;
    if items.len() != 3 {
        return None;
    }
    let point = [items[0].as_f64()?, items[1].as_f64()?, items[2].as_f64()?];
    point.iter().all(|item| item.is_finite()).then_some(point)
}

fn semantic_roi_limits(scope: &str) -> Option<(f64, f64)> {
    match scope {
        GRIPPER_ROI_SCOPE => Some((0.5, 0.02)),
        ARM_BASE_ROI_SCOPE => Some((1.2, 0.06)),
        _ => None,
    }
}

fn validate_arm_point_cloud_observation(
    observation: &Observation,
) -> Result<(), (StatusCode, Json<Value>)> {
    if observation.schema_version != 1 {
        return Err(api_error(
            StatusCode::BAD_REQUEST,
            "arm point-cloud schema_version must be 1",
        ));
    }
    let data = observation.data.as_object().ok_or_else(|| {
        api_error(
            StatusCode::BAD_REQUEST,
            "arm point-cloud data must be an object",
        )
    })?;
    if data.get("contract_version").and_then(Value::as_u64) != Some(1) {
        return Err(api_error(
            StatusCode::BAD_REQUEST,
            "arm point-cloud contract_version must be 1",
        ));
    }
    let frame = observation
        .coordinate_frame
        .as_deref()
        .or_else(|| data.get("coordinate_frame").and_then(Value::as_str))
        .map(str::trim)
        .filter(|value| !value.is_empty());
    if frame.is_none() {
        return Err(api_error(
            StatusCode::BAD_REQUEST,
            "arm point-cloud coordinate_frame is required",
        ));
    }
    match observation.freshness_ms {
        Some(value) if (1..=2_000).contains(&value) => {}
        _ => {
            return Err(api_error(
                StatusCode::BAD_REQUEST,
                "arm point-cloud freshness_ms must be between 1 and 2000",
            ))
        }
    }
    let inline = data.get("points_m").and_then(Value::as_array);
    let buffer_ref = data.get("buffer_ref").and_then(Value::as_object);
    if inline.is_some() == buffer_ref.is_some() {
        return Err(api_error(
            StatusCode::BAD_REQUEST,
            "arm point-cloud requires exactly one of points_m or buffer_ref",
        ));
    }
    if let Some(points) = inline {
        if points.len() > 250_000
            || points
                .iter()
                .any(|point| finite_vec3(Some(point)).is_none())
        {
            return Err(api_error(
                StatusCode::BAD_REQUEST,
                "arm point-cloud points_m must contain at most 250000 finite XYZ points",
            ));
        }
    }
    if let Some(reference) = buffer_ref {
        let mapping_name = reference
            .get("mapping_name")
            .and_then(Value::as_str)
            .map(str::trim)
            .filter(|value| !value.is_empty());
        let payload_bytes = reference
            .get("payload_bytes")
            .and_then(Value::as_u64)
            .unwrap_or(0);
        if mapping_name.is_none() || payload_bytes < 12 {
            return Err(api_error(
                StatusCode::BAD_REQUEST,
                "arm point-cloud buffer_ref requires mapping_name and payload_bytes >= 12",
            ));
        }
        if data.get("point_encoding").and_then(Value::as_str) != Some("XYZ_F32_LE") {
            return Err(api_error(
                StatusCode::BAD_REQUEST,
                "arm point-cloud BufferRef point_encoding must be XYZ_F32_LE",
            ));
        }
    }
    if !matches!(data.get("units").and_then(Value::as_str), Some("m" | "mm")) {
        return Err(api_error(
            StatusCode::BAD_REQUEST,
            "arm point-cloud units must be m or mm",
        ));
    }
    Ok(())
}

fn validate_arm_semantic_assertions_observation(
    observation: &Observation,
) -> Result<(), (StatusCode, Json<Value>)> {
    if observation.schema_version != 1 {
        return Err(api_error(
            StatusCode::BAD_REQUEST,
            "arm semantic assertions schema_version must be 1",
        ));
    }
    let data = observation.data.as_object().ok_or_else(|| {
        api_error(
            StatusCode::BAD_REQUEST,
            "arm semantic assertions data must be an object",
        )
    })?;
    if data.get("contract_version").and_then(Value::as_u64) != Some(1) {
        return Err(api_error(
            StatusCode::BAD_REQUEST,
            "arm semantic assertions contract_version must be 1",
        ));
    }
    let frame = data
        .get("frame_id")
        .and_then(Value::as_str)
        .or(observation.coordinate_frame.as_deref())
        .map(str::trim)
        .filter(|value| !value.is_empty());
    if frame.is_none() {
        return Err(api_error(
            StatusCode::BAD_REQUEST,
            "arm semantic assertions frame_id is required",
        ));
    }
    match observation.freshness_ms {
        Some(value) if (1..=60_000).contains(&value) => {}
        _ => {
            return Err(api_error(
                StatusCode::BAD_REQUEST,
                "arm semantic assertions freshness_ms must be between 1 and 60000",
            ))
        }
    }
    let assertions = data
        .get("assertions")
        .and_then(Value::as_array)
        .ok_or_else(|| {
            api_error(
                StatusCode::BAD_REQUEST,
                "arm semantic assertions requires an assertions array",
            )
        })?;
    if assertions.len() > 20_000 {
        return Err(api_error(
            StatusCode::BAD_REQUEST,
            "arm semantic assertions exceeds 20000 spheres",
        ));
    }
    let mut geometry_ids = HashSet::new();
    for assertion in assertions {
        let object = assertion.as_object().ok_or_else(|| {
            api_error(
                StatusCode::BAD_REQUEST,
                "arm semantic assertion must be an object",
            )
        })?;
        let object_id = object
            .get("object_id")
            .and_then(Value::as_str)
            .or_else(|| object.get("assertion_id").and_then(Value::as_str))
            .map(str::trim)
            .filter(|value| !value.is_empty())
            .ok_or_else(|| {
                api_error(
                    StatusCode::BAD_REQUEST,
                    "arm semantic assertion requires object_id",
                )
            })?;
        let geometry_id = object
            .get("assertion_id")
            .and_then(Value::as_str)
            .or_else(|| object.get("sphere_id").and_then(Value::as_str))
            .map(str::trim)
            .filter(|value| !value.is_empty())
            .unwrap_or(object_id);
        if !geometry_ids.insert(geometry_id.to_string()) {
            return Err(api_error(
                StatusCode::BAD_REQUEST,
                "arm semantic assertion geometry identifiers must be unique",
            ));
        }
        if finite_vec3(object.get("center_m")).is_none() {
            return Err(api_error(
                StatusCode::BAD_REQUEST,
                "arm semantic assertion center_m must contain three finite values",
            ));
        }
        let radius = object
            .get("radius_m")
            .and_then(Value::as_f64)
            .unwrap_or(f64::NAN);
        if !radius.is_finite() || radius <= 0.0 {
            return Err(api_error(
                StatusCode::BAD_REQUEST,
                "arm semantic assertion radius_m must be positive and finite",
            ));
        }
        let object_type = object.get("type").and_then(Value::as_str).unwrap_or("");
        if !matches!(
            object_type,
            "" | "OBS"
                | "OBSTACLE"
                | "KEEP_OUT"
                | "PUSHABLE"
                | "WORKPIECE"
                | "WORK_PIECE"
                | "WORK_OBJECT"
        ) {
            return Err(api_error(
                StatusCode::BAD_REQUEST,
                "arm semantic assertion type is unsupported",
            ));
        }
        if object_type == "KEEP_OUT"
            && object
                .get("description")
                .and_then(Value::as_str)
                .map(str::trim)
                .filter(|value| !value.is_empty())
                .is_none()
        {
            return Err(api_error(
                StatusCode::BAD_REQUEST,
                "KEEP_OUT arm semantic assertions require an upstream description",
            ));
        }
    }
    Ok(())
}

fn validate_semantic_sphere_scene_observation(
    observation: &Observation,
) -> Result<(), (StatusCode, Json<Value>)> {
    if observation.schema_version != 1 {
        return Err(api_error(
            StatusCode::BAD_REQUEST,
            "semantic sphere scene schema_version must be 1",
        ));
    }
    let data = observation.data.as_object().ok_or_else(|| {
        api_error(
            StatusCode::BAD_REQUEST,
            "semantic sphere scene data must be an object",
        )
    })?;
    if data.get("contract_version").and_then(Value::as_u64) != Some(SEMANTIC_SCENE_CONTRACT_VERSION)
    {
        return Err(api_error(
            StatusCode::BAD_REQUEST,
            "semantic sphere scene contract_version must be 2",
        ));
    }
    if data
        .get("scene_revision")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .is_none()
    {
        return Err(api_error(
            StatusCode::BAD_REQUEST,
            "semantic sphere scene requires scene_revision",
        ));
    }
    if data.get("frame_id").and_then(Value::as_str) != Some("rebot_arm_base") {
        return Err(api_error(
            StatusCode::BAD_REQUEST,
            "semantic sphere scene frame_id must be rebot_arm_base",
        ));
    }

    let raw_layers = data
        .get("roi_layers")
        .and_then(Value::as_array)
        .filter(|layers| !layers.is_empty())
        .ok_or_else(|| {
            api_error(
                StatusCode::BAD_REQUEST,
                "semantic sphere scene requires at least one ROI layer",
            )
        })?;
    let mut layers: HashMap<String, ([f64; 3], f64, f64)> = HashMap::new();
    for value in raw_layers {
        let layer = value.as_object().ok_or_else(|| {
            api_error(
                StatusCode::BAD_REQUEST,
                "semantic ROI layer must be an object",
            )
        })?;
        let scope = layer
            .get("scope")
            .and_then(Value::as_str)
            .map(str::trim)
            .unwrap_or("");
        let (expected_radius, expected_minimum) = semantic_roi_limits(scope).ok_or_else(|| {
            api_error(
                StatusCode::BAD_REQUEST,
                format!("unsupported semantic ROI scope {scope:?}"),
            )
        })?;
        let center = finite_vec3(layer.get("center_m")).ok_or_else(|| {
            api_error(
                StatusCode::BAD_REQUEST,
                "semantic ROI center_m must contain three finite numbers",
            )
        })?;
        let radius = layer
            .get("radius_m")
            .and_then(Value::as_f64)
            .unwrap_or(f64::NAN);
        let minimum = layer
            .get("minimum_sphere_radius_m")
            .and_then(Value::as_f64)
            .unwrap_or(f64::NAN);
        if (radius - expected_radius).abs() > 1e-9 || (minimum - expected_minimum).abs() > 1e-9 {
            return Err(api_error(
                StatusCode::BAD_REQUEST,
                format!("semantic ROI {scope} has non-canonical radius policy"),
            ));
        }
        if scope == ARM_BASE_ROI_SCOPE && center.iter().any(|item| item.abs() > 1e-9) {
            return Err(api_error(
                StatusCode::BAD_REQUEST,
                "ARM_BASE_1P2M ROI center must be the arm-base origin",
            ));
        }
        if layers
            .insert(scope.to_string(), (center, radius, minimum))
            .is_some()
        {
            return Err(api_error(
                StatusCode::BAD_REQUEST,
                "semantic scene ROI scopes must be unique",
            ));
        }
    }

    let raw_spheres = data
        .get("spheres")
        .and_then(Value::as_array)
        .ok_or_else(|| {
            api_error(
                StatusCode::BAD_REQUEST,
                "semantic sphere scene spheres must be an array",
            )
        })?;
    let mut sphere_ids = HashSet::new();
    for value in raw_spheres {
        let sphere = value.as_object().ok_or_else(|| {
            api_error(StatusCode::BAD_REQUEST, "semantic sphere must be an object")
        })?;
        let sphere_id = sphere
            .get("sphere_id")
            .and_then(Value::as_str)
            .map(str::trim)
            .filter(|value| !value.is_empty())
            .ok_or_else(|| {
                api_error(
                    StatusCode::BAD_REQUEST,
                    "semantic sphere requires sphere_id",
                )
            })?;
        if !sphere_ids.insert(sphere_id.to_string()) {
            return Err(api_error(
                StatusCode::BAD_REQUEST,
                "semantic scene sphere_id values must be unique",
            ));
        }
        if sphere
            .get("object_id")
            .and_then(Value::as_str)
            .map(str::trim)
            .filter(|value| !value.is_empty())
            .is_none()
        {
            return Err(api_error(
                StatusCode::BAD_REQUEST,
                "semantic sphere requires object_id",
            ));
        }
        let object_type = sphere.get("type").and_then(Value::as_str).unwrap_or("");
        if !matches!(object_type, "KEEP_OUT" | "PUSHABLE" | "WORK_OBJECT") {
            return Err(api_error(
                StatusCode::BAD_REQUEST,
                "semantic sphere type must be KEEP_OUT, PUSHABLE, or WORK_OBJECT",
            ));
        }
        let scope = sphere
            .get("roi_scope")
            .and_then(Value::as_str)
            .unwrap_or("");
        let (roi_center, roi_radius, minimum_radius) = layers.get(scope).ok_or_else(|| {
            api_error(
                StatusCode::BAD_REQUEST,
                format!("semantic sphere references absent ROI scope {scope:?}"),
            )
        })?;
        let center = finite_vec3(sphere.get("center_m")).ok_or_else(|| {
            api_error(
                StatusCode::BAD_REQUEST,
                "semantic sphere center_m must contain three finite numbers",
            )
        })?;
        let radius = sphere
            .get("radius_m")
            .and_then(Value::as_f64)
            .unwrap_or(f64::NAN);
        if !radius.is_finite() || radius < *minimum_radius {
            return Err(api_error(
                StatusCode::BAD_REQUEST,
                format!("semantic sphere {sphere_id:?} is below its ROI minimum radius"),
            ));
        }
        let distance = center
            .iter()
            .zip(roi_center.iter())
            .map(|(left, right)| (left - right).powi(2))
            .sum::<f64>()
            .sqrt();
        if distance > *roi_radius + 1e-12 {
            return Err(api_error(
                StatusCode::BAD_REQUEST,
                format!("semantic sphere {sphere_id:?} is outside its ROI"),
            ));
        }
    }
    Ok(())
}

async fn latest_observation(
    State(state): State<AppState>,
    Path(stream): Path<String>,
) -> Result<Json<Observation>, (StatusCode, Json<Value>)> {
    let store = state.store.read().await;
    store.latest.get(&stream).cloned().map(Json).ok_or_else(|| {
        api_error(
            StatusCode::NOT_FOUND,
            format!("no observation for {stream}"),
        )
    })
}

async fn recent_observations(
    State(state): State<AppState>,
    Path(stream): Path<String>,
    Query(query): Query<RecentQuery>,
) -> Result<Json<Vec<Observation>>, (StatusCode, Json<Value>)> {
    let store = state.store.read().await;
    let history = store.history.get(&stream).ok_or_else(|| {
        api_error(
            StatusCode::NOT_FOUND,
            format!("no observations for {stream}"),
        )
    })?;
    let limit = query.limit.unwrap_or(32).min(state.history_per_stream);
    let start = history.len().saturating_sub(limit);
    Ok(Json(history.iter().skip(start).cloned().collect()))
}

async fn snapshot(State(state): State<AppState>) -> Json<HashMap<String, Observation>> {
    Json(state.store.read().await.latest.clone())
}

async fn stream_catalog(State(state): State<AppState>) -> Json<Vec<StreamSummary>> {
    let store = state.store.read().await;
    let now = Utc::now();
    let mut streams: Vec<StreamSummary> = store
        .latest
        .iter()
        .map(|(stream_name, observation)| {
            let age_ms = observation.received_at.as_ref().map(|received| {
                now.signed_duration_since(*received)
                    .num_milliseconds()
                    .max(0) as u64
            });
            let stale = match (observation.freshness_ms, age_ms) {
                (Some(freshness_ms), Some(age_ms)) => age_ms > freshness_ms,
                _ => false,
            };
            StreamSummary {
                stream: stream_name.clone(),
                schema: observation.schema.clone(),
                schema_version: observation.schema_version,
                provider_id: observation.provider_id.clone(),
                provider_instance_id: observation.provider_instance_id.clone(),
                boot_id: observation.boot_id.clone(),
                latest_sequence: observation.sequence,
                latest_observed_at_us: observation.observed_at_us,
                received_at: observation.received_at,
                freshness_ms: observation.freshness_ms,
                age_ms,
                stale,
                history_count: store.history.get(stream_name).map_or(0, VecDeque::len),
                coordinate_frame: observation.coordinate_frame.clone(),
                calibration_revision: observation.calibration_revision.clone(),
                clock_domain: observation.clock_domain.clone(),
                valid: observation.valid,
            }
        })
        .collect();
    streams.sort_by(|a, b| a.stream.cmp(&b.stream));
    Json(streams)
}

async fn synchronized_bundle(
    State(state): State<AppState>,
    Query(query): Query<SyncQuery>,
) -> Result<(StatusCode, Json<SyncBundle>), (StatusCode, Json<Value>)> {
    let requested_streams: Vec<String> = query
        .streams
        .split(',')
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(ToOwned::to_owned)
        .collect();
    if requested_streams.is_empty() {
        return Err(api_error(StatusCode::BAD_REQUEST, "streams is required"));
    }

    let anchor_stream = query
        .anchor_stream
        .filter(|value| !value.trim().is_empty())
        .unwrap_or_else(|| requested_streams[0].clone());
    let max_delta_us = query.max_delta_us.unwrap_or(DEFAULT_SYNC_MAX_DELTA_US);
    let require_all = query.require_all.unwrap_or(true);

    let store = state.store.read().await;
    let now = Utc::now();
    let anchor = store.latest.get(&anchor_stream).cloned().ok_or_else(|| {
        api_error(
            StatusCode::NOT_FOUND,
            format!("no observation for anchor stream {anchor_stream}"),
        )
    })?;
    if observation_is_stale(&anchor, &now) {
        return Err(api_error(
            StatusCode::CONFLICT,
            format!("anchor stream {anchor_stream} is stale"),
        ));
    }
    let anchor_time = anchor.observed_at_us;

    let mut observations = HashMap::new();
    let mut deltas_us = HashMap::new();
    let mut missing_streams = Vec::new();
    let mut stale_streams = Vec::new();

    for stream in &requested_streams {
        let matched = store
            .history
            .get(stream)
            .and_then(|history| nearest_fresh_observation(history, anchor_time, &now));
        match matched {
            Some(observation) => {
                let delta = observation.observed_at_us as i128 - anchor_time as i128;
                if delta.unsigned_abs() <= max_delta_us as u128 {
                    observations.insert(stream.clone(), observation.clone());
                    deltas_us.insert(stream.clone(), delta as i64);
                } else {
                    missing_streams.push(stream.clone());
                }
            }
            None => {
                missing_streams.push(stream.clone());
                if store
                    .latest
                    .get(stream)
                    .is_some_and(|observation| observation_is_stale(observation, &now))
                {
                    stale_streams.push(stream.clone());
                }
            }
        }
    }

    let complete = missing_streams.is_empty();
    let bundle = SyncBundle {
        anchor_stream,
        anchor_observed_at_us: anchor_time,
        max_delta_us,
        require_all,
        complete,
        observations,
        deltas_us,
        missing_streams,
        stale_streams,
    };

    if require_all && !bundle.complete {
        return Ok((StatusCode::CONFLICT, Json(bundle)));
    }

    Ok((StatusCode::OK, Json(bundle)))
}

async fn transform_catalog(State(state): State<AppState>) -> Json<Vec<TransformEdgeSummary>> {
    let store = state.store.read().await;
    let now = Utc::now();
    let mut result = Vec::new();
    for (key, history) in &store.transforms {
        for (authority, samples) in &history.authorities {
            let sample = if let Some(latest_static) = samples.static_samples.last() {
                if !transform_observation_is_graph_usable(&latest_static.observation) {
                    continue;
                }
                latest_static
            } else if let Some(latest_dynamic) = samples
                .dynamic_samples
                .iter()
                .rev()
                .find(|sample| transform_observation_is_graph_usable(&sample.observation))
            {
                latest_dynamic
            } else {
                continue;
            };
            let observation = &sample.observation;
            result.push(TransformEdgeSummary {
                parent_frame: key.parent_frame.clone(),
                child_frame: key.child_frame.clone(),
                authority: authority.clone(),
                provider_id: observation.provider_id.clone(),
                provider_instance_id: observation.provider_instance_id.clone(),
                latest_observed_at_us: observation.observed_at_us,
                is_static: sample.data.is_static,
                session_epoch: sample.data.session_epoch.clone(),
                calibration_revision: observation.calibration_revision.clone(),
                sample_count: history.len(),
                stale: observation_is_stale(observation, &now),
            });
        }
    }
    result.sort_by(|a, b| {
        a.parent_frame
            .cmp(&b.parent_frame)
            .then_with(|| a.child_frame.cmp(&b.child_frame))
            .then_with(|| a.authority.cmp(&b.authority))
    });
    Json(result)
}

async fn query_transform(
    State(state): State<AppState>,
    Query(query): Query<TransformQuery>,
) -> Result<Json<TransformQueryResult>, (StatusCode, Json<Value>)> {
    let from_frame = query.from_frame.trim().to_string();
    let to_frame = query.to_frame.trim().to_string();
    if from_frame.is_empty() || to_frame.is_empty() {
        return Err(api_error(
            StatusCode::BAD_REQUEST,
            "from_frame and to_frame are required",
        ));
    }

    let at_us = query.at_us.unwrap_or_else(current_time_us);
    if from_frame == to_frame {
        return Ok(Json(TransformQueryResult {
            from_frame,
            to_frame,
            at_us,
            translation_m: [0.0, 0.0, 0.0],
            rotation_xyzw: [0.0, 0.0, 0.0, 1.0],
            path: Vec::new(),
        }));
    }

    let max_extrapolation_us = query
        .max_extrapolation_us
        .unwrap_or(DEFAULT_TRANSFORM_MAX_EXTRAPOLATION_US);
    let wait_for_bracket_ms = query
        .wait_for_bracket_ms
        .unwrap_or(0)
        .min(MAX_TRANSFORM_BRACKET_WAIT_MS);
    if wait_for_bracket_ms == 0 {
        return resolve_transform_once(
            &state,
            from_frame,
            to_frame,
            at_us,
            max_extrapolation_us,
            query.session_epoch.as_deref(),
        )
        .await;
    }

    let deadline = Instant::now() + Duration::from_millis(wait_for_bracket_ms);
    loop {
        let notified = state.transform_updates.notified();
        tokio::pin!(notified);
        notified.as_mut().enable();

        let outcome = resolve_transform_once(
            &state,
            from_frame.clone(),
            to_frame.clone(),
            at_us,
            max_extrapolation_us,
            query.session_epoch.as_deref(),
        )
        .await;
        if !transform_query_needs_bracket(&outcome) {
            return outcome;
        }

        let now = Instant::now();
        if now >= deadline {
            return outcome;
        }
        if timeout(deadline - now, notified).await.is_err() {
            return outcome;
        }
    }
}

fn transform_query_needs_bracket(
    outcome: &Result<Json<TransformQueryResult>, (StatusCode, Json<Value>)>,
) -> bool {
    match outcome {
        Ok(result) => result.path.iter().any(|step| step.extrapolated_by_us > 0),
        Err((status, _)) => *status == StatusCode::NOT_FOUND,
    }
}

async fn resolve_transform_once(
    state: &AppState,
    from_frame: String,
    to_frame: String,
    at_us: u64,
    max_extrapolation_us: u64,
    session_epoch: Option<&str>,
) -> Result<Json<TransformQueryResult>, (StatusCode, Json<Value>)> {
    let (resolved_edges, conflicts) = {
        let store = state.store.read().await;
        let mut resolved_edges = Vec::new();
        let mut conflicts = Vec::new();
        for (key, history) in &store.transforms {
            match resolve_edge(key, history, at_us, max_extrapolation_us, session_epoch) {
                Ok(Some(edge)) => resolved_edges.push(edge),
                Ok(None) => {}
                Err(authorities) => conflicts.push(json!({
                    "parent_frame": key.parent_frame,
                    "child_frame": key.child_frame,
                    "authorities": authorities,
                })),
            }
        }
        (resolved_edges, conflicts)
    };

    let mut adjacency: HashMap<String, Vec<GraphArc>> = HashMap::new();
    for edge in resolved_edges {
        add_resolved_edge_to_graph(&mut adjacency, edge);
    }

    let mut queue = VecDeque::new();
    let mut visited: HashSet<String> = HashSet::new();
    let mut transforms: HashMap<String, RigidTransform> = HashMap::new();
    let mut paths: HashMap<String, Vec<TransformPathStep>> = HashMap::new();
    queue.push_back(from_frame.clone());
    visited.insert(from_frame.clone());
    transforms.insert(from_frame.clone(), RigidTransform::identity());
    paths.insert(from_frame.clone(), Vec::new());

    while let Some(current) = queue.pop_front() {
        if current == to_frame {
            let transform = transforms[&current];
            return Ok(Json(TransformQueryResult {
                from_frame,
                to_frame,
                at_us,
                translation_m: transform.translation_m,
                rotation_xyzw: transform.rotation_xyzw,
                path: paths.remove(&current).unwrap_or_default(),
            }));
        }
        let Some(arcs) = adjacency.get(&current) else {
            continue;
        };
        for arc in arcs {
            if visited.contains(&arc.to_frame) {
                continue;
            }
            let current_transform = transforms[&current];
            let next_transform = arc.transform_to_from.compose(current_transform);
            let mut next_path = paths[&current].clone();
            next_path.push(arc.step.clone());
            transforms.insert(arc.to_frame.clone(), next_transform);
            paths.insert(arc.to_frame.clone(), next_path);
            visited.insert(arc.to_frame.clone());
            queue.push_back(arc.to_frame.clone());
        }
    }

    if !conflicts.is_empty() {
        return Err((
            StatusCode::CONFLICT,
            Json(json!({
                "error": "no unambiguous transform path",
                "from_frame": from_frame,
                "to_frame": to_frame,
                "at_us": at_us,
                "conflicts": conflicts,
            })),
        ));
    }

    Err(api_error(
        StatusCode::NOT_FOUND,
        format!("no transform path from {from_frame} to {to_frame} at {at_us}"),
    ))
}

fn add_resolved_edge_to_graph(adjacency: &mut HashMap<String, Vec<GraphArc>>, edge: ResolvedEdge) {
    let child_to_parent = TransformPathStep {
        from_frame: edge.key.child_frame.clone(),
        to_frame: edge.key.parent_frame.clone(),
        parent_frame: edge.key.parent_frame.clone(),
        child_frame: edge.key.child_frame.clone(),
        direction: "child_to_parent".to_string(),
        authority: edge.authority.clone(),
        provider_id: edge.provider_id.clone(),
        provider_instance_id: edge.provider_instance_id.clone(),
        observed_at_us: edge.observed_at_us,
        interpolated: edge.interpolated,
        extrapolated_by_us: edge.extrapolated_by_us,
        session_epoch: edge.session_epoch.clone(),
        calibration_revision: edge.calibration_revision.clone(),
    };
    adjacency
        .entry(edge.key.child_frame.clone())
        .or_default()
        .push(GraphArc {
            to_frame: edge.key.parent_frame.clone(),
            transform_to_from: edge.transform_parent_from_child,
            step: child_to_parent,
        });

    let parent_to_child = TransformPathStep {
        from_frame: edge.key.parent_frame.clone(),
        to_frame: edge.key.child_frame.clone(),
        parent_frame: edge.key.parent_frame.clone(),
        child_frame: edge.key.child_frame.clone(),
        direction: "parent_to_child".to_string(),
        authority: edge.authority,
        provider_id: edge.provider_id,
        provider_instance_id: edge.provider_instance_id,
        observed_at_us: edge.observed_at_us,
        interpolated: edge.interpolated,
        extrapolated_by_us: edge.extrapolated_by_us,
        session_epoch: edge.session_epoch,
        calibration_revision: edge.calibration_revision,
    };
    adjacency
        .entry(edge.key.parent_frame)
        .or_default()
        .push(GraphArc {
            to_frame: edge.key.child_frame,
            transform_to_from: edge.transform_parent_from_child.inverse(),
            step: parent_to_child,
        });
}

fn resolve_edge(
    key: &TransformEdgeKey,
    history: &TransformEdgeHistory,
    at_us: u64,
    max_extrapolation_us: u64,
    requested_session_epoch: Option<&str>,
) -> Result<Option<ResolvedEdge>, Vec<String>> {
    let mut candidates = Vec::new();
    for (authority, samples) in &history.authorities {
        if let Some(latest_static) = samples.static_samples.last() {
            if !transform_observation_is_graph_usable(&latest_static.observation) {
                continue;
            }
            candidates.push(resolved_from_sample(
                key,
                authority.clone(),
                latest_static,
                false,
                0,
            ));
            continue;
        }
        if let Some(candidate) = resolve_authority_samples(
            key,
            authority.clone(),
            &samples.dynamic_samples,
            at_us,
            max_extrapolation_us,
            requested_session_epoch,
        ) {
            candidates.push(candidate);
        }
    }

    if candidates.len() > 1 {
        return Err(candidates
            .into_iter()
            .map(|value| value.authority)
            .collect());
    }
    Ok(candidates.pop())
}

fn resolve_authority_samples(
    key: &TransformEdgeKey,
    authority: String,
    samples: &[Arc<StoredTransform>],
    at_us: u64,
    max_extrapolation_us: u64,
    requested_session_epoch: Option<&str>,
) -> Option<ResolvedEdge> {
    if samples.is_empty() {
        return None;
    }

    let is_eligible = |sample: &&Arc<StoredTransform>| {
        transform_observation_is_graph_usable(&sample.observation)
            && requested_session_epoch
                .is_none_or(|requested| sample.data.session_epoch.as_deref() == Some(requested))
    };
    let before_end = samples.partition_point(|sample| sample.observation.observed_at_us <= at_us);
    let before = samples[..before_end]
        .iter()
        .rev()
        .find(is_eligible)
        .map(Arc::as_ref);
    let after_start = samples.partition_point(|sample| sample.observation.observed_at_us < at_us);
    let after = samples[after_start..]
        .iter()
        .find(is_eligible)
        .map(Arc::as_ref);

    match (before, after) {
        (Some(left), Some(right))
            if left.observation.observed_at_us != right.observation.observed_at_us
                && left.data.session_epoch == right.data.session_epoch =>
        {
            let span = right.observation.observed_at_us - left.observation.observed_at_us;
            let alpha = (at_us - left.observation.observed_at_us) as f64 / span as f64;
            let transform = RigidTransform {
                translation_m: [
                    lerp(
                        left.data.translation_m[0],
                        right.data.translation_m[0],
                        alpha,
                    ),
                    lerp(
                        left.data.translation_m[1],
                        right.data.translation_m[1],
                        alpha,
                    ),
                    lerp(
                        left.data.translation_m[2],
                        right.data.translation_m[2],
                        alpha,
                    ),
                ],
                rotation_xyzw: quat_slerp(left.data.rotation_xyzw, right.data.rotation_xyzw, alpha),
            };
            Some(ResolvedEdge {
                key: key.clone(),
                transform_parent_from_child: transform,
                authority,
                provider_id: right.observation.provider_id.clone(),
                provider_instance_id: right.observation.provider_instance_id.clone(),
                observed_at_us: at_us,
                interpolated: true,
                extrapolated_by_us: 0,
                session_epoch: right.data.session_epoch.clone(),
                calibration_revision: right.observation.calibration_revision.clone(),
            })
        }
        (Some(sample), _) => {
            let delta = at_us.saturating_sub(sample.observation.observed_at_us);
            if delta > max_extrapolation_us {
                return None;
            }
            Some(resolved_from_sample(key, authority, sample, false, delta))
        }
        (None, Some(sample)) => {
            let delta = sample.observation.observed_at_us.saturating_sub(at_us);
            if delta > max_extrapolation_us {
                return None;
            }
            Some(resolved_from_sample(key, authority, sample, false, delta))
        }
        (None, None) => None,
    }
}

fn resolved_from_sample(
    key: &TransformEdgeKey,
    authority: String,
    sample: &StoredTransform,
    interpolated: bool,
    extrapolated_by_us: u64,
) -> ResolvedEdge {
    ResolvedEdge {
        key: key.clone(),
        transform_parent_from_child: RigidTransform {
            translation_m: sample.data.translation_m,
            rotation_xyzw: sample.data.rotation_xyzw,
        },
        authority,
        provider_id: sample.observation.provider_id.clone(),
        provider_instance_id: sample.observation.provider_instance_id.clone(),
        observed_at_us: sample.observation.observed_at_us,
        interpolated,
        extrapolated_by_us,
        session_epoch: sample.data.session_epoch.clone(),
        calibration_revision: sample.observation.calibration_revision.clone(),
    }
}

fn transform_authority(observation: &Observation, data: &TransformData) -> String {
    data.authority.clone().unwrap_or_else(|| {
        format!(
            "{}:{}",
            observation.provider_id, observation.provider_instance_id
        )
    })
}

fn nearest_fresh_observation<'a>(
    history: &'a VecDeque<Observation>,
    target_us: u64,
    now: &DateTime<Utc>,
) -> Option<&'a Observation> {
    history
        .iter()
        .filter(|observation| !observation_is_stale(observation, now))
        .min_by_key(|observation| observation.observed_at_us.abs_diff(target_us))
}

fn observation_is_stale(observation: &Observation, now: &DateTime<Utc>) -> bool {
    if observation.valid == Some(false) {
        return true;
    }
    if let Some(expires_at_us) = observation.expires_at_us {
        if current_time_us() > expires_at_us {
            return true;
        }
    }
    match (observation.freshness_ms, observation.received_at.as_ref()) {
        (Some(freshness_ms), Some(received_at)) => {
            now.signed_duration_since(*received_at)
                .num_milliseconds()
                .max(0) as u64
                > freshness_ms
        }
        _ => false,
    }
}

fn transform_observation_is_graph_usable(observation: &Observation) -> bool {
    if observation.valid == Some(false) {
        return false;
    }
    if observation
        .expires_at_us
        .is_some_and(|expires_at_us| current_time_us() > expires_at_us)
    {
        return false;
    }
    if observation
        .data
        .get("motion_usable")
        .and_then(Value::as_bool)
        == Some(false)
    {
        return false;
    }
    observation.data.get("review_state").and_then(Value::as_str)
        != Some("CANDIDATE_REVIEW_REQUIRED")
}

fn current_time_us() -> u64 {
    Utc::now().timestamp_micros().max(0) as u64
}

fn lerp(left: f64, right: f64, alpha: f64) -> f64 {
    left + (right - left) * alpha
}

fn quat_norm(q: [f64; 4]) -> f64 {
    (q[0] * q[0] + q[1] * q[1] + q[2] * q[2] + q[3] * q[3]).sqrt()
}

fn normalize_quat(q: [f64; 4]) -> [f64; 4] {
    let norm = quat_norm(q);
    if norm < 1e-12 {
        return [0.0, 0.0, 0.0, 1.0];
    }
    [q[0] / norm, q[1] / norm, q[2] / norm, q[3] / norm]
}

fn quat_conjugate(q: [f64; 4]) -> [f64; 4] {
    [-q[0], -q[1], -q[2], q[3]]
}

fn quat_multiply(left: [f64; 4], right: [f64; 4]) -> [f64; 4] {
    let [lx, ly, lz, lw] = left;
    let [rx, ry, rz, rw] = right;
    [
        lw * rx + lx * rw + ly * rz - lz * ry,
        lw * ry - lx * rz + ly * rw + lz * rx,
        lw * rz + lx * ry - ly * rx + lz * rw,
        lw * rw - lx * rx - ly * ry - lz * rz,
    ]
}

fn quat_rotate(q: [f64; 4], vector: [f64; 3]) -> [f64; 3] {
    let q = normalize_quat(q);
    let pure = [vector[0], vector[1], vector[2], 0.0];
    let rotated = quat_multiply(quat_multiply(q, pure), quat_conjugate(q));
    [rotated[0], rotated[1], rotated[2]]
}

fn quat_slerp(left: [f64; 4], right: [f64; 4], alpha: f64) -> [f64; 4] {
    let left = normalize_quat(left);
    let mut right = normalize_quat(right);
    let mut dot = left[0] * right[0] + left[1] * right[1] + left[2] * right[2] + left[3] * right[3];
    if dot < 0.0 {
        right = [-right[0], -right[1], -right[2], -right[3]];
        dot = -dot;
    }
    if dot > 0.9995 {
        return normalize_quat([
            lerp(left[0], right[0], alpha),
            lerp(left[1], right[1], alpha),
            lerp(left[2], right[2], alpha),
            lerp(left[3], right[3], alpha),
        ]);
    }
    let theta_0 = dot.clamp(-1.0, 1.0).acos();
    let theta = theta_0 * alpha;
    let sin_theta = theta.sin();
    let sin_theta_0 = theta_0.sin();
    let scale_left = theta.cos() - dot * sin_theta / sin_theta_0;
    let scale_right = sin_theta / sin_theta_0;
    normalize_quat([
        scale_left * left[0] + scale_right * right[0],
        scale_left * left[1] + scale_right * right[1],
        scale_left * left[2] + scale_right * right[2],
        scale_left * left[3] + scale_right * right[3],
    ])
}

fn api_error(status: StatusCode, message: impl Into<String>) -> (StatusCode, Json<Value>) {
    (status, Json(json!({"error": message.into()})))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn transform_observation(data_overrides: Value) -> Observation {
        let mut data = json!({
            "parent_frame": "world",
            "child_frame": "arm",
            "translation_m": [0.0, 0.0, 0.0],
            "rotation_xyzw": [0.0, 0.0, 0.0, 1.0],
            "is_static": true
        });
        for (key, value) in data_overrides.as_object().unwrap() {
            data[key] = value.clone();
        }
        Observation {
            observation_id: "observation".to_string(),
            schema: TRANSFORM_SCHEMA.to_string(),
            schema_version: 1,
            stream: "transform.test".to_string(),
            provider_id: "test.provider".to_string(),
            provider_instance_id: "test-instance".to_string(),
            boot_id: "test-boot".to_string(),
            sequence: 1,
            observed_at_us: current_time_us(),
            received_at: Some(Utc::now()),
            freshness_ms: None,
            frame_id: None,
            coordinate_frame: None,
            calibration_revision: None,
            clock_domain: None,
            expires_at_us: None,
            related_skill_id: None,
            confidence: None,
            valid: Some(true),
            data,
        }
    }

    fn semantic_scene_observation(data: Value) -> Observation {
        Observation {
            observation_id: "scene-observation".to_string(),
            schema: SEMANTIC_SPHERE_SCENE_SCHEMA.to_string(),
            schema_version: 1,
            stream: "robot_arm.primary.integrated.scene".to_string(),
            provider_id: "test.scene".to_string(),
            provider_instance_id: "test-scene-instance".to_string(),
            boot_id: "test-scene-boot".to_string(),
            sequence: 1,
            observed_at_us: current_time_us(),
            received_at: Some(Utc::now()),
            freshness_ms: Some(1000),
            frame_id: Some("rebot_arm_base".to_string()),
            coordinate_frame: Some("rebot_arm_base".to_string()),
            calibration_revision: None,
            clock_domain: None,
            expires_at_us: None,
            related_skill_id: None,
            confidence: None,
            valid: Some(true),
            data,
        }
    }

    fn semantic_assertions_observation(assertions: Value) -> Observation {
        Observation {
            observation_id: "assertions-observation".to_string(),
            schema: ARM_SEMANTIC_ASSERTIONS_SCHEMA.to_string(),
            schema_version: 1,
            stream: "robot_arm.scene.tracked_semantic_assertions".to_string(),
            provider_id: "perception.sam2_scene_tracker".to_string(),
            provider_instance_id: "tracker-instance".to_string(),
            boot_id: "tracker-boot".to_string(),
            sequence: 1,
            observed_at_us: current_time_us(),
            received_at: Some(Utc::now()),
            freshness_ms: Some(3000),
            frame_id: Some("rebot_arm_base".to_string()),
            coordinate_frame: Some("rebot_arm_base".to_string()),
            calibration_revision: None,
            clock_domain: None,
            expires_at_us: None,
            related_skill_id: None,
            confidence: None,
            valid: Some(true),
            data: json!({
                "contract_version": 1,
                "frame_id": "rebot_arm_base",
                "assertions": assertions
            }),
        }
    }

    fn indexed_transform_history(
        observations: impl IntoIterator<Item = Observation>,
        maximum_samples: usize,
    ) -> TransformEdgeHistory {
        let mut history = TransformEdgeHistory::default();
        for (insertion_id, observation) in observations.into_iter().enumerate() {
            let mut data = parse_transform(&observation).expect("valid transform test fixture");
            data.rotation_xyzw = normalize_quat(data.rotation_xyzw);
            let authority = transform_authority(&observation, &data);
            history.insert(
                Arc::new(StoredTransform {
                    observation,
                    data,
                    authority,
                    insertion_id: insertion_id as u64,
                }),
                maximum_samples,
            );
        }
        history
    }

    fn test_state() -> AppState {
        AppState {
            store: Arc::new(RwLock::new(FabricStore::default())),
            transform_updates: Arc::new(Notify::new()),
            history_per_stream: DEFAULT_HISTORY_PER_STREAM,
            transform_history_per_edge: DEFAULT_TRANSFORM_HISTORY_PER_EDGE,
        }
    }

    fn approx(left: f64, right: f64) {
        assert!((left - right).abs() < 1e-9, "{left} != {right}");
    }

    #[test]
    fn inverse_round_trip_is_identity() {
        let transform = RigidTransform {
            translation_m: [1.0, -2.0, 3.0],
            rotation_xyzw: [0.0, 0.0, (0.5_f64).sqrt(), (0.5_f64).sqrt()],
        };
        let identity = transform.compose(transform.inverse());
        for value in identity.translation_m {
            approx(value, 0.0);
        }
        approx(identity.rotation_xyzw[0], 0.0);
        approx(identity.rotation_xyzw[1], 0.0);
        approx(identity.rotation_xyzw[2], 0.0);
        approx(identity.rotation_xyzw[3].abs(), 1.0);
    }

    #[test]
    fn composition_uses_parent_from_child_semantics() {
        let world_from_body = RigidTransform {
            translation_m: [1.0, 0.0, 0.0],
            rotation_xyzw: [0.0, 0.0, 0.0, 1.0],
        };
        let body_from_camera = RigidTransform {
            translation_m: [0.0, 2.0, 0.0],
            rotation_xyzw: [0.0, 0.0, 0.0, 1.0],
        };
        let world_from_camera = world_from_body.compose(body_from_camera);
        assert_eq!(world_from_camera.translation_m, [1.0, 2.0, 0.0]);
    }

    #[test]
    fn slerp_midpoint_is_normalized() {
        let start = [0.0, 0.0, 0.0, 1.0];
        let end = [0.0, 0.0, 1.0, 0.0];
        let midpoint = quat_slerp(start, end, 0.5);
        approx(quat_norm(midpoint), 1.0);
        approx(midpoint[2].abs(), (0.5_f64).sqrt());
        approx(midpoint[3].abs(), (0.5_f64).sqrt());
    }

    #[test]
    fn transform_graph_excludes_explicit_non_motion_candidate() {
        let observation = transform_observation(json!({
            "review_state": "CANDIDATE_REVIEW_REQUIRED",
            "motion_usable": false
        }));
        assert!(!transform_observation_is_graph_usable(&observation));
        assert!(validate_transform_observation(&observation).is_ok());
    }

    #[test]
    fn transform_graph_excludes_expired_static_transform() {
        let mut observation = transform_observation(json!({}));
        observation.expires_at_us = Some(1);
        assert!(!transform_observation_is_graph_usable(&observation));
    }

    #[test]
    fn transform_graph_keeps_legacy_and_explicitly_usable_transforms() {
        let legacy = transform_observation(json!({}));
        let accepted = transform_observation(json!({
            "review_state": "ACCEPTED",
            "motion_usable": true
        }));
        assert!(transform_observation_is_graph_usable(&legacy));
        assert!(transform_observation_is_graph_usable(&accepted));
    }

    #[test]
    fn latest_static_revocation_suppresses_older_active_transform() {
        let now = current_time_us();
        let mut active = transform_observation(json!({
            "authority": "manager.workcell_calibration_activation",
            "review_state": "ACCEPTED",
            "activation_state": "ACTIVE",
            "motion_usable": true
        }));
        active.observed_at_us = now - 1;
        let mut revoked = transform_observation(json!({
            "authority": "manager.workcell_calibration_activation",
            "review_state": "REVOKED",
            "activation_state": "REVOKED",
            "motion_usable": false
        }));
        revoked.observed_at_us = now;
        let history = indexed_transform_history([active, revoked], 4096);
        let key = TransformEdgeKey {
            parent_frame: "world".to_string(),
            child_frame: "arm".to_string(),
        };

        let resolved = resolve_edge(&key, &history, now, 100_000, None)
            .expect("revocation should suppress without creating a conflict");

        assert!(resolved.is_none());
    }

    #[test]
    fn indexed_dynamic_history_interpolates_without_reordering_raw_retention() {
        let mut right = transform_observation(json!({
            "is_static": false,
            "session_epoch": "session-1",
            "translation_m": [2.0, 0.0, 0.0]
        }));
        right.sequence = 2;
        right.observed_at_us = 1_200;
        let mut left = transform_observation(json!({
            "is_static": false,
            "session_epoch": "session-1",
            "translation_m": [0.0, 0.0, 0.0]
        }));
        left.observed_at_us = 1_000;

        let history = indexed_transform_history([right, left], 4096);
        let key = TransformEdgeKey {
            parent_frame: "world".to_string(),
            child_frame: "arm".to_string(),
        };
        let resolved = resolve_edge(&key, &history, 1_100, 100_000, Some("session-1"))
            .expect("single authority")
            .expect("interpolated transform");

        assert!(resolved.interpolated);
        assert_eq!(resolved.extrapolated_by_us, 0);
        approx(resolved.transform_parent_from_child.translation_m[0], 1.0);
        assert_eq!(history.insertion_order[0].observation.observed_at_us, 1_200);
        assert_eq!(history.insertion_order[1].observation.observed_at_us, 1_000);
    }

    #[test]
    fn indexed_history_evicts_by_original_global_insertion_order() {
        let mut first = transform_observation(json!({"authority": "authority-a"}));
        first.observed_at_us = 3_000;
        let mut second = transform_observation(json!({"authority": "authority-b"}));
        second.observed_at_us = 1_000;
        let mut third = transform_observation(json!({"authority": "authority-a"}));
        third.observed_at_us = 2_000;

        let history = indexed_transform_history([first, second, third], 2);

        assert_eq!(history.len(), 2);
        assert_eq!(history.insertion_order[0].authority, "authority-b");
        assert_eq!(history.insertion_order[1].authority, "authority-a");
        assert_eq!(history.authorities["authority-a"].static_samples.len(), 1);
    }

    #[test]
    fn indexed_history_preserves_authority_conflict_reporting() {
        let first = transform_observation(json!({"authority": "authority-a"}));
        let mut second = transform_observation(json!({"authority": "authority-b"}));
        second.sequence = 2;
        let history = indexed_transform_history([first, second], 4096);
        let key = TransformEdgeKey {
            parent_frame: "world".to_string(),
            child_frame: "arm".to_string(),
        };

        let mut authorities = resolve_edge(&key, &history, current_time_us(), 100_000, None)
            .expect_err("multiple authorities must conflict");
        authorities.sort();

        assert_eq!(authorities, ["authority-a", "authority-b"]);
    }

    #[tokio::test]
    async fn raw_and_transform_histories_keep_independent_configured_bounds() {
        let mut state = test_state();
        state.history_per_stream = 2;
        state.transform_history_per_edge = 3;
        for sequence in 1..=4 {
            let mut observation = transform_observation(json!({
                "is_static": false,
                "session_epoch": "session-1"
            }));
            observation.sequence = sequence;
            observation.observed_at_us = 1_000 + sequence;
            let mut store = state.store.write().await;
            insert_observation_locked(&state, &mut store, observation)
                .expect("insert transform observation");
        }

        let store = state.store.read().await;
        let key = TransformEdgeKey {
            parent_frame: "world".to_string(),
            child_frame: "arm".to_string(),
        };
        assert_eq!(store.latest["transform.test"].sequence, 4);
        assert_eq!(store.history["transform.test"].len(), 2);
        assert_eq!(store.transforms[&key].len(), 3);
        assert_eq!(store.history["transform.test"][0].sequence, 3);
        assert_eq!(
            store.transforms[&key].insertion_order[0]
                .observation
                .sequence,
            2
        );
    }

    #[tokio::test]
    async fn transform_query_waits_for_a_bracketing_sample_on_the_same_api() {
        let state = test_state();
        let mut left = transform_observation(json!({
            "is_static": false,
            "session_epoch": "session-1",
            "translation_m": [0.0, 0.0, 0.0]
        }));
        left.observed_at_us = 1_000;
        {
            let mut store = state.store.write().await;
            insert_observation_locked(&state, &mut store, left).expect("insert left sample");
        }

        let publisher_state = state.clone();
        let publisher = tokio::spawn(async move {
            tokio::time::sleep(Duration::from_millis(20)).await;
            let mut right = transform_observation(json!({
                "is_static": false,
                "session_epoch": "session-1",
                "translation_m": [2.0, 0.0, 0.0]
            }));
            right.sequence = 2;
            right.observed_at_us = 1_200;
            let mut store = publisher_state.store.write().await;
            let (_, transform_accepted) =
                insert_observation_locked(&publisher_state, &mut store, right)
                    .expect("insert right sample");
            drop(store);
            if transform_accepted {
                publisher_state.transform_updates.notify_waiters();
            }
        });

        let result = query_transform(
            State(state),
            Query(TransformQuery {
                from_frame: "arm".to_string(),
                to_frame: "world".to_string(),
                at_us: Some(1_100),
                max_extrapolation_us: Some(1_000),
                session_epoch: Some("session-1".to_string()),
                wait_for_bracket_ms: Some(200),
            }),
        )
        .await
        .expect("bracketed transform");
        publisher.await.expect("publisher task");

        assert_eq!(result.path.len(), 1);
        assert!(result.path[0].interpolated);
        assert_eq!(result.path[0].extrapolated_by_us, 0);
        approx(result.translation_m[0], 1.0);
    }

    #[test]
    fn review_required_transform_must_explicitly_deny_motion() {
        let observation = transform_observation(json!({
            "review_state": "CANDIDATE_REVIEW_REQUIRED"
        }));
        let error = validate_transform_observation(&observation).unwrap_err();
        assert_eq!(error.0, StatusCode::BAD_REQUEST);
    }

    #[test]
    fn canonical_semantic_scene_is_accepted() {
        let observation = semantic_scene_observation(json!({
            "contract_version": 2,
            "scene_revision": "scene-1",
            "frame_id": "rebot_arm_base",
            "roi_layers": [
                {
                    "scope": "GRIPPER_0P5M",
                    "center_m": [0.4, 0.0, 0.3],
                    "radius_m": 0.5,
                    "minimum_sphere_radius_m": 0.02
                },
                {
                    "scope": "ARM_BASE_1P2M",
                    "center_m": [0.0, 0.0, 0.0],
                    "radius_m": 1.2,
                    "minimum_sphere_radius_m": 0.06
                }
            ],
            "spheres": [
                {
                    "sphere_id": "toilet-paper-1",
                    "object_id": "toilet-paper",
                    "center_m": [0.45, 0.0, 0.3],
                    "radius_m": 0.05,
                    "type": "WORK_OBJECT",
                    "roi_scope": "GRIPPER_0P5M"
                }
            ]
        }));

        assert!(validate_semantic_sphere_scene_observation(&observation).is_ok());
    }

    #[test]
    fn tracked_semantic_spheres_allow_repeated_object_with_unique_geometry_ids() {
        let observation = semantic_assertions_observation(json!([
            {
                "assertion_id": "table:cell:1",
                "sphere_id": "table:cell:1",
                "object_id": "table",
                "description": "the user-declared table obstacle",
                "center_m": [0.4, -0.1, 0.05],
                "radius_m": 0.02,
                "type": "KEEP_OUT"
            },
            {
                "assertion_id": "table:cell:2",
                "sphere_id": "table:cell:2",
                "object_id": "table",
                "description": "the user-declared table obstacle",
                "center_m": [0.4, 0.1, 0.05],
                "radius_m": 0.02,
                "type": "KEEP_OUT"
            }
        ]));

        assert!(validate_arm_semantic_assertions_observation(&observation).is_ok());
    }

    #[test]
    fn tracked_semantic_spheres_reject_duplicate_geometry_ids() {
        let observation = semantic_assertions_observation(json!([
            {
                "assertion_id": "table:cell:1",
                "object_id": "table",
                "description": "the table",
                "center_m": [0.4, -0.1, 0.05],
                "radius_m": 0.02,
                "type": "KEEP_OUT"
            },
            {
                "assertion_id": "table:cell:1",
                "object_id": "table",
                "description": "the table",
                "center_m": [0.4, 0.1, 0.05],
                "radius_m": 0.02,
                "type": "KEEP_OUT"
            }
        ]));

        assert!(validate_arm_semantic_assertions_observation(&observation).is_err());
    }

    #[test]
    fn keep_out_semantic_sphere_requires_description() {
        let observation = semantic_assertions_observation(json!([
            {
                "assertion_id": "table:cell:1",
                "object_id": "table",
                "center_m": [0.4, 0.0, 0.05],
                "radius_m": 0.02,
                "type": "KEEP_OUT"
            }
        ]));

        assert!(validate_arm_semantic_assertions_observation(&observation).is_err());
    }

    #[test]
    fn semantic_scene_rejects_small_base_sphere() {
        let observation = semantic_scene_observation(json!({
            "contract_version": 2,
            "scene_revision": "scene-2",
            "frame_id": "rebot_arm_base",
            "roi_layers": [
                {
                    "scope": "ARM_BASE_1P2M",
                    "center_m": [0.0, 0.0, 0.0],
                    "radius_m": 1.2,
                    "minimum_sphere_radius_m": 0.06
                }
            ],
            "spheres": [
                {
                    "sphere_id": "too-small",
                    "object_id": "too-small",
                    "center_m": [0.3, 0.0, 0.1],
                    "radius_m": 0.01,
                    "type": "KEEP_OUT",
                    "roi_scope": "ARM_BASE_1P2M"
                }
            ]
        }));

        let error = validate_semantic_sphere_scene_observation(&observation).unwrap_err();
        assert_eq!(error.0, StatusCode::BAD_REQUEST);
    }

    #[test]
    fn canonical_arm_point_cloud_input_is_accepted() {
        let mut observation = semantic_scene_observation(json!({
            "contract_version": 1,
            "units": "m",
            "points_m": [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]
        }));
        observation.schema = ARM_POINT_CLOUD_SCHEMA.to_string();
        observation.stream = "robot_arm.scene.point_cloud".to_string();
        observation.coordinate_frame = Some("external_depth_optical_frame".to_string());

        assert!(validate_arm_point_cloud_observation(&observation).is_ok());
    }

    #[test]
    fn arm_point_cloud_rejects_unbounded_freshness() {
        let mut observation = semantic_scene_observation(json!({
            "contract_version": 1,
            "units": "m",
            "points_m": []
        }));
        observation.schema = ARM_POINT_CLOUD_SCHEMA.to_string();
        observation.coordinate_frame = Some("external_depth_optical_frame".to_string());
        observation.freshness_ms = Some(10_000);

        let error = validate_arm_point_cloud_observation(&observation).unwrap_err();
        assert_eq!(error.0, StatusCode::BAD_REQUEST);
    }

    #[test]
    fn canonical_arm_semantic_assertions_are_accepted() {
        let mut observation = semantic_scene_observation(json!({
            "contract_version": 1,
            "frame_id": "rebot_arm_base",
            "assertions": [
                {
                    "object_id": "toilet-paper",
                    "center_m": [0.3, 0.0, 0.2],
                    "radius_m": 0.06,
                    "type": "WORKPIECE"
                },
                {
                    "object_id": "unclassified-box",
                    "center_m": [0.6, 0.1, 0.2],
                    "radius_m": 0.08
                }
            ]
        }));
        observation.schema = ARM_SEMANTIC_ASSERTIONS_SCHEMA.to_string();
        observation.stream = "robot_arm.scene.semantic_assertions".to_string();
        observation.freshness_ms = Some(5_000);

        assert!(validate_arm_semantic_assertions_observation(&observation).is_ok());
    }

    #[test]
    fn semantic_assertions_reject_ambiguous_pushable_type() {
        let mut observation = semantic_scene_observation(json!({
            "contract_version": 1,
            "frame_id": "rebot_arm_base",
            "assertions": [
                {
                    "object_id": "maybe-pushable",
                    "center_m": [0.3, 0.0, 0.2],
                    "radius_m": 0.06,
                    "type": "MAYBE_PUSHABLE"
                }
            ]
        }));
        observation.schema = ARM_SEMANTIC_ASSERTIONS_SCHEMA.to_string();
        observation.freshness_ms = Some(5_000);

        let error = validate_arm_semantic_assertions_observation(&observation).unwrap_err();
        assert_eq!(error.0, StatusCode::BAD_REQUEST);
    }
}
