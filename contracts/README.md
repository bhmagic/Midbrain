# Physical AI Contracts

Version: 0.4 working draft.

This contract set defines Resource Providers, World State Fabric transport,
safety/authority policy, native timestamped transforms, finite Skills, device
calibration, Local VIO, startup space cognition, motion-inhibit coordination,
advisory agent Skill discovery, and provider data-route advertisement.

Document 11 defines the non-enforcing OpenAI Agents SDK discovery boundary and
its separation from deterministic Manager provider binding.

Document 12 defines Fabric-visible direct data-route discovery without putting
large or latency-sensitive payloads through the Fabric.

Document 13 defines the Manager-hosted Midbrain portal,
implementation-neutral component observation pages, finite-Skill liveness
semantics, and the guarded transition to development surfaces. Its descriptor schema is
`schemas/component_ui.v1.schema.json`.

Documents 00-05 retain the v0.2 foundations and incorporate the v0.3.11
safety-critical process-escalation and layered-authority-lineage rules.
Documents 06-10 add the interfaces required for camera/IMU pose tracking and
world-frame spatial visualization.

Document 14 defines the canonical `+X` forward, `+Y` left, `+Z` up spatial
language, keeps raw camera optical axes explicit, and requires deterministic
semantic-direction resolution before motion planning.

Document 15 defines the implementation-neutral Agent event envelope, the
initial OpenAI Agents SDK adapter projection, replayable browser SSE transport,
and the separation between development approval interactions and autonomous
host authorization policy.

Document 16 defines exact visual-evidence references, normalized structured
annotations, channel applicability, browser-controlled overlays, and
client-side flattened export without making a burned raster overlay the
authoritative artifact.

Document 17 defines the SDK-neutral user-image upload/reference boundary and
keeps operator attachments separate from authoritative robot-camera evidence.

Document 18 defines the bounded browser-session chat projection, per-turn
expandable execution summaries, process-epoch continuity boundary, and the
exclusion of private reasoning and raw tool data.

Document 19 defines the bounded robot-local SQLite journal for SDK-neutral
Agent events, its read-only two-level developer viewer, interrupted-run
recovery semantics, and the boundary between durable development diagnostics
and a future authenticated field-audit store.

The finite-Skill contract also defines the depth-backed general effector-front
landmark boundary and separates it from task-specific action geometry.

The Fabric-hosted arm scene policy and tracked sphere payloads are regulated by
`schemas/arm_scene_segmentation_policy.v1.schema.json` and
`schemas/arm_semantic_assertions.v1.schema.json`. Only described upstream
objects may become `KEEP_OUT`; unclaimed visible geometry defaults to
non-blocking `PUSHABLE` data.
