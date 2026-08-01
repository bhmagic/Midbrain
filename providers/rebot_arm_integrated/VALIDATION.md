# Integrated 0.8.1 validation

Software regression suite: 95/95 passes from the current workspace source
root; Basic Controller regression suite: 83/83 passes.

Coverage includes Manager capability readiness, provider-local
discovery/operation mapping, exclusion of experimental POS_VEL continuous and
arm POS_TOR one-shot profiles from advertised motion capabilities, configured
CONTACT_WORK POS_TOR command generation using a separately captured baseline,
deadline completion reporting distinct from stable target arrival, hard IK
position/orientation residual rejection at preview, fresh commit, and replan,
zero-length singularity handling, JOINT_6 and Cartesian/isotropic
wrench-to-joint budget calculation, physical-ceiling ratio/residual
saturation without early task abandonment, corrected gripper targets,
latched gripper keepalive and joint-7 propagation into arm envelopes,
serialized Basic lease transitions, exact provider-local control audit
lifecycle, strict pre-action audit failure, non-masking post-action audit
failure, direct MIT execution, POS_VEL speed saturation, changed-target-only
HOLD_LB replanning, TRANSIT_SPEED stable-arrival gating, controller-owned
nonphysical transit-path planning, adaptive waypoint continuity, exact
one-time signed transit commit, measured-arrival staged execution, per-joint
and Cartesian speed enforcement, final endpoint hold with explicit release,
authorization-header redaction from the exact local audit, payload forwarding,
Fabric freshness/duplicate handling, versioned Manager-task-authority versus
Integrated-writer versus Basic-lease shadow comparison with separate fencing
namespaces, standby classification, and disagreement metrics, and reachable
6-DoF kinematics.

The Phase 2 and Phase 3 Gate 0 reports contain the completed guarded physical
coverage. CONTACT_WORK, physical-ceiling saturation, Cartesian-wrench work,
payload behavior, and strict audit enforcement remain outside the validated
physical scope.

The signed staged path completed one guarded Phase 5 no-contact observation
transit. The authority-lineage evaluator and final-hold gripper STOP correction
remain software-only; Manager-to-local lease lineage is not yet bound or
enforced.

The complete provider manifest covers and verifies 56 non-runtime source
files. Provider `.venv`, active machine-local configuration, caches, generated
egg-info, runtime audit logs, and checksum files are intentionally excluded
from source validation.
