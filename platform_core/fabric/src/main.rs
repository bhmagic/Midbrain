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
use tokio::sync::RwLock;
use tower_http::{cors::CorsLayer, trace::TraceLayer};
use tracing::info;
use uuid::Uuid;

const DEFAULT_HISTORY_PER_STREAM: usize = 256;
const DEFAULT_TRANSFORM_HISTORY_PER_EDGE: usize = 4096;
const DEFAULT_SYNC_MAX_DELTA_US: u64 = 50_000;
const DEFAULT_TRANSFORM_MAX_EXTRAPOLATION_US: u64 = 100_000;
const TRANSFORM_SCHEMA: &str = "physical_agent.transform";

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

#[derive(Debug, Deserialize)]
struct TransformQuery {
    from_frame: String,
    to_frame: String,
    at_us: Option<u64>,
    max_extrapolation_us: Option<u64>,
    session_epoch: Option<String>,
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

#[derive(Debug, Serialize)]
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

#[derive(Default)]
struct FabricStore {
    latest: HashMap<String, Observation>,
    history: HashMap<String, VecDeque<Observation>>,
    transforms: HashMap<TransformEdgeKey, VecDeque<Observation>>,
    accepted: u64,
}

#[derive(Clone)]
struct AppState {
    store: Arc<RwLock<FabricStore>>,
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
            "timestamped_transform_graph"
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
    ])
}

async fn publish_observation(
    State(state): State<AppState>,
    Json(observation): Json<Observation>,
) -> Result<(StatusCode, Json<Value>), (StatusCode, Json<Value>)> {
    let accepted = insert_observation(&state, observation).await?;
    Ok((StatusCode::ACCEPTED, Json(json!({"accepted": accepted}))))
}

async fn publish_batch(
    State(state): State<AppState>,
    Json(batch): Json<ObservationBatch>,
) -> Result<(StatusCode, Json<Value>), (StatusCode, Json<Value>)> {
    let mut accepted = 0usize;
    for observation in batch.observations {
        if insert_observation(&state, observation).await? {
            accepted += 1;
        }
    }
    Ok((StatusCode::ACCEPTED, Json(json!({"accepted": accepted}))))
}

async fn insert_observation(
    state: &AppState,
    mut observation: Observation,
) -> Result<bool, (StatusCode, Json<Value>)> {
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
    observation.received_at = Some(Utc::now());

    let mut store = state.store.write().await;
    if let Some(current) = store.latest.get(&observation.stream) {
        if current.provider_instance_id == observation.provider_instance_id
            && current.boot_id == observation.boot_id
            && observation.sequence <= current.sequence
        {
            return Ok(false);
        }
    }

    let stream = observation.stream.clone();
    store.latest.insert(stream.clone(), observation.clone());
    let history = store.history.entry(stream).or_default();
    history.push_back(observation.clone());
    while history.len() > state.history_per_stream {
        history.pop_front();
    }

    if observation.schema == TRANSFORM_SCHEMA {
        let transform = parse_transform(&observation)?;
        let key = TransformEdgeKey {
            parent_frame: transform.parent_frame,
            child_frame: transform.child_frame,
        };
        let edge_history = store.transforms.entry(key).or_default();
        edge_history.push_back(observation);
        while edge_history.len() > state.transform_history_per_edge {
            edge_history.pop_front();
        }
    }

    store.accepted += 1;
    Ok(true)
}

fn validate_transform_observation(
    observation: &Observation,
) -> Result<(), (StatusCode, Json<Value>)> {
    let transform = parse_transform(observation)?;
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
                now.signed_duration_since(received.clone())
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
                received_at: observation.received_at.clone(),
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
        let mut latest_by_authority: HashMap<String, &Observation> = HashMap::new();
        for observation in history {
            if observation.valid == Some(false) {
                continue;
            }
            let Ok(data) = serde_json::from_value::<TransformData>(observation.data.clone()) else {
                continue;
            };
            let authority = transform_authority(observation, &data);
            let replace = latest_by_authority.get(&authority).map_or(true, |current| {
                observation.observed_at_us > current.observed_at_us
            });
            if replace {
                latest_by_authority.insert(authority, observation);
            }
        }
        for (authority, observation) in latest_by_authority {
            let Ok(data) = serde_json::from_value::<TransformData>(observation.data.clone()) else {
                continue;
            };
            result.push(TransformEdgeSummary {
                parent_frame: key.parent_frame.clone(),
                child_frame: key.child_frame.clone(),
                authority,
                provider_id: observation.provider_id.clone(),
                provider_instance_id: observation.provider_instance_id.clone(),
                latest_observed_at_us: observation.observed_at_us,
                is_static: data.is_static,
                session_epoch: data.session_epoch,
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
    let store = state.store.read().await;
    let mut adjacency: HashMap<String, Vec<GraphArc>> = HashMap::new();
    let mut conflicts = Vec::new();

    for (key, history) in &store.transforms {
        match resolve_edge(
            key,
            history,
            at_us,
            max_extrapolation_us,
            query.session_epoch.as_deref(),
        ) {
            Ok(Some(edge)) => add_resolved_edge_to_graph(&mut adjacency, edge),
            Ok(None) => {}
            Err(authorities) => conflicts.push(json!({
                "parent_frame": key.parent_frame,
                "child_frame": key.child_frame,
                "authorities": authorities,
            })),
        }
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
    history: &VecDeque<Observation>,
    at_us: u64,
    max_extrapolation_us: u64,
    requested_session_epoch: Option<&str>,
) -> Result<Option<ResolvedEdge>, Vec<String>> {
    let mut by_authority: HashMap<String, Vec<(&Observation, TransformData)>> = HashMap::new();
    for observation in history {
        if observation.valid == Some(false) {
            continue;
        }
        let Ok(mut data) = serde_json::from_value::<TransformData>(observation.data.clone()) else {
            continue;
        };
        if data.parent_frame != key.parent_frame || data.child_frame != key.child_frame {
            continue;
        }
        if let Some(requested) = requested_session_epoch {
            if data.session_epoch.as_deref() != Some(requested) && !data.is_static {
                continue;
            }
        }
        data.rotation_xyzw = normalize_quat(data.rotation_xyzw);
        let authority = transform_authority(observation, &data);
        by_authority
            .entry(authority)
            .or_default()
            .push((observation, data));
    }

    let mut candidates = Vec::new();
    for (authority, mut samples) in by_authority {
        samples.sort_by_key(|(observation, _)| observation.observed_at_us);
        if let Some(candidate) =
            resolve_authority_samples(key, authority, &samples, at_us, max_extrapolation_us)
        {
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
    samples: &[(&Observation, TransformData)],
    at_us: u64,
    max_extrapolation_us: u64,
) -> Option<ResolvedEdge> {
    let latest_static = samples
        .iter()
        .rev()
        .find(|(_, data)| data.is_static)
        .cloned();
    if let Some((observation, data)) = latest_static {
        return Some(resolved_from_sample(
            key,
            authority,
            observation,
            &data,
            false,
            0,
        ));
    }

    let dynamic: Vec<(&Observation, &TransformData)> = samples
        .iter()
        .filter(|(_, data)| !data.is_static)
        .map(|(observation, data)| (*observation, data))
        .collect();
    if dynamic.is_empty() {
        return None;
    }

    let before = dynamic
        .iter()
        .rev()
        .find(|(observation, _)| observation.observed_at_us <= at_us)
        .copied();
    let after = dynamic
        .iter()
        .find(|(observation, _)| observation.observed_at_us >= at_us)
        .copied();

    match (before, after) {
        (Some((left_obs, left)), Some((right_obs, right)))
            if left_obs.observed_at_us != right_obs.observed_at_us
                && left.session_epoch == right.session_epoch =>
        {
            let span = right_obs.observed_at_us - left_obs.observed_at_us;
            let alpha = (at_us - left_obs.observed_at_us) as f64 / span as f64;
            let transform = RigidTransform {
                translation_m: [
                    lerp(left.translation_m[0], right.translation_m[0], alpha),
                    lerp(left.translation_m[1], right.translation_m[1], alpha),
                    lerp(left.translation_m[2], right.translation_m[2], alpha),
                ],
                rotation_xyzw: quat_slerp(left.rotation_xyzw, right.rotation_xyzw, alpha),
            };
            Some(ResolvedEdge {
                key: key.clone(),
                transform_parent_from_child: transform,
                authority,
                provider_id: right_obs.provider_id.clone(),
                provider_instance_id: right_obs.provider_instance_id.clone(),
                observed_at_us: at_us,
                interpolated: true,
                extrapolated_by_us: 0,
                session_epoch: right.session_epoch.clone(),
                calibration_revision: right_obs.calibration_revision.clone(),
            })
        }
        (Some((observation, data)), _) => {
            let delta = at_us.saturating_sub(observation.observed_at_us);
            if delta > max_extrapolation_us {
                return None;
            }
            Some(resolved_from_sample(
                key,
                authority,
                observation,
                data,
                false,
                delta,
            ))
        }
        (None, Some((observation, data))) => {
            let delta = observation.observed_at_us.saturating_sub(at_us);
            if delta > max_extrapolation_us {
                return None;
            }
            Some(resolved_from_sample(
                key,
                authority,
                observation,
                data,
                false,
                delta,
            ))
        }
        (None, None) => None,
    }
}

fn resolved_from_sample(
    key: &TransformEdgeKey,
    authority: String,
    observation: &Observation,
    data: &TransformData,
    interpolated: bool,
    extrapolated_by_us: u64,
) -> ResolvedEdge {
    ResolvedEdge {
        key: key.clone(),
        transform_parent_from_child: RigidTransform {
            translation_m: data.translation_m,
            rotation_xyzw: normalize_quat(data.rotation_xyzw),
        },
        authority,
        provider_id: observation.provider_id.clone(),
        provider_instance_id: observation.provider_instance_id.clone(),
        observed_at_us: observation.observed_at_us,
        interpolated,
        extrapolated_by_us,
        session_epoch: data.session_epoch.clone(),
        calibration_revision: observation.calibration_revision.clone(),
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
            now.signed_duration_since(received_at.clone())
                .num_milliseconds()
                .max(0) as u64
                > freshness_ms
        }
        _ => false,
    }
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
}
