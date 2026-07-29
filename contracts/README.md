# Physical AI Contracts

Version: 0.3.11 working draft.

This contract set defines Resource Providers, World State Fabric transport,
safety/authority policy, native timestamped transforms, finite Skills, device
calibration, Local VIO, startup space cognition, motion-inhibit coordination,
advisory agent Skill discovery, and provider data-route advertisement.

Document 11 defines the non-enforcing OpenAI Agents SDK discovery boundary and
its separation from deterministic Manager provider binding.

Document 12 defines Fabric-visible direct data-route discovery without putting
large or latency-sensitive payloads through the Fabric.

Documents 00-05 retain the v0.2 foundations and incorporate the v0.3.11
safety-critical process-escalation and layered-authority-lineage rules.
Documents 06-10 add the interfaces required for camera/IMU pose tracking and
world-frame spatial visualization.

The finite-Skill contract also defines the depth-backed general effector-front
landmark boundary and separates it from task-specific action geometry.
