# Local HTTP API — Core 0.2.0

These are prototype local interfaces. They are intentionally simple and are not the final authenticated or version-negotiated transport.

## Resource Provider Manager — `http://127.0.0.1:7001`

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Manager health and feature flags |
| GET | `/v1/providers` | Configured providers, process state, and latest report |
| GET | `/v1/capabilities` | Capability-specific availability derived from provider heartbeats |
| POST | `/v1/providers/register` | Register one provider instance |
| POST | `/v1/providers/heartbeat` | Refresh lifecycle, health, readiness, and capabilities |
| POST | `/v1/providers/{id}/start` | Start the configured provider process |
| POST | `/v1/providers/{id}/hot` | Ensure process is running and request HOT residency |
| POST | `/v1/providers/{id}/warm` | Request WARM residency |
| POST | `/v1/providers/{id}/stop` | Request graceful stop; terminate on timeout only when that provider permits automatic force termination |
| POST | `/v1/providers/{id}/kill` | Force process-tree termination |

A provider heartbeat is expired after its configured `heartbeat_timeout_ms`. Expiry forces `ready=false`, marks the report `UNHEALTHY`, removes capability availability, and publishes an unavailable status observation to the Fabric.

Provider configuration accepts `graceful_stop_timeout_ms` and
`force_kill_on_stop_timeout`. The latter defaults to `true`. When it is
`false`, a graceful-stop timeout returns an error and leaves the process
running; an explicit `/kill` request is still authoritative.

## World State Fabric — `http://127.0.0.1:7002`

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Fabric health, stream count, and feature flags |
| POST | `/v1/observations` | Publish one observation |
| POST | `/v1/observations/batch` | Publish multiple observations |
| GET | `/v1/latest/{stream}` | Latest observation for a stream |
| GET | `/v1/recent/{stream}?limit=32` | Bounded recent history |
| GET | `/v1/snapshot` | Latest observation for every stream |
| GET | `/v1/streams` | Stream catalog with freshness and stale status |
| GET | `/v1/sync` | Timestamp-nearest multi-stream observation bundle |

Example synchronized query:

```text
/v1/sync?streams=camera.rgb.frame_ref,camera.depth.frame_ref,camera.imu.accel&anchor_stream=camera.rgb.frame_ref&max_delta_us=50000&require_all=false
```

The response includes the anchor timestamp, matched observations, per-stream deltas, missing streams, stale streams, and `complete`. Stale observations are excluded. When `require_all=true`, an incomplete bundle is returned with HTTP 409 so callers cannot silently treat partial state as complete.

## v0.3 additions

- `GET /v1/schemas`
- `GET /v1/transforms`
- `GET /v1/transform?from_frame=...&to_frame=...&at_us=...&session_epoch=...`
- `POST /v1/providers/:id/request`
- `GET /v1/motion/inhibit`
- `POST /v1/motion/inhibit/acquire`
- `POST /v1/motion/inhibit/release`
