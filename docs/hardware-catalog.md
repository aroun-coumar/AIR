# Hardware catalog

AIR ships a typed catalog of the robots, devices and protocols it can describe:

| | Count | Examples |
|---|---|---|
| Robot classes | 100 | articulated, SCARA, delta, cobot · AGV, AMR, forklift, mobile manipulator · quadruped, biped, humanoid, exoskeleton · multirotor, fixed-wing, VTOL · USV, ROV, AUV · agricultural, construction, surgical, cleaning, inspection crawlers, space |
| Device types | 648 | BLDC/PMSM/stepper/servo/linear motors, QDD and strain-wave joint modules, hydraulics, pneumatics · drives and ESCs · gearboxes, screws, brakes · encoders, IMUs, GNSS, lidar, radar, cameras, F/T, tactile, environment, marine sensors · batteries, BMS, chargers, converters · MCUs, SoMs, PLCs, safety PLCs · radios, gateways, switches · IoT sensors, meters, locks, trackers · docks, doors, elevators, conveyors · grippers, hands, tools · pendants, displays · e-stops, light curtains, safety relays |
| Protocols | 122 | PWM, step/dir, BiSS-C, EnDat · I2C, SPI, UART, RS-485, USB, MIPI CSI-2, GMSL · CAN, CAN FD, CANopen, J1939, DroneCAN, Cyphal · EtherCAT, PROFINET, EtherNet/IP, Modbus, IO-Link, FSoE, PROFIsafe, CIP Safety · Ethernet, TSN, Wi-Fi, 5G, BLE, Zigbee, Thread, LoRaWAN, UWB · MQTT, CoAP, LwM2M, OPC UA, Matter, MAVLink · ROS 2, DDS, Zenoh, micro-ROS · VDA 5050, Open-RMF |

Run `air hardware stats` for the current numbers.

## Use it

```bash
air hardware types lidar
```

```bash
air hardware types --category safety
```

```bash
air hardware types --interface fieldbus.ethercat
```

```bash
air hardware show robot.legged.quadruped
```

```bash
air hardware template robot.mobile.amr --robot-id amr-7 > amr-7.manifest.json
```

```bash
air hardware check examples/robots/*.json --strict
```

```bash
air hardware plan examples/robots/amr.manifest.json
```

`check` reports:
- unknown types or robot classes
- missing required properties
- wrong units or dimensions, and invalid enum values
- interfaces a type is not known to speak
- robot-class slots with too few or too many components

`plan` lists the telemetry limits and maintenance items that follow from the hardware.

In a manifest (`ar.component_manifest/1.1`):

```json
{"component_id": "battery", "kind": "Traction battery", "revision": "r1",
 "type_id": "power.battery.lfp_pack", "vendor": "…", "model": "…",
 "properties": {"nominal_voltage": {"value": 48, "unit": "V"},
                "capacity": 40, "max_discharge_current": 80}}
```

A bare number is in the property's declared unit. `{"value", "unit"}` is converted and
checked for dimension.

The examples in `examples/robots/` cover a quadruped, a quadcopter, an AMR and a cobot.

## AIR never requires hardware

The catalog describes robots; it does not prescribe them. `air hardware check` reports
two kinds of finding:

| Kind | Examples | Level |
|---|---|---|
| Data errors | unknown type id, a required value missing for the type you chose, wrong unit, a document that changed after it was attached | always an error |
| Composition advice | fewer parts than a robot class typically has, domain rules ("a lithium pack usually has a BMS"), untyped parts | a warning; an error only with `--strict` |

A robot built differently is still a valid robot, and nothing in the runtime needs a
catalog entry.

## Context: semantics, domains, tasks, custom parts

**Custom components:**
- `extends` lets a vendor type specialize a built-in one. A specialization may tighten
  its parent, but never clear its safety flag or loosen a safety-relevant limit.
- In a manifest, a component can carry:
  - `limits` (this unit's datasheet values, with a reason)
  - `documents`, pinned by digest: `air hardware attach manifest.json wheel-left datasheet.pdf --sensitivity restricted`
  - typed `relations`: `powered_by`, `drives`, `senses`, `protects`, `controls` and others
  - a `sensitivity`
- Every limit says where it came from.

**Semantics:**
- Units link to QUDT, categories to W3C SOSA/SSN and ETSI SAREF, and IoT types to their
  OMA LwM2M object URN and Matter device-type id.
- `air hardware export sensor.imu.mems_6axis` writes a W3C WoT Thing Model.
- `air hardware export robot.manifest.json` writes the robot as a JSON-LD graph.

**Domain packs** (D01 to D19):
- Each has vocabulary, standards, invariants, a failure taxonomy, recurring checks and
  declarative rules.
- `air hardware brief D02 D14` briefs an agent or a person.
- `check` applies the rules as advice.

**Task context:**

```bash
air hardware context robot.manifest.json --text "left wheel overheating" --observe "wheel-left.winding_temperature=128 degC"
```

It returns the facts that matter for the task, ranked and within a budget, each with
its source. `--no-restricted` withholds restricted facts. Work agents get the same thing
through the `hardware_context` tool, which withholds restricted facts unless every model
stays on the device.

See [ADR 0011](decisions/0011-hardware-semantics-and-context.md).

## Extend it

Put JSON files in your project's `.air/hardware/`. They are merged with the built-in
catalog. Use your own sub-prefix, for example `sensor.acme.*`. An id that already exists
is an error: an extension adds types, but never silently redefines one.

### Authoring format

A file may contain `device_types`, `robot_classes` and `protocols`.

```json
{"device_types": [{
  "type_id": "actuator.motor.bldc_outrunner", "name": "…", "category": "actuator",
  "description": "…", "domains": ["D01", "D12"], "aliases": ["…"],
  "properties": [{"name": "rated_voltage", "kind": "quantity", "unit": "V", "required": true}],
  "interfaces": ["electrical.three_phase"], "provides": ["torque", "rotation"],
  "telemetry": [{"name": "winding_temperature", "unit": "degC",
                 "warn_above": 100, "critical_above": 130}],
  "maintenance": [{"item": "bearings", "basis": "hours", "interval": 20000,
                   "action": "inspect, replace on noise"}],
  "failure_modes": ["bearing wear"], "safety_relevant": false,
  "typical_parents": ["robot.*"], "references": [{"name": "IEC 60034-1"}]}]}
```

The fields, with their allowed values:

| Field | Allowed values |
|---|---|
| Ids | Lowercase, dotted, `[a-z0-9_]` segments |
| Device-type prefixes | `actuator`, `drive`, `transmission`, `sensor`, `power`, `compute`, `thermal`, `comms`, `iot`, `infra`, `end_effector`, `mechanical`, `hmi`, `safety` |
| Robot-class prefix | `robot.` |
| Property `kind` | `quantity` (needs a unit from `agentic_robotics.core.units.known_units()`), `number`, `integer`, `string`, `enum` (needs `values`), `bool` |
| Maintenance `basis` | `hours`, `cycles`, `distance_km`, `calendar_days`, `condition` |
| `domains` | AIR domain ids `D01`–`D19` |

Patterns in robot-class slots:
- Globs work: `sensor.lidar.*`.
- A bare id also matches its children: `sensor.imu` matches `sensor.imu.mems_6axis`.
- A slot may name robot classes, for sub-robots.

`DeviceCatalog.problems()` (also run by `air hardware stats --strict`) checks all of this.

Telemetry limits are typical values for monitoring, not safety limits. A datasheet or
the manifest takes precedence. References are pointers to check, not compliance claims.
See [ADR 0010](decisions/0010-hardware-catalog.md).
