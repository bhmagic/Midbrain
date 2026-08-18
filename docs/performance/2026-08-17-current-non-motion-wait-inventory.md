# Current non-motion wait inventory

Implementation note (2026-08-17): the runtime-snapshot and Skill-result
mechanisms described below were the measured pre-change baseline. The approved
compact Manager catalog and mandatory discovery-v3 two-tier Skill results are
now implemented; see the linked feasibility/implementation record for the new
boundary. Historical timing and payload measurements in this inventory remain
unchanged.

Date: 2026-08-17

## Purpose and definition

This inventory ranks the current observed time that was not spent executing a
physical robot trajectory. It follows the operator's broad definition of
"waiting": Agent deliberation, model inference, perception computation,
Provider lifecycle/readiness, polling, controller settling, Skill planning,
authorization transport, graph bookkeeping, and result serialization all
count. Only signed physical movement time is excluded.

The primary evidence is the latest successful calibration-and-motion run
`92e8772a-5edb-4f4a-86f7-1dce79d6f401` and the latest successful semantic-
scene/corner-motion/two-slice run
`2792e656-a7cc-4f26-806b-789c03b5ded3`. Agent wall and top-level tool intervals
come from `test_agent/run/agent_run_journal.v1.sqlite3`. Graph child intervals
and structured controller/Provider results come from
`test_agent/run/agent_sessions.sqlite3`.

Integrated publishes both signed `planned_duration_s` and the longer
`completion.elapsed_s`, but it does not publish when the final trajectory
sample was actually sent. The controller preserves every 50 Hz path sample
and slows the physical trajectory when command cycles overrun. Therefore,
`completion.elapsed_s - planned_duration_s` mixes continued physical motion
with final position/velocity settling; it is not defensibly all movement or
all waiting.

The strict non-motion figures consequently use a range. The lower bound treats
the entire Integrated completion interval as physical/mixed controller time.
The upper bound treats only the signed plan as physical and the entire excess
as non-motion. The real value lies between those bounds. Contact publishes its
three `velocity_limited_transition_time_s` values, but those transitions occur
inside the signed post-acceptance delay windows rather than after them. The
tables show that overlap explicitly instead of adding both intervals.

All figures below are single-run observations rounded to the nearest
millisecond. They are useful for finding the current critical path, but they
are not latency guarantees or multi-run benchmark distributions.

## Ranked Skill invocations: longest non-motion upper bound first

The table lists every Skill invocation in the two retained runs. Repeated
movement and slicing calls are kept separate because their waiting time was
not identical. Rows are ordered by the upper bound; overlapping ranges mean
the exact order of some motion rows is not currently knowable.

| Rank | Skill and invocation | Strict non-motion range | Input / capture | Model / computation | Contact delay window | Contact movement inside delay | Integrated signed plan | Integrated excess: movement / settling mixed | Other exact non-motion | Unsplit exact non-motion | Skill wall |
| ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | `calibrate_stationary_workcell`: FoundationPose arm-base candidate | **84.312 s** | 2.594 s | 79.108 s | — | — | — | — | 2.611 s | — | 84.312 s |
| 2 | `refine_arm_root_translation`: five-sample VLM refinement | **12.328 s** | — | 12.328 s | — | — | — | — | — | — | 12.328 s |
| 3 | `slice_with_blade`: first 20 cm slice | **10.173–11.660 s** | — | — | 9.000 s | 0.452 s | 1.200 s | 1.487 s | 1.625 s | — | 13.312 s |
| 4 | `slice_with_blade`: second 20 cm slice | **9.577–10.814 s** | — | — | 9.000 s | 0.455 s | 1.200 s | 1.237 s | 1.032 s | — | 12.469 s |
| 5 | `establish_world_axis` | **7.172 s** | — | — | — | — | — | — | — | 7.172 s | 7.172 s |
| 6 | `perform_relative_effector_motion`: raise 20 cm | **4.078–5.385 s** | — | — | — | — | 1.740 s | 1.307 s | 4.078 s | — | 7.125 s |
| 7 | `move_effector_to_world_point`: 10 cm above first slice point | **0.781–2.348 s** | — | — | — | — | 1.620 s | 1.567 s | 0.781 s | — | 3.968 s |
| 8 | `move_effector_to_world_point`: workpiece corner offset | **0.312–2.224 s** | — | — | — | — | 1.620 s | 1.912 s | 0.312 s | — | 3.844 s |
| 9 | `perform_relative_effector_motion`: arm-base forward 30 cm | **0.265–1.837 s** | — | — | — | — | 1.600 s | 1.572 s | 0.265 s | — | 3.437 s |
| 10 | `inspect_arm_semantic_scene` | **0.313 s** | — | — | — | — | — | — | 0.313 s | — | 0.313 s |
| 11 | `derive_fabric_world_point` | **0.140 s** | — | — | — | — | — | — | 0.140 s | — | 0.140 s |
| 12 | `offset_world_point` | **0.032 s** | — | — | — | — | — | — | 0.032 s | — | 0.032 s |
| 13 | `translate_fabric_direction_to_world` | **0.031 s** | — | — | — | — | — | — | 0.031 s | — | 0.031 s |

`Input / capture` is time before the selected observation became the phase
reference. `Model / computation` is wall-clock critical-path work, not the sum
of concurrent requests. `Contact delay window` is signed command spacing and
contains the separately shown Contact movement; those two cells are not
additive. `Integrated signed plan` is the minimum planned physical timeline.
`Integrated excess` is the ambiguous completion-minus-plan interval. `Other
exact non-motion` is outside the controller completion and Contact delay
windows. `Unsplit exact non-motion` means there is no finer phase boundary. A
dash means no separately attributed interval in that column, not an assertion
that the underlying operation took exactly zero time.

The three rounded FoundationPose component cells add to 84.313 seconds while
the measured wall is 84.312 seconds. This one-millisecond difference is only
independent display rounding. Its 79.108-second computation window includes
eight retained base samples plus FoundationPose/VLM processing; it is not a
pure GPU inference measurement. The refinement's eight individual VLM calls
sum to 31.378 seconds, but overlap reduces their critical-path wall to the
12.328 seconds shown in the table.

### Explicit signed delays

These are intentional Slicing motion-profile intervals after Contact accepts
engage, slice, and retract. The current profile signs 3.500, 2.000, and 3.500
seconds, respectively. Contact waits for the greater of the profile interval
or its transition-time lower bound plus margin before allowing the next
command. Therefore, the 9.000-second total is a command-spacing window, not
nine seconds with an idle arm. The measured Contact transitions occupy 0.452
seconds of the first slice and 0.455 seconds of the second slice inside those
windows. The remaining 8.548 and 8.545 seconds are known non-motion spacing.

The profile values can technically be reduced, but that changes the signed
contact-work policy. It requires cutting-behavior and safety validation; it is
not a Limited Graph optimization. The transition lower bound and runtime
margin still apply even if the profile is shortened.

### Integrated excess: movement and settling mixed

For the raise, the controller reported a 1.740-second signed plan and a
3.047-second completion interval, leaving 1.307 seconds of mixed excess. It
sent 105 command frames for an 88-sample trajectory, recorded 32 schedule
overruns, preserved rather than skipped trajectory samples, and then required
ten stable 50 Hz feedback samples within the configured joint-position and
joint-velocity tolerances. Some of the 1.307 seconds was therefore continued
physical path execution; some was endpoint settling.

This value should not simply be deleted or shortened as idle sleep. Possible
improvement areas are command-loop overruns, Basic command latency, feedback
cadence, and safe arrival tuning. Reducing stable-sample or tolerance checks
without physical validation would weaken the measured-arrival contract. The
clean instrumentation fix is to publish the actual final-trajectory-sample
timestamp and the later stable-arrival timestamp separately.

### Other Skill and Fabric overhead

This is time outside the controller completion or Contact delay windows. It
can include Provider residency handover, transform and direction resolution,
IK/collision preview, authorization, commit transport, Fabric reads, polling,
and result packaging. It is real non-motion wall time, but the current trace
does not timestamp every listed subphase separately.

The raise's 4.078 seconds is strongly associated with first-call Integrated
Provider preparation, not with the fact that it is geometrically an upward
move. The graph recorded a handover to `robot_arm.primary.integrated` HOT on
that child. Two earlier 2026-08-17 calibration chains show the same first-raise
outside-controller overhead at 4.391 and 4.485 seconds, while their later
forward moves used 0.282 and 0.297 seconds. An earlier graph that explicitly
made the controller HOT before submission used only 0.296 seconds outside the
controller for its raise.

This supports a residency/warm-up explanation, but does not prove that all
4.078 seconds was lifecycle work because graph preparation events receive
their timestamps only after the child returns. The later `moveabove` also
needed an Integrated HOT handover after Slicing had moved Integrated to WARM
and used Contact, but the already-running transition cost only 0.781 seconds.
Thus, "first movement" is a useful symptom; "first Integrated call while the
required Provider is not already HOT and current" is the more accurate cause.

The FoundationPose graph returned `CALIBRATION_FAILED` because its valid
candidate required explicit review rather than being immediately motion-
usable. The Agent then reviewed and activated that same candidate in 200 ms.
The 84.312-second Skill measurement is therefore retained even though the
graph terminal label described an incomplete activation workflow.

## Slicing delay detail

Both slices resolved the current default timing policy to 3.500 seconds after
engage, 2.000 seconds after the 20 cm slicing command, and 3.500 seconds after
retract. The middle value is computed as
`slice_length_m / slice_wait_speed_m_s`, or `0.2 / 0.1`. It is command spacing,
not Cartesian speed control.

| Slice | Engage window | Slice spacing window | Retract window | Total delay window | Contact motion inside window | Known non-motion inside window | Integrated signed plan | Integrated mixed excess | Other exact non-motion | Strict total non-motion range | Total Skill wall |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| First | 3.500 s | 2.000 s | 3.500 s | **9.000 s** | 0.452 s | 8.548 s | 1.200 s | 1.487 s | 1.625 s | **10.173–11.660 s** | 13.312 s |
| Second | 3.500 s | 2.000 s | 3.500 s | **9.000 s** | 0.455 s | 8.545 s | 1.200 s | 1.237 s | 1.032 s | **9.577–10.814 s** | 12.469 s |

Each Contact step also published `next_command_timeout_s = 6.0`. That value is
an inactivity watchdog and was not an additional six-second wait. Counting it
as elapsed time would triple-count the observed Skill wall.

### Between the two slice strokes

The graph trace shows no Agent or model turn between the two Slicing children.
The first `slice_with_blade` child completed at graph elapsed time 17.656
seconds, and the second child started at 21.656 seconds: an exact 4.000-second
inter-child interval. It consisted of 32 ms in `offset_world_point` followed
by 3.968 seconds in the operator-requested move to the point 10 cm above the
first slice start. That move reported a 1.620-second signed plan, 3.187 seconds
to controller completion, and 0.781 seconds outside the controller interval.

The visually apparent stroke-to-stroke interval is longer than those four
seconds. After the first SLICE command is accepted, its profile waits 2.000
seconds before RETRACT and 3.500 seconds after RETRACT. After the reposition,
the second Slicing Skill completes its Integrated alignment, measured at
2.437 seconds, and waits 3.500 seconds after ENGAGE before issuing its SLICE
command. These known sequential intervals total at least 15.437 seconds from
the first SLICE command acceptance to the second SLICE command acceptance,
before small preparation and cleanup work. Contact transition motion occurs
inside the signed hold windows, so this is not 15.437 seconds of an idle arm.
The retained result does not publish per-command acceptance timestamps, so an
exact physical slice-end-to-next-slice-start interval cannot be reconstructed.

## VLM refinement detail

The five-sample refinement retained the following model-route durations:

| VLM work | Individual durations | Sum |
| --- | --- | ---: |
| Five landmark detections | 4.391, 4.438, 5.125, 4.047, and 3.813 s | 21.814 s |
| Three accepted-sample quality reviews | 3.704, 2.875, and 2.985 s | 9.564 s |
| All Provider work | Eight calls | **31.378 s** |

The 31.378 seconds must not be added to the 12.328-second Skill wall. The
requests overlapped, so 12.328 seconds is the critical-path time visible to the
workflow. Samples two and four ended after their detection rejected the scene;
the other three proceeded to quality review and aggregation.

## Semantic obstacle-map and Provider readiness path

The current two-slice run spent 15.691 seconds in the top-level
`set_provider_residency` call for `world_model.arm_scene_compiler` and its HOT
dependencies. The Manager ultimately reported the compiler as `already_hot`
and all dependencies ready. This was still real blocking wall time.

The retained readiness result exposes only one internal sub-timing: SAM2's VLM
mask-quality review took 2.219 seconds. The remaining 13.472 seconds contains
some combination of Manager lifecycle transport, dependency observation,
annotation/segmentation, semantic fusion, scene compilation, and readiness
polling, but those phase durations were not retained at the Agent boundary.
They cannot be divided without guessing.

This specific scene did not wait for a conventional 3D point cloud. The
compiled result reports `point_stream = semantic-only-fallback`, zero input
points in the base ROI, and `depth_mode = SEMANTIC_MASKED_DEPTH`. It accepted
257 SAM2 semantic assertions. The scene used by the graph was observed 1.089
seconds before graph submission and the 313 ms inspection reported it as 1.258
seconds old. Consequently, later scene production overlapped the Agent's
22.706-second graph-construction interval and did not extend the graph's
critical path.

The compiler source contains `compile_duration_ms` and per-phase timing fields,
and SAM2 computes `tick_elapsed_ms`, but the retained Agent/Skill projection
did not preserve those values for this run. This is the main current telemetry
gap for answering questions such as "sphere computation versus point-cloud
wait" precisely.

## System waiting outside child Skills

The latest two-slice run took 91.225 seconds end to end. The retained evidence
bounds strict non-motion time at **78.475–84.678 seconds**. The complementary
signed-plan-to-full-controller range is 6.547–12.750 seconds and mixes physical
movement with endpoint settling at its upper end.

| Category | Time | Share of full run |
| --- | ---: | ---: |
| Agent deliberation, graph construction, inter-tool gaps, and final response | **40.606 s** | 44.5% |
| Child-Skill strict non-motion | **21.359–27.562 s** | 23.4–30.2% |
| Arm-scene Provider lifecycle/readiness | **15.691 s** | 17.2% |
| Scene-policy configuration and runtime inspection tools | **0.632 s** | 0.7% |
| Graph SDK wrapper and bookkeeping | **0.187 s** | 0.2% |
| Signed physical minimum through full controller/mixed upper bound | 6.547–12.750 s | 7.2–14.0% |

The Agent portion can be ranked further:

| Rank | Agent interval | Time |
| ---: | --- | ---: |
| 1 | Provider-ready result to graph submission: construct and serialize eight Skill nodes plus nine terminal nodes | **22.706 s** |
| 2 | Graph completion to terminal answer | **8.243 s** |
| 3 | Run start to first policy call, including initial interpretation and deferred-tool discovery | **5.348 s** |
| 4 | Runtime inspection completion to Provider lifecycle call | **2.438 s** |
| 5 | Policy publication completion to runtime inspection call | **1.871 s** |

These five intervals correspond to **five sequential primary Agent LLM
response cycles**, excluding every Provider-side VLM call. Policy publication
and runtime inspection were not emitted by one model response: the retained
history places the policy function output before the later inspection
function call, and the 1.871-second interval between them is a separate model
cycle. The five Agent-observed response intervals sum to 40.606 seconds. They
include API transport, streamed reasoning/tool-argument generation, and final
response serialization, so they are not server-only inference measurements.

The 22.706-second graph-authoring model cycle split at retained event
boundaries as follows: 8.412 seconds from the Provider result to the first
completed reasoning item, another 3.108 seconds to the second completed
reasoning item, and 11.186 seconds from there to the completed graph tool
call. The last interval includes generation, streaming, and validation of an
8,239-character tool argument containing 17 node records: eight Skill nodes
and nine terminal nodes. It was therefore not merely a short decision followed
by idle time.

That model cycle also inherited a large session history. Before its own two
reasoning items, the retained history occupied 310,889 serialized JSON
characters. Tool outputs from the preceding alignment-and-raise command
accounted for 146,629 characters, and outputs already produced for the current
command accounted for 106,976 characters; the current runtime-inspection
output alone was 101,700 characters. These are retained serialization sizes,
not model-token counts, because this run did not retain per-response token
usage. They nevertheless identify context ingestion and a verbose graph
argument as the two evidenced contributors to the long model cycle.

### Graph argument format overhead

The submitted `run_limited_graph` tool argument used 8,239 characters; its
inner graph object used 8,229. The `nodes` array occupied 7,937 characters, or
96.5% of the graph object. Eight Skill nodes used 5,917 characters and nine
terminal nodes used 2,002 characters. Graph metadata and all six bounded-run
limits were comparatively small.

The current strict schema requires every node to carry all four nullable union
payload fields (`skill`, `switch`, `model_route`, and `terminal`) even though
`kind` selects exactly one. This graph consequently carried 51 unused null
payload fields. A conservative transformation that changes no workflow
semantics found 1,348 characters of format-induced redundancy:

| Format overhead | Avoidable characters |
| --- | ---: |
| 51 unused node-union payloads encoded as null | 806 |
| `source_name: null` repeated in 12 node-result bindings | 228 |
| Skill arguments encoded as escaped JSON strings instead of JSON objects | 174 |
| Required empty `initial_values` or `bindings` arrays | 48 |
| Required null retry-condition fields | 92 |
| **Confirmed structural total** | **1,348 (16.4%)** |

The graph also used eight separate failure terminals with distinct statuses
and messages. Replacing them with one generic failure terminal would save
1,679 characters. Combined with the structural cleanup above, the same main
workflow would fall from 8,229 to approximately 5,485 graph-object characters,
a 33.3% reduction. This second saving is not classified as pure junk: the
current contract intentionally makes each failure outcome explicit and
human-readable. A future contract could preserve typed failure provenance in
the engine result instead of requiring each graph author to encode a terminal
node and message.

The 2,101 characters of typed bindings, 1,658 characters of child arguments,
bounded-run limits, child retry bounds, and success/failure routing carry real
workflow or safety meaning. They should not be removed merely to shorten the
payload. A discriminated node representation, host-filled defaults, and an
engine-generated failure result could reduce authoring text while preserving
those boundaries. Such a schema change would alter the public Limited Graph
contract and was not made by this audit.

### Per-Agent-model-turn context relevance

The retained session permits a relevance audit even though it does not retain
per-response token usage. `Input footprint` below is serialized session JSON,
not a model-token count. It excludes the system instructions and currently
offered tool definitions, so the real model input surface was larger.

| Agent model turn | Input footprint at turn boundary | Newly material input | Relevance finding |
| ---: | ---: | --- | --- |
| 1: request to policy call | 172,790 characters | Current 613-character user request plus the preceding command history | The preceding alignment-and-raise command contributed 146,629 characters of raw tool outputs. Current physical state was already hosted by Manager/Fabric; the raw graph traces and child results were not needed to choose the scene policy. |
| 2: policy result to runtime inspection | 194,268 characters | 356-character policy result and 15,534 characters of deferred schemas for the five later-used child Skills | The policy result was appropriately compact. The five Skill schemas were relevant to later graph authoring, but not to the immediate inspection decision. The raw preceding-command outputs remained irrelevant. |
| 3: runtime result to Provider residency | 303,286 characters | 101,700-character runtime snapshot | The decision needed the scene compiler identity, its stopped state, its dependency identities/states, and required readiness. Instead it received all eight complete Provider reports and 46 capabilities. The Integrated report alone used 53,409 characters; FoundationPose, Local VIO, and Contact added another 10,277 characters, while most camera and Basic details were also irrelevant to this lifecycle choice. |
| 4: Provider-ready result to graph call | 310,889 characters | 4,920-character residency/readiness result | Only ready status, exact Provider identity, dependency readiness, and semantic-scene coverage were needed. The 4,179-character nested readiness document repeated capability maps, instance/boot identities, timestamps, and diagnostics that were not used to author the graph. The much larger runtime and preceding-command outputs also remained in context. |
| 5: graph result to final response | 456,024 characters | 128,485-character graph result | The graph result contained a 46,310-character execution trace and 81,330 characters of full child results. The final answer needed the terminal outcome plus a small projection of scene counts, derived points/direction, controller completion, timing, and slicing submission semantics. The trace was audit evidence, not response-authoring input, and most child fields were unnecessary for the narrative. |

The session-history guard is item-bounded rather than byte- or token-bounded.
Its configured limit is 32 items, while the preceding command occupied only
14 items, so both large graph results were retained in full. The runtime
snapshot is also deliberately built as complete Manager evidence, including
controller telemetry and launch configuration. That is appropriate for a
developer inspection surface but substantially over-scoped for the regular
Agent's narrow Provider-selection decision.

The deterministic intent route narrows the offered tools but appends its
route instructions to the complete regular-Agent instruction text instead of
replacing unrelated sections. Consequently, all five turns also received
general instructions for FoundationPose initialization, safe home, VIO reset,
mount confirmation, user-image authority, and other paths not applicable to
this scene-and-slicing command. Their exact serialized size was not retained,
so this audit does not assign them a latency amount.

The 15,534-character deferred schema result is not classified as useless: all
five discovered Skills appeared in the final graph, and their declared input
and output pointers were needed for safe bindings. It is nevertheless a
candidate for a smaller graph-authoring projection. Likewise, full traces and
Provider reports remain valuable in Fabric and diagnostic logs; the finding
is that they should not be projected wholesale into the Agent model context.

### Replay-safe completed-turn memory

The previous alignment-and-motion command demonstrates why completed history
should not be discarded entirely. A later request such as "repeat the early
motion" needs the original operator wording and an ordered action ledger. It
does not need raw controller telemetry, graph traces, observation samples, or
stale prepared-action artifacts.

A representative replay-safe projection of that actual completed turn used
2,972 compact JSON characters. It retained the exact 290-character operator
request and five ordered actions: establish world axis, establish and review
the FoundationPose arm-base calibration, raise 20 cm in world up, refine arm
translation with five samples, and move 30 cm along arm-base positive X. Each
entry retained its semantic label, Skill name, compact non-default arguments,
replay mode, concise outcome/state effect, and graph audit reference.

| Completed-turn representation | Size | Reduction from the 172,148-character prior turn |
| --- | ---: | ---: |
| Full retained SDK/session items | 172,148 characters | — |
| Raw tool outputs only | 146,629 characters | — |
| Representative replay-safe digest | 2,972 characters | **98.3% smaller than the full turn** |

The recommended completed-turn projection retains:

- exact operator wording and a short normalized intent label;
- ordered operation number, semantic action label, Skill tool name, and
  compact non-default arguments;
- replay semantics such as `RELATIVE_FROM_CURRENT_MEASURED_POSE`,
  `ABSOLUTE_WORLD_TARGET`, `NEW_OBSERVATIONS_UPDATE_CURRENT_STATE`, or
  `REEVALUATE_CURRENT_STATE`;
- result status, `physical_motion_completed` when applicable, relevant
  profile-selection semantics, and concise state effects such as an applied
  calibration revision;
- coordinate source semantics and frame/session identity only when the
  operator supplied or explicitly intends an absolute coordinate;
- graph run/hash or equivalent Fabric/log reference for on-demand diagnostics;
- explicit replay rules requiring current authorization, current frame and
  calibration validation, and new previews for every physical action.

It excludes opaque preview/plan/assertion/decision IDs, signed authorization,
per-stage controller arrays, full transforms already hosted by Fabric, raw
Provider reports, graph trace entries, VLM/sample internals, repeated visual
metadata, and derived absolute endpoints for relative moves. A repeated
relative move must remain a new displacement from the current measured pose;
reusing its old endpoint would change that meaning and could be unsafe.

For profile-backed work, memory should retain both selection semantics and the
observed resolved profile. `LIVE_DEFAULT` means a normal repeat should resolve
the current default again; an operator request for the exact former profile
can use the recorded resolved profile only after current schema and policy
validation. For object- or Fabric-derived targets, retain the semantic recipe
(object, corner, offset, and frame role) and derive a fresh current coordinate
instead of replaying a stale world point.

Replacing only the prior completed turn with this digest would reduce the
first current-command model boundary from 172,790 to roughly 3,614 serialized
characters, a 97.9% reduction. At the graph-authoring boundary it would reduce
the retained footprint from 310,889 to about 141,713 characters, or 54.4%,
because the current command's 101,700-character runtime result and other live
items would still remain. At final-response generation it would reduce
456,024 to about 286,848 characters, or 37.1%, before applying the separate
runtime- and graph-result projections identified above.

This projection belongs at the completed-turn/session boundary. The current
in-progress tool loop should retain the exact SDK call/output pairing it needs
to continue safely. Manager and Fabric remain authoritative for live physical
state; the compact turn memory is an intent and replay index, not a state,
authorization, or execution cache. No session or public contract change was
made by this audit.

### Result-bloat ownership by layer

The 128,485-character final graph result is not caused by one layer. Its eight
retained child results occupied 81,330 characters. Directly calling the same
Skills would still return substantially the same child payloads to the Agent,
so that base size belongs to the individual Skill result contracts and to any
Provider details those Skills embed.

Limited Graph then added a 46,310-character trace. Its eight
`CHILD_ATTEMPT_COMPLETED` events occupied 40,428 characters because the graph
inlined a second copy of every child result at or below its 16,384-byte trace
threshold. Only the two approximately 21 KB Slicing results were replaced by
hash/size references; scene inspection, both world-point derivations, both
movement results, and direction translation were duplicated in both
`node_results` and `trace`. This duplication is a global Limited Graph output
design issue, independent of the child Skill schemas.

Full child results remain necessary inside a running graph so later bindings,
conditions, and model routes can read declared fields. They do not all need to
be returned twice in the terminal Agent-facing result. A graph terminal
projection can retain the overall outcome, node statuses, binding-relevant
outputs or requested summary fields, and Fabric/audit references while the
full internal results remain in the execution record.

The current ownership split is therefore:

| Layer | Current contribution | Appropriate responsibility |
| --- | --- | --- |
| Provider | Publishes detailed controller, perception, route, and diagnostic reports | Host full evidence in Fabric/diagnostic storage and publish a compact task/readiness summary plus reference |
| Skill | Often embeds the complete Provider result in its typed result | Publish the finite task outcome, binding fields, safety-relevant facts, and evidence references; avoid duplicating full Provider diagnostics |
| Limited Graph | Accumulates all child results and duplicates sub-16 KB results in trace | Keep full results internally for execution; return one compact terminal projection and trace references |
| Agent/session | Retains raw tool call/output items across later user turns | Preserve the current SDK loop, then replace completed turns with replay-safe memory |

The 101,700-character runtime snapshot has a related but distinct global
projection problem. `build_midbrain_runtime_snapshot` deliberately copies all
complete Manager Provider records and all capabilities, redacting only
credential-like environment values. Provider report design controls the size
of each contribution, but the regular Agent receives all of them because of
this global snapshot policy.

| Runtime snapshot component | Serialized size |
| --- | ---: |
| Eight complete Provider records | 87,426 characters |
| All 46 capability records | 13,479 characters |
| Integrated Provider record | 53,409 characters |
| Camera Provider record | 12,473 characters |
| Basic/DM Provider record | 9,379 characters |
| FoundationPose Provider record | 5,340 characters |
| Local VIO Provider record | 4,083 characters |

Integrated and Camera expose more detailed diagnostic surfaces, but that is
not by itself a Provider defect when the data is intended for Fabric or a
developer inspection surface. The regular Agent's Provider-selection decision
needed only compact identity, dependency, lifecycle, health/readiness,
requested-capability, expiry, and error fields. A separate compact runtime
projection could remove most of the 101,700 characters without changing any
Provider's diagnostic report. Individual Providers can later reduce internal
duplication or move large diagnostics behind references, but that is a
secondary optimization rather than the first architectural fix.

### Existing compact contracts and the missing result tier

The runtime snapshot is Manager-derived, but it is not currently a regulated
compact Manager view. `GET /v1/providers` returns every `ProviderView`, which
contains the complete configured process record and the Provider's arbitrary
heartbeat `details`. The Agent's misleadingly named
`build_midbrain_runtime_snapshot` preserves that complete view and only
redacts credential-like environment values.

Manager already owns the canonical fields needed for a compact view:
`process_state`, residency, health, readiness, expiry/freshness, active
instance and boot identity, and capability availability. Its
`GET /v1/capabilities` response is already a regulated compact structure, and
the Manager UI independently projects a small Provider summary. The UI
summary is presentation-specific and should not become the Agent control-plane
contract, but it demonstrates that no Provider redesign is required to create
an Agent-safe Manager projection.

Residency alone is insufficient. A Provider can be `HOT` while not ready,
unhealthy, expired, associated with a superseded instance/boot, or missing the
requested capability. A useful Manager-owned Agent view therefore needs the
Provider identity, process state, residency, health, ready/expired state,
instance/boot identity, only the requested capability and compact dependency
states, and a bounded error or blocking-prerequisite field when unavailable.
It does not need command lines, environment keys, complete Provider
diagnostics, every configured capability, or UI fields. A target-specific
projection should plausibly reduce the current 101,700-character snapshot to
low single-digit kilobytes; the exact size remains to be measured after the
contract is designed.

There is no equivalent general two-tier Skill-result mechanism today. Skill
discovery schema version 2 supplies `output_schema`, and declared property
paths form the stable validation and graph-composition surface. The direct
Skill host validates the normalized result and then returns the original
result unchanged. Declared properties are not an Agent-output filter, an open
`additionalProperties` does not move diagnostics elsewhere, and Limited Graph
retains complete child results for binding before returning them again under
`node_results`.

The repository has related but narrower precedents: Fabric references replace
same-machine large media/tensor payloads; visual-evidence events expose an
allowlisted projection instead of raw tool output; Agent run-journal records
exclude raw tool results; and Limited Graph hashes trace copies larger than a
fixed threshold. None of these creates a compact Agent/graph result plus an
on-demand diagnostic result for every Skill.

A future two-tier contract can build on the audited output schemas but cannot
be inferred from them without per-Skill semantics. The compact tier must keep
the finite outcome, completion/physical-action semantics, safety-relevant
facts, every field intentionally usable by downstream graph bindings, and
evidence or diagnostic references. Provider/controller detail, repeated
plans/transforms, sample arrays, and other diagnostics can remain behind an
explicit reference owned by the component designated in the relevant
Manager, Fabric, Provider, or Agent-journal contract. A downstream graph that
legitimately needs a diagnostic value must have that value deliberately
promoted into the compact declared surface rather than binding arbitrarily
through an opaque diagnostic object.

This audit does not select the storage owner for every diagnostic class and
does not change the Manager API, Skill discovery schema, output contracts,
authentication, authorization, or physical-control duties. Those are public
contract changes and require an explicit design decision before
implementation.

The graph itself was not a material latency source: its children occupied
34.109 of 34.125 active seconds. Internal graph edge/node bookkeeping used
16 ms, and the surrounding top-level graph tool wrapper used another 171 ms.
Within the graph, strict non-motion was 21.375–27.578 seconds. Its
signed-physical minimum was 6.547 seconds, while treating the full mixed
controller intervals as physical gives a 12.750-second upper bound.

The calibration-and-motion run gives a similar cross-check. It took 140.371
seconds. Its signed plans totalled 3.340 seconds, while full controller
completion totalled 6.219 seconds, so strict non-motion is **134.152–137.031
seconds**. Its two graphs contributed 108.155–111.034 seconds of child-Skill
non-motion, the Agent and deferred-tool search contributed 25.618 seconds,
candidate review/activation used 0.200 seconds, graph wrappers used 0.147
seconds, and graph bookkeeping used 0.032 seconds. FoundationPose, not Limited
Graph bookkeeping, dominated that run.

## Installed Skill coverage

There are 23 installed Skill manifests: 22 discoverable Agent Skills and one
undiscoverable nested FoundationPose primitive. The table below prevents an
unmeasured Skill from being mistaken for a zero-latency Skill. Numeric entries
are current retained observations from the two primary runs; `not measured`
means no defensible current-version invocation was present in those runs.

| Installed tool | Current counted non-motion observation | Discovery latency class | Coverage note |
| --- | ---: | --- | --- |
| `calibrate_stationary_workcell` | 84.312 s | HIGH | Measured once |
| `refine_arm_root_translation` | 12.328 s | HIGH | Measured once with five VLM samples |
| `slice_with_blade` | 9.577–11.660 s | HIGH | Two calls with mixed-controller ranges |
| `establish_world_axis` | 7.172 s | MEDIUM | Measured once |
| `perform_relative_effector_motion` | 0.265–5.385 s | MEDIUM | Two calls with mixed-controller ranges |
| `move_effector_to_world_point` | 0.312–2.348 s | MEDIUM | Two calls with mixed-controller ranges |
| `inspect_arm_semantic_scene` | 0.313 s | LOW | Measured once; excludes upstream scene production |
| `derive_fabric_world_point` | 0.140 s | LOW | Measured once |
| `offset_world_point` | 0.032 s | LOW | Measured once |
| `translate_fabric_direction_to_world` | 0.031 s | LOW | Measured once |
| `run_limited_graph` | 21.375–27.578 s for the two-slice graph | HIGH | Child-inclusive orchestration range; do not add it to child rows |
| `analyze_visual_scene` | not measured | MEDIUM | No invocation in the primary retained runs |
| `execute_reviewed_observation_motion` | not measured | HIGH | No invocation in the primary retained runs |
| `identify_pointed_object` | not measured | MEDIUM | No invocation in the primary retained runs |
| `localize_known_cad_object` | not measured | HIGH | Installed nested primitive; intentionally not Agent-discoverable |
| `locate_effector_front` | not measured | MEDIUM | No invocation in the primary retained runs |
| `locate_item` | not measured | MEDIUM | No invocation in the primary retained runs |
| `plan_no_contact_item_approach` | not measured | MEDIUM | No invocation in the primary retained runs |
| `register_rgbd_pixel_to_world` | not measured | LOW | No invocation in the primary retained runs |
| `register_tool_to_control_frame` | not measured | MEDIUM | No invocation in the primary retained runs |
| `reinitialize_space_cognition` | not measured | HIGH | No invocation in the primary retained runs |
| `translate_fabric_pose_to_world` | not measured | LOW | No invocation in the primary retained runs |
| `verify_rgbd_image_alignment` | not measured | MEDIUM | No invocation in the primary retained runs |

The discovery latency class is qualitative metadata, not a timeout or measured
duration. Configured retry limits and watchdog timeouts are also not listed as
waiting unless they were actually consumed in the retained run.

## Current bottleneck order

For the latest two-slice workflow, the actionable critical-path order is:

1. Agent orchestration and graph construction: 40.606 seconds.
2. Child-Skill computation and waiting: 21.359–27.562 seconds. The two Slicing
   delay windows contain 17.093 seconds of known non-motion spacing combined.
3. Semantic-scene Provider lifecycle/readiness: 15.691 seconds.
4. Signed physical minimum through controller/mixed upper bound: 6.547–12.750
   seconds. Exact physical versus settling attribution is not retained.
5. Graph machinery and auxiliary inspection/configuration: 0.819 seconds.

For calibration, FoundationPose candidate production is the dominant current
roadblock at 84.312 seconds. For repeated slicing, the default signed delays
are the dominant deterministic roadblock at 9.000 seconds per slice. For the
semantic map, phase-level Provider attribution remains unavailable even though
the total readiness wall and one VLM sub-call are known.
