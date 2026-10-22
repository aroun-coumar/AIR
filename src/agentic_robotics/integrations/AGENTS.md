# `agentic_robotics.integrations`

Reference and bridge adapters behind the public `RobotAdapter` protocol. Nothing in the core imports this package.

## Files

- `fake.py`, `fake_arm.py`, `world.py`: dependency-free reference adapters
- `common.py`: shared bridge helpers (capability revisions, observations, interrupt gate)
- `microduck/`: robotd JSON-RPC/NDJSON (G02 second integration)
- `unitree/`: unitree_sdk2 DDS channel model (injected channel layer)
- `piper/`: AgileX Piper over CAN (injected CAN layer)
- `ros2/`: ROS 2 actions; verification is the action result
- `vda5050/`: VDA 5050 v2/v3 orders and instant actions over MQTT
- `openpi/`, `lerobot/`: policy clients with timeouts and staleness bounds
- `dora/`: AIR skills as a dora operator; dataflow validation

## Rules

- Every bridge takes an injected transport and ships a fake peer that speaks the verified wire protocol; tests never touch a device.
- Third-party SDKs only behind extras and imported lazily.
- Register explicitly (`<bridge>.register()`) or via entry points; never through import side effects.
- Record in ADR 0019 which upstream files a wire model was read from.

## Tests

tests/test_bridge_*.py, tests/test_platform.py

## Before you change anything here

- Read the root [`AGENTS.md`](../../../AGENTS.md) and [`CLAUDE.md`](../../../CLAUDE.md).
- Tag new modules with `__requirements__ = ("REQxx", ...)` and tests with
  `@pytest.mark.req(...)`.
- All five checks pass before a commit: pytest, ruff, mypy (strict), `air schema check`,
  `air requirements matrix --check`.
