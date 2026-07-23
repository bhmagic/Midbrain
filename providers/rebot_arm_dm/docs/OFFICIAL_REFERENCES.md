# Reference implementation notes

- Official reBot Arm Python examples: `https://github.com/vectorBH6/reBotArm_control_py`
- MotorBridge Python interface and Damiao modes: `https://github.com/NoBody-114514/motorbridge`
- Supplied Unity bridge: `ReBotArm_Unity_Bridge_Final_Bundle_2026_06_21`

The nominal kinematic origins, masses, centers of mass, inertia tensors, and fixed-tool transform in `arm_model.factory.json` were transcribed from the official fixed-end URDF. The first three configured motor models are DM-J4340P and the remaining four are DM-J4310 in the official configuration. Physical identity and revision must be checked on the installed arm.
