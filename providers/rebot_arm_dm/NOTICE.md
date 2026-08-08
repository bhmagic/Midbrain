# Notice

This package contains original integration code and configuration assembled for the Physical AI project. It does not bundle MotorBridge, the official reBot Arm Python repository, ROS, MoveIt, Unity, or third-party mesh assets.

Nominal reBot kinematic and inertial values are transcribed from the public fixed-end URDF in `vectorBH6/reBotArm_control_py`. Motor command names and signatures follow the public MotorBridge 0.5.1 API surface plus the additive state-generation/receive-age patch retained under `third_party/`. Verify the licenses of those upstream projects before redistributing their code or assets with this kit.
