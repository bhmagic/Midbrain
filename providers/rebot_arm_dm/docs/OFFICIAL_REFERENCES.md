# Reference implementation notes

- Official Seeed reBot Arm Python SDK and examples: `https://github.com/Seeed-Projects/reBotArm_control_py`
- Official Seeed reBot hardware project: `https://github.com/Seeed-Projects/reBot-DevArm`
- Official B601-DM real-machine performance test: `https://github.com/Seeed-Projects/reBot-DevArm/blob/main/hardware/reBot_B601_DM/performance_testing/Performance_Testing.md`
- Damiao DM-J4340P-2EC manual: `https://damiao.enactic.ai/en/products/hardware/dm-j4340p-2ec-v1.0/`
- Damiao DM-J4310-2EC V1.1 manual: `https://damiao.enactic.ai/en/products/hardware/dm-j4310-2ec-v1.1/`
- Supplied Unity bridge: `ReBotArm_Unity_Bridge_Final_Bundle_2026_06_21`

The nominal kinematic origins, masses, centers of mass, inertia tensors, and fixed-tool transform in `arm_model.factory.json` were transcribed from the official fixed-end URDF. The first three configured motor models are DM-J4340P and the remaining four are DM-J4310 in the official configuration. Physical identity and revision must be checked on the installed arm.

The upstream sources were rechecked live on 2026-08-13. The official Seeed SDK
was at commit `edf9279905f7ba399da0b5d81ef60fd359851d0c`. Its
`config/rebotarm_dm.yaml` declares POS_VEL `vlim` values of 5.0 rad/s for
joints 1-3 and 3.0 rad/s for joints 4-6, with MIT gains of 120/8 and 18/2
respectively. Those values describe the official controller configuration;
they are not, by themselves, validated autonomous whole-arm operating speeds.

Midbrain keeps a separate configured motor envelope of 5.0 rad/s for the three
DM-J4340P joints and 10.0 rad/s for the three arm DM-J4310 joints and the
gripper. The Basic operational command limits are narrower than that motor
envelope: 4.0 rad/s for all six arm joints and 2.1 rad/s for the gripper. The
J1-J3 value is 80% of the official reBot application value; the developmental
J4-J6 value exceeds the official application `vlim` of 3.0 rad/s but remains
below the configured 10.0 rad/s motor envelope. These values are not a claim of
physical qualification. Basic publishes those mode-specific limits under
`command_limits`; higher providers consume that public boundary instead of
calibration or developer-test fields. At 24 V,
5.0 rad/s is below the
DM-J4340P 52 rpm no-load characteristic but above its 36 rpm rated
characteristic; 10.0 rad/s is below the DM-J4310 120 rpm rated characteristic.
The wider values remain motor-envelope configuration, not validated
continuous-duty whole-arm operating speeds. The active machine-local model and
Provider telemetry are authoritative for an installed arm.

The official hardware repository was at commit
`a0c709638a341d6b12b005f22f5592c4b1579f56` during the same recheck. The
performance guidance below is therefore tied to that revision rather than an
older web cache.

The official fixed-end URDF declares the broad mechanical joint ranges used by
the upstream kinematics. Its velocity attributes of 50 and 200 conflict in
scale with the SDK POS_VEL configuration and the motor characteristic speeds,
so Midbrain must not interpret those URDF values as qualified rad/s limits
without an upstream unit clarification.

The Damiao manuals document MIT Kp in `[0, 500]`, Kd in `[0, 5]`,
position-speed control with a maximum-velocity command, and
zero-speed/measured-position capture before a mode switch. The current Seeed
SDK configuration nevertheless specifies Kd 8 for joints 1-3. Treat that as an
unresolved version/protocol conflict: verify the installed motor firmware and
actual command encoding before making the upstream gain an autonomous profile
default. The DM-J4340P-2EC 24 V characteristic speeds are 36 rpm rated and 52
rpm no-load; the DM-J4310-2EC V1.1 values are 120 rpm rated and 200 rpm no-load.
Manufacturer no-load speed, protocol mapping range, arm-level SDK
configuration, Basic provider caps, and a physically qualified autonomous
profile are separate limits and must remain separately identified.

Seeed's April 2026 B601-DM V4-motor real-machine test recommends a working load
below 1.5 kg, working radius below 70% reach (reported as 450 mm), and speed
below 70% of maximum. Its extreme tests stopped because motor 2 reached a
thermal limit; the report explicitly says to verify performance on the actual
arm. These are arm-level operating recommendations, not permission to use 70%
of every raw motor or URDF value without trajectory and stopping qualification.
