# Limited Graph movement rejection and missing alignment visuals

Date: 2026-08-17

## Observed runs

- Agent run `fc649aae-e182-4648-9606-61f7e818f5e0`, graph
  `5beaa4e662fd405597621ec4107db458`: world-axis establishment,
  FoundationPose candidate creation, review, and activation succeeded. The
  first post-calibration physical node completed its Integrated Provider
  handover and submitted one raise motion. Limited Graph then returned
  `UNKNOWN_OUTCOME`.
- Agent run `3da4c12d-6b37-4a4f-86d5-d8fa032dcf3a`, graph
  `42f784c933b64753be8a5abb7504ff73`: `derive_fabric_world_point`
  completed from a fresh Fabric scene. The following corner movement reached
  the same `UNKNOWN_OUTCOME` result-validation failure.
- Agent run `98a689d8-5ebd-4cfc-908e-25abb70d77ee`: the combined
  existing-scene corner-motion and slicing graph was rejected before execution
  because child Skill `derive_fabric_world_point` was not eligible on the
  selected route.
- FoundationPose alignment `20260817T055224Z-c51c4ff4` produced
  `camera.jpg`, `depth.png`, `overlay.jpg`, and
  `foundation_pose_attempt_1_selected_overlay.jpg`, but the Agent journal had
  no `visual.evidence.created` event for the calibration result.

## Exact retained validation error

The physical movement results were rejected with:

```text
ValidationError: '[REDACTED]' is not of type 'object'
schema path: properties.authorization.type
instance path: authorization
```

Direct movement results in the same batch contained a valid object-valued
`authorization`. Limited Graph normalized the child result, redacted every
value under the root key `authorization`, and only then validated it against
the child output schema. The redactor therefore created the schema mismatch
after the physical action completed.

## Route eligibility finding

The combined request matched work-object motion, slicing, and mixed-frame terms,
but did not request a new scene policy. The only combined route required scene
policy terms, so the narrower mixed-frame-slicing route won and excluded the
Fabric point and absolute-world motion Skills. Graph preflight correctly
enforced that incomplete active child catalog. No Fabric read, freshness, or
transform failure occurred in this rejected run.

## Visual finding

Agent-triggered calibration constructed a separate in-process
`AlignmentSkill`, whose artifact buffer was not the buffer owned by the
standalone aligner web process. The adapter returned artifact paths only inside
nested diagnostics and did not register a root standard `visual_evidence`
object. Consequently, the Agent event translator had nothing to publish and
the standalone page could not see the other process's in-memory images even
though the files existed.

## Implemented correction

- Validate normalized child results before credential redaction. Redact before
  any retention, routing, binding, tracing, or returned graph result.
- Serialize validation failures without the invalid instance or authorization
  value.
- Add a deterministic compound route for existing-scene corner motion followed
  by mixed-frame slicing.
- Register persisted FoundationPose pose overlay, VLM overlay, RGB, and depth as
  standard Agent visual evidence.
- Let the idle standalone aligner page serve fixed image names from the latest
  persisted alignment directory after validating the alignment identifier and
  resolved directory boundary.

## Validation checkpoints

- Limited Graph runner and deterministic discovery: 60 tests passed.
- Calibration adapter, Agent event translation, output-contract audit, and
  aligner GUI artifact fallback: 30 tests and 9 subtests passed.
- Complete Test Agent suite: 455 tests and 27 subtests passed.
- Configured repository package-root suite: 1,114 tests and 27 subtests passed.
- Python compilation, documentation integrity for 134 Markdown files, JSON
  parsing for 104 files, configuration baselines, Python environment isolation,
  and source-integrity manifest refresh passed.
- Limited Graph, stationary alignment, and Test Agent wheels built successfully.
- These checkpoints were stopped-software tests. They started no Provider,
  Manager runtime, robot process, or physical action.
