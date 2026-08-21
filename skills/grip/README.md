# Action: grip

This finite Skill closes the gripper at its current measured pose. Before Contact acquires the arm, the Skill activates Integrated, requires verified `FLOAT` with no active trajectory, moves Integrated to `WARM`, verifies its Basic arm-group lease is inactive, and only then activates Contact. Contact holds the measured acting-frame pose with every arm joint in `POSITION_EFFORT_LIMITED`.

The Skill rejects a new grip when any active-joint temperature is unavailable, stale, or at least 85 C. For normal objects, the owner-observed default close request moves toward `+12` degrees at the owner-requested attended-development limit of 4 rad/s, and the stable-contact timeout is 10 seconds. Grip contact requires absolute measured gripper-motor torque at or above `0.15` N m for 10 consecutive 50 Hz samples; position and velocity are diagnostic only. Basic retains a 0.75 native FORCE_POS translation, so that request sends a 3 rad/s native motor ceiling. The request exceeds the official reBot application 3 rad/s value and is not autonomous physical qualification. Basic separately enforces `+17` degrees as the absolute gripper close limit. After stable gripper contact, both Contact and Grip confirm the same carry and attachment identities. If stable contact is not confirmed, the Skill opens to the functional `-180` degree position, verifies measured openness, enters MIT float, relaxes Contact, and returns `FAILED_TO_GRIP` without creating a carry. Cleanup failure is reported explicitly rather than hidden. Soft or thin objects require a different Skill.

Agent calls provide the intended object's stable identity as
`object_binding.object_id`. This field identifies the object for the runtime
attachment; it does not replace the independent physical-contact inference.

`gripping_torque_limit_nm` is a motor torque ceiling, not calibrated jaw force. The Skill owns its `.venv` and motion-profile configuration. Physical force, contact inference, and object-attachment qualification remain required.

Release changes are recorded in [CHANGELOG.md](CHANGELOG.md).
