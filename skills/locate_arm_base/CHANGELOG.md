# Changelog

## 1.8.1 - 2026-08-19

- Reduce both default ensemble sizes from three to two while preserving the
  independent 1-8 developer controls and the shared voted-mask fitting flow.
- Accept the Agent run's selected visual model as an explicit per-run Skill
  route, deriving the matching Gemini or OpenAI backend without coupling the
  Skill environment to the Agent package. Standalone runs still default to
  Gemini Robotics-ER 2.0.
- Project the resolved post-orientation pose and final semantic axes into the
  regular Agent evidence window as a distinct image card.

## 1.8.0 - 2026-08-19

- Reduce the default independent mask ensemble and FoundationPose fitting batch
  from four to three while retaining independent 1-8 developer controls.
- Route all Skill-owned visual judgments through Gemini Robotics-ER 2.0. Keep
  the native FoundationPose score as audit provenance only, hide it from the
  fit-selection images, and lower the geometry-review confidence threshold to
  0.55 without weakening world-up or Manager activation checks.
- Add a separate resolved-pose overlay after the selected bounded world-up and
  local-Z corrections, with the final semantic X/Y/Z axes rendered for both
  the developer UI and regular Agent evidence window.

## 1.7.0 - 2026-08-19

- Bind the exact timestamped world transform before pose selection. Normalize
  the known upside-down FoundationPose family with one fixed local-X 180-degree
  half-turn, then reject any fit whose semantic arm-base +Z still lacks the
  configured world-up alignment. All raw fit renders remain available.
- Add one bounded VLM tie-break call when two below-threshold fit or orientation
  choices do not establish consensus. Accept only a unique candidate supported
  by at least two above-floor votes, and make Manager independently validate the
  complete majority proof before activation.
- Query the same capture-time transform again before publication and fail if
  the Local VIO epoch or immutable historical transform changed during the
  visual pipeline.

## 1.6.2 - 2026-08-19

- Bind live calibration candidates to the physical camera identity published by
  `camera.device_info`, and fail before candidate publication when activation-
  grade camera identity or calibration revision is unavailable.
- Add an arm-profile-backed first-VLM guidance field to the developer UI, pass
  it to every independent seed-localization request, and retain it with mask
  evidence.
- Clarify the generic seed prompt so a black target base housing is not confused
  with unrelated black support hardware.

## 1.6.1 - 2026-08-19

- Request SAM2 HOT residency once before the mask ensemble and FoundationPose
  HOT residency once before the fitting batch instead of repeating lifecycle
  transitions for every candidate.
- Declare the 180-second minimum Limited Graph allocation in discovery metadata
  for the default multi-VLM, multi-mask, multi-fit pipeline.

## 1.6.0 - 2026-08-19

- Replace same-mask dilation candidates with a configurable ensemble of
  independent VLM point prompts and independent SAM2 masks.
- Ask one mask-review VLM to remove bad masks, then retain each pixel present
  in at least half of the surviving masks using
  `ceil(survivor_count / 2)`.
- Apply one configured dilation only after the vote and reuse that exact final
  mask for every independently repeated FoundationPose fit.
- Keep the mask-attempt count and pose-fit count as separate 1–8 controls in
  the developer UI, and retain every seed, mask, review decision, vote,
  dilated mask, fit, and orientation candidate as evidence.

## 1.5.0 - 2026-08-19

- Bind live candidates to the current epoch-scoped Local VIO world frame and
  session identity, rechecking the epoch after the timestamped Fabric query.
- Give the seed-localization VLM both the base CAD atlas and the full-arm
  no-effector reference; require one negative SAM2 seed in the excluded support
  below the base joint and retain it in evidence.
- Accept two spatially agreeing seed boxes above a bounded confidence floor,
  while preserving the higher one-attempt threshold and fail-closed behavior.
- Retry transient Responses API connection and server failures once, and
  serialize runs inside the Skill so a cancelled Agent timeout cannot let a
  still-running worker overwrite a later inspection.
- Declare both successful candidates and structured failed results in the
  Agent output contract so retained mask and fit evidence survives validation.
- Align Manager activation with raw FoundationPose ranking semantics and the
  bounded repeated-fit/orientation consensus proofs.

## 1.4.1 - 2026-08-19

- Fix seed-connected mask extraction by copying Pillow's read-only array image
  before flood fill; retained evidence now distinguishes real seed-component
  filtering from bounded fallback.
- Tighten VLM-to-SAM2 localization around only CAD-defined cylindrical-base
  and mounting-plate geometry, explicitly excluding touching pedestals, risers,
  illuminated enclosures, trays, and tables.
- Retry low-confidence fit selection once and accept either the normal
  confidence threshold or two independent same-candidate decisions above a
  configured consensus floor. Apply the same bounded consensus rule to the
  profiled orientation candidates.
- Expose mask component policy plus fit and orientation decision bases in the
  developer evidence summary.

## 1.4.0 - 2026-08-19

- Treat repeated Windows-native FoundationPose registration as empirically
  nondeterministic: run every configured fit independently on the one mask
  selected by the mask-review VLM, allow the fit count to exceed the mask
  count, and retain all fit overlays and source-mask provenance.
- Retry incomplete Responses API structured output once with a larger output
  budget and retain response status, reason, ID, and attempt count in errors or
  inspection evidence.
- Retry borderline VLM seed localization and bounded orientation once while
  preserving the existing fail-closed confidence thresholds and all attempts.
- Retry a synchronized camera snapshot when its generation-checked BufferRefs
  expire during copying, and retain capture-attempt provenance.
- Crop and enhance bounded-orientation evidence around the observed mask with
  more visible semantic axes, and retain elapsed timing in failed inspections.

## 1.3.0 - 2026-08-19

- Generate a configurable set of seed-connected, box-bounded SAM2 dilation
  candidates; the initial qualification set uses radii 0, 4, 8, and 12 pixels.
- Add separate one-run mask-candidate and FoundationPose-fit count controls to
  the Skill developer UI; defaults remain four and four.
- Ask the VLM to select the strongest semantic mask while retaining every mask
  overlay and decision with per-stage timing.
- Run one Windows-native FoundationPose fit per mask candidate, render the
  projected CAD and supporting mask for every result, and ask the VLM to select
  the best geometric fit before bounded orientation resolution.
- Stop applying an invalid zero threshold to FoundationPose's uncalibrated raw
  ranking score.
- Project all mask and fit overlays into the Agent window, including retained
  evidence in structured failed results.
- Refuse duplicate Windows developer-UI listeners so an older process cannot
  randomly serve stale Skill code on the same port.

## 1.2.1 - 2026-08-19

- Request assembly-selected arm Provider HOT residency through Manager and
  wait a bounded interval for `robot_arm.assembly_state` before profile binding.
- Declare the arm assembly-state capability and validate that the publishing
  Provider matches the locally selected arm Provider.
- Create per-attempt inspection evidence before external readiness checks so a
  preflight failure cannot overwrite an older run.
- Replace raw missing-stream HTTP errors with arm-readiness domain errors.

## 1.2.0 - 2026-08-19

- Add the user-supplied four-view full-arm semantic-axis reference with an
  explicit intentionally-absent-effector contract.
- Route reference images by declared VLM consumers so base-only references
  seed SAM2 while the full-arm reference resolves bounded orientation only.
- Add an exact-mesh, hash-bound static CAD preview and render all configured
  visual assets in the simplified Skill developer UI.
- Add a diagnostic-only visual pipeline that does not require the VIO world
  transform and never publishes a calibration candidate.
- Make the Agent adapter establish current VIO tracking before a full
  localization run, and improve missing-world-transform errors.
- Repair Windows Python discovery in Skill and Agent setup scripts.

## 1.1.1 - 2026-08-18

- Repair the Manager development launcher and align the Skill UI with the main
  portal theme.
- Load the selected Provider-owned arm-profile registry entry and display or
  edit its exact Locate Arm Base CAD, reference images, and flexible appendix.
- Fix generated JavaScript escaping so profile data and inspection evidence
  load in the browser.

## 1.1.0 - 2026-08-18

- Introduce the finite, effector-independent arm-base localization workflow
  with Skill-level VLM prompting, SAM2 invocation, bounded orientation
  selection, and review-only candidate publication.
