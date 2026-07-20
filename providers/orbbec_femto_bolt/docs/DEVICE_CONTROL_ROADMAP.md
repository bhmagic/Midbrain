# Femto Bolt Device-Control Roadmap

The sensory data plane is separate from operations that change device state.

| SDK area | Proposed platform form |
|---|---|
| Exposure, gain, white balance, laser/flood controls | Versioned provider command with range discovery and rollback |
| Recording | Separate recorder Resource Provider consuming Fabric/BufferRefs |
| Playback | Playback Resource Provider with deterministic timestamps |
| Triggered capture | Camera command plus hardware wiring/status observation |
| Device-time synchronization | Time-sync command and clock-domain observation |
| Multi-camera synchronization | Coordination Skill or dedicated synchronization Provider |
| Firmware update | Offline administrative tool; never an agent-autonomous default command |
| Post-processing filters | Derived processing Provider or explicit camera profile |
| Hot-plug recovery | Manager restart policy plus provider device-change handling |

These features should be added only with permissions, deadlines, audit records, and explicit operator control where hardware state can change.
