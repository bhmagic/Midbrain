# Action: let go

This finite Skill opens the gripper under position/effort control, verifies measured opening, and requests the timed 50 Hz MIT transition into gripper-group float. When a confirmed carry exists, it also clears the runtime attachment and then relaxes Contact. Without a confirmed carry, it performs the gripper-only open-and-float path and does not acquire Contact. The 85 C new-grip admission gate never blocks release or opening.

The default measured-open timeout is 10 seconds. The open command uses the mounted effector control profile rather than duplicating hardware limits in this Skill.
