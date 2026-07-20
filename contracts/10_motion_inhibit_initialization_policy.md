# Motion Inhibit Policy for Sensor Initialization

Status: v0.3 working draft.

The Manager exposes acquire, status, and release operations for motion-inhibit leases. Each lease has an owner, reason, optional related Skill, creation time, and expiry policy when introduced.

The Initialize Space Cognition Skill acquires an inhibit before collecting stationary accelerometer samples and keeps it until VIO reaches its initialized tracking state or the Skill fails. Release occurs in a guaranteed cleanup path.

With no motion hardware present, the Manager reports that no motion Provider is available while still publishing the inhibit state. Future wheel, arm, base, and neck Providers must subscribe to or query the canonical inhibit state and reject new motion commands while any valid lease exists. Emergency and safety-stop semantics remain higher priority.

Motion inhibit is not a substitute for Control Authority Leases. It is a global coordination constraint used during calibration and initialization; authority leases still determine who may command an actuator when movement is permitted.
