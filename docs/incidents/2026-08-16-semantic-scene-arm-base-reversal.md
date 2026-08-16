# Semantic scene arm-base reversal handoff

## Observed behavior

After activation of stationary calibration
`20260816T100145Z-14e677e3`, the semantic spheres and the toilet-paper
visible-surface AABB appeared reversed relative to the arm-base axis.

## Latest affected run

- Agent run: `c7f8d05e-1181-4b0a-9c86-40849b4ea889`
- Agent-session message: `9339`
- Graph run: `a963349d423940db87b4a7e7d1405dea`
- Scene provider instance: `9626f9db-eaf0-4a14-ae1a-4df615ca76a8`
- Scene boot: `0a4c6a0f-1703-45b9-aa8b-4b46f851e427`
- Scene revision: `scene-0a4c6a0f-000000000137`
- Scene object: `toilet-paper`
- Scene extent: `VISIBLE_SURFACE_AABB`
- Scene frame: `rebot_arm_base`
- Named corner: `right_forward_up`
- Source point: `[-0.415087545084301, -0.11008875536182228, 0.13603176944577916]` metres
- Active calibration: `a4d3564d-6b31-45e5-bf9d-10836787bb53`
- Calibration revision: `20260816T100145Z-14e677e3`
- Resulting world point: `[1.32970448140856, -0.5835777946302757, -0.18391227409057412]` metres

The active `world_from_base` transform recorded for that calibration was:

```json
{
  "translation_m": [
    1.0253110905696048,
    -0.26890493662073245,
    -0.4656223795778497
  ],
  "rotation_xyzw": [
    0.0014049733927533846,
    -0.014555881635382873,
    0.8556046830534663,
    0.5174232104347078
  ]
}
```

## Earlier comparison run

- Agent run: `95ce28be-1630-4638-9719-ecb2c8a208cf`
- Agent-session message: `9263`
- Scene provider instance: `291e96c6-5841-4ee8-8efe-1ffb4fb3671f`
- Scene boot: `723d3610-5f03-41bd-b3cc-e7afa16295f5`
- Scene revision: `scene-723d3610-000000000086`
- Scene object: `toilet_paper`
- Scene extent: `VISIBLE_SURFACE_AABB`
- Scene frame: `rebot_arm_base`
- Named corner: `right_forward_up`
- Source point: `[0.4028558376032087, 0.006840127761027492, 0.139933037767909]` metres
- Active calibration: `2dee9ab7-428d-488a-a3ac-ca03cf75c120`
- Calibration revision: `20260816T083353Z-2f8d4a5e:translation-refinement:1`
- Resulting world point: `[0.716507989898857, 0.15689386028646377, -0.17739728554910822]` metres

## Graph-carried values

The affected graph bound these three fields from the point-derivation result
into `move_effector_to_world_point` without editing them:

- `target_position_world_m`
- `target_world_frame_id`
- `target_session_epoch`

The graph did not contain sphere generation, AABB generation, arm-base
coordinate conversion, or calibration activation nodes.

## Newest affected run

- FoundationPose Agent run: `dcefa1fe-444e-40c2-8f77-7b6840e03d58`
- Scene and motion Agent run: `8d617143-0646-4e10-a44d-92f8b39477f5`
- Superseded calibration: `20260816T103200Z-a36cc21a`
- Superseded activation: `b2fbed12-c157-432a-9170-f999eb0ef131`
- Replacement calibration: `20260816T103534Z-731739f9`
- Replacement activation: `4c6633cd-71c5-4415-8d60-99374979a0ec`
- Scene revision: `scene-2024c50a-000000000021`
- Scene source point: `[-0.3539274563832034, -0.16919827814410315, 0.19585638616188855]` metres
- Scene frame: `rebot_arm_base`
- Resulting world point: `[1.3595886970463233, -0.503937279794704, -0.13008736336639404]` metres

The replacement FoundationPose camera-to-base rotation was 178.963 degrees
from the superseded camera-to-base rotation and 6.676 degrees from the earlier
working calibration `20260816T083353Z-2f8d4a5e`.

Applying the superseded camera-to-base transform to the newest scene source
point produced camera point
`[0.04956230140526935, 0.06810421003686545, 0.7748186335511873]` metres.
Applying the replacement camera-to-base transform to that same source point
produced
`[0.509572115008919, -0.20093404149065813, 1.3551369774520527]` metres.

## Confirmed stale-transform cause

Manager published each replacement activation to Fabric, but changed the prior
active records to `SUPERSEDED` only in Manager memory. It did not publish
revoked envelopes for the superseded camera, VIO, and arm-base transforms.
Fabric therefore retained both reviewed transform graphs. The scene tracker
could derive an arm-base point through the expired graph, after which the
point-to-world operation used the replacement graph.

Manager now sends the superseded transforms as `REVOKED` and
`motion_usable=false` together with the accepted replacement transforms in one
Fabric batch. It commits the matching Manager records only after Fabric
accepts that complete transition.

## FoundationPose axis evidence and safeguard

The saved gripper masks from calibrations
`20260816T103200Z-a36cc21a` and `20260816T103534Z-731739f9` localized the arm
shoulder/background rather than reliable end-effector support. Their retained
VLM confidences were 0.58 and 0.56. Although the second calibration returned to
the earlier working orientation family, those masks were not reliable enough
to decide the base-axis 0/180-degree ambiguity.

FoundationPose alignment now requires gripper localization confidence of at
least 0.70 before aligned gripper depth can act as axis authority. Lower or
non-numeric confidence leaves that reference unavailable, records a warning,
and invokes the existing bounded RGB overlay review.
