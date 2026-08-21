# Grip Work Runtime

This non-discoverable support package implements the signed Provider protocols shared by the four finite carrying Skills. It does not own robot state or background loops. Persistent gripper and carrying behavior remains in the Grip and Contact Providers.

Agent discovery loads only the standard-library bridge in `host_bridge.py`.
Each finite workflow then runs through its owning Skill's `.venv`; the bridge
exposes only an allowlisted Manager and Integrated RPC surface. Grip, Contact,
geometry, and workflow dependencies are never imported into the Agent
interpreter.

The shared Grip runtime also owns the fail-safe release sequence used when a
normal grip reaches its close endpoint without stable contact: command the
functional-open position, verify measured openness, and only then transition
the gripper to MIT float. Individual Skills remain responsible for relaxing
their own Contact session and reporting the unsuccessful task result.
