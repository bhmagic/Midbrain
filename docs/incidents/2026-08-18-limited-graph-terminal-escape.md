# Limited Graph compact terminal escape

## Affected runs

- `2869b95b-a158-42fa-aa39-6e0c7056d8c0`, started
  `2026-08-18T05:39:48.650035Z`.
- `b00c5c6b-3d56-44ff-b2a6-2d31c5ebbcc2`, started
  `2026-08-18T05:41:19.462097Z`.

## Observed graph execution

The first run submitted graph
`toilet-paper-corner-approach-and-two-slices`. The graph completed scene
inspection, Fabric corner derivation, corner motion, and arm-base direction
translation. Slicing then raised a trusted pre-submission rejection with
`SHADOW_PLANNING_TIME_BUDGET_EXCEEDED`.

Graph run `45b83ac6b49e430ab8b5fd2c70eefbbf` recorded:

- status `FAILED`;
- terminal node `failed`;
- last completed node `translate-arm-negative-x`;
- one completed physical action;
- `first-slice -> failed`;
- no execution of `above-first-slice`, `move-above-first-slice`, or
  `second-slice`.

## Agent-visible information loss

The complete graph detail contained
`CHILD_PHYSICAL_ACTION_NOT_SUBMITTED`, node `first-slice`, tool
`slice_with_blade`, the exact rejection reason, and
`physical_action_submitted=False`. The two-tier compact projection did not
include `trace`. Its top-level failure information was limited to status
`FAILED`, terminal `failed`, last completed node `translate-arm-negative-x`,
and message `Limited Graph failed`.

The subsequent Agent cycle activated the Contact Provider, derived a new world
point, and invoked `slice_with_blade` directly. The direct call returned
`SHADOW_PLANNING_TIME_BUDGET_EXCEEDED`. When the complete prompt was repeated,
the same Agent session interpreted the prior partial motion as progress and
directly invoked `slice_with_blade` again. That call returned a singularity and
IK-residual rejection.

## Repair

Limited Graph results now include compact `last_failure` fields:

| Field | Value for the retained graph |
|---|---|
| `kind` | `CHILD_PHYSICAL_ACTION_NOT_SUBMITTED` |
| `node_id` | `first-slice` |
| `tool_name` | `slice_with_blade` |
| `reason` | Exact child-owned preview rejection |
| `physical_action_submitted` | `false` |

The full trace remains in the detail tier. Agent guidance now states that a
non-success graph result terminates the submitted workflow, failed and
remaining children cannot be continued directly, materially different
replanning uses a new complete graph, and repeated user input is fresh unless
resumption is explicit.

No Slicing, IK, collision, Fabric, Provider responsibility, authorization, or
physical-retry behavior changed in this repair.

## Validation

- Focused Limited Graph and Test Agent regression selection: 90 passed.
- Complete Test Agent suite: 481 passed and 27 subtests passed.
- Limited Graph and Slicing suites: 60 passed.
- Complete repository Python suite: 1,146 passed and 27 subtests passed.
- Rust workspace: 80 passed.
- Documentation: 140 Markdown files passed.
- JSON: 106 files parsed.
- Python wheels, source-integrity manifests, and Limited Graph Skill validation
  passed.

## Forward-test result

Runs `04ff2e46-5024-4ef0-bca0-734a39da19e5` and
`e54aaecf-4b93-42b8-9f78-64559a87a73b` each submitted one complete corner-
motion and two-cut graph. Both returned `FAILED` at `first-slice` with compact
`last_failure.kind=CHILD_PHYSICAL_ACTION_NOT_SUBMITTED`, the exact Slicing
reason, and `physical_action_submitted=false`. Each counted only the completed
corner move as a physical action. Neither run called Slicing directly, invoked
Contact outside the graph, or executed any later graph node.
