# 0010. A typed hardware catalog for robots, devices and protocols

Status: Accepted
Date: 2026-10-22

## Context
The owner pointed out that AIR covered only a few kinds of robots, motors, IoT devices
and subcomponents. Before this change, a manifest component was just a free-text `kind`
string. AIR could not say:
- what a "harmonic_drive" or "battery_pack" is
- which properties it must state, and in which units
- which interfaces it speaks
- what to monitor, what wears out, or whether it is safety relevant

Requirement REQ03 (coverage of the 19 engineering domains D01–D19) had no data behind it.

## Decisions
1. **A data catalog, not code** (`agentic_robotics.hardware`, contract
   `ar.device_catalog/1.0`). Three kinds of entry:
   - **DeviceType** (`actuator.motor.bldc_outrunner`), with:
     - typed properties: quantity with a unit, number, integer, enum, string or bool, marked required or not
     - interfaces it can speak (protocol ids) and capabilities it provides
     - telemetry signals with typical warn and critical limits
     - maintenance items: basis and interval
     - failure modes, a safety-relevant flag, typical parents and references
   - **RobotClass** (`robot.legged.quadruped`): slots with type patterns and min/max
     counts, plus capabilities, interfaces and safety standards. A slot may hold a whole
     robot; for example, a mobile manipulator holds an industrial arm.
   - **Protocol** (`fieldbus.ethercat`): layer, medium, real-time class, typical rate,
     topology, security properties and standards.
2. **Breadth from the start.** The built-in catalog has:
   - 648 device types in 14 categories (actuator, drive, transmission, sensor, power,
     compute, thermal, comms, iot, infra, end_effector, mechanical, hmi, safety)
   - 100 robot classes: industrial, mobile, legged, humanoid, wearable, aerial, marine,
     field, service, medical, logistics, inspection and others
   - 122 protocols: electrical, buses, CAN family, fieldbuses including the safety
     protocols, networks and radios, application protocols, middleware and fleet interfaces

   Where an entry corresponds to an open registry, it cites it: OMA LwM2M/IPSO object
   ids, CSA Matter device type ids (from the open-source connectedhomeip data model),
   ROS REPs and the W3C WoT Thing Description. Standards are cited by name. Writers
   omitted a reference when they were unsure of it.
3. **The core stays stdlib-only.** The catalog is JSON shipped inside the package
   (`hardware/data/*.json`). Projects extend it with `.air/hardware/*.json`. Ids may
   never collide: a project can add types, but it cannot silently redefine a built-in
   one.
4. **Manifests reference the catalog** (`ar.component_manifest/1.1`, a minor bump).
   - Components gain optional `type_id`, `vendor`, `model` and `properties`. A property
     is a number in the declared unit, or `{"value", "unit"}`, which is checked for
     dimension.
   - Manifests gain an optional `robot_class`.
   - Old manifests stay valid; their untyped components produce warnings, which become
     errors under `--strict`.
5. **The catalog checks itself.** `DeviceCatalog.problems()` checks:
   - id format and prefixes
   - known units and domains
   - enum values present, and threshold ordering
   - every interface is a defined protocol
   - every slot pattern matches something

   The built-in catalog has zero problems, and a test keeps it that way.
6. **The catalog feeds monitoring and maintenance.** `telemetry_limits` and
   `maintenance_plan` turn a manifest into per-component thresholds and maintenance
   items, with safety-relevant items first. These are the starting point for the
   planned health work (W3.7) and for triage.

7. **The catalog never requires hardware** (owner decision). Robot-class slots are a
   class's *typical* composition. A manifest that differs gets warnings, which become
   errors only with `--strict`. Errors are kept for data that is wrong as written. See
   [ADR 0011](0011-hardware-semantics-and-context.md).

## Consequences
- `air hardware types | classes | protocols | show | check | plan | template | stats`.
- Telemetry limits in the catalog are *typical* values. A device's datasheet or the
  manifest overrides them. They are not safety limits, and the runtime does not act on
  them by itself.
- Catalog content was written by several researchers in parallel, against one
  [authoring format](../hardware-catalog.md#authoring-format). Their cross-references
  were normalized by a build step. Some standards were cited from domain knowledge and
  not checked online. Treat references as pointers to verify, not as compliance claims.
- Adapters for these devices are separate work: the catalog says what a device *is*, not
  how to drive it. The planned Unitree (DDS), AgileX Piper (CAN) and Microduck adapters
  should declare `type_id`s from this catalog.
